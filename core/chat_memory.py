# ============================================================
# 会话记忆 / 用户画像 / 主动追问 (core/chat_memory.py)
# ------------------------------------------------------------
# 杠杆3：
#   1. 服务端结构化多轮消息 —— 存入现成 chat_messages 表
#      （role=user/assistant，content=正文，user 行 question=原问，assistant 行
#       ref_docs/search_query 记录来源，实现"可查档"的多轮记忆）
#   2. 用户画像 —— 存入现成 agent_config(key-value) 表：
#       key=f"profile:{session_id}"；画像字段：身高/年龄/人数/关注点/天气提示
#   3. 回答后的"依据上下文的主动追问"—— 复用同一条 LLM 生成
# 说明：全程不新增数据库表；Redis 掉线/重启都不影响画像（agent_config 落库）。
# ============================================================
import json
import logging
import re
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_PROFILE_KEY_PREFIX = "profile:"

# 关注点词表（用户画像素材）：园区项目名 + 服务话题
_TOPIC_WORDS = [
    "旋转木马", "荡秋千", "过山车", "蹦床", "浴室大作战", "大炮乐园",
    "办卡", "充值", "退卡", "门票", "营业时间", "天气", "雨天", "下雨",
    "失物", "陪同", "身高", "安全",
]
_ATTRACTION_WORDS = ["旋转木马", "荡秋千", "过山车", "蹦床", "浴室大作战", "大炮乐园"]

_RE_AGE = re.compile(r"(\d{1,2})\s*岁")
_RE_HEIGHT = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*米|(\d{3})\s*[cｃ]m")


def _normalize_session(session_id: str) -> str:
    return (session_id or "").strip() or "default"


# ============================================================
# 会话 / 消息持久化
# ============================================================

def ensure_session(session_id: str, user_id: Optional[int] = None) -> str:
    """确保会话存在（惰性建行），返回规范化 session_id"""
    sid = _normalize_session(session_id)
    if sid == "default":
        return sid
    try:
        from database.db import create_session
        create_session(sid, user_id=user_id, title="AI 对话")
    except Exception as e:
        logger.warning(f"ensure_session 失败: {e}")
    return sid


def remember_exchange(session_id: str, user_id: Optional[int], question: str,
                      answer: str, ref_docs: Optional[List] = None,
                      search_query: str = "") -> None:
    """把一轮问答以结构化消息写入 chat_messages（失败不影响主流程）"""
    sid = _normalize_session(session_id)
    if sid == "default" or not question or not answer:
        return
    try:
        from database.db import add_message, update_session
        # 用户问题
        add_message(sid, "user", content=question[:2000], question=question[:500],
                    user_id=user_id, intent="chat")
        # AI 回答（携带检索改写与来源，供后续溯源）
        add_message(sid, "assistant", content=answer[:4000], answer=answer[:4000],
                    ref_docs=(ref_docs or [])[:10], search_query=search_query[:500],
                    user_id=user_id)
    except Exception as e:
        logger.warning(f"remember_exchange 落库失败: {e}")


def load_context(session_id: str, user_id: Optional[int] = None, limit: int = 12,
                 query: str = "") -> Dict:
    """
    读取服务端结构化历史 + 用户画像。
    返回: {"history_text": str,       # "用户：…\nAI：…\n" 供 Query 改写/地图预判
           "messages": [{"role","content","question","ref_docs","search_query"}],
           "profile": {...}, "profile_note": str, "profile_source": "user"|"session"|""}

    画像按 **user 级优先**：给了 user_id 且命中就**不再查会话级**（DB 往返净增 0），
    只有没命中（游客 / 从未答过）才回落到会话级画像。

    query（C3 工作记忆）：本轮问句，只用来决定"要不要注入偏好类信息"（纯内存判断，
    不新增任何 IO）。不传 = 保持全量注入的旧行为。
    """
    sid = _normalize_session(session_id)
    out = {"history_text": "", "messages": [], "profile": {}, "profile_note": "",
           "profile_source": ""}
    try:
        from database.db import get_messages
        rows = get_messages(sid, limit=limit * 2) if sid != "default" else []
    except Exception as e:
        logger.warning(f"load_context 失败: {e}")
        rows = []
    user_msgs = [r for r in rows if r.get("role") in ("user", "assistant")]
    history_lines = []
    for r in user_msgs[-limit:]:
        if r.get("role") == "user":
            history_lines.append(f"用户：{r.get('content', '')}")
        else:
            history_lines.append(f"AI：{r.get('content', '')}")
        out["messages"].append({
            "role": r.get("role"),
            "content": r.get("content", ""),
            "question": r.get("question", ""),
            "ref_docs": r.get("ref_docs") or [],
            "search_query": r.get("search_query", ""),
        })
    out["history_text"] = "\n".join(history_lines)
    if history_lines:
        out["history_text"] += "\n"

    # 画像：user 级优先（跟着账号走，换设备不丢）；没命中才读会话级
    note, prof, src = "", {}, ""
    if user_id:
        try:
            from services import user_profile as _up
            hit = _up.load_for_context(user_id, query)
            note = hit.get("note") or ""
            prof = hit.get("profile") or {}
            src = "user" if note else ""
        except Exception as e:
            logger.warning(f"user 级画像读取失败（回落会话级）: {e}")
    if not note:
        prof = load_profile(sid)
        note = render_profile(prof)
        src = "session" if note else ""
    out["profile"] = prof
    out["profile_note"] = note
    out["profile_source"] = src
    return out


