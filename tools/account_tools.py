# ============================================================
# 账户管家工具集（个人中心能力 → OpenAI 兼容 function calling）
# ------------------------------------------------------------
# 与 tools/business_tools.py 同一约定（同一份 tools_to_openai 序列化）：
#   1. 用户身份由调用方（Orchestrator）注入，绝不经过 LLM 生成；
#   2. 金额 / 余币 / 可退金额一律以 cards 表与持卡记录为权威，模型不改数；
#   3. 工具返回人类可读字符串，模型只负责汇总与决策。
#
# 补齐的能力（此前只有 REST 接口，AI 够不着，只能"念规则"不能"办事"）：
#   - get_my_orders            我的办卡/充值订单（支付后最常问"到账了吗"）
#   - get_my_card_detail       我的卡明细（余币/生效/是否可退）
#   - check_refund_eligibility 退卡资格核查（读，给出可退金额与不可退原因）
#   - request_refund_card      自助退卡（写，必须用户确认）
#   - get_redeem_qrcode        会员码 / 提币码（对话内出码）
#   - get_my_consumptions      我的消费明细
#   - list_my_favorites        我的收藏
#   - get_notices              园区公告 / 今日热点
#   - get_my_profile           我的账号资料（只读，手机号脱敏）
#
# 延迟约束（项目硬要求：检索速度不能变慢）：
#   本模块 9 个工具的 schema 会进入 prompt，**仅当本轮问句命中
#   is_account_query() 时才由 business_agent 挂载**，普通知识问答的
#   prompt 与工具列表保持改动前原样，零额外开销。
# ============================================================
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 账户意图触发词（路由与工具挂载的唯一真源）
# 说明：business_agent 已有的余额/卡类词表保持不动，这里是"账户管家"新增的
# 订单 / 退卡 / 会员码 / 明细 / 收藏 / 资料 语义；两边取并集。
# ------------------------------------------------------------------
ACCOUNT_TRIGGER_WORDS = [
    # 订单与到账
    "订单", "下单", "付了", "付款", "支付", "到账", "没到账", "扣款", "买了卡",
    "买的卡", "什么时候到", "订单号", "交易", "消费记录", "消费明细", "账单",
    # 退卡
    "退卡", "退款", "能退", "可以退", "能不能退", "怎么退", "想退", "退掉",
    # 会员码 / 提币
    "会员码", "提币", "出码", "二维码", "核销码", "扫码", "出个码",
    # 收藏与个人资料
    "我的收藏", "收藏了", "收藏的内容", "我的资料", "我的账号", "我的信息",
    "我的手机号", "绑定的手机",
    # 卡明细
    "我的卡", "卡明细", "持卡", "剩多少币", "卡里还有",
    # 园区公告 / 活动（由 get_notices 工具回答；不命中就掉进 RAG 由模型猜）
    "公告", "通知", "有什么活动", "什么活动", "近期活动", "最近活动", "园区活动",
    "今日热点", "有什么新鲜", "上新",
]

# ------------------------------------------------------------------
# "政策/规则咨询" vs "在自己账户上办事"
# ------------------------------------------------------------
# "退卡规则是什么""退卡政策/流程"属**知识问答**：知识库里有确定答案、可被 Redis 缓存，
# 走账户管家反而答不好（工具只能查到"你这张卡能不能退"，答不了"政策是什么"），
# 而且会白白多跑一轮工具循环。这与 business_agent 里对"怎么办卡"（流程咨询）
# 的既有处理是同一个套路。
# 但一旦带上"在我的账户上办事"的信号（我的/帮我/想退/这张卡…），仍走账户管家。
# ------------------------------------------------------------------
_POLICY_WORDS = ["规则", "政策", "规定", "制度", "流程", "步骤", "怎么规定",
                 "是什么", "有哪些", "什么条件", "需要什么", "多少天", "几天",
                 "能不能退", "可以退吗", "怎么退", "如何退"]
