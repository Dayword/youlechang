# ============================================================
# 游乐场RAG智能知识库问答系统 - MySQL 数据库模块
# 驱动: pymysql
# 表清单（同 SQLite 版本）:
#   1. users            用户表
#   2. chat_sessions    会话表
#   3. chat_messages    问答消息表
#   4. documents        文档元数据表
#   5. document_chunks  文档切片表
#   6. feedback         用户反馈表
#   7. agent_config     Agent运行配置持久化表
#   8. operation_logs   操作日志表
#   9. cards            卡产品表（小程序办卡）
#  10. card_orders      办卡订单表
#  11. user_cards       用户持卡表
#  12. question_logs    问答日志表
# ============================================================
import pymysql
import pymysql.cursors
import json
import logging
import os
import threading
from typing import Optional, List, Dict, Any

# 从 config 读取 MySQL 配置
try:
    from config import (
        MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD,
        MYSQL_DATABASE, MYSQL_CHARSET
    )
except ImportError:
    # 兜底默认值
    MYSQL_HOST = "127.0.0.1"
    MYSQL_PORT = 3306
    MYSQL_USER = "root"
    MYSQL_PASSWORD = ""
    MYSQL_DATABASE = "amusement_rag"
    MYSQL_CHARSET = "utf8mb4"

_lock = threading.Lock()

# ============================================================
# 连接工具（连接复用）
# ------------------------------------------------------------
# 【历史坑 · 问答变慢的元凶】原先每次 DB 操作都新建连接，而不同 Python 解释器下
# pymysql 建连成本差异巨大（同一台机器实测）：
#     conda rag 环境 0.012s/次   系统 Python 0.612s/次（慢 49 倍）
# 一次问答链路里可能有 3~10 次 DB 操作（会话记忆/卡产品匹配/日志落库/账户查询），
# 于是"后端被系统 Python 启动"这一件事就让普通问答多等 2~5s、业务类问答多等 10s+
# （最典型的：Redis 缓存命中后仍要查一次 cards 表做办卡推荐 → 命中也要 1.1s）。
#
# 处理：连接按线程复用（FastAPI 同步接口跑在线程池里，线程会被复用）。
#   · 复用前 ping(reconnect=True) 探活，避免 MySQL 主动断开的僵尸连接；
#   · close() 不再真正断开，只回滚可能残留的未提交事务后归还本线程复用；
#   · 设 DB_CONN_REUSE=0 可退回"每次新建"的旧行为。
# ============================================================
_DB_CONN_REUSE = os.getenv("DB_CONN_REUSE", "1") != "0"
_local = threading.local()
_conn_stats = {"new": 0, "reuse": 0, "reconnect": 0}


def _new_raw_conn() -> pymysql.Connection:
    """真正建立一条 MySQL 连接（DictCursor，手动提交）"""
    _conn_stats["new"] += 1
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        charset=MYSQL_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


class _ReusableConn:
    """连接复用代理：对外与 pymysql.Connection 用法一致（cursor/commit/close…）。

    与"每次新建"的唯一差异是 close() 不真正断开：
      · 用引用计数 depth 处理"函数内部又调用另一个 db 函数"的嵌套取用，
        只有最外层 close() 才回滚残留事务并归还连接；
      · 事务语义与旧实现一致 —— 未提交的改动在归还时被回滚丢弃。
    """

    __slots__ = ("_conn", "_real_close", "_depth", "_last_holder")

    def __init__(self, conn: pymysql.Connection):
        self._conn = conn
        self._real_close = conn.close
        self._depth = 0
        self._last_holder = ""

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def raw(self) -> pymysql.Connection:
        return self._conn

    def acquire(self, holder: str = "") -> None:
        self._depth += 1
        self._last_holder = holder

    def close(self) -> None:
        if self._depth > 0:
            self._depth -= 1
        if self._depth > 0:
            return  # 仍是嵌套取用中，交给最外层归还
        try:
            if self._conn.open and not self._conn.get_autocommit():
                self._conn.rollback()  # 丢弃可能残留的未提交改动（与旧 close() 隐式回滚一致）
        except Exception:
            self._drop()

    def _drop(self) -> None:
        """真正断开并从本线程缓存里移除（连接已不可用时调用）"""
        try:
            self._real_close()
        except Exception:
            pass
        self._depth = 0
        if getattr(_local, "proxy", None) is self:
            _local.proxy = None


def _healthcheck(proxy: _ReusableConn) -> bool:
    """复用前探活；连接已断则尝试重连"""
    try:
        if not proxy.raw.open:
            return False
        proxy.raw.ping(reconnect=True)
        return True
    except Exception:
        return False


def _get_conn():
    """获取 MySQL 连接（默认按线程复用；DB_CONN_REUSE=0 时每次新建）"""
    if not _DB_CONN_REUSE:
        return _new_raw_conn()

    proxy = getattr(_local, "proxy", None)
    if proxy is not None and _healthcheck(proxy):
        if proxy._depth > 2:
            # 上层流程疑似漏了 close()，重置引用计数避免永远不回滚
            logging.getLogger(__name__).warning(
                "[DB] 连接复用深度异常(%d)，已重置；请检查是否有函数未 close()", proxy._depth)
            proxy._depth = 0
        proxy.acquire()
        _conn_stats["reuse"] += 1
        return proxy

    if proxy is not None:
        _conn_stats["reconnect"] += 1
        proxy._drop()

    proxy = _ReusableConn(_new_raw_conn())
    _local.proxy = proxy
    proxy.acquire()
    return proxy


def conn_reuse_stats() -> Dict[str, int]:
    """连接复用统计（验收/排查用：reuse 远大于 new 说明复用生效）"""
    return dict(_conn_stats)


def _row_to_dict(row) -> Optional[Dict]:
    if row is None:
        return None
    return dict(row)


def _rows_to_list(rows) -> List[Dict]:
    return [dict(r) for r in rows]


