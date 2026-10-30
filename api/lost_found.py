# ============================================================
# 失物招领 API 路由
# 游客端（微信小程序）:
#   POST /lost-found/items      用户报失（名称 + 照片，服务端自动生成时间）
#                               → 落 pending 待审核，审核通过后才对外展示
#   GET  /lost-found/items      失物列表（失物认领弹窗数据源，仅已审核通过）
# 商家端（运营后台 PC，需管理员登录）:
#   POST /admin/lost-found/items          商家登记失物（名称 + 照片）
#   GET  /admin/lost-found/items          失物统一管理列表（游客报失+商家登记）
#   PUT  /admin/lost-found/items/{id}/status   审核通过/驳回、标记已认领/撤销
#   DELETE /admin/lost-found/items/{id}        删除失物（同步清理照片文件）
#
# 状态机（游客能看到的只有 lost / claimed）：
#   pending 待审核 ──通过──> lost 待认领 ──认领──> claimed 已认领
#                        └──驳回──> rejected 已驳回
#
# 照片统一保存到 static/lost_found/ 目录，通过 /static 静态路由访问，
# 商家 PC 端与游客小程序共用同一数据源，实现失物统一管理。
# ============================================================
import os
import time
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from core.auth import get_current_admin

logger = logging.getLogger(__name__)

# 游客端路由（无需登录）
router = APIRouter(prefix="/lost-found", tags=["失物招领"])

# 商家端路由（需管理员登录）
admin_router = APIRouter(
    prefix="/admin/lost-found",
    tags=["失物招领管理"],
    dependencies=[Depends(get_current_admin)],
)

# 照片存储目录（挂载于 /static 静态路由下访问）
PHOTO_DIR = os.path.join("static", "lost_found")
os.makedirs(PHOTO_DIR, exist_ok=True)

# 允许的照片类型
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB


def _item_to_view(item: dict) -> dict:
    """数据库行 -> 前端展示结构（时间转 ISO 字符串，图片补全静态访问路径）

    ai：照片的模型识别结果（未识别/识别失败时各字段为空）。
    游客端一般不展示，商家端用于按特征比对、辅助认领匹配。
    """
    created_at = item.get("created_at")
    return {
        "id": item["id"],
        "name": item["name"],
        "photo_url": item.get("photo_url") or "",
        "status": item.get("status") or "lost",
        "source": item.get("source") or "user",
        "reporter": item.get("reporter") or "",
        "created_at": str(created_at) if created_at else "",
        "ai": _ai_of(item),
    }


def _ai_of(item: dict) -> dict:
    """把库里的 AI 识别列还原成前端友好的结构"""
    feats = [f for f in str(item.get("ai_features") or "").split("；") if f.strip()]
    return {
        "category": item.get("ai_category") or "",
        "color": item.get("ai_color") or "",
        "brand": item.get("ai_brand") or "",
        "features": feats,
        "visible_text": item.get("ai_text") or "",
    }


async def _read_photo(photo: UploadFile) -> bytes:
    """校验照片类型与大小，返回原始字节（不落盘）"""
    if photo.content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"仅支持图片上传（jpg/png/webp/gif/bmp），当前类型: {photo.content_type}",
        )
    content = await photo.read()
    if len(content) > MAX_PHOTO_SIZE:
        raise HTTPException(status_code=413, detail="照片过大，最大支持 10MB")
    return content


def _store_photo(content: bytes, filename: str = "") -> str:
    """把照片字节写入 static/lost_found/，返回静态访问路径"""
    ext = os.path.splitext(filename or "")[1].lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        ext = ".jpg"
    name = f"lf_{int(time.time() * 1000)}_{os.urandom(4).hex()}{ext}"
    path = os.path.join(PHOTO_DIR, name)
    with open(path, "wb") as f:
        f.write(content)
    return f"/static/lost_found/{name}"


async def _save_photo(photo: UploadFile) -> str:
    """校验并保存照片文件，返回静态访问路径 /static/lost_found/xxx.jpg"""
    content = await _read_photo(photo)
    return _store_photo(content, photo.filename)


