# ============================================================
# 数据库模块代理 - 根据 config.DB_DRIVER 自动切换
# DB_DRIVER="mysql"  -> 使用 db_mysql.py (pymysql)
# DB_DRIVER="sqlite" -> 使用 db_sqlite.py (sqlite3)
#
# 所有业务代码统一 from database.db import xxx 即可，
# 无需关心底层驱动。
# ============================================================
from config import DB_DRIVER

if DB_DRIVER == "mysql":
    from database.db_mysql import *  # noqa: F401,F403
    from database.db_mysql import (  # 显式导出，便于 IDE 识别
        init_db, create_user, get_user_by_username, get_user_by_id,
        update_user, delete_user, list_users, search_users, dup_phone_groups, authenticate,
        create_session, get_session, list_sessions, update_session, delete_session,
        add_message, get_messages, get_recent_messages, count_messages,
        upsert_document, get_document, get_document_by_filename, list_documents,
        update_document_status, delete_document,
        add_chunk, get_chunks_by_document, count_chunks,
        add_feedback, list_feedback,
        set_config, get_config, get_all_config,
        add_operation_log, list_operation_logs,
        get_stats,
        # 微信登录
        get_user_by_openid, create_wechat_user, bind_wechat,
        # 手机号登录 / 绑定
        get_user_by_phone, create_phone_user,
        # 手机号 + 密码登录 / 历史账号 / 免密令牌（小程序登录页 v2）
        create_phone_user_with_password, set_user_password,
        upsert_mp_login_account, list_mp_login_accounts, delete_mp_login_account,
        create_remember_token, get_remember_token,
        revoke_remember_token, revoke_user_remember_tokens,
        # 账号合并（手机号归属冲突时把旧账号数据并到当前账号）
        merge_user_assets, merge_accounts,
        # 持卡生效标记
        get_user_card, set_user_card_in_use,
        # 游乐消费
        list_park_projects, get_park_project, consume_coins, bonus_available,
        refund_consumption_order,
        sum_bonus_used, create_consumption_order, get_consumption_by_no,
        get_consumption_by_code, mark_consumption_used, list_consumptions_by_user,
        list_all_consumptions, count_all_consumptions,
        # 消费券（一单一券包）：一券 = 一项一次
        create_consumption_items, get_consumption_item_by_code, mark_consumption_item_used,
        mark_consumption_items_used_by_order, list_consumption_items_by_order,
        count_consumption_items_by_order, list_consumption_items_by_user,
        count_consumption_items_by_user, count_consumption_items_by_orders,
        list_consumption_items_by_orders,
        # 按券退款（核心实现；整单退款也转发到这里）
        refund_consumption_items,
        # 卡产品
        list_cards, get_card, create_card, update_card, delete_card,
        # 订单与持卡
        create_card_order, get_card_order_by_no, update_card_order_status,
        list_user_orders, add_user_card, list_user_cards,
        # 办卡账单管理
        list_all_orders, count_all_orders, get_order_stats,
        set_user_card_coins, refund_order,
        # 营收汇总（经营小助手图表数据源）
        revenue_by_card, daily_revenue_trend,
        # 问答日志
        add_question_log, list_question_logs, count_question_logs,
        # 问答分析统计
        top_questions, daily_question_trend,
        count_questions_today, feedback_rating_distribution,
        list_question_texts, count_new_users_today, count_consumptions,
        # 知识库盲区（知识库智能体）
        list_fallback_questions,
        # 每日签到
        do_checkin, get_checkin_status, sum_checkin_coins,
        # 失物招领
        create_lost_item, get_lost_item, list_lost_items,
        count_lost_items, update_lost_item_status, delete_lost_item,
        # 园区公告 / 今日热点
        create_park_notice, get_park_notice, list_park_notices,
        count_park_notices, update_park_notice, delete_park_notice,
        # 游客意见反馈
        add_user_feedback, list_user_feedback, count_user_feedback,
        # 热点问题（近7天 Top6 → 小程序快捷提问按钮）
        hot_question_candidates, replace_hot_questions, list_hot_questions,
        hot_questions_refreshed_at,
        # 用户收藏（AI 回答「收藏」→ 我的页「我的收藏」）
        add_favorite, list_favorites, count_favorites, delete_favorite,
        # 响应耗时统计（商家 PC 看板）
        question_latency_samples,
        # 用户画像（小程序「AI 记得我」/ 商家端画像看板）
        upsert_user_profile, get_user_profile, delete_user_profile,
        list_user_profiles, count_user_profiles, delete_user_messages,
        # 画像版本轨迹（审计：不可变记录 + 可脱敏）
        add_profile_version, list_profile_versions, redact_profile_version,
    )
