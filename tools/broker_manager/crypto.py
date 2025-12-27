
import os
from cryptography.fernet import Fernet

def get_fernet():
    key = os.getenv("SECRET_KEY")
    if not key:
        raise RuntimeError("SECRET_KEY missing (Fernet)")
    return Fernet(key.encode() if isinstance(key, str) else key)

def encrypt_value(text: str) -> bytes:
    return get_fernet().encrypt(text.encode())

def decrypt_value(blob: bytes) -> str:
    return get_fernet().decrypt(blob).decode()
