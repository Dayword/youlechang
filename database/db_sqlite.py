# ============================================================
# 游乐场RAG智能知识库问答系统 - 数据库模块
# 表清单：
#   1. users            用户表（已有，保留兼容）
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
import sqlite3
import os
import json
import threading
from typing import Optional, List, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database", "users.db")
_lock = threading.Lock()


# ============================================================
# 基础连接工具
# ============================================================
def _get_conn() -> sqlite3.Connection:
    """获取数据库连接（开启外键约束、行工厂）"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict]:
    """Row 转 dict，JSON 字段自动解析"""
    if row is None:
        return None
    d = dict(row)
    return d


def _rows_to_list(rows) -> List[Dict]:
    return [dict(r) for r in rows]


# ============================================================
# 初始化所有表
# ============================================================
def init_db():
    """初始化数据库所有表（幂等，可重复调用）"""
    with _lock:
        conn = _get_conn()
        cur = conn.cursor()

        # ---------- 1. 用户表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname      TEXT DEFAULT '',
                avatar        TEXT DEFAULT '',
                email         TEXT DEFAULT '',
                role          TEXT DEFAULT 'user',
                wx_openid     TEXT UNIQUE,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------- 已有库升级：users 表补充 wx_openid 列（新建库无需） ----------
        cur.execute("PRAGMA table_info(users)")
        user_cols = {r["name"] for r in cur.fetchall()}
        if "wx_openid" not in user_cols:
            # SQLite 的 ALTER 不能加 UNIQUE 约束，唯一性由 get_user_by_openid 逻辑保证
            cur.execute("ALTER TABLE users ADD COLUMN wx_openid TEXT")

        # ---------- 2. 会话表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT UNIQUE NOT NULL,
                user_id         INTEGER,
                title           TEXT DEFAULT '新会话',
                message_count   INTEGER DEFAULT 0,
                last_message_at TIMESTAMP,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON chat_sessions(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_sid ON chat_sessions(session_id)")

        # ---------- 3. 问答消息表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL,
                user_id       INTEGER,
                role          TEXT NOT NULL DEFAULT 'user',   -- user / assistant
                question      TEXT,                              -- 用户原始问题（role=user时）
                answer        TEXT,                              -- 助手回答（role=assistant时）
                content       TEXT,                              -- 统一内容字段
                ref_docs      TEXT DEFAULT '[]',                 -- JSON: 引用文档列表
                search_query  TEXT DEFAULT '',                   -- 改写后的检索问句
                intent        TEXT DEFAULT '其他',               -- 意图分类
                elapsed_ms    REAL DEFAULT 0,                    -- 耗时（毫秒）
                ip_address    TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON chat_messages(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_created ON chat_messages(created_at)")

        # ---------- 4. 文档元数据表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                filename      TEXT UNIQUE NOT NULL,              -- 存储文件名（含时间戳）
                original_name TEXT NOT NULL,                      -- 原始上传名
                file_size     INTEGER DEFAULT 0,
                file_type     TEXT DEFAULT '',                    -- pdf/txt/docx/doc
                chunk_count   INTEGER DEFAULT 0,
                status        TEXT DEFAULT 'pending',             -- pending/processing/completed/failed
                error_msg     TEXT DEFAULT '',
                uploaded_by   INTEGER,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status)")

        # ---------- 5. 文档切片表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS document_chunks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id   INTEGER NOT NULL,
                chunk_index   INTEGER NOT NULL,                   -- 块序号
                content       TEXT NOT NULL,                       -- 块文本
                token_count   INTEGER DEFAULT 0,
                milvus_id     TEXT DEFAULT '',                     -- Milvus 中的实体ID
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                UNIQUE(document_id, chunk_index)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(document_id)")

        # ---------- 6. 用户反馈表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id    INTEGER,                             -- 关联 chat_messages.id
                session_id    TEXT DEFAULT '',
                user_id       INTEGER,
                rating        TEXT NOT NULL,                       -- like / dislike
                comment       TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_msg ON feedback(message_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id)")

        # ---------- 7. Agent运行配置持久化表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_config (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key    TEXT UNIQUE NOT NULL,
                config_value  TEXT NOT NULL,                       -- JSON 字符串
                description   TEXT DEFAULT '',
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------- 7.1 用户画像表（小程序「AI 记得我」+ 商家端画像看板） ----------
        # 与 db_mysql.py 同构；单独建表而不塞 agent_config 的 KV 的原因见那边注释。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        INTEGER UNIQUE NOT NULL,
                profile_json   TEXT DEFAULT '{}',
                tags_json      TEXT DEFAULT '[]',
                msg_count      INTEGER DEFAULT 0,
                source_version TEXT DEFAULT 'v2',
                last_active_at TIMESTAMP,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_profile_active ON user_profiles(last_active_at)")

        # ---------- 7.2 画像版本表（审计：只 INSERT，不改不删） ----------
        # 为什么单独一张表：画像写坏了以前没有任何痕迹（用户投诉"AI 说我家两个孩子"
        # 却查不出哪一轮写进去的），而 clear() 又是真删 —— 连证据一起销毁。
        # 这里每次写入留一条不可变记录；要抹内容走 redact（置 NULL 保轨迹），不物理删。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_profile_versions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                rev         INTEGER DEFAULT 0,
                op          TEXT NOT NULL,                  -- created | modified | deleted
                field_path  TEXT DEFAULT '',                -- 预留：定位到具体字段用
                before_json TEXT,
                after_json  TEXT,
                sha256      TEXT DEFAULT '',
                actor       TEXT DEFAULT 'system',          -- user | session | system
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                redacted_at TIMESTAMP,
                redacted_by TEXT DEFAULT ''
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_profile_ver_user "
                    "ON user_profile_versions(user_id, id)")

        # ---------- 8. 操作日志表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER,
                action        TEXT NOT NULL,                       -- 操作类型
                target        TEXT DEFAULT '',                     -- 操作对象
                detail        TEXT DEFAULT '{}',                   -- JSON: 详细信息
                ip_address    TEXT DEFAULT '',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_oplogs_user ON operation_logs(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_oplogs_action ON operation_logs(action)")

        # ---------- 9. 卡产品表（价格权威来源） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cards (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT UNIQUE NOT NULL,
                price       INTEGER NOT NULL DEFAULT 0,
                description TEXT DEFAULT '',
                status      TEXT DEFAULT 'on_sale',
                sort        INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------- 10. 办卡订单表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no       TEXT UNIQUE NOT NULL,
                user_id        INTEGER,
                card_id        INTEGER NOT NULL,
                card_name      TEXT DEFAULT '',
                amount         INTEGER NOT NULL DEFAULT 0,
                openid         TEXT DEFAULT '',
                status         TEXT DEFAULT 'pending',
                transaction_id TEXT DEFAULT '',
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                paid_at        TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_user ON card_orders(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON card_orders(status)")

        # ---------- 11. 用户持卡表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_cards (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                card_id    INTEGER NOT NULL,
                card_name  TEXT DEFAULT '',
                order_no   TEXT DEFAULT '',
                status     TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ucards_user ON user_cards(user_id)")

        # ---------- 已有库升级：user_cards 表补充 coins 列（持卡实际余币，发卡时写入面值，
        #          商家可调整；旧数据 NULL 时按卡产品面值解析兑底） ----------
        cur.execute("PRAGMA table_info(user_cards)")
        uc_cols = {r["name"] for r in cur.fetchall()}
        if "coins" not in uc_cols:
            cur.execute("ALTER TABLE user_cards ADD COLUMN coins INTEGER NULL DEFAULT NULL")
        # kind：member=会员充值卡（可生效/退卡）；small=小额充币卡（直接进余币、不可退）
        # 与 MySQL 实现（db_mysql.add_user_card / schema_mysql.sql）保持同口径，
        # 否则 SQLite 模式下发卡会因 add_user_card 收到未知参数而直接抛错。
        if "kind" not in uc_cols:
            cur.execute("ALTER TABLE user_cards ADD COLUMN kind TEXT DEFAULT 'member'")

        # ---------- 12. 问答日志表 ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS question_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                question        TEXT,
                search_query    TEXT DEFAULT '',
                answer          TEXT,
                answer_full_len INTEGER DEFAULT 0,
                references_json TEXT DEFAULT '[]',
                elapsed_seconds REAL DEFAULT 0,
                session_id      TEXT DEFAULT '',
                source          TEXT DEFAULT 'web',
                answer_type     TEXT DEFAULT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qlogs_created ON question_logs(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_qlogs_session ON question_logs(session_id)")

        # ---------- 已有库升级：question_logs 补 intent / answer_type 列 ----------
        # answer_type: normal / cached / fallback_none / fallback_partial
        # 知识库盲区统计的正确判据（旧数据为 NULL，统计侧用答案开头的兜底话术兜底识别）
        cur.execute("PRAGMA table_info(question_logs)")
        ql_cols = {r["name"] for r in cur.fetchall()}
        if "intent" not in ql_cols:
            cur.execute("ALTER TABLE question_logs ADD COLUMN intent TEXT DEFAULT '其他'")
        if "answer_type" not in ql_cols:
            cur.execute("ALTER TABLE question_logs ADD COLUMN answer_type TEXT DEFAULT NULL")

        # ---------- 13. 每日签到表（我的页签到送币，奖励计入余币余额） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_checkins (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER,
                checkin_date TEXT NOT NULL,
                reward_coins INTEGER NOT NULL DEFAULT 1,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                UNIQUE(user_id, checkin_date)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_checkins_user ON user_checkins(user_id)")

        # ---------- 14. 失物招领表（游客报失 + 商家登记统一管理） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lost_found_items (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                photo_url  TEXT DEFAULT '',
                status     TEXT DEFAULT 'lost',
                source     TEXT DEFAULT 'user',
                reporter   TEXT DEFAULT '',
                ai_category TEXT DEFAULT '',
                ai_color    TEXT DEFAULT '',
                ai_brand    TEXT DEFAULT '',
                ai_features TEXT DEFAULT '',
                ai_text     TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lf_status ON lost_found_items(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lf_created ON lost_found_items(created_at)")

        # ---------- 已有库升级：lost_found_items 补充 AI 识别列 ----------
        # 与 db_mysql.py 的同名迁移对应：失物照片经多模态模型识别后落库，
        # 供商家端按特征比对辅助认领。空字符串 = 未识别或识别失败。
        lfcols = [r[1] for r in conn.execute("PRAGMA table_info(lost_found_items)").fetchall()]
        for _col in ("ai_category", "ai_color", "ai_brand", "ai_features", "ai_text"):
            if _col not in lfcols:
                conn.execute(f"ALTER TABLE lost_found_items ADD COLUMN {_col} TEXT DEFAULT ''")

        # ---------- 已有库升级：users 表补充 bonus_spent 列 ----------
        ucols2 = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "bonus_spent" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN bonus_spent INTEGER DEFAULT 0")

        # ---------- 已有库升级：user_cards 补充 in_use 列 ----------
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(user_cards)").fetchall()]
        if "in_use" not in ucols:
            conn.execute("ALTER TABLE user_cards ADD COLUMN in_use INTEGER DEFAULT 0")

        # ---------- 13.5 游乐项目价目表（首页游乐消费数据源） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS park_projects (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                price_coins INTEGER NOT NULL DEFAULT 0,
                unit_desc   TEXT DEFAULT '',
                status      TEXT DEFAULT 'on',
                sort        INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------- 13.6 消费订单表（多选项目合并扣币，生成核销码） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consumption_orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no    TEXT NOT NULL,
                verify_code TEXT NOT NULL,
                user_id     INTEGER NULL,
                items_json  TEXT NULL,
                card_coins  INTEGER DEFAULT 0,
                bonus_coins INTEGER DEFAULT 0,
                total_coins INTEGER NOT NULL DEFAULT 0,
                status      TEXT DEFAULT 'unused',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_co_user ON consumption_orders(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_co_created ON consumption_orders(created_at)")

        # ---------- 13.7 消费券明细表（一券 = 一项一次，一券一码） ----------
        # 拆券理由与 MySQL 侧一致：一单一码时员工看不到明细、整单一次核销会漏掉没玩的项目。
        cur.execute("""
            CREATE TABLE IF NOT EXISTS consumption_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no     TEXT NOT NULL,
                user_id      INTEGER NULL,
                project_id   INTEGER NULL,
                project_name TEXT NOT NULL,
                unit_coins   INTEGER NOT NULL DEFAULT 0,
                item_code    TEXT NOT NULL,
                status       TEXT DEFAULT 'unused',
                used_at      TIMESTAMP NULL,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uk_item_code ON consumption_items(item_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ci_order ON consumption_items(order_no)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ci_user_status ON consumption_items(user_id, status)")

        # ---------- 15. 园区公告 / 今日热点（仅商家 PC 端发布，小程序首页展示） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS park_notices (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                kind       TEXT NOT NULL DEFAULT 'announcement',
                title      TEXT NOT NULL,
                content    TEXT DEFAULT '',
                status     TEXT DEFAULT 'on',
                sort       INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pn_kind ON park_notices(kind)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pn_created ON park_notices(created_at)")

        # ---------- 16. 游客意见反馈（小程序「意见反馈」弹窗提交，商家端查看） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NULL,
                nickname   TEXT DEFAULT '',
                content    TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_uf_created ON user_feedback(created_at)")

        # ---------- 17. 热点问题（近 7 天高频问题 Top6 → 小程序快捷提问按钮） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS hot_questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                question     TEXT NOT NULL,
                label        TEXT NOT NULL DEFAULT '',
                icon         TEXT NOT NULL DEFAULT '',
                hit_count    INTEGER NOT NULL DEFAULT 0,
                raw_count    INTEGER NOT NULL DEFAULT 0,
                window_start TIMESTAMP NULL,
                window_end   TIMESTAMP NULL,
                source       TEXT NOT NULL DEFAULT 'auto',
                sort         INTEGER NOT NULL DEFAULT 0,
                refreshed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ---------- 18. 用户收藏（AI 回答「收藏」→ 我的页「我的收藏」） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_favorites (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                session_id  TEXT DEFAULT '',
                question    TEXT NULL,
                answer      TEXT NULL,
                images_json TEXT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ufav_user ON user_favorites(user_id)")
        # 已有库升级：补 images_json 列（收藏附图）
        _fav_cols = [r[1] for r in conn.execute("PRAGMA table_info(user_favorites)").fetchall()]
        if "images_json" not in _fav_cols:
            conn.execute("ALTER TABLE user_favorites ADD COLUMN images_json TEXT NULL")

        # ---------- 已有库升级：users 表补充 phone 列（微信登录后必须绑定手机号） ----------
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "phone" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT ''")

        # ---------- 已有库升级：users 表补充 pwd_set 列 ----------
        # pwd_set=1 用户自己设过登录密码；=0 密码是系统随机占位串，首次输密码视为设置密码
        if "pwd_set" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN pwd_set INTEGER DEFAULT 0")

        # ---------- 已有库升级：users 表补充 email 列（管理端「个人信息」可填写邮箱） ----------
        if "email" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")

        # ---------- 已有库升级：users.phone 唯一（一个手机号 = 一个账号） ----------
        # 用表达式唯一索引：SQLite 唯一索引不约束 NULL，而 NULLIF(phone,'') 把空串也变成 NULL，
        # 所以多行"未绑定"不会互相冲突，现有写 '' 的代码零改动。
        # 建索引前必须先清掉历史重复（_fix_phone_dup.py），失败只告警不阻断启动。
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uk_phone ON users(NULLIF(phone, ''))"
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(
                "[升级] SQLite users.phone 唯一索引未建立（多半已有重复手机号）：%s\n"
                "        → 运行 `python _fix_phone_dup.py` 清理后重启", e)

        # ---------- 19. 小程序历史登录账号（设备维度） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mp_login_accounts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id     TEXT NOT NULL,
                user_id       INTEGER NOT NULL,
                phone         TEXT DEFAULT '',
                username      TEXT DEFAULT '',
                nickname      TEXT DEFAULT '',
                avatar        TEXT DEFAULT '',
                login_count   INTEGER DEFAULT 0,
                last_login_at TIMESTAMP NULL,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uk_device_user ON mp_login_accounts(device_id, user_id)"
        )

        # ---------- 20. 小程序免密登录令牌（"记住我 / 点头像直接登录"） ----------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mp_remember_tokens (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash  TEXT NOT NULL,
                user_id     INTEGER NOT NULL,
                device_id   TEXT DEFAULT '',
                expires_at  TIMESTAMP NOT NULL,
                revoked     INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uk_token_hash ON mp_remember_tokens(token_hash)"
        )

        # ---------- 种子数据：游乐项目价目（空表时自动灌入） ----------
        pc = conn.execute("SELECT COUNT(*) AS c FROM park_projects").fetchone()["c"]
        if pc == 0:
            conn.execute(
                """INSERT INTO park_projects (name, price_coins, unit_desc, status, sort) VALUES
                   ('旋转木马', 30, '5分钟', 'on', 1),
                   ('荡秋千', 40, '4分钟', 'on', 2),
                   ('小型过山车', 50, '15圈', 'on', 3),
                   ('蹦床', 40, '5分钟', 'on', 4),
                   ('浴室大作战', 40, '5分钟', 'on', 5),
                   ('大炮乐园', 30, '80发子弹', 'on', 6)"""
            )

        # ---------- 创建默认管理员账号 ----------
        from core.auth import hash_password
        cursor = cur.execute("SELECT id FROM users WHERE username = 'admin'")
        if not cursor.fetchone():
            cur.execute(
                "INSERT INTO users (username, password_hash, nickname, role) VALUES (?, ?, ?, ?)",
                ("admin", hash_password("admin123"), "管理员", "admin")
            )

        conn.commit()
        conn.close()


