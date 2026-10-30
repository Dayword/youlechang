# ============================================================
# 知识库「问题级去重」守卫（单一真源）
# ------------------------------------------------------------
# 目标：**同一个业务问题在知识库里只能有一条答案**。
#
# 三道闸门共用本模块，避免"哪里都要判一次重复"导致口径漂移：
#   1) 生成侧  kb_agent.collect_gaps  —— 库内已能答上的问题不进盲区列表
#   2) 入库侧  kb_agent.ingest_docx   —— 与库内已有问题重复则跳过（权威源豁免）
#   3) 检索侧  retrieval_agent.search —— 同一问题只保留最高分那条（兜底）
#
# 判据分三级（见 config.py 的说明）：
#   ① 精确层：问题归一化后完全相同 → 必判重复（零成本、零误判）
#   ② 向量层：与「库内已有问法清单」做 BGE-M3 语义匹配，召回候选
#   ③ LLM 层：待判问题 + 候选问法**批量**交给 LLM 裁决是否同一问题
#
# ⚠️ 为什么必须有 LLM 层：实测向量相似度在这种短问题上**分不开** ——
#    噪声本底 0.75~0.80（「办卡可以开发票吗」vs「办卡」0.80、
#    「儿童能一起玩蹦床吗」vs「蹦床玩多久」0.76），
#    而真重复「蹦床一次多少币」vs「蹦床多少钱」只有 0.88，用阈值切必然两头都错。
# ⚠️ 为什么不能用 CrossEncoder(问题, 块全文)：块很长会稀释相关性，
#    「蹦床一次多少币」对上 duck 的「### 1.4 蹦床」只有 0.0676 分，且单 pair 要 1.6s。
# ⚠️ 为什么不能用文本哈希：同一个问题的两版答案措辞必然不同（LLM 每次表述不同）。
# ============================================================
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from config import (KB_COVER_LLM, KB_COVER_VEC_HIGH, KB_DEDUP_CANDIDATES,
                    KB_FAQ_BASE, KB_FAQ_CEIL, KB_FAQ_W_SECOND)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 权威源（"原始文档"）：命中这些子串的来源不做入库查重，永远优先
# 默认 "duck" —— 对应 duck_1786354148.docx；可用 .env 覆盖，逗号分隔
# ------------------------------------------------------------
KB_PRIMARY_SOURCES = tuple(
    s.strip().lower() for s in
    os.getenv("KB_PRIMARY_SOURCES", "duck").split(",") if s.strip()
)


def is_primary_source(source: str) -> bool:
    """该来源是否为权威（原始）文档 —— 权威源不做入库查重，且证据优先级最高"""
    name = (source or "").lower()
    return any(p in name for p in KB_PRIMARY_SOURCES)


# ============================================================
# 1. 文本归一化 / 问法抽取
# ============================================================
_WS_RE = re.compile(r"\s+")
_TAIL_PUNCT_RE = re.compile(r"[\s。，、,.!！?？~～…;；:：'\"“”‘’（）()\[\]【】]+$")
_MD_HASH_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_NUM_PREFIX_RE = re.compile(
    r"^(?:q|question|问题)?\s*\d+(?:[.\-]\d+)*\s*[.、:：)）]?\s*"
    r"|^第\s*[一二三四五六七八九十百零\d]+\s*[章节条部分]\s*[、.：:]?\s*",
    re.I,
)
# `**常见问法**：a、b、c` —— 文档作者手工声明的问题清单，是最权威的"该块能回答什么"
_FAQ_RE = re.compile(r"常见问法\s*\**\s*[:：]\s*(.+)")


def normalize_question(q: str) -> str:
    """问题归一化：去空白 + 去尾部标点 + 小写，用于把同一问题的多次提问合并"""
    s = _WS_RE.sub("", (q or "").strip())
    s = _TAIL_PUNCT_RE.sub("", s)
    return s.lower()


