# ============================================================
# JWT 认证工具模块
# ============================================================
from jose import JWTError, jwt
from passlib.context import CryptContext
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import logging
import os
import secrets

# ---------- JWT 配置 ----------
# 密钥必须通过环境变量(.env)注入，禁止使用固定默认值（可被伪造 token）；
# 未配置时生成随机密钥并告警：随机密钥重启后变化，所有已签发 token 会失效（安全上优于固定默认值）
SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_urlsafe(48)
    logging.getLogger(__name__).warning(
        "⚠️ 未配置 JWT_SECRET_KEY，已生成随机密钥（服务重启后已签发 token 将全部失效）"
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24小时

# ---------- 密码哈希 ----------
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ---------- Bearer Token ----------
security = HTTPBearer()


def hash_password(password: str) -> str:
    """密码哈希"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """创建 JWT access token"""
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.utcnow()})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """解码 JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return {}


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """从请求中获取当前用户（FastAPI 依赖注入）"""
    token = credentials.credentials
    payload = decode_token(token)

    username: str = payload.get("sub")
    if username is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "username": username,
        "user_id": payload.get("user_id"),
        "role": payload.get("role", "user"),
        # scope="staff" = 从员工端登录入口签发的**降权令牌**（同一账号密码，权限更低）
        "scope": payload.get("scope", ""),
    }


def get_current_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """管理员鉴权依赖 - 非管理员访问返回 403（管理/配置类接口统一使用）

    员工端用**同一套账号密码**登录，但走 `/auth/staff/login` 签发的令牌带 `scope=staff`，
    这里一律挡掉：前端不调用不等于后端挡住了——员工把 token 抠出来直接打 /admin/*
    也是 403，只能走 /staff/api/*（见 api/staff_api.py 的白名单）。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足，需要管理员权限",
        )
    if current_user.get("scope") == "staff":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="员工令牌无权访问管理接口，请在员工端操作",
        )
    return current_user


def get_current_staff(current_user: dict = Depends(get_current_user)) -> dict:
    """员工端鉴权依赖 - /staff/api/* 使用。

    员工令牌（scope=staff）与管理员令牌都能过：管理员在手机上开员工端不必再登一次。
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="权限不足，需要员工或管理员权限",
        )
    return current_user


def get_optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(
    HTTPBearer(auto_error=False)
)) -> Optional[dict]:
    """可选认证 - 不强制要求token"""
    if credentials is None:
        return None
    token = credentials.credentials
    payload = decode_token(token)
    username = payload.get("sub")
    if username is None:
        return None
    return {
        "username": username,
        "user_id": payload.get("user_id"),
        "role": payload.get("role", "user"),
        "scope": payload.get("scope", ""),
    }
