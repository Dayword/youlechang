# ============================================================
# 高德地图 Web 服务工具集 - LangChain Tool 封装（8 个能力）
# ------------------------------------------------------------
# 1. amap_geocode      地理编码（文字地址 → 经纬度坐标）
# 2. amap_regeocode    逆地理编码（经纬度 → 详细地址）
# 3. amap_place_search POI 关键字搜索（查找地点）
# 4. amap_place_around 周边 POI 搜索（周边设施）
# 5. amap_direction    路径规划（驾车/步行/公交，返回距离耗时）
# 6. amap_staticmap    静态地图（生成后端代理图片地址）
# 7. amap_weather      天气查询（实时/预报）
# 8. amap_inputtips    输入提示（地址联想补全）
#
# 设计要点：
# - Web-Service Key 从 config 读取（环境变量注入），禁止硬编码、禁止下发前端；
# - 地图跳转链接(uri.amap.com) / 静态图地址统一由本模块生成，模型不编造链接；
# - 静态图通过后端代理 /amap/staticmap 返回，key 不暴露给浏览器；
# - 每次调用记录配额日志，达到每日阈值后 amap_quota_exceeded() 返回 True，
#   由 MapAgent 降级为纯 RAG 文本回答。
# ============================================================
import json
import logging
import time
import threading
from urllib.parse import quote
from typing import List, Union

import requests
from langchain_core.tools import tool

from config import AMAP_WEB_SERVICE_KEY, AMAP_BASE_URL, AMAP_TIMEOUT, AMAP_DAILY_QUOTA

logger = logging.getLogger(__name__)

# 静态图后端代理路径（FastAPI 在 api/amap.py 实现，负责携带 key 请求高德）
STATICMAP_PROXY_PATH = "/amap/staticmap"


# ============================================================
# 工具调用状态（保存最近一次工具调用产生的地图资源，供 MapAgent 回读）
# ============================================================
class AmapToolState:
    """记录最近一次高德工具调用产生的地图资源"""

    def __init__(self):
        self.lock = threading.Lock()
        self.last = {}  # {"map_url": str, "map_image": str, "extra_info": dict}

    def set(self, map_url: str = "", map_image: str = "", extra_info: dict = None):
        """
        记录工具调用产生的地图资源（合并模式，不覆盖已有非空字段）
        - Agent 多步调用工具时，map_url/map_image 保留最后产生的非空值（中途工具失败不覆盖）
        - extra_info 按 key 合并，避免后调用的工具覆盖先前的 POI/路线数据
        """
        with self.lock:
            cur = self.last
            self.last = {
                "map_url": (map_url or cur.get("map_url", "")) or "",
                "map_image": (map_image or cur.get("map_image", "")) or "",
                "extra_info": {**cur.get("extra_info", {}), **(extra_info or {})},
            }

    def get(self) -> dict:
        with self.lock:
            return dict(self.last)


amap_state = AmapToolState()


# ============================================================
# 工具执行进度回调（地图Agent执行期间，实时推送思考状态到前端）
# ============================================================
_progress_cb = None
_progress_lock = threading.Lock()

# 各接口对应的思考提示文案（_amap_get 统一入口处触发）
AMAP_PROGRESS_TEXT = {
    "/geocode/geo": "📍 正在地理编码：把地址转换为坐标...",
    "/geocode/regeo": "📍 正在逆地理编码：解析坐标对应地址...",
    "/place/text": "🔎 正在 POI 搜索相关地点...",
    "/place/around": "🍽️ 正在搜索周边设施...",
    "/direction/walking": "🚶 正在规划步行路线，计算距离与耗时...",
    "/direction/driving": "🚗 正在规划驾车路线，计算距离与耗时...",
    "/direction/transit/integrated": "🚌 正在规划公交路线，计算距离与耗时...",
    "/weather/weatherInfo": "🌤️ 正在查询实时天气...",
    "/assistant/inputtips": "💡 正在联想补全地址...",
}


