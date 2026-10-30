# -*- coding: utf-8 -*-
"""
用户画像（user 级）—— 全项目唯一核心

定位：把「AI 记得我」从会话级（换设备就丢）升级为用户级（跟着账号走），
同一份数据供小程序「我的 → AI 记得我」与 AI 问答侧个性化共用。

三条铁律（改之前先读）：
1. **写入只发生在 core.chat_memory.remember_turn 的后台线程里**。收尾逻辑跑在
   SSE 的 done 之前，在这里同步写库会直接拖慢模型响应——这是硬约束，别改成同步。
   热路径读取只走 `load_note()`：一次单行查询，失败一律降级为空串。
2. **manual 分区与自动抽取物理隔离**。用户手改的字段存 `manual`，自动抽取只写
   `family` / `interest`，读取时 `manual` 覆盖自动值（`effective_view()`）。
   于是"自动抽取覆盖用户手改值"这件事在结构上不可能发生，不需要靠合并逻辑去防守。
3. **手机号 / wx_openid 绝不写进画像 JSON**（手机号属敏感个人信息）。画像里只有
   孩子年龄段、身高、性别、关注项目与话题、最近问过的问题。
4. **孩子按「每个孩子一条」存**（v3 起）：`family.kids = [{age, height, gender}]`。
   `kids_ages` / `kids_heights` / `kids_count` 是给旧读取口径的**派生兼容字段**，
   只在输出时由 `_derive_flat()` 生成 —— 不要让它们变成第二个真源。
   为什么必须成组：平行列表回答不了「1.2 米的是哪个孩子」，模型就说不出
   「你家 6 岁的女儿适合玩蹦床」。
5. **只从「用户说的话」里抽，不从 AI 的回答里抽**（`refresh_from_turn` 只吃 question）。
   回答文本里带着知识库文档标题（「…3岁能玩吗」）和模型自己推荐过的项目名，
   吃进画像会形成**自我强化污染**：模型推什么 → 画像记成"用户喜欢" → 下轮推得更狠。
6. **负向偏好必须单独存**（`interest.dislikes`），不得与正向混在一个列表里。
   用户说「不喜欢旋转木马」若被记成"关注项目"，模型就会主动推它并给它背书 ——
   这是本项目里唯一会"主动做错事"的缺陷。

抽取口径与 session 级画像（core/chat_memory.py）同源：纯正则、零 LLM、零额外延迟。
"""
import copy
import logging
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from database.db import (
    delete_user_profile as _db_delete_profile,
    delete_user_messages as _db_delete_messages,
    get_user_profile as _db_get_profile,
    upsert_user_profile as _db_upsert_profile,
)

logger = logging.getLogger(__name__)

# 画像写入串行化（进程内）。为什么必须有：
#   写路径是「读整行 → 改 → 写回整行」的 read-modify-write，而它跑在**后台线程**里。
#   一轮连发两个问题时，A 轮的线程可能在 B 轮之后才写回，把 B 的结果整段盖掉
#   （典型"丢更新"）；用户手改/清除与后台写撞上时同理（手改刚存就被抹）。
#   写本身只 ~15ms 且完全在关键路径之外，串行化的代价可以忽略。
#   注：单进程单 worker 部署下这就够了；多 worker 需要行级版本号做 CAS（见 _record_version 的 rev）。
_WRITE_LOCK = threading.RLock()

PROFILE_VERSION = 4

# ------------------------------------------------------------------
# 写入规范（C5 · 与 GLM Memory Store 的挂载 instructions 同思路）
# ------------------------------------------------------------
# 官方最佳实践是"用 instructions 立规矩，比事后清理更有效"。这里把"什么值得记、
# 记到哪个分区"显式写出来，代码与文档同源 —— 新人不必读完 200 行合并逻辑才知道规则。
#
#   记         → 分区
#   孩子年龄/身高/性别（每个孩子一条）      family.kids
#   同行儿童人数（「N 个孩子」才对，不数碎片） family.kids_count
#   明确喜欢的项目（「喜欢蹦床」）           interest.projects
#   明确排斥的项目（「不喜欢旋转木马」）      interest.dislikes
#   办卡/退卡/失物/天气等话题              interest.topics
#   最近问过的问题（滚动 5 条）             interest.recent_questions
#
#   绝不记     → 原因
#   手机号 / wx_openid                   敏感个人信息（铁律 3）
#   AI 回答里的内容                      自我强化污染（铁律 5）
#   模型推荐过的项目                      那是模型说的，不是用户说的
#   用户手改过的字段                      手改走 manual，自动抽取不许覆盖（铁律 2）
# ------------------------------------------------------------------
WRITE_RULES = {
    "family": ("孩子年龄/身高/性别（每个孩子一条）", "同行儿童人数"),
    "interest": ("明确喜欢的项目", "明确排斥的项目", "办卡/退卡/失物/天气等话题",
                 "最近问过的问题"),
    "never": ("手机号", "wx_openid", "AI 回答里的内容", "模型推荐过的项目",
              "用户手改过的字段"),
}

# 项目词表：以结构化价目表为准，导不动时退回内置副本（避免重依赖拖慢导入）
try:
    from tools.business_tools import ATTRACTIONS
    PROJECT_WORDS: List[str] = [a["name"] for a in ATTRACTIONS if a.get("name")]
except Exception:
    PROJECT_WORDS = ["旋转木马", "荡秋千", "小型过山车", "蹦床", "浴室大作战", "大炮乐园"]

# 话题词表（与 core/chat_memory.py 的 session 级口径保持一致）
TOPIC_WORDS = ["办卡", "充值", "退卡", "门票", "营业时间", "天气", "下雨", "失物", "陪同", "身高", "安全"]

# 数值边界：正则很容易把"100 米"当身高、"120 岁"当年龄，一律卡在合理区间内
_MIN_HEIGHT, _MAX_HEIGHT = 0.5, 1.9
_MIN_AGE, _MAX_AGE = 1, 18

