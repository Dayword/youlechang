# ============================================================
# 到访统计（services/trip_stats.py）
# ------------------------------------------------------------
# 解决的真实需求（用户原话）：
#   「一个男孩是几岁，平时喜欢玩什么，**每次来都玩多少币**，
#     **每个项目玩几次**，取一个平均值（必须是整数）」
#
# 前两项在用户画像里（family.kids / interest.projects），后两项**画像里根本没有**，
# 只能从 consumption_orders 算。用户要的"每次来"是一个**到访日**的概念，不是一单：
# 一天来玩 3 次、分 3 单，那是"来了一次"，不该被算成 3 次到访。
#
# 口径（与 services/statistics.py 保持一致，避免两处账对不上）：
#   · 1 币 = 1 元
#   · 已退款消费单**全额剔除**（次数与金额都不计）—— 退款的是"没玩成"，不是"玩过"
#   · 平均值一律 round() 成**整数**（用户明确要求"必须是整数"）
#
# 设计原则：纯函数 + 只读，零 LLM、零网络。算术不出服务端，绝不让模型心算。
# ============================================================
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 到访日最少有几次才能拿来讲"平均"——1 次到访不叫习惯，那是这一次本身
MIN_VISITS_FOR_AVG = 2


def _parse_dt(value) -> Optional[datetime]:
    """兼容 sqlite 'YYYY-MM-DD HH:MM:SS' 与 mysql DATETIME（与 statistics 同一写法）"""
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


