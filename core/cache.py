# ============================================================
# Redis 热点缓存 - 问答结果缓存 + 会话管理 + 问答日志
# ============================================================
import redis
import json
import hashlib
import logging
import time
from config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB, CACHE_TTL
import threading
import jieba.posseg as pseg

# 与 BM25 检索共用同一份业务词典（幂等注册），
# 确保无论模块加载顺序如何，关键词提取结果一致、缓存 key 稳定
from core.keywords import ensure_business_words, BUSINESS_WORDS_SET

ensure_business_words()

_logger = logging.getLogger("redis")
_lock = threading.Lock()

# 停用词表：语义无关的功能词
_STOP_WORDS = {
    "的", "了", "吗", "在", "怎么", "如何", "是", "我", "你", "他", "她",
    "它", "们", "不", "很", "就", "都", "也", "还", "要", "会", "能",
    "可以", "这个", "那个", "什么", "哪个", "怎样", "为什么", "多少",
    "和", "与", "或", "但", "而", "及", "被", "把", "让", "给",
    "对", "从", "到", "往", "向", "在", "呢", "啊", "吧", "呀",
}

# 保留的词性标签：名词、动词、专名、英文
_KEEP_POS = {"n", "nr", "ns", "nt", "nz", "v", "vd", "vn", "eng"}


def profile_tag(profile_note: str) -> str:
    """画像指纹 → 问答缓存 key 的一级命名空间。

    为什么必须有：画像注入生成上下文后，同一条问句对不同家庭的答案是**不一样的**
    （「您 6 岁的女儿可以玩…」）。而问答缓存只按 question 提取关键词做 key，
    若带画像的回答沿用公共 key，A 家的孩子信息会被 B 家读到（串用户）。

    规则：
      - 无画像 → 返回 ""，key 格式与改造前完全一致（历史缓存不失效、命中率不掉）；
      - 有画像 → 返回 note 的 8 位短 hash；只有画像摘要相同的请求才互相命中，
        而画像相同 ⇒ 定制化内容本就相同，命中是安全且正确的。
    """
    if not profile_note:
        return ""
    note = profile_note.strip()
    if not note:
        return ""
    return hashlib.md5(note.encode("utf-8")).hexdigest()[:8]


