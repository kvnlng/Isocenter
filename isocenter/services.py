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
from typing import Dict, List
from tqdm import tqdm
import numpy as np

from .entities import Instance, DicomItem, DicomSequence
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

    def prepare_redaction_tasks(self, machine_rules: dict, verbose: bool = False) -> List[dict]:
        """
        Generates a list of fine-grained tasks (dicts) from a single machine rule.

        Each task represents one instance to be redacted. Used for distributing
        work across parallel workers.

        Args:
            machine_rules (dict): Configuration rule containing "serial_number" and "redaction_zones".
            verbose (bool): If True, logs skips and warnings.

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

        # Compute Hash
        rois_stable = sorted(valid_rois)
        config_str = json.dumps({"serial": serial, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        # Create Tasks
        tasks = []
        for inst in targets:
            tasks.append({
                "instance": inst,
                "rois": valid_rois,
                "config_hash": config_hash,
                "machine_sn": serial
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
            dict: A mutation dictionary containing changes to apply in the main process, or None.
        """
        inst = task["instance"]
        original_uid = inst.sop_instance_uid  # Capture before mutation
        rois = task["rois"]
        config_hash = task["config_hash"]

        try:
            # Optimized: Skip if already redacted with same config
            current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

            if current_hash == config_hash:
                return None

            # Triggers Lazy Load from disk
            arr = inst.get_pixel_data()

            if arr is None:
                return None

            modified = False
            for roi in rois:
                if self._apply_roi_to_instance(inst, arr, roi):
                    modified = True

            if modified:
                self._apply_redaction_flags(inst)
                inst.regenerate_uid()
                # Mark as redacted with this hash
                inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
                inst._dirty = True

                # CRITICAL: Persist modified pixel data to sidecar (generate new Loader)
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
                "sop_uid": inst.sop_instance_uid,  # New UID
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
            return mutation

        except Exception as e:
            traceback.print_exc()
            self.logger.error(f"  Failed {inst.sop_instance_uid}: {e}")
            return None
        finally:
            # Persistence & Memory Cleanup
            if self.store_backend and hasattr(self.store_backend, 'persist_pixel_data'):
                try:
                    self.store_backend.persist_pixel_data(inst)
                except Exception as pe:
                    self.logger.error(f"Failed to persist swap for {inst.sop_instance_uid}: {pe}")

            inst.unload_pixel_data()

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
            verbose: bool = False):
        """
        Applies a LIST of ROIs to all images from the specified machine.

        Optimized to iterate images ONCE per machine rule, applying all ROIs in a single pass.

        Args:
            machine_sn (str): The serial number (for logging/auditing).
            rois (List[tuple]): List of (y1, y2, x1, x2) ROIs.
            targets (List[Instance], optional): Pre-filtered list of instances.
            show_progress (bool): If True, shows progress bar.
        """
        if targets is None:
            targets = self.index.get_by_machine(machine_sn)

        self.logger.info(f"Redacting {len(targets)} images for {machine_sn} ({len(rois)} zones)...")

        if self.store_backend and targets:
            self.store_backend.log_audit(
                action_type="REDACTION",
                entity_uid=machine_sn,
                details=f"Redacting {len(targets)} images with {len(rois)} zones"
            )

        # 1. Compute Hash for this Config
        # We assume rois list fully captures the intent (zones)
        # Sort to ensure stability if zones are re-ordered
        rois_stable = sorted(rois)
        config_str = json.dumps({"serial": machine_sn, "rois": rois_stable}, sort_keys=True)
        config_hash = hashlib.md5(config_str.encode('utf-8')).hexdigest()

        for inst in tqdm(
                targets,
                desc=f"Redacting {machine_sn}",
                unit="img",
                disable=not show_progress):
            try:
                # Optimized: Skip if already redacted with same config
                current_hash = inst.attributes.get("_ISOCENTER_REDACTION_HASH")

                # DEBUG: Log hashes
                # if verbose and current_hash:
                #    self.logger.debug(f"DEBUG: {inst.sop_instance_uid} Current: {current_hash} vs New: {config_hash}")

                if current_hash == config_hash:
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

                modified = False
                for roi in rois:
                    if self._apply_roi_to_instance(inst, arr, roi):
                        modified = True

                if modified:
                    self._apply_redaction_flags(inst)
                    inst.regenerate_uid()
                    # Mark as redacted with this hash
                    inst.attributes["_ISOCENTER_REDACTION_HASH"] = config_hash
                    # Force Dirty to persist metadata update
                    inst._dirty = True
                    self.logger.debug(f"  Modified {inst.sop_instance_uid}")

            except Exception as e:
                self.logger.error(f"  Failed {inst.sop_instance_uid}: {e}")
            finally:
                # OPTIMIZATION: Release memory immediately after processing
                # If modified, we MUST persist pixels to sidecar, otherwise unload_pixel_data returns False (unsafe)
                # We check for store_backend availability.
                if self.store_backend and hasattr(self.store_backend, 'persist_pixel_data'):
                    # We only strictly NEED to persist if we hold dirty pixels in memory.
                    # But persist_pixel_data handles checks (returns if no pixels).
                    try:
                        self.store_backend.persist_pixel_data(inst)
                    except Exception as pe:
                        self.logger.error(
                            f"Failed to persist swap for {
                                inst.sop_instance_uid}: {pe}")

                inst.unload_pixel_data()

    @staticmethod
    def apply_redaction_to_array(arr: np.ndarray, rois: List[tuple]) -> bool:
        """
        Applies a list of ROIs to the pixel array in place.

        Args:
            arr (np.ndarray): The pixel array to modify.
            rois (List[tuple]): List of (y1, y2, x1, x2) regions.

        Returns:
            bool: True if any modification was applied.
        """
        modified = False

        ndim = len(arr.shape)
        # Default to the last two dimensions (standard grayscale/planar).
        row_dim = ndim - 2
        col_dim = ndim - 1

        if ndim >= 3 and arr.shape[-1] in [3, 4]:
            # RGB/RGBA interleaved: (..., Rows, Cols, Channels)
            row_dim = ndim - 3
            col_dim = ndim - 2

        rows = arr.shape[row_dim]
        cols = arr.shape[col_dim]

        # Writeability is the caller's responsibility: this returns a bool,
        # so it has no way to hand a copy back. Both callers
        # (_apply_roi_to_instance and the export worker) copy first. A
        # read-only array reaching here now raises below rather than being
        # silently skipped.

        for roi in rois:
            try:
                r1, r2, c1, c2 = [int(v) for v in roi]

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
                get_logger().error(
                    "Redaction zone %s could not be applied to an array of "
                    "shape %s: %s", tuple(roi), arr.shape, exc)
                raise

        return modified

    def _apply_roi_to_instance(self, inst: Instance, arr, roi: tuple) -> bool:
        """
        Applies a single ROI to the pixel array in place.

        Wrapper around static apply_redaction_to_array for instance management.
        """
        if not arr.flags.writeable:
            arr = arr.copy()
            inst.set_pixel_data(arr)

        return self.apply_redaction_to_array(arr, [roi])

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
