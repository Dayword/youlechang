-- ============================================================
-- 游乐场RAG智能知识库问答系统 - 数据库 Schema
-- 数据库: SQLite (兼容 MySQL 迁移)
-- 共 8 张表
-- ============================================================

-- 1. 用户表
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nickname      TEXT DEFAULT '',
    avatar        TEXT DEFAULT '',
    role          TEXT DEFAULT 'user',          -- admin / user
    wx_openid     TEXT UNIQUE,                   -- 微信小程序openid
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. 会话表
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
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON chat_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_sid  ON chat_sessions(session_id);

-- 3. 问答消息表
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    user_id       INTEGER,
    role          TEXT NOT NULL DEFAULT 'user',   -- user / assistant
    question      TEXT,
    answer        TEXT,
    content       TEXT,
    ref_docs      TEXT DEFAULT '[]',                 -- JSON 引用文档列表
    search_query  TEXT DEFAULT '',
    intent        TEXT DEFAULT '其他',
    elapsed_ms    REAL DEFAULT 0,
    ip_address    TEXT DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_user    ON chat_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON chat_messages(created_at);

-- 4. 文档元数据表
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT UNIQUE NOT NULL,
    original_name TEXT NOT NULL,
    file_size     INTEGER DEFAULT 0,
    file_type     TEXT DEFAULT '',                   -- pdf/txt/docx/doc
    chunk_count   INTEGER DEFAULT 0,
    status        TEXT DEFAULT 'pending',            -- pending/processing/completed/failed
    error_msg     TEXT DEFAULT '',
    uploaded_by   INTEGER,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);

-- 5. 文档切片表
CREATE TABLE IF NOT EXISTS document_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER NOT NULL,
    chunk_index   INTEGER NOT NULL,
    content       TEXT NOT NULL,
    token_count   INTEGER DEFAULT 0,
    milvus_id     TEXT DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
    UNIQUE(document_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON document_chunks(document_id);

-- 6. 用户反馈表
CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    INTEGER,
    session_id    TEXT DEFAULT '',
    user_id       INTEGER,
    rating        TEXT NOT NULL,                     -- like / dislike
    comment       TEXT DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_msg  ON feedback(message_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id);

-- 7. Agent运行配置持久化表
CREATE TABLE IF NOT EXISTS agent_config (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    config_key    TEXT UNIQUE NOT NULL,
    config_value  TEXT NOT NULL,                     -- JSON
    description   TEXT DEFAULT '',
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 8. 操作日志表
CREATE TABLE IF NOT EXISTS operation_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,
    action        TEXT NOT NULL,
    target        TEXT DEFAULT '',
    detail        TEXT DEFAULT '{}',                  -- JSON
    ip_address    TEXT DEFAULT '',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_oplogs_user   ON operation_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_oplogs_action ON operation_logs(action);

-- ============================================================
-- 默认数据: 管理员账号 (admin / admin123)
-- 密码由应用启动时通过 bcrypt 哈希后写入
-- ============================================================

-- 9. 卡产品表（价格权威来源，管理端维护；LLM 只推荐 card_id，金额以此表为准）
CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT UNIQUE NOT NULL,              -- 卡名称（AI回答匹配用）
    price       INTEGER NOT NULL DEFAULT 0,        -- 价格，单位：分
    description TEXT DEFAULT '',                    -- 权益说明
    status      TEXT DEFAULT 'on_sale',             -- on_sale在售 / off_sale下架
    sort        INTEGER DEFAULT 0,                  -- 展示排序，越小越靠前
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 10. 办卡订单表（幂等键 order_no；金额在创建时锁定）
CREATE TABLE IF NOT EXISTS card_orders (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no       TEXT UNIQUE NOT NULL,           -- 业务订单号（幂等键）
    user_id        INTEGER,
    card_id        INTEGER NOT NULL,
    card_name      TEXT DEFAULT '',
    amount         INTEGER NOT NULL DEFAULT 0,     -- 实付金额，单位：分
    openid         TEXT DEFAULT '',                 -- 支付者小程序openid
    status         TEXT DEFAULT 'pending',         -- pending待支付 / paid已支付 / closed已关闭
    transaction_id TEXT DEFAULT '',                 -- 微信支付单号
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at        TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_user   ON card_orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON card_orders(status);

-- 11. 用户持卡表（支付回调成功后发卡写入）
CREATE TABLE IF NOT EXISTS user_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    card_id    INTEGER NOT NULL,
    card_name  TEXT DEFAULT '',
    order_no   TEXT DEFAULT '',
    status     TEXT DEFAULT 'active',              -- active生效 / expired过期
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_ucards_user ON user_cards(user_id);

-- 12. 问答日志表（管理端问答记录页数据源；Redis 仅保留热数据）
CREATE TABLE IF NOT EXISTS question_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question        TEXT,
    search_query    TEXT DEFAULT '',
    answer          TEXT,
    answer_full_len INTEGER DEFAULT 0,
    references_json TEXT DEFAULT '[]',
    elapsed_seconds REAL DEFAULT 0,
    session_id      TEXT DEFAULT '',
    source          TEXT DEFAULT 'web',            -- web / miniprogram
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_qlogs_created ON question_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_qlogs_session ON question_logs(session_id);

-- ============================================================
-- 已有库升级（新建库无需执行）
-- ALTER TABLE users ADD COLUMN wx_openid TEXT;
-- ============================================================

-- ---------- 15. 园区公告 / 今日热点（仅商家 PC 端发布，小程序首页展示） ----------
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- 16. 游客意见反馈（小程序「意见反馈」提交，商家端查看） ----------
CREATE TABLE IF NOT EXISTS user_feedback (
    id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id    INT UNSIGNED NULL COMMENT '微信用户id（登录时自动关联，可空）',
    nickname   VARCHAR(64)  DEFAULT '' COMMENT '游客昵称/联系方式（选填）',
    content    VARCHAR(512) NOT NULL COMMENT '反馈内容',
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_uf_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
