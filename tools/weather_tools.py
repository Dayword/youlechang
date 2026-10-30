# ============================================================
# 天气出行助手（实时天气 → 园区出游决策）
# ------------------------------------------------------------
# 为什么单独一个模块，而不是继续用 MapAgent 里那 8 个高德工具？
#   1. 高德天气能力此前挂在 LangChain AgentExecutor 上：用户问一句"今天天气怎么样"
#      要起整个地图 Agent（多轮 LLM 往返 + 工具循环），实测 3~10s；
#   2. 天气是**高频、低歧义、无需推理**的问题，完全可以用规则模板直接出结论，
#      0 次 LLM 调用即可回答，端到端目标 ≤1s；
#   3. 园区是**室外**乐园，天气直接影响"来不来、玩什么"，
#      所以输出不是转发温度数字，而是给出出游决策结论。
#
# 设计要点：
#   - 复用 tools/amap_tools 的 _amap_get（统一 key/超时/配额计数/错误处理），
#     但**函数内 import**，避免只问天气的路径在冷启动时提前拉起 langchain；
#   - 问句同时含路线/导航语义时**不接管**（交回 MapAgent，它更擅长多点规划）；
#   - 天气属实时数据，回答**绝不写 Redis 问答缓存**（由 orchestrator 保证）。
# ============================================================
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 园区固定在北京（与 map_agent.PARK_ADDRESS 同城）；用户问外地天气时交回 MapAgent
DEFAULT_CITY = "北京"

# 天气意图触发词（语义层：不只"天气"两个字，把口语说法一并覆盖）
WEATHER_TRIGGER_WORDS = [
    "天气", "气温", "多少度", "几度", "冷不冷", "热不热", "热吗", "冷吗",
    "下雨", "下雪", "下不下雨", "会下雨", "会不会下雨", "带伞", "雨伞",
    "晒", "晒不晒", "紫外线", "风大", "有风", "风力",
    "适合去玩吗", "适不适合去", "适合去吗", "能去玩吗", "今天适合",
    "天气怎么样", "什么天气", "预报", "降温", "升温", "闷不闷",
]
# 命中这些词说明是"路线/导航"复合问句 → 交回 MapAgent（不接管）
_ROUTE_WORDS = ["怎么去", "怎么走", "怎么到", "路线", "导航", "多远", "几公里",
                "多少公里", "开车", "自驾", "公交", "地铁", "坐车", "打车", "车程"]
# 命中这些词说明"天气只是背景，真正想问的是玩什么" → 交回 RAG/业务 Agent（不接管），
# 否则会把"明天下雨的话还能玩什么"错误地简化成一句天气播报
_NON_PURE_WORDS = ["玩什么", "有什么玩", "玩啥", "适合玩", "能玩", "可以玩",
                   "项目", "推荐", "办卡", "多少钱", "价格", "优惠", "退卡"]


def is_weather_question(query: str) -> bool:
    """是否是**纯天气**问句（可用于 0 LLM 快速通道）。

    保守判定：问句过长（可能是复合问题，需走 RAG/多 Agent）或含路线导航语义时
    一律不接管，避免把"明天下雨那室内有什么玩的"这类问题错误地简化成一句天气播报。
    """
    text = (query or "").strip()
    if not text or len(text) > 30:
        return False
    if any(w in text for w in _ROUTE_WORDS):
        return False
    if any(w in text for w in _NON_PURE_WORDS):
        return False
    return any(w in text for w in WEATHER_TRIGGER_WORDS)


def mentions_weather(query: str) -> bool:
    """问句是否**提到**天气（不限长度）：用于给业务 Agent 挂 get_weather 工具。

    与 is_weather_question 的区别：那个决定"要不要绕过 LLM 直接播报"，
    这个只决定"要不要把天气工具交给模型"，门槛更宽（复合问句也能用上天气）。

    ★ 出行规划扩展（2026-09-23）：用户说「**明天**要带一个男孩**去你们那玩**」时，
    他**没有说"天气"这两个字**，但园区是**室外**乐园 —— 天气直接决定"能不能来"，
    所以这类问句必须挂上天气工具，否则模型只能在结尾许愿
    （真实 case：截图回「明天天气方面如果需要我帮你看看下雨情况」却没查）。
    判据复用 business_agent.is_trip_plan_call，与"挂不挂三件套"同源，避免两边错配。
    """
    text = (query or "").strip()
    if not text:
        return False
    if any(w in text for w in WEATHER_TRIGGER_WORDS):
        return True
    try:
        from agents.business_agent import is_trip_plan_call
        return is_trip_plan_call(text)
    except Exception:  # pragma: no cover - 循环 import / 异常时退回原行为
        return False


