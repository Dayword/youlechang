# ============================================================
# 运营管理后台 API 路由 - /admin/*（需管理员登录）
# ============================================================
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional
from pydantic import BaseModel
import time

from config import DOCS_DIR
from core.auth import get_current_admin

router = APIRouter(prefix="/admin", tags=["运营管理后台"], dependencies=[Depends(get_current_admin)])
logger = logging.getLogger(__name__)


def _intent_distribution_realtime(days: int):
    """意图分布（读取侧实时聚合）：返回 (分布列表, 样本总数)。

    ⚠️ 唯一的正确口径，别再各写一份：
      1. **不能** `GROUP BY question_logs.intent` —— 该列从来没有写入点，全是默认"其他"；
      2. **不能**读 Redis 计数器 `rag:stats:intent:*` —— 1 小时过期，且只有非流式 /chat 计数。
    正确做法：取近 N 天问题文本，用 `query_agent.detect_intent()`（纯关键词、零 LLM）
    现算 —— 对历史数据立即生效、无需回填、规则升级后分布自动跟着变。
    """
    from agents.query_agent import query_agent
    from database.db import list_question_texts

    texts = list_question_texts(days=days)
    counter: dict = {}
    for q in texts:
        it = query_agent.detect_intent(q)
        counter[it] = counter.get(it, 0) + 1
    # 固定 6 类顺序（缺失补 0），便于前端稳定展示
    order = [name for name, _kw in query_agent.INTENT_KEYWORDS] + ["其他"]
    return [{"intent": k, "cnt": counter.get(k, 0)} for k in order], len(texts)


# ---------- 仪表盘数据 ----------
@router.get("/dashboard", summary="运营仪表盘数据", dependencies=[Depends(get_current_admin)])
async def dashboard(days: int = 7):
    """运营仪表盘：核心指标 + 意图分布（实时聚合）+ 知识库盲区 + 今日运营 + 性能 + 待办。

    ⚠️ 两个历史坑，别再踩：
      1. 意图分布**不能**读 Redis 计数器 `rag:stats:intent:*`：
         `incr_counter(ttl=3600)` 只有 1 小时寿命，且只有非流式 `/chat` 在计数，
         小程序实际走的 `/chat/stream` 从不计数 → 饼图长期只有一个"其他"。
      2. 也**不能** `GROUP BY question_logs.intent`：该列从来没有写入点（全是默认值）。
      正确口径已收敛到 `_intent_distribution_realtime()`（近 N 天文本 + 关键词规则现算），
      本接口与 /admin/analytics/overview 共用同一份，避免两边再走岔。
    """
    from datetime import datetime
    from core.cache import redis_cache
    from agents.retrieval_agent import retrieval_agent
    from agents.document_agent import document_agent
    from database.db import (count_question_logs, count_questions_today,
                             count_new_users_today,
                             count_consumptions, count_lost_items, get_order_stats)

    days = min(max(1, days), 60)

    # ---------- 问答量（持久计数，不再用 1 小时过期的 Redis 计数器） ----------
    total_questions = count_question_logs()
    today_questions = count_questions_today()

    # ---------- 意图分布：读取侧实时聚合（口径见 _intent_distribution_realtime） ----------
    intent_distribution, intent_total = _intent_distribution_realtime(days)

    # ---------- 知识库规模 ----------
    index_stats = retrieval_agent.get_index_stats()
    docs, chunk_total, last_updated = [], 0, ""
    try:
        docs = document_agent.list_documents()
        chunk_files = document_agent.list_all_chunk_files()
        chunk_total = sum(int(c.get("total_chunks") or 0) for c in chunk_files)
        stamps = [os.path.getmtime(os.path.join(DOCS_DIR, d))
                  for d in docs if os.path.exists(os.path.join(DOCS_DIR, d))]
        if stamps:
            last_updated = datetime.fromtimestamp(max(stamps)).strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        logger.warning(f"[dashboard] 知识库规模统计失败（忽略）: {e}")

    # ---------- 知识库盲区（复用知识库智能体的判定，只取 Top5 展示） ----------
    try:
        from services.kb_agent import collect_gaps, _type_label
        g = collect_gaps(days=days, limit=5)
        gaps = {
            "raw_total": g.get("raw_total", 0),
            "distinct_total": g.get("distinct_total", 0),
            "by_type": g.get("by_type", {}),
            "top": [{"question": i["question"], "count": i["count"],
                     "fallback_label": _type_label(i.get("fallback_type", ""))}
                    for i in g.get("items", [])],
        }
    except Exception as e:
        logger.warning(f"[dashboard] 盲区统计失败（忽略）: {e}")
        gaps = {"raw_total": 0, "distinct_total": 0, "by_type": {}, "top": []}

    # ---------- 性能 / 今日运营 / 待办 ----------
    latency = question_latency_stats(days)
    try:
        order_stats = get_order_stats() or {}
    except Exception:
        order_stats = {}
    try:
        todo = {
            "pending_orders": int(order_stats.get("pending_count") or 0),
            "unused_consumptions": count_consumptions("unused"),
            "lost_items": count_lost_items("lost"),
        }
    except Exception:
        todo = {"pending_orders": 0, "unused_consumptions": 0, "lost_items": 0}

    return {
        "code": 200,
        "data": {
            "days": days,
            "total_questions": total_questions,
            "today_questions": today_questions,
            "today_new_users": count_new_users_today(),
            "today_paid_amount": round(float(order_stats.get("today_paid_amount") or 0) / 100.0, 2),
            "intent_distribution": intent_distribution,
            "intent_total": intent_total,
            "knowledge_base": {
                "milvus_entities": index_stats.get("milvus_entities", 0),
                "bm25_documents": index_stats.get("bm25_documents", 0),
                "documents": len(docs),
                "chunks": chunk_total,
                "last_updated": last_updated,
            },
            "gaps": gaps,
            "latency": latency,
            "todo": todo,
            "components": {
                "redis": "connected" if redis_cache.available else "disconnected",
            },
        }
    }


