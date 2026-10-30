# -*- coding: utf-8 -*-
"""回答来源分流 + 画像问答模板（纯规则、零 LLM、零 IO）

## 为什么需要它

真实 case：用户问「我家里有几个孩子？？」。这条问题的**答案就在用户画像里**，但：

1. 它不命中任何业务词表（`is_business_question` 判 False）；
2. 于是被当成**知识库问题**送进检索 —— 而"用户家里几个孩子"这件事**根本不在知识库里**，
   零命中是注定的；
3. 零命中分支**不调模型**，直接吐「园区知识库里暂时没有收录这个问题」；
4. 结果：用户拿到一句"我答不上来"，而**同一段回答里已经把答案念了一遍**。

这不是"模型不智能"，是**把非检索问题硬塞进了检索通道**。

## 本模块的职责（三个纯函数，可单测、零延迟）

- `classify_profile_question(query)` → 命中哪个画像字段（"" 表示不是画像问题）
- `render_profile_answer(field, view)` → 直接可用的回答（已填 / 未填 / 部分填写三套话术）
- `is_offtopic(query)`               → 是不是与园区和用户档案都不沾的问题
- `statement_confirm(query, view)`   → 用户陈述式输入后的一行确认（让"记住了"可见）

## 设计约束

- **零 LLM、零 DB 往返**：全部是词表 + 正则 + 内存里的画像视图。P 类问题因此**比走 RAG 更快**。
- **保守优先**：判不出就返回 ""（继续走原来的路），绝不抢真问题。
  误判成"画像问题"会让一个正经的园区问题得不到知识库答案，代价比漏判大得多。
- 话术里**不出现"知识库""检索""画像字段"这类内部词**，用户不关心我们的架构。
"""
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 画像抽屉在小程序里的位置（引导话术要用准确位置，否则用户找不到）
_PORTAL_HINT = "聊天页工具条的「画像」小图标"

_FOOTER = f"（来自你的「画像」，在{_PORTAL_HINT}里可以随时改。）"

# ------------------------------------------------------------------
# 一、画像问题识别
# ------------------------------------------------------------------
# ⚠️ 顺序即优先级。「不喜欢玩什么项目」同时含「喜欢玩什么」，
#    所以 dislikes 必须排在 projects 前面，否则会把排斥项答成偏好项。
_PROFILE_RULES = (
    ("dislikes", ("不喜欢什么", "不喜欢哪些", "不喜欢玩什么", "讨厌什么", "讨厌哪些",
                  "不爱玩什么", "不爱什么", "我排斥什么")),
    ("projects", ("喜欢玩什么", "喜欢什么项目", "喜欢哪些项目", "喜欢玩哪些",
                  "爱玩什么", "偏好什么", "我喜欢什么", "我喜欢玩什么",
                  "有什么喜欢的项目", "喜欢什么")),
    ("kids_count", ("几个孩子", "几个小孩", "几个娃", "几个小朋友", "几个宝宝",
                    "几个宝", "孩子几个", "家里有几个", "我有几个", "养了几个",
                    "几个儿子", "几个女儿", "孩子数量", "几个娃儿")),
    ("kids_age", ("孩子多大", "孩子几岁", "孩子多大了", "娃多大", "娃几岁",
                  "小孩多大", "小孩几岁", "女儿多大", "女儿几岁", "儿子多大",
                  "儿子几岁", "宝宝多大", "宝宝几岁", "孩子年龄", "多大年纪",
                  "多大了", "几岁了")),
    ("kids_height", ("孩子多高", "娃多高", "小孩多高", "女儿多高", "儿子多高",
                     "宝宝多高", "孩子身高", "小孩身高", "身高是多少",
                     "身高多少", "孩子多高啊")),
    ("kids_gender", ("男孩女孩", "男孩还是女孩", "男的女的", "儿子女儿",
                     "是男是女", "性别是什么", "男孩还是")),
    ("topics", ("我关注什么", "我问过什么", "你记得我什么", "记得我什么",
                "你记得我", "我的关注点")),
)

# 归属词：上面这些模式里若**完全不含**归属信号，多半不是在问用户自己。
# 例："蹦床适合几个孩子一起玩" 含「几个孩子」但讲的是项目规则，不是用户家。
_OWN_MARKERS = ("我", "我家", "家里", "自己", "娃", "孩子", "小孩", "小朋友",
                "女儿", "儿子", "宝宝", "宝", "闺女", "你记得", "你记")