# ============================================================
# 数据获取
# ============================================================
def _fetch_weather(city: str = DEFAULT_CITY) -> Optional[dict]:
    """调高德天气接口并归一成 {live, casts}；失败返回 None（由调用方降级）。

    两次实测踩坑，写法都被钉在这里：
    ① extensions 语义互斥：base → 只回 lives（实时观测），all → 只回 forecasts（未来预报），
       **all 不含 lives**。早期只用 all 却去读 lives，导致"有 Key 有网络"也永远取不到数据。
    ② 个人开发者 Key 有**并发 QPS 上限**，两路并发会直接返回
       AMAP_API_ERROR[10021] CUQPS_HAS_EXCEEDED_THE_LIMIT（实测复现）。
    ⇒ 所以这里**只发一次请求**：优先 all（一次拿全未来 3 天，含今天，是"来不来玩"的主数据源），
      只有它取不到预报时才补一次 base 兜底拿实时观测。
      live 字段在 casts 路径下由今天那条预报降级填充（口径不同，措辞也会标成"今日天气"）。
    """
    from tools.amap_tools import _amap_get  # 函数内 import：见模块头说明

    try:
        d = _amap_get("/weather/weatherInfo",
                      {"city": city, "extensions": "all", "output": "json"})
    except Exception as e:
        logger.warning(f"天气接口调用异常: {e}")
        return None

    casts = []
    if str((d or {}).get("status")) == "1":
        forecasts = d.get("forecasts") or []
        casts = (forecasts[0].get("casts") if forecasts else []) or []

    if casts:
        c = casts[0]
        live = {"city": city, "weather": c.get("dayweather"),
                "temperature": c.get("daytemp"), "temp_low": c.get("nighttemp"),
                "winddirection": c.get("daywind"), "windpower": c.get("daypower"),
                "humidity": "", "reporttime": c.get("date"), "from_forecast": True}
        return {"live": live, "casts": casts}

    # 预报拿不到（或接口异常）→ 补一次实时观测兜底
    logger.info(f"天气预报取数失败，改用实时观测兜底: {city} - {(d or {}).get('info')}")
    try:
        d2 = _amap_get("/weather/weatherInfo",
                       {"city": city, "extensions": "base", "output": "json"})
    except Exception as e:
        logger.warning(f"天气实时接口调用异常: {e}")
        return None
    if str((d2 or {}).get("status")) != "1":
        logger.info(f"天气实时查询亦未成功（降级）: {city} - {(d2 or {}).get('info')}")
        return None
    lives = d2.get("lives") or []
    if not lives:
        return None
    return {"live": lives[0], "casts": []}


# ============================================================
# 规则化结论（0 LLM）：数字 → 出游决策
# ============================================================
def _num(value, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace("℃", ""))
    except Exception:
        return default


def _wind_power(text) -> int:
    """风力文本 → 数字（"≤3" → 3，"4-5" → 5）"""
    digits = [int(c) for c in str(text or "") if c.isdigit()]
    return max(digits) if digits else 0


# ============================================================
# 特殊天气闭园 + 室内备选（与知识库 6.2 口径同一份）
# ------------------------------------------------------------
# 知识库「下雨天开园吗/暴雨大风天开园吗」条目写的原文是：
#   「## 六、营业时间 / ### 6.2 特殊天气闭园说明 / 恶劣暴雨大风天气会临时闭园。」
# 而旧话术在暴雨天只说「可能暂停开放」——**比园区自己的规则还弱**，用户看不到
# 「可能不开园」这个最关键的结论。这里把它按原文口径接上。
#
# ⚠️ 室内备选是**真实存在的场馆**（知识库已收录），不是编的：
#   北京市怀柔区万达广场三号楼 happy go 室内游乐园（园区就在万达广场 2 号门旁）。
# ============================================================
INDOOR_ALTERNATIVE = "北京市怀柔区万达广场三号楼 happy go 室内游乐园"