# ============================================================
# 用户画像（agent_config key-value 落库 + 文本规则抽取）
# ============================================================

def load_profile(session_id: str) -> Dict:
    sid = _normalize_session(session_id)
    try:
        from database.db import get_config
        raw = get_config(_PROFILE_KEY_PREFIX + sid, default=None)
        if isinstance(raw, str):
            data = json.loads(raw)
        else:
            data = raw
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"heights": [], "ages": [], "kids": 0, "topics": [], "rain": False}


def save_profile(session_id: str, profile: Dict) -> None:
    sid = _normalize_session(session_id)
    try:
        from database.db import set_config
        set_config(_PROFILE_KEY_PREFIX + sid, json.dumps(profile, ensure_ascii=False),
                   description="AI 会话用户画像（自动抽取）")
    except Exception as e:
        logger.warning(f"save_profile 失败: {e}")


def extract_profile_from_text(text: str) -> Dict:
    """从一段对话文本抽取画像候选（本地规则，零 LLM 开销）"""
    p = {"heights": [], "ages": [], "kids": 0, "topics": [], "rain": False}
    if not text:
        return p
    for m in _RE_HEIGHT.finditer(text):
        val = m.group(1) or m.group(2)
        if val and val not in p["heights"]:
            p["heights"].append(float(val) if m.group(1) else int(val))
    for m in _RE_AGE.finditer(text):
        a = int(m.group(1))
        if a not in p["ages"]:
            p["ages"].append(a)
    p["kids"] = max(len(re.findall(r"孩子|儿童|小孩|小朋友", text)) or 0,
                    sum(1 for a in p["ages"] if a <= 14))
    if any(w in text for w in ("雨", "下雨")):
        p["rain"] = True
    for w in _ATTRACTION_WORDS + ["办卡", "充值", "门票", "失物", "退卡"]:
        if w in text:
            p["topics"].append(w)
    return p


def merge_profile(old: Dict, new: Dict) -> Dict:
    merged = {"heights": [], "ages": [], "kids": 0, "topics": [], "rain": False}
    merged["heights"] = list(dict.fromkeys(old.get("heights", []) + new.get("heights", [])))
    merged["ages"] = sorted(set(old.get("ages", []) + new.get("ages", [])))
    merged["kids"] = max(int(old.get("kids", 0) or 0), int(new.get("kids", 0) or 0))
    merged["topics"] = list(dict.fromkeys(old.get("topics", []) + new.get("topics", [])))[:12]
    merged["rain"] = bool(old.get("rain") or new.get("rain"))
    return merged


def update_profile_from_exchange(session_id: str, texts: List[str]) -> Dict:
    """用一轮对话（问+答）增量更新画像并落库"""
    fresh = {"heights": [], "ages": [], "kids": 0, "topics": [], "rain": False}
    for t in texts:
        one = extract_profile_from_text(t or "")
        fresh = merge_profile(fresh, one)
    cur = load_profile(session_id)
    merged = merge_profile(cur, fresh)
    if merged != cur:
        save_profile(session_id, merged)
    return merged