def _title_lines(text: str) -> List[str]:
    """取出块内所有 Markdown 标题行的正文（去掉 # 记号）"""
    out = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", line)
        if m and m.group(1):
            out.append(m.group(1))
    return out


def question_of(text: str) -> str:
    """块的可读"问题/主题"文本（**单一主问题**，用于展示与检索侧去重键）。

    优先取 `## Qn. xxx` 形式的问答标题（知识库智能体产出的补充文档）；
    否则取块内**最深**的标题行（duck 这类分节文档，最深标题才是有区分度的那层）；
    再退化为首行。
    """
    titles = _title_lines(text)
    if not titles:
        first = (text or "").strip().splitlines()
        return (first[0] if first else "").strip()
    for t in titles:
        m = re.match(r"^\s*(?:q|question|问题)\s*\d+\s*[.、:：)）]\s*(.+)$", t, re.I)
        if m:
            return m.group(1).strip()
    return titles[-1].strip()


def questions_of(text: str) -> List[str]:
    """抽取一个块承载的**全部问题表达**（判重的基本单位）。

    * 有 `**常见问法**：…` 行 → 以它为准（作者声明的问法清单，如"蹦床多少钱、蹦床票价…"）。
      这是判"原始文档是否已覆盖某问题"最准确的依据 —— 拿"块全文"去比会因稀释而失准。
    * 否则取 `## Qn. xxx` / 最深标题 / 首行。
    """
    m = _FAQ_RE.search(text or "")
    if m:
        out = []
        for part in re.split(r"[、，,；;]", m.group(1)):
            part = part.strip(" 　*·-")
            part = _NUM_PREFIX_RE.sub("", part).strip()
            if 2 <= len(part) <= 30:
                out.append(part)
        if out:
            return out
    q = question_of(text)
    q = _NUM_PREFIX_RE.sub("", q).strip()
    return [q] if q else []


def dedup_key(text: str) -> str:
    """去重键：块主问题的归一化结果（**剥掉编号前缀**）。

    剥掉 Qn./1.1/一、 这类编号是必须的 —— 同一个问题在不同文档里编号不同
    （B 的 `## Q8. 园区允许带宠物入园吗` 与 C 的 `## Q1. 园区允许带宠物入园吗`），
    不剥就永远判不成同一个问题。检索侧兜底去重直接用它。
    """
    return normalize_question(_NUM_PREFIX_RE.sub("", question_of(text)).strip())


# ============================================================
# 2. 库内问题清单（带缓存）
# ============================================================
_INDEX_LOCK = threading.Lock()
_INDEX: Dict[str, object] = {
    "ts": 0.0,
    "entries": [],      # [(question, source, chunk_text)]
    "exact": {},        # norm(question) -> (question, source, chunk_text)
    "vecs": None,       # np.ndarray (n, 1024)
    "by_chunk": {},     # (source, chunk_text) -> [entry 下标]，供问法级检索提分用
    "ready": False,
}
# 缓存不靠 TTL 过期，而靠**知识库变更后的显式 invalidate_cache()** ——
# 重建要读 Milvus + 向量化上百条问法（实测 8~20s），绝不能出现在用户问答的热路径上。
_INDEX_TTL = float(os.getenv("KB_DEDUP_INDEX_TTL", "1800"))


