# ============================================================
# 服务工单工具集（失物登记 / 意见反馈 → OpenAI 兼容 function calling）
# ------------------------------------------------------------
# 与 business_tools / account_tools 同一约定：身份由调用方注入，
# 写操作必须用户确认，工具返回人类可读字符串。
#
# 补齐的"办事"能力（此前只有前端按钮能提交，AI 只能引导去点按钮）：
#   - match_lost_items   按描述模糊匹配待认领失物（多关键词拆分，读）
#   - report_lost_item   报失登记（写，先自动查库再入库，需用户确认）
#   - submit_feedback    提交意见反馈 / 投诉建议（写，需用户确认）
#
# 延迟约束：本模块 3 个工具**仅当本轮问句命中 is_service_query() 时**
# 才由 business_agent 挂载，普通问答的 prompt 保持改动前原样。
# ============================================================
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 工单意图触发词（路由与工具挂载的唯一真源）
# ------------------------------------------------------------------
SERVICE_TRIGGER_WORDS = [
    # 失物
    "丢了", "丢失", "不见", "不见了", "找不着", "找不到", "找不到了", "弄丢",
    "遗落", "落在", "忘在", "失物", "招领", "报失", "登记失物", "找东西",
    "丢东西", "东西丢", "物品丢", "捡到", "捡了",
    # 反馈 / 投诉
    "反馈", "意见", "建议", "投诉", "举报", "吐槽", "不满", "差评", "太差",
    "体验不好", "要求改进",
]

# 报失前的自动匹配：命中的最小关键词数量（避免"水壶"误配到"水杯"以外的无关物品）
_MATCH_MAX_KEYWORDS = 6


def is_service_query(query: str) -> bool:
    """本轮问句是否属"服务工单"范畴（失物 / 反馈投诉）。只按本轮问句判断。"""
    text = (query or "").strip()
    if not text:
        return False
    return any(w in text for w in SERVICE_TRIGGER_WORDS)


# ------------------------------------------------------------------
# 关键词拆分：把用户的自然描述切成可比对的检索词
# ------------------------------------------------------------------
# 常见修饰词：去掉后剩下的才是"失物本体"，否则"我的粉色水壶丢了"整串匹配不到
_STOP_WORDS = ["我的", "我", "的", "一个", "一只", "一把", "一件", "个", "把", "件",
               "了", "在", "于", "园区", "游乐园", "里面", "里", "刚才", "刚刚",
               "昨天", "今天", "好像", "可能", "大概", "帮忙", "一下", "找找",
               "丢", "丢失", "不见", "找不到", "弄丢", "遗落", "落在", "忘在", "丢了"]
# 语义相近词归一：让"水壶"能命中库里写的"水杯"
_SYNONYMS = {
    "水壶": ["水杯", "保温杯", "杯子"], "水杯": ["水壶", "保温杯"],
    "保温杯": ["水壶", "水杯"], "钱包": ["钱夹", "皮夹"],
    "手机": ["电话", "iphone", "苹果"], "外套": ["衣服", "上衣", "夹克"],
    "钥匙": ["钥匙串", "车钥匙", "门钥匙"], "背包": ["书包", "双肩包", "包包"],
    "伞": ["雨伞", "遮阳伞"], "眼镜": ["墨镜", "近视镜"],
}


def _split_keywords(text: str) -> list:
    """把"我的粉色水壶丢了" → ["粉色水壶", "水壶", "水杯", "保温杯"]（去停用词 + 近义词）"""
    raw = (text or "").strip()
    for w in _STOP_WORDS:
        raw = raw.replace(w, " ")
    parts = [p for p in raw.replace("，", " ").replace(",", " ").split() if len(p) >= 2]
    keys = []
    for p in parts:
        keys.append(p)
        # 中文"形容词 + 名词"（如"粉色水壶"）：本体词是后两字，才是能跟库里名字对上的一段
        if len(p) >= 3:
            keys.append(p[-2:])
    # 近义词扩展要对**每一个候选键**做（否则"粉色水壶"查不到近义词、"水壶"又被跳过）
    for k in list(keys):
        keys.extend(_SYNONYMS.get(k, []))
    # 整串兜底（用户可能只说了"粉色水壶"）
    if raw.strip() and raw.strip() not in keys:
        keys.append(raw.strip())
    seen, out = set(), []
    for k in keys:
        k = k.strip()
        if len(k) >= 2 and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:_MATCH_MAX_KEYWORDS + 2]


def _match_lost(keyword: str, limit: int = 5) -> list:
    """在"待认领"失物库中模糊匹配，返回命中项（附命中词与命中数）"""
    from database.db import list_lost_items
    keys = _split_keywords(keyword)
    if not keys:
        return []
    rows = list_lost_items(status="lost", page=1, page_size=200) or []
    hits = []
    for it in rows:
        name = str(it.get("name") or "")
        matched = [k for k in keys if k and k in name]
        if matched:
            hits.append({**it, "_matched": matched, "_hits": len(matched)})
    hits.sort(key=lambda x: (-x["_hits"], -int(x.get("id") or 0)))
    return hits[:limit]