def render_profile(profile: Dict) -> str:
    """画像 → 一行可读摘要（注入改写/生成/业务提示）"""
    if not profile or not any(profile.values()):
        return ""
    parts = []
    if profile.get("ages"):
        parts.append("孩子年龄：" + "、".join(f"{a}岁" for a in profile["ages"]))
    if profile.get("heights"):
        parts.append("孩子身高：" + "、".join(f"{h}米" for h in profile["heights"]))
    if profile.get("kids"):
        parts.append(f"随行儿童约 {profile['kids']} 名")
    if profile.get("rain"):
        parts.append("用户关心雨天/天气")
    if profile.get("topics"):
        parts.append("关注：" + "、".join(profile["topics"][:6]))
    return "；".join(parts) if parts else ""


def _mention_profile(note: str) -> str:
    if not note:
        return ""
    return (f"\n【用户档案（来自之前的对话，供个性化参考，若与本轮无关请忽略）】{note}\n"
            "若用户提到孩子身高/年龄与项目限制相关，请友善提醒资料未标注的项目以现场工作人员要求为准。")


def build_prompt_suffix(profile_note: str) -> str:
    """返回可拼接到问题文本后的画像提示（Query 改写 / 生成阶段共用）"""
    return _mention_profile(profile_note)


# ============================================================
# 主动追问（基于刚生成的回答，给出上下文相关的下一步问题）
# ============================================================

_RULE_DEFAULT = ["办卡充值划算吗", "营业时间是什么时候", "哪些项目适合小朋友"]


def suggest_followups(question: str, answer: str, profile_note: str = "", n: int = 3) -> List[str]:
    """
    回答完成后，依据"**本轮刚生成的回答** + 用户问题 + 画像"生成 n 条追问建议。

    要点：建议必须紧扣刚才那条回答里出现的具体信息（项目名/价格/卡档位/规则/流程步骤），
    而不是套用与回答无关的通用问题——否则用户会觉得"答非所问"。
    失败/超时返回 []（由上层用规则化建议兜底）。
    """
    try:
        from core.llm_client import llm_client
        profile_line = f"\n用户画像：{profile_note}" if profile_note else ""
        prompt = (
            f"你是园区问答助手的追问策划。下面是一轮刚刚结束的问答{profile_line}。\n"
            f"请推断用户看完**这段回答**之后，最可能接着问的 {n} 个问题。\n"
            f"【硬性要求】\n"
            f"1. 每个问题都必须与【我的回答】里出现的具体信息直接相关（例如回答里提到的"
            f"某个项目名、价格、卡档位、办理步骤、闭园条件、失物流程等），"
            f"可以顺着它继续深挖细节、追问例外情况或询问下一步操作；\n"
            f"2. 严禁给出与回答内容无关的通用问题（如回答在讲蹦床票价，就不要问儿童安全、办卡优惠）；\n"
            f"3. 每个问题不超过 15 个字，口语化，不要编号、不要引号、不要解释。\n\n"
            f"【用户刚才问】{question[:300]}\n"
            f"【我的回答】{answer[:600]}\n"
            f"【追问列表（每行一个）】"
        )
        result = llm_client.invoke(prompt)
        items = []
        for ln in result.splitlines():
            ln = ln.strip().strip('"\'。，').strip()
            if not ln:
                continue
            for pre in ("1.", "2.", "3.", "4.", "5.", "-", "•", "（", "("):
                if ln.startswith(pre):
                    ln = ln[len(pre):].strip()
            if ln and ln not in items and len(ln) <= 40:
                items.append(ln)
            if len(items) >= n:
                break
        # 质量闸门：至少有一半（向上取整）要与回答内容有实词重合，否则视为跑偏 → 交给规则兜底
        if items and _answer_overlap_ok(items, answer):
            return items
        if items:
            logger.info(f"LLM 追问与回答重合度过低，改用规则化建议: {items}")
        return []
    except Exception as e:
        logger.warning(f"生成追问失败: {e}")
        return []


# 追问质量校验时忽略的通用疑问词（它们出现在任何问题里，不能作为"与回答相关"的证据）
_FOLLOWUP_STOP = {
    "多少钱", "怎么样", "可以吗", "有没有", "是不是", "能不能", "是什么", "哪些",
    "什么", "怎么", "为什么", "多久", "几岁", "几个", "一次", "还有", "另外",
    "适合", "可以", "需要", "多少", "如何", "以及", "问题", "情况", "时候",
}


