# ============================================================
# 高德地图 API 路由 - /amap/*
# 仅提供静态图代理（Web-Service Key 由后端携带，不下发前端）
# 前端只消费后端返回的公开 uri.amap.com 链接和本代理图片地址
# ============================================================
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
import logging

import requests

from config import AMAP_WEB_SERVICE_KEY, AMAP_BASE_URL, AMAP_TIMEOUT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/amap", tags=["高德地图"])


@router.get("/staticmap", summary="静态地图代理（key 不下发前端）")
async def staticmap(
    location: str = Query(..., description="中心点经纬度，格式: 经度,纬度"),
    zoom: int = Query(16, description="缩放级别 3-18"),
    size: str = Query("600*300", description="图片尺寸 宽*高"),
    markers: str = Query("", description="标记点，如 mid,,A:116.397,39.909"),
):
    """服务端代理高德静态图接口，返回图片字节，前端无需携带 key"""
    if not AMAP_WEB_SERVICE_KEY:
        raise HTTPException(status_code=503, detail="未配置高德Web服务Key")

    # 容错：markers 为裸坐标（如 "116.49,39.86"）时自动补标准标记样式前缀
    if markers and ":" not in markers and "," in markers:
        markers = f"mid,,A:{markers}"

    params = {
        "location": location,
        "zoom": zoom,
        "size": size,
        "key": AMAP_WEB_SERVICE_KEY,
    }
    if markers:
        params["markers"] = markers

    try:
        resp = requests.get(f"{AMAP_BASE_URL}/staticmap", params=params, timeout=AMAP_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="高德静态图请求超时")
    except Exception as e:
        logger.error(f"高德静态图请求失败: {e}")
        raise HTTPException(status_code=502, detail=f"高德静态图请求失败: {str(e)}")

    content_type = resp.headers.get("Content-Type", "image/jpeg")
    # 高德返回文本（如 JSON 错误信息）时视为失败
    if "image" not in content_type:
        raise HTTPException(status_code=502, detail="高德静态图接口返回异常")
    return Response(content=resp.content, media_type=content_type)
