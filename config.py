# ============================================================
# 游乐场RAG智能知识库问答系统 - 全局配置（多Agent架构 v2.0）
# ============================================================
import os
import certifi
from dotenv import load_dotenv

# -------------------- 加载 .env 环境变量 ---------
load_dotenv()

# -------------------- 网络/SSL 配置（必须在导入模型库之前设置） ---------
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"       # HuggingFace 国内镜像
os.environ["SSL_CERT_FILE"] = certifi.where()             # 修复 conda 环境 SSL 证书
os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()

# -------------------- HuggingFace 离线模式（消除冷启动联网重试） ---------
# 本地模型（BGE-M3 / bge-reranker-v2-m3）已缓存时必须离线加载：
# 否则 huggingface_hub 会为每个模型文件发 HEAD 请求校验 etag，网络被墙/抖动时
# 每个文件重试 5 次（1+2+4+8+8s），实测冷启动 embed_query 达 201s、reranker 达 56s，
# 服务重启后的第一个问题会长时间无响应。
# 如需重新下载模型（首次部署/换模型），启动前设置 HF_OFFLINE=0 即可恢复联网。
if os.getenv("HF_OFFLINE", "1") != "0":
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

# -------------------- Embedding 模型配置 --------------------
EMBEDDING_MODEL_NAME = "BAAI/bge-m3"
EMBEDDING_DIM = 1024                        # BGE-M3 固定1024维
EMBEDDING_DEVICE = "cpu"                    # 有GPU改 "cuda"

# -------------------- Rerank 重排序模型 --------------------
RERANK_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

# -------------------- 向量库配置（Milvus 服务端） --------------------
# 必须支持环境变量覆盖：容器/云端部署时 Milvus 不在 127.0.0.1，
# 硬编码会让 docker-compose 里的 MILVUS_HOST=standalone 完全失效（连不上向量库）。
MILVUS_HOST = os.getenv("MILVUS_HOST", "127.0.0.1")
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
COLLECTION_NAME = "amusement_park_kb"
# （已回滚到 Milvus，不再使用 FAISS 进程内索引与下列持久化文件）
# FAISS_META_PATH = os.path.join("index", "faiss_amusement_park_kb.meta.json")
# FAISS_VECTORS_PATH = os.path.join("index", "faiss_amusement_park_kb.vectors.npy")

# -------------------- LLM 大模型配置 --------------------
# ⚠️ API Key 必须通过 .env 注入（LLM_API_KEY=xxx），禁止硬编码进代码/仓库；
# 历史上硬编码泄露过的 key 应尽快在 DeepSeek 控制台轮换失效
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_ENDPOINT = os.getenv("LLM_ENDPOINT", "https://api.deepseek.com/v1")
# 文本模型（纯文字问答 / RAG 生成 / 改写 / 校验 / 工具调用 / 意图路由）
# 换型史（都是"额度耗尽 → 换型"，两次都栽在无日期别名上）：
#   ① deepseek-v4-flash（无日期别名）→ 402 / 401008；
#   ② deepseek/deepseek-flash → 2026-09-17 同样 402 / 401008（logs/app.log 754 条），
#      用户侧问答直接 500；
#   ③ 现用 deepseek-v4-pro-202606（**带日期快照**，避免别名被静默重指 / 提前耗尽）。
#
# ⚠️ 三个变体实测对比后选 -202606，不是无日期别名（详见 README「模型选型与延迟」）：
#   流式首字 TTFT 均值  deepseek-v4-pro 1.50s / -0813 1.37s / **-202606 0.93~1.42s（最快）**；
#   Router 延迟        无日期 1.75s（2.5s 超时几乎顶满） / -0813 1.95s / **-202606 1.23s**；
#   额度              三者当前均 OK，但无日期别名历史风险最高。
# 已是推理模型，但**只要 prompt 里给了【参考资料】就几乎不思考**；且本模型**支持**关思考参数
# （实测 reasoning_tokens=0，比 deepseek/deepseek-flash 强 —— 后者对该参数完全不响应）。
# 实测：RAG 生成非流式均值 2.28s、空回答 0（n=15）、function calling 正常、并行工具调用正常。
# 可被 .env 的 LLM_TEXT_MODEL 覆盖（当前 .env 已设为 deepseek-v4-pro-202606）。
LLM_TEXT_MODEL = os.getenv("LLM_TEXT_MODEL", "deepseek-v4-pro-202606")
# 视觉模型（用户上传图片时走多模态推理；旧 LLM_MODEL 兼容 .env 中的 vision 配置）
LLM_VISION_MODEL = os.getenv("LLM_VISION_MODEL",
                              os.getenv("LLM_MODEL", "deepseek/deepseek-v4-flash-vision-exp"))