@router.get("/questions/recent", summary="最近问答记录", dependencies=[Depends(get_current_admin)])
async def recent_questions(limit: int = 20):
    """获取最近问答记录"""
    from core.cache import redis_cache
    # 从缓存获取统计（实际项目中可从数据库获取）
    return {
        "code": 200,
        "data": {
            "total": redis_cache.get_counter("total_questions"),
            "note": "问答统计数据，详细记录需要连接数据库"
        }
    }


# ---------- 系统状态 ----------
@router.get("/system/status", summary="系统运行状态", dependencies=[Depends(get_current_admin)])
async def system_status():
    """获取系统各组件的运行状态"""
    from core.cache import redis_cache
    from core.vector_store import vector_store

    redis_ok = redis_cache.available
    milvus_ok = False
    try:
        vs = vector_store.get_collection_stats()
        milvus_ok = vs.get("num_entities", -1) >= 0
    except Exception:
        milvus_ok = False

    return {
        "code": 200,
        "data": {
            "redis": "connected" if redis_ok else "disconnected",
            "milvus": "connected" if milvus_ok else "disconnected",
            "server_uptime": round(time.time(), 0),
        }
    }


# ---------- 缓存管理 ----------
@router.post("/cache/clear", summary="清除所有缓存", dependencies=[Depends(get_current_admin)])
async def clear_cache():
    from core.cache import redis_cache
    if redis_cache.available:
        redis_cache.client.flushdb()
        return {"code": 200, "msg": "缓存已清除"}
    return {"code": 200, "msg": "Redis 不可用，无需清除"}


@router.post("/cache/clear_search", summary="清除检索缓存", dependencies=[Depends(get_current_admin)])
async def clear_search_cache():
    from core.cache import redis_cache
    if redis_cache.available:
        keys = redis_cache.client.keys("rag:search:*")
        if keys:
            redis_cache.client.delete(*keys)
        return {"code": 200, "msg": f"已清除 {len(keys)} 条检索缓存"}
    return {"code": 200, "msg": "Redis 不可用"}


# ---------- 卡产品管理（cards 表 = 价格权威来源） ----------
from pydantic import BaseModel as _BaseModel


class CardUpsertRequest(_BaseModel):
    name: str
    price: int                      # 单位：分
    description: str = ""
    sort: int = 0
    status: Optional[str] = None


@router.get("/cards", summary="卡产品列表（含下架）", dependencies=[Depends(get_current_admin)])
async def admin_list_cards():
    from database.db import list_cards
    cards = list_cards()
    return {
        "code": 200,
        "data": [{
            "card_id": c["id"],
            "name": c["name"],
            "price": c["price"],
            "price_yuan": round(c["price"] / 100, 2),
            "description": c.get("description", ""),
            "status": c.get("status", "on_sale"),
            "sort": c.get("sort", 0),
        } for c in cards],
    }


@router.post("/cards", summary="创建卡产品", dependencies=[Depends(get_current_admin)])
async def admin_create_card(req: CardUpsertRequest):
    from database.db import create_card
    card = create_card(req.name, req.price, req.description, req.sort)
    if not card:
        raise HTTPException(status_code=400, detail="同名卡产品已存在")
    return {"code": 200, "msg": "创建成功", "data": {"card_id": card["id"]}}


