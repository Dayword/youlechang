# ============================================================
# RouterAgent 意图路由（主 Agent 调度）
# ------------------------------------------------------------
# 为什么需要它：
#   原先三个 Agent 靠三张关键词表互相"抢"（map_agent.MAP_TRIGGER_KEYWORDS、
#   business_agent 的 7 张词表、orchestrator._analyze_complexity 的标点+长度规则），
#   属于零 LLM 决策的硬编码分流。漏召是必然的——"这张卡还能用吗"「今天适合带
#   5 岁孩子去吗」都不含关键词，直接掉进 RAG 由模型自由发挥；而且**每加一个新
#   Agent 都要往词表里补词**，越长越脆。
#
# 三级路由（核心设计：把 LLM 决策放在不拖慢热路径的位置）：
#   L1 规则快速通道：命中现有关键词表 → 直接返回，0 次 LLM、0ms 新增耗时；
#   L2 小模型单次分类：仅当 L1 全未命中 **且** 问句带"行动/实时数据"标记时才触发
#      （硬超时 1.5s，结果进 Redis 缓存 1h），把漏召的办事型问句捞回来；
#   L3 默认 RAG：L1/L2 都没结论 → 走原知识问答链路，行为与改动前完全一致。
#
# 硬约束（项目要求"检索速度不能变慢"）：
#   纯知识类问句（"营业时间是什么""旋转木马有什么规则"）不含行动标记，
#   不会触发 L2，**RAG 热路径一次 LLM 都不多**。
#   开关：ROUTER_LLM_ENABLED=0 可一键退回纯规则路由。
# ============================================================
import hashlib
import json
import logging
import re
import time
from typing import Dict

logger = logging.getLogger(__name__)

_JSON_RE = re.compile(r"\{.*?\}", re.S)

# 分类结果缓存版本号：改路由 prompt / 词表后 +1 即可让旧缓存失效
# v2：ROUTER_SYSTEM 补入"购买/办卡充值属 business"的判据，旧缓存里的 business 误判需重算
_CACHE_PREFIX = "router:v2:"

ROUTER_SYSTEM = """你是园区智能客服系统的意图路由器。只输出一行 JSON，不要解释、不要换行。

可选 agent 只有这几个：
- map：地址 / 位置 / 怎么去 / 路线 / 导航 / 周边 / 距离 / 天气
- business：要我们**替他办事**——购买游乐项目（"帮我买三个过山车"）、办卡充值（"我要充200"）、
  个人账户（余额、我的卡、签到）、失物查询、"剩X币能玩什么"
- account：订单与到账（付了钱没到账）、退卡退款、会员码/提币码、我的收藏、我的资料
- service：报失登记（我丢了东西）、意见反馈/投诉建议
- rag：园区规章制度、游玩须知、价格规则、营业时间、退卡政策等**知识与规则问答**
- chat：打招呼、闲聊、与园区无关的寒暄

判断准则：
- 用户说"帮我 / 我要 / 给我 + 买 / 办 / 充 / 订"这类**委托动作** → business（他要结果，不是要知识）；
- 只是"问规则问知识"（含"多少钱 / 怎么 / 有哪些 / 是什么"）→ rag。
- 拿不准一律选 rag。

输出格式：{"agent":"rag","intent":"8字内的中文意图","confidence":0.0~1.0}"""