def _strip_sources(text: str) -> str:
    """去掉回答里的来源标注，避免"来源文档名"（如"…收费与办卡规则知识库"）
    被当成话题关键词，生成与回答无关的追问（例：问"几点关门"却推荐"办卡"相关追问）。"""
    t = text or ""
    t = re.sub(r"【来源文档】[^\n]*", " ", t)
    t = re.sub(r"【来源】[^\n]*", " ", t)
    return t


def _answer_overlap_ok(items: List[str], answer: str, min_ratio: float = 0.5) -> bool:
    """粗校验：建议里出现的实词是否能在回答中找到（避免"答非所问"的建议直接上屏）

    例：回答讲"蹦床票价40元"，建议"蹦床适合几岁孩子"含实词"蹦床"→ 命中；
    而"儿童票价有优惠吗"的实词（儿童/票价/优惠）在回答里都没有 → 判为跑偏。
    """
    try:
        ans = _strip_sources(answer)
        if not ans.strip():
            return True
        import jieba
        try:
            from core.keywords import ensure_business_words
            ensure_business_words()
        except Exception:
            pass
        need = max(1, int(len(items) * min_ratio + 0.999))
        hit = 0
        for it in items:
            toks = [w.strip() for w in jieba.lcut(it)
                    if len(w.strip()) >= 2 and w.strip() not in _FOLLOWUP_STOP]
            if any(t in ans for t in toks):
                hit += 1
        return hit >= need
    except Exception:
        return True


def _entities_from_text(text: str, limit: int = 3) -> List[str]:
    """从回答文本里提取业务实体（项目名/卡档位等），用于规则化追问"""
    found: List[str] = []
    for keys, _label, _icon in _TOPIC_HINTS:
        for k in keys:
            if k in (text or "") and k not in found:
                found.append(k)
                break
        if len(found) >= limit:
            break
    return found


# 规则化追问用的主题词（从"提问+回答"里找实体；比只看提问更贴近刚才的回答）
_TOPIC_HINTS = [
    (("旋转木马", "荡秋千", "过山车", "蹦床", "浴室大作战", "大炮乐园"), "项目", "🎢"),
    (("200元", "500元", "1000元", "10币", "20币", "30币", "储值卡", "会员卡"), "卡档", "💳"),
    (("退卡", "退款", "退币"), "退卡", "💰"),
    (("营业时间", "闭园", "开放时间", "夜场"), "营业", "🕐"),
    (("签到", "奖励币", "余币", "余额"), "余币", "🪙"),
    (("失物", "认领", "报失"), "失物", "🧸"),
    (("陪同", "身高", "年龄", "儿童"), "儿童", "👶"),
    (("安全", "看护", "受伤"), "安全", "⚠️"),
]


def rule_based_followups(question: str, answer: str = "", n: int = 3) -> List[str]:
    """规则化追问建议（零延迟兜底）：**结合提问与刚生成的回答**生成。

    优先用回答里出现的实体（项目名/卡档位/规则主题）拼出贴合回答的追问；
    没有任何实体可用时，再退回"常见问法库"的相近问法。
    注意：匹配前先剥掉"【来源文档】"标注，否则文档名里的词（办卡/收费…）会污染判断。
    """
    q = (question or "").strip()
    a = _strip_sources(answer).strip()
    text = f"{q} {a}"

    found: List[str] = []
    # 1) 回答/提问里的项目名 → 追问该项目规则
    projects = [k for k in ("旋转木马", "荡秋千", "小型过山车", "过山车", "蹦床", "浴室大作战", "大炮乐园")
                if k in text]
    if projects:
        p = projects[0]
        found += [f"{p}适合几岁孩子", f"{p}一次能玩多久"]
    # 2) 卡档/退卡 → 追问卡规则
    if any(k in text for k in ("储值卡", "会员卡", "元得", "充值", "办卡")):
        found += ["卡里的币会过期吗", "办卡后怎么使用"]
    if any(k in text for k in ("退卡", "退款")):
        found += ["退卡要收手续费吗", "退款多久到账"]
    # 3) 营业/关门时间 → 追问时间规则
    if any(k in text for k in ("营业时间", "闭园", "开放时间", "夜场", "关门", "开门", "几点")):
        found += ["节假日时间一样吗", "恶劣天气会闭园吗"]
    # 4) 余币/签到
    if any(k in text for k in ("签到", "余币", "余额", "奖励币")):
        found += ["余币会过期吗", "签到奖励怎么用"]
    # 5) 失物
    if any(k in text for k in ("失物", "认领", "报失")):
        found += ["失物招领处在哪里", "多久没人认领会处理"]
    # 6) 儿童/安全
    if any(k in text for k in ("陪同", "身高", "年龄", "儿童", "安全", "看护")):
        found += ["哪些项目家长可以陪同", "园内有医务室吗"]

    # 兜底：问法库里与当前问题最相近的问法
    if len(found) < n:
        try:
            from agents.query_agent import query_agent
            _sq, matches = query_agent.match_question(q, top_k=n + 2)
            for m in matches or []:
                pat = (m.get("pattern") or "").strip()
                if pat and pat != q and len(pat) <= 20 and pat not in found:
                    found.append(pat)
                if len(found) >= n:
                    break
        except Exception:
            pass

    out: List[str] = []
    for i in found:
        if i != q and i not in out:
            out.append(i)
    return out[:n]


