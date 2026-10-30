# ============================================================
# 下单核心（游乐消费 / 办卡充值）
# ------------------------------------------------------------
# 为什么单独抽出来：
#   同一条"花钱"链路现在有两个入口——
#     ① 前端按钮：api/pay.py 的 POST /consume、POST /orders
#     ② AI 对话：tools/consume_tools.py（用户说"帮我买三个过山车"）
#   两处若各写一套下单逻辑，价格口径、扣币优先级、防重复下单窗口迟早分叉，
#   而分叉的代价是真实的币和钱。因此核心收敛在这里，两个入口都只做包装。
#
# 约定：
#   - 价格一律以 cards / park_projects 表为准，绝不采信调用方（前端 / AI 模型）传来的金额
#   - 返回值统一 {"ok": bool, "msg": str, ...}，HTTP 状态码放在 http 字段里由路由层决定
#   - 本模块**不做鉴权**，user_id 由调用方保证（JWT 用户 / 服务端注入）
#   - 延迟导入 api.pay 的纯函数（币数解析、订单号、支付参数签名），
#     保证模块加载期不产生 api.pay <-> services.purchase 的循环导入
# ============================================================
import json
import logging
import secrets
import time as _time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 核销码字符集：去掉易混的 I/O/0/1，方便工作人员口播与手输
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


# ------------------------------------------------------------------
# 读侧：用户可用余币（与前端下单扣减口径完全一致，单一真源）
# ------------------------------------------------------------------
def available_coins(user_id: int) -> int:
    """用户可用余币 = 全部生效持卡（会员卡 + 小额充币）余币 + 签到奖励剩余。"""
    from api.pay import _user_available_coins
    return _user_available_coins(user_id)


# ------------------------------------------------------------------
# 名称匹配：给 AI 工具用（模型看到的是"项目名/卡名"，拿不到数据库 id）
# ------------------------------------------------------------------
def _norm(text: str) -> str:
    """归一化：去空白与常见装饰符，便于"小型过山车"↔"过山车"这类口语匹配。"""
    s = (text or "").strip()
    for ch in (" ", "\u3000", "的", "个", "张", "次"):
        s = s.replace(ch, "")
    return s


def match_park_project(name: str) -> Dict:
    """按名称定位在售游乐项目。

    返回 {"project": 项目 dict | None, "candidates": [候选...], "exact": bool}
      - 唯一命中（含口语简称，如"过山车"→"小型过山车"）→ project 非空
      - 命中多个候选 → project 为空、candidates 供上层要求用户澄清
      - 完全无命中 → project 为空、candidates 为全部在售项目（便于如实告知"没有这个项目"）
    """
    from database.db import list_park_projects

    projects = list_park_projects(status="on") or []
    compact = [{"id": p["id"], "name": p["name"],
                "price_coins": p["price_coins"], "unit_desc": p.get("unit_desc") or ""}
               for p in projects]
    q = _norm(name)
    if not q:
        return {"project": None, "candidates": compact, "exact": False}

    exact = [p for p in compact if _norm(p["name"]) == q]
    if len(exact) == 1:
        return {"project": exact[0], "candidates": exact, "exact": True}

    # 包含匹配：只在候选唯一时采信（"车"会同时命中多个 → 交给用户澄清）
    fuzzy = [p for p in compact if q in _norm(p["name"]) or _norm(p["name"]) in q]
    if len(fuzzy) == 1:
        return {"project": fuzzy[0], "candidates": fuzzy, "exact": False}
    if len(fuzzy) > 1:
        return {"project": None, "candidates": fuzzy, "exact": False}
    return {"project": None, "candidates": compact, "exact": False}


