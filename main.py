# ============================================================
# 游乐场RAG智能知识库问答系统 - 主入口 (多Agent架构 v2.0)
# 架构：FastAPI网关 + 五大Agent协同（调度-文档-Query-检索-校验）
# ============================================================
import torch  # ⚠️ 必须在paddleocr之前导入，否则paddle的DLL会干扰torch加载
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import os
import sys
import time
import logging

# ---------- 日志配置 ----------
os.makedirs('logs', exist_ok=True)  # 目录不存在时自动创建（服务器首次部署 logs/ 不随包携带）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/app.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

from config import SERVER_HOST, SERVER_PORT, CORS_ALLOW_ORIGINS


def _interpreter_info() -> dict:
    """运行解释器信息（用于自检"是否误用系统 Python 启动"）。

    历史坑：系统 Python 下 pymysql 建连 0.5~0.6s/次（conda rag 仅 0.01s），
    一次问答链路有多次 DB 操作 → Redis 命中也要 1s+、常规问答 10s+。
    database/db_mysql.py 已改为连接复用兜底，但 conda 环境在依赖版本上仍更一致。
    """
    import sys as _sys
    exe = _sys.executable or ""
    env_name = os.environ.get("CONDA_DEFAULT_ENV", "") or os.environ.get("CONDA_PREFIX", "")
    conda_ok = ("conda" in exe.lower()) or bool(env_name)
    return {"interpreter": exe, "conda_env": env_name, "conda_ok": conda_ok}


def _db_runtime_info() -> dict:
    """DB 运行态：连接复用开关与计数（reuse 远大于 new 说明复用生效）"""
    try:
        from config import DB_DRIVER
        if DB_DRIVER == "mysql":
            from database.db_mysql import _DB_CONN_REUSE, conn_reuse_stats
            return {"driver": "mysql", "conn_reuse": _DB_CONN_REUSE, **conn_reuse_stats()}
    except Exception:
        pass
    return {}


# ---------- FastAPI 应用 ----------
app = FastAPI(
    title="鸭先知 · 大黄鸭游乐园智能问答与运营系统",
    version="2.0.0",
    description="多Agent协同架构 | 混合检索 + Rerank | 防幻觉校验 + 溯源 | Redis缓存 + Docker容器化"
)


# ---------- 请求耗时中间件 ----------
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(round(process_time, 3))
    return response


# ---------- CORS 跨域支持 ----------
# 前端认证走 Authorization 头（Bearer Token），不依赖 Cookie；
# 关闭 allow_credentials 后通配符来源才是安全组合；
# 生产环境建议在 .env 配置 CORS_ALLOW_ORIGINS 白名单（逗号分隔）收紧来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 统一频控中间件 ----------
# 全站入口限流：同一身份的请求按窗口计数，超限直接 429。
# 分档依据是"单次请求的成本"而不是业务重要性：
#   vision  5/分钟 —— 会同步调用多模态大模型，既烧钱、又会长时间占住 event loop
#   auth   20/分钟 —— 登录/注册/验证码，是撞库与批量注册的入口
#   write  30/分钟 —— 其它写接口（报失、反馈、收藏、下单…）
#   read  180/分钟 —— 纯查询
# 身份口径：带有效 token 的按 user_id 计，其余按 IP 计。
# ⚠️ 只信任 request.client.host。将来若在前面挂 Nginx，必须先确认 Nginx 会覆写
#    X-Forwarded-For 再决定是否取用 —— 否则客户端能自己伪造该头来绕过限流。
_RATE_LIMITS = {
    "vision": (5, 60),
    "auth": (20, 60),
    "write": (30, 60),
    "read": (180, 60),
}

# 会同步调用多模态大模型的接口（成本最高，单独一档）
_VISION_PATHS = ("/lost-found/analyze-photo", "/chat/vision")

# 免限流路径：健康检查被限会导致看护脚本误判服务已死；静态资源是图片文件
_RATE_EXEMPT_PREFIXES = ("/health", "/static/", "/favicon.ico")


