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
from tqdm import tqdm
import numpy as np

from .entities import Instance, DicomItem, DicomSequence
from .pixel_geometry import PixelGeometry, resolve_pixel_geometry
from .store import DicomStore
from .logger import get_logger


# Define standard codes for the Sequence
CODE_BASIC_PROFILE = {"0008,0100": "113100", "0008,0102": "DCM",
                      "0008,0104": "Basic Application Confidentiality Profile"}
CODE_CLEAN_PIXEL = {
    "0008,0100": "113101",
    "0008,0102": "DCM",
    "0008,0104": "Clean Pixel Data Option"}


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

    def prepare_redaction_tasks(self, machine_rules: dict, verbose: bool = False,
                                force: bool = False) -> List[dict]:
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

        Returns:
            List[dict]: A list of task dictionaries ready for `execute_redaction_task`.
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

        # Check matches in store
        targets = []
        if serial == "*":
            # Wildcard: Apply to ALL machines
            for sn_key in self.index._index:
                targets.extend(self.index.get_by_machine(sn_key))
        else:
            # Exact Match
            targets = self.index.get_by_machine(serial)

        if not targets:
            if verbose and serial != "*":
                self.logger.warning(
                    f"Config rule exists for {serial}, but no matching images found in Session.")
            return []

        # Parse ROIs
        valid_rois = []
        for zone in zones:
            if isinstance(zone, list):
                roi = zone
            else:
                roi = zone.get("roi")

            if roi and len(roi) == 4:
                valid_rois.append(tuple(roi))
            else:
                self.logger.warning(f"Invalid ROI format in config: {roi}")

        if not valid_rois:
            return []

        # Compute Hash. `sorted` for the reason `redact_machine_instances`
        # gives at length: zeroing is commutative, so zone order cannot
        # change the pixels, and the sort is therefore correct rather than
        # a collision. Do not change this input (#237).
        rois_stable = sorted(valid_rois)
        config_str = json.dumps({"serial": serial, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        # Create Tasks
        tasks = []
        for inst in targets:
            tasks.append({
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
        failed = False
        # Bound before the `try`, not inside it. Two early returns and the
        # exception path all reach the `finally`, which reads this; an
        # `UnboundLocalError` raised *in* a `finally` replaces the return
        # value, so an unbound name here would turn every legitimate skip
        # into a worker failure.
        modified = False

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

            self._apply_redaction_flags(inst)
            inst.regenerate_uid()
            # Mark as redacted with this hash
            inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
            inst.mark_modified()

            # CRITICAL: Persist modified pixel data to sidecar (generate new Loader).
            #
            # This call cannot move into the `finally` alongside the other
            # one, however redundant the pair looks: the mutation dict
            # below reads `inst._pixel_loader`, and it is *this* call that
            # re-points it at the redacted frame. Drop it and the parent
            # is handed a loader for the pre-redaction pixels.
            if self.store_backend and hasattr(self.store_backend, 'persist_pixel_data'):
                self.store_backend.persist_pixel_data(inst)
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
            failed = True
            traceback.print_exc()
            # `original_uid`, not the live attribute: this line names the
            # identity the parent's failure row carries, and a sibling
            # worker may have moved `inst.sop_instance_uid` by now (#257).
            self.logger.error(f"  Failed {original_uid}: {e}")
            return RedactionOutcome(ok=False, sop_instance_uid=original_uid,
                                    error=f"{type(e).__name__}: {e}")
        finally:
            # Persistence & Memory Cleanup
            #
            # Not persisted on a failure, and that gate is the whole of
            # #213's "a failed instance is left as it was found".
            # `apply_redaction_to_array` raises *mid-loop*, so zones 1..k-1
            # are already zeroed when zone k fails; persisting made that
            # partial mutation durable on the threads path (3.14t's default)
            # while the processes path (3.12's) mutated a copy and left the
            # instance untouched -- the same failed redaction leaving two
            # different sidecars depending on the interpreter. Without the
            # persist, the unconditional `discard_pixel_data()` below drops
            # the mutated array and the next `get_pixel_data()` reloads the
            # original through the loader.
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
            #
            # `modified` narrows #213's condition; it does not replace it.
            # `persist_pixel_data` has no deduplication -- it hashes,
            # writes a frame and re-points the loader every time it is
            # called with a resident array -- so persisting a swap that
            # never happened appended a frame nothing referenced, and only
            # `compact()` ever noticed. That is the same "attested without
            # being earned" defect as the null attributes, in the sidecar
            # instead of in the graph (#235).
            if (modified and not failed and self.store_backend
                    and hasattr(self.store_backend, 'persist_pixel_data')):
                try:
                    self.store_backend.persist_pixel_data(inst)
                except Exception as pe:
                    self.logger.error(f"Failed to persist swap for {inst.sop_instance_uid}: {pe}")

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
            verbose: bool = False):
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

        # Check matches in store
        targets = []
        if serial == "*":
            # Wildcard: Apply to ALL machines
            for sn_key in self.index._index:
                targets.extend(self.index.get_by_machine(sn_key))
        else:
            # Exact Match
            targets = self.index.get_by_machine(serial)

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

        valid_rois = []
        for zone in zones:
            if isinstance(zone, list):
                # Legacy/Simplified format: zone IS the ROI
                roi = zone
            else:
                roi = zone.get("roi")  # Expected [r1, r2, c1, c2]

            if roi and len(roi) == 4:
                valid_rois.append(tuple(roi))
            else:
                self.logger.warning(f"Invalid ROI format in config: {roi}")

        if valid_rois:
            self.redact_machine_instances(
                serial,
                valid_rois,
                targets=targets,
                show_progress=show_progress,
                verbose=verbose)

    def redact_machine_instances(
            self,
            machine_sn: str,
            rois: List[tuple],
            targets: List[Instance] = None,
            show_progress: bool = True,
            verbose: bool = False,
            force: bool = False):
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

        Raises:
            RedactionError: If any instance's zone could not be applied.
                Raised at the end of the pass, for the same reasons as
                `Session.redact()`: the instances that could be redacted
                are redacted and every failure is already an `ERROR` row.
                This is the serial path, so it must answer the same
                question the parallel one does -- it is public, it is what
                `process_machine_rules` calls, and a failure here left the
                burned-in identifier in the pixels just as silently (#213).

        Returns:
            None. The signature is unchanged;
            `test_redaction_optimization.py` mocks this method and asserts
            on the call, not the result.
        """
        if targets is None:
            targets = self.index.get_by_machine(machine_sn)

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

        for inst in tqdm(
                targets,
                desc=f"Redacting {machine_sn}",
                unit="img",
                disable=not show_progress):
            original_uid = inst.sop_instance_uid  # Capture before mutation
            failed = False
            # Bound before the `try`: every `continue` below and the
            # exception path reach the `finally`, which reads it.
            modified = False
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

                # Safety: Invalidates current hash since we are about to modify.
                # If persist/save fails later, we don't want to match the Old Hash.
                inst._pixel_hash = None

                # One call, the whole zone list. See the note in
                # `execute_redaction_task`: a per-zone loop here kept only
                # the last zone's work on a reloaded instance (#229).
                modified = self._redact_instance_pixels(inst, arr, rois)

                if modified:
                    self._apply_redaction_flags(inst)
                    inst.regenerate_uid()
                    # Mark as redacted with this hash
                    inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
                    # Force Dirty to persist metadata update
                    inst.mark_modified()
                    applied += 1
                    self.logger.debug(f"  Modified {inst.sop_instance_uid}")

            except Exception as e:
                # Broad on purpose, and a missing-argument `TypeError` is
                # audited here like any other failure -- see the note in
                # `execute_redaction_task` (#217).
                failed = True
                failures.append(
                    (original_uid,
                     f"Redaction failed for {original_uid}: "
                     f"{type(e).__name__}: {e}"))
                self.logger.error(f"  Failed {inst.sop_instance_uid}: {e}")
            finally:
                # OPTIMIZATION: Release memory immediately after processing
                # If modified, we MUST persist pixels to sidecar, otherwise discard_pixel_data returns False (unsafe)
                # We check for store_backend availability.
                #
                # Not on a failure: zones before the one that raised are
                # already zeroed, and persisting them makes a partial
                # redaction durable. See `execute_redaction_task`'s `finally`
                # for the whole argument (#213).
                #
                # Not when nothing was modified either, and this path had
                # the defect just as the parallel one did: measured on
                # `84113ab`, an off-image rule grew the sidecar 17 -> 34
                # bytes and left `instance_blobs` pointing at the second
                # copy, orphaning the first. `persist_pixel_data` does not
                # deduplicate (#235).
                if (modified and not failed and self.store_backend
                        and hasattr(self.store_backend, 'persist_pixel_data')):
                    # We only strictly NEED to persist if we hold dirty pixels in memory.
                    # But persist_pixel_data handles checks (returns if no pixels).
                    try:
                        self.store_backend.persist_pixel_data(inst)
                    except Exception as pe:
                        self.logger.error(
                            f"Failed to persist swap for {
                                inst.sop_instance_uid}: {pe}")

                # `discard_pixel_data`, not `unload_pixel_data`: dropping the
                # resident array is the INTENT here, not an optimisation. On a
                # failed redaction it is a partially-zeroed array that must go
                # so the next `get_pixel_data()` reloads the original through
                # the loader, and `unload_pixel_data()` now refuses exactly
                # that case (#293). Byte-for-byte the pre-#293 behaviour.
                inst.discard_pixel_data()

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
                    "shape %s: %s", tuple(roi), arr.shape, exc)
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
        current_type = inst.attributes.get("0008,0008", [])
        if isinstance(current_type, str):
            current_type = [current_type]

        # Ensure 'DERIVED' is the first value (Value 1)
        new_type = ["DERIVED"] + [x for x in current_type if x != "ORIGINAL" and x != "DERIVED"]
        # Ensure we have at least 'PRIMARY' or 'SECONDARY' as Value 2
        if len(new_type) < 2:
            new_type.append("SECONDARY")

        inst.set_attr("0008,0008", new_type)

        # 2. Burned In Annotation (0028,0301) -> NO
        inst.set_attr("0028,0301", "NO")

        # 3. Derivation Description (0008,2111)
        inst.set_attr("0008,2111", "Isocenter Pixel Redaction: Burned-in PHI removed")

        # 4. Derivation Code Sequence (0008,9215)
        # Code 113062: Pixel Data modification
        seq = DicomSequence(tag="0008,9215")
        item = DicomItem()
        item.set_attr("0008,0100", "113062")
        item.set_attr("0008,0102", "DCM")
        item.set_attr("0008,0104", "Pixel Data modification")
        seq.items.append(item)

        inst.sequences["0008,9215"] = seq
