import gc
import os
import re
import json
import datetime
import concurrent.futures
from collections import Counter
from typing import (List, Union, Dict, Any, Optional, Set, Tuple,
                    NamedTuple)

import yaml
from tqdm import tqdm

from .io_handlers import (DicomImporter, DicomExporter, ExportContext,
                          SidecarPixelLoader, SidecarWaveformLoader,
                          export_folder_names)
from .store import DicomStore
from .services import RedactionService
from .config_manager import ConfigLoader
from .privacy import PhiInspector, PhiFinding, PhiReport
from .logger import configure_logger, get_logger
from .reporting import ComplianceReport, get_renderer
from .manifest import Manifest, ManifestItem, generate_manifest_file
from .persistence import SqliteStore
from .crypto import KeyManager
from .reversibility import ReversibilityService
from .persistence_manager import PersistenceManager
from .parallel import run_parallel, _env_int
from .configuration import IsocenterConfiguration, FlowList
from .entities import PhiStatus, clone_sequences, resolve_item_path
from .profiles import PRIVACY_PROFILES
from . import pixel_analysis
from .automation import ConfigAutomator

def scan_worker(args):
    """
    Worker function for parallel PHI scanning.
    Args:
        args: Tuple of (db_path, patient_id, config_source, remove_private)
              OR (patient_obj, config_source, remove_private)

    Returns: List[PhiFinding] (WITHOUT entities)
    """
    patient = None

    # Check for Object Passing (Legacy/In-Memory/Tests)
    # If first arg is NOT a string (it's a Patient object)
    if len(args) >= 1 and not isinstance(args[0], str):
        if len(args) == 3:
            patient, config_source, remove_private = args
        else:
            patient, config_source = args
            remove_private = True

    # Check for DB Loading (Large Scale / Production)
    elif len(args) == 4 and isinstance(args[0], str) and isinstance(args[1], str):
        db_path, patient_id, config_source, remove_private = args
        # Rehydrate
        store = SqliteStore(db_path)
        patient = store.load_patient(patient_id)

    if not patient:
        return []



    if isinstance(config_source, dict):
        inspector = PhiInspector(config_tags=config_source, remove_private_tags=remove_private)
    elif isinstance(config_source, str) or config_source is None:
        inspector = PhiInspector(config_path=config_source, remove_private_tags=remove_private)
    else:
        inspector = PhiInspector()

    findings = inspector.scan_patient(patient)

    # Strip heavy entity objects before returning across process boundary
    for f in findings:
        f.entity = None

    return findings




def _verify_worker(args):
    """
    Worker for pixel verification.
    Args:
        args: Tuple(Instance, Equipment, List[Rules])
    """
    from .verification import RedactionVerifier
    instance, equipment, rules = args
    if not instance:
        return []

    verifier = RedactionVerifier(rules)
    return verifier.verify_instance(instance, equipment)


RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "resources")

# The header written above every scaffolded config.
_CONFIG_HEADER = """# Isocenter Privacy Configuration (v2.0)
# ==========================================
#
#
# privacy_profile: "basic"
#   - Standard profile handling common PHI (Name, ID, etc).
#   - Set to "none" for manual control.
#
# phi_tags:
#   - Define custom overrides here.
#   - Actions: KEEP, REMOVE, EMPTY, REPLACE, JITTER (SHIFT)
#
# date_jitter:
#   - Range of days to shift dates by (negative = into past).
#
# remove_private_tags:
#   - If true, removes all odd-group tags except Isocenter Metadata.
#
#
"""


