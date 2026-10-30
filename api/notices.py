# ============================================================
# 园区公告 / 今日热点 / 游客意见反馈 API 路由
# 游客端（微信小程序，无需管理员）:
#   GET  /notices?kind=      公告/热点列表（仅上架，小程序首页展示）
#   POST /user-feedback      游客意见反馈提交（登录用户自动关联 user_id）
#                            注：/feedback 已被问答满意度评价占用，故用独立路径
# 商家端（运营后台 PC，需管理员登录）:
#   GET    /admin/notices         公告/热点统一管理列表（含下架）
#   POST   /admin/notices         新增（kind: announcement 园区公告 / hot 今日热点）
#   PUT    /admin/notices/{id}    编辑（标题/内容/状态/排序）
#   DELETE /admin/notices/{id}    删除
#   GET    /admin/feedback        游客反馈列表（只读）
#
# 内容唯一来源是商家 PC 端发布，小程序端只读展示。
# ============================================================
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import get_current_admin, get_optional_user

logger = logging.getLogger(__name__)

# 游客端路由（公开）
router = APIRouter(tags=["园区公告/热点"])

# 商家端路由（需管理员登录）
admin_router = APIRouter(
    prefix="/admin/notices",
    tags=["公告/热点管理"],
    dependencies=[Depends(get_current_admin)],
)

VALID_KINDS = {"announcement", "hot"}


def _notice_to_view(n: dict) -> dict:
    return {
        "id": n["id"],
        "kind": n.get("kind") or "announcement",
        "title": n.get("title") or "",
        "content": n.get("content") or "",
        "status": n.get("status") or "on",
        "sort": n.get("sort") or 0,
        "created_at": str(n.get("created_at")) if n.get("created_at") else "",
    }


def _feedback_to_view(f: dict) -> dict:
    return {
        "id": f["id"],
        "nickname": f.get("nickname") or "",
        "content": f.get("content") or "",
        "created_at": str(f.get("created_at")) if f.get("created_at") else "",
    }


# ============================================================
# 游客端
# ============================================================
@router.get("/notices", summary="公告/热点列表（仅上架，小程序首页数据源）")
async def list_public_notices(kind: str = ""):
    """kind 为空返回两种全部上架内容；kind=announcement / hot 按类筛选"""
    from database.db import list_park_notices

    if kind and kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail="无效的公告类型")
    items = list_park_notices(kind=kind, status="on", page=1, page_size=100)
    return {"code": 200, "data": [_notice_to_view(i) for i in items]}


class FeedbackRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=500, description="反馈内容")
    nickname: str = Field("", max_length=64, description="称呼/联系方式（选填）")


@router.post("/user-feedback", summary="游客意见反馈提交")
async def submit_user_feedback(req: FeedbackRequest, user: Optional[dict] = Depends(get_optional_user)):
    """小程序「意见反馈」弹窗提交；已登录微信用户自动关联 user_id"""
    from database.db import add_user_feedback

    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="请填写反馈内容")

    item = add_user_feedback(
        content=content,
        nickname=req.nickname.strip(),
        user_id=user.get("user_id") if user else None,
    )
    if not item:
        raise HTTPException(status_code=500, detail="提交失败，请稍后重试")

    logger.info(f"[意见反馈] user={user.get('user_id') if user else '匿名'}: {content[:50]}")
    return {"code": 200, "msg": "反馈已提交，感谢您的建议！", "data": _feedback_to_view(item)}


# ============================================================
# 商家端：公告 / 热点管理
# ============================================================
@admin_router.get("", summary="公告/热点统一管理列表（含下架内容）")
async def admin_list_notices(kind: str = "", status: str = "", page: int = 1, page_size: int = 100):
    from database.db import list_park_notices, count_park_notices

    if kind and kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail="无效的公告类型")
    page = max(1, page)
    page_size = min(max(1, page_size), 200)
    total = count_park_notices(kind=kind, status=status)
    items = list_park_notices(kind=kind, status=status, page=page, page_size=page_size)
    return {
        "code": 200,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [_notice_to_view(i) for i in items],
        },
    }


class NoticeRequest(BaseModel):
    kind: str = Field(..., description="announcement 园区公告 / hot 今日热点")
    title: str = Field(..., min_length=1, max_length=128, description="标题")
    content: str = Field("", max_length=512, description="正文内容")
    status: str = Field("on", description="on 上架 / off 下架")
    sort: int = Field(0, description="排序（越小越靠前）")


@admin_router.post("", summary="新增公告/热点")
async def admin_create_notice(req: NoticeRequest):
    from database.db import create_park_notice

    if req.kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail="无效的公告类型")
    if req.status not in {"on", "off"}:
        raise HTTPException(status_code=400, detail="无效的状态值")

    item = create_park_notice(
        kind=req.kind, title=req.title, content=req.content,
        sort=req.sort, status=req.status,
    )
    if not item:
        raise HTTPException(status_code=500, detail="创建失败，请稍后重试")
    logger.info(f"[公告管理] 新增 {req.kind}: {req.title}")
    return {"code": 200, "msg": "已发布", "data": _notice_to_view(item)}


@admin_router.put("/{notice_id}", summary="编辑公告/热点（标题/内容/状态/排序）")
async def admin_update_notice(notice_id: int, req: NoticeRequest):
    from database.db import get_park_notice, update_park_notice

    if req.kind not in VALID_KINDS:
        raise HTTPException(status_code=400, detail="无效的公告类型")
    if req.status not in {"on", "off"}:
        raise HTTPException(status_code=400, detail="无效的状态值")
    if not get_park_notice(notice_id):
        raise HTTPException(status_code=404, detail="内容不存在")

    ok = update_park_notice(
        notice_id, kind=req.kind, title=req.title,
        content=req.content, status=req.status, sort=req.sort,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="更新失败")
    return {"code": 200, "msg": "已保存", "data": _notice_to_view(get_park_notice(notice_id))}


@admin_router.delete("/{notice_id}", summary="删除公告/热点")
async def admin_delete_notice(notice_id: int):
    from database.db import get_park_notice, delete_park_notice

    if not get_park_notice(notice_id):
        raise HTTPException(status_code=404, detail="内容不存在")
    if not delete_park_notice(notice_id):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"code": 200, "msg": "已删除"}


# ============================================================
# 商家端：游客反馈查看
# ============================================================
@admin_router.get("/feedback", summary="游客反馈列表")
async def admin_list_feedback(page: int = 1, page_size: int = 20):
    from database.db import list_user_feedback, count_user_feedback

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    return {
        "code": 200,
        "data": {
            "total": count_user_feedback(),
            "page": page,
            "page_size": page_size,
            "items": [_feedback_to_view(f) for f in list_user_feedback(page=page, page_size=page_size)],
        },
    }