# 命中这些词说明讲的是"园区/别人的"情况，直接放行走原流程
_NOT_OWN_MARKERS = ("适合", "能玩", "可以玩", "一起玩", "同时玩", "几个人",
                    "限制", "规定", "要求", "多少钱", "价格", "票价", "优惠",
                    "办卡", "充值")

# 这些字段讲的是"用户家有谁" —— 一旦句子里出现**具体项目名**，
# 问的多半是"这个项目对几个孩子/多大孩子开放"，属于园区规则，必须放行给知识库。
# 例：「几个孩子可以一起玩蹦床吗」含"几个孩子"，但它不是档案问题。
_KID_FACT_FIELDS = ("kids_count", "kids_age", "kids_height", "kids_gender")


def _mentions_project(t: str) -> bool:
    try:
        from services.user_profile import PROJECT_WORDS
        return any(p in t for p in PROJECT_WORDS)
    except Exception:
        return False


def classify_profile_question(query: str) -> str:
    """是不是「关于用户自己/家人」的问题？是则返回画像字段名，否则 ""。

    纯字符串匹配，无 IO，实测 < 0.01ms。
    """
    t = str(query or "").strip()
    if not t or len(t) > 40:
        return ""
    if not any(k in t for k in _OWN_MARKERS):
        return ""
    hit = ""
    for field, pats in _PROFILE_RULES:
        if any(p in t for p in pats):
            hit = field
            break
    if not hit:
        return ""
    # 讲的是"园区规则/价格"而非用户自己 —— 放行给 RAG/业务（保守优先）
    if any(k in t for k in _NOT_OWN_MARKERS):
        return ""
    # 带具体项目名的"几个孩子/多大/多高"→ 问的是项目对谁开放，不是用户家有谁
    if hit in _KID_FACT_FIELDS and _mentions_project(t):
        return ""
    return hit


# ------------------------------------------------------------------
# 二、画像问答模板（已填 / 未填 / 部分填写）
# ------------------------------------------------------------------
def _kids_of(view: Dict) -> List[Dict]:
    fam = (view or {}).get("family") or {}
    return [k for k in (fam.get("kids") or []) if isinstance(k, dict)]


def _kid_brief(k: Dict) -> str:
    """单个孩子的自然语言描述：3 岁女孩（1.2 米）"""
    parts = []
    if k.get("age") is not None:
        parts.append(f"{k['age']} 岁")
    if k.get("gender"):
        parts.append(str(k["gender"]))
    head = "".join(parts)
    if k.get("height") is not None:
        tail = f"（{k['height']} 米）"
    else:
        tail = ""
    if not head:
        # 只有身高：不带年龄/性别就只说身高，避免"（1.2 米）"这种没主语的说法
        return f"{k['height']} 米" if k.get("height") is not None else ""
    return head + tail


def _ask_hint(what: str = "孩子的年龄和身高") -> str:
    return (f"你可以：① 直接在这儿说一句「我有两个孩子，一个 3 岁女孩 1.2 米」，我就记住了；"
            f"② 点{_PORTAL_HINT}，点选着填（不用打字）；"
            f"③ 想让我帮你挑项目的话，先告诉我{what}就够。")


