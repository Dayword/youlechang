# ============================================================
# 用户认证 API 路由 - /auth/*
# ============================================================
from fastapi import APIRouter, File, HTTPException, Depends, UploadFile
from pydantic import BaseModel, Field
from typing import Optional
import re
from core.auth import create_access_token, get_current_user, hash_password

router = APIRouter(prefix="/auth", tags=["用户认证"])

# 邮箱基本格式（只做冒烟级校验：非空时必须像邮箱）
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
# 用户名：3-32 位字母/数字/下划线/点/中划线（避免空格与全角字符导致登录困难）
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")


def _me_payload(user: dict) -> dict:
    """GET/PUT /auth/me 统一的返回结构（含 email，供管理端「个人信息」页回填）"""
    return {
        "id": user["id"],
        "username": user["username"],
        "nickname": user.get("nickname") or "",
        "avatar": user.get("avatar") or "",
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "role": user.get("role") or "user",
        "created_at": str(user.get("created_at") or ""),
    }


# ---------- 请求/响应模型 ----------
class RegisterRequest(BaseModel):
    username: str
    password: str
    nickname: str = ""


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateProfileRequest(BaseModel):
    """管理端「个人信息」可改项：昵称 / 邮箱 / 用户名 / 密码"""
    nickname: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    old_password: Optional[str] = None
    new_password: Optional[str] = None


class UserResponse(BaseModel):
    code: int = 200
    data: dict


class TokenResponse(BaseModel):
    code: int = 200
    data: dict


# ---------- 注册 ----------
@router.post("/register", summary="用户注册")
async def register(req: RegisterRequest):
    """注册新用户"""
    if len(req.username) < 3 or len(req.username) > 32:
        raise HTTPException(status_code=400, detail="用户名长度需在3-32个字符之间")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少6位")

    from database.db import create_user, get_user_by_username

    # 检查用户是否已存在
    existing = get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = create_user(req.username, req.password, req.nickname or req.username)
    if not user:
        raise HTTPException(status_code=400, detail="注册失败")

    # 生成 token
    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"]
    })

    return {
        "code": 200,
        "msg": "注册成功",
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar": user.get("avatar") or "",
                "email": user.get("email") or "",
                "role": user["role"],
            }
        }
    }


# ---------- 登录 ----------
@router.post("/login", summary="用户登录")
async def login(req: LoginRequest):
    """用户登录"""
    from database.db import authenticate

    user = authenticate(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"]
    })

    return {
        "code": 200,
        "msg": "登录成功",
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar": user.get("avatar") or "",
                "email": user.get("email") or "",
                "role": user["role"],
            }
        }
    }


# ---------- 员工端登录（同一账号密码，签发降权令牌） ----------
@router.post("/staff/login", summary="员工端登录（同账号密码，签发 scope=staff 的降权令牌）")
async def staff_login(req: LoginRequest):
    """员工端登录入口。

    用的是**同一套管理员账号密码**（园区不需要再维护一份员工名册），
    区别只在签出来的令牌带 `scope=staff`：
      · /admin/* 一律 403（core.auth.get_current_admin 里挡的，不是靠前端不调用）；
      · 只能走 /staff/api/* 的白名单（核销、退款、仪表盘、失物、内容、办卡）。
    令牌有效期也短一些（12 小时），手机上丢在更衣室风险小一点。

    报错为什么分成"账号不存在/密码错误"两句（不像 /auth/login 那样笼统）：
    员工端是内部工具、账号就那么几个，一线员工在手机上输错时，
    「用户名或密码错误」会让人一直卡着猜到底是哪个不对。**只对员工端细分，
    面向游客的 /auth/login 仍然是笼统一句**（避免被用来枚举账号是否存在）。
    """
    from datetime import timedelta

    from database.db import authenticate, get_user_by_username

    user = authenticate(req.username, req.password)
    if not user:
        exists = get_user_by_username((req.username or "").strip())
        raise HTTPException(status_code=401,
                            detail="密码错误，请检查大小写与首尾空格" if exists else "账号不存在")
    if (user.get("role") or "user") != "admin":
        raise HTTPException(status_code=403, detail="该账号不是工作人员账号")

    token = create_access_token(
        {
            "sub": user["username"],
            "user_id": user["id"],
            "role": user["role"],
            "scope": "staff",
        },
        expires_delta=timedelta(hours=12),
    )
    return {
        "code": 200,
        "msg": "登录成功",
        "data": {
            "token": token,
            "scope": "staff",
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "role": user["role"],
            },
        },
    }