def _build_index() -> None:
    """从 Milvus 重建「库内已有问法清单」（含问法向量 + 块→问法映射）"""
    from core.vector_store import vector_store

    entries: List[Tuple[str, str, str]] = []
    try:
        col = vector_store.collection
        col.flush()
        rows = col.query(expr="id >= 0", output_fields=["text", "source"], limit=16384)
    except Exception as e:
        logger.warning(f"[kb_dedup] 读取全库失败，去重索引为空: {e}")
        rows = []

    for r in rows:
        text = r.get("text") or ""
        source = r.get("source") or ""
        for q in questions_of(text):
            entries.append((q, source, text))

    exact: Dict[str, Tuple[str, str, str]] = {}
    by_chunk: Dict[Tuple[str, str], List[int]] = {}
    for i, (q, s, t) in enumerate(entries):
        exact.setdefault(normalize_question(q), (q, s, t))
        by_chunk.setdefault((s, t), []).append(i)

    vecs = None
    if entries:
        try:
            import numpy as np
            from core.embedding import embedding_engine
            vecs = np.array(
                embedding_engine.embed_documents([q for q, _s, _t in entries]),
                dtype="float32")
        except Exception as e:
            logger.warning(f"[kb_dedup] 问法向量化失败（向量层降级）: {e}")
            vecs = None

    _INDEX["entries"] = entries
    _INDEX["exact"] = exact
    _INDEX["by_chunk"] = by_chunk
    _INDEX["vecs"] = vecs
    _INDEX["ready"] = True
    _INDEX["ts"] = time.time()
    logger.info(f"[kb_dedup] 库内问法清单已重建: {len(entries)} 条问法"
                f"（来自 {len(rows)} 个块，向量={'有' if vecs is not None else '无'}）")


def _index() -> Dict[str, object]:
    now = time.time()
    if not _INDEX["entries"] or now - float(_INDEX["ts"]) > _INDEX_TTL:
        with _INDEX_LOCK:
            if not _INDEX["entries"] or time.time() - float(_INDEX["ts"]) > _INDEX_TTL:
                _build_index()
    return _INDEX


def index_ready() -> bool:
    return bool(_INDEX.get("ready"))


def warm_up() -> None:
    """启动时预热（在 main.py 的预热线程里调用）：把重建成本挪出用户请求路径"""
    try:
        _index()
    except Exception as e:                          # pragma: no cover
        logger.warning(f"[kb_dedup] 预热失败（问答会自动跳过问法提分）: {e}")


def invalidate_cache() -> None:
    """知识库变更后强制重建（入库/删除后必须调用，否则闸门与提分都会用旧清单）"""
    with _INDEX_LOCK:
        _INDEX["ts"] = 0.0
        _INDEX["entries"] = []
        _INDEX["exact"] = {}
        _INDEX["vecs"] = None
        _INDEX["by_chunk"] = {}
        _INDEX["ready"] = False


all_questions = lambda: list(_index()["entries"])          # noqa: E731


def primary_pool() -> List[Tuple[str, str, str]]:
    """只取权威源（原始文档）的问法 —— 用于"该问题是否已被原始文档覆盖"的判定"""
    return [e for e in _index()["entries"] if is_primary_source(e[1])]


# ============================================================
# 3. 向量层：召回候选问法
# ============================================================
def vector_candidates(question: str, k: int = None,
                      pool: List[Tuple[str, str, str]] = None) -> List[Tuple[float, str, str, str]]:
    """与库内问法做语义匹配，返回 [(相似度, 问法, 来源, 块原文)] 降序

    Args:
        pool: 只在这些问法里找（如"只看原始文档"）；None = 全库
    """
    k = k or KB_DEDUP_CANDIDATES
    idx = _index()
    vecs = idx["vecs"]
    entries = pool if pool is not None else idx["entries"]
    if vecs is None or not entries:
        # 无向量时退化为精确包含匹配
        nq = normalize_question(question)
        return [(1.0, q, s, t) for q, s, t in entries
                if nq and (nq in normalize_question(q) or normalize_question(q) in nq)][:k]
    try:
        import numpy as np
        from core.embedding import embedding_engine
        qv = np.array(embedding_engine.embed_query(question), dtype="float32")
    except Exception as e:                      # pragma: no cover
        logger.warning(f"[kb_dedup] 问题向量化失败: {e}")
        return []
    # 向量是按全库 entries 顺序算的；pool 是子集 → 用下标回填
    all_entries = idx["entries"]
    if pool is None:
        scores = vecs @ qv
        order = np.argsort(-scores)[:k]
        return [(float(scores[i]), all_entries[i][0], all_entries[i][1], all_entries[i][2])
                for i in order]
    pos = {id(e): i for i, e in enumerate(all_entries)}
    picked = [(pos.get(id(e), -1), e) for e in entries]
    picked = [(i, e) for i, e in picked if i >= 0]
    if not picked:
        return []
    sub = np.array([vecs[i] for i, _e in picked])
    scores = sub @ qv
    order = np.argsort(-scores)[:k]
    return [(float(scores[j]), picked[j][1][0], picked[j][1][1], picked[j][1][2])
            for j in order]