def _r(v) -> int:
    """四舍五入成整数（用户要求"必须是整数"）。Python 的 round 是银行家舍入，
    .5 会往偶数靠（round(2.5)==2）—— 这里改用"加 0.5 再取整"的常规四舍五入，
    否则 2.5 次会被报成 2 次，用户会觉得系统在少算。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return int(f + 0.5) if f >= 0 else int(f - 0.5)


def visits_from_orders(rows: List[Dict]) -> List[Dict]:
    """把消费单聚合成**到访**列表（一个到访日 = 一次来玩）。

    Returns: [{"date","total_coins","order_count","projects":{name:qty}}]，按日期升序。
    退款单全额剔除（与 statistics.aggregate_consumption 同一口径）。
    """
    by_day: Dict[str, Dict] = {}
    for r in rows or []:
        if (r.get("status") or "unused") == "refunded":
            continue
        dt = _parse_dt(r.get("created_at"))
        if dt is None:
            continue
        day = dt.strftime("%Y-%m-%d")
        rec = by_day.setdefault(day, {"date": day, "total_coins": 0,
                                      "order_count": 0, "projects": {}})
        rec["order_count"] += 1
        rec["total_coins"] += int(r.get("total_coins") or 0)
        try:
            items = json.loads(r.get("items_json") or "[]")
        except Exception:
            items = []
        for it in items or []:
            name = str(it.get("name") or "").strip()
            if not name:
                continue
            qty = int(it.get("qty") or 1)
            rec["projects"][name] = rec["projects"].get(name, 0) + qty
    return [by_day[k] for k in sorted(by_day)]


def summarize(rows: List[Dict]) -> Dict:
    """到访统计总表（纯函数，便于单测）。

    Returns:
        {"visits": [...], "n_visits": int, "avg_coins_per_visit": int,
         "coins_range": [min,max] | [], "avg_qty_per_project": {name: int},
         "raw_avg_qty_per_project": {name: float}, "day_span": [first,last] | [],
         "enough": bool}
    enough=False 表示到访次数太少（<MIN_VISITS_FOR_AVG），不该拿来讲"平均"。
    """
    visits = visits_from_orders(rows)
    n = len(visits)
    out: Dict = {"visits": visits, "n_visits": n,
                 "avg_coins_per_visit": 0, "coins_range": [],
                 "avg_qty_per_project": {}, "raw_avg_qty_per_project": {},
                 "day_span": [], "enough": n >= MIN_VISITS_FOR_AVG}
    if not n:
        return out

    totals = [int(v["total_coins"]) for v in visits]
    out["avg_coins_per_visit"] = _r(sum(totals) / n)
    out["coins_range"] = [min(totals), max(totals)]
    out["day_span"] = [visits[0]["date"], visits[-1]["date"]]

    # 每个项目：总次数 ÷ 到访次数 → 平均每次玩几次（整数）
    sums: Dict[str, int] = {}
    for v in visits:
        for name, qty in (v.get("projects") or {}).items():
            sums[name] = sums.get(name, 0) + qty
    out["raw_avg_qty_per_project"] = {k: v / n for k, v in sums.items()}
    out["avg_qty_per_project"] = {k: _r(v / n) for k, v in sums.items()}
    return out


# 推荐次数规则（用户 2026-09-23 原话）：
#   「推荐默认一次，如果用户**每次来都玩好几遍**某一个项目，就推荐两次，
#     **最多推荐某一个项目三次**」
# 落到实现上 = 拿该项目"平均每次玩几次"当依据，映射成 1/2/3 档，**上限 3**。
MAX_QTY_PER_PROJECT = 3            # 单个项目推荐次数上限（绝不突破）
MAX_PROJECTS_PER_COMBO = 3         # 一套推荐里最多几个项目（不铺满价目表）


def qty_for_avg(avg_qty: float) -> int:
    """"平均每次玩几次" → 推荐几次（用户定死的规则表）。

        无记录 / 没玩过        → 1（默认一次）
        平均 ~1 次             → 1
        平均 ~2 次             → 2
        平均 ≥2.5 次           → 3（上限，绝不超过）
    """
    try:
        v = float(avg_qty or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v >= 2.5:
        return MAX_QTY_PER_PROJECT
    if v >= 1.5:
        return 2
    return 1


def combo_for_avg(avg_coins: int, projects: List[Dict],
                  preferred: Optional[List[str]] = None,
                  disliked: Optional[List[str]] = None,
                  avg_qty: Optional[Dict[str, float]] = None,
                  max_qty_per_project: int = MAX_QTY_PER_PROJECT,
                  max_projects: int = MAX_PROJECTS_PER_COMBO) -> Dict:
    """按"历史平均消费"排一份**贴近均值的整数组合**。

    与 services.purchase.plan_play_combo 的区别（两者服务不同问题，都保留）：
      · plan_play_combo：用户问"我**还剩**多少币能玩什么" → 按**当前余额**填满预算
      · combo_for_avg  ：用户问"我**每次来**都玩多少、每个项目几次" → 按**历史均值**排，
                        贴近均值即可，**不必花光**，也不受当前余额限制

    ★ 次数规则（用户明确要求，2026-09-23 重写）：
      每个项目的次数**由该项目的历史平均次数决定**（`qty_for_avg` → 1/2/3，上限 3），
      **不再按"能塞几次塞几次"贪心拉满** —— 旧实现会把每项顶到 3 次、铺满 4 个项目，
      于是"平均 690 币"这种用户从没花过的数字被算了出来。

    ★ 只排用户**填过喜好**的项目（用户明确要求）：
      `preferred` 非空 → 候选只有这些项目（再剔除 disliked）；
      `preferred` 为空 → 不猜、不拿别的项目凑数，`ok=False` + reason 让上层如实说
      「还不知道您喜欢玩什么项目」。

    Returns: {"ok","combo":[{name,qty,unit_coins,subtotal}],"total","avg","diff","reason"}
    """
    price: Dict[str, int] = {}
    for p in (projects or []):
        nm = str(p.get("name") or "").strip()
        unit = int(p.get("price_coins") or 0)
        if nm and unit > 0:
            price.setdefault(nm, unit)

    dis = {str(x).strip() for x in (disliked or []) if str(x or "").strip()}
    pref = [str(x).strip() for x in (preferred or []) if str(x or "").strip()]
    avg_qty = avg_qty or {}

    if not pref:
        return {"ok": False, "combo": [], "total": 0, "avg": int(avg_coins or 0), "diff": 0,
                "reason": "用户还没填过喜欢的项目——不要猜，如实说「还不知道您喜欢玩什么项目」"}
    if avg_coins <= 0:
        return {"ok": False, "combo": [], "total": 0, "avg": int(avg_coins or 0), "diff": 0,
                "reason": "没有可用的消费均值"}

    # 候选 = 用户说过的项目（按说的顺序优先），且必须能查到价、且不在不喜欢里
    cands: List[Dict] = []
    seen: set = set()
    for nm in pref:
        if nm in seen or nm in dis:
            continue
        unit = price.get(nm)
        if not unit:
            continue
        seen.add(nm)
        cands.append({"name": nm, "price_coins": unit,
                      "qty": qty_for_avg(avg_qty.get(nm, 0))})

    if not cands:
        return {"ok": False, "combo": [], "total": 0, "avg": int(avg_coins), "diff": 0,
                "reason": "用户喜欢的项目在价目表里查不到"}

    # 先便宜后贵：同样的钱尽量多点几个项目（体验更丰富），也让"不超过均值"更容易凑上
    cands.sort(key=lambda p: (int(p["price_coins"]), pref.index(p["name"])))
    combo: List[Dict] = []
    total = 0
    for p in cands:
        if len(combo) >= int(max_projects):
            break
        unit = int(p["price_coins"])
        qty = min(int(p["qty"]), int(max_qty_per_project))
        # 次数按规则给，但**总量不超过均值**：超了就往下调到还在预算内的最多次数
        while qty > 0 and total + unit * qty > avg_coins:
            qty -= 1
        if qty <= 0:
            continue
        combo.append({"name": p["name"], "qty": qty, "unit_coins": unit,
                      "subtotal": unit * qty})
        total += unit * qty

    if not combo:
        cheapest = min(int(p["price_coins"]) for p in cands)
        return {"ok": False, "combo": [], "total": 0, "avg": int(avg_coins), "diff": 0,
                "reason": "平均每次 %d 币，连最便宜的项目（%d 币/次）都排不进，"
                          "如实说明按平均消费排不出，请他按单次体验挑一个"
                          % (int(avg_coins), cheapest)}

    return {"ok": True, "combo": combo, "total": total,
            "avg": int(avg_coins), "diff": int(avg_coins) - total, "reason": ""}


def trip_facts(text: str) -> Dict:
    """从工具返回文本里**反向解析** `[[TRIP_FACTS]]` 块（纯函数，给末端校验用）。

    Returns: {"present": bool, "coins": set[int], "n_visits": int,
              "has_avg": bool, "qty": {name: int}, "raw": {name: float}}
    """
    out = {"present": False, "coins": set(), "n_visits": 0, "has_avg": False,
           "qty": {}, "raw": {}}
    raw = str(text or "")
    i, j = raw.find("[[TRIP_FACTS]]"), raw.find("[[/TRIP_FACTS]]")
    if i < 0 or j <= i:
        return out
    out["present"] = True
    for line in raw[i + len("[[TRIP_FACTS]]"):j].splitlines():
        line = line.strip()
        if line.startswith("n_visits="):
            out["n_visits"] = _to_int(line.split("=", 1)[1])
        elif line.startswith("avg_coins_per_visit="):
            out["has_avg"] = True
            out["coins"].add(_to_int(line.split("=", 1)[1]))
        elif line.startswith("coins_range="):
            for x in line.split("=", 1)[1].split("~"):
                out["coins"].add(_to_int(x))
        elif line.startswith("avg_qty|"):
            p = line.split("|")
            if len(p) >= 4:
                try:
                    out["raw"][p[1]] = float(p[2])
                except ValueError:
                    pass
                out["qty"][p[1]] = _to_int(p[3])
        elif line.startswith("combo|"):
            p = line.split("|")
            if len(p) >= 4:
                out["coins"].add(_to_int(p[2]))
                out["coins"].add(_to_int(p[3]))
        elif line.startswith("total="):
            out["coins"].add(_to_int(line.split("=", 1)[1]))
    out["coins"].discard(0)
    return out


def _to_int(v) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return 0


# 回答里"看起来像金额"的写法：数字 + 币/元（允许中间有空格）
_RE_MONEY = re.compile(r"(\d{1,6})\s*(?:币|元)")


def check_trip_numbers(answer: str, tool_text: str,
                       exempt: Optional[List[str]] = None) -> Dict:
    """★ E1b 末端兜底：回答里出现的**出行类**金额，是否都在工具下发的数字集合里。

    只做**判定**，不改写回答 —— 后置再调一次 LLM 改写会违反"模型响应耗时不变"红线。
    判定为 False 时由上层决定怎么处理（当前：记日志 + 该轮不进问答缓存）。

    ★ 只用来判「到访统计」这类数字（均值/次数/组合小计）。
      **办卡价目 / 余币来自别的工具**，它们的金额不在 TRIP_FACTS 里，若一并比对会大面积误判
      （实测：回答里的 200/500/1000 元档全被判成"编造"）。因此调用方要么只在本轮
      **确实调了 plan_trip 且没调其它带金额的工具**时判定，要么把那些工具的金额
      通过 `exempt` 传进来豁免。

    Returns: {"checked": bool, "ok": bool, "unknown": [int], "known": [int]}
      checked=False 表示工具没给指纹块（没调 plan_trip），**不参与判定**。
    """
    facts = trip_facts(tool_text)
    if not facts["present"]:
        return {"checked": False, "ok": True, "unknown": [], "known": []}
    known = set(facts["coins"])
    for x in (exempt or []):
        known.add(_to_int(x))
    unknown: List[int] = []
    for m in _RE_MONEY.finditer(str(answer or "")):
        v = _to_int(m.group(1))
        if v and v not in known:
            unknown.append(v)
    return {"checked": True, "ok": not unknown, "unknown": sorted(set(unknown)),
            "known": sorted(known)}
    """把画像里的孩子整理成"每个孩子 + 他自己的喜好"（E2b）。

    入参形如 `[{"desc": "3岁·1.2米·女孩", "likes": ["蹦床"], "dislikes": ["旋转木马"]}]`，
    由调用方（tools/business_tools）从 effective_view 里取，**本模块不认识画像结构**。

    返回同一个列表（规范化 name），缺 likes 的孩子保留 `likes=[]` —— 上层据此
    **明确点名说"还不知道 XX 喜欢玩什么"**，而不是拿账号级偏好硬顶（用户红线）。
    """
    out: List[Dict] = []
    for k in (kids_note or []):
        if not isinstance(k, dict):
            continue
        out.append({
            "desc": str(k.get("desc") or "").strip(),
            "likes": [str(x).strip() for x in (k.get("likes") or []) if str(x or "").strip()],
            "dislikes": [str(x).strip() for x in (k.get("dislikes") or []) if str(x or "").strip()],
        })
    return out


def _norm(s: str) -> str:
    return str(s or "").strip()


def kid_pref_view(kids_note: Optional[List[Dict]] = None) -> List[Dict]:
    """把画像里的孩子整理成"每个孩子 + 他自己的喜好"（E2b）。

    入参形如 `[{"desc": "3岁·1.2米·女孩", "likes": ["蹦床"], "dislikes": ["旋转木马"]}]`，
    由调用方（tools/business_tools）从 effective_view 里取，**本模块不认识画像结构**。

    缺 likes 的孩子保留 `likes=[]` —— 上层据此**明确点名说"还不知道 XX 喜欢玩什么"**，
    而不是拿账号级偏好硬顶（用户红线：归属绝不能串）。
    """
    out: List[Dict] = []
    for k in (kids_note or []):
        if not isinstance(k, dict):
            continue
        likes = [str(x).strip() for x in (k.get("likes") or []) if str(x or "").strip()]
        dislikes = [str(x).strip() for x in (k.get("dislikes") or []) if str(x or "").strip()]
        out.append({"desc": str(k.get("desc") or "").strip(),
                    "likes": likes, "dislikes": dislikes})
    return out


def _facts_block(s: Dict, kids: List[Dict], combo: Dict,
                 avg_qty: Dict[str, float]) -> str:
    """★ E1 数字指纹（根因收口，不只是末端校验）。

    把**本工具算出的全部数字**收进一个机器标记块，并声明"这是本轮唯一可用的金额/次数，
    块外的任何金额都是编的"。模型拿到的是**结论**而不是原始单据，
    因此它**不需要算、也无从算错** —— 编造空间被压到最小。
    """
    lines = ["[[TRIP_FACTS]]",
             "n_visits=%d" % int(s.get("n_visits") or 0),
             "avg_coins_per_visit=%d" % int(s.get("avg_coins_per_visit") or 0)]
    if s.get("coins_range"):
        lines.append("coins_range=%d~%d" % (s["coins_range"][0], s["coins_range"][1]))
    for k, v in sorted((avg_qty or {}).items()):
        lines.append("avg_qty|%s|%.2f|%d" % (k, float(v), qty_for_avg(v)))
    if combo.get("ok"):
        for c in combo["combo"]:
            lines.append("combo|%s|%d|%d" % (c["name"], c["qty"], c["subtotal"]))
        lines.append("total=%d" % int(combo["total"]))
    for i, k in enumerate(kids or []):
        lines.append("kid%d|%s|likes=%s" % (i + 1, k.get("desc") or "",
                                            "/".join(k.get("likes") or [])))
    lines.append("[[/TRIP_FACTS]]")
    lines.append("⚠️ 上面 [[TRIP_FACTS]] 里是本轮**唯一的数字来源**："
                 "回答里出现的每一个金额（币数）与次数都必须逐字来自它（或它里面的加总），"
                 "**块外的任何金额都是在编造**，一律不得出现。")
    return "\n".join(lines)


def trip_text(rows: List[Dict], projects: List[Dict],
              preferred: Optional[List[str]] = None,
              disliked: Optional[List[str]] = None,
              profile_note: str = "",
              kids: Optional[List[Dict]] = None) -> str:
    """到访统计 → 模型可直接引用的文本（含真实数字，模型只负责组织语言）。

    没有记录 / 次数太少时**如实说明**，不编 —— 编一个"平均 3 次"比说"还不知道"更糟。
    kids：每个孩子 + 他自己的喜好（见 `kid_pref_view`）—— 有孩子时**分孩子**给推荐，
          某个孩子没填过喜好就**点名说不知道**（绝不拿账号级偏好顶替他）。
    """
    s = summarize(rows)
    kids = kid_pref_view(kids)
    head = "【到访统计】"
    if not s["n_visits"]:
        body = (head + "系统里还没有这位用户的游乐消费记录，"
                "**没有历史数据可算平均**，请如实告诉用户「还不知道您平时来玩多少」，"
                "并邀请他这次先玩一轮、之后就能按习惯推荐了。")
        return body + "\n" + _facts_block(s, kids, {"ok": False}, {})
    if not s["enough"]:
        v = s["visits"][0]
        body = (head + "只有 %d 次到访记录（%s，消费 %d 币），"
                "**次数太少不足以算「平均习惯」**，请如实说明是首次/第二次来，"
                "不要用单次记录冒充「平时」；"
                "**绝对不要**说出「平均每次花 X 币」这类话。"
                % (s["n_visits"], v["date"], v["total_coins"]))
        return body + "\n" + _facts_block(s, kids, {"ok": False}, {})

    lo, hi = s["coins_range"]
    lines = [head + "共 %d 次到访记录（%s ~ %s）"
             % (s["n_visits"], s["day_span"][0], s["day_span"][1]),
             "· 每次平均消费 %d 币（整数均值；实际区间 %d~%d 币）"
             % (s["avg_coins_per_visit"], lo, hi)]
    qty = s["avg_qty_per_project"]
    raw_qty = s["raw_avg_qty_per_project"]
    if qty:
        # 按"平均次数"降序，让模型的第一句就落在最常玩的项目上
        ranked = sorted(qty.items(), key=lambda kv: (-kv[1], kv[0]))
        lines.append("· 每个项目平均玩几次：" + "、".join(
            "%s %d 次/次" % (k, v) for k, v in ranked[:6]))

    avg = s["avg_coins_per_visit"]

    # ---------- 分孩子推荐（E2b）：每个孩子各排一套，缺喜好的点名说不知道 ----------
    if kids:
        lines.append("· **按每个孩子分别推荐**（用户要求：只排他填过的喜好项目，"
                     "没填过的孩子如实说「还不知道 XX 喜欢玩什么项目」，"
                     "**绝不要把某个孩子的喜好安到另一个孩子头上**）：")
        for i, k in enumerate(kids):
            who = k["desc"] or ("孩子%d" % (i + 1))
            if not k["likes"]:
                lines.append("  - 孩子%d（%s）：**档案里没记到他喜欢玩什么项目** → "
                             "如实告诉用户「我还不知道%s喜欢玩什么项目」，"
                             "并邀请他在「AI 记得我」里给孩子勾选。**不要猜、不要用别人的喜好顶替**。"
                             % (i + 1, who, who))
                continue
            c = combo_for_avg(avg, projects, preferred=k["likes"],
                              disliked=k["dislikes"], avg_qty=raw_qty)
            if c.get("ok"):
                detail = " + ".join("%s %d 次（%d 币）" % (x["name"], x["qty"], x["subtotal"])
                                    for x in c["combo"])
                lines.append("  - 孩子%d（%s，喜欢%s）→ %s = 合计 %d 币"
                             % (i + 1, who, "、".join(k["likes"][:3]), detail, c["total"]))
            else:
                lines.append("  - 孩子%d（%s，喜欢%s）→ 排不出组合：%s"
                             % (i + 1, who, "、".join(k["likes"][:3]), c.get("reason")))
        lines.append("  · 推荐次数规则：默认 1 次；他**每次来都玩好几遍**的项目才给 2 次，"
                     "**最多 3 次**；一班最多排 3 个项目。")

    # ---------- 账号级组合（没填孩子偏好 / 非带娃场景时用） ----------
    combo = combo_for_avg(avg, projects, preferred=preferred,
                          disliked=disliked, avg_qty=raw_qty)
    if combo.get("ok"):
        detail = " + ".join("%s %d 次（%d 币）" % (c["name"], c["qty"], c["subtotal"])
                            for c in combo["combo"])
        line = "· 按历史均值 %d 币排出的组合：%s = 合计 %d 币" % (
            combo["avg"], detail, combo["total"])
        if combo["diff"] > 0:
            line += "（比平均少 %d 币，均值排不出整数组合，这是最贴近的一档）" % combo["diff"]
        lines.append(line)
    else:
        lines.append("· 账号级偏好排不出组合：%s" % combo["reason"])

    if profile_note:
        lines.append("· 用户档案：" + profile_note)
    lines.append("请把这些数字用口语讲给用户，**不要自己再算一遍**；"
                 "**回答里出现的任何金额都必须是上面已经给出的数字**，"
                 "不许自己加起来造一个新金额（例如把几项相加报一个总数）。"
                 "若用户档案里没有这个孩子的年龄，如实说明「档案里没记到」，不要猜。")
    # ★ 红线：账号级偏好 ≠ 某个孩子的喜好。上面「按每个孩子分别推荐」里，
    #   没填过的孩子已经明确写了"档案里没记到他喜欢玩什么项目"；这里再钉一道，
    #   防止模型拿账号级的「偏好项目」去替某个孩子说话（实测漏过一次：
    #   孩子 likes 为空，模型却说"你女儿档案里记到的偏好项目是蹦床"）。
    if kids:
        lines.append("⚠️ **不要把「账号级偏好项目」说成某个孩子的喜好**："
                     "上面每个孩子后面括号里的才是他本人的喜好；"
                     "某个孩子写着「档案里没记到他喜欢玩什么项目」时，"
                     "就只能如实说不知道他喜欢什么，"
                     "**不许**拿账号级的偏好项目冒名顶替（「千万别搞混了」是用户的硬要求）。")
    return "\n".join(lines) + "\n" + _facts_block(s, kids, combo, raw_qty)
