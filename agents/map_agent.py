# ============================================================
# 地图 Agent (MapAgent)
# 基于 LangChain AgentExecutor + 高德地图 Tool 集
# ------------------------------------------------------------
# 职责：
# 1. 意图预判：关键词路由判断用户问题是否涉及地址/位置/路线/周边
# 2. Agent 执行：AgentExecutor 自动选择调用高德工具 → 结构化数据 → LLM 整理回答
# 3. 降级兜底：高德超时/异常/配额耗尽时返回 fallback，由 Orchestrator 走 RAG
# 4. 地图链接/静态图由工具函数生成（amap_state 回读），模型不编造链接
# ============================================================
import logging
import threading
import time
from typing import Dict

logger = logging.getLogger(__name__)

# 地图意图触发关键词（涉及地址/位置/路线/周边/天气等）
MAP_TRIGGER_KEYWORDS = [
    "地址", "位置", "在哪", "哪里", "在哪儿", "怎么走", "怎么去", "路线", "导航",
    "周边", "附近", "乘车", "公交", "地铁", "自驾", "多远", "路程", "地图",
    "坐标", "经纬度", "天气", "定位", "到达", "前往", "方向", "距离", "几公里",
    "多少公里", "几站", "怎么到", "如何去", "位置在哪", "在哪里",
    "要开多久", "耗时多久",
]
# "多久/多长时间"是歧义词：问"项目一次能玩多久"是游玩时长（不是要路线），
# 只有和出行语境同时出现时才算地图问题——否则每次都要白跑一趟地图 Agent（约 2~3s）。
_DURATION_WORDS = ("多久", "多长时间", "耗时")
_TRAVEL_WORDS = ("去", "到", "走", "开", "自驾", "过去", "路上", "坐车", "打车",
                 "公交", "地铁", "车程", "你们那里", "你们那里", "园区怎么")

# 本园区唯一门店固定信息（用户问"你们店在哪/怎么去你们那里"时必须直接使用）
PARK_NAME = "大黄鸭室外游乐园"
PARK_ADDRESS = "北京市怀柔区万达广场2号门"
PARK_LOCATION = "116.630026,40.317475"  # 高德地理编码门址坐标（经度,纬度）

# Agent 系统 Prompt：告知模型地图工具能力，明确链接由工具生成
SYSTEM_PROMPT = f"""你是"{PARK_NAME}"的智能问答助手，具备调用高德地图工具的能力，可以为用户提供地址定位、路线规划、周边推荐和天气查询服务。

【本园区固定信息（全城唯一门店，必须牢记）】
- 园区名称：{PARK_NAME}
- 固定地址：{PARK_ADDRESS}
- 园区坐标：{PARK_LOCATION}（经度,纬度）
- 本园区全城仅此一家门店，不存在其他分店或同名园区

【你的地图工具能力】
- 地理编码：把文字地址转换为经纬度坐标；
- 逆地理编码：把经纬度坐标转换为详细地址；
- POI搜索：按关键字查找地点（游乐场、餐厅、酒店等）；
- 周边搜索：查找某坐标周边的设施；
- 路径规划：驾车/步行/公交路线规划，返回距离和预计耗时；
- 静态地图：生成地点位置的地图图片；
- 天气查询：查询城市实时天气；
- 输入提示：地址联想补全。

【必须遵守的规则】
1. 只要用户问题涉及地址、位置、坐标、路线、导航、周边、附近、距离、天气等实时信息，你必须先调用对应地图工具获取数据，禁止凭自身知识直接回答或编造地址、坐标、距离、耗时；
2. 地图跳转链接（uri.amap.com）和静态地图图片地址只能引用工具返回的结果，严禁自己编造任何链接或坐标；
3. 工具返回的结构化数据（地点名称、地址、电话、坐标、路线距离与耗时、天气）由你整理成口语化、简洁自然的回答；
4. 如果工具返回失败或异常，如实告诉用户"暂时无法获取实时地图信息，请稍后再试"，不要编造数据；
5. 回答贴合游乐场场景（入园路线、周边餐饮、停车、天气建议等），单个回答控制在3-5句话以内；
6. 回答中如提到地点，可顺带告知用户"可在下方地图中查看/导航"。

【本园区位置类问题的强制规则（最高优先级）】
- 当用户询问"你们店在哪里"、"你们在什么位置"、"园区在哪"、"怎么去你们那里"、"去你们那里要多久"等关于本园区自身位置的问题时：
  1. 你必须直接调用地理编码工具(amap_geocode)，对固定地址"{PARK_ADDRESS}"定位，然后基于返回结果告知用户园区地址；
  2. 严禁调用 POI 搜索去搜索"游乐场"等其他地点，严禁反问用户"您指的是哪一家"或请求用户确认——本园区只有一家，地址唯一，直接回答即可；
  3. 若用户问"怎么去/多久能到"但未提供出发位置：先完成园区定位，再回答园区地址，并友好提示"告诉我您从哪里出发，我帮您查询路线和预计耗时"，不要因缺少出发点而拒绝回答或只反问；
  4. 若用户提供了出发位置，先用地理编码定位出发点，再用路径规划工具计算到"{PARK_ADDRESS}"（坐标 {PARK_LOCATION}）的路线，回答距离与耗时。
"""