# 恶劣档判据：知识库说的是"恶劣暴雨大风天气"
_SEVERE_KEYS = ("暴雨", "大雨", "雷阵雨", "雷雨", "大暴雨", "狂风", "暴风")
# 一般降水档：下雨但不至于闭园
_MILD_KEYS = ("雨", "雪", "雷", "冰雹")
_SEVERE_WIND = 6      # 风力 >= 6 级按"大风"并入恶劣档（知识库 6.2 含"大风"）


def _precip_level(weather: str, wind: int) -> str:
    """降水强度分级 → "none" / "mild" / "severe"。

    为什么必须分级：旧实现把"小雨"和"暴雨"一视同仁，都输出同一句
    「可能暂停开放」——结果暴雨天既不说"可能闭园"，又因为温度舒适而夸
    「很适合带孩子来玩室外项目」，自相矛盾。分级后恶劣档才能给出正确结论。
    """
    w = str(weather or "")
    if any(k in w for k in _SEVERE_KEYS) or wind >= _SEVERE_WIND:
        return "severe"
    if any(k in w for k in _MILD_KEYS):
        return "mild"
    return "none"

def _outdoor_advice(live: dict, casts: list) -> list:
    """按天气事实生成"来不来 / 怎么玩 / 带什么"的结论（纯规则，无 LLM）

    优先级与"来不来 > 玩什么"的业务常识一致：能否成行 → 穿衣 → 项目影响。
    降水分 none/mild/severe 三档：只有 severe 才说"可能闭园"，
    mild 只提醒带雨具——**不能一有雨就喊闭园**，那会让用户白跑还觉得系统爱吓人。
    """
    tips = []
    weather = str(live.get("weather") or "")
    temp = _num(live.get("temperature"), -999)
    wind = _wind_power(live.get("windpower"))
    level = _precip_level(weather, wind)
    rainy_now = level != "none"

    # 1. 降水：室外乐园最关键的判断（决定"来不来"，所以永远排第一句）
    rainy_next = [c for c in casts[:2]
                  if any(k in str(c.get("dayweather", "")) + str(c.get("nightweather", ""))
                         for k in ("雨", "雪", "雷"))]
    if level == "severe":
        # 与知识库 6.2 同一口径：「恶劣暴雨大风天气会临时闭园」
        # ⚠️ 不能说"当天有晴"——severe 也可能是「晴 + 7 级大风」，得按实际致因换词
        if any(k in weather for k in _MILD_KEYS):
            cond = f"当天有{weather}"
        else:
            cond = f"当天风力达 {wind} 级（{weather or '天气'}）"
        tips.append(f"⚠️ {cond}，园区是室外乐园，"
                    f"按园区规定「恶劣暴雨大风天气会临时闭园」，**很可能开不了园**，"
                    f"建议出门前先打园区电话确认，或改期前来。"
                    f"如果一定要出来，可以改去室内玩：{INDOOR_ALTERNATIVE}。"
                    f"已经买好的游园券不会作废，留着下次用就行。")
    elif level == "mild":
        tips.append(f"当天有{weather or '降水'}，室外的旋转木马、蹦床、大炮乐园等项目"
                    f"可能临时暂停开放，建议带上雨具、并留意现场通知。")
    elif rainy_next:
        day = rainy_next[0]
        tips.append(f"{day.get('date') or '次日'}有雨"
                    f"（{day.get('dayweather') or ''}），如果要来建议今天，或带上雨具。")
    else:
        tips.append("近期没有降水，室外项目正常开放的把握大，适合来玩。")

    # 2. 温度：影响"玩多久 / 要不要防晒补水"
    #    ⚠️ 降水时**不再输出"很适合来玩"**——那会与第 1 句的"可能暂停/可能闭园"
    #    直接打架（旧实现的实际输出就是这个矛盾：暴雨天夸"很适合带孩子来玩室外项目"）。
    if temp != -999:
        if temp >= 33:
            tips.append(f"当前 {temp:.0f}℃ 偏热，建议避开中午 11:00-15:00，"
                        "备好防晒和饮用水（园区为室外项目，暴晒明显）。")
        elif temp >= 28:
            tips.append(f"当前 {temp:.0f}℃ 较热，做好防晒；"
                        "建议上午或傍晚来，玩起来更舒服。")
        elif temp <= 5:
            tips.append(f"当前 {temp:.0f}℃ 较冷，建议穿厚外套，"
                        "孩子游玩时注意保暖，注意地面可能湿滑。")
        elif temp <= 18:
            tips.append(f"当前 {temp:.0f}℃ 偏凉，建议给孩子带一件防风薄外套"
                        "（园区为室外项目，体感会比市区低一些）。")
        elif rainy_now:
            tips.append(f"当前 {temp:.0f}℃ 温度不算冷，但当天有雨，"
                        "户外体感会偏凉，建议备一件薄外套。")
        else:
            tips.append(f"当前 {temp:.0f}℃，温度舒适，很适合带孩子来玩室外项目。")

    # 3. 风力：影响部分器械类项目（severe 档已在第 1 句说过闭园，不重复）
    if wind >= 5 and level != "severe":
        tips.append(f"风力约 {wind} 级偏大，小型过山车等器械类项目可能受安全限制停开，建议出发前确认。")

    # 4. 湿度：体感补充（可选，避免信息过载只在极端时提）
    humidity = _num(live.get("humidity"), -1)
    if humidity >= 85:
        tips.append(f"湿度 {humidity:.0f}% 较高，体感偏闷，注意补水。")
    return tips