# ============================================================
# 初始化所有表
# ============================================================
def init_db():
    """初始化数据库所有表（幂等，可重复调用）"""
    # 先连接到 MySQL 服务器（不指定 database），创建数据库
    server_conn = pymysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, charset=MYSQL_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with server_conn.cursor() as cur:
        cur.execute(f"""
            CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}`
            DEFAULT CHARACTER SET {MYSQL_CHARSET}
            DEFAULT COLLATE {MYSQL_CHARSET}_unicode_ci
        """)
    server_conn.close()

    # 连接到具体数据库建表
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()

        # ---------- 1. 用户表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                username      VARCHAR(64)  NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                nickname      VARCHAR(64)  DEFAULT '',
                avatar        VARCHAR(512) DEFAULT '',
                email         VARCHAR(128) DEFAULT '',
                role          VARCHAR(20)  DEFAULT 'user',
                wx_openid     VARCHAR(64)  DEFAULT NULL,
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_username (username),
                UNIQUE KEY uk_wx_openid (wx_openid)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 已有库升级：users 表补充 wx_openid 列（新建库无需） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'wx_openid'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN wx_openid VARCHAR(64) DEFAULT NULL")
            cur.execute("ALTER TABLE users ADD UNIQUE KEY uk_wx_openid (wx_openid)")

        # ---------- 2. 会话表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id              INT UNSIGNED NOT NULL AUTO_INCREMENT,
                session_id      VARCHAR(128) NOT NULL,
                user_id         INT UNSIGNED NULL,
                title           VARCHAR(255) DEFAULT '新会话',
                message_count   INT          DEFAULT 0,
                last_message_at DATETIME     NULL,
                created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_session_id (session_id),
                KEY idx_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 3. 问答消息表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                session_id    VARCHAR(128) NOT NULL,
                user_id       INT UNSIGNED NULL,
                role          VARCHAR(20)  NOT NULL DEFAULT 'user',
                question      TEXT         NULL,
                answer        TEXT         NULL,
                content       TEXT         NULL,
                ref_docs      TEXT         NULL,
                search_query  VARCHAR(512) DEFAULT '',
                intent        VARCHAR(50)  DEFAULT '其他',
                elapsed_ms    FLOAT        DEFAULT 0,
                ip_address    VARCHAR(64)  DEFAULT '',
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_session_id (session_id),
                KEY idx_user_id (user_id),
                KEY idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 4. 文档元数据表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                filename      VARCHAR(255) NOT NULL,
                original_name VARCHAR(255) NOT NULL,
                file_size     BIGINT       DEFAULT 0,
                file_type     VARCHAR(20)  DEFAULT '',
                chunk_count   INT          DEFAULT 0,
                status        VARCHAR(20)  DEFAULT 'pending',
                error_msg     TEXT         NULL,
                uploaded_by   INT UNSIGNED NULL,
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_filename (filename),
                KEY idx_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 5. 文档切片表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                document_id   INT UNSIGNED NOT NULL,
                chunk_index   INT          NOT NULL,
                content       TEXT         NOT NULL,
                token_count   INT          DEFAULT 0,
                milvus_id     VARCHAR(128) DEFAULT '',
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_doc_chunk (document_id, chunk_index),
                KEY idx_document_id (document_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 6. 用户反馈表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                message_id    INT UNSIGNED NULL,
                session_id    VARCHAR(128) DEFAULT '',
                user_id       INT UNSIGNED NULL,
                rating        VARCHAR(20)  NOT NULL,
                comment       TEXT         NULL,
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_message_id (message_id),
                KEY idx_user_id (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 7. Agent配置持久化表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_config (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                config_key    VARCHAR(128) NOT NULL,
                config_value  TEXT         NOT NULL,
                description   VARCHAR(512) DEFAULT '',
                updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_config_key (config_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 7.1 用户画像表（小程序「AI 记得我」+ 商家端画像看板） ----------
        # 为什么单独建表而不塞进 agent_config 的 KV：看板要按 last_active_at 分群
        # 与排序，KV 表只能全表扫 + Python 侧反序列化，随用户量线性劣化。
        # ⚠️ 画像正文里绝不写入手机号 / wx_openid（见 docs/用户画像实现方案.md §3.4）。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id        INT UNSIGNED NOT NULL,
                profile_json   TEXT         NULL COMMENT '画像正文 family/interest/context/manual',
                tags_json      TEXT         NULL COMMENT '派生标签缓存，供看板筛选',
                msg_count      INT          DEFAULT 0,
                source_version VARCHAR(16)  DEFAULT 'v2',
                last_active_at DATETIME     NULL,
                created_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at     DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_profile_user (user_id),
                KEY idx_profile_active (last_active_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 7.2 画像版本表（审计：只 INSERT，不改不删） ----------
        # 与 db_sqlite.py 同构；为什么单独一张表见那边注释。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_versions (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id     INT UNSIGNED NOT NULL,
                rev         INT          DEFAULT 0,
                op          VARCHAR(16)  NOT NULL COMMENT 'created|modified|deleted',
                field_path  VARCHAR(128) DEFAULT '' COMMENT '预留：定位到具体字段',
                before_json MEDIUMTEXT   NULL,
                after_json  MEDIUMTEXT   NULL,
                sha256      CHAR(64)     DEFAULT '',
                actor       VARCHAR(16)  DEFAULT 'system' COMMENT 'user|session|system',
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                redacted_at DATETIME     NULL,
                redacted_by VARCHAR(64)  DEFAULT '',
                PRIMARY KEY (id),
                KEY idx_profile_ver_user (user_id, id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 8. 操作日志表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id       INT UNSIGNED NULL,
                action        VARCHAR(64)  NOT NULL,
                target        VARCHAR(255) DEFAULT '',
                detail        TEXT         NULL,
                ip_address    VARCHAR(64)  DEFAULT '',
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_user_id (user_id),
                KEY idx_action (action)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 9. 卡产品表（价格权威来源） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                name        VARCHAR(64)  NOT NULL,
                price       INT UNSIGNED NOT NULL DEFAULT 0,
                description VARCHAR(512) DEFAULT '',
                status      VARCHAR(20)  DEFAULT 'on_sale',
                sort        INT          DEFAULT 0,
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_card_name (name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 10. 办卡订单表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_orders (
                id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_no       VARCHAR(32)  NOT NULL,
                user_id        INT UNSIGNED NULL,
                card_id        INT UNSIGNED NOT NULL,
                card_name      VARCHAR(64)  DEFAULT '',
                amount         INT UNSIGNED NOT NULL DEFAULT 0,
                openid         VARCHAR(64)  DEFAULT '',
                status         VARCHAR(20)  DEFAULT 'pending',
                transaction_id VARCHAR(64)  DEFAULT '',
                created_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
                paid_at        DATETIME     NULL,
                PRIMARY KEY (id),
                UNIQUE KEY uk_order_no (order_no),
                KEY idx_orders_user (user_id),
                KEY idx_orders_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 11. 用户持卡表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_cards (
                id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id    INT UNSIGNED NULL,
                card_id    INT UNSIGNED NOT NULL,
                card_name  VARCHAR(64)  DEFAULT '',
                order_no   VARCHAR(32)  DEFAULT '',
                coins      INT UNSIGNED NULL,
                in_use     TINYINT      DEFAULT 0,
                kind       VARCHAR(10)  NOT NULL DEFAULT 'member' COMMENT 'member会员充值卡 / small小额充币卡',
                status     VARCHAR(20)  DEFAULT 'active',
                created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_ucards_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 12. 问答日志表 ----------
        # 注意：MySQL 的 TEXT/BLOB 列不允许 DEFAULT 值（严格模式报 1101），
        # 默认值由写入代码（add_question_log）保证非 NULL
        cur.execute("""
            CREATE TABLE IF NOT EXISTS question_logs (
                id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
                question         TEXT         NULL,
                search_query     VARCHAR(512) DEFAULT '',
                answer           TEXT         NULL,
                answer_full_len  INT          DEFAULT 0,
                references_json  TEXT,
                elapsed_seconds  FLOAT        DEFAULT 0,
                session_id       VARCHAR(128) DEFAULT '',
                source           VARCHAR(20)  DEFAULT 'web',
                answer_type      VARCHAR(24)  DEFAULT NULL COMMENT '回答类型 normal/cached/fallback_none/fallback_partial',
                created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_qlogs_created (created_at),
                KEY idx_qlogs_session (session_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 已有库升级：question_logs 补 intent 列（早期建的表无此列，
        #          CREATE TABLE IF NOT EXISTS 不会补，分析统计/意图识别落库依赖它） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'question_logs' AND COLUMN_NAME = 'intent'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE question_logs ADD COLUMN intent VARCHAR(50) DEFAULT '其他'")
            logger = logging.getLogger(__name__)
            logger.info("[升级] question_logs 表已补充 intent 列")

        # ---------- 已有库升级：question_logs 补 answer_type 列 ----------
        # 知识库盲区统计的正确判据：靠"回答类型"而不是"references 是否为空"
        # （缓存命中的回答 references 也是空的，但它是此前已答上来的正常回答）。
        # 历史数据该列为 NULL，统计侧用「答案开头的兜底话术」兜底识别，无需回填。
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'question_logs' AND COLUMN_NAME = 'answer_type'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE question_logs ADD COLUMN answer_type "
                        "VARCHAR(24) DEFAULT NULL COMMENT '回答类型 normal/cached/fallback_none/fallback_partial'")
            logger = logging.getLogger(__name__)
            logger.info("[升级] question_logs 表已补充 answer_type 列")

        # ---------- 已有库升级：users 表补充 phone 列（微信登录后必须绑定手机号） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'phone'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN phone VARCHAR(20) DEFAULT ''")
            logger = logging.getLogger(__name__)
            logger.info("[升级] users 表已补充 phone 列")

        # ---------- 已有库升级：users.phone 唯一约束（一个手机号 = 一个账号） ----------
        # 用**生成列**承载唯一约束：phone_uk = NULLIF(phone,'')。
        # 为什么要生成列而不是直接给 phone 建唯一索引：库里"未绑定"一律写空串，
        # 直接建索引会让所有空串行互相冲突；生成列把空串/ NULL 都变成 NULL，
        # 而 MySQL 唯一索引不约束多个 NULL → 现有写 '' 的代码零改动。
        # 建索引前必须先清掉历史重复（_fix_phone_dup.py），否则 ALTER 会失败——
        # 这里捕获异常只告警，不阻断启动，避免"升级脚本一挂服务就起不来"。
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'phone_uk'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            _log = logging.getLogger(__name__)
            try:
                cur.execute(
                    "ALTER TABLE users ADD COLUMN phone_uk VARCHAR(20) "
                    "GENERATED ALWAYS AS (NULLIF(phone, '')) VIRTUAL, "
                    "ADD UNIQUE KEY uk_phone (phone_uk)"
                )
                _log.info("[升级] users 表已加手机号唯一约束（生成列 phone_uk）")
            except Exception as e:
                _log.warning(
                    "[升级] users 手机号唯一约束未建立（多半是库里已存在重复手机号）：%s\n"
                    "        → 运行 `python _fix_phone_dup.py` 清理重复后重启即可自动补上", e)

        # ---------- 已有库升级：user_cards 表补充 coins 列（持卡实际余币，发卡时写入面值，
        #          商家可调整；旧数据 NULL 时按卡产品面值解析兑底） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user_cards' AND COLUMN_NAME = 'coins'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE user_cards ADD COLUMN coins INT NULL DEFAULT NULL")

        # ---------- 已有库升级：users 表补充 bonus_spent 列（签到奖励币已消费额） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'bonus_spent'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN bonus_spent INT DEFAULT 0")
            logger = logging.getLogger(__name__)
            logger.info("[升级] users 表已补充 bonus_spent 列")

        # ---------- 已有库升级：user_cards 表补充 in_use 列（用户选中的"生效中"卡，
        #          充值页会员码按它出码；仅一张卡时自动置为生效） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user_cards' AND COLUMN_NAME = 'in_use'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE user_cards ADD COLUMN in_use TINYINT DEFAULT 0")
            logger = logging.getLogger(__name__)
            logger.info("[升级] user_cards 表已补充 in_use 列")

        # ---------- 已有库升级：user_cards 表补充 kind 列（member会员充值卡 / small小额充币卡）。
        #          小额充币（10/20/30币）直接进余币：不进会员卡列表、不提供"使用/退卡"，
        #          历史小额纯币卡（卡名形如"10币"）自动回填 small ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user_cards' AND COLUMN_NAME = 'kind'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE user_cards ADD COLUMN kind VARCHAR(10) NOT NULL DEFAULT 'member'")
            cur.execute(
                """UPDATE user_cards uc JOIN cards c ON c.id = uc.card_id
                   SET uc.kind = 'small' WHERE c.name REGEXP '^[0-9]+币$'"""
            )
            logger = logging.getLogger(__name__)
            logger.info("[升级] user_cards 表已补充 kind 列并按卡名回填 small/member")

        # ---------- 0 币自动清理：余额扣到 0 的持卡记录不再展示（保留订单流水可追溯） ----------
        cur.execute(
            "DELETE FROM user_cards WHERE status = 'active' AND coins IS NOT NULL AND coins <= 0"
        )
        # 生效标记不允许落在小额充币卡上（会员码仅按会员卡出码）
        cur.execute("UPDATE user_cards SET in_use = 0 WHERE kind = 'small'")

        # ---------- 13. 每日签到表（我的页签到送币，奖励计入余币余额） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_checkins (
                id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id      INT UNSIGNED NULL,
                checkin_date DATE         NOT NULL,
                reward_coins INT          NOT NULL DEFAULT 1,
                created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_user_date (user_id, checkin_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 13.5 游乐项目价目表（首页游乐消费数据源） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS park_projects (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                name        VARCHAR(64)  NOT NULL,
                price_coins INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '单价（币）',
                unit_desc   VARCHAR(32)  DEFAULT '' COMMENT '单位说明（5分钟/15圈/80发）',
                status      VARCHAR(20)  DEFAULT 'on' COMMENT 'on上架 / off下架',
                sort        INT          DEFAULT 0,
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 13.6 消费订单表（多选项目合并扣币，生成核销码） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consumption_orders (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_no    VARCHAR(32)  NOT NULL COMMENT '消费单号(CF前缀)',
                verify_code VARCHAR(16)  NOT NULL COMMENT '唯一核销码',
                user_id     INT UNSIGNED NULL,
                items_json  TEXT         NULL COMMENT '消费明细JSON[{name,qty,coins}]',
                card_coins  INT          DEFAULT 0 COMMENT '卡内币扣除部分',
                bonus_coins INT          DEFAULT 0 COMMENT '签到奖励币扣除部分',
                total_coins INT          NOT NULL DEFAULT 0,
                status      VARCHAR(20)  DEFAULT 'unused' COMMENT 'unused未核销 / used已核销',
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_verify_code (verify_code),
                KEY idx_co_user (user_id),
                KEY idx_co_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 13.7 消费券明细表（一券 = 一项一次，一券一码） ----------
        # 为什么要在 consumption_orders 之外再拆一张表：
        #   一单一码时，员工扫到码只知道"这单买过东西"，看不到明细，忙起来也记不住；
        #   而整单一次核销更危险——买 3 项拆 2 次玩，剩 1 项没玩但单已核销，
        #   用户失去凭据、员工也查不出来。拆成券后：扫一张核销一张，两张表职责分明。
        #   consumption_orders 继续当"订单/金额/整单状态"的真源，items_json 保留作展示快照。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consumption_items (
                id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
                order_no     VARCHAR(32)  NOT NULL COMMENT '所属消费单号',
                user_id      INT UNSIGNED NULL,
                project_id   INT UNSIGNED NULL COMMENT '游乐项目 id（历史数据可能为 NULL）',
                project_name VARCHAR(64)  NOT NULL COMMENT '项目名快照',
                unit_coins   INT          NOT NULL DEFAULT 0 COMMENT '单价快照',
                item_code    VARCHAR(16)  NOT NULL COMMENT '券码（全园唯一）',
                status       VARCHAR(20)  DEFAULT 'unused' COMMENT 'unused待玩 / used已玩 / refunded已退',
                used_at      DATETIME     NULL COMMENT '核销时刻',
                created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_item_code (item_code),
                KEY idx_ci_order (order_no),
                KEY idx_ci_user_status (user_id, status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 14. 失物招领表（游客报失 + 商家登记统一管理） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lost_found_items (
                id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
                name       VARCHAR(128) NOT NULL COMMENT '失物名称',
                photo_url  VARCHAR(512) DEFAULT '' COMMENT '照片路径(/static/lost_found/xxx.jpg)',
                status     VARCHAR(20)  DEFAULT 'lost' COMMENT 'lost待认领 / claimed已认领',
                source     VARCHAR(20)  DEFAULT 'user' COMMENT 'user游客报失 / merchant商家登记',
                reporter   VARCHAR(64)  DEFAULT '' COMMENT '报失人昵称/联系方式',
                ai_category VARCHAR(32) DEFAULT '' COMMENT 'AI识别：类别',
                ai_color    VARCHAR(32) DEFAULT '' COMMENT 'AI识别：主色',
                ai_brand    VARCHAR(64) DEFAULT '' COMMENT 'AI识别：品牌',
                ai_features VARCHAR(512) DEFAULT '' COMMENT 'AI识别：外观特征（；分隔）',
                ai_text     VARCHAR(255) DEFAULT '' COMMENT 'AI识别：图中可见文字',
                created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_lf_status (status),
                KEY idx_lf_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 15. 园区公告 / 今日热点（仅商家 PC 端发布，小程序首页展示） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS park_notices (
                id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
                kind       VARCHAR(20)  NOT NULL DEFAULT 'announcement' COMMENT 'announcement园区公告 / hot今日热点',
                title      VARCHAR(128) NOT NULL COMMENT '标题（列表首行）',
                content    VARCHAR(512) DEFAULT '' COMMENT '正文内容',
                status     VARCHAR(20)  DEFAULT 'on' COMMENT 'on上架 / off下架',
                sort       INT          DEFAULT 0 COMMENT '排序（越小越靠前）',
                created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_pn_kind (kind),
                KEY idx_pn_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 16. 游客意见反馈（小程序「意见反馈」弹窗提交，商家端查看） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id    INT UNSIGNED NULL COMMENT '微信用户id（登录时自动关联，可空）',
                nickname   VARCHAR(64)  DEFAULT '' COMMENT '游客昵称/联系方式（选填）',
                content    VARCHAR(512) NOT NULL COMMENT '反馈内容',
                created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_uf_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 17. 热点问题（近 7 天高频问题 Top6） ----------
        # 小程序 AI 问答页"快捷提问"6 个按钮的数据源：
        # 由 services/hot_questions.py 每 7 天基于 question_logs 聚类统计写入，
        # 商家 PC「分析图谱」页可查看并手动刷新。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hot_questions (
                id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
                question     VARCHAR(255) NOT NULL COMMENT '完整问句（点击后直接发送）',
                label        VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '压缩标签（≤4字，按钮文案）',
                icon         VARCHAR(8)   NOT NULL DEFAULT '' COMMENT 'emoji 图标',
                hit_count    INT          NOT NULL DEFAULT 0 COMMENT '近7天该问题簇提问次数',
                raw_count    INT          NOT NULL DEFAULT 0 COMMENT '簇内原始记录条数',
                window_start DATETIME     NULL COMMENT '统计窗口起',
                window_end   DATETIME     NULL COMMENT '统计窗口止',
                source       VARCHAR(16)  NOT NULL DEFAULT 'auto' COMMENT 'auto自动统计 / manual商家指定',
                sort         INT          NOT NULL DEFAULT 0 COMMENT '展示顺序（越小越靠前）',
                refreshed_at DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '该批数据的刷新时间',
                PRIMARY KEY (id),
                KEY idx_hq_sort (sort)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 18. 用户收藏（AI 回答操作条「收藏」→ 我的页「我的收藏」） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_favorites (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                user_id     INT UNSIGNED NOT NULL COMMENT '收藏人',
                session_id  VARCHAR(128) DEFAULT '' COMMENT '收藏时所在会话',
                question    TEXT         NULL COMMENT '用户提问',
                answer      MEDIUMTEXT   NULL COMMENT 'AI 回答原文',
                images_json TEXT         NULL COMMENT '收藏附图JSON数组（用户上传原图/地图图/图表）',
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                KEY idx_ufav_user (user_id),
                KEY idx_ufav_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)

        # ---------- 已有库升级：user_favorites 补 images_json 列 ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'user_favorites'
                 AND COLUMN_NAME = 'images_json'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE user_favorites ADD COLUMN images_json TEXT NULL")
            logger = logging.getLogger(__name__)
            logger.info("[升级] user_favorites 表已补充 images_json 列")

        # ---------- 已有库升级：users 表补充 pwd_set 列 ----------
        # pwd_set=1 表示用户自己设过登录密码（可走「手机号+密码」登录，密码错误即拒绝）；
        # pwd_set=0 表示密码是系统随机占位串（微信/验证码时代的账号），
        # 首次在手机号密码登录页输入密码时视为「设置密码」并置 1。
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'pwd_set'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN pwd_set TINYINT DEFAULT 0")
            logger = logging.getLogger(__name__)
            logger.info("[升级] users 表已补充 pwd_set 列")

        # ---------- 已有库升级：users 表补充 email 列（管理端「个人信息」可填写邮箱） ----------
        cur.execute(
            """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
               WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'users' AND COLUMN_NAME = 'email'""",
            (MYSQL_DATABASE,)
        )
        if cur.fetchone()["c"] == 0:
            cur.execute("ALTER TABLE users ADD COLUMN email VARCHAR(128) DEFAULT ''")
            logger = logging.getLogger(__name__)
            logger.info("[升级] users 表已补充 email 列")

        # ---------- 已有库升级：lost_found_items 补充 AI 识别列 ----------
        # 用途：失物照片交给多模态大模型识别后落库（类别/主色/品牌/外观特征/可见文字），
        # 供商家端按特征比对、辅助认领匹配。
        # 空字符串 = 未识别或识别失败（识别是增强项，失败不影响报失本身）。
        _lf_logger = logging.getLogger(__name__)
        for _col, _type in (("ai_category", "VARCHAR(32) DEFAULT ''"),
                            ("ai_color", "VARCHAR(32) DEFAULT ''"),
                            ("ai_brand", "VARCHAR(64) DEFAULT ''"),
                            ("ai_features", "VARCHAR(512) DEFAULT ''"),
                            ("ai_text", "VARCHAR(255) DEFAULT ''")):
            cur.execute(
                """SELECT COUNT(*) AS c FROM information_schema.COLUMNS
                   WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'lost_found_items'
                     AND COLUMN_NAME = %s""",
                (MYSQL_DATABASE, _col)
            )
            if cur.fetchone()["c"] == 0:
                cur.execute(f"ALTER TABLE lost_found_items ADD COLUMN {_col} {_type}")
                _lf_logger.info(f"[升级] lost_found_items 表已补充 {_col} 列")

        # ---------- 19. 小程序登录历史账号（本机设备维度） ----------
        # 用途：下次打开小程序时直接展示「上次登录的账号（头像+昵称+手机尾号）」，
        # 点击即凭记忆令牌免密进入；退出登录后历史仍保留，方便切回。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mp_login_accounts (
                id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
                device_id     VARCHAR(64)  NOT NULL COMMENT '小程序本地生成并持久化的设备标识',
                user_id       INT UNSIGNED NOT NULL COMMENT 'users.id',
                phone         VARCHAR(20)  DEFAULT '' COMMENT '登录手机号',
                username      VARCHAR(64)  DEFAULT '' COMMENT '登录用户名',
                nickname      VARCHAR(64)  DEFAULT '' COMMENT '登录时昵称快照',
                avatar        VARCHAR(512) DEFAULT '' COMMENT '登录时头像快照',
                login_count   INT          DEFAULT 0 COMMENT '本机累计登录次数',
                last_login_at DATETIME     NULL COMMENT '本机最近一次登录时间',
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_device_user (device_id, user_id),
                KEY idx_dla_device (device_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
              COMMENT='小程序历史登录账号（设备维度）'
        """)

        # ---------- 20. 小程序免密登录令牌（"记住我 / 点头像直接登录"） ----------
        # 密码只存不可逆哈希，所以「下次免密」不能靠存密码：
        # 登录时签发随机令牌给小程序本地保存，库里只存 sha256(令牌)，30 天有效、可吊销。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mp_remember_tokens (
                id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
                token_hash  VARCHAR(64)  NOT NULL COMMENT 'sha256(明文令牌)，不存明文',
                user_id     INT UNSIGNED NOT NULL COMMENT 'users.id',
                device_id   VARCHAR(64)  DEFAULT '' COMMENT '签发时的设备标识',
                expires_at  DATETIME     NOT NULL COMMENT '过期时间（默认 30 天）',
                revoked     TINYINT      DEFAULT 0 COMMENT '1=已吊销（登出/改密时置位）',
                created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id),
                UNIQUE KEY uk_token_hash (token_hash),
                KEY idx_mrt_user (user_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
              COMMENT='小程序免密登录令牌'
        """)

        # ---------- 种子数据：游乐项目价目（空表时自动灌入，知识库真实价目） ----------
        cur.execute("SELECT COUNT(*) AS c FROM park_projects")
        if cur.fetchone()["c"] == 0:
            cur.execute(
                """INSERT INTO park_projects (name, price_coins, unit_desc, status, sort) VALUES
                   ('旋转木马', 30, '5分钟', 'on', 1),
                   ('荡秋千', 40, '4分钟', 'on', 2),
                   ('小型过山车', 50, '15圈', 'on', 3),
                   ('蹦床', 40, '5分钟', 'on', 4),
                   ('浴室大作战', 40, '5分钟', 'on', 5),
                   ('大炮乐园', 30, '80发子弹', 'on', 6)"""
            )
            logger = logging.getLogger(__name__)
            logger.info("[种子] park_projects 已灌入 6 个游乐项目")

        # ---------- 创建默认管理员账号 ----------
        from core.auth import hash_password
        cur.execute("SELECT id FROM users WHERE username = %s", ("admin",))
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, nickname, role) VALUES (%s, %s, %s, %s)",
                ("admin", hash_password("admin123"), "管理员", "admin")
            )

        conn.commit()
        cur.close()
        conn.close()


# ============================================================
# 1. 用户相关
# ============================================================
def create_user(username: str, password: str, nickname: str = "") -> Optional[Dict]:
    from core.auth import hash_password
    with _lock:
        conn = _get_conn()
        try:
            password_hash = hash_password(password)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO users (username, password_hash, nickname) VALUES (%s, %s, %s)",
                    (username, password_hash, nickname or username)
                )
                user_id = cur.lastrowid
            conn.commit()
            user = get_user_by_id(user_id)
            conn.close()
            return user
        except pymysql.err.IntegrityError:
            conn.close()
            return None


def get_user_by_username(username: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def update_user(user_id: int, **kwargs) -> Optional[Dict]:
    # username 也允许改（管理端「个人信息」页），唯一性由调用方先校验（uk_username 兜底）
    # role 允许改（管理端「用户管理」页提权/降权），调用方必须先做权限校验
    allowed_fields = {"nickname", "avatar", "password_hash", "phone", "pwd_set",
                      "email", "username", "role"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        return get_user_by_id(user_id)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            set_clause = ", ".join([f"{k} = %s" for k in updates])
            values = list(updates.values()) + [user_id]
            cur.execute(f"UPDATE users SET {set_clause} WHERE id = %s", values)
        conn.commit()
        conn.close()
    return get_user_by_id(user_id)


# ------------------------------------------------------------
# 账号彻底删除（管理端「用户管理」）
# 本项目所有表都**没有外键**，级联删除必须手工列全 —— 漏一张就留下指向
# 已删账号的孤儿行（统计/画像/收藏都会读到它）。两类处理方式：
#   · 业务与个人数据：整行删除（下表）；
#   · 审计类：documents.uploaded_by / operation_logs 只断开关联（置 NULL），
#     保留记录本身，否则审计轨迹会凭空少行。
# ------------------------------------------------------------
_USER_OWNED_TABLES = (
    "chat_messages", "chat_sessions", "feedback",
    "user_profiles", "user_profile_versions",
    "card_orders", "user_cards", "consumption_orders", "consumption_items",
    "user_checkins", "user_favorites", "user_feedback",
    "mp_login_accounts", "mp_remember_tokens",
)


def delete_user(user_id: int) -> Optional[Dict[str, int]]:
    """彻底删除账号及其全部个人数据；账号不存在返回 None。

    返回各表被删行数（只列非零项），供接口回显"删了什么"。
    """
    if not user_id:
        return None
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE id = %s", (user_id,))
                if not cur.fetchone():
                    return None
                deleted: Dict[str, int] = {}
                for table in _USER_OWNED_TABLES:
                    cur.execute(f"DELETE FROM `{table}` WHERE user_id = %s", (user_id,))
                    if cur.rowcount:
                        deleted[table] = cur.rowcount
                cur.execute("UPDATE documents SET uploaded_by = NULL WHERE uploaded_by = %s",
                            (user_id,))
                cur.execute("UPDATE operation_logs SET user_id = NULL WHERE user_id = %s",
                            (user_id,))
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
                deleted["users"] = cur.rowcount
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def list_users() -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT id, username, nickname, phone, role, created_at FROM users ORDER BY id")
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def search_users(keyword: str = "", only_bound: bool = False,
                 page: int = 1, limit: int = 20) -> Dict:
    """管理端用户列表（搜索 + 筛选 + 分页）。

    keyword    模糊匹配 手机号 / 昵称 / 用户名，纯数字时也按 ID 精确匹配；
    only_bound True 时只看"能登录的账号"（绑过手机号或有微信 openid），
               把微信静默登录留下的空壳与测试残留滤掉；
    返回 {"users": [...], "total": 总数}，users 里带 has_wx 标记供前端渲染来源标签。
    """
    where, args = [], []
    keyword = (keyword or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(COALESCE(phone, '') LIKE %s OR COALESCE(nickname, '') LIKE %s "
                     "OR username LIKE %s OR CAST(id AS CHAR) = %s)")
        args += [like, like, like, keyword]
    if only_bound:
        where.append("(COALESCE(phone, '') <> '' OR COALESCE(wx_openid, '') <> '')")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 20), 200))
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM users{sql_where}", args)
        total = cur.fetchone()["c"]
        cur.execute(
            "SELECT id, username, nickname, phone, role, created_at, "
            "(wx_openid IS NOT NULL AND wx_openid <> '') AS has_wx "
            f"FROM users{sql_where} ORDER BY id LIMIT %s OFFSET %s",
            args + [limit, (page - 1) * limit]
        )
        rows = cur.fetchall()
    conn.close()
    return {"users": _rows_to_list(rows), "total": total}


def dup_phone_groups() -> List[Dict]:
    """列出仍重复的手机号（唯一索引建好后应恒为空，留作管理端兜底监控）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT phone, COUNT(*) AS cnt, GROUP_CONCAT(id ORDER BY id) AS ids "
            "FROM users WHERE COALESCE(phone, '') <> '' "
            "GROUP BY phone HAVING cnt > 1 ORDER BY cnt DESC"
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def authenticate(username: str, password: str) -> Optional[Dict]:
    """登录校验（用户端与员工端共用）。

    为什么要 strip：手机端/员工端的登录框要么手输、要么**从聊天记录里粘贴**，
    前后带空格是最常见的一类"密码没错但登不上"。用户名一律去首尾空格；
    口令先按原样验（保持已有口令的语义不变），原样失败再拿去空格的版本重试一次
    —— 只放宽"首尾空白"这一件事，**中间空格仍然算口令的一部分**。
    """
    from core.auth import verify_password

    user = get_user_by_username((username or "").strip())
    if not user:
        return None
    if verify_password(password or "", user["password_hash"]):
        return user
    trimmed = (password or "").strip()
    if trimmed and trimmed != password and verify_password(trimmed, user["password_hash"]):
        return user
    return None


# ============================================================
# 2. 会话相关
# ============================================================
def create_session(session_id: str, user_id: Optional[int] = None, title: str = "新会话") -> Dict:
    existing = get_session(session_id)
    if existing:
        return existing
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_sessions (session_id, user_id, title) VALUES (%s, %s, %s)",
                (session_id, user_id, title)
            )
        conn.commit()
        conn.close()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM chat_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_sessions(user_id: Optional[int] = None, limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        if user_id:
            cur.execute(
                "SELECT * FROM chat_sessions WHERE user_id = %s ORDER BY last_message_at DESC LIMIT %s",
                (user_id, limit)
            )
        else:
            cur.execute(
                "SELECT * FROM chat_sessions ORDER BY last_message_at DESC LIMIT %s", (limit,)
            )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def update_session(session_id: str, **kwargs) -> Optional[Dict]:
    allowed = {"title", "message_count", "last_message_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_session(session_id)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            set_clause = ", ".join([f"{k} = %s" for k in updates])
            values = list(updates.values()) + [session_id]
            cur.execute(f"UPDATE chat_sessions SET {set_clause} WHERE session_id = %s", values)
        conn.commit()
        conn.close()
    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE session_id = %s", (session_id,))
            cur.execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))
            deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


# ============================================================
# 3. 问答消息相关
# ============================================================
def add_message(
    session_id: str,
    role: str,
    content: str = "",
    question: str = "",
    answer: str = "",
    ref_docs: Optional[List] = None,
    search_query: str = "",
    intent: str = "其他",
    elapsed_ms: float = 0,
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> int:
    refs_json = json.dumps(ref_docs or [], ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO chat_messages
                   (session_id, user_id, role, content, question, answer, ref_docs, search_query, intent, elapsed_ms, ip_address)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (session_id, user_id, role, content, question, answer, refs_json, search_query, intent, elapsed_ms, ip_address)
            )
            msg_id = cur.lastrowid
            cur.execute(
                """UPDATE chat_sessions
                   SET message_count = message_count + 1,
                       last_message_at = NOW(),
                       updated_at = NOW()
                   WHERE session_id = %s""",
                (session_id,)
            )
        conn.commit()
        conn.close()
    return msg_id


def get_messages(session_id: str, limit: int = 100, offset: int = 0) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM chat_messages WHERE session_id = %s ORDER BY id ASC LIMIT %s OFFSET %s",
            (session_id, limit, offset)
        )
        rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["ref_docs"] = json.loads(d.get("ref_docs", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["ref_docs"] = []
        result.append(d)
    return result


def get_recent_messages(limit: int = 20) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM chat_messages WHERE role = 'user' ORDER BY id DESC LIMIT %s", (limit,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_messages(session_id: Optional[str] = None) -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        if session_id:
            cur.execute("SELECT COUNT(*) as cnt FROM chat_messages WHERE session_id = %s", (session_id,))
        else:
            cur.execute("SELECT COUNT(*) as cnt FROM chat_messages")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 4. 文档元数据相关
# ============================================================
def upsert_document(
    filename: str,
    original_name: str,
    file_size: int = 0,
    file_type: str = "",
    status: str = "pending",
    chunk_count: int = 0,
    uploaded_by: Optional[int] = None,
    error_msg: str = "",
) -> Dict:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE filename = %s", (filename,))
            existing = cur.fetchone()
            if existing:
                cur.execute(
                    """UPDATE documents
                       SET original_name=%s, file_size=%s, file_type=%s, status=%s,
                           chunk_count=%s, uploaded_by=%s, error_msg=%s, updated_at=NOW()
                       WHERE filename=%s""",
                    (original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg, filename)
                )
                doc_id = existing["id"]
            else:
                cur.execute(
                    """INSERT INTO documents (filename, original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (filename, original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg)
                )
                doc_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_document(doc_id)


def get_document(doc_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE id = %s", (doc_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def get_document_by_filename(filename: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM documents WHERE filename = %s", (filename,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_documents(status: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute("SELECT * FROM documents WHERE status = %s ORDER BY id DESC", (status,))
        else:
            cur.execute("SELECT * FROM documents ORDER BY id DESC")
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def update_document_status(filename: str, status: str, chunk_count: int = 0, error_msg: str = ""):
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status=%s, chunk_count=%s, error_msg=%s, updated_at=NOW() WHERE filename=%s",
                (status, chunk_count, error_msg, filename)
            )
        conn.commit()
        conn.close()


def delete_document(filename: str) -> bool:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM documents WHERE filename = %s", (filename,))
            deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


# ============================================================
# 5. 文档切片相关
# ============================================================
def add_chunk(document_id: int, chunk_index: int, content: str, token_count: int = 0, milvus_id: str = "") -> int:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO document_chunks (document_id, chunk_index, content, token_count, milvus_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE content=VALUES(content), token_count=VALUES(token_count), milvus_id=VALUES(milvus_id)""",
                (document_id, chunk_index, content, token_count, milvus_id)
            )
            cid = cur.lastrowid
        conn.commit()
        conn.close()
    return cid


def get_chunks_by_document(document_id: int) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM document_chunks WHERE document_id = %s ORDER BY chunk_index ASC", (document_id,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_chunks(document_id: Optional[int] = None) -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        if document_id:
            cur.execute("SELECT COUNT(*) as cnt FROM document_chunks WHERE document_id = %s", (document_id,))
        else:
            cur.execute("SELECT COUNT(*) as cnt FROM document_chunks")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 6. 用户反馈相关
# ============================================================
def add_feedback(
    rating: str,
    message_id: Optional[int] = None,
    session_id: str = "",
    user_id: Optional[int] = None,
    comment: str = "",
) -> int:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback (message_id, session_id, user_id, rating, comment) VALUES (%s, %s, %s, %s, %s)",
                (message_id, session_id, user_id, rating, comment)
            )
            fid = cur.lastrowid
        conn.commit()
        conn.close()
    return fid


def list_feedback(limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 7. Agent配置持久化相关
# ============================================================
def set_config(key: str, value: Any, description: str = ""):
    val_json = json.dumps(value, ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO agent_config (config_key, config_value, description)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE config_value=VALUES(config_value), description=VALUES(description)""",
                (key, val_json, description)
            )
        conn.commit()
        conn.close()


def get_config(key: str, default: Any = None) -> Any:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT config_value FROM agent_config WHERE config_key = %s", (key,))
        row = cur.fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["config_value"])
        except (json.JSONDecodeError, TypeError):
            return row["config_value"]
    return default


def get_all_config() -> Dict[str, Any]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT config_key, config_value FROM agent_config")
        rows = cur.fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["config_key"]] = json.loads(r["config_value"])
        except (json.JSONDecodeError, TypeError):
            result[r["config_key"]] = r["config_value"]
    return result


# ============================================================
# 7.1 用户画像（小程序「AI 记得我」/ 商家端画像看板）
# ------------------------------------------------------------
# 一个用户一行；画像正文与派生标签都按 JSON 文本存。
# 业务逻辑一律走 services/user_profile.py，不要在别处直接拼 SQL。
# ============================================================
def _parse_profile_row(d: Optional[Dict]) -> Optional[Dict]:
    """把 profile_json / tags_json 两个文本列解成对象（坏数据降级为空，不抛）"""
    if not d:
        return None
    for k, default in (("profile_json", {}), ("tags_json", [])):
        raw = d.get(k)
        try:
            d[k] = (json.loads(raw) if raw else default) or default
        except (json.JSONDecodeError, TypeError):
            d[k] = default
    return d


def upsert_user_profile(user_id: int, profile: Dict, tags: Optional[List[str]] = None,
                        msg_count: int = 0) -> None:
    """按 user_id 写入/更新画像（last_active_at 每次刷新为当前时间）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_profiles
                       (user_id, profile_json, tags_json, msg_count, last_active_at)
                   VALUES (%s, %s, %s, %s, NOW())
                   ON DUPLICATE KEY UPDATE
                       profile_json=VALUES(profile_json),
                       tags_json=VALUES(tags_json),
                       msg_count=VALUES(msg_count),
                       last_active_at=NOW()""",
                (user_id, json.dumps(profile or {}, ensure_ascii=False),
                 json.dumps(tags or [], ensure_ascii=False), int(msg_count or 0))
            )
        conn.commit()
        conn.close()


def get_user_profile(user_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM user_profiles WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    conn.close()
    return _parse_profile_row(_row_to_dict(row))


def delete_user_profile(user_id: int) -> int:
    """删除画像，返回受影响行数（0 = 本来就没有）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_profiles WHERE user_id = %s", (user_id,))
            n = cur.rowcount
        conn.commit()
        conn.close()
    return int(n or 0)


def delete_user_messages(user_id: int) -> int:
    """删除某用户在 chat_messages 的全部消息（「清除记忆」时随画像一起清）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_messages WHERE user_id = %s", (user_id,))
            n = cur.rowcount
        conn.commit()
        conn.close()
    return int(n or 0)


def list_user_profiles(limit: int = 20, offset: int = 0) -> List[Dict]:
    """按最近活跃倒序分页（商家端画像看板）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM user_profiles
               ORDER BY last_active_at DESC, id DESC LIMIT %s OFFSET %s""",
            (int(limit), int(offset))
        )
        rows = cur.fetchall()
    conn.close()
    return [r for r in (_parse_profile_row(_row_to_dict(x)) for x in rows) if r]


def count_user_profiles() -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM user_profiles")
        n = cur.fetchone()["c"]
    conn.close()
    return int(n or 0)


# ============================================================
# 7.2 画像版本（审计轨迹）—— 只 INSERT，不改不删
# ------------------------------------------------------------
# 与 db_sqlite.py 同构；业务逻辑一律走 services/user_profile.py。
# ============================================================
def add_profile_version(user_id: int, op: str, before: Dict, after: Dict,
                        actor: str = "system", rev: int = 0) -> int:
    """追加一条不可变版本记录，返回自增 id。"""
    import hashlib
    before_json = json.dumps(before or {}, ensure_ascii=False, sort_keys=True)
    after_json = json.dumps(after or {}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(after_json.encode("utf-8")).hexdigest()
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_profile_versions
                       (user_id, rev, op, before_json, after_json, sha256, actor)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (int(user_id), int(rev or 0), str(op or "modified"),
                 before_json, after_json, digest, str(actor or "system")))
            lid = cur.lastrowid
        conn.commit()
        conn.close()
    return int(lid or 0)


def list_profile_versions(user_id: int, limit: int = 50) -> List[Dict]:
    """某用户的版本轨迹（倒序）。已 redact 的记录内容为 None，但行还在。"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM user_profile_versions
               WHERE user_id = %s ORDER BY id DESC LIMIT %s""",
            (int(user_id), int(limit)))
        rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        for k in ("before_json", "after_json"):
            raw = d.get(k)
            try:
                d[k] = json.loads(raw) if raw else None
            except (json.JSONDecodeError, TypeError):
                d[k] = None
        out.append(d)
    return out


def redact_profile_version(version_id: int, redacted_by: str = "admin") -> int:
    """脱敏：抹掉内容但**保留轨迹**（redacted_at / redacted_by 留着），返回受影响行数。"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE user_profile_versions
                      SET before_json = NULL, after_json = NULL, sha256 = '',
                          redacted_at = NOW(), redacted_by = %s
                    WHERE id = %s""",
                (str(redacted_by or "admin"), int(version_id)))
            n = cur.rowcount
        conn.commit()
        conn.close()
    return int(n or 0)


# ============================================================
# 8. 操作日志相关
# ============================================================
def add_operation_log(
    action: str,
    target: str = "",
    detail: Optional[Dict] = None,
    user_id: Optional[int] = None,
    ip_address: str = "",
) -> int:
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operation_logs (user_id, action, target, detail, ip_address) VALUES (%s, %s, %s, %s, %s)",
                (user_id, action, target, detail_json, ip_address)
            )
            lid = cur.lastrowid
        conn.commit()
        conn.close()
    return lid


def list_operation_logs(limit: int = 100, user_id: Optional[int] = None) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        if user_id:
            cur.execute(
                "SELECT * FROM operation_logs WHERE user_id = %s ORDER BY id DESC LIMIT %s", (user_id, limit)
            )
        else:
            cur.execute("SELECT * FROM operation_logs ORDER BY id DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 统计辅助
# ============================================================
def get_stats() -> Dict:
    conn = _get_conn()
    stats = {}
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) as c FROM users")
        stats["total_users"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM chat_sessions")
        stats["total_sessions"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM chat_messages")
        stats["total_messages"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM documents")
        stats["total_documents"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM document_chunks")
        stats["total_chunks"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM feedback")
        stats["total_feedback"] = cur.fetchone()["c"]

        # 意图分布
        cur.execute(
            "SELECT intent, COUNT(*) as cnt FROM chat_messages WHERE role='user' GROUP BY intent"
        )
        intent_rows = cur.fetchall()
        stats["intent_distribution"] = {r["intent"]: r["cnt"] for r in intent_rows}

        # 近7天问答量
        cur.execute(
            """SELECT DATE(created_at) as day, COUNT(*) as cnt
               FROM chat_messages WHERE role='user' AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
               GROUP BY DATE(created_at) ORDER BY day"""
        )
        daily_rows = cur.fetchall()
        stats["daily_questions"] = {str(r["day"]): r["cnt"] for r in daily_rows}

    conn.close()
    return stats


# ============================================================
# 9. 微信用户相关
# ============================================================
def get_user_by_openid(openid: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE wx_openid = %s", (openid,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def create_wechat_user(openid: str, nickname: str = "") -> Optional[Dict]:
    """创建微信小程序用户：无密码登录凭证，username 由 openid 生成，随机串哈希占位。
    username 取 openid 前 24 字符——不同 openid 可能撞同一前缀，撞名时换随机后缀重试
    （否则新建账号会静默失败，用户看到的是"用户创建失败"）。"""
    import secrets
    from core.auth import hash_password
    for attempt in range(3):
        if attempt == 0:
            username = f"wx_{openid[:24]}"
        else:
            username = f"wx_{openid[:20]}_{secrets.token_hex(3)}"
        with _lock:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO users (username, password_hash, nickname, wx_openid)
                           VALUES (%s, %s, %s, %s)""",
                        (username, hash_password(secrets.token_urlsafe(24)),
                         nickname or f"游客{openid[-6:]}", openid)
                    )
                    user_id = cur.lastrowid
                conn.commit()
                conn.close()
                return get_user_by_id(user_id)
            except pymysql.err.IntegrityError:
                conn.close()
                # openid 已存在（同一微信号并发登录）→ 直接返回已有用户
                existed = get_user_by_openid(openid)
                if existed:
                    return existed
                continue  # 只是用户名撞名 → 换后缀重试
    return None


def bind_wechat(user_id: int, openid: str) -> bool:
    """将微信 openid 绑定到已有账号（openid 未被占用时）"""
    existing = get_user_by_openid(openid)
    if existing and existing["id"] != user_id:
        return False
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET wx_openid = %s WHERE id = %s", (openid, user_id))
        conn.commit()
        conn.close()
    return True


# ============================================================
# 10. 卡产品相关
# ============================================================
def list_cards(status: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute("SELECT * FROM cards WHERE status = %s ORDER BY sort ASC, id ASC", (status,))
        else:
            cur.execute("SELECT * FROM cards ORDER BY sort ASC, id ASC")
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def get_card(card_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM cards WHERE id = %s", (card_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def create_card(name: str, price: int, description: str = "", sort: int = 0) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO cards (name, price, description, sort) VALUES (%s, %s, %s, %s)",
                    (name, price, description, sort)
                )
                card_id = cur.lastrowid
            conn.commit()
            conn.close()
            return get_card(card_id)
        except pymysql.err.IntegrityError:
            conn.close()
            return None  # 同名卡已存在


def update_card(card_id: int, **kwargs) -> Optional[Dict]:
    allowed_fields = {"name", "price", "description", "status", "sort"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}
    if not updates:
        return get_card(card_id)
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                set_clause = ", ".join([f"{k} = %s" for k in updates])
                values = list(updates.values()) + [card_id]
                cur.execute(f"UPDATE cards SET {set_clause} WHERE id = %s", values)
            conn.commit()
            conn.close()
            return get_card(card_id)
        except pymysql.err.IntegrityError:
            conn.close()
            return None  # 改名撞车


def delete_card(card_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cards WHERE id = %s", (card_id,))
            deleted = cur.rowcount > 0
        conn.commit()
        conn.close()
        return deleted


# ============================================================
# 11. 办卡订单与持卡相关
# ============================================================
def create_card_order(order_no: str, user_id: Optional[int], card_id: int,
                      card_name: str, amount: int, openid: str = "") -> Optional[Dict]:
    """创建订单（order_no 幂等：已存在则直接返回原订单）"""
    existing = get_card_order_by_no(order_no)
    if existing:
        return existing
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO card_orders (order_no, user_id, card_id, card_name, amount, openid)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (order_no, user_id, card_id, card_name, amount, openid)
                )
            conn.commit()
            conn.close()
            return get_card_order_by_no(order_no)
        except pymysql.err.IntegrityError:
            conn.close()
            return get_card_order_by_no(order_no)


def get_card_order_by_no(order_no: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM card_orders WHERE order_no = %s", (order_no,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def update_card_order_status(order_no: str, status: str, transaction_id: str = "") -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            if status == "paid":
                cur.execute(
                    """UPDATE card_orders SET status = %s, transaction_id = %s, paid_at = NOW()
                       WHERE order_no = %s""",
                    (status, transaction_id, order_no)
                )
            else:
                cur.execute(
                    "UPDATE card_orders SET status = %s, transaction_id = %s WHERE order_no = %s",
                    (status, transaction_id, order_no)
                )
        conn.commit()
        conn.close()
    return get_card_order_by_no(order_no)


def list_user_orders(user_id: int, limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM card_orders WHERE user_id = %s ORDER BY id DESC LIMIT %s",
            (user_id, limit)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def add_user_card(user_id: Optional[int], card_id: int, card_name: str, order_no: str = "",
                  coins: Optional[int] = None, kind: str = "member") -> int:
    """发卡/发币：支付成功后写入用户持卡记录（coins = 面值，供商家调整/余额统计）。
    kind: member=会员充值卡（可生效/退卡）；small=小额充币卡（直接进余币，无使用/退卡）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_cards (user_id, card_id, card_name, order_no, coins, kind)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (user_id, card_id, card_name, order_no, coins, kind)
            )
            uc_id = cur.lastrowid
        conn.commit()
        conn.close()
    return uc_id


def list_user_cards(user_id: int) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM user_cards WHERE user_id = %s AND status = 'active' ORDER BY id DESC",
            (user_id,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 11+. 办卡账单管理（商家端：全量订单/统计/退卡/调币）
# ============================================================
def list_all_orders(status: str = "", keyword: str = "",
                    page: int = 1, page_size: int = 20) -> List[Dict]:
    """全量订单账单（联用户昵称与持卡记录，供管理端展示/退卡/调币）"""
    where, params = [], []
    if status:
        where.append("o.status = %s")
        params.append(status)
    if keyword:
        like = f"%{keyword}%"
        where.append("(o.order_no LIKE %s OR o.card_name LIKE %s OR u.nickname LIKE %s OR u.username LIKE %s)")
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT o.*, u.username, u.nickname,
                       uc.id AS uc_id, uc.status AS uc_status, uc.coins AS uc_coins
                FROM card_orders o
                LEFT JOIN users u ON u.id = o.user_id
                LEFT JOIN user_cards uc ON uc.order_no = o.order_no
                {where_sql}
                ORDER BY o.id DESC LIMIT %s OFFSET %s""",
            params + [page_size, offset]
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_all_orders(status: str = "", keyword: str = "") -> int:
    where, params = [], []
    if status:
        where.append("o.status = %s")
        params.append(status)
    if keyword:
        like = f"%{keyword}%"
        where.append("(o.order_no LIKE %s OR o.card_name LIKE %s OR u.nickname LIKE %s OR u.username LIKE %s)")
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT COUNT(*) AS cnt FROM card_orders o
                LEFT JOIN users u ON u.id = o.user_id {where_sql}""",
            params
        )
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_order_stats() -> Dict:
    """账单统计：累计收款/退款、订单数、今日收款"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT
                COALESCE(SUM(CASE WHEN status = 'paid' THEN amount END), 0) AS paid_amount,
                COALESCE(SUM(CASE WHEN status = 'refunded' THEN amount END), 0) AS refunded_amount,
                COALESCE(SUM(CASE WHEN status = 'paid' AND DATE(paid_at) = CURDATE() THEN amount END), 0) AS today_paid_amount,
                SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END) AS refunded_count,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count
              FROM card_orders"""
        )
        row = cur.fetchone()
    conn.close()
    row = dict(row) if row else {}
    return {
        "paid_amount": int(row.get("paid_amount") or 0),
        "refunded_amount": int(row.get("refunded_amount") or 0),
        "today_paid_amount": int(row.get("today_paid_amount") or 0),
        "paid_count": int(row.get("paid_count") or 0),
        "refunded_count": int(row.get("refunded_count") or 0),
        "pending_count": int(row.get("pending_count") or 0),
    }


def revenue_by_card(days: int = 1) -> List[Dict]:
    """近 N 天各卡产品销售汇总（仅 status='paid'）：卡名 / 销售额(分) / 笔数

    days<=1 表示「今天」（按自然日）；days>1 表示最近 N 天滚动窗口（含今天）。
    供经营小助手「营收汇总」图表使用；退款单不计入（status 已变 refunded）。
    """
    days = int(days or 1)
    sql = ("SELECT card_name, COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS cnt "
           "FROM card_orders WHERE status = 'paid'")
    params: list = []
    if days <= 1:
        sql += " AND DATE(paid_at) = CURDATE()"
    else:
        sql += " AND paid_at >= %s"
        params.append(_since_str(days - 1)[:10] + " 00:00:00")
    sql += " GROUP BY card_name ORDER BY amount DESC"
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def daily_revenue_trend(days: int = 7) -> List[Dict]:
    """按日收款趋势（近 N 天，仅已支付，仅返回有数据的日期，接口层补零对齐）"""
    since = _since_str(int(days or 7) - 1)[:10] + " 00:00:00"
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DATE(paid_at) AS d, COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS cnt
               FROM card_orders WHERE status = 'paid' AND paid_at >= %s
               GROUP BY DATE(paid_at) ORDER BY d""",
            (since,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def set_user_card_coins(user_card_id: int, coins: int) -> bool:
    """商家调整某张持卡的余币面值（线下补偿/纠错）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_cards SET coins = %s WHERE id = %s AND status = 'active'",
                (coins, user_card_id)
            )
            updated = cur.rowcount > 0
        conn.commit()
        conn.close()
    return updated


def refund_order(order_no: str) -> Dict:
    """退卡退款：仅已支付订单可退；订单标记 refunded，对应持卡记录同步失效
    （用户余币余额随持卡失效自动减少，记录保留可追溯）"""
    order = get_card_order_by_no(order_no)
    if not order:
        return {"ok": False, "msg": "订单不存在"}
    if order["status"] == "refunded":
        return {"ok": False, "msg": "该订单已退卡，请勿重复操作"}
    if order["status"] != "paid":
        return {"ok": False, "msg": "仅已支付订单可退卡"}
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE card_orders SET status = 'refunded' WHERE order_no = %s", (order_no,))
            cur.execute("UPDATE user_cards SET status = 'refunded' WHERE order_no = %s", (order_no,))
        conn.commit()
        conn.close()
    return {"ok": True, "msg": "退卡成功，用户余币已同步扣减"}


# ============================================================
# 12. 问答日志相关（管理端问答记录页数据源）
# ============================================================
def add_question_log(question: str, search_query: str = "", answer: str = "",
                     answer_full_len: int = 0, references: Optional[List] = None,
                     elapsed_seconds: float = 0, session_id: str = "", source: str = "web",
                     answer_type: str = None) -> int:
    refs_json = json.dumps(references or [], ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO question_logs
                   (question, search_query, answer, answer_full_len, references_json,
                    elapsed_seconds, session_id, source, answer_type)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (question, search_query, answer, answer_full_len, refs_json,
                 elapsed_seconds, session_id, source, answer_type)
            )
            log_id = cur.lastrowid
        conn.commit()
        conn.close()
    return log_id


def list_question_logs(page: int = 1, page_size: int = 20, keyword: str = "") -> List[Dict]:
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    with conn.cursor() as cur:
        if keyword:
            like = f"%{keyword}%"
            cur.execute(
                """SELECT * FROM question_logs
                   WHERE question LIKE %s OR answer LIKE %s
                   ORDER BY id DESC LIMIT %s OFFSET %s""",
                (like, like, page_size, offset)
            )
        else:
            cur.execute(
                "SELECT * FROM question_logs ORDER BY id DESC LIMIT %s OFFSET %s",
                (page_size, offset)
            )
        rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["references"] = json.loads(d.pop("references_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            d["references"] = []
        result.append(d)
    return result


def count_question_logs(keyword: str = "") -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        if keyword:
            like = f"%{keyword}%"
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM question_logs WHERE question LIKE %s OR answer LIKE %s",
                (like, like)
            )
        else:
            cur.execute("SELECT COUNT(*) AS cnt FROM question_logs")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def list_fallback_questions(days: int = 30, limit: int = 200,
                            markers: Optional[List[str]] = None) -> List[Dict]:
    """列出「模型没有检索到知识库 → 给建议」的问答记录（知识库盲区统计数据源）。

    ⚠️ 判据是**回答类型**，不是 `references_json` 为空：
    缓存命中 / 地图 / 业务类回答的 references 同样是空的，但它们都**已经答上来了**，
    用 references 判空会把高频正常问题全算成盲区。
      * 新记录（写入侧已落 `answer_type`）→ 直接按类型过滤；
      * 历史记录（answer_type 为 NULL）→ 用答案开头的兜底话术识别，无需回填。
    压测脚本产生的 source='probe' / 'stress' 记录一并排除。
    按时间倒序返回原始记录；归一化去重/剔除寒暄与业务类问题由 services/kb_agent.py 负责。
    """
    if markers is None:
        # 话术特征串的唯一真源在 VerificationAgent（它知道自己的兜底文案）
        try:
            from agents.verification_agent import verification_agent as _va
            markers = [m for m, _t in _va.FALLBACK_MARKERS]
        except Exception:
            markers = ["知识库暂无相关文档", "知识库没有直接回答该问题的条目"]

    like_clause = " OR ".join(["answer LIKE %s"] * len(markers))
    sql = f"""SELECT id, question, answer, source, created_at, answer_type
              FROM question_logs
              WHERE question IS NOT NULL AND TRIM(question) <> ''
                AND COALESCE(source, '') NOT IN ('probe', 'stress')
                AND (
                      answer_type IN ('fallback_none', 'fallback_partial')
                   OR (
                        (answer_type IS NULL OR answer_type = '')
                        AND ({like_clause})
                      )
                    )"""
    params: list = [f"%{m}%" for m in markers]
    if days:
        sql += " AND created_at >= %s"
        params.append(_since_str(days))
    sql += " ORDER BY id DESC LIMIT %s"
    params.append(int(limit))

    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 12+. 问答分析统计（商家端图谱：高频问题/意图分布/趋势/满意度）
# ============================================================
def _since_str(days: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def top_questions(limit: int = 10, days: int = 30) -> List[Dict]:
    """高频问题 TOP N（TRIM 归一化分组，近 N 天，0=不限）"""
    conn = _get_conn()
    sql = """SELECT TRIM(question) AS question, COUNT(*) AS cnt
             FROM question_logs
             WHERE question IS NOT NULL AND TRIM(question) != ''"""
    params: list = []
    if days:
        sql += " AND created_at >= %s"
        params.append(_since_str(days))
    sql += " GROUP BY TRIM(question) ORDER BY cnt DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def daily_question_trend(days: int = 7) -> List[Dict]:
    """按日问答量趋势（近 N 天，仅返回有数据的日期，接口层补零对齐）"""
    since = _since_str(days - 1)[:10] + " 00:00:00"
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DATE(created_at) AS d, COUNT(*) AS cnt
               FROM question_logs WHERE created_at >= %s
               GROUP BY DATE(created_at) ORDER BY d""",
            (since,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_questions_today() -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM question_logs WHERE DATE(created_at) = CURDATE()"
        )
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 仪表盘专用补充查询（意图实时聚合 / 今日新增用户 / 消费单状态计数）
# ============================================================
def list_question_texts(days: int = 7, limit: int = 20000) -> List[str]:
    """取近 N 天的原始问题文本（供意图分布"实时聚合"用）。

    为什么不直接 `GROUP BY intent`：`question_logs.intent` 列从来没有任何写入点
    （历史行全是默认值"其他"），且意图是纯关键词规则识别的 —— 在读取侧用**当前规则**
    现算，既不依赖回填，规则升级后历史分布也会自动跟着变，不会失真。
    """
    sql = ("SELECT question FROM question_logs "
           "WHERE question IS NOT NULL AND TRIM(question) <> ''")
    params: list = []
    if days:
        sql += " AND created_at >= %s"
        params.append(_since_str(days))
    sql += " LIMIT %s"
    params.append(int(limit))
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return [(r["question"] or "").strip() for r in rows]


def count_new_users_today() -> int:
    """今日新增注册用户数（仪表盘「今日运营」卡）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE DATE(created_at) = CURDATE()")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def count_consumptions(status: str = "") -> int:
    """消费单按状态计数（status 空=全部；'unused' = 待核销，仪表盘待办用）"""
    sql = "SELECT COUNT(*) AS cnt FROM consumption_orders WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def feedback_rating_distribution() -> Dict:
    """满意度 1-5 星分布与平均分（仅统计数字星级，兼容历史 like/dislike 数据）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT rating, COUNT(*) AS cnt FROM feedback
               WHERE rating IN ('1', '2', '3', '4', '5')
               GROUP BY rating"""
        )
        rows = cur.fetchall()
    conn.close()
    dist = {str(i): 0 for i in range(1, 6)}
    total = 0
    weighted = 0
    for r in rows:
        rating = str(r["rating"])
        cnt = int(r["cnt"])
        dist[rating] = cnt
        total += cnt
        weighted += int(rating) * cnt
    return {
        "distribution": dist,
        "total": total,
        "average": round(weighted / total, 2) if total else 0,
    }


# ============================================================
# 13. 每日签到相关（我的页签到送币，奖励计入余币余额）
# ============================================================
def _list_checkin_dates(user_id: int) -> List[str]:
    """签到日期列表（ISO 字符串，倒序），供连续天数/状态计算"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DATE_FORMAT(checkin_date, '%%Y-%%m-%%d') AS d
               FROM user_checkins WHERE user_id = %s ORDER BY checkin_date DESC""",
            (user_id,)
        )
        rows = cur.fetchall()
    conn.close()
    return [r["d"] for r in rows]


def sum_checkin_coins(user_id: int) -> int:
    """签到累计奖励币（余币余额 = 持卡面值合计 + 签到奖励合计）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(reward_coins), 0) AS s FROM user_checkins WHERE user_id = %s",
            (user_id,)
        )
        row = cur.fetchone()
    conn.close()
    return int(row["s"] or 0)


def do_checkin(user_id: int, reward_coins: int = 1) -> Dict:
    """每日签到（当日唯一键幂等，重复签不重复发币），返回最新签到状态"""
    from datetime import date
    with _lock:
        conn = _get_conn()
        signed_now = False
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO user_checkins (user_id, checkin_date, reward_coins) VALUES (%s, %s, %s)",
                    (user_id, date.today(), reward_coins)
                )
                conn.commit()
                signed_now = True
            except Exception:
                conn.rollback()  # 当日已签（uk_user_date 冲突），幂等返回
        conn.close()
    status = get_checkin_status(user_id)
    status["signed_now"] = signed_now
    return status


def get_checkin_status(user_id: int) -> Dict:
    """签到状态：今日是否已签 / 连续天数 / 签到累计奖励币 / 累计签到次数"""
    from datetime import date, timedelta
    today = date.today()
    dates = _list_checkin_dates(user_id)
    dset = set(dates)
    signed_today = today.isoformat() in dset
    # 连续天数：从今天（未签则从昨天）向前连续计数
    streak = 0
    cursor_day = today if signed_today else today - timedelta(days=1)
    while cursor_day.isoformat() in dset:
        streak += 1
        cursor_day -= timedelta(days=1)
    return {
        "signed_today": signed_today,
        "streak_days": streak,
        "total_coins": sum_checkin_coins(user_id),
        "checkin_count": len(dates),
    }


# ============================================================
# 14. 失物招领相关（lost_found_items：游客报失 + 商家登记统一管理）
# ============================================================
def create_lost_item(name: str, photo_url: str = "", source: str = "user",
                     reporter: str = "", ai: Optional[Dict] = None,
                     status: str = "lost") -> Optional[Dict]:
    """新增一条失物记录（游客报失 source=user / 商家登记 source=merchant）

    ai: 可选，services.vision_service.recognize_lost_photo() 的识别结果。
        传 None 或 {}（未识别/识别失败）时落空字符串 —— 识别是增强项，不阻断报失。

    status: 失物状态。游客报失传 pending（待管理员审核，通过前不对游客展示）；
            商家登记用默认的 lost（商家即管理员，可信内容直接上架可认领）。
    """
    ai = ai or {}
    feats = ai.get("features") or []
    if isinstance(feats, list):
        feats = "；".join(str(f)[:60] for f in feats[:5])
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO lost_found_items
                       (name, photo_url, source, reporter, status,
                        ai_category, ai_color, ai_brand, ai_features, ai_text)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (name.strip(), photo_url, source, reporter, status,
                 str(ai.get("category") or "")[:32],
                 str(ai.get("color") or "")[:32],
                 str(ai.get("brand") or "")[:64],
                 str(feats)[:512],
                 str(ai.get("visible_text") or "")[:255])
            )
            item_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_lost_item(item_id)


def get_lost_item(item_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM lost_found_items WHERE id = %s", (item_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_lost_items(status: str = "", source: str = "", page: int = 1,
                    page_size: int = 50, keyword: str = "") -> List[Dict]:
    """失物列表：支持状态/来源筛选 + 名称关键词 + 分页（新->旧）

    keyword 同时匹配 AI 识别字段（类别/主色/品牌/特征/可见文字），
    这样搜「iPhone」「黑色」「小熊挂件」都能命中 —— 认领匹配靠的就是这些特征。
    """
    offset = max(0, (page - 1) * page_size)
    sql = "SELECT * FROM lost_found_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if source:
        sql += " AND source = %s"
        params.append(source)
    if keyword:
        kw = f"%{keyword}%"
        sql += (" AND (name LIKE %s OR ai_category LIKE %s OR ai_color LIKE %s"
                " OR ai_brand LIKE %s OR ai_features LIKE %s OR ai_text LIKE %s)")
        params.extend([kw] * 6)
    sql += " ORDER BY id DESC LIMIT %s OFFSET %s"
    params.extend([page_size, offset])
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_lost_items(status: str = "", source: str = "", keyword: str = "") -> int:
    sql = "SELECT COUNT(*) AS cnt FROM lost_found_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = %s"
        params.append(status)
    if source:
        sql += " AND source = %s"
        params.append(source)
    if keyword:
        kw = f"%{keyword}%"
        sql += (" AND (name LIKE %s OR ai_category LIKE %s OR ai_color LIKE %s"
                " OR ai_brand LIKE %s OR ai_features LIKE %s OR ai_text LIKE %s)")
        params.extend([kw] * 6)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def update_lost_item_status(item_id: int, status: str) -> bool:
    """更新失物状态：lost 待认领 -> claimed 已认领（或反向撤销）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lost_found_items SET status = %s WHERE id = %s",
                (status, item_id)
            )
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


def delete_lost_item(item_id: int) -> bool:
    """删除失物记录（同时清理照片文件由 API 层负责）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lost_found_items WHERE id = %s", (item_id,))
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok



# ============================================================
# 15. 园区公告 / 今日热点（仅商家 PC 端发布，小程序首页展示）
# ============================================================
def create_park_notice(kind: str, title: str, content: str = "",
                       sort: int = 0, status: str = "on") -> Optional[Dict]:
    """新增公告/热点（kind: announcement 园区公告 / hot 今日热点）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO park_notices (kind, title, content, status, sort)
                   VALUES (%s, %s, %s, %s, %s)""",
                (kind, title.strip(), (content or "").strip(), status or "on", sort)
            )
            notice_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_park_notice(notice_id)


def get_park_notice(notice_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM park_notices WHERE id = %s", (notice_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_park_notices(kind: str = "", status: str = "on",
                      page: int = 1, page_size: int = 100) -> List[Dict]:
    """公告/热点列表：kind 空为全部；status 空=全部（管理端），'on'=仅上架（小程序）"""
    offset = max(0, (page - 1) * page_size)
    sql = "SELECT * FROM park_notices WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = %s"
        params.append(kind)
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY sort ASC, id DESC LIMIT %s OFFSET %s"
    params.extend([page_size, offset])
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_park_notices(kind: str = "", status: str = "") -> int:
    sql = "SELECT COUNT(*) AS cnt FROM park_notices WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = %s"
        params.append(kind)
    if status:
        sql += " AND status = %s"
        params.append(status)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def update_park_notice(notice_id: int, **fields) -> bool:
    """更新公告/热点：仅允许 title / content / status / sort / kind 字段"""
    allowed = {k: v for k, v in fields.items()
               if k in {"title", "content", "status", "sort", "kind"} and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = %s" for k in allowed)
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(f"UPDATE park_notices SET {sets} WHERE id = %s",
                        (*allowed.values(), notice_id))
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


def delete_park_notice(notice_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM park_notices WHERE id = %s", (notice_id,))
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


# ============================================================
# 16. 游客意见反馈（小程序提交，商家端查看）
# ============================================================
def add_user_feedback(content: str, nickname: str = "",
                      user_id: Optional[int] = None) -> Optional[Dict]:
    """新增游客反馈，返回落库后的记录"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_feedback (user_id, nickname, content) VALUES (%s, %s, %s)",
                (user_id, (nickname or "").strip(), content.strip())
            )
            fid = cur.lastrowid
        conn.commit()
        conn.close()

    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM user_feedback WHERE id = %s", (fid,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def list_user_feedback(page: int = 1, page_size: int = 50) -> List[Dict]:
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM user_feedback ORDER BY id DESC LIMIT %s OFFSET %s",
            (page_size, offset)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_user_feedback() -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM user_feedback")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 手机号登录 / 绑定
# ============================================================
def get_user_by_phone(phone: str) -> Optional[Dict]:
    """按手机号取账号。
    必须带 ORDER BY id ASC：历史数据万一还残留同号两行时，fetchone() 的返回值
    不稳定会让调用方的"冲突检测"静默失效（曾因此造出重复手机号），固定取最早的账号。
    """
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM users WHERE phone = %s AND phone != '' ORDER BY id ASC",
            (phone,)
        )
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def create_phone_user(phone: str) -> Optional[Dict]:
    """手机号验证码登录的新用户：username 由手机号生成，随机串哈希占位密码。
    手机号换绑后旧号会重新注册，用户名冲突时自动加随机后缀重试"""
    import secrets
    from core.auth import hash_password
    for attempt in range(3):
        username = f"ph_{phone}" if attempt == 0 else f"ph_{phone}_{secrets.token_hex(3)}"
        with _lock:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO users (username, password_hash, nickname, phone)
                           VALUES (%s, %s, %s, %s)""",
                        (username, hash_password(secrets.token_urlsafe(24)),
                         f"用户{phone[-4:]}", phone)
                    )
                    user_id = cur.lastrowid
            except pymysql.err.IntegrityError:
                conn.close()
                # 号码唯一约束命中（并发重复创建）→ 回读该号码已有账号，视为登录成功；
                # 回读不到才说明是用户名冲突，换个后缀重试。
                existing = get_user_by_phone(phone)
                if existing:
                    return existing
                continue
            conn.commit()
            conn.close()
        return get_user_by_id(user_id)
    return None


# ============================================================
# 手机号 + 密码登录 / 历史账号 / 免密令牌（小程序登录页 v2）
# ------------------------------------------------------------
# 设计要点：
#   1. 密码永不落明文，统一 bcrypt 存 users.password_hash，pwd_set=1 表示用户自设过；
#   2. 「下次点一下头像就登录」不靠存密码，靠 mp_remember_tokens 的随机令牌
#      （库存 sha256，30 天有效，可吊销）；
#   3. mp_login_accounts 记录本机登录过的账号，用于登录页/我的页展示历史账号卡片。
# ============================================================
def create_phone_user_with_password(phone: str, password: str,
                                    nickname: str = "") -> Optional[Dict]:
    """手机号 + 密码注册新用户（bcrypt 存密码，pwd_set=1）
    手机号换绑后旧号会重新注册，用户名冲突时自动加随机后缀重试"""
    import secrets
    from core.auth import hash_password
    for attempt in range(3):
        username = f"ph_{phone}" if attempt == 0 else f"ph_{phone}_{secrets.token_hex(3)}"
        with _lock:
            conn = _get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO users (username, password_hash, nickname, phone, pwd_set)
                           VALUES (%s, %s, %s, %s, 1)""",
                        (username, hash_password(password),
                         nickname or f"用户{phone[-4:]}", phone)
                    )
                    user_id = cur.lastrowid
            except pymysql.err.IntegrityError:
                conn.close()
                # 号码唯一约束命中（并发重复注册）→ 回读该号码已有账号，视为登录成功
                existing = get_user_by_phone(phone)
                if existing:
                    return existing
                continue
            conn.commit()
            conn.close()
        return get_user_by_id(user_id)
    return None


def set_user_password(user_id: int, password: str) -> bool:
    """设置/重设登录密码：写 bcrypt 哈希并置 pwd_set=1；
    同时吊销该用户所有免密令牌（改密后旧设备免密立即失效）"""
    from core.auth import hash_password
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s, pwd_set = 1 WHERE id = %s",
                (hash_password(password), user_id)
            )
        conn.commit()
        conn.close()
    revoke_user_remember_tokens(user_id)
    return True


def upsert_mp_login_account(device_id: str, user: Dict) -> bool:
    """记录/更新本机历史账号（同设备同用户只留一条，累计登录次数并刷新快照）"""
    if not device_id or not user:
        return False
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO mp_login_accounts
                       (device_id, user_id, phone, username, nickname, avatar,
                        login_count, last_login_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 1, NOW())
                   ON DUPLICATE KEY UPDATE
                       phone = VALUES(phone),
                       username = VALUES(username),
                       nickname = VALUES(nickname),
                       avatar = VALUES(avatar),
                       login_count = login_count + 1,
                       last_login_at = NOW()""",
                (device_id, user["id"], user.get("phone") or "", user.get("username") or "",
                 user.get("nickname") or "", user.get("avatar") or "")
            )
        conn.commit()
        conn.close()
    return True


def list_mp_login_accounts(device_id: str) -> List[Dict]:
    """本机历史登录账号（按最近登录倒序；只回展示所需字段，不含任何密码信息）"""
    if not device_id:
        return []
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, device_id, user_id, phone, username, nickname, avatar,
                      login_count, last_login_at
               FROM mp_login_accounts WHERE device_id = %s
               ORDER BY last_login_at DESC, id DESC""",
            (device_id,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def delete_mp_login_account(device_id: str, user_id: int) -> bool:
    """移除本机某条历史账号（同时吊销该设备上该用户的免密令牌）"""
    if not device_id:
        return False
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mp_login_accounts WHERE device_id = %s AND user_id = %s",
                (device_id, user_id)
            )
            cur.execute(
                "UPDATE mp_remember_tokens SET revoked = 1 WHERE device_id = %s AND user_id = %s",
                (device_id, user_id)
            )
        conn.commit()
        conn.close()
    return True


def create_remember_token(user_id: int, device_id: str, token_hash: str,
                          expires_at) -> bool:
    """写入免密令牌（只存 sha256 哈希）"""
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO mp_remember_tokens
                           (token_hash, user_id, device_id, expires_at)
                       VALUES (%s, %s, %s, %s)""",
                    (token_hash, user_id, device_id or "", expires_at)
                )
            conn.commit()
        except pymysql.err.IntegrityError:
            conn.close()
            return False
        conn.close()
    return True


def get_remember_token(token_hash: str) -> Optional[Dict]:
    """按令牌哈希取记录（未吊销的）；有效期由调用方比对 expires_at"""
    if not token_hash:
        return None
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, user_id, device_id, expires_at, revoked
               FROM mp_remember_tokens WHERE token_hash = %s AND revoked = 0""",
            (token_hash,)
        )
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def revoke_remember_token(token_hash: str) -> bool:
    """吊销单个免密令牌（登出）"""
    if not token_hash:
        return False
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mp_remember_tokens SET revoked = 1 WHERE token_hash = %s",
                (token_hash,)
            )
        conn.commit()
        conn.close()
    return True


def revoke_user_remember_tokens(user_id: int) -> bool:
    """吊销某用户全部免密令牌（改密/重置密码后调用）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE mp_remember_tokens SET revoked = 1 WHERE user_id = %s",
                (user_id,)
            )
        conn.commit()
        conn.close()
    return True


# ============================================================
# 账号合并（手机号归属冲突解决）
# ------------------------------------------------------------
# 铁律：**一个手机号 = 一个账号**，且"手机号账号 P"是主账号。
#   · 微信登录得到的空壳账号 W（没手机号）与手机号账号 P 撞上时：
#     把 W 的业务数据并到 P、openid 改绑到 P、W 的手机号/openid 置空，登录态落 P。
#   · 两边都有手机号（换绑场景）时，才反过来把被绑定号码的账号并入当前账号。
# 调用统一走 merge_accounts(from=辅助账号, to=主账号)。
# ============================================================
_MERGE_TABLES = (
    "chat_sessions", "chat_messages", "feedback",
    "card_orders", "user_cards", "consumption_orders",
    "user_favorites", "user_checkins", "user_feedback",
)


def merge_user_assets(from_user_id: int, to_user_id: int) -> Dict[str, int]:
    """把 from 账号的业务数据迁移到 to 账号，返回各表迁移行数。

    用 UPDATE IGNORE：遇到唯一键冲突（如两账号同一天都签到过）跳过该行而不是整体失败。
    users.bonus_spent（签到奖励已消费额）累加合并；from 的手机号清空，避免继续占用。
    """
    if not from_user_id or not to_user_id or from_user_id == to_user_id:
        return {}
    moved: Dict[str, int] = {}
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT bonus_spent FROM users WHERE id = %s", (from_user_id,))
                row_from = cur.fetchone()
                cur.execute("SELECT bonus_spent FROM users WHERE id = %s", (to_user_id,))
                row_to = cur.fetchone()
                bonus = int((row_from or {}).get("bonus_spent") or 0)

                for table in _MERGE_TABLES:
                    cur.execute(
                        f"UPDATE IGNORE `{table}` SET user_id = %s WHERE user_id = %s",
                        (to_user_id, from_user_id)
                    )
                    moved[table] = cur.rowcount

                if bonus:
                    cur.execute(
                        "UPDATE users SET bonus_spent = COALESCE(bonus_spent, 0) + %s WHERE id = %s",
                        (bonus, to_user_id)
                    )
                    cur.execute("UPDATE users SET bonus_spent = 0 WHERE id = %s", (from_user_id,))
                    moved["bonus_spent"] = bonus

                # 手机号从旧账号摘下（用户表 phone 无唯一索引，但必须只留一份归属）
                cur.execute("UPDATE users SET phone = '' WHERE id = %s", (from_user_id,))
                cur.execute(
                    "UPDATE mp_remember_tokens SET revoked = 1 WHERE user_id = %s",
                    (from_user_id,)
                )
                _ = row_to
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return moved


def merge_accounts(from_user_id: int, to_user_id: int) -> Dict:
    """把 from（辅助账号）整体并入 to（主账号），返回合并明细。

    比 merge_user_assets 多做的事（登录凭据的归属，缺了就会造出重复手机号）：
      * from 的 wx_openid **转移**到 to（先置空 from 再写 to，避开 uk_wx_openid）；
      * from 在本机的历史账号记录（mp_login_accounts）重指到 to，
        同设备已有 to 的记录时删掉 from 那条（避免撞 uk_device_user）；
      * from 的手机号由 merge_user_assets 置空。

    方向只有一种：**空壳（无手机号）的一方并入有手机号的一方**。
    """
    if not from_user_id or not to_user_id or from_user_id == to_user_id:
        return {"moved": {}, "openid_moved": False, "accounts_repointed": 0}

    moved = merge_user_assets(from_user_id, to_user_id)

    openid_moved = False
    repointed = 0
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT wx_openid FROM users WHERE id = %s", (from_user_id,))
                openid = ((cur.fetchone() or {}).get("wx_openid") or "").strip()
                if openid:
                    cur.execute("UPDATE users SET wx_openid = NULL WHERE id = %s", (from_user_id,))
                    cur.execute("UPDATE users SET wx_openid = %s WHERE id = %s", (openid, to_user_id))
                    openid_moved = True

                cur.execute(
                    "SELECT device_id FROM mp_login_accounts WHERE user_id = %s", (from_user_id,))
                for row in cur.fetchall():
                    device = row["device_id"]
                    cur.execute(
                        "SELECT id FROM mp_login_accounts WHERE device_id = %s AND user_id = %s",
                        (device, to_user_id))
                    if cur.fetchone():
                        cur.execute(
                            "DELETE FROM mp_login_accounts WHERE device_id = %s AND user_id = %s",
                            (device, from_user_id))
                    else:
                        cur.execute(
                            "UPDATE mp_login_accounts SET user_id = %s "
                            "WHERE device_id = %s AND user_id = %s",
                            (to_user_id, device, from_user_id))
                        repointed += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return {"moved": moved, "openid_moved": openid_moved, "accounts_repointed": repointed}


# ============================================================
# 持卡"生效中"标记（使用/切换；充值页会员码按生效卡出码）
# ============================================================
def get_user_card(card_id: int, user_id: int) -> Optional[Dict]:
    """取用户名下某张生效中的卡"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM user_cards WHERE id = %s AND user_id = %s AND status = 'active'",
            (card_id, user_id)
        )
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def set_user_card_in_use(user_id: int, card_id: int) -> bool:
    """把某张会员卡标记为生效中（先清掉该用户所有生效标记，保证唯一）。
    小额充币卡（kind='small'）不作为会员码生效卡。"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_cards SET in_use = 0 WHERE user_id = %s AND status = 'active'",
                (user_id,)
            )
            cur.execute(
                """UPDATE user_cards SET in_use = 1
                   WHERE id = %s AND user_id = %s AND status = 'active' AND kind = 'member'""",
                (card_id, user_id)
            )
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


# ============================================================
# 游乐消费：项目价目 / 多选项目合并扣币 / 核销码 / 消费记录
# ============================================================
def list_park_projects(status: str = "on") -> List[Dict]:
    """游乐项目价目表（消费清单数据源；status='on' 仅上架，''=全部）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute(
                "SELECT * FROM park_projects WHERE status = %s ORDER BY sort ASC, id ASC",
                (status,)
            )
        else:
            cur.execute("SELECT * FROM park_projects ORDER BY sort ASC, id ASC")
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def get_park_project(project_id: int) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM park_projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def sum_bonus_used(user_id: int) -> int:
    """该用户历史消费中动用签到奖励币的总额"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(bonus_coins), 0) AS used FROM consumption_orders
               WHERE user_id = %s AND status != 'refunded'""",
            (user_id,)
        )
        row = cur.fetchone()
    conn.close()
    return int(row["used"]) if row else 0


def bonus_available(user_id: int) -> int:
    """签到奖励币可用余额 = 累计奖励 - 已消费动用"""
    from database.db import sum_checkin_coins
    return max(0, sum_checkin_coins(user_id) - sum_bonus_used(user_id))


def consume_coins(user_id: int, total_coins: int) -> Dict:
    """消费扣币事务（行锁防并发）。
    扣币优先级：生效中的卡 → 其余生效卡按购买时间先到先扣（可跨卡拆分）→ 签到奖励剩余。
    返回 {"ok": True, "card_deductions": [...], "bonus_used": int} 或 {"ok": False, "msg": ...}"""
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, coins, in_use FROM user_cards
                       WHERE user_id = %s AND status = 'active'
                       ORDER BY in_use DESC, created_at ASC, id ASC FOR UPDATE""",
                    (user_id,)
                )
                cards = cur.fetchall()
                card_total = sum(int(c["coins"] or 0) for c in cards)
                bonus_avail = bonus_available(user_id)
                if card_total + bonus_avail < total_coins:
                    return {"ok": False, "msg": f"余币不足（可用 {card_total + bonus_avail} 币，需 {total_coins} 币），请先充值"}

                remaining = total_coins
                card_deductions = []
                # 1) 生效中的卡优先扣
                for c in cards:
                    if remaining <= 0:
                        break
                    if not c.get("in_use"):
                        continue
                    take = min(int(c["coins"] or 0), remaining)
                    if take > 0:
                        cur.execute("UPDATE user_cards SET coins = coins - %s WHERE id = %s", (take, c["id"]))
                        card_deductions.append({"card_id": c["id"], "coins": take})
                        remaining -= take
                # 2) 其余卡按购买时间先到先扣
                for c in cards:
                    if remaining <= 0:
                        break
                    if c.get("in_use"):
                        continue
                    take = min(int(c["coins"] or 0), remaining)
                    if take > 0:
                        cur.execute("UPDATE user_cards SET coins = coins - %s WHERE id = %s", (take, c["id"]))
                        card_deductions.append({"card_id": c["id"], "coins": take})
                        remaining -= take
                # 2.5) 扣到 0 币的持卡记录自动删除（用户端不再展示，订单/消费流水可追溯）
                cur.execute(
                    "DELETE FROM user_cards WHERE user_id = %s AND status = 'active' AND coins IS NOT NULL AND coins <= 0",
                    (user_id,)
                )
                # 3) 签到奖励币兜底
                bonus_used = remaining
                if bonus_used > 0:
                    card_deductions.append({"card_id": None, "coins": bonus_used})
            conn.commit()
        except Exception as e:
            conn.close()
            raise e
        conn.close()
    return {"ok": True, "card_deductions": card_deductions, "bonus_used": bonus_used}


def create_consumption_order(user_id: int, order_no: str, verify_code: str,
                             items_json: str, card_coins: int, bonus_coins: int,
                             total_coins: int, items: Optional[List[Dict]] = None) -> Optional[Dict]:
    """落消费订单（扣币事务成功后调用）。

    items 非空时，把该单展开出的券**在同一事务里**一并写入 consumption_items，
    避免出现"订单落了、券没落"的半截状态。items 默认 None，老调用方行为不变。
    每项：{"project_id", "project_name", "unit_coins", "item_code"}
    """
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO consumption_orders
                       (order_no, verify_code, user_id, items_json, card_coins, bonus_coins, total_coins)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (order_no, verify_code, user_id, items_json, card_coins, bonus_coins, total_coins)
                )
                if items:
                    cur.executemany(
                        """INSERT INTO consumption_items
                           (order_no, user_id, project_id, project_name, unit_coins, item_code)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        [(order_no, user_id, it.get("project_id"), it.get("project_name") or "",
                          int(it.get("unit_coins") or 0), it["item_code"]) for it in items]
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
    return get_consumption_by_no(order_no)


def get_consumption_by_no(order_no: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM consumption_orders WHERE order_no = %s", (order_no,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def get_consumption_by_code(verify_code: str) -> Optional[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM consumption_orders WHERE verify_code = %s", (verify_code,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def mark_consumption_used(order_no: str) -> bool:
    """商家核销：unused → used（幂等：已核销返回 False）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE consumption_orders SET status = 'used' WHERE order_no = %s AND status = 'unused'",
                (order_no,)
            )
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


def list_consumptions_by_user(user_id: int, limit: int = 10, today: bool = False) -> List[Dict]:
    """用户消费记录；today=True 仅返回今日（首页游乐消费弹窗用）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        if today:
            cur.execute(
                """SELECT * FROM consumption_orders
                   WHERE user_id = %s AND DATE(created_at) = CURDATE()
                   ORDER BY id DESC LIMIT %s""",
                (user_id, limit)
            )
        else:
            cur.execute(
                "SELECT * FROM consumption_orders WHERE user_id = %s ORDER BY id DESC LIMIT %s",
                (user_id, limit)
            )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def list_all_consumptions(page: int = 1, page_size: int = 20) -> List[Dict]:
    """全量消费记录（联用户昵称，商家端消费记录 tab 数据源）"""
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT co.*, u.nickname, u.phone
               FROM consumption_orders co LEFT JOIN users u ON u.id = co.user_id
               ORDER BY co.id DESC LIMIT %s OFFSET %s""",
            (page_size, offset)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_all_consumptions() -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM consumption_orders")
        row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 消费券（一单一券包）：一券 = 一项一次，一券一码
# 券级状态独立于订单状态——员工扫一张核销一张，同单其它券不受影响。
# 订单仍是金额与整单状态的真源，券只负责"哪一项玩没玩"。
# ============================================================
def create_consumption_items(order_no: str, user_id: Optional[int],
                             items: List[Dict]) -> int:
    """批量写入消费券（一券一行），返回写入张数。

    正常下单走 create_consumption_order(items=...) —— 券与订单**同一事务**落库；
    本函数是给「旧消费单迁移成券」这类一次性脚本用的独立入口。
    每项：{"project_id", "project_name", "unit_coins", "item_code", "status", "used_at"}
    """
    if not items:
        return 0
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO consumption_items
                       (order_no, user_id, project_id, project_name, unit_coins,
                        item_code, status, used_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    [(order_no, user_id, it.get("project_id"), it.get("project_name") or "",
                      int(it.get("unit_coins") or 0), it["item_code"],
                      it.get("status") or "unused", it.get("used_at")) for it in items]
                )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
    return len(items)


def get_consumption_item_by_code(item_code: str) -> Optional[Dict]:
    """按券码取单张券（扫码核销、券码查重的入口）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM consumption_items WHERE item_code = %s", (item_code,))
        row = cur.fetchone()
    conn.close()
    return _row_to_dict(row)


def mark_consumption_item_used(item_code: str) -> bool:
    """扫码核销单张券：unused → used（幂等：已核销/已退款返回 False）"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE consumption_items SET status = 'used', used_at = CURRENT_TIMESTAMP
                   WHERE item_code = %s AND status = 'unused'""",
                (item_code,)
            )
            ok = cur.rowcount > 0
        conn.commit()
        conn.close()
    return ok


def mark_consumption_items_used_by_order(order_no: str) -> int:
    """整单一次性核销（一家三口一次玩完）：返回本次实际核销的券数"""
    with _lock:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE consumption_items SET status = 'used', used_at = CURRENT_TIMESTAMP
                   WHERE order_no = %s AND status = 'unused'""",
                (order_no,)
            )
            n = cur.rowcount
        conn.commit()
        conn.close()
    return n


def list_consumption_items_by_order(order_no: str) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM consumption_items WHERE order_no = %s ORDER BY id ASC",
            (order_no,)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_consumption_items_by_order(order_no: str, status: Optional[str] = None) -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM consumption_items WHERE order_no = %s AND status = %s",
                (order_no, status)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM consumption_items WHERE order_no = %s", (order_no,)
            )
        row = cur.fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def list_consumption_items_by_user(user_id: int, status: Optional[str] = None,
                                   limit: int = 50) -> List[Dict]:
    """我的游园码：status=None 返回全部，待玩的排在前面（同状态按购买顺序）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute(
                """SELECT * FROM consumption_items WHERE user_id = %s AND status = %s
                   ORDER BY id ASC LIMIT %s""",
                (user_id, status, limit)
            )
        else:
            cur.execute(
                """SELECT * FROM consumption_items WHERE user_id = %s
                   ORDER BY (status = 'unused') DESC, id ASC LIMIT %s""",
                (user_id, limit)
            )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_consumption_items_by_user(user_id: int, status: Optional[str] = None) -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        if status:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM consumption_items WHERE user_id = %s AND status = %s",
                (user_id, status)
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM consumption_items WHERE user_id = %s", (user_id,)
            )
        row = cur.fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def count_consumption_items_by_orders(order_nos: List[str]) -> Dict[str, Dict]:
    """批量统计多张消费单的券数 → {order_no: {"total": n, "unused": m}}。

    商家端消费记录列表用。逐单调用会把连接开成 2×页大小 次，这里一趟 GROUP BY 拿完。
    """
    if not order_nos:
        return {}
    placeholders = ", ".join(["%s"] * len(order_nos))
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT order_no,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS unused
                FROM consumption_items WHERE order_no IN ({placeholders})
                GROUP BY order_no""",
            tuple(order_nos)
        )
        rows = cur.fetchall()
    conn.close()
    return {r["order_no"]: {"total": int(r["total"] or 0),
                            "unused": int(r["unused"] or 0)} for r in rows}