_TRANSACTION_WORDS = ["我的", "帮我", "给我", "我要", "我想", "想退", "要退", "退掉",
                      "退这张", "这张卡", "这卡", "我这张", "我能不能", "我能退",
                      "退多少", "能退多少", "我有哪些", "出码", "会员码", "提币",
                      "到账", "没到账", "还没到", "没到", "扣款", "扣了"]


def is_account_query(query: str) -> bool:
    """本轮问句是否属"账户管家"范畴（订单/退卡/会员码/明细/收藏/资料）。

    只按本轮问句判断，不拼历史：历史是多轮长文本，一旦聊过"我的卡"，
    后续任何问句都会被判成账户类，白白多挂 9 个工具、多一轮 LLM 往返。
    """
    text = (query or "").strip()
    if not text:
        return False
    if not any(w in text for w in ACCOUNT_TRIGGER_WORDS):
        return False
    # 政策/规则咨询且不带"在账户上办事"的信号 → 交 RAG（知识库有答案且可缓存）
    if any(w in text for w in _POLICY_WORDS) and not any(w in text for w in _TRANSACTION_WORDS):
        return False
    return True


# ------------------------------------------------------------------
# 展示辅助
# ------------------------------------------------------------------
_ORDER_STATUS_CN = {
    "pending": "待支付",
    "paid": "已支付",
    "closed": "已关闭",
    "refunded": "已退款（已退卡）",
}
_CARD_KIND_CN = {"member": "会员充值卡", "small": "小额充币"}


def _yuan(amount_cent) -> str:
    """分 → 元字符串（老板据一律以分为单位存储）"""
    try:
        return f"{int(amount_cent) / 100:.2f}"
    except Exception:
        return "0.00"


def _mask_phone(phone: str) -> str:
    """手机号脱敏：138****1234（工具输出会进 prompt 与日志，不做全量回显）"""
    p = (phone or "").strip()
    return p[:3] + "****" + p[-4:] if len(p) >= 7 else (p or "未绑定")


def _dt(value) -> str:
    """时间字段统一取到分钟（DB 可能是 datetime 或字符串）"""
    s = str(value or "").strip()
    return s[:16] if s else "—"


def _refundable_member_cards(user_id: int) -> list:
    """退卡资格核算（唯一真源，供核查与执行共用）。

    退卡政策（与 api/pay.py:391 保持一致，改政策时两处都要动）：
      1. 小额充币（kind=small）已直接进余币，不支持退卡；
      2. 会员卡剩余币数 ≥ 卡面值（即未使用过）才可退，退款额为支付原价；
      3. 仅"已支付"订单可退，无订单号的卡需联系工作人员。

    Returns: [{uc, card_name, face, remaining, price_yuan, eligible, reason}]
    """
    from database.db import get_card, get_card_order_by_no, list_user_cards
    from tools.business_tools import _parse_coins

    out = []
    for uc in list_user_cards(user_id) or []:
        kind = uc.get("kind") or "member"
        card_name = uc.get("card_name") or "储值卡"
        card = get_card(uc.get("card_id"))
        face = _parse_coins(card["name"], card.get("description", "")) if card else 0
        remaining = int(uc["coins"]) if uc.get("coins") is not None else face

        order_no = uc.get("order_no") or ""
        order = get_card_order_by_no(order_no) if order_no else None
        price_yuan = _yuan(order["amount"]) if order else "0.00"

        eligible, reason = False, ""
        if kind == "small":
            reason = "小额充币已直接存入余币，不支持退卡"
        elif not order_no or not order:
            reason = "该卡没有对应订单，请联系现场工作人员处理"
        elif face <= 0:
            reason = "卡面值无法核定，请联系现场工作人员"
        elif str(order.get("status")) == "refunded":
            reason = "该订单已退过款"
        elif str(order.get("status")) != "paid":
            reason = "仅已支付订单可退"
        elif remaining < face:
            reason = (f"已使用过（剩余 {remaining} 币 < 面值 {face} 币），"
                      f"按园区政策不能退，可到园区继续游玩抵扣")
        else:
            eligible = True
            reason = f"未使用过，可退 ¥{price_yuan}"

        out.append({
            "uc_id": uc.get("id"),
            "card_name": card_name,
            "kind": kind,
            "kind_cn": _CARD_KIND_CN.get(kind, kind),
            "face": face,
            "remaining": remaining,
            "price_yuan": price_yuan,
            "eligible": eligible,
            "reason": reason,
            "order_no": order_no,
            "in_use": bool(uc.get("in_use")),
        })
    return out