class RedisCache:
    """Redis 缓存客户端 — 含完整日志与统计"""
    _instance = None
    _FAIL_COOLDOWN = 30  # 秒：Redis 不可用后的冷却时间，避免每次访问都等连接超时

    def __new__(cls):
        if cls._instance is None:
            with _lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._client = None
                    cls._instance._unavailable_until = 0.0
        return cls._instance

    def _ensure_connected(self):
        """获取 Redis 连接。

        性能说明（延迟优化）：原实现**每次属性访问都 ping 一次**，Redis 正常时只是
        白白多一次往返；Redis 不可用时每次都要等 connect/socket 超时（3s），
        一次问答里会被调用 6~8 次，直接拖满整条链路。
        现在：连接一旦建立就直接复用（redis-py 自带连接池 + 断线自动重连），
        仅在操作抛错时标记不可用并进入 30s 冷却，避免反复重试拖慢问答。
        """
        if self._client is not None:
            return self._client
        if time.time() < self._unavailable_until:
            return None
        try:
            client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD if REDIS_PASSWORD else None,
                db=REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
                health_check_interval=30,
            )
            client.ping()
            self._client = client
        except Exception:
            self._client = None
            self._unavailable_until = time.time() + self._FAIL_COOLDOWN
        return self._client

    def _drop_client(self, exc: Exception = None):
        """操作异常时丢弃连接并进入冷却（下次访问会重建/或直接短路返回不可用）"""
        if exc is not None:
            self._log(f"⚠️ Redis 操作异常，暂时降级（{type(exc).__name__}: {str(exc)[:80]}）")
        self._client = None
        self._unavailable_until = time.time() + self._FAIL_COOLDOWN

    @property
    def client(self):
        return self._ensure_connected()

    @property
    def available(self) -> bool:
        return self._ensure_connected() is not None

    def _make_key(self, prefix: str, content: str, tag: str = "") -> str:
        """生成缓存 key — 基于关键词（语义归并）

        tag：可选命名空间（目前只用于问答缓存的画像指纹，见 profile_tag）。
             为空时 key 与改造前逐字节一致。
        """
        scope = f"{tag}:" if tag else ""
        keywords = self._extract_keywords(content)
        if not keywords:
            # 兜底：无有效关键词时回退 MD5
            h = hashlib.md5(content.encode()).hexdigest()
            return f"rag:{prefix}:{scope}{h}"
        kw_str = "_".join(keywords)
        self._log(f"🔑 关键词提取: '{content[:30]}...' → [{', '.join(keywords)}]")
        return f"rag:{prefix}:{scope}kwd_{kw_str}"

    def _extract_keywords(self, text: str) -> list:
        """
        中文分词 → 剔除停用词 → 筛名词/动词/专业词/业务词 → 排序去重
        
        例如：'我想知道怎么办卡充值' → ['怎么办卡', '充值']
        """
        words = []
        for word, flag in pseg.cut(text):
            word = word.strip()
            # 过滤空字符串、单字（可能无意义）、停用词
            if not word or len(word) < 2 or word in _STOP_WORDS:
                continue
            # 保留名词、动词、英文/专业词；自定义词性(x)但属于业务词典的也保留
            # （如"怎么办卡"被 jieba 标记为 x，是高价值检索词）
            if flag in _KEEP_POS or (flag == "x" and word in BUSINESS_WORDS_SET):
                words.append(word)
        # 排序去重
        unique = sorted(set(words))
        return unique

    def _log(self, msg: str):
        """统一 Redis 操作日志输出到控制台"""
        _logger.info(f"[Redis] {msg}")

    # ---------- 问答缓存 ----------
    def cache_answer(self, question: str, answer: str, references: list = None, ttl: int = None,
                     profile_note: str = ""):
        """缓存问答结果（仅有效回答时写入）

        references 一并缓存：这样缓存命中时前端「引用来源」和问答记录页照样有依据
        （历史缓存里没有该字段，读取时按空处理，向前兼容）。

        profile_note: 生成该答案时注入的用户画像摘要。**非空时 key 会带画像指纹**——
        凡是"看着用户档案说出来的话"都只能被同一画像的请求命中，否则会串用户。
        调用方只要在注入了画像的分支把同一个 prof_note 传进来即可（读取侧必须传同一个值）。
        """
        if not self.available:
            return
        if not answer or not answer.strip():
            self._log(f"⏭️ 跳过缓存：答案为空白")
            return
        if "暂时没有查询到相关信息" in answer:
            self._log(f"⏭️ 跳过缓存：无检索结果")
            return
        try:
            key = self._make_key("ans", question, profile_tag(profile_note))
            payload = {"answer": answer, "references": list(references or [])[:10]}
            self._client.setex(key, ttl or CACHE_TTL, json.dumps(payload, ensure_ascii=False))
            self._log(f"💾 问答缓存写入 key={key}")
        except Exception as e:
            # 写入失败不影响回答主流程（Redis 抖动/重启）
            self._drop_client(e)

    def get_cached_entry(self, question: str, profile_note: str = ""):
        """获取缓存的问答条目 {"answer", "references"}（命中时刷新 TTL），未命中返回 None

        profile_note 必须与写入时一致：有画像就查画像命名空间，没画像就查公共命名空间。
        """
        if not self.available:
            return None
        try:
            key = self._make_key("ans", question, profile_tag(profile_note))
            data = self._client.get(key)
            if data:
                self._client.expire(key, CACHE_TTL)  # 刷新过期时长
                self._log(f"🎯 问答缓存命中 key={key} (TTL已刷新)")
                entry = json.loads(data)
                return {"answer": entry.get("answer") or "",
                        "references": entry.get("references") or []}
        except Exception as e:
            # 缓存不可用不能影响问答：降级为"未命中"
            self._drop_client(e)
        return None

    def get_cached_answer(self, question: str, profile_note: str = "") -> str:
        """获取缓存的答案文本（命中时刷新TTL）；只关心文本的调用方用它即可"""
        entry = self.get_cached_entry(question, profile_note)
        return entry.get("answer") if entry else None

    # ---------- 检索缓存 ----------
    def cache_search(self, query: str, results: list, ttl: int = None):
        """缓存检索结果（仅有效结果时写入）"""
        if not self.available:
            return
        if not results:
            self._log(f"⏭️ 跳过检索缓存：无命中结果")
            return
        try:
            key = self._make_key("search", query)
            self._client.setex(key, ttl or CACHE_TTL, json.dumps({"results": results}, ensure_ascii=False))
            self._log(f"🔍 检索缓存写入 key={key} ({len(results)} 条结果)")
        except Exception as e:
            self._drop_client(e)

    def get_cached_search(self, query: str) -> list:
        """获取缓存的检索结果（命中时刷新TTL）"""
        if not self.available:
            return None
        try:
            key = self._make_key("search", query)
            data = self._client.get(key)
            if data:
                self._client.expire(key, CACHE_TTL)  # 刷新过期时长
                self._log(f"🎯 检索缓存命中 key={key} (TTL已刷新)")
                return json.loads(data).get("results")
        except Exception as e:
            self._drop_client(e)
        return None

    # ---------- 会话历史 ----------
    def save_history(self, session_id: str, history: str, ttl: int = 3600):
        """保存会话历史"""
        if not self.available:
            return
        key = f"rag:session:{session_id}"
        self._client.setex(key, ttl, history)
        self._log(f"💬 会话历史已保存 session={session_id} (TTL={ttl}s)")

    def get_history(self, session_id: str) -> str:
        """获取会话历史"""
        if not self.available:
            return ""
        key = f"rag:session:{session_id}"
        val = self._client.get(key) or ""
        if val:
            self._log(f"📖 会话历史已加载 session={session_id}")
        return val

    # ---------- 请求频控 ----------
    def rate_limit_hit(self, scope: str, max_req: int, window: int = 60) -> bool:
        """计数窗口频控：scope 是调用方拼好的标识（桶名 + 身份），返回 True 表示放行。

        各接口的桶名由调用方决定（如 "write:user:12"、"vision:ip:1.2.3.4"），
        互不干扰；Redis 不可用时一律放行，不因为缓存挂掉而阻断业务。
        """
        if not self.available:
            return True
        try:
            key = f"rag:ratelimit:{scope}"
            current = self._client.incr(key)
            if current == 1:
                self._client.expire(key, window)
            return current <= max_req
        except Exception as e:
            self._drop_client(e)
            return True

    def rate_limit_check(self, ip: str, max_req: int = 30, window: int = 60) -> bool:
        """按 IP 频控（预热与直接调用方使用）"""
        return self.rate_limit_hit(f"ip:{ip}", max_req, window)

    # ---------- 统计 ----------
    def incr_counter(self, name: str, ttl: int = 3600):
        """递增计数器，TTL 默认 1 小时"""
        if not self.available:
            return
        try:
            key = f"rag:stats:{name}"
            self._client.incr(key)
            self._client.expire(key, ttl)
        except Exception as e:
            self._drop_client(e)

    def get_counter(self, name: str) -> int:
        """获取计数器"""
        if not self.available:
            return 0
        val = self._client.get(f"rag:stats:{name}")
        return int(val) if val else 0

    def get_all_stats(self) -> dict:
        """获取所有统计指标"""
        if not self.available:
            return {}
        keys = self._client.keys("rag:stats:*")
        result = {}
        for k in keys:
            name = k.decode() if isinstance(k, bytes) else k
            name = name.replace("rag:stats:", "")
            result[name] = int(self._client.get(k) or 0)
        return result

    # ---------- 通用 KV（小块配置/写死数据） ----------
    # 给"要长期留着、不该被统计刷掉"的小数据用（如快捷提问的固定 6 条）。
    # 不设 TTL：它是配置而不是缓存，flush_cache() 的清理清单里也刻意不含这个前缀。
    def get_json(self, key: str):
        """读一个 JSON 值；不存在 / Redis 不可用 → None"""
        if not self.available:
            return None
        try:
            raw = self._client.get(key)
            if not raw:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return json.loads(raw)
        except Exception as e:
            self._drop_client(e)
            return None

    def set_json(self, key: str, value, ttl: int = None) -> bool:
        """写一个 JSON 值；ttl=None 表示不过期"""
        if not self.available:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False)
            if ttl:
                self._client.setex(key, int(ttl), payload)
            else:
                self._client.set(key, payload)
            self._log(f"💾 写死数据写入 key={key}")
            return True
        except Exception as e:
            self._drop_client(e)
            return False

    def delete_key(self, key: str) -> bool:
        if not self.available:
            return False
        try:
            self._client.delete(key)
            self._log(f"🗑️ 删除 key={key}")
            return True
        except Exception as e:
            self._drop_client(e)
            return False

    # ---------- 缓存刷新 ----------
    def flush_cache(self, keep_stats: bool = True, keep_logs: bool = True):
        """
        清空所有 rag:* 缓存（问答缓存 + 检索缓存 + 会话历史）
        Args:
            keep_stats: 保留统计计数器（默认 True）
            keep_logs: 保留问答日志（默认 True）
        """
        if not self.available:
            self._log("⚠️ Redis 不可用，跳过缓存刷新")
            return 0

        patterns = []
        if not keep_stats:
            patterns.append("rag:stats:*")
        if not keep_logs:
            patterns.append("rag:question_log")

        # 需要清除的模式：问答缓存、检索缓存、会话历史
        clear_patterns = ["rag:ans:*", "rag:search:*", "rag:session:*"] + patterns

        deleted = 0
        for pattern in clear_patterns:
            keys = list(self._client.scan_iter(match=pattern, count=100))
            if keys:
                deleted += self._client.delete(*keys)

        self._log(f"🗑️ 缓存已刷新 | 删除 {deleted} 个 key | 原因：知识库文档变更")
        return deleted

    # ---------- 问答日志（完整记录） ----------
    def save_question_log(self, question: str, answer: str, references: list,
                           elapsed: float, search_query: str = "",
                           session_id: str = "", ttl: int = 7 * 24 * 3600,
                           source: str = "web", answer_type: str = None):
        """
        问答日志双写：Redis List（热数据，TTL 7 天）+ MySQL/SQLite 持久化
        （管理端问答记录页数据源；落库失败不影响问答主流程）

        answer_type：normal / cached / fallback_none / fallback_partial
        （见 VerificationAgent.classify_answer）—— 知识库盲区统计的唯一判据。
        """
        if not self.available:
            return
        import time as _time
        record = {
            "timestamp": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "question": question,
            "search_query": search_query or question,
            "answer": answer[:500],  # 截断长答案
            "answer_full_len": len(answer),
            "references": references[:10],
            "elapsed_seconds": round(elapsed, 3),
            "session_id": session_id or "default",
            "source": source,
            "answer_type": answer_type,
        }
        key = "rag:question_log"
        try:
            self._client.lpush(key, json.dumps(record, ensure_ascii=False))
            self._client.ltrim(key, 0, 999)  # 仅保留最近 1000 条
            self._client.expire(key, ttl)
            self._log(f"📝 问答日志已存储 key={key} | Q: '{question[:40]}...' | 耗时: {elapsed:.1f}s")
        except Exception as e:
            self._drop_client(e)

        # ---- 持久化落库（失败不影响主流程）----
        try:
            from database.db import add_question_log
            add_question_log(
                question=question,
                search_query=search_query or question,
                answer=answer,
                answer_full_len=len(answer),
                references=references[:10],
                elapsed_seconds=round(elapsed, 3),
                session_id=session_id or "default",
                source=source,
                answer_type=answer_type,
            )
        except Exception as e:
            self._log(f"⚠️ 问答日志落库失败（不影响回答）: {e}")


# 全局单例
redis_cache = RedisCache()
