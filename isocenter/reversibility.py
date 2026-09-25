"""Reversible anonymization: encrypted identity tokens in the Encrypted Attributes Sequence."""
import base64
import json
from typing import Dict, Any, Optional

from cryptography.fernet import InvalidToken

from .entities import Instance, PhiStatus
from .crypto import CryptoEngine, KeyManager
from .logger import get_logger, describe_exception


# A class of its own so the lock's plan can say the key *opens* the token
# rather than the wrong-key text; raised in place of the `JSONDecodeError`,
# whose `.doc` carries the decrypted plaintext.
class _TokenHoldsNoRecord(RuntimeError):
    """A token of ours that the key opens, whose plaintext is not the JSON
    object `generate_identity_token` writes.

    A `RuntimeError`, so a caller that catches `RuntimeError` for "cannot
    open" catches this too. Its message names the key path and never the
    plaintext.
    """


class _TokenOfALaterScheme(_TokenHoldsNoRecord):
    """A token of ours whose plaintext names a scheme this release does not
    know: a later release wrote it.

    A subclass of `_TokenHoldsNoRecord`, so a caller that catches that
    catches this; catch this class first to word the refusal differently.
    """


# Recognised only so that recovery and the lock refuse it by name: read as
# foreign it would be "no token", and a lock would replace it, losing the
# identity silently. Not a `_TokenHoldsNoRecord`, which says the key opens
# the token.
class _TokenOfAnEarlierLayout(RuntimeError):
    """A token of ours in `(0400,0510)`, with the transfer syntax UID in
    `(0400,0520)`: the layout releases before 1.0 wrote.

    Recognised by shape and never decrypted. A `RuntimeError`, but not a
    `_TokenHoldsNoRecord`: nothing was opened.
    """


_EARLIER_LAYOUT_MESSAGE = (
    "this patient's identity token is in the layout Isocenter wrote before "
    "1.0 (the token in (0400,0510), where DICOM puts the Encrypted Content "
    "Transfer Syntax UID), which 1.x does not read; recover it with "
    "Isocenter 0.9.x and the key it was locked with")


