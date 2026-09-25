"""PHI detection: `PhiInspector` scans the object graph under a policy and
returns `PhiFinding`s, each with a `PhiRemediation` proposal, in a
`PhiReport`.

Also the keyed derivations under the project secret that the scan
proposes and remediation checks: the `ANON_` pseudonym, the date-jitter
seed (`canonical_patient_key`), and the version-8 `2.25.` replacement
UIDs.
"""
import copy
from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict, Tuple
import hashlib
import hmac
import re
from pydicom.multival import MultiValue
from .entities import (JITTER_SCHEME_KEYED, JITTER_SCHEME_UNKEYED, Instance,
                       Patient, Study, is_synthetic_patient_id,
                       iter_item_tree)
from .config_manager import _vr_dummy
from .logger import get_logger
from .profiles import FLOOR_POLICY


# The replacements `scan_patient` proposes, spelled once for the two
# places that ask "is this already the Patient's replacement?":
# `scan_patient`, which raises nothing for a patient holding one, and
# `_scan_instance`, which raises nothing for an instance's top-level copy
# of it. Two spellings of one test would drift, and a drift here
# either re-flags every anonymized instance on a re-audit or stops
# flagging a real identifier that happens to start with the prefix.
def _is_replacement_name(value) -> bool:
    return value == "ANONYMIZED"


def _is_replacement_id(value) -> bool:
    return str(value).startswith("ANON_")


def _owned_rule(phi_tags, tag) -> Tuple[str, Optional[str]]:
    """`(action, value)` under which the owner of `tag` is remediated.

    No rule and the string form are `REPLACE` with no value: `ANONYMIZED`
    for the name, the keyed pseudonym for the ID, the per-patient shift
    for the date. On Study Date, `REPLACE` with no value and
    `SHIFT`/`JITTER` all read `SHIFT`.

    Args:
        phi_tags (dict): The policy.
        tag (str): Patient Name, Patient ID or Study Date.

    Returns:
        Tuple[str, Optional[str]]: The upper-cased action and the rule's
        `value:`, or None.
    """
    # The one reading of the rule for the three owned tags, so the owner
    # scans and the instance scan cannot disagree. Study Date's string form
    # (`"0008,0020": "Study Date"`) means the shift, and a DA cannot hold
    # `ANONYMIZED`. A rule the validator refuses (an emptied Patient ID,
    # say) never gets here: `PhiInspector.__init__` raises first.
    rule = (phi_tags or {}).get(tag)
    if isinstance(rule, dict):
        action = str(rule.get("action") or "REPLACE").upper()
        value = rule.get("value") or None
    else:
        action, value = "REPLACE", None
    if tag == "0008,0020" and (action in ("SHIFT", "JITTER")
                               or (action == "REPLACE" and value is None)):
        return "SHIFT", None
    return action, value


def _rule_name(rule: Dict[str, Any]) -> str:
    """A rule mapping's display name, or `Unknown Tag`.

    `name: null` is absent, as the loader reads it, so it also gives
    `Unknown Tag` rather than `None`.

    Args:
        rule (Dict[str, Any]): The rule mapping.

    Returns:
        str: The name.
    """
    name = rule.get("name")
    return "Unknown Tag" if name is None else name


def _rule_for(phi_tags, tag):
    """The rule for the concrete `tag`, or None: the most specific key wins.

    `tag` itself first; then, for an even group in 5000-501E or 6000-601E
    (PS3.5 7.6), the table's element mask (`60xx,0022`) and then its group
    mask (`60xx,xxxx`). So `6002,0022` beats `60xx,0022`, which beats
    `60xx,xxxx`. An odd group between them is private and never matches a
    mask: it is the `remove_private_tags` sweep's.

    Args:
        phi_tags (dict): The policy, lowercase-keyed.
        tag (str): A concrete `gggg,eeee` key.

    Returns:
        The rule (a mapping or a display name), or None.
    """
    # The finding a mask raises names the concrete tag, so remediation, the
    # audit rows and the process boundary see an ordinary rule.
    rule = phi_tags.get(tag)
    if rule:
        return rule
    try:
        group = int(tag[:4], 16)
    except (TypeError, ValueError):
        return None
    if group % 2 or not (0x5000 <= group <= 0x501E or 0x6000 <= group <= 0x601E):
        return None
    prefix = tag[:2]
    return phi_tags.get(f"{prefix}xx,{tag[5:]}") or phi_tags.get(f"{prefix}xx,xxxx")


def _holds_owned_replacement(phi_tags, tag, value, study=None) -> bool:
    """Whether `value`, an owner's value for `tag`, is already what the rule
    on `tag` resolves to.

    The scan's "already replaced?" test, action by action. Only the value
    the rule resolves to *now* counts: a name holding `ANONYMIZED` under
    `REPLACE value: v` is raised and rewritten, and so is a `v1` under
    `value: v2`.

    - `KEEP`, `REMOVE`, `EMPTY`: False. REMOVE is judged by presence and
      EMPTY by blankness, which every caller tests before it asks this.
    - Name `REPLACE`: the rule's `value:`, or `ANONYMIZED`.
    - ID `REPLACE`: an `ANON_` pseudonym.
    - Date `SHIFT`: a date this pipeline's shift produced
      (`_study_date_is_this_pipelines`; needs `study`).
    - Date `REPLACE value: v`: the value renders as `v`.

    Args:
        phi_tags (dict): The policy.
        tag (str): Patient Name, Patient ID or Study Date.
        value: The owner's value.
        study (Study, optional): The owning study, for a Study Date.

    Returns:
        bool: Whether the value is the rule's replacement.
    """
    # `lock_identities` also refuses the constants on any policy; that OR
    # belongs to the lock and must not be shared with the scan, which would
    # then never write `v` over an old `ANONYMIZED`.
    action, rule_value = _owned_rule(phi_tags, tag)
    if tag == "0010,0010" and action == "REPLACE":
        return value == (rule_value or "ANONYMIZED")
    if tag == "0010,0020" and action == "REPLACE":
        return _is_replacement_id(value)
    if tag == "0008,0020" and action == "SHIFT":
        return _study_date_is_this_pipelines(study)
    if tag == "0008,0020" and action == "REPLACE":
        # Lazy: io_handlers is the heavy module, and this is the one
        # spelling of "a Study's date as a DA string".
        from .io_handlers import format_study_date  # pylint: disable=import-outside-toplevel
        return value is not None and format_study_date(value) == rule_value
    return False


#: The hex run inside an unkeyed `ANON_` replacement. Used to refuse a
#: value that merely starts with the prefix (`ANON_xyz`, a user's own id)
#: before `_unkeyed_jitter_digest` reads eight characters out of it.
_UNKEYED_IS_HEX = re.compile(r"[0-9a-f]+")

#: The unkeyed scheme's shape: how many hex characters of the PatientID
#: digest its replacement carried, and how many its date jitter seeded
#: on. Read only for patients a store classed `JITTER_SCHEME_UNKEYED`;
#: nothing keyed reads them.
_UNKEYED_REPLACEMENT_DIGEST_CHARS = 12
_UNKEYED_JITTER_DIGEST_CHARS = 8

#: The keyed scheme. The pseudonym is `ANON_` + `_DIGEST_HEX` hex of an
#: HMAC of the original + `_CHECK_HEX` hex of an HMAC of that digest, so
#: 29 characters. The digest is 64 bits because a collision merges two
#: patients under `patients UNIQUE(patient_id)`; the check is what lets
#: the library tell whether a secret it was given minted a pseudonym
#: (`_pseudonym_verifies`), which is how a missing or foreign secret is
#: refused or warned about rather than silently used.
#:
#: Three labels, one per derivation, so no output of one is an output of
#: another: the date offset in particular is **not** readable out of the
#: pseudonym. Changing a label changes every pseudonym and offset a store
#: has minted.
_DIGEST_HEX = 16
_CHECK_HEX = 8
_LABEL_PATIENT_ID = b"isocenter/v1/patient-id\x00"
_LABEL_PSEUDONYM_CHECK = b"isocenter/v1/pseudonym-check\x00"
_LABEL_DATE_JITTER = b"isocenter/v1/date-jitter\x00"
_KEYED_PSEUDONYM = re.compile(
    rf"ANON_[0-9a-f]{{{_DIGEST_HEX + _CHECK_HEX}}}")