# ------------------------------------------------------------------
# 失物零 LLM 直查短路（orchestrator 快速通道用）
# ------------------------------------------------------------------
def lost_fastpath_answer(query: str) -> Optional[str]:
    """失物问句的确定性短路：**不调任何模型**，直接查失物库，命中即返回可读结论。

    为什么需要这一层：正常链路是「路由 → 业务 Agent（由大模型决定调 match_lost_items）
    → 工具」，任何一环出问题（模型不可用 / 额度耗尽 / 模型这轮决定不调工具），
    用户都会掉进 RAG 兜底，明明库里有记录却被告知"暂时没查到"。
    —— 这正是本项目的真实事故：库里的「居民身份证」查不出来。

    语义边界（宁可放行、不可误答）：
      · 只在**命中**时返回结论；未命中返回 None，把话头交回原链路去引导报失登记；
      · 只读，不写库、**不写问答缓存**（失物随时可能被认领，缓存会答错）。
    """
    text = (query or "").strip()
    if not text or not is_service_query(text):
        return None
    try:
        hits = _match_lost(text, limit=3)
    except Exception as e:  # pragma: no cover
        logger.warning(f"失物直查失败（放行原链路）: {e}")
        return None
    if not hits:
        return None
    lines = ["查到了，园区失物库里确实登记着待认领的物品："]
    for it in hits:
        when = str(it.get("created_at") or "")[:16]
        from_who = "游客报失" if (it.get("source") or "user") == "user" else "商家登记"
        lines.append(f"- {it.get('name')}｜{from_who}｜{when}｜编号 {it.get('id')}"
                     + ("（含照片）" if it.get("photo_url") else ""))
    lines.append("请携带能证明归属的凭证到园区前台（游客服务中心）核对领取；"
                 "如果核对不符，再告诉我们更具体的特征（颜色/品牌/特征物），可以再帮你找。")
    return "\n".join(lines)