# ------------------------------------------------------------------
# L2 触发门槛：问句带"**要你办事 / 要实时个人数据**"的强信号才值得花一次分类调用。
#
# 这是**失败即安全**的闸门，方向必须宁紧勿松：
#   漏判 → 退回 RAG，行为与改动前完全一致（没有损失）；
#   误判 → 给每个知识问答都加一次 LLM 往返（直接违反"检索速度不能变慢"）。
#
# 因此这里**刻意不收**"几点/多久/多少钱/价格/推荐/办/如何"这类通用疑问词——
# 它们几乎出现在每一条知识问答里（"营业时间是几点""旋转木马多少钱"），
# 收进来就等于给整个 RAG 热路径加了 1~1.5s 的固定开销。
# 这些问句本来就由各 Agent 的关键词表（L1）负责，不需要 L2 兜底。
# ------------------------------------------------------------------
ACTIONABLE_MARKERS = [
    # 第一人称 + 个人数据（规则表最容易漏的形态："我的X""我X的"）
    "我的", "我办", "我买", "我充", "我丢", "我有", "我卡",
    # 强办事动作：这些动作必须落到工具上，知识库答不了
    "到账", "到没到", "没到账", "退卡", "退款", "报失", "登记", "提交",
    "投诉", "反馈", "出码", "会员码", "核销", "扫码", "认领",
    # 口语化委托（用户在"要你干活"，不是在问知识）
    "帮我", "给我", "替我", "能不能", "可不可以", "还能", "还有", "还剩", "有没有",
    # 实时状态（知识库给不了，只能查库）
    "实时", "状态", "进度", "生效", "过期",
]
# "知识问句"形态：出现这些词说明用户在问规则/政策/流程，而不是要我们办事。
# 这类问题知识库里有确定答案、还能被 Redis 缓存，是 RAG 的主场，不该被兜底捞走。
_POLICY_HINTS = ["规则", "政策", "规定", "制度", "流程", "步骤", "是什么", "有哪些",
                 "什么条件", "怎么规定", "需要什么"]


def _looks_actionable(query: str) -> bool:
    """是否值得做一次 LLM 意图分类（命中任一行动标记即可）。

    宁可漏判（退回 RAG，行为与旧版一致）也不要误判（给每个知识问答都加一次
    LLM 往返，直接违反"检索速度不能变慢"）。
    """
    text = (query or "").strip()
    if not text or len(text) > 60:
        return False
    # 明显的"规则/政策/流程"咨询形态 → 一定不是要办事，直接判知识问答，
    # 连分类调用都省掉（"退卡规则是什么""办卡流程有哪些步骤"都走这里）
    if any(w in text for w in _POLICY_HINTS):
        return False
    return any(w in text for w in ACTIONABLE_MARKERS)


# ------------------------------------------------------------------
# 分类结果缓存（问句级，避免同一问句反复分类）
# ------------------------------------------------------------------
def _cache_key(query: str) -> str:
    norm = re.sub(r"\s+", "", (query or "").strip())
    return _CACHE_PREFIX + hashlib.md5(norm.encode("utf-8")).hexdigest()


def _cache_get(query: str):
    try:
        from core.cache import redis_cache
        if not redis_cache.available:
            return None
        raw = redis_cache.client.get(_cache_key(query))
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _cache_set(query: str, value: dict, ttl: int = 3600):
    try:
        from core.cache import redis_cache
        if not redis_cache.available:
            return
        redis_cache.client.setex(_cache_key(query), ttl, json.dumps(value, ensure_ascii=False))
    except Exception:
        pass


