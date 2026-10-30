# ============================================================
# 园区业务工具集（OpenAI 兼容 function calling）
# ------------------------------------------------------------
# 把"后端已有能力"注册成可被 LLM 自主调用的工具（原生 OpenAI tools 格式，
# 不依赖 langchain-openai）：
#   - 查询我的余额/持卡（需登录用户）
#   - 每日签到
#   - 在售卡产品 / 游乐项目价目（结构化事实，来自知识库 + cards 表权威价）
#   - 失物库检索（名称关键词）
#   - 知识库检索（复用 RAG 检索管道，作为兜底事实源）
# 设计原则：
#   1. 用户身份由调用方（Orchestrator）注入，绝不经过 LLM 生成；
#   2. 金额/价格一律以 cards 表 / 结构化价目为权威；
#   3. 工具返回人类可读字符串，模型只负责汇总与决策。
# ============================================================
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 游乐项目结构化价目（与 docs 知识库一致：1元=1游戏币，全项目可用币抵扣）
# 维护提醒：若知识库更新价目，请同步此表或改为运行时从知识库解析。
# ------------------------------------------------------------------
ATTRACTIONS = [
    {"name": "旋转木马", "price_yuan": 30, "unit": "单次游玩 5 分钟",
     "rule": "仅限儿童乘坐，大人禁止乘坐"},
    {"name": "荡秋千", "price_yuan": 40, "unit": "单次游玩 4 分钟", "rule": ""},
    {"name": "小型过山车", "price_yuan": 50, "unit": "单次运行 15 圈", "rule": ""},
    {"name": "蹦床", "price_yuan": 40, "unit": "单次游玩 5 分钟", "rule": ""},
    {"name": "浴室大作战", "price_yuan": 40, "unit": "单次游玩 5 分钟", "rule": ""},
    {"name": "大炮乐园", "price_yuan": 30, "unit": "单次提供 80 发子弹", "rule": ""},
]

_COINS_RE = re.compile(r"(\d+)\s*币")


def _parse_coins(name: str, description: str = "") -> int:
    m = _COINS_RE.search(f"{name} {description}")
    return int(m.group(1)) if m else 0


def _fmt_sale_card(c) -> str:
    return (f"{c['name']}（¥{round(c['price'] / 100, 2)}，含 {_parse_coins(c['name'], c.get('description',''))} 币）"
            + (f"，{c['description']}" if c.get("description") else ""))


def _user_coins_total(user_id: int) -> int:
    """余币合计 = 生效持卡 coins 之和（NULL 时按卡面值解析兑底）"""
    from database.db import get_card, list_user_cards
    total = 0
    for uc in list_user_cards(user_id) or []:
        if uc.get("coins") is not None:
            total += int(uc["coins"])
            continue
        card = get_card(uc.get("card_id"))
        if card:
            total += _parse_coins(card["name"], card.get("description", ""))
    return total


def _not_logged_hint() -> str:
    return ("当前对话未携带登录身份，无法查询个人账户。请先通过微信登录后，"
            "再问我「我的余额/我的卡/签到」等问题。")


