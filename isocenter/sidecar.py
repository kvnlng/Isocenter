"""The append-only binary sidecar that holds pixel and waveform bytes."""
# Module scope, deliberately. `fcntl` is POSIX-only and `setup.py`
# says `Operating System :: POSIX` because of it. At module scope a
# Windows install fails at `import isocenter` -- `sidecar` is imported
# by `persistence`, which is imported by `session` -- rather than at the
# first sidecar write. `tests/test_packaging_contract.py` checks for
# this import unguarded at module scope; do not wrap it in `try:`.
import fcntl
import os
import zlib
from typing import Tuple


class SidecarManager:
    """
    Manages appending and reading from a binary sidecar file.

    Thread-safe for writes (append-only) using file locking (`fcntl`).
    Format: Raw concatenated blobs (optionally compressed). Offsets/lengths are
    managed by the caller (Instance object).
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._ensure_file()

    def _ensure_file(self):
        if not os.path.exists(self.filepath):
            # Create empty file
            with open(self.filepath, 'wb'):
                pass

    def write_frame(self, data: bytes, compression: str = 'zlib') -> Tuple[int, int]:
        """
        Appends data to the sidecar file, and fsyncs it.

        Takes an exclusive `flock` on the sidecar for the append only. A
        caller that must not race `compact_sidecar` holds
        `SqliteStore._hold_sidecar_gate` across this call; this method never
        takes the gate itself.

        Args:
            data (bytes): The binary data to store.
            compression (str): 'zlib' or 'raw'.

        Returns:
            Tuple[int, int]: (offset, length) of the written blob.

        Raises:
            ValueError: If `compression` is neither 'zlib' nor 'raw'.
        """
        if compression == 'zlib':
            blob = zlib.compress(data)
        elif compression == 'raw':
            blob = data
        else:
            raise ValueError(f"Unsupported compression: {compression}")

        length = len(blob)

        # Process-Safe Locking using fcntl (POSIX). This flock is on the
        # sidecar's own inode and is a leaf: it serialises appends
        # against each other and nothing else. It does NOT serialise
        # writers against `compact_sidecar`, whose `os.replace` gives
        # the path a new inode -- a writer blocked here wakes and
        # appends into the unlinked one. That is the gate's job
        # (`SqliteStore._hold_sidecar_gate`, on a stable path beside
        # the sidecar), which is held by every caller of this method
        # and never taken inside it.
        #
        # Strict append: r+b and an explicit seek to the end keep tell()
        # accurate and writes contiguous, avoiding 'ab' mode ambiguity in
        # some environments.
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

    def read_frame(self, offset: int, length: int, compression: str = 'zlib') -> bytes:  # pylint: disable=missing-raises-doc  # `raise e` re-raises zlib.error, documented below
        """
        Reads a frame from the sidecar at the specified offset.

        Args:
            offset (int): File offset in bytes.
            length (int): Length of the blob to read.
            compression (str): Compression method used ('zlib' or 'raw').

        Returns:
            bytes: The decompressed/raw data.

        Raises:
            OSError: If the read is incomplete.
            ValueError: If compression is unsupported.
            zlib.error: If a 'zlib' blob does not decompress.
        """

        with open(self.filepath, 'rb') as f:
            f.seek(offset)
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
                    chunk_data = blob[i:i + chunk_size]
                    chunks.append(dobj.decompress(chunk_data))

                chunks.append(dobj.flush())
                res = b"".join(chunks)

                return res
            except Exception as e:
                raise e
        elif compression == 'raw':
            return blob
        else:
            raise ValueError(f"Unsupported compression: {compression}")

    size = property(lambda self: os.path.getsize(self.filepath))

    # No `__getstate__`/`__setstate__`: the only attribute is `filepath`,
    # a string, and default pickling carries it. This class deliberately
    # holds **no mutable state**, so a fresh manager is indistinguishable
    # from the one it replaces and `compact_sidecar` can rebind
    # `self.sidecar` safely. Keep it that way: mutable state here would
    # make every pickled copy in a spawned worker a separate answer, and
    # would make that rebind a real bug.