# ---------- 获取当前用户信息 ----------
@router.get("/me", summary="获取当前用户信息")
async def get_me(current_user: dict = Depends(get_current_user)):
    """获取当前登录用户的详细信息（含邮箱/头像，管理端个人信息页数据源）"""
    from database.db import get_user_by_id

    user = get_user_by_id(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    return {"code": 200, "data": _me_payload(user)}


# ---------- 更新个人信息 ----------
@router.put("/me", summary="更新个人信息（昵称/邮箱/用户名/密码）")
async def update_me(
    req: UpdateProfileRequest,
    current_user: dict = Depends(get_current_user)
):
    """更新昵称 / 邮箱 / 用户名 / 密码。

    两个"改完必须重新登录"的项（JWT 的 sub 就是用户名，改了什么旧 token 都不该继续用）：
      * 改用户名 —— 唯一性校验（uk_username 兜底并发）+ 格式校验；
      * 改密码   —— 校验原密码 + 长度 + 新旧不同，并顺带吊销该账号的免密令牌。
    返回 need_relogin，前端据此清 token 并跳登录页。
    """
    from database.db import (update_user, get_user_by_id, get_user_by_username,
                             revoke_user_remember_tokens)
    from core.auth import verify_password

    user = get_user_by_id(current_user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    updates: dict = {}
    need_relogin = False

    # ---- 昵称 ----
    if req.nickname is not None:
        nick = req.nickname.strip()
        if nick != (user.get("nickname") or ""):
            if not nick:
                raise HTTPException(status_code=400, detail="昵称不能为空")
            if len(nick) > 32:
                raise HTTPException(status_code=400, detail="昵称最长 32 个字符")
            updates["nickname"] = nick

    # ---- 邮箱（允许清空） ----
    if req.email is not None:
        mail = (req.email or "").strip()
        if mail:
            if not _EMAIL_RE.match(mail):
                raise HTTPException(status_code=400, detail="邮箱格式不正确")
            if len(mail) > 128:
                raise HTTPException(status_code=400, detail="邮箱过长")
        if mail != (user.get("email") or ""):
            updates["email"] = mail

    # ---- 用户名（登录凭据） ----
    if req.username is not None:
        uname = req.username.strip()
        if uname != user["username"]:
            if not _USERNAME_RE.match(uname):
                raise HTTPException(
                    status_code=400,
                    detail="用户名需为 3-32 位字母、数字、下划线、点或中划线")
            if get_user_by_username(uname):
                raise HTTPException(status_code=409, detail="该用户名已被占用，请换一个")
            updates["username"] = uname
            need_relogin = True

    # ---- 密码 ----
    pwd_changed = False
    if req.old_password or req.new_password:
        if not (req.old_password and req.new_password):
            raise HTTPException(status_code=400, detail="修改密码需同时提供原密码与新密码")
        if not verify_password(req.old_password, user["password_hash"]):
            raise HTTPException(status_code=400, detail="原密码错误")
        if len(req.new_password) < 6:
            raise HTTPException(status_code=400, detail="新密码长度至少6位")
        if req.new_password == req.old_password:
            raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
        updates["password_hash"] = hash_password(req.new_password)
        pwd_changed = True
        need_relogin = True

    if not updates:
        raise HTTPException(status_code=400, detail="没有需要更新的内容")

    try:
        updated = update_user(current_user["user_id"], **updates)
    except Exception as e:
        # 并发下唯一键兜底（两个请求同时改成同一个用户名）
        if "duplicate" in str(e).lower() or "1062" in str(e) or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="该用户名已被占用，请换一个")
        raise
    if not updated:
        raise HTTPException(status_code=500, detail="更新失败，请重试")

    if pwd_changed:
        try:
            revoke_user_remember_tokens(current_user["user_id"])
        except Exception:
            pass

    return {
        "code": 200,
        "msg": "密码已修改，请重新登录" if need_relogin else "信息已更新",
        "data": _me_payload(updated),
        "need_relogin": need_relogin,
    }


# ---------- 用户列表（管理员） ----------
@router.get("/users", summary="用户列表（管理员：搜索 + 筛选 + 分页）")
async def list_users(current_user: dict = Depends(get_current_user),
                     keyword: str = "", only_bound: bool = False,
                     page: int = 1, limit: int = 20):
    """管理端「用户管理」数据源。

    keyword    模糊匹配手机号 / 昵称 / 用户名，纯数字时兼按 ID 精确匹配；
    only_bound 只看"能登录的账号"（绑过手机号或有微信），滤掉微信静默登录留下的空壳；
    source     展示用来源标签（微信 / 手机号 / PC 端），由用户名前缀与 openid 派生，不落库。
    返回 dup_phones：仍重复的手机号列表（唯一索引建好后应恒为空，作为兜底告警）。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="权限不足")

    from database.db import search_users, dup_phone_groups

    result = search_users(keyword=keyword, only_bound=only_bound, page=page, limit=limit)
    users = []
    for u in result["users"]:
        username = u.get("username") or ""
        if u.get("has_wx"):
            source = "微信"
        elif username.startswith("ph_"):
            source = "手机号"
        else:
            source = "PC 端"
        users.append({
            "id": u["id"],
            "username": username,
            "nickname": u.get("nickname") or "",
            "phone": u.get("phone") or "",
            "role": u.get("role") or "user",
            "created_at": str(u.get("created_at") or ""),
            "source": source,
        })
    return {"code": 200, "data": {
        "users": users,
        "total": result["total"],
        "dup_phones": [d["phone"] for d in dup_phone_groups()],
    }}


# ---------- 微信小程序登录 ----------
class WechatLoginRequest(BaseModel):
    code: str
    nickname: str = ""
    device_id: str = Field("", max_length=64, description="小程序本地设备标识（用于记录历史账号）")


def _code2session(js_code: str) -> dict:
    """调微信 code2session 接口，code 换 openid/session_key
    返回: {"openid": str} 或抛 HTTPException(502)

    常见错误码（排错提示已写进 detail，前端 toast 会直接显示）：
      40029 invalid code → ① 小程序 appid 与后端 .env 配置的 WECHAT_APPID 不是同一个；
                            ② 同一个 code 被用了两次（code 只能兑换一次，5 分钟内有效）；
                            ③ code 已过期。
      40125 invalid appsecret → Secret 与 AppID 不匹配。
      45011 频率限制 → 调用过于频繁。
    """
    import requests
    from config import WECHAT_APPID, WECHAT_SECRET
    if not WECHAT_APPID or not WECHAT_SECRET:
        raise HTTPException(
            status_code=502,
            detail="微信登录未配置：请在 .env 中配置 WECHAT_APPID / WECHAT_SECRET"
        )
    try:
        resp = requests.get(
            "https://api.weixin.qq.com/sns/jscode2session",
            params={
                "appid": WECHAT_APPID,
                "secret": WECHAT_SECRET,
                "js_code": js_code,
                "grant_type": "authorization_code",
            },
            timeout=8,
        )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"微信登录服务不可用: {e}")

    errcode = data.get("errcode")
    if errcode not in (None, 0):
        # 把"后端正在用的 appid"回显出来，方便一眼看出是否与小程序 project.config.json 一致
        tip = ""
        if errcode == 40029:
            tip = (
                f"（后端 WECHAT_APPID={WECHAT_APPID}，请确认与小程序 project.config.json 的 "
                "appid 完全一致；也可能是同一个 code 被重复兑换或已过期）"
            )
        elif errcode == 40125:
            tip = f"（后端 WECHAT_APPID={WECHAT_APPID}，Secret 与 AppID 不匹配）"
        raise HTTPException(
            status_code=401,
            detail=f"微信登录失败: code={errcode} msg={data.get('errmsg', '')[:80]}{tip}"
        )
    openid = data.get("openid")
    if not openid:
        raise HTTPException(status_code=502, detail="微信登录响应异常: 缺少 openid")
    return {"openid": openid}


@router.get("/wechat/config", summary="查看微信登录配置（自查用，不回显 Secret）")
async def wechat_config():
    """只回显 AppID 与 Secret 是否已配置，便于排查 40029（appid 不一致）。
    同时实测一次 client_credential，能返回 token 才说明 AppID/Secret 配对有效。"""
    import requests
    from config import WECHAT_APPID, WECHAT_SECRET

    info = {
        "appid": WECHAT_APPID or "",
        "secret_configured": bool(WECHAT_SECRET),
        "paired_ok": False,
        "pair_error": "",
    }
    if WECHAT_APPID and WECHAT_SECRET:
        try:
            r = requests.get(
                "https://api.weixin.qq.com/cgi-bin/token",
                params={"grant_type": "client_credential",
                        "appid": WECHAT_APPID, "secret": WECHAT_SECRET},
                timeout=8,
            ).json()
            if r.get("access_token"):
                info["paired_ok"] = True
            else:
                info["pair_error"] = f"{r.get('errcode')} {r.get('errmsg', '')[:80]}"
        except Exception as e:
            info["pair_error"] = f"请求微信失败: {e}"
    return {"code": 200, "data": info}


@router.post("/wechat/login", summary="微信小程序登录（code换openid并签发JWT）")
async def wechat_login(req: WechatLoginRequest):
    """
    小程序端 wx.login 拿 code 后调用：
    - 新用户：自动创建（role=user，昵称默认'游客xxxxxx'）
    - 老用户：直接返回
    签发与 PC 端同构的 JWT，后续接口鉴权链路完全一致
    """
    if not req.code.strip():
        raise HTTPException(status_code=400, detail="code 不能为空")

    from database.db import get_user_by_openid, create_wechat_user

    session = _code2session(req.code.strip())
    openid = session["openid"]

    user = get_user_by_openid(openid)
    if not user:
        user = create_wechat_user(openid, req.nickname.strip())
    if not user:
        raise HTTPException(status_code=500, detail="用户创建失败，请稍后重试")

    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"]
    })

    _record_login_account(req.device_id, user)
    payload = _user_payload(user)
    payload["is_new"] = user["nickname"].startswith("游客")
    return {
        "code": 200,
        "msg": "登录成功",
        "data": {"token": token, "user": payload},
    }


# ============================================================
# 手机号登录 / 绑定（微信登录后必须绑定手机号；或手机号验证码直接登录）
# 演示阶段无短信服务商：验证码由后端生成并随响应返回（demo_code），
# 接入真实短信后只需把 demo_code 改为空、走服务商下行即可，前端无需改动。
# ============================================================
import re as _re
import time as _time

_PHONE_RE = _re.compile(r"^1[3-9]\d{9}$")
_PHONE_CODE_TTL = 300  # 验证码有效期 5 分钟
_phone_codes: dict = {}  # phone -> (code, expire_ts)


def _gen_phone_code(phone: str) -> str:
    code = f"{__import__('random').randint(0, 999999):06d}"
    _phone_codes[phone] = (code, _time.time() + _PHONE_CODE_TTL)
    return code


def _verify_phone_code(phone: str, code: str) -> bool:
    item = _phone_codes.get(phone)
    if not item:
        return False
    saved, expire = item
    if _time.time() > expire:
        _phone_codes.pop(phone, None)
        return False
    if saved != (code or "").strip():
        return False
    _phone_codes.pop(phone, None)  # 一次性使用
    return True


class PhoneCodeRequest(BaseModel):
    phone: str


class PhoneLoginRequest(BaseModel):
    phone: str
    code: str


def _check_phone(phone: str):
    if not _PHONE_RE.match(phone or ""):
        raise HTTPException(status_code=400, detail="请输入正确的 11 位手机号")


@router.post("/sms-code", summary="发送手机号验证码（演示模式直接返回验证码）")
async def send_sms_code(req: PhoneCodeRequest):
    _check_phone(req.phone)
    code = _gen_phone_code(req.phone)
    return {
        "code": 200,
        "msg": "验证码已发送（演示环境直接回显，接入短信服务商后不再返回）",
        "data": {"demo_code": code, "expire_seconds": _PHONE_CODE_TTL},
    }


class PhoneLoginRequest(BaseModel):
    phone: str
    code: str
    wx_code: str = Field("", max_length=128, description="wx.login 的 code（可选，用于绑定微信身份，办卡下单需要）")


@router.post("/phone/login", summary="手机号验证码登录（未注册自动创建）")
async def phone_login(req: PhoneLoginRequest):
    from database.db import get_user_by_phone, create_phone_user

    _check_phone(req.phone)
    if not _verify_phone_code(req.phone, req.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    user = get_user_by_phone(req.phone)
    if not user:
        user = create_phone_user(req.phone)
    if not user:
        raise HTTPException(status_code=500, detail="登录失败，请稍后重试")

    # 可选：顺带把微信身份归到手机号账号上（办卡下单需要 openid）。
    # ⚠️ 绝不能反过来把手机号"复制"到 openid 账号上 —— 那会造出同号两个账号。
    user, _merged = _bind_wx_if_given(user, req.wx_code)

    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"],
    })
    return {
        "code": 200,
        "msg": "登录成功",
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar": user.get("avatar") or "",
                "role": user["role"],
                "phone": user.get("phone") or "",
            },
        },
    }


@router.post("/phone/bind", summary="绑定手机号（微信登录后必须完成）")
async def bind_phone(req: PhoneLoginRequest, current_user: dict = Depends(get_current_user)):
    from database.db import update_user, get_user_by_id, get_user_by_phone

    _check_phone(req.phone)
    if not _verify_phone_code(req.phone, req.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    existed = get_user_by_phone(req.phone)
    if existed and existed["id"] != current_user["user_id"]:
        raise HTTPException(status_code=400, detail="该手机号已绑定其他账号")

    if not update_user(current_user["user_id"], phone=req.phone):
        raise HTTPException(status_code=500, detail="绑定失败，请稍后重试")
    user = get_user_by_id(current_user["user_id"])

    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"],
    })
    return {
        "code": 200,
        "msg": "手机号绑定成功",
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar": user.get("avatar") or "",
                "role": user["role"],
                "phone": user.get("phone") or "",
            },
        },
    }


def _wechat_access_token() -> str:
    """获取小程序全局接口凭证（缓存到过期前 5 分钟）"""
    import time
    import requests
    from config import WECHAT_APPID, WECHAT_SECRET
    if not WECHAT_APPID or not WECHAT_SECRET:
        raise HTTPException(status_code=502, detail="微信登录未配置：请在 .env 配置 WECHAT_APPID / WECHAT_SECRET")
    cached = _wechat_access_token.__dict__.get("cached")
    if cached and cached[0] > time.time() + 300:
        return cached[1]
    try:
        resp = requests.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": WECHAT_APPID, "secret": WECHAT_SECRET},
            timeout=8,
        )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取微信凭证失败: {e}")
    if data.get("errcode"):
        raise HTTPException(status_code=502, detail=f"获取微信凭证失败: {data.get('errcode')} {data.get('errmsg', '')[:60]}")
    token = data["access_token"]
    _wechat_access_token.__dict__["cached"] = (time.time() + data.get("expires_in", 7200), token)
    return token


class WechatPhoneRequest(BaseModel):
    phone_code: str  # Button open-type=getPhoneNumber 回调返回的 code


@router.post("/wechat/phone", summary="本机手机号一键登录/绑定（需企业认证小程序）")
async def wechat_phone(req: WechatPhoneRequest, current_user: dict = Depends(get_current_user)):
    """前端 Button open-type=getPhoneNumber 拿到 code 后调用；
    个人主体/未认证小程序该能力不可用，会返回明确错误，前端降级到验证码绑定"""
    import requests
    from database.db import update_user, get_user_by_id, get_user_by_phone

    try:
        resp = requests.post(
            f"https://api.weixin.qq.com/wxa/business/getuserphonenumber?access_token={_wechat_access_token()}",
            json={"code": req.phone_code},
            timeout=8,
        )
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"手机号获取失败: {e}")

    info = (data.get("phone_info") or {}) if data.get("errcode") in (None, 0) else None
    if not info or not info.get("purePhoneNumber"):
        raise HTTPException(
            status_code=400,
            detail=f"本机手机号获取不可用（{data.get('errcode')} {data.get('errmsg', '')[:60]}），请用「微信一键登录」+验证码绑定",
        )

    phone = info["purePhoneNumber"]
    existed = get_user_by_phone(phone)
    if existed and existed["id"] != current_user["user_id"]:
        raise HTTPException(status_code=400, detail="该手机号已绑定其他账号")
    if not update_user(current_user["user_id"], phone=phone):
        raise HTTPException(status_code=500, detail="绑定失败")
    user = get_user_by_id(current_user["user_id"])

    token = create_access_token({
        "sub": user["username"],
        "user_id": user["id"],
        "role": user["role"],
    })
    return {
        "code": 200,
        "msg": "手机号绑定成功",
        "data": {
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "nickname": user["nickname"],
                "avatar": user.get("avatar") or "",
                "role": user["role"],
                "phone": user.get("phone") or "",
            },
        },
    }


# ============================================================
# 头像上传（chooseAvatar 临时文件 → 存服务器 → 返回 /static 路径）
# ============================================================
import os as _os
import time as _time

_AVATAR_DIR = _os.path.join("static", "avatars")
_os.makedirs(_AVATAR_DIR, exist_ok=True)
_ALLOWED_AVATAR_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_AVATAR_SIZE = 5 * 1024 * 1024


@router.post("/avatar", summary="上传头像（chooseAvatar 临时图片）")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    from database.db import update_user

    if file.content_type not in _ALLOWED_AVATAR_TYPES:
        raise HTTPException(status_code=400, detail="仅支持图片格式（jpg/png/webp）")
    content = await file.read()
    if len(content) > MAX_AVATAR_SIZE:
        raise HTTPException(status_code=413, detail="头像图片过大，最大支持 5MB")

    ext = _os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        ext = ".jpg"
    filename = f"av_{current_user['user_id']}_{int(_time.time() * 1000)}{ext}"
    with open(_os.path.join(_AVATAR_DIR, filename), "wb") as f:
        f.write(content)
    url = f"/static/avatars/{filename}"

    if not update_user(current_user["user_id"], avatar=url):
        raise HTTPException(status_code=500, detail="头像更新失败")
    return {"code": 200, "msg": "头像已更新", "data": {"avatar": url}}


# ============================================================
# 小程序登录 v2：手机号 + 密码（bcrypt 入库）/ 历史账号 / 免密令牌
# ------------------------------------------------------------
# 前端已移除「本机手机号一键登录」和「手机号验证码登录」，改为：
#   ① 微信一键登录（code 换 openid）—— 首次进入仍需绑定手机号并设置密码
#   ② 手机号 + 密码登录（未注册则自动建号，密码 bcrypt 存 users.password_hash）
#   ③ 历史账号一键登录（点击头像/用户名，凭免密令牌直接进入，无需再输密码）
# 旧的验证码接口（/auth/sms-code、/auth/phone/login、/auth/phone/bind）
# 保留在后端供其他端调用，小程序侧不再使用。
#
# 关于「存历史手机号和密码」：密码一律 bcrypt 不可逆哈希入库，绝不落明文；
# 「下次点一下头像就登录」由 mp_remember_tokens 的随机免密令牌实现（库存 sha256，
# 30 天有效、可吊销），效果等价于记住密码但不会造成明文泄露。
# ============================================================
import hashlib as _hashlib
import secrets as _secrets
from datetime import datetime as _datetime, timedelta as _timedelta

REMEMBER_TOKEN_DAYS = 30  # 免密令牌有效期


def _user_payload(user: dict) -> dict:
    """统一的用户返回结构（pwd_set 让前端知道该账号是否已设置过密码）"""
    return {
        "id": user["id"],
        "username": user["username"],
        "nickname": user["nickname"],
        "avatar": user.get("avatar") or "",
        "email": user.get("email") or "",
        "role": user["role"],
        "phone": user.get("phone") or "",
        "pwd_set": int(user.get("pwd_set") or 0),
    }


def _hash_token(token: str) -> str:
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_remember_token(user_id: int, device_id: str) -> str:
    """签发免密令牌：返回明文给前端本地保存，库里只存 sha256 哈希"""
    from database.db import create_remember_token

    token = _secrets.token_urlsafe(32)
    expires_at = _datetime.now() + _timedelta(days=REMEMBER_TOKEN_DAYS)
    if not create_remember_token(user_id, device_id or "", _hash_token(token), expires_at):
        return ""
    return token


def _record_login_account(device_id: str, user: dict) -> None:
    """记录本机历史账号（登录页 / 我的页历史账号卡片数据源），失败不影响登录"""
    if not device_id:
        return
    try:
        from database.db import upsert_mp_login_account
        upsert_mp_login_account(device_id, user)
    except Exception:
        pass


def _bind_wx_if_given(user: dict, wx_code: str) -> tuple:
    """登录时若带了 wx.login 的 code，把微信身份归到**手机号账号**上（办卡下单需要 openid）。

    铁律：手机号账号是主账号，openid 只是登录方式。当前微信号的 openid 若已属于另一个账号 W：
      · W 没有手机号（或与当前账号同号）→ 把 W 并入当前账号，openid 改绑过来；
      · W 绑的是**另一个**手机号 → 视为两个实名账号，不做任何搬运（不能吞掉别人的号码）。
    返回 (user, merged)：user 为登录应落的账号，merged 为合并明细（空 dict 表示未合并）。
    微信服务异常时静默跳过，不阻塞登录。
    """
    if not (wx_code or "").strip():
        return user, {}
    from database.db import (bind_wechat, get_user_by_id, get_user_by_openid,
                             merge_accounts)
    try:
        openid = _code2session(wx_code.strip())["openid"]
    except HTTPException:
        return user, {}

    existed = get_user_by_openid(openid)
    if existed and existed["id"] != user["id"]:
        other_phone = (existed.get("phone") or "").strip()
        my_phone = (user.get("phone") or "").strip()
        if not other_phone or other_phone == my_phone:
            merged = merge_accounts(existed["id"], user["id"])
            return get_user_by_id(user["id"]) or user, merged
        return user, {}

    if not user.get("wx_openid"):
        bind_wechat(user["id"], openid)
        return get_user_by_id(user["id"]) or user, {}
    return user, {}


class PhonePwdLoginRequest(BaseModel):
    phone: str
    password: str = Field(..., min_length=6, max_length=64, description="登录密码，至少 6 位")
    device_id: str = Field("", max_length=64, description="小程序本地设备标识（历史账号维度）")
    remember: bool = Field(True, description="记住这个账号：签发 30 天免密令牌")
    wx_code: str = Field("", max_length=128, description="wx.login 的 code（可选，用于绑定微信身份）")


@router.post("/phone/pwd-login", summary="手机号+密码登录（未注册自动建号 / 老账号首次输密码即设置密码）")
async def phone_pwd_login(req: PhonePwdLoginRequest):
    """密码一律 bcrypt 存 users.password_hash。
    - 手机号不存在 → 自动创建账号，本次输入的密码即为该账号登录密码；
    - 账号存在但从未设过密码（pwd_set=0，如旧的微信/验证码账号）→ 本次输入的密码视为设置密码；
    - 账号已设过密码 → 校验 bcrypt，错误直接拒绝。
    """
    from database.db import (create_phone_user_with_password, get_user_by_id,
                             get_user_by_phone, set_user_password)

    _check_phone(req.phone)
    pwd = req.password or ""
    if len(pwd) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少 6 位")

    is_new = False
    user = get_user_by_phone(req.phone)
    if not user:
        user = create_phone_user_with_password(req.phone, pwd)
        is_new = True
        if not user:
            raise HTTPException(status_code=500, detail="创建账号失败，请稍后重试")
    elif not int(user.get("pwd_set") or 0):
        # 老账号（随机占位密码）：首次输入的密码即设置为登录密码
        set_user_password(user["id"], pwd)
        user = get_user_by_id(user["id"])
        is_new = True
    else:
        from core.auth import verify_password
        if not verify_password(pwd, user.get("password_hash") or ""):
            raise HTTPException(status_code=401, detail="手机号或密码错误")

    user, merged = _bind_wx_if_given(user, req.wx_code)
    if merged:
        is_new = False  # 登录落到了既有的手机号账号上，不是新建账号
    _record_login_account(req.device_id, user)

    token = create_access_token({
        "sub": user["username"], "user_id": user["id"], "role": user["role"],
    })
    remember_token = _issue_remember_token(user["id"], req.device_id) if req.remember else ""
    return {
        "code": 200,
        "msg": "登录成功" if not is_new else "账号已创建，欢迎回来",
        "data": {
            "token": token,
            "remember_token": remember_token,
            "is_new": is_new,
            "user": _user_payload(user),
            "merged": merged,
        },
    }


class RememberLoginRequest(BaseModel):
    token: str = Field(..., description="上次登录时下发的免密令牌")
    device_id: str = Field("", max_length=64)


@router.post("/remember/login", summary="历史账号免密登录（凭记忆令牌，无需输入密码）")
async def remember_login(req: RememberLoginRequest):
    from database.db import (get_remember_token, get_user_by_id,
                             revoke_remember_token)

    token_hash = _hash_token((req.token or "").strip())
    rec = get_remember_token(token_hash)
    if not rec:
        raise HTTPException(status_code=401, detail="登录凭证已失效，请输入密码登录")

    expires_at = rec.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = _datetime.strptime(expires_at[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            expires_at = None
    if expires_at and expires_at < _datetime.now():
        revoke_remember_token(token_hash)
        raise HTTPException(status_code=401, detail="登录凭证已过期，请输入密码登录")

    user = get_user_by_id(rec["user_id"])
    if not user:
        revoke_remember_token(token_hash)
        raise HTTPException(status_code=401, detail="账号不存在，请重新登录")

    device_id = (req.device_id or rec.get("device_id") or "").strip()
    # 滚动续期：旧令牌作废，换发新的（避免同一个令牌被长期反复使用）
    revoke_remember_token(token_hash)
    new_token = _issue_remember_token(user["id"], device_id)

    _record_login_account(device_id, user)
    return {
        "code": 200,
        "msg": "登录成功",
        "data": {
            "token": create_access_token({
                "sub": user["username"], "user_id": user["id"], "role": user["role"],
            }),
            "remember_token": new_token,
            "user": _user_payload(user),
        },
    }


@router.get("/accounts", summary="本机历史登录账号列表（含头像/昵称/手机号，不含任何密码）")
async def list_login_accounts(device_id: str = ""):
    from database.db import list_mp_login_accounts
    return {"code": 200, "data": list_mp_login_accounts(device_id.strip())}


@router.delete("/accounts/{account_id}", summary="移除本机某条历史账号")
async def remove_login_account(account_id: int, device_id: str = ""):
    from database.db import delete_mp_login_account, list_mp_login_accounts

    device_id = device_id.strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="缺少设备标识")
    target = next((a for a in list_mp_login_accounts(device_id) if a["id"] == account_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="历史账号不存在")
    delete_mp_login_account(device_id, target["user_id"])
    return {"code": 200, "msg": "已移除该历史账号"}


class LogoutRequest(BaseModel):
    remember_token: str = ""
    device_id: str = ""


@router.post("/logout", summary="退出登录（吊销本机免密令牌；历史账号保留）")
async def logout(req: LogoutRequest):
    from database.db import revoke_remember_token
    if req.remember_token.strip():
        revoke_remember_token(_hash_token(req.remember_token.strip()))
    return {"code": 200, "msg": "已退出登录"}


class BindPhoneRequest(BaseModel):
    phone: str
    password: str = Field("", max_length=64,
                          description="首次绑定=设置登录密码；已设过密码的换绑=新密码（可空表示不改密码）")
    current_password: str = Field("", max_length=64,
                                  description="已设置过密码时必填：当前账号密码，用于确认身份")
    verify_code: str = Field("", max_length=8,
                             description="手机号已绑在其他账号时必填：该手机号收到的验证码（证明归属，用于合并账号）")
    takeover_password: str = Field("", max_length=64,
                                   description="手机号已绑在其他账号时可选：那个账号的登录密码（替代验证码证明归属）")


def _conflict_detail(phone: str) -> dict:
    return {
        "code": 40901,
        "msg": f"手机号 {phone} 已绑定其他账号，请先验证该手机号以合并账号",
        "need": "verify_code",
    }


@router.put("/phone/bind", summary="绑定/换绑手机号（首次绑定设密码；换绑需当前密码；冲突时验证后合并账号）")
async def bind_phone_v2(req: BindPhoneRequest, current_user: dict = Depends(get_current_user)):
    """手机号归属的四种情况（铁律：一个手机号 = 一个账号，**手机号账号是主账号**）：

    ① 号码空闲 → 当前账号直接绑定（首次绑定同时设置登录密码）；
    ② 号码属于另一个账号 P，而当前账号还没手机号（微信登录留下的空壳）→ 验证归属后
       **把当前空壳账号并入 P、登录态切到 P**：用户的持卡/消费记录本来就在 P 上，原地不动；
    ③ 号码属于自己 → 换绑/改密（已设过密码须提供 current_password）；
    ④ 号码属于另一个账号 P，且当前账号已有别的手机号（真·换绑）→ 验证归属后把 P 并入
       当前账号，再把号码换过来（两边都有手机号，不适用「并入手机号账号」）。

    验证归属：该号码收到的验证码（verify_code）优先，其次 P 的登录密码（takeover_password）。
    这条通道专治"手机号绑在旧账号上、旧账号又登不进去"的死锁。
    """
    from database.db import (get_user_by_id, get_user_by_phone, merge_accounts,
                             set_user_password, update_user)

    _check_phone(req.phone)
    me = get_user_by_id(current_user["user_id"])
    if not me:
        raise HTTPException(status_code=404, detail="账号不存在")

    existed = get_user_by_phone(req.phone)
    other = existed if existed and existed["id"] != me["id"] else None

    if other:
        # 必须证明"这个手机号归我"：验证码优先，其次那个账号的登录密码
        ok_by_code = bool(req.verify_code.strip()) and _verify_phone_code(req.phone, req.verify_code)
        ok_by_pwd = False
        if not ok_by_code and req.takeover_password and int(other.get("pwd_set") or 0):
            from core.auth import verify_password
            ok_by_pwd = verify_password(req.takeover_password, other.get("password_hash") or "")
        if not (ok_by_code or ok_by_pwd):
            raise HTTPException(status_code=409, detail=_conflict_detail(req.phone))

    # ---------- ② 当前是"没手机号的空壳"：并入手机号账号 P，登录态切到 P ----------
    if other and not (me.get("phone") or "").strip():
        if req.password:
            if len(req.password) < 6:
                raise HTTPException(status_code=400, detail="新密码长度至少 6 位")
            set_user_password(other["id"], req.password)  # 归属已证明：本次输入的密码即 P 的新密码
        merged = merge_accounts(me["id"], other["id"])
        user = get_user_by_id(other["id"])
        token = create_access_token({
            "sub": user["username"], "user_id": user["id"], "role": user["role"],
        })
        return {
            "code": 200,
            "msg": "手机号已验证，已切换到该手机号的原账号（历史持卡与消费记录保持不变）",
            "data": {
                "token": token,
                "user": _user_payload(user),
                "merged": merged,
                "merged_from_user_id": me["id"],
            },
        }

    # ---------- ①③④ 当前账号为主账号：设置/校验密码后把号码写到自己名下 ----------
    pwd_set = int(me.get("pwd_set") or 0)
    if pwd_set:
        # 已设过密码 → 属于"换绑"，必须先用当前密码确认身份
        from core.auth import verify_password
        if not verify_password(req.current_password or "", me.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="当前密码错误，无法换绑")
        if req.password:
            if len(req.password) < 6:
                raise HTTPException(status_code=400, detail="新密码长度至少 6 位")
            set_user_password(me["id"], req.password)
    else:
        # 首次绑定（微信登录用户）→ 本次输入的密码即设置为登录密码
        if len(req.password or "") < 6:
            raise HTTPException(status_code=400, detail="请设置至少 6 位的登录密码")
        set_user_password(me["id"], req.password)

    merged: dict = {}
    if other:
        # ④ 先把 P 的手机号摘下来（避免两账号同时"拥有"同一号码），再并数据
        merged = merge_accounts(other["id"], me["id"])

    if not update_user(me["id"], phone=req.phone):
        raise HTTPException(status_code=500, detail="绑定失败，请稍后重试")
    user = get_user_by_id(me["id"])

    token = create_access_token({
        "sub": user["username"], "user_id": user["id"], "role": user["role"],
    })
    msg = "手机号已绑定"
    if other:
        moved = (merged or {}).get("moved") or {}
        msg = (f"手机号已绑定并合并账号（迁移持卡 {moved.get('user_cards', 0)} 张、"
               f"消费订单 {moved.get('consumption_orders', 0)} 条）")
    return {
        "code": 200,
        "msg": msg,
        "data": {
            "token": token,
            "user": _user_payload(user),
            "merged": merged,
            "merged_from_user_id": (other or {}).get("id"),
        },
    }


class ResetPasswordRequest(BaseModel):
    phone: str
    code: str = Field(..., max_length=8, description="该手机号收到的验证码")
    new_password: str = Field(..., min_length=6, max_length=64)


@router.post("/phone/reset-pwd", summary="忘记密码：验证码校验后重置登录密码并直接登录")
async def reset_password(req: ResetPasswordRequest):
    """忘记密码的唯一自助通道：验证码发到手机号本身，天然证明归属。
    重置成功后自动签发登录态，用户无需再输一次密码。
    注意：验证码只用于「找回/接管」这类兜底场景，不作为登录方式。"""
    from database.db import get_user_by_id, get_user_by_phone, set_user_password

    _check_phone(req.phone)
    if not _verify_phone_code(req.phone, req.code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度至少 6 位")

    user = get_user_by_phone(req.phone)
    if not user:
        raise HTTPException(status_code=404, detail="该手机号还没有账号，直接输入手机号+密码即可注册登录")

    # set_user_password 内部会吊销该用户全部免密令牌（改密后旧设备免密立即失效）
    set_user_password(user["id"], req.new_password)
    user = get_user_by_id(user["id"])

    token = create_access_token({
        "sub": user["username"], "user_id": user["id"], "role": user["role"],
    })
    return {
        "code": 200,
        "msg": "密码已重置，已为你登录",
        "data": {
            "token": token,
            "user": _user_payload(user),
        },
    }



class PasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=6, max_length=64)
    old_password: str = Field("", max_length=64, description="已设置过密码时必填")


@router.put("/password", summary="设置/重设登录密码（已设过密码需提供原密码）")
async def update_password(req: PasswordRequest, current_user: dict = Depends(get_current_user)):
    from database.db import get_user_by_id, set_user_password

    me = get_user_by_id(current_user["user_id"])
    if not me:
        raise HTTPException(status_code=404, detail="账号不存在")
    if int(me.get("pwd_set") or 0):
        from core.auth import verify_password
        if not verify_password(req.old_password or "", me.get("password_hash") or ""):
            raise HTTPException(status_code=400, detail="原密码错误")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码长度至少 6 位")
    set_user_password(me["id"], req.new_password)
    return {"code": 200, "msg": "密码已更新，其他设备需重新登录"}

