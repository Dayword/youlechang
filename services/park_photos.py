# -*- coding: utf-8 -*-
"""园区项目实景配图（纯规则、零 LLM、零 IO）

## 用途

用户问句里提到某个游乐项目（旋转木马 / 荡秋千 / 大炮乐园 / 浴室大作战 /
蹦床 / 小型过山车）时，在该条回答下方附上这个项目的**真实照片**，
复用前端已有的「图表图片卡」通道（`charts` 事件 / 非流式的 `data.charts`）。

## 设计约束

- **零 LLM、零 DB 往返**：纯词表匹配 + 静态文件存在性判断，毫秒级，不影响问答耗时。
- **保守优先**：认不出项目就返回空列表（回答完全不变），绝不猜测配错图。
- **单一数据源**：项目名以 `tools/business_tools.ATTRACTIONS` 为准；
  图片文件名在此处显式登记（图片是人工素材，与项目名不是机械对应关系）。
- **图片不占小程序主包**：素材放后端 `static/park/`，本模块只回 `/static/park/xxx.jpg`
  相对路径，前端用 `API_BASE + url` 拼绝对地址（与失物照片同一套 /static 路由）。
"""
import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)

# 静态图相对根目录（StaticFiles 挂在 /static → static/）
_PARK_DIR = os.path.join("static", "park")

# 项目名 → 实景图文件名。键必须与 tools/business_tools.ATTRACTIONS 的 name 完全一致。
_PROJECT_IMAGE: Dict[str, str] = {
    "旋转木马": "carousel.jpg",
    "荡秋千": "swing.jpg",
    "大炮乐园": "cannon.jpg",
    "浴室大作战": "bath.jpg",
    "蹦床": "trampoline.jpg",
    "小型过山车": "coaster.jpg",
}

# 别名 → 标准项目名。只放"用户真会这么打、且不会误伤其它项目"的说法：
# 一律不用单字（「车」「床」），否则会把「停车场在哪」「病床」之类误判成项目问题。
_ALIASES: Dict[str, str] = {
    "旋转木马": "旋转木马",
    "木马": "旋转木马",
    "旋木": "旋转木马",
    "骑一匹马": "旋转木马",
    "骑马": "旋转木马",
    "carousel": "旋转木马",
    "荡秋千": "荡秋千",
    "秋千": "荡秋千",
    "大炮乐园": "大炮乐园",
    "大炮": "大炮乐园",
    "打炮": "大炮乐园",
    "浴室大作战": "浴室大作战",
    "浴室": "浴室大作战",
    "蹦床": "蹦床",
    "蹦蹦床": "蹦床",
    "跳床": "蹦床",
    "小型过山车": "小型过山车",
    "过山车": "小型过山车",
    "小过山车": "小型过山车",
    # 「儿童/小孩」不指向具体项目，故意不登记 —— 无法确定配哪张图时不猜。
}

# 匹配优先级：长名优先，保证「小型过山车」不会被「过山车」抢先、
# 「浴室大作战」不会被「浴室」抢先（同一问句只配一张图时结果更符合直觉）。
_ORDERED_ALIASES = sorted(_ALIASES.items(), key=lambda kv: -len(kv[0]))


def _image_exists(filename: str) -> bool:
    """配图文件是否真的在磁盘上（缺失就不出图，避免前端拿到裂图）"""
    try:
        return os.path.isfile(os.path.join(_PARK_DIR, filename))
    except Exception:                       # pragma: no cover
        return False


def match_project(query: str) -> str:
    """从问句里识别出唯一的园区项目名；认不出返回 ""。

    一个问句提多个项目时（如「旋转木马和蹦床哪个好玩」）返回**最先出现**的那个 ——
    配图是辅助信息，多图堆叠会挤占回答阅读空间，宁可少而准。
    """
    t = str(query or "")
    if not t:
        return ""
    best_name, best_pos = "", len(t) + 1
    for alias, name in _ORDERED_ALIASES:
        pos = t.find(alias)
        if pos >= 0 and pos < best_pos:
            best_name, best_pos = name, pos
    return best_name


def project_photos(query: str) -> List[dict]:
    """问句命中项目 → 返回配图卡片列表（供 charts 通道展示）；未命中返回 []。

    返回项结构与 orchestrator 里工具生成图片的通道一致：
    `{"chart_id": ..., "title": ..., "url": ...}`，前端零改动即可渲染成图片卡。
    """
    name = match_project(query)
    if not name:
        return []
    filename = _PROJECT_IMAGE.get(name)
    if not filename or not _image_exists(filename):
        logger.warning(f"[配图] {name} 的实景图缺失（{filename}），本轮不出图")
        return []
    return [{
        "chart_id": f"park_proj_{filename}",
        "title": f"{name}·园区实景",
        "url": f"/static/park/{filename}",
    }]
