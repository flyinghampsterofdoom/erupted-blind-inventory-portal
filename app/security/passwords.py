from pwdlib import PasswordHash


password_hash = PasswordHash.recommended()


def hash_password(raw_password: str) -> str:
    return password_hash.hash(raw_password)


def verify_password(raw_password: str, hashed_password: str) -> bool:
    return password_hash.verify(raw_password, hashed_password)
