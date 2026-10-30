# ============================================================
# 图片识别服务 (services/vision_service.py)
# ------------------------------------------------------------
# 用途：失物招领场景下，把用户/商家上传的照片交给**多模态大模型本身**识别，
#      产出结构化特征（类别 / 主色 / 品牌 / 可见文字 / 外观特征 / 建议名称）。
#
# 用在哪：
#   ① 选完照片立即识别 → 自动填好「失物名称」，用户不必手打（也避免填成"123"）；
#   ② 提交时报失单入库带上识别结果 → 商家端可按特征比对、辅助匹配。
#
# 为什么不用 OCR：项目已接入原生多模态模型（core.llm_client.invoke_vision，
#   LLM_VISION_MODEL=deepseek-v4-flash-vision-exp），图片直接入参。
#   失物招领要的是"这是什么物品、长什么样"，而不是"图上有哪些字"，
#   所以走多模态识别而不是 PaddleOCR。
#
# 设计要点（对照 _verify_vision_service.py 的单测）：
#   1. **严格 JSON 输出 + 健壮解析**：去 ``` 围栏 → 取首个 { 到末个 } → json.loads；
#      解析失败不让异常冒泡，返回 ok=False。
#   2. **按图片内容哈希缓存**：选图预识别 + 提交入库是两次调用，同一张图会识别两遍。
#      内容 md5 做键、TTL 5 分钟，第二次直接命中，省一次视觉模型往返（实测 2~4s）。
#      多进程部署时缓存不共享，只是多花一次调用，不影响正确性。
#   3. **失败必须优雅**：识别失败绝不阻断报失流程 —— 返回 ok=False，
#      调用方降级为"用户手填名称"，功能仍可用。
# ============================================================
import base64
import hashlib
import io
import json
import logging
import re
import threading
import time
from typing import Optional

from config import LLM_VISION_MODEL

logger = logging.getLogger(__name__)

# 识别结果的缓存（内容哈希 -> 结果），同图短时间内不重复调模型
_CACHE_TTL = 300          # 5 分钟：够覆盖"选图 → 填表 → 提交"的全过程
_CACHE_MAX = 200          # 上限，防内存无界增长
_cache: dict = {}
_cache_lock = threading.Lock()

# 识别输出的 token 上限：结构化 JSON 很短，给 512 足够；
# 不给上限时模型容易长篇描述图片，白等好几秒。
RECOGNIZE_MAX_TOKENS = 512

# 送模型的图片长边。实测同一张图：1536 → 3.29s，1024 → 0.93~1.45s，768 → 1.58s。
# 视觉 token 随边长平方增长，缩到 1024 后识别质量没有可见下降（失物名称/特征仍准确）。
RECOGNIZE_MAX_SIDE = 1024

# 超时（秒）。两个场景分开设，因为它们对"等待"的容忍度完全不同：
#   - 预识别（用户刚选完照片，前端非阻塞展示"识别中"）→ 给足 30s。
#     ⚠️ 进程内**第一次**视觉调用含客户端初始化+TLS+网关预热，实测可达 25s，
#     超时给小了会把这次冷启动直接判死。
#   - 提交入库时的补识别 → 只给 8s 兜底（此时通常已命中缓存、耗时毫秒级；
#     真未命中也不该让"提交报失"跟着卡住，识别是增强项）。
RECOGNIZE_TIMEOUT = 30
ENRICH_TIMEOUT = 8

_RECOGNIZE_SYSTEM = (
    "你是游乐园失物招领的登记助手。用户会上传一张失物的照片，"
    "你要看清照片里的物品，并输出结构化识别结果，用于帮用户把失物名称填好。\n"
    "【只输出一个 JSON 对象】不要解释、不要 Markdown 围栏、不要多余文字。\n"
    "【绝对不许编造】看不清的字段留空字符串或空数组；"
    "看不清品牌就不要猜品牌，图中没有文字就不要编文字。"
)

_RECOGNIZE_PROMPT = """请识别这张失物照片，按下面的 JSON 结构输出（字段名必须完全一致）：

{{
  "suggested_name": "失物名称，10~20 字，含颜色+物品，例如「黑色 iPhone 手机」「蓝色儿童水杯」。看不清物品时写能确定的部分，无法判断则留空",
  "category": "从这些里选一个：手机数码 / 证件钱包 / 衣物鞋帽 / 钥匙 / 玩具 / 水杯餐具 / 眼镜 / 背包箱包 / 首饰配饰 / 其他",
  "color": "物品主色，如 黑色 / 白色 / 粉色 / 不确定",
  "brand": "能明确辨认出的品牌名；看不清或没有品牌则留空字符串",
  "features": ["外观特征，最多 5 条，如「屏幕右下角有裂纹」「挂着一个黄色小熊挂件」。看不清就给空数组"],
  "visible_text": "图中可辨认的文字（如姓名贴、编号、商标文字），没有则留空字符串",
  "confidence": 0.0
}}

其中 confidence 是你对本次识别的把握，取 0 到 1 之间的小数。
{hint}
只输出这个 JSON。"""


