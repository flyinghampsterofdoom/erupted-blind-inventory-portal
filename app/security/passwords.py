from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError


password_hash = PasswordHash.recommended()


def hash_password(raw_password: str) -> str:
    return password_hash.hash(raw_password)


def verify_password(raw_password: str, hashed_password: str | None) -> bool:
    if not hashed_password or len(raw_password) > 1024:
        return False
    try:
        return password_hash.verify(raw_password, hashed_password)
    except (ValueError, TypeError, UnknownHashError):
        return False
