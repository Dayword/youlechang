# ============================================================
# 支付 API 路由 - /pay/*
# 卡产品查询 / 办卡下单 / 微信支付回调 / 订单查询 / 我的卡 / 余币余额
#
# 支付开关（config.WECHAT_PAY_ENABLED，.env 注入）：
#   true  - 正式模式：调微信 JSAPI 统一下单（需商户号三件套）
#   其余  - 模拟模式（false 未开通/mock 联调）：点击充值直接模拟支付完成并发卡，
#           全流程可体验，无需调起微信收银台
#
# 安全设计：
#   - 金额绝不来自前端/LLM：下单时以 cards 表权威价格锁定
#   - 下单需 JWT 鉴权；订单号服务端生成，幂等防重复
#   - 正式模式回调验签（APIv3）后才发卡；前端仅可查自己的订单
# ============================================================
import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pay", tags=["卡产品与支付"])


# ---------- 请求模型 ----------
class CreateOrderRequest(BaseModel):
    card_id: int


def _pay_mode() -> str:
    """支付模式：显式 true=正式微信支付；其余（false 未开通/mock 联调）一律模拟发卡"""
    from config import WECHAT_PAY_ENABLED
    return "true" if WECHAT_PAY_ENABLED == "true" else "mock"


# 币数解析：从卡名/描述提取「X币」的面值（"200元=320币"→320；"10币"→10）
_COINS_RE = re.compile(r"(\d+)\s*币")


# 卡类型：小额充币卡（名称形如"10币"，1币=1元无赠送）与会员充值卡（"200元=320币"有赠送）分组；
# 小额充值直接进余币：不进会员卡列表、不提供"使用/退卡"；会员卡维持现有完整权益
_SMALL_CARD_RE = re.compile(r"^\d+\s*币$")


def _card_kind(card) -> str:
    """返回卡类型: small=小额充币 / member=会员充值"""
    if not card:
        return "member"
    return "small" if _SMALL_CARD_RE.match((card.get("name") or "").strip()) else "member"


def _parse_coins(name: str, description: str = "") -> int:
    m = _COINS_RE.search(f"{name} {description}")
    return int(m.group(1)) if m else 0


def _user_coins(user_id: int, cards: Optional[list] = None) -> int:
    """用户持卡余币合计 = 生效卡（会员卡+小额充币卡）的币数之和（优先持卡记录上的 coins，
    旧数据/商家调整前为 NULL 时按卡产品面值解析兑底）"""
    from database.db import get_card
    if cards is None:
        from database.db import list_user_cards
        cards = list_user_cards(user_id)
    total = 0
    for uc in cards:
        uc_coins = uc.get("coins")
        if uc_coins is not None:
            total += int(uc_coins)
            continue
        card = get_card(uc["card_id"])
        if card:
            total += _parse_coins(card["name"], card.get("description", ""))
    return total


def _user_available_coins(user_id: int, cards: Optional[list] = None) -> int:
    """用户可用余币（游乐消费可扣上限）= 全部生效记录（会员卡 + 小额充币）余币 + 签到奖励剩余可用。
    说明：签到所得同样可直接抵扣项目消费；『生效卡』仅决定扣减时的优先级，不代表可用额度。"""
    from database.db import bonus_available
    return _user_coins(user_id, cards) + bonus_available(user_id)


def _gen_order_no() -> str:
    """业务订单号：时间戳 + 随机段（32 位内）"""
    return f"{time.strftime('%Y%m%d%H%M%S')}{uuid.uuid4().hex[:12].upper()}"


# 同用户同卡在此窗口内不重复办卡（秒）
REPEAT_ORDER_WINDOW = 30


def _seconds_since(created_at) -> float:
    """订单创建时间距今的秒数。

    时间列在 MySQL 驱动下是 datetime、SQLite 下是字符串，统一在 Python 侧归一化；
    格式异常时返回无穷大，按"可以新建订单"处理（不因为时间解析不了就卡住用户下单）。
    """
    if isinstance(created_at, str):
        try:
            created_at = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return float("inf")
    if not isinstance(created_at, datetime):
        return float("inf")
    return (datetime.now() - created_at).total_seconds()