# ------------------------------------------------------------------
# 图片预处理
# ------------------------------------------------------------------
def image_to_data_url(content: bytes, max_side: int = 1536) -> str:
    """校验并压缩图片 → data URL（统一转 JPEG，长边缩到 max_side，便于多模态入参）

    与 api/chat.py 的图片问答共用同一套预处理，避免两处实现漂移。
    """
    from PIL import Image

    img = Image.open(io.BytesIO(content))
    if img.mode in ("RGBA", "LA", "P") or img.mode not in ("RGB", "L"):
        bg = Image.new("RGB", img.size, "white")
        if img.mode in ("RGBA", "LA"):
            img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1])
        else:
            bg.paste(img.convert("RGB"))
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------------
# 解析与缓存
# ------------------------------------------------------------------
def _extract_json(raw: str) -> Optional[dict]:
    """从模型输出里抠出 JSON 对象。推理/多模态模型爱加围栏或解释，必须容错。"""
    if not raw:
        return None
    text = raw.strip()
    # 去 ``` / ```json 围栏
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 退化：取首个 { 到末个 }
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(text[i:j + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _as_str(v, limit: int = 60) -> str:
    return str(v or "").strip()[:limit]


def _as_list(v, limit: int = 5, item_limit: int = 60) -> list:
    if not isinstance(v, list):
        return []
    out = []
    for it in v[:limit]:
        if isinstance(it, dict):
            it = it.get("text") or it.get("desc") or ""
        s = _as_str(it, item_limit)
        if s:
            out.append(s)
    return out


def _cache_get(key: str) -> Optional[dict]:
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > _CACHE_TTL:
        with _cache_lock:
            _cache.pop(key, None)
        return None
    return val


def _cache_set(key: str, val: dict) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # 简单淘汰：清掉过期项，仍满则丢掉最早的一半
            now = time.time()
            for k in [k for k, (ts, _) in _cache.items() if now - ts > _CACHE_TTL]:
                _cache.pop(k, None)
            if len(_cache) >= _CACHE_MAX:
                for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 2]:
                    _cache.pop(k, None)
        _cache[key] = (time.time(), val)


def _empty_result(error: str = "") -> dict:
    """识别失败/降级时的空结果（调用方按 ok=False 处理）"""
    return {"ok": False, "suggested_name": "", "category": "", "color": "",
            "brand": "", "features": [], "visible_text": "", "confidence": 0.0,
            "summary": "", "error": error}


def _to_summary(r: dict) -> str:
    """给人看的一行摘要，如「黑色 iPhone 手机 · 主色 黑色 · 屏幕右下角有裂纹」"""
    parts = [r.get("suggested_name") or r.get("category") or "已识别到物品"]
    if r.get("brand"):
        parts.append("品牌 " + r["brand"])
    if r.get("features"):
        parts.append("；".join(r["features"][:2]))
    return " · ".join(p for p in parts if p)[:120]


# ------------------------------------------------------------------
# 对外接口
# ------------------------------------------------------------------
def recognize_lost_photo(content: bytes, hint: str = "",
                         use_cache: bool = True, timeout: int = None) -> dict:
    """识别失物照片 → 结构化特征。

    Args:
        content: 图片原始字节
        hint:    额外提示（如用户已填的名称），会加进 prompt 帮助模型聚焦
        use_cache: 是否允许命中内容哈希缓存（提交入库时建议 True，避免重复识别）
        timeout: 覆盖默认超时（秒）。提交路径建议传 ENRICH_TIMEOUT(8)，
                 避免冷启动时把「提交报失」一起拖住。

    Returns:
        {ok, suggested_name, category, color, brand, features[], visible_text,
         confidence, summary, error}
        ok=False 时其余字段为空，调用方应降级为「用户手填」而不是报错。
    """
    if not content:
        return _empty_result("图片内容为空")

    key = hashlib.md5(content).hexdigest()
    if use_cache:
        hit = _cache_get(key)
        if hit:
            logger.info(f"[vision_service] 命中识别缓存 key={key[:8]}")
            return hit

    try:
        data_url = image_to_data_url(content, max_side=RECOGNIZE_MAX_SIDE)
    except Exception as e:
        logger.warning(f"[vision_service] 图片无法解析: {e}")
        return _empty_result(f"图片无法解析：{str(e)[:60]}")

    prompt_hint = ""
    if (hint or "").strip():
        prompt_hint = f"\n【用户已填写的名称（可参考，冲突时以图片为准）】{hint.strip()[:40]}\n"

    raw = ""
    try:
        from core.llm_client import llm_client
        raw = llm_client.invoke_vision(
            _RECOGNIZE_PROMPT.format(hint=prompt_hint),
            data_url,
            system=_RECOGNIZE_SYSTEM,
            max_tokens=RECOGNIZE_MAX_TOKENS,
            timeout=timeout or RECOGNIZE_TIMEOUT,
        )
    except Exception as e:
        # 视觉模型不可用（额度/网络/超时）绝不能阻断报失
        logger.warning(f"[vision_service] 视觉模型调用失败: {type(e).__name__}: {str(e)[:120]}")
        return _empty_result(f"识别服务暂不可用：{type(e).__name__}")

    obj = _extract_json(raw)
    if not obj:
        logger.warning(f"[vision_service] 识别结果非 JSON，原文前 120 字: {raw[:120]!r}")
        return _empty_result("识别结果格式异常")

    try:
        conf = float(obj.get("confidence") or 0.0)
    except Exception:
        conf = 0.0
    result = {
        "ok": True,
        "suggested_name": _as_str(obj.get("suggested_name"), 40),
        "category": _as_str(obj.get("category"), 20),
        "color": _as_str(obj.get("color"), 20),
        "brand": _as_str(obj.get("brand"), 30),
        "features": _as_list(obj.get("features")),
        "visible_text": _as_str(obj.get("visible_text"), 120),
        "confidence": max(0.0, min(1.0, conf)),
        "summary": "",
        "error": "",
    }
    result["summary"] = _to_summary(result)
    logger.info(f"[vision_service] 识别完成 model={LLM_VISION_MODEL} "
                f"name={result['suggested_name']!r} conf={result['confidence']:.2f} "
                f"features={len(result['features'])}")
    if use_cache:
        _cache_set(key, result)
    return result


def clear_cache() -> int:
    """清空识别缓存（测试用），返回清掉的条数"""
    with _cache_lock:
        n = len(_cache)
        _cache.clear()
    return n