@router.put("/cards/{card_id}", summary="更新卡产品（改价/改名/上下架/排序）", dependencies=[Depends(get_current_admin)])
async def admin_update_card(card_id: int, req: CardUpsertRequest):
    from database.db import update_card, get_card
    if not get_card(card_id):
        raise HTTPException(status_code=404, detail="卡产品不存在")
    card = update_card(
        card_id,
        name=req.name, price=req.price, description=req.description,
        sort=req.sort, status=req.status,
    )
    if not card:
        raise HTTPException(status_code=400, detail="更新失败（同名冲突）")
    return {"code": 200, "msg": "更新成功"}


@router.delete("/cards/{card_id}", summary="删除卡产品", dependencies=[Depends(get_current_admin)])
async def admin_delete_card(card_id: int):
    from database.db import delete_card
    ok = delete_card(card_id)
    if not ok:
        raise HTTPException(status_code=404, detail="卡产品不存在")
    return {"code": 200, "msg": "已删除"}


# ---------- 用户管理（修改 / 删除；列表接口在 api/auth.py 的 GET /auth/users） ----------
class UserUpdateRequest(_BaseModel):
    nickname: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = None


@router.put("/users/{user_id}", summary="修改用户（昵称/手机号/角色）",
            dependencies=[Depends(get_current_admin)])
async def admin_update_user(user_id: int, req: UserUpdateRequest):
    """用户管理页的「修改」。

    只开放三个字段，**不提供改密码**：管理员无法得知用户密码，代为设密属于越权，
    改密请让用户走自助重置。手机号受唯一约束（uk_phone）保护，撞号时回 400 而不是 500。
    """
    from database.db import get_user_by_id, get_user_by_phone, update_user

    if not get_user_by_id(user_id):
        raise HTTPException(status_code=404, detail="用户不存在")

    updates: dict = {}
    if req.nickname is not None:
        updates["nickname"] = req.nickname.strip()
    if req.phone is not None:
        phone = req.phone.strip()
        if phone:
            other = get_user_by_phone(phone)
            if other and int(other["id"]) != int(user_id):
                raise HTTPException(
                    status_code=400,
                    detail=f"手机号 {phone} 已被账号 {other['id']} 占用")
        updates["phone"] = phone
    if req.role is not None:
        if req.role not in ("admin", "user"):
            raise HTTPException(status_code=400, detail="角色只能是 admin 或 user")
        updates["role"] = req.role

    if not updates:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")

    try:
        update_user(user_id, **updates)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"修改失败：{str(e)[:120]}")
    return {"code": 200, "msg": "修改成功"}


@router.delete("/users/{user_id}", summary="删除用户（连同其个人数据）",
               dependencies=[Depends(get_current_admin)])
async def admin_delete_user(user_id: int, current: dict = Depends(get_current_admin)):
    """彻底删除账号，不可恢复（问答记录/画像/订单/卡/消费/签到/收藏一并清除）。

    两条硬保护，都是"删完自己就进不来后台"的场景：
      · 不能删当前登录的账号；
      · 不能删最后一个管理员。
    """
    from database.db import delete_user, list_users

    if int(current.get("user_id") or 0) == int(user_id):
        raise HTTPException(status_code=400, detail="不能删除当前登录的账号")

    rows = list_users() or []
    target = next((u for u in rows if int(u["id"]) == int(user_id)), None)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if (target.get("role") or "user") == "admin":
        admins = [u for u in rows if (u.get("role") or "") == "admin"]
        if len(admins) <= 1:
            raise HTTPException(status_code=400, detail="这是最后一个管理员账号，不能删除")

    deleted = delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="用户不存在")
    detail = "、".join(f"{k}×{v}" for k, v in deleted.items() if k != "users")
    return {"code": 200, "msg": "已删除该账号" + (f"（{detail}）" if detail else "")}

# ---------- 问答记录（分页，question_logs 持久化数据源） ----------
@router.get("/questions", summary="问答记录分页查询", dependencies=[Depends(get_current_admin)])
async def list_questions(page: int = 1, page_size: int = 20, keyword: str = ""):
    """分页查询问答日志（MySQL/SQLite 持久化表，含小程序端来源标记）"""
    from database.db import list_question_logs, count_question_logs

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total = count_question_logs(keyword)
    items = list_question_logs(page=page, page_size=page_size, keyword=keyword)

    return {
        "code": 200,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "items": items,
        }
    }


