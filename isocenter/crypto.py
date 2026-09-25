"""
Cryptography utilities for handling encryption keys and operations.
"""
import os
import stat
import tempfile
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

        Recovery reads through this, so a mistyped path raises rather than
        minting a new key that cannot decrypt anything.

        A key is cached on `self.key` only once it is usable: the file is
        read, checked non-empty and handed to `Fernet` before it is
        assigned, so a later call after the file is fixed reads it again.

        A key file readable beyond its owner (any group or other mode bit)
        logs one WARNING naming the mode, not the path, and its mode is
        left unchanged. The warning is logged after validation, so a file
        that is refused is not also warned about.

        Returns:
            bytes: The URL-safe base64-encoded key.

        Raises:
            FileNotFoundError: No file at `key_path`. The message names the
                path, which is the caller's own argument.
            ValueError: The file is empty (the message names the path and
                says so), or its content is not a Fernet key (`Fernet`'s
                own message). Neither is cached.
        """
        if self.key is None:
            try:
                with open(self.key_path, "rb") as f:
                    mode = stat.S_IMODE(os.fstat(f.fileno()).st_mode)
                    key = f.read()
            except FileNotFoundError:
                raise FileNotFoundError(
                    f"no key file at {self.key_path}; recovery needs the key "
                    "the identities were locked with, and does not create "
                    "one") from None
            if not key.strip():
                raise ValueError(
                    f"the key file at {self.key_path} is empty, so it holds no "
                    "key; a lock interrupted while creating it leaves the file "
                    "behind -- remove it and lock again")
            # Raises `ValueError` for a malformed key. The content is not
            # quoted: whatever the file holds, it was meant to be a secret.
            Fernet(key)
            self.key = key
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

        The lock reads through this; locking is the one operation that may
        mint a key. A new key file is created already written, at mode
        0600, and is never seen empty: the key is written to a temporary
        file in the key's own directory (`tempfile.mkstemp`, which creates
        at 0600 whatever the umask) and hard-linked into place. `os.link`
        refuses to replace an existing path, so of two sessions creating
        the key at once exactly one wins and the other loads the winner's
        file. On a filesystem without hard links (`os.link` raises an
        `OSError` other than `FileExistsError`) the key is written after an
        exclusive `O_EXCL` create at 0600, and a reader between the create
        and the write can find an empty file. The temporary file is always
        removed.

        An existing file's mode is left as it is. An existing file that
        is empty or malformed raises as `load_key` does, and is never
        overwritten.

        Returns:
            bytes: The URL-safe base64-encoded key.

        Raises:
            FileNotFoundError: The key path's directory does not exist;
                the temporary file cannot be created there either. Any
                `OSError` from creating the temporary file is re-raised as
                its own type and errno against `key_path`.
            ValueError: An existing file at the path is empty or malformed.
        """
        if self.key is None:
            try:
                return self.load_key()
            except FileNotFoundError:
                pass
            key = Fernet.generate_key()
            directory = os.path.dirname(self.key_path) or "."
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=os.path.basename(self.key_path) + ".", dir=directory)
            except OSError as exc:
                # A missing or read-only directory is reported against
                # the path the caller gave, not the temporary name nobody
                # asked for (review of #633, P-6). Same type, same errno.
                raise type(exc)(exc.errno, exc.strerror, self.key_path) from None
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(key)
                try:
                    os.link(temp_path, self.key_path)
                except FileExistsError:
                    # The other session won; its file is complete, because
                    # it too was linked into place already written.
                    return self.load_key()
                except OSError:
                    # No hard links here. The exclusive create, as before
                    # this release: a reader between it and the write can
                    # still find an empty file on such a filesystem.
                    try:
                        exclusive = os.open(
                            self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        return self.load_key()
                    with os.fdopen(exclusive, "wb") as f:
                        f.write(key)
            finally:
                os.unlink(temp_path)
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
