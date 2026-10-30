# ============================================================
# 商家端「经营小助手」API 路由 - /admin/assistant/*（需管理员登录）
# ------------------------------------------------------------
#   POST /admin/assistant/run     一句话执行（SSE 流式）
#   POST /admin/assistant/approve 确认卡续跑（SSE，单步，不再问模型）
#
# SSE 帧格式与 /chat/stream、/admin/kb-agent/analyze 完全一致（`data: {json}\n\n`），
# 前端用 fetch + ReadableStream 消费；事件类型见 agents/admin_assistant.py 的 yield。
# ============================================================
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agents.admin_assistant import admin_assistant
from api.kb_agent import _sse          # 复用同一套「同步生成器 → SSE」包装
from config import ADMIN_ASSISTANT_ENABLED, ADMIN_ASSISTANT_MODEL
from core.auth import get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/assistant", tags=["经营小助手"],
                   dependencies=[Depends(get_current_admin)])


class RunRequest(BaseModel):
    """管理员的一句话指令。

    allow_dangerous：弹窗内「本会话允许直接执行」开关。为 True 时高危操作（退款/调币/删除）
    不再弹确认卡，直接执行 —— 这是管理员在前端显式打开的授权，不来自模型。
    """
    message: str = Field("", max_length=500, description="要做的事，一句话")
    allow_dangerous: bool = Field(False, description="本会话是否允许直接执行高危操作")


class ApproveRequest(BaseModel):
    """确认卡续跑：token 由 run 的 confirm 事件下发（5 分钟一次性）"""
    token: str = Field(..., max_length=64)
    allow_dangerous: bool = False


def _guard():
    if not ADMIN_ASSISTANT_ENABLED:
        raise HTTPException(status_code=503, detail="经营小助手未启用（ADMIN_ASSISTANT_ENABLED=0）")


@router.post("/run", summary="经营小助手：一句话执行（SSE）")
async def assistant_run(req: RunRequest, admin: dict = Depends(get_current_admin)):
    """管理员一句话 → 模型直接调工具把事办完 → 确定性动作流水。

    本端点**不产出聊天回答**：模型每轮的文字输出一律丢弃；一轮工具都没调时回固定话术。
    高危操作返回确认卡（事件 type=confirm），管理员点「执行」后走 /approve 单步续跑。
    """
    _guard()
    msg = (req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="请填写要做的事")
    logger.info(f"[admin_assistant] run by={admin.get('username')} "
                f"allow_dangerous={req.allow_dangerous} msg={msg[:60]!r}")
    ctx = {"allow_dangerous": bool(req.allow_dangerous)}
    return _sse(admin_assistant.run(msg, ctx=ctx, user=admin))


@router.post("/approve", summary="经营小助手：确认高危操作并执行（SSE）")
async def assistant_approve(req: ApproveRequest, admin: dict = Depends(get_current_admin)):
    """确认卡续跑：只执行被确认的那一步工具，不重新问模型（避免模型改参数）。"""
    _guard()
    ctx = {"allow_dangerous": bool(req.allow_dangerous)}
    logger.info(f"[admin_assistant] approve by={admin.get('username')} token={req.token[:6]}…")
    return _sse(admin_assistant.run_approved(req.token, ctx=ctx, user=admin))


@router.get("/config", summary="经营小助手运行参数（前端展示/排查用）")
async def assistant_config():
    return {"code": 200, "data": {"enabled": ADMIN_ASSISTANT_ENABLED,
                                  "model": ADMIN_ASSISTANT_MODEL}}
