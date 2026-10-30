# ============================================================
# 用户消费统计服务 (services/statistics.py)
# ------------------------------------------------------------
# 面向"游客本人"的自助消费统计（客服对话中由业务 Agent 工具调用）：
#   1) 项目游玩次数/花费排行        ← consumption_orders.items_json
#   2) 办卡类型统计（张数/金额）     ← card_orders(status='paid')
#   3) 消费金额按月的分类/趋势       ← consumption_orders.created_at
# 口径（已与业务确认）：
#   - 周期 week/month/year（分别约 7/30/365 天）
#   - 金额只统计项目消费：1 币 = 1 元（不做办卡充值与消费混加）
#   - 已退款消费单 全额剔除（次数、金额都不计）
# ============================================================
import json
import logging
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

PERIODS = {"day": 1, "week": 7, "month": 30, "year": 365}
PERIOD_LABELS = {"day": "近一天", "week": "最近一周", "month": "最近一个月", "year": "最近一年"}


def resolve_period(period: str) -> Dict:
    """把用户语义周期换算成窗口边界与展示名"""
    p = (period or "month").strip().lower()
    if p not in PERIODS:
        p = "month"
    days = PERIODS[p]
    end = date.today()
    start = end - timedelta(days=days - 1)
    return {"key": p, "label": PERIOD_LABELS[p], "start": start, "end": end}


def _parse_dt(value) -> Optional[datetime]:
    """兼容 sqlite 'YYYY-MM-DD HH:MM:SS' 与 mysql DATETIME 的日期解析"""
    if not value:
        return None
    s = str(value)
    try:
        s = s.replace("T", " ")[:19]
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d")
        except Exception:
            return None


def _iter_months(start: date, end: date):
    """start..end 覆盖的连续月份 'YYYY-MM'"""
    ym = start.year * 12 + (start.month - 1)
    ym_end = end.year * 12 + (end.month - 1)
    while ym <= ym_end:
        y, m = divmod(ym, 12)
        yield f"{y:04d}-{m + 1:02d}"
        ym += 1


def aggregate_consumption(rows: List[Dict], start: date, end: date) -> Dict:
    """
    纯聚合（便于单测）：rows 来自 consumption_orders 字典列表。
    返回: {project_rank, monthly, order_count, total_yuan, refunded_excluded}
    口径：已退款全额剔除。
    """
    rank: Dict[str, Dict] = {}
    monthly: Dict[str, float] = {}
    total_coins = 0
    order_count = 0
    refunded_excluded = 0

    for r in rows or []:
        dt = _parse_dt(r.get("created_at"))
        if dt is None or dt.date() < start or dt.date() > end:
            continue
        if (r.get("status") or "unused") == "refunded":
            refunded_excluded += 1
            continue
        order_count += 1
        total_coins += int(r.get("total_coins") or 0)
        key = dt.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + int(r.get("total_coins") or 0)

        try:
            items = json.loads(r.get("items_json") or "[]")
        except Exception:
            items = []
        for it in items or []:
            name = str(it.get("name") or "未知项目")
            qty = int(it.get("qty") or 1)
            coins = int(it.get("coins") or 0)
            rec = rank.setdefault(name, {"name": name, "qty": 0, "coins": 0})
            rec["qty"] += qty
            rec["coins"] += coins

    project_rank = sorted(rank.values(), key=lambda x: (-x["qty"], -x["coins"]))
    # 月度连续补零（只保留窗口覆盖的月份）
    month_series = []
    for m in _iter_months(start, end):
        coins = monthly.get(m, 0)
        month_series.append({
            "month": m,
            "coins": int(round(coins)),
            "yuan": int(round(coins)),   # 1 币 = 1 元
        })
    return {
        "project_rank": project_rank,
        "monthly": month_series,
        "order_count": order_count,
        "total_coins": int(total_coins),
        "total_yuan": int(total_coins),
        "refunded_excluded": refunded_excluded,
    }


def aggregate_cards(orders: List[Dict], start: date, end: date) -> Dict:
    """card_orders 聚合：仅 status='paid'，按卡名分组（金额以分存，转元）"""
    stats: Dict[str, Dict] = {}
    for o in orders or []:
        dt = _parse_dt(o.get("paid_at") or o.get("created_at"))
        if dt is None or dt.date() < start or dt.date() > end:
            continue
        if (o.get("status") or "") != "paid":
            continue
        name = str(o.get("card_name") or "未命名卡")
        amount_cents = int(o.get("amount") or 0)
        rec = stats.setdefault(name, {"card_name": name, "count": 0, "amount_yuan": 0.0})
        rec["count"] += 1
        rec["amount_yuan"] = round(rec["amount_yuan"] + amount_cents / 100.0, 2)
    card_stats = sorted(stats.values(), key=lambda x: (-x["count"], -x["amount_yuan"]))
    return {
        "card_stats": card_stats,
        "card_count": sum(c["count"] for c in card_stats),
        "card_amount_yuan": round(sum(c["amount_yuan"] for c in card_stats), 2),
    }


def user_stats(user_id: int, period: str = "month") -> Dict:
    """面向登录用户的完整统计（查询 + 聚合），失败时返回空结构而非抛错"""
    bounds = resolve_period(period)
    empty = {
        "period": bounds, "project_rank": [], "monthly": [],
        "order_count": 0, "total_yuan": 0, "refunded_excluded": 0,
        "card_stats": [], "card_count": 0, "card_amount_yuan": 0.0,
    }
    try:
        from database.db import list_consumptions_by_user, list_user_orders
        rows = list_consumptions_by_user(user_id, limit=20000) or []
        cons = aggregate_consumption(rows, bounds["start"], bounds["end"])
        orders = list_user_orders(user_id, limit=2000) or []
        cards = aggregate_cards(orders, bounds["start"], bounds["end"])
        return {**empty, **cons, **cards, "period": bounds}
    except Exception as e:
        logger.warning(f"user_stats 失败: {e}")
        return empty


def stats_to_text(stats: Dict) -> str:
    """把统计结构转成模型可直接引用/组织回答的文本（含真实数字）"""
    b = stats.get("period", {})
    lines = [
        f"统计周期：{b.get('label', '')}（{b.get('start')} ~ {b.get('end')}）",
        f"项目消费合计：{stats.get('total_yuan', 0)} 元（{stats.get('total_coins', 0)} 币），"
        f"共 {stats.get('order_count', 0)} 笔消费单"
        + (f"，已剔除 {stats.get('refunded_excluded', 0)} 笔退款单" if stats.get("refunded_excluded") else ""),
    ]
    rank = stats.get("project_rank") or []
    if rank:
        lines.append("【项目游玩排行】")
        for r in rank[:8]:
            lines.append(f"- {r['name']}：{r['qty']} 次，花 {r['coins']} 币(≈{r['coins']}元)")
    else:
        lines.append("【项目游玩排行】该周期内暂无游玩消费记录")
    monthly = stats.get("monthly") or []
    if monthly:
        lines.append("【按月消费分布】")
        for m in monthly:
            lines.append(f"- {m['month']}：{m['yuan']} 元")
    cards = stats.get("card_stats") or []
    if cards:
        lines.append("【办卡类型统计】")
        for c in cards[:8]:
            lines.append(f"- {c['card_name']}：{c['count']} 张，共 {c['amount_yuan']} 元")
    else:
        lines.append("【办卡类型统计】该周期内没有办理储值卡")
    return "\n".join(lines)