# 兼容保留：早期调用方/配置展示仍可读取（不参与实际请求路由）
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-v4-flash")
LLM_TEMPERATURE = 0.1

# -------------------- 检索参数 --------------------
TOP_K = 10
RERANK_TOP = 3                            # 送入 LLM 生成上下文的资料篇数（越少生成越快）
RERANK_PRE_FILTER = 4                     # 送入 CrossEncoder 的候选篇数
                                          # （CPU 上精排是链路最重的一步：实测 6 篇长文 10.8s、4 篇 2.5s）
RERANK_DOC_MAX_CHARS = 160                # 精排时每篇只取前 N 字参与打分（原文仍完整用于生成）
                                          # 相关性主要由片段开头决定，截断后精排耗时近似线性下降
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "1") != "0"   # 关掉则直接用融合分排序（最快）
SIMILARITY_THRESHOLD = 0.45
HYBRID_ALPHA = float(os.getenv("HYBRID_ALPHA", "0.6"))   # 混合检索权重：向量检索占比（0~1），BM25 占 (1-alpha)
VECTOR_CANDIDATE_K = int(os.getenv("VECTOR_CANDIDATE_K", "30"))  # 向量检索候选集（缩小以减少Reranker压力）
BM25_CANDIDATE_K = int(os.getenv("BM25_CANDIDATE_K", "20"))      # BM25 检索候选集（提升至20以确保关键词检索覆盖面）

# -------------------- 延迟优化开关（目标：端到端 ≤10s） --------------------
# 多路检索（原始问句 + 改写问句 + 复杂问题子问题）原先"每路各跑一次 CrossEncoder"，
# 在 CPU 上并行争抢算力反而更慢；改为各路先合并去重、只跑一次精排，实测省 3~8s。
RERANK_MERGED_ONCE = os.getenv("RERANK_MERGED_ONCE", "1") != "0"
# 多路检索问句上限（原始 + 改写）
MAX_RETRIEVAL_QUERIES = int(os.getenv("MAX_RETRIEVAL_QUERIES", "2"))
# LLM 逐条相关性校验：默认关闭，改用 CrossEncoder 精排分阈值（精排分本身就是语义相关分），
# 省掉一次完整 LLM 往返（实测 3.5s）。设 VERIFY_LLM_ENABLED=1 可恢复旧行为。
VERIFY_LLM_ENABLED = os.getenv("VERIFY_LLM_ENABLED", "0") == "1"
VERIFY_MIN_SCORE = float(os.getenv("VERIFY_MIN_SCORE", "0.3"))
# 生成回答长度上限（token）：生成长度是延迟的最大变量（实测约 43 字/秒）
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "500"))

# -------------------- 推理模型"思考"开关（时延关键项） --------------------
# deepseek-v4-flash 是**推理模型**：思考（reasoning）消耗的时间与 token 都计入同一次
# 调用。实测一次 max_tokens=500 的回复里 300 个 token 有 216 个花在思考上，
# 耗时 4.2s、最终回答只有 129 字；关掉思考后同样预算下 2.0s、回答 133 字（更长）。
# 即：关掉思考＝更快 + 同样预算下回答更完整。若遇复杂推理题质量下降，置 0 可恢复。
LLM_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "1") != "0"
# 关闭思考用的参数名（网关兼容两套写法，实测 200 均生效）：
#   thinking        → {"type": "disabled"}   优先，实测更快
#   enable_thinking → false
LLM_THINKING_PARAM = os.getenv("LLM_THINKING_PARAM", "thinking")
# 答后"主动追问建议"：LLM 生成带软超时，超时立刻用规则化建议兜底，不再阻塞收尾。
# 注意：追问建议是在 done 事件**之后**推送的（用户已看到回答），因此这里可以放宽等待，
# 换取"更贴合刚才那条回答"的建议质量（超时过短会几乎总是走规则兜底 → 建议与回答无关）。
SUGGEST_LLM_ENABLED = os.getenv("SUGGEST_LLM_ENABLED", "1") != "0"
SUGGEST_LLM_TIMEOUT = float(os.getenv("SUGGEST_LLM_TIMEOUT", "6"))