_UNKEYED_PSEUDONYM = re.compile(
    rf"ANON_[0-9a-f]{{{_UNKEYED_REPLACEMENT_DIGEST_CHARS}}}")


def _require_secret(secret) -> bytes:
    """The project secret, or `RuntimeError`.

    Args:
        secret: The secret, or a falsy value.

    Returns:
        bytes: The secret.

    Raises:
        RuntimeError: When `secret` is falsy.
    """
    # There is deliberately no default. A keyed derivation handed no secret
    # that quietly used the unkeyed one instead would be a single-line road
    # back to offsets anyone can compute, and no end-to-end test would see
    # it, because the output still looks like a pseudonym.
    if not secret:
        raise RuntimeError(
            "No project secret: the pseudonym, the date offset and the "
            "replacement UIDs are derived under the store's project secret, "
            "and there is no unkeyed fallback. Session.audit(), anonymize() "
            "and redact() obtain it from the store; a PhiInspector, "
            "RemediationService or RedactionService built by hand has to be "
            "given one.")
    return bytes(secret)


def _hmac(secret, label: bytes, text) -> bytes:
    return hmac.new(_require_secret(secret), label + str(text).encode(),
                    hashlib.sha256).digest()


def _replacement_id_for(patient_id, secret) -> str:
    """The PatientID replacement `scan_patient` proposes for a keyed
    patient: `ANON_` + 16 hex of a keyed digest + 8 hex of its check.

    Args:
        patient_id: The original id; read as `str`.
        secret: The project secret.

    Returns:
        str: The pseudonym.

    Raises:
        RuntimeError: With no secret.
    """
    # Spelled once, because the date jitter canonicalizes an original id
    # to this value (`canonical_patient_key`) and `_pseudonym_verifies`
    # recomputes its check: two spellings would let the prefix, the
    # lengths or a label drift on one side only.
    digest = _hmac(secret, _LABEL_PATIENT_ID, patient_id).hex()[:_DIGEST_HEX]
    check = _hmac(secret, _LABEL_PSEUDONYM_CHECK, digest).hex()[:_CHECK_HEX]
    return f"ANON_{digest}{check}"


#: UID replacement. Three more labels, disjoint from the three
#: above, so a minted UID is not readable out of a pseudonym or an
#: offset, nor a redacted instance's UID out of its unredacted one.
#: Changing a label re-maps every UID a store has minted: every exported
#: path and every UI element moves (fingerprint/output.json).
_LABEL_UID = b"isocenter/v1/uid\x00"
_LABEL_REDACTED_UID = b"isocenter/v1/redacted-uid\x00"
_LABEL_UID_CHECK = b"isocenter/v1/uid-check\x00"
#: How the 16 bytes of a minted UUID split: an 11-byte digest of the
#: source (82 bits once the version and variant are set) and a 5-byte
#: (40-bit) keyed check over those 11 bytes as written.
_UID_DIGEST_BYTES = 11
_UID_CHECK_BYTES = 5
#: `2.25.` and a UUID integer with no leading zero: the one spelling
#: `uids.uid_from_bytes16` writes, so no other spelling of the same
#: number verifies.
_MINTED_UID = re.compile(r"2\.25\.(0|[1-9][0-9]{0,38})")


def _uid_check(secret, digest11: bytes) -> bytes:
    return hmac.new(_require_secret(secret), _LABEL_UID_CHECK + digest11,
                    hashlib.sha256).digest()[:_UID_CHECK_BYTES]


def _mint_uid(secret, label: bytes, text: str) -> str:
    """`2.25.<n>`, `n` an RFC 9562 version-8 UUID: 82 bits of an HMAC of
    `text` under `label`, and 40 bits of a keyed check over them.

    Args:
        secret: The project secret.
        label (bytes): The derivation's label.
        text (str): What the UID is derived from.

    Returns:
        str: The UID.

    Raises:
        RuntimeError: With no secret.
    """
    # The version and variant are set on the 11 digest bytes *before* the
    # check is computed, so a verifier reading the UID back recomputes it
    # over exactly what it reads; `uid_from_bytes16` sets them again, which
    # changes nothing (both are in the first 11 bytes, and masking is
    # idempotent).
    from .uids import uid_from_bytes16  # pylint: disable=import-outside-toplevel
    digest = bytearray(_hmac(secret, label, text)[:_UID_DIGEST_BYTES])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uid_from_bytes16(bytes(digest) + _uid_check(secret, bytes(digest)))


def _replacement_uid_for(uid, secret) -> str:
    """The UID that replaces `uid` in this project.

    A function of the value alone -- never the tag it sits under, the
    patient or the study -- so a SOP Instance UID, the Referenced SOP
    Instance UID in another file that names it, and the file meta's copy
    all map to one replacement. Deterministic per project secret, and
    recognisable by `_uid_is_minted` under that secret.

    Args:
        uid: The source UID; read as `str`.
        secret: The project secret.

    Returns:
        str: The replacement UID.

    Raises:
        RuntimeError: With no secret.
    """
    return _mint_uid(secret, _LABEL_UID, str(uid))


def _redaction_uid_for(source_uid, config_hash, secret) -> str:
    """The SOP Instance UID a redaction gives an instance.

    Keyed on the instance's **source** SOP Instance UID and the redaction
    configuration's hash, so the same redaction in the same project gives
    the same UID whether it runs before or after `anonymize()`, a
    different set of zones gives a different one, and the label
    keeps it from ever equalling `_replacement_uid_for(source_uid)`: the
    redacted pixels never share a UID with the unredacted image.

    Args:
        source_uid: The instance's source SOP Instance UID.
        config_hash: The redaction configuration's hash.
        secret: The project secret.

    Returns:
        str: The new SOP Instance UID.

    Raises:
        RuntimeError: With no secret.
    """
    return _mint_uid(secret, _LABEL_REDACTED_UID,
                     f"{source_uid}\x00{config_hash}")


def _uid_is_minted(value, secret) -> bool:
    """Whether `value` is a UID this project minted, of either kind.

    A UID minted under another secret does not verify. Exactly one
    spelling verifies (no leading zero, no whitespace, below 2**128), and
    only a `str`: a multi-valued element is judged value by value by the
    caller.

    Args:
        value: The candidate UID.
        secret: The project secret.

    Returns:
        bool: Whether it verifies under `secret`.

    Raises:
        RuntimeError: With no secret, whatever `value` is.
    """
    # What makes UID replacement idempotent without stored state: a scan
    # leaves a minted UID alone, so a second `anonymize()`, a reopened
    # store and a re-ingested export of this project do not replace it
    # again.
    secret = _require_secret(secret)
    raw = _minted_uid_bytes(value)
    if raw is None:
        return False
    return hmac.compare_digest(raw[_UID_DIGEST_BYTES:],
                               _uid_check(secret, raw[:_UID_DIGEST_BYTES]))


def _minted_uid_bytes(value) -> Optional[bytes]:
    """The 16 bytes of `value` if it has the shape this library mints --
    `2.25.`, the one spelling of an integer below 2**128, version 8,
    variant `10` -- else None. No secret: the shape alone.

    Args:
        value: The candidate; anything but a `str` gives None.

    Returns:
        Optional[bytes]: The 16 bytes, or None.
    """
    if not isinstance(value, str):
        return None
    match = _MINTED_UID.fullmatch(value)
    if match is None:
        return None
    number = int(match.group(1))
    if number >= 1 << 128:
        return None
    raw = number.to_bytes(16, "big")
    if raw[6] >> 4 != 0x8 or raw[8] >> 6 != 0b10:
        return None
    return raw


def _has_minted_uid_shape(value) -> bool:
    """Whether `value` has a minted UID's shape, verifiable or not.

    The evidence a store that lost its secret refuses on, where there is
    no secret left to verify with.

    Args:
        value: The candidate.

    Returns:
        bool: Whether `_minted_uid_bytes` reads it.
    """
    return _minted_uid_bytes(value) is not None


