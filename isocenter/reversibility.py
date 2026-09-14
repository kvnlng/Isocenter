import base64
import json
from typing import Dict, Any, Optional

from cryptography.fernet import InvalidToken

from .entities import Instance
from .crypto import CryptoEngine, KeyManager
from .logger import get_logger, describe_exception


class _TokenHoldsNoRecord(RuntimeError):
    """A token of ours that the key opens, whose plaintext is not the JSON
    object `generate_identity_token` writes (review of #633, P-2).

    Only constructible with the key, so it is a corner; but until it had
    a type of its own it escaped `lock_identities()` as a `JSONDecodeError`
    whose `.doc` is the decrypted plaintext, and the batch, which collects
    `RuntimeError` alone, did not number it. A `RuntimeError` so every
    strict-read caller that refuses on "cannot open" refuses on this too;
    a subclass so the lock's plan can say the truer thing -- the key
    *opens* the token -- instead of the wrong-key text.
    """


class ReversibilityService:
    """
    Handles the embedding and recovery of encrypted original data in DICOM files.

    Compliant with DICOM Part 15, E.1.2 "Re-identifier" logic via the
    Encrypted Attributes Sequence (0400,0500). Uses `CryptoEngine` for encryption.
    """

    # DICOM Standard Tags for Encrypted Attributes
    TAG_ENCRYPTED_ATTRS_SEQ = "0400,0500"
    TAG_ENCRYPTED_CONTENT = "0400,0510"
    TAG_TRANSFER_SYNTAX_UID = "0400,0520"

    # Transfer Syntax for the Encrypted Payload (Dataset)
    # We use Implicit VR Little Endian (Default) as a signal that the decrypted bytes
    # form a dataset-like structure (even though we wrap JSON, this is metadata).
    PAYLOAD_TRANSFER_SYNTAX = "1.2.840.10008.1.2"

    def __init__(self, key_manager: KeyManager):
        self.key_manager = key_manager
        self._engine: Optional[CryptoEngine] = None
        self.logger = get_logger()

    @property
    def engine(self) -> CryptoEngine:
        """The `CryptoEngine` over the key manager's key, built on first use.

        Lazy because the key may not exist yet: since #539
        `enable_reversible_anonymization()` creates no key file, the first
        lock does, so a service built at enable has no key to build an
        engine from. Cached, so every later read -- and a test patching
        `engine.decrypt` -- sees one object. Raises `RuntimeError` ("Key
        not loaded") when neither `load_key()` nor `load_or_generate_key()`
        has run, and `ValueError` for a malformed key.
        """
        if self._engine is None:
            self._engine = CryptoEngine(self.key_manager.get_key())
        return self._engine

    def generate_identity_token(self, original_attributes: Dict[str, Any]) -> bytes:
        """
        Serializes and encrypts the attributes into a reusable token.

        Args:
            original_attributes (Dict[str, Any]): Dictionary of tag-value pairs to preserve.

        Returns:
            bytes: The encrypted JSON payload.
        """
        if not original_attributes:
            return b""

        json_str = json.dumps(original_attributes)
        data_bytes = json_str.encode('utf-8')
        return self.engine.encrypt(data_bytes)

    def embed_identity_token(self, instance: Instance, token: bytes):
        """
        Embeds a pre-calculated encrypted token into the instance.

        Wraps the token in an Encrypted Attributes Sequence item with the
        appropriate Transfer Syntax UID, and **replaces** whatever
        `(0400,0500)` held: after any call the sequence carries exactly
        one item, however many times the instance has been locked (#399).

        Args:
            instance (Instance): The target instance.
            token (bytes): The encrypted payload.
        """
        if not token:
            return

        try:
            # Create Sequence Item
            from .entities import DicomItem

            item = DicomItem()
            item.set_attr(self.TAG_ENCRYPTED_CONTENT, token)
            item.set_attr(self.TAG_TRANSFER_SYNTAX_UID, self.PAYLOAD_TRANSFER_SYNTAX)

            # `add_sequence()` plus a slice assignment rather than
            # `add_sequence_item()`, which appends: this sequence holds
            # exactly one item, and that item is the token this call was
            # handed. Both reads below take items[0] (`_token_item`), and
            # until #399 the two disagreed -- so a second lock was
            # accepted, reported as success, persisted and exported while
            # recovery kept answering with the *first* capture, and every
            # stale token shipped in the file. Whatever the sequence held
            # is replaced, including an Encrypted Attributes Sequence the
            # source file carried: such an instance was not recoverable at
            # all before this, because the foreign blob sat at index 0.
            #
            # `mark_modified()` is NOT redundant and must not be tidied
            # away. `add_sequence()` marks the instance modified **only
            # when it creates** -- `self.mark_modified()` at
            # `entities.py` line 458 sits under `if sequence is None`,
            # #186's rule -- and this path reaches into `items` in place
            # rather than through `add_sequence_item()`, which marks on
            # every call. Without the line below the second and later
            # locks advance no revision, `has_unsaved_changes` stays
            # False, the next `save()` skips the instance, and the new
            # token never reaches the store: memory answers with capture
            # #2 and a reopened session answers with capture #1, with
            # nothing saying so. That is #173's shape one module over.
            #
            # Stamped **before** the write, at this one site (#607): it
            # is the only place this library embeds a token, so every
            # token it embeds is vouched for. Before and not
            # after, for `record_remediation`'s reason -- a background
            # save between the two stores either a stamp without its
            # token (harmless: the stamp is keyed on the token) or, the
            # other way round, a token without its stamp, which this
            # store's next changed-value re-lock then refuses as foreign.
            instance.record_identity_token(token)
            sequence = instance.add_sequence(self.TAG_ENCRYPTED_ATTRS_SEQ)
            sequence.items[:] = [item]
            instance.mark_modified()

            # self.logger.debug(f"Embedded token into {instance.sop_instance_uid}.")

        except Exception as e:
            # `describe_exception`, not `{e}`: a bare raise has an
            # empty `str()`, and this line then said a step failed
            # without saying how (#487, #435's class).
            self.logger.error(
                f"Failed to embed token: {describe_exception(e)}")
            raise

    #: The first byte of every Fernet token: the format's version, of
    #: which there is exactly one. A token this library wrote is a Fernet
    #: token and nothing else it writes into `(0400,0510)` is, so this
    #: byte is what tells "ours" from a foreign Encrypted Content
    #: (measured: ours begin `gAAAAAB`; `b"NOT-OUR-TOKEN"` decodes to
    #: 0x34).
    OUR_TOKEN_FIRST_BYTE = 0x80

    #: The characters a Fernet token is spelled in. Checked before the
    #: decode, because `urlsafe_b64decode` translates `-_` to `+/` and
    #: then decodes the *standard* alphabet without validating it, so
    #: `+` and `/` -- which no token of ours carries -- decoded rather
    #: than failed the shape test (review of #633, P-5).
    _BASE64URL_ALPHABET = frozenset(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    @classmethod
    def is_one_of_ours(cls, content) -> bool:
        """Whether `content` is shaped like a token this library wrote:
        base64url whose first decoded byte is Fernet's version (#617).

        A shape test on the first 12 characters (nine bytes) only, and
        deliberately not a whole-string check: a token of ours that was
        truncated in transit is still ours, and the read then refuses it
        under the key rather than replacing it. A CMS blob, arbitrary
        text, an empty value, anything shorter than 12 characters, a
        spelling outside the base64url alphabet, or a `str` UTF-8 cannot
        encode is not ours.
        """
        if isinstance(content, str):
            # A lone surrogate is unencodable and raised out of every
            # lock in the session, because the Q8 sniff walks every
            # instance before any plan and the batch collects only
            # `RuntimeError` (review of #633 round 2, P-4). Not ours,
            # wherever the surrogate sits: no token of ours is spelled
            # outside base64url, so there is nothing for the key to
            # refuse -- and not `surrogateescape`, which would call a
            # value ours-shaped in front of one ours.
            try:
                content = content.encode("utf-8")
            except UnicodeEncodeError:
                return False
        if not isinstance(content, (bytes, bytearray)) or len(content) < 12:
            return False
        head = bytes(content)[:12]
        if any(byte not in cls._BASE64URL_ALPHABET for byte in head):
            return False
        # Twelve alphabet characters always decode: no padding, no error.
        head = base64.urlsafe_b64decode(head)
        return bool(head) and head[0] == cls.OUR_TOKEN_FIRST_BYTE

    def token_of_ours(self, instance: Instance) -> Optional[bytes]:
        """The Encrypted Content of `instance`'s token item as `bytes`, if
        it is shaped like one of ours; None for no item, no content, or a
        foreign sequence. No key is needed: this is what the lock reads
        before it decides whether to create one (#617, Q8)."""
        item = self._token_item(instance)
        content = item.attributes.get(self.TAG_ENCRYPTED_CONTENT) if item else None
        if not content or not self.is_one_of_ours(content):
            return None
        # `bytes`, whatever the hydration path handed back (a `bytearray`
        # is unhashable, and the lock keys a dict on this), and a `str`
        # encoded as the sniff read it.
        return content.encode("utf-8") if isinstance(content, str) else bytes(content)

    def open_token(self, content: bytes) -> Dict[str, Any]:
        """The values a token of ours holds, under this key.

        Raises:
            RuntimeError: The key does not decrypt it. No message names
                the instance or a patient: the key path is the caller's
                own argument, and a raise from inside `except
                InvalidToken` would chain the cryptography traceback
                (`from None`).
            _TokenHoldsNoRecord: The key decrypts it, but what it holds
                is not the JSON object this library writes (not UTF-8,
                not JSON, JSON that is not an object, or an empty
                object, which no lock writes: `generate_identity_token`
                returns `b""` for an empty record and
                `embed_identity_token` embeds nothing for it). Nothing
                interpolated, and `from None` at every raise: a
                `JSONDecodeError` carries the whole plaintext in `.doc`,
                which is the originals. What `from None` does is the
                Python-defined thing -- it sets `__suppress_context__`,
                so no formatted traceback prints the `JSONDecodeError`;
                the object stays attached as `__context__`, reachable
                to whoever holds the exception, who also holds the key
                and the session (review of #633 round 2, P-1).
        """
        try:
            decrypted_bytes = self.engine.decrypt(content)
        except InvalidToken:
            raise RuntimeError(
                f"the key at {self.key_manager.key_path} does not decrypt this "
                "patient's identity token; recovery needs the key the "
                "identity was locked with") from None
        try:
            record = json.loads(decrypted_bytes.decode("utf-8"))
        except ValueError:
            raise _TokenHoldsNoRecord(self._no_record_message()) from None
        # The shape of the object is not judged past "non-empty": its
        # values are `json.dumps` of whatever the attribute held at the
        # lock, so an `int` set by hand before it is a token this
        # library wrote, and a value-type check here would refuse it
        # (review of #633 round 2, P-3). `from None` here too, outside
        # any `except`, where it changes no traceback: it lets "nothing
        # is chained behind this refusal" be the one assertion
        # (`__suppress_context__`) at every raise of this door.
        if not isinstance(record, dict) or not record:
            raise _TokenHoldsNoRecord(self._no_record_message()) from None
        return record

    def _no_record_message(self) -> str:
        return (f"the key at {self.key_manager.key_path} opens this patient's "
                "identity token, but it holds no identity record this library "
                "writes, so nothing can be recovered from it")

    def held_identity(self, instance: Instance):
        """`(token bytes, values)` for a token of ours this key opens, or
        None when the instance carries no token of ours (#617).

        One spelling of "what is on this instance, and can we open it":
        `token_of_ours` then `open_token`, which a caller reading many
        instances that share one token calls separately, one decrypt per
        distinct token.

        Raises:
            RuntimeError: A token of ours does not open under this key,
                or opens to no record (`open_token`'s messages).
        """
        content = self.token_of_ours(instance)
        if content is None:
            return None
        return content, self.open_token(content)

    def recover_or_raise(self, instance: Instance) -> Dict[str, Any]:
        """The recovered attributes of `instance`'s token, or an exception.

        `recover_patient_identity`'s read (#539). `recover_original_data`
        below answers None for "no token" and "this key cannot open it"
        alike, which recovery printed as one sentence and a caller could
        not act on; that tolerant read is released and tests read
        through it, so the strict read is a second method rather than a
        changed one. Built on `held_identity` since #617, so a foreign
        Encrypted Attributes Sequence -- one holding no Fernet token --
        is "no token", not "the wrong key".

        Raises:
            RuntimeError: No Encrypted Attributes Sequence item holding a
                token of ours, or the key does not decrypt the token.
        """
        found = self.held_identity(instance)
        if found is None:
            raise RuntimeError(
                "no encrypted identity token on this patient's instances; "
                "was it locked with lock_identities() before anonymize()?")
        return found[1]

    def _token_item(self, instance: Instance):
        """The Encrypted Attributes Sequence item recovery reads, or None.

        **Item 0, and not the last item** -- the one spelling of the index
        both reads share. Since #399 every sequence this library writes
        holds exactly one item, so `items[0]` and `items[-1]` are the same
        expression on every file it will write again; they are not the
        same on a file written by 0.9.4 or earlier, which carries one item
        per lock and whose *first* one is what that release's recovery
        answered with. `docs/api/stability.md` promises those files stay
        recoverable, so this index is a compatibility commitment. It was
        spelled once in each read until review of #615 (F-2) measured
        `items[-1]` in the strict read green on the whole suite;
        `tests/test_relock_identity_token.py` holds it on both reads and
        through `recover_patient_identity()`.
        """
        seq = instance.sequences.get(self.TAG_ENCRYPTED_ATTRS_SEQ)
        return seq.items[0] if seq is not None and seq.items else None

    def recover_original_data(self, instance: Instance) -> Optional[Dict[str, Any]]:
        """
        Extracts and decrypts the original attributes from the instance.

        Locates the Encrypted Attributes Sequence, decrypts the first item's
        Encrypted Content, and deserializes the JSON.

        **Item 0, and not the last item**, through `_token_item`, which
        says why that index is a compatibility commitment.

        Args:
            instance (Instance): The anonymized instance.

        Returns:
            Optional[Dict[str, Any]]: The recovered dictionary of original attributes, or None if failed/missing.
        """
        try:
            # 1. The token item, if the instance carries one
            item = self._token_item(instance)
            if item is None:
                return None

            # 2. Read its Encrypted Content
            encrypted_bytes = item.attributes.get(self.TAG_ENCRYPTED_CONTENT)

            if not encrypted_bytes:
                self.logger.warning("EncryptedContent (0400,0510) not found in sequence item.")
                return None

            # 3. Decrypt
            decrypted_bytes = self.engine.decrypt(encrypted_bytes)

            # 4. Deserialize
            json_str = decrypted_bytes.decode('utf-8')
            return json.loads(json_str)

        except Exception as e:
            # `describe_exception`, not `{e}`. The one failure that
            # means "a token is here and this key cannot open it" is
            # Fernet's `InvalidToken`, whose `str()` is empty, so this
            # line read `Failed to recover data from <uid>: ` and a
            # wrong key could not be told from any other failure
            # (#487). Now `...: InvalidToken`.
            self.logger.error(
                f"Failed to recover data from {instance.sop_instance_uid}: "
                f"{describe_exception(e)}")
            return None