else:
    from database.db_sqlite import *  # noqa: F401,F403
    from database.db_sqlite import (
        init_db, create_user, get_user_by_username, get_user_by_id,
        update_user, delete_user, list_users, search_users, dup_phone_groups, authenticate,
        create_session, get_session, list_sessions, update_session, delete_session,
        add_message, get_messages, get_recent_messages, count_messages,
        upsert_document, get_document, get_document_by_filename, list_documents,
        update_document_status, delete_document,
        add_chunk, get_chunks_by_document, count_chunks,
        add_feedback, list_feedback,
        set_config, get_config, get_all_config,
        add_operation_log, list_operation_logs,
        get_stats,
        # 微信登录
        get_user_by_openid, create_wechat_user, bind_wechat,
        # 手机号登录 / 绑定
        get_user_by_phone, create_phone_user,
        # 手机号 + 密码登录 / 历史账号 / 免密令牌（小程序登录页 v2）
        create_phone_user_with_password, set_user_password,
        upsert_mp_login_account, list_mp_login_accounts, delete_mp_login_account,
        create_remember_token, get_remember_token,
        revoke_remember_token, revoke_user_remember_tokens,
        # 账号合并（手机号归属冲突时把旧账号数据并到当前账号）
        merge_user_assets, merge_accounts,
        # 持卡生效标记
        get_user_card, set_user_card_in_use,
        # 游乐消费
        list_park_projects, get_park_project, consume_coins, bonus_available,
        refund_consumption_order,
        sum_bonus_used, create_consumption_order, get_consumption_by_no,
        get_consumption_by_code, mark_consumption_used, list_consumptions_by_user,
        list_all_consumptions, count_all_consumptions,
        # 消费券（一单一券包）：一券 = 一项一次
        create_consumption_items, get_consumption_item_by_code, mark_consumption_item_used,
        mark_consumption_items_used_by_order, list_consumption_items_by_order,
        count_consumption_items_by_order, list_consumption_items_by_user,
        count_consumption_items_by_user, count_consumption_items_by_orders,
        list_consumption_items_by_orders,
        # 按券退款（核心实现；整单退款也转发到这里）
        refund_consumption_items,
        # 卡产品
        list_cards, get_card, create_card, update_card, delete_card,
        # 订单与持卡
        create_card_order, get_card_order_by_no, update_card_order_status,
        list_user_orders, add_user_card, list_user_cards,
        # 办卡账单管理
        list_all_orders, count_all_orders, get_order_stats,
        set_user_card_coins, refund_order,
        # 营收汇总（经营小助手图表数据源）
        revenue_by_card, daily_revenue_trend,
        # 问答日志
        add_question_log, list_question_logs, count_question_logs,
        # 问答分析统计
        top_questions, daily_question_trend,
        count_questions_today, feedback_rating_distribution,
        list_question_texts, count_new_users_today, count_consumptions,
        # 知识库盲区（知识库智能体）
        list_fallback_questions,
        # 每日签到
        do_checkin, get_checkin_status, sum_checkin_coins,
        # 失物招领
        create_lost_item, get_lost_item, list_lost_items,
        count_lost_items, update_lost_item_status, delete_lost_item,
        # 园区公告 / 今日热点
        create_park_notice, get_park_notice, list_park_notices,
        count_park_notices, update_park_notice, delete_park_notice,
        # 游客意见反馈
        add_user_feedback, list_user_feedback, count_user_feedback,
        # 热点问题（近7天 Top6 → 小程序快捷提问按钮）
        hot_question_candidates, replace_hot_questions, list_hot_questions,
        hot_questions_refreshed_at,
        # 用户收藏（AI 回答「收藏」→ 我的页「我的收藏」）
        add_favorite, list_favorites, count_favorites, delete_favorite,
        # 响应耗时统计（商家 PC 看板）
        question_latency_samples,
        # 用户画像（小程序「AI 记得我」/ 商家端画像看板）
        upsert_user_profile, get_user_profile, delete_user_profile,
        list_user_profiles, count_user_profiles, delete_user_messages,
        # 画像版本轨迹（审计：不可变记录 + 可脱敏）
        add_profile_version, list_profile_versions, redact_profile_version,
    )
