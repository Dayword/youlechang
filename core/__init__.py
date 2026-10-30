# core/__init__.py - 懒加载避免循环导入和过早初始化
# 不在模块级导入重型模块，使用时才导入

def get_embedding_engine():
    from .embedding import embedding_engine
    return embedding_engine

def get_vector_store():
    from .vector_store import vector_store
    return vector_store

def get_bm25_index():
    from .bm25_index import bm25_index
    return bm25_index

def get_reranker():
    from .reranker import reranker_engine
    return reranker_engine

def get_llm_client():
    from .llm_client import llm_client
    return llm_client

def get_redis_cache():
    from .cache import redis_cache
    return redis_cache
