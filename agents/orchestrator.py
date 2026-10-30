# ============================================================
# 调度 Agent (OrchestratorAgent)
# 中央调度器：任务分解、Agent编排、结果聚合
# ============================================================
import time
import logging
from typing import List, Dict, Any, Generator
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (RERANK_MERGED_ONCE, RERANK_PRE_FILTER, MAX_RETRIEVAL_QUERIES,
                    FALLBACK_ADVICE_ENABLED)
from core.llm_client import set_llm_user as _set_llm_user

logger = logging.getLogger(__name__)


def _profile_ctx_for(session_id: str, user_id, query: str = "") -> Dict:
    """取用户画像（摘要 + 结构化视图 + 原始 json）。等价于 `_profile_note_for` + 结构化。

    多返回的两个字段都是**纯内存派生**（`effective_view` 不查库），所以没有额外 IO：
    画像问答模板（"我有几个孩子"）需要结构化字段，光有摘要字符串渲染不出来。
    """
    blank = {"note": "", "profile": {}, "view": {}}
    if not user_id:
        return blank
    try:
        from core import chat_memory as _cm
        from services import user_profile as _up
        # session_id 传 "default"：load_context 会跳过历史查询，只读 user 级画像
        hit = _cm.load_context(session_id or "default", user_id, query=query)
        raw = hit.get("profile") or {}
        return {"note": hit.get("profile_note") or "", "profile": raw,
                "view": _up.effective_view(raw) if raw else {}}
    except Exception as e:
        logger.warning(f"画像读取失败（按无画像继续）: {e}")
        return blank


def _profile_note_for(session_id: str, user_id, query: str = "") -> str:
    """取用户画像摘要（"每次回答都要基于画像"的唯一入口）。

    为什么要有这个函数：画像此前是**搭会话记忆的车**加载的（`if session_id and
    session_id != "default"`），于是凡是没带会话 id 的调用（web/legacy、部分脚本）
    都静默拿不到画像 —— 产品承诺是"每次"，那就不能有任何一条路径漏掉。

    代价：这条分支本来完全不查画像，现在多一次单行查询（有会话 id 的路径不变，
    仍是 load_context 里 1 换 1）。任何异常都降级成空，绝不因画像打断回答。
    query 只用于 C3 的"工作记忆"（要不要注入偏好类信息），纯内存判断。
    """
    return _profile_ctx_for(session_id, user_id, query).get("note") or ""


class SubTaskType(str, Enum):
    """子任务类型"""
    QUERY_REWRITE = "query_rewrite"       # Query 改写
    DOCUMENT_SEARCH = "document_search"   # 文档检索
    RETRIEVAL = "retrieval"               # 混合检索
    VERIFICATION = "verification"         # 校验与溯源
    GENERATION = "generation"             # 答案生成
    COMPLEX_SPLIT = "complex_split"       # 复杂问题拆分


@dataclass
class SubTask:
    """子任务定义"""
    task_id: str
    task_type: SubTaskType
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # pending/running/completed/failed
    error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0


@dataclass
class TaskPlan:
    """任务执行计划"""
    original_query: str
    is_complex: bool = False
    sub_queries: List[str] = field(default_factory=list)
    sub_tasks: List[SubTask] = field(default_factory=list)
    execution_order: List[str] = field(default_factory=list)


# 业务服务出口 → 前端可渲染的按钮（id + 标题）
_SERVICE_TITLES = {
    "recharge": "💳 去办卡 / 充值",
    "lost_menu": "🧸 查看失物 / 认领",
    "report_lost": "📝 登记报失",
    "lost_claim": "🔍 失物认领",
}


def _service_items(tags: list) -> list:
    """把业务 Agent 返回的服务 id 列表转成前端按钮对象 [{id,title}, ...]"""
    out = []
    for tag in tags or []:
        if tag in _SERVICE_TITLES:
            out.append({"id": tag, "title": _SERVICE_TITLES[tag]})
    return out


def _park_photos(query: str) -> list:
    """问句命中园区游乐项目时返回实景图卡（纯规则、零 LLM、零 IO），否则 []。

    实现收敛在 services/park_photos.py，流式与非流式两条路径共用同一份口径，
    避免"流式有图、非流式没图"这类漂移。任何异常都吞掉返回 []，绝不影响问答主链路。
    """
    try:
        from services.park_photos import project_photos
        return project_photos(query)
    except Exception as e:                  # pragma: no cover
        logger.warning(f"项目配图匹配失败（忽略继续）: {e}")
        return []