def weather_brief_text(city: str = DEFAULT_CITY) -> Optional[str]:
    """纯天气问句的快速回答（0 LLM）。

    Returns: 回答文本；接口不可用时返回 None（调用方降级走 MapAgent / RAG）。
    """
    w = _fetch_weather(city)
    if not w or not w.get("live"):
        return None
    live, casts = w["live"], w.get("casts") or []

    city_name = live.get("city") or city
    if live.get("from_forecast"):
        # 由今天那条预报降级填充：口径是"今日预报"而非"实时观测"，措辞必须区分开
        head = (f"{city_name}今日天气：{live.get('weather') or '—'} "
                f"{live.get('temp_low') or '?'}~{live.get('temperature') or '?'}℃"
                f"（{live.get('winddirection') or ''}风{live.get('windpower') or ''}级）")
        next_days = casts[1:3]      # 今日已在上方，这里只看后续两天
    else:
        head = (f"{city_name}实时天气：{live.get('weather') or '—'} "
                f"{live.get('temperature') or '?'}℃"
                f"（{live.get('winddirection') or ''}风{live.get('windpower') or ''}级，"
                f"湿度 {live.get('humidity') or '?'}%）")
        next_days = casts[:2]

    lines = [head]
    if next_days:
        parts = []
        for c in next_days:
            day = (c.get("date") or "")[5:]  # 只保留月-日
            dayw = c.get("dayweather") or ""
            nightw = c.get("nightweather") or ""
            wtxt = dayw if dayw == nightw or not nightw else f"{dayw}转{nightw}"
            parts.append(f"{day} {wtxt} {c.get('nighttemp') or '?'}~{c.get('daytemp') or '?'}℃")
        if parts:
            lines.append("未来两天：" + "；".join(parts))

    tips = _outdoor_advice(live, casts)
    if tips:
        lines.append("出游建议：" + "".join(t if t.endswith("。") else t + "。" for t in tips))
    lines.append(f"（数据来自高德天气 · 更新于 {(live.get('reporttime') or '')[-8:]}）")
    return "\n".join(lines)


def get_weather_tools() -> list:
    """给 LLM 工具循环用的天气工具（OpenAI tools 格式 + run）。

    快速通道（weather_brief_text）已覆盖纯天气问句；本工具用于**复合问句**
    （如"明天下雨的话还能玩什么"），由业务 Agent 在推理过程中按需调用，
    避免为了天气再起一次地图 Agent。
    """
    def _run(args: dict) -> str:
        city = str(args.get("city") or DEFAULT_CITY).strip() or DEFAULT_CITY
        text = weather_brief_text(city)
        return text or f"暂时取不到{city}的实时天气，请稍后再试或直接看手机自带天气。"

    return [{
        "name": "get_weather",
        "description": (
            "查询指定城市（默认北京·园区所在城市）的实时天气与未来两天预报，"
            "并给出**园区出游建议**（是否适合来玩、要不要带伞、哪些室外项目可能受影响）。"
            "用户问题里同时包含天气与其他诉求（如「下雨的话玩什么」「明天热不热，适不适合带孩子」）时调用。"
            "纯天气问句由系统快速通道直接回答，无需调用本工具。"),
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string", "description": "城市名，默认北京"}},
            "required": []},
        "run": _run,
    }]
