"""
Cryptography utilities for handling encryption keys and operations.
"""
import os
import stat
from typing import Optional
from cryptography.fernet import Fernet

from .logger import get_logger


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

    def load_key(self) -> bytes:
        """
        Loads the key at `key_path`; never creates one.

        Recovery's read (#539). A recovery that generated a key under a
        mistyped path wrote a new key file beside the real one, then
        failed to decrypt with it and said only that no token was found:
        a caller could not tell a wrong path from a patient never locked.

        Returns:
            bytes: The URL-safe base64-encoded key.

        **A key file readable beyond its owner logs one WARNING and is
        left as it is.** Since P2 the lock creates the key at 0600, but
        every key 0.9.7 and earlier wrote has the umask's mode (0644
        typically), and a key that decrypts every locked identity should
        not be group- or world-readable. The library does not chmod a
        file it did not create -- its mode may be deliberate (a group
        that shares the key), and a silent permission change on the
        caller's file is worse than a said one. The warning names the
        mode and not the path: a path can carry whatever the caller
        named a directory after, and the caller already holds it.

        Raises:
            FileNotFoundError: No file at `key_path`. The message names the
                path, which is the caller's own argument.
        """
        if self.key is None:
            try:
                with open(self.key_path, "rb") as f:
                    mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
                    self.key = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"no key file at {self.key_path}; recovery needs the key "
                    "the identities were locked with, and does not create "
                    "one") from None
            if mode & 0o077:
                get_logger().warning(
                    "The key file given to enable_reversible_anonymization() "
                    "has mode %04o, so users other than its owner can read "
                    "the key that decrypts every locked identity. Restrict "
                    "it with chmod 600; an existing key file's mode is not "
                    "changed.", mode)
        return self.key

    def load_or_generate_key(self) -> bytes:
        """
        Loads the key at `key_path`, creating one there if none exists.

        The lock's read (#539): locking is the one operation that may mint
        a key. **Created exclusively, mode 0600, in one call** (`O_EXCL`),
        the way `SqliteStore.write_project_secret` writes the project
        secret. An existence test followed by `open(..., "wb")` let two
        sessions locking at once each write a different key, the second
        over the first, so the identities the first locked were
        unrecoverable; and it left the file at the umask's mode (0644
        typically) for a key that decrypts every locked identity (P2). A
        file that appears first is loaded, never overwritten. An existing
        file's mode is left as it is.

        Returns:
            bytes: The URL-safe base64-encoded key.
        """
        if self.key is None:
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return self.load_key()
            key = Fernet.generate_key()
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            self.key = key
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
            raise RuntimeError(
                "Key not loaded. Call load_key() or load_or_generate_key() first.")
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