def set_progress_cb(cb):
    """设置/清除进度回调（cb(text)），供 MapAgent 执行期间实时上报思考状态"""
    global _progress_cb
    with _progress_lock:
        _progress_cb = cb


def _notify_progress(path: str):
    """高德接口请求前通知进度（回调异常不影响主流程）"""
    with _progress_lock:
        cb = _progress_cb
    if cb:
        try:
            cb(AMAP_PROGRESS_TEXT.get(path, f"🔧 正在调用高德接口 {path}..."))
        except Exception:
            pass


# ============================================================
# 配额监控与调用日志
# ============================================================
def _daily_key() -> str:
    return f"amap:{time.strftime('%Y-%m-%d')}"


def _record_call(path: str):
    """记录一次高德 API 调用（Redis 计数器 + 日志），用于配额监控"""
    try:
        from core.cache import redis_cache
        redis_cache.incr_counter(_daily_key())
        redis_cache.incr_counter(f"{_daily_key()}:{path}")
        total = redis_cache.get_counter(_daily_key())
        logger.info(f"🗺️ 高德API调用 {path} | 当日累计 {total}/{AMAP_DAILY_QUOTA}")
    except Exception as e:
        logger.warning(f"高德调用计数失败: {e}")


def amap_quota_exceeded() -> bool:
    """当日高德调用次数是否已达配额阈值（达到后降级为纯 RAG）"""
    try:
        from core.cache import redis_cache
        return redis_cache.get_counter(_daily_key()) >= AMAP_DAILY_QUOTA
    except Exception:
        return False