def _recognize(content: bytes, hint: str = "", enrich: bool = False) -> dict:
    """识别照片（best-effort）：识别失败返回空结果，绝不阻断报失。

    选图时前端已调过 /lost-found/analyze-photo，服务端按图片内容哈希缓存，
    这里的第二次调用通常直接命中缓存（毫秒级），不会拖慢提交。

    enrich=True 表示这是"提交时的补识别"：只给短超时，
    保证「提交报失」不会被视觉模型的冷启动拖住（识别是增强项，可缺）。
    """
    try:
        from services.vision_service import recognize_lost_photo, ENRICH_TIMEOUT
        return recognize_lost_photo(
            content, hint=hint,
            timeout=ENRICH_TIMEOUT if enrich else None)
    except Exception as e:                     # pragma: no cover - 兜底
        logger.warning(f"[失物招领] 照片识别异常（已忽略）: {e}")
        return {}


@router.post("/analyze-photo", summary="失物照片预识别（选完照片即调用，自动填名用）")
async def analyze_lost_photo(
    photo: UploadFile = File(..., description="失物照片"),
    name: str = File("", description="用户已填的名称（可选，作为识别提示）"),
):
    """把照片交给多模态大模型识别，返回结构化特征供前端自动填入名称。

    与提交接口的区别：**不落盘、不入库**，用户取消报失也不会留下垃圾文件。
    服务端按图片内容哈希缓存 5 分钟，所以提交时的第二次识别通常直接命中缓存。

    识别失败不报错（HTTP 仍 200）：返回 ok=false，前端退回「手动填写名称」。
    """
    content = await _read_photo(photo)
    result = _recognize(content, hint=name)
    ok = bool(result.get("ok"))
    logger.info(f"[失物招领] 照片预识别 ok={ok} "
                f"name={result.get('suggested_name') or '-'} "
                f"conf={result.get('confidence', 0)}")
    return {
        "code": 200,
        "msg": "识别完成" if ok else (result.get("error") or "暂时无法识别，请手动填写名称"),
        "data": result or {},
    }


# ============================================================
# 游客端
# ============================================================
@router.post("/items", summary="用户报失（名称 + 照片，时间自动生成）")
async def user_report_lost(
    name: str = File("", description="失物名称（留空时用 AI 识别结果自动补全）"),
    photo: UploadFile = File(None, description="失物照片（可选）"),
    reporter: str = File("", description="报失人称呼（可选）"),
):
    """游客报失：记录名称/照片/时间，与商家登记统一进入 lost_found_items

    照片会交给多模态模型识别，识别结果随单落库，供商家端按特征比对。
    识别失败不影响报失（AI 字段留空，名称由用户自己填）。
    """
    from database.db import create_lost_item

    name = (name or "").strip()
    content = await _read_photo(photo) if photo is not None else b""
    ai = _recognize(content, hint=name, enrich=True) if content else {}

    # 名称留空时用识别结果兜底（前端已预填，这里是服务端二次保障）
    if not name:
        name = str(ai.get("suggested_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写失物名称（或上传一张能看清物品的照片）")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="失物名称过长（最多 100 字）")

    photo_url = _store_photo(content, photo.filename) if content else ""

    # 游客报失落 pending：必须管理员在商家端审核通过（status 变 lost）后，
    # 才会出现在小程序的失物认领列表里，避免任意内容不经审核就对外展示。
    item = create_lost_item(name=name, photo_url=photo_url, source="user",
                            reporter=reporter.strip(), ai=ai, status="pending")
    if not item:
        raise HTTPException(status_code=500, detail="报失失败，请稍后重试")

    logger.info(f"[失物招领] 游客报失(待审核): {name}, 照片: {photo_url or '无'}, "
                f"AI识别: {'有' if ai else '无'}")
    return {
        "code": 200,
        "msg": "报失已提交，工作人员审核通过后会出现在失物认领列表",
        "data": _item_to_view(item),
    }


@router.get("/items", summary="失物列表（失物认领弹窗数据源，仅已审核通过）")
async def user_list_lost(status: str = ""):
    """游客认领弹窗展示全部失物（名称/时间/图片）。

    只放行已通过审核的状态：默认 lost（待认领），也可显式查 claimed（已认领）。
    pending（待审核）/ rejected（已驳回）必须挡在这里 —— 否则审核形同虚设，
    调用方只要自己传 status=pending 就能把未审核的内容拉出来。
    """
    from database.db import list_lost_items

    if status not in ("lost", "claimed"):
        status = "lost"
    items = list_lost_items(status=status, page=1, page_size=200)
    return {"code": 200, "data": [_item_to_view(i) for i in items]}


