"""AES-256-GCM encryption/decryption for vault secrets."""
import base64
import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from app.config import get_settings


def _get_key() -> bytes:
    """Get the 32-byte encryption key from settings.
    
    The VAULT_KEY setting should be a base64-encoded 32-byte key.
    Generate one with: python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    """
    raw = get_settings().vault_key
    try:
        key = base64.urlsafe_b64decode(raw)
        if len(key) != 32:
            raise ValueError
    except Exception:
        # If not valid base64 or wrong length, derive a 32-byte key from the string
        # This is a fallback for development; in production, use a proper base64 key
        import hashlib
        key = hashlib.sha256(raw.encode()).digest()
    return key


def encrypt(plaintext: str) -> tuple[bytes, bytes, bytes]:
    """Encrypt plaintext using AES-256-GCM.
    
    Returns:
        (ciphertext, nonce, tag) as raw bytes.
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    # AESGCM.encrypt returns ciphertext + tag concatenated
    ct_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    # Split: last 16 bytes are the GCM tag
    ciphertext = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]
    return ciphertext, nonce, tag


def decrypt(ciphertext: bytes, nonce: bytes, tag: bytes) -> str:
    """Decrypt AES-256-GCM ciphertext.
    
    Returns:
        The original plaintext string.
    """
    key = _get_key()
    aesgcm = AESGCM(key)
    # Reconstruct the ciphertext+tag format expected by AESGCM
    ct_with_tag = ciphertext + tag
    plaintext = aesgcm.decrypt(nonce, ct_with_tag, None)
    return plaintext.decode("utf-8")


def encode_for_storage(data: bytes) -> str:
    """Encode raw bytes to base64 string for database storage."""
    return base64.b64encode(data).decode("ascii")


def decode_from_storage(data: str) -> bytes:
    """Decode base64 string from database back to raw bytes."""
    return base64.b64decode(data.encode("ascii"))
