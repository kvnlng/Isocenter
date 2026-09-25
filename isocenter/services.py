"""
Services for Isocenter.

This module contains the core service logic for:
- MachinePixelIndex: Fast retrieval of instances by device serial number.
- RedactionService: Handling the application of redaction rules to pixel data.
"""

import hashlib
import json
import traceback
import gc
from dataclasses import dataclass
from typing import Dict, List, Optional
import numpy as np

from .entities import (Instance, DicomItem, DicomSequence, PhiStatus,
                       SOURCE_SOP_UID_ATTR, _SET_PIXEL_DATA_TAGS,
                       iter_item_tree)
from .pixel_geometry import PixelGeometry, resolve_pixel_geometry
from .store import DicomStore
from .logger import describe_exception, get_logger
from .parallel import progress_bar


class MachinePixelIndex:
    """
    Inverted index allowing O(1) retrieval of Instances by Device Serial Number.

    This optimization struct maps serial numbers to lists of Instance objects,
    preventing full-store scans for every redaction rule.
    """

    def __init__(self):
        self._index: Dict[str, List[Instance]] = {}

    def index_store(self, store: DicomStore):
        """
        Indexes all instances in the given store.

        Iterates through the entire hierarchy and populates the internal map.

        Args:
            store (DicomStore): The store to index.
        """
        self._index.clear()
        for p in store.patients:
            for st in p.studies:
                for se in st.series:
                    if se.equipment and se.equipment.device_serial_number:
                        sn = se.equipment.device_serial_number
                        if sn not in self._index:
                            self._index[sn] = []
                        self._index[sn].extend(se.instances)

    def get_by_machine(self, sn):
        """The instances of every series whose Device Serial Number is `sn`.

        Args:
            sn (str): The Device Serial Number.

        Returns:
            List[Instance]: The indexed instances, or an empty list.
        """
        return self._index.get(sn, [])


@dataclass
class RedactionOutcome:
    """What one worker has to tell the parent about one instance.

    Attributes:
        ok (bool): False when the worker raised anywhere (a zone that
            could not be applied, a failed pixel load, a failed persist),
            which leaves burned-in PHI in the instance and must be
            reported.
        sop_instance_uid (str): The instance's **pre-redaction** UID,
            which the parent's map is keyed on.
        mutation (Optional[dict]): What to apply to the parent's instance,
            present only when a zone landed; None with `ok=True` is a
            legitimate skip.
        error (Optional[str]): The failure, as text, when `ok` is False.
    """
    # `error` is prose, not a `BaseException` (unlike `ExportOutcome.error`):
    # an exception whose `__init__` does not round-trip through `pickle`
    # would reach the parent as a pickling error about the *result* rather
    # than the failure it reports.
    ok: bool
    sop_instance_uid: str
    mutation: Optional[dict] = None
    error: Optional[str] = None


class RedactionError(RuntimeError):
    """Redaction did not remove what it was asked to remove.

    Raised after the whole pass, not at the first failure: the instances
    that could be redacted are redacted, and the failures are already in
    the audit log, so a caller that catches this still gets a compliance
    report that grades REVIEW_REQUIRED.

    A `RuntimeError` subclass, so `except RuntimeError` catches it; its
    own class tells it from the export path's bare `RuntimeError`s, which
    mean nothing was written, where this means something unsafe is still
    in the graph.

    Args:
        failures: `(entity_uid, details)` pairs, kept as `self.failures`.
        attempted (int): How many instances the pass targeted, kept as
            `self.attempted`.
    """
    # Must stay a `RuntimeError` subclass: subclassing `Exception` directly
    # would let every existing `except RuntimeError` miss it.

    def __init__(self, failures, attempted):
        self.failures = list(failures)   # [(entity_uid, details)]
        self.attempted = attempted
        first = self.failures[0] if self.failures else ("UNKNOWN", "unknown")
        super().__init__(
            f"Redaction failed for {len(self.failures)} of {attempted} "
            "instances; their pixel data still carries whatever the "
            f"configured zones were meant to remove. First: {first[0]}: "
            f"{first[1]}. See the audit log for the rest.")


def _report_redaction_failures(failures, store_backend=None):
    """Log every zone that could not be applied, and audit it if we can.

    Each failure is logged at ERROR and, when there is a store backend,
    written as an `ERROR` audit row, which takes the compliance report to
    REVIEW_REQUIRED. Both redaction paths call this. The detail is
    flattened to one line with its pipes escaped, and not truncated.

    Args:
        failures: `(entity_uid, detail)` pairs.
        store_backend (optional): The store to audit to; None logs only.

    Returns:
        List[Tuple[str, str]]: `(entity_uid, details)` per failure, flattened.
    """
    # `ERROR`, not `DATA_LOSS`: nothing was dropped -- the burned-in
    # identifier is present, which is a failed operation, as
    # `DicomExporter._report_export_failures` records it. And a `DATA_LOSS`
    # row is graded by `loss_scope`, where `STANDARD` leaves the run at PASS
    # and `PRIVATE` would describe a tag that does not exist.
    #
    # The log is not gated on the backend: `RedactionService(store)` with no
    # backend is supported and would otherwise lose the failure entirely.
    # The detail is flattened and pipe-escaped because the report renders
    # it straight into a markdown table row.
    logger = get_logger()
    reported = []
    for uid, detail in failures:
        detail = " ".join(str(detail).split()).replace("|", "\\|")
        logger.error("%s: %s", uid, detail)
        reported.append((uid, detail))
        if store_backend is not None:
            # `log_audit`, not `log_audit_batch` -- the batch method writes
            # straight to the database while the audit writer thread is
            # live and swallows `sqlite3.Error` into a log line, so
            # contention would lose the very entry that exists because a
            # log line was not enough.
            store_backend.log_audit(action_type="ERROR", entity_uid=uid,
                                    details=detail)
    return reported


def _redacted_uid_for(inst, config_hash, secret) -> str:
    """The SOP Instance UID `inst` takes when redacted under `config_hash`.

    Derived from the UID it was **ingested** under -- the source recorded
    by an earlier UID replacement or redaction, else its own -- so
    redacting before or after `anonymize()` gives one UID, and a
    re-redaction under other zones another.

    Args:
        inst (Instance): The instance to be redacted.
        config_hash (str): The redaction configuration's hash.
        secret (bytes): The project secret.

    Returns:
        str: The derived SOP Instance UID.
    """
    # Always computed in the parent: a worker is handed the result, never
    # the secret.
    from .privacy import _redaction_uid_for  # pylint: disable=import-outside-toplevel
    source = inst.attributes.get(SOURCE_SOP_UID_ATTR) or inst.sop_instance_uid
    return _redaction_uid_for(source, config_hash, secret)


#: What `_apply_redaction_flags` writes, spelled once. The status-carry guard
#: (`_flags_are_redactions`) accepts each of these tags at the value
#: captured before the pass or at exactly this value, and nothing else.
#: A second spelling of any of them would let an edit to the flags
#: quietly widen what redaction is allowed to carry.
_REDACTION_FLAG_TAGS = ("0008,0008", "0028,0301", "0008,2111")
_REDACTION_FLAG_SEQUENCE = "0008,9215"        # Derivation Code Sequence
_REDACTION_BURNED_IN = "NO"
_REDACTION_DERIVATION_DESCRIPTION = "Isocenter Pixel Redaction: Burned-in PHI removed"
#: Code 113062, DCM: Pixel Data modification.
_REDACTION_DERIVATION_CODE = (("0008,0100", "113062"), ("0008,0102", "DCM"),
                              ("0008,0104", "Pixel Data modification"))
