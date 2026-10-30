# ============================================================
# 用户画像 API - /profile/me
# ------------------------------------------------------------
# 小程序「我的 → AI 记得我」：查看 / 修正 / 清除 AI 对当前账号的长期记忆。
#
# 安全约定：
#   · user_id 一律取自 JWT，绝不接受客户端传入，杜绝越权读写他人画像；
#   · 手改只放行白名单字段（服务层 MANUAL_FIELDS），其余字段丢弃；
#   · 画像里本来就不含手机号 / openid，接口返回自然也不会带出去。
# ============================================================
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import get_current_user

router = APIRouter(prefix="/profile", dependencies=[Depends(get_current_user)])


class Kid(BaseModel):
    """一个孩子：年龄 / 身高 / 性别 + 专属的项目好恶与注意事项（都可缺）。

    用户原话（2026-09-23）：「每一个孩子后面都跟上喜欢什么项目，
    补充（选填：有没有什么症状/不喜欢什么项目）」
    —— 所以 likes / dislikes / symptoms 都挂在**孩子**这一层，不是账号级。
    """
    age: Optional[int] = None
    height: Optional[float] = None
    gender: Optional[str] = None
    # 这个孩子喜欢玩的项目（受项目词表限制，脏值会被服务层丢掉）
    likes: Optional[List[str]] = None
    # 这个孩子不喜欢的项目（模型据此不再推给他）
    dislikes: Optional[List[str]] = None
    # 选填：症状 / 注意事项（「怕高」「有哮喘」……自由文本，限长）
    symptoms: Optional[List[str]] = None


class ProfilePatch(BaseModel):
    """手改画像。只传要改的字段；某字段传 null / 空列表 = 删掉手改、恢复 AI 自动值"""
    kids: Optional[List[Kid]] = None
    kids_count: Optional[int] = None
    projects: Optional[List[str]] = None
    topics: Optional[List[str]] = None
    # v4：明确排斥的项目（用户说"我不喜欢旋转木马"会落这里，模型据此不再推荐）
    dislikes: Optional[List[str]] = None
    # 兼容旧版小程序（v2 的平行列表）：服务层会翻译成 kids，不另存一份
    kids_ages: Optional[List[int]] = None
    kids_heights: Optional[List[float]] = None
    # 乐观并发（C6）：带上读到的 rev，若已被别处改过则返回 409，由前端刷新后重试
    expect_rev: Optional[int] = None


class SettingRequest(BaseModel):
    personalization_on: bool = True


def _require_uid(current_user: dict) -> int:
    uid = (current_user or {}).get("user_id")
    if not uid:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return int(uid)


def _with_setting(uid: int, data: dict) -> dict:
    from services import user_profile as up
    data["personalization_on"] = not up.is_optout(uid)
    return data


@router.get("/me", summary="我的 AI 画像（AI 记得我什么）")
async def get_my_profile(current_user: dict = Depends(get_current_user)):
    uid = _require_uid(current_user)
    from services import user_profile as up
    return {"code": 200, "data": _with_setting(uid, up.get_profile(uid))}


@router.put("/me", summary="修正画像（手改值优先于 AI 自动抽取）")
async def update_my_profile(req: ProfilePatch,
                            current_user: dict = Depends(get_current_user)):
    uid = _require_uid(current_user)
    patch = req.model_dump(exclude_unset=True)
    expect_rev = patch.pop("expect_rev", None)      # C6：不参与字段白名单，单独取用
    if not patch:
        raise HTTPException(status_code=400, detail="没有需要修改的字段")
    from services import user_profile as up
    try:
        data = up.update_manual(uid, patch, expect_rev=expect_rev)
    except up.ProfileConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"字段格式不正确：{e}")
    return {"code": 200, "data": _with_setting(uid, data)}


@router.delete("/me", summary="清除 AI 对我的记忆")
async def clear_my_profile(also_messages: bool = False,
                           current_user: dict = Depends(get_current_user)):
    """also_messages=1 时连同本人在 chat_messages 里的对话一起清掉
    （画像本来就来自那些原话，只清画像不算清干净）"""
    uid = _require_uid(current_user)
    from services import user_profile as up
    return {"code": 200, "data": up.clear(uid, also_messages=also_messages)}


@router.get("/options", summary="画像弹窗的选项词表（项目 / 关注点 / 性别 / 滚动区间）")
async def get_profile_options():
    """选项一律后端下发，小程序只渲染 —— 项目名改动时不会两边漂移"""
    from services import user_profile as up
    return {"code": 200, "data": up.get_options()}


@router.get("/me/setting", summary="读取个性化开关")
async def get_my_setting(current_user: dict = Depends(get_current_user)):
    uid = _require_uid(current_user)
    from services import user_profile as up
    return {"code": 200, "data": {"personalization_on": not up.is_optout(uid)}}


@router.put("/me/setting", summary="开关个性化（关闭后不再写入画像）")
async def set_my_setting(req: SettingRequest,
                         current_user: dict = Depends(get_current_user)):
    uid = _require_uid(current_user)
    from services import user_profile as up
    optout = up.set_optout(uid, not req.personalization_on)
    return {"code": 200, "data": {"personalization_on": not optout}}