# ------------------------------------------------------------------
# 自由文本找项目：画像抽取与下单匹配共用同一套名称规则
# ------------------------------------------------------------
# 为什么需要它：画像侧原先用 `[w for w in PROJECT_WORDS if w in text]` 纯包含匹配，
# 于是「喜欢过山车」抽不到（词表里是「小型过山车」）、「不喜欢旋转木马」还会被
# 反转成正向兴趣。这里补齐口语别名，并把"取名字"这件事收敛到同一处。
#
# ⚠️ 关键约束：**这条路径必须在问答收尾路径上零 DB 往返**。
#    `core.chat_memory.update_profile_from_exchange` 跑在 SSE 的 done 之前（同步），
#    所以名称清单取自模块常量 ATTRACTIONS（与 user_profile.PROJECT_WORDS 同一来源），
#    不查 park_projects 表。价格口径仍只认库（见 purchase_projects）。
# ------------------------------------------------------------------
_PROJECT_ALIASES = {
    "旋转木马": ("木马",),
    "荡秋千": ("秋千",),
    "小型过山车": ("小过山车",),
    "蹦床": ("蹦蹦床",),
    "大炮乐园": ("大炮",),
}
# "小型过山车" → "过山车" 这类去掉规格前缀的简称（双向子串覆盖不到）
_SPEC_PREFIXES = ("小型", "中型", "大型", "迷你", "超级", "儿童")


def _project_names_for_scan() -> List[str]:
    """项目名清单（供自由文本扫描）。优先结构化价目表常量，导入失败才退回库。"""
    try:
        from tools.business_tools import ATTRACTIONS
        names = [a["name"] for a in ATTRACTIONS if a.get("name")]
        if names:
            return names
    except Exception:
        pass
    return [p["name"] for p in list_park_projects_view()]


def _keywords_for(name: str) -> List[str]:
    """一个项目名 → 可在文本里直接搜的关键词（本名 + 去规格前缀的简称 + 口语别名）"""
    keys = [name]
    for pre in _SPEC_PREFIXES:
        if name.startswith(pre) and len(name) > len(pre):
            keys.append(name[len(pre):])
    keys.extend(_PROJECT_ALIASES.get(name, ()))
    return keys


def find_projects_in_text(text: str) -> List[Dict]:
    """在一段自由文本里找出被提到的项目，按出现位置排序。

    Returns: [{"name": 权威项目名, "start": int, "end": int, "keyword": 命中的说法}]

    纯内存、零 DB —— 可安全用于问答收尾路径与画像抽取。
    """
    t = str(text or "")
    if not t:
        return []
    hits: Dict[str, Dict] = {}
    for name in _project_names_for_scan():
        best = None
        for kw in _keywords_for(name):
            i = t.find(kw)
            if i >= 0 and (best is None or i < best["start"]):
                best = {"name": name, "start": i, "end": i + len(kw), "keyword": kw}
        if best:
            hits[name] = best
    return sorted(hits.values(), key=lambda x: x["start"])


def _canonical_project(name: str) -> str:
    """把口语说法（"木马""过山车"）落到权威项目名；落不到就原样保留（不静默丢弃）"""
    raw = str(name or "").strip()
    if not raw:
        return ""
    try:
        m = match_park_project(raw)
        if m.get("project"):
            return m["project"]["name"]
    except Exception:
        pass
    return raw


def match_card(name: str) -> Dict:
    """按名称/档位金额定位在售储值卡。语义同 match_park_project。

    支持口语："200" / "200元" / "充值200" 都能命中「200元=320币」。
    """
    from database.db import list_cards

    cards = list_cards(status="on_sale") or []
    from api.pay import _parse_coins
    view = [{"id": c["id"], "name": c["name"],
             "price": c["price"], "price_yuan": round(c["price"] / 100, 2),
             "coins": _parse_coins(c["name"], c.get("description", "")),
             "description": c.get("description") or ""}
            for c in cards]
    q = _norm(name)
    if not q:
        return {"card": None, "candidates": view, "exact": False}

    exact = [c for c in view if _norm(c["name"]) == q]
    if len(exact) == 1:
        return {"card": exact[0], "candidates": exact, "exact": True}

    # 金额档位匹配：问句里的数字与该卡价格（元）或面值（币）一致即命中
    digits = "".join(ch for ch in q if ch.isdigit())
    if digits:
        hit = [c for c in view
               if str(int(c["price_yuan"])) == str(int(digits))
               or str(c["coins"]) == str(int(digits))]
        if len(hit) == 1:
            return {"card": hit[0], "candidates": hit, "exact": False}
        if len(hit) > 1:
            return {"card": None, "candidates": hit, "exact": False}

    fuzzy = [c for c in view if q in _norm(c["name"]) or _norm(c["name"]) in q]
    if len(fuzzy) == 1:
        return {"card": fuzzy[0], "candidates": fuzzy, "exact": False}
    if len(fuzzy) > 1:
        return {"card": None, "candidates": fuzzy, "exact": False}
    return {"card": None, "candidates": view, "exact": False}