# ============================================================
# 3.5 问法级检索提分（FAQ boost）
# ============================================================
def faq_scores(query: str, cands: List[Tuple[str, str]]) -> List[Tuple[float, str]]:
    """对每个候选块算「问法命中分」（0~1）与命中的问法原文。

    为什么需要它：块是长段落，CrossEncoder(短问句, 长块) 的打分偏低且不稳 ——
    实测同一个正确块换个问法就从 0.18 跳到 0.87，「门票多少钱」对上含该问法的块
    只有 0.08。而块自带的多条「常见问法」是**短问句 vs 短问句**，
    语义相似度稳定得多（正确块普遍 0.82~1.00，不相关问题 ≤0.59）。

    融合方式：块内多条问法取 `(1-w)*最高 + w*次高`；块只有一条问法时次高取自身，
    否则单问法块（kb_agent 生成的块每条只有一个问题）会天然吃亏。
    先扣掉噪声本底 KB_FAQ_BASE，再映射到 KB_FAQ_CEIL，避免"只是关键词重合"被抬过阈值。

    成本：1 次 query 向量化（~47ms）+ 一次小矩阵乘；问法向量已随索引缓存。
    索引未就绪时返回全 0（问答自动降级为原行为，不报错、不阻塞）。

    Returns: [(分, 命中的问法)]，与 cands 一一对应
    """
    zeros = [(0.0, "") for _ in cands]
    idx = _index()
    vecs, by_chunk, entries = idx["vecs"], idx["by_chunk"], idx["entries"]
    if vecs is None or not by_chunk or not entries:
        return zeros
    try:
        import numpy as np
        from core.embedding import embedding_engine
        qv = np.array(embedding_engine.embed_query(query), dtype="float32")
    except Exception as e:                          # pragma: no cover
        logger.warning(f"[kb_dedup] 问法提分向量化失败: {e}")
        return zeros

    base, ceil, w2 = KB_FAQ_BASE, KB_FAQ_CEIL, KB_FAQ_W_SECOND
    out: List[Tuple[float, str]] = []
    for text, source in cands:
        ids = by_chunk.get((source, text))
        if not ids:
            out.append((0.0, ""))
            continue
        sims = vecs[ids] @ qv
        order = np.argsort(-sims)
        top = float(sims[order[0]])
        second = float(sims[order[1]]) if len(order) > 1 else top
        blend = (1 - w2) * top + w2 * second
        best_q = entries[ids[order[0]]][0]
        if blend <= base or base >= 1.0:
            out.append((0.0, best_q))
        else:
            out.append((min(1.0, (blend - base) / (1 - base)) * ceil, best_q))
    return out


# ============================================================
# 4. LLM 层：裁决"是否同一问题"
# ============================================================
JUDGE_SYSTEM = "你是知识库去重审核员，只按格式输出结果，不做任何解释。"

