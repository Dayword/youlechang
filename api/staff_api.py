# ============================================================
# 员工端 API - /staff/api/*（员工降权令牌与管理员令牌都能访问）
# ------------------------------------------------------------
# 为什么单独开一层，而不是让员工端页面直接打 /admin/*：
#   员工端是手机 H5，页面里的接口地址**对使用者是可见的**。
#   只靠"前端不去调用危险接口"挡不住任何人——把地址抄出来用 curl 打一下就穿了。
#   所以这里用 add_api_route 把 api/admin.py（含 lost_found / notices 的 admin_router）
#   里**允许员工用**的 handler 重新挂一份，挂在 /staff/api 前缀 + get_current_staff 之下；
#   而 /admin/* 对 scope=staff 的令牌一律 403（见 core.auth.get_current_admin）。
#
# 为什么复用 handler 而不是抄一遍实现：
#   券核销 / 退款的算术只能有一份，两边各写一遍迟早漂移
#   （同 services/purchase.py 的思路：下单扣币只有一份核心）。
#
# 员工端拿不到的（刻意不给）：
#   退卡退款、调币、知识库管理/智能体、问答记录/分析、Agent 配置、用户管理、
#   卡产品增删改、删除类操作、清缓存。
# ============================================================
import logging
import random
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import get_current_staff

# 复用商家端的 handler（add_api_route 会按这里声明的依赖重建路由，
# 装饰器上原来的 get_current_admin 不会被带过来）
from api.admin import (
    dashboard,
    admin_list_cards,
    admin_orders,
    admin_list_consumptions,
    admin_verify_consumption,
    admin_verify_consumption_item,
    admin_verify_consumption_items,
    admin_refund_consumption_items,
    admin_refund_consumption,
    admin_get_consumption_by_code,
    ItemCodesRequest,
)
from api.lost_found import (
    merchant_add_lost,
    admin_list_lost,
    admin_update_lost_status,
    LostStatusRequest,
)
from api.notices import (
    admin_list_notices,
    admin_create_notice,
    admin_update_notice,
    admin_list_feedback,
    NoticeRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/staff/api",
    tags=["员工端 API"],
    dependencies=[Depends(get_current_staff)],
)


def _route(path: str, endpoint, methods, summary: str) -> None:
    router.add_api_route(path, endpoint, methods=methods, summary=summary)


# ---------- 仪表盘（只读） ----------
_route("/dashboard", dashboard, ["GET"], "员工端仪表盘（核心指标 + 待办）")

# ---------- 扫码核销 / 退款（一单一券包） ----------
_route("/consumptions", admin_list_consumptions, ["GET"], "消费记录列表（含券明细）")
_route("/consumptions/by-code/{verify_code}", admin_get_consumption_by_code,
       ["GET"], "按券码/核销码查询（券优先，整单兜底）")
_route("/consumptions/items/{item_code}/verify", admin_verify_consumption_item,
       ["POST"], "核销单张券（扫一张核销一张）")
_route("/consumptions/items/verify", admin_verify_consumption_items,
       ["POST"], "批量核销券（勾选多张一次核销）")
_route("/consumptions/items/refund", admin_refund_consumption_items,
       ["POST"], "退款券（勾选若干张，已核销的也能退）")
_route("/consumptions/{order_no}/verify", admin_verify_consumption,
       ["PUT"], "整单核销（该单所有未核销券一次核销）")
# 整单退款只补"没有券行的历史单"这一格：有券的单用上面的按券退款（全选即可退满），
# 效果完全一致，所以不存在"员工能退整单但商家端口径不同"的分叉。
_route("/consumptions/{order_no}/refund", admin_refund_consumption,
       ["POST"], "整单退款（无券行的历史单；有券单走按券退款）")

# ---------- 办卡管理：查订单 + 卡产品价目表 ----------
_route("/orders", admin_orders, ["GET"], "办卡账单列表（统计+分页+筛选）")
_route("/cards", admin_list_cards, ["GET"], "卡产品价目表（现场开卡选卡用）")

# ---------- 失物招领 ----------
_route("/lost-found/items", admin_list_lost, ["GET"], "失物列表（分页 + 状态/来源筛选）")
_route("/lost-found/items", merchant_add_lost, ["POST"], "员工登记拾获物（名称 + 照片）")
_route("/lost-found/items/{item_id}/status", admin_update_lost_status,
       ["PUT"], "更新失物状态（认领/驳回）")

