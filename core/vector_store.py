# ============================================================
# Milvus 向量库管理（懒加载 + 连接池）
# ============================================================
from pymilvus import connections, Collection, FieldSchema, CollectionSchema, DataType, utility
from config import MILVUS_HOST, MILVUS_PORT, COLLECTION_NAME, EMBEDDING_DIM, TOP_K
import threading

_lock = threading.Lock()


class VectorStore:
    """Milvus 向量存储单例"""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._connected = False
                    cls._instance._collection = None
        return cls._instance

    def _ensure_connected(self):
        """确保 Milvus 已连接并初始化集合"""
        if self._connected:
            return self._collection

        connections.connect(host=MILVUS_HOST, port=MILVUS_PORT)

        if utility.has_collection(COLLECTION_NAME):
            self._collection = Collection(COLLECTION_NAME)
            self._collection.load()
        else:
            fields = [
                FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=2000),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
                FieldSchema(name="source", dtype=DataType.VARCHAR, max_length=200)
            ]
            schema = CollectionSchema(fields=fields, description="Amusement Park Knowledge Base")
            self._collection = Collection(COLLECTION_NAME, schema)
            index_params = {
                "index_type": "IVF_FLAT",
                "metric_type": "IP",
                "params": {"nlist": 128}
            }
            self._collection.create_index(field_name="vector", index_params=index_params)
            self._collection.load()

        self._connected = True
        return self._collection

    @property
    def collection(self):
        return self._ensure_connected()

    def insert(self, texts: list, vectors: list, sources: list):
        """批量插入向量数据"""
        col = self._ensure_connected()
        col.insert([texts, vectors, sources])
        col.flush()

    def search(self, query_vec: list, top_k: int = None) -> list:
        """
        向量检索
        返回: [(text, source, score), ...]
        """
        if top_k is None:
            top_k = TOP_K
        col = self._ensure_connected()
        search_param = {"metric_type": "IP", "params": {"nprobe": 10}}
        res = col.search(
            data=[query_vec],
            anns_field="vector",
            param=search_param,
            limit=top_k,
            output_fields=["text", "source"],
            timeout=5,
        )
        return [
            (hit.entity.get("text"), hit.entity.get("source"), hit.score)
            for hit in res[0]
        ]

    def get_collection_stats(self) -> dict:
        """获取集合统计信息"""
        try:
            col = self._ensure_connected()
            col.flush()
            return {
                "collection_name": COLLECTION_NAME,
                "num_entities": col.num_entities,
            }
        except Exception:
            return {"collection_name": COLLECTION_NAME, "num_entities": 0}

    def delete_by_source(self, source: str) -> int:
        """按来源删除文档（用于重新入库）"""
        col = self._ensure_connected()
        # 防表达式注入：转义反斜杠与双引号后再拼接（Milvus 布尔表达式字符串以双引号包裹）
        safe_source = str(source).replace("\\", "\\\\").replace('"', '\\"')
        expr = f'source == "{safe_source}"'
        col.delete(expr)
        col.flush()
        return 0  # Milvus 删除不返回计数

    def drop_collection(self):
        """清空集合（谨慎使用）"""
        if utility.has_collection(COLLECTION_NAME):
            utility.drop_collection(COLLECTION_NAME)
            self._connected = False
            self._collection = None


# 全局单例
vector_store = VectorStore()