#: `_apply_redaction_flags` replaces the whole sequence with this one item.
_REDACTION_DERIVATION_ITEM = (tuple(sorted(
    (tag, repr(value)) for tag, value in _REDACTION_DERIVATION_CODE)), ())
_NO_VALUE = ("absent",)   # a flag tag the instance does not carry


def _derived_image_type(current) -> list:
    """The ImageType redaction writes over `current`: DERIVED first,
    ORIGINAL gone, and SECONDARY as Value 2 where nothing else fills it.

    Args:
        current: The ImageType as it stands: a list, a single string, or
            None.

    Returns:
        list: The new ImageType values.
    """
    if isinstance(current, str):
        current = [current]
    derived = ["DERIVED"] + [x for x in (current or [])
                            if x not in ("ORIGINAL", "DERIVED")]
    if len(derived) < 2:
        derived.append("SECONDARY")
    return derived


#: Bookkeeping pixel redaction writes to an instance itself, and so may
#: change without invalidating the instance's tag-scan conclusion:
#: `regenerate_uid()` writes the
#: SOP Instance UID and records the one it replaced; the attestation
#: hash; and `set_pixel_data()` the descriptors in `_SET_PIXEL_DATA_TAGS`.
#: The flags redaction writes (`_REDACTION_FLAG_TAGS`, the Derivation
#: Code Sequence) are deliberately not here: a caller can author a value
#: in any of them, so they are compared to what redaction writes rather
#: than skipped. **A tag added here is a tag an edit to which, by anyone,
#: during a pass, will be carried past the revision rule** -- so add one
#: only for a value no caller authors and that identifies no one.
_REDACTION_OWN_ATTRS = frozenset({
    "0008,0018",                    # SOPInstanceUID, from regenerate_uid()
    SOURCE_SOP_UID_ATTR,            # the UID regenerate_uid() replaced
    "_ISOCENTER_REDACTION_HASH",    # the attestation
    *_SET_PIXEL_DATA_TAGS,
})
#: Left out of the fingerprint, and checked by `_flags_are_redactions`.
_REDACTION_SKIPPED_ATTRS = _REDACTION_OWN_ATTRS | frozenset(_REDACTION_FLAG_TAGS)
_REDACTION_OWN_SEQUENCES = frozenset({_REDACTION_FLAG_SEQUENCE})

#: Only an assurance is worth carrying. UNSCANNED has nothing to carry, and
#: IDENTIFIED is not an assurance; both are left to the revision rule.
_CARRIED_STATUSES = (PhiStatus.REMEDIATED, PhiStatus.CLEARED)


def _metadata_outside_redaction(inst: Instance) -> tuple:
    """Everything a tag scan could conclude from, minus redaction's own writes.

    Covers the whole item tree, including each item's sequence keys, so
    an edit anywhere -- an emptied or added sequence included -- compares
    unequal.

    Args:
        inst (Instance): The instance to fingerprint.

    Returns:
        tuple: A comparable fingerprint; only equality is meaningful.
    """
    # The whole tree, not the top level: an edit inside a nested item is as
    # much an edit the scan has not seen as one at the top. `repr` because
    # values include lists, and comparison is all this is for.
    def flat(item, skip_attrs=frozenset(), skip_seqs=frozenset()):
        """One item's attributes and sequence keys, minus the skipped ones.

        Args:
            item (DicomItem): The item.
            skip_attrs (frozenset): Attribute tags to leave out.
            skip_seqs (frozenset): Sequence tags to leave out.

        Returns:
            tuple: `(sorted (tag, repr(value)) pairs, sorted sequence tags)`.
        """
        return (tuple(sorted((tag, repr(value))
                             for tag, value in item.attributes.items()
                             if tag not in skip_attrs)),
                tuple(sorted(tag for tag in item.sequences
                             if tag not in skip_seqs)))

    nested = []
    for tag in sorted(inst.sequences):
        if tag in _REDACTION_OWN_SEQUENCES:
            continue
        for index, item in enumerate(inst.sequences[tag].items):
            for sub, path in iter_item_tree(item, ((tag, index),)):
                nested.append((path, flat(sub)))
    return (flat(inst, _REDACTION_SKIPPED_ATTRS, _REDACTION_OWN_SEQUENCES),
            tuple(nested))


def _redaction_flags(inst: Instance) -> tuple:
    """The flag tags and the Derivation Code Sequence as they stand.

    Args:
        inst (Instance): The instance to read.

    Returns:
        tuple: `(flag values, sequence items or None)`, comparable by
        `_flags_are_redactions`.
    """
    seq = inst.sequences.get(_REDACTION_FLAG_SEQUENCE)
    items = None if seq is None else tuple(
        (tuple(sorted((tag, repr(value)) for tag, value in item.attributes.items())),
         tuple(sorted(item.sequences)))
        for item in seq.items)
    return (tuple(inst.attributes.get(tag, _NO_VALUE) for tag in _REDACTION_FLAG_TAGS),
            items)


def _flags_are_redactions(before: tuple, after: tuple) -> bool:
    """Each flag is as captured, or exactly what redaction writes over the
    captured value; the sequence is as captured, or exactly the one item.

    Args:
        before (tuple): `_redaction_flags` before the pass.
        after (tuple): `_redaction_flags` after it.

    Returns:
        bool: True when only redaction (or nothing) changed the flags;
        False for any other edit, such as a caller's text in
        DerivationDescription or a second Derivation Code Sequence item.
    """
    # "As captured" is accepted too, because a hash-match skip and a failed
    # instance leave the captured values in place, and neither is an edit.
    (before_values, before_items), (after_values, after_items) = before, after
    image_type = None if before_values[0] is _NO_VALUE else before_values[0]
    expected = (_derived_image_type(image_type), _REDACTION_BURNED_IN,
                _REDACTION_DERIVATION_DESCRIPTION)
    for was, now, wanted in zip(before_values, after_values, expected):
        if now not in (was, wanted):
            return False
    return after_items in (before_items, (_REDACTION_DERIVATION_ITEM,))


def capture_phi_status_for_redaction(inst: Instance) -> Optional[tuple]:
    """What to carry across a redaction pass, read before the pass.

    **Call it before dispatch**: under threads the worker writes to the
    live instance, so a status read when the outcome lands is already
    UNSCANNED. Hand the result to `carry_phi_status_across_redaction`
    after the instance is redacted.

    Args:
        inst (Instance): The instance about to be redacted.

    Returns:
        Optional[tuple]: `(status, metadata, flags)` when the instance's
        status is REMEDIATED or CLEARED at its current revision, else None.
    """
    # Redaction's own writes advance the revision, so without the capture
    # and the carry every redacted instance would read UNSCANNED after the
    # pass, and an anonymize -> redact -> export run would report its
    # redacted instances as not anonymized.
    status = inst.phi_status
    if status not in _CARRIED_STATUSES:
        return None
    return status, _metadata_outside_redaction(inst), _redaction_flags(inst)


