# -*- coding: utf-8 -*-
"""临时脚本：清空检索/答案缓存（修复 rerank 后需清缓存验证，测试完删除）"""
import sys
import io
import redis

from config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 连接参数统一从 config（读取 .env）取，代码里不写死任何凭据
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    password=REDIS_PASSWORD or None,
    db=REDIS_DB,
    decode_responses=True,
)

total = 0
for pattern in ["rag:search:*", "rag:ans:*"]:
    keys = list(r.scan_iter(match=pattern, count=200))
    if keys:
        r.delete(*keys)
        total += len(keys)
    print(f"{pattern}: 清除 {len(keys)} 个")

print(f"共清除 {total} 个缓存key")