# ============================================================
# 工具构建
# ============================================================
def get_account_tools(user: Optional[dict] = None, ctx: Optional[dict] = None) -> list:
    """
    构建"账户管家"工具清单（OpenAI tools 格式 + 可执行函数）。

    Args:
        user: dict {user_id, username, role}，来自 JWT（get_optional_user）
        ctx:  请求级上下文；本模块会把生成的会员码图片追加到
              ctx["images"] = [{"title","url"}]（避免全局单例并发串台）

    Returns:
        [{name, description, parameters, run}]，run(args: dict) -> str
        未登录时工具照常挂载，但 run 一律返回"需先登录"提示（模型据此引导登录，
        不会编造个人数据）。
    """
    user_id = (user or {}).get("user_id")
    is_logged = user_id is not None

    def _need_login() -> str:
        return ("当前对话未携带登录身份，查不到个人账户数据。请先在微信里登录小程序，"
                "再问我「我的订单/我的卡/退卡/会员码/我的收藏」。")

    # ---------- 1. 我的订单 ----------
    def _run_get_my_orders(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            from database.db import list_user_orders
            limit = int(args.get("limit") or 5)
            orders = list_user_orders(user_id, limit=min(max(1, limit), 20)) or []
            if not orders:
                return "你还没有办卡/充值订单记录。目前可以先问我「有哪些卡」了解在售卡产品。"
            lines = [f"共查到你最近 {len(orders)} 笔办卡/充值订单："]
            for o in orders:
                st = _ORDER_STATUS_CN.get(str(o.get("status")), str(o.get("status") or "未知"))
                when = _dt(o.get("paid_at") or o.get("created_at"))
                lines.append(
                    f"- {o.get('card_name') or '储值卡'}｜¥{_yuan(o.get('amount'))}｜"
                    f"{st}｜{when}｜单号 {str(o.get('order_no') or '')[-8:]}"
                )
            lines.append("（单号只显示后 8 位；如需完整单号可到「我的-账单」查看）")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover - 数据库异常兜底
            logger.warning(f"查询订单失败: {e}")
            return f"查询订单失败：{str(e)[:80]}"

    # ---------- 2. 我的卡明细 ----------
    def _run_get_my_card_detail(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            from database.db import bonus_available, list_user_cards
            from tools.business_tools import _user_coins_total
            cards = _refundable_member_cards(user_id)
            card_total = _user_coins_total(user_id)
            bonus = bonus_available(user_id)
            if not cards:
                return (f"你名下目前没有生效中的卡。可用余币 {card_total + bonus} 币"
                        f"（其中签到所得 {bonus} 币）。")
            lines = [f"你名下共 {len(cards)} 张生效卡，可用余币合计 {card_total + bonus} 币"
                     f"（办卡/充值 {card_total} 币 + 签到所得 {bonus} 币）："]
            for c in cards:
                flags = []
                if c["kind"] == "small":
                    flags.append("直接入余币")
                else:
                    flags.append("会员码生效中" if c["in_use"] else "未选为生效卡")
                flags.append("可退卡" if c["eligible"] else "不可退")
                lines.append(
                    f"- {c['card_name']}（{c['kind_cn']}）：剩余 {c['remaining']} 币"
                    f"｜{'、'.join(flags)}｜单号 {str(c['order_no'] or '')[-8:]}"
                )
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询卡明细失败: {e}")
            return f"查询卡明细失败：{str(e)[:80]}"

    # ---------- 3. 退卡资格核查（读） ----------
    def _run_check_refund_eligibility(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            cards = _refundable_member_cards(user_id)
            member_cards = [c for c in cards if c["kind"] != "small"]
            if not member_cards:
                return ("你名下没有可办理退卡的会员卡（小额充币不支持退卡）。")
            ok = [c for c in member_cards if c["eligible"]]
            lines = []
            if ok:
                total = sum(float(c["price_yuan"]) for c in ok)
                lines.append(f"✅ 你有 {len(ok)} 张卡符合退卡条件，可退合计 ¥{total:.2f}：")
                for c in ok:
                    lines.append(f"- {c['card_name']}：剩余 {c['remaining']} 币（未使用过），"
                                 f"可退 ¥{c['price_yuan']}")
                lines.append("⚠️ 退卡后该卡立即失效、余币同步扣减且不可恢复。"
                             "请向用户复述可退金额并明确询问是否确认；"
                             "用户回复确认后，再调用 request_refund_card（带 user_confirmed=true）。")
            else:
                lines.append("❌ 你名下的会员卡目前都不满足退卡条件：")
            for c in member_cards:
                if not c["eligible"]:
                    lines.append(f"- {c['card_name']}：{c['reason']}")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"退卡资格核查失败: {e}")
            return f"退卡资格核查失败：{str(e)[:80]}"

    # ---------- 4. 自助退卡（写，需用户确认） ----------
    def _run_request_refund_card(args: dict) -> str:
        if not is_logged:
            return _need_login()
        # 双重保护：schema 层要求 user_confirmed，代码层再校验一次，
        # 避免模型"顺手"直接退卡（真实退款不可逆，必须用户先明确确认）。
        if str(args.get("user_confirmed")).lower() not in ("true", "1", "yes"):
            return ("⛔ 未执行退卡：退卡不可逆，必须先让用户明确回复确认。"
                    "请先调用 check_refund_eligibility 告知可退金额，再询问"
                    "「是否确认退卡？」，用户确认后再以 user_confirmed=true 调用本工具。")
        try:
            from database.db import refund_order
            cards = _refundable_member_cards(user_id)
            candidates = [c for c in cards if c["eligible"]]
            if not candidates:
                return ("当前没有可退的卡。请先调用 check_refund_eligibility 查看原因，"
                        "不要自行判断。")
            target_name = str(args.get("card_name") or "").strip()
            chosen = None
            if target_name:
                chosen = next((c for c in candidates if target_name in c["card_name"]), None)
                if chosen is None:
                    names = "、".join(c["card_name"] for c in candidates)
                    return (f"没有找到名为「{target_name}」且符合退卡条件的卡。"
                            f"当前可退的卡有：{names}。请与用户确认要退哪一张。")
            elif len(candidates) == 1:
                chosen = candidates[0]
            else:
                names = "、".join(f"{c['card_name']}（可退 ¥{c['price_yuan']}）" for c in candidates)
                return (f"你有 {len(candidates)} 张卡都符合退卡条件：{names}。"
                        "请先与用户确认要退哪一张，再用 card_name 指定后调用。")

            res = refund_order(chosen["order_no"])
            if not res.get("ok"):
                return f"退卡未成功：{res.get('msg', '未知原因')}，可联系现场工作人员处理。"
            logger.info(f"[账户管家] user={user_id} 自助退卡成功 card={chosen['card_name']} "
                        f"order={chosen['order_no']}")
            return (f"✅ 退卡成功：{chosen['card_name']} 已失效，退款金额 ¥{chosen['price_yuan']}，"
                    f"余币已同步扣减。款项将原路退回（微信支付一般 1~3 个工作日到账）。")
        except Exception as e:  # pragma: no cover
            logger.warning(f"退卡失败: {e}")
            return f"退卡失败：{str(e)[:80]}"

    # ---------- 5. 会员码 / 提币码 ----------
    def _run_get_redeem_qrcode(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            import base64
            import io
            from database.db import get_card, get_user_card, sum_checkin_coins
            from tools.business_tools import _user_coins_total

            coins = _user_coins_total(user_id) + sum_checkin_coins(user_id)
            payload = f"DUCKYARD-REDEEM|uid={user_id}|coins={coins}"
            card_name = ""
            card_id = int(args.get("card_id") or 0)
            if card_id:
                uc = get_user_card(card_id, user_id)
                if uc:
                    card = get_card(uc["card_id"])
                    face = _parse_face(card)
                    c_coins = int(uc["coins"]) if uc.get("coins") is not None else face
                    card_name = uc.get("card_name") or (card["name"] if card else "")
                    payload += f"|card={card_name}|card_coins={c_coins}"

            url = _save_qr_png(payload, user_id)
            # 图片走请求级 ctx，由 Orchestrator 透出给前端（不依赖模型抄长 base64）
            if ctx is not None:
                ctx.setdefault("images", []).append({"title": "会员码", "url": url})

            tip = (f"当前可用余币 {coins} 币。" if not card_name
                   else f"生效卡「{card_name}」共 {coins} 币。")
            if not url:
                # 二维码库缺失时降级为文本码（与 /pay/redeem/qrcode 同策略）
                return (f"{tip}二维码生成不可用，请把下面这串文本码出示给工作人员核验：\n{payload}")
            return (f"{tip}会员码已生成，可在下方查看。请把二维码出示给园区提币机或工作人员扫码核验。")
        except Exception as e:  # pragma: no cover
            logger.warning(f"生成会员码失败: {e}")
            return f"会员码生成失败：{str(e)[:80]}"

    # ---------- 6. 我的消费明细 ----------
    def _run_get_my_consumptions(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            from database.db import list_consumptions_by_user
            limit = int(args.get("limit") or 5)
            today = bool(args.get("today"))
            rows = list_consumptions_by_user(user_id, limit=min(max(1, limit), 20),
                                            today=today) or []
            scope = "今天" if today else "最近"
            if not rows:
                return f"你{scope}还没有游乐消费记录。"
            lines = [f"你{scope}的 {len(rows)} 笔游乐消费："]
            for o in rows:
                items = _items_brief(o.get("items_json"))
                st = {"used": "已核销", "unused": "待核销",
                      "refunded": "已退款"}.get(str(o.get("status")), str(o.get("status")))
                lines.append(f"- {_dt(o.get('created_at'))}｜{items}｜"
                             f"共 {o.get('total_coins')} 币｜{st}")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询消费明细失败: {e}")
            return f"查询消费明细失败：{str(e)[:80]}"

    # ---------- 7. 我的收藏 ----------
    def _run_list_my_favorites(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            from database.db import count_favorites, list_favorites
            limit = int(args.get("limit") or 5)
            total = count_favorites(user_id)
            if not total:
                return "你还没有收藏过回答。看到有用的回答时，点右下角「收藏」就会存进「我的-我的收藏」。"
            items = list_favorites(user_id, limit=min(max(1, limit), 20)) or []
            lines = [f"你共收藏了 {total} 条回答，最近 {len(items)} 条："]
            for it in items:
                lines.append(f"- {_dt(it.get('created_at'))}｜问：{str(it.get('question') or '')[:40]}")
            lines.append("完整内容与附图请在「我的-我的收藏」查看。")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询收藏失败: {e}")
            return f"查询收藏失败：{str(e)[:80]}"

    # ---------- 8. 园区公告 ----------
    def _run_get_notices(args: dict) -> str:
        try:
            from database.db import list_park_notices
            kind = str(args.get("kind") or "").strip()
            limit = int(args.get("limit") or 5)
            rows = list_park_notices(kind=kind, status="on", page=1,
                                     page_size=min(max(1, limit), 20)) or []
            if not rows:
                return "园区当前没有上架的公告或活动。"
            label = {"announcement": "园区公告", "hot": "今日热点"}.get(kind, "公告/热点")
            lines = [f"园区当前上架的{label}："]
            for n in rows:
                title = n.get("title") or ""
                content = str(n.get("content") or "").strip()
                lines.append(f"- {title}" + (f"：{content[:60]}" if content else ""))
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询公告失败: {e}")
            return f"查询公告失败：{str(e)[:80]}"

    # ---------- 9. 我的账号资料（只读） ----------
    def _run_get_my_profile(args: dict) -> str:
        if not is_logged:
            return _need_login()
        try:
            from database.db import get_user_by_id
            u = get_user_by_id(user_id) or {}
            return ("你的账号资料：\n"
                    f"- 昵称：{u.get('nickname') or '未设置'}\n"
                    f"- 用户名：{u.get('username') or '—'}\n"
                    f"- 手机号：{_mask_phone(u.get('phone'))}\n"
                    f"- 注册时间：{_dt(u.get('created_at'))}\n"
                    "（手机号已脱敏。修改昵称/密码请到「我的-账号设置」，"
                    "换绑手机号属敏感操作，需在设置页完成，我无法代为修改。）")
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询账号资料失败: {e}")
            return f"查询账号资料失败：{str(e)[:80]}"

    def _tool(name: str, description: str, params: dict, run) -> dict:
        return {"name": name, "description": description, "parameters": params, "run": run}

    _s = lambda d: {"type": "object", "properties": d, "required": list(d)}
    _no_args = {"type": "object", "properties": {}}
    return [
        _tool("get_my_orders",
              "查询【当前登录用户】的办卡/充值订单（卡名、金额、支付状态、时间、单号后8位）。"
              "用户问\"我的订单/付了钱没到账/买的卡到没到/什么时候发的卡/交易记录\"时调用。",
              {"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "返回条数，默认5，最多20"}},
               "required": []},
              _run_get_my_orders),
        _tool("get_my_card_detail",
              "查询【当前登录用户】名下每张卡的明细：卡名、类型（会员卡/小额充币）、剩余币数、"
              "是否为生效卡、是否可退卡。用户问\"我有哪些卡/卡里还剩多少/哪张卡在用\"时调用。",
              _no_args, _run_get_my_card_detail),
        _tool("check_refund_eligibility",
              "核查【当前登录用户】的退卡资格：逐张说明能不能退、可退多少钱、不能退的原因。"
              "用户问\"我想退卡/能不能退/退卡能退多少钱/怎么退款\"时**必须先调用本工具**再回答，"
              "严禁凭印象说能退或不能退。本工具只查不改。",
              _no_args, _run_check_refund_eligibility),
        _tool("request_refund_card",
              "执行【当前登录用户】的自助退卡（不可逆：卡失效、余币扣减）。"
              "调用前置条件（缺一不可）：① 已调用过 check_refund_eligibility；"
              "② 已把可退金额告诉用户；③ 用户已明确回复确认退卡。"
              "用户还没确认时必须传 user_confirmed=false，本工具会拒绝执行。",
              {"type": "object", "properties": {
                  "card_name": {"type": "string",
                                "description": "要退的卡名（有多张可退卡时必填，与用户确认的那张一致）"},
                  "user_confirmed": {"type": "boolean",
                                     "description": "用户是否已明确确认退卡；未确认必须为 false"}},
               "required": ["user_confirmed"]},
              _run_request_refund_card),
        _tool("get_redeem_qrcode",
              "生成【当前登录用户】的会员码/提币二维码（园内提币机或工作人员扫码核验用）。"
              "用户问\"给我出个码/会员码/提币码/核销码\"时调用。生成后回答里应提示用户查看下方二维码。"
              "只要用户已登录就一定能出码（含名下没有生效储值卡、只有签到币的情况），"
              "**不要因为用户没有卡/币少就拒绝调用**——能不能出码由本工具判断，工具说不行才说不行。",
              {"type": "object", "properties": {
                  "card_id": {"type": "integer",
                              "description": "指定按某张生效卡出码；不传则按账户总余币出码"}},
               "required": []},
              _run_get_redeem_qrcode),
        _tool("get_my_consumptions",
              "查询【当前登录用户】的游乐消费明细（每笔：时间、玩了哪些项目、扣了多少币、是否已核销）。"
              "用户问\"我玩了什么/最近消费记录/今天玩了几个项目/这笔消费核销了吗\"时调用。"
              "（要按周期汇总排行/图表请用 query_consumption_stats 与 generate_stat_charts）",
              {"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "返回条数，默认5，最多20"},
                  "today": {"type": "boolean", "description": "true=只看今天"}},
               "required": []},
              _run_get_my_consumptions),
        _tool("list_my_favorites",
              "查询【当前登录用户】收藏过的 AI 回答（问题与时间；正文请引导用户去「我的-我的收藏」看）。"
              "用户问\"我收藏了什么/我之前收藏的那条/我的收藏\"时调用。",
              {"type": "object", "properties": {
                  "limit": {"type": "integer", "description": "返回条数，默认5，最多20"}},
               "required": []},
              _run_list_my_favorites),
        _tool("get_notices",
              "查询园区当前上架的公告与今日热点/活动（无需登录）。"
              "用户问\"有什么活动/最近有什么通知/园区公告/今天有什么热点\"时调用。",
              {"type": "object", "properties": {
                  "kind": {"type": "string", "enum": ["", "announcement", "hot"],
                           "description": "announcement=园区公告，hot=今日热点，留空=全部"},
                  "limit": {"type": "integer", "description": "返回条数，默认5"}},
               "required": []},
              _run_get_notices),
        _tool("get_my_profile",
              "查询【当前登录用户】自己的账号资料（昵称/用户名/脱敏手机号/注册时间）。"
              "用户问\"我的资料/我的账号/我绑的手机号\"时调用。本工具只读，不能修改任何资料。",
              _no_args, _run_get_my_profile),
    ]


# ------------------------------------------------------------------
# 内部小工具（会员码用，避免依赖 api/pay.py 的私有实现）
# ------------------------------------------------------------------
def _parse_face(card) -> int:
    """按卡名/描述解析卡面值（与 cards 表口径一致）"""
    if not card:
        return 0
    from tools.business_tools import _parse_coins
    return _parse_coins(card.get("name", ""), card.get("description", ""))


def _items_brief(items_json) -> str:
    """consumption_orders.items_json → "旋转木马x1、蹦床x2"（容错解析）"""
    if not items_json:
        return "游乐项目"
    try:
        items = json.loads(items_json) if isinstance(items_json, str) else items_json
        return "、".join(f"{i.get('name', '项目')}x{i.get('qty', 1)}" for i in items) or "游乐项目"
    except Exception:
        return "游乐项目"


def _save_qr_png(payload: str, user_id: int) -> str:
    """把会员码渲染为 PNG 存到 static/qrcode/，返回可访问 URL（失败返回空串）。

    固定文件名（按用户覆盖）：避免每次问都堆一张图占满磁盘。
    """
    try:
        import qrcode
        folder = os.path.join("static", "qrcode")
        os.makedirs(folder, exist_ok=True)
        filename = f"redeem_u{user_id}.png"
        img = qrcode.make(payload)
        img.save(os.path.join(folder, filename), format="PNG")
        return f"/static/qrcode/{filename}?t={int(time.time())}"
    except Exception as e:
        logger.warning(f"会员码二维码渲染失败（降级文本码）: {e}")
        return ""
