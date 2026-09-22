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

        Recovery's read (#539). A recovery that generated a key under a
        mistyped path wrote a new key file beside the real one, then
        failed to decrypt with it and said only that no token was found:
        a caller could not tell a wrong path from a patient never locked.

        Returns:
            bytes: The URL-safe base64-encoded key.

        **A key is cached only once it is usable (#618).** The file is
        read, checked non-empty, and handed to `Fernet` *before* it is
        assigned to `self.key`: an empty read cached as `b''` made every
        later call in the session raise `Key not loaded` whatever the
        file had since been filled with, and a malformed read repeated
        its own `ValueError` the same way. A crash between the key
        file's creation and its write is what leaves an empty file;
        `load_or_generate_key` now creates the file already written, so
        a reader of this release's key never meets one, but a file left
        by an earlier release, or emptied by hand, still can.

        **A key file readable beyond its owner logs one WARNING and is
        left as it is.** Since P2 the lock creates the key at 0600, but
        every key 0.9.7 and earlier wrote has the umask's mode (0644
        typically), and a key that decrypts every locked identity should
        not be group- or world-readable. The library does not chmod a
        file it did not create -- its mode may be deliberate (a group
        that shares the key), and a silent permission change on the
        caller's file is worse than a said one. The warning names the
        mode and not the path: a path can carry whatever the caller
        named a directory after, and the caller already holds it. It is
        logged after validation, so a file that is refused is not also
        warned about.

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

        The lock's read (#539): locking is the one operation that may mint
        a key. **Created already written, mode 0600, and never seen
        empty (#618).** The key is written to a temporary file in the
        key's own directory (`tempfile.mkstemp`, which creates at 0600
        whatever the umask) and hard-linked into place; `os.link` refuses
        to replace an existing path, so of two sessions creating the key
        at once exactly one wins and the other loads the winner's file.
        Until now the file was created with `O_EXCL` and written
        afterwards, and a reader between the two -- another session's
        lock, or every later session after a crash there -- found an
        empty key file. A filesystem without hard links (`os.link` raises
        an `OSError` other than `FileExistsError`) falls back to that
        exclusive create, so the worst case is the previous behaviour.

        An existing file's mode is left as it is. An existing file that
        is empty or malformed raises as `load_key` does, and is never
        overwritten.

        Returns:
            bytes: The URL-safe base64-encoded key.

        Raises:
            FileNotFoundError: The key path's directory does not exist;
                the temporary file cannot be created there either.
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