def get_business_tools(user: Optional[dict] = None, ctx: Optional[dict] = None) -> list:
    """
    构建"园区业务"工具清单（OpenAI tools 格式 + 可执行函数）。
    tools 与当前对话用户绑定（用户身份由服务端注入，不会由模型生成）；
    无需登录的能力（在售卡/价目/失物库/知识库）同样可用。

    Args:
        user: 可选 dict {user_id, username, role}，来自 JWT（get_optional_user）
        ctx: 可选请求级上下文 dict；图表工具会把生成的图表信息
             {"chart_id","title","url"} 追加到 ctx["charts"]（避免全局单例并发串台）

    Returns:
        [{name, description, parameters, run}]，run(args: dict) -> str
    """
    user_id = (user or {}).get("user_id")
    is_logged = user_id is not None

    # ---------- 1. 我的余额与持卡 ----------
    def _run_get_my_balance(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        try:
            from database.db import list_user_cards, bonus_available
            cards = list_user_cards(user_id) or []
            card_total = _user_coins_total(user_id)
            bonus = bonus_available(user_id)   # 签到累计 - 已动用 = 签到剩余可用
            total = card_total + bonus
            lines = [
                f"当前账户可用余币共 {total} 枚：其中充值/办卡 {card_total} 币，"
                f"每日签到所得 {bonus} 币（签到所得同样可直接抵扣游乐项目消费）。明细："
            ]
            for uc in cards[:10]:
                coins = uc.get("coins")
                kind = uc.get("kind") or "member"
                note = "（会员卡·生效中）" if kind != "small" and uc.get("in_use") \
                    else "（小额充币，已计入余币）" if kind == "small" else ""
                lines.append(
                    f"- {uc.get('card_name') or '储值卡'}：{coins if coins is not None else '未记录'} 币{note}"
                )
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover - 数据库异常兜底
            logger.warning(f"查询余额失败: {e}")
            return f"查询余额失败：{str(e)[:80]}"

    # ---------- 2. 每日签到 ----------
    def _run_daily_checkin(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        try:
            from database.db import do_checkin
            st = do_checkin(user_id, reward_coins=1)
            if st.get("signed_now"):
                return (f"✅ 签到成功！今日 +1 币（签到累计奖励共 {st.get('total_coins', 0)} 币，"
                        f"已连续签到 {st.get('streak_days', 0)} 天）。")
            return (f"今天已经签到过啦（连续 {st.get('streak_days', 0)} 天），"
                    f"签到累计奖励共 {st.get('total_coins', 0)} 币。")
        except Exception as e:  # pragma: no cover
            logger.warning(f"签到失败: {e}")
            return f"签到失败：{str(e)[:80]}"

    # ---------- 3. 在售卡产品（办卡/充值推荐的数据源，价格为权威） ----------
    def _run_list_sale_cards(args: dict) -> str:
        try:
            from database.db import list_cards
            cards = list_cards(status="on_sale") or []
            if not cards:
                return "当前没有在售的储值卡。"
            lines = ["目前在售储值卡："]
            for c in cards:
                lines.append(f"- {_fmt_sale_card(c)}")
            lines.append("注：1元 = 1枚游戏币，所有游乐项目均可使用游戏币抵扣消费；"
                         "另有 10 元/20 元小额临时充值（无赠送）。")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询在售卡失败: {e}")
            return f"查询在售卡失败：{str(e)[:80]}"

    # ---------- 4. 失物招领查询 ----------
    def _run_search_lost_and_found(args: dict) -> str:
        keyword = str(args.get("keyword") or "").strip()
        try:
            from database.db import list_lost_items
            items = list_lost_items(status="lost", keyword=keyword, page=1, page_size=8) if keyword \
                else list_lost_items(status="lost", page=1, page_size=8)
            if not items:
                return f"未在失物库中找到与「{keyword or '任何'}」相关的待认领物品。"
            lines = [f"失物库中找到 {len(items)} 件疑似物品："]
            for it in items:
                when = (it.get("created_at") or "")[:16]
                lines.append(
                    f"- ID {it.get('id')}｜{it.get('name')}｜{it.get('reporter') or '游客'} 报失｜{when}")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"查询失物失败: {e}")
            return f"查询失物失败：{str(e)[:80]}"

    # ---------- 5. 游乐项目价目（币值换算/玩什么/推荐组合的权威数据） ----------
    def _run_get_attractions(args: dict) -> str:
        lines = ["园区共 6 个游乐项目（1元=1游戏币，均可使用游戏币抵扣）："]
        for a in ATTRACTIONS:
            rule = f"，{a['rule']}" if a.get("rule") else ""
            lines.append(f"- {a['name']}：{a['price_yuan']} 元（{a['price_yuan']} 币），{a['unit']}{rule}")
        lines.append("陪游说明：一名儿童可带一名大人免费陪同，第二名及以上家长需按项目单独购票；"
                     "无儿童随行的成人按原价购票。")
        return "\n".join(lines)

    # ---------- 5.1 余额 → 游玩组合（服务端算账，不让模型心算） ----------
    def _run_plan_play_combo(args: dict) -> str:
        """「我还剩多少币 / 这些币能玩什么」→ 可直接下单的组合方案。

        偏好取自**服务端画像**而不是让模型复述：模型背不全也背不准，而"推荐了用户
        明确说不喜欢的项目"是本项目里唯一会主动做错事的情形。
        算术全部在 services.purchase.plan_play_combo（纯函数）里完成。
        """
        if not is_logged:
            return _not_logged_hint()
        from services.purchase import plan_play_combo

        liked: list = []
        disliked: list = []
        try:
            from services import user_profile as _up
            prof = (_up.load_for_context(user_id).get("profile") or {})
            inter = (_up.effective_view(prof).get("interest") or {})
            liked = [str(x) for x in (inter.get("projects") or [])]
            disliked = [str(x) for x in (inter.get("dislikes") or [])]
        except Exception as e:
            logger.warning(f"读取画像偏好失败（按无偏好继续）: {e}")
        # 本轮用户点名的好恶优先（模型主动传了才覆盖）
        liked = [str(x) for x in (args.get("liked") or [])] or liked
        disliked = [str(x) for x in (args.get("disliked") or [])] or disliked
        try:
            budget = int(args.get("max_coins") or 0)
        except (TypeError, ValueError):
            budget = 0

        res = plan_play_combo(user_id, max_coins=budget, liked=liked, disliked=disliked)
        head = "当前可用余币 %d 币（1元=1游戏币）。" % res["balance"]
        if not res.get("ok"):
            from tools.consume_tools import _recharge_hint
            return (head + "排不出组合：%s。\n请照实告诉用户排不出以及原因，"
                    "并给出这些充值选项：%s" % (res.get("reason"), _recharge_hint()))

        lines = [head + "已按权威价目排出这份组合（数字由服务端算出，可直接引用）："]
        for it in res["combo"]:
            lines.append("- %s × %d 次 = %d 币（单次 %d 币%s）"
                         % (it["name"], it["qty"], it["subtotal"], it["unit_coins"],
                            ("，" + it["unit_desc"]) if it.get("unit_desc") else ""))
        lines.append("- 合计 %d 币，剩余 %d 币" % (res["total_coins"], res["leftover"]))
        if res["leftover"] > 0:
            lines.append("（余下的 %d 币不要强行花掉，请主动告诉用户还剩多少、可以留到下次玩）"
                         % res["leftover"])
        if res["excluded"]:
            lines.append("- 已排除：" + "、".join(
                "%s（%s）" % (e["name"], e["reason"]) for e in res["excluded"]))
        if res["alternatives"]:
            lines.append("- 想省一点可以说：" + "、".join(
                "%s少玩一次省 %d 币" % (a["name"], -a["delta_coins"])
                for a in res["alternatives"]))
        lines.append("请把这套组合用口语讲给用户（几次什么项目、合计多少币、还剩多少），"
                     "**不要**把它改写成价目表、也不要自己再算一遍账；"
                     "用户明确说要买时再调用 purchase_projects 下单。")
        return "\n".join(lines)

    # ---------- 5.2 出行规划（到访习惯 → 平均值，用户明确要求"必须是整数"） ----------
    def _run_plan_trip(args: dict) -> str:
        """「明天带孩子来玩，给个攻略」→ 按**历史到访习惯**出方案。

        与 plan_play_combo 的分工（两者都保留，服务不同问题）：
          · plan_play_combo → "我**还剩**多少币能玩什么"（按当前余额填满预算）
          · plan_trip       → "我**每次来**都玩多少、每个项目几次"（按历史均值，不花光）
        算术全部在 services.trip_stats（纯函数）里完成，不让模型心算。
        """
        if not is_logged:
            return _not_logged_hint()
        from services.purchase import list_park_projects_view
        from services.trip_stats import trip_text

        liked: list = []
        disliked: list = []
        prof_note = ""
        kids_note: list = []
        try:
            from services import user_profile as _up
            _ctx = _up.load_for_context(user_id)
            prof = _ctx.get("profile") or {}
            view = _up.effective_view(prof)
            inter = (view.get("interest") or {})
            liked = [str(x) for x in (inter.get("projects") or [])]
            disliked = [str(x) for x in (inter.get("dislikes") or [])]
            # ★ E2b：每个孩子 + **他自己的**喜好 —— 「只按用户填的喜好排」，且归属不能串。
            #   某个孩子没填过 likes 时同样下发（likes 为空列表），让模型**点名说不知道**，
            #   而不是拿账号级偏好顶替他（用户红线）。
            for _k in ((view.get("family") or {}).get("kids") or []):
                if not isinstance(_k, dict):
                    continue
                kids_note.append({
                    "desc": _up._kid_desc(_k) if hasattr(_up, "_kid_desc") else "",
                    "likes": list(_k.get("likes") or []),
                    "dislikes": list(_k.get("dislikes") or []),
                })
            # 档案摘要随统计一起给模型：一次调用就能把"孩子几岁 + 平时玩什么 + 每次多少币"
            # 三件事同时拿到，省掉一轮工具往返（首字延迟不增加）
            prof_note = _ctx.get("note") or ""
        except Exception as e:
            logger.warning(f"读取画像偏好失败（按无偏好继续）: {e}")
        liked = [str(x) for x in (args.get("liked") or [])] or liked
        disliked = [str(x) for x in (args.get("disliked") or [])] or disliked

        try:
            from database.db import list_consumptions_by_user
            rows = list_consumptions_by_user(user_id, limit=20000) or []
        except Exception as e:  # pragma: no cover - 数据库异常兜底
            logger.warning(f"读取消费记录失败: {e}")
            return f"读取本人消费记录失败：{str(e)[:80]}"

        return trip_text(rows, list_park_projects_view(),
                         preferred=liked, disliked=disliked,
                         profile_note=prof_note, kids=kids_note or None)

    # ---------- 6. 知识库检索（规则/注意事项/退卡/安全等文档类内容） ----------
    def _run_knowledge_search(args: dict) -> str:
        query = str(args.get("query") or "").strip()
        try:
            from agents.retrieval_agent import retrieval_agent
            docs = retrieval_agent.search(query, top_k=4, use_cache=True)
            if not docs:
                return "知识库中没有检索到与该问题相关的内容。"
            lines = []
            for text, source, score in docs:
                lines.append(f"[{source}｜相关度{score:.2f}] {text[:260]}")
            return "\n".join(lines)
        except Exception as e:  # pragma: no cover
            logger.warning(f"知识库检索失败: {e}")
            return f"知识库检索失败：{str(e)[:80]}"

    # ---------- 7. 消费统计与图表（游客自助分析，需登录；退款单全额剔除） ----------
    _PERIOD_SCHEMA = {"type": "string", "enum": ["day", "week", "month", "year"],
                      "description": "统计周期：day=近一天，week=最近一周(7天)，month=最近一个月(30天)，year=最近一年(365天)"}

    def _run_query_consumption_stats(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        from services.statistics import stats_to_text, user_stats
        period = str(args.get("period") or "month")
        stats = user_stats(user_id, period)
        return stats_to_text(stats)

    def _run_generate_stat_charts(args: dict) -> str:
        if not is_logged:
            return _not_logged_hint()
        from services.chart_render import render_consumption_charts
        from services.statistics import stats_to_text, user_stats
        period = str(args.get("period") or "month")
        stats = user_stats(user_id, period)
        folder = os.path.join("static", "charts")
        charts = render_consumption_charts(stats, folder)
        if ctx is not None:
            ctx.setdefault("charts", []).extend(charts)
        if not charts:
            return "该周期没有可生成图表的数据。" + stats_to_text(stats)
        lines = ["已为您生成 " + str(len(charts)) + " 张图表："]
        for c in charts:
            lines.append(f"- {c['title']}（{c['url']}）")
        lines.append(stats_to_text(stats))
        return "\n".join(lines)

    # ---------- 5.3 园区门店地址（常量，0 网络 0 LLM） ----------
    def _run_park_location(args: dict) -> str:
        """用户问"你们店在哪/位置/怎么去"，或出行规划里要报地址时调用。

        地址与坐标是**常量**（agents.map_agent 里已有权威定义），这里只做转述 +
        拼一个导航链接。**不走地图 Agent**：那条路要 2~3 次 LLM 往返（实测 16~18s），
        而这是个固定字符串，纯属浪费；且纯文字的「位置」压根不触发地图关键词。
        """
        try:
            from agents.map_agent import PARK_NAME, PARK_ADDRESS, PARK_LOCATION
        except Exception as e:  # pragma: no cover
            logger.warning(f"读取园区常量失败: {e}")
            return "园区地址暂时读取失败，请稍后再试。"
        lng, lat = (PARK_LOCATION.split(",") + ["", ""])[:2]
        link = ""
        try:
            from tools.amap_tools import build_navigation_url
            link = build_navigation_url(lng.strip(), lat.strip(), PARK_NAME)
        except Exception as e:  # pragma: no cover
            logger.warning(f"生成导航链接失败: {e}")
        lines = [f"{PARK_NAME}｜地址：{PARK_ADDRESS}",
                 "（全城仅此一家门店；园区为室外乐园，就在万达广场 2 号门旁。）"]
        if link:
            lines.append(f"导航链接：{link}")
        lines.append("请把地址告诉用户，并提示可以点下方地图卡片查看位置或直接导航。")
        return "\n".join(lines)

    def _tool(name: str, description: str, params: dict, run) -> dict:
        return {
            "name": name,
            "description": description,
            "parameters": params,
            "run": run,
        }

    _s = lambda d: {"type": "object", "properties": d, "required": list(d)}
    return [
        _tool("get_my_balance",
              "查询【当前登录用户】的储值卡余额（总游戏币数）与名下生效卡列表。仅当用户询问\"我的余额/我的卡/还剩多少币/持卡\"时调用。",
              {"type": "object", "properties": {}},
              _run_get_my_balance),
        _tool("daily_checkin",
              "执行【当前登录用户】的每日签到并返回结果（今天是否已签、连续天数、累计签到获得币数）。用户要求签到/打卡时调用。",
              {"type": "object", "properties": {}},
              _run_daily_checkin),
        _tool("list_sale_cards",
              "查询当前在售的储值卡产品（含充值金额、赠送币数与总币数）。用户询问办卡/充值/送多少币/办什么卡划算时调用。",
              {"type": "object", "properties": {}},
              _run_list_sale_cards),
        _tool("search_lost_and_found",
              "按名称关键词检索园区失物招领库中尚未认领的物品（如\"粉色水壶\"\"外套\"\"钥匙\"）。返回匹配物品列表；无匹配返回空结果。",
              _s({"keyword": {"type": "string",
                              "description": "失物名称关键词，如：水壶、钥匙、外套"}}),
              _run_search_lost_and_found),
        _tool("get_attractions",
              "查询园区全部游乐项目及单价/时长/特殊规则。用户询问\"有哪些项目/多少钱/能玩什么"
              "/XX币还能玩什么/适合玩什么/怎么搭配\"时调用。只是要清单时用它；"
              "要**按余额排出具体组合**请用 plan_play_combo。",
              {"type": "object", "properties": {}},
              _run_get_attractions),
        _tool("plan_play_combo",
              "【按余额出方案】根据用户当前可用余币 + 画像里的偏好（自动避开用户明确不喜欢的项目），"
              "排出可直接下单的游玩组合：每个项目玩几次、合计多少币、还剩多少、"
              "以及想省钱时少玩一次能省多少。用户问\"我还剩多少币/这些币还能玩什么/"
              "够玩几次/帮我推荐几个项目/怎么搭配\"时**必须调用本工具**，"
              "禁止自己心算金额或凭印象排组合。返回的是权威数字，照实转述即可；"
              "本工具不扣币，用户明确要买时再调 purchase_projects。",
              {"type": "object", "properties": {
                  "max_coins": {"type": "integer",
                                "description": "可选：按指定预算出方案；不传（或 0）则用账户当前可用余币"},
                  "liked": {"type": "array", "items": {"type": "string"},
                            "description": "可选：用户本轮点名的想玩项目，会覆盖画像里的偏好"},
                  "disliked": {"type": "array", "items": {"type": "string"},
                               "description": "可选：用户本轮点名的排斥项目，一律不出现在方案里"}},
               "required": []},
              _run_plan_play_combo),
        _tool("plan_trip",
              "【按历史习惯出行规划】读取【当前登录用户】的历次到访记录，算出"
              "「每次来平均消费多少币」「每个项目平均玩几次」（**平均值均为整数**），"
              "并结合用户档案里的偏好排出本次的游玩组合。"
              "用户问「我平时来都玩多少 / 每个项目玩几次 / 明天带孩子来该怎么玩 / "
              "给个攻略 / 帮我安排一下行程」时必须调用本工具，"
              "**禁止凭印象编造平均次数或金额**。"
              "注意：本工具按**历史均值**出方案（不一定花光余额）；"
              "若用户问的是「我现在还剩多少币能玩什么」，请改用 plan_play_combo。"
              "本工具不扣币，用户明确要买时再调 purchase_projects。",
              {"type": "object", "properties": {
                  "liked": {"type": "array", "items": {"type": "string"},
                            "description": "可选：用户本轮点名的想玩项目，会覆盖画像里的偏好"},
                  "disliked": {"type": "array", "items": {"type": "string"},
                               "description": "可选：用户本轮点名的排斥项目，一律不出现在方案里"}},
               "required": []},
              _run_plan_trip),
        _tool("park_location",
              "查询本园区的门店地址与导航入口（固定信息，全城仅此一家）。"
              "用户问「你们店在哪 / 地址 / 位置 / 怎么去 / 怎么导航」，"
              "或在做出行规划需要告知园区位置时调用。无需登录。",
              {"type": "object", "properties": {}},
              _run_park_location),
        _tool("knowledge_search",
              "在园区运营知识库（规章制度、游玩须知、办卡规则、售后、营业时间等文档）中检索与 query 相关的原文片段。需要引用具体规章制度/书面口径时调用。",
              _s({"query": {"type": "string",
                            "description": "检索问句，如：退卡规则、营业时间、设备故障补偿"}}),
              _run_knowledge_search),
        _tool("query_consumption_stats",
              "查询【当前登录用户】近一天/最近一周/一个月/一年的消费统计（项目游玩次数与花费排行、办卡类型张数与金额、消费金额按月分布）。用户要求\"分析/统计消费\"“玩了什么最多”“办了哪些卡”“按月花了多少”时调用。",
              {"type": "object",
               "properties": {"period": _PERIOD_SCHEMA},
               "required": ["period"]},
              _run_query_consumption_stats),
        _tool("generate_stat_charts",
              "为【当前登录用户】最近一周/一个月/一年的消费统计生成可视化图表（PNG）：项目游玩次数排行、办卡类型统计、消费金额按月分布。用户明确要求\"生成图表/可视化/图\"或需要直观展示统计时调用；通常应配合 query_consumption_stats 一起使用。",
              {"type": "object",
               "properties": {"period": _PERIOD_SCHEMA},
               "required": ["period"]},
              _run_generate_stat_charts),
    ]


def tools_to_openai(tools: list) -> list:
    """把业务工具清单转成 OpenAI chat.completions 的 tools 参数"""
    result = []
    for t in tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        })
    return result


def dump_tools(tools: list) -> str:
    """调试/缓存用：打印工具清单摘要"""
    return json.dumps([{k: v for k, v in t.items() if k != "run"} for t in tools],
                      ensure_ascii=False, indent=1)
