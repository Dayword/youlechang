# ============================================================
# BM25 稀疏检索索引（内存级，支持增量分词缓存 + pickle 本地持久化）
# ============================================================
import os
import pickle
from rank_bm25 import BM25Okapi
from config import TOP_K
import jieba
import threading
import re

_lock = threading.Lock()

# ----------------------------------------
# 本地持久化路径：<项目根>/index/bm25_index.pkl
# 服务重启时优先加载持久化索引，免分词重建、且不依赖 Milvus 存活
# （*.pkl 已在 .gitignore 中忽略，不会进仓库）
# ----------------------------------------
_PERSIST_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index"
)
_PERSIST_PATH = os.path.join(_PERSIST_DIR, "bm25_index.pkl")

# ----------------------------------------
# jieba 自定义分词词典（业务关键词）
# 词典已抽取到 core/keywords.py 共享（与 core/cache.py 的关键词提取共用），
# 确保核心业务术语不会被错误切分，且与模块加载顺序无关
# ----------------------------------------
from core.keywords import ensure_business_words

ensure_business_words()


class BM25Index:
    """BM25 关键词检索引擎"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._corpus = []
                    cls._instance._sources = []
                    cls._instance._tokenized = []  # 与 corpus 对齐的分词缓存，避免全量重分词
                    cls._instance._index = None
        return cls._instance

    @staticmethod
    def _tokenize(text: str) -> list:
        """中文分词（jieba cut_for_search + 自定义词典 + 2-gram字符回退）

        cut_for_search 会将复合词拆分为子词以提升召回：
          例："怎么办卡充值" → ["怎么", "怎么办", "办卡", "卡", "充值"]
        而普通 cut 只会得到：["怎么办", "卡", "充值"]
        """
        # 1. jieba cut_for_search 分词（搜索引擎模式，召回率更高）
        tokens = list(jieba.cut_for_search(text))
        tokens = [t.strip() for t in tokens if t.strip() and len(t.strip()) >= 1]

        # 2. 字符级 bigram 补充（防止 jieba 遗漏关键短词）
        if not tokens:
            tokens = list(text)
        else:
            # 对纯中文片段提取 2-gram，作为额外的关键词补充
            text_clean = re.sub(r'[^\u4e00-\u9fa5]', '', text)
            bigrams = [text_clean[i:i+2] for i in range(len(text_clean)-1)]
            # 只添加不在已有 tokens 中的 bigram
            existing = set(tokens)
            for bg in bigrams:
                if bg not in existing:
                    tokens.append(bg)

        return tokens

    def _build_index_locked(self):
        """重建 BM25 索引（需持 _lock 调用）；复用已缓存的分词结果，仅新文档需分词"""
        if self._tokenized:
            self._index = BM25Okapi(self._tokenized)
        else:
            self._index = None

    def _persist_locked(self):
        """持久化索引到本地（需持 _lock 调用）；先写临时文件再原子替换，失败不影响检索服务"""
        try:
            os.makedirs(_PERSIST_DIR, exist_ok=True)
            tmp_path = _PERSIST_PATH + ".tmp"
            with open(tmp_path, "wb") as f:
                pickle.dump(
                    {"corpus": self._corpus, "sources": self._sources,
                     "tokenized": self._tokenized},
                    f, protocol=pickle.HIGHEST_PROTOCOL
                )
            os.replace(tmp_path, _PERSIST_PATH)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"BM25 索引持久化失败: {e}")

    def add_documents(self, texts: list, sources: list):
        """批量添加文档到 BM25 索引（仅新增部分分词，避免全量重分词）"""
        with _lock:
            self._corpus.extend(texts)
            self._sources.extend(sources)
            self._tokenized.extend(self._tokenize(t) for t in texts)
            # BM25Okapi 不支持增量更新，需全量重建；分词结果已缓存，重建开销大幅降低
            self._build_index_locked()
            self._persist_locked()

    def search(self, query: str, top_k: int = None) -> list:
        """
        BM25 关键词检索
        返回: [(text, source, score), ...]
        """
        if top_k is None:
            top_k = TOP_K
        with _lock:
            if self._index is None or not self._corpus:
                return []

            query_tokens = self._tokenize(query)
            scores = self._index.get_scores(query_tokens)

            results = []
            top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
            for idx in top_indices:
                if scores[idx] > 0:
                    results.append((self._corpus[idx], self._sources[idx], float(scores[idx])))
            return results

    def remove_by_source(self, source: str):
        """按来源删除文档（同步裁剪分词缓存并持久化）"""
        with _lock:
            keep_indices = [i for i, s in enumerate(self._sources) if s != source]
            self._corpus = [self._corpus[i] for i in keep_indices]
            self._sources = [self._sources[i] for i in keep_indices]
            self._tokenized = [self._tokenized[i] for i in keep_indices if i < len(self._tokenized)]
            self._build_index_locked()
            self._persist_locked()

    def clear(self):
        """清空索引"""
        with _lock:
            self._corpus = []
            self._sources = []
            self._tokenized = []
            self._index = None
            self._persist_locked()

    @property
    def doc_count(self) -> int:
        return len(self._corpus)

    def load_persisted(self) -> int:
        """
        从本地 pickle 加载持久化索引。
        一致性由持久化时机保证（每次增删/重建后同步写盘）；
        加载失败返回 0，调用方可回退从 Milvus 重建。
        """
        with _lock:
            if self._index is not None and self._corpus:
                return len(self._corpus)  # 已有索引，无需加载
            if not os.path.exists(_PERSIST_PATH):
                return 0
            try:
                with open(_PERSIST_PATH, "rb") as f:
                    data = pickle.load(f)
                corpus = data.get("corpus") or []
                sources = data.get("sources") or []
                tokenized = data.get("tokenized") or []
                if not corpus or len(corpus) != len(sources) or len(corpus) != len(tokenized):
                    return 0
                self._corpus = corpus
                self._sources = sources
                self._tokenized = tokenized
                self._build_index_locked()
                import logging
                logging.getLogger(__name__).info(
                    f"✅ BM25 索引已从本地持久化加载: {len(corpus)} 篇")
                return len(corpus)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"BM25 持久化索引加载失败: {e}")
                return 0

    def rebuild_from_milvus(self) -> int:
        """
        服务重启后恢复 BM25 索引：
        1. 优先加载本地 pickle 持久化（无分词/网络开销，Milvus 不可用也能恢复）
        2. 本地无持久化时从 Milvus 全量重建，并写回持久化
        返回索引文档数
        """
        # 1. 优先加载本地持久化
        loaded = self.load_persisted()
        if loaded > 0:
            return loaded

        # 2. 从 Milvus 全量重建
        from core.vector_store import vector_store
        try:
            col = vector_store.collection
            col.flush()
            # 分页读取 Milvus 中的所有文本
            expr = "id >= 0"
            results = col.query(expr=expr, output_fields=["text", "source"], limit=10000)
            if not results:
                return 0
            texts = [r["text"] for r in results]
            sources = [r["source"] for r in results]
            with _lock:
                self._corpus = texts
                self._sources = sources
                self._tokenized = [self._tokenize(t) for t in self._corpus]
                self._build_index_locked()
                self._persist_locked()
            return len(texts)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"BM25 索引重建失败: {e}")
            return 0


# 全局单例
bm25_index = BM25Index()