JUDGE_PROMPT = """下面每个「待判问题」都附了几条「库内已有问法」，请逐条判断该待判问题是否与其中某一条**是同一个问题**。

【算同一个问题】（问的是同一件事，答案可以直接互换）
- 同一件事的不同问法、口语变体、同义词替换、带不带主语/语气词
- 例：「蹦床一次多少币」=「蹦床多少钱」=「蹦床价格」
- 例：「营业时间是几点到几点」=「营业到几点」
- 例：「办理充值卡怎么操作」=「充值办卡」

【不算同一个问题】（话题相近或关键词重合，但问的是不同的事）
- 例：「办卡可以开发票吗」≠「办卡」（一个问发票，一个问怎么办卡/有什么优惠）
- 例：「帮我买两个旋转木马的票」≠「旋转木马多少钱」
      （一个是**要求下单购票的动作**，一个是问价格，不是同一个问题）
- 例：「儿童能一起玩蹦床吗」≠「蹦床玩多久」（一个问能不能多名儿童一起玩，一个问时长）
- 例：「四个孩子轮流玩怎么收费」≠「没有孩子大人玩怎么收费」
- 例：「两个大人带三个孩子怎么买票划算」≠「三个大人一个小孩怎么买票划算」
      （人数配置不同，最划算的方案也不同）
- 例：「园区允许带宠物入园吗」≠「什么不能带进园区」（禁带清单里没有宠物）
- 例：「园区允许带无人机航拍吗」≠「什么不能带进园区」
- **关键判据**：待判问题里出现了库内问法**没有涉及的新对象或新维度**
  （如 发票、宠物、无人机、母婴室、轮椅、年卡、手续费…），一律判「否」。

【输出格式】严格每行一条，竖线分隔，不要任何其它文字、不要编号以外的解释：
序号|是或否|命中的库内问法原文（判「否」时写 -）

【待判问题】
{items}

【结果】"""

_JUDGE_LINE_RE = re.compile(r"^\s*(\d+)\s*[|｜]\s*(是|否|yes|no|Y|N)\s*[|｜]?\s*(.*)$", re.I)


def _judge_batch(pairs: List[Tuple[str, List[str]]]) -> Dict[str, Optional[str]]:
    """一次 LLM 调用裁决一批问题。返回 {问题: 命中的库内问法 or None}

    None 表示"判否"，同时用于"LLM 不可用/解析不出"—— 调用方据此回退。
    """
    from core.llm_client import llm_client

    lines = []
    for i, (q, cands) in enumerate(pairs, 1):
        lines.append(f"{i}. {q}")
        lines.append(f"   库内候选：{' | '.join(cands) if cands else '（无）'}")
    prompt = JUDGE_PROMPT.format(items="\n".join(lines))

    raw = llm_client.invoke(prompt, system=JUDGE_SYSTEM,
                            max_tokens=min(2000, 60 + 30 * len(pairs))) or ""
    out: Dict[str, Optional[str]] = {}
    for line in raw.splitlines():
        m = _JUDGE_LINE_RE.match(line)
        if not m:
            continue
        i = int(m.group(1))
        if not (1 <= i <= len(pairs)):
            continue
        q = pairs[i - 1][0]
        verdict = m.group(2).lower()
        matched = (m.group(3) or "").strip().strip("-—")
        out[q] = matched if verdict in ("是", "yes", "y") else None
    return out


# ============================================================
# 5. 对外：覆盖判定
# ============================================================
_EMPTY = {"hit": False, "score": 0.0, "text": "", "source": "",
          "matched": "", "exact": False, "by": ""}