#: The proposal metadata key that marks a keyed UID replacement.
#: What remediation reads to move an Instance's own SOP Instance UID with
#: its top-level element (`Instance._take_sop_uid`), rather than write the
#: element alone as a `REPLACE value:` does.
UID_REPLACEMENT = "uid_replacement"


def _owned_uid_is_open(phi_tags, tag, uid, secret) -> bool:
    """Whether the owner holding `uid` under `tag` is raised: the
    policy's rule on `tag` is the value-less REPLACE, and `uid` is a
    non-blank UID this project did not mint.

    Shared by `PhiInspector._scan_owned_uid`, which raises the finding,
    and `RemediationService._settle_statuses`, which asks it of every
    Series at a pass end.

    Args:
        phi_tags (dict): The policy.
        tag (str): `0020,000d` or `0020,000e`.
        uid: The owner's UID.
        secret: The project secret.

    Returns:
        bool: Whether the owner would be raised.

    Raises:
        RuntimeError: With no secret, when the rule is the UID replacement
            and `uid` is not blank.
    """
    # One spelling for both, so the two cannot disagree. A Series has no
    # stored status, so after a reopen a pass handed a plain list has
    # nothing else to say its UID is still open.
    return (_is_uid_replacement(_rule_for(phi_tags, tag), tag) and uid is not None
            and bool(str(uid).strip()) and not _uid_is_minted(str(uid), secret))


def _is_uid_replacement(rule, tag) -> bool:
    """Whether `rule` on `tag` is the keyed UID replacement: REPLACE
    with no `value:` (or the string form, which is that), on a tag whose
    standard dictionary VR is UI.

    A private key has no dictionary VR, so it keeps writing `ANONYMIZED`;
    a rule with a `value:` writes the value; and no rule is not this rule,
    so `privacy_profile: none` with no tags keeps every UID.

    Args:
        rule: The rule for `tag`, a mapping, a display name, or None.
        tag (str): The concrete tag.

    Returns:
        bool: Whether it is the keyed UID replacement.
    """
    # One reading for the instance scan and the owners' scans, and the
    # loader admits exactly this spelling (`_refused_phi_rule`).
    if isinstance(rule, dict):
        if (str(rule.get("action") or "REPLACE").upper() != "REPLACE"
                or rule.get("value")):
            return False
    elif not isinstance(rule, str):
        return False
    from .config_manager import _standard_dictionary_vr  # pylint: disable=import-outside-toplevel
    return _standard_dictionary_vr(tag) == "UI"