class MapAgent:
    """
    高德地图 Agent（LangChain AgentExecutor 封装）
    """

    def __init__(self):
        self._executor = None
        self._lock = threading.Lock()
        self._loaded = False

    # ============== 意图预判 ==============

    def is_map_question(self, query: str, history: str = "") -> bool:
        """
        关键词预判：用户问题是否涉及地址/位置/路线/周边/天气

        延迟/准确性说明：**只按本轮问句判断，不拼接历史**。历史是多轮长文本，
        只要之前聊过"哪里/多久/天气"等词，后续**任何**问句（如"怎么办卡充值"）
        都会被误判为地图问题，被地图 Agent 抢走 10s+ 且回答跑偏。
        真实的指代式追问（"那怎么去呢""多远呢"）本身就含地图关键词，按问句判断即可命中。
        """
        q = (query or "").strip()
        if not q:
            return False
        if any(k in q for k in MAP_TRIGGER_KEYWORDS):
            return True
        # 歧义词消歧："多久/多长时间/耗时"单独出现时通常是"游玩时长"（走 RAG 更准也更快），
        # 只有同时带出行语境（去/到/开车/公交…）才按地图-路线问题处理
        if any(d in q for d in _DURATION_WORDS) and any(t in q for t in _TRAVEL_WORDS):
            return True
        return False

    # ============== Agent 构建（懒加载） ==============

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            from langchain_openai import ChatOpenAI
            from langchain.agents import AgentExecutor, create_tool_calling_agent
            from langchain_core.prompts import ChatPromptTemplate
            from config import LLM_API_KEY, LLM_ENDPOINT, LLM_TEXT_MODEL, LLM_TEMPERATURE
            from core.llm_client import thinking_extra_body
            from tools.amap_tools import get_amap_tools

            # 与 core.llm_client 保持一致：关闭推理模型的"思考"以降低时延
            # （思考 token 会挤占 max_tokens 预算且显著拉长单次往返）
            _extra_body = thinking_extra_body()
            llm = ChatOpenAI(
                model=LLM_TEXT_MODEL,
                api_key=LLM_API_KEY,
                base_url=LLM_ENDPOINT,
                temperature=LLM_TEMPERATURE,
                timeout=60,
                max_retries=1,
                **({"extra_body": _extra_body} if _extra_body else {}),
            )
            self._tools = get_amap_tools()
            prompt = ChatPromptTemplate.from_messages([
                ("system", SYSTEM_PROMPT),
                # 强制要求：涉及位置/路线/天气必须先调用工具，避免模型凭知识编造
                ("human", "{input}\n\n【强制要求】如果问题涉及地址、位置、路线、周边、距离、天气，你必须先调用相关地图工具获取真实数据，再基于工具结果回答。"),
                # tool-calling agent 的 scratchpad 是消息列表，需用 placeholder
                ("placeholder", "{agent_scratchpad}"),
            ])
            # 注: DeepSeek 对 ReAct 文本格式遵循不稳定，改用 OpenAI 兼容的 tool-calling agent
            agent = create_tool_calling_agent(llm, self._tools, prompt)
            self._executor = AgentExecutor(
                agent=agent,
                tools=self._tools,
                handle_parsing_errors=True,
                max_iterations=6,
                max_execution_time=45,
                verbose=False,
                # 必须返回工具调用步骤，MapAgent 据此判断是否实际调用了地图工具
                return_intermediate_steps=True,
            )
            self._loaded = True
            logger.info(f"🗺️ 地图Agent已加载，注册 {len(self._tools)} 个高德工具")

    # ============== 主流程 ==============

    def process(self, user_query: str, history: str = "", progress_cb=None,
                profile_note: str = "") -> Dict:
        """
        执行地图 Agent 完整流程
        progress_cb: 可选回调 cb(text)，Agent 各阶段实时推送思考状态（供前端展示思考过程）
        profile_note: 用户画像摘要。系统 Prompt 是**一次性构建并缓存**的（_ensure_loaded），
            所以画像只能从 {input} 这个唯一的每请求变量注入 —— 不重排 prompt、不改缓存。
        返回: {"query", "search_query", "answer", "references",
               "map_url", "map_image", "extra_info",
               "used_map_tool", "fallback", "trace"}
        fallback=True 表示未使用地图工具（或失败），由 Orchestrator 降级走 RAG
        """
        start = time.time()

        # 0. 配额检查：达到每日阈值直接降级，不再消耗 LLM 调用
        from tools.amap_tools import amap_quota_exceeded, amap_state, set_progress_cb
        if amap_quota_exceeded():
            logger.warning("🗺️ 高德当日配额已达阈值，降级为纯RAG文本回答")
            return self._fallback_result(user_query, history, start, "quota_exceeded")

        # 0.5 园区自身位置/导航类问题 → 直接应答（0 次 LLM，~0.1s）
        # 园区只有一家门店、地址与坐标是常量，系统 Prompt 也要求"必须直接使用、不得反问"。
        # 走 AgentExecutor 需要 2~3 次 LLM 往返（实测 16~18s），对这类固定信息纯属浪费。
        quick = self._park_self_location_answer(user_query)
        if quick is not None:
            logger.info(f"🗺️ 命中园区自身位置快速通道，耗时 {round(time.time()-start, 3)}s")
            return quick

        # 注册进度回调：工具内部调用高德接口时实时上报思考状态
        if progress_cb:
            set_progress_cb(progress_cb)

        # 1. 运行 AgentExecutor
        try:
            self._ensure_loaded()
            if progress_cb:
                progress_cb("🧠 正在分析问题，选择合适的地图工具...")
            # 画像并进 {input}：只影响"怎么答"，不影响选哪个工具/是否调工具
            _input = user_query
            if profile_note:
                _input += (f"\n\n【用户档案】（来自之前对话，回答可自然结合，"
                           f"不要生硬罗列；与位置/路线无关时不必提及）\n{profile_note}")
            result = self._executor.invoke({"input": _input})
            if progress_cb:
                progress_cb("✍️ 已获取数据，正在整理回答...")
        except Exception as e:
            logger.error(f"🗺️ 地图Agent执行异常: {e}")
            return self._fallback_result(user_query, history, start, f"agent_error: {str(e)[:120]}")
        finally:
            if progress_cb:
                set_progress_cb(None)

        # 2. 判断是否实际调用了地图工具
        steps = result.get("intermediate_steps") or []
        answer = (result.get("output") or "").strip()
        if not steps:
            # Agent 判断不需要地图工具 → 交给 RAG（保证答案有知识库支撑）
            logger.info("🗺️ Agent未调用任何地图工具，交由RAG处理")
            return self._fallback_result(user_query, history, start, "no_tool_used")
        if not answer:
            return self._fallback_result(user_query, history, start, "empty_answer")

        # 3. 回读工具生成的地图资源（链接/图片由 Python 工具生成，非模型编造）
        state = amap_state.get()
        elapsed = round(time.time() - start, 3)
        logger.info(f"🗺️ 地图Agent完成 | 调用工具 {len(steps)} 次 | 耗时 {elapsed}s")
        return {
            "query": user_query,
            "search_query": user_query,
            "answer": answer,
            "references": [],
            "map_url": state.get("map_url", ""),
            "map_image": state.get("map_image", ""),
            "extra_info": state.get("extra_info", {}) or {},
            "used_map_tool": True,
            "fallback": False,
            "trace": {
                "agent": "map",
                "tool_calls": len(steps),
                "elapsed": elapsed,
            },
        }

    # 园区自身位置类问题（固定信息、无需外部实时数据）
    _SELF_LOCATION_MARKERS = ("你们", "园区", "乐园", "游乐场", "这里", "店内", "店里", "你们家")
    _SELF_LOCATION_ASKS = ("在哪", "位置", "地址", "怎么去", "怎么走", "怎么到", "导航", "坐标")
    # 提到这些词的说明问的是"别的地方"（如"附近哪有餐厅"），不能走快速通道
    _OTHER_PLACE_HINTS = ("附近", "周边", "餐厅", "饭店", "酒店", "停车场在哪", "公交站",
                          "地铁站", "哪里有", "哪有", "别的", "其他", "推荐", "对比")

    def _park_self_location_answer(self, user_query: str) -> Dict:
        """园区自身位置/导航快速通道：直接用固定地址与坐标应答，省掉 2~3 次 LLM 往返。

        仅命中"问本园区自己在哪"的问句；一旦出现"附近/周边/哪有餐厅"等第三方地点诉求，
        返回 None 交给 AgentExecutor 正常处理。
        """
        q = (user_query or "").strip()
        if not q:
            return None
        if not any(m in q for m in self._SELF_LOCATION_MARKERS):
            return None
        if not any(a in q for a in self._SELF_LOCATION_ASKS):
            return None
        if any(h in q for h in self._OTHER_PLACE_HINTS):
            return None
        # "去你们那里要多久"这类缺出发地的问题也在这里兜底（原始实现要求先定位再友好追问）
        try:
            from tools.amap_tools import build_navigation_url, build_staticmap_url, amap_state
            map_url = build_navigation_url(PARK_LOCATION.split(",")[0],
                                           PARK_LOCATION.split(",")[1], PARK_NAME)
            map_image = build_staticmap_url(PARK_LOCATION, markers=f"mid,,A:{PARK_LOCATION}")
        except Exception as e:
            logger.warning(f"园区位置快速通道生成地图资源失败: {e}")
            map_url, map_image = "", ""
            from tools.amap_tools import amap_state  # noqa: F811
        amap_state.set(map_url=map_url, map_image=map_image,
                       extra_info={"staticmap": {"location": PARK_LOCATION,
                                                 "source": "park_self_location"}})
        answer = (f"🎡 我们「{PARK_NAME}」就位于 {PARK_ADDRESS}，全城仅此一家哦～\n"
                  f"您可以在下方地图卡片中直接查看位置或点「打开地图 / 导航」出发。\n"
                  f"告诉我您从哪出发，我还能帮您算路线和预计耗时～")
        return {
            "query": user_query,
            "search_query": user_query,
            "answer": answer,
            "references": [],
            "map_url": map_url,
            "map_image": map_image,
            "extra_info": {"staticmap": {"location": PARK_LOCATION,
                                         "source": "park_self_location"}},
            "used_map_tool": True,
            "fallback": False,
            "trace": {"agent": "map", "tool_calls": 0, "fast_path": "park_self_location",
                      "elapsed": 0.0},
        }

    @staticmethod
    def _fallback_result(user_query: str, history: str, start: float, reason: str) -> Dict:
        """降级结果：由 Orchestrator 继续执行 RAG 检索（原始地址文本返回）"""
        return {
            "query": user_query,
            "search_query": user_query,
            "answer": "",
            "references": [],
            "map_url": "",
            "map_image": "",
            "extra_info": {},
            "used_map_tool": False,
            "fallback": True,
            "trace": {
                "agent": "map",
                "fallback_reason": reason,
                "elapsed": round(time.time() - start, 3),
            },
        }


# 全局单例
map_agent = MapAgent()
