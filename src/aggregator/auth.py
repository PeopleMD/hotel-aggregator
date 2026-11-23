import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aggregator.config import get_settings
from aggregator.db import get_session
from aggregator.models import User

ALGORITHM = "HS256"
_hasher = PasswordHasher()
# Checked when the user does not exist, so both answers take the same time.
_DUMMY_HASH = _hasher.hash("dummy")
_bearer = OAuth2PasswordBearer(tokenUrl="/auth/token")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        return False


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    user = await session.scalar(select(User).where(User.email == email.lower()))
    password_hash = user.password_hash if user else _DUMMY_HASH
    # argon2 is slow by design, a thread keeps the event loop free.
    is_valid = await asyncio.to_thread(_verify, password_hash, password)
    return user if user and is_valid else None


def create_token(user_id: int) -> str:
    settings = get_settings()
    expires = datetime.now(UTC) + timedelta(minutes=settings.jwt_ttl_minutes)
    payload = {"sub": str(user_id), "exp": expires}
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


async def current_user(
    token: Annotated[str, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Invalid token", {"WWW-Authenticate": "Bearer"}
    )
    try:
        payload = jwt.decode(
            token,
            get_settings().jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]},
        )
        user = await session.get(User, int(payload["sub"]))
    except (jwt.PyJWTError, ValueError):
        raise unauthorized from None
    if user is None:
        raise unauthorized
    return user


async def admin_user(user: Annotated[User, Depends(current_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user