def _uid_text(value) -> str:
    """One UI value as the text the derivation reads.

    Args:
        value: A `str`, or `bytes` -- what a UI element arrives as when its
            VR was not on the wire and the dictionary did not name it.

    Returns:
        str: The text; bytes are decoded as ASCII with trailing NUL and
        space padding stripped.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", "replace").rstrip("\x00 ")
    return str(value)


def _replaced_uids(value, secret):
    """What the keyed UID replacement writes over `value`, or None when it
    writes nothing: every value is blank or was minted by this project.

    A multi-valued element (`0008,0058`, VM 1-n) is judged and replaced
    value by value and comes back as a list, which the exporter writes as
    a multi-value; a value this project minted keeps its place unchanged.

    Args:
        value: One UID, or a list, tuple or `MultiValue` of them.
        secret: The project secret.

    Returns:
        The replacement (`str`, or `list` for a multi-valued input), or
        None.

    Raises:
        RuntimeError: With no secret, when a value is not blank.
    """
    multi = isinstance(value, (list, tuple, MultiValue))
    texts = [_uid_text(v) for v in (value if multi else [value])]
    todo = [bool(t.strip()) and not _uid_is_minted(t, secret) for t in texts]
    if not any(todo):
        return None
    new = [_replacement_uid_for(t, secret) if replace else t
           for t, replace in zip(texts, todo)]
    return new if multi else new[0]


def _pseudonym_verifies(patient_id, secret) -> bool:
    """Whether `patient_id` is a keyed pseudonym minted under `secret`.

    Diagnostics only -- the refusals and warnings about a missing or
    foreign secret. Never used to decide an offset: every `ANON_` id
    seeds from its own text, whether or not it verifies.

    Args:
        patient_id: The id; read as `str`.
        secret: The secret to verify under.

    Returns:
        bool: Whether it has the keyed shape and its check verifies.

    Raises:
        RuntimeError: With no secret, for an id of the keyed shape.
    """
    text = str(patient_id)
    if not _KEYED_PSEUDONYM.fullmatch(text):
        return False
    digest, check = text[5:5 + _DIGEST_HEX], text[5 + _DIGEST_HEX:]
    expected = _hmac(secret, _LABEL_PSEUDONYM_CHECK, digest).hex()[:_CHECK_HEX]
    return hmac.compare_digest(check, expected)


def _is_keyed_pseudonym_shape(patient_id) -> bool:
    return bool(_KEYED_PSEUDONYM.fullmatch(str(patient_id)))


def _is_unkeyed_pseudonym_shape(patient_id) -> bool:
    return bool(_UNKEYED_PSEUDONYM.fullmatch(str(patient_id)))


def _unkeyed_replacement_id_for(patient_id) -> str:
    """The replacement the unkeyed scheme minted, for a legacy patient.

    Reachable only for a patient its store classed
    `JITTER_SCHEME_UNKEYED` when opened.

    Args:
        patient_id: The original id; read as `str`.

    Returns:
        str: `ANON_` + the first 12 hex of its unkeyed SHA-256.
    """
    # A legacy patient whose id is no longer `ANON_` (restored, or never
    # replaced) has to re-mint *this* value, or the next pass reads a
    # different id back and the patient ends up with two offsets.
    digest = hashlib.sha256(str(patient_id).encode()).hexdigest()
    return f"ANON_{digest[:_UNKEYED_REPLACEMENT_DIGEST_CHARS]}"


def _unkeyed_jitter_digest(patient_id) -> str:
    """The unkeyed scheme's jitter seed, for a legacy patient.

    An `ANON_` id whose first eight characters after the prefix are
    lowercase hex seeds from those characters, anything else from
    `sha256(text)[:8]`.

    Args:
        patient_id: The id; read as `str`.

    Returns:
        str: Eight hex characters.
    """
    # Must stay bit for bit as the unkeyed scheme computed it, so a patient
    # whose dates a store already shifted under it gets the same offset for
    # a date shifted now, and never a second one.
    text = str(patient_id)
    if _is_replacement_id(text):
        carried = text[5:5 + _UNKEYED_JITTER_DIGEST_CHARS]
        if (len(carried) == _UNKEYED_JITTER_DIGEST_CHARS
                and _UNKEYED_IS_HEX.fullmatch(carried)):
            return carried
    return hashlib.sha256(text.encode()).hexdigest()[
        :_UNKEYED_JITTER_DIGEST_CHARS]


def canonical_patient_key(patient_id, secret, scheme) -> int:
    """The integer that seeds a patient's date offset, from either
    spelling of its identity.

    The original id and its pseudonym give the same seed, so a date first
    shifted in a later pass (which reads the pseudonym) gets the offset
    its siblings got.

    **Keyed** (`JITTER_SCHEME_KEYED`): an original id is mapped to the
    pseudonym it would be replaced by; an id already `ANON_` is taken as
    it stands. The seed is an HMAC of that pseudonym under its own label,
    so nothing about it can be read out of the pseudonym's characters.

    **Unkeyed** (`JITTER_SCHEME_UNKEYED`): `_unkeyed_jitter_digest`,
    for a patient its store classed legacy at open. No secret is read.

    Args:
        patient_id (str): The original id or its pseudonym; any value is
            read as `str`.
        secret (bytes): The project secret; read only by the keyed
            scheme.
        scheme (str): `JITTER_SCHEME_KEYED` or `JITTER_SCHEME_UNKEYED`.

    Returns:
        int: The seed, which the caller reduces mod the jitter span.

    Raises:
        RuntimeError: For the keyed scheme with no secret.
        ValueError: For a scheme it does not know.
    """
    # Never a default scheme: a wrong guess is a second offset for a
    # patient.
    if scheme == JITTER_SCHEME_UNKEYED:
        return int(_unkeyed_jitter_digest(patient_id), 16)
    if scheme != JITTER_SCHEME_KEYED:
        raise ValueError(f"unknown jitter scheme {scheme!r}")
    text = str(patient_id)
    canonical = (text if _is_replacement_id(text)
                 else _replacement_id_for(text, secret))
    return int.from_bytes(
        _hmac(secret, _LABEL_DATE_JITTER, canonical)[:8], "big")


def _study_date_is_this_pipelines(study) -> bool:
    """Whether `study.study_date` is a value this pipeline's shift
    produced -- as far as the store can tell.

    A study with `date_shifted` set and no record of the shifted value
    (a store from before per-value records) counts as "this pipeline's";
    the load says so once, as a WARNING audit row.

    Args:
        study: The study, or None (False).

    Returns:
        bool: Whether the date is the shift's own output.
    """
    # Spelled once because two places ask it: `_scan_study` and
    # `_holds_owners_replacement`. Neither may test `study.date_shifted`
    # alone: that flag records *that* a shift happened, never *what it
    # produced*, so a fresh original assigned to `study_date` would never
    # be raised again, and an instance's copy of it would be skipped as
    # "the owner's replacement".
    #
    # `getattr` throughout because the arm and the scan both fire for any
    # object carrying these names, test doubles included.
    if study is None:
        return False
    vouches = getattr(study, "date_shift_vouches_for", None)
    if callable(vouches) and vouches(getattr(study, "study_date", None)):
        return True
    return (bool(getattr(study, "date_shifted", False))
            and getattr(study, "_shifted_study_date", None) is None)


@dataclass(slots=True)
class PhiRemediation:
    """Proposed action to fix a PHI finding.

    Attributes:
        action_type (str): The remediation logic code (e.g., "REPLACE_TAG", "SHIFT_DATE").
        target_attr (str): The attribute or tag to modify.
        new_value (Any): The proposed new value (if known).
        original_value (Any): The original value for audit/reversion.
        metadata (Dict[str, Any]): Context metadata (e.g. patient linkage for date shifting).
    """
    action_type: str  # e.g., "REPLACE_TAG", "REDACT_REGION"
    target_attr: str  # e.g., "patient_name", "study_date"
    new_value: Any = None
    original_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PhiFinding:  # pylint: disable=too-many-instance-attributes
    """A potential PHI breach discovered during a scan: one report line.

    Attributes:
        entity_uid (str): Unique identifier of the entity (PatientID, SOPInstanceUID).
        entity_type (str): Type of entity ("Patient", "Instance", etc).
        field_name (str): The specific field or tag description.
        value (Any): The PHI value found.
        reason (str): Why this was flagged (e.g. "Safe Harbor Rules").
        tag (Optional[str]): The DICOM tag (e.g., "0010,0010").
        patient_id (Optional[str]): Linkage for context.
        entity (Any): Reference to the Python object for direct remediation.
        remediation_proposal (Optional[PhiRemediation]): The suggested fix.
        metadata (Dict[str, Any]): Context carried with the finding.
        entity_path (Tuple): Route from the Instance to the item this was
            raised against, as `(sequence_tag, index)` steps. Empty means
            the Instance itself. The only way to rebind a finding to its
            sequence item after it crosses a process boundary.
    """
    # Not split to satisfy the attribute count: it is a report line, and
    # every field is something a reader of that report needs.
    entity_uid: str
    entity_type: str
    field_name: str
    value: Any
    reason: str
    tag: Optional[str] = None  # specific DICOM tag if applicable
    patient_id: Optional[str] = None  # linkage
    entity: Any = None  # Reference to the actual object (Patient, Study, etc.)
    remediation_proposal: Optional[PhiRemediation] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    entity_path: Tuple = ()  # Route from the Instance down to a sequence item


class PhiReport:
    """A container for PHI findings that supports analysis and export.

    Iterates, indexes and measures like the list of findings it wraps,
    and adds DataFrame export.

    `failures` is a list of `(entity_uid, reason)`, one per instance a
    pixel scan could not read in full: its pixels could not be loaded, or
    OCR raised on at least one of its frames. `scan_pixel_content()` fills
    it; an instance that failed on some frames keeps the findings of the
    frames that were read. It is always a list, never `None`. `audit()`'s
    is always empty: a failure in its workers raises instead.
    """

    def __init__(self, findings: List[PhiFinding],
                 failures: Optional[List[Tuple[str, str]]] = None):
        self.findings = findings
        self.failures: List[Tuple[str, str]] = list(failures or [])

    def to_dataframe(self):
        """Convert the findings to a pandas DataFrame, one row each.

        Returns:
            pd.DataFrame: A dataframe containing flattened finding details.

        Raises:
            ImportError: If pandas is not installed.
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "Pandas is required for this feature. Install it with `pip install pandas`.")

        data = []
        for f in self.findings:
            row = {
                "patient_id": f.patient_id,
                "entity_type": f.entity_type,
                "entity_uid": f.entity_uid,
                "tag": f.tag,
                "field": f.field_name,
                "value": str(f.value),
                "reason": f.reason,
                "action": f.remediation_proposal.action_type if f.remediation_proposal else None
            }
            data.append(row)
        return pd.DataFrame(data)

    def __iter__(self):
        return iter(self.findings)

    def __len__(self):
        return len(self.findings)

    def __getitem__(self, index):
        return self.findings[index]

    def __repr__(self):
        return (f"<PhiReport: {len(self.findings)} findings, "
                f"{len(self.failures)} failures>")