# ============================================================
# 办卡账单与持卡管理（card_orders + user_cards）
# ============================================================
def _order_to_view(o: dict) -> dict:
    """订单行转前端展示结构（金额分→元，余币兼容持卡记录/面值解析）"""
    import re as _re
    uc_coins = o.get("uc_coins")
    if uc_coins is None:
        # 兜底：按卡名解析面值（旧数据未写 coins）
        m = _re.search(r"(\d+)\s*币", o.get("card_name", ""))
        uc_coins = int(m.group(1)) if m else 0
    return {
        "order_no": o["order_no"],
        "user_id": o.get("user_id"),
        "username": o.get("username") or "",
        "nickname": o.get("nickname") or "",
        "card_id": o.get("card_id"),
        "card_name": o.get("card_name"),
        "amount": o.get("amount"),
        "amount_yuan": round((o.get("amount") or 0) / 100, 2),
        "coins": int(uc_coins),
        "uc_id": o.get("uc_id"),
        "uc_status": o.get("uc_status") or "",
        "status": o.get("status"),
        "transaction_id": o.get("transaction_id") or "",
        "created_at": str(o.get("created_at") or ""),
        "paid_at": str(o.get("paid_at") or ""),
    }


@router.get("/orders", summary="办卡账单列表（统计+分页+筛选）")
async def admin_orders(
    page: int = 1,
    page_size: int = 20,
    status: str = "",      # paid / refunded / pending / 空=全部
    keyword: str = "",     # 订单号/卡名/用户昵称
):
    from database.db import list_all_orders, count_all_orders, get_order_stats

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total = count_all_orders(status=status, keyword=keyword)
    items = [_order_to_view(o) for o in list_all_orders(
        status=status, keyword=keyword, page=page, page_size=page_size)]
    return {
        "code": 200,
        "data": {
            "stats": get_order_stats(),
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "items": items,
        }
    }


class RefundRequest(_BaseModel):
    reason: str = ""   # 退卡原因（记录用）


@router.post("/orders/{order_no}/refund", summary="退卡退款（订单+持卡同步失效，余币自动扣减）")
async def admin_refund_order(order_no: str, req: RefundRequest = None):
    from database.db import refund_order
    result = refund_order(order_no)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg", "退卡失败"))
    import logging
    logging.getLogger(__name__).info(
        f"[退卡] 订单 {order_no} 已退款退卡，原因: {req.reason if req else '未填写'}")
    return {"code": 200, "msg": result["msg"]}


class CoinsAdjustRequest(_BaseModel):
    coins: int          # 调整后的余币面值（非负）
    reason: str = ""    # 调整原因


@router.put("/user-cards/{uc_id}/coins", summary="调整用户持卡余币（线下补偿/纠错）")
async def admin_adjust_coins(uc_id: int, req: CoinsAdjustRequest):
    if req.coins < 0:
        raise HTTPException(status_code=400, detail="余币数不能为负")
    from database.db import set_user_card_coins
    ok = set_user_card_coins(uc_id, req.coins)
    if not ok:
        raise HTTPException(status_code=404, detail="持卡记录不存在或已失效")
    import logging
    logging.getLogger(__name__).info(
        f"[调币] 持卡 {uc_id} 余币调整为 {req.coins}，原因: {req.reason or '未填写'}")
    return {"code": 200, "msg": f"余币已调整为 {req.coins} 币"}


@router.get("/user-cards/summary", summary="全部用户持卡汇总（用户管理列表用）")
async def admin_user_cards_summary():
    """user_id → 持卡数/余币合计（含签到奖励），一次拉全量供用户管理页合并展示"""
    from database.db import list_all_orders, sum_checkin_coins, get_user_by_id
    orders = list_all_orders(page=1, page_size=1000)
    summary: dict = {}
    for o in orders:
        uid = o.get("user_id")
        if uid is None:
            continue
        s = summary.setdefault(uid, {"cards_count": 0, "coins": 0})
        view = _order_to_view(o)
        if o.get("status") == "paid" and o.get("uc_status") == "active":
            s["cards_count"] += 1
            s["coins"] += view["coins"]
        elif o.get("status") == "refunded":
            pass  # 已退卡不计入
    users = list(summary.keys())
    for uid in users:
        summary[uid]["coins"] += sum_checkin_coins(uid)
        u = get_user_by_id(uid)
        if u:
            summary[uid]["nickname"] = u.get("nickname") or ""
            summary[uid]["username"] = u.get("username") or ""
    return {"code": 200, "data": summary}