# ------------------------------------------------------------------
# 游乐消费：多选项目合并扣币 → 一单一券包（一券 = 一项一次，一券一码）
# 扣币优先级：生效中的卡 → 其余生效卡按购买时间先到先扣 → 签到奖励剩余
#
# 为什么要一券一码（2026-09-20 改造）：
#   一单一码时，员工扫到的码只能告诉他"这单买过东西"，看不到明细，忙起来记不住；
#   而"整单一次核销"更危险——买 3 项拆 2 次玩，剩 1 项没玩但单已核销，
#   用户失去凭据、员工也查不出来。拆券后员工扫一张核销一张，零判断成本。
# ------------------------------------------------------------------
def _code_taken(code: str) -> bool:
    """券码是否已被占用。

    整单表（consumption_orders.verify_code）与券表（consumption_items.item_code）
    各有一套码，跨表撞码同样会串单，所以两边都要查。
    """
    from database.db import get_consumption_by_code, get_consumption_item_by_code

    return bool(get_consumption_by_code(code) or get_consumption_item_by_code(code))


def _gen_verify_codes(n: int) -> List[str]:
    """生成 n 个园区内唯一券码（8 位）。

    一单一券包下单要按 项目数 × 数量 出券，所以按 n 批量生成；
    返回值第一个会同时充当整单的 verify_code（老 PC 端按整单码查单、整单核销仍可用）。
    """
    codes: List[str] = []
    for _ in range(n):
        for _ in range(8):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            if code not in codes and not _code_taken(code):
                codes.append(code)
                break
        else:
            raise RuntimeError("核销码生成失败，请稍后重试")
    return codes


def purchase_projects(user_id: int, items: List[Dict]) -> Dict:
    """游乐消费下单（写操作，不可逆地扣减余币）。

    Args:
        user_id: 服务端权威用户 id
        items:   [{"project_id": int, "qty": int}]，价格以 park_projects 表为准

    Returns:
        {"ok": True, "order_no", "verify_code", "items", "passes", "pass_count",
         "total_coins", "remaining_coins", "msg"}
        {"ok": False, "http": 400/404/500, "msg"}
    """
    from database.db import (get_park_project, consume_coins,
                             create_consumption_order, get_consumption_by_code)

    if not items:
        return {"ok": False, "http": 400, "msg": "请至少选择一个游乐项目"}

    # 1. 校验项目并计算合计（价格以 park_projects 表为准，不信调用方）
    #    passes 同步按 qty 展开成券骨架，券码在第 3 步统一生成
    items_view, total, passes = [], 0, []
    seen = {}
    for it in items:
        pid = it.get("project_id")
        try:
            qty = int(it.get("qty") or 1)
        except (TypeError, ValueError):
            return {"ok": False, "http": 400, "msg": "数量必须是整数"}
        if qty < 1 or qty > 99:
            return {"ok": False, "http": 400, "msg": "单个项目数量需在 1~99 之间"}
        if pid in seen:
            return {"ok": False, "http": 400, "msg": "同一项目请合并数量后提交"}
        p = get_park_project(pid) if pid is not None else None
        if not p or p.get("status") != "on":
            return {"ok": False, "http": 404,
                    "msg": f"项目不存在或已下架（id={pid}）"}
        unit_coins = p["price_coins"]
        coins = unit_coins * qty
        total += coins
        seen[pid] = True
        items_view.append({"project_id": pid, "name": p["name"], "qty": qty, "coins": coins,
                           "unit_coins": unit_coins, "unit_desc": p.get("unit_desc") or ""})
        passes.extend({"project_id": pid, "project_name": p["name"],
                       "unit_coins": unit_coins, "item_code": ""} for _ in range(qty))

    # 2. 余币前置校验：不够就别生成订单，直接把差额说清楚
    balance = available_coins(user_id)
    if balance < total:
        return {"ok": False, "http": 400, "insufficient": True,
                "total_coins": total, "balance_coins": balance,
                "shortage_coins": total - balance,
                "msg": f"余币不足：本次需 {total} 币，当前可用 {balance} 币，"
                       f"还差 {total - balance} 币"}

    # 3. 生成券码与单号（先于扣币：扣币失败只浪费几个码，不会丢币）
    #    一单一券包：n = 项目数 × 数量，逐张出码；第一张兼任整单 verify_code
    order_no = f"CF{int(_time.time() * 1000)}{secrets.token_hex(2).upper()}"
    try:
        codes = _gen_verify_codes(len(passes))
    except RuntimeError as e:
        return {"ok": False, "http": 500, "msg": str(e)}
    verify_code = codes[0]
    for _pass, _code in zip(passes, codes):
        _pass["item_code"] = _code

    # 4. 扣币事务（生效卡优先 → 其余卡 FIFO → 签到奖励兜底）
    result = consume_coins(user_id, total)
    if not result.get("ok"):
        return {"ok": False, "http": 400, "msg": result.get("msg", "扣费失败")}

    card_coins = total - result["bonus_used"]
    order = create_consumption_order(
        user_id=user_id, order_no=order_no, verify_code=verify_code,
        items_json=json.dumps(items_view, ensure_ascii=False),
        card_coins=card_coins, bonus_coins=result["bonus_used"], total_coins=total,
        items=passes,
    )
    if not order:
        # 订单/券落库失败：币已扣，必须让上层知道（与前端路径同口径）
        return {"ok": False, "http": 500, "msg": "消费记录写入失败"}

    if not get_consumption_by_code(verify_code):  # 理论不可达，仅作一致性断言
        logger.warning(f"[游乐消费] 核销码校验异常 order={order_no} code={verify_code}")

    logger.info(f"[游乐消费] user={user_id} 扣 {total} 币 | {len(passes)} 张券 | "
                f"首码 {verify_code} | 明细 {items_view}")
    return {
        "ok": True,
        "order_no": order_no,
        "verify_code": verify_code,
        "items": items_view,
        "passes": [{"project_name": p["project_name"], "item_code": p["item_code"]}
                   for p in passes],
        "pass_count": len(passes),
        "total_coins": total,
        "remaining_coins": available_coins(user_id),
        "msg": f"扣费成功，合计 {total} 币",
    }


