import os
import re
import json
import datetime
import concurrent.futures
from typing import List, Union

import yaml
from tqdm import tqdm

from .io_handlers import (DicomImporter, DicomExporter, SidecarPixelLoader,
                          SidecarWaveformLoader)
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
from .parallel import run_parallel
from .configuration import IsocenterConfiguration
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


class LockingResult(list):
    """
    A list subclass that suppresses verbose REPL output for large datasets.
    """

    def __repr__(self):
        return f"<LockingResult: {len(self)} instances secured>"


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
            self.store_backend.save_all(self.store.patients)
        elif hasattr(self, 'persistence_manager'):
            self.persistence_manager.save_async(self.store.patients)

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
        Attempts to release memory by unloading pixel data from all instances.
        Safe to call: only unloads data that is safely persisted (on disk or sidecar).
        Useful after running extensive redaction or export operations.
        """
        get_logger().info("Releasing memory (RAM cleanup)...")
        count = 0
        freed = 0

        # Count total instances first for progress bar
        total_instances = sum(len(se.instances)
                              for p in self.store.patients for st in p.studies for se in st.series)

        if total_instances == 0:
            return

        with tqdm(total=total_instances, desc="Releasing Memory", unit="img") as pbar:
            for p in self.store.patients:
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            count += 1
                            if inst.unload_pixel_data():
                                freed += 1
                            pbar.update(1)

        get_logger().info(
            f"Memory release complete. Unloaded pixels for {freed}/{count} instances.")
        if freed > 0:
            print(f"Memory Cleanup: Released {freed} images from RAM.")

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

        This method analyzes the current session inventory (Equipment, Manufacturers)
        and attempts to auto-generate redaction rules based on internal knowledge bases
        (e.g., CTP rules). It also includes a default set of PHI tags.

        Args:
            output_path (str): The file path where the generated YAML configuration should be saved.
        """


        # Helper for Flow-Style Lists (Bracketed)
        class FlowList(list):
            pass

        def flow_list_representer(dumper, data):
            return dumper.represent_sequence('tag:yaml.org,2002:seq', data, flow_style=True)

        yaml.add_representer(FlowList, flow_list_representer)

        if not (output_path.endswith(".yaml") or output_path.endswith(".yml")):
            output_path += ".yaml"
            print(f"Note: Appending .yaml extension -> {output_path}")

        # 1. Identify what we have
        all_equipment = self.store.get_unique_equipment()

        # Instantiate service to query pixel/tag data efficiently
        service = RedactionService(self.store)

        # 2. Identify what is already configured (Pixel Rules)
        configured_serials = {rule.get("serial_number") for rule in self.configuration.rules}

        # Load Knowledge Base for Machines
        kb_path = os.path.join(
            os.path.dirname(
                os.path.abspath(__file__)),
            "resources",
            "redaction_rules.json")
        kb_machines = []
        if os.path.exists(kb_path):
            try:
                with open(kb_path, 'r', encoding='utf-8') as f:
                    kb_data = json.load(f)
                    kb_machines = kb_data.get("machines", [])
            except (OSError, json.JSONDecodeError) as exc:
                # Scaffolding continues without the knowledge base, but
                # silently producing a config with no pre-filled machines
                # looked identical to "the KB had nothing for you".
                get_logger().warning(
                    "Could not read the redaction knowledge base at %s: %s",
                    kb_path, exc)

        # 3. Find missing machines and try to pre-fill
        missing_configs = []
        for eq in all_equipment:
            if eq.device_serial_number and eq.device_serial_number not in configured_serials:

                # Check KB
                matched_rule = None
                # Primary: Serial Match
                for rule in kb_machines:
                    if rule.get("serial_number") == eq.device_serial_number:
                        matched_rule = rule
                        break

                # Check CTP Rules (Knowledge Base 2)
                ctp_path = os.path.join(
                    os.path.dirname(
                        os.path.abspath(__file__)),
                    "resources",
                    "ctp_rules.yaml")
                if not os.path.exists(ctp_path):
                    # Fallback to JSON if YAML doesn't exist
                    ctp_path = os.path.join(
                        os.path.dirname(
                            os.path.abspath(__file__)),
                        "resources",
                        "ctp_rules.json")

                if not matched_rule and os.path.exists(ctp_path):
                    try:
                        if ctp_path.endswith('.yaml'):
                            with open(ctp_path, 'r', encoding='utf-8') as f:
                                ctp_data = yaml.safe_load(f)
                        else:
                            with open(ctp_path, 'r', encoding='utf-8') as f:
                                ctp_data = json.load(f)

                        ctp_rules = ctp_data.get("rules", [])

                        for rule in ctp_rules:
                            # Fuzzy matching on Manufacturer and Model
                            # CTP rules usually have "manufacturer" and "model_name"
                            r_man = rule.get("manufacturer", "").lower()
                            r_mod = rule.get("model_name", "").lower()

                            eq_man = (eq.manufacturer or "").lower()
                            eq_mod = (eq.model_name or "").lower()

                            # Simple containment check as per CTP style
                            if r_man and r_man in eq_man and r_mod and r_mod in eq_mod:
                                matched_rule = rule.copy()
                                matched_rule["serial_number"] = eq.device_serial_number

                                # Move _ctp_condition to comment if present
                                cond = matched_rule.pop("_ctp_condition", None)
                                if cond:
                                    matched_rule["comment"] = f"Auto-matched from CTP. Condition: {cond}"
                                else:
                                    matched_rule["comment"] = f"Auto-matched from CTP Knowledge Base ({
                                        rule.get('manufacturer')} {
                                        rule.get('model_name')})"

                                break

                    except Exception as e:
                        get_logger().warning(f"Failed to load CTP rules: {e}")

                # Secondary: Model Match (Internal KB)
                if not matched_rule:
                    for rule in kb_machines:
                        if rule.get("model_name") == eq.model_name:
                            # It's a model match, so we should probably copy the zones
                            matches_man = not rule.get("manufacturer") or (
                                rule.get("manufacturer") == eq.manufacturer)
                            if matches_man:
                                matched_rule = rule.copy()
                                matched_rule["serial_number"] = eq.device_serial_number
                                matched_rule["comment"] = f"Auto-matched from Model Knowledge Base ({
                                    eq.model_name})"
                                break

                # 3.b Check for Burned In Annotations (Safety Check)
                # query index for this machine
                instances = service.index.get_by_machine(eq.device_serial_number)
                burned_in_count = 0
                for inst in instances:
                    val = inst.attributes.get("0028,0301", "NO")
                    if isinstance(val, str) and "YES" in val.upper():
                        burned_in_count += 1

                safety_comment = ""
                if burned_in_count > 0:
                    safety_comment = f"WARNING: {burned_in_count} images have 'Burned In Annotation' flag. Verify pixel redaction."

                if matched_rule:
                    # Use the template
                    rule_copy = matched_rule.copy()  # Ensure we don't mutate KB
                    if safety_comment:
                        existing = rule_copy.get("comment", "")
                        rule_copy["comment"] = f"{existing} {safety_comment}".strip()
                    missing_configs.append(rule_copy)
                else:
                    # Create empty scaffold
                    new_rule = {
                        "manufacturer": eq.manufacturer or "Unknown",
                        "model_name": eq.model_name or "Unknown",
                        "serial_number": eq.device_serial_number,
                        "redaction_zones": []
                    }
                    if safety_comment:
                        new_rule["comment"] = safety_comment
                    missing_configs.append(new_rule)

        # 4. Load PHI Tags Default (if not loaded)
        phi_tags = self.configuration.phi_tags
        if not phi_tags:
            # Load default config for scaffold
            try:
                phi_tags = ConfigLoader.load_phi_config()
            except Exception as e:
                get_logger().warning(f"Failed to load research tags: {e}")

        # 4b. Enhance PHI Tags (Transform to structured defaults)
        structured_tags = {}

        # Ensure critical tags are present
        if "0008,0020" not in phi_tags:
            phi_tags["0008,0020"] = "Study Date"
        if "0010,0040" not in phi_tags:
            phi_tags["0010,0040"] = "Patient Sex"
        if "0010,1010" not in phi_tags:
            phi_tags["0010,1010"] = "Patient Age"  # Helper

        for tag, val in phi_tags.items():
            name = val if isinstance(val, str) else val.get("name", "Unknown")
            action = "REMOVE"  # Default safety

            # Apply Research-Friendly Smart Defaults
            if tag == "0008,0020":  # Study Date
                action = "JITTER"
            elif tag == "0010,0040":  # Sex
                action = "KEEP"
            elif tag == "0010,1010":  # Age
                action = "KEEP"
            elif "Date" in name or "Time" in name:
                action = "REMOVE"  # Times are sensitive
            elif "ID" in name:
                action = "REMOVE"  # IDs are sensitive

            # Preserve existing structure if it was already structured
            if isinstance(val, dict):
                structured_tags[tag] = val
            else:
                # Minimal Scaffold: Skip tags that are simply REMOVED (covered by Basic profile)
                # Unless explicitly requested to show all? For now, match tests.
                if action == "REMOVE":
                    continue

                structured_tags[tag] = {
                    "name": name,
                    "action": action
                }

        # 5. Construct Unified Data
        data = {
            "version": "2.0",
            "privacy_profile": "basic",
            # No _instructions dict anymore, we use comments!
            "phi_tags": structured_tags,
            "date_jitter": self.configuration.date_jitter,
            "remove_private_tags": self.configuration.remove_private_tags,
            "machines": missing_configs + self.configuration.rules
        }

        if not missing_configs and not self.configuration.rules:
            print("No machines detected to scaffold.")

        # Pre-process data to ensure comments are single-line strings
        # And ensure redaction_zones use FlowList for bracketed style
        for m in data.get("machines", []):
            if "comment" in m and isinstance(m["comment"], str):
                # Replace newlines with spaces/semicolons
                m["comment"] = m["comment"].replace("\n", " ").replace("\r", "")
                # collapse multiple spaces
                m["comment"] = re.sub(r'\s+', ' ', m["comment"]).strip()

            if "redaction_zones" in m and isinstance(m["redaction_zones"], list):
                # Wrap inner lists (zones) in FlowList
                # And assume user wants [[...], [...]] so wrap outer too?

                zones = m["redaction_zones"]
                new_zones = FlowList()
                for z in zones:
                    if isinstance(z, list):
                        new_zones.append(FlowList(z))
                    else:
                        new_zones.append(z)
                m["redaction_zones"] = new_zones  # Assign flow list wrapper

        try:
            # Generate YAML string
            # sort_keys=False ensures order is preserved (machines list)
            # width=float("inf") prevents line wrapping for long strings
            yaml_content = yaml.dump(
                data,
                sort_keys=False,
                default_flow_style=False,
                width=float("inf"))

            # Post-process: Convert "comment: ..." into "# ..."
            lines = yaml_content.splitlines()
            new_lines = []
            for line in lines:
                # Simple match for key-value pair
                match = re.search(r'^(\s*)comment:\s*(.*)$', line)
                if match:
                    indent = match.group(1)
                    content = match.group(2).strip()

                    # Check for surrounding quotes and strip them
                    if content.startswith("'") and content.endswith("'"):
                        content = content[1:-1]
                        content = content.replace("''", "'")
                    elif content.startswith('"') and content.endswith('"'):
                        content = content[1:-1]
                        content = content.replace('\\"', '"')

                    new_lines.append(f"{indent}# {content}")
                else:
                    if line.strip().startswith(
                            "- ") and len(new_lines) > 0 and new_lines[-1].strip() != "":
                        new_lines.append("")

                    new_lines.append(line)

            # Prepend Header Comments
            header = """# Isocenter Privacy Configuration (v2.0)
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
            final_content = header + "\n" + "\n".join(new_lines) + "\n"

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(final_content)

            get_logger().info(
                f"Scaffolded Unified Config to {output_path} ({
                    len(missing_configs)} new machines)")
            print(f"Scaffolded Unified Config to {output_path}")
        except Exception as e:
            get_logger().error(f"Failed to write scaffold: {e}")

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

        get_logger().info(f"PHI Scan Complete. Found {len(all_findings)} issues.")

        return PhiReport(all_findings)

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

        Uses the currently loaded configuration (`self.configuration.rules`) to find and
        redact sensitive regions in the pixel data. This operation modifies the
        pixel data in-memory (and via Sidecar for persistence).

        Args:
            show_progress (bool): If True, displays a progress bar.
        """
        if not self.configuration.rules:
            get_logger().warning("No configuration loaded. Use .load_config() first.")
            print("No configuration loaded. Use .load_config() first.")
            return

        service = RedactionService(self.store, self.store_backend)

        try:


            # Parallel Execution for Speed
            # Threading works well here because pixel I/O and NumPy ops release GIL.
            # Shared memory allows in-place modification of instances.
            # OPTIMIZATION: Limited to 0.5x CPU or Max 8 to prevent OOM with large datasets
            cpu_count = os.cpu_count() or 1
            if os.environ.get("ISOCENTER_MAX_WORKERS"):
                max_workers = int(os.environ["ISOCENTER_MAX_WORKERS"])
            else:
                max_workers = max(1, min(int(cpu_count * 0.5), 8))

            # Generate granular tasks for better load balancing
            all_tasks = []
            get_logger().info("Analyzing workload...")
            for rule in self.configuration.rules:
                tasks = service.prepare_redaction_tasks(rule)
                all_tasks.extend(tasks)

            if not all_tasks:
                get_logger().warning("No matching images found for any loaded rules.")
                print("No matching images found for any loaded rules.")
                return

            print(
                f"Queued {len(all_tasks)} redaction tasks across {len(self.configuration.rules)} rules.")
            print(f"Executing using {max_workers} workers (Process Isolation)...")
            # 2. Parallel Redaction (Granular)
            get_logger().info(
                f"Starting granular redaction ({
                    len(all_tasks)} tasks, workers={max_workers})...")

            # Map for quick Result application (SOP -> Instance)
            instance_map = {t['instance'].sop_instance_uid: t['instance'] for t in all_tasks}

            try:
                # Use Process Isolation (Standard Pool) - Workers clean up via GC/Exit
                # We consume generator to apply updates incrementally
                results_gen = run_parallel(
                    service.execute_redaction_task,
                    all_tasks,
                    desc="Redacting Pixels",
                    max_workers=max_workers,
                    return_generator=True,
                    chunksize=1,
                    progress=show_progress)

                for mutation in results_gen:
                    if mutation:
                        sop = mutation.get('original_sop_uid') or mutation.get('sop_uid')
                        if sop in instance_map:
                            inst = instance_map[sop]

                            # 1. Apply Attributes & Sequences
                            if mutation.get('attributes'):
                                inst.attributes.update(mutation['attributes'])
                            if mutation.get('sequences'):
                                inst.sequences.update(mutation['sequences'])

                            # 2. Apply Pixel Loader (Critical)
                            # The loader acts as our handle to the sidecar data
                            loader = mutation.get('pixel_loader')
                            if loader:
                                # Fix Reference: Loader points to Worker's Instance copy.
                                # Re-point it to the Main Process Instance.
                                loader.instance = inst
                                inst._pixel_loader = loader
                                print(f"DEBUG: Updated loader for {sop} -> {loader.offset}")

                            if mutation.get('pixel_hash'):
                                inst._pixel_hash = mutation['pixel_hash']

                            inst._dirty = True
                            print(f"DEBUG: Instance {sop} updated in memory.")
                        else:
                            print(
                                f"DEBUG: MISS! {sop} not in instance_map {
                                    list(
                                        instance_map.keys())[
                                        :3]}...")

            finally:
                pass

            # Run Safety Checks
            service.scan_burned_in_annotations()

            print("Execution Complete. Remember to call .save() to persist.")
            print("Execution Complete. Session saved.")

        except Exception as e:
            get_logger().error(f"Execution interrupted: {e}")
            print(f"Execution interrupted: {e}")

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
               check_burned_in=False, check_reversibility=True, patient_ids: List[str] = None, show_progress=True,
               subset=None):
        """
        Exports the current session to a directory, structured by Patient/Study/Series.

        Args:
            folder (str): The output directory path.
            use_compression (bool): If True, compresses output images using JPEG2000 (Lossless).
            check_burned_in (bool): If True, performs a safety scan for 'Burned In Annotation' flags before export.
            check_reversibility (bool): If True, checks if reversibility is enabled (informational).
            patient_ids (List[str], optional): Limit export to specific Patient IDs.
            show_progress (bool): If True, shows progress bar.
            subset (Union[str, list, pd.DataFrame]): Filter export using a query string, list of UIDs, or DataFrame.
        """
        import os
        from .io_handlers import DicomExporter

        # 1. Validation Checks
        if check_reversibility and self.reversibility_service:
            # warn if we are exporting encrypted data without warning?
            # Actually Isocenter exports exactly what is in store (which might be encrypted).
            pass

        target_ids = patient_ids
        if target_ids is None:
            target_ids = [p.patient_id for p in self.store.patients]

        # SAFETY CHECK & FEEDBACK LOOP
        # If running in safe mode, run a full scan first to give aggregated feedback
        if check_burned_in:
            get_logger().info("Performing pre-export safety scan...")
            findings = self.scan_for_phi()
            if findings:
                print("\nSafety Scan Found Issues")
                print("The following tags were flagged as dirty:")
                print(f"{'Tag':<15} {'Description':<30} {'Count':<10} {'Examples'}")
                print("-" * 80)

                from collections import Counter
                counts = Counter()
                examples = {}
                descriptions = {}

                for f in findings:
                    tag = f.tag or f.field_name
                    counts[tag] += 1
                    if tag not in examples:
                        examples[tag] = str(f.value)
                    descriptions[tag] = f.reason

                for tag, count in counts.items():
                    ex = examples[tag][:30]
                    desc = descriptions[tag][:28]
                    print(f"{tag:<15} {desc:<30} {count:<10} {ex}")

                print("\nSuggested Config Update:")
                print("Add the following rules to your config to resolve these:")
                print("{")
                print('    "phi_tags": {')
                rows = []
                for tag in counts:
                    # Attempt to infer name
                    name = "patient_name" if "0010,0010" in tag else "unknown_tag"
                    if "0010,0020" in tag:
                        name = "patient_id"
                    if "0008,0020" in tag:
                        name = "study_date"

                    rows.append(
                        f'        "{tag}": {{ "name": "{name}", "action": "REMOVE" }} , // Found {counts[tag]} times')
                print(",\n".join(rows))
                print('    }')
                print("}")

                print('    }')
                print("}")

                get_logger().warning("Safe Export: PHI findings detected. Proceeding to export ONLY safe instances (Skipping dirty).")
                # Build Dirty Filter
                dirty_uids = set()
                for f in findings:
                    if f.entity_uid:
                        dirty_uids.add(f.entity_uid)

            else:
                dirty_uids = set()

        # Subset resolution
        allowed_uids = None
        if subset is not None:
            import pandas as pd
            df = None
            if isinstance(subset, str):
                # Query string
                full_df = self.get_cohort_report(expand_metadata=True)
                try:
                    df = full_df.query(subset)
                except Exception as e:
                    get_logger().error(f"Failed to query subset '{subset}': {e}")
                    return
            elif isinstance(subset, pd.DataFrame):
                df = subset
            elif isinstance(subset, list):
                # Assume list of UIDs (Patient, Series, or Instance)
                # We need to match against any level. simpler to scan.
                # For now, let's assume if it matches PatientID, SeriesUID, or SOPUID we keep it.
                subset_set = set(subset)
                allowed_uids = subset_set  # We will pass this to filter

            if df is not None:
                # Extract all UIDs relevant
                allowed_uids = set()
                # PRECISION EXPORT:
                # If we have SOPInstanceUID, we ONLY use that to ensure we match the exact rows returned by the query.
                # Adding PatientID would re-include ALL instances for that patient
                # (defeating granular filters like Modality='CT').
                if "SOPInstanceUID" in df.columns:
                    allowed_uids.update(df["SOPInstanceUID"].tolist())
                # Fallbacks if SOPInstanceUID is missing (e.g. custom dataframe)
                elif "SeriesInstanceUID" in df.columns:
                    allowed_uids.update(df["SeriesInstanceUID"].tolist())
                elif "StudyInstanceUID" in df.columns:
                    allowed_uids.update(df["StudyInstanceUID"].tolist())
                elif "PatientID" in df.columns:
                    allowed_uids.update(df["PatientID"].tolist())

        get_logger().info(f"Exporting session to: {folder}")
        print("Preparing export plan...")

        # 2. Memory Management Check
        # Before starting a massive export (which might load pixels), ensure we save pending changes
        # and flush memory to avoid OOM if user did a lot of redaction.
        print("Saving pending changes to free memory...")
        self.save()
        self.release_memory()

        # 3. Create Export Plan (Lightweight objects)
        export_tasks = []
        total_instances = 0

        count_p = 0
        count_i = 0

        # We iterate our store to build tasks.
        # But for parallelism, we want to pass file paths or DB IDs, not full objects.
        # DicomExporter needs (Instance -> OutputPath) mapping.

        # Pre-calculate paths
        # Structure: Folder / Patient / Study / Series / Instance

        # Optimization: We can generate the plan using minimal metadata
        from .io_handlers import ExportContext, export_folder_names

        for p in self.store.patients:
            if p.patient_id not in target_ids:
                continue

            count_p += 1

            pat_attrs = {
                "0010,0010": p.patient_name,
                "0010,0020": p.patient_id
            }
            if hasattr(p, 'birth_date') and p.birth_date:
                pat_attrs["0010,0030"] = p.birth_date
            if hasattr(p, 'sex') and p.sex:
                pat_attrs["0010,0040"] = p.sex

            for st in p.studies:
                study_attrs = {
                    "0020,000D": st.study_instance_uid,
                    "0008,0020": st.study_date,
                }
                if hasattr(st, 'study_time') and st.study_time:
                    study_attrs["0008,0030"] = st.study_time
                if hasattr(st, 'accession_number'):
                    study_attrs["0008,0050"] = st.accession_number

                for se in st.series:
                    # Hybrid Naming: shared with every other export format
                    # (see `export_folder_names` in io_handlers.py) so
                    # trees stay co-located.
                    subj_name, study_folder, series_folder = export_folder_names(p, st, se)
                    se_path = os.path.join(folder, subj_name, study_folder, series_folder)

                    series_attrs = {
                        "0020,000E": se.series_instance_uid,
                        "0008,0060": se.modality,
                        "0020,0011": str(se.series_number)
                    }
                    if hasattr(se, 'series_description'):
                        series_attrs["0008,103E"] = se.series_description

                    for inst in se.instances:
                        # Debug
                        # print(f"Checking instance {inst.sop_instance_uid}...")

                        if check_burned_in:
                            # HIERARCHICAL SAFETY CHECK
                            # If parent (Patient, Study, Series) is dirty, skip instance.
                            # Also check instance itself.
                            is_dirty = False
                            if p.patient_id in dirty_uids:
                                is_dirty = True
                            elif st.study_instance_uid in dirty_uids:
                                is_dirty = True
                            elif se.series_instance_uid in dirty_uids:
                                is_dirty = True
                            elif inst.sop_instance_uid in dirty_uids:
                                is_dirty = True

                            # Fallback: Per-instance check if not already flagged dirty but inspector failed?
                            # No, Pre-Check covered everything.

                            if is_dirty:
                                get_logger().warning(
                                    f"Skipping unsafe instance {
                                        inst.sop_instance_uid} (Entity or Parent is Dirty).")
                                continue

                        # Legacy Subset Filtering
                        if allowed_uids is not None:
                            if (inst.sop_instance_uid not in allowed_uids and
                                se.series_instance_uid not in allowed_uids and
                                st.study_instance_uid not in allowed_uids and
                                    p.patient_id not in allowed_uids):
                                continue
                        count_i += 1
                        # Use SOP Instance UID for filename to ensure uniqueness and match tests
                        inst_clean = f"{inst.sop_instance_uid}.dcm"
                        out_path = os.path.join(se_path, inst_clean)

                        # Determine if this instance needs redaction
                        redaction_zones = []
                        if se.equipment and se.equipment.device_serial_number:
                            sn = se.equipment.device_serial_number
                            rule = self.configuration.get_rule(sn)
                            if rule:
                                redaction_zones = rule.get("redaction_zones", [])

                        # Build Context
                        # Compression handled by worker finalizing dataset
                        ctx = ExportContext(
                            instance=inst,
                            output_path=out_path,
                            patient_attributes=pat_attrs,
                            study_attributes=study_attrs,
                            series_attributes=series_attrs,
                            compression='j2k' if use_compression else None,
                            redaction_zones=redaction_zones
                        )
                        export_tasks.append(ctx)
                        total_instances += 1

        if not export_tasks:
            get_logger().warning("No instances found to export.")
            return

        print(f"Exporting {total_instances} images from {count_p} patients...")

        # 4. Execute Export
        # We use global run_parallel logic or specialized internal batcher?
        show_progress = True

        if total_instances > 0:
            # MEMORY LEAK MITIGATION:
            # We use worker recycling (maxtasksperchild=100) via multiprocessing.Pool
            # This forces workers to restart periodically, clearing any leaked memory (e.g. from C-libs).
            # We do NOT use the shared self._executor for this, as ProcessPoolExecutor
            # doesn't support recycling.
            try:
                # Optimized for stability: maxtasksperchild=25 clears memory frequently
                # GC Optimization: Disable GC in workers
                success_count = DicomExporter.export_batch(
                    export_tasks,
                    show_progress=show_progress,
                    total=total_instances,
                    maxtasksperchild=25,
                    disable_gc=True)
            except Exception as e:
                get_logger().error(f"Export Failed! Error: {e}")
                raise e
            finally:
                # Main process GC trigger
                import gc
                gc.collect()

            if success_count < total_instances:
                # Partial failure used to be invisible here: the count came
                # back and was dropped, and "Export complete." printed
                # whether 1200 of 1200 instances survived or 3 did. Per-file
                # errors are already in the audit log; this is the summary
                # that says to go and read it.
                get_logger().warning(
                    "Export finished with failures: %d of %d instances exported. "
                    "See the audit log for per-instance errors.",
                    success_count, total_instances)
                print(f"Export finished with failures: "
                      f"{success_count}/{total_instances} instances exported.")
            else:
                get_logger().info("Export complete.")
        else:
            get_logger().warning("No instances queued for export.")

        print("Done.")

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
                if f.entity_uid in instance_map:
                    f.entity = instance_map[f.entity_uid]

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