def carry_phi_status_across_redaction(inst: Instance, captured) -> bool:
    """Re-record a captured status if only redaction touched the instance.

    Records the status only when every attribute and nested item outside
    redaction's own bookkeeping is exactly as captured, and every flag
    redaction writes is as captured or exactly what redaction writes.
    After any other change -- another tag set during the pass, top-level
    or nested, or a caller's value in a flag -- nothing is recorded and
    the instance stays UNSCANNED.

    Args:
        inst (Instance): The instance after redaction.
        captured (Optional[tuple]): What `capture_phi_status_for_redaction`
            returned before the pass; None records nothing.

    Returns:
        bool: Whether the status was re-recorded.
    """
    # The guard is the point. The revision rule exists so an edit nobody
    # re-scanned cannot inherit an assurance, and this is the one place
    # that overrides it.
    if captured is None:
        return False
    status, before, flags_before = captured
    if _metadata_outside_redaction(inst) != before:
        return False
    if not _flags_are_redactions(flags_before, _redaction_flags(inst)):
        return False
    inst.record_phi_status(status)
    return True


#: What a redaction's attestation writes before its pixels are persisted,
#: built from the spellings above rather than listed a second time: the
#: flags `_apply_redaction_flags` writes, and redaction's own bookkeeping
#: minus the descriptors `set_pixel_data()` writes, which the discard in
#: each arm's `finally` puts back -- that leaves the SOP Instance
#: UID and the UID it replaced, from `regenerate_uid()`, and the hash.
_ATTESTATION_ATTRS = _REDACTION_FLAG_TAGS + tuple(sorted(
    _REDACTION_OWN_ATTRS - frozenset(_SET_PIXEL_DATA_TAGS)))
_ATTESTATION_SEQUENCES = tuple(sorted(_REDACTION_OWN_SEQUENCES))
_ABSENT = object()


def _capture_attestation(inst: Instance) -> tuple:
    """The instance as it stands before the attestation is written.

    Args:
        inst (Instance): The instance about to be attested.

    Returns:
        tuple: `(sop_instance_uid, file_path, attributes, sequences)` for
        `_withdraw_attestation`, with `_ABSENT` for anything missing.
    """
    # References, not copies: `_apply_redaction_flags` replaces each value
    # and the Derivation Code Sequence whole, and `regenerate_uid()`
    # rebinds the identity, so nothing held here is mutated in place.
    return (inst.sop_instance_uid, inst.file_path,
            {tag: inst.attributes.get(tag, _ABSENT)
             for tag in _ATTESTATION_ATTRS},
            {tag: inst.sequences.get(tag, _ABSENT)
             for tag in _ATTESTATION_SEQUENCES})


def _withdraw_attestation(inst: Instance, attested_from: tuple) -> None:
    """Put back what `_capture_attestation` saw, after a failed persist.

    Restores the SOP Instance UID, `file_path`, the flag tags, the
    redaction hash and the Derivation Code Sequence, then calls
    `mark_modified()`, leaving the instance as it was found.

    Args:
        inst (Instance): The instance whose persist failed.
        attested_from (tuple): What `_capture_attestation` returned.
    """
    # Without this, an attestation would claim a redaction over pixels the
    # loader still reads unredacted, and its hash would make a retry skip
    # the instance.
    #
    # Withdrawn after a failure rather than withheld until a success:
    # `_swap_pixels_under_gate` records the new frame's blob row under the
    # UID the instance carries when it persists, so persisting before
    # `regenerate_uid()` would write the redacted frame's row under the old
    # UID while the unsaved `instances` row still names the ingest frame --
    # a store that disagrees with itself until the next save.
    #
    # Direct writes, as `Instance._restore_replaced_descriptors` makes them:
    # two of these keys are uppercase, `set_attr` lowercases, and there is
    # no remove-attribute method.
    uid, file_path, attrs, sequences = attested_from
    inst.sop_instance_uid = uid
    inst.file_path = file_path
    for tag, value in attrs.items():
        if value is _ABSENT:
            inst.attributes.pop(tag, None)
        else:
            inst.attributes[tag] = value
    for tag, value in sequences.items():
        if value is _ABSENT:
            inst.sequences.pop(tag, None)
        else:
            inst.sequences[tag] = value
    inst.mark_modified()


def rule_applies_to(rule_serial, serial) -> bool:
    """Does a redaction rule written for `rule_serial` cover a series whose
    Device Serial Number is `serial`?

    `redact()`'s target walk and the export's zones both ask this, so the
    two agree on which series a rule covers.

    Args:
        rule_serial (Optional[str]): The rule's `serial_number`: an exact
            serial, or `"*"` for every series.
        serial (Optional[str]): The series' Device Serial Number.

    Returns:
        bool: True for an exact match or `"*"`; False when `serial` is
        empty or None, whatever the rule.
    """
    # A serial-less series matches nothing because `MachinePixelIndex`
    # indexes only series with equipment and a serial: `redact()` never
    # reaches one, and the export must agree with it.
    if not serial:
        return False
    return rule_serial in ("*", serial)


def rules_matching(rules, serial) -> List[dict]:
    """Every rule that covers a series with this serial, in rule order.

    Every one, not the first: `redact()` runs each loaded rule as its own
    pass, so an exact rule and a `"*"` rule, or two rules on one serial,
    all apply to that series.

    Args:
        rules (Optional[List[dict]]): The machine rules, or None.
        serial (Optional[str]): The series' Device Serial Number.

    Returns:
        List[dict]: The rules `rule_applies_to` accepts.
    """
    # An export that took only the first match would leave the second
    # rule's zones unredacted. The session's `_redaction_zones_for` and,
    # through it, the store-wide icon gate read this.
    return [rule for rule in (rules or ())
            if rule_applies_to(rule.get("serial_number"), serial)]


def zone_rois(zones, on_invalid=None) -> List[tuple]:
    """The ROIs a rule's `redaction_zones` names, each as a 4-tuple.

    Two shapes are accepted: a bare `[y1, y2, x1, x2]`, and
    `{"roi": [y1, y2, x1, x2], ...}`, the shape the shipped knowledge base
    and `create_config`'s scaffolder write. A tuple zone is not accepted.

    Args:
        zones (Optional[list]): The rule's `redaction_zones`, or None.
        on_invalid (Callable, optional): Called once per rejected zone:
            with the zone's ROI when it does not hold exactly four values,
            or with None when the zone is not one of the two shapes or is
            a dict with no `roi`. A rejected zone is dropped either way.

    Returns:
        List[tuple]: Each valid ROI as a tuple of its values as given,
        with no `int()` coercion.
    """
    # No `int()` here: `prepare_redaction_tasks` hashes `sorted()` of these
    # tuples into the redaction attestation, so coercing would change every
    # attestation hash and turn an already-redacted instance back into a
    # candidate. `apply_redaction_to_array` coerces, where the pixels are
    # addressed.
    rois = []
    for zone in zones or ():
        if isinstance(zone, list):
            roi = zone
        elif isinstance(zone, dict):
            roi = zone.get("roi")
        else:
            roi = None
        if roi and len(roi) == 4:
            rois.append(tuple(roi))
        elif on_invalid is not None:
            on_invalid(roi)
    return rois