# ------------------------------------------------------------------
# 办卡 / 充值：下单 → （模拟模式直接发卡 / 正式模式返回微信支付参数）
# ------------------------------------------------------------------
def purchase_card(user_id: int, card_id: int) -> Dict:
    """办卡 / 充值下单（写操作）。

    支付开关见 config.WECHAT_PAY_ENABLED：
      mock（默认，未开通商户号）→ 直接置为已支付并发卡/发币，全流程可体验
      wxpay                     → 返回 wx.requestPayment 所需参数，由前端调起收银台

    Returns:
        {"ok": True, "mode", "order_no", "status", "card_name", "coins",
         "amount_yuan", "reused", "payment"|None, "msg"}
        {"ok": False, "http": 400/401/404/500/502, "msg"}
    """
    from api.pay import (_pay_mode, _gen_order_no, _parse_coins, _card_kind,
                         _seconds_since, _jsapi_unified_order, REPEAT_ORDER_WINDOW)
    from database.db import (get_card, get_user_by_id, create_card_order,
                             update_card_order_status, add_user_card,
                             list_user_orders)

    mode = _pay_mode()

    # 1. 校验卡（价格以库为准，不信调用方）
    card = get_card(card_id)
    if not card or card.get("status") != "on_sale":
        return {"ok": False, "http": 404, "msg": "卡不存在或已下架"}

    # 2. 支付人 openid（仅正式微信支付必需；mock 模式下手机号登录用户无 openid 也可下单）
    user = get_user_by_id(user_id)
    if not user:
        return {"ok": False, "http": 401, "msg": "用户不存在"}
    openid = user.get("wx_openid") or ""
    if not openid and mode != "mock":
        return {"ok": False, "http": 400, "msg": "缺少微信 openid，请重新进入小程序"}

    # 3. 同用户同卡不可在窗口内重复办卡：
    #      · 待支付订单一律复用 —— 用户要继续支付同一笔，不能堆一堆 pending；
    #      · 其余订单只在 REPEAT_ORDER_WINDOW 秒内复用（且不再发卡），超窗才允许重新办卡。
    #    这一步同时堵住了"paid 之后立刻再下一单"的无限发卡路径。
    recent = next(
        (o for o in list_user_orders(user["id"], limit=10) if o["card_id"] == card["id"]),
        None
    )
    if recent and (recent.get("status") == "pending"
                   or _seconds_since(recent.get("created_at")) < REPEAT_ORDER_WINDOW):
        order = recent
    else:
        order = create_card_order(
            order_no=_gen_order_no(), user_id=user["id"], card_id=card["id"],
            card_name=card["name"], amount=card["price"], openid=openid,
        )
    if not order:
        return {"ok": False, "http": 500, "msg": "下单失败，请稍后重试"}

    already_paid = order.get("status") == "paid"
    face_coins = _parse_coins(card["name"], card.get("description", ""))

    # 4. mock：直接支付成功并发卡/发币（已支付过的订单不再重复发卡）
    if mode == "mock":
        if not already_paid:
            order = update_card_order_status(order["order_no"], "paid", transaction_id="MOCK")
            add_user_card(user["id"], card["id"], card["name"], order["order_no"],
                          coins=face_coins, kind=_card_kind(card))
            logger.info(f"[mock支付] 订单 {order['order_no']} 已支付并发卡/发币: "
                        f"{card['name']} ({_card_kind(card)})")
        return {
            "ok": True, "mode": "mock", "order_no": order["order_no"], "status": "paid",
            "card_id": card["id"], "card_name": order.get("card_name") or card["name"],
            "coins": face_coins, "amount_yuan": round(card["price"] / 100, 2),
            "reused": already_paid, "payment": None,
            "msg": f"充值成功：{card['name']}",
        }

    # 5. 正式模式：微信 JSAPI 统一下单（V3）
    if already_paid:
        return {
            "ok": True, "mode": "wxpay", "order_no": order["order_no"], "status": "paid",
            "card_id": card["id"], "card_name": order.get("card_name") or card["name"],
            "coins": face_coins, "amount_yuan": round(card["price"] / 100, 2),
            "reused": True, "payment": None,
            "msg": f"该笔订单已支付：{card['name']}",
        }
    try:
        pay_params = _jsapi_unified_order(order, openid)
    except Exception as e:
        logger.error(f"微信统一下单失败: {e}", exc_info=True)
        return {"ok": False, "http": 502, "msg": "微信下单失败，请稍后重试"}

    return {
        "ok": True, "mode": "wxpay", "order_no": order["order_no"],
        "status": order["status"], "card_id": card["id"],
        "card_name": order.get("card_name") or card["name"],
        "coins": face_coins, "amount_yuan": round(card["price"] / 100, 2),
        "reused": False, "payment": pay_params,
        "msg": f"订单已创建：{card['name']}，待支付",
    }


