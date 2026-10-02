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
        # A key generated for a lock to plan under, held in memory only
        # until the lock commits it (`_commit_planned_key`); never on
        # `self.key`, which means "the key on disk" (#813).
        self._planned_key: Optional[bytes] = None

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
            ValueError: The file is empty, its content is not a Fernet
                key, or the path is not a regular file (a directory, a
                FIFO, a device), which is refused before it is opened. The
                message names the path in every case. Nothing is cached.
        """
        if self.key is None:
            # Before `open`, not after it: `open` on a FIFO blocks until a
            # writer appears, which hung `Session()` for good (#791), so an
            # `fstat` of the opened file is too late. `isfile` follows
            # links, so a symlink to a key file still loads; a dangling one
            # does not exist and takes the `FileNotFoundError` arm.
            if (os.path.exists(self.key_path)
                    and not os.path.isfile(self.key_path)):
                raise ValueError(
                    f"{self.key_path} is not a regular file (a directory, "
                    "a pipe or a device), so it holds no key; a key file is "
                    "a file")
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
            # The content is not quoted: whatever the file holds, it was
            # meant to be a secret. Fernet's own message names no file
            # (#791).
            try:
                Fernet(key)
            except ValueError:
                raise ValueError(
                    f"the key file at {self.key_path} does not hold a Fernet "
                    "key (32 url-safe base64-encoded bytes); it is not the "
                    "file a lock writes") from None
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
        self._key_for_planning()
        return self._commit_planned_key()

    def _key_for_planning(self) -> bytes:
        """The key a lock plans its tokens under; writes nothing.

        The file's key when one exists (loaded and cached as `load_key()`
        does); otherwise a key generated in memory, the same one on every
        call until `_commit_planned_key` writes it or finds another
        session's key in its place.

        Returns:
            bytes: The key.

        Raises:
            ValueError: An existing file at the path is empty or malformed,
                or the path is not a regular file.
        """
        # Split from the write so a lock that writes no token -- refused,
        # or matching no patient, or none with an instance -- leaves no
        # key file behind (#813): a key file in the working directory turns
        # reversible anonymization on in every later `Session()` there.
        if self.key is not None:
            return self.key
        try:
            return self.load_key()
        except FileNotFoundError:
            pass
        if self._planned_key is None:
            self._planned_key = Fernet.generate_key()
        return self._planned_key

    def _commit_planned_key(self) -> bytes:
        """Write the planned key to `key_path` unless a key is there, and
        return the key now on disk.

        Returns:
            bytes: The key at `key_path`: this one's, or the key another
                session wrote first, which the caller must then plan under
                again.

        Raises:
            OSError: As `load_or_generate_key()`.
            ValueError: The file another session wrote is empty or
                malformed.
        """
        if self.key is not None:
            return self.key
        key = self._planned_key or Fernet.generate_key()
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
                self._planned_key = None
                return self.load_key()
            except OSError:
                # No hard links here: fall back to an exclusive create.
                # A reader between it and the write can find an empty
                # file on such a filesystem.
                try:
                    exclusive = os.open(
                        self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    self._planned_key = None
                    return self.load_key()
                with os.fdopen(exclusive, "wb") as f:
                    f.write(key)
        finally:
            os.unlink(temp_path)
        self.key = key
        self._planned_key = None
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