class RedactionService:
    """
    Applies pixel redaction to DICOM instances based on configuration rules.

    Handles ROI application, parallel execution (via task preparation), and
    audit logging/flagging of modified instances.

    Args:
        store (DicomStore): The graph to redact; indexed by Device Serial
            Number at construction.
        store_backend (optional): The persistence backend pixels are
            persisted to and audit rows written to; None does neither.
    """

    def __init__(self, store: DicomStore, store_backend=None):
        self.store = store
        self.index = MachinePixelIndex()
        self.index.index_store(store)
        self.logger = get_logger()
        self.store_backend = store_backend

    def scan_burned_in_annotations(self):
        """
        Scans all instances for 'Burned In Annotation' (0028,0301) == 'YES'.

        Logs warnings for any found that have NOT been remediated (i.e. Image Type
        does not contain 'DERIVED'), and writes a `RISK` audit row for each
        when there is a store backend. This is a post-process safety check.
        """
        self.logger.info("Scanning for untreated Burned In Annotations...")
        count = 0
        untreated = 0

        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        # Check Tag (case insensitive)
                        val = inst.attributes.get("0028,0301", "NO")
                        if isinstance(val, str) and "YES" in val.upper():
                            count += 1
                            # Check if we remediated it
                            img_type = inst.attributes.get("0008,0008", [])
                            if isinstance(img_type, str):
                                img_type = [img_type]

                            is_treated = any("DERIVED" in str(x).upper() for x in img_type)

                            if not is_treated:
                                untreated += 1
                                if untreated <= 5:
                                    self.logger.error(
                                        f"High Risk: Untreated Burned In Annotation in {
                                            inst.sop_instance_uid}")
                                elif untreated == 6:
                                    self.logger.error(
                                        "... (Suppressing further individual errors for Burned In Annotations) ...")

                                if self.store_backend:
                                    self.store_backend.log_audit(
                                        action_type="RISK",
                                        entity_uid=inst.sop_instance_uid,
                                        details="Burned In Annotation (0028,0301) present but not remediated.")

        if untreated > 0:
            self.logger.warning(
                f"Found {untreated} instances with potential Burned In Annotations that were NOT remediated.")
            self.logger.warning(
                f"WARNING: {untreated} instances flagged with 'Burned In Annotation' were not targeted by any rule. "
                "Action Required: Review audit logs or add rules for these instances.")
        elif count > 0:
            self.logger.info(f"Verified {count} Burned In Annotations were remediated.")

    def record_redaction_pass(self, machine_sn: str, zone_count: int,
                              targeted: int, applied: int):
        """One `REDACTION` audit row per rule-pass, spelled once for both paths.

        This is the row `generate_report`'s section 2 counts and the
        grade's `audit_summary` arm sees. Call it once per rule-pass,
        after the pass, in the parent process. Writes nothing when the
        service has no store backend.

        Args:
            machine_sn (str): The serial spelling the rule was configured
                with, `"*"` included; the row's entity.
            zone_count (int): How many zones the rule applies.
            targeted (int): How many instances the pass targeted.
            applied (int): How many it redacted.
        """
        # Per rule-pass, so the report stays bounded: per-instance rows
        # would put 10k lines in a 10k-instance session's report. After the
        # pass, so the row states an outcome, not an intent. In the parent,
        # because a worker's audit thread is torn down at pool shutdown
        # without `stop()`, so a row queued there can be lost, and for a
        # `:memory:` database the child writes nowhere at all. Both
        # redaction paths call this and nothing else writes `REDACTION`
        # rows, so they account identically for identical work.
        if not self.store_backend:
            return
        self.store_backend.log_audit(
            action_type="REDACTION",
            entity_uid=machine_sn,
            details=(f"Applied {applied} of {targeted} candidate images "
                     f"with {zone_count} zones"))

    def _targets_for(self, rule_serial) -> List[Instance]:
        """Every indexed instance a rule for `rule_serial` covers, in index order.

        Args:
            rule_serial: The rule's `serial_number`, exact or `"*"`.

        Returns:
            List[Instance]: The covered instances (`rule_applies_to`).
        """
        targets = []
        for serial in self.index._index:
            if rule_applies_to(rule_serial, serial):
                targets.extend(self.index.get_by_machine(serial))
        return targets

    def prepare_redaction_tasks(self, machine_rules: dict, verbose: bool = False,
                                force: bool = False,
                                project_secret: Optional[bytes] = None) -> List[dict]:
        """
        Generates a list of fine-grained tasks (dicts) from a single machine rule.

        Each task represents one instance to be redacted. Used for distributing
        work across parallel workers.

        Args:
            machine_rules (dict): Configuration rule containing "serial_number" and "redaction_zones".
            verbose (bool): If True, logs skips and warnings.
            force (bool): Carried into every task as `task["force"]`: redact
                an instance even when its `_ISOCENTER_REDACTION_HASH`
                already matches this configuration.
            project_secret (bytes, optional): What each task's
                `new_sop_uid` is derived under; without it, the
                store backend's. `Session.redact()` passes it.

        Returns:
            List[dict]: A list of task dictionaries ready for
                `execute_redaction_task`, each carrying its instance's
                pre-redaction and new SOP Instance UIDs; empty when the
                rule has no serial, no zones, no targets or no valid ROI.

        Raises:
            RuntimeError: When a rule has targets and there is no project
                secret to derive their UIDs under, as
                `redact_machine_instances` raises.
        """
        serial = machine_rules.get("serial_number")
        zones = machine_rules.get("redaction_zones", [])

        if not serial:
            if verbose:
                self.logger.warning("Skipping rule with missing serial number.")
            return []

        if not zones:
            if verbose:
                self.logger.info(f"Machine {serial} has no redaction zones configured. Skipping.")
            return []

        # Every indexed serial this rule covers, by the one predicate the
        # export's zones also ask. The index holds only series with
        # a serial, so this is exact or "*" over those.
        targets = self._targets_for(serial)

        if not targets:
            if verbose and serial != "*":
                self.logger.warning(
                    f"Config rule exists for {serial}, but no matching images found in Session.")
            return []

        valid_rois = zone_rois(
            zones,
            on_invalid=lambda roi: self.logger.warning(
                f"Invalid ROI format in config: {roi}"))

        if not valid_rois:
            return []

        # Compute Hash. `sorted` for the reason `redact_machine_instances`
        # gives at length: zeroing is commutative, so zone order cannot
        # change the pixels, and the sort is therefore correct rather than
        # a collision. Do not change this input.
        rois_stable = sorted(valid_rois)
        config_str = json.dumps({"serial": serial, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        # The redacted UIDs are derived here, in the parent: the secret is
        # read once and never put on a task or on the service.
        secret = self._redaction_secret(project_secret)

        # Create Tasks
        tasks = []
        for inst in targets:
            tasks.append({
                # The UID the worker gives the instance if a zone lands
                # (`Instance.regenerate_uid`), derived from its source UID
                # and this configuration, so the worker needs no secret.
                "new_sop_uid": _redacted_uid_for(inst, config_hash, secret),
                "instance": inst,
                # Captured here -- parent-side, before any worker runs --
                # and the worker reads *this*, never the live attribute.
                # Under threads every task's worker shares `inst`, and two
                # rules can target one instance (`load_config` de-dups
                # nothing): a capture taken inside the worker can follow a
                # sibling's `regenerate_uid()`, keying the mutation on a
                # post-redaction UID the parent's pre-redaction map cannot
                # match, so the redaction is silently discarded.
                # Same pattern as `config_hash` and `force`: per-task
                # state the worker must not re-derive.
                "original_sop_uid": inst.sop_instance_uid,
                "rois": valid_rois,
                "config_hash": config_hash,
                "machine_sn": serial,
                "force": force
            })

        return tasks

    def execute_redaction_task(self, task: dict):  # pylint: disable=missing-raises-doc
        # The bare `raise` below re-raises into this method's own
        # `except Exception`, which returns a failed outcome; nothing escapes.
        """
        Executes a single redaction task (one instance).

        Designed to be run in a worker thread/process. Loads pixels, applies ROIs,
        updates metadata flags, and returns a mutation structure for the main process.

        Args:
            task (dict): The task structure created by `prepare_redaction_tasks`.

        Returns:
            RedactionOutcome: `ok=True` with a mutation dict when zones were
                applied, `ok=True` with `mutation=None` for a legitimate
                skip (already redacted under this configuration, no pixel
                data, or **no configured zone landed inside the image**),
                and `ok=False` with an `error` string on any exception
                (a zone that could not be applied, a failed pixel load, a
                failed persist).

        The mutation dict exists only when a zone landed. On success the
        instance carries the redaction flags, the new SOP Instance UID and
        `_ISOCENTER_REDACTION_HASH`, and its pixels are persisted through
        the store backend; if that persist raises, the attestation is
        withdrawn and the task fails. On every path the resident pixel
        array is discarded afterwards, so a failed redaction's partly
        zeroed array is never persisted.

        The worker never raises: every failure comes back as an outcome.
        """
        # A mutation dict built for an instance no zone reached would hand
        # the parent null flag values from an instance
        # `_apply_redaction_flags` never touched, which it would write onto
        # the graph and count as updated -- so its presence is the parent's
        # signal.
        #
        # Never raising: `_apply_redaction_rules` consumes
        # `run_parallel(..., return_generator=True)` incrementally, and an
        # exception escaping a worker ends that generator mid-iteration, so
        # every mutation queued behind it would be lost and the instances
        # that *were* redacted would never reach the graph.
        inst = task["instance"]
        # From the task, never `inst.sop_instance_uid`. Under threads the
        # sibling task's worker shares this very object, and its
        # `regenerate_uid()` may already have moved the live attribute by
        # the time this worker starts -- a re-read here keys the mutation
        # on a post-redaction UID the parent's map discards.
        original_uid = task["original_sop_uid"]
        rois = task["rois"]
        config_hash = task["config_hash"]
        force = task.get("force", False)
        # Nothing in the `finally` reads a name bound inside the `try`. If
        # one ever does, pre-bind it here: an `UnboundLocalError` raised
        # *in* a `finally` replaces the return value.

        try:
            # Skip if already redacted with the same configuration.
            #
            # `force` suppresses this and nothing else. The attestation is
            # over the configuration, not over the pixels, so a store whose
            # pixels are wrong can carry a hash byte-identical to the one
            # this code would write, and the skip would then decline to
            # look at it forever; `force=True` is the lever out of it.
            current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

            if not force and current_hash == config_hash:
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            # Triggers Lazy Load from disk
            arr = inst.get_pixel_data()

            if arr is None:
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            # The whole zone list in one call, never a loop: the callee
            # rebinds `arr` locally when it copies a read-only array, so a
            # second call would get the pristine original again and only
            # the last zone would survive.
            modified = self._redact_instance_pixels(inst, arr, rois)

            if not modified:
                # No configured zone landed inside this image: a
                # legitimate skip like the two early returns above -- no
                # mutation, nothing for the parent to copy, nothing
                # counted. A dict built here would write null
                # `(0028,0301)`/`(0008,0008)`/`(0008,2111)` elements onto
                # an untouched instance and report it as updated.
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            attested_from = _capture_attestation(inst)
            self._apply_redaction_flags(inst)
            inst.regenerate_uid(task["new_sop_uid"])
            # Mark as redacted with this hash
            inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
            inst.mark_modified()

            # Persist the redacted pixels to the sidecar (a new loader).
            #
            # This is the only persist on this path, and it has to be
            # here rather than in the `finally`: the mutation dict below
            # reads `inst._pixel_loader`, and it is *this* call that
            # re-points it at the redacted frame. Drop it and the parent
            # is handed a loader for the pre-redaction pixels.
            #
            # Exactly one call: `persist_pixel_data` does not deduplicate,
            # and a second would append a second frame, leaving the
            # mutation's loader and the committed `instance_blobs` row
            # naming different frames. `tests/test_services.py` counts the
            # calls. The serial arm (`redact_machine_instances`) persists in
            # its `try` too.
            if self.store_backend and hasattr(self.store_backend, 'persist_pixel_data'):
                try:
                    self.store_backend.persist_pixel_data(inst)
                except Exception:
                    # The attestation above is over pixels that never
                    # reached the store: withdraw it, then fail the task
                    # like any other failure. Under threads `inst` is the
                    # live instance, and without this it would keep
                    # `BurnedInAnnotation NO`, the new UID and the hash, so
                    # the retry `redact()`'s error promises would skip it
                    # as already redacted.
                    _withdraw_attestation(inst, attested_from)
                    raise
            else:
                # No backend to persist to.
                pass

            # Prepare Mutated State to return (for Process Isolation)
            mutation = {
                "original_sop_uid": original_uid,  # the parent's map key
                # The rule-pass this application belongs to, for the
                # parent's REDACTION audit row. Carried on the
                # mutation rather than joined back through a UID map in
                # the parent, because one instance matched by two rules
                # produces two mutations under one pre-redaction UID --
                # a map keyed on the UID would attribute both to
                # whichever rule built it first. The key is the rule's
                # index (set by `_apply_redaction_rules`), not the
                # serial: two rules can share one serial spelling, and
                # each pass accounts for itself. `.get` because tests
                # drive this worker on bare `prepare_redaction_tasks`
                # output, which does not carry the key.
                "pass_key": task.get("pass_key"),
                # The post-redaction UID, and the parent **assigns** it
                # (`_apply_redaction_outcomes`). Reaching this line
                # means `regenerate_uid()` ran a few lines above, so this
                # is always a new identity and never the original --
                # which is why the parent gates on the mutation existing
                # rather than on the two UIDs differing. Move the
                # `regenerate_uid()` call above out of this block and
                # every instance takes a new identity for nothing.
                "sop_uid": inst.sop_instance_uid,
                "pixel_loader": inst._pixel_loader,
                "pixel_hash": getattr(inst, "_pixel_hash", None),
                # The label of the frame `pixel_loader` reads. The read
                # this worker made in order to redact relabelled *its* copy
                # wherever the decode converted -- a YBR file that pydicom
                # or the handler returns as RGB -- and under processes that
                # copy is discarded. Without this the parent would keep the
                # YBR label over the worker's RGB frame, with `file_path`
                # cleared and no file left to read again, and export would
                # write the two together. Assigned in the parent beside the
                # loader rebind (`_apply_redaction_outcomes`). Its own key
                # rather than a row in `attributes` below, which the parent
                # applies whether or not a new frame came with it. None
                # when the instance carries no label: nothing to write.
                "photometric_interpretation": inst.attributes.get("0028,0004"),
                "attributes": {
                    "0008,0008": inst.attributes.get("0008,0008"),
                    "0028,0301": inst.attributes.get("0028,0301"),
                    "0008,2111": inst.attributes.get("0008,2111"),
                    "_ISOCENTER_REDACTION_HASH": inst.attributes.get("_ISOCENTER_REDACTION_HASH"),
                },
                "sequences": {
                    k: v for k, v in inst.sequences.items() if k == "0008,9215"
                }
            }
            return RedactionOutcome(ok=True, sop_instance_uid=original_uid,
                                    mutation=mutation)

        except Exception as e:
            # The catch stays broad, and a missing-argument `TypeError` from
            # `apply_redaction_to_array` is audited here like any other
            # failure. That collision is deliberate: a malformed zone in a
            # JSON config raises `TypeError` too -- `(0, None, 0, 8)` and
            # `(0, [1], 0, 8)` both do -- so narrowing this catch to make
            # room for the programming error would drop real failed
            # redactions. Do not add traceback-frame inspection to tell
            # the two apart.
            traceback.print_exc()
            # `original_uid`, not the live attribute: this line names the
            # identity the parent's failure row carries, and a sibling
            # worker may have moved `inst.sop_instance_uid` by now.
            self.logger.error(f"  Failed {original_uid}: {describe_exception(e)}")
            return RedactionOutcome(ok=False, sop_instance_uid=original_uid,
                                    error=describe_exception(e))
        finally:
            # Memory cleanup only. No persist here: the one in the `try`
            # body is the only append this path makes, and a failed
            # redaction must not be persisted at all, so a failed instance
            # is left as it was found. `apply_redaction_to_array` raises
            # *mid-loop*, so zones 1..k-1 are already zeroed when zone k
            # fails; persisting would make that partial mutation durable on
            # the threads path while the processes path mutated a copy --
            # two different sidecars for one failure, depending on the
            # interpreter. The unconditional `discard_pixel_data()` below
            # drops the mutated array and the next `get_pixel_data()`
            # reloads the original through the loader, under the
            # descriptors it was stored with: the discard also puts back
            # whatever the copying arm's `set_pixel_data()` wrote.
            #
            # The one instance this cannot reach is one with neither a
            # loader nor a `file_path` -- a graph built in memory and never
            # reloaded. `discard_pixel_data()` refuses there, deliberately,
            # because clearing would be a silent discard, so it keeps the
            # zones applied before the failure. That is accepted: zeroing is
            # monotone, so a partial redaction has removed *more* PHI than
            # none. What matters is that it is not reported as a success and
            # carries no hash, so the next run retries it. Do not "fix" it
            # with a pre-image copy of every array -- that is exactly the
            # resident-memory cost the lazy-pixel design exists to avoid.

            # `discard_pixel_data`, not `unload_pixel_data`: dropping the
            # resident array is the INTENT here, not an optimisation. On a
            # failed redaction it is a partially-zeroed array that must go
            # so the next `get_pixel_data()` reloads the original through
            # the loader, and `unload_pixel_data()` refuses exactly that
            # case.
            inst.discard_pixel_data()

            # Explicit GC to handle large array fragmentation immediately
            gc.collect()

    def process_machine_rules(
            self,
            machine_rules: dict,
            show_progress: bool = True,
            verbose: bool = False,
            project_secret: Optional[bytes] = None):
        """
        Applies all zones defined in a single machine config object sequentially.

        The serial entry point; `Session.redact()` uses the parallel path
        (`prepare_redaction_tasks` and `execute_redaction_task`). Returns
        without redacting when the rule has no serial, no zones, no
        targets or no valid ROI; otherwise redacts through
        `redact_machine_instances`, with its side effects.

        Args:
            machine_rules (dict): The rule configuration.
            show_progress (bool): If True, shows progress bar.
            verbose (bool): If True, logs details.
            project_secret (bytes, optional): Passed to
                `redact_machine_instances`.

        Raises:
            RedactionError: Propagated from `redact_machine_instances` when
                any instance's zone could not be applied.
            RuntimeError: Propagated from `redact_machine_instances` when
                there is no project secret to derive a UID under.
        """
        serial = machine_rules.get("serial_number")
        zones = machine_rules.get("redaction_zones", [])

        if not serial:
            if verbose:
                self.logger.warning("Skipping rule with missing serial number.")
            return

        if not zones:
            if verbose:
                self.logger.info(f"Machine {serial} has no redaction zones configured. Skipping.")
            return

        targets = self._targets_for(serial)

        if not targets:
            # Only warn if not wildcard (wildcard yielding 0 means empty store, which is fine)
            if verbose and serial != "*":
                self.logger.warning(
                    f"Config rule exists for {serial}, but no matching images found in Session.")
            return

        if verbose:
            self.logger.info(
                f"Applying config rules for Machine: {serial} ({
                    len(targets)} images)...")

        valid_rois = zone_rois(
            zones,
            on_invalid=lambda roi: self.logger.warning(
                f"Invalid ROI format in config: {roi}"))

        if valid_rois:
            self.redact_machine_instances(
                serial,
                valid_rois,
                targets=targets,
                show_progress=show_progress,
                verbose=verbose,
                project_secret=project_secret)

    def _redaction_secret(self, project_secret: Optional[bytes]) -> bytes:
        """The project secret a redaction derives its UIDs under.

        Args:
            project_secret (Optional[bytes]): The secret to use, if given.

        Returns:
            bytes: `project_secret`, else the store backend's
            (`_project_secret_for_use`, which creates one on a store that
            has none).

        Raises:
            RuntimeError: With no `project_secret` and no backend, or when
                the backend's store lost the secret it was using.
        """
        # Returned to the caller and never kept on the service: under
        # processes the service is pickled to every worker with its bound
        # `execute_redaction_task`, and a worker is handed the UID, not the
        # secret. There is no random-UID fallback.
        if project_secret:
            return project_secret
        reader = getattr(self.store_backend, "_project_secret_for_use", None)
        if reader is None:
            # `_require_secret`'s refusal, so there is one wording for "no
            # secret, no unkeyed fallback".
            from .privacy import _require_secret  # pylint: disable=import-outside-toplevel
            return _require_secret(None)
        return reader(diagnose=False)

    def redact_machine_instances(  # pylint: disable=missing-raises-doc
            # The bare `raise` below re-raises into this method's own
            # `except Exception`, which records the failure; it never
            # escapes, so it is not documented.
            self,
            machine_sn: str,
            rois: List[tuple],
            targets: List[Instance] = None,
            show_progress: bool = True,
            verbose: bool = False,
            force: bool = False,
            project_secret: Optional[bytes] = None):
        """
        Applies a LIST of ROIs to all images from the specified machine.

        Iterates the images once per machine rule, applying all ROIs in a
        single pass. Each redacted instance gets the redaction flags, a new
        SOP Instance UID and `_ISOCENTER_REDACTION_HASH`, and its pixels
        are persisted through the store backend. Each instance's PHI status
        is carried across the pass when only redaction changed it
        (`carry_phi_status_across_redaction`). One `REDACTION` audit row is
        written after the pass when there were targets
        (`record_redaction_pass`), before any raise.

        Args:
            machine_sn (str): The serial number (for logging/auditing).
            rois (List[tuple]): List of (y1, y2, x1, x2) ROIs.
            targets (List[Instance], optional): Pre-filtered list of instances.
            show_progress (bool): If True, shows progress bar.
            verbose (bool): If True, logs a warning for each instance
                skipped for having no pixel data.
            force (bool): If True, re-redact an instance whose
                `_ISOCENTER_REDACTION_HASH` already matches this
                configuration. Suppresses that skip and nothing else.
                `Session.redact(force=True)` is the same lever on the
                parallel path.
            project_secret (bytes, optional): The project secret each
                redacted instance's SOP Instance UID is derived under.
                Without it, the store backend's; a service with no
                backend has to be given one.

        Raises:
            RedactionError: If any instance's zone could not be applied.
                Raised at the end of the pass, for the same reasons as
                `Session.redact()`: the instances that could be redacted
                are redacted and every failure is already an `ERROR` row.
            RuntimeError: Before any instance is touched, when there is
                no project secret to derive a UID under -- no
                `project_secret` and no store backend -- or the backend's
                store refuses one (it lost the secret its dates or UIDs
                were derived under).
        """
        # `force` stays last in the signature and defaulted: callers pass
        # the first two arguments positionally.
        if targets is None:
            targets = self.index.get_by_machine(machine_sn)
        # Before the first instance, so a refusal changes nothing.
        secret = self._redaction_secret(project_secret)

        self.logger.info(f"Redacting {len(targets)} images for {machine_sn} ({len(rois)} zones)...")

        # 1. Compute Hash for this Config
        #
        # Sorted, and the sort is *correct*, not merely stable: redaction
        # zeroes, and zeroing is commutative and idempotent, so two
        # orderings of one zone list cannot produce different pixels.
        #
        # **Do not change this input.** Every store whose config order
        # differs from its sorted order would find its attestation moved,
        # re-redact, and take a new SOP Instance UID and a new exported
        # filename on the next ordinary call -- and every other store
        # would not. That is an unannounced partial migration delivered to
        # an arbitrary subset. `force=` is the announced lever.
        rois_stable = sorted(rois)
        config_str = json.dumps({"serial": machine_sn, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        failures = []
        applied = 0

        for inst in progress_bar(
                targets,
                show=show_progress,
                desc=f"Redacting {machine_sn}",
                unit="img"):
            original_uid = inst.sop_instance_uid  # Capture before mutation
            # Before the pass touches it, as `_apply_redaction_rules`
            # does for the parallel path. `captured` is that status; the
            # attestation's own snapshot below is `attested_from`, so the
            # carry after the `finally` is never handed the wrong one.
            captured = capture_phi_status_for_redaction(inst)
            try:
                # Skip if already redacted with the same configuration.
                # `force` suppresses this and nothing else -- see
                # `execute_redaction_task`, which carries the same flag
                # through its task dict.
                current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

                if not force and current_hash == config_hash:
                    # Log at DEBUG level (requires logging configuration to show)
                    self.logger.debug(
                        f"  Skipping {
                            inst.sop_instance_uid}: Already redacted (Hash Match).")
                    continue

                # Triggers Lazy Load from disk
                arr = inst.get_pixel_data()

                if arr is None:
                    if verbose:
                        self.logger.warning(
                            f"  Skipping {
                                inst.sop_instance_uid}: No pixel data found (or file missing).")
                    continue

                # `_pixel_hash` is left alone. It names the frame the loader
                # reads, and until the swap below has written a new frame
                # that is still the original: the swap assigns the new hash
                # with the new loader, after its write. Do not clear it
                # here: after a failed persist the loader still reads the
                # old frame, and a None would reach the saved row, so a
                # reopened session would read that frame unchecked.

                # One call, the whole zone list. See the note in
                # `execute_redaction_task`: a per-zone loop would keep only
                # the last zone's work on a reloaded instance.
                modified = self._redact_instance_pixels(inst, arr, rois)

                if modified:
                    attested_from = _capture_attestation(inst)
                    self._apply_redaction_flags(inst)
                    inst.regenerate_uid(_redacted_uid_for(inst, config_hash, secret))
                    # Mark as redacted with this hash
                    inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
                    # Force Dirty to persist metadata update
                    inst.mark_modified()
                    # The persist is in the `try`, after the hash and before
                    # the count, so a raise (a full disk, an EIO) withdraws
                    # the attestation and takes the `except` below like any
                    # other failure. In a `finally` it would be logged and
                    # dropped: no failure recorded, "Applied 1 of 1" in the
                    # pass row, and an instance attesting a redaction over
                    # pixels the loader still reads unredacted.
                    #
                    # Only when a zone landed: `persist_pixel_data` does
                    # not deduplicate, so an off-image rule would grow the
                    # sidecar with a copy nothing points at. Never after a
                    # zone that failed, which raises before reaching here:
                    # the zones before it are already zeroed, and persisting
                    # them would make a partial redaction durable (the whole
                    # argument is in `execute_redaction_task`'s `finally`).
                    if (self.store_backend
                            and hasattr(self.store_backend, 'persist_pixel_data')):
                        try:
                            self.store_backend.persist_pixel_data(inst)
                        except Exception:
                            _withdraw_attestation(inst, attested_from)
                            raise
                    applied += 1
                    self.logger.debug(f"  Modified {inst.sop_instance_uid}")

            except Exception as e:
                # Broad on purpose, and a missing-argument `TypeError` is
                # audited here like any other failure -- see the note in
                # `execute_redaction_task`.
                failures.append(
                    (original_uid,
                     f"Redaction failed for {original_uid}: "
                     f"{describe_exception(e)}"))
                self.logger.error(f"  Failed {inst.sop_instance_uid}: {describe_exception(e)}")
            finally:
                # Memory cleanup only; the persist is in the `try`.
                #
                # `discard_pixel_data`, not `unload_pixel_data`: dropping the
                # resident array is the INTENT here, not an optimisation. On a
                # failed redaction it is a partially-zeroed array that must go
                # so the next `get_pixel_data()` reloads the original through
                # the loader, and `unload_pixel_data()` refuses exactly that
                # case.
                inst.discard_pixel_data()

            # After the `finally`, not inside `if modified:` -- the persist
            # and `discard_pixel_data()` above can move the revision too,
            # and a carry made before them would be undone by them. A skip
            # (`continue` above) never reaches here and needs nothing: it
            # wrote nothing, so its status never moved.
            carry_phi_status_across_redaction(inst, captured)

        # After the pass and before the raise, for the same reason the
        # ERROR rows are: a caller that catches `RedactionError` still
        # holds a report whose section 2 accounts for this pass. After, not
        # before: a row written as intent would attest passes whose every
        # instance was then skipped or failed.
        if targets:
            self.record_redaction_pass(
                machine_sn, len(rois), len(targets), applied)

        if failures:
            raise RedactionError(
                _report_redaction_failures(failures, self.store_backend),
                len(targets))

    @staticmethod
    def apply_redaction_to_array(arr: np.ndarray, rois: List[tuple],
                                 geometry: PixelGeometry) -> bool:
        """
        Applies a list of ROIs to the pixel array in place.

        A zone starting past the image edge is skipped; one extending
        past it is clipped. The array must be writeable; callers copy a
        read-only one first. A zone that cannot be applied is logged at
        ERROR and its exception re-raised, with the zones before it
        already zeroed.

        Args:
            arr (np.ndarray): The pixel array to modify.
            rois (List[tuple]): List of (y1, y2, x1, x2) regions.
            geometry (PixelGeometry): The instance's resolved geometry,
                from `isocenter.pixel_geometry`. **It must have been
                resolved from the shape of *this* array**; nothing here
                can check it.

        Returns:
            bool: True if any modification was applied.

        Raises:
            ValueError: A zone selects no pixels (`y2 <= y1` or
                `x2 <= x1`), or a value is not an integer.
            TypeError: A zone's values cannot be read as four integers.
            IndexError: A zone cannot be applied to the array.
        """
        # `geometry` is required, with no default: a guess from the array's
        # shape cannot tell a multi-frame grayscale array from a
        # single-frame colour one, and addresses the wrong axes.
        modified = False

        ndim = len(arr.shape)
        # No `ndim >= 3` guard here, and the invariant that makes that safe
        # lives in the caller: `geometry` must have been resolved from the
        # shape of *this* array, and `resolve_pixel_geometry` cannot return
        # samples > 1 for a rank-2 one. Both in-tree callers do exactly
        # that. A geometry borrowed from a different array could pair
        # samples > 1 with ndim == 2 and silently address
        # `row_dim=-1, col_dim=0`. That invariant is the only thing standing
        # here, which is why `geometry` is required rather than defaulted.
        interleaved = geometry.samples > 1

        if interleaved:
            # RGB/RGBA interleaved: (..., Rows, Cols, Channels)
            row_dim = ndim - 3
            col_dim = ndim - 2
        else:
            # Standard grayscale/planar: the last two dimensions.
            row_dim = ndim - 2
            col_dim = ndim - 1

        rows = arr.shape[row_dim]
        cols = arr.shape[col_dim]

        # Writeability is the caller's responsibility: this returns a bool,
        # so it has no way to hand a copy back. Both callers
        # (_redact_instance_pixels and the export worker) copy first. A
        # read-only array reaching here raises below rather than being
        # silently skipped.

        for roi in rois:
            try:
                r1, r2, c1, c2 = [int(v) for v in roi]

                # A zone whose *shape* is empty is a configuration error
                # on any image: `arr[r1:r2, c1:c2]` with `r2 <= r1` or
                # `c2 <= c1` selects zero pixels, the assignment below
                # would still set `modified = True`, and the instance
                # would be counted, renamed, and fully attested --
                # `BurnedInAnnotation = NO` on pixels nothing touched.
                # Judged before the off-image `continue` below, which is
                # the boundary for a *real* zone that landed elsewhere and
                # stays a legitimate skip. The raise takes the failure path
                # in every caller: ERROR rows and `RedactionError` on both
                # redact paths, a failed outcome and no file in the export
                # worker. One common source of this shape is a box in
                # x,y,w,h order; zones are (y1, y2, x1, x2).
                if r2 <= r1 or c2 <= c1:
                    raise ValueError(
                        f"redaction zone {tuple(roi)} selects no pixels: "
                        "zones are (y1, y2, x1, x2) and this one has "
                        "y2 <= y1 or x2 <= x1, so it cannot redact "
                        "anything on any image -- it used to earn a full "
                        "attestation anyway (#244)")

                # A zone starting past the edge describes nothing to redact.
                if r1 >= rows or c1 >= cols:
                    continue

                # Clipping
                r2_clamped = min(r2, rows)
                c2_clamped = min(c2, cols)

                # Construct slices dynamically
                slices = [slice(None)] * ndim
                slices[row_dim] = slice(r1, r2_clamped)
                slices[col_dim] = slice(c1, c2_clamped)

                # Apply redaction
                arr[tuple(slices)] = 0
                modified = True
            except (ValueError, IndexError, TypeError) as exc:
                # Never swallow this. A zone that fails to apply means PHI
                # is still in the pixel data, and the export worker writes
                # arr.tobytes() immediately after calling us -- so a silent
                # skip ships the unredacted image while reporting success.
                # The bool return cannot express "tried and failed": False
                # already means "no zones matched".
                #
                # Every caller must turn this raise into a recorded failure.
                # The export worker's call (`io_handlers._export_instance_worker`)
                # lets it reach the worker's outermost `except`, which
                # returns `ExportOutcome(ok=False)` and writes no file.
                # `redact_machine_instances` and `execute_redaction_task`
                # catch it one frame up, audit an ERROR row, and raise
                # `RedactionError` after the pass; a caller that only logged
                # would leave the instance in the graph for `export()` to
                # write.
                get_logger().error(
                    "Redaction zone %s could not be applied to an array of "
                    "shape %s: %s", tuple(roi), arr.shape, describe_exception(exc))
                raise

        return modified

    def _redact_instance_pixels(self, inst: Instance, arr,
                                rois: List[tuple]) -> bool:
        """
        Applies **every** ROI to one instance's pixel array, in one pass.

        **Call this at most once per instance per pass, with the full zone
        list**, and do not read your own `arr` afterwards: a read-only
        array is copied and handed to `set_pixel_data`, so the array the
        instance holds may be a different object.

        **The caller must call `inst.mark_modified()` when this returns
        True.** A read-only array's copy goes through `set_pixel_data`,
        which marks the instance modified; a writeable array is redacted
        in place and leaves `has_unsaved_changes` False:

            writeable=False  returned=True  dirty=True   zone_zeroed=True
            writeable=True   returned=True  dirty=False  zone_zeroed=True

        Args:
            inst (Instance): The instance being redacted.
            arr: Its pixel array, as `get_pixel_data()` returned it.
            rois (List[tuple]): Every (y1, y2, x1, x2) zone of the rule.

        Returns:
            bool: True if any zone was applied.

        Raises:
            ValueError: As `apply_redaction_to_array` raises it.
            TypeError: As `apply_redaction_to_array` raises it.
            IndexError: As `apply_redaction_to_array` raises it.
        """
        # A caller that called this once per zone would hand it the pristine
        # original every time (it rebinds `arr` locally on the copying arm)
        # and keep only the last zone's work -- with `modified` True, a
        # redaction hash written and a report grading PASS. There is
        # deliberately no per-zone entry point.
        #
        # Both callers -- `redact_machine_instances` and
        # `execute_redaction_task` -- call `inst.mark_modified()` under
        # `if modified:` and persist the pixels afterwards. A caller that
        # skipped it would silently drop the redacted pixels on the
        # writeable arm: zeroed in memory, the instance reporting itself
        # saved, an incremental `save_all` writing nothing, and the exported
        # file still carrying the burned-in identifiers. A test of this
        # method alone cannot catch that. Move or remove either
        # `mark_modified()` only together with this arm.
        if not arr.flags.writeable:
            arr = arr.copy()
            inst.set_pixel_data(arr)

        # The instance is in hand, so the axes are a lookup rather than a
        # guess. Resolved after the copy above, not before: `set_pixel_data`
        # can correct a descriptor, and the redaction has to address the
        # array the way the instance now describes it.
        geometry = resolve_pixel_geometry(arr.shape, inst.attributes)
        return self.apply_redaction_to_array(arr, rois, geometry=geometry)

    def _apply_redaction_flags(self, inst: Instance):
        """
        Sets DICOM tags indicating Pixel Data modification.

        Marks ImageType as DERIVED, clears BurnedInAnnotation, and adds
        DerivationCodeSequence.

        Args:
            inst (Instance): The instance to flag.
        """

        # 1. Image Type (0008,0008): the existing values are read and kept,
        # with DERIVED first and ORIGINAL dropped (`_derived_image_type`).
        # Every value here is a module constant the status-carry guard reads:
        # `_flags_are_redactions` accepts these tags only at the value
        # captured before the pass or at exactly what is written here,
        # so a value spelled a second time would be one the guard
        # refuses to carry.
        inst.set_attr("0008,0008",
                      _derived_image_type(inst.attributes.get("0008,0008", [])))

        # 2. Burned In Annotation (0028,0301) -> NO
        inst.set_attr("0028,0301", _REDACTION_BURNED_IN)

        # 3. Derivation Description (0008,2111)
        inst.set_attr("0008,2111", _REDACTION_DERIVATION_DESCRIPTION)

        # 4. Derivation Code Sequence (0008,9215)
        # Code 113062: Pixel Data modification
        seq = DicomSequence(tag=_REDACTION_FLAG_SEQUENCE)
        item = DicomItem()
        for tag, value in _REDACTION_DERIVATION_CODE:
            item.set_attr(tag, value)
        item._parent = inst
        seq.items.append(item)

        inst.sequences[_REDACTION_FLAG_SEQUENCE] = seq