class OrchestratorAgent:
    """
    中央调度 Agent
    职责：
    1. 接收用户提问，判断是否需要复杂拆分
    2. 编排 Query/Retrieval/Verification Agent 的执行顺序
    3. 聚合各 Agent 结果，生成最终回答
    4. 异常处理与降级策略
    """

    def __init__(self):
        self._query_agent = None
        self._doc_agent = None
        self._retrieval_agent = None
        self._verification_agent = None

    @property
    def query_agent(self):
        if self._query_agent is None:
            # 必须复用模块级单例：早期这里 new 了独立实例，绕过了启动预热，
            # 导致服务重启后的第一个真实提问要现场扫描 chunks 并向量化全部常见问法
            # （实测 130 条问法 → 7.1s），白白拖慢首问。
            from agents.query_agent import query_agent as _shared_query_agent
            self._query_agent = _shared_query_agent
        return self._query_agent

    @property
    def doc_agent(self):
        if self._doc_agent is None:
            from agents.document_agent import document_agent as _shared
            self._doc_agent = _shared
        return self._doc_agent

    @property
    def retrieval_agent(self):
        if self._retrieval_agent is None:
            # 复用模块级单例：商家 PC「Agent 参数」页热更的 hybrid_alpha 等参数
            # 改的是单例上的属性，独立实例会读不到，热更形同失效。
            from agents.retrieval_agent import retrieval_agent as _shared
            self._retrieval_agent = _shared
        return self._retrieval_agent

    @property
    def verification_agent(self):
        if self._verification_agent is None:
            from agents.verification_agent import verification_agent as _shared
            self._verification_agent = _shared
        return self._verification_agent

    def _analyze_complexity(self, query: str) -> TaskPlan:
        """
        分析问题复杂度，判断是否需要拆分子任务
        通过 LLM 判断问题是否涉及多个维度
        """
        plan = TaskPlan(original_query=query)

        # 复杂问题特征：多个问号、对比类、条件类、多维度
        complexity_markers = ["?", "？", "对比", "区别", "分别", "哪些", "以及", "还有", "同时"]
        marker_count = sum(1 for m in complexity_markers if m in query)

        if "?" in query or "？" in query:
            questions = query.replace("？", "?").split("?")
            questions = [q.strip() for q in questions if q.strip()]
            if len(questions) > 1:
                plan.is_complex = True
                plan.sub_queries = questions
                return plan

        if marker_count >= 2 or len(query) > 100:
            plan.is_complex = True
            # 通过 LLM 拆分子问题
            plan.sub_queries = self._llm_split_query(query)

        return plan

    def _llm_split_query(self, query: str) -> List[str]:
        """使用 LLM 拆分复杂问题为子问题"""
        from core.llm_client import llm_client
        prompt = f"""将以下复杂问题拆分为多个独立的子问题，每个子问题单独一行。
要求：不要回答原问题，只输出拆分后的子问题，每行一个。

【复杂问题】
{query}

【子问题列表】
"""
        try:
            result = llm_client.invoke(prompt)
            sub_queries = [q.strip() for q in result.split("\n") if q.strip()]
            # 过滤掉编号前缀
            cleaned = []
            for q in sub_queries:
                # 移除可能的编号前缀
                for prefix in ["1.", "2.", "3.", "4.", "5.", "1、", "2、", "3、", "4、", "5、", "- "]:
                    if q.startswith(prefix):
                        q = q[len(prefix):].strip()
                cleaned.append(q)
            return cleaned if cleaned else [query]
        except Exception as e:
            logger.warning(f"Query splitting failed: {e}, fallback to single query")
            return [query]

    # ============== 公共检索管线（process / process_stream 共用） ==============

    @staticmethod
    def _merge_docs(*doc_lists) -> list:
        """
        合并多路检索结果：按文本去重（保持首次出现顺序）。
        同一文本在多路都命中时保留分数更高的版本——
        原始问句与改写问句的 Rerank 分可能有数倍差异（措辞敏感），
        若"先到先得"会丢弃改写路的高分版本，导致整批被阈值过滤掉。
        """
        best: Dict[str, Any] = {}
        order: List[str] = []
        for docs in doc_lists:
            for d in docs:
                text = d[0]
                if text not in best:
                    best[text] = d
                    order.append(text)
                elif len(d) > 2 and d[2] > best[text][2]:
                    best[text] = d  # 保留更高分版本
        return [best[t] for t in order]

    def _collect_retrieval_queries(self, user_query: str, search_query: str,
                                   plan: TaskPlan, has_history: bool) -> List[str]:
        """
        收集全部待检索问句：
        - 简单问题：原始问句 + 改写问句（双路）
        - 复杂问题：每个子问题的原始问句 + 改写问句（有历史时子问题改写并行执行）
        """
        if plan.is_complex and len(plan.sub_queries) > 1:
            sub_queries = plan.sub_queries
            if has_history:
                # 子问题改写并行化（每次改写是一次 LLM 调用，串行会线性叠加延迟）
                rewritten_map: Dict[str, str] = {}
                with ThreadPoolExecutor(max_workers=min(4, len(sub_queries))) as pool:
                    future_map = {
                        pool.submit(self.query_agent.rewrite, "", sub_q): sub_q
                        for sub_q in sub_queries
                    }
                    for fut, sub_q in future_map.items():
                        try:
                            rewritten_map[sub_q] = fut.result()
                        except Exception as e:
                            logger.warning(f"子问题改写失败，回退原始问句: {e}")
                            rewritten_map[sub_q] = sub_q
            else:
                rewritten_map = {sub_q: sub_q for sub_q in sub_queries}

            queries: List[str] = []
            for sub_q in sub_queries:
                queries.append(sub_q)
                rewritten = rewritten_map.get(sub_q, sub_q)
                if rewritten and rewritten != sub_q:
                    queries.append(rewritten)
            return queries[:MAX_RETRIEVAL_QUERIES]

        # 简单问题双路：原始 + 改写（路数上限见 MAX_RETRIEVAL_QUERIES：
        # 多一路就多一次向量召回 + 候选，合并精排前候选变多会更慢）
        queries = [user_query]
        if search_query and search_query != user_query:
            queries.append(search_query)
        return queries[:MAX_RETRIEVAL_QUERIES]

    def _parallel_search(self, queries: List[str], on_query=None,
                         rerank_query: str = None, alt_rerank_query: str = None) -> list:
        """
        并行执行多路检索并合并去重，返回 Top10 候选。

        延迟优化（RERANK_MERGED_ONCE，默认开）：
        CrossEncoder 精排在 CPU 上是整条链路最重的一步（实测 6 篇 ≈2.2s），
        原先每一路问句各跑一次精排、多路并行还会互相争抢算力，整体反而更慢。
        现在各路只做「向量 + BM25 + 融合」，合并去重后**统一跑一次精排**
        （用 rerank_query，通常是用户的原始问句）。

        Args:
            queries: 待检索问句列表
            on_query: 可选回调 on_query(query, docs, report)，每路检索结果就绪后
                      立即调用（发生在调用方线程内），用于上抛真实检索统计。
            rerank_query: 合并后统一精排使用的问句；None 或关闭开关时退回"每路各精排"
        """
        if not queries:
            return []

        _merged_once = RERANK_MERGED_ONCE and bool(rerank_query)

        def _run_one(q: str):
            rpt = {}
            docs = self.retrieval_agent.search(q, report=rpt, rerank=not _merged_once)
            return q, docs, rpt

        if len(queries) == 1:
            q = queries[0]
            _, docs, rpt = _run_one(q)
            if on_query is not None:
                on_query(q, docs, rpt)
            merged = self._merge_docs(docs)[:RERANK_PRE_FILTER]
            return (self._rerank_merged(rerank_query, merged, alt_rerank_query)
                    if _merged_once else merged[:10])

        # 按提交顺序收集（保证合并结果顺序确定），on_query 仍在各路完成时即时回调
        ordered: List[Optional[list]] = [None] * len(queries)
        with ThreadPoolExecutor(max_workers=min(4, len(queries))) as pool:
            fut_index = {pool.submit(_run_one, q): i for i, q in enumerate(queries)}
            for fut in as_completed(fut_index):
                i = fut_index[fut]
                try:
                    q, docs, rpt = fut.result()
                except Exception as e:
                    logger.warning(f"并行检索失败: {e}")
                    continue
                ordered[i] = docs
                if on_query is not None:
                    on_query(q, docs, rpt)
        results = [d for d in ordered if d is not None]
        merged = self._merge_docs(*results)[:RERANK_PRE_FILTER]
        return (self._rerank_merged(rerank_query, merged, alt_rerank_query)
                if _merged_once else merged[:10])

    def _rerank_merged(self, query: str, merged: list, alt_query: str = None) -> list:
        """多路候选合并去重后统一精排一次（返回跨路 Top10，按精排分降序）

        args:
            query: 首选精排问句（用户原始问句）
            alt_query: 备选精排问句（改写/问法归一后的问句）。
                场景：问句里带口语化成分（如"办卡充值的流程是什么"）时，原句与资料片段的
                语义相关分可能整体偏低（实测 0.15，全被阈值滤掉），而改写句
                （"充值办卡"）能拿到 0.60 —— 此时用改写句再精排一次可救回漏答。
                该兜底只在"原句精排后一条都不达标"时触发（正常命中不会多花时间）。
        """
        if not merged:
            return []
        try:
            from core.reranker import reranker_engine
            from config import RERANK_DOC_MAX_CHARS, VERIFY_MIN_SCORE
            _t0 = time.time()
            reranked = reranker_engine.rerank(query, merged, RERANK_PRE_FILTER,
                                              max_chars=RERANK_DOC_MAX_CHARS)
            _hit = [s for _, _, s in reranked if s >= VERIFY_MIN_SCORE]
            if not _hit and alt_query and alt_query.strip() and alt_query.strip() != query:
                _t1 = time.time()
                # 兜底精排用更短的截断（110 字）：只是"救回"用途，成本约为正常精排的 2/3，
                # 保证漏答场景的总耗时仍然可控（不影响正常命中的那条路径）
                _alt = reranker_engine.rerank(alt_query, merged, RERANK_PRE_FILTER,
                                              max_chars=min(110, RERANK_DOC_MAX_CHARS))
                _alt_hit = [s for _, _, s in _alt if s >= VERIFY_MIN_SCORE]
                if _alt_hit:
                    logger.info(f"[耗时] 合并精排 {len(merged)} 篇(截断{RERANK_DOC_MAX_CHARS}字) → "
                                f"原句无达标，改用改写句「{alt_query}」救回 {len(_alt_hit)} 篇，"
                                f"累计用时 {time.time()-_t0:.2f}s（含二次精排 {time.time()-_t1:.2f}s）")
                    return _alt
            logger.info(f"[耗时] 合并精排 {len(merged)} 篇(截断{RERANK_DOC_MAX_CHARS}字) → "
                        f"{len(_hit)} 篇达标/{len(reranked)} 篇，用时 {time.time()-_t0:.2f}s")
            return reranked
        except Exception as e:
            logger.error(f"合并精排失败，退回融合分排序: {e}")
            return merged[:RERANK_PRE_FILTER]

    # ============== 办卡推荐（action 卡片） ==============

    @staticmethod
    def _card_match_tokens(name: str) -> List[str]:
        """从卡名提取匹配 token："200元=320币" → ["200元", "320币"]；"20币" → ["20币"]"""
        return [p.strip() for p in name.split("=") if len(p.strip()) >= 2]

    @staticmethod
    def _token_in_text(token: str, text: str) -> bool:
        """数字边界感知的子串匹配：
        防止"共320币"误命中"20币"（匹配位前紧邻数字时拒绝）"""
        import re
        for m in re.finditer(re.escape(token), text):
            start, end = m.start(), m.end()
            before = text[start - 1] if start > 0 else ""
            # token 以数字开头时，前一个字符不能也是数字（否则是更长数字的一部分）
            if token[0].isdigit() and before.isdigit():
                continue
            _ = end  # 尾部边界由 token 自身末字符（如"币"/"元"）保证
            return True
        return False

    @classmethod
    def _detect_card_actions(cls, answer: str) -> List[dict]:
        """
        办卡推荐：用在售卡的匹配 token 对 AI 回答做边界感知匹配（零新增 LLM 调用）。
        例：回答含「充值200元赠送120币」→ 命中「200元」→ 推荐卡「200元=320币」；
        「共320币」不会误命中「20币」（数字边界拦截）。
        价格由前端下单时后端从 cards 表权威读取，此处仅展示参考价。
        """
        if not answer:
            return []
        try:
            from database.db import list_cards
            cards = list_cards(status="on_sale")
        except Exception as e:
            logger.warning(f"办卡推荐查询卡产品失败: {e}")
            return []

        matched = []
        for c in cards:
            tokens = cls._card_match_tokens(c["name"])
            if tokens and any(cls._token_in_text(t, answer) for t in tokens):
                matched.append({
                    "card_id": c["id"],
                    "name": c["name"],
                    "price": c["price"],
                    "price_yuan": round(c["price"] / 100, 2),
                    "description": c.get("description", ""),
                })
        return matched[:3]  # 最多推 3 张，避免刷屏

    # ============== 主流程：完整问答 ==============

    # ==================================================================
    # 回答来源分流（画像类 / 无关类）—— 纯规则、零 LLM，跑在检索与模型之前
    # ==================================================================
    # 为什么要有这一层：真实 case「我家里有几个孩子？？」，答案**就在用户画像里**，
    # 但它不命中任何业务词表 → 被当成知识库问题送进检索 → 知识库里当然没有
    # "用户家几个孩子"这种东西 → 零命中 → 旧行为直接吐「园区知识库里暂时没有收录这个问题」。
    # 用户拿到的是"我答不上来"，而同一段回答里其实已经把答案念了一遍。
    #
    # 这一层只做两件**保守**的事（判不出就放行给原流程）：
    #   · 画像类：答案在档案里 → 模板直答（零 LLM，比走 RAG 更快，且零幻觉）；
    #   · 无关类：与园区和档案都不沾 → 说明定位，而不是伪装成"知识库没收录"。
    @staticmethod
    def _view_of(prof_raw: Dict) -> Dict:
        """画像结构化视图（纯内存派生，不产生任何 IO）"""
        if not prof_raw:
            return {}
        try:
            from services import user_profile as _up
            return _up.effective_view(prof_raw)
        except Exception as e:
            logger.warning(f"画像视图构造失败（按空处理）: {e}")
            return {}

    @staticmethod
    def _shortcut_answer(query: str, view: Dict):
        """→ (答案文本, 来源标记)；不适用返回 (None, "")。

        ⚠️ 画像**为空**时也要走画像类：产品要求是"没填就告诉用户还没填"，
        而不是把问题丢给知识库（那样又会回到"未收录"）。
        """
        try:
            from agents import answer_router as _ar
        except Exception as e:
            logger.warning(f"分流模块不可用（跳过分流）: {e}")
            return None, ""
        # 办事型问题优先：像"我卡里还有几张孩子的票"这种带归属词但本质是业务查询的，
        # 不能被画像问答截走（业务 Agent 有余额/订单工具，答得比模板准）。
        try:
            from agents.business_agent import is_business_question as _ibq
            if _ibq(query):
                return None, ""
        except Exception:
            pass
        field = _ar.classify_profile_question(query)
        if field:
            txt = _ar.render_profile_answer(field, view)
            if txt:
                return txt, "profile"
        if _ar.is_offtopic(query):
            return _ar.OFFTOPIC_ANSWER, "offtopic"
        return None, ""

    @staticmethod
    def _confirm_line(query: str, view: Dict) -> str:
        """用户这轮**陈述**了关于自己的新信息 → 返回一行确认；没有新信息返回 ""。

        为什么值得做：画像此前是**悄悄**更新的，用户看不到"AI 记住了"，
        于是会反复确认同一件事（"我家里有几个孩子"就是这么来的）。
        只在真的抽到新字段时出现，不会每轮都刷。
        """
        try:
            from agents import answer_router as _ar
            return _ar.statement_confirm(query, view)
        except Exception:
            return ""

    @classmethod
    def _with_confirm(cls, query: str, view: Dict, answer: str) -> str:
        """非流式路径：把确认行接到答案末尾"""
        if not answer:
            return answer
        line = cls._confirm_line(query, view)
        if not line or line in answer:
            return answer
        return answer.rstrip() + "\n\n" + line

    def process(self, user_query: str, history: str = "", user: dict = None,
                session_id: str = "") -> Dict[str, Any]:
        """
        主处理流程 - 非流式版本
        返回: {"query", "answer", "references", "map_url", "map_image", "extra_info",
              "cards", "services", "trace"}
        user: 可选 JWT 用户 {user_id,...}，业务 Agent 查询个人账户时使用
        session_id: 可选会话 ID，启用服务端多轮记忆（history 仅作回退）
        """
        start_time = time.time()

        # ---- 服务端会话记忆：结构化历史 + 用户画像（杠杆3） ----
        _mem = None
        eff_history = history
        prof_note = ""
        _pv: Dict = {}               # 画像结构化视图（画像问答模板要用）
        _uid = (user or {}).get("user_id")
        _set_llm_user(_uid)          # 让下游模型调用带上隔离用的 user_id（不含隐私）
        if session_id and session_id != "default":
            from core import chat_memory as _cm
            try:
                _mem = _cm.load_context(session_id, _uid, query=user_query)
                eff_history = _mem.get("history_text") or history
                prof_note = _mem.get("profile_note") or ""
                _pv = self._view_of(_mem.get("profile"))
            except Exception as e:
                logger.warning(f"会话记忆加载失败: {e}")
        elif _uid:
            # 没有会话 id：历史不读也不落（保持原语义），但**画像必须生效**
            _pctx = _profile_ctx_for("", _uid, query=user_query)
            prof_note = _pctx.get("note") or ""
            _pv = _pctx.get("view") or {}

        # ---- 0.0 回答来源分流（画像类 / 无关类）：纯规则、零 LLM，先于检索 ----
        _sc_txt, _sc_src = self._shortcut_answer(user_query, _pv)
        if _sc_txt is not None:
            _sc_txt = self._with_confirm(user_query, _pv, _sc_txt)
            if _mem is not None:
                try:
                    from core import chat_memory as _cms
                    _cms.remember_turn(session_id, _uid, user_query, _sc_txt, [], user_query)
                except Exception as e:
                    logger.warning(f"记忆落库失败: {e}")
            return {
                "query": user_query, "answer": _sc_txt, "references": [],
                "cards": [], "services": [], "charts": [],
                "map_url": "", "map_image": "",
                "extra_info": {"agent": _sc_src},
                "trace": {"agent": _sc_src, "shortcut": True,
                          "elapsed": round(time.time() - start_time, 3)},
            }
        # 0. 意图路由（RouterAgent）：与流式路径同一套三级路由（规则 → 小模型分类 → RAG），
        #    与既有词表取并集，保证既有分流不退化、纯知识问答不多花 LLM。
        from agents.router_agent import router_agent
        from agents.map_agent import map_agent
        _route = router_agent.route(user_query)
        _route_agent = _route.get("agent") or "rag"

        # 0.1 天气快速通道：纯天气问句规则化直接回答（0 次 LLM，实时数据不写缓存）
        try:
            from config import WEATHER_FASTPATH_ENABLED
            from tools import weather_tools
            if WEATHER_FASTPATH_ENABLED and weather_tools.is_weather_question(user_query):
                _w_text = weather_tools.weather_brief_text()
                if _w_text:
                    if _mem is not None:
                        try:
                            from core import chat_memory as _cmw
                            _cmw.remember_turn(session_id, _uid, user_query, _w_text, [], user_query)
                        except Exception as e:
                            logger.warning(f"记忆落库失败: {e}")
                    return {
                        "query": user_query, "answer": _w_text, "references": [],
                        "cards": [], "services": [], "charts": [],
                        "map_url": "", "map_image": "",
                        "extra_info": {"agent": "weather"},
                        "trace": {"agent": "weather",
                                  "elapsed": round(time.time() - start_time, 3)},
                    }
                logger.info("🌤️ 天气快速通道取数失败，回落地图 Agent / RAG")
        except Exception as e:
            logger.warning(f"天气快速通道异常（已落地图/RAG）: {e}")

        # 0.2 失物零 LLM 直查短路口（0 次 LLM）：问"我丢的 X 有没有人捡到"时
        #     直接查失物库，命中即给确定性结论（查到就是查到，不靠模型转述）；
        #     未命中返回 None → 放行原链路，由业务 Agent 引导报失登记。
        #     ⚠️ 失物状态随时会被认领/归还 → 属实时数据，**不写问答缓存**（同天气通道）。
        try:
            from tools.service_tools import lost_fastpath_answer
            _lf_text = lost_fastpath_answer(user_query)
            if _lf_text:
                if _mem is not None:
                    try:
                        from core import chat_memory as _cml
                        _cml.remember_turn(session_id, _uid, user_query, _lf_text, [], user_query)
                    except Exception as e:
                        logger.warning(f"记忆落库失败: {e}")
                return {
                    "query": user_query, "answer": _lf_text, "references": [],
                    "cards": [], "services": [], "charts": [],
                    "map_url": "", "map_image": "",
                    "extra_info": {"agent": "lost_found"},
                    "trace": {"agent": "lost_found",
                              "elapsed": round(time.time() - start_time, 3)},
                }
        except Exception as e:
            logger.warning(f"失物直查通道异常（已落原链路）: {e}")

        # 1. 地图意图路由：涉及地址/位置/路线/周边/天气时优先走 AgentExecutor（实时性高，不缓存）
        if map_agent.is_map_question(user_query, history) or _route_agent == "map":
            map_result = map_agent.process(user_query, history, profile_note=prof_note)
            if not map_result.get("fallback"):
                # Agent 成功返回（含地图工具生成的链接/图片）
                return map_result
            # 降级：继续走 RAG 检索原始地址文本
            logger.info(f"🗺️ 地图Agent降级(RAG): {map_result.get('trace', {}).get('fallback_reason', '')}")

        # 1. 尝试从缓存获取（置于业务路由之前：纯知识 FAQ 与业务公共问答二次提问直接秒回；
        #    命中缓存 = 历史上走的是公共链路——个人账户类答案从不写入缓存，不会串用户）
        #    ⚠️ 画像命名空间：注入了用户档案的回答写在 `rag:ans:{画像指纹}:*` 下，
        #    读取必须传同一个 prof_note，否则会读到别人家的定制化答案（反之亦然）。
        from core.cache import redis_cache
        cached = redis_cache.get_cached_answer(user_query, prof_note)
        if cached:
            return {
                "query": user_query,
                "answer": cached,
                "references": [],
                # 缓存命中也要补办卡推荐：缓存只存了答案文本，不存卡片
                "cards": self._detect_card_actions(cached),
                "services": [],
                # 项目实景配图同理：缓存只存答案文本，配图按问句重新匹配
                "charts": _park_photos(user_query),
                "map_url": "",
                "map_image": "",
                "extra_info": {},
                "trace": {"cached": True, "elapsed": round(time.time() - start_time, 3)}
            }

        # 0.5 业务 Agent 路由：个人账户 / 办卡 / 失物 / 组合推荐等"办事型"问题
        from agents.business_agent import business_agent, PERSONAL_TOOL_NAMES
        if business_agent.is_business_question(user_query, history) \
                or _route_agent in ("business", "account", "service"):
            biz = business_agent.process(user_query, history, user=user,
                                         profile_note=prof_note)
            if not biz.get("fallback"):
                answer = biz.get("answer", "")
                # ★ E1b：plan_trip 数字校验未通过（回答里出现工具没下发的金额）→
                #   **该轮不写公共缓存**，避免"编出来的 742"被缓存固化，下次问还返回它。
                #   注意：plan_trip 属 PERSONAL_TOOL_NAMES，本就不会走公共缓存；
                #   这道判断是给未来可能带数字的其它工具留的兜底闸门。
                #   非严格判定（本轮混排了办卡价目等其它工具金额）不作为拦截依据。
                _num = biz.get("number_check") or {}
                _num_bad = (bool(_num.get("checked")) and not bool(_num.get("ok"))
                            and _num.get("strict", True))
                # 业务公共问答（未查询个人账户/消费统计，由实际调用工具集判定）答案也入缓存，
                # 使"怎么办卡充值"这类通用咨询二次提问直接命中缓存，不再重复跑 Agent 工具循环
                # （带画像时写入画像命名空间，见 cache_answer 的 profile_note 说明）
                if answer and not _num_bad \
                        and not (PERSONAL_TOOL_NAMES & set(biz.get("tool_names") or [])):
                    redis_cache.cache_answer(user_query, answer, profile_note=prof_note)
                # 工具生成的图片（如会员码）并入图表卡通道，前端零改动即可展示
                _charts = list(biz.get("charts") or [])
                _charts += [{"chart_id": f"img{i}", "title": im.get("title") or "图片",
                             "url": im.get("url")}
                            for i, im in enumerate(biz.get("images") or []) if im.get("url")]
                # 项目实景配图：问句提到游乐项目就附实景照（纯规则、零 LLM）
                _charts += _park_photos(user_query)
                return {
                    "query": user_query,
                    "answer": answer,
                    "references": [],
                    "cards": self._detect_card_actions(answer) if answer else [],
                    "services": _service_items(biz.get("services", [])),
                    "charts": _charts,
                    "map_url": "",
                    "map_image": "",
                    "extra_info": {"agent": "business"},
                    "trace": {
                        "agent": "business",
                        "tool_calls": biz.get("trace", {}).get("tool_calls", 0),
                        "tool_names": list(biz.get("tool_names") or []),
                        "number_check": biz.get("trace", {}).get("number_check"),
                        "elapsed": round(time.time() - start_time, 3),
                    },
                }
            logger.info(f"💼 业务Agent降级(RAG): {biz.get('trace', {}).get('fallback_reason', '')}")

        # 1. 分析问题复杂度，制定执行计划
        plan = self._analyze_complexity(user_query)

        # 2. Query Agent: 多轮查询改写
        search_query = self.query_agent.rewrite(eff_history, user_query)

        # 3. 并行多路检索（原始 + 改写，复杂问题含全部子问题变体），合并去重
        #    防止改写偏航导致新chunk漏检；各路检索并行执行，降低整体延迟
        retrieval_queries = self._collect_retrieval_queries(user_query, search_query, plan, bool(history))
        top_docs = self._parallel_search(retrieval_queries, rerank_query=user_query,
                                         alt_rerank_query=None)

        # 4. Verification Agent: 校验+溯源
        verified_docs = self.verification_agent.verify(user_query, top_docs)

        # 5. 生成答案
        _is_fallback_answer = False
        if not verified_docs:
            # 零命中：**岔路口**，不再是终点。
            #   · 问句含园区实体词（项目名/票务/办卡/位置…）→ 这是"园区问题但资料里确实没写"，
            #     保留带标记的"资料未收录"话术（知识库盲区看板靠这个标记统计，口径不变）；
            #   · 不含园区实体词 → 交给软兜底，让模型带着**档案 + 对话历史**给出有用回答。
            #     走这条的都不是"知识库缺条目"（是关于用户自己、或与园区无关的问题），
            #     所以**不带标记**，不该被统计进知识库盲区。
            from agents import answer_router as _ar
            _soft = FALLBACK_ADVICE_ENABLED and not _ar.has_park_signal(user_query)
            if _soft:
                answer = "".join(self.verification_agent.generate_soft_fallback_stream(
                    user_query, profile_note=prof_note, history=eff_history))
                _is_fallback_answer = True      # 带画像的个性化回答，不写公共缓存
            else:
                # 确定性结论 + 可直接办理的事项（不写缓存）
                # 画像一并传入：兜底回答也要基于用户档案（零资料分支不会额外调模型）
                answer = ("".join(self.verification_agent.generate_fallback_stream(
                              user_query, profile_note=prof_note))
                          if FALLBACK_ADVICE_ENABLED else "暂时没有查询到相关信息，请换个问法试试。")
            references = []
            cards = []
            _is_fallback_answer = _is_fallback_answer or FALLBACK_ADVICE_ENABLED
        else:
            context = "\n---\n".join([d[0] for d in verified_docs])
            references = list(set([d[1] for d in verified_docs if d[1] != "bm25"]))
            # 三块信息**分区**注入（档案 / 历史 / 资料），不再把画像塞进"参考资料"里 ——
            # 旧写法与"严格只允许使用下方参考资料"的规则自相矛盾，模型于是只敢提档案、不敢依档案作答。
            answer = self.verification_agent.generate_answer(
                user_query, context, references,
                profile_note=prof_note, history=eff_history)
            # 资料命中但答不了问题（模型按 prompt 回了拒绝话术）→ 用「资料 + 通用建议」兜底
            # 这一支仍带 FALLBACK_PREFIX_PARTIAL 标记：它**确实是**知识库缺条目，
            # 盲区看板要统计它（与改造前口径一致）。
            if FALLBACK_ADVICE_ENABLED and "暂时没有查询到相关信息" in (answer or ""):
                _fb_ctx = context
                if prof_note:
                    _fb_ctx += f"\n---\n【用户档案】（来自之前对话，回答可自然提及）\n{prof_note}"
                answer = "".join(self.verification_agent.generate_fallback_stream(user_query, _fb_ctx))
                _is_fallback_answer = True
            cards = self._detect_card_actions(answer)

        # 5.5 用户这轮陈述了关于自己的新信息 → 末尾补一行确认（让"记住了"可见）
        answer = self._with_confirm(user_query, _pv, answer)

        # 6. 缓存结果（通用建议类回答不写缓存）
        if not _is_fallback_answer:
            redis_cache.cache_answer(user_query, answer, references, profile_note=prof_note)

        # 6.5 记忆收尾：落库本轮问答 + 画像更新
        if _mem is not None and answer:
            from core import chat_memory as _cm
            try:
                _cm.remember_turn(session_id, _uid, user_query, answer,
                                  references if verified_docs else [], search_query)
            except Exception as e:
                logger.warning(f"记忆落库失败: {e}")

        elapsed = round(time.time() - start_time, 3)
        return {
            "query": user_query,
            "search_query": search_query,
            "answer": answer,
            "references": references,
            "cards": cards,
            "charts": _park_photos(user_query),
            "map_url": "",
            "map_image": "",
            "extra_info": {},
            "trace": {
                "cached": False,
                "complex": plan.is_complex,
                "sub_queries": plan.sub_queries,
                "docs_retrieved": len(top_docs),
                "docs_verified": len(verified_docs),
                "elapsed": elapsed
            }
        }

    # ============== 流式流程 ==============

    def process_stream(self, user_query: str, history: str = "", user: dict = None,
                       session_id: str = "") -> Generator[Dict, None, None]:
        """
        流式处理流程 - SSE 版本
        yield: {"type": "meta"|"chunk"|"done"|"error"|"step"|"action"|"suggestions", ...}
        step: 真实思考步骤（改写结果/各路召回数/精排保留数/校验通过数/耗时），
              供前端渲染"思考轨迹"时间线。
        user: 可选 JWT 用户 {user_id,...}，业务 Agent 查询个人账户时使用
        session_id: 可选会话 ID；提供时启用服务端结构化多轮记忆与用户画像
                    （history 参数仅在其为空/会话无记录时作为回退）
        """
        start_time = time.time()

        # ---- 服务端会话记忆：结构化历史 + 用户画像（杠杆3） ----
        _mem = None
        eff_history = history
        prof_note = ""
        _pv: Dict = {}               # 画像结构化视图（画像问答模板要用）
        _uid = (user or {}).get("user_id")
        _set_llm_user(_uid)          # 让下游模型调用带上隔离用的 user_id（不含隐私）
        if session_id and session_id != "default":
            from core import chat_memory as _cm
            try:
                _mem = _cm.load_context(session_id, _uid, query=user_query)
                eff_history = _mem.get("history_text") or history
                prof_note = _mem.get("profile_note") or ""
                _pv = self._view_of(_mem.get("profile"))
            except Exception as e:
                logger.warning(f"会话记忆加载失败: {e}")
        elif _uid:
            # 没有会话 id：历史不读也不落（保持原语义），但**画像必须生效**
            _pctx = _profile_ctx_for("", _uid, query=user_query)
            prof_note = _pctx.get("note") or ""
            _pv = _pctx.get("view") or {}

        def _finalize(answer_text: str, refs: list, s_query: str) -> list:
            """答后收尾（不含 LLM）：落库本轮问答 + 更新画像。返回待 yield 的事件列表。

            注意：主动追问建议改由 _suggestions() 在 done 之后产出——
            原先追问用 LLM 生成且卡在 done 之前，实测让用户多等 5~11s。
            """
            events = []
            if _mem is not None and answer_text:
                try:
                    from core import chat_memory as _cm3
                    _cm3.remember_turn(session_id, _uid, user_query, answer_text,
                                       refs, s_query)
                except Exception as e:
                    logger.warning(f"记忆落库失败: {e}")
            return events

        def _suggestions(answer_text: str) -> list:
            """主动追问建议（LLM 软超时 SUGGEST_LLM_TIMEOUT，超时用规则化建议兜底）。"""
            if _mem is None or not answer_text or "暂时没有查询到相关信息" in answer_text:
                return []
            try:
                from core import chat_memory as _cm2
                from config import SUGGEST_LLM_TIMEOUT
                items = _cm2.suggest_followups_with_timeout(
                    user_query, answer_text, prof_note, n=3, timeout=SUGGEST_LLM_TIMEOUT)
                return items or []
            except Exception as e:
                logger.warning(f"生成追问失败: {e}")
                return []

        _step_no = [0]

        def _step(phase: str, title: str, detail: str, meta: dict = None) -> dict:
            """构造一个真实思考步骤事件（meta 携带结构化原始数据）"""
            _step_no[0] += 1
            return {
                "type": "step",
                "step": {
                    "id": f"s{_step_no[0]}",
                    "phase": phase,
                    "title": title,
                    "detail": detail,
                    "meta": meta or {},
                },
            }

        def _clip(s: str, n: int = 22) -> str:
            s = (s or "").strip().replace("\n", " ")
            return s if len(s) <= n else s[:n] + "…"

        # 分段耗时埋点（用于"响应耗时"看板与线上定位瓶颈；随 done 事件下发）
        _marks: Dict[str, float] = {}

        def _mark(name: str):
            _marks[name] = round(time.time() - start_time, 2)

        def _timings_snapshot(_start: float) -> dict:
            snap = dict(_marks)
            snap["total"] = round(time.time() - start_time, 2)
            return snap

        # 0.0 回答来源分流：画像类 / 无关类（纯规则、零 LLM，先于检索与模型）
        #     为什么必须放在检索之前：「我家里有几个孩子」的答案**根本不在知识库里**，
        #     送进检索注定零命中，旧行为会回一句「园区知识库里暂时没有收录这个问题」——
        #     而同一段回答里其实已经把答案念了一遍（真实 case，见 answer_router 模块说明）。
        _sc_txt, _sc_src = self._shortcut_answer(user_query, _pv)
        if _sc_txt is not None:
            _sc_txt = self._with_confirm(user_query, _pv, _sc_txt)
            _sc_title = "用户档案问答" if _sc_src == "profile" else "范围说明"
            _sc_detail = ("识别为关于用户自己的问题，直接用他的档案作答（无需检索、无需大模型）"
                          if _sc_src == "profile" else
                          "与园区业务和用户档案都无关，说明服务范围（不伪装成'知识库没收录'）")
            yield _step("route", _sc_title, _sc_detail, {"agent": _sc_src})
            yield {"type": "meta", "search_query": user_query, "reference_doc": [],
                   "map_url": "", "map_image": "", "extra_info": {"agent": _sc_src}}
            for i in range(0, len(_sc_txt), 3):
                yield {"type": "chunk", "content": _sc_txt[i:i + 3]}
            _mark("shortcut_done")
            for _ev in _finalize(_sc_txt, [], user_query):
                yield _ev
            yield {"type": "done", "elapsed": round(time.time() - start_time, 3),
                   "path": _sc_src, "timings": _timings_snapshot(start_time)}
            _sugg_sc = _suggestions(_sc_txt)
            if _sugg_sc:
                yield {"type": "suggestions", "items": _sugg_sc}
            return

        # 0. 意图路由（RouterAgent）：统一回答"这个问题该谁答"
        #    L1 关键词快速通道（0 次 LLM）→ 仅当三张词表全未命中**且**问句带
        #    "行动/实时数据"标记时，才做一次小模型分类（硬超时 1.5s + Redis 缓存）
        #    → 其余一律默认 RAG。与原关键词判定**取并集**，既有分流行为不退化；
        #    纯知识问答不会触发分类调用，满足"检索速度不能变慢"的硬约束。
        from agents.router_agent import router_agent
        from agents.map_agent import map_agent
        from core.cache import redis_cache
        _route = router_agent.route(user_query)
        _route_agent = _route.get("agent") or "rag"
        if _route.get("source") in ("llm", "cache"):
            _src_cn = "模型判定" if _route.get("source") == "llm" else "路由缓存"
            yield _step("route", "意图路由",
                        f"{_src_cn}：{_route.get('intent') or '未标注'}"
                        f"（{_route_agent} · 置信度 {float(_route.get('confidence') or 0):.2f}）",
                        {"router": _route})

        # 0.1 天气快速通道：纯天气问句走规则模板直接回答（0 次 LLM，实测 ≤1s）
        #     园区是室外乐园，天气直接决定"来不来、玩什么"，所以输出的是出游结论
        #     而不只是温度数字。取数失败/非纯天气问句 → 自动落回地图 Agent 或 RAG。
        #     ⚠️ 天气是实时数据，回答**不写 Redis 问答缓存**（下面直接 return，不调 cache_answer）
        try:
            from config import WEATHER_FASTPATH_ENABLED
            from tools import weather_tools
            if WEATHER_FASTPATH_ENABLED and weather_tools.is_weather_question(user_query):
                yield _step("route", "天气助手",
                            "识别为天气问题，直接查询实时天气与未来两天预报（无需大模型）")
                _w_text = weather_tools.weather_brief_text()
                if _w_text:
                    _mark("weather_done")
                    yield {"type": "meta", "search_query": user_query, "reference_doc": [],
                           "map_url": "", "map_image": "", "extra_info": {"agent": "weather"}}
                    for i in range(0, len(_w_text), 3):
                        yield {"type": "chunk", "content": _w_text[i:i + 3]}
                    for _ev in _finalize(_w_text, [], user_query):
                        yield _ev
                    yield {"type": "done", "elapsed": round(time.time() - start_time, 3),
                           "path": "weather", "timings": _timings_snapshot(start_time)}
                    _sugg_w = _suggestions(_w_text)
                    if _sugg_w:
                        yield {"type": "suggestions", "items": _sugg_w}
                    return
                logger.info("🌤️ 天气快速通道取数失败，回落地图 Agent / RAG")
        except Exception as e:
            logger.warning(f"天气快速通道异常（已落地图/RAG）: {e}")

        # 0.2 失物零 LLM 直查短路口（0 次 LLM，语义与非流式路径完全一致）：
        #     命中失物库即确定性回答；未命中直接放行原链路（业务 Agent 引导报失）。
        #     ⚠️ 同天气：实时数据，不写 Redis 问答缓存（下面直接 return，不调 cache_answer）。
        try:
            from tools.service_tools import lost_fastpath_answer
            _lf_text = lost_fastpath_answer(user_query)
            if _lf_text:
                _mark("lost_found")
                yield _step("route", "失物招领",
                            "识别为失物查询，直接检索园区失物库（无需大模型）")
                yield {"type": "meta", "search_query": user_query, "reference_doc": [],
                       "map_url": "", "map_image": "", "extra_info": {"agent": "lost_found"}}
                for i in range(0, len(_lf_text), 3):
                    yield {"type": "chunk", "content": _lf_text[i:i + 3]}
                for _ev in _finalize(_lf_text, [], user_query):
                    yield _ev
                yield {"type": "done", "elapsed": round(time.time() - start_time, 3),
                       "path": "lost_found", "timings": _timings_snapshot(start_time)}
                _sugg_l = _suggestions(_lf_text)
                if _sugg_l:
                    yield {"type": "suggestions", "items": _sugg_l}
                return
        except Exception as e:
            logger.warning(f"失物直查通道异常（已落原链路）: {e}")

        # 1. 地图意图路由：涉及地址/位置/路线/周边/天气时优先走 AgentExecutor
        if map_agent.is_map_question(user_query, eff_history) or _route_agent == "map":
            yield _step("route", "意图路由",
                        "检测到位置/路线相关问题，启动地图 Agent", {"agent": "map"})
            # 后台线程执行 Agent，工具调用进度经队列实时 yield（Agent 多步执行 10~40s，避免用户干等）
            import queue as _queue
            import threading as _threading
            q: _queue.Queue = _queue.Queue()

            def _run_map():
                try:
                    res = map_agent.process(
                        user_query, history, profile_note=prof_note,
                        progress_cb=lambda m: q.put({"type": "status", "content": m}),
                    )
                    q.put({"__result__": res})
                except Exception as e:  # 兕底：线程内异常也转成降级结果，不中断 SSE
                    q.put({"__error__": str(e)})

            _threading.Thread(target=_run_map, daemon=True).start()

            map_result = None
            while True:
                try:
                    item = q.get(timeout=60)  # AgentExecutor 自限 45s，此处 60s 兜底
                except _queue.Empty:
                    map_result = map_agent._fallback_result(user_query, history, start_time, "agent_timeout")
                    break
                if "__result__" in item:
                    map_result = item["__result__"]
                    break
                if "__error__" in item:
                    map_result = map_agent._fallback_result(
                        user_query, history, start_time, f"agent_error: {item['__error__'][:120]}")
                    break
                # 地图工具的每一步真实进度 → 思考步骤
                yield _step("map_tool", "地图工具", item.get("content", ""))

            if not map_result.get("fallback"):
                # Agent 成功：meta 携带地图资源 → 流式输出答案
                yield {
                    "type": "meta",
                    "search_query": map_result.get("search_query", user_query),
                    "reference_doc": map_result.get("references", []),
                    "map_url": map_result.get("map_url", ""),
                    "map_image": map_result.get("map_image", ""),
                    "extra_info": map_result.get("extra_info", {}),
                    "agent": "map",
                }
                answer = map_result.get("answer", "")
                # 按 3 字符分片推送：保持打字机流式效果，同时减少 SSE 事件数（逐字符会导致事件量过大）
                for i in range(0, len(answer), 3):
                    yield {"type": "chunk", "content": answer[i:i + 3]}
                redis_cache.cache_answer(user_query, answer)
                for _ev in _finalize(answer, [], ""):
                    yield _ev
                yield {"type": "done", "elapsed": map_result.get("trace", {}).get("elapsed", 0),
                       "path": "map", "timings": _timings_snapshot(start_time)}
                _sugg = _suggestions(answer)
                if _sugg:
                    yield {"type": "suggestions", "items": _sugg}
                return
            # 降级：继续走 RAG
            _fb_reason = map_result.get('trace', {}).get('fallback_reason', '')
            logger.info(f"🗺️ 地图Agent降级(RAG): {_fb_reason}")
            if _fb_reason:
                _fb_brief = {
                    "quota_exceeded": "地图服务当日配额已用完",
                    "agent_timeout": "地图 Agent 执行超时",
                    "no_tool_used": "地图 Agent 判断无需调用地图工具",
                    "empty_answer": "地图 Agent 未返回有效结果",
                    "agent_error": "地图 Agent 执行出错",
                }.get(_fb_reason.split(":")[0].strip(), "实时地图服务暂时不可用")
                yield _step("route", "降级策略",
                            f"{_fb_brief}，已切换为知识库资料回答",
                            {"fallback_reason": _fb_reason})

        # 1. 缓存检查（置于业务路由之前）：纯知识 FAQ / 业务公共问答曾答过即直接流式返回；
        #    命中缓存 = 该问句历史上走的是公共链路（个人账户类答案从不写入缓存），不会串用户
        #    ⚠️ prof_note 必须与写入侧一致：带画像的答案只在同一画像命名空间里被命中
        cached_entry = redis_cache.get_cached_entry(user_query, prof_note)
        cached = (cached_entry or {}).get("answer") if cached_entry else None
        if cached:
            _mark("cache_hit")
            yield _step("cache", "命中缓存",
                        "这个问题之前回答过，直接返回缓存答案（无需重复检索）")
            # 缓存里连 references 一起存（见 RedisCache.cache_answer）：
            # 命中时照样把来源透出，问答记录页与前端引用卡片都不会丢依据。
            # 历史缓存没有该字段 → 退化为 []，但元信息带 cached=True，
            # 盲区统计按"回答类型"判定，不会再把缓存命中误当成知识库没答上来。
            yield {"type": "meta", "search_query": user_query,
                   "reference_doc": list(cached_entry.get("references") or []),
                   "cached": True}
            # 缓存答案也分片流式输出
            for i in range(0, len(cached), 3):
                yield {"type": "chunk", "content": cached[i:i + 3]}
            # 缓存命中同样补办卡推荐（缓存只存了答案文本）
            action_cards = self._detect_card_actions(cached)
            if action_cards:
                yield {"type": "action", "cards": action_cards}
            # 项目实景配图同理：缓存只存答案文本，配图按问句重新匹配（纯规则，零成本）
            _park_pics_cached = _park_photos(user_query)
            if _park_pics_cached:
                yield {"type": "charts", "charts": _park_pics_cached}
            yield {"type": "done", "elapsed": round(time.time() - start_time, 2),
                   "path": "cache", "timings": _timings_snapshot(start_time)}
            # 缓存命中也给"接着问"建议（基于缓存答案，规则化即时产出，不影响秒回体验）
            _sugg_cached = _suggestions(cached)
            if _sugg_cached:
                yield {"type": "suggestions", "items": _sugg_cached}
            return

        # 0.5 业务 Agent 路由：个人账户 / 办卡 / 失物 / 组合推荐等"办事型"问题
        #     业务答案仅在未查询个人账户时写缓存（由实际工具集判定），避免个人数据串用户
        #     ⚠️ 这里的 `or _route_agent in (...)` 与非流式路径（见 process 的同名判定）**必须一致**：
        #     此前流式路径漏了这半句，导致 RouterAgent 已经判成 business 的问句
        #     （如"帮我买三个过山车"）在这里仍按关键词表为 False → 直接掉进 RAG →
        #     知识库无此条目 → 回一段"AI 通用建议"。小程序走的就是流式路径。
        from agents.business_agent import business_agent, PERSONAL_TOOL_NAMES
        if business_agent.is_business_question(user_query, eff_history) \
                or _route_agent in ("business", "account", "service"):
            _mark("route_business")
            yield _step("route", "业务助手",
                        "识别为可执行的园区业务，正在调起业务 Agent 查询真实数据",
                        {"agent": "business"})
            import queue as _queue
            import threading as _threading
            q: _queue.Queue = _queue.Queue()

            def _run_biz():
                try:
                    res = business_agent.process(
                        user_query, history, user=user, profile_note=prof_note,
                        progress_cb=lambda m: q.put({"type": "status", "content": m}),
                    )
                    q.put({"__result__": res})
                except Exception as e:  # 兜底：线程内异常转降级结果，不中断 SSE
                    q.put({"__error__": str(e)})

            _threading.Thread(target=_run_biz, daemon=True).start()

            biz_result = None
            while True:
                try:
                    item = q.get(timeout=70)  # AgentExecutor 自限 50s，此处 70s 兜底
                except _queue.Empty:
                    biz_result = business_agent._fallback_result(
                        user_query, time.time(), "agent_timeout")
                    break
                if "__result__" in item:
                    biz_result = item["__result__"]
                    break
                if "__error__" in item:
                    biz_result = business_agent._fallback_result(
                        user_query, time.time(), f"agent_error: {item['__error__'][:120]}")
                    break
                yield _step("business", "业务执行", item.get("content", ""))

            if not biz_result.get("fallback"):
                _mark("business_done")
                biz_answer = biz_result.get("answer", "")
                # ★ E1b：plan_trip 数字校验未通过 → 不写公共缓存（见同步路径同一注释）
                _num = biz_result.get("number_check") or {}
                _num_bad = (bool(_num.get("checked")) and not bool(_num.get("ok"))
                            and _num.get("strict", True))
                # 业务公共问答（未调用个人账户/统计工具）也写答案缓存，
                # 使"怎么办卡充值"等通用咨询二次提问直接命中缓存
                if biz_answer and not _num_bad \
                        and not (PERSONAL_TOOL_NAMES & set(biz_result.get("tool_names") or [])):
                    redis_cache.cache_answer(user_query, biz_answer, profile_note=prof_note)
                yield {
                    "type": "meta",
                    "search_query": user_query,
                    "reference_doc": [],
                    "agent": "business",
                    "map_url": "",
                    "map_image": "",
                    "extra_info": {"agent": "business"},
                }
                # 打字机流式输出业务答案
                for i in range(0, len(biz_answer), 3):
                    yield {"type": "chunk", "content": biz_answer[i:i + 3]}
                # 可执行出口：办卡卡片（答案规则匹配）+ 服务按钮（查失物/报失/去充值）
                action_cards = self._detect_card_actions(biz_answer)
                biz_services = _service_items(biz_result.get("services", []))
                if action_cards or biz_services:
                    yield {"type": "action", "cards": action_cards, "services": biz_services}
                # 可视化图表（消费统计工具生成，PNG URL 列表）+ 工具生成的图片
                # （如会员码二维码）：复用前端已有的图表图片卡通道，零前端改动
                _biz_charts = list(biz_result.get("charts") or [])
                _biz_charts += [{"chart_id": f"img{i}", "title": im.get("title") or "图片",
                                 "url": im.get("url")}
                                for i, im in enumerate(biz_result.get("images") or [])
                                if im.get("url")]
                # 项目实景配图：问句提到游乐项目就附实景照（纯规则、零 LLM）
                _biz_charts += _park_photos(user_query)
                if _biz_charts:
                    yield {"type": "charts", "charts": _biz_charts}
                for _ev in _finalize(biz_answer, [], user_query):
                    yield _ev
                yield {"type": "done", "elapsed": round(time.time() - start_time, 3),
                       "path": "business", "timings": _timings_snapshot(start_time)}
                # done 之后再产出追问建议：用户已经看到完整回答，不再等 LLM 追问
                _sugg = _suggestions(biz_answer)
                if _sugg:
                    yield {"type": "suggestions", "items": _sugg}
                return
            # 降级：继续走缓存 / RAG
            logger.info(f"💼 业务Agent降级(RAG): {biz_result.get('trace', {}).get('fallback_reason', '')}")

        # 1. 分析复杂度（识别是否多维度问题）
        plan = self._analyze_complexity(user_query)
        if plan.is_complex and len(plan.sub_queries) > 1:
            yield _step("plan", "问题拆解",
                        f"这是一个多维度问题，拆成 {len(plan.sub_queries)} 个子问题分别检索",
                        {"sub_queries": plan.sub_queries})

        # 2. Query Agent：结合上文改写/问法归一，得到真实检索问句
        rewrite_report = {}
        search_query = self.query_agent.rewrite(eff_history, user_query, report=rewrite_report)
        _mark("rewrite_done")
        rw_mode = rewrite_report.get("mode")
        if rw_mode == "llm":
            _orig = _clip(rewrite_report.get("original", user_query))
            _rw = _clip(rewrite_report.get("rewritten", search_query))
            _pat = rewrite_report.get("matched_pattern")
            _detail = f"结合上文把「{_orig}」改写为独立检索问句「{_rw}」"
            if _pat:
                _detail += f"，并归一到常见问法「{_clip(_pat, 18)}」"
            yield _step("rewrite", "理解与改写", _detail, rewrite_report)
        elif rw_mode == "pattern" and rewrite_report.get("matched_pattern"):
            _pat = rewrite_report["matched_pattern"]
            _sim = rewrite_report.get("similarity", 0)
            yield _step("rewrite", "理解与改写",
                        f"你的问法与知识库常见问法「{_clip(_pat, 18)}」最相似（相似度 {_sim:.2f}），用它来检索",
                        rewrite_report)
        else:
            # 无历史 / 无改写 / 失败回退：直接使用原始问句
            _rw = _clip(search_query or user_query)
            if rw_mode == "fallback_pattern":
                yield _step("rewrite", "理解与改写",
                            f"改写暂不可用，使用问法库匹配后的问句「{_rw}」直接检索",
                            rewrite_report)
            else:
                yield _step("rewrite", "理解问题", f"问题明确，直接检索「{_rw}」", rewrite_report)

        # 3. 并行多路检索（原始 + 改写，复杂问题含全部子问题变体），合并去重
        #    防止改写偏航导致新chunk漏检；各路检索并行执行，降低整体延迟
        retrieval_queries = self._collect_retrieval_queries(user_query, search_query, plan, bool(eff_history))
        yield _step("retrieve", "并行检索",
                    f"正在并行检索 {len(retrieval_queries)} 路问句：BGE-M3 语义召回 + BM25 关键词召回",
                    {"queries": retrieval_queries})

        _retrieval_t0 = time.time()
        _query_steps = []

        def _on_query_done(query: str, docs: list, rpt: dict):
            """每路检索的真实结果 → 一条思考步骤（在并行结果就绪时立即生成）"""
            if rpt.get("cache_hit"):
                _detail = f"「{_clip(query, 16)}」命中检索缓存，直接返回 {rpt.get('docs_returned', len(docs))} 条"
            elif rpt.get("docs_returned", 0) == 0:
                _detail = f"「{_clip(query, 16)}」没有检索到匹配内容"
            else:
                _vec = rpt.get("vector_candidates", 0)
                _bm = rpt.get("bm25_candidates", 0)
                _fused = rpt.get("fused_candidates", 0)
                _out = rpt.get("docs_returned", 0)
                _detail = (f"「{_clip(query, 16)}」语义召回 {_vec} 条 + 关键词命中 {_bm} 条"
                           f" → 融合 {_fused} 条 → Rerank 精排后保留 {_out} 条")
                if not rpt.get("threshold_passed"):
                    _detail += "（相关度不足，兜底返回）"
            _ev = _step("retrieve", "检索结果", _detail, rpt)
            # 该路检索自身的耗时（毫秒→秒）
            _ev["step"]["elapsed"] = round(float(rpt.get("elapsed_ms", 0) or 0) / 1000.0, 2)
            _query_steps.append(_ev)

        # 兜底精排问句的准入：仅当改写结果高置信时才允许用它救回漏答。
        # 依据：问法库匹配相似度 ≥0.8（如"办卡充值的流程是什么"→"充值办卡" 0.89 能救回 3 篇），
        # 或走了 LLM 改写（通常更贴合原意）。低置信改写（如"停车方便吗"→"过山车收费" 0.74）
        # 不参与兜底，宁可如实回答"未检索到"，也不给出跑偏答案。
        _alt_q = None
        if search_query and search_query != user_query:
            _sim = float(rewrite_report.get("similarity") or 0)
            if _sim >= 0.8 or rewrite_report.get("mode") in ("llm", "llm_no_change"):
                _alt_q = search_query

        top_docs = self._parallel_search(retrieval_queries, on_query=_on_query_done,
                                         rerank_query=user_query,
                                         alt_rerank_query=_alt_q)
        _retrieval_elapsed = round(time.time() - _retrieval_t0, 2)
        _mark("retrieval_done")
        for ev in _query_steps:
            yield ev

        # 3.1 多路结果合并去重
        if len(retrieval_queries) > 1:
            _merge_detail = f"合并 {len(retrieval_queries)} 路结果并去重，共 {len(top_docs)} 篇候选进入校验"
            if RERANK_MERGED_ONCE:
                _merge_detail += "（多路候选合并后统一精排一次）"
            yield _step("merge", "合并去重", _merge_detail,
                        {"merged_candidates": len(top_docs), "elapsed": _retrieval_elapsed,
                         "rerank_merged_once": RERANK_MERGED_ONCE})

        # 4. 校验 Agent：阈值 + LLM 相关性过滤，只留真正相关的资料
        verify_report = {}
        verified_docs = self.verification_agent.verify(user_query, top_docs, report=verify_report)
        _mark("verify_done")
        _vin = verify_report.get("docs_in", len(top_docs))
        _vout = verify_report.get("docs_out", len(verified_docs))
        if verify_report.get("llm_verify"):
            yield _step("verify", "相关性校验",
                        f"LLM 逐条校验 {_vin} 篇候选，保留 {_vout} 篇高相关资料",
                        verify_report)
        else:
            yield _step("verify", "相关性校验",
                        f"按相关度过滤 {_vin} 篇候选，保留 {_vout} 篇高相关资料",
                        verify_report)

        references = list(set([d[1] for d in verified_docs if d[1] != "bm25"])) if verified_docs else []
        yield {"type": "meta", "search_query": search_query, "reference_doc": references,
               "complex": plan.is_complex, "map_url": "", "map_image": "", "extra_info": {}}

        # 5. 生成 — 基于校验通过的参考资料实时输出
        _is_fallback_answer = False
        if not verified_docs:
            if not FALLBACK_ADVICE_ENABLED:
                # 开关关闭：恢复旧行为（直接如实告知没查到）
                yield _step("answer", "未找到资料",
                            "知识库中暂时没有与这个问题直接相关的内容，正在如实告知",
                            {"verified": 0})
                yield {"type": "chunk", "content": "暂时没有查询到相关信息，请换个问法试试。"}
                answer = ""
            else:
                # 零命中：**岔路口**，不再是终点（与非流式路径同一套规则）。
                from agents import answer_router as _ar
                if not _ar.has_park_signal(user_query):
                    # 不含园区实体词 → 软兜底：带着档案 + 对话历史让模型回答
                    # （走这条的不是"知识库缺条目"，所以**不带标记**，不进盲区统计）
                    yield _step("answer", "档案问答",
                                "知识库里没有这条，改由用户档案与对话历史来回答",
                                {"verified": 0, "soft_fallback": True})
                    _mark("gen_start")
                    answer_parts = []
                    try:
                        for chunk in self.verification_agent.generate_soft_fallback_stream(
                                user_query, profile_note=prof_note, history=eff_history):
                            yield {"type": "chunk", "content": chunk}
                            answer_parts.append(chunk)
                    except Exception as e:
                        yield {"type": "error", "content": f"生成回答时出错: {str(e)}"}
                    answer = "".join(answer_parts)
                    _mark("gen_done")
                    _is_fallback_answer = True
                else:
                    # 园区问题但资料里确实没写：给出确定性结论（没收录就是没收录，不编）
                    # + 可直接办理的事项。带 FALLBACK_PREFIX 标记，盲区看板口径不变。
                    yield _step("answer", "未找到资料",
                                "知识库中暂时没有与这个问题直接相关的内容，正在如实答复并给出可以直接办理的事项",
                                {"verified": 0, "fallback_answer": True})
                    _mark("gen_start")
                    answer_parts = []
                    try:
                        for chunk in self.verification_agent.generate_fallback_stream(
                                user_query, profile_note=prof_note):
                            yield {"type": "chunk", "content": chunk}
                            answer_parts.append(chunk)
                    except Exception as e:
                        yield {"type": "error", "content": f"生成建议时出错: {str(e)}"}
                    answer = "".join(answer_parts)
                    _mark("gen_done")
                    _is_fallback_answer = True
        else:
            context = "\n---\n".join([d[0] for d in verified_docs])
            yield _step("generate", "生成回答",
                        f"已获取 {len(verified_docs)} 篇参考资料，正在整理回答（严格基于资料，不编造）",
                        {"context_docs": len(verified_docs)})
            _mark("gen_start")
            answer_parts = []
            # 拒绝话术闸门：资料虽命中但内容答不了问题时，模型会照 prompt 输出
            # "暂时没有查询到相关信息"。这里只缓冲开头极少量字符（≤1 句），
            # 一旦识别为拒绝话术就丢弃，改走"通用建议 + 非官方文档声明"兜底，
            # 避免用户只看到一句干巴巴的"没查到"。缓冲对首字延迟的影响可忽略。
            _REFUSAL = "暂时没有查询到相关信息"
            _buf = ""
            _refused = False
            try:
                _gen = self.verification_agent.generate_answer_stream(
                    user_query, context, references,
                    profile_note=prof_note, history=eff_history)
                for chunk in _gen:
                    if _buf is None:
                        yield {"type": "chunk", "content": chunk}
                        answer_parts.append(chunk)
                        continue
                    _buf += chunk
                    # 忽略开头空白/换行后再比对（模型常先输出一个换行）
                    _probe = _buf.lstrip()
                    if _REFUSAL.startswith(_probe[:len(_REFUSAL)]):
                        if len(_probe) >= len(_REFUSAL):
                            _refused = True
                            break
                        continue  # 仍可能是拒绝话术的前缀 → 继续缓冲
                    # 不是拒绝话术：立即把缓冲整段吐出，恢复正常流式
                    yield {"type": "chunk", "content": _buf}
                    answer_parts.append(_buf)
                    _buf = None
            except Exception as e:
                yield {"type": "error", "content": f"生成回答时出错: {str(e)}"}
            if _buf and not _refused:
                yield {"type": "chunk", "content": _buf}
                answer_parts.append(_buf)
                _buf = None
            if _refused and FALLBACK_ADVICE_ENABLED:
                # 丢弃拒绝话术，改为**只基于已命中的资料**给结论。
                # 关键：把已检索到的资料一并传给兜底 prompt，避免把知识库里已有的事实
                # （如退卡规则、儿童陪同规则）也一起丢掉；但不再补"通用建议"。
                yield _step("answer", "资料不足以回答",
                            "命中的资料不足以直接回答该问题，正在只依据资料已有信息重新组织回答",
                            {"verified": len(verified_docs), "fallback_answer": True})
                answer_parts = []
                _fb_ctx2 = context
                if prof_note:
                    _fb_ctx2 += f"\n---\n【用户档案】（来自之前对话，回答可自然提及）\n{prof_note}"
                for chunk in self.verification_agent.generate_fallback_stream(user_query, _fb_ctx2):
                    yield {"type": "chunk", "content": chunk}
                    answer_parts.append(chunk)
                _is_fallback_answer = True
            answer = "".join(answer_parts)
            _mark("gen_done")

        # 5.05 用户这轮陈述了关于自己的新信息 → 末尾补一行确认（让"记住了"可见）
        _cl = self._confirm_line(user_query, _pv)
        if _cl and _cl not in (answer or ""):
            _extra = "\n\n" + _cl
            yield {"type": "chunk", "content": _extra}
            answer = (answer or "").rstrip() + _extra

        # 5.1 办卡推荐：回答完整后做卡名规则匹配，推送 action 卡片事件
        action_cards = self._detect_card_actions(answer)
        if action_cards:
            yield {"type": "action", "cards": action_cards}

        # 5.2 项目实景配图：问句命中游乐项目 → 复用图片卡通道推送实景照。
        #     纯规则匹配 + 文件存在性判断（毫秒级、零 LLM），不影响回答耗时；
        #     缓存的回答同样会走到这里，所以缓存命中时配图也照样出。
        _park_pics = _park_photos(user_query)
        if _park_pics:
            yield {"type": "charts", "charts": _park_pics}

        # 6. 缓存（通用建议类回答不写缓存：它不是知识库权威答案，避免长期命中）
        #    连 references 一起缓存，缓存命中时才能把来源透出
        #    profile_note：答案里可能提到孩子年龄/身高 → 只能被同一画像命中
        if answer and not _is_fallback_answer:
            redis_cache.cache_answer(user_query, answer, references, profile_note=prof_note)

        # 6.5 记忆收尾：落库 + 画像（本地 DB，快）
        for _ev in _finalize(answer, references, search_query):
            yield _ev

        elapsed = round(time.time() - start_time, 3)
        yield {"type": "done", "elapsed": elapsed, "path": "rag",
               "timings": _timings_snapshot(start_time)}
        # 6.6 主动追问建议（杠杆3）：done 之后再生成，不阻塞用户看到回答
        _sugg = _suggestions(answer)
        if _sugg:
            yield {"type": "suggestions", "items": _sugg}


# 全局单例
orchestrator = OrchestratorAgent()
