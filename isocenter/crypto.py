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

        The key is cached on `self.key` once it has been validated; a call
        that raises caches nothing, so a later call reads the file again.
        A valid key file readable beyond its owner (any group or other mode
        bit) logs one WARNING naming the mode, and its mode is left
        unchanged.

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
            # Never chmod a file this code did not create: its mode may be
            # deliberate (a group sharing the key). The warning names the
            # mode, not the path, which can carry whatever the caller
            # named a directory.
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

        A new key file is created at mode 0600 and already written. Of two
        sessions creating the key at once, exactly one key is written and
        both load it. On a filesystem without hard links a concurrent
        reader can briefly find the new file empty. An existing file is
        never overwritten and its mode is left as it is.

        Returns:
            bytes: The URL-safe base64-encoded key.

        Raises:
            OSError: The temporary key file cannot be created in the key
                path's directory (for example `FileNotFoundError` when the
                directory does not exist); re-raised with its own type and
                errno against `key_path`.
            ValueError: An existing file at the path is empty or malformed.
        """
        if self.key is None:
            try:
                return self.load_key()
            except FileNotFoundError:
                pass
            key = Fernet.generate_key()
            # Written to a temporary file in the key's own directory
            # (`mkstemp` creates at 0600 whatever the umask) and hard-linked
            # into place, so the key file is never seen empty. `os.link`
            # refuses to replace an existing path, so of two sessions
            # creating the key at once exactly one wins.
            directory = os.path.dirname(self.key_path) or "."
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=os.path.basename(self.key_path) + ".", dir=directory)
            except OSError as exc:
                # A missing or read-only directory is reported against
                # the path the caller gave, not the temporary name nobody
                # asked for. Same type, same errno.
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
                    # No hard links here: fall back to an exclusive create.
                    # A reader between it and the write can find an empty
                    # file on such a filesystem.
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
            RuntimeError: No key has been loaded by `load_key()` or
                `load_or_generate_key()`.
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
        """Encrypts the byte payload.

        Args:
            data (bytes): The plaintext.

        Returns:
            bytes: The Fernet token.
        """
        return self.fernet.encrypt(data)

    def decrypt(self, token: bytes) -> bytes:
        """Decrypts the token payload.

        Args:
            token (bytes): A Fernet token.

        Returns:
            bytes: The plaintext.

        Raises:
            cryptography.fernet.InvalidToken: The key does not open the
                token, or it is not a well-formed token.
        """
        return self.fernet.decrypt(token)
