# ============================================================
# 检索 Agent (RetrievalAgent)
# 职责：BM25 + BGE-M3 混合检索 + BGE-Reranker 重排序
# 融合策略：分数级 min-max 归一化 + 加权融合（alpha=0.6 语义，0.4 关键词）
# ============================================================
import logging
import time
from typing import List, Tuple
from config import (TOP_K, RERANK_TOP, RERANK_PRE_FILTER, SIMILARITY_THRESHOLD,
                    HYBRID_ALPHA, VECTOR_CANDIDATE_K, BM25_CANDIDATE_K, FAQ_BOOST)

logger = logging.getLogger(__name__)


class RetrievalAgent:
    """
    BM25 + BGE-M3 混合检索 Agent
    流程：
    1. BGE-M3 向量检索（稠密语义匹配，Milvus + IP 内积）
    2. BM25 关键词检索（稀疏词频匹配，jieba 分词 + BM25Okapi）
    3. 分数归一化 + 加权融合（alpha 控制语义 vs 关键词权重）
    4. BGE-Reranker 语义重排序（BAAI/bge-reranker-v2-m3）
    5. 相似度阈值过滤
    """

    def __init__(self):
        self._alpha = HYBRID_ALPHA  # 支持运行时热更新

    # ============== 主检索入口 ==============

    def search(self, query: str, top_k: int = None, use_cache: bool = True,
               alpha: float = None, report: dict = None,
               rerank: bool = True) -> List[Tuple[str, str, float]]:
        """
        BM25 + BGE-M3 混合检索入口

        Args:
            query: 用户检索问句
            top_k: 最终返回数量，None=默认 TOP_K
            use_cache: 是否使用 Redis 缓存
            alpha: 向量检索权重（0~1），0=纯BM25，1=纯向量，None=默认 0.6
            rerank: 是否在本方法内做 CrossEncoder 精排。
                    False 时返回融合候选（分数为 min-max 融合分），
                    供调用方（Orchestrator 多路检索）合并去重后**统一跑一次精排**——
                    CPU 上 CrossEncoder 是最大瓶颈，N 路各跑一次会线性叠加/互相争抢算力。
            report: 可选 dict，调用方传入后本方法会写入本次检索的真实统计
                    （各路召回数/融合数/精排数/通过数/缓存命中/耗时），用于前端
                    "思考步骤"展示；不传则完全不影响原有行为。

        Returns:
            [(text, source, score), ...]  按 Rerank 分数降序（rerank=False 时按融合分降序）
        """
        if top_k is None:
            top_k = TOP_K
        if alpha is None:
            alpha = self._alpha

        _start = time.monotonic()
        if report is not None:
            report.update({"query": query, "cache_hit": False, "reranked": bool(rerank)})

        # 缓存检查（仅精排模式缓存最终结果；非精排模式的中间候选不写缓存）
        if use_cache:
            from core.cache import redis_cache
            cached = redis_cache.get_cached_search(query)
            if cached:
                if report is not None:
                    report.update({
                        "cache_hit": True,
                        "docs_returned": len(cached),
                        "elapsed_ms": round((time.monotonic() - _start) * 1000, 1),
                    })
                return cached

        # 1. BGE-M3 向量检索 (Milvus) — 扩大候选集以提升召回
        vector_results = self._vector_search(query, VECTOR_CANDIDATE_K)
        logger.debug(f"Vector search: {len(vector_results)} candidates")

        # 2. BM25 关键词检索 — 扩大候选集
        bm25_results = self._bm25_search(query, BM25_CANDIDATE_K)
        logger.debug(f"BM25 search: {len(bm25_results)} candidates")

        # 3. 分数归一化 + 加权融合
        fused = self._score_fusion(vector_results, bm25_results, alpha)
        logger.info(f"🔀 融合候选集: {len(fused)} docs → 截断至 {RERANK_PRE_FILTER} 送入 Reranker")

        # 3.5 问法级提分（FAQ boost）
        # 块自带的「**常见问法**：a、b、c…」是与用户问句**最接近的短文本**，用它算出的
        # 相似度比"短问句 vs 长段落"的 CrossEncoder 分稳定得多：实测同一个正确块
        # 换个问法精排分就从 0.18 跳到 0.87，「门票多少钱」对上含该问法的块只有 0.08
        # （直接掉出候选 → 明明能答却答不出）。这一层把正确块稳定抬回来。
        faq_map: dict = {}
        if FAQ_BOOST:
            try:
                fused, faq_map = self._faq_boost(query, fused)
            except Exception as e:                  # pragma: no cover
                logger.warning(f"问法级提分失败（忽略，走原逻辑）: {e}")
                faq_map = {}

        # ⚡ 截断优化：融合后只取 Top N 送入 CrossEncoder
        pre_filtered = fused[:RERANK_PRE_FILTER]

        if not rerank:
            # 非精排模式：直接把融合候选交给调用方（多路合并后再统一精排）
            if report is not None:
                report.update({
                    "vector_candidates": len(vector_results),
                    "bm25_candidates": len(bm25_results),
                    "fused_candidates": len(fused),
                    "rerank_input": 0,
                    "rerank_output": 0,
                    "threshold_passed": True,
                    "docs_returned": len(pre_filtered),
                    "elapsed_ms": round((time.monotonic() - _start) * 1000, 1),
                })
            return pre_filtered

        # 4. BGE-Reranker 语义重排序
        reranked = self._rerank(query, pre_filtered, RERANK_TOP)

        # 4.5 最终分取"精排分"与"问法命中分"的较大者：
        #     问法命中说明该块确实声明过能回答这个问法，不该被长段落的低精排分压掉。
        if faq_map:
            reranked = [(t, s, max(sc, faq_map.get((s, t), 0.0)))
                        for t, s, sc in reranked]
            reranked.sort(key=lambda x: x[2], reverse=True)

        # 5. 相似度阈值过滤 + 兜底策略
        filtered = [(t, s, sc) for t, s, sc in reranked if sc >= SIMILARITY_THRESHOLD]
        has_real_hits = bool(filtered)
        if not filtered:
            # 兜底：无高分文档时至少返回 top_k 条避免空响应
            filtered = reranked[:min(top_k, len(reranked))]

        # 6. 问题级去重（兜底闸门）：同一业务问题只保留最高分的一条。
        #    库里若残留了两版答案，这里必须挡掉后一版，否则两条会一起进 LLM 上下文
        #    → 同一问题时而答 A 版、时而答 B 版。
        _before_dedup = len(filtered)
        filtered = self._dedupe_by_question(filtered)
        _dedup_dropped = _before_dedup - len(filtered)

        if report is not None:
            report.update({
                "vector_candidates": len(vector_results),
                "bm25_candidates": len(bm25_results),
                "fused_candidates": len(fused),
                "rerank_input": len(pre_filtered),
                "rerank_output": len(reranked),
                "threshold_passed": has_real_hits,
                "dedup_dropped": _dedup_dropped,
                "faq_boosted": sum(1 for v in faq_map.values() if v > 0),
                "faq_top": max(faq_map.values()) if faq_map else 0.0,
                "docs_returned": len(filtered),
                "elapsed_ms": round((time.monotonic() - _start) * 1000, 1),
            })

        # 缓存 — 仅真实命中时写入（兜底结果不缓存）
        if use_cache and has_real_hits:
            from core.cache import redis_cache
            redis_cache.cache_search(query, filtered)

        return filtered

    # ============== 问题级去重闸门 ==============

    def candidates(self, query: str, k: int = 12) -> List[Tuple[str, str, float]]:
        """融合候选（向量 + BM25 归一化加权），**不做** CrossEncoder 精排。

        供知识库"问题级去重"等离线判断使用：search(rerank=False) 会按
        RERANK_PRE_FILTER 固定截断，候选取不齐，所以这里把融合结果按 k 返回。
        """
        vector_results = self._vector_search(query, k)
        bm25_results = self._bm25_search(query, k)
        fused = self._score_fusion(vector_results, bm25_results, self._alpha)
        return fused[:k]

    # ============== 问法级提分 ==============

    @staticmethod
    def _faq_boost(query: str, cands: list) -> tuple:
        """用块自带的「常见问法」做一层问法级匹配，把正确块抬回来。

        返回 (提分并重排后的候选, {(来源, 原文): 问法命中分})。
        判定与参数见 services/kb_dedup.faq_scores 与 config.py 的 KB_FAQ_* 。
        """
        from services.kb_dedup import faq_scores

        if not cands:
            return cands, {}
        pairs = [(t, s) for t, s, _sc in cands]
        res = faq_scores(query, pairs)
        score_map: dict = {}
        out = []
        for (t, s, sc), (fs, _q) in zip(cands, res):
            score_map[(s, t)] = float(fs)
            out.append((t, s, max(float(sc), float(fs))))
        out.sort(key=lambda x: x[2], reverse=True)
        return out, score_map

    @staticmethod
    def _dedupe_by_question(docs: list, keep: int = None) -> list:
        """同一业务问题只保留最高分的一条（输入须已按分数降序）

        去重键 = 块标题归一化（剥掉 Qn./1.1/一、 等编号前缀），见 services/kb_dedup.py。
        只对有标题的块生效：标题相同即视为同一个问题 —— 这是"库里残留两版答案"时
        不让它们同时进 LLM 上下文的最后一道保险。
        """
        try:
            from services.kb_dedup import dedup_key
        except Exception:                           # pragma: no cover
            return docs[:keep] if keep else docs

        seen = set()
        out = []
        for t, s, sc in docs:
            key = ""
            try:
                key = dedup_key(t)
            except Exception:                       # pragma: no cover
                key = ""
            if key:
                if key in seen:
                    logger.info(f"🔁 检索去重：同问题块已保留更高分版本，丢弃 {s} 的重复块")
                    continue
                seen.add(key)
            out.append((t, s, sc))
            if keep and len(out) >= keep:
                break
        return out

    def _vector_search(self, query: str, top_k: int) -> List[Tuple[str, str, float]]:
        """BGE-M3 向量语义检索（向量库缺失/未启动时自动降级，由 BM25 单独支撑，不中断主流程）"""
        try:
            from core.embedding import embedding_engine
            from core.vector_store import vector_store

            query_vec = embedding_engine.embed_query(query)
            return vector_store.search(query_vec, top_k)
        except Exception as e:
            # vector_store 当前为 Milvus 版且 config 无 MILVUS_* 时 import 会抛错；
            # 此处统一捕获，避免 ImportError 冒泡到上层导致整路检索/业务工具失败
            logger.warning(f"向量检索不可用，降级为 BM25 关键词检索: {e}")
            return []

    def _bm25_search(self, query: str, top_k: int) -> List[Tuple[str, str, float]]:
        """BM25 关键词匹配检索（jieba 分词）"""
        from core.bm25_index import bm25_index

        try:
            return bm25_index.search(query, top_k)
        except Exception as e:
            logger.error(f"BM25 search failed: {e}")
            return []

    # ============== 分数融合 ==============

    def _score_fusion(self, vec_results: list, bm25_results: list,
                       alpha: float = 0.6) -> List[Tuple[str, str, float]]:
        """
        BM25 + BGE-M3 分数级加权融合

        策略：
        1. 分别对 BGE-M3 向量分数和 BM25 分数做 min-max 归一化到 [0,1]
        2. 加权融合：final = alpha * vec_norm + (1-alpha) * bm25_norm
        3. 同一文本在两个来源都出现时，取加权后的最大值（去重）

        Args:
            alpha: 向量检索权重，0=纯BM25关键词，1=纯BGE-M3语义
        """
        if not vec_results and not bm25_results:
            return []
        if not vec_results:
            return bm25_results
        if not bm25_results:
            return vec_results

        # --- 提取原始分数 ---
        vec_scores_raw = [s for _, _, s in vec_results]
        bm25_scores_raw = [s for _, _, s in bm25_results]

        # --- Min-Max 归一化到 [0, 1] ---
        def _minmax(scores: list) -> list:
            mn, mx = min(scores), max(scores)
            if mx == mn:
                return [1.0] * len(scores)
            return [(s - mn) / (mx - mn) for s in scores]

        vec_norm = _minmax(vec_scores_raw)
        bm25_norm = _minmax(bm25_scores_raw)

        # --- 加权融合 + 去重 ---
        fused_map = {}  # text -> (source, max_weighted_score)

        for (text, source, _), norm_v in zip(vec_results, vec_norm):
            weighted = alpha * norm_v
            if text not in fused_map or weighted > fused_map[text][1]:
                fused_map[text] = (source, weighted)

        for (text, source, _), norm_b in zip(bm25_results, bm25_norm):
            weighted = (1 - alpha) * norm_b
            if text not in fused_map or weighted > fused_map[text][1]:
                fused_map[text] = (source, weighted)

        # --- 按加权分数降序排列 ---
        sorted_items = sorted(fused_map.items(), key=lambda x: x[1][1], reverse=True)
        return [(text, src, score) for text, (src, score) in sorted_items]

    # ============== 重排序 ==============

    def _rerank(self, query: str, docs: list, top_n: int) -> List[Tuple[str, str, float]]:
        """BGE-Reranker 语义重排序（CrossEncoder）
        注意：无论候选多少都送 Reranker——返回的才是真正的语义相关分（0~1），
        融合归一化分数不能代替（小知识库候选少时曾因短路返回融合分数，导致后续阈值过滤全部失效）"""
        from core.reranker import reranker_engine

        try:
            return reranker_engine.rerank(query, docs, top_n)
        except Exception as e:
            logger.error(f"BGE-Reranker failed: {e}, returning original order")
            return docs[:top_n]

    # ============== 统计 ==============

    def get_index_stats(self) -> dict:
        """获取混合索引统计"""
        from core.vector_store import vector_store
        from core.bm25_index import bm25_index

        vs = vector_store.get_collection_stats()
        return {
            "milvus_entities": vs.get("num_entities", 0),
            "bm25_documents": bm25_index.doc_count,
            "collection_name": vs.get("collection_name", ""),
        }


# 全局单例
retrieval_agent = RetrievalAgent()