# ---------- 内容管理（公告/热点 + 游客反馈） ----------
_route("/notices", admin_list_notices, ["GET"], "公告/热点列表（含下架）")
_route("/notices", admin_create_notice, ["POST"], "新增公告/热点")
_route("/notices/{notice_id}", admin_update_notice, ["PUT"], "编辑公告/热点")
_route("/notices/feedback", admin_list_feedback, ["GET"], "游客反馈列表")


# ---------- 右下角智能体（仅查询类） ----------
from agents.admin_assistant import admin_assistant          # noqa: E402
from api.admin_assistant import RunRequest, _guard          # noqa: E402
from api.kb_agent import _sse                               # noqa: E402


@router.post("/assistant/ask", summary="员工端智能体（仅查询类工具，SSE）")
async def staff_assistant_ask(req: RunRequest, staff: dict = Depends(get_current_staff)):
    """员工端右下角智能体。

    与商家端 /admin/assistant/run 只差一件事：ctx 里带 readonly=True，
    get_admin_tools 会把 W（写）与 X（高危）工具整批摘掉——
    员工在工具清单里根本看不到退款 / 调币 / 删除这些动作，也就调不动，
    所以这里不需要确认卡那一套流程（危险操作不对员工开放）。
    """
    _guard()
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="请填写要问的事")
    logger.info(f"[staff_assistant] ask by={staff.get('username')} msg={msg[:60]!r}")
    return _sse(admin_assistant.run(msg, ctx={"readonly": True}, user=staff))


# ============================================================
# 现场开卡（柜台收现金/线下售卡）
# ------------------------------------------------------------
# 与小程序下单的区别：这里没有微信支付回调，所以直接落一张 paid 订单 + 发卡，
# 金额默认取卡产品原价，柜台实收不同时由员工填。
# 刻意**不做**退卡：退卡退款是危险操作，只在商家 PC 端（/admin/*）。
# ============================================================
class IssueCardRequest(BaseModel):
    phone: str = ""                 # 游客手机号（已在小程序注册过的号）
    card_id: int = 0                # 卡产品 id
    paid_yuan: float = -1           # 柜台实收金额（元）；-1 = 按卡产品原价


@router.post("/cards/issue", summary="现场开卡（按手机号发卡 + 发币）")
async def staff_issue_card(req: IssueCardRequest):
    from database.db import (get_user_by_phone, get_card, create_card_order,
                             update_card_order_status, add_user_card, list_user_cards)
    from api.pay import _card_kind, _parse_coins

    phone = (req.phone or "").strip()
    if len(phone) != 11 or not phone.isdigit():
        raise HTTPException(status_code=400, detail="请填写 11 位手机号")
    if not req.card_id:
        raise HTTPException(status_code=400, detail="请选择要开的卡")

    user = get_user_by_phone(phone)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="该手机号还没注册过，请先让游客在小程序里登录一次再开卡",
        )

    card = get_card(req.card_id)
    if not card:
        raise HTTPException(status_code=404, detail="卡产品不存在")
    if (card.get("status") or "on_sale") != "on_sale":
        raise HTTPException(status_code=400, detail="该卡已下架，不能开卡")

    amount = int(round(req.paid_yuan * 100)) if req.paid_yuan >= 0 else int(card.get("price") or 0)

    order_no = "OFF%s%04d" % (time.strftime("%Y%m%d%H%M%S"), random.randint(0, 9999))
    order = create_card_order(order_no, user["id"], card["id"], card["name"], amount)
    if not order:
        raise HTTPException(status_code=500, detail="开卡失败：订单创建异常")
    update_card_order_status(order_no, "paid", transaction_id="offline-staff")
    add_user_card(
        user["id"], card["id"], card["name"], order_no,
        coins=_parse_coins(card["name"], card.get("description", "")),
        kind=_card_kind(card),
    )
    logger.info(f"[员工端] 现场开卡 {order_no}｜用户 {user['id']}｜卡「{card['name']}」"
                f"｜实收 {amount / 100:.2f} 元")

    created = [c for c in (list_user_cards(user["id"]) or []) if c.get("order_no") == order_no]
    return {
        "code": 200,
        "msg": f"开卡成功：{card['name']}，已发到 {phone}",
        "data": {
            "order_no": order_no,
            "card_name": card["name"],
            "amount_yuan": round(amount / 100, 2),
            "nickname": user.get("nickname") or "",
            "card": created[0] if created else None,
        },
    }