def render_profile_answer(field: str, view: Dict) -> str:
    """把画像字段渲染成一句可直接发出的回答。

    view：`services.user_profile.effective_view(profile)` 的结果（manual 覆盖 auto 之后）。
    返回空串表示"这个字段我答不了"，调用方应回落到原流程。
    """
    view = view or {}
    fam = view.get("family") or {}
    inter = view.get("interest") or {}
    kids = _kids_of(view)
    n = int(fam.get("kids_count") or 0) or len(kids)

    if field == "kids_count":
        if not n:
            return ("这个我还**不知道** —— 你还没告诉过我家里有几个孩子。\n" + _ask_hint())
        briefs = [b for b in (_kid_brief(k) for k in kids) if b]
        if briefs:
            return f"你告诉过我 **{n} 个孩子**：{'、'.join(briefs)}。\n{_FOOTER}"
        return f"你告诉过我 **{n} 个孩子**（年龄身高还没填）。\n{_FOOTER}"

    if field == "kids_age":
        ages = [k["age"] for k in kids if k.get("age") is not None]
        if not ages:
            if n:
                return (f"你告诉过我 **{n} 个孩子**，但**年龄还没填** —— "
                        f"补上我就能帮你看哪些项目能玩。\n{_ask_hint('孩子的年龄')}")
            return ("这个我还**不知道** —— 你还没告诉过我孩子的年龄。\n" + _ask_hint())
        txt = "、".join(f"{a} 岁" for a in sorted(set(int(a) for a in ages)))
        extra = ""
        if n and len(set(ages)) < n:
            extra = "（还有一个孩子的年龄没填）"
        return f"记的是 **{txt}**。{extra}\n{_FOOTER}"

    if field == "kids_height":
        hs = [k["height"] for k in kids if k.get("height") is not None]
        if not hs:
            if n:
                return (f"你告诉过我 **{n} 个孩子**，但**身高还没填** —— "
                        f"补上我就能判断哪些项目能玩（有些项目有身高限制）。\n"
                        f"{_ask_hint('孩子的身高')}")
            return ("这个我还**不知道** —— 你还没告诉过我孩子的身高。\n" + _ask_hint())
        txt = "、".join(f"{h} 米" for h in sorted(set(float(h) for h in hs)))
        return f"记的是 **{txt}**。\n{_FOOTER}"

    if field == "kids_gender":
        gs = [str(k["gender"]) for k in kids if k.get("gender")]
        if not gs:
            if n:
                return (f"你告诉过我 **{n} 个孩子**，但**是男孩还是女孩还没填**。\n"
                        f"{_ask_hint('孩子的性别')}")
            return ("这个我还**不知道** —— 你还没告诉过我孩子是男孩还是女孩。\n"
                    + _ask_hint())
        boys = sum(1 for g in gs if g == "男")
        girls = sum(1 for g in gs if g == "女")
        parts = []
        if boys:
            parts.append(f"{boys} 个男孩")
        if girls:
            parts.append(f"{girls} 个女孩")
        return f"是 **{'、'.join(parts)}**。\n{_FOOTER}"

    if field in ("projects", "dislikes"):
        projs = [str(x) for x in (inter.get("projects") or [])]
        dislikes = [str(x) for x in (inter.get("dislikes") or [])]

        if field == "projects":
            if not projs and not dislikes:
                if n:
                    return ("你还没跟我说过孩子喜欢玩什么。\n"
                            "你随口说一句「她喜欢蹦床」，我就能记住了。")
                return ("你还没跟我说过喜欢玩什么，我也还不知道你家孩子的情况。\n"
                        + _ask_hint())
            out = []
            if projs:
                out.append("你提到过喜欢 **" + "、".join(projs[:6]) + "**")
            else:
                out.append("你还没说过特别喜欢哪个项目")
            if dislikes:
                out.append(f"**{'、'.join(dislikes[:4])}** 你说过不喜欢，我就不推了")
            body = "；".join(out) + "。"
            hook = ("\n想让我把它们排成一套的话，说一声「帮我安排一下」，"
                    "我按你现在的余币排给你。")
            return body + hook + f"\n{_FOOTER}"

        # dislikes
        if not dislikes:
            return ("你还没跟我说过不喜欢什么。有不喜欢的项目直接说一句"
                    "「我不喜欢旋转木马」，我以后就不给你推了。")
        return (f"你明确说过不喜欢 **{'、'.join(dislikes[:4])}**，"
                f"这些我一律不往方案里排。\n{_FOOTER}")

    if field == "topics":
        tps = [str(x) for x in (inter.get("topics") or [])]
        if not tps:
            return ("你还没问过我什么固定的问题。随便聊，我会记住你关心的方向。")
        return f"你之前问得比较多的是：**{'、'.join(tps[:6])}**。\n{_FOOTER}"

    return ""


# ------------------------------------------------------------------
# 三、无关问题：定位提醒（不伪装成"知识库没收录"）
# ------------------------------------------------------------------
# 只放**明确属于外部领域**的词。判不出就返回 False —— 宁可让它走 RAG 兜底，
# 也不要把一个正经的园区问题误判成"无关"（那样用户会被莫名拒绝）。
_OFFTOPIC_WORDS = (
    # 金融投资
    "股票", "基金", "期货", "炒股", "比特币", "加密货币", "外汇牌价", "A股", "美股",
    # 编程开发
    "写代码", "代码", "编程", "python", "java", "javascript", "typescript", "sql",
    "算法题", "报错", "写个函数", "爬虫", "数据库怎么",
    # 写作 / 翻译 / 学业
    "写诗", "写一首诗", "写作文", "写一篇", "写文案", "翻译成", "翻译一下",
    "解方程", "微积分", "数学题", "英语单词", "背单词", "物理题", "化学方程式",
    # 闲聊娱乐
    "讲个笑话", "讲笑话", "唱首歌", "写首歌", "推荐部电影", "讲个故事", "猜个谜语",
    "帮我起个名", "写个祝福语",
)