# ============================================================
# 卡产品
# ============================================================
@router.get("/cards", summary="在售卡列表（kind: small小额充币 / member会员充值）")
async def get_cards():
    """聊天页办卡卡片、充值页档位共用数据源（无需登录）
    kind: small=小额充币卡（10/20/30币，直接入余币）；member=会员充值卡（200/500/1000元）"""
    from database.db import list_cards
    cards = list_cards(status="on_sale")
    return {
        "code": 200,
        "data": [{
            "card_id": c["id"],
            "name": c["name"],
            "price": c["price"],                 # 单位：分
            "price_yuan": round(c["price"] / 100, 2),
            "description": c["description"],
            "coins": _parse_coins(c["name"], c.get("description", "")),  # 面值（币）
            "kind": _card_kind(c),                # small / member
        } for c in cards]
    }


# ============================================================
# 下单
# ============================================================
@router.post("/orders", summary="办卡下单（模拟支付直接发卡 / 正式模式返回支付参数）")
async def create_order(req: CreateOrderRequest, current_user: dict = Depends(get_current_user)):
    """非 true 模式（未开通商户号）下模拟支付完成并直接发卡，全流程可体验。

    下单核心在 services/purchase.py：与 AI 对话侧（tools/consume_tools.py）共用同一份
    价格口径、幂等窗口与发卡逻辑，避免"前端按钮"和"AI 帮用户买"两套实现分叉。
    """
    from services.purchase import purchase_card

    res = purchase_card(current_user["user_id"], req.card_id)
    if not res.get("ok"):
        raise HTTPException(status_code=res.get("http", 400), detail=res.get("msg", "下单失败"))

    data = {
        "mode": res["mode"],
        "order_no": res["order_no"],
        "status": res["status"],
        "reused": res["reused"],
    }
    if res["mode"] == "mock":
        data["card_name"] = res["card_name"]
    elif res.get("payment"):
        data["payment"] = res["payment"]   # 透传给 wx.requestPayment
    return {"code": 200, "data": data}