# -------------------- 意图路由（RouterAgent） --------------------
# 三级路由：① 关键词快速通道（0 LLM）→ ② 仅"带行动标记且关键词全未命中"时
# 才做一次小模型分类（硬超时 ROUTER_TIMEOUT，结果进 Redis 缓存 1h）→ ③ 默认走 RAG。
# 设计目的：把漏召的"办事型"问句（如"这张卡还能用吗""付了钱没到账"）捞回来，
# 同时**保证纯知识问答一次 LLM 都不多**（"检索速度不能变慢"是硬约束）。
# 置 ROUTER_LLM_ENABLED=0 可一键退回纯关键词路由。
ROUTER_LLM_ENABLED = os.getenv("ROUTER_LLM_ENABLED", "1") != "0"
# ⚠️ 换文本模型必须同步复核这个值。openai SDK 的 timeout 是**单次读超时**：
#    模型首字慢于该值时请求被直接掐断 → `_llm_route` 返回 source=llm_failed，
#    L2 路由**静默失效**（退化成纯关键词路由，功能少一截但不报错，很难察觉）。
#    实测：deepseek/deepseek-flash 0.85s → 1.5s 够用；换 deepseek-v4-pro-202606 后
#    变成 1.23s 均值 / 1.43s 最慢，1.5s 会把 12/12 全部掐断；放宽到 2.5s 后 12/12 成功。
#    代价可控：只有"带行动标记且关键词全未命中"的问句才走这条路，且结果进 Redis 缓存 1h，
#    纯知识问答热路径一次 LLM 都不多花。
ROUTER_TIMEOUT = float(os.getenv("ROUTER_TIMEOUT", "2.5"))

# -------------------- 天气出行快速通道 --------------------
# 纯天气问句（"今天天气怎么样""要不要带伞""今天适合去玩吗"）走规则模板直接回答，
# 不经 LLM、不经 LangChain 地图 Agent，实测 ≤1s；复合问句仍由业务 Agent 用
# get_weather 工具处理。置 0 可退回"全部交给地图 Agent"的旧行为。
WEATHER_FASTPATH_ENABLED = os.getenv("WEATHER_FASTPATH_ENABLED", "1") != "0"

# 知识库查不到（或资料不足以回答）时的兜底策略开关（v2，2026-09-20）：
#   1（默认）→ 不再输出"AI 通用建议清单"。
#     · 完全没检索到资料：直接给一句确定性结论「知识库没有收录，我不编」，
#       并告诉用户哪些事可以直接替他办（查余币/签到/看项目/买项目/办卡充值）。不调 LLM。
#     · 检索到部分相关资料：只基于资料给结论，不补通用建议、不把问题推回给用户。
#   0 → 恢复旧行为：直接回"暂时没有查询到相关信息，请换个问法试试。"
# 注意：无论开关如何，兜底话术都是"回答类型判定"（知识库盲区统计）的锚点，
# 改话术必须同步改 verification_agent.FALLBACK_MARKERS。
FALLBACK_ADVICE_ENABLED = os.getenv("FALLBACK_ADVICE_ENABLED", "1") != "0"

# -------------------- 文本分块参数 --------------------
CHUNK_SIZE = 512                            # token 级别（cl100k_base 编码，中文约 250-400 字/chunk）
CHUNK_OVERLAP = 100                         # 字符级重叠（平台配置 API 使用）
TOKEN_OVERLAP = 90                          # 80-100 token 级重叠（标题层级+递归切块方案）

# -------------------- 知识库智能体（商家 PC「知识库智能体」） --------------------
# 用途：统计"知识库没答上来的用户问题" → 基于知识库生成标准答案 → 导出可编辑 Word
#       → 管理员修改后回传 → 自动解析/切片/入 Milvus+BM25 供检索
KB_AGENT_OUT_DIR = os.path.join("static", "kb_agent")   # 生成的 Word 存放目录（/static 可直接下载）
KB_AGENT_DAYS = int(os.getenv("KB_AGENT_DAYS", "30"))            # 默认统计近 N 天（0=不限）
KB_AGENT_LIMIT = int(os.getenv("KB_AGENT_LIMIT", "20"))          # 默认最多处理多少个不重复问题
KB_AGENT_ANSWER_TOKENS = int(os.getenv("KB_AGENT_ANSWER_TOKENS", "300"))  # 每条标准答案的 token 上限
KB_AGENT_CTX_TOP = int(os.getenv("KB_AGENT_CTX_TOP", "4"))       # 每题送入 LLM 的知识库资料篇数
KB_AGENT_WORKERS = int(os.getenv("KB_AGENT_WORKERS", "4"))       # 生成标准答案的并发线程数
KB_AGENT_MAX_RAW = int(os.getenv("KB_AGENT_MAX_RAW", "2000"))    # 统计时最多拉取多少条原始问答记录