# ============================================================
# 问答分析图谱（高频问题/意图分布/趋势/满意度，商家可视化数据源）
# ============================================================
@router.get("/analytics/overview", summary="问答分析图谱数据（TOP问题/意图/趋势/满意度）")
async def analytics_overview(days: int = 7):
    """一次返回全部图谱数据：指标卡 + 四张图表（近 days 天趋势，默认 7）"""
    from datetime import date, timedelta
    from database.db import (
        top_questions, daily_question_trend,
        count_question_logs, count_questions_today, feedback_rating_distribution,
    )

    days = min(max(1, days), 60)
    trend_raw = daily_question_trend(days)
    # 补零对齐近 N 天连续日期序列
    series = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        cnt = next((r["cnt"] for r in trend_raw if str(r["d"]) == d), 0)
        series.append({"date": d, "count": cnt})

    # 意图分布：与 /admin/dashboard 同一口径（读取侧实时聚合，近 30 天）
    intent_dist, _ = _intent_distribution_realtime(30)

    return {
        "code": 200,
        "data": {
            "total_questions": count_question_logs(),
            "today_questions": count_questions_today(),
            "top_questions": top_questions(10),
            "intent_distribution": intent_dist,
            "trend": series,
            "satisfaction": feedback_rating_distribution(),
            "latency": question_latency_stats(days),
        }
    }


def question_latency_stats(days: int = 7) -> dict:
    """响应耗时统计（P50/P90/P95/最慢 + 达标率），数据源 question_logs.elapsed_seconds。

    用于持续验证"端到端 ≤10s"的提速目标是否守住（问答日志本就记录了每轮真实耗时）。
    已排除 source IN ('probe','stress') 的压测样本，指标反映的是真实用户提问体验。
    """
    try:
        from database.db import question_latency_samples
        vals = sorted(float(v) for v in (question_latency_samples(days) or []))
    except Exception:
        return {"samples": 0}
    if not vals:
        return {"samples": 0}

    def _pct(p: float) -> float:
        idx = min(len(vals) - 1, max(0, int(round(p * (len(vals) - 1)))))
        return round(vals[idx], 2)

    fast = len([v for v in vals if v <= 10.0])
    cached = len([v for v in vals if v <= 2.0])
    return {
        "samples": len(vals),
        "avg": round(sum(vals) / len(vals), 2),
        "p50": _pct(0.50),
        "p90": _pct(0.90),
        "p95": _pct(0.95),
        "max": round(vals[-1], 2),
        "under_10s_rate": round(fast / len(vals) * 100, 1),
        "under_2s_rate": round(cached / len(vals) * 100, 1),
        "window_days": days,
    }


# ============================================================
# 热点问题（小程序 AI 问答页快捷提问 6 按钮的数据源）
# ============================================================
@router.get("/hot-questions", summary="当前热点问题批次（Top6 快捷提问）",
            dependencies=[Depends(get_current_admin)])
async def admin_hot_questions(limit: int = 6, days: int = 7):
    from services.hot_questions import hot_questions_status
    return {"code": 200, "data": hot_questions_status(limit=min(max(1, limit), 12),
                                                      days=min(max(1, days), 60))}


@router.post("/hot-questions/refresh", summary="立即重新统计近7天热点问题（商家手动刷新）",
             dependencies=[Depends(get_current_admin)])
async def admin_refresh_hot_questions(days: int = 7, limit: int = 6):
    """⚠️ 已经写死时（Redis 里有 pinned）这一步是**空操作**：
    自动统计会把 6 个按钮变回"最近日志回放"（同一主题重复、冒出极低频问法），
    所以 pinned 存在时直接跳过并如实返回 reason=pinned，不假装刷新成功。"""
    from services.hot_questions import (refresh_hot_questions, hot_questions_status,
                                        get_pinned)
    days = min(max(1, days), 60)
    limit = min(max(1, limit), 12)
    res = refresh_hot_questions(days=days, limit=limit, force=True)
    if res.get("reason") == "pinned":
        return {
            "code": 200,
            "msg": "当前 6 条是写死的固定问法，自动统计已跳过；要改成自动统计请先点「解除写死」",
            "data": {**hot_questions_status(limit=limit, days=days), "auto_count": 0},
        }
    return {
        "code": 200,
        "msg": f"已按近 {days} 天问答记录重新统计 {len(res.get('items') or [])} 条热点问题",
        "data": {**hot_questions_status(limit=limit, days=days), "auto_count": res.get("auto_count", 0)},
    }


@router.post("/hot-questions/pin", summary="恢复写死的 6 条常见问法（写入 Redis）",
             dependencies=[Depends(get_current_admin)])