# ============================================================
# 微信支付 V3 正式模式（需商户号三件套后联调）
# ============================================================
def _jsapi_unified_order(order: dict, openid: str) -> dict:
    """调微信 V3 统一下单接口，返回 wx.requestPayment 所需五参数"""
    import requests
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import base64

    from config import (
        WECHAT_APPID, WECHAT_MCHID, WECHAT_CERT_SERIAL,
        WECHAT_PRIVATE_KEY_PATH, WECHAT_PAY_NOTIFY_URL,
    )
    if not all([WECHAT_MCHID, WECHAT_CERT_SERIAL, WECHAT_PRIVATE_KEY_PATH]):
        raise RuntimeError("微信支付配置不完整：请配置 WECHAT_MCHID/CERT_SERIAL/PRIVATE_KEY_PATH")

    with open(WECHAT_PRIVATE_KEY_PATH, "rb") as f:
        private_key = load_pem_private_key(f.read(), password=None)

    def _sign(message: str) -> str:
        sig = private_key.sign(
            message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(sig).decode()

    def _auth_header(method: str, url_path: str, body: str) -> str:
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        message = f"{method}\n{url_path}\n{timestamp}\n{nonce}\n{body}\n"
        signature = _sign(message)
        return (
            f'WECHATPAY2-SHA256-RSA2048 mchid="{WECHAT_MCHID}",'
            f'nonce_str="{nonce}",signature="{signature}",'
            f'timestamp="{timestamp}",serial_no="{WECHAT_CERT_SERIAL}"'
        )

    body = json.dumps({
        "appid": WECHAT_APPID,
        "mchid": WECHAT_MCHID,
        "description": order["card_name"],
        "out_trade_no": order["order_no"],
        "notify_url": WECHAT_PAY_NOTIFY_URL,
        "amount": {"total": order["amount"], "currency": "CNY"},
        "payer": {"openid": openid},
    }, ensure_ascii=False)

    url_path = "/v3/pay/transactions/jsapi"
    resp = requests.post(
        f"https://api.mch.weixin.qq.com{url_path}",
        data=body.encode("utf-8"),
        headers={
            "Authorization": _auth_header("POST", url_path, body),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=10,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"统一下单失败: HTTP {resp.status_code} {resp.text[:200]}")
    prepay_id = resp.json().get("prepay_id")
    if not prepay_id:
        raise RuntimeError(f"统一下单响应缺少 prepay_id: {resp.text[:200]}")

    # 二次签名（wx.requestPayment 参数）
    from config import WECHAT_APPID as APPID
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    package = f"prepay_id={prepay_id}"
    pay_sign = _sign(f"{APPID}\n{timestamp}\n{nonce}\n{package}\n")
    return {
        "timeStamp": timestamp,
        "nonceStr": nonce,
        "package": package,
        "signType": "RSA",
        "paySign": pay_sign,
    }


@router.post("/notify", summary="微信支付异步回调（正式模式）")
async def pay_notify(request: Request):
    """
    微信 V3 支付结果通知：验签 -> 解密 -> 更新订单 -> 发卡。
    ⚠️ 验签依赖微信平台证书（首次可经 /v3/certificates 下载并缓存），
    需商户号开通后联调；当前个人主体阶段该回调不会被触发。
    """
    from database.db import get_card_order_by_no, update_card_order_status, add_user_card

    try:
        data = await request.json()
        resource = data.get("resource", {})
        # TODO 商户号开通后联调：验签(Wechatpay-Signature) + 平台证书 + AES-256-GCM 解密
        # 解密后明文: {"out_trade_no": "...", "transaction_id": "...", "trade_state": "SUCCESS", ...}
        plaintext = resource.get("plaintext")  # 联调时由解密逻辑填充
        if not plaintext:
            raise RuntimeError("notify 解密逻辑待商户号开通后联调")
        payload = json.loads(plaintext) if isinstance(plaintext, str) else plaintext
    except Exception as e:
        logger.error(f"支付回调处理失败: {e}")
        return {"code": "FAIL", "message": "处理失败"}

    if payload.get("trade_state") != "SUCCESS":
        return {"code": "SUCCESS", "message": "非成功状态忽略"}

    order = get_card_order_by_no(payload.get("out_trade_no", ""))
    if not order:
        return {"code": "FAIL", "message": "订单不存在"}
    if order["status"] == "paid":
        return {"code": "SUCCESS", "message": "重复通知忽略"}

    update_card_order_status(order["order_no"], "paid",
                             transaction_id=payload.get("transaction_id", ""))
    from database.db import get_card
    card = get_card(order["card_id"])
    add_user_card(order["user_id"], order["card_id"], order["card_name"], order["order_no"],
                  coins=_parse_coins(card["name"], card.get("description", "")) if card else 0,
                  kind=_card_kind(card) if card else "member")
    logger.info(f"[微信支付] 订单 {order['order_no']} 支付成功并发卡/发币: {order['card_name']}")
    return {"code": "SUCCESS", "message": "OK"}


# ============================================================
# 订单查询 / 我的卡
# ============================================================
@router.get("/orders/{order_no}", summary="查询订单状态（前端支付后轮询）")
async def get_order(order_no: str, current_user: dict = Depends(get_current_user)):
    from database.db import get_card_order_by_no
    order = get_card_order_by_no(order_no)
    if not order or order["user_id"] != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="订单不存在")
    return {
        "code": 200,
        "data": {
            "order_no": order["order_no"],
            "card_name": order["card_name"],
            "amount": order["amount"],
            "status": order["status"],
        }
    }


@router.get("/my/cards", summary="我的卡列表（会员卡+小额充币记录，含剩余币/生效标记）")
async def my_cards(current_user: dict = Depends(get_current_user)):
    """返回全部生效记录（kind=member 会员卡 + kind=small 小额充币），供“我的卡”抽屉分区展示；
    小额充币记录不提供使用/退卡（前端隐藏按钮，后端接口亦拒绝）"""
    from database.db import list_user_cards, get_card, get_card_order_by_no, set_user_card_in_use

    user_id = current_user["user_id"]
    cards = list_user_cards(user_id)  # 全部 active（含小额充币记录）
    member_cards = [c for c in cards if c.get("kind", "member") != "small"]

    # 仅一张生效会员卡且未标记生效 → 自动置为生效中（会员码只按会员卡出码；小额充币不参与）
    if len(member_cards) == 1 and not member_cards[0].get("in_use"):
        set_user_card_in_use(user_id, member_cards[0]["id"])
        cards = list_user_cards(user_id)

    items = []
    for uc in cards:
        card = get_card(uc["card_id"])
        face = _parse_coins(card["name"], card.get("description", "")) if card else 0
        remaining = int(uc["coins"]) if uc.get("coins") is not None else face
        order = get_card_order_by_no(uc["order_no"]) if uc.get("order_no") else None
        price_yuan = round(order["amount"] / 100, 2) if order else 0
        items.append({
            "id": uc["id"],
            "card_id": uc["card_id"],
            "card_name": uc["card_name"] or (card["name"] if card else ""),
            "order_no": uc["order_no"],
            "coins": remaining,
            "face_coins": face,
            "price_yuan": price_yuan,
            "kind": uc.get("kind", "member"),
            "refundable": face > 0 and remaining >= face,
            "in_use": bool(uc.get("in_use")),
            "created_at": str(uc.get("created_at") or ""),
        })
    return {"code": 200, "data": items}


@router.post("/my/cards/{card_id}/use", summary="使用某张卡（标记为生效中，会员码按它出码）")
async def use_my_card(card_id: int, current_user: dict = Depends(get_current_user)):
    from database.db import get_user_card, set_user_card_in_use

    uc = get_user_card(card_id, current_user["user_id"])
    if not uc:
        raise HTTPException(status_code=404, detail="卡片不存在或不属于当前用户")
    if (uc.get("kind") or "member") == "small":
        raise HTTPException(status_code=400, detail="小额充值直接存入余币，无需设置生效卡")
    if not set_user_card_in_use(current_user["user_id"], card_id):
        raise HTTPException(status_code=500, detail="操作失败，请稍后重试")
    return {"code": 200, "msg": "已切换为生效中，会员码将按此卡出示"}


@router.post("/my/cards/{card_id}/refund", summary="自助退卡（剩余币不足面值时不可退）")
async def refund_my_card(card_id: int, current_user: dict = Depends(get_current_user)):
    """退卡政策：剩余币数 ≥ 卡面值（未使用过）才可退，退款金额为支付原价；
    已使用过（剩余币不足）无法退款，提示到游乐场游玩"""
    from database.db import get_user_card, get_card, get_card_order_by_no, refund_order

    uc = get_user_card(card_id, current_user["user_id"])
    if not uc:
        raise HTTPException(status_code=404, detail="卡片不存在或不属于当前用户")

    # 小额充币直接存入余币，不提供退卡功能（不可退，仅可在园内消费使用）
    if (uc.get("kind") or "member") == "small":
        raise HTTPException(status_code=400, detail="小额充值已直接存入余币，不支持退卡")

    card = get_card(uc["card_id"])
    face = _parse_coins(card["name"], card.get("description", "")) if card else 0
    remaining = int(uc["coins"]) if uc.get("coins") is not None else face
    order = get_card_order_by_no(uc["order_no"]) if uc.get("order_no") else None
    price_yuan = round(order["amount"] / 100, 2) if order else 0

    if face <= 0 or remaining < face:
        raise HTTPException(
            status_code=400,
            detail=f"该卡剩余 {remaining} 币，不足面值 {face} 币，无法退款，请到游乐场进行游玩",
        )
    if not uc.get("order_no"):
        raise HTTPException(status_code=400, detail="该卡无对应订单，请联系工作人员")

    result = refund_order(uc["order_no"])
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg", "退卡失败"))
    return {
        "code": 200,
        "msg": f"退卡成功，退款 ¥{price_yuan} 将原路退回",
        "data": {"refund_yuan": price_yuan, "order_no": uc["order_no"]},
    }


@router.get("/my/balance", summary="我的可用余币（全部卡+小额+签到剩余可用）")
async def my_balance(current_user: dict = Depends(get_current_user)):
    """可用余币 = 所有生效记录（会员卡+小额充币）余币 + 签到奖励剩余可用；
    签到所得同样可抵扣游乐项目消费，『生效卡』只影响扣减优先级"""
    from database.db import list_user_cards
    all_cards = list_user_cards(current_user["user_id"])
    total_coins = _user_available_coins(current_user["user_id"], all_cards)
    member_count = len([c for c in all_cards if c.get("kind", "member") != "small"])
    return {
        "code": 200,
        "data": {"coins": total_coins, "cards_count": member_count}
    }


# ============================================================
# 每日签到（我的页：每日签到送 1 币，奖励计入余币余额）
# ============================================================
CHECKIN_REWARD_COINS = 1


@router.get("/my/checkin", summary="签到状态查询（今日是否已签/连续天数/累计奖励）")
async def checkin_status(current_user: dict = Depends(get_current_user)):
    from database.db import get_checkin_status
    return {"code": 200, "data": get_checkin_status(current_user["user_id"])}


@router.post("/my/checkin", summary="每日签到（每日送币，重复签幂等不重复发币）")
async def checkin(current_user: dict = Depends(get_current_user)):
    from database.db import do_checkin
    status = do_checkin(current_user["user_id"], reward_coins=CHECKIN_REWARD_COINS)
    if status.get("signed_now"):
        logger.info(f"[签到] 用户 {current_user['user_id']} 签到成功，送 {CHECKIN_REWARD_COINS} 币")
    return {"code": 200, "data": status}


# ============================================================
# 提币二维码（充值页展示，园内提币机/人工核销扫码使用）
# ============================================================
@router.get("/redeem/qrcode", summary="提币二维码（PNG base64，含用户标识与余币/生效卡）")
async def redeem_qrcode(card_id: int = 0, current_user: dict = Depends(get_current_user)):
    """生成当前用户的提币二维码：园内提币机扫码后按余币发放游戏币。
    指定 card_id 时按"生效中"的卡出码（卡名+剩余币入码），否则按用户总余额出码。

    容错：qrcode / Pillow 缺失或生成异常时不再返回 500，而是降级为"纯文本会员码"
    （qr_ok=false + content 原文），工作人员可按文本码手工核验；
    —— 历史上因运行环境未安装 qrcode 导致本接口 500、前端一直停在"会员码加载中"。
    """
    import base64
    import io
    from database.db import sum_checkin_coins, get_user_card, get_card

    user_id = current_user["user_id"]
    coins = _user_coins(user_id) + sum_checkin_coins(user_id)

    payload = f"DUCKYARD-REDEEM|uid={user_id}|coins={coins}"
    card_name, card_coins = "", 0
    if card_id:
        uc = get_user_card(card_id, user_id)
        if uc:
            card = get_card(uc["card_id"])
            face = _parse_coins(card["name"], card.get("description", "")) if card else 0
            card_coins = int(uc["coins"]) if uc.get("coins") is not None else face
            card_name = uc["card_name"] or (card["name"] if card else "")
            payload += f"|card={card_name}|card_coins={card_coins}"

    image = ""
    qr_error = ""
    try:
        import qrcode
        img = qrcode.make(payload)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        qr_error = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning(f"[会员码] 二维码生成失败，降级为文本会员码: {qr_error}")

    return {
        "code": 200,
        "msg": "" if image else "二维码生成失败，请使用文本会员码核验（或联系管理员安装 qrcode 依赖）",
        "data": {
            "content": payload,
            "coins": coins,
            "card_name": card_name,
            "card_coins": card_coins,
            "image": image,
            "qr_ok": bool(image),
            "qr_error": qr_error,
        }
    }


# ============================================================
# 游乐消费：多选项目合并扣币 → 唯一核销码（工作人员核验）
# 扣币优先级：生效中的卡 → 其余生效卡按购买时间先到先扣 → 签到奖励剩余
# ============================================================
class ConsumeItem(BaseModel):
    project_id: int
    qty: int = Field(1, ge=1, le=99)


class ConsumeRequest(BaseModel):
    items: list[ConsumeItem] = Field(..., min_length=1)


@router.get("/projects", summary="游乐项目价目表（消费清单数据源）")
async def list_projects():
    from database.db import list_park_projects
    items = list_park_projects(status="on")
    return {
        "code": 200,
        "data": [{
            "id": p["id"], "name": p["name"], "price_coins": p["price_coins"],
            "unit_desc": p.get("unit_desc") or "",
        } for p in items],
    }


@router.post("/consume", summary="游乐消费（多选项目合并扣币，生成游园券）")
async def consume(req: ConsumeRequest, current_user: dict = Depends(get_current_user)):
    """多选项目合并扣币：生效中的卡优先扣，跨卡拆分，签到奖励兜底。

    扣币成功按「项目数 × 数量」展开成 N 张游园券（一券 = 一项一次，一券一码），
    用户到项目现场逐张出示、工作人员扫一张核销一张；
    `verify_code` 是第一张券的码，仍可用于按整单查询/整单核销。

    下单核心在 services/purchase.py：与 AI 对话侧（tools/consume_tools.py）共用同一份
    扣币优先级与券码生成逻辑。
    """
    from services.purchase import purchase_projects

    res = purchase_projects(
        current_user["user_id"],
        [{"project_id": it.project_id, "qty": it.qty} for it in req.items],
    )
    if not res.get("ok"):
        raise HTTPException(status_code=res.get("http", 400), detail=res.get("msg", "扣费失败"))

    return {
        "code": 200,
        "msg": res["msg"],
        "data": {
            "order_no": res["order_no"],
            "verify_code": res["verify_code"],
            "items": res["items"],
            "passes": res.get("passes") or [],
            "pass_count": res.get("pass_count") or 0,
            "total_coins": res["total_coins"],
            "remaining_coins": res["remaining_coins"],
        },
    }


def _lan_ip() -> str:
    """本机局域网 IP（给员工手机扫码用）。

    必须跳过 127./169.254. 以及 Clash/Mihomo TUN 的 fake-ip 段 198.18./198.19. ——
    带 TUN 代理的机器上，UDP connect 拿到的经常是 TUN 网卡地址，
    编进二维码就是手机路由不到的地址（小程序编译脚本里为同一个坑改过一次探测口径）。
    """
    import socket

    bad = ("127.", "169.254.", "198.18.", "198.19.")
    cands = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("114.114.114.114", 80))   # UDP connect 不发包，只问出口网卡
            cands.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        cands.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    for ip in cands:
        if ip and not ip.startswith(bad):
            return ip
    return "127.0.0.1"


