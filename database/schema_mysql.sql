-- ============================================================
-- 游乐场RAG智能知识库问答系统 - MySQL 数据库 Schema
-- 兼容 MySQL 5.7+ / 8.0+
-- 共 8 张业务表
-- 使用方式: mysql -u root -p < schema_mysql.sql
-- ============================================================

-- 创建数据库（如不存在）
CREATE DATABASE IF NOT EXISTS amusement_rag
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE amusement_rag;

-- ============================================================
-- 1. 用户表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    nickname      VARCHAR(64)  DEFAULT '',
    avatar        VARCHAR(512) DEFAULT '',
    role          VARCHAR(20)  DEFAULT 'user',
    phone         VARCHAR(20)  DEFAULT '' COMMENT '手机号（登录/换绑）',
    wx_openid     VARCHAR(64)  DEFAULT NULL COMMENT '微信小程序openid',
    pwd_set       TINYINT      DEFAULT 0 COMMENT '1=用户自设过登录密码 / 0=密码为系统占位随机串',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_username (username),
    UNIQUE KEY uk_wx_openid (wx_openid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ============================================================
-- 2. 会话表
-- ============================================================
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
    KEY idx_user_id (user_id),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话表';

-- ============================================================
-- 3. 问答消息表
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_messages (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    session_id    VARCHAR(128) NOT NULL,
    user_id       INT UNSIGNED NULL,
    role          VARCHAR(20)  NOT NULL DEFAULT 'user',
    question      TEXT         NULL,
    answer        TEXT         NULL,
    content       TEXT         NULL,
    ref_docs      TEXT,
    search_query  VARCHAR(512) DEFAULT '',
    intent        VARCHAR(50)  DEFAULT '其他',
    elapsed_ms    FLOAT        DEFAULT 0,
    ip_address    VARCHAR(64)  DEFAULT '',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_session_id (session_id),
    KEY idx_user_id (user_id),
    KEY idx_created_at (created_at),
    CONSTRAINT fk_messages_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='问答消息表';

-- ============================================================
-- 4. 文档元数据表
-- ============================================================
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
    KEY idx_status (status),
    CONSTRAINT fk_docs_user FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='文档元数据表';

-- ============================================================
-- 5. 文档切片表
-- ============================================================
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
    KEY idx_document_id (document_id),
    CONSTRAINT fk_chunks_doc FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='文档切片表';

-- ============================================================
-- 6. 用户反馈表
-- ============================================================
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
    KEY idx_user_id (user_id),
    CONSTRAINT fk_feedback_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户反馈表';

-- ============================================================
-- 7. Agent运行配置持久化表
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_config (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    config_key    VARCHAR(128) NOT NULL,
    config_value  TEXT         NOT NULL,
    description   VARCHAR(512) DEFAULT '',
    updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_config_key (config_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Agent配置持久化表';

-- ============================================================
-- 8. 操作日志表
-- ============================================================
CREATE TABLE IF NOT EXISTS operation_logs (
    id            INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id       INT UNSIGNED NULL,
    action        VARCHAR(64)  NOT NULL,
    target        VARCHAR(255) DEFAULT '',
    detail        TEXT,
    ip_address    VARCHAR(64)  DEFAULT '',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_user_id (user_id),
    KEY idx_action (action),
    CONSTRAINT fk_oplogs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='操作日志表';

-- ============================================================
-- 默认管理员账号 (admin / admin123)
-- 密码哈希由应用启动时通过 bcrypt 生成并写入
-- 此处仅创建占位，实际密码由 db_mysql.py init_db() 写入
-- ============================================================

-- ============================================================
-- 9. 卡产品表（价格权威来源，管理端维护；LLM 只推荐 card_id，金额以此表为准）
-- ============================================================
CREATE TABLE IF NOT EXISTS cards (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)  NOT NULL COMMENT '卡名称（AI回答匹配用）',
    price       INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '价格，单位：分',
    description VARCHAR(512) DEFAULT '' COMMENT '权益说明',
    status      VARCHAR(20)  DEFAULT 'on_sale' COMMENT 'on_sale在售 / off_sale下架',
    sort        INT          DEFAULT 0 COMMENT '展示排序，越小越靠前',
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_card_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='卡产品表';

-- ============================================================
-- 10. 办卡订单表（幂等键 order_no；金额在创建时锁定）
-- ============================================================
CREATE TABLE IF NOT EXISTS card_orders (
    id             INT UNSIGNED NOT NULL AUTO_INCREMENT,
    order_no       VARCHAR(32)  NOT NULL COMMENT '业务订单号（幂等键）',
    user_id        INT UNSIGNED NULL,
    card_id        INT UNSIGNED NOT NULL,
    card_name      VARCHAR(64)  DEFAULT '',
    amount         INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '实付金额，单位：分',
    openid         VARCHAR(64)  DEFAULT '' COMMENT '支付者小程序openid',
    status         VARCHAR(20)  DEFAULT 'pending' COMMENT 'pending待支付 / paid已支付 / closed已关闭',
    transaction_id VARCHAR(64)  DEFAULT '' COMMENT '微信支付单号',
    created_at     DATETIME     DEFAULT CURRENT_TIMESTAMP,
    paid_at        DATETIME     NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_order_no (order_no),
    KEY idx_orders_user (user_id),
    KEY idx_orders_status (status),
    CONSTRAINT fk_orders_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_orders_card FOREIGN KEY (card_id) REFERENCES cards(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='办卡订单表';

-- ============================================================
-- 11. 用户持卡表（支付回调成功后发卡写入）
-- ============================================================
CREATE TABLE IF NOT EXISTS user_cards (
    id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id    INT UNSIGNED NULL,
    card_id    INT UNSIGNED NOT NULL,
    card_name  VARCHAR(64)  DEFAULT '',
    order_no   VARCHAR(32)  DEFAULT '',
    coins      INT UNSIGNED NULL COMMENT '剩余币数（NULL 时按卡面值解析）',
    in_use     TINYINT      DEFAULT 0 COMMENT '1=生效中（会员码按它出码；仅 member 卡）',
    kind       VARCHAR(10)  NOT NULL DEFAULT 'member' COMMENT 'member会员充值卡(可生效/退卡) / small小额充币卡(直接入余币)',
    status     VARCHAR(20)  DEFAULT 'active' COMMENT 'active生效 / refunded已退卡 / expired过期',
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_ucards_user (user_id),
    CONSTRAINT fk_ucards_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户持卡表';

-- ============================================================
-- 12. 问答日志表（管理端问答记录页数据源；Redis 仅保留热数据）
-- ============================================================
CREATE TABLE IF NOT EXISTS question_logs (
    id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
    question         TEXT         NULL,
    search_query     VARCHAR(512) DEFAULT '',
    answer           TEXT         NULL,
    answer_full_len  INT          DEFAULT 0,
    references_json  TEXT,
    elapsed_seconds  FLOAT        DEFAULT 0,
    session_id       VARCHAR(128) DEFAULT '',
    source           VARCHAR(20)  DEFAULT 'web' COMMENT 'web / miniprogram',
    created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_qlogs_created (created_at),
    KEY idx_qlogs_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='问答日志表';

-- ============================================================
-- 13. 失物招领表（游客报失 + 商家登记统一管理）
-- ============================================================
CREATE TABLE IF NOT EXISTS lost_found_items (
    id         INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name       VARCHAR(128) NOT NULL COMMENT '失物名称',
    photo_url  VARCHAR(512) DEFAULT '' COMMENT '照片路径(/static/lost_found/xxx.jpg)',
    status     VARCHAR(20)  DEFAULT 'lost' COMMENT 'pending待审核 / lost待认领 / claimed已认领 / rejected已驳回',
    source     VARCHAR(20)  DEFAULT 'user' COMMENT 'user游客报失 / merchant商家登记',
    reporter   VARCHAR(64)  DEFAULT '' COMMENT '报失人昵称/联系方式',
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_lf_status (status),
    KEY idx_lf_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='失物招领表';

-- ============================================================
-- 已有库升级脚本（新建库无需执行；db_mysql.init_db() 也会自动幂等补齐）
-- ALTER TABLE users ADD COLUMN wx_openid VARCHAR(64) DEFAULT NULL;
-- ALTER TABLE users ADD UNIQUE KEY uk_wx_openid (wx_openid);
-- ALTER TABLE users ADD COLUMN phone VARCHAR(20) DEFAULT '';
-- ALTER TABLE users ADD COLUMN pwd_set TINYINT DEFAULT 0;
-- ============================================================

-- ============================================================
-- 17. 小程序历史登录账号（设备维度）
-- 用途：下次打开小程序直接展示「上次登录的账号（头像+昵称+手机尾号）」，
--       点击即凭 mp_remember_tokens 的记忆令牌免密进入；退出登录后历史仍保留。
-- ============================================================
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
  COMMENT='小程序历史登录账号（设备维度）';

-- ============================================================
-- 18. 小程序免密登录令牌（"记住我 / 点头像直接登录"）
-- 密码只存 bcrypt 不可逆哈希，因此免密登录不能靠存密码：
-- 登录时签发随机令牌给小程序本地保存，库里只存 sha256(令牌)，30 天有效、可吊销。
-- ============================================================
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
  COMMENT='小程序免密登录令牌';

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

-- ============================================================
-- 每日签到表（我的页签到送币，奖励计入余币余额）
-- ============================================================
CREATE TABLE IF NOT EXISTS user_checkins (
    id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id      INT UNSIGNED NULL,
    checkin_date DATE         NOT NULL,
    reward_coins INT          NOT NULL DEFAULT 1,
    created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_user_date (user_id, checkin_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- 种子数据：默认管理员 admin / admin123（首次登录后请尽快修改密码）
-- ============================================================
INSERT INTO users (username, password_hash, nickname, role)
VALUES ('admin', '$2b$12$r5VAkvMhCjeMfPtCNKsSDO46Tm9sFeAGRNVafzPd.3YhOiKE5b3kK', '管理员', 'admin');

-- ============================================================
-- 种子数据：六档卡产品（id 1-6 与小程序本地兜底档位一致；价格为分）
-- ============================================================
INSERT INTO cards (id, name, price, description, status, sort) VALUES
(1, '10币',        1000,   '10个游戏币',        'on_sale', 1),
(2, '20币',        2000,   '20个游戏币',        'on_sale', 2),
(3, '30币',        3000,   '30个游戏币',        'on_sale', 3),
(4, '200元=320币', 20000,  '充值200元得320币',  'on_sale', 4),
(5, '500元=1000币', 50000, '充值500元得1000币', 'on_sale', 5),
(6, '1000元=2500币', 100000, '充值1000元得2500币', 'on_sale', 6);

-- ---------- 游乐项目价目表（首页游乐消费数据源） ----------
CREATE TABLE IF NOT EXISTS park_projects (
    id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
    name        VARCHAR(64)  NOT NULL,
    price_coins INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '单价（币）',
    unit_desc   VARCHAR(32)  DEFAULT '' COMMENT '单位说明（5分钟/15圈/80发）',
    status      VARCHAR(20)  DEFAULT 'on' COMMENT 'on上架 / off下架',
    sort        INT          DEFAULT 0,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- 消费订单表（多选项目合并扣币，生成核销码） ----------
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- 消费券明细表（一券 = 一项一次，一券一码） ----------
-- consumption_orders 仍是订单/金额/整单状态的真源；本表拆出券级状态，
-- 让员工扫一张核销一张，也让用户能一眼看到"还剩哪几个没玩"。
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;


-- ---------- 用户画像表（user 级「AI 记得我」，跟着账号走、换设备不丢） ----------
-- 与 db_mysql.py 的 init_db() 同构；这里补上是为了让手工建库（DBA 走 .sql）与
-- 代码自动建库两条路不漂移 —— 此前两张画像表只在 init_db() 里创建。
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ---------- 画像版本表（审计：只 INSERT，不改不删） ----------
-- 每次写入留一条不可变版本，解决"谁把画像改了/改之前是什么"；
-- 用户「一键清除画像」也先落一条 deleted 版本（保留轨迹，内容可用 redact 抹除）。
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