async def admin_pin_hot_questions():
    from services.hot_questions import pin_hot_questions, hot_questions_status
    items = pin_hot_questions()
    return {
        "code": 200,
        "msg": f"已写死 {len(items)} 条常见问法（自动统计不会再覆盖）",
        "data": hot_questions_status(limit=len(items)),
    }


@router.post("/hot-questions/unpin", summary="解除写死，恢复按近7天问答自动统计",
             dependencies=[Depends(get_current_admin)])
async def admin_unpin_hot_questions(days: int = 7, limit: int = 6):
    from services.hot_questions import (unpin_hot_questions, refresh_hot_questions,
                                        hot_questions_status)
    unpin_hot_questions()
    days = min(max(1, days), 60)
    limit = min(max(1, limit), 12)
    res = refresh_hot_questions(days=days, limit=limit, force=True)
    return {
        "code": 200,
        "msg": f"已解除写死，按近 {days} 天问答记录统计出 {len(res.get('items') or [])} 条",
        "data": {**hot_questions_status(limit=limit, days=days),
                 "auto_count": res.get("auto_count", 0)},
    }


# ============================================================
# 游乐消费记录（多选项目合并扣币）+ 核销
# ============================================================
def _mask_phone(phone: str) -> str:
    """手机号脱敏（核销页给工作人员看归属，但不给出完整号码）"""
    p = (phone or "").strip()
    if len(p) < 7:
        return p
    return p[:3] + "****" + p[-4:]


@router.get("/consumptions", summary="消费记录列表（分页，联用户昵称 + 券进度）")
async def admin_list_consumptions(page: int = 1, page_size: int = 20):
    from database.db import (list_all_consumptions, count_all_consumptions,
                             count_consumption_items_by_orders,
                             list_consumption_items_by_orders)

    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    total = count_all_consumptions()
    items = list_all_consumptions(page=page, page_size=page_size)
    import json as _json
    order_nos = [o["order_no"] for o in items]
    # 一单一券包：列表要能看出"这单 3 张券、还剩 2 张没玩"，
    # 否则商家端只看订单状态会把玩了一半的单当成没动过。一趟 GROUP BY 取完，不逐单查。
    stats = count_consumption_items_by_orders(order_nos)
    # 券行也一起带出去：前端「单独核销」弹窗要逐张券列出（券码 + 核销状态），
    # 由前端打开弹窗时再逐单拉一遍就是 20 次往返；这里同样一趟 IN 拿完再按订单分组。
    pass_items: dict = {}
    for it in list_consumption_items_by_orders(order_nos):
        pass_items.setdefault(it["order_no"], []).append({
            "item_code": it["item_code"],
            "project_name": it.get("project_name") or "",
            "unit_coins": int(it.get("unit_coins") or 0),
            "status": it.get("status") or "unused",
            "used_at": str(it.get("used_at") or ""),
        })
    view = []
    for o in items:
        try:
            items_detail = _json.loads(o.get("items_json") or "[]")
        except Exception:
            items_detail = []
        st = stats.get(o["order_no"]) or {}
        view.append({
            "order_no": o["order_no"],
            "verify_code": o["verify_code"],
            "nickname": o.get("nickname") or "",
            "phone": o.get("phone") or "",
            "items": items_detail,
            "card_coins": o.get("card_coins") or 0,
            "bonus_coins": o.get("bonus_coins") or 0,
            "total_coins": o.get("total_coins") or 0,
            "status": o.get("status") or "unused",
            "pass_total": st.get("total", 0),
            "pass_unused": st.get("unused", 0),
            "pass_items": pass_items.get(o["order_no"], []),
            "created_at": str(o.get("created_at") or ""),
        })
    return {
        "code": 200,
        "data": {"total": total, "page": page, "page_size": page_size, "items": view},
    }


@router.put("/consumptions/{order_no}/verify", summary="整单核销（该单所有未核销券一次核销）")
async def admin_verify_consumption(order_no: str):
    """整单核销：适合"一家三口一次把买的项目全玩完"。

    除了把订单置 used，还要把该单所有还没玩的券一起置 used——
    否则订单显示已核销、用户「我的游园码」里却还挂着待玩券，两边对不上。
    """
    from database.db import (get_consumption_by_no, mark_consumption_used,
                             mark_consumption_items_used_by_order)

    order = get_consumption_by_no(order_no)
    if not order:
        raise HTTPException(status_code=404, detail="消费单不存在")
    if order.get("status") == "used":
        raise HTTPException(status_code=400, detail="该消费单已核销，请勿重复操作")
    if not mark_consumption_used(order_no):
        raise HTTPException(status_code=500, detail="核销失败")
    n = mark_consumption_items_used_by_order(order_no)
    logger.info(f"[游乐消费] 整单核销 {order_no}（同时核销 {n} 张券）")
    return {"code": 200, "msg": f"核销成功（{n} 张券）" if n else "核销成功"}