# ============================================================
# 主类
# ============================================================
class RouterAgent:
    """意图路由器：给 orchestrator 一个统一的"这问题该谁答"的答案。"""

    # ============== L1 规则快速通道 ==============
    @staticmethod
    def _rule_route(query: str) -> Dict:
        """复用各 Agent 已有的关键词判定（0 LLM）。

        顺序与 orchestrator 现有分流顺序保持一致（map > business/account/service），
        保证"路由结果不改变既有行为"。
        """
        try:
            from agents.map_agent import map_agent
            if map_agent.is_map_question(query):
                return {"agent": "map", "intent": "位置与出行", "confidence": 1.0,
                        "source": "rule"}
        except Exception:
            pass
        try:
            from tools.account_tools import is_account_query
            if is_account_query(query):
                return {"agent": "account", "intent": "账户管家", "confidence": 0.95,
                        "source": "rule"}
        except Exception:
            pass
        try:
            from tools.service_tools import is_service_query
            if is_service_query(query):
                return {"agent": "service", "intent": "服务工单", "confidence": 0.95,
                        "source": "rule"}
        except Exception:
            pass
        try:
            from agents.business_agent import is_business_question
            if is_business_question(query):
                return {"agent": "business", "intent": "园区业务", "confidence": 0.95,
                        "source": "rule"}
        except Exception:
            pass
        return {"agent": "rag", "intent": "", "confidence": 0.0, "source": "none"}

    # ============== L2 小模型单次分类 ==============
    @staticmethod
    def _llm_route(query: str) -> Dict:
        """单次 LLM 分类（硬超时 + 失败即退回 RAG）。

        不抛异常：任何失败都返回 agent=rag，让主链路按原逻辑走。
        """
        from config import LLM_TEXT_MODEL, ROUTER_TIMEOUT
        from core.llm_client import llm_client

        t0 = time.time()
        try:
            res = llm_client.get_llm().chat.completions.create(
                model=LLM_TEXT_MODEL,
                messages=[
                    {"role": "system", "content": ROUTER_SYSTEM},
                    {"role": "user", "content": query},
                ],
                temperature=0,
                max_tokens=96,      # 只够输出那行 JSON（中文 JSON 约 40~70 token，
                                    # 早期设 48 会被截断 → 解析失败退化成 rag，兜底失效）
                timeout=ROUTER_TIMEOUT,
            )
            raw = (res.choices[0].message.content or "").strip()
        except Exception as e:
            # 超时/网络/解析任何一种失败 → 退回 RAG（绝不阻断主链路）
            logger.info(f"🧭 Router LLM 分类失败（已退回 RAG）: {type(e).__name__}: {str(e)[:80]}")
            return {"agent": "rag", "intent": "", "confidence": 0.0, "source": "llm_failed"}

        m = _JSON_RE.search(raw)
        agent, intent, conf = "rag", "", 0.0
        if m:
            try:
                data = json.loads(m.group(0))
                agent = str(data.get("agent") or "rag").strip().lower()
                intent = str(data.get("intent") or "")[:12]
                conf = float(data.get("confidence") or 0.0)
            except Exception:
                pass
        if agent not in ("map", "business", "account", "service", "rag", "chat"):
            agent = "rag"
        elapsed = round(time.time() - t0, 3)
        logger.info(f"🧭 Router LLM: 「{query[:20]}」→ {agent}({intent}) "
                    f"conf={conf:.2f} {elapsed}s")
        return {"agent": agent, "intent": intent, "confidence": conf,
                "source": "llm", "elapsed": elapsed}

    # ============== 对外入口 ==============
    def route(self, query: str, use_llm: bool = None) -> Dict:
        """返回 {"agent","intent","confidence","source"}。

        agent ∈ map / business / account / service / rag / chat
        source ∈ rule / cache / llm / llm_failed / none
        """
        from config import ROUTER_LLM_ENABLED

        text = (query or "").strip()
        if not text:
            return {"agent": "rag", "intent": "", "confidence": 0.0, "source": "none"}

        # L1：关键词快速通道（0 LLM）
        rule = self._rule_route(text)
        if rule["source"] == "rule":
            return rule

        if use_llm is None:
            use_llm = ROUTER_LLM_ENABLED
        # L2 闸门：不带行动标记的问句直接判 RAG，**不花这次 LLM**
        if not use_llm or not _looks_actionable(text):
            return {"agent": "rag", "intent": "", "confidence": 0.0, "source": "none"}

        cached = _cache_get(text)
        if cached:
            return {**cached, "source": "cache"}

        result = self._llm_route(text)
        if result.get("source") == "llm":
            _cache_set(text, {k: v for k, v in result.items()
                              if k in ("agent", "intent", "confidence")})
        return result


# 全局单例
router_agent = RouterAgent()