def list_consumption_items_by_orders(order_nos: List[str]) -> List[Dict]:
    """批量取多张消费单的券行（商家端列表要逐单展开券明细）。

    逐单调 list_consumption_items_by_order 会把连接开成页大小次，这里一趟 IN 拿完，
    由调用方按 order_no 自己分组。
    """
    if not order_nos:
        return []
    placeholders = ", ".join(["%s"] * len(order_nos))
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT * FROM consumption_items WHERE order_no IN ({placeholders})
                ORDER BY order_no ASC, id ASC""",
            tuple(order_nos)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 游乐消费退款（商家 PC / 员工端操作；用户端无此功能）
# 规则（2026-09-20 修订）：**退款不再看核销状态**——
#   用户可能刚上去就下来，现场由员工/管理员按实际情况退，
#   已经核销过的券同样可以退（核销只代表"玩过"，不代表"不能退"）。
#   两种入口共用同一份核心 refund_consumption_items()：
#     · 按券退（勾选若干张）→ refund_consumption_items(codes)
#     · 整单退（该单所有没退过的券）→ refund_consumption_order(order_no) 转发到核心
#   退款额 = 选中券的面额合计，按订单原扣币比例拆成卡内币/签到奖励币：
#   卡内币回充到生效中的卡（无生效卡时转回签到奖励池），
#   签到奖励部分通过调低 bonus_coins 让池子恢复（bonus 统计排除已退款订单）。
#   没有券行的历史订单仍按整单全额退（老单没有券级信息，无法只退指定项）。
# ============================================================
def refund_consumption_items(codes: List[str]) -> Dict:
    """按券退款（核心实现）：只处理传入的券码，**已核销(used)的券也能退**。

    与整单退款的差别只在"退哪些券"；钱的算法两边完全一样，所以整单退款也走这里
    （refund_consumption_order 转发），避免两套算术各写一遍后慢慢漂移。

    卡内币/奖励币的拆分用**累计口径**算差量：
        card_cum(n) = card_coins * n // total_coins
        本批卡内币  = card_cum(已退 + 本批) - card_cum(已退)
    一张张分多次退时，若每批都按「本批面额 × 比例」各自取整，多批次会丢币、总账对不上。
    """
    # 去重 + 统一大写（券码大小写不敏感，前端可能传来小写或带空格）
    norm: List[str] = []
    seen = set()
    for c in (codes or []):
        c = str(c or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            norm.append(c)
    if not norm:
        return {"ok": False, "msg": "没有需要退款的券", "refunded_coins": 0,
                "count": 0, "items": [], "skipped": []}

    refunded_items: List[Dict] = []
    skipped: List[Dict] = []
    total_refund = 0

    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                ph = ", ".join(["%s"] * len(norm))
                cur.execute(
                    f"SELECT * FROM consumption_items WHERE item_code IN ({ph}) FOR UPDATE",
                    tuple(norm)
                )
                found = {r["item_code"]: r for r in (cur.fetchall() or [])}

                # 先分拣：不存在的 / 已退过的直接跳过，剩下的按单分组
                by_order: Dict[str, List[Dict]] = {}
                for c in norm:
                    r = found.get(c)
                    if not r:
                        skipped.append({"item_code": c, "reason": "券码不存在"})
                    elif (r.get("status") or "unused") == "refunded":
                        skipped.append({"item_code": c, "reason": "该券已退过款"})
                    else:
                        by_order.setdefault(r["order_no"], []).append(r)

                for order_no, its in by_order.items():
                    cur.execute(
                        "SELECT * FROM consumption_orders WHERE order_no = %s FOR UPDATE",
                        (order_no,)
                    )
                    order = cur.fetchone()
                    if not order:
                        for r in its:
                            skipped.append({"item_code": r["item_code"], "reason": "消费单不存在"})
                        continue

                    code_list = [r["item_code"] for r in its]
                    iph = ", ".join(["%s"] * len(code_list))
                    batch = sum(int(r.get("unit_coins") or 0) for r in its)
                    if batch <= 0:
                        for r in its:
                            skipped.append({"item_code": r["item_code"], "reason": "该券面额为 0"})
                        continue

                    total_coins = int(order["total_coins"] or 0)
                    card_coins = int(order["card_coins"] or 0)

                    # 已退部分的累计面额（不含本批）→ 累计差量，多批次取整不丢币
                    cur.execute(
                        f"""SELECT COALESCE(SUM(unit_coins), 0) AS coins
                            FROM consumption_items
                            WHERE order_no = %s AND status = 'refunded'
                              AND item_code NOT IN ({iph})""",
                        tuple([order_no] + code_list)
                    )
                    before = int(cur.fetchone()["coins"] or 0)
                    after = before + batch
                    if total_coins > 0:
                        card_part = (card_coins * after // total_coins) - (card_coins * before // total_coins)
                    else:
                        card_part = 0
                    card_part = max(0, min(card_part, batch))
                    bonus_part = batch - card_part

                    if card_part > 0:
                        cur.execute(
                            """SELECT id FROM user_cards
                               WHERE user_id = %s AND status = 'active'
                               ORDER BY in_use DESC, created_at ASC, id ASC""",
                            (order["user_id"],)
                        )
                        cards = cur.fetchall()
                        if cards:
                            left = card_part
                            even = card_part // max(1, len(cards))
                            for c in cards:
                                take = even if c is not cards[-1] else left
                                if take > 0:
                                    cur.execute(
                                        "UPDATE user_cards SET coins = coins + %s WHERE id = %s",
                                        (take, c["id"])
                                    )
                                    left -= take
                        else:
                            # 无生效卡：卡内币转回签到奖励池（减少该单的 bonus 占用）
                            bonus_part += card_part

                    if bonus_part > 0:
                        cur.execute(
                            "UPDATE consumption_orders SET bonus_coins = GREATEST(bonus_coins - %s, 0) WHERE order_no = %s",
                            (bonus_part, order_no)
                        )

                    # 不管 unused 还是 used，统一置 refunded——现场退的是"这一项不玩了"
                    cur.execute(
                        f"UPDATE consumption_items SET status = 'refunded' WHERE item_code IN ({iph})",
                        tuple(code_list)
                    )

                    # 订单状态按券的实际分布重算：
                    #   还有待玩 → unused；待玩清零但仍有已核销 → used；全退 → refunded
                    cur.execute(
                        """SELECT SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS u,
                                  SUM(CASE WHEN status = 'used'   THEN 1 ELSE 0 END) AS s
                           FROM consumption_items WHERE order_no = %s""",
                        (order_no,)
                    )
                    st = cur.fetchone() or {}
                    u = int(st.get("u") or 0)
                    s = int(st.get("s") or 0)
                    new_status = "refunded" if (u == 0 and s == 0) else ("used" if u == 0 else "unused")
                    cur.execute(
                        "UPDATE consumption_orders SET status = %s WHERE order_no = %s",
                        (new_status, order_no)
                    )

                    total_refund += batch
                    for r in its:
                        refunded_items.append({
                            "item_code": r["item_code"],
                            "order_no": order_no,
                            "project_name": r.get("project_name") or "",
                            "unit_coins": int(r.get("unit_coins") or 0),
                        })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # 提前 return 的路径也要回收连接，统一收到 finally 里
            conn.close()

    if not refunded_items:
        return {"ok": False, "msg": "没有可退款的券（可能都已退过款）",
                "refunded_coins": 0, "count": 0, "items": [], "skipped": skipped}

    msg = f"退款成功，{total_refund} 币已退回用户的卡（共 {len(refunded_items)} 张券）"
    if skipped:
        msg += f"，另有 {len(skipped)} 张已跳过"
    return {"ok": True, "msg": msg, "refunded_coins": total_refund,
            "count": len(refunded_items), "items": refunded_items, "skipped": skipped}


def refund_consumption_order(order_no: str) -> Dict:
    """整单退款：把该单**所有还没退过的券**一次退掉（含已核销的）。

    有券行 → 转发核心 refund_consumption_items，与按券退款共用一套算术；
    无券行（迁移前的历史单）→ 走下方 legacy 整单全额退，行为与从前一致。
    """
    items = list_consumption_items_by_order(order_no)
    if items:
        order = get_consumption_by_no(order_no)
        if not order:
            return {"ok": False, "msg": "消费单不存在"}
        codes = [it["item_code"] for it in items
                 if (it.get("status") or "unused") != "refunded"]
        if not codes:
            return {"ok": False, "msg": "该消费单已退款，请勿重复操作"}
        res = refund_consumption_items(codes)
        if res.get("ok"):
            res["data"] = get_consumption_by_no(order_no)
        return res
    return _refund_consumption_order_legacy(order_no)


def _refund_consumption_order_legacy(order_no: str) -> Dict:
    """历史消费单（没有券行）整单全额退款——老单没有券级信息，只能整单退。"""
    with _lock:
        conn = _get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM consumption_orders WHERE order_no = %s FOR UPDATE",
                    (order_no,)
                )
                order = cur.fetchone()
                if not order:
                    return {"ok": False, "msg": "消费单不存在"}
                if order["status"] == "refunded":
                    return {"ok": False, "msg": "该消费单已退款，请勿重复操作"}

                total_coins = int(order["total_coins"] or 0)
                card_coins = int(order["card_coins"] or 0)
                user_id = order["user_id"]
                refunded_coins = total_coins

                # 按订单原来的扣币比例，把退款额拆成卡内币 / 签到奖励币
                card_part = (card_coins * refunded_coins // total_coins) if total_coins > 0 else 0
                bonus_part = refunded_coins - card_part

                if card_part > 0:
                    cur.execute(
                        """SELECT id FROM user_cards
                           WHERE user_id = %s AND status = 'active'
                           ORDER BY in_use DESC, created_at ASC, id ASC""",
                        (user_id,)
                    )
                    cards = cur.fetchall()
                    if cards:
                        left = card_part
                        even = card_part // max(1, len(cards))
                        for c in cards:
                            take = even if c is not cards[-1] else left
                            if take > 0:
                                cur.execute(
                                    "UPDATE user_cards SET coins = coins + %s WHERE id = %s",
                                    (take, c["id"])
                                )
                                left -= take
                    else:
                        # 无生效卡：卡内币转回签到奖励池（减少该单的 bonus 占用）
                        bonus_part += card_part

                if bonus_part > 0:
                    cur.execute(
                        "UPDATE consumption_orders SET bonus_coins = GREATEST(bonus_coins - %s, 0) WHERE order_no = %s",
                        (bonus_part, order_no)
                    )

                cur.execute(
                    "UPDATE consumption_orders SET status = 'refunded' WHERE order_no = %s",
                    (order_no,)
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # 原来的三处 conn.close() 在"消费单不存在/已退款"这类提前 return
            # 的路径上会漏掉，连接不回收。统一收到 finally 里，所有出口都能关。
            conn.close()

    order = get_consumption_by_no(order_no)
    return {"ok": True, "msg": f"退款成功，{refunded_coins} 币已退回用户的卡", "data": order}


# ============================================================
# 热点问题（近 7 天高频问题 Top6 → 小程序快捷提问按钮）
# ============================================================
def hot_question_candidates(days: int = 7) -> List[Dict]:
    """近 N 天的问答原始分组数据（供 services/hot_questions.py 做同义聚类）。

    相比 top_questions 的精确分组，这里额外带出 search_query（QueryAgent 改写后的
    规范问句），聚类时优先用它把"门票多少钱/门票价格是多少"这类同义问法并到一起。
    """
    conn = _get_conn()
    sql = """SELECT TRIM(question) AS question,
                    COALESCE(NULLIF(TRIM(search_query), ''), TRIM(question)) AS norm_query,
                    COUNT(*) AS cnt
             FROM question_logs
             WHERE question IS NOT NULL AND TRIM(question) != ''"""
    params: list = []
    if days:
        sql += " AND created_at >= %s"
        params.append(_since_str(days))
    sql += " GROUP BY TRIM(question), COALESCE(NULLIF(TRIM(search_query), ''), TRIM(question))"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def replace_hot_questions(items: List[Dict], days: int = 7) -> int:
    """整批替换热点问题（7 天刷新一次，先清空再写入，保证按钮集合始终是"这一批"）。

    items: [{"question","label","icon","hit_count","raw_count","source"}]
    """
    from datetime import datetime, timedelta
    now = datetime.now()
    win_start = now - timedelta(days=days)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hot_questions")
            for i, it in enumerate(items):
                cur.execute(
                    """INSERT INTO hot_questions
                       (question, label, icon, hit_count, raw_count,
                        window_start, window_end, source, sort, refreshed_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        (it.get("question") or "")[:255],
                        (it.get("label") or "")[:16],
                        (it.get("icon") or "")[:8],
                        int(it.get("hit_count") or 0),
                        int(it.get("raw_count") or 0),
                        win_start.strftime("%Y-%m-%d %H:%M:%S"),
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                        it.get("source") or "auto",
                        i,
                        now.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return len(items)


def list_hot_questions(limit: int = 6) -> List[Dict]:
    """读取当前批次热点问题（按 sort 升序）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, question, label, icon, hit_count, raw_count,
                      window_start, window_end, source, sort, refreshed_at
               FROM hot_questions ORDER BY sort ASC, id ASC LIMIT %s""",
            (int(limit),)
        )
        rows = cur.fetchall()
    conn.close()
    return _rows_to_list(rows)