# 固定词表覆盖不了的**变体**（"写一首关于春天的诗" 里没有 "写诗" 这个子串）。
# 用正则补，但每条都必须**明确属于外部领域**——判宽了会把园区问题误拒。
_OFFTOPIC_RES = (
    r"写\s*[一１]?[首個个]?.{0,10}(诗|歌词|曲)",
    r"(作|对)\s*[一]?\s*(副|个)?\s*对联",
    r"解\s*[个]?\s*.{0,4}方程",
    r"(数学|物理|化学|生物|英语|语文|历史|地理)\s*(题|作业|考试)",
    r"背\s*.{0,4}(单词|课文|古诗)",
    r"(推荐|介绍)\s*.{0,6}(电影|电视剧|综艺|小说|漫画|动漫)",
    r"(讲|说|来)\s*[个]?\s*(笑话|故事|段子|谜语)",
    r"翻\s*译\s*.{0,6}(成|为|一下|下)",
    r"(帮|给)\s*.{0,4}(我)?\s*(起|取)\s*[个]?\s*(名|名字)",
    r"写\s*.{0,6}(代码|函数|程序|脚本|爬虫|算法|正则)",
    r"用\s*(python|java|js|javascript|c\+\+|go|sql)\b",
)
_RE_OFFTOPIC = tuple(re.compile(rx) for rx in _OFFTOPIC_RES)

# 这些词一出现就说明是园区/档案范畴 —— 与 _OFFTOPIC_WORDS 同时命中时，以园区为准。
# 拆成两组是因为它们用途不同：
#   _PARK_WORDS   —— 判断"零命中时是不是园区问题"（决定走"资料没写"还是走软兜底）
#   _FAMILY_WORDS —— 只用于否决"无关"，不算园区信号
#     （「我家里有几个孩子」含"孩子"，但它不是园区问题，不能被判成园区问题）
_PARK_WORDS = (
    "园区", "乐园", "游乐园", "大黄鸭", "鸭先知", "游乐", "项目", "设备", "故障",
    "门票", "票价", "票", "办卡", "会员卡", "储值", "充值", "退卡", "退款", "余额",
    "币", "营业", "开放时间", "闭园", "几点", "失物", "认领", "报失", "捡到",
    "陪同", "大人", "家长", "安全", "排队", "限制", "规定", "须知",
    "天气", "下雨", "地图", "路线", "怎么走", "厕所", "洗手间", "停车", "餐饮",
    "寄存", "工作人员", "现场", "客服",
)
_FAMILY_WORDS = ("孩子", "娃", "儿童", "小孩", "宝宝", "女儿", "儿子", "闺女", "家人")

_DOMAIN_WORDS = _PARK_WORDS + _FAMILY_WORDS


def has_park_signal(query: str) -> bool:
    """问句是不是在问**园区的事**（含项目名/票务/办卡/位置等实体词）。

    用途：知识库零命中时决定走哪条岔路 ——
      · 有园区信号 → 这是"园区问题但资料里确实没写"，保留带标记的"资料未收录"话术
        （知识库盲区看板靠这个标记统计，行为与改造前一致）；
      · 没有园区信号 → 交给"软兜底"，让模型基于用户档案/对话历史来答。
    """
    t = str(query or "")
    if any(w in t for w in _PARK_WORDS):
        return True
    try:
        from services.user_profile import PROJECT_WORDS
        return any(p in t for p in PROJECT_WORDS)
    except Exception:
        return False


def _offtopic_hits(t: str) -> bool:
    """命中外部领域词表或外部领域句式"""
    if any(w in t for w in _OFFTOPIC_WORDS):
        return True
    return any(rx.search(t) for rx in _RE_OFFTOPIC)


def _strip_offtopic(t: str) -> str:
    """把外部领域词整段剥掉，再拿去判有没有园区域词。

    为什么必须剥：`_PARK_WORDS` 里有单字词「票」「币」，而
    「股票今天涨了吗」含「票」、「比特币能买吗」含「币」——
    不剥就会把明显的金融问题当成园区问题，用户既不被告知定位、
    又会被送进检索然后拿到「未收录」。
    """
    rest = t
    for w in _OFFTOPIC_WORDS:
        if w in rest:
            rest = rest.replace(w, " ")
    for rx in _RE_OFFTOPIC:
        rest = rx.sub(" ", rest)
    return rest