def _staff_verify_base() -> str:
    """员工核销页的对外地址：配置优先，否则按局域网 IP 拼。"""
    from config import SERVER_PORT, STAFF_VERIFY_BASE_URL

    base = (STAFF_VERIFY_BASE_URL or "").strip().rstrip("/")
    if base:
        return base
    return f"http://{_lan_ip()}:{SERVER_PORT}/staff/verify"


@router.get("/passes/qrcode", summary="游园券二维码（扫码直达员工核销页）")
async def pass_qrcode(c: str, current_user: dict = Depends(get_current_user)):
    """按券码出二维码：内容 = 员工核销页 URL，工作人员微信扫一扫直接打开核销。

    只允许给**当前用户自己的券**出图——否则别人拿到券码就能生成可转发的核销图。
    容错与会员码一致：qrcode/Pillow 缺失时降级为纯文本券码（qr_ok=false），
    前端展示大字券码让工作人员手工输入，不会卡在"加载中"。
    """
    from database.db import get_consumption_item_by_code

    code = (c or "").strip().upper()
    item = get_consumption_item_by_code(code)
    if not item or item.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="券不存在")

    content = f"{_staff_verify_base()}?c={code}"
    image, qr_error = "", ""
    try:
        import base64
        import io as _io
        import qrcode
        buf = _io.BytesIO()
        qrcode.make(content).save(buf, format="PNG")
        image = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        qr_error = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning(f"[游园券] 二维码生成失败，降级为文本券码: {qr_error}")

    return {
        "code": 200,
        "msg": "" if image else "二维码生成失败，可让工作人员手工输入券码",
        "data": {
            "item_code": code,
            "content": content,
            "image": image,
            "qr_ok": bool(image),
            "qr_error": qr_error,
        },
    }


