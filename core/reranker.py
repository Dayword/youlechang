# ============================================================
# Reranker 重排序引擎 - BGE-Reranker-v2-m3
# ============================================================
# ⚠️ config 必须**先于** sentence_transformers 导入：huggingface_hub 是在
#    **模块导入时**读取 HF_HUB_OFFLINE / HF_ENDPOINT 的（huggingface_hub.constants 里
#    一次性求值），config 负责设置这两个变量。顺序反了就会去联网做 HEAD 校验 etag，
#    网络不通时每个模型文件重试 5 次 → 冷启动从秒级变成分钟级。
from config import RERANK_MODEL_NAME, RERANK_TOP, RERANK_DOC_MAX_CHARS, RERANK_ENABLED
from sentence_transformers import CrossEncoder
import threading

_lock = threading.Lock()


class RerankerEngine:
    """BGE-Reranker 交叉编码器重排序"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._model = None
        return cls._instance

    @property
    def model(self):
        if self._model is None:
            self._model = CrossEncoder(RERANK_MODEL_NAME, trust_remote_code=True)
        return self._model

    def rerank(self, query: str, docs: list, top_n: int = None,
               max_chars: int = None) -> list:
        """
        对检索结果重排序
        Args:
            query: 查询文本
            docs: [(text, source, score), ...]
            top_n: 返回 TopN，默认 RERANK_TOP
            max_chars: 参与打分的单篇字符上限（默认 RERANK_DOC_MAX_CHARS）。
                       CrossEncoder 耗时随输入长度近似线性增长，CPU 上 6 篇 1000 字
                       需 10s+、4 篇 200 字仅约 2.5s；相关性主要由片段开头决定，
                       因此只截断"打分用文本"，返回的仍是完整原文（供 LLM 生成）。
        Returns:
            [(text, source, rerank_score), ...]
        """
        if top_n is None:
            top_n = RERANK_TOP
        if max_chars is None:
            max_chars = RERANK_DOC_MAX_CHARS
        if not docs:
            return []

        # 关闭精排：直接按融合分排序返回（极限提速模式，精度略降）
        if not RERANK_ENABLED:
            return [(d[0], d[1], float(d[2])) for d in docs[:top_n]]

        pairs = [[query, (doc[0][:max_chars] if max_chars and max_chars > 0 else doc[0])]
                 for doc in docs]
        scores = [float(s) for s in self.model.predict(pairs)]

        # 分数归一化：CrossEncoder 可能输出原始 logit（范围超出 0~1），
        # 统一用 sigmoid 映射为相关概率，保证下游阈值（0.45/0.3）语义一致；已是概率则保持不变
        if scores and (max(scores) > 1 or min(scores) < 0):
            scores = [1.0 / (1.0 + pow(2.718281828459045, -s)) for s in scores]

        sorted_docs = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
        return [(item[0][0], item[0][1], float(item[1])) for item in sorted_docs[:top_n]]

    @property
    def model_name(self) -> str:
        return RERANK_MODEL_NAME


# 全局单例
reranker_engine = RerankerEngine()