def is_offtopic(query: str) -> bool:
    """与「园区」和「用户档案」都不沾的问题 → True（用定位话术回复，而不是"未收录"）。

    纯词表 + 正则判断，零 IO。必须同时满足：命中外部领域词/句式，
    且**剥掉这些词之后**仍然不含任何园区域词。
    """
    t = str(query or "").strip()
    if not t:
        return False
    if not _offtopic_hits(t):
        return False
    rest = _strip_offtopic(t)
    if any(w in rest for w in _DOMAIN_WORDS):
        return False
    try:
        from services.user_profile import PROJECT_WORDS
        if any(p in rest for p in PROJECT_WORDS):      # "写篇蹦床的文案" → 仍是园区
            return False
    except Exception:
        pass
    return not has_park_signal(rest)


OFFTOPIC_ANSWER = (
    "我是**大黄鸭室外游乐园**的定制问答助手，只擅长园区里的这些事："
    "项目怎么玩、票价办卡、营业时间、失物招领，还有按你家孩子的情况给点建议。\n"
    "这个问题超出我的范围了 —— 我怕答不准反而误导你。\n"
    "园区相关的你随便问，比如「旋转木马 3 岁能玩吗」「办卡怎么划算」。"
)


# ------------------------------------------------------------------
# 四、陈述式输入当场确认（让"我记住了"这件事可见）
# ------------------------------------------------------------------
def statement_confirm(query: str, view: Dict) -> str:
    """用户这轮说了关于自己的新信息 → 返回一行确认；没有新信息返回 ""。

    用**与落库同一套** extract_from_text（纯正则，<0.5ms），因此确认的内容
    与真正写进画像的内容一致 —— 不会出现"确认了 A、实际记了 B"。

    真正的落库仍在后台线程（`refresh_from_turn`），这里只是把它即将写入的结果
    提前告诉用户，不产生任何额外 IO，也不改变写入时机。

    ⚠️ **只在"陈述"时确认**：用户问「我有几个孩子？」时不能回「记下了：1 个孩子」——
    那是提问，不是告知。
    """
    q = str(query or "")
    # 问句（问号 / 吗 / 呢）不确认；问的又是画像本身（"我有几个孩子"）就更要拦
    if any(m in q for m in ("?", "？", "吗", "呢")) or classify_profile_question(q):
        return ""
    try:
        from services import user_profile as up
        fresh = up.extract_from_text(q)
    except Exception as e:      # 画像模块异常绝不影响回答
        logger.warning(f"陈述确认抽取失败（忽略）: {e}")
        return ""

    if not fresh:
        return ""

    view = view or {}
    fam = view.get("family") or {}
    inter = view.get("interest") or {}
    f_fam = fresh.get("family") or {}
    f_inter = fresh.get("interest") or {}

    bits: List[str] = []

    # 项目是好恶里最值得确认的（直接决定后面推荐什么）
    cur_p = {str(x) for x in (inter.get("projects") or [])}
    cur_d = {str(x) for x in (inter.get("dislikes") or [])}
    new_p = [str(x) for x in (f_inter.get("projects") or []) if str(x) not in cur_p]
    new_d = [str(x) for x in (f_inter.get("dislikes") or []) if str(x) not in cur_d]
    if new_d:
        bits.append("不喜欢 " + "、".join(new_d[:3]))
    if new_p:
        bits.append("喜欢 " + "、".join(new_p[:3]))

    # 孩子：只在数量/年龄/身高**确实变多**时才确认
    old_n = int(fam.get("kids_count") or 0) or len(_kids_of(view))
    new_n = int(f_fam.get("kids_count") or 0)
    old_ages = {k.get("age") for k in _kids_of(view) if k.get("age") is not None}
    new_ages = {k.get("age") for k in (f_fam.get("kids") or [])
                if isinstance(k, dict) and k.get("age") is not None} - old_ages
    old_hs = {k.get("height") for k in _kids_of(view) if k.get("height") is not None}
    new_hs = {k.get("height") for k in (f_fam.get("kids") or [])
              if isinstance(k, dict) and k.get("height") is not None} - old_hs

    if new_n and new_n > old_n:
        bits.append(f"{new_n} 个孩子")
    if new_ages:
        bits.append("孩子 " + "、".join(f"{int(a)} 岁" for a in sorted(new_ages)))
    if new_hs:
        bits.append("身高 " + "、".join(f"{h} 米" for h in sorted(new_hs)))

    if not bits:
        return ""
    return "（记下了：" + "；".join(bits[:3]) + f"。在{_PORTAL_HINT}里可以改。）"


__all__ = ["classify_profile_question", "render_profile_answer", "is_offtopic",
           "has_park_signal", "OFFTOPIC_ANSWER", "statement_confirm"]
