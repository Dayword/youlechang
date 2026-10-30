# ============================================================
# 知识库智能体 (KbCuratorAgent)
# ------------------------------------------------------------
# 职责：把"知识库没答上来的用户问题"变成知识库资产。四步闭环：
#   1) 统计盲区 —— question_logs 中"回答类型 = 兜底建议"的记录
#      （模型没检索到资料 → 📌 通用建议；资料不足 → 📌 资料 + 补充建议）
#      ⚠️ 绝不能用 "references_json 为空" 当判据：缓存命中/地图/业务类回答的
#      references 同样是空的，但它们都已经答上来了（实测 7 天内 86/117 是假阳性）。
#   2) 生成标准答案 —— 基于知识库现有资料，用 LLM 产出 ≤N token 的防幻觉答案
#   3) 导出 Word —— 每题一个以 "## " 开头的标题块，管理员可直接在 Word 里改
#   4) 回传入库 —— 解析 → 切片 → Milvus + BM25 → 清缓存 → 重载问法库（立即供检索）
#
# 对外统一用「同步生成器 yield 事件」的形式，由 api/kb_agent.py 包成 SSE，
# 商家端即可实时看到 统计 → 生成 → 导出 的每一步进度。
# ============================================================
import os
import re
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Generator, List, Optional, Tuple

from config import (KB_AGENT_OUT_DIR, KB_AGENT_DAYS, KB_AGENT_LIMIT,
                    KB_AGENT_ANSWER_TOKENS, KB_AGENT_CTX_TOP, KB_AGENT_WORKERS,
                    KB_AGENT_MAX_RAW, KB_GAP_COVER_CHECK, KB_GAP_COVER_BATCHES)

logger = logging.getLogger(__name__)

# 智能体生成文档的统一前缀（文件名以此开头 = 由知识库智能体产出，便于识别与二次更新）
KB_CURATOR_PREFIX = "知识库补充标准答案"

# 自动剔除的问题类型（它们不是"知识库该补的洞"）：
#   greeting      —— 寒暄（"你好""在吗"），走规则应答即可
#   price_inquiry —— 充值档位咨询，已有确定性话术
#   map           —— 位置/导航类，走地图 Agent
#   personal_data —— 个人数据类（签到、我的消费/订单统计），必须登录后按账号回答，不该进知识库
_PERSONAL_RE = re.compile(
    r"^(签到|签退|打卡)$"
    r"|(我的|我|本人|本月|这个月|上月|上个月)[^，。]{0,12}"
    r"(签到|消费|订单|余额|记录|统计|花了|玩了|积分|等级|收藏|剩多少|账单)"
)

# token 计数（tiktoken cl100k_base 与切片口径一致；不可用时按字符保守估算）
_TOKENIZER = None
_TOKENIZER_TRIED = False


def _tokenizer():
    global _TOKENIZER, _TOKENIZER_TRIED
    if not _TOKENIZER_TRIED:
        _TOKENIZER_TRIED = True
        try:
            import tiktoken
            _TOKENIZER = tiktoken.get_encoding("cl100k_base")
        except Exception as e:      # pragma: no cover
            logger.warning(f"tiktoken 不可用，改用字符估算 token: {e}")
            _TOKENIZER = None
    return _TOKENIZER


def count_tokens(text: str) -> int:
    """统计 token 数（与切片使用同一编码 cl100k_base）"""
    text = text or ""
    enc = _tokenizer()
    if enc is None:
        return int(len(text) / 1.6) + 1
    try:
        return len(enc.encode(text))
    except Exception:               # pragma: no cover
        return int(len(text) / 1.6) + 1


def enforce_token_limit(text: str, limit: int) -> Tuple[str, bool]:
    """把文本压到 limit token 以内（按中文句读边界截断）。返回 (文本, 是否被截断)"""
    text = (text or "").strip()
    if not text:
        return "", False
    if count_tokens(text) <= limit:
        return text, False

    pieces = [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p.strip()]
    kept = ""
    for p in pieces:
        if count_tokens(kept + p) > max(1, limit - 1):
            break
        kept += p
    kept = kept.strip()
    if not kept:
        kept = text[: max(20, int(limit * 1.2))].strip()
    guard = 0
    while kept and count_tokens(kept) > limit and guard < 200:
        kept = kept[:-5].strip()
        guard += 1
    return (kept + "…") if kept else text[:60], True


# ============================================================
# 1. 统计"知识库没答上来的问题"
# ============================================================
# 问题归一化 / 问题级去重键统一由 services/kb_dedup.py 提供：
# 生成侧闸门、入库侧闸门、检索侧兜底必须用**同一套口径**，否则会互相漏判。
# 这里保留同名导入，兼容既有调用方。
from services.kb_dedup import (          # noqa: E402
    coverage_batch, coverage_many, dedup_key, is_primary_source,
    normalize_question, question_of,
)


def _type_label(ftype: str) -> str:
    """回答类型的中文名（取 VerificationAgent.ANSWER_TYPE_LABELS，单一真源）"""
    try:
        from agents.verification_agent import verification_agent
        return verification_agent.ANSWER_TYPE_LABELS.get(ftype, ftype or "未知")
    except Exception:                           # pragma: no cover
        return {"fallback_none": "无资料建议",
                "fallback_partial": "资料不足建议"}.get(ftype, ftype or "未知")


def _skip_reason(question: str) -> Optional[str]:
    """判断问题是否不属于"知识库该补的洞"，返回原因（None=保留）"""
    q = (question or "").strip()
    if not q:
        return "empty"
    if len(q) < 2:
        return "too_short"

    if _PERSONAL_RE.search(q):
        return "personal_data"

    try:
        from agents.query_agent import query_agent
        if query_agent.is_greeting(q):
            return "greeting"
        if query_agent.is_price_inquiry(q):
            return "price_inquiry"
    except Exception as e:                      # pragma: no cover
        logger.debug(f"query_agent 分类跳过: {e}")

    try:
        from agents.map_agent import map_agent
        if map_agent.is_map_question(q):
            return "map"
    except Exception as e:                      # pragma: no cover
        logger.debug(f"map_agent 分类跳过: {e}")

    return None