def coverage_batch(questions: List[str], pool: List[Tuple[str, str, str]] = None,
                   use_llm: bool = None,
                   exclude_source: str = None) -> Dict[str, Dict]:
    """批量判断"库内是否已经能回答这些问题"。

    三级判定：精确 → 向量召回候选 → LLM 裁决。
    LLM 不可用或解析失败时回退为"向量相似度 ≥ KB_COVER_VEC_HIGH 才算命中"
    （回退方向是**放行**，宁可漏判也不误杀该补的盲区问题；下游还有入库闸门与检索兜底）。

    Args:
        pool:           只在权威源里找；None = 全库
        exclude_source: 忽略该来源的问法（同名重传时避免把"自己上一版"判成重复）

    Returns: {question: {"hit","score","matched","source","text","exact","by"}}
    """
    use_llm = KB_COVER_LLM > 0 if use_llm is None else use_llm
    uniq = [q for q in dict.fromkeys(q or "" for q in questions) if q]
    if not uniq:
        return {}
    idx = _index()
    exact_map = idx["exact"]
    if pool is not None:
        pool = [e for e in pool if not (exclude_source and e[1] == exclude_source)]
    elif exclude_source:
        pool = [e for e in idx["entries"] if e[1] != exclude_source]
    out: Dict[str, Dict] = {}

    # ---- ① 精确层 ----
    pending: List[str] = []
    for q in uniq:
        key = normalize_question(q)
        if pool is None:
            hit = exact_map.get(key)
            if hit and exclude_source and hit[1] == exclude_source:
                hit = None
        else:
            hit = next((e for e in pool if normalize_question(e[0]) == key), None)
        if hit:
            out[q] = {"hit": True, "score": 1.0, "text": hit[2], "source": hit[1],
                      "matched": hit[0], "exact": True, "by": "exact"}
        else:
            pending.append(q)

    # ---- ② 向量层：为每题召回候选问法 ----
    cand_map: Dict[str, List[Tuple[float, str, str, str]]] = {}
    for q in pending:
        try:
            cand_map[q] = vector_candidates(q, KB_DEDUP_CANDIDATES, pool)
        except Exception as e:                  # pragma: no cover
            logger.warning(f"[kb_dedup] 候选召回失败 {q[:20]}: {e}")
            cand_map[q] = []

    # 无候选可比的：直接判未覆盖
    to_judge = [q for q in pending if cand_map.get(q)]
    for q in pending:
        if not cand_map.get(q):
            out[q] = dict(_EMPTY, by="no-candidate")

    # ---- ③ LLM 层 ----
    verdict: Dict[str, Optional[str]] = {}
    if to_judge:
        if use_llm:
            batch_size = 40
            for i in range(0, len(to_judge), batch_size):
                part = to_judge[i:i + batch_size]
                pairs = [(q, [c[1] for c in cand_map[q]]) for q in part]
                try:
                    verdict.update(_judge_batch(pairs))
                except Exception as e:
                    logger.warning(f"[kb_dedup] LLM 裁决失败（本批回退向量阈值）: {e}")

        for q in to_judge:
            cands = cand_map[q]
            top_score, top_q, top_src, top_text = cands[0]
            if use_llm and q in verdict:
                matched = verdict[q]
                if matched:
                    # 用命中的问法反查它所属的块（拿原文/来源）
                    e = next((c for c in cands if normalize_question(c[1])
                              == normalize_question(matched)), cands[0])
                    out[q] = {"hit": True, "score": float(e[0]), "text": e[3],
                              "source": e[2], "matched": e[1], "exact": False,
                              "by": "llm"}
                else:
                    out[q] = {"hit": False, "score": float(top_score), "text": top_text,
                              "source": top_src, "matched": top_q, "exact": False,
                              "by": "llm"}
            else:
                # LLM 不可用/未覆盖到 → 保守回退：只认高置信向量命中
                hit = top_score >= KB_COVER_VEC_HIGH
                out[q] = {"hit": hit, "score": float(top_score), "text": top_text,
                          "source": top_src, "matched": top_q, "exact": False,
                          "by": "vector-fallback"}

    for q in uniq:
        out.setdefault(q, dict(_EMPTY, by="skipped"))
    return out


def coverage(question: str, pool: List[Tuple[str, str, str]] = None,
             use_llm: bool = None) -> Dict:
    """单问题版覆盖判定（入库闸门内部按批调用，不要逐块循环调用它）"""
    return coverage_batch([question], pool=pool, use_llm=use_llm).get(question, dict(_EMPTY))


def coverage_many(questions: List[str], pool: List[Tuple[str, str, str]] = None,
                  workers: int = 4) -> Dict[str, Dict]:
    """兼容旧签名（workers 已不再需要：判定已改为批量、无 CrossEncoder 重排）"""
    return coverage_batch(questions, pool=pool)
