# ============================================================
# 商家端运营工具集（右下角「经营小助手」弹窗专用，OpenAI 兼容 function calling）
# ------------------------------------------------------------
# 把商家 PC 后台的 REST 能力工具化，让管理员说一句话就能把事办完。
# 与 tools/business_tools.py（游客侧）的分工：
#   - business_tools：游客身份，服务端注入 user_id，只读为主；
#   - admin_tools  ：管理员身份，覆盖商家端全部经营动作（含高危写）。
#
# 设计原则（与既有工具族一致）：
#   1. 工具直接调 database.db / services.*，不经 HTTP（少一次序列化，且不存在鉴权绕行）；
#   2. 模型无权传身份：管理员身份由服务端端点注入，工具签名里没有 user_id 入参；
#   3. 金额入参一律「元」（price_yuan），内部 ×100 转分；出参由 DB 权威值计算；
#   4. 高危不可逆操作（退钱 / 调币 / 删数据 / 改 Agent 配置）默认**不执行**，
#      返回 `⛔ {...}` 确认卡，由管理员在前端点「执行」后凭 confirm_token 续跑；
#      模型即使幻觉出 confirmed=true 也无效（本模块完全不看该参数）。
#   5. 写操作一律写 logger.info("[admin_assistant] ...") 供审计，不新建表。
# ============================================================
import json
import logging
import os
import re
import secrets
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 风险分档
R = "read"          # 只读
W = "write"         # 常规写（可人工纠正）
X = "danger"        # 高危不可逆（涉及钱 / 删除 / 影响线上）

# ------------------------------------------------------------------
# 确认卡：高危操作的一次性令牌（内存态，5 分钟有效）
# ------------------------------------------------------------------
_CONFIRM_TTL = 300
_pending_confirm: dict = {}


def new_confirm_token(tool: str, args: dict) -> str:
    """登记一次待确认的高危调用，返回一次性令牌（前端点「执行」时带回来）"""
    now = time.time()
    for k in [k for k, v in _pending_confirm.items() if now - v["ts"] > _CONFIRM_TTL]:
        _pending_confirm.pop(k, None)
    tok = secrets.token_urlsafe(12)
    _pending_confirm[tok] = {"tool": tool, "args": args, "ts": now}
    return tok


def pop_confirm_token(tok: str) -> Optional[dict]:
    """取出并作废令牌（一次性）；过期或不存在返回 None"""
    item = _pending_confirm.pop(tok or "", None)
    if not item:
        return None
    if time.time() - item["ts"] > _CONFIRM_TTL:
        return None
    return item


# ------------------------------------------------------------------
# 小工具
# ------------------------------------------------------------------
def _obj(props: dict = None, required: list = None) -> dict:
    return {"type": "object", "properties": props or {}, "required": required or []}


def _s(desc: str, **kw) -> dict:
    return {"type": "string", "description": desc, **kw}


def _i(desc: str) -> dict:
    return {"type": "integer", "description": desc}


def _yuan_to_fen(v) -> int:
    """元 → 分（唯一换算入口，避免各处四舍五入不一致）"""
    return int(round(float(v) * 100))


def _fen(v) -> str:
    return f"¥{(int(v or 0) / 100):.2f}"


def _card_line(c: dict) -> str:
    status = "在售" if (c.get("status") or "on_sale") == "on_sale" else "已下架"
    coins = re.search(r"(\d+)\s*币", f"{c.get('name','')} {c.get('description','')}")
    extra = f"，含 {coins.group(1)} 币" if coins else ""
    desc = f"，{c['description']}" if c.get("description") else ""
    return (f"- id={c['id']}｜{c['name']}｜{_fen(c.get('price'))}{extra}｜{status}"
            f"｜排序 {c.get('sort') or 0}{desc}")


def _order_line(o: dict) -> str:
    uc_coins = o.get("uc_coins")
    if uc_coins is None:
        m = re.search(r"(\d+)\s*币", o.get("card_name") or "")
        uc_coins = int(m.group(1)) if m else 0
    st = {"paid": "已支付", "refunded": "已退卡", "pending": "待支付"}.get(
        o.get("status") or "", o.get("status") or "")
    who = o.get("nickname") or o.get("username") or f"用户{o.get('user_id')}"
    return (f"- {o['order_no']}｜{o.get('card_name')}｜{_fen(o.get('amount'))}｜"
            f"{uc_coins} 币｜{st}｜{who}｜{str(o.get('created_at') or '')[:16]}"
            f"｜uc_id={o.get('uc_id')}({o.get('uc_status') or '-'})")


def _consumption_line(o: dict, items: list) -> str:
    names = "、".join(
        (i.get("name") if isinstance(i, dict) else str(i)) for i in (items or [])[:4])
    st = {"unused": "未核销", "used": "已核销", "refunded": "已退款"}.get(
        o.get("status") or "", o.get("status") or "")
    return (f"- {o['order_no']}｜核销码 {o.get('verify_code')}｜{names}｜"
            f"共 {o.get('total_coins') or 0} 币｜{st}｜"
            f"{(o.get('nickname') or '')}｜{str(o.get('created_at') or '')[:16]}")


def _confirm(ctx: dict, tool: str, args: dict, preview: str) -> Optional[str]:
    """高危操作闸门：返回 None 放行；返回字符串则直接作为工具结果（确认卡）"""
    if ctx.get("allow_dangerous"):
        logger.info(f"[admin_assistant] 高危直通（会话已授权） tool={tool} args={args}")
        return None
    tok = new_confirm_token(tool, args)
    logger.info(f"[admin_assistant] 高危待确认 tool={tool} args={args} token={tok}")
    return "⛔ " + json.dumps(
        {"tool": tool, "args": args, "preview": preview, "token": tok},
        ensure_ascii=False)


def _fail(msg: str, e: Exception = None) -> str:
    if e is not None:
        logger.warning(f"[admin_assistant] {msg}: {e}")
    return f"❌ {msg}" + (f"（{str(e)[:80]}）" if e is not None else "")


# ------------------------------------------------------------------
# 富回答产物：数字卡 + 图表（只读工具用）
# ------------------------------------------------------------------
# 为什么图表规格由**工具层**产出，而不是让模型写：
#   执行器只采信工具结果、丢弃模型文字（见 agents/admin_assistant.py），
#   所以"可视化"必须由工具自己给出，模型无权参与 —— 也就不存在编数字的空间。
#   前端拿到的是**原生 ECharts option**，直接 <ReactECharts option={...} /> 渲染。
#
# 约定：只读工具返回 _report(text, stats, charts)；写工具照旧只返回字符串
#       （它们的价值是"动作流水"，不需要图表）。
_PALETTE = ["#26334D", "#3B82F6", "#10B981", "#F59E0B", "#EF4444",
            "#8B5CF6", "#14B8A6", "#EC4899", "#0EA5E9", "#6B7280"]
_AXIS_TEXT = "#64748B"
_SPLIT_LINE = {"lineStyle": {"color": "#F1F5F9"}}


def _report(text: str, stats: list = None, charts: list = None) -> dict:
    """只读工具的富结果：{text: 完整文字报告, stats: 数字卡[], charts: 图表[]}"""
    return {"text": text or "", "stats": stats or [], "charts": charts or []}


def _stat(label: str, value, unit: str = "", tone: str = "default") -> dict:
    """数字卡。tone ∈ default/money/good/warn —— 只影响前端配色，不传任何业务含义"""
    return {"label": label, "value": value, "unit": unit, "tone": tone}


def _chart(title: str, option: dict, height: int = 200) -> dict:
    return {"title": title, "height": height, "option": option}


def _clip(text: str, n: int = 12) -> str:
    """图表轴标签截断（完整值留给 tooltip / 文字报告）"""
    text = str(text or "")
    return text if len(text) <= n else text[:n] + "…"


def _grid(**kw) -> dict:
    g = {"left": 4, "right": 16, "top": 16, "bottom": 4, "containLabel": True}
    g.update(kw)
    return g


def _opt_bar(cats: list, vals: list, color: str = "#26334D", unit: str = "") -> dict:
    """纵向柱状图（类别少时用）"""
    return {
        "grid": _grid(),
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": cats,
                  "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT, "interval": 0},
                  "axisLine": {"lineStyle": {"color": "#E2E8F0"}}},
        "yAxis": {"type": "value", "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT},
                  "splitLine": _SPLIT_LINE},
        "series": [{"type": "bar", "data": vals, "barMaxWidth": 28,
                    "itemStyle": {"color": color, "borderRadius": [4, 4, 0, 0]},
                    "label": {"show": True, "position": "top", "fontSize": 10,
                              "color": "#475569", "formatter": "{c}" + unit}}],
    }


def _opt_hbar(pairs: list, color: str = "#3B82F6") -> dict:
    """横向条形图（标签长/类别多时用，如高频问题 Top10）。pairs = [(标签, 值)]"""
    labels = [_clip(p[0]) for p in pairs][::-1]
    vals = [p[1] for p in pairs][::-1]
    return {
        "grid": _grid(right=40),
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "value", "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT},
                  "splitLine": _SPLIT_LINE},
        "yAxis": {"type": "category", "data": labels,
                  "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT},
                  "axisLine": {"lineStyle": {"color": "#E2E8F0"}}},
        "series": [{"type": "bar", "data": vals, "barMaxWidth": 16,
                    "itemStyle": {"color": color, "borderRadius": [0, 4, 4, 0]},
                    "label": {"show": True, "position": "right", "fontSize": 10,
                              "color": "#475569", "formatter": "{c}"}}],
    }


