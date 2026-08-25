import os
import zlib
import numpy as np
from typing import Tuple, Optional
from threading import Lock


class SidecarManager:
    """
    Manages appending and reading from a binary sidecar file.

    Thread-safe for writes (append-only) using file locking (`fcntl`).
    Format: Raw concatenated blobs (optionally compressed). Offsets/lengths are
    managed by the caller (Instance object).
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._lock = Lock()
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.filepath):
            # Create empty file
            with open(self.filepath, 'wb') as f:
                pass

    def write_frame(self, data: bytes, compression: str = 'zlib') -> Tuple[int, int]:
        """
        Appends data to the sidecar file.

        Args:
            data (bytes): The binary data to store.
            compression (str): 'zlib' or 'raw'.

        Returns:
            Tuple[int, int]: (offset, length) of the written blob.
        """
        if compression == 'zlib':
            blob = zlib.compress(data)
        elif compression == 'raw':
            blob = data
        else:
            raise ValueError(f"Unsupported compression: {compression}")

        length = len(blob)

        # Process-Safe Locking using fcntl (POSIX)
        import fcntl

        # We assume strict append mode
        # We assume strict append mode
        # Use r+b and explicit seek to ensure tell() is accurate and writes are
        # contiguous, avoiding 'ab' mode ambiguity in some environments.
        with open(self.filepath, 'r+b') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0, 2)  # Force Seek to End
                offset = f.tell()
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

        return offset, length

    def read_frame(self, offset: int, length: int, compression: str = 'zlib') -> bytes:
        """
        Reads a frame from the sidecar at the specified offset.

        Args:
            offset (int): File offset in bytes.
            length (int): Length of the blob to read.
            compression (str): Compression method used ('zlib' or 'raw').

        Returns:
            bytes: The decompressed/raw data.

        Raises:
            IOError: If read is incomplete.
            ValueError: If compression is unsupported.
        """

        with open(self.filepath, 'rb') as f:
            # print(f"  -> Sidecar: Seek {offset}", flush=True)
            f.seek(offset)
            # print(f"  -> Sidecar: Read {length}", flush=True)
            blob = f.read(length)

        if len(blob) != length:
            raise IOError(f"Incomplete read from sidecar. Expected {length}, got {len(blob)}.")

        if compression == 'zlib':

            try:
                dobj = zlib.decompressobj()
                chunks = []
                chunk_size = 1024 * 1024  # 1MB chunks
                total_in = len(blob)

                for i in range(0, total_in, chunk_size):
                    # print(f"    dchunk {i}/{total_in}", flush=True)
                    chunk_data = blob[i:i + chunk_size]
                    chunks.append(dobj.decompress(chunk_data))

                chunks.append(dobj.flush())
                res = b"".join(chunks)

                return res
            except Exception as e:
                # print(f"[Worker {os.getpid()}] Sidecar: DECOMPRESS ERROR: {e}", flush=True)
                raise e
        elif compression == 'raw':
            return blob
        else:
            raise ValueError(f"Unsupported compression: {compression}")

    size = property(lambda self: os.path.getsize(self.filepath))

    def __getstate__(self):
        """Exclude lock from pickling."""
        state = self.__dict__.copy()
        # Remove the unpickleable lock
        if '_lock' in state:
            del state['_lock']
        return state

    def __setstate__(self, state):
        """Recreate lock on unpickling."""
        self.__dict__.update(state)
        self._lock = Lock()
