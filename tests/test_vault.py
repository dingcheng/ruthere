"""Tests for the vault encryption service (AES-256-GCM)."""
import pytest
from app.services.vault import encrypt, decrypt, encode_for_storage, decode_from_storage


class TestVaultEncryption:
    """Server-side AES-256-GCM encryption/decryption."""

    def test_encrypt_returns_three_components(self):
        ct, nonce, tag = encrypt("hello world")
        assert isinstance(ct, bytes)
        assert isinstance(nonce, bytes)
        assert isinstance(tag, bytes)
        assert len(nonce) == 12  # 96-bit GCM nonce
        assert len(tag) == 16   # 128-bit GCM auth tag

    def test_decrypt_round_trip(self):
        plaintext = "The password is: correct-horse-battery-staple"
        ct, nonce, tag = encrypt(plaintext)
        result = decrypt(ct, nonce, tag)
        assert result == plaintext

    def test_decrypt_unicode(self):
        plaintext = "Secret with unicode: \u00e9\u00e0\u00fc \u4f60\u597d \U0001f512"
        ct, nonce, tag = encrypt(plaintext)
        assert decrypt(ct, nonce, tag) == plaintext

    def test_decrypt_empty_string(self):
        ct, nonce, tag = encrypt("")
        assert decrypt(ct, nonce, tag) == ""

    def test_decrypt_large_content(self):
        plaintext = "A" * 100_000
        ct, nonce, tag = encrypt(plaintext)
        assert decrypt(ct, nonce, tag) == plaintext

    def test_different_nonce_each_time(self):
        """Each encryption should produce a unique nonce."""
        _, nonce1, _ = encrypt("same text")
        _, nonce2, _ = encrypt("same text")
        assert nonce1 != nonce2

    def test_wrong_tag_raises(self):
        ct, nonce, tag = encrypt("secret")
        bad_tag = bytes([b ^ 0xFF for b in tag])
        with pytest.raises(Exception):
            decrypt(ct, nonce, bad_tag)

    def test_wrong_nonce_raises(self):
        ct, nonce, tag = encrypt("secret")
        bad_nonce = bytes([b ^ 0xFF for b in nonce])
        with pytest.raises(Exception):
            decrypt(ct, bad_nonce, tag)

    def test_tampered_ciphertext_raises(self):
        ct, nonce, tag = encrypt("secret")
        bad_ct = bytes([b ^ 0xFF for b in ct])
        with pytest.raises(Exception):
            decrypt(bad_ct, nonce, tag)


class TestStorageEncoding:
    """Base64 encoding/decoding for database storage."""

    def test_round_trip(self):
        original = b"\x00\x01\x02\xff\xfe\xfd"
        encoded = encode_for_storage(original)
        assert isinstance(encoded, str)
        assert decode_from_storage(encoded) == original

    def test_encrypt_store_decrypt_round_trip(self):
        """Full pipeline: encrypt -> encode -> store -> decode -> decrypt."""
        plaintext = "my secret data"
        ct, nonce, tag = encrypt(plaintext)

        stored_ct = encode_for_storage(ct)
        stored_nonce = encode_for_storage(nonce)
        stored_tag = encode_for_storage(tag)

        result = decrypt(
            decode_from_storage(stored_ct),
            decode_from_storage(stored_nonce),
            decode_from_storage(stored_tag),
        )
        assert result == plaintext