def _opt_pie(pairs: list) -> dict:
    """环形图。pairs = [(名称, 值)]"""
    return {
        "color": _PALETTE,
        "tooltip": {"trigger": "item", "formatter": "{b}: {c}（{d}%）"},
        "legend": {"type": "scroll", "bottom": 0, "itemWidth": 10, "itemHeight": 10,
                   "textStyle": {"fontSize": 10, "color": _AXIS_TEXT}},
        "series": [{"type": "pie", "radius": ["42%", "66%"], "center": ["50%", "42%"],
                    "data": [{"name": str(n), "value": v} for n, v in pairs],
                    "label": {"fontSize": 10, "color": "#475569"},
                    "itemStyle": {"borderColor": "#fff", "borderWidth": 2}}],
    }


def _opt_line(cats: list, vals: list, color: str = "#3B82F6") -> dict:
    """折线图（带面积填充），用于趋势"""
    return {
        "grid": _grid(),
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": cats, "boundaryGap": False,
                  "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT, "interval": 0},
                  "axisLine": {"lineStyle": {"color": "#E2E8F0"}}},
        "yAxis": {"type": "value", "axisLabel": {"fontSize": 10, "color": _AXIS_TEXT},
                  "splitLine": _SPLIT_LINE},
        "series": [{"type": "line", "data": vals, "smooth": True, "symbolSize": 5,
                    "lineStyle": {"width": 2, "color": color},
                    "itemStyle": {"color": color},
                    "areaStyle": {"color": "rgba(59,130,246,0.12)"}}],
    }


def _fill_days(rows: list, key: str, days: int, value_key: str) -> tuple:
    """把「仅有数据的日期」补齐成连续 N 天，返回 (日期MM-DD[], 值[])。

    趋势图直接画稀疏数据会缺日、看着跳档，必须补零对齐。
    """
    from datetime import date, timedelta
    have = {str(r.get(key))[:10]: r.get(value_key) for r in (rows or [])}
    today = date.today()
    cats, vals = [], []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        cats.append(d[5:])
        vals.append(have.get(d) or 0)
    return cats, vals