def _rate_bucket(method: str, path: str) -> str:
    """按"单次请求成本"给请求分档"""
    if path in _VISION_PATHS:
        return "vision"
    if path.startswith("/auth/"):
        return "auth" if method != "GET" else "read"
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return "write"
    return "read"


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method
    if method == "OPTIONS" or path.startswith(_RATE_EXEMPT_PREFIXES):
        return await call_next(request)

    bucket = _rate_bucket(method, path)
    max_req, window = _RATE_LIMITS[bucket]

    from core.auth import decode_token
    from core.cache import redis_cache

    identity = ""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.lower().startswith("bearer "):
        payload = decode_token(auth_header[7:].strip())
        if payload.get("user_id"):
            identity = f"user:{payload['user_id']}"
    if not identity:
        identity = f"ip:{request.client.host if request.client else 'unknown'}"

    if not redis_cache.rate_limit_hit(f"{bucket}:{identity}", max_req, window):
        logger.warning(f"⚠️ 触发频控: {bucket} {method} {path} by {identity}")
        return JSONResponse(
            status_code=429,
            content={"code": 429, "msg": "请求过于频繁，请稍后再试"},
        )
    return await call_next(request)

# ---------- 注册 API 路由 ----------
from api.chat import router as chat_router
from api.knowledge import router as knowledge_router
from api.admin import router as admin_router
from api.agent_config import router as agent_config_router
from api.auth import router as auth_router
from api.amap import router as amap_router
from api.pay import router as pay_router
from api.lost_found import router as lost_found_router
from api.lost_found import admin_router as lost_found_admin_router
from api.notices import router as notices_router
from api.notices import admin_router as notices_admin_router
from api.kb_agent import router as kb_agent_router
from api.admin_assistant import router as admin_assistant_router
from api.staff_page import router as staff_page_router
from api.staff_api import router as staff_api_router
from api.profile import router as profile_router

app.include_router(profile_router)
app.include_router(chat_router)
app.include_router(knowledge_router)
app.include_router(admin_router)
app.include_router(agent_config_router)
app.include_router(auth_router)
app.include_router(amap_router)
app.include_router(pay_router)
app.include_router(lost_found_router)
app.include_router(lost_found_admin_router)
app.include_router(notices_router)
app.include_router(notices_admin_router)
app.include_router(kb_agent_router)
app.include_router(admin_assistant_router)
app.include_router(staff_page_router)
app.include_router(staff_api_router)


# ---------- 静态文件（失物招领照片等上传资源） ----------
# 照片保存在 static/lost_found/，通过 /static/lost_found/xxx.jpg 直接访问
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------- 全局异常处理 ----------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    # 不回传异常细节给客户端，避免泄露内部实现信息；详情见服务端日志
    return JSONResponse(
        status_code=500,
        content={"code": 500, "msg": "服务内部错误，请稍后重试"}
    )


# ---------- 根路径：提供聊天界面 ----------
@app.get("/")
async def root():
    return FileResponse("index.html")


# ---------- 健康检查 ----------
@app.get("/health", summary="健康检查")
async def health_check():
    from core.cache import redis_cache
    redis_ok = redis_cache.available

    # 向量索引 (Milvus IVF_FLAT)
    milvus_ok = False
    milvus_docs = 0
    try:
        from core.vector_store import vector_store
        vs = vector_store.get_collection_stats()
        milvus_docs = vs.get("num_entities", 0)
        milvus_ok = milvus_docs >= 0
    except Exception:
        pass

    # BM25 倒排索引
    bm25_ok = False
    bm25_docs = 0
    try:
        from core.bm25_index import bm25_index
        bm25_docs = bm25_index.doc_count
        bm25_ok = bm25_docs > 0
    except Exception:
        pass

    return {
        "code": 200,
        "status": "ok" if (milvus_ok or bm25_ok) else "degraded",
        "service": "duck_sage_v2",
        "version": "2.0.0",
        "architecture": "Multi-Agent: Orchestrator + Document + Query + Retrieval + Verification",
        "indexes": {
            "vector": {
                "engine": "Milvus IVF_FLAT (IP)",
                "status": "connected" if milvus_ok else "disconnected",
                "documents": milvus_docs,
                "role": "语义检索 (BGE-M3)"
            },
            "bm25": {
                "engine": "BM25Okapi + jieba",
                "status": "ready" if bm25_ok else "empty",
                "documents": bm25_docs,
                "role": "关键词检索"
            },
            "fusion": {
                "strategy": "分数级 Min-Max 归一化 + alpha=0.6 加权融合",
                "reranker": "BGE-Reranker-v2-m3"
            }
        },
        "components": {
            "redis": "connected" if redis_ok else "disconnected",
        },
        # 运行态自检：解释器是否 conda（否则 DB 建连慢 → 问答变慢）、DB 连接复用是否生效
        "runtime": {
            **_interpreter_info(),
            "db": _db_runtime_info(),
        }
    }


