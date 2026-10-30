# -*- coding: utf-8 -*-
"""临时脚本：全量重建 Milvus 向量索引（知识库 25 chunks 重新入库，测试完保留备用）"""
import json
import sys
import io
import os
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, r"D:\PythonProject\鸭先知-大黄鸭游乐园智能问答与运营系统")
os.chdir(r"D:\PythonProject\鸭先知-大黄鸭游乐园智能问答与运营系统")

from core.vector_store import vector_store
from core.embedding import embedding_engine

# 1. 读取知识库 chunks
with open("chunks/duck_1786354148_chunks.json", encoding="utf-8") as f:
    data = json.load(f)
chunks = data if isinstance(data, list) else data.get("chunks", [])
texts = [c["text"] for c in chunks]
sources = [c.get("metadata", {}).get("source", "duck_1786354148.docx") for c in chunks]
print(f"知识库 chunk 数: {len(texts)}")

# 2. 清空旧集合（旧索引只有10篇旧chunk，与新知识库不一致）
_ = vector_store.collection  # 先触发连接
vector_store.drop_collection()
print("旧 Milvus 集合已清除")

# 3. 批量向量化 + 入库
t0 = time.time()
vectors = embedding_engine.embed_documents(texts)
print(f"向量化完成 ({round(time.time()-t0,1)}s)，维度: {len(vectors[0])}")

vector_store.insert(texts, vectors, sources)
stats = vector_store.get_collection_stats()
print(f"入库完成！集合统计: {stats}")
