# ============================================================
# Agent 参数配置 API 路由 - /config/*（需管理员登录）
#
# ⚠️ 参数分两类，不要混：
#   · 热更新（HOT_KEYS）：进程内可立即生效，有真实消费方。
#   · 只读（其余）：config.py 在 import 时固化成模块常量，且被
#     `from config import TOP_K` 这种方式绑进各模块命名空间，
#     运行期改 config.X 或写进 _runtime_config 都**无任何消费方**。
#     以前这里把它们一并收下并回"已热更新"，运营以为改了检索行为，
#     实际线上一点没变（误导）。现在显式区分：只应用 HOT_KEYS，
#     其余原样回报为 ignored，并说明必须改 config.py 后重启。
# ============================================================
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from core.auth import get_current_admin

router = APIRouter(prefix="/config", tags=["Agent参数配置"], dependencies=[Depends(get_current_admin)])

# 运行时热更新配置（仅存真正有消费方的键）
_runtime_config = {}

# 唯一真正生效的键：RetrievalAgent._alpha，_score_fusion 每次检索都读它。
HOT_KEYS = ("hybrid_alpha",)

# 只读键 → 生效方式说明（用于前端/接口如实回话）
READONLY_REASON = {
    "top_k": "需改 config.py 的 TOP_K 后重启服务",
    "rerank_top": "需改 config.py 的 RERANK_TOP 后重启服务",
    "similarity_threshold": "需改 config.py 的 SIMILARITY_THRESHOLD 后重启服务",
    "chunk_size": "只影响文档入库分块，需改 config.py 后重新入库",
    "chunk_overlap": "只影响文档入库分块，需改 config.py 后重新入库",
    "llm_temperature": "需改 config.py 的 LLM_TEMPERATURE 后重启服务",
    "cache_ttl": "需改 config.py 的 CACHE_TTL 后重启服务",
}


class AgentConfigUpdate(BaseModel):
    top_k: Optional[int] = None
    rerank_top: Optional[int] = None
    similarity_threshold: Optional[float] = None
    hybrid_alpha: Optional[float] = None
    chunk_size: Optional[int] = None
    chunk_overlap: Optional[int] = None
    llm_temperature: Optional[float] = None
    cache_ttl: Optional[int] = None


@router.get("/agent", summary="获取当前 Agent 配置", dependencies=[Depends(get_current_admin)])
async def get_agent_config():
    """获取当前 Agent 运行参数。hybrid_alpha 返回运行时值，其余返回 config.py 实际生效值。"""
    from config import TOP_K, RERANK_TOP, SIMILARITY_THRESHOLD, HYBRID_ALPHA
    from config import CHUNK_SIZE, CHUNK_OVERLAP, LLM_TEMPERATURE, CACHE_TTL
    from config import LLM_TEXT_MODEL, EMBEDDING_MODEL_NAME, RERANK_MODEL_NAME

    return {
        "code": 200,
        "data": {
            # 前端据此把除 hybrid_alpha 外的输入框置为只读
            "hot_updatable": list(HOT_KEYS),
            "readonly_reason": READONLY_REASON,
            "retrieval": {
                "top_k": TOP_K,
                "rerank_top": RERANK_TOP,
                "similarity_threshold": SIMILARITY_THRESHOLD,
                "hybrid_alpha": _runtime_config.get("hybrid_alpha", HYBRID_ALPHA),
            },
            "chunking": {
                "chunk_size": CHUNK_SIZE,
                "chunk_overlap": CHUNK_OVERLAP,
            },
            "llm": {
                "model": LLM_TEXT_MODEL,
                "temperature": LLM_TEMPERATURE,
            },
            "models": {
                "embedding": EMBEDDING_MODEL_NAME,
                "reranker": RERANK_MODEL_NAME,
            },
            "cache": {
                "ttl": CACHE_TTL,
            },
        }
    }


@router.post("/agent", summary="更新 Agent 配置（仅 hybrid_alpha 热更新）", dependencies=[Depends(get_current_admin)])
async def update_agent_config(update: AgentConfigUpdate):
    """热更新 Agent 运行时参数（重启后恢复默认）。

    只应用 HOT_KEYS；其余参数如实回报为 ignored，不会静默写入假值。
    """
    applied = {}
    ignored = []

    if update.hybrid_alpha is not None:
        clamped = max(0.0, min(1.0, update.hybrid_alpha))
        _runtime_config["hybrid_alpha"] = clamped
        # 热更新 RetrievalAgent 的 alpha（唯一真实消费方）
        from agents.retrieval_agent import retrieval_agent
        retrieval_agent._alpha = clamped
        applied["hybrid_alpha"] = clamped

    for key, reason in READONLY_REASON.items():
        if getattr(update, key) is not None:
            ignored.append({"key": key, "reason": reason})

    if applied and ignored:
        msg = f"已热更新 {len(applied)} 项；{len(ignored)} 项当前不支持热更新，需改 config.py 后重启"
    elif applied:
        msg = "已热更新（重启后恢复 config.py 默认值）"
    elif ignored:
        msg = "本次提交的配置项均不支持热更新，未做任何改动，详见 ignored"
    else:
        msg = "未提交任何配置项"

    return {"code": 200, "msg": msg, "changes": applied, "ignored": ignored}


@router.get("/models", summary="获取模型列表", dependencies=[Depends(get_current_admin)])
async def list_models():
    """列出使用的模型信息"""
    from config import LLM_TEXT_MODEL, EMBEDDING_MODEL_NAME, RERANK_MODEL_NAME
    return {
        "code": 200,
        "data": {
            "llm": LLM_TEXT_MODEL,
            "embedding": EMBEDDING_MODEL_NAME,
            "reranker": RERANK_MODEL_NAME,
        }
    }
