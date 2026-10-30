# ============================================================
# Embedding 引擎 - BGE-M3 向量化（单例模式）
# ============================================================
# ⚠️ config 必须**先于** langchain_huggingface / sentence_transformers 导入：
#    huggingface_hub 在**模块导入时**读取 HF_HUB_OFFLINE / HF_ENDPOINT，config 负责设置它们。
#    顺序反了就会联网做 HEAD 校验 etag，网络不通时冷启动会被重试拖到分钟级。
from config import EMBEDDING_MODEL_NAME, EMBEDDING_DIM, EMBEDDING_DEVICE
from langchain_huggingface import HuggingFaceEmbeddings
import threading

_lock = threading.Lock()


class EmbeddingEngine:
    """BGE-M3 Embedding 单例"""
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
            self._model = HuggingFaceEmbeddings(
                model_name=EMBEDDING_MODEL_NAME,
                model_kwargs={"device": EMBEDDING_DEVICE},
                encode_kwargs={"normalize_embeddings": True}
            )
        return self._model

    def embed_query(self, query: str) -> list:
        """单条查询向量化"""
        return self.model.embed_query(query)

    def embed_documents(self, texts: list) -> list:
        """批量文档向量化"""
        return self.model.embed_documents(texts)

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM


# 全局单例
embedding_engine = EmbeddingEngine()
