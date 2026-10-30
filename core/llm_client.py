# ============================================================
# LLM 客户端 - OpenAI 兼容接口（DeepSeek / 百炼）
# ============================================================
import contextvars
import logging

from openai import OpenAI
from config import (LLM_API_KEY, LLM_ENDPOINT, LLM_TEMPERATURE, LLM_TEXT_MODEL,
                    LLM_VISION_MODEL, LLM_DISABLE_THINKING, LLM_THINKING_PARAM)
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()

DEFAULT_TIMEOUT = 60  # 秒（业务工具循环/长回答流式需要足够超时；原先 30s 偏紧）

# ------------------------------------------------------------------
# 请求级 user_id：供应商侧的前缀缓存隔离键
# ------------------------------------------------------------
# DeepSeek 官方对 `user_id` 的定义是三种隔离（内容安全 / **KVCache** / 调度），
# 原文明确"**请不要在 user_id 中包含用户隐私信息**" —— 所以这里只放数据库自增 id
# 并加一个 "u" 前缀（官方要求字符集 [a-zA-Z0-9\-_]+），绝不放手机号 / openid。
# 用 contextvar 而不是全局变量：同一进程并发服务多用户，全局变量必然串号。
# ------------------------------------------------------------------
_llm_user: contextvars.ContextVar = contextvars.ContextVar("llm_user_id", default="")

# 前缀缓存命中观测（实测本网关确实回传，见 _probe_kv_usage.py）
_USAGE = {"calls": 0, "prompt_tokens": 0, "cached_tokens": 0}


def set_llm_user(user_id) -> None:
    """把当前请求的用户放进上下文；下游所有模型调用会自动带上隔离键。"""
    try:
        _llm_user.set(("u%d" % int(user_id)) if user_id else "")
    except (TypeError, ValueError):
        _llm_user.set("")


def llm_usage_stats() -> dict:
    """累计的 prompt / 命中 token（供看板或探针读取命中率）"""
    d = dict(_USAGE)
    pt = d.get("prompt_tokens") or 0
    d["hit_rate"] = round(100.0 * (d.get("cached_tokens") or 0) / pt, 2) if pt else 0.0
    return d