def suggest_followups_with_timeout(question: str, answer: str, profile_note: str = "",
                                   n: int = 3, timeout: float = 6.0) -> List[str]:
    """
    追问建议（带软超时，用于"不阻塞回答收尾"）：
    - 后台线程跑 LLM 版追问，最多等 timeout 秒（默认 6s，因为 done 事件已先发出，不影响体感）；
    - 未及时返回（或关闭/失败/质量校验不过）→ 立即用规则化建议（结合回答内容）兜底。
    """
    from config import SUGGEST_LLM_ENABLED
    if not SUGGEST_LLM_ENABLED:
        return rule_based_followups(question, answer, n)
    import threading
    box: dict = {}

    def _work():
        try:
            box["items"] = suggest_followups(question, answer, profile_note, n)
        except Exception:
            box["items"] = []

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(timeout=max(0.0, timeout))
    items = box.get("items") or []
    if not items:
        items = rule_based_followups(question, answer, n)
    return items[:n]


# ============================================================
# 顶层便捷入口（供 Orchestrator 调用）
# ============================================================

def prepare_session(session_id: str, user_id: Optional[int]) -> str:
    return ensure_session(session_id, user_id)


def remember_turn(session_id: str, user_id: Optional[int], question: str, answer: str,
                  ref_docs: Optional[List] = None, search_query: str = "") -> None:
    """一轮问答结束：确保会话行 → 落库结构化消息 → 增量抽取画像

    ⚠️ user 级画像的刷新必须留在**后台线程**里：本函数由 orchestrator 在 SSE 的
    done 之前调用，在这里同步写库会直接拖慢模型响应（硬约束）。
    """
    sid = _normalize_session(session_id)
    if sid == "default" or not answer:
        return
    try:
        ensure_session(sid, user_id)
    except Exception as e:
        logger.warning(f"ensure session failed: {e}")
    remember_exchange(sid, user_id, question, answer, ref_docs, search_query)
    session_profile = None
    try:
        # ★ 只吃「用户说的话」（N1）。原先把 answer 一起喂进来，等于让 AI 自己的回答
        #   参与"用户画像"的抽取：回答里引用的知识库文档标题（"…3岁能玩吗"）会凭空
        #   长出孩子，模型推荐过的项目会被记成"用户关注" → 自我强化污染。
        session_profile = update_profile_from_exchange(sid, [question])
    except Exception as e:
        logger.warning(f"画像更新失败: {e}")
    if user_id:
        _refresh_user_profile_async(user_id, question, answer, session_profile)


def _refresh_user_profile_async(user_id: Optional[int], question: str, answer: str,
                               session_profile: Optional[Dict] = None) -> None:
    """把 user 级画像刷新丢到后台线程：不占请求线程，也不进问答关键路径。

    顺带把游客期攒下的会话级画像并进 user 级（登录后不至于从零开始）。
    """
    def _work():
        try:
            from services import user_profile as _up
            _up.refresh_from_turn(user_id, question, answer, session_profile)
        except Exception as e:
            logger.warning(f"user 级画像后台刷新失败（已忽略）: {e}")

    try:
        import threading
        threading.Thread(target=_work, daemon=True).start()
    except Exception as e:
        logger.warning(f"画像后台线程启动失败（已忽略）: {e}")
