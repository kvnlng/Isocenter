import pytest
import os
from isocenter.crypto import KeyManager, CryptoEngine
from cryptography.fernet import InvalidToken

def test_key_manager_lifecycle(tmp_path):
    key_file = tmp_path / "test.key"
    km = KeyManager(str(key_file))

    # 1. Initial State
    with pytest.raises(RuntimeError, match="Key not loaded"):
        km.get_key()

    # 2. Generate
    key = km.load_or_generate_key()
    assert isinstance(key, bytes)
    assert len(key) > 0
    assert key_file.exists()

    # 3. Reload (Persistent)
    km2 = KeyManager(str(key_file))
    key2 = km2.load_or_generate_key()
    assert key == key2

def test_crypto_engine_roundtrip(tmp_path):
    key_file = tmp_path / "roundtrip.key"
    km = KeyManager(str(key_file))
    key = km.load_or_generate_key()

    engine = CryptoEngine(key)

    data = b"Secret Data"
    token = engine.encrypt(data)

    assert token != data
    assert isinstance(token, bytes)

    decrypted = engine.decrypt(token)
    assert decrypted == data

def test_crypto_tampering(tmp_path):
    """Ensure modified tokens fail validation."""
    km = KeyManager(str(tmp_path / "taller.key"))
    key = km.load_or_generate_key()
    engine = CryptoEngine(key)

    token = engine.encrypt(b"Valid")

    # Tamper with the token (flip last byte)
    bad_token = bytearray(token)
    bad_token[-1] ^= 1

    with pytest.raises(InvalidToken):
        engine.decrypt(bytes(bad_token))

def test_wrong_key(tmp_path):
    """Ensure data encrypted with Key A cannot be decrypted by Key B"""
    km1 = KeyManager(str(tmp_path / "k1.key"))
    k1 = km1.load_or_generate_key()

    km2 = KeyManager(str(tmp_path / "k2.key"))
    k2 = km2.load_or_generate_key()

    e1 = CryptoEngine(k1)
    e2 = CryptoEngine(k2)

    token = e1.encrypt(b"Msg")

    with pytest.raises(InvalidToken):
        e2.decrypt(token)
