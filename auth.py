# -*- coding: utf-8 -*-
"""认证：bcrypt 密码哈希 + JWT 签发/校验 + FastAPI 当前用户依赖。"""
import datetime as dt

import bcrypt
import jwt
from fastapi import Depends, Header, HTTPException

import config
import db


# ---------------------------------------------------------------- 密码哈希
def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


# ---------------------------------------------------------------- JWT
def create_access_token(user_id: int, username: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + dt.timedelta(hours=config.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的登录凭证")


# ---------------------------------------------------------------- 依赖
def get_current_user(authorization: str = Header(default="")) -> db.User:
    """从 Authorization: Bearer <token> 解析当前用户。"""
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="未登录（缺少 Bearer Token）")
    payload = decode_token(parts[1])
    with db.SessionLocal() as session:
        user = db.get_user(session, int(payload["sub"]))
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        # detach 后属性仍可读（expire_on_commit 默认 True 但 get 命中即已加载）
        session.expunge(user)
        return user
