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
        return self._index.get(sn, [])


@dataclass
class RedactionOutcome:
    """What one worker has to tell the parent about one instance (#213).

    `None` used to mean three things -- already redacted under this
    configuration, no pixel data to redact, and an exception -- and the
    parent read all three as "nothing to apply". Only the third is a
    failure, and it is the one that leaves burned-in PHI in an instance
    the pipeline then reports as fine.

    `sop_instance_uid` is the **pre-redaction** UID, for the same reason
    the mutation dict carries `original_sop_uid`: a redacted image gets a
    new UID and the parent's map is keyed on the old one.

    `error` is prose, not a `BaseException`, and that is a deliberate
    divergence from `ExportOutcome.error`. Every consumer of that field
    stringifies it, and carrying an object across a process boundary adds
    a failure mode that turns a reportable failure into an unreportable
    one: an exception whose `__init__` does not round-trip through
    `pickle` fails to serialise, and what the parent receives is a
    pickling error about the *result* rather than the failure the worker
    was trying to report.
    """
    ok: bool
    sop_instance_uid: str
    mutation: Optional[dict] = None
    error: Optional[str] = None


class RedactionError(RuntimeError):
    """Redaction did not remove what it was asked to remove (#213).

    Raised after the whole pass, not at the first failure: the instances
    that could be redacted are redacted, and the failures are already in
    the audit log, so a caller that catches this still gets a compliance
    report that grades REVIEW_REQUIRED.

    **`RuntimeError`, not `Exception`, and not to be demoted to a bare
    `RuntimeError` later for symmetry.** `write_tree` and
    `_export_instance_worker` already raise bare `RuntimeError`s on this
    same pipeline, so `except RuntimeError` around a full run cannot tell
    the three apart -- but subclassing keeps every existing
    `except RuntimeError` catching this one, where subclassing `Exception`
    directly would turn a caught error into an escaping one. The
    asymmetry with the export raises is the point: those mean "nothing
    was written", this one means "something unsafe is still in the graph".
    """

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

    The mirror of `DicomExporter._report_export_failures`, and one
    spelling shared by both redaction paths -- the parallel one in
    `Session._apply_redaction_outcomes` and the serial one in
    `RedactionService.redact_machine_instances`.

    `ERROR`, not `DATA_LOSS`, for two independent reasons. *Vocabulary*:
    nothing was dropped -- the burned-in identifier is **present**, and
    being present is the problem, which is a failed operation and exactly
    what `_report_export_failures` writes `ERROR` for. *Grading*: a
    `DATA_LOSS` row is graded by `loss_scope`, and `STANDARD` leaves the
    run at `PASS` while `PRIVATE` would be a lie about a tag that does not
    exist. An `ERROR` row lands in `get_audit_errors()`, populates
    `exceptions`, and takes `validation_status` to `REVIEW_REQUIRED`.

    Warning and auditing are deliberately not the same condition, for the
    reason `_report_export_losses` gives: `RedactionService(store)` with no
    backend is a supported construction, and gating the report on one would
    lose the failure entirely.

    The detail is flattened to one line and its pipes escaped because it is
    rendered straight into a markdown table row in the compliance report.
    It is **not** truncated.

    Returns:
        List[Tuple[str, str]]: `(entity_uid, details)` per failure, flattened.
    """
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
    """The SOP Instance UID `inst` takes when redacted under `config_hash`
    (#544): derived from the UID it was **ingested** under -- the source
    recorded by an earlier UID replacement or redaction, else its own --
    so redacting before or after `anonymize()` gives one UID, and a
    `force=True` re-redaction under other zones another. Always computed
    in the parent: a worker is handed the result, never the secret."""
    from .privacy import _redaction_uid_for  # pylint: disable=import-outside-toplevel
    source = inst.attributes.get(SOURCE_SOP_UID_ATTR) or inst.sop_instance_uid
    return _redaction_uid_for(source, config_hash, secret)


#: What `_apply_redaction_flags` writes, spelled once. The #486 guard
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
    ORIGINAL gone, and SECONDARY as Value 2 where nothing else fills it."""
    if isinstance(current, str):
        current = [current]
    derived = ["DERIVED"] + [x for x in (current or [])
                            if x not in ("ORIGINAL", "DERIVED")]
    if len(derived) < 2:
        derived.append("SECONDARY")
    return derived


#: Bookkeeping pixel redaction writes to an instance itself, and so may
#: change without invalidating the instance's tag-scan conclusion (#486;
#: confirmed by the owner on 2026-09-11): `regenerate_uid()` writes the
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

    The whole tree, not the top level: an edit inside a nested item is as
    much an edit the scan has not seen as one at the top, and #57 is what
    a nested value skipped by a top-level-only view cost once. `repr`
    because values include lists, and comparison is all this is for.
    Sequence keys are included per item, so an emptied or added sequence
    counts as a change even though it holds no attribute.
    """
    def flat(item, skip_attrs=frozenset(), skip_seqs=frozenset()):
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
    """The flag tags and the Derivation Code Sequence as they stand."""
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

    Either, because a hash-match skip and a failed instance leave the
    captured values in place, and neither is an edit. Anything else --
    a caller's text in DerivationDescription, a value appended to
    ImageType, a second item in the Derivation Code Sequence -- is an
    edit the scan has not seen. These tags used to be skipped by the
    fingerprint, so such an edit was carried (found in the review of
    #486).
    """
    (before_values, before_items), (after_values, after_items) = before, after
    image_type = None if before_values[0] is _NO_VALUE else before_values[0]
    expected = (_derived_image_type(image_type), _REDACTION_BURNED_IN,
                _REDACTION_DERIVATION_DESCRIPTION)
    for was, now, wanted in zip(before_values, after_values, expected):
        if now not in (was, wanted):
            return False
    return after_items in (before_items, (_REDACTION_DERIVATION_ITEM,))


def capture_phi_status_for_redaction(inst: Instance) -> Optional[tuple]:
    """What to carry across a redaction pass, read before the pass (#486).

    Returns `(status, metadata, flags)` when the instance's status is REMEDIATED
    or CLEARED at its current revision, else None. **Call it before
    dispatch**: under threads the worker writes to the live instance, so a
    status read when the outcome lands is already UNSCANNED.

    This is option 2 on #486, confirmed by the owner on 2026-09-11.
    Without it,
    `redact()`'s own writes move every redacted instance to UNSCANNED --
    measured, revision 12 to 19 -- and the documented anonymize -> redact
    -> export path produces a manifest saying `"anonymized": false` for
    every instance it redacted.
    """
    status = inst.phi_status
    if status not in _CARRIED_STATUSES:
        return None
    return status, _metadata_outside_redaction(inst), _redaction_flags(inst)


def carry_phi_status_across_redaction(inst: Instance, captured) -> bool:
    """Re-record a captured status if only redaction touched the instance.

    **The guard is the point.** The revision rule exists so an edit nobody
    re-scanned cannot inherit an assurance; this is the one place that
    overrides it, and it does so only when every attribute and nested item
    outside `_REDACTION_OWN_ATTRS` and `_REDACTION_OWN_SEQUENCES` is
    exactly as captured, and every flag redaction writes is as captured
    or exactly what redaction writes (`_flags_are_redactions`). Any other
    change -- a concurrent `set_attr` of another tag, top-level or nested,
    or a caller's value in a flag -- and nothing is recorded, so the
    instance stays UNSCANNED as the rule requires. Returns whether it
    re-recorded.
    """
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
#: each arm's `finally` puts back (#434) -- that leaves the SOP Instance
#: UID and the UID it replaced, from `regenerate_uid()`, and the hash.
_ATTESTATION_ATTRS = _REDACTION_FLAG_TAGS + tuple(sorted(
    _REDACTION_OWN_ATTRS - frozenset(_SET_PIXEL_DATA_TAGS)))
_ATTESTATION_SEQUENCES = tuple(sorted(_REDACTION_OWN_SEQUENCES))
_ABSENT = object()


def _capture_attestation(inst: Instance) -> tuple:
    """The instance as it stands before the attestation is written (#474).

    References, not copies: `_apply_redaction_flags` replaces each value
    and the Derivation Code Sequence whole, and `regenerate_uid()`
    rebinds the identity, so nothing held here is mutated in place.
    """
    return (inst.sop_instance_uid, inst.file_path,
            {tag: inst.attributes.get(tag, _ABSENT)
             for tag in _ATTESTATION_ATTRS},
            {tag: inst.sequences.get(tag, _ABSENT)
             for tag in _ATTESTATION_SEQUENCES})


def _withdraw_attestation(inst: Instance, attested_from: tuple) -> None:
    """Put back what `_capture_attestation` saw: the persist failed (#474).

    Both redaction arms write the attestation -- `ImageType` DERIVED,
    `BurnedInAnnotation` NO, the derivation description and code
    sequence, a new SOP Instance UID and the configuration hash -- and
    then persist the redacted pixels. When that persist raised, the
    instance kept the attestation over pixels the loader still read
    unredacted: the serial arm returned normally, and the threads arm,
    whose instance is the live one, raised but left the hash that made
    the retry skip it as already redacted. Withdrawn here, the instance
    is as it was found, which is what `Session.redact()` promises for a
    failed instance.

    **Withdrawn after a failure rather than withheld until a success.**
    `_swap_pixels_under_gate` records the new frame's blob row under the
    UID the instance carries when it persists. Persisting before
    `regenerate_uid()` would write the redacted frame's row under the
    old UID, over the ingest frame's row, while the unsaved `instances`
    row still names the ingest frame: a store that disagrees with itself
    until the next save, across any crash or `compact()` in between.
    Withdrawing leaves the successful path exactly as it was.

    Direct writes, as `Instance._restore_replaced_descriptors` makes
    them: two of these keys are uppercase, `set_attr` lowercases, and
    there is no remove-attribute method.
    """
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
    Device Serial Number is `serial`? (#580)

    The one spelling of the predicate. Exact spelling, or `"*"` for every
    series; a series with no serial matches nothing, and neither does a
    rule with none. That last half is not a choice made here:
    `MachinePixelIndex` indexes only series with equipment and a serial,
    so `redact()` has never reached a serial-less series under any rule,
    and the export has to agree with it. `redact()`'s target walk and the
    export's zones both ask this, which is what #580 was: the export
    asked `Configuration.get_rule`, exact and first-match, while
    `redact()` honoured `"*"` and every rule.
    """
    if not serial:
        return False
    return rule_serial in ("*", serial)


def rules_matching(rules, serial) -> List[dict]:
    """Every rule that covers a series with this serial, in rule order
    (#580).

    Every one, not the first: `redact()` runs each loaded rule as its own
    pass, so an exact rule and a `"*"` rule, or two rules on one serial,
    both redact that series, and an export that took the first match
    exported the second rule's zones unredacted. The session's
    `_redaction_zones_for` and, through it, the store-wide icon gate read
    this.
    """
    return [rule for rule in (rules or ())
            if rule_applies_to(rule.get("serial_number"), serial)]


def zone_rois(zones, on_invalid=None) -> List[tuple]:
    """The ROIs a rule's `redaction_zones` names, each as a 4-tuple (#580).

    Two shapes are accepted, because both are in use: a bare
    `[y1, y2, x1, x2]`, and `{"roi": [y1, y2, x1, x2], ...}` -- the shape
    the shipped knowledge base and `create_config`'s scaffolder write,
    which `load_config` accepts and `redact()` applied, and which failed
    every export of a matching instance with `ValueError: invalid literal
    for int() with base 10: 'roi'` because the export passed the raw zone
    through. Anything without exactly four values is dropped, and handed
    to `on_invalid` when one is given. A tuple zone is not a third shape:
    `load_config` refuses one ("must be list or dict") and nothing builds
    one, so accepting it here would be a spelling no door produces.

    **The values are passed through as given, as a tuple -- no `int()`.**
    `prepare_redaction_tasks` hashes `sorted()` of these tuples into the
    redaction attestation, so coercing here would change every
    attestation hash and turn an already-redacted instance back into a
    candidate. `apply_redaction_to_array` does the coercion, where the
    pixels are addressed.
    """
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
        does not contain 'DERIVED'). This is a post-process safety check.
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
        grade's `audit_summary` arm sees, so its unit and its wording are
        the published shape (#247), decided here rather than inherited:

        - **Per rule-pass**, keyed on the serial spelling the rule was
          configured with (`"*"` included) -- bounded like the serial
          path's old per-machine row, where per-instance rows would put
          10k lines in a 10k-instance session's report.
        - **Outcome, not intent.** The row is written after the pass, and
          `applied`/`targeted` say what happened. The serial path used to
          write "Redacting N images..." before its loop, which attested a
          pass whose every instance was then skipped or failed.
        - **In the parent, always** (#126): a worker's audit thread is
          torn down at pool shutdown without `stop()`, so a row queued
          there can be lost -- and for a `:memory:` database the child
          writes nowhere at all.

        Both `redact_machine_instances` and `Session._apply_redaction_rules`
        call this and nothing else writes `REDACTION` rows;
        `tests/test_redaction_audit_accounting.py` pins the two paths to
        byte-identical accounting for identical work.
        """
        if not self.store_backend:
            return
        self.store_backend.log_audit(
            action_type="REDACTION",
            entity_uid=machine_sn,
            details=(f"Applied {applied} of {targeted} candidate images "
                     f"with {zone_count} zones"))

    def _targets_for(self, rule_serial) -> List[Instance]:
        """Every indexed instance a rule for `rule_serial` covers (#580):
        the index-side half of `rule_applies_to`, in index order."""
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
            force (bool): Carried into every task as `task["force"]` and read
                only by `execute_redaction_task`'s attestation skip. See
                `Session.redact()`, which is where a caller chooses it, and
                `redact_machine_instances`, which takes the same flag as a
                keyword so the two paths stay symmetrical (#237).
            project_secret (bytes, optional): What each task's
                `new_sop_uid` is derived under (#544); without it, the
                store backend's. `Session.redact()` passes it.

        Returns:
            List[dict]: A list of task dictionaries ready for `execute_redaction_task`.

        Raises:
            RuntimeError: When a rule has targets and there is no project
                secret to derive their UIDs under (#544), as
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
        # export's zones also ask (#580). The index holds only series with
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
        # a collision. Do not change this input (#237).
        rois_stable = sorted(valid_rois)
        config_str = json.dumps({"serial": serial, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        # The redacted UIDs are derived here, in the parent (#544): the
        # secret is read once and never put on a task or on the service.
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
                # match, so the redaction is silently discarded (#257).
                # Same pattern as `config_hash` and `force`: per-task
                # state the worker must not re-derive.
                "original_sop_uid": inst.sop_instance_uid,
                "rois": valid_rois,
                "config_hash": config_hash,
                "machine_sn": serial,
                "force": force
            })

        return tasks

    def execute_redaction_task(self, task: dict):
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
                and `ok=False` with an `error` string when a zone could not
                be applied.

        **The mutation dict exists only when a zone landed**, which is
        what makes its presence the parent's honest signal. It used to be
        built unconditionally, so an instance whose every zone started
        past the edge of the image came back carrying
        `{"0028,0301": None, "0008,0008": None, ...}` -- read from the
        worker's own instance, where `_apply_redaction_flags` had never
        run -- and the parent wrote those nulls onto the graph and counted
        the instance as updated. `redact_machine_instances` never had that
        shape, and this is the change that stopped the two paths
        disagreeing (#235).

        **The worker never raises.** `_apply_redaction_rules` consumes
        `run_parallel(..., return_generator=True)` incrementally, and an
        exception escaping a worker terminates that generator mid-iteration
        -- so every mutation still queued behind it would be lost, and the
        instances that *were* redacted would silently never reach the graph.
        Returning an outcome is what makes "all successful mutations are
        applied before the raise" true rather than aspirational (#213).
        """
        inst = task["instance"]
        # From the task, never `inst.sop_instance_uid`. Under threads the
        # sibling task's worker shares this very object, and its
        # `regenerate_uid()` may already have moved the live attribute by
        # the time this worker starts -- a re-read here keys the mutation
        # on a post-redaction UID the parent's map discards (#257).
        original_uid = task["original_sop_uid"]
        rois = task["rois"]
        config_hash = task["config_hash"]
        force = task.get("force", False)
        # Nothing in the `finally` reads a name bound inside the `try`
        # any more. It used to read `modified` and `failed` to decide a
        # second persist, and both had to be pre-bound because an
        # `UnboundLocalError` raised *in* a `finally` replaces the return
        # value. That persist is gone (#368); if a `finally` ever reads
        # a `try`-bound name again, pre-bind it here for that reason.

        try:
            # Optimized: Skip if already redacted with same config.
            #
            # `force` suppresses this and nothing else. The attestation is
            # over the configuration, not over the pixels, so a store whose
            # pixels a defective release left wrong carries a hash
            # byte-identical to the one this code would write -- and the
            # skip then declines to look at it forever. That is #237, and
            # `force=True` is the lever out of it.
            current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

            if not force and current_hash == config_hash:
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            # Triggers Lazy Load from disk
            arr = inst.get_pixel_data()

            if arr is None:
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            # The whole zone list in one call. A loop here was #229: the
            # callee rebinds `arr` locally when it copies a read-only array,
            # so the next iteration handed it the pristine original again
            # and only the last zone survived.
            modified = self._redact_instance_pixels(inst, arr, rois)

            if not modified:
                # No configured zone landed inside this image. That is a
                # legitimate skip of exactly the kind the two early
                # returns above describe, and it says so in the same
                # vocabulary: no mutation, nothing for the parent to
                # copy, nothing counted. Building the dict here anyway is
                # what wrote null `(0028,0301)`/`(0008,0008)`/
                # `(0008,2111)` elements onto an untouched instance and
                # reported it as updated (#235).
                return RedactionOutcome(ok=True, sop_instance_uid=original_uid)

            attested_from = _capture_attestation(inst)
            self._apply_redaction_flags(inst)
            inst.regenerate_uid(task["new_sop_uid"])
            # Mark as redacted with this hash
            inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
            inst.mark_modified()

            # CRITICAL: Persist modified pixel data to sidecar (generate new Loader).
            #
            # This is the only persist on this path, and it has to be
            # here rather than in the `finally`: the mutation dict below
            # reads `inst._pixel_loader`, and it is *this* call that
            # re-points it at the redacted frame. Drop it and the parent
            # is handed a loader for the pre-redaction pixels.
            #
            # There used to be a second call in the `finally`, guarded by
            # `modified and not failed`. On every path where that guard
            # is true this call has already run (had it raised, `failed`
            # would be True), so the second was a double append: measured
            # on 0.9.3, one task wrote two frames, `[(28, 36), (64, 36)]`,
            # and the mutation's loader pointed at the first while the
            # committed `instance_blobs` row pointed at the second -- two
            # answers to where the redacted frame lives, until the next
            # save's dedup re-emitted it. Deleted in #368;
            # `tests/test_services.py` counts the calls. The serial arm
            # (`redact_machine_instances`) persists in its `try` too, since
            # #474.
            if self.store_backend and hasattr(self.store_backend, 'persist_pixel_data'):
                try:
                    self.store_backend.persist_pixel_data(inst)
                except Exception:
                    # The attestation above is over pixels that never
                    # reached the store: withdraw it, then fail the task
                    # like any other failure (#474). Under threads `inst`
                    # is the live instance, and without this it kept
                    # `BurnedInAnnotation NO`, the new UID and the hash,
                    # so the retry `redact()`'s error promises was skipped
                    # as already redacted.
                    _withdraw_attestation(inst, attested_from)
                    raise
            else:
                # Fallback or Warning? If we don't persist, pixel data is memory-only and won't export correctly?
                # Actually, export might handle in-memory data if it's dirty?
                # But we need SidecarPixelLoader for process isolation return.
                pass

            # Prepare Mutated State to return (for Process Isolation)
            mutation = {
                "original_sop_uid": original_uid,  # KEY FIX: Mapped to Main Process
                # The rule-pass this application belongs to, for the
                # parent's REDACTION audit row (#247). Carried on the
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
                # (`_apply_redaction_outcomes`, #228). Reaching this line
                # means `regenerate_uid()` ran a few lines above, so this
                # is always a new identity and never the original --
                # which is why the parent gates on the mutation existing
                # rather than on the two UIDs differing. Move the
                # `regenerate_uid()` call above out of this block and
                # every instance takes a new identity for nothing.
                "sop_uid": inst.sop_instance_uid,
                "pixel_loader": inst._pixel_loader,
                "pixel_hash": getattr(inst, "_pixel_hash", None),
                # The label of the frame `pixel_loader` reads (#482). The
                # read this worker made in order to redact relabelled *its*
                # copy wherever the decode converted -- a YBR file that
                # pydicom or the handler returns as RGB -- and under
                # processes that copy is discarded. Without this the
                # parent kept the YBR label over the worker's RGB frame,
                # with `file_path` cleared and no file left to read again,
                # and export wrote the two together. Assigned in the parent
                # beside the loader rebind (`_apply_redaction_outcomes`),
                # the seam #228 used for the identity. Its own key rather
                # than a row in `attributes` below, which the parent
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
            # DEBUG
            # print(f"DEBUG: Worker returning mutation for {inst.sop_instance_uid}", file=sys.stderr)
            return RedactionOutcome(ok=True, sop_instance_uid=original_uid,
                                    mutation=mutation)

        except Exception as e:
            # The catch stays broad, and a missing-argument `TypeError` from
            # `apply_redaction_to_array` is audited here like any other
            # failure. That collision is deliberate: a malformed zone in a
            # JSON config raises `TypeError` too -- `(0, None, 0, 8)` and
            # `(0, [1], 0, 8)` both do -- so narrowing this catch to make
            # room for the programming error would drop real failed
            # redactions and re-open #213. Do not add traceback-frame
            # inspection to tell the two apart (#217).
            traceback.print_exc()
            # `original_uid`, not the live attribute: this line names the
            # identity the parent's failure row carries, and a sibling
            # worker may have moved `inst.sop_instance_uid` by now (#257).
            self.logger.error(f"  Failed {original_uid}: {describe_exception(e)}")
            return RedactionOutcome(ok=False, sop_instance_uid=original_uid,
                                    error=describe_exception(e))
        finally:
            # Memory cleanup only. No persist lives here any more: the
            # one in the `try` body is the only append this path makes
            # (#368), and a failed redaction must not be persisted at all
            # -- that gate is the whole of #213's "a failed instance is
            # left as it was found". `apply_redaction_to_array` raises
            # *mid-loop*, so zones 1..k-1 are already zeroed when zone k
            # fails; persisting made that partial mutation durable on the
            # threads path (3.14t's default) while the processes path
            # (3.12's) mutated a copy and left the instance untouched --
            # the same failed redaction leaving two different sidecars
            # depending on the interpreter. Without a persist, the
            # unconditional `discard_pixel_data()` below drops the
            # mutated array and the next `get_pixel_data()` reloads the
            # original through the loader, under the descriptors it was
            # stored with: the discard also puts back whatever the
            # copying arm's `set_pixel_data()` wrote (#434).
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
            # the loader, and `unload_pixel_data()` now refuses exactly
            # that case (#293). Byte-for-byte the pre-#293 behaviour.
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

        Legacy/Single-threaded entry point (mostly replaced by parallel approach).

        Args:
            machine_rules (dict): The rule configuration.
            show_progress (bool): If True, shows progress bar.
            verbose (bool): If True, logs details.

        Raises:
            RedactionError: Propagated from `redact_machine_instances` when
                any instance's zone could not be applied. This method used
                to return normally in that case, because the failure was
                logged and dropped one frame down (#213).
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
        """The project secret a redaction derives its UIDs under (#544).

        The one given; otherwise the store backend's, read in the parent
        (`_project_secret_for_use`, which creates one on a store that has
        none and refuses on a store that lost its own); otherwise
        `RuntimeError`, never a random UID. Returned to the caller and
        **never kept on the service**: under processes the service is
        pickled to every worker with its bound `execute_redaction_task`,
        and the worker is handed the UID, not the secret.
        """
        if project_secret:
            return project_secret
        reader = getattr(self.store_backend, "_project_secret_for_use", None)
        if reader is None:
            # `_require_secret`'s refusal, so there is one wording for "no
            # secret, no unkeyed fallback".
            from .privacy import _require_secret  # pylint: disable=import-outside-toplevel
            return _require_secret(None)
        return reader(diagnose=False)

    def redact_machine_instances(
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

        Optimized to iterate images ONCE per machine rule, applying all ROIs in a single pass.

        Args:
            machine_sn (str): The serial number (for logging/auditing).
            rois (List[tuple]): List of (y1, y2, x1, x2) ROIs.
            targets (List[Instance], optional): Pre-filtered list of instances.
            show_progress (bool): If True, shows progress bar.
            force (bool): If True, re-redact an instance whose
                `_ISOCENTER_REDACTION_HASH` already matches this
                configuration. Suppresses that skip and nothing else. It
                is **last in the signature and defaulted** deliberately:
                `test_redaction_optimization.py`, `test_redaction_rgb.py`,
                `test_services.py` and `test_pixel_geometry_pipeline.py`
                all call this method positionally with two arguments.
                `Session.redact(force=True)` is the same lever on the
                parallel path (#237).
            project_secret (bytes, optional): The project secret each
                redacted instance's SOP Instance UID is derived under
                (#544). Without it, the store backend's; a service with
                no backend has to be given one.

        Raises:
            RedactionError: If any instance's zone could not be applied.
                Raised at the end of the pass, for the same reasons as
                `Session.redact()`: the instances that could be redacted
                are redacted and every failure is already an `ERROR` row.
                This is the serial path, so it must answer the same
                question the parallel one does -- it is public, it is what
                `process_machine_rules` calls, and a failure here left the
                burned-in identifier in the pixels just as silently (#213).
            RuntimeError: Before any instance is touched, when there is
                no project secret to derive a UID under -- no
                `project_secret` and no store backend -- or the backend's
                store refuses one (it lost the secret its dates or UIDs
                were derived under). Until 1.0 this drew a random UID and
                needed no secret (#544).

        Returns:
            None. The signature is unchanged;
            `test_redaction_optimization.py` mocks this method and asserts
            on the call, not the result.
        """
        if targets is None:
            targets = self.index.get_by_machine(machine_sn)
        # Before the first instance, so a refusal changes nothing.
        secret = self._redaction_secret(project_secret)

        self.logger.info(f"Redacting {len(targets)} images for {machine_sn} ({len(rois)} zones)...")

        # 1. Compute Hash for this Config
        # We assume rois list fully captures the intent (zones)
        #
        # Sort to ensure stability if zones are re-ordered -- and the sort
        # is *correct*, not merely stable. Redaction zeroes, and zeroing
        # is commutative and idempotent, so two orderings of one zone list
        # cannot produce different pixels. Measured on `84113ab`, disjoint
        # (`[[0,8,0,8],[100,200,100,200]]`) and overlapping
        # (`[[0,8,0,8],[4,12,4,12]]`), identical totals both ways. #237
        # read the pre-#229 order dependence as a hash collision; it was
        # the per-zone-copy bug, and it is gone.
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
            # does for the parallel path (#486; confirmed by the owner).
            # `captured` is that status; the attestation's own snapshot
            # below is `attested_from`, so the carry after the `finally`
            # is never handed the wrong one (#474).
            captured = capture_phi_status_for_redaction(inst)
            try:
                # Optimized: Skip if already redacted with same config.
                # `force` suppresses this and nothing else -- see
                # `execute_redaction_task`, which carries the same flag
                # through its task dict (#237).
                current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

                # DEBUG: Log hashes
                # if verbose and current_hash:
                #    self.logger.debug(f"DEBUG: {inst.sop_instance_uid} Current: {current_hash} vs New: {config_hash}")

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
                # with the new loader, after its write. This arm used to set
                # it to None here, "so a failed save does not match the old
                # hash" -- but the loader still reads the old frame after a
                # failure, and the None reached the row: a failed persist
                # (a full disk, an EIO) on an instance whose UID the pass
                # had regenerated saved a new row with no hash, so a
                # reopened session read that frame unchecked (#436, review
                # of #466). The threads and processes arm never cleared it.

                # One call, the whole zone list. See the note in
                # `execute_redaction_task`: a per-zone loop here kept only
                # the last zone's work on a reloaded instance (#229).
                modified = self._redact_instance_pixels(inst, arr, rois)

                if modified:
                    attested_from = _capture_attestation(inst)
                    self._apply_redaction_flags(inst)
                    inst.regenerate_uid(_redacted_uid_for(inst, config_hash, secret))
                    # Mark as redacted with this hash
                    inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
                    # Force Dirty to persist metadata update
                    inst.mark_modified()
                    # The persist: in the `try` since #474, after the hash
                    # and before the count. It sat in the `finally`, where
                    # a raise (a full disk, an EIO) was logged and dropped:
                    # no failure recorded, "Applied 1 of 1" in the pass row,
                    # and an instance attesting a redaction over pixels the
                    # loader still read unredacted. Here a raise withdraws
                    # the attestation and takes the `except` below like any
                    # other failure.
                    #
                    # Only when a zone landed: `persist_pixel_data` does
                    # not deduplicate, and measured on `84113ab` an
                    # off-image rule grew the sidecar 17 -> 34 bytes with a
                    # copy nothing pointed at (#235). Never after a zone
                    # that failed, which raises before reaching here: the
                    # zones before it are already zeroed, and persisting
                    # them made a partial redaction durable (#213; the
                    # whole argument is in `execute_redaction_task`'s
                    # `finally`).
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
                # `execute_redaction_task` (#217).
                failures.append(
                    (original_uid,
                     f"Redaction failed for {original_uid}: "
                     f"{describe_exception(e)}"))
                self.logger.error(f"  Failed {inst.sop_instance_uid}: {describe_exception(e)}")
            finally:
                # Memory cleanup only; the persist is in the `try` (#474).
                #
                # `discard_pixel_data`, not `unload_pixel_data`: dropping the
                # resident array is the INTENT here, not an optimisation. On a
                # failed redaction it is a partially-zeroed array that must go
                # so the next `get_pixel_data()` reloads the original through
                # the loader, and `unload_pixel_data()` now refuses exactly
                # that case (#293). Byte-for-byte the pre-#293 behaviour.
                inst.discard_pixel_data()

            # After the `finally`, not inside `if modified:` -- the persist
            # and `discard_pixel_data()` above can move the revision too,
            # and a carry made before them would be undone by them. A skip
            # (`continue` above) never reaches here and needs nothing: it
            # wrote nothing, so its status never moved.
            carry_phi_status_across_redaction(inst, captured)

        # After the pass and before the raise, for the same reason the
        # ERROR rows are: a caller that catches `RedactionError` still
        # holds a report whose section 2 accounts for this pass (#213,
        # #247). This row used to be written *before* the pass, as intent
        # -- "Redacting N images..." -- which attested passes whose every
        # instance was subsequently skipped or failed.
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

        Args:
            arr (np.ndarray): The pixel array to modify.
            rois (List[tuple]): List of (y1, y2, x1, x2) regions.
            geometry (PixelGeometry): The instance's resolved geometry,
                from `isocenter.pixel_geometry`. **It must have been
                resolved from the shape of *this* array** -- that is the
                invariant the axis selection below depends on, and nothing
                here can check it. Required, with no default: the default
                was the last-axis heuristic, which could not tell a
                4-frame 8x4 grayscale array from a 2x8 RGBA one and
                addressed the wrong axes, so 32 of 32 identifier cells
                reached an exported file while redaction reported success
                (#186, #205, #217).

        Returns:
            bool: True if any modification was applied.
        """
        modified = False

        ndim = len(arr.shape)
        # No `ndim >= 3` guard here, and the invariant that makes that safe
        # lives in the caller: `geometry` must have been resolved from the
        # shape of *this* array, and `resolve_pixel_geometry` cannot return
        # samples > 1 for a rank-2 one. Both in-tree callers do exactly
        # that. A geometry borrowed from a different array could pair
        # samples > 1 with ndim == 2 and silently address
        # `row_dim=-1, col_dim=0`. That invariant is the only thing standing
        # here now, which is why `geometry` is required rather than
        # defaulted (#217).
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
        # read-only array reaching here now raises below rather than being
        # silently skipped.

        for roi in rois:
            try:
                r1, r2, c1, c2 = [int(v) for v in roi]

                # A zone whose *shape* is empty is a configuration error
                # on any image: `arr[r1:r2, c1:c2]` with `r2 <= r1` or
                # `c2 <= c1` selects zero pixels, the assignment below
                # would still set `modified = True`, and the instance
                # would be counted, renamed, and fully attested --
                # `BurnedInAnnotation = NO` on pixels nothing touched
                # (#244). Judged before the off-image `continue` below,
                # which is #235's boundary for a *real* zone that landed
                # elsewhere and stays a legitimate skip. The raise takes
                # the #213 failure path in every caller: ERROR rows and
                # `RedactionError` on both redact paths, a failed
                # outcome and no file in the export worker. One common
                # source of this shape is a box in x,y,w,h order --
                # `discovery.py` converts to (y1, y2, x1, x2), and
                # `automation.py` did not until #258.
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
                # already means "no zones matched". (#66)
                #
                # The premise above was only half true, and which half is
                # worth knowing before changing either end. The **export
                # worker's** call (`io_handlers._export_instance_worker`)
                # has no handler between it and the worker's outermost
                # `except`, so this raise did propagate, did become
                # `ExportOutcome(ok=False)`, and no file was written. The
                # two callers it does *not* name -- `redact_machine_instances`
                # and `execute_redaction_task` -- each caught bare
                # `Exception` one frame up and only logged, so the raise
                # travelled exactly one stack frame and the instance stayed
                # in the graph for `export()` to write. Both now audit an
                # ERROR row and raise `RedactionError` after the pass (#213).
                get_logger().error(
                    "Redaction zone %s could not be applied to an array of "
                    "shape %s: %s", tuple(roi), arr.shape, describe_exception(exc))
                raise

        return modified

    def _redact_instance_pixels(self, inst: Instance, arr,
                                rois: List[tuple]) -> bool:
        """
        Applies **every** ROI to one instance's pixel array, in one pass.

        Wrapper around static apply_redaction_to_array for instance management.

        **Call this at most once per instance per pass, with the full zone
        list.** It rebinds `arr` locally on the not-writeable arm, so a
        caller that called it once per zone would hand it the pristine
        original again every time and keep only the last zone's work -- with
        `modified` True, a redaction hash written, and a report grading
        PASS. That was #229. There is deliberately no per-zone entry point
        to call in a loop, and a caller must not read its own `arr` after
        this returns: on the not-writeable arm the array the instance now
        holds is a different object.

        **This method dirties the instance on one arm only, and the callers
        are load-bearing for the other.** A not-writeable array is copied and
        handed to `set_pixel_data`, which ends in an unconditional
        `mark_modified()`, so that arm returns with the instance needing a
        save. A writeable array is redacted *in place*: `set_pixel_data` is
        never called, no attribute changes, and this method returns True
        leaving `has_unsaved_changes` False. Measured, both arms:

            writeable=False  returned=True  dirty=True   zone_zeroed=True
            writeable=True   returned=True  dirty=False  zone_zeroed=True

        Nothing is wrong today, because both callers close it -- the serial
        `redact_machine_instances` and `execute_redaction_task` each call
        `inst.mark_modified()` under `if modified:` and persist the pixels
        afterwards. But a third caller that trusts the return value and
        skips that call silently drops the redacted pixels on the writeable
        arm: the zone really is zeroed in memory, the instance reports
        itself saved, and an incremental `save_all` writes nothing, so the
        exported file still carries the burned-in identifiers. No test can
        catch that, because the writeable arm's dirtying does not live in
        the function under test. Move or remove either `mark_modified()`
        only together with this arm.
        """
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

        # 1. Image Type (0008,0008)
        # We need to preserve existing values but ensure 'DERIVED' is first.
        # Note: In a robust implementation, we'd read the old value first.
        # Here we force a standard Derived type.
        # Every value here is a module constant the #486 guard reads:
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
        seq.items.append(item)

        inst.sequences[_REDACTION_FLAG_SEQUENCE] = seq