class PhiInspector:
    """Scans the object graph for PHI under a policy.

    Applies the policy's tag rules at every depth, the owners' rules for
    Patient Name, Patient ID, Study Date and the Study and Series UIDs,
    and, when asked, the private-tag sweep.
    """

    def __init__(self,
                 config_tags: Dict[str,
                                   str] = None,
                 remove_private_tags: bool = False,
                 project_secret: bytes = None):
        """Build an inspector over one policy.

        Args:
            config_tags (Dict[str, Union[str, Dict]], optional): The PHI
                policy, as `configuration.phi_tags` holds it; None applies
                a copy of the floor, `profiles.FLOOR_POLICY`, as a bare
                session does. There is no path argument: a configuration
                file is read by `load_config()` or `audit(config_path=)`,
                the one loader. A tag's value is either a **rule** --
                `{"name": ..., "action": "REMOVE"|"EMPTY"|"SHIFT"|"JITTER"}`
                -- or a plain string, which is the tag's **display name**
                and leaves the action as `REPLACE`. The string form names
                the tag; it does not choose what happens to it. Passing a
                bare action name (`{"0008,0080": "REMOVE"}`) therefore
                replaces the value instead, and is warned about at
                construction. Study Date is the exception: its REPLACE
                with no value is the per-patient shift. Keys are
                lowercased here, with a WARNING when two keys collide.
            remove_private_tags (bool): If True, scans all attributes for non-whitelisted private tags.
            project_secret (bytes, optional): The store's project secret,
                which keys the PatientID replacement. Optional here and
                required at use: `scan_patient` raises `RuntimeError`
                when it has to mint a keyed replacement without one.
                `Session.audit()` always passes it.

        Raises:
            ValueError: For a rule the scan cannot honour
                (`config_manager.validate_phi_policy`).
        """
        self.remove_private_tags = remove_private_tags
        self.project_secret = project_secret

        if config_tags is not None:
            self.phi_tags = config_tags
        else:
            # A copy: the table is normalized below, and a caller may
            # edit it.
            self.phi_tags = copy.deepcopy(FLOOR_POLICY)

        # Normalize tag-key casing here, once, at the boundary between
        # however phi_tags got built (a built-in profile, a user's YAML, an
        # external profile file, the floor, or a caller's dict) and how
        # it's looked up. Every ingested attribute key on the object graph
        # is lowercased (`io_handlers.py`'s `populate_attrs`), so a tag
        # spelled with an uppercase hex letter -- `0008,103E` for Series
        # Description -- would otherwise never match and silently disable
        # a declared policy with no error anywhere.
        self.phi_tags = self._normalize_tag_keys(self.phi_tags)
        # A rule the scan cannot honour raises here, as it does at the
        # loader and at `audit()`. The inspector is also built directly,
        # and in every worker; the check is cheap.
        from .config_manager import validate_phi_policy  # pylint: disable=import-outside-toplevel
        validate_phi_policy(self.phi_tags, "config_tags")
        self._warn_on_bare_action_values()
        # (tag, action) pairs already warned about as meaningless on a
        # sequence, so an audit says it once rather than once per
        # instance.
        self._sequence_actions_warned = set()

    # Action names a caller is most likely to write where a description
    # belongs, having read `Dict[str, str]` and reasonably concluded the
    # string chooses the behaviour. `REPLACE` is deliberately absent: it
    # is what the string form already does, so there is no gap between
    # what was asked and what happens, and the warning would only advise
    # writing a rule to obtain the behaviour already in effect.
    _ACTION_WORDS = frozenset({"REMOVE", "EMPTY", "SHIFT", "JITTER"})

    def _warn_on_bare_action_values(self) -> None:
        """Log a WARNING for each tag whose string value names an action.

        A string value is the tag's display name and always leaves the
        action as `REPLACE`, so `{"0008,0012": "SHIFT"}` writes the DA
        dummy `19000101` instead of shifting the date. On Study Date the
        string form means the shift, so `"SHIFT"` and `"JITTER"` there are
        not warned about; `"REMOVE"` and `"EMPTY"` are, naming the shift as
        what happens.
        """
        # Warned rather than raised: the string form works as designed, and
        # a caller may legitimately have a tag *described* as "Shift".
        offenders = sorted(
            tag for tag, val in self.phi_tags.items()
            if isinstance(val, str) and val.strip().upper() in self._ACTION_WORDS
            and not (_owned_rule(self.phi_tags, tag)[0] == "SHIFT"
                     and val.strip().upper() in ("SHIFT", "JITTER")))
        if not offenders:
            return

        for tag in offenders:
            effect = ("on Study Date that is the shift"
                      if _owned_rule(self.phi_tags, tag)[0] == "SHIFT"
                      else "the action stays REPLACE")
            get_logger().warning(
                "config_tags[%r] is %r, which is read as the tag's display "
                "name, not its action -- %s. To %s "
                "this tag, write {'action': %r, 'name': ...}.",
                tag, self.phi_tags[tag], effect,
                self.phi_tags[tag].strip().lower(),
                self.phi_tags[tag].strip().upper())

    @staticmethod
    def _normalize_tag_keys(phi_tags: Dict[str, Any]) -> Dict[str, Any]:
        """Lowercase every string PHI-tag key so it matches the
        lowercased 'gggg,eeee' keys the object graph actually uses.

        On a collision the later key wins, with a WARNING.

        Args:
            phi_tags (Dict[str, Any]): The policy.

        Returns:
            Dict[str, Any]: A new mapping (or `phi_tags` itself when
            empty); non-string keys pass through unchanged.
        """
        if not phi_tags:
            return phi_tags

        normalized: Dict[str, Any] = {}
        for key, value in phi_tags.items():
            norm_key = key.lower() if isinstance(key, str) else key
            if norm_key in normalized:
                # Two source keys collided after lowercasing (e.g. both
                # "0008,103E" and "0008,103e" were present). Last one
                # wins, matching ordinary dict.update()/dict-literal
                # overwrite semantics -- but log it, since a silent
                # overwrite here is exactly the kind of thing this
                # normalization exists to make loud.
                get_logger().warning(
                    "PHI tag key collision after case normalization: "
                    "%r overwrites the earlier entry for %r.", key, norm_key)
            normalized[norm_key] = value
        return normalized

    def scan_patient(self, patient: Patient) -> List[PhiFinding]:
        """Scan a Patient and everything beneath it for PHI.

        Patient Name and Patient ID are judged under their own rules
        (`_owned_rule`); a value already holding the rule's replacement,
        and a synthetic key for a subject with no Patient ID, raise
        nothing. Then every study, series and instance beneath is scanned.

        Args:
            patient (Patient): The patient object to scan.

        Returns:
            List[PhiFinding]: A list of all identified PHI findings.

        Raises:
            RuntimeError: When the inspector has no project secret and
                a keyed Patient ID replacement has to be minted, or a
                non-blank UID sits under the keyed UID replacement.
        """
        findings = []

        # 1. Direct Attributes, under their own rules.
        name_action, name_value = _owned_rule(self.phi_tags, "0010,0010")
        name_proposal = None
        if name_action == "REMOVE":
            if patient.patient_name is not None:
                name_proposal = PhiRemediation(
                    action_type="REMOVE_TAG", target_attr="patient_name",
                    original_value=patient.patient_name)
        elif name_action == "EMPTY":
            if patient.patient_name:
                name_proposal = PhiRemediation(
                    action_type="REPLACE_TAG", target_attr="patient_name",
                    new_value="", original_value=patient.patient_name)
        elif name_action == "REPLACE":
            if (patient.patient_name and patient.patient_name != "Unknown"
                    and not _holds_owned_replacement(
                        self.phi_tags, "0010,0010", patient.patient_name)):
                name_proposal = PhiRemediation(
                    action_type="REPLACE_TAG",
                    target_attr="patient_name",
                    new_value=name_value or "ANONYMIZED",
                    original_value=patient.patient_name
                )
        # KEEP: no finding.
        if name_proposal is not None:
            proposal = name_proposal
            findings.append(PhiFinding(
                entity_uid=patient.patient_id,
                entity_type="Patient",
                field_name="patient_name",
                value=patient.patient_name,
                reason="Names are PHI",
                tag="0010,0010",
                patient_id=patient.patient_id,
                entity=patient,
                remediation_proposal=proposal
            ))

        # Patient ID is kept or pseudonymised and nothing else: every other
        # rule is refused at construction (`validate_phi_policy`), because
        # the ID keeps two patients apart.
        id_action, _ = _owned_rule(self.phi_tags, "0010,0020")
        assert id_action in ("KEEP", "REPLACE"), id_action
        if (id_action == "REPLACE" and patient.patient_id
                # A subject with no Patient ID has nothing to pseudonymize:
                # its key is never exported, and replacing it would move
                # its date offset.
                and not is_synthetic_patient_id(patient.patient_id)
                and not _is_replacement_id(patient.patient_id)):
            # Through the constructors, not spelled here: the date
            # jitter canonicalizes an original id to this value, so the
            # prefix, lengths and labels have one home. The scheme is the
            # one the store fixed for this patient at open: a legacy
            # (unkeyed) patient re-mints its old replacement, or its next
            # pass reads back a different id and gets a second offset.
            if patient._jitter_scheme == JITTER_SCHEME_UNKEYED:
                hashed_id = _unkeyed_replacement_id_for(patient.patient_id)
            else:
                hashed_id = _replacement_id_for(patient.patient_id,
                                                self.project_secret)
            proposal = PhiRemediation(
                action_type="REPLACE_TAG",
                target_attr="patient_id",
                new_value=hashed_id,
                original_value=patient.patient_id
            )
            findings.append(PhiFinding(
                entity_uid=patient.patient_id,
                entity_type="Patient",
                field_name="patient_id",
                value=patient.patient_id,
                reason="Medical Record Numbers are PHI",
                tag="0010,0020",
                patient_id=patient.patient_id,
                entity=patient,
                remediation_proposal=proposal
            ))

        # 2. Traverse Children & Scan Instances (Generic Unified Config)
        for study in patient.studies:
            findings.extend(self._scan_study(
                study, patient.patient_id,
                jitter_scheme=patient._jitter_scheme))

            for series in study.series:
                findings.extend(self._scan_series(series, patient.patient_id))
                for instance in series.instances:
                    findings.extend(self._scan_instance(instance, patient.patient_id,
                                                        study=study, patient=patient))

        return findings

    def _scan_instance(self, instance: Instance, patient_id: str,
                       study: Study = None, patient: Patient = None) -> List[PhiFinding]:
        """Scan one instance, at every depth, under the policy and the
        private-tag sweep.

        Findings on sequences (private sequences, and REMOVE or EMPTY
        rules on a sequence tag) come last, deepest first
        (`_innermost_first`), so anything raised inside a sequence is
        remediated before its container is removed.

        Args:
            instance (Instance): The instance.
            patient_id (str): Recorded on each finding.
            study (Study, optional): The owning study.
            patient (Patient, optional): The owning patient. With the
                owners, a top-level copy of a tag they own that already
                holds their replacement is not a finding; without them
                the policy judges every copy.

        Returns:
            List[PhiFinding]: The findings.

        Raises:
            RuntimeError: When the inspector has no project secret and
                a non-blank UID sits under the keyed UID replacement,
                minted or not.
        """
        findings = []

        # Appended last, and that is the point: these delete a whole
        # sequence, and a configured-tag finding raised *inside* one
        # holds a live reference to an item within it. Remediating the
        # contents before removing the container means every audit row
        # describes an item that was still in the graph when it was
        # written.
        seq_removals = []

        # 0. Determine Scan Targets
        # Walked from the instance itself, not read off a prebuilt index.
        # A stored index is a second answer to "where does text live" that
        # can disagree with the graph: one built at ingest is not rebuilt
        # on load nor carried into the worker copies `session.audit()`
        # scans, so the scan would go top-level-only and report clean on
        # sequence content it never opened. If an index is ever wanted,
        # derive it here, per scan. There is no text-VR filter: a
        # configured PHI tag is one wherever it sits and whatever its VR.
        scan_targets = [
            (item, tag, path)
            for item, path in iter_item_tree(instance)
            for tag in list(item.attributes.keys())
        ]

        # 1. Private Tag Removal Logic
        if self.remove_private_tags:
            # Private tags live in odd groups. Remove all of them except
            # two, which are NOT the reversibility service's tags:
            # reversibility uses the Encrypted Attributes Sequence
            # (0400,0500), an *even* group, which this sweep never reaches.
            #
            # (0099,0010) and (0099,1001) are the encrypted-identity
            # payload of a store written by this library's one release
            # under the name `gantry`. The exemption keeps
            # `remove_private_tags` from stripping those identities,
            # leaving them unrecoverable with their own key.
            #
            # This is one of two halves. `DicomExporter._merge`
            # (io_handlers.py) gives the same two tags explicit VRs on
            # the way out, because they are private and `dictionary_VR`
            # raises for them. Removing either half alone leaves the
            # exporter carefully preserving a tag the sweep now strips.
            WHITELIST_TAGS = {"0099,0010", "0099,1001"}

            for item, tag, path in scan_targets:
                try:
                    group_str, _ = tag.split(',')
                    group = int(group_str, 16)
                    if group % 2 != 0:  # Odd group = Private
                        if tag not in WHITELIST_TAGS:
                            findings.append(PhiFinding(
                                entity_uid=instance.sop_instance_uid,
                                entity_type="Instance",
                                field_name=f"Private Tag {tag}",
                                value="<PRIVATE>",
                                reason="Private Tag Removal Requested",
                                tag=tag,
                                patient_id=patient_id,
                                entity=item,
                                entity_path=path,
                                remediation_proposal=PhiRemediation(
                                    action_type="REMOVE_TAG",
                                    target_attr=tag
                                )
                            ))
                except ValueError:
                    pass  # Malformed tag?

            # A private *sequence* is a private tag. `scan_targets` is
            # built from `attributes` alone, so the sweep has to ask the
            # question of `sequences` directly, at every depth, or the
            # default configuration exports private sequences.
            for owner, path in iter_item_tree(instance):
                for seq_tag in list(owner.sequences.keys()):
                    try:
                        if int(seq_tag.split(',')[0], 16) % 2 == 0:
                            continue
                    except ValueError:
                        continue
                    if seq_tag in WHITELIST_TAGS:
                        continue
                    seq_removals.append(PhiFinding(
                        entity_uid=instance.sop_instance_uid,
                        entity_type="Instance",
                        field_name=f"Private Sequence {seq_tag}",
                        value="<PRIVATE>",
                        reason="Private Tag Removal Requested",
                        tag=seq_tag,
                        patient_id=patient_id,
                        entity=owner,
                        entity_path=path,
                        remediation_proposal=PhiRemediation(
                            action_type="REMOVE_TAG",
                            target_attr=seq_tag)))

        # 2. Configured PHI Tags
        if not self.phi_tags:
            return findings + self._innermost_first(seq_removals)

        for item, tag, path in scan_targets:
            # Parse config
            # Through the masks: a `60xx,xxxx` rule covers
            # `6002,0022`, and a concrete key beats it.
            config_val = _rule_for(self.phi_tags, tag)
            if not config_val:
                continue

            if isinstance(config_val, dict):
                description = _rule_name(config_val)
                action_code = config_val.get("action", "REPLACE").upper()
                rule_value = config_val.get("value")
            else:
                description = str(config_val)
                action_code = "REPLACE"
                rule_value = None
            # Study Date's REPLACE with no value is the shift, at any depth:
            # the owner's rule reader says so, and the validator
            # lets it through only on that reading, since a DA cannot hold
            # `ANONYMIZED`.
            if tag == "0008,0020":
                action_code = _owned_rule(self.phi_tags, tag)[0]

            # Check if tag exists in item items
            val = item.attributes.get(tag)

            if val is None:
                continue

            # The owner's replacement is not PHI. An owner's remediation
            # writes its value onto every instance's top-level copy, and
            # without this a re-audit would raise the instance's own rule
            # against it: the instance would go IDENTIFIED, and a second
            # `anonymize()` would write a second value over the owner's.
            # Top-level only, because that is as far as the owner's write
            # and the exporter's stamp reach. Not for REMOVE, which asks
            # for the copy to be absent whatever it holds: a present copy
            # is raised, and folds into the owner's removal when the owner
            # removed it.
            if (action_code != "REMOVE" and not path
                    and self._holds_owners_replacement(tag, val, patient, study)):
                continue

            # Isocenter's own redaction note is not PHI. PS3.15
            # Table E.1-1 removes Derivation Description, and redaction
            # writes this exact value there; `export(check_burned_in=True)`
            # re-audits and skips every entity with a finding, so without
            # this no redacted instance would ever pass safe export. The
            # value, not the tag: an operator's text here is still
            # removed. Local import because `services` pulls in numpy and
            # the pixel stack, and this runs only when the tag is present.
            if tag == "0008,2111":
                from .services import (  # pylint: disable=import-outside-toplevel
                    _REDACTION_DERIVATION_DESCRIPTION)
                if val == _REDACTION_DERIVATION_DESCRIPTION:
                    continue

            # Determine if remediation is needed
            needs_remediation = False
            remediation_action = "REPLACE_TAG"
            new_val = None
            uid_replacement = False

            if action_code == "REMOVE":
                # If user wants it gone, and it exists (val is not None), finding!
                needs_remediation = True
                remediation_action = "REMOVE_TAG"
            elif action_code == "EMPTY":
                # Zero-length bytes are empty too: a binary element EMPTY
                # wrote reads back as `b""`, from the file and from the
                # store, and `b"" != ""` would raise it again.
                if val != "" and val != b"":
                    needs_remediation = True
                    remediation_action = "REPLACE_TAG"
                    new_val = ""
            elif action_code in ["SHIFT", "JITTER"]:
                # Date shifting, decided **per value**. Do not read
                # `instance.date_shifted` or `study.date_shifted` here:
                # each says a shift landed somewhere on an entity, not on
                # this value, so it would skip a date the pipeline never
                # touched (a rule first named in a later pass, or a value
                # left out of `anonymize(findings=[...])`) and re-shift a
                # date inside a sequence, which has no flag, on every pass.
                # The legacy branch below carries the only persisted
                # evidence that a shift ever ran without a record.
                if item.date_shift_vouches_for(tag, val):
                    # This item shifted this tag to this value, and the
                    # tag still holds it. Shifting again would move it
                    # twice.
                    needs_remediation = False
                elif not str(val).strip():
                    # A blank value is not a `SHIFT`/`JITTER` finding at
                    # all. `val` cannot be `None` here -- the walk above
                    # skips a tag the item does not hold -- so this tests
                    # blank, not absent, as `EMPTY` and `REPLACE` skip
                    # blank. An empty value is not retained PHI: there is
                    # nothing to shift, which is why the arm writes no
                    # decline row for it. Spelled as the arm spells it
                    # (`str(...).strip()`), so a multi-valued element is
                    # not mistaken for a blank one. Without this, every
                    # pass would raise a finding the arm declines to act
                    # on, and the pass-end demotion would leave a clean
                    # instance IDENTIFIED. This branch is *above* the
                    # legacy one, so `_date_shift_declines`' own blank
                    # guard is not reachable from here; it is kept because
                    # the predicate is also read directly and must answer
                    # the same way alone as it does in place.
                    needs_remediation = False
                elif getattr(instance, "_legacy_shift_provenance", False):
                    # A store with no per-value shift records for the
                    # dates it already holds: this instance keeps the
                    # entity-level rule for them, permanently, because
                    # reading "no record" as "not shifted" would shift
                    # every already-shifted date a second time. The load
                    # says so once, as a WARNING audit row.
                    #
                    # A value the arm could not parse is raised again so
                    # its decline recurs and the pass-end demotion keeps
                    # the instance IDENTIFIED, while a value the shift
                    # could apply to is skipped because it has moved once
                    # already. The import is local because remediation
                    # imports this module.
                    from .remediation import (  # pylint: disable=import-outside-toplevel
                        _date_shift_declines)
                    needs_remediation = _date_shift_declines(val)
                else:
                    needs_remediation = True
                if needs_remediation:
                    remediation_action = "SHIFT_DATE"
            elif action_code == "KEEP":
                needs_remediation = False
            else:  # REPLACE (Default)
                # The rule's `value:`, or the dummy of the tag's
                # dictionary VR, or `ANONYMIZED` for a private or
                # unknown tag. The loader judges the same spelling
                # (`_refused_phi_rule`). Computed in this arm alone: Study
                # Date's value-less REPLACE was read as SHIFT above and
                # must never get the DA dummy. "Already replaced" compares
                # with the value this rule writes, so a dummied element
                # reads clear on a re-audit, and a copy left at another
                # value by an earlier policy is rewritten. A blank value,
                # text or binary (`b""`), carries nothing and is not
                # given one.
                #
                # On a UI with no `value:` it is the keyed UID replacement
                # instead: each value replaced by the UID derived from
                # it, at any depth, so every reference to one UID gets
                # one replacement. "Already replaced" is a UID this
                # project minted (`_uid_is_minted`), not agreement with
                # anything: a copy of an owner's source UID is raised
                # like any other.
                if rule_value is None and _is_uid_replacement(config_val, tag):
                    new_val = _replaced_uids(val, self.project_secret)
                    needs_remediation = uid_replacement = new_val is not None
                else:
                    replace_value = rule_value or _vr_dummy(tag) or "ANONYMIZED"
                    if val != replace_value and val != "" and val != b"":
                        needs_remediation = True
                        remediation_action = "REPLACE_TAG"
                        new_val = replace_value

            if needs_remediation:
                proposal = PhiRemediation(
                    action_type=remediation_action,
                    target_attr=tag,
                    new_value=new_val,
                    original_value=val,
                    # The scheme rides with the id, because the arm that
                    # computes the offset sees only the finding: a
                    # legacy patient's date shifted with a keyed offset
                    # would carry two offsets. No patient (a direct call)
                    # is a keyed patient, as every new patient is.
                    metadata={
                        "patient_id": patient_id,
                        "jitter_scheme": getattr(
                            patient, "_jitter_scheme", JITTER_SCHEME_KEYED),
                    } if remediation_action == "SHIFT_DATE"
                    else {UID_REPLACEMENT: True} if uid_replacement else {})

                findings.append(PhiFinding(
                    entity_uid=instance.sop_instance_uid,
                    entity_type="Instance",
                    field_name=f"{description} (Deep)" if item != instance else description,
                    value=val,
                    reason=f"Matched PHI Tag {tag} ({description})",
                    tag=tag,
                    patient_id=patient_id,
                    entity=item,  # Point to the specific deep item!
                    entity_path=path,
                    remediation_proposal=proposal
                ))

        # 3. Configured rules on sequence tags. The loop above reads
        # `attributes` alone, so without this a `REMOVE` or `EMPTY` on a
        # sequence would raise nothing and the sequence would be exported
        # with its items. Container findings, so into `seq_removals` with
        # the private ones and for their reason -- remediated after
        # anything raised inside them.
        swept = {(id(f.entity), f.tag) for f in seq_removals}
        for owner, path in iter_item_tree(instance):
            for seq_tag in list(owner.sequences.keys()):
                # One lookup path with the loop above, though no
                # 50xx or 60xx element is a sequence.
                config_val = _rule_for(self.phi_tags, seq_tag)
                # The private sweep already raised this one.
                if not config_val or (id(owner), seq_tag) in swept:
                    continue
                if isinstance(config_val, dict):
                    description = _rule_name(config_val)
                    action_code = config_val.get("action", "REPLACE").upper()
                else:
                    description = str(config_val)
                    action_code = "REPLACE"

                if action_code == "REMOVE":
                    proposal = PhiRemediation(action_type="REMOVE_TAG",
                                              target_attr=seq_tag)
                elif action_code == "EMPTY":
                    # Zero items is already empty, as `val != ""` is for
                    # an attribute: a re-audit must read it clear.
                    if not owner.sequences[seq_tag].items:
                        continue
                    proposal = PhiRemediation(action_type="REPLACE_TAG",
                                              target_attr=seq_tag,
                                              new_value="")
                else:
                    if (action_code != "KEEP" and (seq_tag, action_code)
                            not in self._sequence_actions_warned):
                        self._sequence_actions_warned.add((seq_tag, action_code))
                        get_logger().warning(
                            f"Rule {action_code} on {seq_tag} ({description}) "
                            "has no meaning on a sequence and is not applied; "
                            "use REMOVE, EMPTY or KEEP")
                    continue

                seq_removals.append(PhiFinding(
                    entity_uid=instance.sop_instance_uid,
                    entity_type="Instance",
                    field_name=(f"{description} (Deep)" if owner is not instance
                                else description),
                    value="<SEQUENCE>",
                    reason=f"Matched PHI Tag {seq_tag} ({description})",
                    tag=seq_tag,
                    patient_id=patient_id,
                    entity=owner,
                    entity_path=path,
                    remediation_proposal=proposal))

        return findings + self._innermost_first(seq_removals)

    @staticmethod
    def _innermost_first(seq_removals: List[PhiFinding]) -> List[PhiFinding]:
        """The container findings, deepest first.

        Stable, so sequences at equal depth keep the walk's order.

        Args:
            seq_removals (List[PhiFinding]): The sequence findings.

        Returns:
            List[PhiFinding]: Sorted by `entity_path` length, longest
            first.
        """
        # A sequence can hold another one, and `iter_item_tree` yields the
        # container before the thing inside it. In that order remediation
        # would delete the outer sequence, and the inner finding -- whose
        # `entity` was resolved before either ran -- would then delete from
        # a dict no longer reachable from the instance and file a
        # `REMEDIATION_REMOVE` row for it. The export is right either way;
        # the audit trail is not, and "every row describes an item that was
        # still in the graph" is the claim this ordering keeps. Applied on
        # every return, not only when `remove_private_tags` is on: a
        # configured sequence inside a configured sequence needs the same
        # order with the sweep off.
        return sorted(seq_removals, key=lambda f: len(f.entity_path),
                      reverse=True)

    def _holds_owners_replacement(self, tag: str, value: Any, patient: Patient,
                                  study: Study) -> bool:
        """Whether `value`, an instance's top-level copy of `tag`, is the
        replacement its owner already holds.

        The copy must equal the owner's value, and the owner's value must
        pass `_holds_owned_replacement`, the test `scan_patient` and
        `_scan_study` stop raising on. No owner, no skip. A Patient ID copy
        under a subject with no Patient ID is always skipped: the export
        stamps that ID empty.

        Args:
            tag (str): The tag of the copy.
            value: The copy's value.
            patient (Patient): The owning patient, or None.
            study (Study): The owning study, or None.

        Returns:
            bool: Whether to skip the copy.
        """
        # Agreement alone is not enough: before anything is anonymized
        # every copy equals its owner's *original*, and that is PHI.
        if tag == "0008,0020":
            if study is None:
                return False
            # Lazy: io_handlers is the heavy module, and this is the one
            # spelling of "a Study's date as a DA string" -- the spelling
            # the owner's write put on the copy.
            from .io_handlers import format_study_date
            return (value == format_study_date(study.study_date)
                    and _holds_owned_replacement(self.phi_tags, tag,
                                                 study.study_date, study=study))
        if patient is None:
            return False
        if tag == "0010,0010":
            return (value == patient.patient_name
                    and _holds_owned_replacement(self.phi_tags, tag, value))
        if tag == "0010,0020":
            # A subject with no Patient ID: the export stamps its
            # ID empty whatever the copy holds, and its key is never
            # replaced, so a finding here could only write a pseudonym
            # onto a copy no file carries. Not dead: the copy is usually
            # absent or `''`, which raise nothing, but pydicom keeps a
            # blank ID's leading whitespace (" \t " reads " \t"), which
            # ingest counts as no ID and the scan would raise.
            if is_synthetic_patient_id(patient.patient_id):
                return True
            return (value == patient.patient_id
                    and _holds_owned_replacement(self.phi_tags, tag, value))
        # No StudyTime (0008,0030) arm, though `ENTITY_FIELD_TAGS` carries
        # one. The skip needs a value *known* to be a replacement, and a
        # time has no such test: no `date_shifted` flag, no `ANON_`
        # prefix, and no shipped scan remediates `Study.study_time`. An
        # arm could only skip on agreement, and agreement with an original
        # is PHI -- the case this function exists to refuse.
        return False

    def _scan_owned_uid(self, entity, tag: str, attr: str, entity_type: str,
                        patient_id: str) -> List[PhiFinding]:
        """The owner's own UID under the keyed UID replacement.

        Raised against the entity itself, and only for the value-less
        REPLACE; a UID this project minted is not raised. The remediation
        writes the new UID onto each instance's top-level copy.

        Args:
            entity: The Study or Series.
            tag (str): `0020,000d` or `0020,000e`.
            attr (str): The entity's UID attribute.
            entity_type (str): `"Study"` or `"Series"`.
            patient_id (str): Recorded on the finding.

        Returns:
            List[PhiFinding]: One finding, or none.

        Raises:
            RuntimeError: When the rule is the UID replacement, the UID is
                not blank, and the inspector has no project secret.
        """
        # The exporter stamps each file's copy from the entity
        # (`export_stamp_attributes`), so the entity is what has to move. A
        # `REPLACE value:` leaves the owner alone, since one literal written
        # into every Study would merge them under the store's UNIQUE key.
        rule = _rule_for(self.phi_tags, tag)
        uid = getattr(entity, attr, None)
        if not _owned_uid_is_open(self.phi_tags, tag, uid, self.project_secret):
            return []
        name = _rule_name(rule) if isinstance(rule, dict) else str(rule)
        return [PhiFinding(
            entity_uid=uid, entity_type=entity_type, field_name=attr, value=uid,
            reason=f"Matched PHI Tag {tag} ({name})", tag=tag,
            patient_id=patient_id, entity=entity,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr=attr,
                new_value=_replacement_uid_for(uid, self.project_secret),
                original_value=uid, metadata={UID_REPLACEMENT: True}))]

    def _scan_series(self, series, patient_id: str = None) -> List[PhiFinding]:
        """A Series' own PHI: its Series Instance UID.

        Args:
            series: The Series.
            patient_id (str, optional): Recorded on the finding.

        Returns:
            List[PhiFinding]: `_scan_owned_uid`'s findings.
        """
        return self._scan_owned_uid(series, "0020,000e", "series_instance_uid",
                                    "Series", patient_id)

    def _scan_study(self, study: Study, patient_id: str = None,
                    jitter_scheme: str = JITTER_SCHEME_KEYED) -> List[PhiFinding]:
        """Scan a Study for study-level PHI: its Study Instance UID and its
        Study Date.

        Args:
            study (Study): The study.
            patient_id (str, optional): Recorded on each finding.
            jitter_scheme (str): The patient's scheme, carried on a shift.

        Returns:
            List[PhiFinding]: The findings.
        """
        return (self._scan_owned_uid(study, "0020,000d", "study_instance_uid",
                                     "Study", patient_id)
                + self._scan_study_date(study, patient_id, jitter_scheme))

    def _scan_study_date(self, study: Study, patient_id: str = None,
                         jitter_scheme: str = JITTER_SCHEME_KEYED) -> List[PhiFinding]:
        """The Study Date arm of `_scan_study`, under the rule on
        `0008,0020` (`_owned_rule`).

        Under SHIFT a date this pipeline's shift produced
        (`_study_date_is_this_pipelines`) is not raised again; anything
        else is.

        Args:
            study (Study): The study.
            patient_id (str, optional): Recorded on the finding.
            jitter_scheme (str): The patient's scheme, carried on a shift.

        Returns:
            List[PhiFinding]: One finding, or none.
        """
        findings = []
        uid = study.study_instance_uid

        # The rule on Study Date governs the study's own date.
        action, value = _owned_rule(self.phi_tags, "0008,0020")
        proposal = None
        if action == "KEEP":
            return findings
        if action == "REMOVE":
            if study.study_date is not None:
                proposal = PhiRemediation(action_type="REMOVE_TAG",
                                          target_attr="study_date",
                                          original_value=study.study_date)
        elif action == "EMPTY":
            if study.study_date:
                proposal = PhiRemediation(action_type="REPLACE_TAG",
                                          target_attr="study_date", new_value="",
                                          original_value=study.study_date)
        elif action == "REPLACE":
            # A study with no date, or an empty one, is not given one, as
            # the instance arm's REPLACE skips `""`.
            if study.study_date and not _holds_owned_replacement(
                    self.phi_tags, "0008,0020", study.study_date, study=study):
                proposal = PhiRemediation(action_type="REPLACE_TAG",
                                          target_attr="study_date", new_value=value,
                                          original_value=study.study_date)
        if action != "SHIFT":
            if proposal is not None:
                findings.append(PhiFinding(
                    entity_uid=uid, entity_type="Study", field_name="study_date",
                    value=study.study_date,
                    reason="Dates are Safe Harbor restricted", tag="0008,0020",
                    patient_id=patient_id, entity=study,
                    remediation_proposal=proposal))
            return findings

        # SHIFT. Below the other actions, not above them: a date shifted in
        # an earlier pass and now under EMPTY or REPLACE must still be
        # raised, and this early return would skip it.
        #
        # A date this pipeline's own shift produced is not raised again;
        # anything else under `study_date` is. Not `study.date_shifted`:
        # the flag records *that* a shift happened, never *what it
        # produced*, so a fresh original assigned to `study.study_date`
        # after a shift would never be raised and would be exported.
        if _study_date_is_this_pipelines(study):
            return findings

        if study.study_date:
            proposal = PhiRemediation(
                action_type="SHIFT_DATE",  # Special action for the Service to handle
                target_attr="study_date",
                original_value=study.study_date,
                metadata={"patient_id": patient_id,
                          "jitter_scheme": jitter_scheme}
            )
            findings.append(PhiFinding(
                entity_uid=uid,
                entity_type="Study",
                field_name="study_date",
                value=study.study_date,
                reason="Dates are Safe Harbor restricted",
                tag="0008,0020",
                patient_id=patient_id,
                entity=study,
                remediation_proposal=proposal
            ))

        return findings