# ------------------------------------------------------------------
# 余额 → 项目组合规划（纯函数，零模型调用）
# ------------------------------------------------------------
# 为什么必须由服务端算：预算配对是算术约束，而 LLM 心算是已知的脆弱点
# （会把 200 算成 180，或排出总额超余额的组合），而且还要同时满足
# "避开不喜欢的项目""单项目次数有上限""只用得完的量"三个约束 —— 让模型自己
# 兼顾则无法验收。所以模型只负责"说人话"，算术交这里。
#
# 产品取向（用户 2026-09-21 拍板，方案 A）：**只排玩得完的量**。
#   单项目最多 3 次、最多 3 个项目；剩下的余额**主动说明**，不硬凑着花光。
#   （反面：余额 810 时贪心会排成"蹦床×6 + 荡秋千×6 + 过山车×6"，像清库存。）
# ------------------------------------------------------------------
def plan_play_combo(user_id: Optional[int] = None,
                    max_coins: int = 0,
                    liked: Optional[List[str]] = None,
                    disliked: Optional[List[str]] = None,
                    prefer_kinds: int = 3,
                    max_qty_per_project: int = 3) -> Dict:
    """把"余币 + 偏好"排成一份可直接下单的组合方案。

    Args:
        user_id:  服务端权威用户 id；max_coins 为 0 时用它查账户余币
        max_coins: 预算上限（0 = 用账户当前可用余币）。余额口径与下单**同一份实现**
                   （available_coins），否则会出现"方案说够、下单说不够"
        liked:    正向偏好（画像里的 interest.projects），顺序即优先级
        disliked: 负向偏好（画像里的 interest.dislikes）——**绝不进方案**
        prefer_kinds: 最多排几个项目
        max_qty_per_project: 单项目最多玩几次

    Returns:
        {"ok", "balance", "combo":[{name,qty,unit_coins,subtotal,unit_desc}],
         "total_coins", "leftover", "excluded":[{name,reason}],
         "alternatives":[{name,qty,delta_coins}], "reason"}
        ok=False 且 combo=[] 表示排不出（余额 0 / 不足以玩任何一项），reason 说明原因。
    """
    balance = int(max_coins or 0)
    if not balance:
        balance = available_coins(user_id) if user_id else 0

    projects = list_park_projects_view()
    liked_order = [_canonical_project(x) for x in (liked or []) if str(x or "").strip()]
    disliked_set = {_canonical_project(x) for x in (disliked or []) if str(x or "").strip()}

    excluded = [{"name": p["name"], "reason": "你提到不喜欢"}
                for p in projects if p["name"] in disliked_set]

    def _sort_key(p: Dict):
        # 喜欢过的排前面（按画像里的顺序），其余按单价降序（先玩贵的）
        name = p["name"]
        if name in liked_order:
            return (0, liked_order.index(name), -int(p["price_coins"] or 0))
        return (1, 0, -int(p["price_coins"] or 0))

    cands = sorted([p for p in projects if p["name"] not in disliked_set], key=_sort_key)

    if balance <= 0:
        return {"ok": False, "balance": balance, "combo": [], "total_coins": 0,
                "leftover": 0, "excluded": excluded, "alternatives": [],
                "reason": "账户可用余币为 0"}

    combo: List[Dict] = []
    total = 0
    for p in cands[:max(1, int(prefer_kinds))]:
        unit = int(p["price_coins"] or 0)
        if unit <= 0:
            continue
        qty = min(int(max_qty_per_project), (balance - total) // unit)
        if qty <= 0:
            continue
        combo.append({"name": p["name"], "qty": qty, "unit_coins": unit,
                      "subtotal": unit * qty, "unit_desc": p.get("unit_desc") or ""})
        total += unit * qty

    if not combo:
        cheapest = min((int(p["price_coins"] or 0) for p in cands), default=0)
        short = max(0, cheapest - balance) if cheapest else 0
        reason = (f"余额不足以玩任何一个项目（最便宜的 {cheapest} 币，还差 {short} 币）"
                  if cheapest else "当前没有在售的游乐项目")
        return {"ok": False, "balance": balance, "combo": [], "total_coins": 0,
                "leftover": balance, "excluded": excluded, "alternatives": [],
                "reason": reason}

    alternatives = [{"name": it["name"], "qty": it["qty"] - 1,
                     "delta_coins": -it["unit_coins"]}
                    for it in combo if it["qty"] >= 2]

    return {"ok": True, "balance": balance, "combo": combo, "total_coins": total,
            "leftover": balance - total, "excluded": excluded,
            "alternatives": alternatives, "reason": ""}


def list_onsale_cards() -> List[Dict]:
    """在售卡档位（含小额充币与会员充值），供 AI 在余币不足时给出充值建议。"""
    from api.pay import _parse_coins, _card_kind
    from database.db import list_cards

    return [{"card_id": c["id"], "name": c["name"],
             "price_yuan": round(c["price"] / 100, 2),
             "coins": _parse_coins(c["name"], c.get("description", "")),
             "kind": _card_kind(c)}
            for c in (list_cards(status="on_sale") or [])]


def list_park_projects_view() -> List[Dict]:
    """在售游乐项目（权威价目），供 AI 展示与匹配。"""
    from database.db import list_park_projects

    return [{"id": p["id"], "name": p["name"], "price_coins": p["price_coins"],
             "unit_desc": p.get("unit_desc") or ""}
            for p in (list_park_projects(status="on") or [])]


__all__ = ["available_coins", "match_park_project", "match_card",
           "purchase_projects", "purchase_card",
           "list_onsale_cards", "list_park_projects_view",
           "find_projects_in_text", "plan_play_combo"]