@router.post("/consumptions/items/{item_code}/verify", summary="核销单张券（扫一张核销一张）")
async def admin_verify_consumption_item(item_code: str):
    """员工扫码核销：**只核销扫到的这一张券**，同单其它券不受影响。

    这是"一单一券包"解决的核心问题——买 3 项拆 3 次玩时，
    一单一码模型下第 1 次就把整单核销掉了，用户失去剩余凭据、员工也查不出来。
    """
    from database.db import (get_consumption_item_by_code, mark_consumption_item_used,
                             count_consumption_items_by_order, mark_consumption_used)

    code = (item_code or "").strip().upper()
    item = get_consumption_item_by_code(code)
    if not item:
        raise HTTPException(status_code=404, detail="券码不存在")
    status = item.get("status") or "unused"
    if status == "used":
        raise HTTPException(status_code=400, detail="这张券已核销，请勿重复操作")
    if status == "refunded":
        raise HTTPException(status_code=400, detail="这张券已退款，不能核销")
    if not mark_consumption_item_used(code):
        raise HTTPException(status_code=500, detail="核销失败")

    order_no = item["order_no"]
    left = count_consumption_items_by_order(order_no, "unused")
    if left == 0:
        # 该单所有券都玩完了 → 订单同步置 used，商家端"待核销"才不会一直挂着
        mark_consumption_used(order_no)
    logger.info(f"[消费券] 核销 {code}（{item['project_name']}）"
                f"order={order_no} 本单剩余 {left} 张")
    return {
        "code": 200,
        "msg": f"已核销：{item['project_name']} ×1" + ("（本单已全部核销）" if left == 0 else ""),
        "data": {
            "item_code": code,
            "project_name": item["project_name"],
            "order_no": order_no,
            "unused_left": left,
        },
    }


# ---------- 券级批量操作（商家 PC「单独核销」弹窗 / 员工端共用） ----------
class ItemCodesRequest(_BaseModel):
    """勾选的一批券码（一券一码，大小写不敏感）"""
    codes: List[str] = []


def _norm_codes(codes: List[str]) -> List[str]:
    """去空格 / 统一大写 / 去重，保持传入顺序"""
    out: List[str] = []
    seen = set()
    for c in (codes or []):
        c = str(c or "").strip().upper()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


@router.post("/consumptions/items/verify", summary="批量核销券（勾选多张一次核销）")
async def admin_verify_consumption_items(req: ItemCodesRequest):
    """弹窗里勾若干张券 → 一次核销。

    逐张调单张核销接口也能实现，但那要 N 次往返、中途失败还会留下"核销了一半"的中间态；
    这里一次过，并且只在某单待玩清零时才把该订单置 used（商家端"待核销"才不会一直挂着）。
    """
    from database.db import (get_consumption_item_by_code, mark_consumption_item_used,
                             count_consumption_items_by_order, mark_consumption_used)

    codes = _norm_codes(req.codes)
    if not codes:
        raise HTTPException(status_code=400, detail="请先勾选要核销的券")

    ok: List[str] = []
    failed: List[dict] = []
    touched_orders = set()
    for c in codes:
        item = get_consumption_item_by_code(c)
        if not item:
            failed.append({"item_code": c, "reason": "券码不存在"})
            continue
        st = item.get("status") or "unused"
        if st == "used":
            failed.append({"item_code": c, "reason": "已核销，请勿重复操作"})
            continue
        if st == "refunded":
            failed.append({"item_code": c, "reason": "已退款，不能核销"})
            continue
        if not mark_consumption_item_used(c):
            failed.append({"item_code": c, "reason": "核销失败，请刷新后重试"})
            continue
        ok.append(c)
        touched_orders.add(item["order_no"])

    for order_no in touched_orders:
        if count_consumption_items_by_order(order_no, "unused") == 0:
            mark_consumption_used(order_no)

    logger.info(f"[消费券] 批量核销 {len(ok)} 张（跳过 {len(failed)} 张）")
    msg = f"已核销 {len(ok)} 张券" if ok else "没有券被核销"
    if failed:
        msg += f"，{len(failed)} 张已跳过"
    return {"code": 200, "msg": msg, "data": {"verified": ok, "failed": failed}}