def _log_usage(resp, stream: bool = False) -> None:
    """记录一次调用的前缀缓存命中情况。

    只在**响应里真的带 usage**时记录（流式需上游支持 include_usage 才有），
    取不到就静默跳过 —— 绝不因为观测影响业务。
    """
    try:
        if stream:
            return
        u = getattr(resp, "usage", None)
        if u is None:
            return
        d = u.model_dump() if hasattr(u, "model_dump") else dict(u)
        pt = int(d.get("prompt_tokens") or 0)
        hit = int(d.get("prompt_cache_hit_tokens")
                  or (d.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        _USAGE["calls"] += 1
        _USAGE["prompt_tokens"] += pt
        _USAGE["cached_tokens"] += hit
        if pt:
            logger.info("[LLM前缀缓存] prompt=%d 命中=%d (%.0f%%)", pt, hit, 100.0 * hit / pt)
    except Exception:
        pass


def thinking_extra_body() -> dict:
    """按配置返回"关闭推理模型思考"的请求体片段。

    文本模型（deepseek-v4-pro-202606）是推理模型：思考耗时与 token 都计入同一次调用。
    ⚠️ 本参数**是否生效取决于模型**，换文本模型后必须重测（实测方法见 _probe_v4_pro.py）：
      - deepseek/deepseek-flash（旧）：两套关思考参数**均不生效**，单次请求的 reasoning_tokens
        完全由 prompt 上下文决定 —— 无上下文时可达 3611 且把 max_tokens 吃光返回空串；
      - deepseek-v4-pro-202606（现用）：**生效**，实测 reasoning_tokens=0（同 prompt 不传
        该参数时是 147~432）。
    无论参数是否生效，都**不要**依赖它来兜住空回答：真正的保障是"生成 prompt 必须带参考资料"。
    返回 {} 表示不干预（LLM_DISABLE_THINKING=0 时）。
    """
    if not LLM_DISABLE_THINKING:
        return {}
    if LLM_THINKING_PARAM == "enable_thinking":
        return {"enable_thinking": False}
    return {"thinking": {"type": "disabled"}}


class LLMClient:
    """LLM 大模型客户端单例
    路由约定：
      - 纯文字（RAG 生成/改写/校验/工具调用）→ 文本模型 LLM_TEXT_MODEL（deepseek-v4-pro-202606）
      - 用户上传图片（/chat/vision）→ 视觉模型 LLM_VISION_MODEL（deepseek-v4-flash-vision-exp）
    单次调用可传 model 覆盖默认路由（如多轮图片追问仍走视觉模型）

    时延约定：所有底层 chat.completions.create 都会被注入"关闭思考"参数
    （见 _install_thinking_switch），reasoning tokens 不再挤占 max_tokens 预算。
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._client = None
        return cls._instance

    @property
    def client(self):
        if self._client is None:
            self._client = OpenAI(
                api_key=LLM_API_KEY,
                base_url=LLM_ENDPOINT,
                timeout=DEFAULT_TIMEOUT,
                max_retries=1
            )
            self._install_thinking_switch(self._client)
        return self._client

    @staticmethod
    def _install_thinking_switch(client: OpenAI) -> None:
        """包装 chat.completions.create，统一注入「关闭思考」参数 + 缓存隔离键。

        在这里做（而不是逐个调用点改）的好处：invoke / stream / invoke_vision 以及
        get_llm() 拿到原始客户端的调用方（业务 Agent 的工具循环）全部自动生效。
        """
        extra = thinking_extra_body()
        completions = client.chat.completions
        original = completions.create

        def create_with_thinking_switch(*args, **kwargs):
            body = dict(kwargs.get("extra_body") or {})
            for k, v in extra.items():
                body.setdefault(k, v)
            uid = _llm_user.get()
            if uid:
                body.setdefault("user_id", uid)
            if body:
                kwargs["extra_body"] = body
            is_stream = bool(kwargs.get("stream"))
            resp = original(*args, **kwargs)
            _log_usage(resp, stream=is_stream)
            return resp

        completions.create = create_with_thinking_switch

    def invoke(self, prompt: str, system: str = None, model: str = None,
               max_tokens: int = None) -> str:
        """同步调用 LLM（纯文字，默认文本模型），返回完整回复"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)

        res = self.client.chat.completions.create(
            model=model or LLM_TEXT_MODEL,
            messages=messages,
            temperature=LLM_TEMPERATURE,
            **kwargs,
        )
        return res.choices[0].message.content.strip()

    def invoke_vision(self, prompt: str, image_data_url: str, system: str = None,
                      max_tokens: int = None, timeout: int = None) -> str:
        """
        原生多模态调用（图片直接入参，不经 OCR）。
        Args:
            prompt: 文字提示
            image_data_url: data:image/jpeg;base64,xxx
            system: 可选系统提示
            max_tokens: 生成上限。结构化识别类调用务必给（默认不限制，
                        模型可能长篇描述图片，白等好几秒）
            timeout: 覆盖默认超时（秒）。识别类调用给 30s 即可；
                        不传则用 DEFAULT_TIMEOUT（60s，适合对话式图片问答）
        Returns:
            模型回复文本
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        })
        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)
        if timeout:
            kwargs["timeout"] = int(timeout)
        res = self.client.chat.completions.create(
            model=LLM_VISION_MODEL,  # 图片/多模态必须走视觉模型
            messages=messages,
            temperature=LLM_TEMPERATURE,
            **kwargs,
        )
        return (res.choices[0].message.content or "").strip()

    def stream(self, prompt: str, system: str = None, model: str = None,
               max_tokens: int = None):
        """流式调用 LLM（纯文字，默认文本模型），yield 每个 token

        max_tokens: 生成上限，用于控制回答长度进而控制延迟（实测约 43 字/秒）
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs = {}
        if max_tokens:
            kwargs["max_tokens"] = int(max_tokens)

        stream = self.client.chat.completions.create(
            model=model or LLM_TEXT_MODEL,
            messages=messages,
            temperature=LLM_TEMPERATURE,
            stream=True,
            **kwargs,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content

    def get_llm(self):
        """获取原始 OpenAI 客户端实例"""
        return self.client


# 全局单例
llm_client = LLMClient()
