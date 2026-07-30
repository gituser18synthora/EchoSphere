"""Password hashing (bcrypt), the shared password policy, and JWT issuing/verification."""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from shared.config import get_settings
from shared.errors import ApiError

# ── Password policy ───────────────────────────────────────────────────────────
# Single source of truth for every endpoint that accepts a user- or admin-chosen
# password: self-service change, admin/super-admin reset, user creation and
# tenant onboarding all call validate_password_policy so the rules never drift
# apart. Auto-generated temporary passwords are far longer than the minimum and
# are not run through this check (they are random and rotated on first login).
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

# A small deny-list on top of composition rules — full breach-corpus checks
# need external infrastructure that does not exist here.
_WEAK_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "1234567890",
    "qwerty123", "letmein123", "admin123", "welcome123", "abc12345", "iloveyou1",
}


def validate_password_policy(password: str, *, field: str = "newPassword") -> None:
    """Enforce the shared password policy; raise ApiError(422) on any violation.

    Requires at least ``MIN_PASSWORD_LENGTH`` characters with an uppercase
    letter, a lowercase letter and a digit, and rejects common passwords.
    ``field`` names the offending form field in the structured error payload."""
    problems = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"at least {MIN_PASSWORD_LENGTH} characters")
    if not any(c.islower() for c in password):
        problems.append("a lowercase letter")
    if not any(c.isupper() for c in password):
        problems.append("an uppercase letter")
    if not any(c.isdigit() for c in password):
        problems.append("a digit")
    if problems:
        raise ApiError(
            "Password must contain " + ", ".join(problems) + ".", 422,
            errors=[{"field": field, "message": "Password policy not met."}],
        )
    if password.lower() in _WEAK_PASSWORDS:
        raise ApiError(
            "This password is too common — choose something less guessable.", 422,
            errors=[{"field": field, "message": "Password is too common."}],
        )


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(*, user_id: str, role: str, tenant_id: str | None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "role": role,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
