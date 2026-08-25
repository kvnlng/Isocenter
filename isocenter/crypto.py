"""
Cryptography utilities for handling encryption keys and operations.
"""
import os
from typing import Optional
from cryptography.fernet import Fernet


class KeyManager:
    """
    Manages the lifecycle of a symmetric encryption key (Fernet).

    Persists the key to a file for consistent encryption/decryption across sessions.
    """

    def __init__(self, key_path: str = "isocenter.key"):
        """
        Args:
            key_path (str): File path to store/load the key.
        """
        self.key_path = os.path.abspath(key_path)
        self.key: Optional[bytes] = None

    def load_or_generate_key(self) -> bytes:
        """
        Loads the key from disk if it exists, otherwise generates a new one.

        Returns:
            bytes: The URL-safe base64-encoded key.
        """
        if os.path.exists(self.key_path):
            with open(self.key_path, "rb") as f:
                self.key = f.read()
        else:
            self.key = Fernet.generate_key()
            with open(self.key_path, "wb") as f:
                f.write(self.key)
        return self.key

    def get_key(self) -> bytes:
        """
        Retrieves the loaded key.

        Returns:
            bytes: The key.

        Raises:
            RuntimeError: If key has not clearly been loaded.
        """
        if not self.key:
            raise RuntimeError("Key not loaded. Call load_or_generate_key() first.")
        return self.key


class CryptoEngine:
    """
    Handles encryption and decryption of bytes using Fernet (AES-128-CBC w/ HMAC-SHA256).
    """

    def __init__(self, key: bytes):
        """
        Args:
            key (bytes): The fernet key.
        """
        self.fernet = Fernet(key)

    def encrypt(self, data: bytes) -> bytes:
        """Encrypts the byte payload."""
        return self.fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """Decrypts the token payload."""
        return self.fernet.decrypt(token)
