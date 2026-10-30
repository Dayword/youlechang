# ============================================================
# Query Agent (QueryAgent)
# 职责：多轮对话改写 + 常见问法相似度匹配 + 意图识别 + 关键词扩展
# ============================================================
import os
import re
import json
import logging
import numpy as np
from typing import Optional, List, Tuple

logger = logging.getLogger(__name__)


class QueryAgent:
    """
    Query 改写 Agent
    流程：
    0. 多轮对话改写（指代消解）
    1. 常见问法匹配 → 把口语化问题映射到知识库常见问法
    2. 意图识别（票务/设施/营业/应急/其他）
    3. 关键词提取与扩展
    """

    REWRITE_PROMPT = """根据对话历史，将用户的新问题改写为一个独立、完整的检索问句。
要求：只输出改写后的问句，不要任何解释和多余文字。

【历史对话】
{history}

【用户新提问】
{query}

【改写后的问句】"""

    KEYWORD_EXPAND_PROMPT = """为以下检索问句生成3-5个相关关键词/同义词，以逗号分隔。
要求：只输出关键词，不要任何解释。

检索问句：{query}
关键词："""

    # 常见问法匹配阈值（余弦相似度）
    PATTERN_SIMILARITY_THRESHOLD = 0.55
    # 多轮场景"免 LLM 改写"的短路阈值：命中问法库且相似度够高时直接用该问法检索
    PATTERN_SHORTCUT_SIMILARITY = float(os.getenv("PATTERN_SHORTCUT_SIMILARITY", "0.85"))

    def __init__(self):
        self._patterns = None          # [(pattern_text, chunk_id, full_chunk_text, source), ...]
        self._pattern_embeddings = None  # numpy array (n_patterns, 1024)
        self._loaded = False

    # ============== 常见问法库加载 ==============

    def _load_patterns_from_chunks(self) -> List[Tuple[str, int, str, str]]:
        """
        从 chunks/ 目录下所有 JSON 文件中提取 **常见问法** 模式
        返回: [(pattern_text, chunk_id, full_chunk_text, source), ...]
        """
        import config
        chunks_dir = os.path.join(
            os.path.dirname(config.DOCS_DIR) if config.DOCS_DIR.startswith(".")
            else os.path.dirname(__file__), "chunks"
        )
        if not os.path.exists(chunks_dir):
            logger.warning(f"chunks 目录不存在: {chunks_dir}")
            return []

        patterns = []
        chunk_files = [f for f in os.listdir(chunks_dir) if f.endswith("_chunks.json")]
        logger.info(f"📂 扫描 chunks 目录，发现 {len(chunk_files)} 个切片文件")

        for fname in chunk_files:
            fpath = os.path.join(chunks_dir, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for chunk in data.get("chunks", []):
                    text = chunk.get("text", "")
                    # 提取 **常见问法**：xxx 或 **常见问法**: xxx
                    match = re.search(r'\*\*常见问法\*\*[：:]\s*(.+?)(?:\n|$)', text)
                    if match:
                        question_str = match.group(1).strip()
                        # 按逗号/顿号/中文逗号拆分多个问法
                        raw_patterns = re.split(r'[，,、]', question_str)
                        for p in raw_patterns:
                            p = p.strip()
                            if p and len(p) >= 2:
                                patterns.append((
                                    p,
                                    chunk.get("chunk_id", 0),
                                    text,
                                    chunk.get("source", fname)
                                ))
            except Exception as e:
                logger.warning(f"解析切片文件失败: {fname}, {e}")

        logger.info(f"✅ 常见问法库加载完成: {len(patterns)} 条问法模式")
        return patterns

    def _ensure_loaded(self):
        """懒加载：首次调用时加载问法库并向量化"""
        if self._loaded:
            return
        self._patterns = self._load_patterns_from_chunks()
        if self._patterns:
            self._build_embeddings()
        self._loaded = True

    def _build_embeddings(self):
        """用 BGE-M3 对所有常见问法做向量化"""
        from core.embedding import embedding_engine
        pattern_texts = [p[0] for p in self._patterns]
        try:
            embeddings = embedding_engine.embed_documents(pattern_texts)
            self._pattern_embeddings = np.array(embeddings, dtype=np.float32)
            logger.info(f"📊 问法向量化完成: shape={self._pattern_embeddings.shape}")
        except Exception as e:
            logger.error(f"问法向量化失败: {e}")
            self._pattern_embeddings = None

    def reload_patterns(self):
        """重新加载问法库（文档更新后调用）"""
        self._loaded = False
        self._patterns = None
        self._pattern_embeddings = None
        self._ensure_loaded()

    # ============== 常见问法匹配 ==============

    def match_question(self, query: str, top_k: int = 3) -> Tuple[str, List[dict]]:
        """
        将用户问题与常见问法库做 BGE-M3 相似度匹配

        Args:
            query: 用户原始问题
            top_k: 返回匹配数

        Returns:
            (best_search_query, [{"pattern": str, "similarity": float, "chunk_text": str, "source": str}, ...])
            如果无匹配或相似度过低，原样返回 query
        """
        self._ensure_loaded()
        if not self._patterns or self._pattern_embeddings is None:
            logger.debug("问法库为空，跳过匹配")
            return query, []

        # 向量化用户问题
        from core.embedding import embedding_engine
        try:
            query_vec = embedding_engine.embed_query(query)
            query_vec = np.array(query_vec, dtype=np.float32)
        except Exception as e:
            logger.error(f"用户问题向量化失败: {e}")
            return query, []

        # 余弦相似度计算
        query_norm = query_vec / (np.linalg.norm(query_vec) + 1e-8)
        pattern_norms = self._pattern_embeddings / (
            np.linalg.norm(self._pattern_embeddings, axis=1, keepdims=True) + 1e-8
        )
        similarities = np.dot(pattern_norms, query_norm)

        # Top-K 匹配
        top_indices = np.argsort(similarities)[::-1][:top_k]
        matches = []
        for idx in top_indices:
            sim = float(similarities[idx])
            if sim >= self.PATTERN_SIMILARITY_THRESHOLD:
                pattern_text, chunk_id, chunk_text, source = self._patterns[idx]
                matches.append({
                    "pattern": pattern_text,
                    "similarity": round(sim, 4),
                    "chunk_id": chunk_id,
                    "chunk_text": chunk_text[:200],
                    "source": source
                })

        if not matches:
            logger.debug(f"🔍 问法匹配: 无高相似度匹配 (max={float(similarities.max()):.3f})")
            return query, []

        # 用最佳匹配的常见问法作为检索 query
        best = matches[0]["pattern"]
        logger.info(
            f"🔍 问法匹配成功: '{query[:30]}...' → '{best}' "
            f"(相似度: {matches[0]['similarity']:.3f}, "
            f"共 {len(matches)} 个匹配)"
        )
        return best, matches

    # ============== 多轮改写 ==============

    # 意图分类关键词表（本地规则版：仅用于运营统计，替代原 LLM 分类，
    # 省去每次问答额外的一次 LLM 调用，按优先级顺序匹配）
    INTENT_KEYWORDS = [
        ("应急", ["安全", "急救", "受伤", "失物", "丢失", "丢了", "投诉", "应急", "医务", "事故", "广播"]),
        ("票务", ["门票", "票价", "价格", "多少钱", "购票", "买票", "退票", "退款", "优惠", "半价", "免票", "办卡", "充值", "余额", "储值"]),
        ("营业", ["营业", "开放", "几点", "开园", "闭园", "关门", "关门时间", "时间", "节假日", "周末"]),
        ("设施", ["设施", "项目", "过山车", "木马", "秋千", "身高", "限制", "排队", "游乐", "大炮乐园", "浴室"]),
        ("规则", ["规则", "禁止", "注意", "规定", "能带", "可以带", "允许", "要求"]),
    ]

    def detect_intent(self, query: str) -> str:
        """识别用户问题意图（本地关键词规则，零 LLM 开销）"""
        for intent, keywords in self.INTENT_KEYWORDS:
            if any(k in query for k in keywords):
                return intent
        return "其他"

    # ============== 多轮改写辅助 ==============

    def _needs_llm_rewrite(self, query: str, report: dict = None) -> bool:
        """多轮场景是否需要 LLM 改写：问法库高置信命中时不需要（省一次 LLM 往返）"""
        try:
            matched, matches = self.match_question(query)
            sim = float(matches[0]["similarity"]) if matches else 0.0
            if matched and matched != query and sim >= self.PATTERN_SHORTCUT_SIMILARITY:
                if report is not None:
                    report["shortcut_similarity"] = sim
                return False
        except Exception:
            pass
        return True

    def _pattern_rewrite(self, query: str, report: dict, mode: str = "pattern_shortcut") -> str:
        """仅用常见问法库完成"改写"（不调 LLM），并写入 report"""
        matched, matches = self.match_question(query)
        final = matched if matched else query
        if report is not None:
            report.update({
                "mode": mode,
                "original": query,
                "rewritten": final,
                "matched_pattern": matches[0]["pattern"] if matches else None,
                "similarity": matches[0]["similarity"] if matches else None,
            })
        return final

    def rewrite(self, history: str, query: str, report: dict = None) -> str:
        """
        多轮对话 Query 改写（含常见问法匹配）
        流程：
        0. 如果无历史，直接用常见问法匹配
        1. 如果有历史，先做指代消解，再做常见问法匹配

        Args:
            history: 多轮历史文本
            query: 用户原始问题
            report: 可选 dict，调用方传入后写入本次改写的真实过程信息
                    （改写模式/原始问句/改写结果/命中的常见问法/相似度），
                    用于前端"思考步骤"展示。

        Returns:
            改写后的检索问句（无改写时原样返回）
        """
        query = query.strip()
        if not history or not history.strip():
            # 无历史：直接用常见问法匹配
            matched, matches = self.match_question(query)
            final = matched if matched else query
            if report is not None:
                report.update({
                    "mode": "pattern",          # 未走 LLM，仅问法库余弦匹配
                    "original": query,
                    "rewritten": final,
                    "matched_pattern": matches[0]["pattern"] if matches else None,
                    "similarity": matches[0]["similarity"] if matches else None,
                })
            return final

        from core.llm_client import llm_client
        try:
            # 延迟优化：多轮场景下若当前问句已高置信命中常见问法库
            # （余弦相似度 ≥ PATTERN_SHORTCUT_SIMILARITY），直接采用该问法检索，
            # 不再花一次 LLM 改写往返（实测 2.0~2.6s）。
            # 问法库命中的问句本身就是"去掉指代、可独立检索"的规范问法，
            # 与 LLM 改写目标一致，因此这一步是安全的。
            if not self._needs_llm_rewrite(query, report):
                return self._pattern_rewrite(query, report, mode="pattern_shortcut")

            prompt = self.REWRITE_PROMPT.format(history=history.strip(), query=query)
            result = llm_client.invoke(prompt)
            rewritten = result.strip()
            if len(rewritten) > 500:
                rewritten = query

            # 对改写后的 query 做常见问法匹配
            if rewritten and rewritten != query:
                matched, matches = self.match_question(rewritten)
                final = matched if matched else rewritten
                if report is not None:
                    report.update({
                        "mode": "llm",
                        "original": query,
                        "llm_rewrite": rewritten,
                        "rewritten": final,
                        "matched_pattern": matches[0]["pattern"] if matches else None,
                        "similarity": matches[0]["similarity"] if matches else None,
                    })
                return final

            # LLM 改写为空或与原文一致 → 无实质改写
            final = rewritten if rewritten else query
            if report is not None:
                report.update({
                    "mode": "llm_no_change",
                    "original": query,
                    "rewritten": final,
                    "matched_pattern": None,
                    "similarity": None,
                })
            return final
        except Exception as e:
            logger.warning(f"Query rewriting failed: {e}, using original query")
            matched, matches = self.match_question(query)
            final = matched if matched else query
            if report is not None:
                report.update({
                    "mode": "fallback_pattern",
                    "original": query,
                    "rewritten": final,
                    "matched_pattern": matches[0]["pattern"] if matches else None,
                    "similarity": matches[0]["similarity"] if matches else None,
                    "error": str(e)[:120],
                })
            return final

    def expand_keywords(self, query: str) -> list:
        """关键词扩展：生成同义词/相关词"""
        from core.llm_client import llm_client
        try:
            prompt = self.KEYWORD_EXPAND_PROMPT.format(query=query)
            result = llm_client.invoke(prompt)
            keywords = [k.strip() for k in result.replace("，", ",").split(",") if k.strip()]
            all_kw = list(set([query] + keywords))
            return all_kw[:6]
        except Exception:
            return [query]

    # 寒暄词表。注意这是 api/chat.py 的**前置短路**判据：一旦为 True 就直接回固定话术、
    # 不再检索，所以宁可漏判（退回正常问答）也不能把夹着真问题的句子吞掉。
    GREETING_WORDS = [
        "你好", "您好", "嗨", "哈喽", "哈啰", "hi", "hello", "在吗", "在么",
        "你是谁", "你是谁呀", "早上好", "中午好", "下午好", "晚上好",
        "谢谢", "多谢", "感谢", "再见", "拜拜", "辛苦了",
    ]
    # 这些寒暄词本身带疑问词，需单独放行（"在吗""你是谁"）
    _QUESTION_GREETINGS = ["在吗", "在么", "你是谁"]
    # 句子出现任一标记 = 里面还有真问题（"你好，营业时间是多少"）→ 不能当纯寒暄短路
    _GREETING_BLOCKERS = ["吗", "？", "?", "多少", "怎么", "为什么", "哪", "几", "什么",
                          "可以", "能不能", "有没有", "帮我", "我要", "我想", "办", "买"]

    def is_greeting(self, query: str) -> bool:
        """判断是否为纯问候/闲聊（"你好""谢谢""再见"）。

        收紧规则的原因：此前是"包含即算"，于是"谢谢，过山车多少钱"也会被当成寒暄，
        直接回一句固定问候语、真问题被吞掉。现在要求整句没有别的问题才算。
        """
        q = (query or "").strip().lower()
        if not q or len(q) > 12:      # 超过 12 字基本不可能是纯寒暄
            return False
        if not any(g in q for g in self.GREETING_WORDS):
            return False
        if any(g in q for g in self._QUESTION_GREETINGS):
            return True
        return not any(b in q for b in self._GREETING_BLOCKERS)

    def get_greeting_response(self) -> str:
        return ("你好呀！我是鸭鸭，园区的事我都能直接帮你办："
                "查余币、每日签到、买游乐项目、办卡充值、查地图和天气。"
                "直接跟我说想干什么就行，比如「帮我买两个旋转木马」，我查完余额就给你办好。")

    # ============== 充值档位咨询（规则应答） ==============

    # 问「有没有其他价格/面额」类问题的关键词
    PRICE_INQUIRY_KEYWORDS = [
        "其他价格", "别的价格", "其他面额", "别的面额", "其他档位", "别的档位",
        "其他金额", "别的金额", "还有别的", "还有其他", "只有这", "哪几种",
        "哪几档", "哪些档位", "什么档位", "有哪些价格", "有别的价",
    ]

    # 储值卡固定三档（与 cards 表 / 充值页一致）
    PRICE_RESPONSE = (
        "目前储值卡只有这三种：200元=320币、500元=1000币、1000元=2500币～\n"
        "您也可以打开微信收付款，出示给工作人员扫码直接支付；"
        "或在小程序「办卡充值」页选择游戏币充值哦～"
    )

    def is_price_inquiry(self, query: str) -> bool:
        """是否在问充值档位/价格种类（规则应答，保证话术确定性）"""
        return any(k in query for k in self.PRICE_INQUIRY_KEYWORDS)

    def get_price_response(self) -> str:
        return self.PRICE_RESPONSE


# 全局单例
query_agent = QueryAgent()