# ============================================================
# 商家端（统一管理：游客报失 + 商家登记）
# ============================================================
@admin_router.post("/items", summary="商家登记失物（名称 + 照片，时间自动生成）")
async def merchant_add_lost(
    name: str = File("", description="失物名称（留空时用 AI 识别结果自动补全）"),
    photo: UploadFile = File(None, description="失物照片（可选）"),
):
    """商家登记拾获的失物，与游客报失统一进入 lost_found_items

    与游客端同样走模型识别：商家拍下拾获物，AI 自动填好名称与特征，
    后续游客报失时可凭特征比对更快匹配。
    """
    from database.db import create_lost_item

    name = (name or "").strip()
    content = await _read_photo(photo) if photo is not None else b""
    ai = _recognize(content, hint=name, enrich=True) if content else {}

    if not name:
        name = str(ai.get("suggested_name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="请填写失物名称（或上传一张能看清物品的照片）")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="失物名称过长（最多 100 字）")

    photo_url = _store_photo(content, photo.filename) if content else ""

    item = create_lost_item(name=name, photo_url=photo_url, source="merchant",
                            reporter="商家", ai=ai)
    if not item:
        raise HTTPException(status_code=500, detail="登记失败，请稍后重试")

    logger.info(f"[失物招领] 商家登记: {name}, 照片: {photo_url or '无'}, "
                f"AI识别: {'有' if ai else '无'}")
    return {
        "code": 200,
        "msg": "失物已登记",
        "data": _item_to_view(item),
    }


@admin_router.get("/items", summary="失物统一管理列表（分页 + 状态/来源筛选）")
async def admin_list_lost(
    page: int = 1,
    page_size: int = 20,
    status: str = "",      # lost / claimed / 空=全部
    source: str = "",      # user / merchant / 空=全部
    keyword: str = "",     # 失物名称关键词
):
    """商家 PC 端失物招领模块：游客报失 + 商家登记统一列表管理"""
    from database.db import list_lost_items, count_lost_items

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total = count_lost_items(status=status, source=source, keyword=keyword)
    items = list_lost_items(
        status=status, source=source, page=page, page_size=page_size, keyword=keyword,
    )
    return {
        "code": 200,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "items": [_item_to_view(i) for i in items],
        },
    }


# 状态机：pending 待审核 →（通过）lost 待认领 →（认领）claimed 已认领
#                      →（驳回）rejected 已驳回
# 已驳回的可重新"通过"回 lost；已认领的也可撤销回 lost。
_STATUS_LABELS = {
    "pending": "待审核",
    "lost": "待认领",
    "claimed": "已认领",
    "rejected": "已驳回",
}


class LostStatusRequest(BaseModel):
    status: str  # pending 待审核 / lost 待认领 / claimed 已认领 / rejected 已驳回


@admin_router.put("/items/{item_id}/status",
                  summary="更新失物状态（审核通过/驳回、标记已认领/撤销）")
async def admin_update_lost_status(item_id: int, req: LostStatusRequest):
    from database.db import get_lost_item, update_lost_item_status

    if req.status not in _STATUS_LABELS:
        raise HTTPException(status_code=400, detail="无效的状态值")
    if not get_lost_item(item_id):
        raise HTTPException(status_code=404, detail="失物不存在")
    if not update_lost_item_status(item_id, req.status):
        raise HTTPException(status_code=500, detail="状态更新失败")
    return {"code": 200, "msg": f"已标记为{_STATUS_LABELS[req.status]}"}


@admin_router.delete("/items/{item_id}", summary="删除失物（同步清理照片文件）")
async def admin_delete_lost(item_id: int):
    from database.db import get_lost_item, delete_lost_item

    item = get_lost_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="失物不存在")

    # 同步清理照片文件
    photo_url = item.get("photo_url") or ""
    if photo_url.startswith("/static/"):
        file_path = os.path.join(".", photo_url.lstrip("/"))
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"照片文件删除失败: {file_path}, {e}")

    if not delete_lost_item(item_id):
        raise HTTPException(status_code=500, detail="删除失败")
    return {"code": 200, "msg": "已删除"}
