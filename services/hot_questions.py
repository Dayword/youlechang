# -*- coding: utf-8 -*-
"""
近 7 天热点问题 → 小程序 AI 问答页「快捷提问」6 个按钮的数据源

设计要点：
1. **统计口径**（相比商家 PC 原有的 `top_questions` 精确字符串分组做了增强）：
   - 先按 `search_query`（QueryAgent 改写/问法归一后的问句）聚类，把
     「门票多少钱 / 门票价格是多少 / 门票怎么收费」这类同义问法并到同一簇；
   - 无改写问句时用 jieba 关键词签名（名词/动词排序拼接）做兜底聚类；
   - 簇内按累计提问次数排序取 Top6，展示问句取簇内最高频的那条。
2. **标签压缩**：「问题过长就压缩成 4 个字」——业务词表命中优先（门票价格/办卡充值…），
   未命中则剥掉疑问词后取前 4 字，并配 emoji 图标。
3. **刷新节奏**：每 7 天一批。读取接口发现当前批次为空或已过期时触发刷新
   （默认后台线程执行，先返回旧数据/兜底数据，不阻塞小程序首屏）。
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

REFRESH_DAYS = 7        # 每 7 天刷新一批
DEFAULT_LIMIT = 6       # 小程序快捷提问按钮个数

# 兜底：知识库未积累到 7 天数据 / 统计失败时使用（与小程序历史写死的 6 条保持一致）
DEFAULT_HOT_QUESTIONS: List[Dict] = [
    {"question": "门票多少钱", "label": "门票价格", "icon": "🎫", "hit_count": 0, "source": "default"},
    {"question": "怎么办卡充值", "label": "办卡充值", "icon": "💳", "hit_count": 0, "source": "default"},
    {"question": "营业时间", "label": "营业时间", "icon": "🕐", "hit_count": 0, "source": "default"},
    {"question": "能退卡吗", "label": "退卡规则", "icon": "💰", "hit_count": 0, "source": "default"},
    {"question": "儿童游玩注意事项", "label": "儿童安全", "icon": "👶", "hit_count": 0, "source": "default"},
    {"question": "设备故障怎么办", "label": "设备故障", "icon": "🔧", "hit_count": 0, "source": "default"},
]

# ============================================================
# 快捷提问「写死」的 6 条（小程序问答页 6 个按钮）
# ------------------------------------------------------------
# 为什么要写死，而不是继续按近 7 天问答日志自动统计：
#   真实日志里问得最多的往往集中在同一两个主题（营业时间出现两次、还冒出
#   「宠物入园」这种极低频问法），6 个按钮被占满后**反而盖住了游客真正常用
#   的入口**（办卡充值、退卡、失物、儿童安全…），新人第一次进来看到的六个词
#   还是随机的。快捷提问的作用是"给出最常见的问法"，不是"回放最近的日志"。
#
# 所以这份清单是**人工定的常量**，并写进 Redis（key 见 PINNED_KEY）：
#   · 写 Redis 而不是只留常量 —— 运营在商家端改了之后能立刻生效、且不会被
#     每 7 天的自动统计覆盖（自动统计在 pinned 存在时直接跳过）；
#   · Redis 不可用 / key 丢了 —— 自动用常量回种一次，按钮不会空。
# 2026-09-21：去掉「门票多少钱/门票价格」，换成「失物招领」，其余 5 条不变。
# ============================================================
PINNED_KEY = "rag:hot:questions:pinned"
# 模式位：pinned=写死 / auto=按日志自动统计 / 缺失=从未初始化（首次读取时按常量播种）。
# 只靠"pinned key 在不在"判断是不够的：运营点了「解除写死」之后 key 也不在，
# 若一律当成"没初始化"就会在下次读取时悄悄写死回去，解除等于没生效。
MODE_KEY = "rag:hot:questions:mode"

PINNED_HOT_QUESTIONS: List[Dict] = [
    {"question": "东西丢了怎么找回", "label": "失物招领", "icon": "🧸", "hit_count": 0, "source": "pinned"},
    {"question": "怎么办卡充值", "label": "办卡充值", "icon": "💳", "hit_count": 0, "source": "pinned"},
    {"question": "营业时间", "label": "营业时间", "icon": "🕐", "hit_count": 0, "source": "pinned"},
    {"question": "能退卡吗", "label": "退卡规则", "icon": "💰", "hit_count": 0, "source": "pinned"},
    {"question": "儿童游玩注意事项", "label": "儿童安全", "icon": "👶", "hit_count": 0, "source": "pinned"},
    {"question": "设备故障怎么办", "label": "设备故障", "icon": "🔧", "hit_count": 0, "source": "pinned"},
]

# 业务主题 → (关键词, 4 字标签, emoji 图标)
_TOPICS = [
    (("门票", "票价", "套票", "通票", "团购"), "门票价格", "🎫"),
    (("办卡", "充值", "会员卡", "储值", "续费", "开卡"), "办卡充值", "💳"),
    (("退卡", "退款", "退币", "退费"), "退卡退款", "💰"),
    (("营业", "几点", "开放时间", "闭园", "打烊", "夜场"), "营业时间", "🕐"),
    (("失物", "丢了", "认领", "报失", "找回"), "失物招领", "🧸"),
    (("儿童", "孩子", "宝宝", "小孩", "身高", "亲子"), "儿童游玩", "👶"),
    (("安全", "受伤", "磕碰", "注意", "看护"), "安全须知", "⚠️"),
    (("停车", "路线", "地址", "导航", "怎么走", "公交", "地铁", "打车"), "交通路线", "🚗"),
    (("天气", "下雨", "雨天", "气温"), "天气影响", "🌦️"),
    (("故障", "坏了", "维修", "不能用", "停运", "检修", "卡住"), "设备故障", "🔧"),
    (("项目", "玩什么", "游乐", "设备", "设施", "好玩"), "项目推荐", "🎢"),
    (("签到", "奖励", "余币", "余额", "金币", "消费"), "余额签到", "🪙"),
    (("优惠", "折扣", "活动", "划算", "套餐"), "优惠活动", "🎁"),
    (("发票", "开票", "税"), "发票开具", "🧾"),
    (("厕所", "洗手间", "卫生间", "母婴室", "休息"), "园内设施", "🚻"),
    (("吃", "餐厅", "餐饮", "零食", "喝"), "餐饮服务", "🍽️"),
    (("宠物", "狗狗", "猫"), "宠物入园", "🐶"),
    (("存包", "寄存", "储物"), "物品寄存", "🎒"),
    (("生日", "派对", "团建", "包场"), "包场派对", "🎉"),
    (("医务", "中暑", "急救", "晕"), "医疗急救", "🏥"),
    (("办卡流程", "流程", "步骤", "怎么办理"), "办理流程", "📋"),
]

# 疑问/语气词（压缩标签时剥掉）
_NOISE_RE = re.compile(
    r"(请问|麻烦|想问一下|想问|我想知道|想知道|帮我|能不能|可不可以|可以吗|是否|有没有|多少个|"
    r"怎么样|怎么|如何|为什么|为啥|什么|哪些|哪个|哪里|在哪|多少|几多|多少钱|吗|呢|啊|呀|吧|的|了|"
    r"[？?！!，,。.、~～：:\s]+)"
)

_KEEP_POS = {"n", "nr", "ns", "nt", "nz", "v", "vn", "eng", "x"}


def _norm_text(text: str) -> str:
    """问句归一化：去空白/标点，用于聚类 key 与展示去重"""
    t = (text or "").strip()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[？?！!。，,、；;：:~～·\.]+", "", t)
    return t


def _keyword_signature(text: str) -> str:
    """关键词签名（名词/动词/业务词排序拼接）——无改写问句时的聚类兜底"""
    t = (text or "").strip()
    if not t:
        return ""
    try:
        import jieba.posseg as pseg
        from core.keywords import ensure_business_words

        ensure_business_words()
        words = []
        for w, flag in pseg.cut(t):
            w = w.strip()
            if len(w) < 2 or w in _NOISE_RE.pattern:
                continue
            if flag in _KEEP_POS:
                words.append(w)
        words = sorted(set(words))
        return "_".join(words) if words else _norm_text(t)[:12]
    except Exception:
        return _norm_text(t)[:12]


def compress_label(question: str, max_len: int = 4) -> Dict[str, str]:
    """把完整问句压缩成 ≤4 字按钮标签 + emoji 图标。

    规则优先（确定性、零延迟、离线可用）：
      1) 命中业务主题词表 → 用主题标准说法（如「门票多少钱」→ 门票价格）；
      2) 未命中 → 剥掉疑问/语气词，取剩余实词前 4 字（如「如何办理生日派对」→ 生日派对）；
      3) 仍为空 → 用原问句前 4 字兜底。
    """
    q = (question or "").strip()
    if not q:
        return {"label": "热门问题", "icon": "❓"}

    for keys, label, icon in _TOPICS:
        if any(k in q for k in keys):
            return {"label": label[:max_len], "icon": icon}

    stripped = _NOISE_RE.sub("", q).strip()
    core = stripped or q
    # 优先取分词后的实词（避免"我想知道"这类无意义前缀占位）
    try:
        import jieba
        from core.keywords import ensure_business_words

        ensure_business_words()
        toks = [t.strip() for t in jieba.lcut(core) if t.strip() and not _NOISE_RE.fullmatch(t)]
        joined = "".join(toks)
        if joined:
            core = joined
    except Exception:
        pass

    label = core[:max_len]
    return {"label": label or q[:max_len], "icon": _guess_icon(q)}


def _guess_icon(question: str) -> str:
    q = question or ""
    for keys, _label, icon in _TOPICS:
        if any(k in q for k in keys):
            return icon
    if any(k in q for k in ("怎么", "如何", "办理", "流程")):
        return "📋"
    if any(k in q for k in ("多少", "价格", "钱", "贵")):
        return "💰"
    if any(k in q for k in ("哪里", "在哪", "位置")):
        return "📍"
    return "💬"


def cluster_hot_questions(rows: List[Dict], limit: int = DEFAULT_LIMIT,
                          days: int = REFRESH_DAYS) -> List[Dict]:
    """把 question_logs 的分组行聚类成 Top-N 热点问题。

    rows: [{"question": 原始问句, "norm_query": 改写问句, "cnt": 次数}]
    """
    clusters: Dict[str, Dict] = {}
    for r in rows or []:
        q = (r.get("question") or "").strip()
        if not q:
            continue
        cnt = int(r.get("cnt") or 0)
        norm = _norm_text(r.get("norm_query") or q)
        key = norm or _keyword_signature(q)
        if not key:
            key = q
        c = clusters.get(key)
        if not c:
            clusters[key] = {
                "key": key,
                "hit_count": cnt,
                "raw_count": cnt,
                "variants": {q: cnt},
            }
        else:
            c["hit_count"] += cnt
            c["raw_count"] += cnt
            c["variants"][q] = c["variants"].get(q, 0) + cnt

    # 簇内取最高频问法作为展示问句（同样高频时取更短的，按钮更整齐）
    ordered = sorted(clusters.values(), key=lambda c: (-c["hit_count"], len(c["key"])))
    items: List[Dict] = []
    for c in ordered[: max(1, limit)]:
        variants = sorted(c["variants"].items(), key=lambda kv: (-kv[1], len(kv[0])))
        question = variants[0][0] if variants else c["key"]
        label_info = compress_label(question)
        items.append({
            "question": question,
            "label": label_info["label"],
            "icon": label_info["icon"],
            "hit_count": int(c["hit_count"]),
            "raw_count": int(c["raw_count"]),
            "source": "auto",
        })
    return items


_refresh_lock = threading.Lock()
_last_attempt = 0.0


# ============================================================
# 写死清单的读写（Redis 为单一真源，常量只负责"Redis 没有时回种"）
# ============================================================
def _cache():
    try:
        from core.cache import redis_cache
        return redis_cache if redis_cache.available else None
    except Exception:
        return None


def get_pinned(seed: bool = True) -> Optional[List[Dict]]:
    """读写死的 6 条；从未初始化过（模式位缺失）才按常量播种一次。

    seed=False 用于「只想查、不要顺手写」的场景（刷新统计、判过期），
    否则一次查询就会把运营刚解除的写死又种回去。
    """
    c = _cache()
    if c:
        if c.get_json(MODE_KEY) == "auto":
            return None
        v = c.get_json(PINNED_KEY)
        if isinstance(v, list) and v:
            return v
    return pin_hot_questions() if seed else None


def pin_hot_questions(items: Optional[List[Dict]] = None) -> List[Dict]:
    """写死一批快捷提问（默认就是 PINNED_HOT_QUESTIONS），同时同步给商家端表格。

    两份存储都要写：`/chat/hot-questions` 走 Redis（快、且不被统计覆盖），
    商家 PC「热点问题」页读 `hot_questions` 表 —— 只写一处会让两边显示不一样。
    """
    items = [dict(i) for i in (items or PINNED_HOT_QUESTIONS)]
    c = _cache()
    if c:
        c.set_json(MODE_KEY, "pinned")
        c.set_json(PINNED_KEY, items)
    try:
        from database.db import replace_hot_questions
        replace_hot_questions(items, days=REFRESH_DAYS)
    except Exception as e:
        logger.warning(f"[热点问题] 同步商家端表格失败（不影响小程序按钮）: {e}")
    return items


def unpin_hot_questions() -> None:
    """解除写死：置模式位为 auto 并删掉写死内容，恢复"按近 7 天问答日志自动统计"。"""
    c = _cache()
    if c:
        c.set_json(MODE_KEY, "auto")
        c.delete_key(PINNED_KEY)
    logger.info("[热点问题] 已解除写死，恢复自动统计")


def refresh_hot_questions(days: int = REFRESH_DAYS, limit: int = DEFAULT_LIMIT,
                          force: bool = False) -> Dict:
    """重新统计并整批写入热点问题（不足 limit 条时用兜底问题补齐）。

    ⚠️ 已经写死（Redis 里有 pinned）时**直接跳过**：自动统计的结果一旦覆盖上去，
    6 个按钮又会变成"最近日志回放"（营业时间重复、冒出极低频问法）。
    """
    pinned = get_pinned(seed=False)
    if pinned:
        return {"refreshed": False, "reason": "pinned", "auto_count": len(pinned),
                "items": pinned[:limit]}

    global _last_attempt
    with _refresh_lock:
        # 防抖：10 秒内重复触发只执行一次（多端并发读取时避免打爆 DB）
        if not force and time.time() - _last_attempt < 10:
            return {"refreshed": False, "reason": "debounced"}
        _last_attempt = time.time()

        rows: List[Dict] = []
        try:
            from database.db import hot_question_candidates
            rows = hot_question_candidates(days)
        except Exception as e:
            logger.warning(f"[热点问题] 读取问答日志失败: {e}")

        items = cluster_hot_questions(rows, limit=limit, days=days) if rows else []
        auto_count = len(items)

        # 补齐：历史数据不足 6 条时用兜底问题填满按钮位
        used = {_norm_text(i["question"]) for i in items}
        for d in DEFAULT_HOT_QUESTIONS:
            if len(items) >= limit:
                break
            if _norm_text(d["question"]) in used:
                continue
            items.append(dict(d))
            used.add(_norm_text(d["question"]))

        try:
            from database.db import replace_hot_questions
            replace_hot_questions(items[:limit], days=days)
        except Exception as e:
            logger.error(f"[热点问题] 写入失败: {e}")
            return {"refreshed": False, "reason": f"db_error: {str(e)[:120]}", "items": items}

        logger.info(
            f"[热点问题] 近 {days} 天统计刷新完成：自动统计 {auto_count} 条 + 兜底补齐至 {len(items[:limit])} 条")
        return {"refreshed": True, "auto_count": auto_count, "items": items[:limit],
                "window_days": days}


def _is_stale(refreshed_at, days: int = REFRESH_DAYS) -> bool:
    # 写死后"过期"这个概念就不存在了：批次本来就是固定的，不该再触发刷新
    if get_pinned(seed=False):
        return False

    if not refreshed_at:
        return True
    try:
        if isinstance(refreshed_at, str):
            ts = datetime.strptime(refreshed_at[:19], "%Y-%m-%d %H:%M:%S")
        else:
            ts = refreshed_at
        return datetime.now() - ts > timedelta(days=days)
    except Exception:
        return True


def get_hot_questions(limit: int = DEFAULT_LIMIT, days: int = REFRESH_DAYS,
                      auto_refresh: bool = True, block: bool = False) -> List[Dict]:
    """读取当前批次热点问题（小程序快捷提问按钮数据源）。

    - **写死优先**：Redis 里有 pinned 就原样返回，不读表、不触发统计；
    - 无数据 / 已过 7 天：触发刷新（默认后台线程，先返回兜底数据，不阻塞首屏）；
    - block=True 时同步刷新（商家 PC「立即刷新」按钮使用）。
    """
    pinned = get_pinned()
    if pinned:
        return [dict(i) for i in pinned[:limit]]

    items: List[Dict] = []
    refreshed_at = None
    try:
        from database.db import list_hot_questions, hot_questions_refreshed_at
        items = list_hot_questions(limit) or []
        refreshed_at = hot_questions_refreshed_at()
    except Exception as e:
        logger.warning(f"[热点问题] 读取失败: {e}")

    stale = _is_stale(refreshed_at, days)
    if auto_refresh and (not items or stale):
        if block:
            res = refresh_hot_questions(days=days, limit=limit, force=True)
            return res.get("items") or (items or DEFAULT_HOT_QUESTIONS[:limit])
        if not items:
            # 首次无数据：后台刷新，先返回兜底 6 条保证按钮不空
            threading.Thread(
                target=lambda: refresh_hot_questions(days=days, limit=limit, force=True),
                daemon=True,
            ).start()
            return DEFAULT_HOT_QUESTIONS[:limit]
        # 有旧数据但过期：后台静默刷新，本次仍返回旧数据（永不阻塞）
        threading.Thread(
            target=lambda: refresh_hot_questions(days=days, limit=limit, force=True),
            daemon=True,
        ).start()

    if not items:
        return DEFAULT_HOT_QUESTIONS[:limit]
    return items[:limit]


def hot_questions_status(limit: int = DEFAULT_LIMIT, days: int = REFRESH_DAYS) -> Dict:
    """商家 PC 用：当前批次 + 刷新时间 + 是否已过期 + 是否写死"""
    items = get_hot_questions(limit=limit, days=days, auto_refresh=False) or []
    try:
        from database.db import hot_questions_refreshed_at
        refreshed_at = hot_questions_refreshed_at()
    except Exception:
        refreshed_at = None
    return {
        "items": items,
        "pinned": bool(get_pinned(seed=False)),
        "refreshed_at": str(refreshed_at) if refreshed_at else "",
        "stale": _is_stale(refreshed_at, days),
        "window_days": days,
        "next_refresh_at": (
            (datetime.strptime(str(refreshed_at)[:19], "%Y-%m-%d %H:%M:%S") + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            if refreshed_at else ""
        ),
    }