# -------------------- 商家端「经营小助手」弹窗（右下角执行式助手） --------------------
# 与 KB 智能体不同：它不产出回答，只把管理员那句话变成真实操作（工具调用），
# 做完给一份确定性动作流水。执行模型单独指定，不影响用户侧问答链路。
#   1) glm-5.3-flash 实测：tool_calls 稳定返回、支持一轮并行多工具、单轮 4.6~6.5s；
#   2) 该项目全局注入 thinking:{type:disabled} 对它近乎无效，需显式再传
#      extra_body={"enable_thinking": False}（两个参数并存时 glm 只认后者，实测省 ~0.5s）；
#   3) 轮数是总时长的最大变量（每轮 5~6s），配合"同轮并行"实测多步任务 2~3 轮完成。
ADMIN_ASSISTANT_ENABLED = os.getenv("ADMIN_ASSISTANT_ENABLED", "1") != "0"
ADMIN_ASSISTANT_MODEL = os.getenv("ADMIN_ASSISTANT_MODEL", "glm-5.3-flash")
ADMIN_ASSISTANT_ROUNDS = int(os.getenv("ADMIN_ASSISTANT_ROUNDS", "6"))   # 单次指令最多几轮工具循环
ADMIN_ASSISTANT_TOOL_RESULT_CHARS = int(os.getenv("ADMIN_ASSISTANT_TOOL_RESULT_CHARS", "500"))
# 汇总默认由确定性流水生成（模型只见过截断后的工具返回，容易编数字）；
# 置 1 才额外花一次 LLM 把流水润色成人话。
ADMIN_ASSISTANT_POLISH = os.getenv("ADMIN_ASSISTANT_POLISH", "0") == "1"

# -------------------- 知识库「问题级去重」参数（services/kb_dedup.py） --------------------
# 判据分三级，宁严勿漏（只判"确凿重复"，绝不误杀该补的盲区问题）：
#   ① 精确层：问题归一化后完全相同 → 必判重复（零成本、零误判）
#   ② 向量层：与"库内已有问法清单"做 BGE-M3 语义匹配，召回 top-N 候选
#   ③ LLM 层：把"待判问题 + 它的候选问法"批量交给 LLM 裁决是否同一问题
#      —— 实测向量相似度**分不开**（噪声本底 0.75~0.80：
#        「办卡可以开发票吗」vs「办卡」0.80，「儿童能一起玩蹦床吗」vs「蹦床玩多久」0.76，
#         而真重复「蹦床一次多少币」vs「蹦床多少钱」只有 0.88），所以必须有 LLM 裁决。
KB_COVER_LLM = int(os.getenv("KB_COVER_LLM", "1"))          # 0=跳过 LLM 层（只做精确+向量）
KB_COVER_VEC_HIGH = float(os.getenv("KB_COVER_VEC_HIGH", "0.90"))  # 向量层高置信阈值
KB_DEDUP_CANDIDATES = int(os.getenv("KB_DEDUP_CANDIDATES", "4"))   # 送 LLM 裁决的候选数
# 生成侧闸门：统计盲区时，剔除"现有知识库已经答得上"的问题（0=关闭）
# 关掉它就会出现"同一问题每跑一次智能体就重生成一版答案"的重复堆积。
KB_GAP_COVER_CHECK = int(os.getenv("KB_GAP_COVER_CHECK", "1"))
# 生成侧闸门最多扫几批候选（每批 limit 个）。扫过的批次里被剔除的问题，
# 会用后面的候选补齐，保证"要找 limit 个真盲区"的目标尽量达成。
KB_GAP_COVER_BATCHES = int(os.getenv("KB_GAP_COVER_BATCHES", "2"))

# -------------------- 问法级检索提分（FAQ boost，agents/retrieval_agent.py） --------------------
# 问题：块本身是长段落，CrossEncoder(短问句, 长块) 的打分**偏低且极不稳定** ——
#   实测同一个正确块，换个问法分就从 0.18 跳到 0.87；而「门票多少钱」对上
#   含该问法的块只有 0.08（字面完全一致！），直接掉出候选。
# 解法：duck 文档每个块都手工写了「**常见问法**：a、b、c…」，这是最权威的
#   "这个块能回答哪些问法"标注。把块承载的**多种问法**分别与用户问句比，
#   融合成一个"问法命中分"，与融合分取较大者 —— 正确块能被稳定抬回来
#   （实测：蹦床 0.18→0.66、退卡 0.27→0.52、门票 0.08→1.00）。
KB_FAQ_BOOST = int(os.getenv("KB_FAQ_BOOST", "1"))
# 提分开关的布尔别名（retrieval_agent 直接用）
FAQ_BOOST = KB_FAQ_BOOST > 0
# 问法相似度的噪声本底：向量对"话题相近但不同问题"也能给到 0.7~0.8
#   （实测 不相关问题 ≤0.59、「什么不能带进园区」0.81、「办卡」0.80），
#   先扣掉本底再归一化，避免把"只是关键词重合"的块抬过阈值。
KB_FAQ_BASE = float(os.getenv("KB_FAQ_BASE", "0.65"))
# 问法完全命中时给到的分数（与精排分同尺度 0~1；检索阈值 SIMILARITY_THRESHOLD=0.45）
KB_FAQ_CEIL = float(os.getenv("KB_FAQ_CEIL", "1.0"))
# 块内多条问法的融合权重：0.85*最高 + 0.15*次高（块只有一条问法时次高 = 最高，
#   否则单问法块会天然吃亏 —— kb_agent 生成的块每条只有一个问题）
KB_FAQ_W_SECOND = float(os.getenv("KB_FAQ_W_SECOND", "0.15"))