@router.get("/my/passes", summary="我的游园码（待玩 / 已玩券）")
async def my_passes(status: str = "", limit: int = 50,
                    current_user: dict = Depends(get_current_user)):
    """我的游园码：默认返回全部券，**待玩的排在前面**。

    券是核销的最小单位——到项目现场出示「当前这一张」，工作人员扫一张核销一张，
    不影响同一单里其它还没玩的券。`next_pass` 就是首页大字卡要显示的那一张。
    """
    from database.db import (list_consumption_items_by_user,
                             count_consumption_items_by_user)

    uid = current_user["user_id"]
    st = (status or "").strip().lower()
    if st not in ("unused", "used", "refunded"):
        st = ""
    limit = min(max(1, limit), 200)

    rows = list_consumption_items_by_user(uid, status=st or None, limit=limit)
    view = [{
        "item_code": r["item_code"],
        "order_no": r["order_no"],
        "project_id": r.get("project_id"),
        "project_name": r["project_name"],
        "unit_coins": r.get("unit_coins") or 0,
        "status": r.get("status") or "unused",
        "used_at": str(r.get("used_at") or ""),
        "created_at": str(r.get("created_at") or ""),
    } for r in rows]
    unused = [v for v in view if v["status"] == "unused"]

    return {
        "code": 200,
        "data": {
            "passes": view,
            "next_pass": unused[0] if unused else None,
            "unused_count": count_consumption_items_by_user(uid, "unused"),
            "used_count": count_consumption_items_by_user(uid, "used"),
            "refunded_count": count_consumption_items_by_user(uid, "refunded"),
        },
    }


