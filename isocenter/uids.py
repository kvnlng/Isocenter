"""UIDs this library derives rather than reads.

Two rules:

- **Never pydicom's `generate_uid(prefix=None, entropy_srcs=...)`.** With
  `prefix=None`, pydicom 3.0.2 ignores `entropy_srcs` and returns
  `2.25.{uuid4().int}`: two calls with identical sources give different
  UIDs. A derived UID that is not deterministic re-keys a study on every
  re-ingest and splits it across two patients.
- **Never `uuid.UUID(version=8)`.** Python 3.12, this package's floor,
  refuses version 8. The bits are set here by integer masking instead,
  which is also idempotent, so a caller may pre-set them.
"""
import hashlib

#: RFC 9562 version and variant fields of a 128-bit UUID read big-endian:
#: the version is the high nibble of byte 6, the variant the top two bits
#: of byte 8.
_VERSION_SHIFT = 76
_VARIANT_SHIFT = 62

#: The label of `generated_uid`'s digest. Versioned: changing the
#: derivation changes every generated UID, which is an output change and
#: moves every re-ingest of an affected study to a new patient.
_ABSENT_UID_LABEL = b"isocenter-absent-uid-v1\0"


def uid_from_bytes16(b: bytes) -> str:
    """A `2.25.` UID from 16 bytes, as an RFC 9562 version-8 UUID.

    Sets the version nibble to 8 and the variant to `10` and returns
    `"2.25." + str(int)`, at most 44 characters (PS3.5 B.2). Raises
    `ValueError` for anything but exactly 16 bytes.
    """
    if not isinstance(b, (bytes, bytearray)) or len(b) != 16:
        raise ValueError("uid_from_bytes16 needs exactly 16 bytes")
    value = int.from_bytes(bytes(b), "big")
    value &= ~(0xF << _VERSION_SHIFT)
    value |= 0x8 << _VERSION_SHIFT
    value &= ~(0x3 << _VARIANT_SHIFT)
    value |= 0x2 << _VARIANT_SHIFT
    return f"2.25.{value}"


def generated_uid(kind: str, anchor: str) -> str:
    """The UID ingest gives a Study or Series the source file omitted.

    `kind` is `"study"` or `"series"`; `anchor` is a UID the file *does*
    carry (its Series or SOP Instance UID for a study, its study's UID for
    a series). Deterministic, so every file of one source series resolves
    to one study in this store and in every other.

    **Unkeyed, and safe only because the anchor is a UID.** The anchor is
    never the Patient ID: an unkeyed hash of an MRN written into an
    exported UID lets anyone confirm a guessed MRN by hashing it. A UID
    the source already carried
    reveals nothing new.
    """
    digest = hashlib.sha256(
        _ABSENT_UID_LABEL + kind.encode("ascii") + b"\0"
        + str(anchor).encode("utf-8")).digest()
    return uid_from_bytes16(digest[:16])
