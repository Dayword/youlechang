# -*- coding: utf-8 -*-
# ============================================================
# jieba 业务自定义词典（共享模块）
# ------------------------------------------------------------
# 背景：jieba.add_word 修改的是进程级全局词典。
# 此前词典只在 core/bm25_index.py 注册，若 core/cache.py 先于其被加载
# （如仅调用统计接口），关键词提取时业务词会被切碎，导致缓存 key 不一致。
# 抽取为共享模块后，两处统一调用 ensure_business_words()，与加载顺序无关。
# ============================================================
import logging
import threading

import jieba

logger = logging.getLogger(__name__)
_reg_lock = threading.Lock()
_registered = False

# 业务关键词（确保核心业务术语不被错误切分）
BUSINESS_WORDS = [
    # 办卡/充值相关
    "办卡", "储值办卡", "充值办卡", "怎么办卡", "临时充值",
    # 游乐项目
    "旋转木马", "荡秋千", "小型过山车", "浴室大作战", "大炮乐园",
    # 业务术语
    "游戏币", "储值卡", "陪同费", "免费陪同", "陪同入场",
    "设备故障", "人为损坏", "营业时间", "闭园",
    # 通用业务
    "余额不足", "退款规则", "有效期", "跨店用卡",
]


def ensure_business_words() -> None:
    """向 jieba 全局词典注册业务词（幂等，可在任意模块安全调用）"""
    global _registered
    if _registered:
        return
    with _reg_lock:
        if _registered:
            return
        for word in BUSINESS_WORDS:
            jieba.add_word(word, freq=100)
        _registered = True
        logger.debug(f"jieba 业务词典已注册 {len(BUSINESS_WORDS)} 个词")


# 业务词集合：供 cache 关键词提取时保留自定义词性(x)的业务词
BUSINESS_WORDS_SET = set(BUSINESS_WORDS)