class PassRefundRequest(BaseModel):
    """自助退券：给券码（退指定的几张）或给订单号（退这单所有没玩过的券）"""
    codes: list[str] = []
    order_no: str = ""


@router.post("/my/passes/refund", summary="自助退券（只退自己的、只退未核销的）")
async def refund_my_passes(req: PassRefundRequest, current_user: dict = Depends(get_current_user)):
    """用户在小程序里自助退券：**只退未核销的**。

    与商家端 / 员工端的按券退款（admin_refund_consumption_items）是两套入口、一份核心
    （database.refund_consumption_items）——钱的算法只有一处，差别只在准入：

      · 只能退**自己的**券：券码会印在二维码上、也可能被抄走，不能凭码就退；
      · 只退 **unused**：玩过的项目让用户自己退掉等于白嫖，
        "刚上去就下来"这类需要退已核销券的情况由现场员工 / 商家判定后处理。
    """
    from database.db import (get_consumption_item_by_code,
                             list_consumption_items_by_order,
                             refund_consumption_items)

    uid = current_user["user_id"]

    codes: list[str] = []
    seen = set()
    for c in (req.codes or []):
        c = str(c or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            codes.append(c)

    # 传订单号 = 退这一单里所有还没玩的券（用户一次买了 5 项、临时有事全不玩了）
    if not codes and (req.order_no or "").strip():
        order_no = (req.order_no or "").strip()
        codes = [r["item_code"]
                 for r in (list_consumption_items_by_order(order_no) or [])
                 if (r.get("status") or "unused") == "unused"
                 and r.get("user_id") == uid]
        if not codes:
            raise HTTPException(status_code=400, detail="这一单没有可退的未核销券")

    if not codes:
        raise HTTPException(status_code=400, detail="请先选择要退的券")

    ok_codes: list[str] = []
    skipped: list[dict] = []
    for c in codes:
        item = get_consumption_item_by_code(c)
        if not item or item.get("user_id") != uid:
            skipped.append({"item_code": c, "reason": "不是你的券"})
            continue
        st = item.get("status") or "unused"
        if st == "refunded":
            skipped.append({"item_code": c, "reason": "已退过款"})
            continue
        if st == "used":
            skipped.append({"item_code": c, "reason": "已核销，需到现场找工作人员退"})
            continue
        ok_codes.append(c)

    if not ok_codes:
        raise HTTPException(
            status_code=400,
            detail="没有可退的券：已玩过或已退过的券不能在小程序里自助退，需要退请找现场工作人员",
        )

    result = refund_consumption_items(ok_codes)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg", "退款失败"))

    coins = result.get("refunded_coins") or 0
    count = result.get("count") or 0
    logger.info(f"[游园券] 用户 {uid} 自助退券 {count} 张，{coins} 币")
    return {
        "code": 200,
        "msg": f"已退 {count} 张券，{coins} 币已退回你的卡",
        "data": {
            "refunded_coins": coins,
            "count": count,
            "items": result.get("items") or [],
            "skipped": skipped,
        },
    }


@router.get("/my/consumptions", summary="我的消费记录（today=true 仅今日，默认全部/最近）")
async def my_consumptions(limit: int = 10, today: bool = False, current_user: dict = Depends(get_current_user)):
    """我的消费记录。带券进度（本单共几张券、还剩几张待玩），
    否则一单一券包下一次买 3 项只显示"未核销"，用户看不出玩到哪一步了。"""
    from database.db import (list_consumptions_by_user,
                             count_consumption_items_by_orders)

    limit = min(max(1, limit), 50)
    rows = list_consumptions_by_user(current_user["user_id"], limit=limit, today=today)
    stats = count_consumption_items_by_orders([o["order_no"] for o in rows])
    return {"code": 200, "data": [{
        "order_no": o["order_no"],
        "verify_code": o["verify_code"],
        "items_json": o.get("items_json") or "[]",
        "card_coins": o.get("card_coins") or 0,
        "bonus_coins": o.get("bonus_coins") or 0,
        "total_coins": o.get("total_coins") or 0,
        "status": o.get("status") or "unused",
        "pass_total": (stats.get(o["order_no"]) or {}).get("total", 0),
        "pass_unused": (stats.get(o["order_no"]) or {}).get("unused", 0),
        "created_at": str(o.get("created_at") or ""),
    } for o in rows]}


@router.get("/my/consumption-stats", summary="我的消费统计（排行/办卡/按月；供“我的”页横向柱状图等使用）")
async def my_consumption_stats(period: str = "month",
                               current_user: dict = Depends(get_current_user)):
    """period: week/month/year；口径=仅项目消费、退款单全额剔除。"""
    from services.statistics import user_stats
    s = user_stats(current_user["user_id"], period)
    # 序列化周期日期（date → iso），避免 JSON 直出失败
    if isinstance(s.get("period"), dict):
        s["period"] = {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                       for k, v in s["period"].items()}
    return {"code": 200, "data": s}