def _drop_covered(items: List[Dict], limit: int) -> Tuple[List[Dict], List[Dict]]:
    """生成侧去重闸门：剔除「现有知识库已经答得上」的问题。

    为什么必须有这道闸门：兜底记录是 **历史日志**，一旦产生就永久存在。
    不加闸门时，只要某个问题历史上出现过兜底回答，它**每跑一次智能体就会被重新生成一遍**，
    同一个问题于是在库里堆出多版措辞不同的答案 —— 这正是"模型对同一问题每次答得不一样"
    的根源（实测两轮生成有 4 个问题完全重复，「蹦床」一个问题堆到 4 条）。

    判定用**全库**覆盖（原始文档 + 已有补充文档都在库里）：
      * 原始文档已覆盖 → 该补的洞其实不存在，不该生成（用户口径："以原始文档为主"）
      * 已有补充文档覆盖 → 上一轮已经补过，重复生成只会再多一版答案

    Returns: (保留的候选, 被判为"已覆盖"的候选)
    """
    if not items or KB_GAP_COVER_CHECK <= 0:
        return items[:limit], []

    covered: List[Dict] = []
    survivors: List[Dict] = []
    idx = 0
    max_scan = max(1, int(KB_GAP_COVER_BATCHES)) * max(1, limit)

    # 分批扫描：本批被全部剔除时用后面的候选补齐，尽量凑够 limit 个真盲区
    while idx < len(items) and len(survivors) < limit and idx < max_scan:
        batch = items[idx: idx + limit]
        if not batch:
            break
        idx += len(batch)
        try:
            res = coverage_many([i["question"] for i in batch],
                                workers=KB_AGENT_WORKERS)
        except Exception as e:
            logger.warning(f"[kb_agent] 覆盖判定失败，本批不做过滤: {e}")
            survivors.extend(batch)
            continue
        for it in batch:
            r = res.get(it["question"]) or {}
            if r.get("hit"):
                src = r.get("source", "") or ""
                covered.append({
                    "question": it["question"],
                    "count": it["count"],
                    "score": r.get("score", 0.0),
                    "source": src,
                    # 区分两种"已覆盖"，前端提示与管理复盘时更好懂
                    "scope": "原始文档" if is_primary_source(src) else "已有补充文档",
                    "matched": r.get("matched", ""),
                    "by": r.get("by", ""),
                })
            else:
                survivors.append(it)

    return survivors[:limit], covered


def collect_gaps(days: int = None, limit: int = None) -> Dict:
    """统计「模型没有检索到知识库 → 给建议」的问题（归一化去重 + 按频次排序）。

    判据是**回答类型**（见 VerificationAgent.classify_answer），不是 references 是否为空：
    缓存命中 / 地图 / 业务类回答的 references 同样是空的，但它们都已经答上来了，
    用 references 判空会把"怎么办卡充值""营业时间"这类高频正常问题全算成盲区
    （实测 7 天内 117 条里有 86 条是这种假阳性）。

    Returns:
        {
          "raw_total": 兜底命中记录数, "skipped": {原因: 条数}, "skipped_total": n,
          "distinct_total": 去重后问题总数, "by_type": {fallback_none: n, fallback_partial: m},
          "items":[{question, key, count, fallback_type, answer_head, last_seen, first_seen}]
        }
    """
    from database.db import list_fallback_questions
    from agents.verification_agent import verification_agent

    days = KB_AGENT_DAYS if days is None else max(0, int(days))
    limit = KB_AGENT_LIMIT if limit is None else max(1, int(limit))

    rows = list_fallback_questions(days=days, limit=KB_AGENT_MAX_RAW) or []
    skipped: Dict[str, int] = {}
    by_type: Dict[str, int] = {}
    grouped: Dict[str, Dict] = {}

    for r in rows:                              # 已按 id 倒序（最新在前）
        q = (r.get("question") or "").strip()
        # 回答类型：新记录直接取 answer_type 列；历史记录用答案开头的兜底话术兜底识别
        ftype = (r.get("answer_type") or "").strip()
        if not verification_agent.is_fallback_type(ftype):
            ftype = verification_agent.classify_answer(r.get("answer") or "")
        if not verification_agent.is_fallback_type(ftype):
            # 双保险：数据层已按话术粗筛，这里再精确判定一次，判不出来就不算盲区
            continue
        by_type[ftype] = by_type.get(ftype, 0) + 1

        reason = _skip_reason(q)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        key = normalize_question(q)
        if not key:
            skipped["empty"] = skipped.get("empty", 0) + 1
            continue
        ts = str(r.get("created_at") or "")
        head = verification_agent.answer_head(r.get("answer") or "", 40)
        item = grouped.get(key)
        if item is None:
            grouped[key] = {
                "question": q,
                "key": key,
                "count": 1,
                "fallback_type": ftype,
                "answer_head": head,
                "first_seen": ts,
                "last_seen": ts,
            }
        else:
            item["count"] += 1
            item["first_seen"] = ts          # 倒序遍历 → 最后写入的是最早时间
            # 保留更简短规范的问法作为展示问句
            if len(q) < len(item["question"]):
                item["question"] = q

    items = sorted(grouped.values(), key=lambda x: (-x["count"], x["last_seen"]))
    distinct_total = len(items)

    # ---- 生成侧去重闸门：剔除"现有知识库已经答得上"的问题 ----
    items, covered = _drop_covered(items, limit)
    if covered:
        skipped["原始文档已覆盖"] = len(covered)

    return {
        "raw_total": sum(by_type.values()),
        "skipped": skipped,
        "skipped_total": sum(skipped.values()),
        "distinct_total": distinct_total,
        "by_type": by_type,
        "items": items,
        "covered": covered,
    }


# ============================================================
# 2. 基于知识库生成标准答案
# ============================================================
ANSWER_PROMPT = """你是大黄鸭游乐园的官方知识库编辑。请为下面这个问题产出一条**可直接放进知识库的标准答案**。

【硬性要求】
1. **只允许使用「现有资料」里的事实**：价格、时间、数量、规则必须与资料完全一致，严禁编造、严禁推测；
2. 「现有资料」不足以回答时：如实写"知识库暂无相关条目"，再列出需要园区人工确认的要点（**不要编造任何具体数字**），最后一行输出 `需人工补充：是`；
3. 「现有资料」足以回答时：最后一行输出 `需人工补充：否`；
4. 答案口语化、简洁，先给结论再补必要的说明，控制在 {max_tokens} token 以内（约 200 字）；
5. 不要出现"根据资料/参考资料/文档中"这类话术，也不要输出来源文件名；
6. 第一行直接是答案正文，不要写"答案："之类的前缀。

【用户问题】
{question}

【现有资料】
{context}

【标准答案】"""


