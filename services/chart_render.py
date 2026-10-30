# ============================================================
# 图表渲染服务 (services/chart_render.py)
# ------------------------------------------------------------
# 环境无 matplotlib（且不能联网安装），改用 PIL 手绘图表：
#   - 项目游玩排行：横向条形图（次数 + 花费币）
#   - 办卡类型统计：横向条形图（张数 + 金额）
#   - 消费金额按月分类：纵向条形图（月度趋势）
# 输出 PNG 到 static/charts/，返回可访问 URL（/static/charts/xxx.png）
# 任何单图失败都不会阻塞整体回答（调用方捕获跳过即可）。
# ============================================================
import logging
import os
import time
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

PALETTE = ["#FF8C42", "#6BCFFF", "#FFD93D", "#B19CD9", "#6BCB77",
           "#FF6B9D", "#4D96FF", "#F4A259", "#8F6BFF", "#3EC1D3"]
BG = "#FFFFFF"
TEXT = "#2F3A4A"
MUTED = "#8A97A8"
GRID = "#E5EDF5"

_FONT_CACHE = {}


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key not in _FONT_CACHE:
        candidates = [
            "C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        f = None
        for p in candidates:
            try:
                f = ImageFont.truetype(p, size)
                break
            except Exception:
                continue
        if f is None:
            f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]


def _text_w(txt, font):
    try:
        tmp = Image.new("RGB", (10, 10))
        return ImageDraw.Draw(tmp).textlength(txt, font=font)
    except Exception:
        return len(txt) * font.size


def _render_hbars(items: List[Dict], title: str, subtitle: str,
                  value_of, label_of, color_field=None, filename: str = "") -> bool:
    """通用横向条形图。items 有序，value_of/d label 控制取值与文案"""
    if not items:
        return False
    top = min(len(items), 10)
    shown = items[:top]
    font_t = _font(30, bold=True)
    font_s = _font(17)
    font_l = _font(20)
    font_v = _font(19)
    row_h = 44
    pad_l = 28
    pad_r = 24
    title_h = 60
    label_w = 0
    for it in shown:
        label_w = max(label_w, _text_w(label_of(it), font_l))
    label_w = min(int(label_w) + 16, 320)
    maxv = max((value_of(it) for it in shown), default=1) or 1
    chart_w = 640
    img_w = pad_l + label_w + 12 + chart_w + pad_r
    img_h = title_h + 8 + (top > 1) * 26 + row_h * top + 20

    img = Image.new("RGB", (img_w, img_h), BG)
    d = ImageDraw.Draw(img)
    d.text((pad_l, 12), title, font=font_t, fill=TEXT)
    if subtitle:
        d.text((pad_l, 48), subtitle, font=font_s, fill=MUTED)

    y0 = title_h + 16 + (top > 1) * 0
    for i, it in enumerate(shown):
        yy = y0 + i * row_h
        col = (color_field and it.get(color_field)) or PALETTE[i % len(PALETTE)]
        lab = label_of(it)
        d.text((pad_l, yy), lab, font=font_l, fill=TEXT)
        v = value_of(it)
        lw = max(6, int(chart_w * (v / maxv)))
        x0 = pad_l + label_w + 12
        d.rounded_rectangle([x0, yy - 8, x0 + lw, yy + 14], radius=6, fill=col)
        d.text((x0 + lw + 10, yy - 6), str(v), font=font_v, fill=col)

    try:
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        img.save(filename, "PNG")
        return True
    except Exception as e:
        logger.warning(f"chart save failed: {e}")
        return False


def _render_vbars(series: List[Dict], title: str, filename: str) -> bool:
    """纵向条形图（月度趋势）。series: [{label, value}]"""
    if not series:
        return False
    n = len(series)
    font_t = _font(30, bold=True)
    font_l = _font(17)
    font_v = _font(16)
    pad = 40
    bottom_h = 46
    top_h = 64
    maxv = max((s.get("value") or 0 for s in series), default=1) or 1
    bar_w = min(52, max(22, 620 // max(n, 1)))
    gap = 26
    chart_w = max(220, n * (bar_w + gap))
    img_w = pad * 2 + chart_w
    img_h = top_h + 260 + bottom_h
    img = Image.new("RGB", (img_w, img_h), BG)
    d = ImageDraw.Draw(img)
    d.text((pad, 10), title, font=font_t, fill=TEXT)

    top = top_h
    base = img_h - bottom_h
    d.line([pad - 6, base, img_w - pad + 6, base], fill=GRID, width=2)
    for gv in (0.25, 0.5, 0.75, 1.0):
        gy = base - int((img_h - top - bottom_h) * gv)
        d.line([pad - 6, gy, img_w - pad + 6, gy], fill=GRID, width=1)

    usable_h = base - top - 30
    for i, s in enumerate(series):
        x0 = pad + i * (bar_w + gap)
        v = s.get("value") or 0
        h = int(usable_h * (v / maxv)) if v else 4
        col = PALETTE[i % len(PALETTE)]
        d.rounded_rectangle([x0, base - h, x0 + bar_w, base], radius=6, fill=col)
        d.text((x0, base - h - 24), str(v), font=font_v, fill=TEXT)
        d.text((x0 - 4, base + 8), str(s.get("label", "")), font=font_l, fill=MUTED)

    try:
        os.makedirs(os.path.dirname(filename) or ".", exist_ok=True)
        img.save(filename, "PNG")
        return True
    except Exception as e:
        logger.warning(f"chart save failed: {e}")
        return False


def render_consumption_charts(stats: Dict, folder: str) -> List[Dict]:
    """
    依据统计结果渲染三张图，返回 [{chart_id,title,url}]；
    单图失败自动跳过（不影响文本回答）。
    """
    charts = []
    stamp = int(time.time() * 1000)

    rank = (stats.get("project_rank") or [])[:8]
    if rank:
        fn = os.path.join(folder, f"rank_{stamp}.png")
        ok = _render_hbars(
            rank,
            "项目游玩次数排行",
            f"{stats.get('period', {}).get('label', '')} · 次数与花费币",
            value_of=lambda r: r.get("qty", 0),
            label_of=lambda r: r.get("name", "?"),
            filename=fn,
        )
        if ok:
            charts.append({"chart_id": "project_rank", "title": "🎢 项目游玩次数排行",
                           "url": "/static/charts/" + os.path.basename(fn)})

    cards = (stats.get("card_stats") or [])[:8]
    if cards:
        fn = os.path.join(folder, f"cards_{stamp}.png")
        ok = _render_hbars(
            cards,
            "办卡类型统计",
            "张数与充值金额（元）",
            value_of=lambda c: c.get("count", 0),
            label_of=lambda c: f"{c.get('card_name', '?')} · ¥{c.get('amount_yuan', 0)}",
            filename=fn,
        )
        if ok:
            charts.append({"chart_id": "card_stats", "title": "💳 办卡类型统计",
                           "url": "/static/charts/" + os.path.basename(fn)})

    monthly = stats.get("monthly") or []
    if monthly:
        fn = os.path.join(folder, f"monthly_{stamp}.png")
        ok = _render_vbars(
            [{"label": m.get("month", "")[5:], "value": m.get("yuan", 0)} for m in monthly],
            "消费金额按月分布（元）",
            fn,
        )
        if ok:
            charts.append({"chart_id": "monthly_trend", "title": "📈 消费金额按月分布",
                           "url": "/static/charts/" + os.path.basename(fn)})

    return charts