# ============================================================
# 工具集
# ============================================================
def get_admin_tools(ctx: Optional[dict] = None) -> list:
    """构建商家端运营工具清单。

    Args:
        ctx: 请求级上下文，支持
             - allow_dangerous: True 时高危操作直接执行（弹窗内「本会话允许直接执行」开关）
             - chart/app 等占位（当前未使用，保留请求级隔离位）

    Returns:
        [{name, description, parameters, run, risk, view}]，
        run(args: dict) -> str（写操作）或 _report(...) 字典（查询类富结果）
    """
    ctx = ctx if ctx is not None else {}

    def _tool(name: str, description: str, params: dict, run, risk: str = R,
              view: str = "log") -> dict:
        """view: log=只回动作流水（写操作）；report=把完整文字/数字卡/图表上屏（查询类）"""
        return {"name": name, "description": description, "parameters": params,
                "run": run, "risk": risk, "view": view}

    # ---------------- A. 经营看板（只读，富回答：文字 + 数字卡 + 图表） ----------------
    def _run_dashboard(args: dict) -> dict:
        try:
            from database.db import (count_question_logs, count_questions_today,
                                     count_new_users_today, count_consumptions,
                                     count_lost_items, get_order_stats,
                                     daily_revenue_trend)
            days = min(max(1, int(args.get("days") or 7)), 60)
            stats = get_order_stats() or {}
            paid_amount = int(stats.get("paid_amount") or 0)
            today_paid = int(stats.get("today_paid_amount") or 0)
            refunded = int(stats.get("refunded_amount") or 0)
            q_total = count_question_logs()
            q_today = count_questions_today()
            new_users = count_new_users_today()
            unused = count_consumptions("unused")
            lost = count_lost_items("lost")

            lines = [f"【近 {days} 天经营概览】",
                     f"- 问答总量 {q_total} 条，今日 {q_today} 条",
                     f"- 今日新增用户 {new_users} 人",
                     f"- 累计收款 {_fen(paid_amount)}"
                     f"（今日 {_fen(today_paid)}），"
                     f"已退款 {_fen(refunded)}",
                     f"- 订单：已支付 {stats.get('paid_count', 0)} 笔，"
                     f"待支付 {stats.get('pending_count', 0)} 笔，"
                     f"已退卡 {stats.get('refunded_count', 0)} 笔",
                     f"- 待办：未核销消费单 {unused} 笔，"
                     f"待认领失物 {lost} 件"]
            try:
                from core.cache import redis_cache
                from agents.retrieval_agent import retrieval_agent
                idx = retrieval_agent.get_index_stats() or {}
                lines.append(f"- 知识库：向量 {idx.get('milvus_entities', 0)} 条，"
                             f"BM25 {idx.get('bm25_documents', 0)} 条；"
                             f"Redis {'可用' if redis_cache.available else '不可用'}")
            except Exception:
                pass

            cards = [
                _stat("今日收款", f"{today_paid / 100:.2f}", "元", "money"),
                _stat("累计收款", f"{paid_amount / 100:.2f}", "元", "money"),
                _stat("今日问答", q_today, "条"),
                _stat("今日新增用户", new_users, "人"),
                _stat("未核销消费单", unused, "笔", "warn" if unused else "good"),
                _stat("待认领失物", lost, "件", "warn" if lost else "good"),
            ]

            charts = []
            # 趋势图至少给 7 天窗口：查"今天"时单点趋势没有意义，铺开一周才有对比
            trend_days = max(days, 7)
            trend = daily_revenue_trend(trend_days) or []
            if trend:
                cats, vals = _fill_days(
                    [{"d": r.get("d"), "amount": int(r.get("amount") or 0)} for r in trend],
                    "d", trend_days, "amount")
                charts.append(_chart(f"近 {trend_days} 天收款趋势（元）",
                                     _opt_line(cats, [round(v / 100, 2) for v in vals])))
            if unused or lost:
                charts.append(_chart("待办事项", _opt_pie(
                    [("未核销消费单", unused), ("待认领失物", lost)]), 170))

            return _report("\n".join(lines), cards, charts)
        except Exception as e:
            return _fail("查询经营概览失败", e)

    def _run_revenue(args: dict) -> dict:
        """营收汇总（"今天赚了多少/营收/卖了多少钱" 的专用入口）"""
        try:
            from database.db import (get_order_stats, revenue_by_card,
                                     daily_revenue_trend)
            days = min(max(1, int(args.get("days") or 1)), 60)
            scope = "今日" if days <= 1 else f"近 {days} 天"
            stats = get_order_stats() or {}
            rows = revenue_by_card(days) or []
            total = sum(int(r.get("amount") or 0) for r in rows)
            cnt = sum(int(r.get("cnt") or 0) for r in rows)
            refunded = int(stats.get("refunded_amount") or 0)

            lines = [f"【{scope}营收汇总】",
                     f"- {scope}收款合计 {_fen(total)}，共 {cnt} 笔（已退款订单不计入）"]
            if rows:
                lines.append(f"- 分卡销售（{scope}）：")
                for r in rows:
                    lines.append(f"  · {r.get('card_name') or '-'}："
                                 f"{_fen(r.get('amount'))}（{r.get('cnt')} 笔）")
            else:
                lines.append(f"- {scope}暂无已支付的办卡订单。")
            if days <= 1:
                lines.append(f"- 参考：累计收款 {_fen(stats.get('paid_amount'))}，"
                             f"累计退款 {_fen(refunded)}")
            else:
                lines.append(f"- 参考：今日收款 {_fen(stats.get('today_paid_amount'))}，"
                             f"累计退款 {_fen(refunded)}")

            cards = [
                _stat(f"{scope}收款", f"{total / 100:.2f}", "元", "money"),
                _stat(f"{scope}订单", cnt, "笔"),
                _stat("今日收款", f"{int(stats.get('today_paid_amount') or 0) / 100:.2f}", "元", "money"),
                _stat("累计收款", f"{int(stats.get('paid_amount') or 0) / 100:.2f}", "元", "money"),
                _stat("累计退款", f"{refunded / 100:.2f}", "元",
                      "warn" if refunded else "default"),
            ]

            charts = []
            if rows:
                charts.append(_chart(f"各卡产品销售额（{scope}，元）", _opt_hbar(
                    [(str(r.get("card_name") or "-"), round(int(r.get("amount") or 0) / 100, 2))
                     for r in rows])))
            trend_days = max(days, 7)
            trend = daily_revenue_trend(trend_days) or []
            if trend:
                cats, vals = _fill_days(
                    [{"d": r.get("d"), "amount": int(r.get("amount") or 0)} for r in trend],
                    "d", trend_days, "amount")
                charts.append(_chart(f"近 {trend_days} 天收款趋势（元）",
                                     _opt_line(cats, [round(v / 100, 2) for v in vals])))

            return _report("\n".join(lines), cards, charts)
        except Exception as e:
            return _fail("查询营收汇总失败", e)

    def _run_analytics(args: dict) -> dict:
        try:
            from database.db import (top_questions, list_question_texts,
                                     daily_question_trend, count_question_logs,
                                     count_questions_today, feedback_rating_distribution)
            days = min(max(1, int(args.get("days") or 7)), 60)
            q_total = count_question_logs()
            q_today = count_questions_today()
            lines = [f"【问答分析·近 {days} 天】",
                     f"- 累计 {q_total} 条，今日 {q_today} 条"]
            cards = [_stat("累计问答", q_total, "条"), _stat("今日问答", q_today, "条")]
            charts = []

            tops = top_questions(10) or []
            if tops:
                lines.append("- 高频问题 Top10：")
                for i, t in enumerate(tops, 1):
                    q = t.get("question") or t.get("q") or ""
                    c = t.get("cnt") or t.get("count") or 0
                    lines.append(f"  {i}. {q}（{c} 次）")
                charts.append(_chart("高频问题 Top10（次）", _opt_hbar(
                    [(str(t.get("question") or "-"), int(t.get("cnt") or 0)) for t in tops],
                    "#26334D"), 250))

            # 意图分布：读取侧实时聚合（禁 question_logs.intent，该列从无写入点）
            from agents.query_agent import query_agent
            intent_counter: dict = {}
            for qq in (list_question_texts(days=30) or []):
                ii = query_agent.detect_intent(qq)
                intent_counter[ii] = intent_counter.get(ii, 0) + 1
            idist = [{"intent": k, "cnt": v}
                     for k, v in sorted(intent_counter.items(), key=lambda x: -x[1])]
            if idist:
                parts = [f"{(d.get('intent') or '其他')} {d.get('cnt', 0)}" for d in idist[:8]]
                lines.append("- 意图分布：" + "、".join(parts))
                charts.append(_chart("意图分布", _opt_pie(
                    [(str(d.get("intent") or "其他"), int(d.get("cnt") or 0))
                     for d in idist[:8]]), 215))

            trend = daily_question_trend(days) or []
            if trend:
                cats, vals = _fill_days(
                    [{"d": r.get("d"), "cnt": int(r.get("cnt") or 0)} for r in trend],
                    "d", days, "cnt")
                lines.append("- 每日趋势：" + "、".join(
                    f"{c} {v}" for c, v in list(zip(cats, vals))[-7:]))
                charts.append(_chart(f"问答量趋势（近 {days} 天）",
                                     _opt_line(cats, vals)))

            sat = feedback_rating_distribution() or {}
            if isinstance(sat, dict) and sat.get("total"):
                dist = sat.get("distribution") or {}
                lines.append(f"- 满意度：平均 {sat.get('average')} 星"
                             f"（共 {sat.get('total')} 条评价）；"
                             + "、".join(f"{k}星 {v}" for k, v in sorted(dist.items())))
                cards.append(_stat("平均满意度", sat.get("average"), "星"))
                pairs = [(f"{k} 星", int(v)) for k, v in sorted(dist.items())]
                if pairs:
                    charts.append(_chart("满意度分布（条）", _opt_bar(
                        [p[0] for p in pairs], [p[1] for p in pairs]), 180))

            return _report("\n".join(lines), cards, charts)
        except Exception as e:
            return _fail("查询问答分析失败", e)

    def _run_hot_questions(args: dict) -> str:
        try:
            from services.hot_questions import hot_questions_status
            days = min(max(1, int(args.get("days") or 7)), 60)
            limit = min(max(1, int(args.get("limit") or 6)), 12)
            st = hot_questions_status(limit=limit, days=days) or {}
            items = st.get("items") or []
            if not items:
                return "当前没有统计到热点问题（可用 refresh_hot_questions 立即重算）。"
            return (f"当前热点问题 {len(items)} 条"
                    f"（统计窗口 {st.get('days', days)} 天，更新于 {st.get('updated_at') or '未知'}）：\n"
                    + "\n".join(f"- {it.get('question')}" for it in items))
        except Exception as e:
            return _fail("查询热点问题失败", e)

    def _run_system_status(args: dict) -> str:
        try:
            from core.cache import redis_cache
            from core.vector_store import vector_store
            redis_ok = redis_cache.available
            milvus_ok, ent = False, -1
            try:
                vs = vector_store.get_collection_stats() or {}
                ent = vs.get("num_entities", -1)
                milvus_ok = ent >= 0
            except Exception:
                pass
            return (f"系统状态：Redis {'已连接' if redis_ok else '未连接'}、"
                    f"Milvus {'已连接' if milvus_ok else '未连接'}"
                    + (f"（向量 {ent} 条）" if ent >= 0 else ""))
        except Exception as e:
            return _fail("查询系统状态失败", e)

    # ---------------- B. 问答记录（只读） ----------------
    def _run_list_questions(args: dict) -> str:
        try:
            from database.db import list_question_logs, count_question_logs
            page = max(1, int(args.get("page") or 1))
            kw = str(args.get("keyword") or "").strip()
            page_size = min(max(1, int(args.get("page_size") or 10)), 30)
            total = count_question_logs(kw)
            items = list_question_logs(page=page, page_size=page_size, keyword=kw) or []
            if not items:
                return f"第 {page} 页没有问答记录" + (f"（关键词「{kw}」）" if kw else "") + "。"
            lines = [f"问答记录共 {total} 条（第 {page} 页，{len(items)} 条）："]
            for q in items:
                answer = (q.get("answer") or "").replace("\n", " ")[:40]
                lines.append(f"- {str(q.get('created_at') or '')[:16]}｜{q.get('question')}"
                             f"｜→ {answer}")
            return "\n".join(lines)
        except Exception as e:
            return _fail("查询问答记录失败", e)

    # ---------------- C. 卡产品 ----------------
    def _run_list_cards(args: dict) -> str:
        try:
            from database.db import list_cards
            cards = list_cards() or []
            if not cards:
                return "当前没有任何卡产品。"
            on = [c for c in cards if (c.get("status") or "on_sale") == "on_sale"]
            return (f"卡产品共 {len(cards)} 个（在售 {len(on)} 个）：\n"
                    + "\n".join(_card_line(c) for c in cards))
        except Exception as e:
            return _fail("查询卡产品失败", e)

    def _run_create_card(args: dict) -> str:
        try:
            from database.db import create_card
            name = str(args.get("name") or "").strip()
            if not name:
                return "❌ 缺少卡名称（name）。"
            if args.get("price_yuan") is None:
                return "❌ 缺少价格（price_yuan，单位：元）。"
            price = _yuan_to_fen(args["price_yuan"])
            if price <= 0:
                return "❌ 价格必须大于 0 元。"
            card = create_card(name, price, str(args.get("description") or ""),
                               int(args.get("sort") or 0))
            if not card:
                return f"❌ 创建失败：名为「{name}」的卡产品已存在。"
            logger.info(f"[admin_assistant] create_card name={name} price={price}")
            return (f"✅ 已创建卡产品：{_card_line(card)}\n"
                    f"（默认状态为在售；如需隐藏，可再调 set_card_status 下架）")
        except Exception as e:
            return _fail("创建卡产品失败", e)

    def _run_update_card(args: dict) -> str:
        try:
            from database.db import update_card, get_card
            card_id = args.get("card_id")
            if card_id is None:
                return "❌ 缺少 card_id。若不知道 id，请先调用 list_cards_admin。"
            cur = get_card(int(card_id))
            if not cur:
                return f"❌ 未找到 id={card_id} 的卡产品（请先用 list_cards_admin 确认 id）。"
            fields: dict = {}
            if args.get("name") is not None:
                fields["name"] = str(args["name"]).strip()
            if args.get("price_yuan") is not None:
                fen = _yuan_to_fen(args["price_yuan"])
                if fen <= 0:
                    return "❌ 价格必须大于 0 元。"
                fields["price"] = fen
            if args.get("description") is not None:
                fields["description"] = str(args["description"])
            if args.get("sort") is not None:
                fields["sort"] = int(args["sort"])
            if args.get("status") is not None:
                st = str(args["status"])
                if st not in ("on_sale", "off_sale"):
                    return "❌ status 只能是 on_sale（上架）或 off_sale（下架）。"
                fields["status"] = st
            if not fields:
                return "❌ 没有给出要修改的字段（name / price_yuan / description / sort / status）。"
            before = _card_line(cur)
            card = update_card(int(card_id), **fields)
            if not card:
                return "❌ 更新失败（可能与已有卡同名）。"
            logger.info(f"[admin_assistant] update_card id={card_id} fields={fields}")
            return f"✅ 已更新卡产品：\n- 修改前：{before}\n- 修改后：{_card_line(card)}"
        except Exception as e:
            return _fail("更新卡产品失败", e)

    def _run_set_card_status(args: dict) -> str:
        try:
            from database.db import update_card, get_card
            card_id = args.get("card_id")
            status = str(args.get("status") or "")
            if card_id is None:
                return "❌ 缺少 card_id（可先调用 list_cards_admin 查 id）。"
            if status not in ("on_sale", "off_sale"):
                return "❌ status 只能是 on_sale（上架）或 off_sale（下架）。"
            cur = get_card(int(card_id))
            if not cur:
                return f"❌ 未找到 id={card_id} 的卡产品。"
            if (cur.get("status") or "on_sale") == status:
                return f"ℹ️ 「{cur['name']}」当前已经是{'在售' if status == 'on_sale' else '已下架'}状态，无需改动。"
            update_card(int(card_id), status=status)
            logger.info(f"[admin_assistant] set_card_status id={card_id} -> {status}")
            return (f"✅ 已{'上架' if status == 'on_sale' else '下架'}「{cur['name']}」"
                    f"（id={card_id}，{_fen(cur.get('price'))}）。")
        except Exception as e:
            return _fail("修改卡状态失败", e)

    def _run_delete_card(args: dict) -> str:
        try:
            from database.db import get_card
            card_id = args.get("card_id")
            if card_id is None:
                return "❌ 缺少 card_id。"
            cur = get_card(int(card_id))
            if not cur:
                return f"❌ 未找到 id={card_id} 的卡产品（请先用 list_cards_admin 确认 id）。"
        except Exception as e:
            return _fail("查询卡产品失败", e)
        preview = (f"删除卡产品：「{cur['name']}」（id={card_id}，{_fen(cur.get('price'))}）。"
                   f"删除后该卡不再出现在卡列表，已购买的用户持卡记录不受影响。")
        c = _confirm(ctx, "delete_card", args, preview)
        if c:
            return c
        try:
            from database.db import delete_card
            ok = delete_card(int(card_id))
            logger.info(f"[admin_assistant] delete_card id={card_id} ok={ok}")
            return (f"✅ 已删除卡产品「{cur['name']}」（id={card_id}）。"
                    if ok else f"❌ 删除失败：id={card_id} 不存在。")
        except Exception as e:
            return _fail("删除卡产品失败", e)

    # ---------------- D. 办卡账单与持卡 ----------------
    def _run_list_orders(args: dict) -> str:
        try:
            from database.db import list_all_orders, count_all_orders, get_order_stats
            status = str(args.get("status") or "").strip()
            kw = str(args.get("keyword") or "").strip()
            page = max(1, int(args.get("page") or 1))
            page_size = min(max(1, int(args.get("page_size") or 10)), 30)
            total = count_all_orders(status=status, keyword=kw)
            items = list_all_orders(status=status, keyword=kw,
                                    page=page, page_size=page_size) or []
            s = get_order_stats() or {}
            head = (f"办卡账单：已支付 {s.get('paid_count', 0)} 笔"
                    f"（{_fen(s.get('paid_amount'))}），已退卡 {s.get('refunded_count', 0)} 笔，"
                    f"待支付 {s.get('pending_count', 0)} 笔")
            if not items:
                return head + f"\n筛选条件下没有订单（status={status or '全部'}，关键词={kw or '无'}）。"
            return head + f"\n符合条件共 {total} 笔，第 {page} 页：\n" + \
                "\n".join(_order_line(o) for o in items)
        except Exception as e:
            return _fail("查询办卡账单失败", e)

    def _run_refund_order(args: dict) -> str:
        try:
            from database.db import get_card_order_by_no
            order_no = str(args.get("order_no") or "").strip()
            if not order_no:
                return "❌ 缺少 order_no。"
            o = get_card_order_by_no(order_no)
            if not o:
                return f"❌ 未找到订单 {order_no}（请先用 list_orders 确认订单号）。"
        except Exception as e:
            return _fail("查询订单失败", e)
        preview = (f"退卡退款：订单 {order_no}｜卡「{o.get('card_name')}」｜"
                   f"金额 {_fen(o.get('amount'))}｜用户 {o.get('user_id')}｜"
                   f"当前状态 {o.get('status')}。退卡不可逆，用户持卡将立即失效、余币扣减。")
        c = _confirm(ctx, "refund_order", args, preview)
        if c:
            return c
        try:
            from database.db import refund_order
            res = refund_order(order_no) or {}
            logger.info(f"[admin_assistant] refund_order {order_no} ok={res.get('ok')} "
                        f"msg={res.get('msg')}")
            return ("✅ " if res.get("ok") else "❌ ") + str(res.get("msg") or "退卡失败")
        except Exception as e:
            return _fail("退卡失败", e)

    def _run_adjust_coins(args: dict) -> str:
        try:
            from database.db import list_all_orders
            uc_id = args.get("uc_id")
            coins = args.get("coins")
            if uc_id is None or coins is None:
                return "❌ 需要同时提供 uc_id 与调整后的 coins（可先用 list_orders 查 uc_id）。"
            coins = int(coins)
            if coins < 0:
                return "❌ 余币数不能为负。"
            # 存在性 + 现状（uc_id 来自订单联表，这里反查一遍确认有效）
            cur_uc = next((o for o in (list_all_orders(page=1, page_size=1000) or [])
                           if o.get("uc_id") == int(uc_id)), None)
            if not cur_uc:
                return f"❌ 未找到持卡记录 uc_id={uc_id}（请用 list_orders 查正确的 uc_id）。"
            # 已是目标值 → 短路（MySQL 同值 UPDATE 的 rowcount 为 0，会被误读成"记录不存在"）
            if cur_uc.get("uc_coins") is not None and int(cur_uc["uc_coins"]) == coins:
                return (f"ℹ️ 持卡 uc_id={uc_id}（「{cur_uc.get('card_name')}」）当前就是 "
                        f"{coins} 币，无需调整。")
        except Exception as e:
            return _fail("查询持卡记录失败", e)
        preview = (f"调整余币：持卡 uc_id={uc_id}｜卡「{cur_uc.get('card_name')}」｜"
                   f"用户 {cur_uc.get('nickname') or cur_uc.get('user_id')}｜"
                   f"当前 {cur_uc.get('uc_coins')} 币 → 调整为 {coins} 币。"
                   f"该操作直接改钱，不可自动回滚。")
        c = _confirm(ctx, "adjust_user_card_coins", args, preview)
        if c:
            return c
        try:
            from database.db import set_user_card_coins
            ok = set_user_card_coins(int(uc_id), coins)
            logger.info(f"[admin_assistant] adjust_coins uc_id={uc_id} coins={coins} ok={ok}")
            return (f"✅ 已将持卡 uc_id={uc_id} 的余币调整为 {coins} 币"
                    if ok else f"❌ 调整失败：持卡记录 uc_id={uc_id} 不存在或已失效。")
        except Exception as e:
            return _fail("调整余币失败", e)

    def _run_user_card_summary(args: dict) -> dict:
        try:
            from database.db import list_all_orders, sum_checkin_coins, get_user_by_id
            orders = list_all_orders(page=1, page_size=1000) or []
            agg: dict = {}
            for o in orders:
                uid = o.get("user_id")
                if uid is None:
                    continue
                s = agg.setdefault(uid, {"cards": 0, "coins": 0, "nick": ""})
                m = re.search(r"(\d+)\s*币", o.get("card_name") or "")
                c = int(o.get("uc_coins") if o.get("uc_coins") is not None
                        else (m.group(1) if m else 0))
                if o.get("status") == "paid" and o.get("uc_status") == "active":
                    s["cards"] += 1
                    s["coins"] += c
            for uid, s in agg.items():
                s["coins"] += sum_checkin_coins(uid)
                u = get_user_by_id(uid)
                if u:
                    s["nick"] = u.get("nickname") or u.get("username") or ""
            top = sorted(agg.items(), key=lambda kv: -kv[1]["coins"])[:10]
            total_coins = sum(s["coins"] for s in agg.values())
            active_cards = sum(s["cards"] for s in agg.values())
            text = (f"持卡用户 {len(agg)} 人，有效持卡 {active_cards} 张，"
                    f"余币合计 {total_coins} 币。余币 Top10：\n"
                    + "\n".join(f"- {s['nick'] or ('用户' + str(uid))}(id={uid})："
                                f"{s['cards']} 张卡，{s['coins']} 币" for uid, s in top))
            cards = [_stat("持卡用户", len(agg), "人"),
                     _stat("有效持卡", active_cards, "张"),
                     _stat("余币合计", total_coins, "币")]
            charts = []
            if top:
                charts.append(_chart("余币 Top10（币）", _opt_hbar(
                    [(s["nick"] or f"用户{uid}", int(s["coins"])) for uid, s in top],
                    "#10B981")))
            return _report(text, cards, charts)
        except Exception as e:
            return _fail("查询持卡汇总失败", e)

    # ---------------- E. 游乐消费单 ----------------
    def _run_list_consumptions(args: dict) -> str:
        """按状态 / 日期筛选消费单（供"把昨天未核销的都核销"这类批量指令取单号）"""
        try:
            from database.db import list_all_consumptions, count_all_consumptions
            from datetime import date, timedelta
            status = str(args.get("status") or "").strip()
            date_arg = str(args.get("date") or "").strip().lower()
            limit = min(max(1, int(args.get("limit") or 20)), 100)

            if date_arg in ("today", "今天"):
                want = date.today().isoformat()
            elif date_arg in ("yesterday", "昨天"):
                want = (date.today() - timedelta(days=1)).isoformat()
            else:
                want = date_arg      # 具体日期前缀（2026-09-16）或空

            total = count_all_consumptions()
            picked, scanned = [], 0
            page = 1
            while len(picked) < limit and scanned < 1000 and scanned < total:
                batch = list_all_consumptions(page=page, page_size=100) or []
                if not batch:
                    break
                scanned += len(batch)
                for o in batch:
                    if status and (o.get("status") or "unused") != status:
                        continue
                    if want and not str(o.get("created_at") or "").startswith(want):
                        continue
                    picked.append(o)
                    if len(picked) >= limit:
                        break
                page += 1

            con = f"消费单共 {total} 笔"
            filt = []
            if status:
                filt.append({"unused": "未核销", "used": "已核销",
                             "refunded": "已退款"}.get(status, status))
            if want:
                filt.append(f"{want} 当天")
            if not picked:
                return con + ("，没有符合条件的记录（" + "、".join(filt) + "）。"
                              if filt else "，没有记录。")
            lines = [con + f"，符合条件 {len(picked)} 笔"
                     + ("（" + "、".join(filt) + "）" if filt else "") + "："]
            for o in picked:
                try:
                    items = json.loads(o.get("items_json") or "[]")
                except Exception:
                    items = []
                lines.append(_consumption_line(o, items))
            if len(picked) >= limit:
                lines.append(f"（仅列出前 {limit} 笔；如需更多可调大 limit）")
            return "\n".join(lines)
        except Exception as e:
            return _fail("查询消费单失败", e)

    def _run_get_consumption_by_code(args: dict) -> str:
        try:
            from database.db import get_consumption_by_code
            code = str(args.get("verify_code") or "").strip().upper()
            if not code:
                return "❌ 缺少核销码（verify_code）。"
            o = get_consumption_by_code(code)
            if not o:
                return f"❌ 核销码 {code} 不存在（请让游客核对核销码）。"
            try:
                items = json.loads(o.get("items_json") or "[]")
            except Exception:
                items = []
            return "查到消费单：\n" + _consumption_line(o, items)
        except Exception as e:
            return _fail("按核销码查询失败", e)

    def _run_verify_consumption(args: dict) -> str:
        try:
            from database.db import get_consumption_by_no, mark_consumption_used
            order_no = str(args.get("order_no") or "").strip()
            if not order_no:
                return "❌ 缺少 order_no（可先调用 list_consumptions 拿单号）。"
            o = get_consumption_by_no(order_no)
            if not o:
                return f"❌ 未找到消费单 {order_no}。"
            if o.get("status") == "used":
                return f"ℹ️ 消费单 {order_no} 已经核销过，未重复操作。"
            if o.get("status") == "refunded":
                return f"❌ 消费单 {order_no} 已退款，不能核销。"
            ok = mark_consumption_used(order_no)
            logger.info(f"[admin_assistant] verify_consumption {order_no} ok={ok}")
            return (f"✅ 已核销消费单 {order_no}（{o.get('total_coins') or 0} 币）"
                    if ok else f"❌ 核销失败：{order_no} 状态已变化，请刷新后再试。")
        except Exception as e:
            return _fail("核销失败", e)

    def _run_refund_consumption(args: dict) -> str:
        try:
            from database.db import get_consumption_by_no
            order_no = str(args.get("order_no") or "").strip()
            if not order_no:
                return "❌ 缺少 order_no。"
            o = get_consumption_by_no(order_no)
            if not o:
                return f"❌ 未找到消费单 {order_no}。"
        except Exception as e:
            return _fail("查询消费单失败", e)
        preview = (f"消费单退款：{order_no}｜共 {o.get('total_coins') or 0} 币｜"
                   f"当前状态 {o.get('status')}｜用户 {o.get('user_id')}。"
                   f"退款后币全额退回用户的卡（该单所有还没退过的券一起退，已核销的项目也包含），不可撤销。")
        c = _confirm(ctx, "refund_consumption", args, preview)
        if c:
            return c
        try:
            from database.db import refund_consumption_order
            res = refund_consumption_order(order_no) or {}
            logger.info(f"[admin_assistant] refund_consumption {order_no} ok={res.get('ok')}")
            return ("✅ " if res.get("ok") else "❌ ") + str(res.get("msg") or "退款失败")
        except Exception as e:
            return _fail("消费单退款失败", e)

    # ---------------- F. 失物招领 ----------------
    def _run_list_lost(args: dict) -> str:
        try:
            from database.db import list_lost_items, count_lost_items
            status = str(args.get("status") or "").strip()
            source = str(args.get("source") or "").strip()
            kw = str(args.get("keyword") or "").strip()
            page = max(1, int(args.get("page") or 1))
            page_size = min(max(1, int(args.get("page_size") or 20)), 50)
            total = count_lost_items(status=status, source=source, keyword=kw)
            items = list_lost_items(status=status, source=source, keyword=kw,
                                    page=page, page_size=page_size) or []
            if not items:
                return f"失物库没有符合条件的记录（共 {total} 条）。"
            lines = [f"失物记录共 {total} 条，第 {page} 页 {len(items)} 条："]
            for it in items:
                lines.append(
                    f"- id={it['id']}｜{it['name']}｜"
                    f"{'待认领' if (it.get('status') or 'lost') == 'lost' else '已认领'}｜"
                    f"{'游客报失' if (it.get('source') or 'user') == 'user' else '商家登记'}"
                    f"｜{it.get('reporter') or ''}｜{str(it.get('created_at') or '')[:16]}")
            return "\n".join(lines)
        except Exception as e:
            return _fail("查询失物列表失败", e)

    def _run_create_lost(args: dict) -> str:
        try:
            from database.db import create_lost_item
            name = str(args.get("name") or "").strip()
            if not name:
                return "❌ 缺少失物名称（name）。"
            it = create_lost_item(name=name, photo_url="", source="merchant",
                                  reporter=str(args.get("reporter") or "商家"))
            if not it:
                return "❌ 登记失败，请稍后重试。"
            logger.info(f"[admin_assistant] create_lost_item name={name}")
            return (f"✅ 已登记失物「{name}」（id={it['id']}，来源：商家登记）。\n"
                    f"如需补照片，请到「失物招领」页编辑该记录上传。")
        except Exception as e:
            return _fail("登记失物失败", e)

    def _run_lost_status(args: dict) -> str:
        try:
            from database.db import get_lost_item, update_lost_item_status
            item_id = args.get("item_id")
            status = str(args.get("status") or "")
            if item_id is None:
                return "❌ 缺少 item_id（可先调用 list_lost_items_admin）。"
            if status not in ("lost", "claimed"):
                return "❌ status 只能是 lost（待认领）或 claimed（已认领）。"
            it = get_lost_item(int(item_id))
            if not it:
                return f"❌ 未找到失物 id={item_id}。"
            ok = update_lost_item_status(int(item_id), status)
            logger.info(f"[admin_assistant] lost_status id={item_id} -> {status} ok={ok}")
            return (f"✅ 「{it['name']}」已标记为{'已认领' if status == 'claimed' else '待认领'}"
                    if ok else "❌ 状态更新失败。")
        except Exception as e:
            return _fail("更新失物状态失败", e)

    def _run_delete_lost(args: dict) -> str:
        try:
            from database.db import get_lost_item
            item_id = args.get("item_id")
            if item_id is None:
                return "❌ 缺少 item_id。"
            it = get_lost_item(int(item_id))
            if not it:
                return f"❌ 未找到失物 id={item_id}。"
        except Exception as e:
            return _fail("查询失物失败", e)
        preview = (f"删除失物：id={item_id}｜「{it['name']}」｜"
                   f"{'待认领' if (it.get('status') or 'lost') == 'lost' else '已认领'}。"
                   f"删除后记录与照片不可恢复。")
        c = _confirm(ctx, "delete_lost_item", args, preview)
        if c:
            return c
        try:
            from database.db import delete_lost_item
            photo_url = it.get("photo_url") or ""
            if photo_url.startswith("/static/"):
                path = os.path.join(".", photo_url.lstrip("/"))
                if os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            ok = delete_lost_item(int(item_id))
            logger.info(f"[admin_assistant] delete_lost_item id={item_id} ok={ok}")
            return (f"✅ 已删除失物「{it['name']}」（id={item_id}）"
                    if ok else "❌ 删除失败。")
        except Exception as e:
            return _fail("删除失物失败", e)

    # ---------------- G. 公告 / 热点 / 反馈 ----------------
    def _run_list_notices(args: dict) -> str:
        try:
            from database.db import list_park_notices, count_park_notices
            kind = str(args.get("kind") or "").strip()
            status = str(args.get("status") or "").strip()
            if kind and kind not in ("announcement", "hot"):
                return "❌ kind 只能是 announcement（园区公告）或 hot（今日热点）。"
            total = count_park_notices(kind=kind, status=status)
            items = list_park_notices(kind=kind, status=status,
                                      page=1, page_size=50) or []
            if not items:
                return f"没有符合条件的公告/热点（共 {total} 条）。"
            lines = [f"公告/热点共 {total} 条，列出 {len(items)} 条："]
            for n in items:
                lines.append(
                    f"- id={n['id']}｜{'园区公告' if n.get('kind') == 'announcement' else '今日热点'}"
                    f"｜{n.get('title')}｜{'上架' if (n.get('status') or 'on') == 'on' else '已下架'}"
                    f"｜排序 {n.get('sort') or 0}"
                    + (f"｜{str(n.get('content'))[:30]}" if n.get("content") else ""))
            return "\n".join(lines)
        except Exception as e:
            return _fail("查询公告失败", e)

    def _run_list_feedback(args: dict) -> str:
        try:
            from database.db import list_user_feedback, count_user_feedback
            page = max(1, int(args.get("page") or 1))
            page_size = min(max(1, int(args.get("page_size") or 10)), 50)
            total = count_user_feedback()
            items = list_user_feedback(page=page, page_size=page_size) or []
            if not items:
                return f"暂无游客反馈（共 {total} 条）。"
            lines = [f"游客反馈共 {total} 条，第 {page} 页 {len(items)} 条："]
            for f in items:
                lines.append(f"- {str(f.get('created_at') or '')[:16]}｜"
                             f"{f.get('nickname') or '匿名'}："
                             f"{(f.get('content') or '').replace(chr(10), ' ')[:60]}")
            return "\n".join(lines)
        except Exception as e:
            return _fail("查询反馈失败", e)

    def _run_create_notice(args: dict) -> str:
        try:
            from database.db import create_park_notice
            kind = str(args.get("kind") or "announcement")
            title = str(args.get("title") or "").strip()
            status = str(args.get("status") or "on")
            if kind not in ("announcement", "hot"):
                return "❌ kind 只能是 announcement（园区公告）或 hot（今日热点）。"
            if not title:
                return "❌ 缺少标题（title）。"
            if status not in ("on", "off"):
                return "❌ status 只能是 on（上架）或 off（下架）。"
            n = create_park_notice(kind=kind, title=title,
                                   content=str(args.get("content") or ""),
                                   sort=int(args.get("sort") or 0), status=status)
            if not n:
                return "❌ 发布失败，请稍后重试。"
            logger.info(f"[admin_assistant] create_notice {kind} {title}")
            return (f"✅ 已发布{'园区公告' if kind == 'announcement' else '今日热点'}："
                    f"「{title}」（id={n['id']}，状态：{'上架' if status == 'on' else '下架'}）")
        except Exception as e:
            return _fail("发布公告失败", e)

    def _run_update_notice(args: dict) -> str:
        try:
            from database.db import get_park_notice, update_park_notice
            notice_id = args.get("notice_id")
            if notice_id is None:
                return "❌ 缺少 notice_id（可先调用 list_notices_admin）。"
            cur = get_park_notice(int(notice_id))
            if not cur:
                return f"❌ 未找到公告 id={notice_id}。"
            fields = {}
            for k in ("title", "content", "status", "sort", "kind"):
                if args.get(k) is not None:
                    fields[k] = args[k]
            if "kind" in fields and fields["kind"] not in ("announcement", "hot"):
                return "❌ kind 只能是 announcement 或 hot。"
            if "status" in fields and fields["status"] not in ("on", "off"):
                return "❌ status 只能是 on 或 off。"
            if not fields:
                return "❌ 没有给出要修改的字段。"
            ok = update_park_notice(int(notice_id), **fields)
            logger.info(f"[admin_assistant] update_notice id={notice_id} fields={list(fields)}")
            if not ok:
                return "❌ 更新失败。"
            n = get_park_notice(int(notice_id)) or {}
            return (f"✅ 已更新公告「{n.get('title')}」（id={notice_id}，"
                    f"状态：{'上架' if (n.get('status') or 'on') == 'on' else '下架'}）")
        except Exception as e:
            return _fail("更新公告失败", e)

    def _run_delete_notice(args: dict) -> str:
        try:
            from database.db import get_park_notice
            notice_id = args.get("notice_id")
            if notice_id is None:
                return "❌ 缺少 notice_id。"
            n = get_park_notice(int(notice_id))
            if not n:
                return f"❌ 未找到公告 id={notice_id}。"
        except Exception as e:
            return _fail("查询公告失败", e)
        preview = (f"删除公告：id={notice_id}｜「{n.get('title')}」｜"
                   f"{'园区公告' if n.get('kind') == 'announcement' else '今日热点'}。"
                   f"删除后小程序端立即不可见。")
        c = _confirm(ctx, "delete_notice", args, preview)
        if c:
            return c
        try:
            from database.db import delete_park_notice
            ok = delete_park_notice(int(notice_id))
            logger.info(f"[admin_assistant] delete_notice id={notice_id} ok={ok}")
            return (f"✅ 已删除公告「{n.get('title')}」" if ok else "❌ 删除失败。")
        except Exception as e:
            return _fail("删除公告失败", e)

    def _run_refresh_hot(args: dict) -> str:
        try:
            from services.hot_questions import refresh_hot_questions, hot_questions_status
            days = min(max(1, int(args.get("days") or 7)), 60)
            limit = min(max(1, int(args.get("limit") or 6)), 12)
            res = refresh_hot_questions(days=days, limit=limit, force=True) or {}
            items = res.get("items") or []
            st = hot_questions_status(limit=limit, days=days) or {}
            logger.info(f"[admin_assistant] refresh_hot_questions days={days} n={len(items)}")
            body = "\n".join(f"- {it.get('question')}" for it in items) or "（无）"
            return (f"✅ 已按近 {days} 天问答记录重算热点问题，共 {len(items)} 条：\n{body}\n"
                    f"更新于 {st.get('updated_at') or '刚刚'}。")
        except Exception as e:
            return _fail("刷新热点问题失败", e)

    # ---------------- H. 知识库 ----------------
    def _run_kb_status(args: dict) -> str:
        try:
            from agents.document_agent import document_agent
            from agents.retrieval_agent import retrieval_agent
            docs = document_agent.list_documents() or []
            chunks = document_agent.list_all_chunk_files() or []
            total = sum(int(c.get("total_chunks") or 0) for c in chunks)
            idx = {}
            try:
                idx = retrieval_agent.get_index_stats() or {}
            except Exception:
                pass
            out = [f"知识库：文档 {len(docs)} 篇，切片 {total} 块；"
                   f"向量 {idx.get('milvus_entities', 0)} 条，BM25 {idx.get('bm25_documents', 0)} 条"]
            if args.get("list_docs"):
                out.append("文档清单（最近 10 篇）：")
                for d in sorted(docs, reverse=True)[:10]:
                    cm = next((c for c in chunks if c.get("doc_name") == d), {})
                    out.append(f"- {d}（{cm.get('total_chunks', 0)} 块）")
            else:
                out.append("（如需文档清单，带上 list_docs=true）")
            out.append("如需补充知识库，可在弹窗中点「知识库智能体」去生成/回传 Word。")
            return "\n".join(out)
        except Exception as e:
            return _fail("查询知识库状态失败", e)

    def _run_delete_kb_doc(args: dict) -> str:
        try:
            from agents.document_agent import document_agent
            from config import DOCS_DIR
            doc_name = str(args.get("doc_name") or "").strip()
            if not doc_name:
                return "❌ 缺少 doc_name。"
            path = os.path.join(DOCS_DIR, os.path.basename(doc_name))
            if not os.path.isfile(path):
                docs = document_agent.list_documents() or []
                like = [d for d in docs if doc_name in d][:5]
                return (f"❌ 未找到文档「{doc_name}」。"
                        + (f"相近的有：{'、'.join(like)}" if like else ""))
        except Exception as e:
            return _fail("查询文档失败", e)
        preview = (f"删除知识库文档：{doc_name}。将同时从 Milvus 向量库与 BM25 索引中移除，"
                   f"删除后用户端立检索不到该文档内容，不可恢复。")
        c = _confirm(ctx, "delete_kb_document", args, preview)
        if c:
            return c
        try:
            from agents.document_agent import document_agent
            from config import DOCS_DIR
            path = os.path.join(DOCS_DIR, os.path.basename(doc_name))
            try:
                document_agent.delete_document(doc_name)
            except Exception as e:
                logger.warning(f"[admin_assistant] 索引删除失败（继续删文件）: {e}")
            if os.path.exists(path):
                os.remove(path)
            try:
                from services.kb_dedup import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
            logger.info(f"[admin_assistant] delete_kb_document {doc_name}")
            return f"✅ 已删除知识库文档「{doc_name}」（含向量/BM25 索引与磁盘文件）。"
        except Exception as e:
            return _fail("删除文档失败", e)

    def _run_kb_analyze(args: dict) -> str:
        """内部同步收集 analyze_stream 的事件（不做嵌套 SSE），只回摘要 + 产物地址"""
        try:
            from services.kb_agent import kb_agent
            days = max(0, int(args.get("days") or 0))
            limit = min(max(1, int(args.get("limit") or 3)), 5)
            max_tokens = min(max(64, int(args.get("max_tokens") or 200)), 300)
            generate = bool(args.get("generate", False))
            export = bool(args.get("export", False)) and generate
            answers, artifact, err, gap_n = [], None, "", 0
            for ev in kb_agent.analyze_stream(message="", days=days, limit=limit,
                                              max_tokens=max_tokens,
                                              generate=generate, export=export):
                t = ev.get("type")
                if t == "dataset":
                    gap_n = len(ev.get("items") or [])
                elif t == "answer":
                    answers.append(ev)
                elif t == "artifact":
                    artifact = ev
                elif t == "error":
                    err = ev.get("content") or "未知错误"
            logger.info(f"[admin_assistant] run_kb_analyze days={days} limit={limit} "
                        f"gaps={gap_n} answers={len(answers)}")
            if err:
                return f"❌ 知识库智能体执行出错：{err}"
            lines = [f"✅ 已统计知识库盲区：识别 {gap_n} 个未命中问题"
                     f"（最多处理 {limit} 个，时间范围 {'不限' if days == 0 else f'近 {days} 天'}）。"]
            if generate:
                need = [a for a in answers if a.get("needs_review")]
                lines.append(f"已基于知识库生成 {len(answers)} 条标准答案"
                             + (f"，其中 {len(need)} 条资料不足需人工复核" if need else "。"))
            if artifact:
                lines.append(f"已生成文档：{artifact.get('name')}"
                             f"（{artifact.get('items', 0)} 条）→ {artifact.get('url')}")
            if not generate:
                lines.append("（本次只统计，未生成答案与文档；需要出文档可再说一句）")
            return "\n".join(lines)
        except Exception as e:
            return _fail("执行知识库智能体失败", e)

    # ---------------- I. 用户与系统 ----------------
    def _run_list_users(args: dict) -> str:
        try:
            from database.db import search_users
            kw = str(args.get("keyword") or "").strip()
            role = str(args.get("role") or "").strip()
            page = max(1, int(args.get("page") or 1))
            page_size = min(max(1, int(args.get("page_size") or 10)), 30)
            res = search_users(keyword=kw, only_bound=False, page=page, limit=page_size) or {}
            users = res.get("users") or []
            if role:
                users = [u for u in users if (u.get("role") or "user") == role]
            if not users:
                return f"没有符合条件的用户（关键词「{kw or '无'}」）。"
            return (f"用户共 {res.get('total', 0)} 人，第 {page} 页列出 {len(users)} 人：\n"
                    + "\n".join(
                        f"- id={u.get('id')}｜{u.get('nickname') or u.get('username')}"
                        f"｜{u.get('phone') or '未绑手机'}"
                        f"｜{'管理员' if u.get('role') == 'admin' else '普通用户'}"
                        f"｜注册 {str(u.get('created_at') or '')[:10]}" for u in users))
        except Exception as e:
            return _fail("查询用户失败", e)

    def _run_get_agent_config(args: dict) -> str:
        try:
            from api.agent_config import _runtime_config
            from config import (TOP_K, RERANK_TOP, SIMILARITY_THRESHOLD, HYBRID_ALPHA,
                                CHUNK_SIZE, CHUNK_OVERLAP, LLM_TEMPERATURE, CACHE_TTL,
                                LLM_TEXT_MODEL)
            g = _runtime_config.get
            return (f"当前 Agent 配置（运行时值，重启可能回默认）：\n"
                    f"- 检索：top_k={g('top_k', TOP_K)}，rerank_top={g('rerank_top', RERANK_TOP)}，"
                    f"阈值={g('similarity_threshold', SIMILARITY_THRESHOLD)}，"
                    f"hybrid_alpha={g('hybrid_alpha', HYBRID_ALPHA)}\n"
                    f"- 分块：chunk_size={g('chunk_size', CHUNK_SIZE)}，"
                    f"overlap={g('chunk_overlap', CHUNK_OVERLAP)}\n"
                    f"- LLM：{LLM_TEXT_MODEL}，temperature={g('llm_temperature', LLM_TEMPERATURE)}\n"
                    f"- 缓存 TTL：{g('cache_ttl', CACHE_TTL)} 秒")
        except Exception as e:
            return _fail("读取 Agent 配置失败", e)

    def _run_clear_cache(args: dict) -> str:
        try:
            from core.cache import redis_cache
            if not redis_cache.available:
                return "ℹ️ Redis 当前不可用，无需清理。"
            scope = str(args.get("scope") or "search")
            if scope == "all":
                redis_cache.client.flushdb()
                logger.info("[admin_assistant] clear_cache scope=all")
                return "✅ 已清空全部缓存（含问答缓存与计数器）。"
            keys = redis_cache.client.keys("rag:search:*")
            if keys:
                redis_cache.client.delete(*keys)
            logger.info(f"[admin_assistant] clear_cache scope=search n={len(keys) if keys else 0}")
            return f"✅ 已清除 {len(keys) if keys else 0} 条检索缓存（问答结果重新计算，首次会变慢）。"
        except Exception as e:
            return _fail("清理缓存失败", e)

    def _run_update_agent_config(args: dict) -> str:
        fields = {}
        for k in ("top_k", "rerank_top", "similarity_threshold", "hybrid_alpha",
                  "chunk_size", "chunk_overlap", "llm_temperature", "cache_ttl"):
            v = args.get(k)
            if v is None:
                continue
            fields[k] = float(v) if k in ("similarity_threshold", "hybrid_alpha",
                                          "llm_temperature") else int(v)
        if not fields:
            return "❌ 没有给出要修改的配置项。"
        preview = ("修改 Agent 运行时配置：" +
                   "、".join(f"{k}={v}" for k, v in fields.items()) +
                   "。该配置直接影响线上检索与回答行为（重启后回默认）。")
        c = _confirm(ctx, "update_agent_config", args, preview)
        if c:
            return c
        try:
            from api.agent_config import update_agent_config as _update, AgentConfigUpdate
            import asyncio
            req = AgentConfigUpdate(**args)
            res = asyncio.run(_update(req)) or {}
            logger.info(f"[admin_assistant] update_agent_config {fields}")
            return (f"✅ 已更新 Agent 配置（运行时生效，重启后恢复默认）："
                    f"{'、'.join(f'{k}={v}' for k, v in (res.get('changes') or fields).items())}")
        except Exception as e:
            return _fail("更新 Agent 配置失败", e)

    # ============================================================
    # 组装（对外一次性全量返回 —— 弹窗是管理员场景，不做按需挂载）
    # ============================================================
    tools = [
        # A 经营看板
        _tool("get_dashboard_overview",
              "查询经营总览：问答量、新增用户、收款/退款、订单与待办（未核销消费单、待认领失物）、知识库规模。"
              "当管理员问\"最近经营怎么样/今天数据/经营概况/整体情况\"时**必须调用**本工具取真实数据，禁止凭印象回答。",
              _obj({"days": _i("统计窗口天数，默认 7")}), _run_dashboard, R, "report"),
        _tool("get_revenue_summary",
              "查询**营收汇总**：指定周期的收款合计、订单笔数、各卡产品销售明细、收款趋势。"
              "当管理员问\"今天赚了多少/今日营收/营收汇总/收入怎么样/卖了多少钱/各卡卖得如何\"时"
              "**优先调用本工具**（比 get_dashboard_overview 更聚焦钱，且带销售图表）。",
              _obj({"days": _i("统计周期天数：1=今天（默认），7=近 7 天")}),
              _run_revenue, R, "report"),
        _tool("get_analytics_overview",
              "查询问答分析：高频问题 Top10、意图分布、每日问答趋势、满意度分布。"
              "当管理员问\"哪些问题问得多/用户都在问什么/问答趋势/满意度\"时**必须调用**。",
              _obj({"days": _i("统计窗口天数，默认 7")}), _run_analytics, R, "report"),
        _tool("get_hot_questions",
              "查询小程序当前快捷提问所用的热点问题批次（近 N 天 Top N）。",
              _obj({"days": _i("统计窗口天数，默认 7"), "limit": _i("条数，默认 6，最多 12")}),
              _run_hot_questions, R, "report"),
        _tool("get_system_status",
              "查询系统组件状态（Redis / Milvus 是否连接、向量条数）。管理员问\"系统正常吗/服务状态\"时调用。",
              _obj(), _run_system_status, R, "report"),
        # B 问答记录
        _tool("list_questions",
              "分页查询用户问答记录（问题 + 回答摘要），支持关键词模糊搜索。"
              "管理员要\"看看用户问了什么/搜某个问题的记录\"时调用。",
              _obj({"page": _i("页码，默认 1"), "keyword": _s("关键词，可为空"),
                    "page_size": _i("每页条数，默认 10，最多 30")}), _run_list_questions,
              R, "report"),
        # C 卡产品
        _tool("list_cards_admin",
              "列出**全部**卡产品（含已下架），返回 id/名称/价格/币数/状态/排序。"
              "凡涉及按名称或 id 操作某张卡之前，**必须先调用本工具确认 id**，不要猜 id。",
              _obj(), _run_list_cards, R, "report"),
        _tool("create_card",
              "新建卡产品。价格入参单位是**元**（如 49.9），内部自动换算成分。"
              "管理员说\"加一张 XX 卡/新建充币套餐\"时调用。",
              _obj({"name": _s("卡名称（唯一，重名会失败）"),
                    "price_yuan": {"type": "number", "description": "价格，单位元，如 29.9"},
                    "description": _s("卡说明（可含\"含 100 币\"这类描述）"),
                    "sort": _i("排序，越小越靠前")},
                   ["name", "price_yuan"]), _run_create_card, W),
        _tool("update_card",
              "修改卡产品的名称/价格/说明/排序/状态（只传要改的字段即可，其余保留原值）。"
              "改价格时 price_yuan 单位是**元**。管理员说\"把 XX 卡改成 59 元\"时调用。",
              _obj({"card_id": _i("卡产品 id（先用 list_cards_admin 查）"),
                    "name": _s("新名称（可选）"),
                    "price_yuan": {"type": "number", "description": "新价格，单位元（可选）"},
                    "description": _s("新说明（可选）"),
                    "sort": _i("新排序（可选）"),
                    "status": _s("on_sale 上架 / off_sale 下架（可选）")},
                   ["card_id"]), _run_update_card, W),
        _tool("set_card_status",
              "把某张卡产品上架或下架。管理员说\"把 XX 卡下架/上架/停售\"时**必须调用**本工具，"
              "不要用 update_card 以外的方式处理。card_id 需先用 list_cards_admin 查。",
              _obj({"card_id": _i("卡产品 id"),
                    "status": _s("on_sale 上架 / off_sale 下架")},
                   ["card_id", "status"]), _run_set_card_status, W),
        _tool("delete_card",
              "**删除**卡产品（高危不可逆，需管理员确认）。仅在管理员明确说\"删除这张卡/彻底移除\"时调用；"
              "若只是不想卖（停售），应改用 set_card_status 下架。",
              _obj({"card_id": _i("卡产品 id")}, ["card_id"]), _run_delete_card, X),
        # D 订单与持卡
        _tool("list_orders",
              "分页查询办卡账单（订单号/卡名/金额/币数/状态/用户/uc_id），支持按状态与关键词筛选。"
              "管理员要\"看看订单/谁买过卡/查某笔单\"时调用；退卡或调币前也要先用它确认单号与 uc_id。",
              _obj({"status": _s("paid 已支付 / refunded 已退卡 / pending 待支付；空=全部"),
                    "keyword": _s("订单号/卡名/用户昵称关键词"),
                    "page": _i("页码，默认 1"),
                    "page_size": _i("每页条数，默认 10，最多 30")}), _run_list_orders,
              R, "report"),
        _tool("get_user_card_summary",
              "查询全部用户持卡汇总：持卡人数、余币总量、余币 Top10 用户。管理员问\"谁卡里币多/持卡情况\"时调用。",
              _obj(), _run_user_card_summary, R, "report"),
        _tool("refund_order",
              "对某笔**已支付**办卡订单执行退卡退款（高危不可逆，需管理员确认）：订单标记已退卡、"
              "用户持卡立即失效、余币同步扣减。管理员明确说\"给 XX 退卡/退款\"时**必须调用**本工具（不要自己判断能不能退，交工具判断）。",
              _obj({"order_no": _s("订单号（先用 list_orders 确认）")},
                   ["order_no"]), _run_refund_order, X),
        _tool("adjust_user_card_coins",
              "把某个持卡记录（uc_id）的余币**调整为指定值**（高危不可逆，需管理员确认），用于线下补偿/纠错。"
              "管理员说\"给 XX 卡加 20 币/改成 50 币/补偿\"时调用。uc_id 先用 list_orders 查。",
              _obj({"uc_id": _i("持卡记录 id（list_orders 的 uc_id 字段）"),
                    "coins": _i("调整后的余币数（非负，是最终值不是增量）"),
                    "reason": _s("调整原因（审计用）")}, ["uc_id", "coins"]),
              _run_adjust_coins, X),
        # E 消费单
        _tool("list_consumptions",
              "分页查询游乐消费单（单号/核销码/项目/币数/状态/用户），支持按状态与日期筛选。"
              "管理员说\"看看消费记录/昨天哪些没核销/今天玩了什么\"时**必须调用**；"
              "批量核销前也要先用它拿到单号。",
              _obj({"status": _s("unused 未核销 / used 已核销 / refunded 已退款；空=全部"),
                    "date": _s("today 今天 / yesterday 昨天 / 具体日期 2026-09-16；空=不限"),
                    "limit": _i("最多返回条数，默认 20，最多 100")}), _run_list_consumptions,
              R, "report"),
        _tool("get_consumption_by_code",
              "按**核销码**查询消费单（工作人员核对游客出示的码时用）。",
              _obj({"verify_code": _s("核销码")}, ["verify_code"]),
              _run_get_consumption_by_code, R, "report"),
        _tool("verify_consumption",
              "核销一张消费单（未核销 → 已核销）。管理员说\"核销 XX/核销昨天没核销的\"时**必须调用**本工具，"
              "单号先用 list_consumptions 查。已核销或已退款的单不会重复操作。",
              _obj({"order_no": _s("消费单号")}, ["order_no"]),
              _run_verify_consumption, W),
        _tool("refund_consumption",
              "对消费单执行退款（高危不可逆，需管理员确认）：该单所有还没退过的券一起退掉，"
              "扣掉的币按原扣币比例退回用户的卡，**已核销的项目也包含在内**（用户可能刚上去就下来，"
              "由现场判定）。管理员明确说\"给这单退款/退币\"时调用。",
              _obj({"order_no": _s("消费单号")}, ["order_no"]),
              _run_refund_consumption, X),
        # F 失物招领
        _tool("list_lost_items_admin",
              "分页查询失物招领记录（含游客报失与商家登记），支持状态/来源/名称关键词筛选。"
              "管理员问\"有哪些失物/谁报失了\"时调用。",
              _obj({"status": _s("lost 待认领 / claimed 已认领；空=全部"),
                    "source": _s("user 游客报失 / merchant 商家登记；空=全部"),
                    "keyword": _s("失物名称关键词"),
                    "page": _i("页码，默认 1"),
                    "page_size": _i("每页条数，默认 20，最多 50")}), _run_list_lost,
              R, "report"),
        _tool("create_lost_item",
              "商家登记一件拾获失物（仅名称，照片需到页面补传）。管理员说\"登记一下捡到的 XX\"时调用。",
              _obj({"name": _s("失物名称，如：粉色水壶"),
                    "reporter": _s("报失/拾获人称呼（可选，默认商家）")},
                   ["name"]), _run_create_lost, W),
        _tool("update_lost_item_status",
              "把失物标记为已认领或恢复待认领。管理员说\"XX 被认领了/标记已认领\"时调用。",
              _obj({"item_id": _i("失物 id（先用 list_lost_items_admin 查）"),
                    "status": _s("claimed 已认领 / lost 恢复待认领")},
                   ["item_id", "status"]), _run_lost_status, W),
        _tool("delete_lost_item",
              "**删除**一条失物记录（高危不可逆，需管理员确认；同时清理照片）。仅管理员明确说\"删掉这条失物\"时调用。",
              _obj({"item_id": _i("失物 id")}, ["item_id"]), _run_delete_lost, X),
        # G 公告 / 热点 / 反馈
        _tool("list_notices_admin",
              "列出园区公告与今日热点（含已下架），支持 kind/status 筛选。"
              "管理员说\"看看现在挂了哪些公告/热点\"时调用；改删公告前也先用它查 id。",
              _obj({"kind": _s("announcement 园区公告 / hot 今日热点；空=全部"),
                    "status": _s("on 上架 / off 已下架；空=全部")}), _run_list_notices,
              R, "report"),
        _tool("list_user_feedback",
              "分页查询游客意见反馈（内容 + 昵称 + 时间）。管理员问\"游客反馈了什么/有没有投诉\"时调用。",
              _obj({"page": _i("页码，默认 1"),
                    "page_size": _i("每页条数，默认 10，最多 50")}), _run_list_feedback,
              R, "report"),
        _tool("create_notice",
              "发布园区公告或今日热点。管理员说\"发个公告/挂个热点：XXX\"时调用。",
              _obj({"kind": _s("announcement 园区公告 / hot 今日热点"),
                    "title": _s("标题"),
                    "content": _s("正文（可选）"),
                    "status": _s("on 上架（默认）/ off 下架"),
                    "sort": _i("排序，越小越靠前")}, ["kind", "title"]),
              _run_create_notice, W),
        _tool("update_notice",
              "修改公告/热点的标题/正文/状态/排序（只传要改的字段；下架即 status=off）。",
              _obj({"notice_id": _i("公告 id（先用 list_notices_admin 查）"),
                    "title": _s("新标题（可选）"),
                    "content": _s("新正文（可选）"),
                    "status": _s("on 上架 / off 下架（可选）"),
                    "sort": _i("新排序（可选）"),
                    "kind": _s("announcement / hot（可选）")},
                   ["notice_id"]), _run_update_notice, W),
        _tool("delete_notice",
              "**删除**公告/热点（高危不可逆，需管理员确认）。若只是想撤下展示，应改用 update_notice 置为下架。",
              _obj({"notice_id": _i("公告 id")}, ["notice_id"]), _run_delete_notice, X),
        _tool("refresh_hot_questions",
              "立即按近 N 天问答记录重新统计热点问题（覆盖小程序快捷提问按钮）。"
              "管理员说\"刷新热点/重算快捷问题\"时调用。",
              _obj({"days": _i("统计窗口天数，默认 7"),
                    "limit": _i("条数，默认 6，最多 12")}), _run_refresh_hot, W),
        # H 知识库
        _tool("get_kb_status",
              "查询知识库规模（文档数/切片数/向量与 BM25 条数）。带上 list_docs=true 可同时列出最近 10 篇文档。",
              _obj({"list_docs": {"type": "boolean", "description": "是否列出文档清单"}}),
              _run_kb_status, R, "report"),
        _tool("delete_kb_document",
              "**删除**知识库文档（高危不可逆，需管理员确认）：同时从向量库与 BM25 索引移除，"
              "用户端立即检索不到。仅管理员明确说\"把 XXX 文档从知识库删掉\"时调用。",
              _obj({"doc_name": _s("文档文件名（先用 get_kb_status 的 list_docs 查）")},
                   ["doc_name"]), _run_delete_kb_doc, X),
        _tool("run_kb_analyze",
              "运行知识库智能体：统计用户没答上来的问题（盲区），可选基于知识库生成标准答案并导出 Word。"
              "管理员说\"统计盲区/哪些问题没答上/生成标准答案文档\"时调用。"
              "**耗时较长（数十秒）**，默认只统计（generate=false）且最多处理 3 个问题。",
              _obj({"days": _i("统计窗口天数，0=不限（默认 0）"),
                    "limit": _i("最多处理几个问题，默认 3，最多 5"),
                    "max_tokens": _i("每条答案 token 上限，默认 200，最多 300"),
                    "generate": {"type": "boolean", "description": "是否生成标准答案（默认 false）"},
                    "export": {"type": "boolean", "description": "是否导出 Word（需 generate=true）"}}),
              _run_kb_analyze, W),
        # I 用户与系统
        _tool("list_users",
              "分页查询用户列表（id/昵称/手机号/角色/注册时间），支持关键词搜索与角色过滤。"
              "注意：本工具只读，不能改用户资料。",
              _obj({"keyword": _s("手机号/昵称/用户名关键词"),
                    "role": _s("admin 只看管理员 / user 只看普通用户；空=全部"),
                    "page": _i("页码，默认 1"),
                    "page_size": _i("每页条数，默认 10，最多 30")}), _run_list_users,
              R, "report"),
        _tool("get_agent_config",
              "读取当前 Agent 运行时配置（检索 top_k/rerank/阈值、分块参数、LLM 温度、缓存 TTL）。"
              "管理员问\"现在检索参数是多少/配置是什么\"时调用。",
              _obj(), _run_get_agent_config, R, "report"),
        _tool("clear_cache",
              "清理缓存。scope=search 只清检索缓存（默认），scope=all 清空全部缓存（含计数器）。"
              "管理员说\"清一下缓存/更新没生效\"时调用。",
              _obj({"scope": _s("search（默认）或 all")}), _run_clear_cache, W),
        _tool("update_agent_config",
              "修改 Agent 运行时配置（高危，需管理员确认；直接影响线上检索与回答行为，重启后回默认）。"
              "只传要改的项。管理员说\"把 top_k 改成 8/调低阈值\"时调用。",
              _obj({"top_k": _i("检索候选数（可选）"),
                    "rerank_top": _i("送入生成的知识片段数（可选）"),
                    "similarity_threshold": {"type": "number", "description": "检索阈值 0~1（可选）"},
                    "hybrid_alpha": {"type": "number", "description": "向量检索权重 0~1（可选）"},
                    "chunk_size": _i("分块大小（可选）"),
                    "chunk_overlap": _i("分块重叠（可选）"),
                    "llm_temperature": {"type": "number", "description": "LLM 温度（可选）"},
                    "cache_ttl": _i("缓存 TTL 秒（可选）")}), _run_update_agent_config, X),
    ]

    # readonly：员工端智能体只要查询类（R）。
    #   员工端是个手机网页，写操作的接口地址对使用者是可见的——与其在页面上"把按钮藏起来"，
    #   不如在**工具清单这一层**就把 W / X 全部摘掉：模型连这些工具都看不到，也就无从调用。
    if ctx.get("readonly"):
        kept = [t for t in tools if t.get("risk") == R]
        logger.info(f"[admin_tools] readonly 模式：{len(tools)} → {len(kept)} 个查询类工具")
        return kept
    return tools


def tools_to_openai(tools: list) -> list:
    """转成 OpenAI chat.completions 的 tools 参数（复用与业务工具同一格式）"""
    from tools.business_tools import tools_to_openai as _conv
    return _conv(tools)


def dump_tools(tools: list) -> str:
    return json.dumps([{k: v for k, v in t.items() if k != "run"} for t in tools],
                      ensure_ascii=False, indent=1)