class KbCuratorAgent:
    """知识库智能体：盲区统计 → 标准答案 → Word 导出 → 回传入库"""

    # ---------- 2.1 检索知识库现有资料 ----------
    @staticmethod
    def build_context(question: str, top_k: int = None) -> Tuple[str, List[str]]:
        """检索知识库现有资料作为生成依据。

        这里刻意用 rerank=False（融合候选，不做 CrossEncoder 精排）：
        本步骤是"给 LLM 提供素材"，精排既慢又是 CPU 瓶颈；是否真的能回答问题交给
        下面的防幻觉 Prompt 判断（资料不足就明确要求模型写"需人工补充"）。
        """
        top_k = top_k or KB_AGENT_CTX_TOP
        try:
            from agents.retrieval_agent import retrieval_agent
            docs = retrieval_agent.search(question, top_k=top_k,
                                          use_cache=True, rerank=False) or []
        except Exception as e:
            logger.warning(f"[kb_agent] 检索失败（按无资料处理）: {e}")
            return "", []

        docs = docs[:top_k]
        if not docs:
            return "", []
        context = "\n---\n".join(t for t, _s, _sc in docs if t)
        sources: List[str] = []
        for _t, s, _sc in docs:
            if s and s not in sources:
                sources.append(s)
        return context, sources

    # ---------- 2.2 生成单条标准答案 ----------
    def generate_one(self, question: str, max_tokens: int = None) -> Dict:
        """为单个问题生成标准答案（含依据、是否需人工补充）"""
        from core.llm_client import llm_client

        max_tokens = max_tokens or KB_AGENT_ANSWER_TOKENS
        context, sources = self.build_context(question)
        if not context:
            # 知识库确实一点相关资料都没有：不调用 LLM，直接给出待补充骨架
            return {
                "question": question,
                "answer": "知识库暂无相关条目，需要园区补充该项规则/信息后再入库。",
                "sources": [],
                "needs_review": True,
                "has_context": False,
                "tokens": 0,
                "truncated": False,
            }

        prompt = ANSWER_PROMPT.format(question=question,
                                      context=context[:4000],
                                      max_tokens=max_tokens)
        try:
            raw = llm_client.invoke(prompt, max_tokens=max(max_tokens, 64)) or ""
        except Exception as e:
            logger.error(f"[kb_agent] 生成标准答案失败: {e}")
            raw = f"（生成失败，请人工补充：{str(e)[:60]}）"

        answer, needs_review = self._parse_answer(raw)
        answer, truncated = enforce_token_limit(answer, max_tokens)
        return {
            "question": question,
            "answer": answer,
            "sources": sources,
            "needs_review": needs_review,
            "has_context": True,
            "tokens": count_tokens(answer),
            "truncated": truncated,
        }

    @staticmethod
    def _parse_answer(raw: str) -> Tuple[str, bool]:
        """从模型输出里剥离 `需人工补充：是/否` 标记。

        ⚠️ 必须同时处理**行首**和**行内**两种形态：实测模型经常把标记写在答案正文的
        同一行末尾（"…只能继续在店里使用。需人工补充：否"），只按行首匹配会漏掉，
        标记就会作为"标准答案"的一部分一路入库、检索、答给用户。
        """
        text = (raw or "").strip()
        needs_review = False
        full_re = re.compile(r"^\s*需人工补充\s*[:：]?\s*(是|否|yes|no)?\s*$", re.I)
        inline_re = re.compile(r"需人工补充\s*[:：]?\s*(是|否|yes|no)?", re.I)

        lines = []
        for line in text.splitlines():
            if full_re.match(line):
                m = re.search(r"(是|否|yes|no)", line, re.I)
                needs_review = True if m is None else m.group(1).lower() in ("是", "yes")
                continue
            if inline_re.search(line):
                m = inline_re.search(line)
                needs_review = True if (m is None or not m.group(1)) \
                    else m.group(1).lower() in ("是", "yes")
                line = inline_re.sub("", line)
            line = re.sub(r"^\s*(标准答案|答案)\s*[:：]\s*", "", line).rstrip()
            lines.append(line)

        cleaned = "\n".join(lines).strip()
        if not cleaned:
            cleaned = inline_re.sub("", text).strip()
        # 模型把"无资料"写进正文（未按格式给标记）时也判定为需人工补充
        if not needs_review and any(k in cleaned for k in ("知识库暂无相关条目", "需人工补充")):
            needs_review = True
        return cleaned, needs_review

    # ---------- 3. 导出可编辑 Word ----------
    @staticmethod
    def _docx_fonts(doc) -> None:
        """设置中文字体（微软雅黑），避免 Word 里中文回退成乱字体"""
        try:
            from docx.oxml.ns import qn
            from docx.shared import Pt
            normal = doc.styles["Normal"]
            normal.font.name = "微软雅黑"
            normal.font.size = Pt(10.5)
            rpr = normal.element.get_or_add_rPr()
            rf = rpr.get_or_add_rFonts()
            rf.set(qn("w:eastAsia"), "微软雅黑")
        except Exception as e:                  # pragma: no cover
            logger.debug(f"设置中文字体失败（忽略）: {e}")

    def build_docx(self, items: List[Dict], stats: Dict,
                   max_tokens: int = None, title_suffix: str = "") -> Dict:
        """把问答条目导出成可编辑的 Word 文档。

        关键约定：每个问题写成一行以 `## ` 开头的标题段落。
        这样管理员回传后，DocumentAgent 的 Markdown 标题切块能按"一题一块"切片，
        而不是把整份文档按 512 token 硬切、把不同问题的答案混在一起。
        """
        from docx import Document
        from docx.shared import Pt, RGBColor

        max_tokens = max_tokens or KB_AGENT_ANSWER_TOKENS
        os.makedirs(KB_AGENT_OUT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{KB_CURATOR_PREFIX}_{stamp}{title_suffix}.docx"
        path = os.path.join(KB_AGENT_OUT_DIR, filename)

        doc = Document()
        self._docx_fonts(doc)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        rng = "不限时间" if not stats.get("days") else f"近 {stats.get('days')} 天"
        covered = sum(i.get("count", 0) for i in items)

        doc.add_heading("知识库智能体 · 补充标准答案（待管理员确认）", level=0)
        doc.add_paragraph(
            f"生成时间：{now}    统计范围：{rng}\n"
            f"统计口径：只统计「模型没检索到知识库 → 给出建议」的兜底回答（不含缓存命中/业务类正常回答）\n"
            f"兜底记录：{stats.get('raw_total', 0)} 条    归一化为 {stats.get('distinct_total', 0)} 个问题"
            f"    本次处理 {len(items)} 个    合计被提问 {covered} 次"
        )
        doc.add_paragraph("【使用说明】")
        for line in (
            "1. 每个问题都是一行以 “## ” 开头的标题（Word 里显示为二级标题），"
            "请勿删除或修改 “## ” 与问题文字，否则会影响回传入库时的切片；",
            f"2. 直接修改每个问题下方的「标准答案：」内容即可，建议每条 ≤ {max_tokens} token（约 200 字）；",
            "3. 修改完保存为 .docx，回到商家端「知识库智能体」把文件发回，"
            "智能体会自动解析 → 切片 → 写入向量库与关键词索引，立即供检索使用；",
            "4. 「知识库依据」与「管理员修改区」仅供人工参考，可保留可删除。",
        ):
            doc.add_paragraph(line)

        for idx, it in enumerate(items, 1):
            q = it.get("question") or ""
            ans = (it.get("answer") or "").strip()
            srcs = it.get("sources") or []
            doc.add_heading(f"## Q{idx}. {q}", level=2)

            # 原始回答类型：让管理员一眼看出"当初是怎么没答上来的"
            if it.get("fallback_type"):
                doc.add_paragraph(f"原始回答类型：{_type_label(it.get('fallback_type'))}"
                                  f"（被提问 {it.get('count', 1)} 次）")

            p = doc.add_paragraph()
            run = p.add_run("标准答案：")
            run.bold = True
            p.add_run(ans or "（待补充）")

            # 正名：这是"生成答案时现查到的资料"，不是"原回答的依据"
            src_text = "、".join(srcs) if srcs else "（检索后仍无可用资料，需园区人工补充）"
            doc.add_paragraph(f"补答依据（生成时从当前知识库检索到的资料）：{src_text}")

            if it.get("needs_review"):
                r = doc.add_paragraph().add_run("⚠ 本条知识库资料不足，需园区人工确认后补全。")
                r.font.color.rgb = RGBColor(0xC0, 0x39, 0x2B)
                r.font.size = Pt(9)

            doc.add_paragraph("管理员修改区（可在下方直接补充说明）：")
            doc.add_paragraph("")

        try:
            doc.core_properties.title = "知识库补充标准答案"
            doc.core_properties.comments = f"由知识库智能体生成于 {now}，共 {len(items)} 条"
        except Exception:                       # pragma: no cover
            pass

        doc.save(path)
        size = os.path.getsize(path)
        logger.info(f"[kb_agent] Word 已生成: {path} ({size} bytes, {len(items)} 条)")
        return {
            "name": filename,
            "path": path,
            "url": f"/static/kb_agent/{filename}",
            "size": size,
            "items": len(items),
        }

    # ---------- 4. 回传文档 → 解析/切片/入库 ----------
    @staticmethod
    def strip_preamble(text: str) -> str:
        """丢弃第一个 `## ` 之前的文档说明段（避免说明文字被当成知识切片）"""
        lines = (text or "").splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("## "):
                return "\n".join(lines[i:]).strip()
        return (text or "").strip()

    # 入库时丢弃的纯模板行（提示语，不含知识信息）：
    #   1) 「管理员修改区（…）：」这类空提示行
    #   2) 「AI 供参考：…」——给管理员看的建议，正文里不该留
    # 3) 「⚠ 本条知识库资料不足…」——答案补齐后这行已过时
    #   4) 「原始回答类型：…」/「补答依据（…）：…」——给管理员看的提示与来源指针，
    #      都只对"人工修改"有用，本身不含知识信息，入库只会变成噪声切片
    _BOILERPLATE_RES = (
        re.compile(r"^\s*(管理员修改区|修改区)\b.*[：:]\s*$"),
        re.compile(r"^\s*AI\s*供参考\s*[:：]"),
        re.compile(r"^\s*[⚠❌]\s*"),
        re.compile(r"^\s*原始回答类型.*[：:]"),
        re.compile(r"^\s*(补答依据|知识库依据).*[：:]"),
    )
    # 生成器元信息（Prompt 让 LLM 输出的收尾标记）：可能出现在行首，也可能混在正文行尾。
    # 它不含任何知识信息，一旦入库就会被当成"标准答案"的一部分答给用户，必须行内剥掉。
    _NOISE_INLINE_RE = re.compile(
        r"需人工补充\s*[:：]?\s*(?:是|否|yes|no)?", re.I)

    # 未填写的占位答案：**只认"整条答案就是占位话术"**（开头锚定），
    # ⚠️ 绝不能用子串匹配 —— "…如需通票或套票信息，知识库暂无相关条目，建议咨询售票处…"
    #    是一条**有内容的**答案，含有关键词就被丢弃会让整条问答静默消失
    #    （实测曾因此丢掉《知识库补充标准答案_20260914_122933.docx》的「门票多少钱」整块，
    #      20 个问答块入库只剩 19 个，而界面上完全看不出来）。
    _PLACEHOLDER_ONLY_RE = re.compile(
        r"^\s*(?:[（(]?\s*待补充\s*[）)]?\s*$"
        r"|请园区补充"
        r"|知识库暂无(?:相关)?条目"
        r"|待人工补充)",
        re.I,
    )

    @classmethod
    def clean_for_ingest(cls, text: str) -> str:
        """清洗回传文档：去标题前说明段 → 去模板行 → 剔除未填写的占位条目"""
        content, _total, _kept = cls.prepare_for_ingest(text)
        return content

    @classmethod
    def prepare_blocks_for_ingest(cls, text: str) -> Tuple[List[str], int, int]:
        """清洗回传文档 → 逐个「可入库问答块」正文。

        与 prepare_for_ingest 的区别：这里保留**逐块粒度**，因为入库侧去重闸门
        必须按块判断（哪些块与库里已有问题重复），拼成一整篇就没法按块丢弃了。

        Returns: (可入库块正文列表, 文档内问答块总数, 清洗后块数)
        """
        blocks = cls._split_qa_blocks(cls.strip_preamble(text))
        kept: List[str] = []
        for b in blocks:
            if cls._is_placeholder_block(b):
                continue
            cleaned = [l for l in b if not any(r.match(l) for r in cls._BOILERPLATE_RES)]
            body = cls._NOISE_INLINE_RE.sub("", "\n".join(cleaned)).strip()
            if body:
                kept.append(body)
        return kept, len(blocks), len(kept)

    @classmethod
    def prepare_for_ingest(cls, text: str) -> Tuple[str, int, int]:
        """清洗回传文档并返回 (可入库正文, 文档内问答块总数, 实际保留块数)"""
        bodies, total, kept = cls.prepare_blocks_for_ingest(text)
        return "\n\n".join(bodies).strip(), total, kept

    # ---------- 入库侧去重闸门 ----------
    def _dedup_ingest_blocks(self, bodies: List[str], doc_name: str) -> Tuple[List[str], List[Dict]]:
        """同一问题库里已有 → 跳过，不新增（权威源豁免）。

        为什么必须在这一层挡：生成侧闸门只能挡住"从盲区自动生成"那条路径，
        管理员手工写一份补充文档回传时完全绕开了它。**入库侧是所有写入的必经之路**，
        在这里挡住，才能真正保证"库里同一问题永远只有一条答案"这条不变量
        （历史教训：第二轮补充文档与第一轮有 4 个问题完全重复，且答案措辞不同，
         检索时两条一起进上下文 → 同一问题时而答 A 版、时而答 B 版）。

        权威源（原始文档，默认匹配 "duck"）**豁免**本闸门：它永远优先，
        否则一旦原始文档某块被判为"与补充文档重复"，就会丢原始事实。

        判定按**整批一次**完成（三级判据里的 LLM 层是一次调用判一批），
        逐块调用会让 N 个块变成 N 次 LLM 往返。

        Returns: (保留的块正文列表, 冲突清单)
        """
        primary = is_primary_source(doc_name)
        seen: set = set()
        candidates: List[Tuple[int, str]] = []          # (下标, 问题标题)
        conflicts: List[Dict] = []

        for i, body in enumerate(bodies):
            key = dedup_key(body)
            title = question_of(body)
            # ① 文档内重复
            if key and key in seen:
                conflicts.append({"question": title, "reason": "本文档内重复",
                                  "source": doc_name, "score": 1.0})
                continue
            if key:
                seen.add(key)
            # ② 权威源直接放行（永远优先）
            if primary:
                continue
            candidates.append((i, title))

        # ③ 与库内已有问题重复 → 跳过（整批一次判定）
        dup_idx: Dict[int, Dict] = {}
        if candidates:
            try:
                res = coverage_batch([t for _i, t in candidates],
                                     exclude_source=doc_name)
            except Exception as e:                  # pragma: no cover
                logger.warning(f"[kb_agent] 入库去重判定失败（本批放行）: {e}")
                res = {}
            for i, title in candidates:
                r = (res.get(title) or {}) if res else {}
                if r.get("hit"):
                    dup_idx[i] = {"question": title,
                                  "reason": "库中已有同一问题",
                                  "source": r.get("source", ""),
                                  "score": r.get("score", 0.0),
                                  "matched": r.get("matched", "")}

        kept: List[str] = []
        for i, body in enumerate(bodies):
            if i in dup_idx:
                conflicts.append(dup_idx[i])
            else:
                kept.append(body)
        return kept, conflicts

    def dedup_filter_document(self, raw: str, doc_name: str) -> Tuple[str, List[Dict], int]:
        """给**任意写入口**做问题级去重（知识库管理页上传也走这里）。

        只有"能识别出问答块"的文档才会被干预：判据是有 `## ` 标题块**且**块内有
        「标准答案：」标记 —— 即知识库智能体产出的那种结构。
        其它自由格式的资料（分节写法的 PDF/TXT 等）原样放行，不做任何改动，
        避免用一套只适用于问答块的规则去误伤普通文档。

        Returns: (可入库正文, 冲突清单, 识别到的问答块数)
        """
        try:
            bodies, total, _kept = self.prepare_blocks_for_ingest(raw)
        except Exception as e:                      # pragma: no cover
            logger.warning(f"[kb_agent] 去重预检失败（原样放行）: {e}")
            return raw, [], 0
        if not bodies:
            return raw, [], 0
        kept, conflicts = self._dedup_ingest_blocks(bodies, doc_name)
        return "\n\n".join(kept), conflicts, total

    @staticmethod
    def _split_qa_blocks(text: str) -> List[List[str]]:
        """按 `## ` 标题行把文档切成若干问答块（每块含标题行本身）"""
        blocks: List[List[str]] = []
        cur: Optional[List[str]] = None
        for line in (text or "").splitlines():
            if line.lstrip().startswith("## "):
                if cur:
                    blocks.append(cur)
                cur = [line]
            elif cur is not None:
                cur.append(line)
        if cur:
            blocks.append(cur)
        return blocks

    @classmethod
    def _is_placeholder_block(cls, block: List[str]) -> bool:
        """块内「标准答案」为空、或**整条就是**占位话术 → 视为未填写，不入库"""
        body = "\n".join(block[1:])
        m = re.search(r"标准答案\s*[:：]\s*([^\n]*)", body)
        ans = (m.group(1) if m else "").strip()
        ans = cls._NOISE_INLINE_RE.sub("", ans).strip()
        if not ans:
            return True
        return bool(cls._PLACEHOLDER_ONLY_RE.match(ans))

    def ingest_docx(self, file_path: str, doc_name: str,
                    replace_source: str = "") -> Dict:
        """把管理员修改后的 Word 走完整入库链路：解析 → 清洗 → 切片 → Milvus + BM25 → 清缓存。"""
        from agents.document_agent import document_agent
        from core.embedding import embedding_engine
        from core.vector_store import vector_store
        from core.bm25_index import bm25_index

        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".docx":
            raw = document_agent.extract_text_from_docx(file_path)
        elif ext == ".txt":
            raw = document_agent.extract_text_from_txt(file_path)
        elif ext == ".pdf":
            raw = document_agent.extract_text_from_pdf(file_path)
        else:
            raise ValueError(f"不支持的格式 {ext}（仅支持 .docx/.txt/.pdf）")

        bodies, total_blocks, kept_blocks = self.prepare_blocks_for_ingest(raw)
        if not bodies:
            raise ValueError("没有可入库的内容：所有条目都是空答案/占位答案，"
                             "请先在 Word 里填写「标准答案」后再回传")

        # 覆盖式更新：**必须先清掉旧来源**，两个原因 ——
        #   ① 同名重新上传时若不清，Milvus 是 append 语义，同一份文档会堆两遍
        #      （旧代码的 `replace_source != doc_name` 判断让"同名覆盖"这条路径什么也没删，
        #        是"库里出现重复"的另一个来源）；
        #   ② 必须赶在去重闸门之前清，否则旧块还在库里，闸门会把"自己上一版"判成重复，
        #      导致同名重传时整篇被跳过、修改永远不生效。
        if replace_source:
            try:
                document_agent.delete_document(replace_source)
                invalidate_cache()
                logger.info(f"[kb_agent] 已清理旧来源切片: {replace_source}")
            except Exception as e:
                logger.warning(f"[kb_agent] 清理旧来源失败（忽略）: {e}")

        # ---- 入库侧去重闸门：同一问题库里已有 → 跳过（权威源豁免）----
        kept_bodies, conflicts = self._dedup_ingest_blocks(bodies, doc_name)
        if not kept_bodies:
            raise ValueError(
                f"本次 {len(bodies)} 个问答块全部与现有知识库重复（同一问题已有答案），"
                f"已全部跳过、未写入任何内容。若确需修改，请直接修改已有那份文档后回传。")
        if conflicts:
            logger.info(f"[kb_agent] 入库去重：保留 {len(kept_bodies)} 块，"
                        f"跳过 {len(conflicts)} 块")

        content = "\n\n".join(kept_bodies)
        chunks = document_agent.split_text(content)
        if not chunks:
            raise ValueError("切片结果为空，请检查文档内容")

        chunk_list = [{"text": c, "metadata": {"source": doc_name, "chunk_id": i}}
                      for i, c in enumerate(chunks)]
        document_agent.save_chunks_to_file(doc_name, chunk_list)

        texts = [c["text"] for c in chunk_list]
        sources = [doc_name] * len(texts)
        vectors = embedding_engine.embed_documents(texts)
        vector_store.insert(texts, vectors, sources)
        bm25_index.add_documents(texts, sources)

        # 知识库变更 → 清缓存 + 重载问法库（与 /knowledge/upload 后处理保持一致）
        try:
            from core.cache import redis_cache
            redis_cache.flush_cache()
        except Exception as e:
            logger.warning(f"[kb_agent] 清缓存失败（忽略）: {e}")
        try:
            from agents.query_agent import query_agent
            query_agent.reload_patterns()
        except Exception as e:
            logger.warning(f"[kb_agent] 重载问法库失败（忽略）: {e}")
        try:
            from services.kb_dedup import invalidate_cache
            invalidate_cache()
        except Exception as e:                      # pragma: no cover
            logger.warning(f"[kb_agent] 清去重缓存失败（忽略）: {e}")

        logger.info(f"[kb_agent] 回传入库完成: {doc_name} → {len(chunks)} 个切片"
                    f"（文档 {total_blocks} 个问答块，保留 {kept_blocks} 个，"
                    f"去重跳过 {len(conflicts)} 个）")
        return {"doc_name": doc_name, "chunk_count": len(chunks),
                "total_blocks": total_blocks,
                "kept_blocks": kept_blocks,
                "skipped_blocks": max(0, total_blocks - kept_blocks),
                "dedup_skipped": len(conflicts),
                "conflicts": conflicts,
                "conflicts_truncated": len(conflicts) > 20,
                "text_count": len(content), "path": file_path}

    # ---------- 5. 主流程（同步生成器，逐事件 yield） ----------
    def analyze_stream(self, message: str = "", days: int = None, limit: int = None,
                       max_tokens: int = None, generate: bool = True,
                       export: bool = True) -> Generator[dict, None, None]:
        """统计 → 生成标准答案 → 导出 Word，全程 yield 进度事件。

        事件类型：step / dataset / progress / answer / artifact / assistant / error
        """
        t0 = time.time()
        days = KB_AGENT_DAYS if days is None else max(0, int(days))
        limit = KB_AGENT_LIMIT if limit is None else max(1, min(int(limit), 100))
        max_tokens = KB_AGENT_ANSWER_TOKENS if max_tokens is None else max(64, min(int(max_tokens), 2000))

        yield {"type": "step", "stage": "parse", "status": "done",
               "title": "解析指令",
               "detail": f"统计范围 {('不限时间' if not days else f'近 {days} 天')} · "
                         f"最多 {limit} 个问题 · 每条答案 ≤{max_tokens} token"}

        # ---- 1) 统计 ----
        yield {"type": "step", "stage": "collect", "status": "running",
               "title": "统计没答上来的问题（模型给建议的兜底回答）"}
        try:
            stats = collect_gaps(days, limit)
        except Exception as e:
            logger.error(f"[kb_agent] 统计失败: {e}", exc_info=True)
            yield {"type": "error", "content": f"统计失败：{e}"}
            return

        stats["days"] = days
        items = stats["items"]
        if not items:
            yield {"type": "step", "stage": "collect", "status": "done",
                   "title": "统计没答上来的问题（模型给建议的兜底回答）",
                   "detail": "没有查到兜底回答记录"}
            yield {"type": "assistant", "content":
                   f"在**{('全部时间' if not days else f'近 {days} 天')}**的问答记录里，"
                   f"没有发现「模型没检索到知识库、只能给建议」的问题"
                   f"（兜底回答记录 {stats['raw_total']} 条）。\n\n"
                   f"说明这段时间知识库基本答得上：需要知识库兜底建议的问题为 0。"
                   f"可以换个时间范围再试，或者去「知识库管理」补充资料。"}
            yield {"type": "done", "elapsed": round(time.time() - t0, 2), "stats": stats}
            return

        skip_txt = "、".join(f"{k} {v} 条" for k, v in stats["skipped"].items()) or "无"
        type_txt = "、".join(f"{_type_label(k)} {v} 条"
                             for k, v in (stats.get("by_type") or {}).items()) or "无"
        yield {"type": "step", "stage": "collect", "status": "done",
               "title": "统计没答上来的问题（模型给建议的兜底回答）",
               "detail": f"兜底回答 {stats['raw_total']} 条（{type_txt}）→ "
                         f"归一化去重后 {stats['distinct_total']} 个问题，"
                         f"本次处理 {len(items)} 个（已剔除 {skip_txt}）"}
        yield {"type": "dataset", "title": "知识库盲区（模型没检索到资料、只能给建议的问题）",
               "items": [{"question": i["question"], "count": i["count"],
                          "last_seen": i["last_seen"],
                          "fallback_type": i.get("fallback_type", ""),
                          "fallback_label": _type_label(i.get("fallback_type", "")),
                          "answer_head": i.get("answer_head", "")} for i in items]}

        # ---- 去重闸门：把"现有知识库已经答得上"的问题挡在生成之前 ----
        covered_items = stats.get("covered") or []
        if covered_items:
            yield {"type": "step", "stage": "collect", "status": "done",
                   "title": "去重闸门：剔除已被现有知识库覆盖的问题",
                   "detail": f"{len(covered_items)} 个问题在现有知识库（含原始文档）里已能答上，"
                             f"不再重复生成标准答案"}
            yield {"type": "dataset",
                   "title": "已跳过（现有知识库已能答上，不重复生成）",
                   "items": [{"question": c["question"], "count": c.get("count", 1),
                              "last_seen": "", "fallback_type": "",
                              "fallback_label": f"{c.get('scope', '知识库')}已覆盖"
                                                f" · 命中「{c.get('matched', '')}」",
                              "answer_head": ""} for c in covered_items]}
        results: List[Dict] = []
        # ---- 2) 生成标准答案 ----
        if generate:
            yield {"type": "step", "stage": "answer", "status": "running",
                   "title": "基于知识库生成标准答案",
                   "detail": f"并发 {KB_AGENT_WORKERS} 路，逐题检索知识库现有资料…"}

            done = 0
            total = len(items)
            with ThreadPoolExecutor(max_workers=max(1, KB_AGENT_WORKERS)) as pool:
                futures = {pool.submit(self.generate_one, it["question"], max_tokens): it
                           for it in items}
                for fut in as_completed(futures):
                    it = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        logger.error(f"[kb_agent] 生成失败 {it['question']}: {e}")
                        res = {"question": it["question"], "answer": f"（生成失败：{str(e)[:60]}）",
                               "sources": [], "needs_review": True, "has_context": False,
                               "tokens": 0, "truncated": False}
                    res["count"] = it["count"]
                    res["fallback_type"] = it.get("fallback_type", "")
                    results.append(res)
                    done += 1
                    yield {"type": "progress", "stage": "answer", "current": done, "total": total,
                           "label": res["question"][:40]}
                    yield {"type": "answer", "index": done, "total": total, **res}

            # 保持与统计一致的排序，便于阅读与对照
            order = {it["question"]: i for i, it in enumerate(items)}
            results.sort(key=lambda r: order.get(r["question"], 999))

            need = sum(1 for r in results if r.get("needs_review"))
            yield {"type": "step", "stage": "answer", "status": "done",
                   "title": "基于知识库生成标准答案",
                   "detail": f"已生成 {len(results)} 条标准答案"
                             + (f"，其中 {need} 条知识库资料不足、已标记待人工补充" if need else "")}
        else:
            results = [{"question": it["question"], "answer": "", "sources": [],
                        "needs_review": True, "has_context": False, "tokens": 0,
                        "truncated": False, "count": it["count"],
                        "fallback_type": it.get("fallback_type", "")} for it in items]

        # ---- 3) 导出 Word ----
        doc_info = None
        if export:
            yield {"type": "step", "stage": "docx", "status": "running",
                   "title": "生成可编辑 Word 文档"}
            try:
                doc_info = self.build_docx(results, stats, max_tokens)
            except Exception as e:
                logger.error(f"[kb_agent] 生成 Word 失败: {e}", exc_info=True)
                yield {"type": "error", "content": f"生成 Word 文档失败：{e}"}
                return
            yield {"type": "step", "stage": "docx", "status": "done",
                   "title": "生成可编辑 Word 文档",
                   "detail": f"{doc_info['name']}（{doc_info['size'] / 1024:.1f} KB，{doc_info['items']} 条）"}
            yield {"type": "artifact", "kind": "docx", "name": doc_info["name"],
                   "url": doc_info["url"], "size": doc_info["size"], "items": doc_info["items"]}

        # ---- 汇总 ----
        covered = sum(it["count"] for it in items)
        lines = [
            f"✅ 统计完成：{('全部时间' if not days else f'近 {days} 天')}内共 **{stats['raw_total']}** 条"
            f"「模型没检索到知识库 → 给建议」的兜底回答（{type_txt}），"
            f"归一化后 **{stats['distinct_total']}** 个问题，本次处理 **{len(items)}** 个"
            f"（合计被提问 {covered} 次）。",
            "",
            "> 口径：只算兜底建议类回答；缓存命中、地图/业务类正常回答**不计入**（它们的 references 也是空的，容易被误算）。",
            "",
            "| # | 用户问题 | 提问次数 | 原回答 | 补答依据（当前知识库） | 需人工补充 |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(results, 1):
            q = (r["question"] or "").replace("|", "\\|")
            src = "、".join(r.get("sources") or []) or "—"
            src = src.replace("|", "\\|")
            lines.append(f"| {i} | {q} | {r.get('count', 1)} | "
                         f"{_type_label(r.get('fallback_type', ''))} | {src} | "
                         f"{'⚠ 是' if r.get('needs_review') else '否'} |")
        if doc_info:
            tr = sum(1 for r in results if r.get("truncated"))
            lines += [
                "",
                f"📄 已生成 Word 文档 **{doc_info['name']}**（{doc_info['items']} 条"
                + (f"，{tr} 条因超长被截断到 {max_tokens} token" if tr else "") + "）。",
                "",
                "**下一步**：点上面的下载卡片拿到 Word → 在 Word 里直接修改「标准答案」→ "
                "保存后回到本页把文件发回（或点「回传 Word 入库」），"
                "智能体会自动解析 → 切片 → 写入向量库与关键词索引，"
                "之后用户问到这些问题就能从知识库直接答上了。",
            ]
        else:
            lines += ["", "（本次仅统计，未生成文档。）"]

        yield {"type": "assistant", "content": "\n".join(lines)}
        yield {"type": "done", "elapsed": round(time.time() - t0, 2),
               "summary": {"raw_total": stats["raw_total"], "distinct": stats["distinct_total"],
                           "processed": len(items), "covered": covered,
                           "needs_review": sum(1 for r in results if r.get("needs_review")),
                           "doc": doc_info["name"] if doc_info else ""}}

    # ---------- 6. 回传入库（同步生成器） ----------
    def ingest_stream(self, file_path: str, doc_name: str,
                      replace_source: str = "") -> Generator[dict, None, None]:
        t0 = time.time()
        yield {"type": "step", "stage": "parse", "status": "running",
               "title": "解析 Word 文档", "detail": doc_name}
        try:
            info = self.ingest_docx(file_path, doc_name, replace_source)
        except Exception as e:
            logger.error(f"[kb_agent] 回传入库失败: {e}", exc_info=True)
            yield {"type": "step", "stage": "parse", "status": "failed",
                   "title": "解析 Word 文档", "detail": str(e)}
            yield {"type": "error", "content": f"入库失败：{e}"}
            return

        yield {"type": "step", "stage": "parse", "status": "done",
               "title": "解析 Word 文档",
               "detail": f"正文 {info['text_count']} 字 → 切分 {info['chunk_count']} 个切片"
                         + (f"（去重跳过 {info.get('dedup_skipped', 0)} 块）"
                            if info.get("dedup_skipped") else "")}

        # 去重闸门命中的问题：明确回显，避免管理员误以为"怎么少入库了几条"
        conflicts = info.get("conflicts") or []
        if conflicts:
            yield {"type": "step", "stage": "dedup", "status": "done",
                   "title": "去重闸门：跳过已存在于知识库的问题",
                   "detail": f"{len(conflicts)} 个问题在知识库里已有答案，已跳过、未重复写入"}
            yield {"type": "dataset",
                   "title": "已跳过（知识库中已有同一问题，不重复入库）",
                   "items": [{"question": c["question"], "count": 1,
                              "last_seen": "", "fallback_type": "",
                              "fallback_label": f"{c['reason']}"
                                                f" · 命中「{c.get('matched') or c.get('source') or '-'}」",
                              "answer_head": ""} for c in conflicts[:50]]}
        yield {"type": "step", "stage": "index", "status": "done",
               "title": "写入向量库与关键词索引",
               "detail": f"Milvus {info['chunk_count']} 条向量 + BM25 {info['chunk_count']} 篇，已清缓存并重载问法库"}
        yield {"type": "artifact", "kind": "kb_doc", "name": info["doc_name"],
               "url": f"/knowledge/chunks/{info['doc_name']}", "size": 0,
               "items": info["chunk_count"]}
        _dedup_note = ""
        if info.get("dedup_skipped"):
            _dedup_note = (f"\n- ⛔ 去重闸门跳过 **{info['dedup_skipped']}** 个问题"
                           f"（知识库里已有同一问题的答案，未重复写入）")
        yield {"type": "assistant", "content":
               f"✅ 已把 **{info['doc_name']}** 嵌入知识库：\n"
               f"- 解析正文 **{info['text_count']}** 字 → 按标题块切分 **{info['chunk_count']}** 个切片\n"
               f"- 已写入向量库（Milvus）与关键词索引（BM25）\n"
               f"- 已清空问答缓存并重载常见问法库，**现在即可被检索命中**"
               f"{_dedup_note}\n\n"
               f"可以去「知识库管理」看到这份文档，或用「问答记录」验证效果。"}
        yield {"type": "done", "elapsed": round(time.time() - t0, 2),
               "summary": {"doc": info["doc_name"], "chunks": info["chunk_count"],
                           "dedup_skipped": info.get("dedup_skipped", 0)}}


# ============================================================
# 意图解析（把管理员的一句话变成 意图 + 参数）
# ============================================================
_DAYS_RE = re.compile(r"近\s*(\d+)\s*天|最近\s*(\d+)\s*天|(\d+)\s*天(?:内|以来)")
_LIMIT_RE = re.compile(r"(?:前|最多|处理|取)\s*(\d+)\s*(?:个|条|题)|top\s*(\d+)", re.I)
_TOKENS_RE = re.compile(r"(?:token|Token|TOKEN|字数|字)\s*(?:保持|控制|限制|不超(?:过)?|以内|以下)?\s*(\d+)")

_INGEST_WORDS = ("回传", "入库", "嵌入", "加入知识库", "存进知识库", "更新知识库", "重新切片")

# "只统计"类指令：不要生成答案/文档（管理员想先看清单）
_NO_GEN_WORDS = ("只统计", "仅统计", "先统计", "只先统计", "先不生成", "不用生成",
                 "不要生成", "暂不生成", "只看看", "只看下", "先看看")
# 生成答案但不要 Word（部分管理员习惯在网页上直接看）
_NO_DOC_WORDS = ("不生成文档", "不用文档", "不要文档", "不用导出", "不要导出", "不导出")


def detect_intent(message: str, has_file: bool = False) -> Dict:
    """把管理员的话解析为 {intent, days, limit, max_tokens, generate, export, reason}。

    规则优先（可预期、无额外 LLM 开销）：
      - 附带文件 / 提到回传入库 → ingest
      - 否则默认走 analyze（统计 → 标准答案 → Word）
      - 明确说"只统计" → generate=False（只给清单，不调 LLM、不生成文档）
    """
    msg = (message or "").strip()

    days: Optional[int] = KB_AGENT_DAYS
    if re.search(r"所有|全部|不限|一直以来|历史", msg):
        days = 0
    m = _DAYS_RE.search(msg)
    if m:
        days = int(next(g for g in m.groups() if g))

    limit: Optional[int] = KB_AGENT_LIMIT
    m = _LIMIT_RE.search(msg)
    if m:
        limit = int(next(g for g in m.groups() if g))

    max_tokens: Optional[int] = KB_AGENT_ANSWER_TOKENS
    m = _TOKENS_RE.search(msg)
    if m:
        max_tokens = int(m.group(1))

    generate, export = True, True
    if any(w in msg for w in _NO_GEN_WORDS):
        generate, export = False, False
    elif any(w in msg for w in _NO_DOC_WORDS):
        export = False

    intent = "analyze"
    reason = "按指令统计未命中问题并生成标准答案文档"
    if has_file or any(w in msg for w in _INGEST_WORDS):
        intent = "ingest"
        reason = "识别到回传文档入库意图"
    elif not generate:
        reason = "识别到「只统计」意图：只输出未命中问题清单"

    return {"intent": intent, "days": days, "limit": limit,
            "max_tokens": max_tokens, "generate": generate, "export": export,
            "reason": reason}


# 全局单例
kb_agent = KbCuratorAgent()