def hot_questions_refreshed_at():
    """当前批次刷新时间（无数据返回 None，用于判断是否已过 7 天）"""
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT MAX(refreshed_at) AS ts FROM hot_questions")
        row = cur.fetchone()
    conn.close()
    return (row or {}).get("ts")


# ============================================================
# 用户收藏（AI 回答「收藏」按钮 → 我的页「我的收藏」）
# ============================================================
def add_favorite(user_id: int, question: str, answer: str, session_id: str = "",
                 images: List[str] = None) -> int:
    """新增收藏（同一用户同一问句重复收藏时更新答案与图片，避免列表里出现重复条目）"""
    images_json = json.dumps([u for u in (images or []) if u], ensure_ascii=False)
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM user_favorites
                   WHERE user_id = %s AND question = %s LIMIT 1""",
                (user_id, (question or "")[:2000])
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE user_favorites SET answer = %s, session_id = %s,
                       images_json = %s, created_at = NOW() WHERE id = %s""",
                    (answer or "", session_id or "", images_json, row["id"])
                )
                fav_id = row["id"]
            else:
                cur.execute(
                    """INSERT INTO user_favorites (user_id, session_id, question, answer, images_json)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (user_id, session_id or "", (question or "")[:2000], answer or "", images_json)
                )
                fav_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return int(fav_id)


def _parse_images(raw) -> List[str]:
    """images_json 文本 → 图片地址列表（容错：老数据/NULL/非法 JSON 一律返回空列表）"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    try:
        val = json.loads(raw)
        return [str(x) for x in val if x] if isinstance(val, list) else []
    except Exception:
        return []


def list_favorites(user_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, question, answer, images_json, session_id, created_at
               FROM user_favorites WHERE user_id = %s
               ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
            (user_id, int(limit), int(offset))
        )
        rows = cur.fetchall()
    conn.close()
    items = _rows_to_list(rows)
    for it in items:
        it["images"] = _parse_images(it.get("images_json"))
    return items


def count_favorites(user_id: int) -> int:
    conn = _get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM user_favorites WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    conn.close()
    return int((row or {}).get("c") or 0)


def delete_favorite(fav_id: int, user_id: int) -> bool:
    """删除收藏（带 user_id 约束，防止越权删他人收藏）"""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_favorites WHERE id = %s AND user_id = %s",
                (fav_id, user_id)
            )
            affected = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    return affected > 0


# ============================================================
# 响应耗时统计（商家 PC「响应耗时 P50/P95」看板）
# ============================================================
def question_latency_samples(days: int = 7) -> List[float]:
    """近 N 天每轮问答的真实耗时（秒）列表，供接口层算 P50/P90/P95"""
    conn = _get_conn()
    # 排除压测样本：probe（_probe_latency.py，按固定剧本刷且常在"系统 Python"等已知慢配置下跑）
    # 与 stress（_stress_test.py，并发波次）。混进 P95 会让"真实用户体验"指标长期失真
    # —— 实测 161 条样本里有 55 条来自压测，P95 被抬到 41s、达标率仅 57%。
    sql = ("SELECT elapsed_seconds AS e FROM question_logs "
           "WHERE elapsed_seconds IS NOT NULL AND elapsed_seconds > 0 "
           "AND COALESCE(source, '') NOT IN ('probe', 'stress')")
    params: list = []
    if days:
        sql += " AND created_at >= %s"
        params.append(_since_str(days))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.close()
    return [float(r["e"]) for r in rows if r.get("e") is not None]