@router.post("/consumptions/items/refund", summary="退款券（勾选若干张，已核销的也能退）")
async def admin_refund_consumption_items(req: ItemCodesRequest):
    """按券退款：**不看核销状态**。

    用户可能刚上去就下来，现场员工按实际情况退；已核销的券同样退，
    否则"扫过码"就成了退不了的理由。用户自己在小程序里没有消费退款入口，
    这个动作只对管理员 / 员工开放。
    """
    from database.db import refund_consumption_items

    codes = _norm_codes(req.codes)
    if not codes:
        raise HTTPException(status_code=400, detail="请先勾选要退款的券")
    result = refund_consumption_items(codes)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg", "退款失败"))
    logger.info(f"[消费券] 按券退款 {result.get('count')} 张，"
                f"{result.get('refunded_coins')} 币（管理员操作）")
    return {"code": 200, "msg": result["msg"], "data": result}


@router.post("/consumptions/{order_no}/refund", summary="整单退款（该单所有未退过的券一次退掉，含已核销）")
async def admin_refund_consumption(order_no: str):
    """整单退款：该单**所有还没退过的券**一起退（已核销的也在内）。

    与按券退款共用核心实现（database.refund_consumption_items），差别只在"退哪些券"，
    所以不会出现两条入口算法不一致。没有券行的历史订单仍按整单全额退。
    """
    from database.db import get_consumption_by_no, refund_consumption_order

    order = get_consumption_by_no(order_no)
    if not order:
        raise HTTPException(status_code=404, detail="消费单不存在")
    result = refund_consumption_order(order_no)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("msg", "退款失败"))
    logger.info(f"[游乐消费] 整单退款 {order_no}（管理员操作，{result.get('msg')}）")
    return {"code": 200, "msg": result["msg"], "data": {"order_no": order_no}}


@router.get("/consumptions/by-code/{verify_code}", summary="按券码/核销码查询（券优先，整单兜底）")
async def admin_get_consumption_by_code(verify_code: str):
    """工作人员扫码/输码的统一入口。

    券包下单后用户手上是 N 张券码，同时**整单码 = 第一张券的码**。所以这里先按券查
    （拿到「这一张是什么项目、玩没玩、本单还剩几张」），查不到再回落到整单码——
    历史订单（没有券行）走的就是这条兜底路径，新旧单都能查。
    """
    from database.db import (get_consumption_item_by_code, get_consumption_by_code,
                             get_consumption_by_no, get_user_by_id,
                             count_consumption_items_by_order,
                             list_consumption_items_by_order)

    def _pass_view(order_no: str) -> list:
        """本单所有券的明细（员工端"单独核销"要逐张列出来核销/退款）"""
        return [{
            "item_code": it["item_code"],
            "project_name": it.get("project_name") or "",
            "unit_coins": int(it.get("unit_coins") or 0),
            "status": it.get("status") or "unused",
            "used_at": str(it.get("used_at") or ""),
        } for it in (list_consumption_items_by_order(order_no) or [])]

    code = (verify_code or "").strip().upper()

    item = get_consumption_item_by_code(code)
    if item:
        order = get_consumption_by_no(item["order_no"]) or {}
        user = get_user_by_id(item["user_id"]) if item.get("user_id") else {}
        return {
            "code": 200,
            "data": {
                "kind": "item",
                "item_code": item["item_code"],
                "project_name": item["project_name"],
                "unit_coins": item.get("unit_coins") or 0,
                "item_status": item.get("status") or "unused",
                "used_at": str(item.get("used_at") or ""),
                "order_no": item["order_no"],
                "order_status": order.get("status") or "",
                "phone": _mask_phone((user or {}).get("phone") or ""),
                "pass_total": count_consumption_items_by_order(item["order_no"]),
                "pass_unused": count_consumption_items_by_order(item["order_no"], "unused"),
                "pass_items": _pass_view(item["order_no"]),
                "created_at": str(item.get("created_at") or ""),
            },
        }

    order = get_consumption_by_code(code)
    if not order:
        raise HTTPException(status_code=404, detail="券码不存在")
    import json as _json
    try:
        items_detail = _json.loads(order.get("items_json") or "[]")
    except Exception:
        items_detail = []
    return {
        "code": 200,
        "data": {
            "kind": "order",
            "order_no": order["order_no"],
            "verify_code": order["verify_code"],
            "items": items_detail,
            "total_coins": order.get("total_coins") or 0,
            "status": order.get("status") or "unused",
            "pass_total": count_consumption_items_by_order(order["order_no"]),
            "pass_unused": count_consumption_items_by_order(order["order_no"], "unused"),
            "pass_items": _pass_view(order["order_no"]),
            "created_at": str(order.get("created_at") or ""),
        },
    }