# ============================================================
# 1. 用户相关（保留原有接口，向后兼容）
# ============================================================
def create_user(username: str, password: str, nickname: str = "") -> Optional[Dict]:
    """创建用户"""
    from core.auth import hash_password
    with _lock:
        conn = _get_conn()
        try:
            password_hash = hash_password(password)
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, nickname) VALUES (?, ?, ?)",
                (username, password_hash, nickname or username)
            )
            conn.commit()
            user_id = cursor.lastrowid
            user = get_user_by_id(user_id)
            conn.close()
            return user
        except sqlite3.IntegrityError:
            conn.close()
            return None


def get_user_by_username(username: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_user_by_id(user_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_user(user_id: int, **kwargs) -> Optional[Dict]:
    # username 也允许改（管理端「个人信息」页），唯一性由调用方先校验
    # role 允许改（管理端「用户管理」页提权/降权），调用方必须先做权限校验
    allowed_fields = {"nickname", "avatar", "password_hash", "phone", "pwd_set",
                      "email", "username", "role"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        return get_user_by_id(user_id)
    with _lock:
        conn = _get_conn()
        set_clause = ", ".join([f"{k} = ?" for k in updates])
        values = list(updates.values()) + [user_id]
        conn.execute(f"UPDATE users SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
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
            if not conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
                return None
            deleted: Dict[str, int] = {}
            for table in _USER_OWNED_TABLES:
                cur = conn.execute(f"DELETE FROM `{table}` WHERE user_id = ?", (user_id,))
                if cur.rowcount:
                    deleted[table] = cur.rowcount
            conn.execute("UPDATE documents SET uploaded_by = NULL WHERE uploaded_by = ?",
                         (user_id,))
            conn.execute("UPDATE operation_logs SET user_id = NULL WHERE user_id = ?",
                         (user_id,))
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
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
    rows = conn.execute("SELECT id, username, nickname, phone, role, created_at FROM users ORDER BY id").fetchall()
    conn.close()
    return _rows_to_list(rows)


def search_users(keyword: str = "", only_bound: bool = False,
                 page: int = 1, limit: int = 20) -> Dict:
    """管理端用户列表（搜索 + 筛选 + 分页），与 db_mysql 同名同语义"""
    where, args = [], []
    keyword = (keyword or "").strip()
    if keyword:
        like = f"%{keyword}%"
        where.append("(COALESCE(phone, '') LIKE ? OR COALESCE(nickname, '') LIKE ? "
                     "OR username LIKE ? OR CAST(id AS TEXT) = ?)")
        args += [like, like, like, keyword]
    if only_bound:
        where.append("(COALESCE(phone, '') <> '' OR COALESCE(wx_openid, '') <> '')")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""

    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 20), 200))
    conn = _get_conn()
    total = conn.execute(f"SELECT COUNT(*) AS c FROM users{sql_where}", args).fetchone()["c"]
    rows = conn.execute(
        "SELECT id, username, nickname, phone, role, created_at, "
        "(wx_openid IS NOT NULL AND wx_openid <> '') AS has_wx "
        f"FROM users{sql_where} ORDER BY id LIMIT ? OFFSET ?",
        args + [limit, (page - 1) * limit]
    ).fetchall()
    conn.close()
    return {"users": _rows_to_list(rows), "total": total}


def dup_phone_groups() -> List[Dict]:
    """列出仍重复的手机号（唯一索引建好后应恒为空，留作管理端兜底监控）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT phone, COUNT(*) AS cnt, GROUP_CONCAT(id) AS ids FROM users "
        "WHERE COALESCE(phone, '') <> '' GROUP BY phone HAVING cnt > 1 ORDER BY cnt DESC"
    ).fetchall()
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
    """创建会话（若已存在则返回现有会话）"""
    existing = get_session(session_id)
    if existing:
        return existing
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO chat_sessions (session_id, user_id, title) VALUES (?, ?, ?)",
            (session_id, user_id, title)
        )
        conn.commit()
        conn.close()
    return get_session(session_id)


def get_session(session_id: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_sessions(user_id: Optional[int] = None, limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM chat_sessions WHERE user_id = ? ORDER BY last_message_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM chat_sessions ORDER BY last_message_at DESC LIMIT ?", (limit,)
        ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def update_session(session_id: str, **kwargs) -> Optional[Dict]:
    allowed = {"title", "message_count", "last_message_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return get_session(session_id)
    with _lock:
        conn = _get_conn()
        set_clause = ", ".join([f"{k} = ?" for k in updates])
        values = list(updates.values()) + [session_id]
        conn.execute(f"UPDATE chat_sessions SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?", values)
        conn.commit()
        conn.close()
    return get_session(session_id)


def delete_session(session_id: str) -> bool:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        deleted = cur.rowcount > 0
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
    """新增一条消息，返回消息ID；同时更新会话计数和最后活跃时间"""
    refs_json = json.dumps(ref_docs or [], ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO chat_messages
               (session_id, user_id, role, content, question, answer, ref_docs, search_query, intent, elapsed_ms, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, user_id, role, content, question, answer, refs_json, search_query, intent, elapsed_ms, ip_address)
        )
        msg_id = cur.lastrowid
        # 更新会话统计
        conn.execute(
            """UPDATE chat_sessions
               SET message_count = message_count + 1,
                   last_message_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE session_id = ?""",
            (session_id,)
        )
        conn.commit()
        conn.close()
    return msg_id


def get_messages(session_id: str, limit: int = 100, offset: int = 0) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (session_id, limit, offset)
    ).fetchall()
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
    """获取最近的问答记录（管理员仪表盘用）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM chat_messages WHERE role = 'user' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_messages(session_id: Optional[str] = None) -> int:
    conn = _get_conn()
    if session_id:
        row = conn.execute("SELECT COUNT(*) as cnt FROM chat_messages WHERE session_id = ?", (session_id,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM chat_messages").fetchone()
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
    """插入或更新文档元数据（按 filename 唯一）"""
    with _lock:
        conn = _get_conn()
        existing = conn.execute("SELECT id FROM documents WHERE filename = ?", (filename,)).fetchone()
        if existing:
            conn.execute(
                """UPDATE documents
                   SET original_name=?, file_size=?, file_type=?, status=?, chunk_count=?,
                       uploaded_by=?, error_msg=?, updated_at=CURRENT_TIMESTAMP
                   WHERE filename=?""",
                (original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg, filename)
            )
            doc_id = existing["id"]
        else:
            cur = conn.execute(
                """INSERT INTO documents (filename, original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (filename, original_name, file_size, file_type, status, chunk_count, uploaded_by, error_msg)
            )
            doc_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_document(doc_id)


def get_document(doc_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_document_by_filename(filename: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM documents WHERE filename = ?", (filename,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_documents(status: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    if status:
        rows = conn.execute("SELECT * FROM documents WHERE status = ? ORDER BY id DESC", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM documents ORDER BY id DESC").fetchall()
    conn.close()
    return _rows_to_list(rows)


def update_document_status(filename: str, status: str, chunk_count: int = 0, error_msg: str = ""):
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE documents SET status=?, chunk_count=?, error_msg=?, updated_at=CURRENT_TIMESTAMP WHERE filename=?",
            (status, chunk_count, error_msg, filename)
        )
        conn.commit()
        conn.close()


def delete_document(filename: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM documents WHERE filename = ?", (filename,))
        conn.commit()
        deleted = cur.rowcount > 0
        conn.close()
        return deleted


# ============================================================
# 5. 文档切片相关
# ============================================================
def add_chunk(document_id: int, chunk_index: int, content: str, token_count: int = 0, milvus_id: str = "") -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT OR REPLACE INTO document_chunks (document_id, chunk_index, content, token_count, milvus_id)
               VALUES (?, ?, ?, ?, ?)""",
            (document_id, chunk_index, content, token_count, milvus_id)
        )
        cid = cur.lastrowid
        conn.commit()
        conn.close()
    return cid


def get_chunks_by_document(document_id: int) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM document_chunks WHERE document_id = ? ORDER BY chunk_index ASC", (document_id,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_chunks(document_id: Optional[int] = None) -> int:
    conn = _get_conn()
    if document_id:
        row = conn.execute("SELECT COUNT(*) as cnt FROM document_chunks WHERE document_id = ?", (document_id,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM document_chunks").fetchone()
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
        cur = conn.execute(
            "INSERT INTO feedback (message_id, session_id, user_id, rating, comment) VALUES (?, ?, ?, ?, ?)",
            (message_id, session_id, user_id, rating, comment)
        )
        fid = cur.lastrowid
        conn.commit()
        conn.close()
    return fid


def list_feedback(limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 7. Agent配置持久化相关
# ============================================================
def set_config(key: str, value: Any, description: str = ""):
    """设置配置项（value 自动 JSON 序列化）"""
    val_json = json.dumps(value, ensure_ascii=False)
    with _lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO agent_config (config_key, config_value, description)
               VALUES (?, ?, ?)
               ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value, description=excluded.description, updated_at=CURRENT_TIMESTAMP""",
            (key, val_json, description)
        )
        conn.commit()
        conn.close()


def get_config(key: str, default: Any = None) -> Any:
    conn = _get_conn()
    row = conn.execute("SELECT config_value FROM agent_config WHERE config_key = ?", (key,)).fetchone()
    conn.close()
    if row:
        try:
            return json.loads(row["config_value"])
        except (json.JSONDecodeError, TypeError):
            return row["config_value"]
    return default


def get_all_config() -> Dict[str, Any]:
    conn = _get_conn()
    rows = conn.execute("SELECT config_key, config_value FROM agent_config").fetchall()
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
        conn.execute(
            """INSERT INTO user_profiles
                   (user_id, profile_json, tags_json, msg_count, last_active_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(user_id) DO UPDATE SET
                   profile_json=excluded.profile_json,
                   tags_json=excluded.tags_json,
                   msg_count=excluded.msg_count,
                   last_active_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP""",
            (user_id, json.dumps(profile or {}, ensure_ascii=False),
             json.dumps(tags or [], ensure_ascii=False), int(msg_count or 0))
        )
        conn.commit()
        conn.close()


def get_user_profile(user_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return _parse_profile_row(_row_to_dict(row))


def delete_user_profile(user_id: int) -> int:
    """删除画像，返回受影响行数（0 = 本来就没有）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (user_id,))
        n = cur.rowcount
        conn.commit()
        conn.close()
    return int(n or 0)


def delete_user_messages(user_id: int) -> int:
    """删除某用户在 chat_messages 的全部消息（「清除记忆」时随画像一起清）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM chat_messages WHERE user_id = ?", (user_id,))
        n = cur.rowcount
        conn.commit()
        conn.close()
    return int(n or 0)


def list_user_profiles(limit: int = 20, offset: int = 0) -> List[Dict]:
    """按最近活跃倒序分页（商家端画像看板）"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM user_profiles
           ORDER BY last_active_at DESC, id DESC LIMIT ? OFFSET ?""",
        (int(limit), int(offset))
    ).fetchall()
    conn.close()
    return [r for r in (_parse_profile_row(_row_to_dict(x)) for x in rows) if r]


def count_user_profiles() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM user_profiles").fetchone()
    conn.close()
    return int(row["c"] or 0)


# ============================================================
# 7.2 画像版本（审计轨迹）—— 只 INSERT，不改不删
# ------------------------------------------------------------
# 业务逻辑一律走 services/user_profile.py（_record_version / redact_version）。
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
        cur = conn.execute(
            """INSERT INTO user_profile_versions
                   (user_id, rev, op, before_json, after_json, sha256, actor)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(user_id), int(rev or 0), str(op or "modified"),
             before_json, after_json, digest, str(actor or "system")))
        lid = cur.lastrowid
        conn.commit()
        conn.close()
    return int(lid or 0)


def list_profile_versions(user_id: int, limit: int = 50) -> List[Dict]:
    """某用户的版本轨迹（倒序）。已 redact 的记录内容为 None，但行还在。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM user_profile_versions WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (int(user_id), int(limit))).fetchall()
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
        cur = conn.execute(
            """UPDATE user_profile_versions
                  SET before_json = NULL, after_json = NULL, sha256 = '',
                      redacted_at = CURRENT_TIMESTAMP, redacted_by = ?
                WHERE id = ?""",
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
        cur = conn.execute(
            "INSERT INTO operation_logs (user_id, action, target, detail, ip_address) VALUES (?, ?, ?, ?, ?)",
            (user_id, action, target, detail_json, ip_address)
        )
        lid = cur.lastrowid
        conn.commit()
        conn.close()
    return lid


def list_operation_logs(limit: int = 100, user_id: Optional[int] = None) -> List[Dict]:
    conn = _get_conn()
    if user_id:
        rows = conn.execute(
            "SELECT * FROM operation_logs WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM operation_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return _rows_to_list(rows)


# ============================================================
# 统计辅助
# ============================================================
def get_stats() -> Dict:
    """获取数据库整体统计（仪表盘用）"""
    conn = _get_conn()
    stats = {}
    stats["total_users"] = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    stats["total_sessions"] = conn.execute("SELECT COUNT(*) as c FROM chat_sessions").fetchone()["c"]
    stats["total_messages"] = conn.execute("SELECT COUNT(*) as c FROM chat_messages").fetchone()["c"]
    stats["total_documents"] = conn.execute("SELECT COUNT(*) as c FROM documents").fetchone()["c"]
    stats["total_chunks"] = conn.execute("SELECT COUNT(*) as c FROM document_chunks").fetchone()["c"]
    stats["total_feedback"] = conn.execute("SELECT COUNT(*) as c FROM feedback").fetchone()["c"]

    # 意图分布
    intent_rows = conn.execute(
        "SELECT intent, COUNT(*) as cnt FROM chat_messages WHERE role='user' GROUP BY intent"
    ).fetchall()
    stats["intent_distribution"] = {r["intent"]: r["cnt"] for r in intent_rows}

    # 近7天问答量
    daily_rows = conn.execute(
        """SELECT DATE(created_at) as day, COUNT(*) as cnt
           FROM chat_messages WHERE role='user' AND created_at >= date('now','-7 days')
           GROUP BY DATE(created_at) ORDER BY day"""
    ).fetchall()
    stats["daily_questions"] = {r["day"]: r["cnt"] for r in daily_rows}

    conn.close()
    return stats


# ============================================================
# 9. 微信用户相关
# ============================================================
def get_user_by_openid(openid: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE wx_openid = ?", (openid,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def create_wechat_user(openid: str, nickname: str = "") -> Optional[Dict]:
    """创建微信小程序用户：无密码登录凭证，username 由 openid 生成，随机串哈希占位。
    username 取 openid 前 24 字符——不同 openid 可能撞同一前缀，撞名时换随机后缀重试。"""
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
                conn.execute(
                    "INSERT INTO users (username, password_hash, nickname, wx_openid) VALUES (?, ?, ?, ?)",
                    (username, hash_password(secrets.token_urlsafe(24)),
                     nickname or f"游客{openid[-6:]}", openid)
                )
                conn.commit()
                conn.close()
                return get_user_by_openid(openid)
            except sqlite3.IntegrityError:
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
        conn.execute("UPDATE users SET wx_openid = ? WHERE id = ?", (openid, user_id))
        conn.commit()
        conn.close()
    return True


# ============================================================
# 10. 卡产品相关
# ============================================================
def list_cards(status: Optional[str] = None) -> List[Dict]:
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM cards WHERE status = ? ORDER BY sort ASC, id ASC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM cards ORDER BY sort ASC, id ASC").fetchall()
    conn.close()
    return _rows_to_list(rows)


def get_card(card_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def create_card(name: str, price: int, description: str = "", sort: int = 0) -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO cards (name, price, description, sort) VALUES (?, ?, ?, ?)",
                (name, price, description, sort)
            )
            conn.commit()
            conn.close()
            return get_card_by_name(name)
        except sqlite3.IntegrityError:
            conn.close()
            return None  # 同名卡已存在


def get_card_by_name(name: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM cards WHERE name = ?", (name,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_card(card_id: int, **kwargs) -> Optional[Dict]:
    allowed_fields = {"name", "price", "description", "status", "sort"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}
    if not updates:
        return get_card(card_id)
    with _lock:
        conn = _get_conn()
        try:
            set_clause = ", ".join([f"{k} = ?" for k in updates])
            values = list(updates.values()) + [card_id]
            conn.execute(f"UPDATE cards SET {set_clause} WHERE id = ?", values)
            conn.commit()
            conn.close()
            return get_card(card_id)
        except sqlite3.IntegrityError:
            conn.close()
            return None  # 改名撞车


def delete_card(card_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM cards WHERE id = ?", (card_id,))
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
            conn.execute(
                """INSERT INTO card_orders (order_no, user_id, card_id, card_name, amount, openid)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_no, user_id, card_id, card_name, amount, openid)
            )
            conn.commit()
            conn.close()
            return get_card_order_by_no(order_no)
        except sqlite3.IntegrityError:
            conn.close()
            return get_card_order_by_no(order_no)


def get_card_order_by_no(order_no: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM card_orders WHERE order_no = ?", (order_no,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def update_card_order_status(order_no: str, status: str, transaction_id: str = "") -> Optional[Dict]:
    with _lock:
        conn = _get_conn()
        if status == "paid":
            conn.execute(
                """UPDATE card_orders SET status = ?, transaction_id = ?,
                   paid_at = CURRENT_TIMESTAMP WHERE order_no = ?""",
                (status, transaction_id, order_no)
            )
        else:
            conn.execute(
                "UPDATE card_orders SET status = ?, transaction_id = ? WHERE order_no = ?",
                (status, transaction_id, order_no)
            )
        conn.commit()
        conn.close()
    return get_card_order_by_no(order_no)


def list_user_orders(user_id: int, limit: int = 50) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM card_orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def add_user_card(user_id: Optional[int], card_id: int, card_name: str, order_no: str = "",
                  coins: Optional[int] = None, kind: str = "member") -> int:
    """发卡：支付成功后写入用户持卡记录（coins = 发卡面值，供商家调整/余额统计）。
    kind: member=会员充值卡（可生效/退卡）；small=小额充币卡（直接进余币、无使用/退卡）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO user_cards (user_id, card_id, card_name, order_no, coins, kind) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, card_id, card_name, order_no, coins, kind)
        )
        uc_id = cur.lastrowid
        conn.commit()
        conn.close()
    return uc_id


def list_user_cards(user_id: int) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM user_cards WHERE user_id = ? AND status = 'active' ORDER BY id DESC",
        (user_id,)
    ).fetchall()
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
        where.append("o.status = ?")
        params.append(status)
    if keyword:
        like = f"%{keyword}%"
        where.append("(o.order_no LIKE ? OR o.card_name LIKE ? OR u.nickname LIKE ? OR u.username LIKE ?)")
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    rows = conn.execute(
        f"""SELECT o.*, u.username, u.nickname,
                   uc.id AS uc_id, uc.status AS uc_status, uc.coins AS uc_coins
            FROM card_orders o
            LEFT JOIN users u ON u.id = o.user_id
            LEFT JOIN user_cards uc ON uc.order_no = o.order_no
            {where_sql}
            ORDER BY o.id DESC LIMIT ? OFFSET ?""",
        params + [page_size, offset]
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_all_orders(status: str = "", keyword: str = "") -> int:
    where, params = [], []
    if status:
        where.append("o.status = ?")
        params.append(status)
    if keyword:
        like = f"%{keyword}%"
        where.append("(o.order_no LIKE ? OR o.card_name LIKE ? OR u.nickname LIKE ? OR u.username LIKE ?)")
        params.extend([like, like, like, like])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    conn = _get_conn()
    row = conn.execute(
        f"""SELECT COUNT(*) AS cnt FROM card_orders o
            LEFT JOIN users u ON u.id = o.user_id {where_sql}""",
        params
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_order_stats() -> Dict:
    """账单统计：累计收款/退款、订单数、今日收款"""
    conn = _get_conn()
    row = conn.execute(
        """SELECT
            COALESCE(SUM(CASE WHEN status = 'paid' THEN amount END), 0) AS paid_amount,
            COALESCE(SUM(CASE WHEN status = 'refunded' THEN amount END), 0) AS refunded_amount,
            COALESCE(SUM(CASE WHEN status = 'paid' AND DATE(paid_at) = DATE('now', 'localtime') THEN amount END), 0) AS today_paid_amount,
            SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN status = 'refunded' THEN 1 ELSE 0 END) AS refunded_count,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count
          FROM card_orders"""
    ).fetchone()
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
        sql += " AND DATE(paid_at) = DATE('now', 'localtime')"
    else:
        sql += " AND paid_at >= ?"
        params.append(_since_str(days - 1)[:10] + " 00:00:00")
    sql += " GROUP BY card_name ORDER BY amount DESC"
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return _rows_to_list(rows)


def daily_revenue_trend(days: int = 7) -> List[Dict]:
    """按日收款趋势（近 N 天，仅已支付，仅返回有数据的日期，接口层补零对齐）"""
    since = _since_str(int(days or 7) - 1)[:10] + " 00:00:00"
    conn = _get_conn()
    rows = conn.execute(
        """SELECT DATE(paid_at) AS d, COALESCE(SUM(amount), 0) AS amount, COUNT(*) AS cnt
           FROM card_orders WHERE status = 'paid' AND paid_at >= ?
           GROUP BY DATE(paid_at) ORDER BY d""",
        (since,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def set_user_card_coins(user_card_id: int, coins: int) -> bool:
    """商家调整某张持卡的余币面值（线下补偿/纠错）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE user_cards SET coins = ? WHERE id = ? AND status = 'active'",
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
        conn.execute("UPDATE card_orders SET status = 'refunded' WHERE order_no = ?", (order_no,))
        conn.execute("UPDATE user_cards SET status = 'refunded' WHERE order_no = ?", (order_no,))
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
        cur = conn.execute(
            """INSERT INTO question_logs
               (question, search_query, answer, answer_full_len, references_json,
                elapsed_seconds, session_id, source, answer_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    if keyword:
        like = f"%{keyword}%"
        rows = conn.execute(
            """SELECT * FROM question_logs
               WHERE question LIKE ? OR answer LIKE ?
               ORDER BY id DESC LIMIT ? OFFSET ?""",
            (like, like, page_size, offset)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM question_logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset)
        ).fetchall()
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
    if keyword:
        like = f"%{keyword}%"
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM question_logs WHERE question LIKE ? OR answer LIKE ?",
            (like, like)
        ).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as cnt FROM question_logs").fetchone()
    conn.close()
    return row["cnt"] if row else 0


def list_fallback_questions(days: int = 30, limit: int = 200,
                            markers: Optional[List[str]] = None) -> List[Dict]:
    """列出「模型没有检索到知识库 → 给建议」的问答记录（知识库盲区统计数据源）。

    ⚠️ 判据是**回答类型**，不是 references_json 为空（缓存命中/地图/业务类回答的
    references 同样是空的，但它们都已经答上来了）。
    新记录看 answer_type 列；历史记录（NULL）用答案开头的兜底话术识别。
    排除压测记录（source 为 'probe' 或 'stress'）；归一化去重由 services/kb_agent.py 负责。
    """
    if markers is None:
        try:
            from agents.verification_agent import verification_agent as _va
            markers = [m for m, _t in _va.FALLBACK_MARKERS]
        except Exception:
            markers = ["知识库暂无相关文档", "知识库没有直接回答该问题的条目"]

    like_clause = " OR ".join(["answer LIKE ?"] * len(markers))
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
        sql += " AND created_at >= ?"
        params.append(_since_str(days))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        sql += " AND created_at >= ?"
        params.append(_since_str(days))
    sql += " GROUP BY TRIM(question) ORDER BY cnt DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return _rows_to_list(rows)


def daily_question_trend(days: int = 7) -> List[Dict]:
    """按日问答量趋势（近 N 天，仅返回有数据的日期，接口层补零对齐）"""
    since = _since_str(days - 1)[:10] + " 00:00:00"
    conn = _get_conn()
    rows = conn.execute(
        """SELECT DATE(created_at) AS d, COUNT(*) AS cnt
           FROM question_logs WHERE created_at >= ?
           GROUP BY DATE(created_at) ORDER BY d""",
        (since,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_questions_today() -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM question_logs WHERE DATE(created_at) = DATE('now', 'localtime')"
    ).fetchone()
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
        sql += " AND created_at >= ?"
        params.append(_since_str(days))
    sql += " LIMIT ?"
    params.append(int(limit))
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [(r["question"] or "").strip() for r in rows]


def count_new_users_today() -> int:
    """今日新增注册用户数（仪表盘「今日运营」卡）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE DATE(created_at) = DATE('now', 'localtime')"
    ).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def count_consumptions(status: str = "") -> int:
    """消费单按状态计数（status 空=全部；'unused' = 待核销，仪表盘待办用）"""
    sql = "SELECT COUNT(*) AS cnt FROM consumption_orders WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def feedback_rating_distribution() -> Dict:
    """满意度 1-5 星分布与平均分（仅统计数字星级，兼容历史 like/dislike 数据）"""
    conn = _get_conn()
    rows = conn.execute(
        """SELECT rating, COUNT(*) AS cnt FROM feedback
           WHERE rating IN ('1', '2', '3', '4', '5')
           GROUP BY rating"""
    ).fetchall()
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
    rows = conn.execute(
        "SELECT checkin_date AS d FROM user_checkins WHERE user_id = ? ORDER BY checkin_date DESC",
        (user_id,)
    ).fetchall()
    conn.close()
    return [r["d"] for r in rows]


def sum_checkin_coins(user_id: int) -> int:
    """签到累计奖励币（余币余额 = 持卡面值合计 + 签到奖励合计）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(reward_coins), 0) AS s FROM user_checkins WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return int(row["s"] or 0)


def do_checkin(user_id: int, reward_coins: int = 1) -> Dict:
    """每日签到（当日唯一键幂等，重复签不重复发币），返回最新签到状态"""
    from datetime import date
    today = date.today().isoformat()
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO user_checkins (user_id, checkin_date, reward_coins) VALUES (?, ?, ?)",
            (user_id, today, reward_coins)
        )
        signed_now = cur.rowcount > 0
        conn.commit()
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

    ai: 可选，services.vision_service.recognize_lost_photo() 的识别结果；
        传 None/{} 时落空字符串（识别是增强项，失败不阻断报失）。

    status: 失物状态。游客报失传 pending（待管理员审核，通过前不对游客展示）；
            商家登记用默认的 lost（商家即管理员，可信内容直接上架可认领）。
    """
    ai = ai or {}
    feats = ai.get("features") or []
    if isinstance(feats, list):
        feats = "；".join(str(f)[:60] for f in feats[:5])
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO lost_found_items
                   (name, photo_url, source, reporter, status,
                    ai_category, ai_color, ai_brand, ai_features, ai_text)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    row = conn.execute(
        "SELECT * FROM lost_found_items WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_lost_items(status: str = "", source: str = "", page: int = 1,
                    page_size: int = 50, keyword: str = "") -> List[Dict]:
    """失物列表：支持状态/来源筛选 + 名称关键词 + 分页（新->旧）

    keyword 同时匹配 AI 识别字段（与 db_mysql 行为一致）。
    """
    offset = max(0, (page - 1) * page_size)
    sql = "SELECT * FROM lost_found_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if keyword:
        kw = f"%{keyword}%"
        sql += (" AND (name LIKE ? OR ai_category LIKE ? OR ai_color LIKE ?"
                " OR ai_brand LIKE ? OR ai_features LIKE ? OR ai_text LIKE ?)")
        params.extend([kw] * 6)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_lost_items(status: str = "", source: str = "", keyword: str = "") -> int:
    sql = "SELECT COUNT(*) AS cnt FROM lost_found_items WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if source:
        sql += " AND source = ?"
        params.append(source)
    if keyword:
        kw = f"%{keyword}%"
        sql += (" AND (name LIKE ? OR ai_category LIKE ? OR ai_color LIKE ?"
                " OR ai_brand LIKE ? OR ai_features LIKE ? OR ai_text LIKE ?)")
        params.extend([kw] * 6)
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def update_lost_item_status(item_id: int, status: str) -> bool:
    """更新失物状态：lost 待认领 -> claimed 已认领（或反向撤销）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE lost_found_items SET status = ? WHERE id = ?",
            (status, item_id)
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


def delete_lost_item(item_id: int) -> bool:
    """删除失物记录（同时清理照片文件由 API 层负责）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "DELETE FROM lost_found_items WHERE id = ?", (item_id,)
        )
        conn.commit()
        ok = cur.rowcount > 0
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
        cur = conn.execute(
            "INSERT INTO park_notices (kind, title, content, status, sort) VALUES (?, ?, ?, ?, ?)",
            (kind, title.strip(), (content or "").strip(), status or "on", sort)
        )
        notice_id = cur.lastrowid
        conn.commit()
        conn.close()
    return get_park_notice(notice_id)


def get_park_notice(notice_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM park_notices WHERE id = ?", (notice_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_park_notices(kind: str = "", status: str = "on",
                      page: int = 1, page_size: int = 100) -> List[Dict]:
    """公告/热点列表：kind 空为全部；status 空=全部（管理端），'on'=仅上架（小程序）"""
    offset = max(0, (page - 1) * page_size)
    sql = "SELECT * FROM park_notices WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY sort ASC, id DESC LIMIT ? OFFSET ?"
    params.extend([page_size, offset])
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_park_notices(kind: str = "", status: str = "") -> int:
    sql = "SELECT COUNT(*) AS cnt FROM park_notices WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if status:
        sql += " AND status = ?"
        params.append(status)
    conn = _get_conn()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row["cnt"] if row else 0


def update_park_notice(notice_id: int, **fields) -> bool:
    """更新公告/热点：仅允许 title / content / status / sort / kind 字段"""
    allowed = {k: v for k, v in fields.items()
               if k in {"title", "content", "status", "sort", "kind"} and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with _lock:
        conn = _get_conn()
        cur = conn.execute(f"UPDATE park_notices SET {sets} WHERE id = ?",
                           (*allowed.values(), notice_id))
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


def delete_park_notice(notice_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM park_notices WHERE id = ?", (notice_id,))
        conn.commit()
        ok = cur.rowcount > 0
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
        cur = conn.execute(
            "INSERT INTO user_feedback (user_id, nickname, content) VALUES (?, ?, ?)",
            (user_id, (nickname or "").strip(), content.strip())
        )
        fid = cur.lastrowid
        conn.commit()
        conn.close()

    conn = _get_conn()
    row = conn.execute("SELECT * FROM user_feedback WHERE id = ?", (fid,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def list_user_feedback(page: int = 1, page_size: int = 50) -> List[Dict]:
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM user_feedback ORDER BY id DESC LIMIT ? OFFSET ?",
        (page_size, offset)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_user_feedback() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM user_feedback").fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 手机号登录 / 绑定
# ============================================================
def get_user_by_phone(phone: str) -> Optional[Dict]:
    """按手机号取账号（必须带 ORDER BY id：同号多行时保证结果稳定，取最早账号）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE phone = ? AND phone != '' ORDER BY id ASC", (phone,)
    ).fetchone()
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
                cur = conn.execute(
                    """INSERT INTO users (username, password_hash, nickname, phone)
                       VALUES (?, ?, ?, ?)""",
                    (username, hash_password(secrets.token_urlsafe(24)),
                     f"用户{phone[-4:]}", phone)
                )
                user_id = cur.lastrowid
                conn.commit()
            except Exception:
                conn.close()
                # 号码唯一约束命中 → 回读该号码已有账号；回读不到才是用户名冲突，换后缀重试
                existing = get_user_by_phone(phone)
                if existing:
                    return existing
                continue
            conn.close()
        return get_user_by_id(user_id)
    return None


# ============================================================
# 手机号 + 密码登录 / 历史账号 / 免密令牌（小程序登录页 v2）
# 与 db_mysql.py 同名同语义，保证 DB_DRIVER 切换时行为一致
# ============================================================
def create_phone_user_with_password(phone: str, password: str,
                                    nickname: str = "") -> Optional[Dict]:
    """手机号 + 密码注册新用户（bcrypt 存密码，pwd_set=1）"""
    import secrets
    from core.auth import hash_password
    for attempt in range(3):
        username = f"ph_{phone}" if attempt == 0 else f"ph_{phone}_{secrets.token_hex(3)}"
        with _lock:
            conn = _get_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO users (username, password_hash, nickname, phone, pwd_set)
                       VALUES (?, ?, ?, ?, 1)""",
                    (username, hash_password(password),
                     nickname or f"用户{phone[-4:]}", phone)
                )
                user_id = cur.lastrowid
                conn.commit()
            except Exception:
                conn.close()
                # 号码唯一约束命中 → 回读该号码已有账号；回读不到才是用户名冲突，换后缀重试
                existing = get_user_by_phone(phone)
                if existing:
                    return existing
                continue
            conn.close()
        return get_user_by_id(user_id)
    return None


def set_user_password(user_id: int, password: str) -> bool:
    """设置/重设登录密码（bcrypt + pwd_set=1），并吊销该用户全部免密令牌"""
    from core.auth import hash_password
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE users SET password_hash = ?, pwd_set = 1 WHERE id = ?",
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
        conn.execute(
            """INSERT INTO mp_login_accounts
                   (device_id, user_id, phone, username, nickname, avatar,
                    login_count, last_login_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
               ON CONFLICT(device_id, user_id) DO UPDATE SET
                   phone = excluded.phone,
                   username = excluded.username,
                   nickname = excluded.nickname,
                   avatar = excluded.avatar,
                   login_count = mp_login_accounts.login_count + 1,
                   last_login_at = CURRENT_TIMESTAMP""",
            (device_id, user["id"], user.get("phone") or "", user.get("username") or "",
             user.get("nickname") or "", user.get("avatar") or "")
        )
        conn.commit()
        conn.close()
    return True


def list_mp_login_accounts(device_id: str) -> List[Dict]:
    """本机历史登录账号（按最近登录倒序）"""
    if not device_id:
        return []
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, device_id, user_id, phone, username, nickname, avatar,
                  login_count, last_login_at
           FROM mp_login_accounts WHERE device_id = ?
           ORDER BY last_login_at DESC, id DESC""",
        (device_id,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def delete_mp_login_account(device_id: str, user_id: int) -> bool:
    """移除本机某条历史账号（同时吊销该设备上该用户的免密令牌）"""
    if not device_id:
        return False
    with _lock:
        conn = _get_conn()
        conn.execute(
            "DELETE FROM mp_login_accounts WHERE device_id = ? AND user_id = ?",
            (device_id, user_id)
        )
        conn.execute(
            "UPDATE mp_remember_tokens SET revoked = 1 WHERE device_id = ? AND user_id = ?",
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
            conn.execute(
                """INSERT INTO mp_remember_tokens
                       (token_hash, user_id, device_id, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (token_hash, user_id, device_id or "", expires_at)
            )
            conn.commit()
        except Exception:
            conn.close()
            return False
        conn.close()
    return True


def get_remember_token(token_hash: str) -> Optional[Dict]:
    """按令牌哈希取记录（未吊销的）"""
    if not token_hash:
        return None
    conn = _get_conn()
    row = conn.execute(
        """SELECT id, user_id, device_id, expires_at, revoked
           FROM mp_remember_tokens WHERE token_hash = ? AND revoked = 0""",
        (token_hash,)
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def revoke_remember_token(token_hash: str) -> bool:
    """吊销单个免密令牌（登出）"""
    if not token_hash:
        return False
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE mp_remember_tokens SET revoked = 1 WHERE token_hash = ?",
            (token_hash,)
        )
        conn.commit()
        conn.close()
    return True


def revoke_user_remember_tokens(user_id: int) -> bool:
    """吊销某用户全部免密令牌（改密后调用）"""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE mp_remember_tokens SET revoked = 1 WHERE user_id = ?",
            (user_id,)
        )
        conn.commit()
        conn.close()
    return True


# ============================================================
# 账号合并（手机号归属冲突解决）
# ------------------------------------------------------------
# 铁律：一个手机号 = 一个账号，且"手机号账号 P"是主账号。
# 空壳（无手机号）的一方并入有手机号的一方，openid 随之改绑。
# 与 db_mysql 同名同语义；SQLite 无 UPDATE IGNORE，逐表 try 跳过唯一键冲突。
# ============================================================
_MERGE_TABLES = (
    "chat_sessions", "chat_messages", "feedback",
    "card_orders", "user_cards", "consumption_orders",
    "user_favorites", "user_checkins", "user_feedback",
)


def merge_user_assets(from_user_id: int, to_user_id: int) -> Dict[str, int]:
    """把 from 账号的业务数据迁移到 to 账号，返回各表迁移行数"""
    if not from_user_id or not to_user_id or from_user_id == to_user_id:
        return {}
    moved: Dict[str, int] = {}
    with _lock:
        conn = _get_conn()
        try:
            row_from = conn.execute(
                "SELECT bonus_spent FROM users WHERE id = ?", (from_user_id,)).fetchone()
            bonus = int((row_from or {})["bonus_spent"] or 0) if row_from else 0

            # 两账号同一天都签到时唯一键会冲突，先删掉 to 侧已有的同日记录
            try:
                conn.execute(
                    """DELETE FROM user_checkins
                       WHERE user_id = ? AND checkin_date IN
                             (SELECT checkin_date FROM user_checkins WHERE user_id = ?)""",
                    (from_user_id, to_user_id)
                )
            except Exception:
                pass

            for table in _MERGE_TABLES:
                try:
                    cur = conn.execute(
                        f"UPDATE {table} SET user_id = ? WHERE user_id = ?",
                        (to_user_id, from_user_id)
                    )
                    moved[table] = cur.rowcount
                except Exception:
                    moved[table] = 0

            if bonus:
                conn.execute(
                    "UPDATE users SET bonus_spent = COALESCE(bonus_spent, 0) + ? WHERE id = ?",
                    (bonus, to_user_id)
                )
                conn.execute("UPDATE users SET bonus_spent = 0 WHERE id = ?", (from_user_id,))
                moved["bonus_spent"] = bonus

            conn.execute("UPDATE users SET phone = '' WHERE id = ?", (from_user_id,))
            conn.execute(
                "UPDATE mp_remember_tokens SET revoked = 1 WHERE user_id = ?", (from_user_id,)
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return moved


def merge_accounts(from_user_id: int, to_user_id: int) -> Dict:
    """把 from（辅助账号）整体并入 to（主账号），返回合并明细。
    除业务数据外还处理登录凭据归属：openid 转移 + 本机历史账号重指 + 手机号置空。"""
    if not from_user_id or not to_user_id or from_user_id == to_user_id:
        return {"moved": {}, "openid_moved": False, "accounts_repointed": 0}

    moved = merge_user_assets(from_user_id, to_user_id)

    openid_moved = False
    repointed = 0
    with _lock:
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT wx_openid FROM users WHERE id = ?", (from_user_id,)).fetchone()
            openid = ((row or {})["wx_openid"] or "").strip() if row else ""
            if openid:
                conn.execute("UPDATE users SET wx_openid = NULL WHERE id = ?", (from_user_id,))
                conn.execute("UPDATE users SET wx_openid = ? WHERE id = ?", (openid, to_user_id))
                openid_moved = True

            devices = [r["device_id"] for r in conn.execute(
                "SELECT device_id FROM mp_login_accounts WHERE user_id = ?", (from_user_id,)
            ).fetchall()]
            for device in devices:
                exists = conn.execute(
                    "SELECT id FROM mp_login_accounts WHERE device_id = ? AND user_id = ?",
                    (device, to_user_id)).fetchone()
                if exists:
                    conn.execute(
                        "DELETE FROM mp_login_accounts WHERE device_id = ? AND user_id = ?",
                        (device, from_user_id))
                else:
                    conn.execute(
                        "UPDATE mp_login_accounts SET user_id = ? WHERE device_id = ? AND user_id = ?",
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
    row = conn.execute(
        "SELECT * FROM user_cards WHERE id = ? AND user_id = ? AND status = 'active'",
        (card_id, user_id)
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def set_user_card_in_use(user_id: int, card_id: int) -> bool:
    """把某张卡标记为生效中（先清掉该用户所有生效标记，保证唯一）"""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE user_cards SET in_use = 0 WHERE user_id = ? AND status = 'active'",
            (user_id,)
        )
        cur = conn.execute(
            "UPDATE user_cards SET in_use = 1 WHERE id = ? AND user_id = ?",
            (card_id, user_id)
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


# ============================================================
# 游乐消费：项目价目 / 多选项目合并扣币 / 核销码 / 消费记录
# ============================================================
def list_park_projects(status: str = "on") -> List[Dict]:
    conn = _get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM park_projects WHERE status = ? ORDER BY sort ASC, id ASC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM park_projects ORDER BY sort ASC, id ASC").fetchall()
    conn.close()
    return _rows_to_list(rows)


def get_park_project(project_id: int) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM park_projects WHERE id = ?", (project_id,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def sum_bonus_used(user_id: int) -> int:
    conn = _get_conn()
    row = conn.execute(
        """SELECT COALESCE(SUM(bonus_coins), 0) AS used FROM consumption_orders
           WHERE user_id = ? AND status != 'refunded'""",
        (user_id,)
    ).fetchone()
    conn.close()
    return int(row["used"]) if row else 0


def bonus_available(user_id: int) -> int:
    from database.db import sum_checkin_coins
    return max(0, sum_checkin_coins(user_id) - sum_bonus_used(user_id))


def consume_coins(user_id: int, total_coins: int) -> Dict:
    """消费扣币事务。扣币优先级：生效中的卡 → 其余卡按购买时间先到先扣 → 签到奖励剩余"""
    with _lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                """SELECT id, coins, in_use FROM user_cards
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY in_use DESC, created_at ASC, id ASC""",
                (user_id,)
            )
            cards = cur.fetchall()
            card_total = sum(int(c["coins"] or 0) for c in cards)
            bonus_avail = bonus_available(user_id)
            if card_total + bonus_avail < total_coins:
                conn.close()
                return {"ok": False, "msg": f"余币不足（可用 {card_total + bonus_avail} 币，需 {total_coins} 币），请先充值"}

            remaining = total_coins
            card_deductions = []
            for c in cards:
                if remaining <= 0:
                    break
                if not c["in_use"]:
                    continue
                take = min(int(c["coins"] or 0), remaining)
                if take > 0:
                    conn.execute("UPDATE user_cards SET coins = coins - ? WHERE id = ?", (take, c["id"]))
                    card_deductions.append({"card_id": c["id"], "coins": take})
                    remaining -= take
            for c in cards:
                if remaining <= 0:
                    break
                if c["in_use"]:
                    continue
                take = min(int(c["coins"] or 0), remaining)
                if take > 0:
                    conn.execute("UPDATE user_cards SET coins = coins - ? WHERE id = ?", (take, c["id"]))
                    card_deductions.append({"card_id": c["id"], "coins": take})
                    remaining -= take
            bonus_used = remaining
            if bonus_used > 0:
                card_deductions.append({"card_id": None, "coins": bonus_used})
            conn.commit()
        except Exception:
            conn.close()
            raise
        conn.close()
    return {"ok": True, "card_deductions": card_deductions, "bonus_used": bonus_used}


def create_consumption_order(user_id: int, order_no: str, verify_code: str,
                             items_json: str, card_coins: int, bonus_coins: int,
                             total_coins: int, items: Optional[List[Dict]] = None) -> Optional[Dict]:
    """落消费订单（扣币事务成功后调用）。

    items 非空时，把该单展开出的券在同一事务里一并写入 consumption_items，
    避免"订单落了、券没落"的半截状态。items 默认 None，老调用方行为不变。
    """
    with _lock:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO consumption_orders
               (order_no, verify_code, user_id, items_json, card_coins, bonus_coins, total_coins)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (order_no, verify_code, user_id, items_json, card_coins, bonus_coins, total_coins)
        )
        if items:
            conn.executemany(
                """INSERT INTO consumption_items
                   (order_no, user_id, project_id, project_name, unit_coins, item_code)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(order_no, user_id, it.get("project_id"), it.get("project_name") or "",
                  int(it.get("unit_coins") or 0), it["item_code"]) for it in items]
            )
        conn.commit()
        conn.close()
    return get_consumption_by_no(order_no)


def get_consumption_by_no(order_no: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM consumption_orders WHERE order_no = ?", (order_no,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def get_consumption_by_code(verify_code: str) -> Optional[Dict]:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM consumption_orders WHERE verify_code = ?", (verify_code,)).fetchone()
    conn.close()
    return _row_to_dict(row)


def mark_consumption_used(order_no: str) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE consumption_orders SET status = 'used' WHERE order_no = ? AND status = 'unused'",
            (order_no,)
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


def list_consumptions_by_user(user_id: int, limit: int = 10, today: bool = False) -> List[Dict]:
    """用户消费记录；today=True 仅返回今日"""
    conn = _get_conn()
    if today:
        rows = conn.execute(
            """SELECT * FROM consumption_orders
               WHERE user_id = ? AND DATE(created_at) = DATE('now', 'localtime')
               ORDER BY id DESC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM consumption_orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def list_all_consumptions(page: int = 1, page_size: int = 20) -> List[Dict]:
    offset = max(0, (page - 1) * page_size)
    conn = _get_conn()
    rows = conn.execute(
        """SELECT co.*, u.nickname, u.phone
           FROM consumption_orders co LEFT JOIN users u ON u.id = co.user_id
           ORDER BY co.id DESC LIMIT ? OFFSET ?""",
        (page_size, offset)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_all_consumptions() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS cnt FROM consumption_orders").fetchone()
    conn.close()
    return row["cnt"] if row else 0


# ============================================================
# 消费券（一单一券包）：一券 = 一项一次，一券一码
# 券级状态独立于订单状态——员工扫一张核销一张，同单其它券不受影响。
# ============================================================
def create_consumption_items(order_no: str, user_id: Optional[int],
                             items: List[Dict]) -> int:
    """批量写入消费券（一券一行），返回写入张数。

    正常下单走 create_consumption_order(items=...) —— 券与订单同一事务落库；
    本函数是给「旧消费单迁移成券」这类一次性脚本用的独立入口。
    """
    if not items:
        return 0
    with _lock:
        conn = _get_conn()
        conn.executemany(
            """INSERT INTO consumption_items
               (order_no, user_id, project_id, project_name, unit_coins,
                item_code, status, used_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(order_no, user_id, it.get("project_id"), it.get("project_name") or "",
              int(it.get("unit_coins") or 0), it["item_code"],
              it.get("status") or "unused", it.get("used_at")) for it in items]
        )
        conn.commit()
        conn.close()
    return len(items)


def get_consumption_item_by_code(item_code: str) -> Optional[Dict]:
    """按券码取单张券（扫码核销、券码查重的入口）"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM consumption_items WHERE item_code = ?", (item_code,)
    ).fetchone()
    conn.close()
    return _row_to_dict(row)


def mark_consumption_item_used(item_code: str) -> bool:
    """扫码核销单张券：unused → used（幂等：已核销/已退款返回 False）"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """UPDATE consumption_items SET status = 'used', used_at = CURRENT_TIMESTAMP
               WHERE item_code = ? AND status = 'unused'""",
            (item_code,)
        )
        conn.commit()
        ok = cur.rowcount > 0
        conn.close()
    return ok


def mark_consumption_items_used_by_order(order_no: str) -> int:
    """整单一次性核销（一家三口一次玩完）：返回本次实际核销的券数"""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """UPDATE consumption_items SET status = 'used', used_at = CURRENT_TIMESTAMP
               WHERE order_no = ? AND status = 'unused'""",
            (order_no,)
        )
        conn.commit()
        n = cur.rowcount
        conn.close()
    return n


def list_consumption_items_by_order(order_no: str) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM consumption_items WHERE order_no = ? ORDER BY id ASC", (order_no,)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_consumption_items_by_order(order_no: str, status: Optional[str] = None) -> int:
    conn = _get_conn()
    if status:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM consumption_items WHERE order_no = ? AND status = ?",
            (order_no, status)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM consumption_items WHERE order_no = ?", (order_no,)
        ).fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def list_consumption_items_by_user(user_id: int, status: Optional[str] = None,
                                   limit: int = 50) -> List[Dict]:
    """我的游园码：status=None 返回全部，待玩的排在前面（同状态按购买顺序）"""
    conn = _get_conn()
    if status:
        rows = conn.execute(
            """SELECT * FROM consumption_items WHERE user_id = ? AND status = ?
               ORDER BY id ASC LIMIT ?""",
            (user_id, status, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM consumption_items WHERE user_id = ?
               ORDER BY (status = 'unused') DESC, id ASC LIMIT ?""",
            (user_id, limit)
        ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def count_consumption_items_by_user(user_id: int, status: Optional[str] = None) -> int:
    conn = _get_conn()
    if status:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM consumption_items WHERE user_id = ? AND status = ?",
            (user_id, status)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM consumption_items WHERE user_id = ?", (user_id,)
        ).fetchone()
    conn.close()
    return int(row["cnt"]) if row else 0


def count_consumption_items_by_orders(order_nos: List[str]) -> Dict[str, Dict]:
    """批量统计多张消费单的券数 → {order_no: {"total": n, "unused": m}}（商家端列表用）"""
    if not order_nos:
        return {}
    placeholders = ", ".join(["?"] * len(order_nos))
    conn = _get_conn()
    rows = conn.execute(
        """SELECT order_no,
                  COUNT(*) AS total,
                  SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS unused
           FROM consumption_items WHERE order_no IN (%s)
           GROUP BY order_no""" % placeholders,
        tuple(order_nos)
    ).fetchall()
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
    placeholders = ", ".join(["?"] * len(order_nos))
    conn = _get_conn()
    rows = conn.execute(
        """SELECT * FROM consumption_items WHERE order_no IN (%s)
           ORDER BY order_no ASC, id ASC""" % placeholders,
        tuple(order_nos)
    ).fetchall()
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
#   语义与 MySQL 侧保持一致；没有券行的历史订单仍按整单全额退。
# ============================================================
def refund_consumption_items(codes: List[str]) -> Dict:
    """按券退款（核心实现）：只处理传入的券码，**已核销(used)的券也能退**。

    与整单退款的差别只在"退哪些券"；钱的算法两边完全一样，所以整单退款也走这里
    （refund_consumption_order 转发），避免两套算术各写一遍后慢慢漂移。

    卡内币/奖励币的拆分用**累计口径**算差量：本批卡内币 =
    card_coins*(已退+本批)//total_coins − card_coins*已退//total_coins，
    否则一张张分多次退时每批各自取整会丢币、总账对不上。
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
            ph = ", ".join(["?"] * len(norm))
            rows = conn.execute(
                "SELECT * FROM consumption_items WHERE item_code IN (%s)" % ph,
                tuple(norm)
            ).fetchall()
            found = {r["item_code"]: r for r in rows}

            # 先分拣：不存在的 / 已退过的直接跳过，剩下的按单分组
            by_order: Dict[str, List[Dict]] = {}
            for c in norm:
                r = found.get(c)
                if not r:
                    skipped.append({"item_code": c, "reason": "券码不存在"})
                elif (r["status"] or "unused") == "refunded":
                    skipped.append({"item_code": c, "reason": "该券已退过款"})
                else:
                    by_order.setdefault(r["order_no"], []).append(r)

            for order_no, its in by_order.items():
                order = conn.execute(
                    "SELECT * FROM consumption_orders WHERE order_no = ?", (order_no,)
                ).fetchone()
                if not order:
                    for r in its:
                        skipped.append({"item_code": r["item_code"], "reason": "消费单不存在"})
                    continue

                code_list = [r["item_code"] for r in its]
                iph = ", ".join(["?"] * len(code_list))
                batch = sum(int(r["unit_coins"] or 0) for r in its)
                if batch <= 0:
                    for r in its:
                        skipped.append({"item_code": r["item_code"], "reason": "该券面额为 0"})
                    continue

                total_coins = int(order["total_coins"] or 0)
                card_coins = int(order["card_coins"] or 0)

                # 已退部分的累计面额（不含本批）→ 累计差量，多批次取整不丢币
                row = conn.execute(
                    """SELECT COALESCE(SUM(unit_coins), 0) AS coins
                       FROM consumption_items
                       WHERE order_no = ? AND status = 'refunded'
                         AND item_code NOT IN (%s)""" % iph,
                    tuple([order_no] + code_list)
                ).fetchone()
                before = int(row["coins"] or 0)
                after = before + batch
                if total_coins > 0:
                    card_part = (card_coins * after // total_coins) - (card_coins * before // total_coins)
                else:
                    card_part = 0
                card_part = max(0, min(card_part, batch))
                bonus_part = batch - card_part

                if card_part > 0:
                    cards = conn.execute(
                        """SELECT id FROM user_cards
                           WHERE user_id = ? AND status = 'active'
                           ORDER BY in_use DESC, created_at ASC, id ASC""",
                        (order["user_id"],)
                    ).fetchall()
                    if cards:
                        left = card_part
                        even = card_part // len(cards)
                        for i, c in enumerate(cards):
                            take = even if i < len(cards) - 1 else left
                            if take > 0:
                                conn.execute(
                                    "UPDATE user_cards SET coins = coins + ? WHERE id = ?",
                                    (take, c["id"])
                                )
                                left -= take
                    else:
                        # 无生效卡：卡内币转回签到奖励池（减少该单的 bonus 占用）
                        bonus_part += card_part

                if bonus_part > 0:
                    conn.execute(
                        "UPDATE consumption_orders SET bonus_coins = MAX(bonus_coins - ?, 0) WHERE order_no = ?",
                        (bonus_part, order_no)
                    )

                # 不管 unused 还是 used，统一置 refunded——现场退的是"这一项不玩了"
                conn.execute(
                    "UPDATE consumption_items SET status = 'refunded' WHERE item_code IN (%s)" % iph,
                    tuple(code_list)
                )

                # 订单状态按券的实际分布重算：
                #   还有待玩 → unused；待玩清零但仍有已核销 → used；全退 → refunded
                st = conn.execute(
                    """SELECT SUM(CASE WHEN status = 'unused' THEN 1 ELSE 0 END) AS u,
                              SUM(CASE WHEN status = 'used'   THEN 1 ELSE 0 END) AS s
                       FROM consumption_items WHERE order_no = ?""",
                    (order_no,)
                ).fetchone()
                u = int(st["u"] or 0) if st else 0
                s = int(st["s"] or 0) if st else 0
                new_status = "refunded" if (u == 0 and s == 0) else ("used" if u == 0 else "unused")
                conn.execute(
                    "UPDATE consumption_orders SET status = ? WHERE order_no = ?",
                    (new_status, order_no)
                )

                total_refund += batch
                for r in its:
                    refunded_items.append({
                        "item_code": r["item_code"],
                        "order_no": order_no,
                        "project_name": r["project_name"] or "",
                        "unit_coins": int(r["unit_coins"] or 0),
                    })
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
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
        order = conn.execute(
            "SELECT * FROM consumption_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if not order:
            conn.close()
            return {"ok": False, "msg": "消费单不存在"}
        if order["status"] == "refunded":
            conn.close()
            return {"ok": False, "msg": "该消费单已退款，请勿重复操作"}

        total_coins = int(order["total_coins"] or 0)
        card_coins = int(order["card_coins"] or 0)
        user_id = order["user_id"]
        refunded_coins = total_coins

        # 按订单原来的扣币比例，把退款额拆成卡内币 / 签到奖励币
        card_part = (card_coins * refunded_coins // total_coins) if total_coins > 0 else 0
        bonus_part = refunded_coins - card_part

        if card_part > 0:
            cards = conn.execute(
                """SELECT id FROM user_cards
                   WHERE user_id = ? AND status = 'active'
                   ORDER BY in_use DESC, created_at ASC, id ASC""",
                (user_id,)
            ).fetchall()
            if cards:
                left = card_part
                even = card_part // len(cards)
                for i, c in enumerate(cards):
                    take = even if i < len(cards) - 1 else left
                    conn.execute(
                        "UPDATE user_cards SET coins = coins + ? WHERE id = ?",
                        (take, c["id"])
                    )
                    left -= take
            else:
                # 无生效卡：卡内币转回签到奖励池（减少该单的 bonus 占用）
                bonus_part += card_part

        if bonus_part > 0:
            conn.execute(
                "UPDATE consumption_orders SET bonus_coins = MAX(bonus_coins - ?, 0) WHERE order_no = ?",
                (bonus_part, order_no)
            )

        conn.execute(
            "UPDATE consumption_orders SET status = 'refunded' WHERE order_no = ?", (order_no,)
        )
        conn.commit()
        conn.close()

    order = get_consumption_by_no(order_no)
    return {"ok": True, "msg": f"退款成功，{refunded_coins} 币已退回用户的卡", "data": order}


# ============================================================
# 热点问题（近 7 天高频问题 Top6 → 小程序快捷提问按钮）
# ============================================================
def hot_question_candidates(days: int = 7) -> List[Dict]:
    """近 N 天问答原始分组数据（供 services/hot_questions.py 同义聚类）"""
    conn = _get_conn()
    sql = """SELECT TRIM(question) AS question,
                    COALESCE(NULLIF(TRIM(search_query), ''), TRIM(question)) AS norm_query,
                    COUNT(*) AS cnt
             FROM question_logs
             WHERE question IS NOT NULL AND TRIM(question) != ''"""
    params: list = []
    if days:
        sql += " AND created_at >= ?"
        params.append(_since_str(days))
    sql += " GROUP BY TRIM(question), COALESCE(NULLIF(TRIM(search_query), ''), TRIM(question))"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return _rows_to_list(rows)


def replace_hot_questions(items: List[Dict], days: int = 7) -> int:
    """整批替换热点问题（7 天刷新一次）"""
    from datetime import datetime, timedelta
    now = datetime.now()
    win_start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    win_end = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    conn.execute("DELETE FROM hot_questions")
    for i, it in enumerate(items):
        conn.execute(
            """INSERT INTO hot_questions
               (question, label, icon, hit_count, raw_count,
                window_start, window_end, source, sort, refreshed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (it.get("question") or "")[:255],
                (it.get("label") or "")[:16],
                (it.get("icon") or "")[:8],
                int(it.get("hit_count") or 0),
                int(it.get("raw_count") or 0),
                win_start, win_end,
                it.get("source") or "auto",
                i, win_end,
            )
        )
    conn.commit()
    conn.close()
    return len(items)


def list_hot_questions(limit: int = 6) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, question, label, icon, hit_count, raw_count,
                  window_start, window_end, source, sort, refreshed_at
           FROM hot_questions ORDER BY sort ASC, id ASC LIMIT ?""",
        (int(limit),)
    ).fetchall()
    conn.close()
    return _rows_to_list(rows)


def hot_questions_refreshed_at():
    conn = _get_conn()
    row = conn.execute("SELECT MAX(refreshed_at) AS ts FROM hot_questions").fetchone()
    conn.close()
    return (dict(row) if row else {}).get("ts")


# ============================================================
# 用户收藏
# ============================================================
def add_favorite(user_id: int, question: str, answer: str, session_id: str = "",
                 images: List[str] = None) -> int:
    import json as _json
    images_json = _json.dumps([u for u in (images or []) if u], ensure_ascii=False)
    conn = _get_conn()
    row = conn.execute(
        "SELECT id FROM user_favorites WHERE user_id = ? AND question = ? LIMIT 1",
        (user_id, (question or "")[:2000])
    ).fetchone()
    if row:
        conn.execute(
            """UPDATE user_favorites SET answer = ?, session_id = ?, images_json = ?,
               created_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (answer or "", session_id or "", images_json, row["id"])
        )
        fav_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO user_favorites (user_id, session_id, question, answer, images_json)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, session_id or "", (question or "")[:2000], answer or "", images_json)
        )
        fav_id = cur.lastrowid
    conn.commit()
    conn.close()
    return int(fav_id)


def _parse_images(raw) -> List[str]:
    """images_json 文本 → 图片地址列表（容错：NULL/非法 JSON 返回空列表）"""
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    try:
        import json as _json
        val = _json.loads(raw)
        return [str(x) for x in val if x] if isinstance(val, list) else []
    except Exception:
        return []


def list_favorites(user_id: int, limit: int = 50, offset: int = 0) -> List[Dict]:
    conn = _get_conn()
    rows = conn.execute(
        """SELECT id, question, answer, images_json, session_id, created_at
           FROM user_favorites WHERE user_id = ?
           ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
        (user_id, int(limit), int(offset))
    ).fetchall()
    conn.close()
    items = _rows_to_list(rows)
    for it in items:
        it["images"] = _parse_images(it.get("images_json"))
    return items


def count_favorites(user_id: int) -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM user_favorites WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def delete_favorite(fav_id: int, user_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM user_favorites WHERE id = ? AND user_id = ?", (fav_id, user_id))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


# ============================================================
# 响应耗时统计（商家 PC「响应耗时 P50/P95」看板）
# ============================================================
def question_latency_samples(days: int = 7) -> List[float]:
    conn = _get_conn()
    # 排除压测样本（_probe_latency.py 的 'probe' 与 _stress_test.py 的 'stress'）：
    # 压测记录是脚本按固定剧本刷的，混进 P95 会让"真实用户体验"指标失真
    # （同 db_mysql，两边口径必须一致）
    sql = ("SELECT elapsed_seconds AS e FROM question_logs "
           "WHERE elapsed_seconds IS NOT NULL AND elapsed_seconds > 0 "
           "AND COALESCE(source, '') NOT IN ('probe', 'stress')")
    params: list = []
    if days:
        sql += " AND created_at >= ?"
        params.append(_since_str(days))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [float(r["e"]) for r in rows if r["e"] is not None]