def _load_redaction_knowledge_base() -> List[Dict[str, Any]]:
    """Machine redaction rules shipped with the package, keyed by serial."""
    path = os.path.join(RESOURCES_DIR, "redaction_rules.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f).get("machines", [])
    except (OSError, json.JSONDecodeError) as exc:
        get_logger().warning(
            "Could not read the redaction knowledge base at %s: %s", path, exc)
        return []


def _load_ctp_rules() -> List[Dict[str, Any]]:
    """CTP-derived rules, matched by manufacturer and model rather than serial.

    YAML is preferred when present; the shipped copy is JSON.
    """
    yaml_path = os.path.join(RESOURCES_DIR, "ctp_rules.yaml")
    path = yaml_path if os.path.exists(yaml_path) else os.path.join(
        RESOURCES_DIR, "ctp_rules.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) if path.endswith('.yaml') else json.load(f)
        return (data or {}).get("rules", [])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        get_logger().warning("Failed to load CTP rules: %s", exc)
        return []


def _match_machine_rule(equipment, kb_machines, ctp_rules):
    """The first knowledge-base entry describing this machine, or None.

    Priority is deliberate and behaviour-preserving: an exact serial
    number beats a fuzzy manufacturer/model match from CTP, which in turn
    beats a model-only match. A serial identifies one scanner; a model
    match is an educated guess about a family of them.
    """
    for rule in kb_machines:
        if rule.get("serial_number") == equipment.device_serial_number:
            return rule

    matched = _match_ctp_rule(equipment, ctp_rules)
    if matched:
        return matched

    return _match_kb_by_model(equipment, kb_machines)


def _match_ctp_rule(equipment, ctp_rules):
    """CTP's containment match on manufacturer and model."""
    eq_man = (equipment.manufacturer or "").lower()
    eq_mod = (equipment.model_name or "").lower()

    for rule in ctp_rules:
        r_man = rule.get("manufacturer", "").lower()
        r_mod = rule.get("model_name", "").lower()
        if not (r_man and r_mod):
            continue
        if r_man in eq_man and r_mod in eq_mod:
            matched = rule.copy()
            matched["serial_number"] = equipment.device_serial_number
            condition = matched.pop("_ctp_condition", None)
            if condition:
                matched["comment"] = f"Auto-matched from CTP. Condition: {condition}"
            else:
                matched["comment"] = (
                    f"Auto-matched from CTP Knowledge Base "
                    f"({rule.get('manufacturer')} {rule.get('model_name')})")
            return matched
    return None


def _match_kb_by_model(equipment, kb_machines):
    """Model-name match against the internal KB, ignoring serial."""
    for rule in kb_machines:
        if rule.get("model_name") != equipment.model_name:
            continue
        manufacturer = rule.get("manufacturer")
        if manufacturer and manufacturer != equipment.manufacturer:
            continue
        matched = rule.copy()
        matched["serial_number"] = equipment.device_serial_number
        matched["comment"] = (
            f"Auto-matched from Model Knowledge Base ({equipment.model_name})")
        return matched
    return None


def _default_action_for_tag(tag: str) -> str:
    """The research-friendly default action for a PHI tag.

    Only three tags deviate from REMOVE. Earlier versions also branched on
    "Date"/"Time"/"ID" appearing in the tag's name, but every one of those
    branches also chose REMOVE, so they never changed an outcome.
    """
    if tag == "0008,0020":      # Study Date: keep intervals, lose the date
        return "JITTER"
    if tag in ("0010,0040", "0010,1010"):   # Sex, Age: research-relevant
        return "KEEP"
    return "REMOVE"


def _render_config_yaml(data: Dict[str, Any]) -> str:
    """Renders the config dict as the commented YAML users actually edit.

    PyYAML cannot emit comments, so `comment:` keys are dumped as data and
    rewritten into `#` lines afterwards. That is why comments are
    flattened to one line first: a multi-line value would produce YAML
    that this pass turns into a broken comment block.
    """
    for machine in data.get("machines", []):
        comment = machine.get("comment")
        if isinstance(comment, str):
            machine["comment"] = re.sub(
                r'\s+', ' ', comment.replace("\n", " ").replace("\r", "")).strip()

        zones = machine.get("redaction_zones")
        if isinstance(zones, list):
            machine["redaction_zones"] = FlowList(
                FlowList(z) if isinstance(z, list) else z for z in zones)

    yaml_content = yaml.dump(
        data, sort_keys=False, default_flow_style=False, width=float("inf"))

    rendered = []
    for line in yaml_content.splitlines():
        match = re.search(r'^(\s*)comment:\s*(.*)$', line)
        if match:
            indent, content = match.group(1), match.group(2).strip()
            if content.startswith("'") and content.endswith("'"):
                content = content[1:-1].replace("''", "'")
            elif content.startswith('"') and content.endswith('"'):
                content = content[1:-1].replace('\\"', '"')
            rendered.append(f"{indent}# {content}")
            continue

        # A blank line before each machine keeps the list readable.
        if (line.strip().startswith("- ") and rendered
                and rendered[-1].strip() != ""):
            rendered.append("")
        rendered.append(line)

    return _CONFIG_HEADER + "\n" + "\n".join(rendered) + "\n"


class _ExportOptions(NamedTuple):
    """Everything the export plan needs beyond the store itself.

    Bundled because all four travel together down three levels of the
    walk, and a parameter list that long stops being read.
    """
    folder: str
    identifying_uids: Optional[Set[str]]
    allowed_uids: Optional[Set[str]]
    use_compression: bool


def _excluded(options, patient, study, series, instance) -> bool:
    """Whether an instance is filtered out of the export, and why in the log.

    Two independent filters, both matching at every level of the
    hierarchy: the safety scan excludes anything still carrying an
    identifier, and a subset includes only what the caller selected.
    `None` means the filter is not in use, which is not the same as an
    empty set -- that means it is in use and matched nothing.
    """
    uids = _uid_path(patient, study, series, instance)

    if options.identifying_uids is not None and any(
            uid in options.identifying_uids for uid in uids):
        get_logger().warning(
            "Skipping %s: it or one of its parents still carries identifiers.",
            instance.sop_instance_uid)
        return True

    if options.allowed_uids is not None and not any(
            uid in options.allowed_uids for uid in uids):
        return True

    return False


def _uid_path(patient, study, series, instance) -> Tuple[str, str, str, str]:
    """The four UIDs locating one instance in the hierarchy.

    Both export filters -- the safety scan and the subset -- match against
    every level, so a rule written for a study applies to its images
    without having to be restated for each one.
    """
    return (patient.patient_id, study.study_instance_uid,
            series.series_instance_uid, instance.sop_instance_uid)


def _patient_attributes(patient) -> Dict[str, Any]:
    """Patient-level tags stamped onto every exported instance."""
    attributes = {
        "0010,0010": patient.patient_name,
        "0010,0020": patient.patient_id,
    }
    if getattr(patient, 'birth_date', None):
        attributes["0010,0030"] = patient.birth_date
    if getattr(patient, 'sex', None):
        attributes["0010,0040"] = patient.sex
    return attributes


def _study_attributes(study) -> Dict[str, Any]:
    """Study-level tags stamped onto every exported instance."""
    attributes = {
        "0020,000D": study.study_instance_uid,
        "0008,0020": study.study_date,
    }
    if getattr(study, 'study_time', None):
        attributes["0008,0030"] = study.study_time
    if getattr(study, 'accession_number', None):
        attributes["0008,0050"] = study.accession_number
    return attributes


def _series_attributes(series) -> Dict[str, Any]:
    """Series-level tags stamped onto every exported instance."""
    attributes = {
        "0020,000E": series.series_instance_uid,
        "0008,0060": series.modality,
        "0020,0011": str(series.series_number),
    }
    if getattr(series, 'series_description', None):
        attributes["0008,103E"] = series.series_description
    return attributes


def _uids_from_frame(frame) -> Set[str]:
    """The UIDs a subset DataFrame selects, at the most precise level present.

    Only one column is read, deliberately. A frame filtered down to the CT
    series of a patient still carries that patient's ID in every row, so
    adding PatientID to the set would pull the MR series back in and undo
    the filter the caller asked for.
    """
    for column in ("SOPInstanceUID", "SeriesInstanceUID",
                   "StudyInstanceUID", "PatientID"):
        if column in frame.columns:
            return set(frame[column].tolist())
    return set()


def _report_phi_findings(findings) -> None:
    """Prints what the pre-export scan found, and how to configure it away."""
    counts, examples, descriptions = Counter(), {}, {}
    for finding in findings:
        tag = finding.tag or finding.field_name
        counts[tag] += 1
        examples.setdefault(tag, str(finding.value))
        descriptions[tag] = finding.reason

    print("\nSafety Scan Found Issues")
    print("The following tags still carry identifiers:")
    print(f"{'Tag':<15} {'Description':<30} {'Count':<10} {'Examples'}")
    print("-" * 80)
    for tag, count in counts.items():
        print(f"{tag:<15} {descriptions[tag][:28]:<30} {count:<10} "
              f"{examples[tag][:30]}")

    _print_suggested_config(counts)


# Names for tags the shipped `resources/phi_tags.json` does not list.
# `_scaffold_phi_tags` needs them because they are the research-friendly
# defaults a scaffold should mention, and `_suggested_tag_name` needs them
# because Study Date is one of the tags a safety scan flags most often.
# Kept in one place so the two cannot drift into disagreeing about what a
# tag is called.
_SUPPLEMENTAL_TAG_NAMES = {
    "0008,0020": "Study Date",
    "0010,0040": "Patient Sex",
    "0010,1010": "Patient Age",
}


def _print_suggested_config(counts) -> None:
    """Prints a config fragment removing every tag the scan flagged.

    YAML, and specifically the shape `create_config()` writes, so the
    output can be pasted into the file the user already has. This is the
    only actionable instruction in the safety report, and it used to be
    JSON with `//` comments and a trailing comma -- neither valid JSON nor
    the format `ConfigLoader` reads, since user-facing configs are YAML
    only. Both defects came from the same place: JSON has no comments, so
    the counts had to be smuggled in as `//`.
    """
    print("\nSuggested Config Update:")
    print("Add the following rules to your config to resolve these:")
    print()
    print("phi_tags:")
    for tag, count in counts.items():
        rule = {tag: {"name": _suggested_tag_name(tag), "action": "REMOVE"}}
        # Dumped per tag rather than as one mapping so the count can sit
        # above its own entry. yaml.dump owns the quoting -- a tag key
        # contains a comma, and hand-rolling that is how the previous
        # version produced a document nothing could read.
        block = yaml.dump(rule, sort_keys=False, default_flow_style=False)
        print(f"  # Found {count} times")
        for line in block.splitlines():
            print(f"  {line}")


def _suggested_tag_name(tag: str) -> str:
    """A readable name for a flagged tag, from the shipped PHI defaults.

    This recognised three tags by hand and called everything else
    `unknown_tag`, while `resources/phi_tags.json` already named more --
    two spellings of the same mapping, with the smaller one facing the
    user at the exact moment they need it to be right.

    Falls back to the tag itself rather than to `unknown_tag`: the name is
    a comment to the reader, and a tag repeated is at least true, where
    three rules all called `unknown_tag` are indistinguishable.
    """
    try:
        names = dict(ConfigLoader.load_phi_config())
    except (OSError, ValueError):
        names = {}
    for extra_tag, extra_name in _SUPPLEMENTAL_TAG_NAMES.items():
        names.setdefault(extra_tag, extra_name)

    entry = names.get(tag)
    if isinstance(entry, dict):
        return str(entry.get("name") or tag)
    if isinstance(entry, str) and entry:
        return entry
    return tag


class LockingResult(list):
    """
    A list subclass that suppresses verbose REPL output for large datasets.
    """

    def __repr__(self):
        return f"<LockingResult: {len(self)} instances secured>"


def _redaction_worker_count() -> int:
    """How many workers to redact pixels with.

    Half the CPUs, capped at eight. Each worker holds a decoded image, so
    this cap is a memory ceiling rather than a throughput choice --
    `run_parallel`'s own default of one worker per CPU has exhausted
    memory on large studies.

    `ISOCENTER_MAX_WORKERS` overrides it. A malformed value used to raise
    inside the handler that swallowed everything, so a typo in a shell
    profile turned redaction into a no-op that reported success; now it
    warns and falls back to the default.
    """
    override = _env_int("ISOCENTER_MAX_WORKERS")
    if override is not None:
        return max(1, override)
    return max(1, min((os.cpu_count() or 1) // 2, 8))


class DicomSession:
    """
    The Main Facade for the Isocenter library.

    Manages the lifecycle of the DicomStore including:
    - Loading/Saving session state from SQLite.
    - Ingesting DICOM files.
    - Managing Configuration and Rules.
    - Auditing for PHI.
    - Redaction and Anonymization.
    - Exporting cleaned data.
    """

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    def __init__(self, persistence_file=None):
        """
        Initialize the DicomSession.

        Args:
            persistence_file (str): Path to the SQLite database file for session persistence.
                                    Defaults to "isocenter.db".
        """
        configure_logger()
        self.persistence_file = persistence_file or os.getenv("ISOCENTER_DB_PATH", "isocenter.db")

        # Check existence before SqliteStore potentially creates it
        db_exists = os.path.exists(self.persistence_file)

        self.store_backend = SqliteStore(self.persistence_file)
        self.persistence_manager = PersistenceManager(self.store_backend)

        # Hydrate memory from DB
        self.store = DicomStore()

        if db_exists:
            print(f"Loading session from {self.persistence_file}...")
        else:
            print(f"Initializing new session at {self.persistence_file}...")

        self.store.patients = self.store_backend.load_all()

        # Initialize Configuration Object
        self.configuration = IsocenterConfiguration()

        # Reversibility
        self.key_manager = None
        self.reversibility_service = None

        if os.path.exists("isocenter.key"):
            self.enable_reversible_anonymization("isocenter.key")

        # Shared Global Executor for Process Consistency
        self._executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=None)  # Default: CPU * 1.5

        if db_exists:
            print(f"Loaded session from {self.persistence_file}")

        get_logger().info(f"Session started. {len(self.store.patients)} patients loaded.")

    def __enter__(self) -> "DicomSession":
        """Support `with DicomSession(...) as session:`.

        `close()` releases a ProcessPoolExecutor and two threads holding
        sqlite handles. Without this, forgetting it leaks worker
        subprocesses for the life of the process.
        """
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Always close, including when the body raised.

        Returns None so exceptions propagate -- a session manager that
        swallowed them would hide the caller's failure.
        """
        self.close()

    def close(self):
        """
        Cleanly shuts down the session, stopping background threads and
        flushing queues.

        Runs all three shutdown steps -- the persistence-manager thread,
        the audit thread owning the sqlite connection, and the
        ProcessPoolExecutor -- even if an earlier step raises. Without
        this, a single exception (e.g. a failed flush) would abort the
        sequence partway through and leak whatever hadn't been shut down
        yet, most notably the executor's worker subprocesses, for the
        life of the interpreter.

        If more than one step fails, the first failure is raised (later
        failures are logged, not swallowed) since it is usually the root
        cause; a later step failing on an already-broken resource is
        typically a consequence of the first failure, not new information.
        """
        print("Closing session persistence...")
        first_exception = None

        def _run_step(step):
            nonlocal first_exception
            try:
                step()
            except Exception as exc:  # pylint: disable=broad-except
                get_logger().error(f"Error during session close(): {exc}", exc_info=True)
                if first_exception is None:
                    first_exception = exc

        if hasattr(self, 'persistence_manager'):
            _run_step(self.persistence_manager.shutdown)
        if hasattr(self, 'store_backend'):
            _run_step(self.store_backend.stop)  # Stops audit thread

        if hasattr(self, '_executor'):
            print("Shutting down process pool...")
            _run_step(lambda: self._executor.shutdown(wait=True))

        if first_exception is not None:
            raise first_exception

    def save(self, sync: bool = False):
        """
        Persists the current session state to the database.
        :param sync: If True, blocks until save is complete.
        """
        if sync and hasattr(self, 'store_backend'):
            get_logger().info("Saving session (Synchronous)...")
            self.store_backend.save_all(
                self.store.patients, prune_absent_patients=True)
        elif hasattr(self, 'persistence_manager'):
            # The session owns the whole store, so rows for patients it no
            # longer holds are stale -- including the pre-anonymisation row
            # of a patient whose identifier has since changed.
            self.persistence_manager.save_async(
                self.store.patients, prune_absent_patients=True)

    def _restart_executor(self, max_workers=None):
        """
        Restarts the internal process pool executor, potentially with fewer workers.
        Useful for recovering from BrokenProcessPool errors (OOM).
        """
        get_logger().warning(f"Restarting ProcessPoolExecutor (max_workers={max_workers})...")
        if self._executor:
            try:
                # Force kill old processes if they are stuck/broken
                self._executor.shutdown(wait=False, cancel_futures=True)
            except (RuntimeError, OSError) as exc:
                # The executor is being replaced regardless; a failure to
                # shut the old one down is worth a line in the log, not a
                # crash.
                get_logger().debug("Could not shut down prior executor: %s", exc)

        # Re-init
        self._executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

    def release_memory(self):
        """
        Attempts to release memory by unloading cached pixel and waveform
        data from all instances.

        Safe to call: each unload happens only when the data can be
        restored (from disk or the sidecar), so nothing is discarded.
        Useful after running extensive redaction or export operations.

        Waveforms matter here as much as pixels: samples are cached as
        int16 of shape (num_samples, num_channels), which is ~80 KB for a
        10-second 12-lead but ~104 MB for a 24-hour 3-channel Holter.
        """
        get_logger().info("Releasing memory (RAM cleanup)...")
        count = 0
        pixels_freed = 0
        waveforms_freed = 0
        instances_freed = 0

        # Count total instances first for progress bar
        total_instances = sum(len(se.instances)
                              for p in self.store.patients for st in p.studies for se in st.series)

        if total_instances == 0:
            return

        with tqdm(total=total_instances, desc="Releasing Memory", unit="inst") as pbar:
            for p in self.store.patients:
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            count += 1
                            # Both unloads report True when there was
                            # nothing cached, so the return value alone
                            # cannot tell "released" from "there was
                            # none". Counting it as freed is how this
                            # used to report every instance in a session
                            # holding nothing as reclaimed -- the same
                            # false assurance as not freeing at all.
                            had_pixels = inst.pixel_array is not None
                            had_waveform = inst.waveform_array is not None

                            gave_pixels = inst.unload_pixel_data() and had_pixels
                            gave_waveform = (inst.unload_waveform_data()
                                             and had_waveform)

                            pixels_freed += 1 if gave_pixels else 0
                            waveforms_freed += 1 if gave_waveform else 0
                            if gave_pixels or gave_waveform:
                                instances_freed += 1
                            pbar.update(1)

        get_logger().info(
            f"Memory release complete. Freed {instances_freed}/{count} "
            f"instances (pixels: {pixels_freed}, waveform samples: "
            f"{waveforms_freed}).")
        if instances_freed > 0:
            print(f"Memory Cleanup: Released {pixels_freed} pixel arrays and "
                  f"{waveforms_freed} waveform arrays from RAM.")

    def compact(self):
        """
        Manually triggers Sidecar Compaction to reclaim disk space.
        Rewrites the _pixels.bin file, removing orphaned data from deleted or redacted instances.
        WARNING: This is an expensive I/O operation.
        """
        if hasattr(self, 'store_backend'):
            print("Beginning Sidecar Compaction (this may take a while)...")

            # 1. Sync DB so compaction knows true state
            self.save(sync=True)

            # 2. Compact and get updates
            # Returns Dict[sop_instance_uid, (new_offset, new_length)]
            updates = self.store_backend.compact_sidecar()

            # compact_sidecar's uid_map is pixels-only by design: it is keyed
            # by UID alone, so a waveform entry would be handed to a pixel
            # loader. Waveform offsets are re-read from the blob table
            # instead -- they moved in the same rewrite, and a loader left on
            # a pre-compaction offset reads the wrong bytes or runs off the
            # end of the file.
            wave_updates = self.store_backend.get_blob_refs('waveform')

            if not updates and not wave_updates:
                print("Compaction finished (no changes or empty).")
                return

            # 3. Patch In-Memory Instances (Preserve References)
            print(f"Updating {len(updates)} in-memory instances...")
            count = 0

            # Optimization: Pre-check if we have SidecarPixelLoader imported


            # We must traverse the whole graph.
            # DicomStore doesn't index by UID (yet).
            for p in self.store.patients:
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            if inst.sop_instance_uid in updates:
                                new_off, new_len = updates[inst.sop_instance_uid]

                                # Update Loader
                                if inst._pixel_loader and isinstance(
                                        inst._pixel_loader, SidecarPixelLoader):
                                    inst._pixel_loader.offset = new_off
                                    inst._pixel_loader.length = new_len
                                    count += 1

                                # Note: If inst._pixel_loader is None (e.g. loaded from original DICOM file),
                                # it doesn't use sidecar, so no update needed.
                                # If it has pixel_array loaded (RAM), it's fine.
                                # If we unload() later, we need correct loader.
                                # BUT if it has pixel_array, does it have a loader?
                                # persist_pixel_data ensures loader is created.
                                # So if it was persisted, it has a loader.

                            # Waveform loaders are patched from the blob
                            # table, not from `updates`: see the note above.
                            w_ref = wave_updates.get(inst.sop_instance_uid)
                            if w_ref is not None and isinstance(
                                    inst._waveform_loader, SidecarWaveformLoader):
                                inst._waveform_loader.offset = w_ref[0]
                                inst._waveform_loader.length = w_ref[1]
                                count += 1

            print(f"Patched {count} active objects.")

        else:
            print("Persistence backend does not support compaction.")

    def examine(self):
        """Prints a summary of the session contents and equipment."""
        get_logger().info("Generating inventory report.")

        # 1. Object Counts
        n_p = len(self.store.patients)
        n_st = sum(len(p.studies) for p in self.store.patients)
        n_se = sum(len(st.series) for p in self.store.patients for st in p.studies)
        n_i = sum(len(se.instances)
                  for p in self.store.patients for st in p.studies for se in st.series)

        # 2. Equipment Grouping
        eq_counts = {}  # (man, model) -> count

        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    for _ in se.instances:
                        if se.equipment:
                            key = (se.equipment.manufacturer, se.equipment.model_name)
                            eq_counts[key] = eq_counts.get(key, 0) + 1

        print(f"\nInventory Summary:")
        print(f" Patients:  {n_p}")
        print(f" Studies:   {n_st}")
        print(f" Series:    {n_se}")
        print(f" Instances: {n_i}")

        print(f"\nEquipment Inventory:")
        if not eq_counts:
            print(" No equipment metadata found.")
        else:
            for (man, mod), count in sorted(eq_counts.items()):
                print(f" - {man} - {mod} (Count: {count})")

    # =========================================================================
    # INGESTION
    # =========================================================================

    def ingest(self, directory: str):
        """
        Ingests DICOM files from a directory into the session store.

        Recursively scans the provided directory for valid DICOM files.
        Files are parsed and organized into the Patient -> Study -> Series -> Instance hierarchy.
        This operation automatically saves the session state upon completion.

        Args:
            directory (str): The path to the directory containing DICOM files.
        """
        print(f"Ingesting from '{directory}'...")
        # Pass Sidecar Manager for eager pixel writing
        DicomImporter.import_files(
            [directory],
            self.store,
            executor=self._executor,
            sidecar_manager=self.store_backend.sidecar,
            store_backend=self.store_backend)

        self.save(sync=True)

        # Calculate stats
        n_p = len(self.store.patients)
        n_st = sum(len(p.studies) for p in self.store.patients)
        n_se = sum(len(st.series) for p in self.store.patients for st in p.studies)
        n_i = sum(len(se.instances)
                  for p in self.store.patients for st in p.studies for se in st.series)

        print(f"Ingestion complete. Saved session state.")
        print("Summary:")
        print(f"  - {n_p} Patients")
        print(f"  - {n_st} Studies")
        print(f"  - {n_se} Series")
        print(f"  - {n_i} Instances")

    # =========================================================================
    # CONFIGURATION
    # =========================================================================

    def load_config(self, config_file: str):
        """
        Loads a configuration file into memory without applying it.

        This allows the user to validate the configuration or run a preview using
        `preview_config()` before performing any destructive actions.

        Args:
            config_file (str): Path to the YAML or JSON configuration file.
        """
        try:
            get_logger().info(f"Loading configuration from {config_file}...")
            print(f"Loading configuration from {config_file}...")

            # UNIFIED LOAD (v2) - Now loading into IsocenterConfiguration object
            (tags, rules, jitter, remove_private,
             profile) = ConfigLoader.load_unified_config(config_file)

            # Update the configuration object
            self.configuration.phi_tags = tags
            self.configuration.rules = rules
            self.configuration.date_jitter = jitter
            self.configuration.remove_private_tags = remove_private
            self.configuration.config_path = config_file
            self.configuration.privacy_profile = profile

            get_logger().info(
                f"Loaded {len(self.configuration.rules)} machine rules and {len(self.configuration.phi_tags)} PHI tags.")
            print(
                f"Configuration Loaded:\n - {len(self.configuration.rules)} Machine Redaction Rules\n - {len(self.configuration.phi_tags)} PHI Tags")
            print(
                f" - Date Jitter: {
                    self.configuration.date_jitter['min_days']} to {
                    self.configuration.date_jitter['max_days']} days")
            print(f" - Remove Private Tags: {self.configuration.remove_private_tags}")
            print("Tip: Run .audit() to check PHI, or .redact_pixels() to apply redaction.")
        except Exception as e:
            import traceback
            get_logger().error(f"Load failed: {e}")
            print(f"Load failed: {e}")
            print(traceback.format_exc())
            # Reset on failure? OR keep previous?
            # Original behavior was reset.
            self.configuration.rules = []
            self.configuration.phi_tags = {}
            self.configuration.privacy_profile = None

    def preview_config(self):
        """
        Performs a dry-run of the currently loaded configuration.

        Checks the active redaction rules against the current session inventory and
        prints a summary of which instances would be affected (matched) by the rules.
        Does not modify any data.
        """
        if not self.configuration.rules:
            get_logger().warning("No configuration loaded. Use .load_config() first.")
            print("No configuration loaded. Use .load_config() first.")
            return

        print("\n--- Dry Run / Configuration Preview ---")

        # We need the index to check matches
        # We instantiate the service just to query the index, not to modify
        service = RedactionService(self.store, self.store_backend)

        match_count = 0

        for rule in self.configuration.rules:
            serial = rule.get("serial_number", "UNKNOWN")
            model = rule.get("model_name", "Unknown Model")
            zones = rule.get("redaction_zones", [])

            # check matches in store
            targets = service.index.get_by_machine(serial)

            if targets:
                count = len(targets)
                match_count += count
                print(f"MATCH: '{serial}' ({model})")
                print(f"    - Found {count} images in current session.")
                print(f"    - Actions: Will apply {len(zones)} redaction zones.")
            else:
                print(f"NO MATCH: '{serial}'. Rule loaded, but no images found.")

        print(f"\nSummary: Execution will modify approximately {match_count} images.")
        print("---------------------------------------")

    def create_config(self, output_path: str):
        """
        Generates a unified configuration file (scaffold) in YAML format.

        Reads the session inventory, pre-fills redaction rules for any
        machine the shipped knowledge bases recognise, adds default PHI
        tag policy, and writes the result as commented YAML.

        Args:
            output_path (str): Where to write the generated YAML. A
                `.yaml` suffix is appended if missing.
        """
        if not (output_path.endswith(".yaml") or output_path.endswith(".yml")):
            output_path += ".yaml"
            print(f"Note: Appending .yaml extension -> {output_path}")

        machine_rules = self._scaffold_machine_rules()

        data = {
            "version": "2.0",
            "privacy_profile": "basic",
            "phi_tags": self._scaffold_phi_tags(),
            "date_jitter": self.configuration.date_jitter,
            "remove_private_tags": self.configuration.remove_private_tags,
            "machines": machine_rules + self.configuration.rules
        }

        if not machine_rules and not self.configuration.rules:
            print("No machines detected to scaffold.")

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(_render_config_yaml(data))

            get_logger().info(
                "Scaffolded Unified Config to %s (%d new machines)",
                output_path, len(machine_rules))
            print(f"Scaffolded Unified Config to {output_path}")
        except OSError as exc:
            get_logger().error("Failed to write scaffold: %s", exc)

    def _scaffold_machine_rules(self) -> List[Dict[str, Any]]:
        """Builds a redaction rule for every machine not already configured.

        Each machine is matched against the knowledge bases in priority
        order, then annotated with a burned-in-annotation warning if its
        images claim to carry one. Machines that match nothing still get
        an entry, with empty zones for the user to fill in.
        """
        configured_serials = {
            rule.get("serial_number") for rule in self.configuration.rules}

        # Both knowledge bases are read once here. The CTP file was
        # previously opened and parsed inside the per-machine loop, so a
        # cohort with 40 scanners re-read the same 27KB of rules 40 times.
        kb_machines = _load_redaction_knowledge_base()
        ctp_rules = _load_ctp_rules()

        service = RedactionService(self.store)
        scaffolded = []

        for equipment in self.store.get_unique_equipment():
            serial = equipment.device_serial_number
            if not serial or serial in configured_serials:
                continue

            matched = _match_machine_rule(equipment, kb_machines, ctp_rules)
            warning = self._burned_in_warning(service, serial)

            if matched:
                rule = dict(matched)      # never mutate the knowledge base
                if warning:
                    # Append: the KB comment says what to redact, the
                    # warning says the pixels need checking. Both matter.
                    rule["comment"] = f"{rule.get('comment', '')} {warning}".strip()
            else:
                rule = {
                    "manufacturer": equipment.manufacturer or "Unknown",
                    "model_name": equipment.model_name or "Unknown",
                    "serial_number": serial,
                    "redaction_zones": []
                }
                if warning:
                    rule["comment"] = warning

            scaffolded.append(rule)

        return scaffolded

    @staticmethod
    def _burned_in_warning(service, serial_number: str) -> str:
        """Warns when a machine's images declare burned-in annotations.

        (0028,0301) is the scanner's own claim that PHI is drawn into the
        pixels. It is advisory -- absence proves nothing -- but its
        presence means the zones below need checking rather than trusting.
        """
        flagged = sum(
            1 for inst in service.index.get_by_machine(serial_number)
            if isinstance(inst.attributes.get("0028,0301", "NO"), str)
            and "YES" in inst.attributes.get("0028,0301", "NO").upper())

        if not flagged:
            return ""
        return (f"WARNING: {flagged} images have 'Burned In Annotation' "
                f"flag. Verify pixel redaction.")

    def _scaffold_phi_tags(self) -> Dict[str, Any]:
        """The PHI tag section of a scaffolded config.

        Only tags that *deviate* from the basic profile are written. The
        scaffold sets `privacy_profile: basic`, which already removes
        everything listed there, so re-listing a REMOVE tag would add a
        line that changes nothing. What survives is the research-friendly
        defaults: a jittered study date, and age and sex kept.
        """
        phi_tags = dict(self.configuration.phi_tags)
        if not phi_tags:
            try:
                phi_tags = dict(ConfigLoader.load_phi_config())
            except (OSError, ValueError) as exc:
                get_logger().warning("Failed to load default PHI tags: %s", exc)

        for tag, name in _SUPPLEMENTAL_TAG_NAMES.items():
            phi_tags.setdefault(tag, name)

        structured = {}
        for tag, val in phi_tags.items():
            if isinstance(val, dict):
                # Already structured by a loaded config; pass it through.
                structured[tag] = val
                continue

            action = _default_action_for_tag(tag)
            if action == "REMOVE":
                continue
            structured[tag] = {"name": val, "action": action}

        return structured

    # =========================================================================
    # AUDIT & ANALYSIS
    # =========================================================================

    def audit(self, config_path: str = None) -> "PhiReport":
        """
        Scans all patients in the session for potential PHI.

        If `config_path` is provided, it serves as the source of PHI definition tags.
        Otherwise, the currently loaded configuration (`self.configuration.phi_tags`) is used.

        The scan runs in parallel processes for performance.

        Args:
            config_path (str, optional): Path to a configuration file defining PHI tags.

        Returns:
            PhiReport: An object containing valid PHI findings, iterable and exportable.
        """


        # Default to current config
        tags_to_use = self.configuration.phi_tags

        if config_path:
            try:
                t, _, _, _, _ = ConfigLoader.load_unified_config(config_path)
                tags_to_use = t
            except (OSError, ValueError, yaml.YAMLError) as exc:
                # Not a unified v2 config; try it as a plain PHI tag file.
                get_logger().debug(
                    "%s is not a unified config (%s); reading it as PHI tags",
                    config_path, exc)
                tags_to_use = ConfigLoader.load_phi_config(config_path)

        # Uses IsocenterConfiguration derived tags
        inspector = PhiInspector(config_tags=tags_to_use,
                                 remove_private_tags=self.configuration.remove_private_tags)
        if not inspector.phi_tags:
            get_logger().warning("PHI Scan Warning: No PHI tags defined. Scan will find nothing. Check your config.")

        get_logger().info("Scanning for PHI (Parallel)...")

        # Hybrid Approach:
        # Pass lightweight object CLONES to avoid "Assert left > 0" IPC error
        # AND to ensure we audit in-memory (unsaved) changes.
        worker_args = []
        for p in self.store.patients:
            # Strip pixels to reduce size
            light_p = self._make_lightweight_copy(p)
            worker_args.append((light_p, tags_to_use, self.configuration.remove_private_tags))

        results = run_parallel(scan_worker, worker_args, desc="Scanning PHI")

        all_findings = []
        for findings in results:
            all_findings.extend(findings)

        # Rehydrate Entities!
        self._rehydrate_findings(all_findings)
        self._record_scan_results(all_findings)

        get_logger().info(f"PHI Scan Complete. Found {len(all_findings)} issues.")

        return PhiReport(all_findings)

    def phi_status_summary(self) -> Dict[str, Counter]:
        """What the session currently knows about the PHI in each entity.

        Counts are of `PhiStatus`, per level, and reflect the *current*
        state of each entity rather than the last scan's output -- an item
        edited since it was scanned counts as UNSCANNED, because that is
        what it is.

        Series are absent by design: the inspector reports on patients,
        studies and instances only, so a series has never been examined
        and would report UNSCANNED for every session, which reads as a
        gap rather than as "not applicable".

        Returns:
            Dict[str, Counter]: Keyed "patients", "studies", "instances";
            each a Counter of PhiStatus to how many carry it.
        """
        summary = {"patients": Counter(), "studies": Counter(),
                   "instances": Counter()}

        for patient in self.store.patients:
            summary["patients"][patient.phi_status] += 1
            for study in patient.studies:
                summary["studies"][study.phi_status] += 1
                for series in study.series:
                    for instance in series.instances:
                        summary["instances"][instance.phi_status] += 1

        return summary

    def _record_scan_results(self, findings):
        """Writes what the scan concluded onto the entities it scanned.

        Every entity the inspector reports on gets a status: IDENTIFIED
        where a finding names it, CLEARED where the scan looked and found
        nothing. Series are deliberately left alone -- the inspector emits
        findings for patients, studies and instances only, so a series has
        not been examined and must not claim it has.

        The status is stamped at each entity's current revision, so a
        later edit invalidates it. That is why this runs after
        rehydration: it needs the live objects, not the worker copies.
        """
        identified = {f.entity_uid for f in findings if f.entity_uid}

        def record(entity, uid):
            entity.record_phi_status(
                PhiStatus.IDENTIFIED if uid in identified
                else PhiStatus.CLEARED)

        for patient in self.store.patients:
            record(patient, patient.patient_id)
            for study in patient.studies:
                record(study, study.study_instance_uid)
                for series in study.series:
                    for instance in series.instances:
                        record(instance, instance.sop_instance_uid)

    def scan_pixel_content(self, serial_number: str = None) -> "PhiReport":
        """
        Scans instances in the session for burned-in text using OCR.

        Performs "Intelligent Verification":.
        Only scans instances belonging to machines (Serial Numbers) that are present
        in the current configuration. Unconfigured machines are skipped.

        Args:
            serial_number (str, optional): If provided, restricts the scan to ONLY
                                           machines with this serial number.

        Returns:
            PhiReport: A report containing findings of filtered (uncovered) burned-in text.
        """
        get_logger().info("Scanning pixel content for text (OCR)...")
        print("Scanning pixel content for text (OCR)...")

        # Gather all instances with their equipment context
        current_rules = self.configuration.rules

        worker_items = []
        skipped_count = 0

        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    equip = se.equipment
                    if not equip or not equip.device_serial_number:
                        skipped_count += len(se.instances)
                        continue

                    sn = equip.device_serial_number

                    # Filter 1: Must be in Config
                    # We check if we have a rule for this serial
                    matched_rule = None
                    for r in current_rules:
                        if r.get("serial_number") == sn:
                            matched_rule = r
                            break

                    if not matched_rule:
                        skipped_count += len(se.instances)
                        continue

                    # Rule Refinement: Skip if NO ZONES defined (Scaffolded state)
                    # Unless user explicitly wants to scan? No, user req says skip.
                    if not matched_rule.get("redaction_zones"):
                        # Log once per serial?
                        # For now just skip
                        skipped_count += len(se.instances)
                        continue

                    # Filter 2: Explicit User Filter
                    if serial_number and sn != serial_number:
                        continue

                    for inst in se.instances:
                        worker_items.append((inst, equip, current_rules))

        if not worker_items:
            msg = "No matching configured instances found to scan."
            if skipped_count > 0:
                msg += f" (Skipped {skipped_count} unconfigured instances)"
            print(msg)
            return PhiReport([])

        results = run_parallel(_verify_worker, worker_items, desc="OCR Verification")

        all_findings = []
        for r in results:
            all_findings.extend(r)

        print(f"OCR Scan Complete. Found {len(all_findings)} suspicious regions (Uncovered).")
        return PhiReport(all_findings)

    def auto_remediate_config(self, report: "PhiReport") -> int:
        """
        Analyzes the provided OCR report and automatically updates the session's
        configuration to fix detected leaks (by expanding zones or adding new ones).

        Args:
            report (PhiReport): The findings from .scan_pixel_content()

        Returns:
            int: The number of rules updated.
        """
        get_logger().info("Analyzing report for auto-remediation...")

        suggestions = ConfigAutomator.suggest_config_updates(report, self.configuration)

        if not suggestions:
            print("No configuration updates suggested.")
            return 0

        print(f"Generated {len(suggestions)} suggestions for config updates.")

        count = ConfigAutomator.apply_suggestions(self, suggestions)

        if count > 0:
            print(f"Applied {count} updates to in-memory configuration.")
            print("Tip: Run .scan_pixel_content() again to verify fix, then .configuration.save_config() to persist.")

        return count

        return count

    def discover_redaction_zones(self, serial_number: str, sample_size: int = 50, min_confidence: float = 80.0):
        """
        Scans a random sample of instances from a specific machine to discover
        common locations of burned-in text.

        Returns:
            DiscoveryResult: Object containing all detected text candidates.
            Call .to_zones() on the result to get grouped redaction zones.
        """
        from isocenter.discovery import DiscoveryResult, DiscoveryCandidate, ZoneDiscoverer

        get_logger().info(f"Discovering zones for {serial_number}...")

        # 1. Gather instances
        target_instances = []
        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    if se.equipment and se.equipment.device_serial_number == serial_number:
                        target_instances.extend(se.instances)

        if not target_instances:
            print(f"No instances found for serial {serial_number}")
            return DiscoveryResult([], 0)

        print(f"Found {len(target_instances)} instances. Using sample of {min(len(target_instances), sample_size)}.")

        # 2. Sample
        import random
        if len(target_instances) > sample_size:
            sample = random.sample(target_instances, sample_size)
        else:
            sample = target_instances

        # 3. Analyze
        # We reuse the parallel analysis logic
        raw_regions_lists = run_parallel(
            pixel_analysis.analyze_pixels,
            sample,
            desc="Discovery Scan",
            force_threads=True
        )

        candidates = []

        for i, regions in enumerate(raw_regions_lists):
            # i serves as the unique source index
            for r in regions:
                if r.confidence >= min_confidence:
                    # Classify immediately (or could be lazy)
                    cls = ZoneDiscoverer._classify_text(r.text)

                    cand = DiscoveryCandidate(
                        text=r.text,
                        confidence=r.confidence,
                        box=list(r.box),
                        source_index=i,
                        classification=cls
                    )
                    candidates.append(cand)

        result = DiscoveryResult(candidates, len(sample))
        print(f"Discovery complete. Found {len(candidates)} raw candidates.")
        return result

    def get_cohort_report(self, expand_metadata: bool = False) -> 'pd.DataFrame':
        """
        Returns a Pandas DataFrame containing flattened metadata for the current cohort.
        Useful for analysis and QA.
        """
        import pandas as pd
        rows = []
        for p in self.store.patients:
            for s in p.studies:
                for se in s.series:
                    manufacturer = se.equipment.manufacturer if se.equipment else ""
                    model = se.equipment.model_name if se.equipment else ""
                    device_serial = se.equipment.device_serial_number if se.equipment else ""

                    for inst in se.instances:
                        # Basic row info
                        row = {
                            "PatientID": p.patient_id,
                            "PatientName": p.patient_name,
                            "StudyInstanceUID": s.study_instance_uid,
                            "StudyDate": s.study_date,
                            "SeriesInstanceUID": se.series_instance_uid,
                            "Modality": se.modality,
                            "SOPInstanceUID": inst.sop_instance_uid,
                            "Manufacturer": manufacturer,
                            "Model": model,
                            "DeviceSerial": device_serial
                        }

                        if expand_metadata and hasattr(inst, 'attributes') and inst.attributes:
                            row.update(inst.attributes)

                        rows.append(row)

        return pd.DataFrame(rows)

    def generate_report(self, output_path: str, format: str = "markdown") -> None:
        """
        Generates a formal Compliance Report for the current session.

        The report includes:
        - Session statistics (counts).
        - Audit logs and exceptions.
        - Check for unsafe attributes (e.g., Burned In Annotations).
        - Privacy Profile information.

        Args:
            output_path (str): The file path where the report should be saved.
            format (str): The output format ('markdown' or 'md'). Defaults to "markdown".
        """
        get_logger().info(f"Generating Compliance Report ({format}) to {output_path}...")

        # 1. Gather Statistics
        n_p = len(self.store.patients)
        n_st = sum(len(p.studies) for p in self.store.patients)
        n_se = sum(len(st.series) for p in self.store.patients for st in p.studies)
        n_i = sum(len(se.instances)
                  for p in self.store.patients for st in p.studies for se in st.series)

        # 2. Gather Audit Logs & Exceptions
        audit_summary = self.store_backend.get_audit_summary()
        exceptions = self.store_backend.get_audit_errors()

        # Check for unsafe attributes (BurnedInAnnotation)
        unsafe_items = self.store_backend.check_unsafe_attributes()
        if unsafe_items:
            for uid, _path, msg in unsafe_items:
                exceptions.append(
                    (datetime.datetime.now().isoformat(),
                     "COMPLIANCE_CHECK",
                     f"{msg} - {uid}"))

        # 3. Determine Context
        #
        # Describe what was configured, never what standard it might
        # satisfy. `deid_method` used to be a dataclass default reading
        # "Safe Harbor (Basic Profile)" that nothing assigned, so every
        # report -- including a bare session's, scanning six tags --
        # asserted HIPAA Safe Harbor above a DPO signature line.
        # The tag count mirrors what PhiInspector actually scans with:
        # the configured policy, or the shipped defaults when there is
        # none (see PhiInspector.__init__).
        effective_tags = self.configuration.phi_tags
        if not effective_tags:
            try:
                effective_tags = ConfigLoader.load_phi_config()
            except (OSError, ValueError):
                effective_tags = {}

        profile_name = self.configuration.privacy_profile
        if not profile_name:
            privacy_profile = "None (session defaults)"
            method = "Session defaults"
        elif profile_name in PRIVACY_PROFILES:
            privacy_profile = profile_name
            method = f"DICOM PS3.15 '{profile_name}' profile"
        else:
            # Resolved, but from a file rather than a built-in name.
            privacy_profile = profile_name
            method = f"Custom profile '{profile_name}'"

        deid_method = (
            f"{method}: {len(effective_tags)} tag rules, "
            f"{len(self.configuration.rules)} pixel redaction rules")

        try:
            from importlib.metadata import version, PackageNotFoundError
            ver = version("isocenter")
        except PackageNotFoundError:
            # Running from a source tree that was never installed.
            ver = "0.0.0"

        # 4. Build Report DTO
        report = ComplianceReport(
            isocenter_version=ver,
            project_name=os.path.basename(self.persistence_file),
            privacy_profile=privacy_profile,
            deid_method=deid_method,
            total_patients=n_p,
            total_studies=n_st,
            total_series=n_se,
            total_instances=n_i,
            audit_summary=audit_summary,
            exceptions=exceptions,
            validation_status="PASS" if audit_summary and not exceptions else "REVIEW_REQUIRED"
        )

        renderer = get_renderer(format)
        renderer.render(report, output_path)

    def generate_manifest(self, output_path: str, format: str = "html") -> None:
        """
        Generates a visual (HTML) or machine-readable (JSON) manifest of all instances.

        This manifest lists every SOP Instance currently tracked in the session,
        along with its file path and key metadata (Modality, Manufacturer, etc.).

        Args:
            output_path (str): The file path where the manifest should be saved.
            format (str): The output format ('html' or 'json'). Defaults to "html".
        """
        get_logger().info(f"Generating Manifest ({format}) to {output_path}...")

        items = []
        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    modality = se.modality
                    manufacturer = se.equipment.manufacturer if se.equipment else ""
                    model = se.equipment.model_name if se.equipment else ""

                    for inst in se.instances:
                        fpath = getattr(inst, 'file_path', "N/A")

                        item = ManifestItem(
                            patient_id=p.patient_id,
                            study_instance_uid=st.study_instance_uid,
                            series_instance_uid=se.series_instance_uid,
                            sop_instance_uid=inst.sop_instance_uid,
                            file_path=str(fpath),
                            modality=modality,
                            manufacturer=manufacturer,
                            model_name=model
                        )
                        items.append(item)

        manifest = Manifest(
            generated_at=datetime.datetime.now().isoformat(),
            items=items,
            project_name=os.path.basename(self.persistence_file)
        )

        generate_manifest_file(manifest, output_path, format)

    def save_analysis(self, report):
        """
        Persists the results of a PHI analysis to the database.

        Args:
            report (Union[PhiReport, List[PhiFinding]]): The PHI report object or list of findings to save.
        """
        findings = report
        if hasattr(report, 'findings'):
            findings = report.findings

        self.store_backend.save_findings(findings)

    # =========================================================================
    # PRIVACY & SECURITY
    # =========================================================================

    def lock_identities(self,
                        patient_id: str,
                        persist: bool = False,
                        _patient_obj: "Patient" = None,
                        verbose: bool = True,
                        **kwargs) -> Union[List["Instance"],
                                           LockingResult]:
        """
        Securely embeds the original patient name/ID into a private DICOM tag.

        This mechanism allows for "Reversible Anonymization". The original identity
        is encrypted using a symmetric key and stored in a private attribute
        before the visible public attributes are anonymized.

        Must be called BEFORE anonymization/redaction if recovery is required.

        Args:
            patient_id (str): The ID of the patient to preserve (or a list/report for batch processing).
            persist (bool): If True, writes changes to the database immediately.
                            If False, returns modified instances (useful for batch buffering).
            _patient_obj (Patient, optional): Optimization argument to avoid O(N) lookup.
            verbose (bool): If True, logs debug information.
            **kwargs: Additional arguments passed to `lock_identities_batch`.

        Returns:
            Union[List[Instance], LockingResult]: A list of modified instances.
        """
        if not self.reversibility_service:
            raise RuntimeError(
                "Reversible anonymization not enabled. Call enable_reversible_anonymization() first.")

        # Dispatch to batch method if a list is provided
        if isinstance(patient_id, (list, tuple, set)) or hasattr(patient_id, 'findings'):
            return self.lock_identities_batch(patient_id, **kwargs)

        if verbose:
            get_logger().debug(f"Preserving identity for {patient_id}...")

        modified_instances = []

        if _patient_obj:
            patient = _patient_obj
        else:
            patient = next((p for p in self.store.patients if p.patient_id == patient_id), None)

        if not patient:
            get_logger().error(f"Patient {patient_id} not found.")
            return LockingResult([])

        # Determine Tags to Lock (Default + Custom)
        default_tags = [
            "0010,0010",  # PatientName
            "0010,0020",  # PatientID
            "0010,0030",  # PatientBirthDate
            "0010,0040",  # PatientSex
            "0008,0050"  # AccessionNumber
        ]

        tags_to_lock = kwargs.get("tags_to_lock", default_tags)

        # Capture Original Values from First Instance
        original_attrs = {}
        first_instance = None

        # Locate first instance efficiently
        for st in patient.studies:
            for se in st.series:
                if se.instances:
                    first_instance = se.instances[0]
                    break
            if first_instance:
                break

        if first_instance:
            for tag in tags_to_lock:
                val = first_instance.attributes.get(tag)
                if val is not None:
                    original_attrs[tag] = val
        else:
            # Fallback to Patient object properties if no instances (unlikely)
            if "0010,0010" in tags_to_lock:
                original_attrs["0010,0010"] = patient.patient_name
            if "0010,0020" in tags_to_lock:
                original_attrs["0010,0020"] = patient.patient_id

        cnt = 0

        # Optimization: Encrypt once per patient
        token = self.reversibility_service.generate_identity_token(
            original_attributes=original_attrs)

        # Iterate deep
        for st in patient.studies:
            for se in st.series:
                for inst in se.instances:
                    self.reversibility_service.embed_identity_token(inst, token)
                    modified_instances.append(inst)
                    cnt += 1

        if persist and modified_instances:
            self.store_backend.update_attributes(modified_instances)
            get_logger().info(
                f"Secured identity (tags: {
                    list(
                        original_attrs.keys())}) in {cnt} instances for {patient_id}.")

        return LockingResult(modified_instances)

    def lock_identities_batch(self,
                              patient_ids: Union[List[str],
                                                 "PhiReport",
                                                 List["PhiFinding"]],
                              auto_persist_chunk_size: int = 0) -> Union[List["Instance"],
                                                                         LockingResult]:
        """
        Batch process multiple patients to lock identities.

        Args:
            patient_ids (Union[List[str], PhiReport]): List of PatientIDs to process.
            auto_persist_chunk_size (int): If > 0, persists changes and releases memory every N instances.
                                           IMPORTANT: Returns an empty list if enabled to prevent OOM.

        Returns:
            Union[List[Instance], LockingResult]: List of all modified instances (if chunking is disabled).
        """
        if not self.reversibility_service:
            raise RuntimeError("Reversible anonymization not enabled.")

        # Normalize input to a set of strings
        normalized_ids = set()

        # Handle PhiReport or list containers
        iterable_data = patient_ids
        if hasattr(patient_ids, 'findings'):  # PhiReport
            iterable_data = patient_ids.findings

        for item in iterable_data:
            if isinstance(item, str):
                normalized_ids.add(item)
            elif hasattr(item, 'patient_id') and item.patient_id:
                normalized_ids.add(item.patient_id)

        start_ids = list(normalized_ids)

        modified_instances = []  # Only used if auto_persist_chunk_size == 0
        current_chunk = []      # Used if auto_persist_chunk_size > 0

        count_patients = 0
        count_instances_chunked = 0

        from tqdm import tqdm

        # Optimization: Create a lookup map for O(1) access
        patient_map = {p.patient_id: p for p in self.store.patients}

        with tqdm(start_ids, desc="Locking Identities", unit="patient") as pbar:
            for pid in pbar:
                p_obj = patient_map.get(pid)
                if p_obj:
                    # Use verbose=False to avoid log spam
                    res = self.lock_identities(
                        pid, persist=False, _patient_obj=p_obj, verbose=False)

                    if auto_persist_chunk_size > 0:
                        current_chunk.extend(res)
                        if len(current_chunk) >= auto_persist_chunk_size:
                            self.store_backend.update_attributes(current_chunk)
                            count_instances_chunked += len(current_chunk)
                            current_chunk = []  # Release memory
                    else:
                        modified_instances.extend(res)

                    count_patients += 1
                else:
                    get_logger().error(f"Patient {pid} not found (batch processing).")

        # Final cleanup
        if auto_persist_chunk_size > 0:
            if current_chunk:
                self.store_backend.update_attributes(current_chunk)
                count_instances_chunked += len(current_chunk)

            get_logger().info(
                f"Batch preserved identity for {count_patients} patients ({count_instances_chunked} instances). Persisted incrementally.")
            return LockingResult([])

        if modified_instances:
            msg = f"Preserved identity for {len(modified_instances)} instances."
            get_logger().info(msg)

        get_logger().info(
            f"Batch preserved identity for {count_patients} patients ({
                len(modified_instances)} instances).")
        return LockingResult(modified_instances)

    def recover_patient_identity(self, patient_id: str, restore: bool = True):
        """
        Attempts to recover original identity from the encrypted private token.

        Decrypts the private tag stored by `lock_identities` and optionally
        restores the original PatientName and PatientID public attributes.

        Args:
            patient_id (str): The PatientID to search for and recover.
            restore (bool): If True, applies the recovered attributes back to ALL
                            in-memory instances for this patient.
        """
        if not self.reversibility_service:
            raise RuntimeError("Reversibility not enabled.")

        p = next((x for x in self.store.patients if x.patient_id == patient_id), None)
        if not p:
            print(f"Patient {patient_id} not found.")
            return

        # Locate first instance to get the token
        first_inst = None
        for st in p.studies:
            for se in st.series:
                if se.instances:
                    first_inst = se.instances[0]
                    break

        if not first_inst:
            print("No instances found for patient.")
            return

        original_attrs = self.reversibility_service.recover_original_data(first_inst)

        if original_attrs:
            if restore:
                count = 0
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            for tag, val in original_attrs.items():
                                inst.set_attr(tag, val)
                            count += 1

                # Update Patient Object top-level properties if Name/ID changed
                if "0010,0010" in original_attrs:
                    p.patient_name = original_attrs["0010,0010"]
                if "0010,0020" in original_attrs:
                    p.patient_id = original_attrs["0010,0020"]

                get_logger().info(f"Restored identity attributes to {count} instances.")
        else:
            print("No encrypted identity token found or decryption failed.")

    def enable_reversible_anonymization(self, key_path: str = "isocenter.key"):
        """
        Initializes the encryption subsystem for Reversible Anonymization.

        Loads or generates a symmetric key which is used to encrypt original identities.

        Args:
            key_path (str): Path to the key file.
        """
        self.key_manager = KeyManager(key_path)
        self.key_manager.load_or_generate_key()
        self.reversibility_service = ReversibilityService(self.key_manager)
        get_logger().info(f"Reversible anonymization enabled. Key: {key_path}")

    # =========================================================================
    # REDACTION & REMEDIATION
    # =========================================================================

    def redact(self, show_progress=True):
        """
        Applies pixel redaction rules to the current session.

        Uses the currently loaded configuration (`self.configuration.rules`) to
        find and redact sensitive regions in the pixel data. This operation
        modifies the pixel data in memory (and via Sidecar for persistence);
        call `.save()` afterwards to persist it.

        Args:
            show_progress (bool): If True, displays a progress bar.

        Returns:
            int: How many instances were updated in memory. Zero means nothing
                was redacted -- no rules loaded, or no image matched one.

        Raises:
            Exception: Whatever the redaction backend raised, after logging it.
                Redaction is the step that removes burned-in PHI, so a failure
                here must reach the caller. This used to be caught, printed as
                `Execution interrupted`, and followed by `Execution Complete`,
                which left a half-redacted session looking like a finished one.
        """
        if not self.configuration.rules:
            get_logger().warning("No configuration loaded. Use .load_config() first.")
            print("No configuration loaded. Use .load_config() first.")
            return 0

        service = RedactionService(self.store, self.store_backend)
        try:
            return self._apply_redaction_rules(service, show_progress)
        except Exception:
            get_logger().exception(
                "Redaction failed. Images already processed are still redacted "
                "in memory; the rest are untouched.")
            raise

    def _apply_redaction_rules(self, service, show_progress):
        """Runs every loaded rule and applies the results to the store.

        Returns the number of instances updated. Raises on failure; the
        caller logs and re-raises.
        """
        tasks = []
        get_logger().info("Analyzing workload...")
        for rule in self.configuration.rules:
            tasks.extend(service.prepare_redaction_tasks(rule))

        if not tasks:
            get_logger().warning("No matching images found for any loaded rules.")
            print("No matching images found for any loaded rules.")
            return 0

        max_workers = _redaction_worker_count()
        print(f"Queued {len(tasks)} redaction tasks across "
              f"{len(self.configuration.rules)} rules.")
        print(f"Executing using {max_workers} workers (Process Isolation)...")
        get_logger().info(
            f"Starting granular redaction ({len(tasks)} tasks, "
            f"workers={max_workers})...")

        # Keyed before any worker starts. A redacted image gets a new SOP
        # UID, and `run_parallel` uses threads on a free-threaded build --
        # where the workers share these very objects, not copies of them.
        # A map built after dispatch would be keyed on the post-redaction
        # UIDs and match none of the results coming back, so every image
        # would be dropped by a run that reported no error.
        instances = {t['instance'].sop_instance_uid: t['instance'] for t in tasks}

        # Pixel I/O and NumPy ops release the GIL. The generator is consumed
        # incrementally so each worker's image is applied and released rather
        # than held until the end.
        mutations = run_parallel(
            service.execute_redaction_task,
            tasks,
            desc="Redacting Pixels",
            max_workers=max_workers,
            return_generator=True,
            chunksize=1,
            progress=show_progress)

        applied = self._apply_redaction_mutations(mutations, instances)

        if applied < len(tasks):
            get_logger().warning(
                f"Redaction updated {applied} of {len(tasks)} targeted images. "
                "The remainder returned no change: already redacted under this "
                "configuration, pixel data that would not load, or a worker "
                "that failed -- see the entries above for which.")

        service.scan_burned_in_annotations()

        print(f"Redaction complete: {applied} of {len(tasks)} images updated. "
              "Remember to call .save() to persist.")
        return applied

    @staticmethod
    def _apply_redaction_mutations(mutations, instances):
        """Copies each worker's result back onto the in-memory instance.

        `instances` maps pre-redaction SOP UID to the instance in this
        process. Workers operate on copies, so a mutation that is never
        applied here is a redaction that did not happen. Returns how many
        landed.
        """
        applied = 0

        for mutation in mutations:
            if not mutation:
                # The worker reported no change. It logs its own reason;
                # the shortfall is summarised by the caller.
                continue

            sop = mutation.get('original_sop_uid') or mutation.get('sop_uid')
            instance = instances.get(sop)
            if instance is None:
                get_logger().error(
                    f"Redacted image {sop} does not match any targeted "
                    "instance, so its redaction was discarded.")
                continue

            if mutation.get('attributes'):
                instance.attributes.update(mutation['attributes'])
            if mutation.get('sequences'):
                instance.sequences.update(mutation['sequences'])

            loader = mutation.get('pixel_loader')
            if loader:
                # The loader is our handle on the sidecar copy, but it points
                # at the worker's instance. Re-point it at this process's.
                loader.instance = instance
                instance._pixel_loader = loader

            if mutation.get('pixel_hash'):
                instance._pixel_hash = mutation['pixel_hash']

            instance.mark_modified()
            applied += 1

        return applied

    def redact_by_machine(self, serial_number: str, roi: List[int]):
        """
        Helper to run redaction for a single machine interactively.

        Temporarily overrides the configuration to apply a single ROI to a specific device.

        Args:
            serial_number (str): The device serial number to target.
            roi (List[int]): The Region of Interest as [y1, y2, x1, x2].
        """
        # Swap in a single-rule configuration, run redact() against it, then
        # restore the original rules in `finally` regardless of outcome.
        original = list(self.configuration.rules)  # Shallow copy
        try:
            self.configuration.rules = [{"serial_number": serial_number, "redaction_zones": [roi]}]
            self.redact()
        finally:
            self.configuration.rules = original

    def anonymize(self, findings: List[PhiFinding] = None):
        """
        Apply remediation Actions to PHI Findings (Tag Anonymization).

        If `findings` is provided, only those specific findings are remediated.
        If `findings` is None, a full audit is performed using the current configuration,
        and all resulting findings are remediated ("Blind Execute").

        Args:
            findings (List[PhiFinding], optional): Specific findings to clean.

        Returns:
            int: How many remediations were applied. Failures are logged and
                excluded, so a caller can tell a clean run from a partial
                one -- this used to be unreported, and the console line
                below printed the literal "None".
        """
        from .remediation import RemediationService
        # Pass date jitter config to constructor
        # Use persistence_manager.store_backend (SqliteStore) for audit logging
        remediator = RemediationService(
            store_backend=self.persistence_manager.store_backend,
            date_jitter_config=self.configuration.date_jitter
        )

        count = 0
        if findings:
            count = remediator.apply_remediation(findings)
        else:
            # Blind execution (apply all rules)
            # Logic for blind anonymization: scan then remediate
            # Use audit() which uses self.configuration internally now
            current_findings = self.audit()
            count = remediator.apply_remediation(current_findings)

        get_logger().info(f"Anonymized {count} entities.")
        print(f"Anonymized/Remediated {count} tags according to policy.")
        return count

    # =========================================================================
    # EXPORT
    # =========================================================================

    def export(self, folder: str, format: str = "dicom", **options):
        """Export the session to a directory in the requested format.

        Args:
            folder (str): Output directory.
            format (str): Registered format name. "dicom" (default) writes
                cleaned DICOM files; "wfdb" writes PhysioNet WFDB records.
            **options: Passed through to the selected exporter. See
                `_export_dicom` for the DICOM format's options.

        Returns:
            The exporter's return value. The DICOM exporter returns None
            for backward compatibility.

        Raises:
            ValueError: If `format` is not a registered export format.
        """
        from . import exporters

        exporter = exporters.get_exporter(format)
        return exporter.export(self, folder, **options)

    def _export_dicom(self, folder: str, use_compression=True,
                      check_burned_in=False, check_reversibility=True,
                      patient_ids: List[str] = None, show_progress=True,
                      subset=None):
        """
        Exports the current session to a directory, structured by Patient/Study/Series.

        Args:
            folder (str): The output directory path.
            use_compression (bool): If True, compresses output images using JPEG2000 (Lossless).
            check_burned_in (bool): If True, scans for PHI before exporting and
                skips every instance that still carries an identifier.
            check_reversibility (bool): If True (the default), warn when the
                files about to be written still carry the encrypted originals
                that `lock_identities()` embeds, and record the disclosure in
                the audit log. Those identities are recoverable by anyone
                holding `isocenter.key`, which a recipient of the cohort has
                no way to see for themselves. Passing False is the caller
                stating they already know; it silences the warning and skips
                the audit entry. The export itself is unchanged either way --
                this reports, it does not withhold.
            patient_ids (List[str], optional): Limit export to specific Patient IDs.
            show_progress (bool): If True, shows progress bar.
            subset (Union[str, list, pd.DataFrame]): Filter the export
                using a query string, a list of UIDs, or a DataFrame.
        """
        target_ids = (patient_ids if patient_ids is not None
                      else [p.patient_id for p in self.store.patients])

        # None means "no safety filter"; an empty set means "the scan ran and
        # found nothing". The two are not the same and the walk treats them
        # differently, so they must not collapse into one falsy value.
        identifying_uids = (self._scan_before_export()
                            if check_burned_in else None)
        allowed_uids = self._resolve_subset(subset)

        get_logger().info("Exporting session to: %s", folder)
        print("Preparing export plan...")

        # Flush before the walk: a large export loads pixels back in, and
        # holding both the pending edits and the frames being written has
        # been enough to run a redaction session out of memory.
        print("Saving pending changes to free memory...")
        self.save()
        self.release_memory()

        tasks, patient_count = self._build_export_plan(
            _ExportOptions(folder, identifying_uids, allowed_uids,
                           use_compression),
            target_ids)

        if not tasks:
            get_logger().warning("No instances found to export.")
            return

        if check_reversibility:
            self._report_recoverable_identities(tasks)

        print(f"Exporting {len(tasks)} images from {patient_count} patients...")
        self._run_export_batch(tasks, show_progress)
        print("Done.")

    def _report_recoverable_identities(self, tasks) -> int:
        """Report instances whose exported copy still carries its originals.

        `lock_identities()` embeds the original identifiers, encrypted,
        in an Encrypted Attributes Sequence (0400,0500). That is the
        point of reversible anonymisation and is not a defect -- but the
        exported file then looks de-identified while carrying everything
        needed to undo it, and nothing in the file says so to the person
        who receives it.

        Keyed on the data rather than on `self.reversibility_service`: a
        store can hold tokens embedded by an earlier session that never
        enabled the service in this one, and it is the bytes about to be
        written that matter, not what this session happens to have
        configured.

        Runs against the export plan, so it counts what will actually be
        written -- after the subset filter and the burned-in scan have
        removed whatever they remove.

        Returns:
            int: How many instances carry recoverable identities.
        """
        affected = [
            task.instance.sop_instance_uid
            for task in tasks
            if (task.instance.sequences.get(
                ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ) is not None
                and task.instance.sequences[
                    ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ].items)
        ]
        if not affected:
            return 0

        detail = (
            f"{len(affected)} of {len(tasks)} exported instances carry "
            f"encrypted original identities (0400,0500). They are "
            f"recoverable with the session key; treat the export as "
            f"re-identifiable by any holder of it.")
        get_logger().warning(detail)

        # The warning goes to a log the recipient of the cohort never
        # reads. The audit entry is what puts the disclosure somewhere it
        # survives the session.
        if getattr(self, "store_backend", None) is not None:
            self.store_backend.log_audit(
                action_type="REVERSIBLE_EXPORT",
                entity_uid=affected[0] if len(affected) == 1 else "MULTIPLE",
                details=detail)
        return len(affected)

    def _scan_before_export(self) -> Set[str]:
        """Scans for PHI and reports what it found, before anything is written.

        Returns:
            Set[str]: The UID of every entity carrying an identifier, at any
            level of the hierarchy. An instance is skipped if its own UID or
            any of its parents' appears here, so a patient whose name is
            still present excludes every image beneath them.
        """
        get_logger().info("Performing pre-export safety scan...")
        findings = self.scan_for_phi()
        if not findings:
            return set()

        _report_phi_findings(findings)
        get_logger().warning(
            "Safe export: identifiers detected. Exporting only the instances "
            "that carry none, and skipping the rest.")
        return {f.entity_uid for f in findings if f.entity_uid}

    def _resolve_subset(self, subset) -> Optional[Set[str]]:
        """Turns a subset argument into the UIDs allowed through the walk.

        Accepts a pandas query string, a DataFrame, or a list of UIDs at any
        level. Returns None when no subset was given, which means "export
        everything" -- distinct from an empty set, which means "the filter
        matched nothing".

        Raises:
            TypeError: If `subset` is not one of the three accepted forms.
                It used to be ignored, so a caller who asked for a filter
                and mistyped it got a full unfiltered export instead.
            ValueError: If a query string does not run against the cohort
                report. That also used to abort the export silently, which
                is indistinguishable from a query that matched nothing.
        """
        if subset is None:
            return None

        if isinstance(subset, list):
            # A bare list of UIDs at any level: patient, study, series or
            # instance. All four are matched during the walk.
            return set(subset)

        # pandas is an optional dependency, imported only on the paths that
        # need it so `import isocenter` does not require it.
        import pandas as pd

        if isinstance(subset, str):
            report = self.get_cohort_report(expand_metadata=True)
            try:
                frame = report.query(subset)
            except Exception as exc:
                raise ValueError(
                    f"subset query {subset!r} could not be run against the "
                    f"cohort report: {exc}") from exc
        elif isinstance(subset, pd.DataFrame):
            frame = subset
        else:
            raise TypeError(
                f"subset must be a query string, a DataFrame, or a list of "
                f"UIDs; got {type(subset).__name__}")

        return _uids_from_frame(frame)

    def _build_export_plan(self, options: '_ExportOptions', target_ids):
        """Walks the store and builds one ExportContext per instance to write.

        Nothing is written here. The plan is built first so the count is
        known before the parallel batch starts, and so the filters are
        applied in one place rather than inside the workers.

        Returns:
            Tuple of (contexts, number of patients visited).
        """
        tasks = []
        patient_count = 0

        for patient in self.store.patients:
            if patient.patient_id not in target_ids:
                continue
            patient_count += 1
            patient_attrs = _patient_attributes(patient)

            for study in patient.studies:
                study_attrs = _study_attributes(study)

                for series in study.series:
                    # Hybrid naming: shared with every other export format
                    # (see `export_folder_names` in io_handlers.py) so trees
                    # stay co-located.
                    series_path = os.path.join(
                        options.folder,
                        *export_folder_names(patient, study, series))
                    series_attrs = _series_attributes(series)
                    zones = self._redaction_zones_for(series)

                    for instance in series.instances:
                        if _excluded(options, patient, study, series, instance):
                            continue

                        tasks.append(ExportContext(
                            instance=instance,
                            # The SOP Instance UID names the file: it is
                            # unique where InstanceNumber is not.
                            output_path=os.path.join(
                                series_path, f"{instance.sop_instance_uid}.dcm"),
                            patient_attributes=patient_attrs,
                            study_attributes=study_attrs,
                            series_attributes=series_attrs,
                            compression=('j2k' if options.use_compression
                                         else None),
                            redaction_zones=zones))

        return tasks, patient_count

    def _redaction_zones_for(self, series) -> list:
        """The configured pixel-redaction zones for this series' scanner."""
        if not (series.equipment and series.equipment.device_serial_number):
            return []
        rule = self.configuration.get_rule(
            series.equipment.device_serial_number)
        return rule.get("redaction_zones", []) if rule else []

    @staticmethod
    def _run_export_batch(tasks, show_progress) -> None:
        """Runs the export in worker processes and reports the outcome.

        Uses `export_batch`'s own pool rather than `self._executor`: workers
        are recycled every 25 tasks so memory leaked by the imaging C
        libraries is reclaimed, which `ProcessPoolExecutor` cannot do.
        """
        try:
            success_count = DicomExporter.export_batch(
                tasks,
                show_progress=show_progress,
                total=len(tasks),
                maxtasksperchild=25,
                disable_gc=True)
        except Exception as exc:
            get_logger().error("Export Failed! Error: %s", exc)
            raise
        finally:
            gc.collect()

        if success_count < len(tasks):
            # Partial failure used to be invisible here: the count came back
            # and was dropped, and "Export complete." printed whether 1200 of
            # 1200 instances survived or 3 did. Per-file errors are already in
            # the audit log; this is the summary that says to go and read it.
            get_logger().warning(
                "Export finished with failures: %d of %d instances exported. "
                "See the audit log for per-instance errors.",
                success_count, len(tasks))
            print(f"Export finished with failures: "
                  f"{success_count}/{len(tasks)} instances exported.")
        else:
            get_logger().info("Export complete.")

    def export_dataframe(
            self,
            output_path: str = "export_metadata.csv",
            expand_metadata: bool = False):
        """
        Exports flat validation metadata to CSV or Parquet.

        Args:
            output_path (str): The output file path (ends with .csv or .parquet).
            expand_metadata (bool): If True, includes all DICOM attributes as columns.
        """
        import pandas as pd
        df = self.get_cohort_report(expand_metadata=expand_metadata)

        if output_path.endswith(".parquet"):
            try:
                # Requires pyarrow and pandas
                df.to_parquet(output_path, index=False)
            except Exception as e:
                get_logger().error(f"Failed to export parquet: {e}")
                raise e
        else:
            df.to_csv(output_path, index=False)

        print(f"Exported metadata to {output_path}")
        return df

    def export_to_parquet(self, output_path: str, patient_ids: List[str] = None):
        """
        [EXPERIMENTAL] Exports flattened metadata to a Parquet file.
        Requires 'pandas' and 'pyarrow' or 'fastparquet'.
        """
        try:
            import pandas as pd
        except ImportError:
            get_logger().error("export_to_parquet requires 'pandas' installed.")
            raise ImportError(
                "Please install pandas to use this feature: pip install pandas pyarrow")

        # 1. Sync DB state
        get_logger().info("Saving state before Parquet export...")
        self.save()

        # 2. Stream Data
        get_logger().info("Streaming data from database...")

        target_ids = patient_ids
        if target_ids is None:
            target_ids = [p.patient_id for p in self.store.patients]

        if not target_ids:
            get_logger().warning("No patients to export.")
            return

        generator = self.persistence_manager.store_backend.get_flattened_instances(target_ids)

        rows = list(generator)

        if not rows:
            get_logger().warning("No instances found for these patients.")
            return

        df = pd.DataFrame(rows)

        # 3. Save
        get_logger().info(f"Writing {len(df)} rows to {output_path}...")

        # Ensure directory exists
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        try:
            df.to_parquet(output_path, index=False)
            get_logger().info("Parquet export successful.")
        except ImportError as e:
            get_logger().error("Parquet engine (pyarrow or fastparquet) missing.")
            raise e
        except Exception as e:
            get_logger().error(f"Failed to write parquet: {e}")
            raise

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _rehydrate_findings(self, findings):
        """
        Updates findings in-place to point to live objects in self.store
        instead of the unpickled copies from workers.
        """
        patient_map = {p.patient_id: p for p in self.store.patients}
        study_map = {}
        instance_map = {}

        for p in self.store.patients:
            for s in p.studies:
                study_map[s.study_instance_uid] = s
                for se in s.series:
                    for i in se.instances:
                        instance_map[i.sop_instance_uid] = i

        for f in findings:
            if f.entity_type == "Patient":
                if f.entity_uid in patient_map:
                    f.entity = patient_map[f.entity_uid]
            elif f.entity_type == "Study":
                if f.entity_uid in study_map:
                    f.entity = study_map[f.entity_uid]
            elif f.entity_type == "Instance":
                f.entity = self._live_target(instance_map.get(f.entity_uid), f)

    @staticmethod
    def _live_target(instance, finding):
        """The live object a finding should be remediated against.

        A finding raised inside a sequence carries the path down to its
        item; a sequence item has no UID, so the path is the only way to
        find the same item again in this process. Returns None when it
        cannot be resolved.

        None is the right answer rather than the enclosing instance.
        Remediation skips a finding with no entity, whereas writing a
        nested tag onto the instance fabricates a top-level element that
        was never in the file and leaves the real value untouched inside
        the sequence -- an export carrying the PHI plus a decoy.
        """
        if instance is None:
            get_logger().warning(
                f"Finding for {finding.entity_uid} has no matching instance "
                "in the session; it will not be remediated.")
            return None

        target = resolve_item_path(instance, finding.entity_path)
        if target is None:
            get_logger().warning(
                f"The sequence item behind {finding.field_name} on "
                f"{finding.entity_uid} is gone (path {finding.entity_path}); "
                "it will not be remediated.")
        return target

    def _make_lightweight_copy(self, patient: "Patient") -> "Patient":
        """
        Creates a lightweight clone of the Patient object (and children)
        stripped of heavy pixel data, for efficient IPC transfer.
        Also attaches 'file_path' to instances to ensure workers can reload pixels if needed.
        """
        from .entities import Patient, Study, Series, Instance

        # Clone Patient
        p_new = Patient(
            patient_name=patient.patient_name,
            patient_id=patient.patient_id
        )

        for s in patient.studies:
            s_new = Study(
                study_instance_uid=s.study_instance_uid,
                study_date=s.study_date
            )
            if hasattr(s, "date_shifted"):
                s_new.date_shifted = s.date_shifted

            p_new.studies.append(s_new)

            for se in s.series:
                se_new = Series(
                    series_instance_uid=se.series_instance_uid,
                    modality=se.modality,
                    series_number=se.series_number
                )
                if se.equipment:
                    se_new.equipment = se.equipment
                s_new.series.append(se_new)

                for i in se.instances:
                    # Clone Instance
                    i_new = Instance(
                        sop_instance_uid=i.sop_instance_uid,
                        instance_number=i.instance_number,
                        sop_class_uid=i.sop_class_uid,
                        file_path=i.file_path
                    )
                    # Key: Ensure attributes are copied so workers can scan tags
                    if hasattr(i, 'attributes'):
                        i_new.attributes = i.attributes.copy()

                    # Sequences travel too. Dropping them was #57: the
                    # worker got a top-level-only instance, so the scan
                    # reported clean on every nested tag -- report text,
                    # annotations, anything below the first level.
                    i_new.sequences, item_map = clone_sequences(i)
                    i_new.text_index = [
                        (item_map.get(id(item), i_new), tag)
                        for item, tag in i.text_index]

                    if hasattr(i, "date_shifted"):
                        i_new.date_shifted = i.date_shifted

                    se_new.instances.append(i_new)

        return p_new

    # =========================================================================
    # DEPRECATED
    # =========================================================================

    def scan_for_phi(self, config_path: str = None) -> "PhiReport":
        """
        Legacy alias for audit().
        """
        return self.audit(config_path)
