# ============================================================
# 消费 / 办卡工具族（让 AI 直接把事办了，不用用户再找按钮）
# ------------------------------------------------------------
# 解决的漏点：用户说「帮我买三个过山车」时，模型手里只有"查价目"没有"买"，
# 于是一步工具都不调 → 上层判定 fallback → 掉回 RAG → 知识库没这条
# → 吐一段「知识库暂无相关文档，以下是 AI 通用建议」的无用文案。
#
# 与前端按钮的关系：共用 services/purchase.py 的下单核心。
# 价格口径、扣币优先级、防重复下单窗口、核销码生成全部是同一份实现，
# 所以 AI 报的价与前端按钮扣的币必然一致，不会分叉。
#
# 挂载策略：与 account / service / weather 三族一致 ——
# **只在本轮问句命中 is_consume_query 时才挂**（见 business_agent._run_tool_loop），
# 普通知识问答的 prompt 与工具列表保持逐字不变，不引入额外首字延迟。
#
# ⚠️ 写这个文件时注意：字符串字面量内部一律用「」而不是 ASCII 双引号，
#    否则会在中文引号处把字符串提前闭合（本项目踩过这个坑）。
# ============================================================
import logging
from typing import Optional

logger = logging.getLogger(__name__)

from tools.business_tools import _not_logged_hint                     # noqa: E402

# ------------------------------------------------------------------
# 触发词表（与「工具是否挂载」同一真源，避免路由认了这类问题、工具却没挂给模型）
# ------------------------------------------------------------------
# 强委托短语：出现即认定「用户在要我下单」，此时即便同时出现「多少钱」也算购买
# （「过山车多少钱一张，帮我买两张」→ 要买）
_STRONG_COMMIT = [
    "帮我买", "帮我购", "帮我下", "帮我订", "帮买",
    "我要买", "我想买", "我要玩", "我想玩", "给我买", "给我来",
    "下单", "帮我办", "我要办", "我想办", "给我办",
    "帮我充", "我要充", "我想充", "给我充",
]
# 一般购买动作词：单独出现也可视为购买
_BUY_WORDS = _STRONG_COMMIT + [
    "购买", "买张", "买两", "买三", "买一", "买几", "买2", "买3",
    "来两", "来三", "来一", "来张", "来几", "办张", "办一张", "办个",
]
# 纯询价形态：用户在问价，不是要下单 → 交 RAG / 办卡推荐（知识库有确定答案且可缓存）
_PRICE_WORDS = ["多少钱", "价格", "票价", "收费", "贵不贵", "多少币", "要多少币",
                "多少元", "什么价", "怎么收费"]
# 政策/流程咨询形态：「退卡规则是什么」「办卡流程有哪些」→ 知识问答，不是办事
_POLICY_WORDS = ["规则", "政策", "规定", "制度", "流程", "步骤", "怎么规定",
                 "是什么", "有哪些", "什么条件", "能不能退", "可以退吗"]


def is_consume_query(query: str) -> bool:
    """本轮问句是否要我们「直接买东西 / 办卡充值」（据此决定挂不挂消费工具族）。

    只按本轮问句判断，不拼历史：历史是多轮长文本，一旦聊过「买/充值」，
    后续任意问句都会被判成消费类，白白多挂工具、多跑一轮工具循环。
    """
    text = (query or "").strip()
    if not text:
        return False
    committed = any(w in text for w in _STRONG_COMMIT)
    # 知识/流程咨询、纯询价：除非同时带强委托，否则不是「要下单」
    if not committed:
        if any(w in text for w in _POLICY_WORDS):
            return False
        if any(w in text for w in _PRICE_WORDS):
            return False
    return any(w in text for w in _BUY_WORDS)


def _project_lines() -> str:
    """在售项目一行式清单（用于「没有这个项目」时如实告知，避免模型自己编项目）。"""
    from services.purchase import list_park_projects_view

    items = list_park_projects_view()
    if not items:
        return "当前没有在售的游乐项目。"
    return "、".join("%s（%d币）" % (p["name"], p["price_coins"]) for p in items)


def _recharge_hint() -> str:
    """余币不足时的充值选项（小额档位在前，方便用户补一个刚好够的）。"""
    from services.purchase import list_onsale_cards

    cards = list_onsale_cards()
    small = [c for c in cards if c["kind"] == "small"]
    member = [c for c in cards if c["kind"] == "member"]
    parts = []
    if small:
        parts.append("小额充值：" + "、".join(
            "%d元→%d币" % (c["price_yuan"], c["coins"]) for c in small))
    if member:
        parts.append("办会员卡：" + "、".join(
            "%d元→%d币" % (c["price_yuan"], c["coins"]) for c in member))
    return "；".join(parts)