def get_service_tools(user: Optional[dict] = None, ctx: Optional[dict] = None) -> list:
    """
    构建"服务工单"工具清单（OpenAI tools 格式 + 可执行函数）。

    Args:
        user: dict {user_id, username, role}，来自 JWT（get_optional_user）
        ctx:  预留的请求级上下文（暂未使用，与其它工具集签名保持一致）
    """
    user_id = (user or {}).get("user_id")

    def _reporter() -> str:
        """报失人称呼：取账号昵称，未登录则留空（不编造）"""
        if user_id is None:
            return ""
        try:
            from database.db import get_user_by_id
            u = get_user_by_id(user_id) or {}
            return (u.get("nickname") or u.get("username") or "")[:32]
        except Exception:
            return ""

    # ---------- 1. 失物模糊匹配（读） ----------
    def _run_match_lost_items(args: dict) -> str:
        keyword = str(args.get("keyword") or "").strip()
        if not keyword:
            return "请先告诉我要找的物品是什么（如「粉色水壶」「黑色钥匙串」），我才能帮你在失物库中核对。"
        try:
            hits = _match_lost(keyword, limit=5)
            if not hits:
                return (f"失物库的待认领物品里没有和「{keyword}」匹配的。"
                        "可以再描述得具体些（颜色/品牌/特征），或直接登记报失。")
            lines = [f"失物库中找到 {len(hits)} 件与「{keyword}」相关的待认领物品："]
            for it in hits:
                when = str(it.get("created_at") or "")[:16]
                from_who = "游客报失" if (it.get("source") or "user") == "user" else "商家登记"
                lines.append(f"- {it.get('name')}｜{from_who}｜{when}｜编号 {it.get('id')}"
                             + ("（含照片）" if it.get("photo_url") else ""))
            lines.append("请让用户确认是否为其丢失的物品；确认后引导到「失物认领」页认领，"
                         "或进一步登记报失。")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"失物匹配失败: {e}")
            return f"失物库查询失败：{str(e)[:80]}"

    # ---------- 2. 报失登记（写） ----------
    def _run_report_lost_item(args: dict) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return "请先问清楚丢了什么物品（名称尽量具体），再登记报失。"
        if len(name) > 100:
            return "物品名称太长（最多 100 字），请用简短描述，例如「粉色保温杯」。"
        # 双重保护：先查库再入库，避免同一件东西既在待认领列表里又产出一条重复报失
        if str(args.get("user_confirmed")).lower() not in ("true", "1", "yes"):
            try:
                hits = _match_lost(name, limit=3)
            except Exception:
                hits = []
            hint = ""
            if hits:
                hint = ("另外失物库里已有疑似同款：" +
                        "、".join(str(h.get("name")) for h in hits) +
                        "，请先让用户确认是不是他的。")
            return (f"⛔ 未登记：报失会写入园区失物库，需用户确认后才能提交。"
                    f"请先告知用户「我将为你登记报失：{name}」，询问是否确认；"
                    f"用户确认后再以 user_confirmed=true 调用本工具。{hint}")
        try:
            hits = _match_lost(name, limit=3)
            if hits:
                lines = ["⚠️ 先别急着登记——失物库里已有疑似匹配的物品，请让用户先确认："]
                for it in hits:
                    lines.append(f"- {it.get('name')}（编号 {it.get('id')}）"
                                 + ("，有照片" if it.get("photo_url") else ""))
                lines.append("如果是同一件，请引导用户到「失物认领」页认领；"
                             "确认不是同一件时，再告知用户并重新登记。")
                return "\n".join(lines)

            from database.db import create_lost_item
            item = create_lost_item(name=name, photo_url="", source="user", reporter=_reporter())
            if not item:
                return "报失登记失败，请稍后重试或到「失物招领」页手动提交。"
            logger.info(f"[服务工单] 报失登记成功 id={item.get('id')} name={name} user={user_id}")
            return (f"✅ 报失已登记：{item.get('name')}（编号 {item.get('id')}）。"
                    "工作人员核对到后会通过小程序通知你；也可随时问我「有没有人捡到…」帮你再查。")
        except Exception as e:  # pragma: no cover
            logger.warning(f"报失登记失败: {e}")
            return f"报失登记失败：{str(e)[:80]}"

    # ---------- 3. 意见反馈 / 投诉（写） ----------
    def _run_submit_feedback(args: dict) -> str:
        content = str(args.get("content") or "").strip()
        if len(content) < 4:
            return "反馈内容太短了，请让用户把具体问题说清楚（发生了什么、大概什么时间、哪个项目）。"
        if str(args.get("user_confirmed")).lower() not in ("true", "1", "yes"):
            return ("⛔ 未提交：反馈会直接送达园区运营方，需用户确认后才能提交。"
                    f"请先把整理好的反馈内容复述给用户（「我将为你提交：{content[:60]}」），"
                    "询问是否确认；用户确认后再以 user_confirmed=true 调用本工具。")
        try:
            from database.db import add_user_feedback
            rec = add_user_feedback(content=content[:500], nickname=_reporter(), user_id=user_id)
            if not rec:
                return "反馈提交失败，请稍后重试，或到「我的-意见反馈」页手动提交。"
            logger.info(f"[服务工单] 意见反馈已提交 id={rec.get('id')} user={user_id}")
            return ("✅ 反馈已提交给园区运营方，感谢你的意见。"
                    "如果问题涉及具体消费或退费，也可以直接问我「我的订单/我的卡」帮你核对。")
        except Exception as e:  # pragma: no cover
            logger.warning(f"提交反馈失败: {e}")
            return f"反馈提交失败：{str(e)[:80]}"

    def _tool(name: str, description: str, params: dict, run) -> dict:
        return {"name": name, "description": description, "parameters": params, "run": run}

    _s = lambda d: {"type": "object", "properties": d, "required": list(d)}
    return [
        _tool("match_lost_items",
              "按用户描述的物品特征（颜色/名称/用途，如\"粉色水壶\"\"黑色钥匙串\"）"
              "在园区失物库的**待认领**物品中模糊匹配，自动做关键词拆分与近义词扩展。"
              "用户说\"我丢了东西/有没有人捡到…/帮我找找…\"时**必须先调用本工具**查库，"
              "严禁不查就说没有。本工具只查不改。",
              _s({"keyword": {"type": "string",
                              "description": "用户对丢失物品的自然描述，如：粉色水壶、黑色钥匙串"}}),
              _run_match_lost_items),
        _tool("report_lost_item",
              "为游客登记报失（写操作，会写入园区失物库并通知工作人员）。"
              "调用前置条件（缺一不可）：① 已调用 match_lost_items 查过库；"
              "② 已把报失内容告诉用户；③ 用户已明确确认登记。"
              "用户还没确认时必须传 user_confirmed=false，本工具会拒绝执行。",
              {"type": "object", "properties": {
                  "name": {"type": "string",
                           "description": "失物名称（简短具体，如：粉色保温杯、黑色钥匙串）"},
                  "user_confirmed": {"type": "boolean",
                                     "description": "用户是否已明确确认登记；未确认必须为 false"}},
               "required": ["name", "user_confirmed"]},
              _run_report_lost_item),
        _tool("submit_feedback",
              "把游客的意见/建议/投诉整理成一条反馈提交给园区运营方（写操作）。"
              "用户说\"我要投诉/提个建议/反馈一下/体验太差\"时调用。"
              "调用前置条件：已把整理后的反馈内容复述给用户并取得确认；"
              "用户还没确认时必须传 user_confirmed=false，本工具会拒绝执行。",
              {"type": "object", "properties": {
                  "content": {"type": "string",
                              "description": "整理后的反馈正文：发生了什么、时间、涉及哪个项目/服务"},
                  "user_confirmed": {"type": "boolean",
                                     "description": "用户是否已明确确认提交；未确认必须为 false"}},
               "required": ["content", "user_confirmed"]},
              _run_submit_feedback),
    ]