# ============================================================
# 高德统一请求封装（超时/异常/status 校验）
# ============================================================
def _amap_get(path: str, params: dict) -> dict:
    """
    统一请求高德 Web 服务接口
    返回: {"status": "1", ...} 正常响应
          {"status": "0", "info": 错误原因} 异常/失败
    """
    if not AMAP_WEB_SERVICE_KEY:
        return {"status": "0", "info": "AMAP_KEY_MISSING: 未配置高德Web服务Key"}
    if amap_quota_exceeded():
        return {"status": "0", "info": "AMAP_QUOTA_EXCEEDED: 当日配额已用完"}

    # 请求前推送思考进度（如"正在规划驾车路线..."）
    _notify_progress(path)

    req_params = dict(params)
    req_params["key"] = AMAP_WEB_SERVICE_KEY
    try:
        resp = requests.get(f"{AMAP_BASE_URL}{path}", params=req_params, timeout=AMAP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.Timeout:
        logger.error(f"高德接口超时: {path} (>{AMAP_TIMEOUT}s)")
        return {"status": "0", "info": f"AMAP_TIMEOUT: 高德接口超时"}
    except Exception as e:
        logger.error(f"高德接口请求失败: {path} - {e}")
        return {"status": "0", "info": f"AMAP_REQUEST_FAILED: {e}"}

    _record_call(path)
    if str(data.get("status")) != "1":
        info = data.get("info", "未知错误")
        logger.warning(f"高德接口返回异常 {path}: status={data.get('status')} info={info}")
        # INVALID_USER_KEY 等鉴权错误记录详细日志，方便排查 IP 白名单问题
        if data.get("infocode") in ("10001", "10003", "10008", "10010"):
            logger.error(f"⚠️ 高德鉴权失败(infocode={data.get('infocode')})：请检查Key有效性及控制台IP白名单")
        return {"status": "0", "info": f"AMAP_API_ERROR[{data.get('infocode', '?')}]: {info}"}
    return data


# ============================================================
# 链接生成工具（统一由 Python 生成，模型只引用不编造）
# ============================================================
def build_navigation_url(lng: str, lat: str, name: str = "", mode: str = "driving") -> str:
    """生成 uri.amap.com 导航跳转链接（公开链接，无需 key）"""
    mode_map = {"walking": "walking", "transit": "transit", "bicycling": "walking"}
    m = mode_map.get(mode, "driving")
    return f"https://uri.amap.com/navigation?to={lng},{lat},{quote(str(name))}&mode={m}&coordinate=gaode&callnative=0"


def build_marker_url(lng: str, lat: str, name: str = "") -> str:
    """生成 uri.amap.com 地点标记链接（公开链接，无需 key）"""
    return f"https://uri.amap.com/marker?position={lng},{lat}&name={quote(str(name))}&src=amusement_park&callnative=0"


def build_staticmap_url(location: str, markers: str = "", zoom: int = 16, size: str = "600*300") -> str:
    """生成后端静态图代理地址（key 由后端附加，前端拿不到）"""
    url = f"{STATICMAP_PROXY_PATH}?location={location}&zoom={zoom}&size={size}"
    if markers:
        url += f"&markers={quote(markers)}"
    return url


# ============================================================
# 1. 地理编码：文字地址 → 经纬度坐标
# ============================================================
@tool
def amap_geocode(address: str) -> str:
    """地理编码：将文字地址（如"北京市怀柔区万达广场2号门"）转换为经纬度坐标。用户询问某地点在哪里、位置、坐标时使用。"""
    data = _amap_get("/geocode/geo", {"address": address, "output": "json"})
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"地理编码失败：{data.get('info', '未知错误')}"

    geocodes = data.get("geocodes") or []
    if not geocodes:
        amap_state.set()
        return f"未找到地址「{address}」的坐标，请尝试更详细/规范的地址。"

    g = geocodes[0]
    location = g.get("location", "")
    formatted = g.get("formatted_address", address)
    lng, lat = (location.split(",") + ["", ""])[:2]
    map_url = build_marker_url(lng, lat, g.get("level", address))
    amap_state.set(
        map_url=map_url,
        map_image=build_staticmap_url(location, markers=f"mid,,1:{location}", zoom=16),
        extra_info={"poi": {"name": address, "address": formatted, "location": location, "adcode": g.get("adcode", "")}},
    )
    return json.dumps({
        "name": address, "location": location, "formatted_address": formatted,
        "level": g.get("level", ""), "map_url": map_url,
    }, ensure_ascii=False)


# ============================================================
# 2. 逆地理编码：经纬度 → 详细地址
# ============================================================
@tool
def amap_regeocode(location: str) -> str:
    """逆地理编码：将经纬度坐标（格式"经度,纬度"，如"116.397428,39.90923"）转换为详细地址和周边地标。"""
    data = _amap_get("/geocode/regeo", {"location": location, "output": "json"})
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"逆地理编码失败：{data.get('info', '未知错误')}"

    regeo = data.get("regeocode") or {}
    formatted = regeo.get("formatted_address", "")
    pois = (regeo.get("pois") or [])[:3]
    poi_text = "、".join([p.get("name", "") for p in pois])
    amap_state.set(
        map_url=build_marker_url(*(location.split(",") + [""])[:2], name=formatted[:20]),
        extra_info={"regeocode": {"address": formatted, "pois": pois}},
    )
    return json.dumps({
        "location": location, "formatted_address": formatted,
        "附近地标": poi_text,
    }, ensure_ascii=False)


# ============================================================
# 3. POI 关键字搜索：按名称/类型查找地点
# ============================================================
@tool
def amap_place_search(keywords: str, city: str = "") -> str:
    """POI搜索：按关键字查找地点（如"万达广场"、"游乐园"、"餐厅"），可选城市名。返回地点名称、地址、电话、坐标。"""
    params = {"keywords": keywords, "offset": "5", "page": "1", "output": "json"}
    if city:
        params["city"] = city
    data = _amap_get("/place/text", params)
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"POI搜索失败：{data.get('info', '未知错误')}"

    pois = data.get("pois") or []
    if not pois:
        amap_state.set()
        return f"未搜索到与「{keywords}」相关的地点。"

    results = []
    for p in pois[:5]:
        results.append({
            "name": p.get("name", ""), "address": p.get("address", ""),
            "location": p.get("location", ""), "tel": p.get("tel", ""), "type": p.get("type", ""),
        })
    first = pois[0]
    lng, lat = (first.get("location", "").split(",") + ["", ""])[:2]
    map_url = build_marker_url(lng, lat, first.get("name", ""))
    amap_state.set(
        map_url=map_url,
        map_image=build_staticmap_url(first.get("location", ""), markers=f"mid,,A:{first.get('location', '')}", zoom=15),
        extra_info={"pois": results},
    )
    return json.dumps({"keyword": keywords, "count": len(results), "pois": results}, ensure_ascii=False)


# ============================================================
# 4. 周边 POI 搜索：某坐标周边设施
# ============================================================
@tool
def amap_place_around(location: str, keywords: str = "", radius: str = "3000") -> str:
    """周边搜索：查找某经纬度坐标（格式"经度,纬度"）周边的地点/设施（餐厅、酒店、停车场等），radius为半径米数。"""
    params = {"location": location, "offset": "5", "page": "1", "radius": radius, "output": "json"}
    if keywords:
        params["keywords"] = keywords
    data = _amap_get("/place/around", params)
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"周边搜索失败：{data.get('info', '未知错误')}"

    pois = data.get("pois") or []
    if not pois:
        amap_state.set()
        return f"坐标 {location} 周边{radius}米内未找到相关地点。"

    results = []
    for p in pois[:5]:
        results.append({
            "name": p.get("name", ""), "address": p.get("address", ""),
            "location": p.get("location", ""), "tel": p.get("tel", ""), "type": p.get("type", ""),
        })
    first = pois[0]
    amap_state.set(
        map_url=build_marker_url(*(first.get("location", "").split(",") + ["", ""])[:2], name=first.get("name", "")),
        map_image=build_staticmap_url(location, markers=f"mid,,A:{location},small,0:B:{first.get('location', '')}", zoom=15),
        extra_info={"pois": results, "center": location, "radius": radius},
    )
    return json.dumps({"center": location, "radius": radius, "count": len(results), "pois": results}, ensure_ascii=False)


# ============================================================
# 5. 路径规划：驾车/步行/公交路线
# ============================================================
@tool
def amap_direction(origin: str, destination: str, mode: str = "driving") -> str:
    """路径规划：计算两点（格式均为"经度,纬度"）之间的路线。mode可选 driving(驾车)/walking(步行)/transit(公交)。返回距离与预计耗时。"""
    mode = mode.lower()
    if mode == "walking":
        path = "/direction/walking"
        params = {"origin": origin, "destination": destination, "output": "json"}
    elif mode == "transit":
        path = "/direction/transit/integrated"
        params = {"origin": origin, "destination": destination, "city": "北京", "cityd": "北京", "output": "json"}
    else:
        path = "/direction/driving"
        params = {"origin": origin, "destination": destination, "output": "json"}

    data = _amap_get(path, params)
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"路径规划失败：{data.get('info', '未知错误')}"

    route = data.get("route") or {}
    paths = route.get("paths") or []
    if not paths:
        amap_state.set()
        return "未规划出可行路线。"

    p = paths[0]
    distance_m = int(float(p.get("distance", 0)))
    duration_s = int(float(p.get("duration", 0)))
    km = round(distance_m / 1000, 1)
    minutes = round(duration_s / 60)
    steps_count = len(p.get("steps") or [])
    mode_name = {"driving": "驾车", "walking": "步行", "transit": "公交"}.get(mode, mode)

    # 导航跳转链接（终点地图标注）
    dest_name = ""
    if mode == "transit":
        dest_name = "目的地"
    map_url = build_navigation_url(*(destination.split(",") + ["", ""])[:2], name=dest_name, mode=mode)
    amap_state.set(
        map_url=map_url,
        extra_info={"route": {"mode": mode_name, "distance_km": km, "duration_min": minutes, "steps": steps_count}},
    )
    return json.dumps({
        "mode": mode_name, "distance_km": km, "duration_min": minutes,
        "steps": steps_count, "map_url": map_url,
    }, ensure_ascii=False)


# ============================================================
# 6. 静态地图：生成地图图片（后端代理，key 不下发）
# ============================================================
@tool
def amap_staticmap(location: str, markers: str = "",
                   zoom: Union[str, int] = "16", size: str = "600*300") -> str:
    """静态地图：为经纬度坐标（格式"经度,纬度"）生成一张静态地图图片地址，可用于展示地点位置。markers为标记点（格式如"mid,,A:经度,纬度"），不传则仅显示中心点。"""
    if markers and ":" not in markers and "," in markers:
        # 兼容裸坐标输入（如 "116.49,39.86"），自动补标准标记样式前缀
        markers = f"mid,,A:{markers}"
    # zoom 参数容错：模型有时会传数字（16）而不是字符串（"16"），
    # 早期签名只声明 str，pydantic 直接校验失败会让整个地图 Agent 报错降级
    try:
        zoom_int = int(float(str(zoom).strip() or 16))
    except Exception:
        zoom_int = 16
    img_url = build_staticmap_url(location, markers=markers, zoom=zoom_int, size=size)
    amap_state.set(
        map_image=img_url,
        extra_info={"staticmap": {"location": location, "zoom": zoom_int, "size": size}},
    )
    return f"静态地图图片地址已生成：{img_url}（可直接在回答中告知用户查看地图图片）"


# ============================================================
# 7. 天气查询：城市实时天气
# ============================================================
@tool
def amap_weather(city: str, extensions: str = "base") -> str:
    """天气查询：查询指定城市（城市名或adcode）的实时天气（base）或预报（all）。"""
    data = _amap_get("/weather/weatherInfo", {"city": city, "extensions": extensions, "output": "json"})
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"天气查询失败：{data.get('info', '未知错误')}"

    lives = data.get("lives") or []
    if not lives:
        amap_state.set()
        return f"未查询到城市「{city}」的天气信息。"

    w = lives[0]
    result = {
        "city": w.get("city", city), "weather": w.get("weather", ""),
        "temperature": f"{w.get('temperature', '?')}℃",
        "wind": f"{w.get('winddirection', '')}风{w.get('windpower', '')}级",
        "humidity": f"{w.get('humidity', '?')}%",
        "report_time": w.get("reporttime", ""),
    }
    amap_state.set(extra_info={"weather": result})
    return json.dumps(result, ensure_ascii=False)


# ============================================================
# 8. 输入提示：地址联想补全
# ============================================================
@tool
def amap_inputtips(keywords: str, city: str = "") -> str:
    """输入提示：根据输入的关键字联想补全地址/地点名称（如输入"万达"联想出"万达广场"），返回候选地点列表。"""
    params = {"keywords": keywords, "output": "json"}
    if city:
        params["city"] = city
    data = _amap_get("/assistant/inputtips", params)
    if str(data.get("status")) != "1":
        amap_state.set()
        return f"输入提示失败：{data.get('info', '未知错误')}"

    tips = data.get("tips") or []
    if not tips:
        amap_state.set()
        return f"未找到与「{keywords}」相关的联想结果。"

    results = []
    for t in tips[:5]:
        if t.get("location"):
            results.append({"name": t.get("name", ""), "location": t.get("location", ""),
                            "address": t.get("address", ""), "district": t.get("district", "")})
    first = next((t for t in results if t.get("location")), None)
    map_url = ""
    if first:
        lng, lat = (first["location"].split(",") + ["", ""])[:2]
        map_url = build_marker_url(lng, lat, first["name"])
    amap_state.set(map_url=map_url, extra_info={"tips": results})
    return json.dumps({"keyword": keywords, "count": len(results), "tips": results}, ensure_ascii=False)


# ============================================================
# 工具注册
# ============================================================
def get_amap_tools() -> List:
    """返回全部高德地图 LangChain Tool 对象列表（供 AgentExecutor 注册）"""
    return [
        amap_geocode,
        amap_regeocode,
        amap_place_search,
        amap_place_around,
        amap_direction,
        amap_staticmap,
        amap_weather,
        amap_inputtips,
    ]