class ReversibilityService:
    """Embeds and recovers encrypted original identifiers in DICOM instances.

    Follows DICOM PS3.15 E.1.2 re-identification through the Encrypted
    Attributes Sequence (0400,0500), encrypting with `CryptoEngine`.

    A token's plaintext is a JSON object of the locked tags' values plus
    one key, `TOKEN_SCHEME_KEY`, whose value `TOKEN_SCHEME` says how the lock
    captured it; a token with no scheme key is scheme 1. Every read that
    returns values strips the scheme key, so no caller sees it as a tag.

    Args:
        key_manager (KeyManager): Supplies the key. It need not be loaded
            until the first encrypt or decrypt.
    """

    #: The key the scheme travels under inside a token's plaintext.
    #: It begins with `_` and holds no comma, so it can never be a
    #: `gggg,eeee` tag, and `_merge` skips such a key at export.
    TOKEN_SCHEME_KEY = "__isocenter_token__"

    #: Captured per instance, one token per distinct value-set: the
    #: scheme every token this library writes. 1 is implicit (no key). A token
    #: naming a scheme above this one is refused (`_TokenOfALaterScheme`).
    TOKEN_SCHEME = 2

    # DICOM Standard Tags for Encrypted Attributes (PS3.6): the sequence,
    # Encrypted Content Transfer Syntax UID (UI) and Encrypted Content (OB).
    #
    # Written and read the PS3.6 way only. Releases before 1.0 swapped the
    # two item tags (the token in (0400,0510), the UID in (0400,0520)). An
    # item in that layout is *recognised* (`_token_content`), by shape
    # alone, only so that every read can refuse it by name
    # (`_TokenOfAnEarlierLayout`) -- read as foreign instead, a lock would
    # replace it and the identity would be gone under a lock that reported
    # success.
    TAG_ENCRYPTED_ATTRS_SEQ = "0400,0500"
    TAG_TRANSFER_SYNTAX_UID = "0400,0510"
    TAG_ENCRYPTED_CONTENT = "0400,0520"

    #: What the item's Encrypted Content Transfer Syntax UID holds: Implicit
    #: VR Little Endian, the value every release has written. It is a
    #: label and nothing more -- the payload is Fernet over JSON, not a
    #: CMS-enveloped dataset, so no UID would let a conformant reader
    #: decode it -- and nothing reads it; an earlier-layout item is told
    #: apart by where the token is, never by this value.
    PAYLOAD_TRANSFER_SYNTAX = "1.2.840.10008.1.2"

    def __init__(self, key_manager: KeyManager):
        self.key_manager = key_manager
        self._engine: Optional[CryptoEngine] = None
        self.logger = get_logger()

    @property
    def engine(self) -> CryptoEngine:
        """The `CryptoEngine` over the key manager's key, built on first use and cached.

        Returns:
            CryptoEngine: The engine.

        Raises:
            RuntimeError: Neither `load_key()` nor `load_or_generate_key()` has
                run on the key manager ("Key not loaded").
            ValueError: The loaded key is malformed.
        """
        # Lazy: `enable_reversible_anonymization()` creates no key file,
        # the first lock does, so a service built at enable has no key yet.
        if self._engine is None:
            self._engine = CryptoEngine(self.key_manager.get_key())
        return self._engine

    def generate_identity_token(self, original_attributes: Dict[str, Any]) -> bytes:
        """Serializes and encrypts the attributes into a reusable token.

        Adds the scheme key to the plaintext.

        Args:
            original_attributes (Dict[str, Any]): Tag-value pairs to preserve.

        Returns:
            bytes: The encrypted JSON payload, or `b""` for an empty record.
        """
        # The one place a token's plaintext is built, so the one place the
        # scheme key is added -- after the empty check, so the marker never
        # turns an empty record into a token.
        if not original_attributes:
            return b""

        json_str = json.dumps({**original_attributes,
                               self.TOKEN_SCHEME_KEY: self.TOKEN_SCHEME})
        data_bytes = json_str.encode('utf-8')
        return self.engine.encrypt(data_bytes)

    def embed_identity_token(self, instance: Instance, token: bytes):
        """Embeds a pre-calculated encrypted token into the instance.

        Wraps the token in an Encrypted Attributes Sequence item with the
        Encrypted Content Transfer Syntax UID, and **replaces** whatever
        `(0400,0500)` held, including a sequence the source file carried:
        after any call the sequence carries exactly one item, however many
        times the instance has been locked. An empty `token` does nothing.

        Records the token on the instance (`record_identity_token`) and marks
        the instance modified, so the next save writes it. A PHI status that
        was current before the call is recorded again after it, so the token
        write alone does not leave the instance reading as edited since its
        scan; a status an earlier edit had already left stale is not carried.

        Args:
            instance (Instance): The target instance.
            token (bytes): The encrypted payload.

        Raises:
            Exception: Whatever building or attaching the item raises, logged
                at ERROR and re-raised unchanged.
        """
        if not token:
            return

        try:
            from .entities import DicomItem

            item = DicomItem()
            item.set_attr(self.TAG_ENCRYPTED_CONTENT, token)
            item.set_attr(self.TAG_TRANSFER_SYNTAX_UID, self.PAYLOAD_TRANSFER_SYNTAX)

            # `add_sequence()` plus a slice assignment rather than
            # `add_sequence_item()`, which appends: this sequence holds
            # exactly one item, and that item is the token this call was
            # handed. Both reads take items[0] (`_token_item`); appending
            # would leave recovery answering with the *first* capture and
            # ship every stale token in the file. Whatever the sequence held
            # is replaced, including an Encrypted Attributes Sequence the
            # source file carried, which would otherwise sit at index 0.
            #
            # `mark_modified()` is NOT redundant and must not be tidied
            # away. `add_sequence()` marks the instance modified **only
            # when it creates** -- `self.mark_modified()` at
            # `entities.py` line 482 sits under `if sequence is None`
            # -- and this path reaches into `items` in place
            # rather than through `add_sequence_item()`, which marks on
            # every call. Without the line below the second and later
            # locks advance no revision, `has_unsaved_changes` stays
            # False, the next `save()` skips the instance, and the new
            # token never reaches the store: memory answers with the new
            # capture and a reopened session with the old one, with
            # nothing saying so.
            #
            # Stamped **before** the write, at this one site: it
            # is the only place this library embeds a token, so every
            # token it embeds is vouched for. Before and not
            # after, for `record_remediation`'s reason -- a background
            # save between the two stores either a stamp without its
            # token (harmless: the stamp is keyed on the token) or, the
            # other way round, a token without its stamp, which this
            # store's next changed-value re-lock then refuses as foreign.
            #
            # The status is read here, before the write, and handed back
            # after it: the token is this library's own write and not PHI,
            # and the documented path locks between the audit and the pass,
            # so an instance no finding reaches would otherwise grade as
            # edited after its scan. As redaction's carry does, it is read
            # through `phi_status`, which is
            # UNSCANNED when an edit had already left the status behind,
            # so a change made before the lock is never laundered by it.
            carried = instance.phi_status
            instance.record_identity_token(token)
            sequence = instance.add_sequence(self.TAG_ENCRYPTED_ATTRS_SEQ)
            item._parent = instance
            sequence.items[:] = [item]
            instance.mark_modified()
            if carried is not PhiStatus.UNSCANNED:
                instance.record_phi_status(carried)

            # self.logger.debug(f"Embedded token into {instance.sop_instance_uid}.")

        except Exception as e:
            # `describe_exception`, not `{e}`: a bare raise has an
            # empty `str()`, and the line would say a step failed
            # without saying how.
            self.logger.error(
                f"Failed to embed token: {describe_exception(e)}")
            raise

    #: The first byte of every Fernet token: the format's version, of
    #: which there is exactly one. A token this library wrote is a Fernet
    #: token and nothing else it writes into an Encrypted Attributes item
    #: is, so this byte is what tells "ours" from a foreign Encrypted
    #: Content, and a token from the transfer syntax UID beside it
    #: (ours begin `gAAAAAB`; `.` is outside the alphabet, so no UID
    #: passes).
    OUR_TOKEN_FIRST_BYTE = 0x80

    #: The characters a Fernet token is spelled in. Checked before the
    #: decode, because `urlsafe_b64decode` translates `-_` to `+/` and
    #: then decodes the *standard* alphabet without validating it, so
    #: `+` and `/` -- which no token of ours carries -- would decode
    #: rather than fail the shape test.
    _BASE64URL_ALPHABET = frozenset(
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    @classmethod
    def is_one_of_ours(cls, content) -> bool:
        """Whether `content` is shaped like a token this library wrote.

        A shape test on the first 12 characters only: base64url whose first
        decoded byte is Fernet's version. A CMS blob, arbitrary text, an empty
        value, anything shorter than 12 characters, a spelling outside the
        base64url alphabet, or a `str` UTF-8 cannot encode is not ours. Needs
        no key.

        Args:
            content (object): The candidate value; only `bytes`,
                `bytearray` and `str` can be ours.

        Returns:
            bool: True when the value has the shape of one of our tokens.
        """
        if isinstance(content, str):
            # A lone surrogate is unencodable; raised, it would escape
            # every lock in the session (the key-creation sniff walks every
            # instance before any plan), since the batch collects only
            # `RuntimeError`. Not ours,
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
        # The first 12 characters only, not the whole string: a token of
        # ours truncated in transit is still ours, and the read then refuses
        # it under the key rather than replacing it.
        head = bytes(content)[:12]
        if any(byte not in cls._BASE64URL_ALPHABET for byte in head):
            return False
        # Twelve alphabet characters always decode: no padding, no error.
        head = base64.urlsafe_b64decode(head)
        return bool(head) and head[0] == cls.OUR_TOKEN_FIRST_BYTE

    def token_of_ours(self, instance: Instance) -> Optional[bytes]:
        """The token of ours `instance` carries, if any. Needs no key.

        Args:
            instance (Instance): The instance to read.

        Returns:
            Optional[bytes]: The Encrypted Content of the instance's token item,
            as `bytes`; None for no item, no content, or a foreign sequence.

        Raises:
            _TokenOfAnEarlierLayout: The item holds a token of ours in the
                layout releases before 1.0 wrote.
        """
        item = self._token_item(instance)
        content = self._token_content(item) if item else None
        if content is self.EARLIER_LAYOUT:
            raise _TokenOfAnEarlierLayout(_EARLIER_LAYOUT_MESSAGE) from None
        if content is None:
            return None
        # `bytes`, whatever the hydration path handed back (a `bytearray`
        # is unhashable, and the lock keys a dict on this), and a `str`
        # encoded as the sniff read it.
        return content.encode("utf-8") if isinstance(content, str) else bytes(content)

    def open_token(self, content: bytes) -> Dict[str, Any]:
        """The values a token of ours holds, under this key.

        `open_token_with_scheme` without the scheme: one decrypt.

        Args:
            content (bytes): The token.

        Returns:
            Dict[str, Any]: The locked tags' values, the scheme key stripped.

        Raises:
            RuntimeError: As `open_token_with_scheme`: the key does not decrypt
                the token.
            _TokenHoldsNoRecord: As `open_token_with_scheme`.
            _TokenOfALaterScheme: As `open_token_with_scheme`.
        """
        return self.open_token_with_scheme(content)[0]

    def open_token_with_scheme(self, content: bytes):
        """The values and scheme a token of ours holds, under this key, from one decrypt.

        Args:
            content (bytes): The token.

        Returns:
            Tuple[Dict[str, Any], int]: The locked tags' values with the scheme
            key stripped, and the scheme the token names (1 for a token with
            none). A scheme-1 token may be shared across studies; a scheme-2
            token was captured per value-set.

        Raises:
            RuntimeError: The key does not decrypt the token. The message names
                the key path, never the instance or a patient, and chains no
                cryptography traceback.
            _TokenHoldsNoRecord: The key decrypts the token, but what it holds is
                not a non-empty JSON object (not UTF-8, not JSON, not an object,
                or empty once the scheme key is stripped). Nothing from the
                plaintext is in the message, and no traceback prints it.
            _TokenOfALaterScheme: The key opens the token and it holds a record,
                under a scheme this release does not know.
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
        # library wrote, and a value-type check here would refuse it.
        # `from None` here too, outside
        # any `except`, where it changes no traceback: it lets "nothing
        # is chained behind this refusal" be the one assertion
        # (`__suppress_context__`) at every raise of this door.
        if not isinstance(record, dict):
            raise _TokenHoldsNoRecord(self._no_record_message()) from None
        values, scheme = self._split_record(record)
        if not values:
            raise _TokenHoldsNoRecord(self._no_record_message()) from None
        return values, scheme

    def _split_record(self, record: Dict[str, Any]):
        """Split a decrypted record into its values and its scheme.

        The one place the scheme key is stripped. The caller's dict is not
        modified.

        Args:
            record (Dict[str, Any]): The decrypted JSON object.

        Returns:
            Tuple[Dict[str, Any], int]: A copy of the record without the scheme
            key, and the scheme (1 when the key is absent).

        Raises:
            _TokenOfALaterScheme: The scheme is not an int (a bool is not one
                here) or is not a scheme this release knows.
        """
        # A copy, never a pop, so the caller's dict is untouched. An unknown
        # scheme is refused whatever the record holds: its meaning is a later
        # release's to say.
        values = {tag: val for tag, val in record.items()
                  if tag != self.TOKEN_SCHEME_KEY}
        scheme = record.get(self.TOKEN_SCHEME_KEY, 1)
        if (isinstance(scheme, bool) or not isinstance(scheme, int)
                or not 1 <= scheme <= self.TOKEN_SCHEME):
            raise _TokenOfALaterScheme(self._later_scheme_message()) from None
        return values, scheme

    def _no_record_message(self) -> str:
        return (f"the key at {self.key_manager.key_path} opens this patient's "
                "identity token, but it holds no identity record this library "
                "writes, so nothing can be recovered from it")

    def _later_scheme_message(self) -> str:
        return (f"the key at {self.key_manager.key_path} opens this patient's "
                "identity token, but it was written by a later release of this "
                "library, which recovering it needs")

    def held_identity(self, instance: Instance):
        """The token of ours on `instance` and the values it holds under this key.

        `token_of_ours` then `open_token`. A caller reading many instances
        that share one token can call the two separately, one decrypt per
        distinct token.

        Args:
            instance (Instance): The instance to read.

        Returns:
            Optional[Tuple[bytes, Dict[str, Any]]]: `(token bytes, values)`, or
            None when the instance carries no token of ours.

        Raises:
            RuntimeError: A token of ours does not open under this key, or
                opens to no record (`open_token`'s raises, including
                `_TokenHoldsNoRecord`).
            _TokenOfAnEarlierLayout: As `token_of_ours`.
        """
        content = self.token_of_ours(instance)
        if content is None:
            return None
        return content, self.open_token(content)

    def recover_or_raise(self, instance: Instance) -> Dict[str, Any]:
        """The recovered attributes of `instance`'s token, or an exception.

        The strict counterpart of `recover_original_data`, which answers None
        for "no token" and "this key cannot open it" alike. A foreign
        Encrypted Attributes Sequence (one holding no token of ours) is "no
        token", not "the wrong key".

        Args:
            instance (Instance): The instance to read.

        Returns:
            Dict[str, Any]: The locked tags' values.

        Raises:
            RuntimeError: No Encrypted Attributes Sequence item holding a token
                of ours, or any of `held_identity`'s raises: the key does not
                decrypt the token, it opens to no record, or the token is in
                the earlier layout.
        """
        found = self.held_identity(instance)
        if found is None:
            raise RuntimeError(
                "no encrypted identity token on this patient's instances; "
                "was it locked with lock_identities() before anonymize()?")
        return found[1]

    @classmethod
    def _token_item(cls, instance: Instance):
        """The Encrypted Attributes Sequence item recovery reads, or None.

        Args:
            instance (Instance): The instance to read.

        Returns:
            Optional[DicomItem]: Item 0 of `(0400,0500)`, or None when the
            sequence is absent or empty.
        """
        # Item 0, and not the last item: the one spelling of the index both
        # reads share. Every sequence this library writes holds one item; a
        # multi-item sequence comes from the earlier layout and is refused by
        # name on its first item rather than read at some other index.
        seq = instance.sequences.get(cls.TAG_ENCRYPTED_ATTRS_SEQ)
        return seq.items[0] if seq is not None and seq.items else None

    #: `_token_content`'s answer for an item in the layout releases before
    #: 1.0 wrote: a sentinel, never content, so no caller can decrypt it.
    EARLIER_LAYOUT = object()

    @classmethod
    def _token_content(cls, item):
        """The token of ours in `item`, by shape alone: no key and no decrypt.

        Args:
            item (DicomItem): An Encrypted Attributes Sequence item.

        Returns:
            The Encrypted Content `(0400,0520)` when it is shaped like a token
            of ours; `EARLIER_LAYOUT` when `(0400,0520)` holds none and
            `(0400,0510)` does; else None (no content, or a foreign item).
        """
        # No UID passes the shape test (`.` is outside base64url), so the
        # transfer syntax beside a token is never taken for one.
        content = item.attributes.get(cls.TAG_ENCRYPTED_CONTENT)
        if content and cls.is_one_of_ours(content):
            return content
        if cls.is_one_of_ours(item.attributes.get(cls.TAG_TRANSFER_SYNTAX_UID)):
            return cls.EARLIER_LAYOUT
        return None

    @classmethod
    def holds_a_token_of_ours(cls, instance: Instance) -> bool:
        """Whether `instance` carries a token this library wrote, in either layout.

        The lock's key-creation check: shape only, no key, and it answers
        rather than raising for an earlier-layout token.

        Args:
            instance (Instance): The instance to read.

        Returns:
            bool: True for a token of ours in either layout.
        """
        # An earlier-layout item counts: a key created here opens it no
        # better than a current one, and raising the layout refusal here
        # would stop the lock for every patient in the session.
        item = cls._token_item(instance)
        return item is not None and cls._token_content(item) is not None

    @classmethod
    def holds_an_earlier_layout_token(cls, instance: Instance) -> bool:
        """Whether `instance`'s token is in the layout releases before 1.0 wrote.

        Shape only, no key. The export counts such files, which this library
        cannot recover.

        Args:
            instance (Instance): The instance to read.

        Returns:
            bool: True for a token of ours in the earlier layout.
        """
        item = cls._token_item(instance)
        return item is not None and cls._token_content(item) is cls.EARLIER_LAYOUT

    def recover_original_data(self, instance: Instance) -> Optional[Dict[str, Any]]:  # pylint: disable=missing-raises-doc  # its one raise is caught inside
        """Extracts and decrypts the original attributes from the instance.

        Reads item 0 of the Encrypted Attributes Sequence, decrypts its
        Encrypted Content and deserializes the JSON, stripping the scheme key.
        Never raises: an item in the layout releases before 1.0 wrote (never
        decrypted), a key that does not open the token, a later scheme or a
        plaintext that is not JSON is logged at ERROR and answers None; an
        item with no Encrypted Content logs a WARNING and answers None.

        Args:
            instance (Instance): The anonymized instance.

        Returns:
            Optional[Dict[str, Any]]: The recovered original attributes, or None
            when there is no token or it cannot be read. A plaintext that is
            JSON but not an object is returned as decoded.
        """
        try:
            # 1. The token item, if the instance carries one
            item = self._token_item(instance)
            if item is None:
                return None

            # 2. Read its Encrypted Content. An item in the layout
            # releases before 1.0 wrote is refused by name, undecrypted:
            # the raise lands in the `except` below, which logs it.
            if self._token_content(item) is self.EARLIER_LAYOUT:
                raise _TokenOfAnEarlierLayout(_EARLIER_LAYOUT_MESSAGE)
            encrypted_bytes = item.attributes.get(self.TAG_ENCRYPTED_CONTENT)

            if not encrypted_bytes:
                self.logger.warning("EncryptedContent (0400,0520) not found in sequence item.")
                return None

            # 3. Decrypt
            decrypted_bytes = self.engine.decrypt(encrypted_bytes)

            # 4. Deserialize, and strip the scheme key through the one
            # door: a later scheme raises into the `except` below
            # and reads None, as any token this read cannot open does.
            # A plaintext that is not an object is returned as it was.
            json_str = decrypted_bytes.decode('utf-8')
            record = json.loads(json_str)
            if isinstance(record, dict):
                record = self._split_record(record)[0]
            return record

        except Exception as e:
            # `describe_exception`, not `{e}`. The one failure that
            # means "a token is here and this key cannot open it" is
            # Fernet's `InvalidToken`, whose `str()` is empty, so `{e}`
            # would log `Failed to recover data from <uid>: ` and a
            # wrong key could not be told from any other failure.
            self.logger.error(
                f"Failed to recover data from {instance.sop_instance_uid}: "
                f"{describe_exception(e)}")
            return None