# ---------- 启动事件 ----------
@app.on_event("startup")
async def startup_event():
    """应用启动时的初始化"""
    logger.info("=" * 60)
    logger.info("🚀 游乐场RAG智能知识库问答系统 v2.0 启动中...")
    logger.info("架构: 多Agent协同 (Orchestrator + Document + Query + Retrieval + Verification)")
    logger.info("=" * 60)

    # 初始化数据库（db.py 为代理模块，根据 DB_DRIVER 自动切换 SQLite / MySQL）
    from config import DB_DRIVER
    from database.db import init_db
    logger.info(f"🗄️  使用 {DB_DRIVER.upper()} 数据库驱动")
    init_db()
    logger.info("✅ 用户数据库已初始化（默认管理员: admin/admin123）")

    # 初始化异步任务处理器
    from core.async_tasks import async_processor
    async_processor.start()
    logger.info("✅ 异步任务处理器已启动")

    # 检查 Redis 连接
    from core.cache import redis_cache
    if redis_cache.available:
        logger.info("✅ Redis 已连接")
    else:
        logger.warning("⚠️ Redis 未连接 - 缓存功能禁用")

    # ---------- 运行环境自检（提前暴露"装错解释器/缺依赖"这类问题） ----------
    # 历史坑：后端被系统 Python 启动时缺 qrcode，会员码接口直接 500，前端一直"加载中"；
    #          且该环境 PyMySQL 建连接要 0.5s/次，会让每次问答白白多等 2~4s。
    try:
        _missing = []
        for _mod, _tip in (("qrcode", "会员码二维码 /pay/redeem/qrcode"),
                           ("PIL", "二维码 PNG 渲染"),
                           ("pymysql", "MySQL 驱动")):
            try:
                __import__(_mod)
            except Exception:
                _missing.append(f"{_mod}（{_tip}）")
        if _missing:
            logger.error("❌ 缺少依赖: %s | 当前解释器: %s | 请用项目 conda 环境启动"
                         "（启动服务.bat → conda activate rag）",
                         "、".join(_missing), sys.executable)
        else:
            logger.info(f"✅ 依赖自检通过（解释器: {sys.executable}）")
    except Exception as _e:
        logger.warning(f"依赖自检失败: {_e}")

    # ---------- 解释器环境自检（问答变慢的头号原因） ----------
    try:
        _rt = _interpreter_info()
        if _rt["conda_ok"]:
            logger.info(f"✅ 解释器环境检查通过（conda: {_rt['conda_env'] or '已激活'}）")
        else:
            logger.error(
                "❌ 后端未运行在 conda 环境中（当前解释器: %s）！\n"
                "   系统 Python 的 PyMySQL 建连约 0.5~0.6s/次（conda rag 仅 0.01s），"
                "会让 Redis 命中也要 1s+、常规问答 10s+。\n"
                "   DB 层已启用连接复用兜底，但仍请改用「启动服务.bat」（conda activate rag）启动。",
                _rt["interpreter"],
            )
    except Exception as _e:
        logger.warning(f"解释器环境自检失败: {_e}")

    # 检查 Milvus 连接
    num_docs = 0
    try:
        from core.vector_store import vector_store
        stats = vector_store.get_collection_stats()
        num_docs = stats.get('num_entities', 0)
        logger.info(f"✅ 向量库(Milvus) 已就绪 - 集合: {stats.get('collection_name')}, "
                     f"文档数: {num_docs}")
    except Exception as e:
        logger.warning(f"⚠️ 向量库(Milvus) 未就绪 - {e}")

    # 恢复 BM25 倒排索引（优先加载本地持久化，无持久化时才从 Milvus 重建；
    # 不再依赖 Milvus 存活，Milvus 挂掉时也能从本地恢复关键词检索能力）
    try:
        from core.bm25_index import bm25_index
        bm25_count = bm25_index.rebuild_from_milvus()
        if bm25_count > 0:
            logger.info(f"✅ BM25 倒排索引已恢复 - {bm25_count} 篇")
    except Exception as e:
        logger.warning(f"BM25 重建失败: {e}")

    # 输出 chunks 切片文件状态
    from agents.document_agent import document_agent
    chunk_files = document_agent.list_all_chunk_files()
    docs_in_dir = document_agent.list_documents()
    print(f"📊 [chunks] 启动时状态: docs目录{len(docs_in_dir)}个文件, chunks目录{len(chunk_files)}个切片JSON")

    # ---------- 模型预热（消除首次提问 57s 冷启动）----------
    logger.info("🔥 后台预热模型中...")
    import threading
    def _warmup():
        try:
            from core.embedding import embedding_engine
            _ = embedding_engine.embed_query("预热")
            logger.info("✅ BGE-M3 Embedding 已预热")
            from core.reranker import reranker_engine
            _ = reranker_engine.model
            logger.info("✅ BGE-Reranker 已预热")
            from agents.query_agent import query_agent
            query_agent._ensure_loaded()
            _ = query_agent.match_question("预热", top_k=1)
            logger.info("✅ 常见问法库已预热")
            if num_docs > 0:
                from agents.retrieval_agent import retrieval_agent
                _ = retrieval_agent.search("预热", use_cache=False)
                logger.info("✅ 检索管道已预热")
            # 知识库「问法清单」索引（问法级提分 / 生成侧与入库侧去重闸门共用）：
            # 重建要读 Milvus + 向量化上百条问法（实测 8~20s），必须在这里预热，
            # 否则第一次真实提问会替它等这段时间。
            try:
                from services.kb_dedup import warm_up as _kb_warmup, index_ready
                _kb_warmup()
                if index_ready():
                    logger.info("✅ 知识库问法清单索引已预热")
            except Exception as _e:
                logger.warning(f"问法清单索引预热跳过: {_e}")

            # ---------- 路由与依赖预热 ----------
            # 首个真实提问若在此处做"一次性初始化"（jieba posseg 词典、Redis/MySQL 首连、
            # 业务/地图 Agent 的惰性 import、埋点用到的模块）会白白多等数秒，
            # 这里统一提前跑一遍，让用户的第一问就是热路径。
            try:
                from core.cache import redis_cache
                _ = redis_cache.get_cached_answer("预热")
                _ = redis_cache.rate_limit_check("127.0.0.1")
                logger.info("✅ Redis + jieba 词性词典已预热")
            except Exception as _e:
                logger.warning(f"Redis 预热跳过: {_e}")
            try:
                from agents.map_agent import map_agent
                from agents.business_agent import business_agent
                from tools.business_tools import get_business_tools
                _ = map_agent.is_map_question("预热")
                _ = business_agent.is_business_question("预热")
                _ = get_business_tools(None)
                logger.info("✅ 地图/业务 Agent 路由已预热")
            except Exception as _e:
                logger.warning(f"业务路由预热跳过: {_e}")
            try:
                from core import chat_memory as _cm
                _ = _cm.load_context("warmup")
                from services.hot_questions import get_hot_questions
                _ = get_hot_questions(auto_refresh=False)
                logger.info("✅ 会话记忆 + 热点问题已预热")
            except Exception as _e:
                logger.warning(f"记忆/热点预热跳过: {_e}")
            logger.info("🎉 预热完成！首次提问已是热路径（含路由/DB/词典）")
        except Exception as e:
            logger.warning(f"⚠️ 预热部分失败: {e}")
    threading.Thread(target=_warmup, daemon=True).start()


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时的清理"""
    from core.async_tasks import async_processor
    async_processor.stop()
    logger.info("👋 系统关闭")


def _disable_console_quick_edit() -> bool:
    """关闭 Windows 控制台的「快速编辑」模式（服务窗口里拖选文字不再卡死服务）。

    为什么必须关：开启快速编辑时，只要有人在服务窗口里拖选/双击选中文字，
    该控制台的 WriteFile 就会一直等选区被取消。而 uvicorn 的顺序是
    create_server()（端口此刻已 bind）→ 往控制台打 "Uvicorn running on ..."
    → 才进入 accept 循环。那一行一旦被卡住，进程就永久停在
    「已 LISTENING、却一个请求都不接」：端口通、所有端点超时、内存与 CPU
    时间都不再变化、app.log 也停在启动那一刻——极像"服务没起来"或"接口慢"。
    （2026-09-20 实测定位，排查步骤见 .workbuddy/memory/MEMORY-ops.md O12）

    实现有三个必须踩对的点（2026-09-20 逐个实测得出，别凭直觉改）：
    1. **必须在 STD_INPUT_HANDLE(-10) 上读写**：`ENABLE_QUICK_EDIT_MODE(0x40)` 与
       `ENABLE_EXTENDED_FLAGS(0x80)` 都是**输入句柄**的模式。用输出句柄(STD_OUTPUT_HANDLE=-11)
       去设会因为带上 0x80 被判参数无效 —— SetConsoleMode 返回 0、GetLastError=87，
       读回来的那个句柄模式也永远不含 0x40（实测：-10 是 0x1f7/含快速编辑，-11 是 0x3/不含）。
    2. 改这两位时必须**同时置 ENABLE_EXTENDED_FLAGS**，否则整个调用被判无效。
    3. 必须显式声明 argtypes/restype：HANDLE 是 64 位，让 ctypes 按默认 c_int 收会截断。
    """
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.GetStdHandle.argtypes = [wintypes.DWORD]
        k32.GetStdHandle.restype = wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetConsoleMode.restype = wintypes.BOOL
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.SetConsoleMode.restype = wintypes.BOOL

        STD_INPUT_HANDLE = 0xFFFFFFF6               # -10
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080               # 必须先置位，改快速编辑才生效

        handle = k32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle:
            return False
        mode = wintypes.DWORD()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False                            # 输入被重定向/无控制台 → 无需处理
        if not (mode.value & ENABLE_QUICK_EDIT_MODE):
            return True                             # 本来就是关的，目标已达成
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        return bool(k32.SetConsoleMode(handle, new_mode))
    except Exception:
        return False


# ============================================================
# 启动服务
# ============================================================
if __name__ == "__main__":
    # 确保必要的目录存在
    os.makedirs("logs", exist_ok=True)
    os.makedirs("docs", exist_ok=True)

    logger.info(f"🌐 服务地址: http://localhost:{SERVER_PORT}")
    logger.info(f"📊 API文档: http://localhost:{SERVER_PORT}/docs")
    logger.info(f"💬 聊天界面: http://localhost:{SERVER_PORT}/")

    # ---------- 启动自检：局域网地址 + 微信 AppID 一致性 ----------
    # 目的：把「该用哪个地址」「appid 有没有配错」在启动时直接打出来，
    # 不用等手机连不上、或小程序报 40029 invalid code 再去猜。
    try:
        import socket as _socket
        _ip = "127.0.0.1"
        try:
            _s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))   # UDP 不实际发包，只让内核挑出出口网卡
            _ip = _s.getsockname()[0]
            _s.close()
        except Exception:
            pass
        logger.info(f"📱 局域网地址(小程序真机用这个): http://{_ip}:{SERVER_PORT}")
    except Exception:
        pass

    try:
        import json as _json
        from config import WECHAT_APPID as _cfg_appid
        from config import WECHAT_SECRET as _cfg_secret
        _pcfg = os.path.join("miniprogram", "project.config.json")
        _mp_appid = ""
        if os.path.isfile(_pcfg):
            with open(_pcfg, "r", encoding="utf-8") as _f:
                _mp_appid = (_json.load(_f) or {}).get("appid", "")
        if _cfg_appid and _mp_appid and _cfg_appid != _mp_appid:
            logger.warning("!" * 62)
            logger.warning("⚠️  微信 AppID 不一致 —— 微信一键登录会报 40029 invalid code")
            logger.warning(f"    后端 .env WECHAT_APPID      = {_cfg_appid}")
            logger.warning(f"    小程序 project.config.json  = {_mp_appid}")
            logger.warning("    修法：改 .env 的 WECHAT_APPID 为小程序那一个，")
            logger.warning("          并同时把它自己的 AppSecret 填进 WECHAT_SECRET（两个 AppID 不共用 Secret）。")
            logger.warning("!" * 62)
        elif not _cfg_appid:
            logger.warning("⚠️  .env 未配置 WECHAT_APPID，微信一键登录不可用")
        elif not _cfg_secret:
            logger.warning("⚠️  .env 的 WECHAT_SECRET 为空 —— 微信一键登录会失败。")
            logger.warning(f"    请到 微信公众平台 → 开发 → 开发设置 复制 AppSecret"
                           f"（注意要 {_cfg_appid} 这个 AppID 自己的，两个 AppID 不共用 Secret）")
        else:
            logger.info(f"✅ 微信 AppID 与小程序一致: {_cfg_appid}")
    except Exception as _e:
        logger.warning(f"微信 AppID 自检跳过: {_e}")

    # 关掉控制台快速编辑：否则在服务窗口里拖选/双击会全文阻塞输出，
    # 让进程卡在"已 bind、未 accept"的假死状态（见 _disable_console_quick_edit）
    if _disable_console_quick_edit():
        logger.info("🖱️  已关闭控制台快速编辑（在窗口里点选文字不会再卡死服务）")

    uvicorn.run(
        "main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        log_level="info"
    )