_RE_AGE = re.compile(r"(?<!\d)(\d{1,2})\s*岁")
_RE_HEIGHT_M = re.compile(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*米")
_RE_HEIGHT_CM = re.compile(r"(?<!\d)(\d{3})\s*(?:cm|CM|厘米|公分)")
_RE_KIDS = re.compile(r"孩子|儿童|小孩|小朋友")

# 性别词表：命中即归入 男 / 女（"小女孩""儿子"这类都是子串命中）
_GENDER_WORDS = (("女", ("女孩", "女儿", "女宝", "闺女", "孙女", "女娃")),
                 ("男", ("男孩", "儿子", "男宝", "孙子", "男娃")))
_RE_GENDER = re.compile("|".join(w for _, ws in _GENDER_WORDS for w in ws))

# 中文数字孩子数：「我有两个孩子」原来按"数『孩子』出现次数"算，会错成 1
_CN_NUM = {"一": 1, "两": 2, "俩": 2, "二": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_RE_KIDS_COUNT = re.compile(r"(?:(\d+)|([一两俩二三四五六七八九]))\s*(?:个|名|位|口)?\s*"
                            r"(?:孩子|儿童|小孩|小朋友|娃|宝宝)")

# 「我有几个孩子？」是**提问**，不是陈述。旧实现的兜底（数「孩子」出现次数）
# 会把它记成"有 1 个孩子"，下一轮画像直答就会自信地回「你告诉过我 1 个孩子」。
_RE_ASK_KIDS_COUNT = re.compile(
    r"(?:几|多少)\s*(?:个|名|位|口)?\s*(?:孩子|儿童|小孩|小朋友|娃|宝宝)"
    r"|(?:孩子|儿童|小孩|小朋友|娃|宝宝)\s*(?:有几个|有多少|几个)")

# 句读切分：把「一个6岁女孩1.2米，一个8岁男孩」拆成两条孩子记录
_RE_CLAUSE = re.compile(r"[,，、;；。!！?？~～\n]+")

# 枚举词：「一个男孩一个女孩」「两个孩子」这类**逐个点名**的说法。
# 为什么需要它：一小句里出现 n 个枚举词 ⇒ 这一小句描述了 n 个孩子。
# 旧实现只用 max(len(ages), len(heights), len(genders)) 决定片段条数，
# 于是「一个男孩一个女孩」（0 年龄 0 身高 2 性别）虽然恰好出 2 条，但两条都不带
# _new，第 1 条就去跟画像里已有的孩子比对了 —— 实测「我女儿6岁」+「一个男孩一个女孩」
# 只剩 1 个孩子。见 _probe_trip_planning.py C2/C6 反例。
# 注意「孩子」本身**不**算枚举词：它只是类别名词（「我家孩子6岁」是一个孩子）。
_RE_ENUM = re.compile(r"(?:一|两|俩|二|三|四|五|六)\s*(?:个|名|位)\s*"
                      r"(?:男|女|男孩|女孩|儿子|女儿|孩子|儿童|小孩|小朋友|娃|宝宝|大|小)")

# ------------------------------------------------------------------
# 孩子的**指认词**（"哪个孩子"）：只有出现指认词，才敢把某个喜好挂到某个孩子头上。
# ------------------------------------------------------------
# 为什么必须要求指认：用户说「我家孩子喜欢蹦床」时根本没说哪个孩子；
# 硬挂到"第一个孩子"头上就是编 —— 下一轮模型会拿这个假归属去推项目。
# **判不出归属 → 落回账号级 interest.projects（现状行为），宁可粗不可错。**
#
# 三类指认（按可靠度排序）：
#   1) 序位词：老大/老二/老三 = 按孩子顺序位 0/1/2；弟弟/妹妹/哥哥/姐姐/大宝/二宝/小宝
#      也常被用来指位次，但它同时是 `_RE_NEW_CHILD` 的新增线索 —— 只在**已有 2 个以上
#      孩子**时才当序位用（新家庭第一次说「弟弟」是在描述一个还没入库的孩子）。
#   2) 年龄/身高锚点：「6 岁那个」「1米2 的」→ 按标量字段匹配 kids。
#   3) 性别锚点：「女儿喜欢…」「儿子不喜欢…」→ 按性别匹配；**只有唯一匹配**才敢挂。
# ------------------------------------------------------------------

# 序位词 → 顺序位（0 = 老大）
_ORDINALS = {
    "老大": 0, "大宝": 0, "大的": 0,
    "老二": 1, "二宝": 1, "小的": -1, "小宝": -1,
    "老三": 2,
}
# 「弟弟 / 妹妹 / 哥哥 / 姐姐」：既能指位次、也能表示"新增一个孩子"，语义靠孩子数量定
_RELATIVE_WORDS = ("弟弟", "妹妹", "哥哥", "姐姐")
# 指认后跟的"那个 / 的"等收尾词（「6岁那个喜欢…」）
_RE_REF_TAIL = re.compile(r"^(?:那个|这个|的|一个|孩子|娃|宝宝)")

# 症状 / 注意事项：只在**明确是选填的性格描述**时抽取。
# 这里刻意用"包含即可"的宽口径 —— 症状是用户主动交代的少数信息，
# 漏抽的代价（推荐了坐不了的项目）远大于多抽。
_RE_SYMPTOM_KW = ("过敏", "哮喘", "鼻炎", "恐高", "晕车", "晕车", "癫痫", "心脏病",
                  "自闭", "多动", "胆小", "怕黑", "怕高", "体弱", "刚做过手术",
                  "骨折", "生病", "感冒", "发烧", "皮肤", "湿疹", "扭伤", "残疾", "坐轮椅")
# 「有哮喘」「怕高」这类"症状 + 程度词"的完整片段
_RE_SYMPTOM_FRAG = re.compile(
    r"(?:有点|比较|非常|特别|轻微|严重)?\s*"
    r"(?:对\s*[\u4e00-\u9fa5]{1,6}\s*过敏|"
    r"有\s*(?:哮喘|鼻炎|癫痫|心脏病|多动症|湿疹)|"
    r"恐高|晕车|胆小|怕黑|怕高|体弱|坐轮椅|残疾|"
    r"(?:刚|最近)\s*(?:做过|做完|动过)\s*手术|"
    r"骨折|生病|感冒|发烧|扭伤)"
)

# 弹窗里给的**预置症状标签**（选填，用户可多选，也可自己补充说明）。
# 只用于 UI 建议，不做抽取词表限制 —— 用户最懂自己孩子的情况。
_SYMPTOM_OPTIONS = ("怕高", "恐高", "胆小", "怕黑", "晕车", "体质弱",
                    "过敏", "哮喘", "鼻炎", "多动", "刚做过手术")


def _kid_ordinal_map(clause: str, kids: List[Dict]) -> Optional[int]:
    """从一小句里读出"这是第几个孩子"（返回 kids 的下标）。读不出 / 不可靠 → None。

    ★ 序位按**年龄从大到小**排（老大 = 最年长的那个），不是 kids 列表的物理顺序。
      列表顺序取决于用户先说的是哪个孩子（「我家二宝3岁，大宝1岁」→ 二宝在前），
      直接拿物理下标就是错的 —— 用户嘴里的"老大"永远指最年长的那个。
      没有年龄的孩子按原顺序排在最后（信息不足，只能保守）。

    · 「弟弟/妹妹」在已有 ≥2 个孩子时才当"最小那个"用
      —— 单孩家庭说「我弟弟」说的多半是用户自己的弟弟，不该挂到孩子身上。
    """
    if not kids:
        return None
    # 按年龄降序得到"老大→老小"的真实物理下标序列
    def _sort_key(pair):
        i, k = pair
        return (0, -int(k["age"])) if k.get("age") is not None else (1, i)
    order = [i for i, _ in sorted(enumerate(kids), key=_sort_key)]
    n = len(order)
    for w, idx in _ORDINALS.items():
        if w in clause:
            pos = idx if idx >= 0 else max(0, n - 1)
            return order[pos] if pos < n else None
    for w in _RELATIVE_WORDS:
        if w in clause and n >= 2:
            # 「哥哥/姐姐」= 大一点的那个，「弟弟/妹妹」= 小一点的那个
            return order[0] if w in ("哥哥", "姐姐") else order[-1]
    return None


def _kid_scalar_map(clause: str, kids: List[Dict]) -> Optional[int]:
    """按**唯一匹配**的年龄/身高锚点定位孩子（「6岁那个」「1米2的」）。

    只在"恰好一个孩子命中"时返回下标：两个孩子都是 1.2 米时说「1米2的」等于没指认。
    """
    ages = _ages_in(clause)
    heights = _heights_in(clause)
    idxs: set = set()
    for a in ages:
        hit = {i for i, k in enumerate(kids) if k.get("age") is not None and int(k["age"]) == a}
        if len(hit) == 1:
            idxs |= hit
    for h in heights:
        hit = {i for i, k in enumerate(kids)
               if k.get("height") is not None and abs(float(k["height"]) - h) < 0.01}
        if len(hit) == 1:
            idxs |= hit
    return next(iter(idxs)) if len(idxs) == 1 else None


def _kid_gender_map(clause: str, kids: List[Dict]) -> Optional[int]:
    """按性别指认（「女儿喜欢…」）—— **只有唯一匹配**才返回，否则视为没指认"""
    gs = _genders_in(clause)
    if len(gs) != 1:
        return None
    hit = [i for i, k in enumerate(kids) if k.get("gender") == gs[0]]
    return hit[0] if len(hit) == 1 else None


def _locate_kid(clause: str, kids: List[Dict]) -> Optional[int]:
    """一小句里的喜好该挂到哪个孩子 → kids 下标；判不出返回 None（落账号级）"""
    if not kids:
        return None
    # 1) 序位词最可靠（「老二」永远指第 2 个孩子 —— 按年龄排，见 _kid_ordinal_map）
    idx = _kid_ordinal_map(clause, kids)
    if idx is None:
        # 2) 年龄/身高锚点
        idx = _kid_scalar_map(clause, kids)
    if idx is None:
        # 3) 性别锚点（唯一匹配）
        idx = _kid_gender_map(clause, kids)
    if idx is None:
        return None
    return idx if 0 <= idx < len(kids) else None


def _extract_kid_prefs(t: str, kids: List[Dict]) -> List[Dict]:
    """把"指认过的"项目好恶 + 症状挂到对应孩子身上。

    返回 [{index, likes, dislikes, symptoms}, ...]（只含真的抽到东西的孩子）。

    ⚠️ 抽不到归属的一律**不在这里出现** —— 项目好恶由调用方写回账号级
    `interest.projects`（保持改造前行为），避免"猜归属"污染画像。
    """
    if not any(k.get("age") is not None or k.get("height") is not None
               or k.get("gender") for k in kids):
        return []
    solo = len(kids) == 1
    clauses = _clauses_with_offset(t)
    agg: Dict[int, Dict[str, List[str]]] = {}

    def _slot(idx: int) -> Dict[str, List[str]]:
        return agg.setdefault(idx, {"likes": [], "dislikes": [], "symptoms": []})

    def _locate(clause: str) -> Optional[int]:
        idx = _locate_kid(clause, kids)
        if idx is None and solo:
            # 单孩家庭还要看这一小句是不是在说孩子（「我今年30岁」不是孩子）
            if _RE_KIDS.search(clause) or _RE_GENDER.search(clause) or not _RE_AGE.search(clause):
                idx = 0
        return idx

    # 1) 项目好恶
    for h in _scan_projects(t):
        clause, c_start = "", 0
        for st, seg in clauses:
            if st <= h["start"] < st + len(seg):
                clause, c_start = seg, st
                break
        if not clause:
            continue
        # ★ 按项目在小句内的**相对位置**判态度，而不是整句一刀切：
        #   「6岁的喜欢蹦床不喜欢旋转木马」里，蹦床取"喜欢"、旋转木马取"不喜欢"
        pol = _polarity_at(clause, h["start"] - c_start)
        if pol == "neutral":
            continue        # 只是提到（「旋转木马适合我女儿玩吗」）→ 不算喜好
        idx = _locate(clause)
        if idx is None:
            continue
        slot = _slot(idx)
        key = "dislikes" if pol == "neg" else "likes"
        if h["name"] not in slot[key]:
            slot[key].append(h["name"])

    # 2) 症状（「老大有哮喘」「妹妹怕高」）—— 必须句里有指认词才挂
    for st, seg in clauses:
        if not seg:
            continue
        syms = _extract_symptoms(seg)
        if not syms:
            continue
        idx = _locate(seg)
        if idx is None:
            continue
        slot = _slot(idx)
        for s in syms:
            if s not in slot["symptoms"]:
                slot["symptoms"].append(s)

    out = []
    for idx in sorted(agg):
        rec = {"index": idx, **agg[idx]}
        if any(rec[f] for f in ("likes", "dislikes", "symptoms")):
            out.append(rec)
    return out


def _extract_symptoms(t: str) -> List[str]:
    """抽"症状 / 注意事项"（选填项）。纯规则、只认固定词表 —— 不猜。

    去重口径：**互为子串的只留更具体的那个**（「有点怕高」与「怕高」是同一条）
    —— 否则画像里会并排出现两条几乎一样的注意事项。
    """
    cand: List[str] = []
    for m in _RE_SYMPTOM_FRAG.finditer(t):
        s = m.group(0).strip()[:16]
        if s:
            cand.append(s)
    for w in _RE_SYMPTOM_KW:
        if w in t and len(w) >= 2:
            cand.append(w)
    out: List[str] = []
    # 长的优先（长串包含短串时，短串不再单独保留）
    for s in sorted(dict.fromkeys(cand), key=len, reverse=True):
        if any(s in kept for kept in out):
            continue
        out.append(s)
    return out[:5]


# ------------------------------------------------------------------
# 偏好极性（正向 / 负向）：作用域是一次「句读切分」内的一小句
# ------------------------------------------------------------
# 为什么必须看作用域：原先 `[w for w in PROJECT_WORDS if w in text]` 是纯正向包含匹配，
# 于是「不喜欢旋转木马」被抽成"关注项目：旋转木马" —— 模型据此主动推它并背书。
# 判定顺序：**先看否定**（"不喜欢"里含"喜欢"，顺序反了就会判成喜欢）。
# ------------------------------------------------------------------
_NEG_WORDS = ("不喜欢", "不爱", "讨厌", "不想", "不感兴趣", "没兴趣", "别推荐",
              "不要推荐", "不用推荐", "不要再", "不要玩", "玩腻了", "不好玩",
              "没意思", "不要去", "不想玩", "拒绝")
# 明确表达"喜欢"的词 —— 只有它们能**撤销**一条历史负向偏好。
# 「玩」「推荐」这类词故意不放进来：它们只说明"提到了这个项目"，不说明喜欢，
# 否则「旋转木马玩一次多久」会把用户亲口说过的"不喜欢旋转木马"洗掉。
_POS_WORDS = ("喜欢", "最爱", "爱玩", "想玩", "要玩", "常玩", "偏好", "感兴趣",
              "喜欢玩", "爱去")
# 双重否定：「不是不喜欢 X」＝喜欢 X。中文双重否定边界情况多（「不是不喜欢」也可能
# 只是话没说完），所以只有同一小句里**同时出现肯定词**才采信，否则按"不抽取"处理。
_RE_DOUBLE_NEG = re.compile(r"(不是|并非|又不是|并不是|并没有)\s*不\s*(喜欢|爱|想|感|要)")
# 「别给我推荐 X」「不要介绍 X」这类否定是**动词短语**，不是"不喜欢"的子串，
# 单靠词表包含匹配抓不到（"别推荐" 不是 "别给我推荐" 的子串）。
_RE_NEG_ADVICE = re.compile(r"(别|不要|不用|不需要|不必|无需)\s*(给我|再)?\s*"
                            r"(推荐|推|介绍|安排|提|说)")

# 「值变了」的两种信号（C2 冲突三态，见 _merge_kids）
# 新增线索：出现即认为用户在说**另一个**孩子（"还有一个4岁弟弟"）
_RE_NEW_CHILD = re.compile(r"还有|另外|另一个|第二个|第三|老大|老二|双胞胎|"
                           r"弟弟|妹妹|哥哥|姐姐|大宝|二宝|小宝")
# 显式更正：出现即认为用户在**改口**（"我家孩子现在7岁了"）
_RE_CORRECTION = re.compile(r"不是|更正|改成|说错|改一下|记错|已经|如今|现在|"
                            r"长大|其实|应该是")
_RE_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# 用户可手改的字段白名单（PUT /profile/me 只放行这些），值为 None 表示"恢复自动"
# v3 起孩子信息统一走 kids（成组）；v4 起 dislikes 与 projects 走同一套手改口
# v5 起「喜欢的项目 / 不喜欢的项目 / 症状」都挂在 kids[i] 上（每个孩子各一份）
MANUAL_FIELDS = ("kids", "kids_count", "projects", "topics", "dislikes")
_MANUAL_TARGET = {"kids": "family", "kids_count": "family",
                  "projects": "interest", "topics": "interest",
                  "dislikes": "interest"}
# 列表字段的保留条数（recent_questions 按时间取最近 N 条）
_LIST_LIMITS = {"recent_questions": 5, "projects": 12, "topics": 12,
                "dislikes": 12, "kids": 6}

# 遗忘衰减（C4）：`_meta` 里记了 last_hit_at 的条目，超过 keep_days 就不进 note
# （**数据保留，只影响注入** → 随时可回滚）。没有 last_hit_at 的历史数据一律不隐藏。
_META_KEY = "_meta"


# ============================================================
# 基础结构
# ============================================================

def empty_profile() -> Dict:
    return {"version": PROFILE_VERSION, "family": {}, "interest": {}, "context": {}, "manual": {}}


def _as_list(v: Any) -> List:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _merge_list(old_v: Any, new_v: Any, limit: int) -> List:
    """列表合并：已有的先留、重复的挪到末尾（体现"最近命中"），最后截到 limit"""
    out = list(old_v or [])
    for x in (new_v or []):
        if x in out:
            out.remove(x)
        out.append(x)
    return out[-limit:]


# ⚠️ 这两组字段**必须分开**（2026-09-23 第 15 轮踩过）：
#   · _KID_SCALAR_FIELDS —— 「一个孩子只有一个值」的字段，供 `_conflict` / `_same_person`
#     做"同一个人改口 vs 另一个孩子"的判定。**改动它的语义等于改动合并闸门本身**。
#   · _KID_LIST_FIELDS   —— 可以并列多个值的字段（喜欢/不喜欢某项目），只做合并、
#     **绝不参与冲突判定**：两个孩子喜欢不同项目是**常态**，不是"值变了"。
# 早前写法是单一 `_KID_FIELDS` 同时供落库白名单与冲突判定两处用。若把 likes/dislikes
# 直接塞进去，`（6岁·女，喜欢蹦床）` 与 `（6岁·男，喜欢过山车）` 会被 `_conflict` 判成
# 冲突 → 误走"就地覆盖" → **第 14 轮刚调好的 C1~C14 反例集体回归**。
_KID_SCALAR_FIELDS = ("age", "height", "gender")
# 并列值字段（likes/dislikes 只放项目名；symptoms 是自由文本）—— 全部只做并集、
# **绝不参与冲突判定**（两个孩子喜欢不同项目是常态，不是"值变了"）
_KID_LIST_FIELDS = ("likes", "dislikes")
_KID_TEXT_FIELDS = ("symptoms",)
# 落库白名单 = 标量 + 列表 + 自由文本（值会一并写进 profile_json）
_KID_FIELDS = _KID_SCALAR_FIELDS + _KID_LIST_FIELDS + _KID_TEXT_FIELDS


def _kid_lists(v: Any) -> List[str]:
    """孩子身上的项目列表（likes/dislikes）→ 去空、去重、限长"""
    out: List[str] = []
    for x in (v or []):
        s = str(x or "").strip()[:12]
        if s and s not in out:
            out.append(s)
    return out[:12]


def _strip_kid_tags(kids: List) -> List[Dict]:
    """去掉抽取期的内部标记（_new / _corr 等），保证落库的就是纯数据"""
    out: List[Dict] = []
    for k in (kids or []):
        if isinstance(k, dict):
            item = {f: k.get(f) for f in _KID_SCALAR_FIELDS}
            for f in _KID_LIST_FIELDS:
                item[f] = _kid_lists(k.get(f))
            for f in _KID_TEXT_FIELDS:
                item[f] = _kid_lists(k.get(f))
            out.append(item)
    return out


def _record_supersede(meta: Dict, out: List[Dict], target: Dict, field: str,
                      old_value: Any, today: str) -> None:
    """把被覆盖的旧值记进旁路 _meta（Graphiti 的 bi-temporal：不抹掉"曾经是什么"）"""
    if not isinstance(meta, dict):      # 抽取阶段/探针不落库，没有 _meta 可写
        return
    idx = next((i for i, x in enumerate(out) if x is target), None)
    if idx is None:
        return
    rec = meta.setdefault(f"kids.{idx}", {})
    rec.setdefault(f"{field}_superseded", []).append({"v": old_value, "until": today})
    rec["last_updated"] = today


def _merge_kids(old_v: Any, new_v: Any, *, overwrite: bool = False,
                meta: Optional[Dict] = None) -> List[Dict]:
    """孩子列表合并：同一孩子的碎片互补缺项，其余追加。抽取阶段与入库阶段共用这一处。

    为什么需要它：用户是分几轮、分几个短句说孩子的。
      · 同年龄，或都没年龄且同性别 → 判为同一孩子，互补缺项
      · 没带年龄的补充碎片（「身高 1.2 米」「女孩」）→ 补到最后一个还缺这些项的孩子
    靠这两条，「我家孩子 6 岁」+「身高 1.2 米，女孩」会攒成一条完整记录，
    而不是在列表里长出两个各缺一半的半成品（那会让随行人数膨胀）。

    「值变了」怎么办（v4 / C2）：原先**任何数值更正都被当成新孩子** —— 孩子长高、
    年龄更新都会让 kids 虚高，模型于是开口就说"您家两个孩子"。现在补两档保守判定：
      · 片段带 `_new`（「还有一个4岁弟弟」）→ 一律新增（真实双孩绝不能被误合并）
      · 文本里出现 ≥2 个不同年龄（"一个6岁…一个8岁…"）→ 一律新增（同上）
      · 否则，若新值与某个孩子的**已有值**冲突，且（只有一个孩子 或 带更正词
        `overwrite=True`）→ **就地覆盖**，旧值记进 `meta`（可回溯，不销毁历史）
    """
    out = [dict(x) for x in (old_v or []) if isinstance(x, dict)]
    today = datetime.now().strftime("%Y-%m-%d")
    # 同一段话里出现两个不同年龄 = 在数两个孩子，此时禁止"就一个孩子 → 覆盖"这条推断
    ages_in_new = {k.get("age") for k in (new_v or [])
                   if isinstance(k, dict) and k.get("age") is not None}
    multi_child = len(ages_in_new) >= 2

    for k in (new_v or []):
        if not isinstance(k, dict):
            continue
        k_new = bool(k.get("_new"))
        k_corr = bool(k.get("_corr"))
        allow_overwrite = (overwrite or k_corr) and not multi_child
        _src = k                                    # 保留原始片段（含内部标记与列表字段）
        # 只有标量字段参与"是不是同一个孩子"的判定；likes/dislikes/symptoms 是**并列值**，
        # 参与判定会让"两个孩子喜欢不同项目"被误读成"改口"。
        k = {f: _src.get(f) for f in _KID_SCALAR_FIELDS}
        k_lists = {f: _kid_lists(_src.get(f)) for f in _KID_LIST_FIELDS}
        k_texts = {f: _kid_lists(_src.get(f)) for f in _KID_TEXT_FIELDS}
        if all(k[f] is None for f in k) and not any(k_lists.values()) \
                and not any(k_texts.values()):
            continue
        hit = None
        for exist in out:                       # 1) 判为同一孩子
            same_age = k["age"] is not None and exist.get("age") == k["age"]
            no_age_both = k["age"] is None and exist.get("age") is None
            same_gender = bool(k["gender"]) and exist.get("gender") == k["gender"]
            if same_age or (no_age_both and same_gender):
                hit = exist
                break
        if hit is None and k["age"] is None:    # 2) 补充型碎片
            need = [f for f in ("height", "gender") if k[f] is not None]
            if need:
                for exist in reversed(out):
                    if all(exist.get(f) is None for f in need):
                        hit = exist
                        break
        # 2.4) **同性别重复指代**（「我女儿6岁」+「带一个女孩去玩」）
        #   上面第 1 档要求 no_age_both and same_gender —— 旧片有年龄就匹配不上；
        #   第 2 档要求旧片缺该字段 —— 旧片已有性别也匹配不上；
        #   于是又走"新增孩子"，画像里长出一个 {null,null,女} 的影子（C5 反例）。
        #   ⇒ 只有性别的新片，若已有孩子**性别相同**，判为同一孩子，不新增。
        #     判在 2.5) 之前：2.5) 处理的是"性别相反"的更正，两者互斥。
        if hit is None and k["gender"] and not k_new:
            for exist in out:
                if exist.get("gender") == k["gender"]:
                    hit = exist
                    break
        # 2.5) **按性别指代的孩子**（「带一个男孩来玩」「明天带女孩去」）
        #   这类碎片只有性别、没有年龄，与画像里已存的异性孩子**每一个字段都不相等**
        #   → 上面两条都匹配不上 → 走"新增孩子" → 列表里多出一条 {null,null,男}，
        #   随后被 `out[-N:]` 截断 / 表现成"用户说了男孩，AI 一个字没提"（真实 case）。
        #
        # 只放行**极窄**的一类：新片没带旧片没有的信息，且性别明确相反。
        #   · 若新片带了 age/height 而旧片也有 → 那是两个孩子在数数（不该合并）
        #   · 若新片带了 age/height 而旧片没有 → 走下面第 3 档覆盖逻辑（会明确报冲突）
        #   · 若旧片性别为空 → 由上面第 2 档正常补上，不经过这里
        #   ⇒ 所以这一条只可能在"双方都写了性别且相反"时触发，是真正的"改口/更正"。
        if hit is None and k["gender"] and not k_new:
            for exist in out:
                if (exist.get("gender") and exist["gender"] != k["gender"]
                        and all(k[f] is None for f in _KID_SCALAR_FIELDS if exist.get(f) is not None)):
                    hit = exist
                    break
        force = False                           # 3) 「值变了」的就地覆盖
        if hit is None and not k_new:
            def _conflict(exist: Dict) -> bool:
                return any(exist.get(f) is not None and k[f] is not None
                           and exist.get(f) != k[f] for f in _KID_SCALAR_FIELDS)

            def _same_person(exist: Dict, explicit: bool) -> bool:
                """冲突是否可判为**同一个人改口**（而不是"另一个孩子"）。

                **年龄冲突**只有在用户显式改口时才允许覆盖，因为两个不同年龄
                必然是两个人（孩子不会从 6 岁变成 3 岁）：
                  · 「我家孩子现在 7 岁了」（`_corr` → explicit）      → 同一人改口 ✅
                  · 「带一个 3 岁男孩去玩」而档案是 6 岁女（无更正词） → 另一个孩子 ❌
                身高/性别冲突不受此限（长高、之前没记性别都是正常的）。

                ⚠️ 这条是同一天踩出来的（C6 反例）：旧实现只要冲突就把唯一的孩子
                **就地覆盖**，于是「我女儿6岁」+「带一个3岁男孩去玩」之后，
                画像里那个 6 岁的孩子**消失了**。宁可多留一个（用户能自己删），
                也绝不静默删掉他告诉过我们的孩子。
                """
                if explicit:
                    return True
                return not (k["age"] is not None and exist.get("age") is not None
                            and k["age"] != exist["age"])

            if allow_overwrite:
                # 带更正词（`_corr`）或 overwrite=True → 明确改口，允许覆盖
                for exist in out:
                    if _conflict(exist) and _same_person(exist, True):
                        hit = exist
                        break
            elif not multi_child and len(out) == 1 and _conflict(out[0]) \
                    and _same_person(out[0], False):
                # 同一段话里已经出现两个不同年龄（multi_child）时**禁止**这条推断：
                # 那是用户在数两个孩子，不是在更正同一个孩子的年龄
                hit = out[0]
            force = hit is not None
        if hit is None:
            new_kid = {f: k[f] for f in _KID_SCALAR_FIELDS}
            for f in _KID_LIST_FIELDS:
                if k_lists[f]:
                    new_kid[f] = list(k_lists[f])
            for f in _KID_TEXT_FIELDS:
                if k_texts[f]:
                    new_kid[f] = list(k_texts[f])
            # 内部标记随数据上抛给入库阶段使用（落库前由 _strip_kid_tags 去掉）
            if k_new:
                new_kid["_new"] = True
            if k_corr:
                new_kid["_corr"] = True
            out.append(new_kid)
            continue
        for f in _KID_SCALAR_FIELDS:
            if k[f] is None:
                continue
            if hit.get(f) is None:
                hit[f] = k[f]
            elif force and hit[f] != k[f]:
                _record_supersede(meta, out, hit, f, hit[f], today)
                hit[f] = k[f]
        # 4) 并列列表字段：**永远做并集**，不受 force 影响。
        #    为什么：两个孩子喜欢不同项目是常态而非"值变了"；手改把"喜欢蹦床"删掉后，
        #    下一轮自动抽取再提到蹦床，仍应能补回来（manual 覆盖由 effective_view 负责，
        #    这里不需要也不会去删）。
        for f in _KID_LIST_FIELDS:
            if not k_lists[f]:
                continue
            cur = _kid_lists(hit.get(f))
            for x in k_lists[f]:
                if x not in cur:
                    cur.append(x)
            hit[f] = cur
        for f in _KID_TEXT_FIELDS:
            if not k_texts[f]:
                continue
            cur = _kid_lists(hit.get(f))
            for x in k_texts[f]:
                if x not in cur:
                    cur.append(x)
            hit[f] = cur
    return out[-_LIST_LIMITS["kids"]:]


def _derive_flat(fam: Dict) -> Dict:
    """由 kids 成组数据派生 kids_ages / kids_heights（旧读取口径兼容）。

    v3 起 kids 是唯一真源，这两个扁平字段只在输出时算出来，谁都能读到、谁都不写。
    人数（kids_count）**不由这里推导** —— 它由「N 个孩子」这类文本抽取得出，
    不然一堆碎片会把随行人数撑大。
    """
    kids = [k for k in (fam.get("kids") or []) if isinstance(k, dict)]
    if not kids:
        return fam
    ages = sorted({int(k["age"]) for k in kids if k.get("age") is not None})
    heights = sorted({round(float(k["height"]), 2) for k in kids if k.get("height") is not None})
    if ages:
        fam["kids_ages"] = ages
    else:
        fam.pop("kids_ages", None)
    if heights:
        fam["kids_heights"] = heights
    else:
        fam.pop("kids_heights", None)
    # 明细条数是人数的下界（碎片已在 _merge_kids 里合并干净，不会再虚高）
    fam["kids_count"] = max(int(fam.get("kids_count") or 0), len(kids))
    return fam


def _reconcile_pref(inter: Dict, expl_pos: List[str], expl_neg: List[str],
                    conflict: List[str], meta: Dict) -> None:
    """把「偏好 / 不喜欢」两侧理顺。**负向优先**：宁可少推，不可推错。

      · 本轮明确说不喜欢的项目   → 从偏好里摘掉
      · 本轮明确说喜欢的项目     → 从不喜欢里摘掉（用户改口了，「其实我喜欢旋转木马」）
      · 只是"提到"（neutral）    → 两边都不动 —— 否则一句"旋转木马适合吗"
                                  就能把用户亲口说过的"不喜欢"洗掉
      · 仍同时出现在两侧（历史遗留 / 两轮说法矛盾）→ 按不喜欢处理，并在 _meta 记冲突
    """
    projs = list(inter.get("projects") or [])
    dis = list(inter.get("dislikes") or [])
    for x in expl_neg:
        if x in projs:
            projs.remove(x)
    for x in expl_pos:
        if x in dis:
            dis.remove(x)
    both = [x for x in projs if x in dis]
    for x in both:
        projs.remove(x)
    if both or conflict:
        rec = meta.setdefault("interest.conflict", {})
        rec["projects"] = sorted(set((rec.get("projects") or []) + both + list(conflict)))
        rec["last_seen"] = datetime.now().strftime("%Y-%m-%d")
    if projs:
        inter["projects"] = projs
    else:
        inter.pop("projects", None)
    if dis:
        inter["dislikes"] = dis
    else:
        inter.pop("dislikes", None)


def _merge_auto(old: Dict, new: Dict) -> Dict:
    """合并自动抽取结果。**只动 family / interest，绝不碰 manual。**

    另外消费三个一次性信号（由 `extract_from_text` 产出，只用于本次合并决策，
    不会写进画像）：`_explicit_pos` / `_explicit_neg` / `_pref_conflict`
    `_kid_pref_text`（v5）—— 本轮原话，用于把"明确指认给某个孩子"的好恶/症状
    从账号级挪到 `kids[i]` 上。
    """
    out = {k: copy.deepcopy(v) for k, v in (old or {}).items()}
    meta = out.setdefault(_META_KEY, {})
    meta_before = len(meta)

    src_all = new or {}
    expl_pos = [str(x) for x in (src_all.get("_explicit_pos") or [])]
    expl_neg = [str(x) for x in (src_all.get("_explicit_neg") or [])]
    conflict = [str(x) for x in (src_all.get("_pref_conflict") or [])]
    pref_text = str(src_all.get("_kid_pref_text") or "")

    for group in ("family", "interest"):
        src = src_all.get(group) or {}
        dst = out.setdefault(group, {})
        for k, v in src.items():
            if k == "kids":
                dst[k] = _strip_kid_tags(_merge_kids(dst.get(k), v, meta=meta))
            elif isinstance(v, list):
                dst[k] = _merge_list(dst.get(k), v, _LIST_LIMITS.get(k, 12))
            elif isinstance(v, (int, float)):
                dst[k] = max(int(dst.get(k) or 0), int(v))

    # ★ 孩子专属好恶 / 症状：在 kids 合并完成之后，用"全量孩子"判归属
    #   （「老大喜欢蹦床」—— 老大是谁，只有在合并后的列表里才数得清）
    if pref_text:
        _apply_kid_prefs(out, pref_text)

    _reconcile_pref(out.setdefault("interest", {}), expl_pos, expl_neg, conflict, meta)
    if len(meta) == meta_before:
        out.pop(_META_KEY, None)     # 没有可审计内容就不留空壳，画像保持干净
    return out


def _apply_kid_prefs(profile: Dict, text: str) -> None:
    """把"指认过的"项目好恶/症状**额外**挂一份到 kids[i] 上。

    ⚠️ **账号级 interest.projects 不删**（v5 定稿口径）。两个理由：
      1. 它是老契约：推荐排序、缓存指纹、标签、商家端看板都在读它。
         为了"归属"把它删掉，等于顺手改了一批下游行为（1.2 断言当时就是这样红的）。
      2. 单孩家庭本来就不存在"归属歧义"，删掉纯属倒退。
    展示层的重复由 `render_note` 负责消掉（已挂到孩子名下的项目不再在账号级列一遍），
    所以模型**看不到重复**，而数据侧两边都在 —— 信息不丢、语义不乱。
    """
    kids = [k for k in ((profile.get("family") or {}).get("kids") or [])
            if isinstance(k, dict)]
    if not kids:
        return
    for rec in _extract_kid_prefs(text, kids):
        i = rec["index"]
        if not (0 <= i < len(kids)):
            continue
        for f in _KID_LIST_FIELDS + _KID_TEXT_FIELDS:
            vals = rec.get(f) or []
            if not vals:
                continue
            cur = _kid_lists(kids[i].get(f))
            for x in vals:
                if x not in cur:
                    cur.append(x)
            kids[i][f] = cur


def effective_view(profile: Dict) -> Dict:
    """合并视图：manual 分区覆盖自动分区（manual 里为 None 表示恢复自动）"""
    view = {k: copy.deepcopy(v) for k, v in (profile or {}).items() if k != "manual"}
    man = (profile or {}).get("manual") or {}
    for k, v in man.items():
        group = _MANUAL_TARGET.get(k)
        if v is None or not group:
            continue
        view.setdefault(group, {})[k] = copy.deepcopy(v)
    if isinstance(man.get("kids"), list) and man["kids"]:
        # 手改的孩子列表就是真实人数，不再跟自动抽到的数字取最大值
        view.setdefault("family", {})["kids_count"] = len(man["kids"])
    _derive_flat(view.get("family") or {})
    return view


# ============================================================
# 抽取（零 LLM 纯正则）
# ============================================================

def _ages_in(text: str) -> List[int]:
    return sorted({int(m.group(1)) for m in _RE_AGE.finditer(text)
                   if _MIN_AGE <= int(m.group(1)) <= _MAX_AGE})


def _heights_in(text: str) -> List[float]:
    hs = set()
    for m in _RE_HEIGHT_M.finditer(text):
        v = round(float(m.group(1)), 2)
        if _MIN_HEIGHT <= v <= _MAX_HEIGHT:
            hs.add(v)
    for m in _RE_HEIGHT_CM.finditer(text):
        v = round(int(m.group(1)) / 100, 2)
        if _MIN_HEIGHT <= v <= _MAX_HEIGHT:
            hs.add(v)
    return sorted(hs)


def _genders_in(text: str) -> List[str]:
    """按出现位置返回性别，支持同段多孩（"一个男孩一个女孩"）"""
    out = []
    for m in _RE_GENDER.finditer(text):
        w = m.group(0)
        for g, words in _GENDER_WORDS:
            if w in words:
                out.append(g)
                break
    return out


def _extract_kids(t: str) -> List[Dict]:
    """按句读切段抽孩子，再用 _merge_kids 把同一孩子的碎片合起来（与入库同一套合并逻辑）。

    「我家孩子6岁，1.2米，喜欢蹦床」 → 1 条 kids（6岁 + 1.2米）
    「一个6岁女孩1.2米，一个8岁男孩」 → 2 条 kids

    片段上会带两个**抽取期内部标记**（落库前由 `_strip_kid_tags` 去掉）：
      `_new`  —— 同一小句里有「还有 / 另一个 / 弟弟」这类新增线索 → 一定是"另一个孩子"
      `_corr` —— 同一小句里有「现在 / 改成 / 不是」这类更正词   → 允许就该孩子的槽位就地覆盖
    标记必须挂在片段上（而不是靠调用方传参）：`_merge_kids` 既被抽取阶段调用，
    也被入库阶段调用，还要能被回归探针直接调用，只有"随数据走"才不会漏。
    """
    frags: List[Dict] = []
    for clause in _RE_CLAUSE.split(t):
        if not clause:
            continue
        ages = _ages_in(clause)
        heights = _heights_in(clause)
        # ★ 枚举词数（「一个男孩一个女孩」→ 两个枚举词）要参与决定片段条数，
        #   否则同一小句里两个不同性别的孩子会被下面的 i 循环拍平成一个
        #   —— 结果「一个男孩一个女孩」只留下一个孩子，这正是 C2 反例暴露的问题。
        enums = len(_RE_ENUM.findall(clause))
        genders = _genders_in(clause)
        if not (ages or heights or genders):
            continue
        n = max(len(ages), len(heights), len(genders), enums, 1)
        # 段内条数（>1）说明这一小句本身就描述了多个孩子 → 这些都是"新孩子"，
        # 必须显式带上 _new：否则第 1 条会走去重/覆盖逻辑，把已有的同龄异性孩子
        # 就地改掉（C6 反例），或被 2.5) 性别更正规则吞掉（C2 反例）。
        tag: Dict = {}
        if n > 1 or _RE_NEW_CHILD.search(clause):
            tag["_new"] = True
        if _RE_CORRECTION.search(clause):
            tag["_corr"] = True
        # 性别严格多于其余字段时，片段至少要有这么多个——把一个性别词也没有的
        # 空片段当成"这个孩子"是错的（会污染 kids 列表）
        for i in range(n):
            frag = {"age": ages[i] if i < len(ages) else None,
                    "height": heights[i] if i < len(heights) else None,
                    "gender": genders[i] if i < len(genders) else None}
            frag.update(tag)
            frags.append(frag)
    return _merge_kids([], frags)


def _extract_kids_count(t: str) -> int:
    """孩子人数：优先「N 个孩子」这类显式表述，再退回数『孩子』类词的出现次数"""
    nums = [int(m.group(1)) if m.group(1) else _CN_NUM.get(m.group(2))
            for m in _RE_KIDS_COUNT.finditer(t)]
    nums = [n for n in nums if n]
    if nums:
        return min(max(nums), 9)
    # ⚠️ 提问不当陈述：「我有几个孩子？」「家里有几个娃」→ 不推断人数。
    #    否则会被记成"有 1 个孩子"，画像直答立刻自信地答错。
    if _RE_ASK_KIDS_COUNT.search(t):
        return 0
    return min(len(_RE_KIDS.findall(t)), 9)


def _clauses_with_offset(t: str) -> List:
    """把文本按句读切成 [(start, 小句)]，用于判断项目落在哪一句话里"""
    out = []
    pos = 0
    for seg in _RE_CLAUSE.split(t):
        if not seg:
            continue
        i = t.find(seg, pos)
        if i < 0:
            i = pos
        out.append((i, seg))
        pos = i + len(seg)
    return out


def _polarity_of(clause: str) -> str:
    """一小句里对项目的态度：neg（明确排斥）/ pos（明确喜欢）/ neutral（只是提到）"""
    if _RE_DOUBLE_NEG.search(clause):
        # 「不是不喜欢 X」＝喜欢 X；但双重否定边界情况多，只有同句还有肯定词才采信
        return "pos" if any(w in clause for w in _POS_WORDS) else "neutral"
    if any(w in clause for w in _NEG_WORDS) or _RE_NEG_ADVICE.search(clause):
        return "neg"
    if any(w in clause for w in _POS_WORDS):
        return "pos"
    return "neutral"


def _polarity_at(clause: str, pos: int) -> str:
    """**按项目位置**判态度 —— 解决「6岁的喜欢蹦床不喜欢旋转木马」一句两极性。

    `_polarity_of` 是整句判定，先看否定：一句话里只要出现「不喜欢」，
    整句都判 neg → **前面那个"喜欢蹦床"被一起判成不喜欢**（实测 bug）。
    正确做法是看**离这个项目最近的那个**态度词：
      「喜欢蹦床不喜欢旋转木马」
            ↑(喜欢)      ↑(不喜欢)
      蹦床取"喜欢"，旋转木马取"不喜欢"。两边一样近时取**更靠前**的词表命中，
    因为它们修饰的通常是紧随其后的项目名。
    """
    best = None          # (距离, 是否否定)
    for w in _NEG_WORDS:
        i = clause.rfind(w, 0, pos)
        if i >= 0:
            d = pos - (i + len(w))
            if best is None or d <= best[0]:
                best = (d, True)
    for w in _POS_WORDS:
        i = clause.rfind(w, 0, pos)
        if i >= 0:
            d = pos - (i + len(w))
            if best is None or d < best[0]:
                best = (d, False)
    if best is None:
        return _polarity_of(clause)    # 项目前面没有态度词 → 退回整句判定
    if best[1]:
        # 否定也有双重否定/劝退短语的特例，交回整句判定兜底
        if _RE_DOUBLE_NEG.search(clause):
            return _polarity_of(clause)
        return "neg"
    return "pos"


def _scan_projects(t: str) -> List[Dict]:
    """找文本里被提到的项目（名称规则与下单匹配同一处实现，见 services.purchase）"""
    try:
        from services.purchase import find_projects_in_text
        return find_projects_in_text(t)
    except Exception as e:      # 依赖异常时退回旧口径，宁可少抽不可报错
        logger.warning(f"项目文本识别失败（退回包含匹配）: {e}")
        return [{"name": w, "start": t.find(w), "end": t.find(w) + len(w), "keyword": w}
                for w in PROJECT_WORDS if w in t]


def _extract_projects(t: str) -> Dict:
    """抽「用户提到的项目」并按态度分流（正向 / 负向 / 只是提到）。

    · neutral（只是提到，如「旋转木马适合我女儿玩吗」）→ projects（关注），
      **不撤销**历史 dislikes —— 否则一句"提到"就能把用户亲口说的"不喜欢"洗掉
    · pos（明确喜欢）→ projects，且可撤销历史 dislikes（用户改口了）
    · neg（明确排斥）→ dislikes（负向优先：宁可少推，不可推错）
    """
    hits = _scan_projects(t)
    if not hits:
        return {}
    clauses = _clauses_with_offset(t)
    pols: Dict[str, List[str]] = {}
    for h in hits:
        clause, c_start = "", 0
        for st, seg in clauses:
            if st <= h["start"] < st + len(seg):
                clause, c_start = seg, st
                break
        # ★ 按项目在句里的位置判极性（不是整句一刀切）：
        #   旧写法用整句判定，「喜欢蹦床不喜欢旋转木马」会被判成**蹦床也不喜欢**
        #   —— 命中"先看否定"的顺序 → 整句 neg → 两个项目都进 dislikes（实测 bug）。
        pols.setdefault(h["name"], []).append(
            _polarity_at(clause, h["start"] - c_start) if clause else "neutral")

    projects, dislikes, expl_pos, expl_neg, conflict = [], [], [], [], []
    for name, got in pols.items():
        if "neg" in got:
            dislikes.append(name)
            expl_neg.append(name)
            if "pos" in got:
                conflict.append(name)
        elif "pos" in got:
            projects.append(name)
            expl_pos.append(name)
        else:
            projects.append(name)
    return {"projects": projects, "dislikes": dislikes, "explicit_pos": expl_pos,
            "explicit_neg": expl_neg, "conflict": conflict}


def extract_from_text(text: str) -> Dict:
    """从一段文本抽取 family / interest 候选。

    返回 {"family": {...}, "interest": {...}}，另附几个**一次性信号**（下划线开头，
    `_merge_auto` 只读 family/interest，因此不会写进画像）：
      `_explicit_pos` / `_explicit_neg` —— 本轮明确表达过好恶的项目（用于撤销对侧旧值）
      `_pref_conflict`               —— 同一句里既说喜欢又说不喜欢的项目（记冲突待复核）
      `_kid_pref_text`               —— 本轮原话。**归属判定必须拿"合并前的全量孩子"做**，
                                        所以不能在这里定归属（此刻还不知道档案里已有几个孩子），
                                        交给 `_merge_auto` 在 kids 合并完成后统一挂。

    ⚠️ 只喂「用户说的话」。回答文本里带着知识库文档标题与模型自己推荐过的项目名，
    吃进来会形成自我强化污染（见模块头铁律 5）。
    """
    t = str(text or "")
    fam: Dict = {}
    inter: Dict = {}

    kids = _extract_kids(t)
    if kids:
        fam["kids"] = kids

    count = _extract_kids_count(t)
    if count:
        fam["kids_count"] = count

    pr = _extract_projects(t)
    if pr.get("projects"):
        inter["projects"] = pr["projects"]
    if pr.get("dislikes"):
        inter["dislikes"] = pr["dislikes"]
    tps = [w for w in TOPIC_WORDS if w in t]
    if tps:
        inter["topics"] = tps

    _derive_flat(fam)

    out: Dict = {}
    if fam:
        out["family"] = fam
    if inter:
        out["interest"] = inter
    if pr.get("explicit_pos"):
        out["_explicit_pos"] = pr["explicit_pos"]
    if pr.get("explicit_neg"):
        out["_explicit_neg"] = pr["explicit_neg"]
    if pr.get("conflict"):
        out["_pref_conflict"] = pr["conflict"]
    # 归属判定要拿"合并后的全量孩子"做，所以把原话带下去，由 _merge_auto 处理。
    # ⚠️ 只有本轮确实抽到了项目、或提到了孩子类词才带 —— 否则 `extract_from_text`
    #    对完全无关的话（"100米外有厕所吗"）会返回非空 dict，破坏"无关文本 → 空结果"的老契约。
    if t.strip() and (pr.get("projects") or pr.get("dislikes") or fam.get("kids")):
        out["_kid_pref_text"] = t
    return out


def _flat_session_to_kids(flat: Dict) -> Dict:
    """把 session 级的扁平画像（heights/ages/kids/topics）转成 v3 结构。

    登录用户此前在游客态攒下的画像靠它并入 user 级，避免"登录后从零开始"。
    扁平数据里没有性别，性别留 None（等后续对话抽到再补）。
    """
    fam: Dict = {}
    inter: Dict = {}
    flat = flat or {}
    ages = sorted({int(a) for a in _as_list(flat.get("ages")) if str(a).isdigit()
                   and _MIN_AGE <= int(a) <= _MAX_AGE})
    hs = []
    for h in _as_list(flat.get("heights")):
        try:
            v = round(float(h), 2)
        except (TypeError, ValueError):
            continue
        if _MIN_HEIGHT <= v <= _MAX_HEIGHT:
            hs.append(v)
    hs = sorted(set(hs))
    n = max(len(ages), len(hs))
    if n:
        fam["kids"] = [{"age": ages[i] if i < len(ages) else None,
                        "height": hs[i] if i < len(hs) else None,
                        "gender": None} for i in range(n)]
        fam["kids_count"] = n
    if flat.get("kids"):
        try:
            c = max(1, min(int(flat["kids"]), 9))
            fam["kids_count"] = max(int(fam.get("kids_count") or 0), c)
        except (TypeError, ValueError):
            pass

    topics = [str(x) for x in _as_list(flat.get("topics")) if str(x).strip()]
    projs = [x for x in topics if x in PROJECT_WORDS]
    tps = [x for x in topics if x not in PROJECT_WORDS]
    if projs:
        inter["projects"] = projs
    if tps:
        inter["topics"] = tps

    out: Dict = {}
    if fam:
        out["family"] = fam
    if inter:
        out["interest"] = inter
    return out


def _migrate(profile: Dict) -> Dict:
    """惰性升级画像结构到当前版本（v2 → v3 → v4）。

    v2 → v3：把 `kids_ages` / `kids_heights` 平行列表就地升级成 `kids` 成组。
    v3 → v4：结构不变，只把 version 推到 4 —— v4 新增的 `interest.dislikes`
             与旁路 `_meta` 都是**加法**，旧数据读到就是"没有负向偏好 / 没有审计记录"，
             不需要回溯改写（无法判断历史那句"旋转木马"当初是不是否定说法）。

    惰性迁移：只在**写入路径**（refresh_from_turn / update_manual / get_profile 的内存副本）
    调用，不刷库、不在读取热路径上做写回，保证问答耗时不受影响。
    """
    if not isinstance(profile, dict):
        return profile
    if int(profile.get("version") or 0) >= PROFILE_VERSION:
        return profile

    def _group(ages, heights, genders=None):
        ages = list(ages or [])
        heights = list(heights or [])
        genders = list(genders or [])
        n = max(len(ages), len(heights), len(genders))
        return [{"age": ages[i] if i < len(ages) else None,
                 "height": heights[i] if i < len(heights) else None,
                 "gender": genders[i] if i < len(genders) else None}
                for i in range(n)]

    fam = profile.get("family")
    if isinstance(fam, dict) and not fam.get("kids"):
        kids = _group(fam.get("kids_ages"), fam.get("kids_heights"))
        if kids:
            fam["kids"] = kids
    man = profile.get("manual")
    if isinstance(man, dict) and not man.get("kids"):
        # v2 的手改是"分字段覆盖"：只手改了身高时，年龄仍取自自动值。
        # 迁移必须复现同样的可见结果，否则会凭空把手改没提过的年龄一起变成 null。
        auto_fam = fam if isinstance(fam, dict) else {}
        kids = _group(man.get("kids_ages") or auto_fam.get("kids_ages"),
                      man.get("kids_heights") or auto_fam.get("kids_heights"))
        if kids:
            man["kids"] = kids
        for old_key in ("kids_ages", "kids_heights"):
            man.pop(old_key, None)
    profile["version"] = PROFILE_VERSION
    return profile


# ============================================================
# 展示：摘要与派生标签
# ============================================================

def _kid_desc(kid: Dict) -> str:
    """一个孩子 → 「6岁·1.2米·女」（缺项跳过）"""
    bits = []
    if kid.get("age") is not None:
        bits.append(f"{int(kid['age'])}岁")
    if kid.get("height") is not None:
        bits.append(f"{kid['height']}米")
    if kid.get("gender"):
        bits.append(str(kid["gender"]))
    return "·".join(bits)


def _kid_desc_pref(kid: Dict, idx: int) -> str:
    """一个孩子 + 他的专属好恶/症状 → 「孩子1（6岁·1.2米·女，喜欢蹦床，不喜欢旋转木马，怕高）」。

    只有该孩子**确实有专属信息**时才带上括号内容；否则退回 `_kid_desc` 的朴素写法
    —— 单孩家庭 / 没说过专属偏好的用户，注入串与改造前逐字一致（避开 53 项验收断言）。
    """
    head = f"孩子{idx + 1}（{_kid_desc(kid) or '情况未知'}"
    likes = _kid_lists(kid.get("likes"))
    dislikes = _kid_lists(kid.get("dislikes"))
    syms = _kid_lists(kid.get("symptoms"))
    if likes:
        head += "，喜欢" + "、".join(likes[:4])
    if dislikes:
        head += "，不喜欢" + "、".join(dislikes[:4])
    if syms:
        head += "，注意" + "、".join(syms[:3])
    return head + "）"


# 什么问句才需要"偏好"这类工作记忆（C3）：不需要就整段不注入，别拿它去占 prompt
_NOTE_PREF_TRIGGERS = ("币", "余额", "剩", "推荐", "玩", "项目", "适合", "安排", "组合",
                       "几次", "多少钱", "价格", "票价", "优惠", "划算", "陪")


def _needs_pref(q: str) -> bool:
    """本轮问题是否需要偏好类工作记忆。拿不到问句（老调用方）→ 保持全量注入的旧行为"""
    if not q:
        return True
    return any(w in q for w in (_NOTE_PREF_TRIGGERS + tuple(TOPIC_WORDS)))


def _decay_keep_days(key: str, value: str) -> int:
    """规则化重要性（C7，**不用 LLM** —— 打分一旦调模型就违反"耗时不变"红线）。

    越"重要"的条目留得越久：项目名（直接影响推荐）> 有数字 > 普通话题。60 ~ 180 天。
    """
    score = 2 if str(value) in PROJECT_WORDS else 0
    if str(value) in TOPIC_WORDS:
        score += 1
    if any(c.isdigit() for c in str(value)):
        score += 1
    return 60 + 30 * score


def _alive(values: List, key: str, meta: Dict) -> List:
    """遗忘衰减（C4）：按 `_meta` 里的 last_hit_at 判断是否还注入。

    **只影响注入，不影响数据** —— 超期条目仍完整留在 profile_json 里，随时可回滚。
    没有 last_hit_at 的历史数据一律照常注入（不回溯改写，行为与改造前一致）。
    """
    today = datetime.now()
    out: List = []
    for v in (values or []):
        rec = meta.get(f"interest.{key}.{v}") or {}
        m = _RE_DATE.match(str(rec.get("last_hit_at") or ""))
        if not m:
            out.append(v)
            continue
        try:
            days = (today - datetime.strptime(m.group(1), "%Y-%m-%d")).days
        except ValueError:
            out.append(v)
            continue
        if days <= _decay_keep_days(key, str(v)):
            out.append(v)
    return out


def render_note(profile: Dict, query: str = "") -> str:
    """画像 → 一行可读摘要（注入 prompt / 小程序卡片），与 session 级口径一致。

    v4 起分两层（C3 工作记忆 + 稳定档案）：
      · **稳定档案**（孩子 / 随行人数）—— 恒注入：「这个用户是谁」与问什么无关
      · **工作记忆**（偏好项目 / 不喜欢 / 关注话题）—— 只在**本轮用得上**时注入：
        问句含「币 / 余额 / 推荐 / 玩 / 价格 / 办卡 / 营业时间 …」这类词，
        或问句里直接提到了某个项目名 / 话题词；问"几点关门"这类无关问题就不带偏好，
        免得拿一长串画像去占 prompt（也免得模型硬扯）。
      · **负向偏好必须一起注入** —— 少了它，模型会主动推荐用户明确排斥的项目并给它背书，
        这是本项目里唯一会"主动做错事"的缺陷。
      · 久未命中的条目按 C4 隐去。
    """
    view = effective_view(profile)
    fam = view.get("family") or {}
    inter = view.get("interest") or {}
    meta = (profile or {}).get(_META_KEY) or {}
    parts: List[str] = []
    # v3：优先按「每个孩子」描述，这才是模型能说出「6 岁的女儿」的依据
    # v5：任一个孩子有专属好恶/症状时，改用"带偏好"的按孩子描述；
    #     否则保持原格式（旧调用方 / 单孩无偏好家庭逐字不变）
    kids_all = [k for k in (fam.get("kids") or []) if isinstance(k, dict)]
    scoped = any(any(_kid_lists(k.get(f)) for f in _KID_LIST_FIELDS + _KID_TEXT_FIELDS)
                 for k in kids_all)
    if scoped:
        descs = [_kid_desc_pref(k, i) for i, k in enumerate(kids_all)]
    else:
        descs = [d for d in (_kid_desc(k) for k in kids_all) if d]
    if descs:
        parts.append("孩子：" + "、".join(descs))
    else:
        if fam.get("kids_ages"):
            parts.append("孩子年龄：" + "、".join(f"{a}岁" for a in fam["kids_ages"]))
        if fam.get("kids_heights"):
            parts.append("孩子身高：" + "、".join(f"{h}米" for h in fam["kids_heights"]))
    if fam.get("kids_count") and int(fam["kids_count"]) > len(descs):
        parts.append(f"随行儿童约 {fam['kids_count']} 名")

    # 已"挂到某个孩子名下"的项目，不再在账号级重复列一遍（否则同一项目出现两次，
    # 模型会以为是两个人的偏好 —— 反向污染）
    kid_owned: set = set()
    if scoped:
        for k in kids_all:
            kid_owned.update(_kid_lists(k.get("likes")))
            kid_owned.update(_kid_lists(k.get("dislikes")))

    alive = {k: _alive(inter.get(k) or [], k, meta)
             for k in ("projects", "dislikes", "topics")}
    q = str(query or "")
    if _needs_pref(q):
        shown = alive
    else:
        # 只带本轮真的提到的那几项（工作记忆：按需加载子集）
        shown = {k: [x for x in v if str(x) in q] for k, v in alive.items()}

    acct_proj = [x for x in shown.get("projects", []) if x not in kid_owned]
    acct_dis = [x for x in shown.get("dislikes", []) if x not in kid_owned]
    if acct_proj:
        parts.append("偏好项目：" + "、".join(acct_proj[:4]))
    if acct_dis:
        parts.append("不喜欢：" + "、".join(acct_dis[:4]))
    if shown.get("topics"):
        parts.append("关注：" + "、".join(shown["topics"][:6]))
    return "；".join(parts)


def derive_tags(profile: Dict) -> List[str]:
    """派生标签（纯规则、可解释）：小程序卡片与商家端看板筛选用"""
    view = effective_view(profile)
    fam = view.get("family") or {}
    inter = view.get("interest") or {}
    topics = inter.get("topics") or []
    kids = [k for k in (fam.get("kids") or []) if isinstance(k, dict)]
    ages = [int(k["age"]) for k in kids if k.get("age") is not None] or (fam.get("kids_ages") or [])
    genders = [str(k["gender"]) for k in kids if k.get("gender")]

    tags: List[str] = []
    if fam.get("kids_count") or ages:
        tags.append("带娃家庭")
    if any(a <= 6 for a in ages):
        tags.append("学龄前儿童")
    if any(7 <= a <= 14 for a in ages):
        tags.append("学龄儿童")
    if "女" in genders:
        tags.append("女宝家庭")
    if "男" in genders:
        tags.append("男宝家庭")
    if fam.get("kids_heights"):
        tags.append("关注身高限制")
    for p in (inter.get("projects") or [])[:3]:
        tags.append(f"偏好{p}")
    for p in (inter.get("dislikes") or [])[:3]:
        tags.append(f"排斥{p}")
    # v5：孩子专属好恶单独成标签（「大宝喜欢蹦床」）—— 看板筛"谁家爱玩什么"用
    for i, k in enumerate(kids[:3]):
        for p in _kid_lists(k.get("likes"))[:2]:
            tags.append(f"孩子{i + 1}喜欢{p}")
        for p in _kid_lists(k.get("dislikes"))[:2]:
            tags.append(f"孩子{i + 1}排斥{p}")
    if any(_kid_lists(k.get("symptoms")) for k in kids):
        tags.append("有注意事项")
    if any(x in topics for x in ("办卡", "充值")):
        tags.append("办卡意向")
    if "退卡" in topics:
        tags.append("退卡咨询")
    if "失物" in topics:
        tags.append("失物关注")
    if "天气" in topics or "下雨" in topics:
        tags.append("看天出行")
    return tags


# ============================================================
# 对外的五个入口
# ============================================================

def load_for_context(user_id: Optional[int], query: str = "") -> Dict:
    """★ 热路径专用：问答开始前**一次查询**拿到摘要 + 结构化画像。

    任何异常都降级成空——绝不因为画像拖慢或打断回答。
    调用方（core.chat_memory.load_context）命中时会跳过 session 级画像查询，
    因此 DB 往返次数相对改造前**净增 0**；没命中（游客/从未答过）才回落会话级。

    query 用于 C3 的「工作记忆」：只注入本轮用得上的偏好项（见 render_note）。
    仍是纯内存计算，不新增任何 IO。
    """
    blank: Dict = {"note": "", "profile": {}, "tags": []}
    if not user_id:
        return blank
    try:
        row = _db_get_profile(int(user_id))
        if not row:
            return blank
        profile = row.get("profile_json") or {}
        return {"note": render_note(profile, query), "profile": profile,
                "tags": row.get("tags_json") or []}
    except Exception as e:
        logger.warning(f"读取用户画像失败（已降级为空）: {e}")
        return blank


class ProfileConflict(Exception):
    """乐观并发冲突（C6）：带了 expect_rev，但画像已被别的端改过"""


def _record_version(user_id: int, before: Dict, after: Dict, actor: str,
                    op: str, rev: int = 0) -> None:
    """C1：每次写入留一条**不可变**版本记录（抄 GLM Memory Store 的审计模型）。

    为什么必须有：画像写坏了以前没有任何痕迹 —— 用户投诉"AI 说我家两个孩子"，
    我们查不出是哪一轮写进去的；`clear()` 又是真删，连证据一起销毁。
    这里只 INSERT，不改不删；要抹内容走 `redact_version()`（保轨迹）。
    失败只告警，绝不影响画像写入本身。
    """
    try:
        from database.db import add_profile_version
        add_profile_version(int(user_id), op=op, rev=int(rev or 0),
                            before=before or {}, after=after or {}, actor=actor)
    except Exception as e:
        logger.warning(f"画像版本记录失败（已忽略）: {e}")


def _touch_interest_meta(profile: Dict, question: str) -> None:
    """把「本轮问到的项目 / 话题」记一次命中（C4 遗忘衰减的数据来源）。

    写在**写入路径**（后台线程），读取热路径一笔不写 —— 否则 render_note 就要写库了。
    """
    q = str(question or "")
    if not q:
        return
    inter = profile.get("interest") or {}
    meta = profile.get(_META_KEY)
    if meta is None:
        meta = profile.setdefault(_META_KEY, {})
    today = datetime.now().strftime("%Y-%m-%d")
    for key in ("projects", "dislikes", "topics"):
        for v in (inter.get(key) or []):
            if str(v) and str(v) in q:
                rec = meta.setdefault(f"interest.{key}.{v}", {})
                rec["last_hit_at"] = today
                rec["hits"] = int(rec.get("hits") or 0) + 1
    if not meta:
        profile.pop(_META_KEY, None)


def refresh_from_turn(user_id: Optional[int], question: str, answer: str,
                      session_profile: Optional[Dict] = None) -> None:
    """★ 唯一写入点：一轮问答结束后增量刷新 user 级画像。

    由 core.chat_memory.remember_turn 在**后台线程**里调用（见铁律 1）。
    失败只告警，不影响任何主流程。
    """
    if not user_id:
        return
    try:
        uid = int(user_id)
        if is_optout(uid):
            return
        with _WRITE_LOCK:       # 读-改-写整体串行：否则并发/迟到的后台写会互相覆盖
            _refresh_locked(uid, question, session_profile)
    except Exception as e:
        logger.warning(f"用户画像刷新失败（已忽略）: {e}")


def _refresh_locked(uid: int, question: str, session_profile: Optional[Dict] = None) -> None:
    """refresh_from_turn 的锁内主体（调用方必须已持有 _WRITE_LOCK）"""
    row = _db_get_profile(uid) or {}
    existed = bool(row.get("profile_json"))
    cur = row.get("profile_json")
    if not isinstance(cur, dict) or not cur:
        cur = empty_profile()
    _migrate(cur)

    # ★★ 只吃「用户说的话」（N1）：回答文本里带着知识库文档标题（"…3岁能玩吗"）
    #    与模型自己推荐过的项目名，吃进来就会形成"模型推什么 → 画像记成用户喜欢
    #    → 下轮推得更狠"的自我强化污染。answer 参数保留在签名里以便调用方无感，
    #    但**明确不参与抽取**。
    fresh = extract_from_text(question or "")
    # ★ 会话级画像**只在第一次落库时**并入（服务"游客转登录，不从零开始"）。
    #   每轮都并会把会话里累积的历史年龄反复灌进用户画像：老版本的 session 记录
    #   还带着"从 AI 回答里抽出来的年龄"（N1 修之前的口径），
    #   实测症状 = 用户画像里凭空长出 3 岁孩子、kids 从 2 条涨到 4 条。
    #   对已登录用户而言，同一批问题本来就直接抽过一遍，这次并入**纯属重复**。
    if session_profile and not existed:
        fresh = _merge_auto(fresh, _flat_session_to_kids(session_profile))

    q = (question or "").strip().replace("\n", " ")[:60]
    if q:
        fresh.setdefault("interest", {})
        fresh["interest"]["recent_questions"] = [q]

    before = copy.deepcopy(cur)
    merged = _merge_auto(cur, fresh)

    ctx = merged.setdefault("context", {})
    today = datetime.now().strftime("%Y-%m-%d")
    ctx.setdefault("first_seen", today)
    ctx["last_seen"] = today
    ctx["rev"] = int(ctx.get("rev") or 0) + 1

    _touch_interest_meta(merged, question or "")
    _record_version(uid, before, merged, actor="session",
                    op="created" if not existed else "modified", rev=ctx["rev"])
    _db_upsert_profile(uid, merged, derive_tags(merged),
                       int(row.get("msg_count") or 0) + 1)


def get_profile(user_id: int) -> Dict:
    """读取画像（小程序「AI 记得我」/ 商家端详情）：结构化画像 + 标签 + 摘要"""
    row = _db_get_profile(int(user_id)) or {}
    profile = row.get("profile_json")
    if not isinstance(profile, dict) or not profile:
        profile = empty_profile()
    _migrate(profile)      # 弹窗要按 kids 渲染，读时补一次（仅内存，不写库）
    return {
        "profile": profile,
        "view": effective_view(profile),
        "tags": row.get("tags_json") or derive_tags(profile),
        "note": render_note(profile),
        "rev": int((profile.get("context") or {}).get("rev") or 0),
        "msg_count": int(row.get("msg_count") or 0),
        "last_active_at": row.get("last_active_at"),
        "has_data": bool(row),
    }


def get_options() -> Dict:
    """弹窗选项词表：小程序只渲染、不硬编码，避免与后端价目表/词表漂移。

    项目名取自 tools.business_tools.ATTRACTIONS（唯一真源），话题取自 TOPIC_WORDS。
    """
    return {
        "projects": list(PROJECT_WORDS),
        "dislikes": list(PROJECT_WORDS),      # v4：负向偏好与正向同一份项目词表
        # v5：症状是**选填**的常见注意事项预置（用户也可自己说别的，后端不做词表限制）
        "symptoms": list(_SYMPTOM_OPTIONS),
        "topics": list(TOPIC_WORDS),
        "genders": ["男", "女"],
        "age": {"min": _MIN_AGE, "max": _MAX_AGE, "step": 1},
        "height": {"min": 0.9, "max": 1.6, "step": 0.05},
        "kids_max": _LIST_LIMITS["kids"],
    }


def _clean_manual_value(field: str, value: Any) -> Optional[Any]:
    """手改值的类型与区间规整；返回 None 表示删掉这条手改、恢复自动"""
    if value is None or value == "" or value == []:
        return None
    if field == "kids":
        out: List[Dict] = []
        for item in _as_list(value):
            if not isinstance(item, dict):
                continue
            kid: Dict = {"age": None, "height": None, "gender": None,
                         "likes": [], "dislikes": [], "symptoms": []}
            try:
                if item.get("age") not in (None, ""):
                    a = int(item["age"])
                    if _MIN_AGE <= a <= _MAX_AGE:
                        kid["age"] = a
            except (TypeError, ValueError):
                pass
            try:
                if item.get("height") not in (None, ""):
                    h = round(float(item["height"]), 2)
                    if _MIN_HEIGHT <= h <= _MAX_HEIGHT:
                        kid["height"] = h
            except (TypeError, ValueError):
                pass
            g = str(item.get("gender") or "").strip()
            if g in ("男", "女"):
                kid["gender"] = g
            # 孩子专属项目好恶（v5）：只留项目词表里的名字，防止手填脏数据
            for f in _KID_LIST_FIELDS:
                vals = [v for v in _kid_lists(item.get(f)) if v in PROJECT_WORDS]
                kid[f] = vals[:8]
            # 症状（选填，跟孩子走）：不限制词表，用户知道自己孩子的情况，只是限长
            kid["symptoms"] = _kid_lists(item.get("symptoms"))[:5]
            # 有一条有效信息就保留（标量任一 or 列表任一非空）
            if any(kid[f] is not None for f in _KID_SCALAR_FIELDS) \
                    or any(kid[f] for f in _KID_LIST_FIELDS) or kid["symptoms"]:
                out.append(kid)
        return out[:_LIST_LIMITS["kids"]] or None
    if field == "kids_count":
        # 「家里几个孩子」允许只填数字、不填每个孩子的明细
        return max(0, min(int(value), _LIST_LIMITS["kids"])) or None
    out = [str(x).strip()[:12] for x in _as_list(value) if str(x).strip()]
    return out[:12] or None


def _normalize_patch(patch: Dict) -> Dict:
    """兼容旧客户端：把 v2 的 kids_ages / kids_heights 平行列表拼成 v3 的 kids。

    旧版小程序与验收脚本用的是平行列表，这里翻译成成组结构，
    保证不管哪个版本的客户端进来，落库的都是同一份结构（不出现第二条真源）。
    """
    out = dict(patch or {})
    if "kids" in out:
        out.pop("kids_ages", None)
        out.pop("kids_heights", None)
        return out
    if "kids_ages" in out or "kids_heights" in out:
        ages = _as_list(out.pop("kids_ages", None))
        heights = _as_list(out.pop("kids_heights", None))
        n = max(len(ages), len(heights))
        out["kids"] = [{"age": ages[i] if i < len(ages) else None,
                        "height": heights[i] if i < len(heights) else None,
                        "gender": None} for i in range(n)] if n else None
    return out


def update_manual(user_id: int, patch: Dict, expect_rev: Optional[int] = None) -> Dict:
    """用户手改画像：只放行 MANUAL_FIELDS，写入 manual 分区并立即重算标签。

    expect_rev（C6 乐观并发，抄 Memory Store 的 precondition）：调用方读到画像时拿到
    `rev`，提交时带回来；与当前不符说明别处already改过 → 抛 `ProfileConflict`（路由层转 409），
    由前端重新拉取后再改。不传则行为与改造前完全一致。
    """
    uid = int(user_id)
    with _WRITE_LOCK:       # 与后台自动写互斥：否则刚存的手改值会被迟到的后台写抹掉
        row = _db_get_profile(uid) or {}
        profile = row.get("profile_json")
        if not isinstance(profile, dict) or not profile:
            profile = empty_profile()
        _migrate(profile)
        ctx = profile.setdefault("context", {})
        cur_rev = int(ctx.get("rev") or 0)
        if expect_rev is not None and int(expect_rev) != cur_rev:
            raise ProfileConflict(f"画像已被其他端修改（当前 rev={cur_rev}），请刷新后重试")
        before = copy.deepcopy(profile)
        man = profile.setdefault("manual", {})
        for k, v in _normalize_patch(patch).items():
            if k in MANUAL_FIELDS:
                man[k] = _clean_manual_value(k, v)
        ctx["rev"] = cur_rev + 1
        _record_version(uid, before, profile, actor="user",
                        op="created" if not row.get("profile_json") else "modified",
                        rev=ctx["rev"])
        _db_upsert_profile(uid, profile, derive_tags(profile), int(row.get("msg_count") or 0))
    return get_profile(uid)


def clear(user_id: int, also_messages: bool = False, actor: str = "user") -> Dict:
    """一键清除画像（隐私操作）。also_messages=True 时连同本人在
    chat_messages 里的对话一并清掉——画像本来就来自那些原话，只清画像不算清干净。
    整个动作写 operation_logs 留痕。

    C1：清除**不再是无痕删除** —— 先落一条 `deleted` 版本（保留"曾经有什么"），
    再清当前行。要连带抹掉版本里的内容请用 `redact_version()`（轨迹保留，内容可抹除）。
    """
    uid = int(user_id)
    with _WRITE_LOCK:       # 与后台自动写互斥：避免"清完立刻又被写回一行"
        before = (_db_get_profile(uid) or {}).get("profile_json") or {}
        deleted = _db_delete_profile(uid)
        messages = _db_delete_messages(uid) if also_messages else 0
        if deleted and before:
            _record_version(uid, before, {}, actor=actor, op="deleted")
    try:
        from database.db import add_operation_log
        add_operation_log("clear_profile", target=f"user:{uid}",
                          detail={"profile_rows": deleted, "messages": messages}, user_id=uid)
    except Exception as e:
        logger.warning(f"画像清除留痕失败（不影响清除本身）: {e}")
    return {"deleted": deleted, "messages_cleared": messages}


def redact_version(version_id: int, redacted_by: str = "admin") -> int:
    """C1 脱敏：抹掉版本里的内容，**保留操作轨迹**（谁、什么时候、做过什么）。

    抄 Memory Store 的做法：删除条目不会抹掉历史版本里的内容，涉密数据要对版本
    执行 redact —— redact 后 path/content 置 null，审计轨迹保留。
    """
    try:
        from database.db import redact_profile_version
        return int(redact_profile_version(int(version_id), redacted_by=redacted_by) or 0)
    except Exception as e:
        logger.warning(f"画像版本脱敏失败: {e}")
        return 0


# ============================================================
# 个性化开关
# ------------------------------------------------------------
# 关闭 = 不再写入画像（已存在的保留，要清空请另外调 clear）。
# 语义刻意做成"只影响写入"：这样问答热路径不需要查开关，零额外开销。
# ============================================================
_OPTOUT_KEY = "profile:optout:{}"


def is_optout(user_id: int) -> bool:
    try:
        from database.db import get_config
        return bool(get_config(_OPTOUT_KEY.format(int(user_id)), default=False))
    except Exception as e:
        logger.warning(f"读取个性化开关失败（按开启处理）: {e}")
        return False


def set_optout(user_id: int, optout: bool) -> bool:
    """optout=True 表示关闭个性化（不再写入画像）"""
    from database.db import set_config
    set_config(_OPTOUT_KEY.format(int(user_id)), bool(optout),
               description="用户关闭了「AI 记住我」：不再写入画像")
    return bool(optout)