def get_consume_tools(user: Optional[dict] = None, ctx: Optional[dict] = None) -> list:
    """构建「消费 / 办卡」工具清单（OpenAI tools 格式 + 可执行函数）。

    Args:
        user: 可选 dict {user_id, username, role}，来自 JWT（服务端注入，不经模型）
        ctx:  请求级上下文（本族工具暂不使用，保持与其它工具族同签名）

    Returns:
        [{name, description, parameters, run}]，run(args: dict) -> str
    """
    user_id = (user or {}).get("user_id")
    is_logged = user_id is not None

    # ---------- 1. 购买游乐项目（写操作：真实扣币） ----------
    def _run_purchase_projects(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        from services.purchase import (available_coins, match_park_project,
                                       purchase_projects)

        raw = args.get("items") or []
        if not isinstance(raw, list) or not raw:
            return "没有解析到要购买的项目。请让用户说清楚要买哪个项目、几个。"

        # 1) 逐个把项目名落到 park_projects 表上的真实 id（模型给的是名字，不是 id）
        merged = {}          # project_id -> {"qty", "name"}
        unknown = []         # [(用户说的名字, 候选列表)]
        for it in raw:
            if not isinstance(it, dict):
                continue
            name = str(it.get("project_name") or it.get("name") or "").strip()
            try:
                qty = int(it.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            if qty < 1 or qty > 99:
                return "数量 %d 不合法（单个项目 1~99 之间），本次没有扣币。" % qty
            m = match_park_project(name)
            if m["project"]:
                pid = m["project"]["id"]
                if pid in merged:
                    merged[pid]["qty"] += qty
                else:
                    merged[pid] = {"qty": qty, "name": m["project"]["name"]}
            else:
                unknown.append((name, m["candidates"]))

        if unknown:
            bad = unknown[0][0]
            if len(unknown[0][1]) > 1:
                hint = ("园区在售项目有：" + _project_lines() +
                        "。请照实告诉用户没有这个项目，并把在售项目列给他，"
                        "问清楚是不是指其中某一个；确认后可以直接帮他下单。")
            else:
                hint = "请照实告诉用户园区没有这个项目。"
            return "❌ 没找到名为「%s」的游乐项目，本次**没有扣币**。%s" % (bad, hint)

        # 2) 先报一次账：模型需要这个数字才能在「买得成/买不成」上讲清楚
        balance_before = available_coins(user_id)

        # 3) 下单（余币校验与扣款都在 services.purchase 内以库为准完成）
        res = purchase_projects(
            user_id,
            [{"project_id": pid, "qty": v["qty"]} for pid, v in merged.items()],
        )

        detail = "、".join("%s × %d" % (v["name"], v["qty"]) for v in merged.values())

        if not res.get("ok"):
            if res.get("insufficient"):
                return ("❌ 余币不足，本次**没有扣币**。%s 共需 %d 币，"
                        "用户当前可用 %d 币，还差 %d 币。\n"
                        "请直接告诉用户「您的余币不足，请充值后再购买」，"
                        "并给出这些充值选项：%s"
                        % (detail, res["total_coins"], res["balance_coins"],
                           res["shortage_coins"], _recharge_hint()))
            return "❌ 下单失败，本次没有扣币：%s" % res.get("msg")

        lines = ["✅ 已直接为用户购买成功（用户无需再去点任何按钮）：", "- " + detail]
        for it in res["items"]:
            unit = "（%s）" % it["unit_desc"] if it.get("unit_desc") else ""
            lines.append("  · %s × %d：%d 币%s" % (it["name"], it["qty"], it["coins"], unit))
        lines.append("- 共扣 %d 币；可用余币 %d → %d 币"
                     % (res["total_coins"], balance_before, res["remaining_coins"]))
        _passes = res.get("passes") or []
        if _passes:
            lines.append("- 已生成 %d 张游园券（一券 = 一个项目玩一次），券码：%s"
                         % (len(_passes), "、".join(p["item_code"] for p in _passes)))
            lines.append("请告诉用户：已经帮他买好了，打开「我的游园码」就能看到这些券，"
                         "到项目现场逐张出示给工作人员，扫一张核销一张。"
                         "**不要**只报一个码就说「整单核销」，也别让他去找购买按钮。")
        else:  # 理论不可达：券包下单必然带券
            lines.append("- 核销码：%s（订单 %s）" % (res["verify_code"], res["order_no"]))
            lines.append("请告诉用户：已经帮他买好了，到项目现场把核销码出示给工作人员即可。"
                         "不要让他再去找购买按钮。")
        return "\n".join(lines)

    # ---------- 2. 办卡 / 充值（写操作） ----------
    def _run_purchase_card(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        from services.purchase import available_coins, match_card, purchase_card

        name = str(args.get("card_name") or args.get("card") or "").strip()
        m = match_card(name)
        if not m["card"]:
            if len(m["candidates"]) > 1:
                opts = "、".join("%s（%d元）" % (c["name"], c["price_yuan"])
                                for c in m["candidates"])
                return ("❌ 没有唯一确定要办哪张卡，本次**没有下单**。在售档位有：%s。"
                        "请让用户说清楚要哪一档（例如「帮我充200」或「办50元的卡」）。" % opts)
            return "❌ 当前没有在售的储值卡。"

        card = m["card"]
        res = purchase_card(user_id, card["id"])
        if not res.get("ok"):
            return "❌ 办卡/充值失败，本次没有扣款：%s" % res.get("msg")

        if res["mode"] == "mock":
            return ("✅ 已直接为用户办理成功：%s，支付 ¥%d，到账 %d 币；"
                    "当前可用余币 %d 币。（订单 %s）"
                    "请告诉用户已经办好了，可以直接去玩。"
                    % (card["name"], res["amount_yuan"], res["coins"],
                       available_coins(user_id), res["order_no"]))

        # 正式微信支付：AI 无法代替用户调起收银台，必须如实说明还差最后一步
        return ("⏳ 订单已创建（%s）：%s，金额 ¥%d，状态待支付。"
                "线上支付需要用户本人在小程序里确认，请告诉他：打开小程序"
                "「办卡充值」页完成付款即可，付完到账 %d 币。"
                % (res["order_no"], card["name"], res["amount_yuan"], res["coins"]))

    def _tool(name: str, description: str, params: dict, run) -> dict:
        return {"name": name, "description": description,
                "parameters": params, "run": run}

    items_schema = {
        "type": "array",
        "description": "要购买的项目清单；同一项目请合并成一个元素",
        "items": {
            "type": "object",
            "properties": {
                "project_name": {
                    "type": "string",
                    "description": "游乐项目名称，如：旋转木马、荡秋千、小型过山车、蹦床、"
                                   "浴室大作战、大炮乐园（用用户说的名字即可）",
                },
                "qty": {"type": "integer", "description": "购买数量，默认 1",
                        "minimum": 1, "maximum": 99},
            },
            "required": ["project_name", "qty"],
        },
    }

    return [
        _tool(
            "purchase_projects",
            "【写操作·真实扣币】直接为用户购买游乐项目并生成核销码。"
            "仅当用户在本轮**明确表达了购买意图**（如「帮我买三个过山车」「我要玩蹦床，"
            "来两张」「帮我下单」）时调用；用户只是在问价格/规则/有哪些项目"
            "（如「过山车多少钱」「办卡流程」）时**绝对不要**调用。"
            "本工具会用 park_projects 表的权威价算钱，并自行核对用户余币："
            "够则直接扣币出核销码，不够则返回明确差额且**不会扣币**。"
            "调用后请把结果照实转述给用户，不要让他再去找购买按钮。",
            {"type": "object", "properties": {"items": items_schema},
             "required": ["items"]},
            _run_purchase_projects),
        _tool(
            "purchase_card",
            "【写操作】直接为用户办理储值卡 / 充值（如「帮我充200」「办张卡」"
            "「我要充值500」）。仅当用户明确要办卡/充值时调用；问「办卡有什么优惠」"
            "「怎么办卡」属知识问答，不要调用。"
            "金额与到账币数以 cards 表为准；已开通微信支付时需要用户本人完成付款，"
            "未开通时（模拟模式）会直接发卡到账。",
            {"type": "object",
             "properties": {"card_name": {
                 "type": "string",
                 "description": "目标卡档位，用用户说的说法即可，如：200、500元、10币"}},
             "required": ["card_name"]},
            _run_purchase_card),
    ]


__all__ = ["get_consume_tools", "is_consume_query"]