# -------------------- 高德地图 Web 服务配置 --------------------
# ⚠️ Web-Service Key 必须通过环境变量(.env)注入，禁止硬编码、禁止下发前端
AMAP_WEB_SERVICE_KEY = os.getenv("AMAP_WEB_SERVICE_KEY", "")
AMAP_BASE_URL = os.getenv("AMAP_BASE_URL", "https://restapi.amap.com/v3")
AMAP_DAILY_QUOTA = int(os.getenv("AMAP_DAILY_QUOTA", "5000"))  # 每日配额阈值（个人开发者约5000次/日/服务）
AMAP_TIMEOUT = float(os.getenv("AMAP_TIMEOUT", "5"))            # 高德接口超时（秒）

# -------------------- Redis 缓存配置 --------------------
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")   # 从 .env 读取；凭据一律不入库
REDIS_DB = 0
CACHE_TTL = 3600                            # 缓存过期时间（秒）

# -------------------- MySQL 数据库配置 --------------------
MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "amusement_rag")
MYSQL_CHARSET = "utf8mb4"
# 数据库驱动切换: "mysql" 使用 db_mysql.py, "sqlite" 使用 db.py
DB_DRIVER = os.getenv("DB_DRIVER", "mysql")

# -------------------- 服务配置 --------------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

# 员工手机端（H5）的对外地址（游园券二维码里编的就是它 + ?c=券码）。
# 留空时按本机出口网卡的局域网 IP 自动拼 http://<lan-ip>:<SERVER_PORT>/staff/verify，
# 员工手机连同一个 WiFi 就能扫码打开；上线到内网/公网时在这里写明域名。
# /staff/verify 与 /staff/app 返回同一份单页应用：扫码只是它的一个入口，
# 进去之后还能用仪表盘 / 失物招领 / 内容管理 / 办卡管理（首页默认扫码 + 办卡）。
STAFF_VERIFY_BASE_URL = os.getenv("STAFF_VERIFY_BASE_URL", "")
DOCS_DIR = "./docs"
# 失物招领照片存储目录（挂载于 /static 静态路由下访问）
LOST_FOUND_DIR = "./static/lost_found"

# -------------------- CORS 白名单配置 --------------------
# 逗号分隔的允许来源，默认 "*"（配合 allow_credentials=False 使用；
# 前端认证走 Authorization 头，不依赖 Cookie，局域网手机访问不受影响）
CORS_ALLOW_ORIGINS = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]

# -------------------- 微信小程序配置 --------------------
# AppID/Secret 必须通过 .env 注入，禁止硬编码
WECHAT_APPID = os.getenv("WECHAT_APPID", "")
WECHAT_SECRET = os.getenv("WECHAT_SECRET", "")

# -------------------- 微信支付配置（开关式） --------------------
# false: 未开通商户号（个人主体），下单接口返回 4001 提示；
# true:  正式模式，需配置商户号三件套；
# mock:  联调模式，下单直接置已支付并发卡（无商户号时验证全链路）
WECHAT_PAY_ENABLED = os.getenv("WECHAT_PAY_ENABLED", "false").lower()
WECHAT_MCHID = os.getenv("WECHAT_MCHID", "")
WECHAT_APIV3_KEY = os.getenv("WECHAT_APIV3_KEY", "")
WECHAT_CERT_SERIAL = os.getenv("WECHAT_CERT_SERIAL", "")
WECHAT_PRIVATE_KEY_PATH = os.getenv("WECHAT_PRIVATE_KEY_PATH", "")
WECHAT_PAY_NOTIFY_URL = os.getenv("WECHAT_PAY_NOTIFY_URL", "")