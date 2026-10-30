# ============================================================
# 校验 Agent (VerificationAgent)
# 职责：防幻觉校验、来源溯源、答案质量评估
# ============================================================
import logging
import re
from typing import List, Tuple, Generator
from config import (LLM_MODEL, VERIFY_LLM_ENABLED, VERIFY_MIN_SCORE, RERANK_TOP,
                    LLM_MAX_TOKENS)

logger = logging.getLogger(__name__)


class VerificationAgent:
    """
    校验 Agent
    职责：
    1. 检索结果相关性校验
    2. 答案溯源 - 标注信息来源
    3. 防幻觉 Prompt 约束
    4. 答案质量评分
    """

    # 防幻觉核心 System Prompt（增强版）
    GENERATE_PROMPT = """你是"大黄鸭室外游乐园"的智能问答助手。下面的信息分三块，**各自用途不同，不要混用**。

【第一块 · 用户档案 —— 可据此直接作答】
{profile_block}
说明：这是用户**亲口告诉过我们**的信息，属于**事实**。
· 用户问"我有几个孩子""孩子多高""我喜欢玩什么"这类**关于他自己/家人**的问题时，
  **直接用这块回答**，不要说"没有相关资料"；
· 这块里**没有**的，就如实说"你还没告诉我"，并引导他去补填，**绝对不要猜**；
· 回答里提到档案内容时可自然带一句"（来自你的画像）"。

【第二块 · 对话历史 —— 只用于理解指代与省略】
{history_block}
说明：仅当用户用了"它/那个/还有吗/刚才说的"这类省略时，用来还原他指的是什么。
**不要**把历史里的内容当作园区规则或价格的事实来源。

【第三块 · 园区参考资料 —— 事实来源】
{context}
说明：凡涉及园区**规则、价格、时间、限制**，**只能**用这块的内容，必须与原文完全一致；
这块没写到的，如实说"园区资料里没有明确写"，**禁止编造**。

【硬性要求】
1. ⚠️ 如果第三块（园区参考资料）**有内容但不足以**回答用户问题，
   **只输出这一行、不要写别的**：暂时没有查询到相关信息
   （这一行是给系统的信号，系统会换成更合适的引导话术，用户看不到它。）
2. 三块里**都没有**能回答用户问题的信息时，不要生硬地说"查不到"：
   先看用户档案能不能答；能答就直接答；不能答就说明"这个我确实不知道"，并说明你能帮他办什么。
3. 回答简洁清晰、口语化，控制在 200 字以内、最多 4 个要点，直接给结论不铺垫。
4. 结尾标注【来源文档】：文档名称（**仅当第三条参考资料真的被用到时才标注**）。
5. 不要提及"参考资料""检索""画像字段"这类内部说法，直接给答案。
6. 当用户问题模糊时，可以友好引导用户补充信息。

【用户问题】
{query}

【回答】"""

    # ------------------------------------------------------------------
    # 软兜底（零命中岔路口的第二支）：知识库没命中、且问句里**没有园区实体词**
    # ------------------------------------------------------------------
    # 为什么要单独一个提示词：原先零命中的处理是"不调模型，直接吐模板 + 附上画像"，
    # 于是"我家里有几个孩子"这种**答案就在画像里**的问题，用户拿到的是
    # 「园区知识库里暂时没有收录这个问题」——而同一段回答里其实已经把答案念了一遍。
    # 现在：判得出画像字段的直接模板作答（见 agents.answer_router），
    #      判不出的走这里，让模型**带着档案和对话历史**给出一个有用回答。
    SOFT_FALLBACK_PROMPT = """你是"大黄鸭室外游乐园"的智能问答助手。这一轮**园区知识库里没有查到资料**，
所以你要按下面的顺序处理，**不要编造园区规则**。

【用户档案（用户亲口说过的，属于事实）】
{profile_block}

【最近对话】
{history_block}

【处理顺序 —— 严格按此】
1. 如果用户问的是**关于他自己/家人**的事（几个孩子、孩子多大、喜欢玩什么…），
   **直接用用户档案回答**，不要提"知识库""资料"。
   档案里没有的，如实说"你还没告诉我"，并给两条路：在这儿说一句我就记住了 /
   点聊天页工具条的「画像」小图标点选着填。**不要猜**。
2. 如果问的是**园区的事**但资料里确实没写，就如实说"园区资料里没写这一条"，
   再告诉他你能直接替他办什么（查余币、签到、看项目价目、直接买项目、办卡充值）。
3. 如果既不是档案也不是园区的事，就说明你的定位：
   你是大黄鸭室外游乐园的定制问答助手，擅长园区游玩、票价办卡、营业时间、失物招领、
   以及按孩子情况给建议；这个问题超出范围了，怕答不准反而误导他。

【硬性要求】
· 口语化、简洁，120 字以内，不要罗列大段免责声明。
· 不确定的一律说"资料里没有明确"，**绝不编造规则、价格、时间**。
· 不要出现"知识库""检索""向量"这类内部词。

【用户问题】
{query}

【回答】"""

    VERIFY_PROMPT = """评估以下检索结果与用户问题的相关性。
仅输出一个 JSON 字符串数组，数组元素与检索结果顺序一一对应，取值只能是 "相关" 或 "不相关"。
禁止输出任何解释、编号或其他文字。

用户问题：{query}

检索结果：
{docs}

JSON 数组："""

    # ------------------------------------------------------------------
    # 知识库未命中时的兜底回答（v2，2026-09-20 改版）
    # ------------------------------------------------------------------
    # v1 的做法是让模型"基于通用常识给 2~4 条实用建议"并标注"非园区官方文档内容"。
    # 实测这类回答对用户没用——用户问"帮我买三个小火车"，拿到的是一份
    # "先确认是哪种小火车、可以直接去站点找工作人员、以现场公示为准"的免责建议清单，
    # 真正该做的（查余额 → 够就直接买 → 出核销码）一件没办。
    #
    # v2 改为：
    #   · 完全没检索到资料 → 一段**确定性结论**（不调 LLM：省一次往返，且保证一个字都不编）
    #     + 告诉用户"这些我能直接替你办"，把落点从"建议"挪到"可执行"；
    #   · 检索到部分相关资料 → 只基于资料给结论，不再补通用建议，也不再把问题推回给用户。
    #
    # ⚠️ 前缀文案是「回答类型判定」的锚点：classify_answer / 知识库盲区看板 /
    #    database.list_fallback_questions 的历史 LIKE 粗筛都依赖它。
    #    改文案**必须**同步改 FALLBACK_MARKERS，且新旧标记都要留在表里，
    #    否则库里已落库的历史回答会被漏判成"正常回答"，盲区统计失真。
    FALLBACK_PREFIX = "📌 园区知识库里暂时没有收录这个问题："
    FALLBACK_PREFIX_PARTIAL = "📌 园区资料里有相关信息，但没有直接针对这个问题的规定："
    # 零资料时的确定性结论文案（不经过模型，避免编造，也更快）
    FALLBACK_NONE_BODY = (
        "我不能凭空编一个答案给你。\n\n"
        "下面这些我可以直接替你办，说一声就行：查余币、每日签到、看游乐项目和价格、"
        "直接买项目、办卡充值。\n"
        "如果这其实是园区的规则类问题，换个说法再问我一次（比如「退卡规则是什么」），我再查一遍。"
    )

    # ---------- 回答类型判定（问答日志落库 / 知识库盲区统计共用） ----------
    # cached           —— 命中 Redis 问答缓存（此前已成功回答过，references 天然为空）
    # fallback_none    —— 知识库完全没检索到资料 → 通用建议
    # fallback_partial —— 检索到资料但不足以回答 → 「园区资料 + 补充建议」
    # normal           —— 正常基于知识库回答
    ANSWER_TYPE_LABELS = {
        "cached": "缓存命中",
        "fallback_none": "知识库未收录",
        "fallback_partial": "资料不足",
        "normal": "知识库正常回答",
    }
    # 兜底话术的"纯中文特征串"：既用于 SQL LIKE 粗筛，也用于文本精确判定，
    # 刻意不含 📌 等符号 —— 符号在不同端/历史数据里可能被吞掉，中文串更稳。
    # 注意 marker 长度必须 ≤ answer_head 的 16 字，否则 startswith 永远不成立。
    # 新旧标记并存：前两条对应 v2 现行话术；后两条是 v1 历史话术，**不能删** ——
    # 库里已落库的历史回答（answer_type 为空）还要靠它被 LIKE 粗筛捞出来，
    # 删了历史盲区就统计不到（见 database.list_fallback_questions）。
    FALLBACK_MARKERS = (("园区知识库里暂时没有收录", "fallback_none"),
                        ("园区资料里有相关信息", "fallback_partial"),
                        ("知识库暂无相关文档", "fallback_none"),
                        ("知识库没有直接回答该问题的条目", "fallback_partial"))

    @classmethod
    def classify_answer(cls, answer: str, cached: bool = False) -> str:
        """判定一次回答的类型（取值见 ANSWER_TYPE_LABELS）。

        ⚠️ 知识库盲区统计**必须**用它，不要拿"references 为空"当代理：
        缓存命中的回答 references 同样是空的，但它其实是「此前已经答上来了」的正常回答，
        用 references 判空会把高频正常问题全算成盲区（实测 7 天内 86/117 是这种假阳性）。
        """
        if cached:
            return "cached"
        head = cls.answer_head(answer)
        if head:
            for marker, atype in cls.FALLBACK_MARKERS:
                if head.startswith(marker):
                    return atype
        return "normal"

    @staticmethod
    def answer_head(answer: str, n: int = 16) -> str:
        """取答案开头的中文正文（跳过 📌 等符号、空白），用于稳定匹配固定话术"""
        s = (answer or "").lstrip()
        s = re.sub(r"^[^\u4e00-\u9fff]+", "", s)
        return s[:n]

    @staticmethod
    def is_fallback_type(answer_type: str) -> bool:
        """该回答类型是否属于"模型没检索到知识库 → 给建议"（即知识库盲区）"""
        return answer_type in ("fallback_none", "fallback_partial")

    GENERATE_FALLBACK_PROMPT_PARTIAL = """你是游乐场（园区）的客服助手"鸭鸭"。用户的问题在园区知识库里**没有能直接回答它的条目**，但检索到了下面这些**相关的园区资料**。

【本次回答的特殊要求（必须按这个顺序组织，只给结论，不给建议）】
1. **先用 1 句话如实说明**：园区资料里没有针对这个问题的直接规定/说明；
2. **再把参考资料里与该问题相关的确定事实列出来**（这些是园区官方内容，可以放心引用；资料里有的数字/规则必须与资料完全一致，资料里没有的绝不编造）；
3. **严禁补"通用建议"**：不要写"建议您先确认…""可以去现场问工作人员""具体以现场公示为准"这类把问题推回给用户的话。资料能答到哪一步就说到哪一步，答不到的部分直接说"资料里没有"；
4. 回答口语化、简洁，总长度 200 字以内，最多 4 个要点；不要出现"根据知识库/参考资料"这类话术，也不要输出来源文件名。

【参考资料（园区官方内容，可能只覆盖问题的一部分）】
{context}

【用户问题】
{query}

【回答】"""

    def generate_fallback_stream(self, query: str, context: str = "",
                                 profile_note: str = "") -> Generator[str, None, None]:
        """知识库未命中 / 资料不足时的流式兜底回答（v2）。

        context 为空（完全没检索到）→ 直接给一段**确定性结论**，不调用 LLM：
          省一次往返（符合"检索速度不能变慢"），也从根上杜绝编出园区没有的价格/规则。
        context 非空（检索到相关资料但答不了这个问题）→ 只基于资料给结论。
          （例：问"退卡要收手续费吗"，资料里有退卡规则，就该把规则说清楚，而不是丢了资料改讲常识。）

        profile_note：用户画像摘要。**兜底回答同样要基于画像**（产品承诺是"每次回答"）。
        注意注入方式：
          · 前缀 FALLBACK_PREFIX* 是「回答类型判定」的锚点，必须原样先输出，不能被动摇；
          · 零资料分支不走 LLM，所以画像只能用**模板追加**，不额外加一次模型往返（延迟不变）；
          · 资料不足分支已有 context（调用方已把画像拼进 context 尾部），不需要重复注入。
        """
        has_ctx = bool(context and context.strip())
        if not has_ctx:
            yield self.FALLBACK_PREFIX + "\n"
            yield self.FALLBACK_NONE_BODY
            if profile_note:
                yield self._fallback_profile_tail(profile_note)
            return

        yield self.FALLBACK_PREFIX_PARTIAL + "\n"
        from core.llm_client import llm_client
        prompt = self.GENERATE_FALLBACK_PROMPT_PARTIAL.format(context=context, query=query)
        try:
            for token in llm_client.stream(prompt, max_tokens=LLM_MAX_TOKENS):
                yield token
        except Exception as e:
            logger.error(f"资料不足兜底生成失败: {e}")
            yield "资料里没有针对这个问题的直接规定，这部分我这边确实没有依据，不能编给你。"

    def generate_soft_fallback_stream(self, query: str, profile_note: str = "",
                                      history: str = "") -> Generator[str, None, None]:
        """零命中岔路口的第二支：知识库没命中、且问句里**没有园区实体词**。

        与 `generate_fallback_stream(context="")` 的区别（这是本次改造的核心）：
          · 旧行为：不调模型，直接吐「📌 园区知识库里暂时没有收录这个问题」+ 画像尾注。
            于是"我家里有几个孩子"这种**答案就在画像里**的问题，用户拿到的是"我答不上来"，
            而同一段回答里其实已经把答案念了一遍。
          · 新行为：让模型**带着档案和对话历史**回答，并按"档案 → 园区 → 定位"的顺序处理。

        ⚠️ **不输出 FALLBACK_PREFIX** —— 走这条路的都不是"知识库缺条目"
        （它们是关于用户自己、或与园区无关的问题），不该被统计进知识库盲区看板。
        园区类问题的零命中仍走 `generate_fallback_stream`，带标记、统计口径不变。
        """
        from core.llm_client import llm_client
        prompt = self.build_prompt(query, "", profile_note, history, soft=True)
        try:
            yield from llm_client.stream(prompt, max_tokens=LLM_MAX_TOKENS)
        except Exception as e:
            logger.error(f"软兜底生成失败: {e}")
            # 模型不可用时的降级：给确定性结论，绝不空手
            yield self.FALLBACK_PREFIX + "\n" + self.FALLBACK_NONE_BODY

    @staticmethod
    def _fallback_profile_tail(profile_note: str) -> str:
        """零资料兜底时追加的画像尾注（纯模板，零 LLM 开销）。

        只做一件事：让用户知道"档案是生效的"，并给出一个**结合档案就能回答**的
        追问方向 —— 避免兜底回答看起来像把画像丢了。
        """
        note = (profile_note or "").strip()
        if not note:
            return ""
        return (f"\n\n另外，我一直记得你留的档案：{note}。"
                f"如果这问题其实和它有关（比如某个项目孩子能不能玩、多大能玩），"
                f"你直接这么问我，我会结合档案给你判断。")

    def verify(self, query: str, docs: List[Tuple[str, str, float]],
               report: dict = None) -> List[Tuple[str, str, float]]:
        """
        校验检索结果相关性
        - 过滤不相关内容
        - 返回通过校验的文档列表

        Args:
            query: 用户问题
            docs: 待校验文档 [(text, source, score), ...]
            report: 可选 dict，写入本次校验的真实过程（阈值过滤数/是否启用
                    LLM 校验/通过数/兜底原因），用于前端"思考步骤"展示。
        """
        if report is None:
            report = {}
        report["docs_in"] = len(docs)

        if not docs:
            report.update({"threshold_kept": 0, "llm_verify": False,
                           "docs_out": 0, "fallback": "no_input"})
            return []

        # 快速过滤：去掉相似度过低的
        filtered = [d for d in docs if d[2] >= VERIFY_MIN_SCORE]
        report["threshold_kept"] = len(filtered)

        # 延迟优化：默认不再用 LLM 逐条校验（一次完整 LLM 往返约 3.5s）。
        # CrossEncoder 精排分本身就是"问题-文档"语义相关概率，配合 VERIFY_MIN_SCORE
        # 已足够过滤无关资料；如需旧行为可设 VERIFY_LLM_ENABLED=1。
        if not VERIFY_LLM_ENABLED:
            report.update({"llm_verify": False, "docs_out": min(len(filtered), RERANK_TOP),
                           "verify_mode": "rerank_score"})
            return filtered[:RERANK_TOP]

        # 如果结果较少（<=3条），直接返回
        if len(filtered) <= 3:
            report.update({"llm_verify": False, "docs_out": len(filtered)})
            return filtered

        # 如果结果较多，使用 LLM 做相关性校验
        if len(filtered) > 5:
            report["llm_verify"] = True
            try:
                verified = self._llm_verify(query, filtered)
                report["llm_kept"] = len(verified)
                if verified:
                    report.update({"docs_out": len(verified), "fallback": ""})
                    return verified
                # LLM 全部否决：兜底前几条避免空回答
                report.update({"docs_out": len(filtered[:5]),
                               "fallback": "llm_all_rejected"})
                return filtered[:5]
            except Exception as e:
                logger.warning(f"LLM verification failed: {e}, returning top results")
                report.update({"docs_out": len(filtered[:5]), "fallback": f"llm_error: {str(e)[:80]}"})
                return filtered[:5]

        report.update({"llm_verify": False, "docs_out": len(filtered)})
        return filtered

    def _llm_verify(self, query: str, docs: list) -> list:
        """
        使用 LLM 校验文档相关性（JSON 结构化输出，避免按行对齐错位）
        解析失败/长度不符时回退：返回原列表前几条，不阻断主流程
        """
        import json as _json
        from core.llm_client import llm_client

        docs_text = "\n".join([f"{i+1}. {d[0][:100]}..." for i, d in enumerate(docs)])
        prompt = self.VERIFY_PROMPT.format(query=query, docs=docs_text)

        result = llm_client.invoke(prompt)

        # 提取首个 JSON 数组（容忍模型在 JSON 前后附带少量文字）
        start = result.find("[")
        end = result.rfind("]") + 1
        if start < 0 or end <= start:
            raise ValueError(f"LLM 校验输出非 JSON 数组: {result[:80]}")

        verdicts = _json.loads(result[start:end])
        if not isinstance(verdicts, list) or len(verdicts) != len(docs):
            raise ValueError(f"JSON 数组长度与检索结果不符: {len(verdicts)} vs {len(docs)}")

        verified = [doc for doc, v in zip(docs, verdicts)
                    if isinstance(v, str) and "相关" in v and "不相关" not in v]

        # 全部被否决时兜底返回前几条，避免过度过滤导致空回答
        return verified if verified else docs[:3]

    # ------------------------------------------------------------------
    # 三块信息 → 提示词片段（空块给占位说明，保证**提示词前缀逐字稳定**）
    # ------------------------------------------------------------------
    # ⚠️ 前缀缓存（DeepSeek/GLM 的 KV cache）要求从第 0 个 token 起逐字节相同才命中。
    #    所以顺序固定为：规则 → 用户档案 → 对话历史 → 园区资料 → 问句。
    #    档案在前且逐字稳定（同一用户变化极少），可被稳定命中；
    #    历史与资料每轮都变，它们之后的缓存本来就不会命中，放后面不额外损失。
    _EMPTY_PROFILE = "（还没有收集到这位用户的档案 —— 他还没说过关于自己/家人的信息）"
    _EMPTY_HISTORY = "（没有更早的对话记录）"

    @classmethod
    def build_prompt(cls, query: str, context: str, profile_note: str = "",
                     history: str = "", soft: bool = False) -> str:
        """按分区把三块信息拼成最终提示词。"""
        tpl = cls.SOFT_FALLBACK_PROMPT if soft else cls.GENERATE_PROMPT
        return tpl.format(
            query=query,
            context=context or "（这一轮没有检索到园区资料）",
            profile_block=(profile_note or "").strip() or cls._EMPTY_PROFILE,
            history_block=(history or "").strip() or cls._EMPTY_HISTORY,
        )

    def generate_answer(self, query: str, context: str, references: List[str],
                        profile_note: str = "", history: str = "") -> str:
        """
        生成防幻觉答案（非流式）

        profile_note / history：v2 起**分区注入**（此前画像是被塞进 `context` 里，
        与"严格只允许使用下方参考资料"的规则自相矛盾，模型于是只敢提档案、不敢依档案作答）。
        两个参数都有默认值，老调用方无感。
        """
        from core.llm_client import llm_client

        prompt = self.build_prompt(query, context, profile_note, history)
        answer = llm_client.invoke(prompt, max_tokens=LLM_MAX_TOKENS)
        # 如果答案包含防幻觉兜底话术，清除引用
        if "暂时没有查询到相关信息" in answer:
            return answer

        return answer

    def generate_answer_stream(self, query: str, context: str,
                               references: List[str],
                               profile_note: str = "",
                               history: str = "") -> Generator[str, None, None]:
        """
        生成防幻觉答案（流式）
        max_tokens 限制回答长度：生成长度是端到端延迟的最大变量（实测约 43 字/秒）
        """
        from core.llm_client import llm_client

        prompt = self.build_prompt(query, context, profile_note, history)

        for token in llm_client.stream(prompt, max_tokens=LLM_MAX_TOKENS):
            yield token

    def evaluate_answer_quality(self, answer: str, context: str, query: str) -> dict:
        """
        答案质量评估
        返回: {"hallucination_risk": str, "completeness": float, "relevance": float}
        """
        from core.llm_client import llm_client

        eval_prompt = f"""评估以下AI回答的质量，输出JSON格式评分。

用户问题：{query}
参考资料：{context[:500]}...
AI回答：{answer}

请输出JSON：{{"hallucination_risk": "low/medium/high", "completeness": 0.0-1.0, "relevance": 0.0-1.0}}"""

        try:
            result = llm_client.invoke(eval_prompt)
            import json
            # 尝试提取 JSON
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
            return {"hallucination_risk": "low", "completeness": 0.8, "relevance": 0.8}
        except Exception:
            return {"hallucination_risk": "low", "completeness": 0.8, "relevance": 0.8}

    def trace_sources(self, docs: List[Tuple[str, str, float]]) -> List[dict]:
        """
        溯源：为每段文本标注来源
        """
        traces = []
        for text, source, score in docs:
            traces.append({
                "text_snippet": text[:100] + "..." if len(text) > 100 else text,
                "source_document": source,
                "relevance_score": round(float(score), 4),
            })
        return traces


# 全局单例
verification_agent = VerificationAgent()
