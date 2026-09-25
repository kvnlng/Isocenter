"""The session facade: ingest, audit, anonymize, redact, export, report.

`DicomSession`, exported as `isocenter.Session`, is the library's entry
point. It owns the in-memory object graph, the SQLite store and pixel
sidecar behind it, and the worker pools the heavy passes run on.
"""
import copy
import gc
import os
import re
import json
import contextlib
import threading
import datetime
import multiprocessing
import concurrent.futures
import functools
from collections import Counter
from typing import (List, Union, Dict, Any, Optional, Set, Tuple,
                    NamedTuple)

import yaml
from pydicom.datadict import dictionary_VR
from pydicom.multival import MultiValue

from .io_handlers import (DicomImporter, DicomExporter, DeidMarkers, ExportContext,
                          ExportError, ExportSummary, SidecarPixelLoader,
                          SidecarWaveformLoader,
                          export_folder_names, export_stamp_attributes, GRADED_LOSS_SCOPES,
                          normalize_id_filter, redaction_in_effect, select_patient_ids,
                          unmatched_patient_ids_sentence, unmatched_subset_uids_sentence)
from .store import DicomStore
from .services import (RedactionService, RedactionOutcome, RedactionError,
                       capture_phi_status_for_redaction,
                       carry_phi_status_across_redaction,
                       _report_redaction_failures, rules_matching, zone_rois)
from . import config_manager
from .config_manager import (ConfigLoader, _is_tag_key,
                             require_package_resource, validate_phi_policy)
from .privacy import (PhiInspector, PhiFinding, PhiReport,
                      _is_replacement_id, _is_replacement_name, _owned_rule)
from .logger import configure_logger, describe_exception, get_logger
from .reporting import (ComplianceReport, PixelScanSummary, get_renderer, GAP_REMOVED,
                        GAP_RETAINED, GAP_UNRESOLVED)
from .manifest import Manifest, ManifestItem, get_manifest_renderer
from .blob_kind import serialize_blob_kind
from .persistence import SqliteStore
from .crypto import KeyManager
from .reversibility import (ReversibilityService, _TokenHoldsNoRecord,
                            _TokenOfALaterScheme, _TokenOfAnEarlierLayout)
from .persistence_manager import PersistenceManager
from .parallel import (run_parallel, _env_int, _resolve_strategy,
                       resolve_max_workers, resolve_worker_initializer,
                       progress_bar)
from .configuration import (IsocenterConfiguration, FlowList, _policy_base_label,
                            _scan_policy_for, _deid_method_value)
from ._version import __version__
from .entities import (Patient, PhiStatus, ScanPolicy, SOURCE_SOP_UID_ATTR, clone_sequences,
                       resolve_item_path, iter_item_tree,
                       exported_patient_id, is_synthetic_patient_id, PASS_WRITING)
from .profiles import FLOOR_POLICY
# The module, read at call time: `create_config` names `profiles.FLOOR_BASE`
# and diffs against its table, and the two must be one read.
from . import profiles
from . import entities
from . import pixel_analysis
from .automation import ConfigAutomator

def scan_worker(args):
    """Scan one patient for PHI in a worker process or thread.

    Args:
        args (tuple): Exactly `(patient, config_source, remove_private,
            project_secret)`: the `Patient` to scan, the PHI policy as a
            mapping of tag to rule, whether private tags are swept, and
            the store's project secret, which has no default.

    Returns:
        List[PhiFinding]: The findings, each with `entity` set to None so
            it can cross a process boundary; the caller rehydrates them.

    Raises:
        TypeError: If the first element is not a `Patient`, or
            `config_source` is not a dict.
    """
    # The project secret travels **by value**, in the tuple. A worker
    # cannot read it back out of the store: a spawned process cannot reach
    # a `:memory:` database at all, and a second place the worker could
    # get a secret from is a second answer to "which secret keys this
    # patient". There is no default for the same reason -- a worker handed
    # no secret raises at the first pseudonym it has to mint rather than
    # falling back to an unkeyed one.
    patient, config_source, remove_private, project_secret = args
    if not isinstance(patient, Patient):
        raise TypeError(
            f"scan_worker expects (Patient, config_source, remove_private, "
            f"project_secret); got a {type(patient).__name__} first")

    # The policy itself, never a path: `audit()` resolves it in the
    # parent, and a worker reading a file would read it a second, weaker
    # way.
    if not isinstance(config_source, dict):
        raise TypeError(f"scan_worker expects the PHI policy as a mapping of tag "
                        f"to rule; got a {type(config_source).__name__} (#729)")
    inspector = PhiInspector(config_tags=config_source,
                             remove_private_tags=remove_private,
                             project_secret=project_secret)

    findings = inspector.scan_patient(patient)

    # Strip heavy entity objects before returning across process boundary
    for f in findings:
        f.entity = None

    return findings




class _ScanOutcome(NamedTuple):
    """What `_verify_worker` sends back for one instance.

    Attributes:
        entity_uid: The instance's SOP Instance UID.
        findings: The instance's findings, without entities.
        read: Whether at least one frame was read through OCR.
        failure: Why the instance could not be read in full, or None.
    """
    # Module scope, so it pickles across the process pool. It carries its
    # own UID because the recycling pool is `imap_unordered`: outcomes
    # arrive in completion order, and zipping them back onto the items that
    # were dispatched would pin each failure on the wrong instance.
    entity_uid: Optional[str]
    findings: List[PhiFinding]
    read: bool
    failure: Optional[str]


def _caller_tesseract_cmd() -> Optional[str]:
    """The `tesseract_cmd` this process's pytesseract runs.

    Call it in the caller, after `_require_ocr` has probed the binary, and
    send the value with each work item for `_adopt_tesseract_cmd`.

    Returns:
        Optional[str]: The command, or None when pytesseract (or a stand-in
            for it) carries no `pytesseract.tesseract_cmd`; None means
            "adopt nothing".
    """
    # A spawned worker imports a fresh pytesseract whose `tesseract_cmd` is
    # the bare `"tesseract"`, looked up on `PATH`, so without this a caller
    # who configured the binary rather than installing it on `PATH` would
    # pass the probe and then have every worker fail. `getattr` all the way
    # down because pytesseract is optional and a stand-in for it need not
    # carry the submodule.
    inner = getattr(pixel_analysis.pytesseract, "pytesseract", None)
    return getattr(inner, "tesseract_cmd", None)


def _adopt_tesseract_cmd(cmd: Optional[str]) -> None:
    """Point this process's pytesseract at the caller's binary.

    A process whose `pytesseract` is None, or a stand-in without the
    submodule, adopts nothing.

    Args:
        cmd (Optional[str]): The caller's `tesseract_cmd`, from
            `_caller_tesseract_cmd`; None adopts nothing.
    """
    if cmd is None:
        return
    inner = getattr(pixel_analysis.pytesseract, "pytesseract", None)
    # **Writes only when the value differs**, and that is what keeps the
    # threads path untouched rather than an optimisation. A worker thread
    # shares the caller's module, so it already reads the caller's value
    # and the comparison is equal; it must not write module state the
    # caller owns. In a spawned child the module is the child's own, and
    # the write is what points it at the caller's binary.
    if inner is not None and getattr(inner, "tesseract_cmd", None) != cmd:
        inner.tesseract_cmd = cmd


def _verify_worker(args):
    """OCR one instance and report the burned-in text no zone covers.

    Any exception is caught and returned as the outcome's `failure`, so
    one instance cannot end the pass.

    Args:
        args (tuple): `(instance, equipment, rules, tesseract_cmd)`: the
            `Instance`, its series' equipment, the redaction rules, and
            the caller's `tesseract_cmd` (see `_caller_tesseract_cmd`).

    Returns:
        _ScanOutcome: The instance's findings (without entities), whether
            any frame was read, and why it could not be read in full.
    """
    from .verification import RedactionVerifier
    instance, equipment, rules, tesseract_cmd = args
    if not instance:
        return _ScanOutcome(None, [], False, None)
    uid = instance.sop_instance_uid
    _adopt_tesseract_cmd(tesseract_cmd)

    # A boundary catch, and it must return an outcome rather than
    # re-raise: `run_parallel` re-raises a worker's exception by default,
    # so one instance that fails in a way `_ocr_instance` does not catch
    # would lose the whole pass, every other instance's findings with it.
    #
    # `_ocr_instance` is reached through the module, never a `from`
    # binding, so a patch on `pixel_analysis` reaches it -- in a thread
    # and in a child that patches its own import alike.
    try:
        ocr = pixel_analysis._ocr_instance(instance)  # pylint: disable=protected-access
        findings = RedactionVerifier(rules)._findings_for(  # pylint: disable=protected-access
            instance, ocr.regions, equipment)
    except Exception as e:  # pylint: disable=broad-exception-caught
        return _ScanOutcome(
            uid, [], False,
            describe_exception(e))

    # Strip the instance before the findings cross back, as `scan_worker`
    # does; `scan_pixel_content` puts the live one back. The strip
    # is not tidiness. `_findings_for` attaches the instance to every
    # finding, so without it the result would carry the instance, and
    # any frame resident on it, back to the parent across the pipe.
    # Rehydration alone would hide this: it overwrites the copy, so the
    # entity the caller sees is right while the frame still crosses the
    # pipe. `_ocr_instance` frees a loader-backed frame before this
    # result is built, so what the strip guards is the entity's identity
    # (one meaning for `finding.entity`, which `_rehydrate_findings`
    # restores) and an in-memory instance whose frame was resident
    # before the scan.
    for f in findings:
        f.entity = None
    return _ScanOutcome(uid, findings, ocr.read, ocr.failure)


def _discover_worker(args):
    """OCR one instance for zone discovery.

    Any exception is caught and returned as the result's failure, so one
    instance cannot end the pass.

    Args:
        args (tuple): `(instance, tesseract_cmd)`: the `Instance`, and the
            caller's `tesseract_cmd` (see `_caller_tesseract_cmd`).

    Returns:
        tuple: `(entity_uid, _InstanceOcr)` for the instance.
    """
    # `tesseract_cmd` is needed even though discovery passes
    # `force_threads=True`: `ISOCENTER_MAX_TASKS_PER_CHILD` outranks it
    # (`parallel._resolve_execution_choice`), and under it discovery runs
    # in spawned processes, where every instance would otherwise fail.
    instance, tesseract_cmd = args
    uid = instance.sop_instance_uid
    _adopt_tesseract_cmd(tesseract_cmd)
    # Not through `pixel_analysis.analyze_pixels`, which logs a failed load
    # or frame and returns `[]`: an instance nobody read would count as a
    # source with no text and dilute every zone's occurrence rate.
    # `_ocr_instance` is reached through the module, and the catch is a
    # boundary catch, for the reasons `_verify_worker` gives.
    try:
        return uid, pixel_analysis._ocr_instance(instance)  # pylint: disable=protected-access
    except Exception as e:  # pylint: disable=broad-exception-caught
        return uid, pixel_analysis._InstanceOcr(  # pylint: disable=protected-access
            [], False, describe_exception(e))


def _warn_unread_instances(operation, failures, attempted, where):
    """Log one WARNING counting the instances an OCR pass could not read.

    Silent when nothing failed.

    Args:
        operation (str): The call's name, for the message.
        failures (list): `(entity_uid, reason)` per unread instance; the
            first is quoted.
        attempted (int): How many instances the pass tried to read.
        where (str): Where the caller can find the full list.
    """
    # The warning is what reaches a caller who reads neither the report's
    # `failures` nor the log file: the `isocenter` logger's console
    # handler prints WARNING and above.
    if not failures:
        return
    uid, reason = failures[0]
    get_logger().warning(
        f"{operation}: {len(failures)} of {attempted} instance(s) could not "
        f"be read in full, so their text was not (or not all) scanned; see "
        f"{where}. First: {uid}: {reason}")


def _audit_unread_instances(store_backend, operation, failures):
    """Write one `WARNING` audit row per instance an OCR pass could not read.

    Each row costs the run its PASS. Call it before the warning and before any
    raise, so a caller who catches `PixelScanError` has an audit log that
    already holds every row.

    Args:
        store_backend (SqliteStore): The store whose audit log receives the
            rows.
        operation (str): The call's name, for the row text.
        failures (list): `(entity_uid, reason)` per unread instance.
    """
    # The mirror of `DicomExporter._report_export_failures`: a failure the
    # caller is told about must reach the audit log too, or the compliance
    # report would grade `PASS` a run whose verification never looked at
    # some of its pixels. `WARNING` rather than export's `ERROR`: a pass
    # that read some instances returns a result, and nothing was written
    # wrong. The two grade alike, and both are in the frozen audit
    # vocabulary, so this adds no word.
    #
    # The UID is in `details` as well as `entity_uid` because the report
    # renders `(timestamp, action_type, details)` and nothing else.
    # Flattened and pipe-escaped for the markdown table row it goes into.
    for uid, reason in failures:
        detail = (f"{operation} could not read {uid} in full, so its "
                  f"burned-in text was not (or not all) checked: {reason}")
        detail = " ".join(detail.split()).replace("|", "\\|")
        store_backend.log_audit(action_type="WARNING",
                                entity_uid=uid or "UNKNOWN", details=detail)


def _audit_withheld_instances(store_backend, folder, withheld):
    """Write one `WARNING` audit row per instance the pre-export scan withheld.

    Each row names the instance and the level of the hierarchy that carried the
    identifier (patient, study or instance), never the value, and costs the run
    its PASS.

    Args:
        store_backend (SqliteStore): The store whose audit log receives the
            rows.
        folder (str): The export's folder, for the row text.
        withheld (list): `(sop_instance_uid, level)` per withheld instance.
    """
    # Without these rows `export(check_burned_in=True)` would simply plan
    # fewer entries, and a never-anonymized cohort could export to an empty
    # folder under a PASS grade. `WARNING`, because nothing was written
    # wrong, and a word `get_audit_errors()` already selects, so the rows
    # grade with no new grade rule.
    #
    # The level, never the value: the value is PHI, and the Patient ID is
    # the cohort's key; neither belongs in a trail that renders into a
    # report a recipient reads. The UID is in `details` as well as
    # `entity_uid` because section 4 renders details and nothing else.
    #
    # `log_audit`, one call per row, and the action word spelled at the
    # call: the frozen-vocabulary pin reads the keyword at the site, and a
    # batch write swallows contention into a log line (see
    # `DicomExporter._report_export_losses`).
    for uid, level in withheld:
        detail = (f"DICOM export to {folder} withheld instance {uid}: its "
                  f"{level} still carries an identifier the pre-export scan "
                  f"raised (check_burned_in=True).")
        detail = " ".join(detail.split()).replace("|", "\\|")
        store_backend.log_audit(action_type="WARNING",
                                entity_uid=uid or "UNKNOWN", details=detail)


RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "resources")

# The fixed columns of `get_cohort_report`, in order. Named here rather
# than left implicit in the row dict so that an empty cohort still
# produces a frame with a schema -- see the comment at the return.
# `expand_metadata` adds to these; it does not replace them.
COHORT_REPORT_COLUMNS = [
    "PatientID",
    "PatientName",
    "StudyInstanceUID",
    "StudyDate",
    "SeriesInstanceUID",
    "Modality",
    "SOPInstanceUID",
    "Manufacturer",
    "Model",
    "DeviceSerial",
]

# The header written above every scaffolded config.
_CONFIG_HEADER = """# Isocenter Privacy Configuration (v2.0)
# ==========================================
#
#
# privacy_profile: "basic@2026c"
#   - The DICOM PS3.15 Annex E Basic Profile, Table E.1-1 of edition 2026c.
#   - The name is pinned to its edition: "basic" means "basic@2026c" in
#     every 1.x, and a later edition arrives as a new name.
#   - Omit the line to apply the floor policy beneath your phi_tags.
#   - Set to "none" for manual control: phi_tags is the whole policy.
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


def _profile_source(pinned: str) -> str:
    """Where a built-in profile's rules come from, for the report.

    Args:
        pinned (str): A pinned profile name, such as `basic@2026c`.

    Returns:
        str: The table, and the edition from the part of the name after `@`.
    """
    return f"(PS3.15 Annex E Table E.1-1, edition {pinned.partition('@')[2]})"


def _load_redaction_knowledge_base() -> List[Dict[str, Any]]:
    """Machine redaction rules shipped with the package, keyed by serial.

    Returns:
        List[Dict[str, Any]]: One rule per machine.
    """
    # Before the `try`, and that placement is load-bearing: the handler
    # below catches `OSError`, and `FileNotFoundError` is one -- a
    # refusal that drifted inside would be caught and turned into a
    # silent `return []`. `require_package_resource` raises `RuntimeError`
    # partly so that cannot happen even if it does drift.
    path = require_package_resource(
        RESOURCES_DIR, "redaction_rules.json",
        "scanned every frame with no machine redaction rules")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f).get("machines", [])
    except (OSError, json.JSONDecodeError) as exc:
        get_logger().warning(
            "Could not read the redaction knowledge base at %s: %s", path, describe_exception(exc))
        return []


def _load_ctp_rules() -> List[Dict[str, Any]]:
    """CTP-derived rules, matched by manufacturer and model rather than serial.

    YAML is preferred when present; the shipped copy is JSON.

    Returns:
        List[Dict[str, Any]]: The rules.
    """
    # The YAML keeps its `os.path.exists`, deliberately: `ctp_rules.yaml`
    # is *not* shipped, so its absence is the ordinary case and routing it
    # through the helper would make every correct installation a broken
    # one. Only the JSON fallback -- which `setup.py` does package and
    # `publish.yml` does gate on -- is required.
    yaml_path = os.path.join(RESOURCES_DIR, "ctp_rules.yaml")
    path = yaml_path if os.path.exists(yaml_path) else require_package_resource(
        RESOURCES_DIR, "ctp_rules.json",
        "matched no CTP de-identification rules")
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) if path.endswith('.yaml') else json.load(f)
        return (data or {}).get("rules", [])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        get_logger().warning("Failed to load CTP rules: %s", describe_exception(exc))
        return []


def _match_machine_rule(equipment, kb_machines, ctp_rules):
    """The first knowledge-base entry describing this machine, or None.

    An exact serial number beats a manufacturer/model match from CTP, which in
    turn beats a model-only match.

    Args:
        equipment (Equipment): The machine.
        kb_machines (List[Dict[str, Any]]): The shipped knowledge base.
        ctp_rules (List[Dict[str, Any]]): The CTP-derived rules.

    Returns:
        Optional[Dict[str, Any]]: A copy of the matching rule, carrying the
            machine's serial and a comment naming its source.
    """
    # A serial identifies one scanner; a model match is an educated guess
    # about a family of them, so the order must not change.
    for rule in kb_machines:
        if rule.get("serial_number") == equipment.device_serial_number:
            return rule

    matched = _match_ctp_rule(equipment, ctp_rules)
    if matched:
        return matched

    return _match_kb_by_model(equipment, kb_machines)


def _match_ctp_rule(equipment, ctp_rules):
    """CTP's containment match on manufacturer and model.

    Args:
        equipment (Equipment): The machine.
        ctp_rules (List[Dict[str, Any]]): The CTP-derived rules.

    Returns:
        Optional[Dict[str, Any]]: A copy of the first rule whose manufacturer
            and model are contained in the machine's, or None.
    """
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
    """Model-name match against the internal knowledge base, ignoring serial.

    Args:
        equipment (Equipment): The machine.
        kb_machines (List[Dict[str, Any]]): The shipped knowledge base.

    Returns:
        Optional[Dict[str, Any]]: A copy of the first rule for the machine's
            model (and manufacturer, when the rule names one), or None.
    """
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


def _render_config_yaml(data: Dict[str, Any]) -> str:
    """Render the config dict as the commented YAML users edit.

    Each machine's `comment:` becomes a `#` line, flattened to one line.

    Args:
        data (Dict[str, Any]): The configuration to render.

    Returns:
        str: The YAML text.
    """
    # PyYAML cannot emit comments, so `comment:` keys are dumped as data
    # and rewritten into `#` lines afterwards. That is why comments are
    # flattened to one line first: a multi-line value would produce YAML
    # that this pass turns into a broken comment block.
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
    """Everything the export plan needs beyond the store itself."""
    folder: str
    identifying_uids: Optional[Set[str]]
    allowed_uids: Optional[Set[str]]
    use_compression: bool
    verify_readback: bool = False


#: What `_why_excluded` answers for an instance the subset did not select.
#: Every other non-None answer is a hierarchy level the pre-export scan
#: withheld the instance for; the plan builder tells the two apart by
#: this constant, so the branch that records a withheld instance cannot
#: drift from the one that returns it.
OUTSIDE_THE_SUBSET = "outside the subset"

#: The level words `_why_excluded` answers, in `_uid_path` order. The
#: inspector raises findings on patients, studies and instances only, so
#: "series" is not expected in practice; it is matched anyway, because a
#: filter that skipped a level would fail open for it.
_UID_PATH_LEVELS = ("patient", "study", "series", "instance")


def _why_excluded(options, patient, study, series, instance) -> Optional[str]:
    """Why an instance is filtered out of the export, or None if it is not.

    Two independent filters, both matching at every level of the hierarchy: a
    subset includes only what the caller selected, and the safety scan
    withholds anything still carrying an identifier. `None` means a filter is
    not in use; an empty set means it is in use and matched nothing. An
    instance the caller did not select is never reported as withheld.

    Args:
        options (_ExportOptions): The filters: `allowed_uids` (the subset) and
            `identifying_uids` (the scan's UIDs).
        patient (Patient): The instance's patient.
        study (Study): The instance's study.
        series (Series): The instance's series.
        instance (Instance): The instance.

    Returns:
        Optional[str]: `OUTSIDE_THE_SUBSET` for an instance the subset did not
            select (no log); the level (`"patient"`, `"study"`, `"series"` or
            `"instance"`, the first of `_uid_path`'s UIDs the scan raised a
            finding on) for one the scan withheld (logged); None otherwise.
    """
    uids = _uid_path(patient, study, series, instance)

    # The subset is tested first, and the order is load-bearing: an
    # instance the caller did not select was never asked for. Tested the
    # other way round, it would be logged as "still carries identifiers"
    # and written as a withheld-instance audit row, grading a run on an
    # instance outside the export entirely.
    if options.allowed_uids is not None and not any(
            uid in options.allowed_uids for uid in uids):
        return OUTSIDE_THE_SUBSET

    if options.identifying_uids is not None:
        for uid, level in zip(uids, _UID_PATH_LEVELS):
            if uid in options.identifying_uids:
                get_logger().warning(
                    "Skipping %s: it or one of its parents still carries "
                    "identifiers.", instance.sop_instance_uid)
                return level

    return None


def _uid_path(patient, study, series, instance) -> Tuple[str, str, str, str]:
    """The four UIDs locating one instance in the hierarchy.

    Both export filters, the safety scan and the subset, match against every
    level, so a UID given for a study selects its images.

    Args:
        patient (Patient): The instance's patient.
        study (Study): The instance's study.
        series (Series): The instance's series.
        instance (Instance): The instance.

    Returns:
        Tuple[str, str, str, str]: Patient ID, Study, Series and SOP Instance
            UIDs.
    """
    return (patient.patient_id, study.study_instance_uid,
            series.series_instance_uid, instance.sop_instance_uid)


#: The elements `export()` stamps to say how a file was de-identified:
#: Patient Identity Removed, De-identification Method, and
#: Longitudinal Temporal Information Modified. A rule on any of them, of
#: any action, KEEP included, means the configuration decides that element
#: and the export does not stamp it.
_IDENTITY_REMOVED, _DEID_METHOD, _TEMPORAL_MODIFIED = (
    "0012,0062", "0012,0063", "0028,0303")


@functools.lru_cache(maxsize=None)
def _dictionary_vr(tag: str) -> Optional[str]:
    """A `gggg,eeee` key's dictionary VR.

    Args:
        tag (str): The key.

    Returns:
        Optional[str]: The VR, or None for a key that is not a tag (`_ISO...`
            bookkeeping, a malformed key) and for a tag the dictionary does not
            know, which every private tag is.
    """
    # Cached: the plan asks it for every element of every instance, and
    # the answer is a fact about the tag.
    try:
        group, element = (int(part, 16) for part in tag.split(","))
        return dictionary_VR((group << 16) | element)
    except (ValueError, KeyError):
        return None


def _date_state(value, vr: str, vouched: bool) -> str:
    """One DA or DT element as `(0028,0303)` reads it.

    Args:
        value (Any): The element's value.
        vr (str): `"DA"` or `"DT"`.
        vouched (bool): Whether a shift this store wrote vouches for the value,
            as the caller asked the item that holds it.

    Returns:
        str: `"gone"` when empty or every value is the VR's dummy (`VR_DUMMY`);
            `"shifted"` when vouched; `"found"` otherwise, a date as ingested,
            kept on purpose or not.
    """
    dummy = config_manager.VR_DUMMY[vr]
    values = (list(value) if isinstance(value, (list, tuple, MultiValue))
              else [value])
    if all(v is None or not str(v).strip() or str(v).strip() == dummy
           for v in values):
        return "gone"
    return "shifted" if vouched else "found"


def _longitudinal_temporal_marker(study, instance, stamps) -> Optional[str]:
    """What Longitudinal Temporal Information Modified `(0028,0303)` says about
    the file `instance` exports as.

    Every DA and DT element the file will carry is read: the instance's own,
    with the owner stamps in their place; every nested item's; and private
    elements whose recorded VR is DA or DT. TM is not read. The stamped Study
    Date `(0008,0020)` is vouched for by the `Study`
    (`Study.date_shift_vouches_for`), never by the instance's copy; any other
    stamped date is vouched by nothing.

    Args:
        study (Study): The instance's study.
        instance (Instance): The instance being exported.
        stamps (dict): What `export_stamp_attributes` writes over the
            instance's own top-level attributes.

    Returns:
        Optional[str]: `"REMOVED"` when every date is gone (or there is none);
            `"MODIFIED"` when at least one is shifted by this store and the
            rest are gone; None when any date is as found, and a source's value
            stays. `UNMODIFIED` is never returned.
    """
    # TM is not read: a time of day kept beside a shifted date does not
    # carry the patient's longitudinal position. `UNMODIFIED` is never
    # written: "as found" cannot tell "kept on purpose" from "unknown". An
    # element the worker then drops (a write-time loss, a foreign icon)
    # was still read here, which can only withhold the marker, never
    # write a false one.
    shifted = False
    for item, path in iter_item_tree(instance):
        attributes = item.attributes
        if not path:
            attributes = {**attributes, **stamps}
        for tag, value in attributes.items():
            vr = _dictionary_vr(tag)
            if vr is None and _is_private_tag(tag):
                vr = item.attribute_vrs.get(tag)
            if vr not in ("DA", "DT"):
                continue
            if not path and tag in stamps:
                vouched = (tag == "0008,0020"
                           and study.date_shift_vouches_for(study.study_date))
            else:
                vouched = item.date_shift_vouches_for(tag, value)
            state = _date_state(value, vr, vouched)
            if state == "found":
                return None
            shifted = shifted or state == "shifted"
    return "MODIFIED" if shifted else "REMOVED"


def _is_private_tag(tag: str) -> bool:
    """Whether a key is an odd-group `gggg,eeee` tag.

    Args:
        tag (str): The key.

    Returns:
        bool: True for an odd group; False for anything malformed.
    """
    try:
        return int(tag.split(",")[0], 16) % 2 == 1
    except ValueError:
        return False


#: The columns a subset DataFrame is read by, most precise first.
_SUBSET_FRAME_COLUMNS = ("SOPInstanceUID", "SeriesInstanceUID",
                         "StudyInstanceUID", "PatientID")


def _uids_from_frame(frame) -> List[Any]:
    """The values a subset DataFrame selects by, one per row in row order,
    from the most precise of `_SUBSET_FRAME_COLUMNS` present. Only that one
    column is read.

    Args:
        frame (pd.DataFrame): The caller's subset.

    Returns:
        List[Any]: The column's values.

    Raises:
        ValueError: If the frame has none of the four columns. A frame
            that has the column and no rows is a selection of nothing,
            and is not refused.
    """
    # Only one column, deliberately. A frame filtered down to the CT
    # series of a patient still carries that patient's ID in every row, so
    # adding PatientID to the set would pull the MR series back in and
    # undo the filter the caller asked for. A frame with none of the
    # columns is refused because it can never select anything, and read as
    # an empty selection the export would write nothing and say nothing.
    for column in _SUBSET_FRAME_COLUMNS:
        if column in frame.columns:
            return frame[column].tolist()
    raise ValueError(
        f"subset is a DataFrame with none of the columns it is read by "
        f"({', '.join(_SUBSET_FRAME_COLUMNS)}), so it could select nothing. "
        f"Pass a frame from get_cohort_report(), or one carrying one of "
        f"those columns.")


class _SubsetSelection(NamedTuple):
    """What a `subset` argument selected, from `_resolve_subset`:
    the shape `io_handlers.PatientSelection` has, so the two unmatched
    counts share one sentence's arithmetic."""

    #: The UIDs the walk lets through (the replacements included), or
    #: `None` for no subset.
    uids: Optional[Set[str]]
    #: How many values were given, counting each position.
    given: int
    #: The 1-based positions whose value names nothing in the session.
    unmatched: Tuple[int, ...]


def _report_phi_findings(findings) -> None:
    """Prints what the pre-export scan found, and how to configure it away.

    Prints each tag, its reason and its count, never a flagged value.

    Args:
        findings (Iterable[PhiFinding]): The scan's findings.
    """
    # Never a value: a flagged value printed here would reach the console
    # and any log capturing it. `finding.reason` is safe to print: every
    # reason the inspector writes is a literal or names the tag and the
    # config's own description.
    counts, descriptions = Counter(), {}
    for finding in findings:
        tag = finding.tag or finding.field_name
        counts[tag] += 1
        descriptions[tag] = finding.reason

    print("\nSafety Scan Found Issues")
    print("The following tags still carry identifiers:")
    print(f"{'Tag':<15} {'Description':<30} {'Count'}")
    print("-" * 56)
    for tag, count in counts.items():
        print(f"{tag:<15} {descriptions[tag][:28]:<30} {count}")

    _print_suggested_config(counts)


def _print_suggested_config(counts) -> None:
    """Prints a config fragment resolving every tag the scan flagged.

    The fragment is YAML in the shape `create_config()` writes, so it can
    be pasted into the user's file. Every rule is `REMOVE` except Patient
    ID's, which is `REPLACE` with no `value:` (the keyed pseudonym).
    Findings with no `gggg,eeee` tag get no rule and are counted in a
    comment instead.

    Args:
        counts (Counter): Findings per key, keyed the way the table labels
            a finding: `tag or field_name`.
    """
    # Only a `gggg,eeee` key becomes a rule: a finding with no tag --
    # burned-in text from `verification.py`, or one reloaded from the
    # store's `phi_findings` table, which keeps no tag -- would be a rule
    # keyed on its field name, which `load_config` refuses.
    #
    # Patient ID is REPLACE, never REMOVE: the ID is what keeps two
    # patients apart and `anonymize()` merges patients sharing one, so a
    # removed or emptied ID would collapse them, and the tag-policy rules
    # refuse any Patient ID rule but `KEEP` and that one. The fragment
    # must never suggest a rule `load_config` refuses.
    rules = {}
    untagged = 0
    for key, count in counts.items():
        if isinstance(key, str) and _is_tag_key(key):
            rules[key] = count
        else:
            untagged += count

    print("\nSuggested Config Update:")
    if rules:
        print("Add the following rules to your config to resolve these:")
        print()
        print("phi_tags:")
    for tag, count in rules.items():
        action = "REPLACE" if tag == _PATIENT_ID_TAG else "REMOVE"
        rule = {tag: {"name": _suggested_tag_name(tag), "action": action}}
        # Dumped per tag rather than as one mapping so the count can sit
        # above its own entry. yaml.dump owns the quoting -- a tag key
        # contains a comma, and hand-rolled quoting produces a document
        # nothing can read.
        block = yaml.dump(rule, sort_keys=False, default_flow_style=False)
        print(f"  # Found {count} times")
        for line in block.splitlines():
            print(f"  {line}")
    if untagged:
        print(f"# {untagged} finding(s) above carry no DICOM tag, so no "
              f"phi_tags rule can resolve them; review them by hand.")


#: The one tag whose suggested rule is not `REMOVE`.
_PATIENT_ID_TAG = "0010,0020"


def _suggested_tag_name(tag: str) -> str:
    """A readable name for a flagged tag, from the floor policy.

    Args:
        tag (str): The `gggg,eeee` key.

    Returns:
        str: The name `profiles.FLOOR_POLICY` gives the tag, which is the
            name the config file uses, or the tag itself when it has none.
    """
    # Falls back to the tag itself rather than to a placeholder name: the
    # name is a comment to the reader, and a tag repeated is at least
    # true, where three rules all called `unknown_tag` are
    # indistinguishable.
    entry = FLOOR_POLICY.get(tag)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return tag


def _lock_selection(value, option):
    """Read a selection for the lock pair.

    Read as every other method reads a selection, except that `None` is refused
    and an item may be a finding.

    Args:
        value (Any): The caller's selection.
        option (str): The parameter's name, for the error message.

    Returns:
        Any: What `normalize_id_filter` returns for it.

    Raises:
        TypeError: For `None` and any shape `normalize_id_filter` refuses.
    """
    # `None` is refused because there is no "lock everyone" spelling: read as
    # every patient, it would lock the whole session.
    return normalize_id_filter(
        value, option, allow_none=False,
        element=lambda item: isinstance(item, str) or hasattr(item, 'patient_id'),
        element_is="a str or a finding")


class LockingResult(list):
    """
    A list subclass that suppresses verbose REPL output for large datasets.
    """

    def __repr__(self):
        """A one-line summary instead of every instance.

        Returns:
            str: `<LockingResult: N instances secured>`, where N is the
                count of instances locked.
        """
        return f"<LockingResult: {len(self)} instances secured>"


#: The tags `lock_identities()` embeds when the caller names none.
_DEFAULT_TAGS_TO_LOCK = (
    "0010,0010",  # PatientName
    "0010,0020",  # PatientID
    "0010,0030",  # PatientBirthDate
    "0010,0040",  # PatientSex
    "0008,0050",  # AccessionNumber
)


def _redaction_worker_count() -> int:
    """How many workers to redact pixels with.

    `ISOCENTER_MAX_WORKERS` when it is set to an integer of at least 1;
    otherwise half the CPUs, at least one and at most eight. A malformed
    or smaller value is warned about and the default is used.

    Returns:
        int: The worker count.
    """
    # Each worker holds a decoded image, so the cap of eight is a memory
    # ceiling rather than a throughput choice: one worker per CPU can
    # exhaust memory on large studies. The floor on the override is
    # `_env_int`'s, so there is no clamp on it here.
    override = _env_int("ISOCENTER_MAX_WORKERS", minimum=1)
    if override is not None:
        return override
    # The `max(1, ...)` must stay: `cpu_count() // 2` is `0` on a one-CPU box.
    return max(1, min((os.cpu_count() or 1) // 2, 8))


#: Why processes can never redact a `:memory:` store. Shared by the
#: warning and the refusal below, because it is one fact and two
#: spellings of one fact is what this project's conventions forbid.
_WHY_PROCESSES_CANNOT_REDACT_A_MEMORY_STORE = (
    "a spawned worker is handed _memory_conn=None by "
    "SqliteStore.__setstate__ and opens a fresh, empty in-memory database "
    "with no instance_blobs table")


def _report_processes_lever_on_a_memory_store(db_path, strategy):
    """Warn or refuse when a `:memory:` `redact()` was asked for processes.

    Does nothing for a file-backed store or when nobody asked for
    processes. When the request was discarded and the pass will run in
    threads, logs one WARNING; when it was obeyed, refuses.

    Args:
        db_path (str): The store's path; only `":memory:"` is checked.
        strategy (_Strategy): The execution strategy `redact()` resolved.

    Raises:
        RuntimeError: When the store is `:memory:` and a lever obtained
            processes. Plain, not `RedactionError`, because nothing was
            attempted.
    """
    # Two cases, told apart by `strategy.use_threads`, never by the
    # lever's name. `use_threads` True: the request was discarded and the
    # pass runs correctly in threads, so this warns. False: the request
    # was obeyed, and every redaction worker ends in `persist_pixel_data`,
    # which a process cannot reach on an in-memory database, so this
    # refuses.
    #
    # On the redaction path only two rows are reachable: `redact()` passes
    # `force_threads=True` for a `:memory:` store, which sits at rank 2,
    # so the only lever that can make `use_threads` false is rank 1,
    # worker recycling. Written on `use_threads` all the same, so a lever
    # added at a future rank is classified correctly without touching
    # this function.
    #
    # `strategy.processes_requested_by` is read, never re-derived: a
    # session-side `_env_is("ISOCENTER_FORCE_PROCESSES", ...)` would
    # re-encode the rank-2-beats-rank-3 ordering in a second file and
    # would speak up for an operator who set both force levers, whose
    # effective request is threads.
    #
    # Plain `RuntimeError`, not `RedactionError`: `RedactionError` is a
    # `RuntimeError`, and a caller writing `except RedactionError` to
    # handle a partial pass would read "your environment cannot run this"
    # as "some images failed".
    if db_path != ":memory:":
        return
    lever = strategy.processes_requested_by
    if lever is None:
        # Nobody asked. On 3.12 the ranking's last rank makes processes
        # the default for a store with no lever set, and a default is
        # not a request -- this is the line that keeps
        # `Session(":memory:")` working out of the box on the floor.
        return

    if strategy.use_threads:
        # Four properties this message holds. It names no knob the reader
        # did not set (`force_threads` appears nowhere -- redact() set
        # that, not them); it says in as many words that the result is
        # correct, because a warning in front of a correct result that
        # does not say so sends the reader looking for damage that is not
        # there; it bounds itself, since the fact an operator needs is
        # that their variable works at every step but this one; and it is
        # emitted once per `redact()` call rather than once per
        # `run_parallel`.
        get_logger().warning(
            '%s had no effect on this run. redact() requires threads on a '
            '":memory:" store and asks for them per call, and that request '
            'outranks the variable, so this pass ran in threads and its '
            'result is correct. Processes cannot redact a ":memory:" store '
            'at all: %s. The variable still applies to every other parallel '
            'pass in this process. If you set it expecting redaction in '
            'processes, that needs a file-backed store -- '
            'Session("session.db").',
            lever, _WHY_PROCESSES_CANNOT_REDACT_A_MEMORY_STORE)
        return

    raise RuntimeError(
        f'redact() cannot run on a ":memory:" store with {lever} set. '
        f'A redaction worker writes its redacted frame back to the store, '
        f'and {_WHY_PROCESSES_CANNOT_REDACT_A_MEMORY_STORE}, so processes '
        f'are never correct for this store. Only multiprocessing.Pool '
        f'implements worker recycling, so this call would have run in '
        f'processes and every task would have failed with "no such table: '
        f'instance_blobs". Unset {lever} for this session, or use a '
        f'file-backed store -- Session("session.db") -- where processes are '
        f'the default.')


def _same_stashed_value(new_value, kept) -> bool:
    """Whether `new_value` would stash exactly what a token already holds.

    Compares the JSON the token is built from.

    Args:
        new_value (Any): The value about to be stashed.
        kept (Any): The value the token already holds.

    Returns:
        bool: True when both serialize to the same JSON; False when they
            differ or either cannot be serialized (the token build refuses
            such a value anyway).
    """
    try:
        return json.dumps(new_value, sort_keys=True) == json.dumps(kept, sort_keys=True)
    except (TypeError, ValueError):
        return False


def _unheld_spelling(value):
    """How the lock's grouping key spells a value JSON cannot hold.

    Equal unheld values share a group, and the token build refuses that
    group, naming the tag. The spelling never reaches a token.

    Args:
        value (Any): A value `json.dumps` cannot serialize.

    Returns:
        list: A marker, the value's type name and its repr.
    """
    return ["\x00unheld", type(value).__name__, repr(value)]



def _edited_since_its_status(entity) -> bool:
    """Whether a status recorded on `entity` is stale (grade condition 8).

    Args:
        entity (TrackedEntity): A `Patient`, `Study` or `Instance`; the
            scan records nothing on a Series, so none is passed.

    Returns:
        bool: True when a status other than UNSCANNED was recorded at a
            revision the entity has since left.
    """
    # The raw record is read, not `phi_status`: a status that no longer
    # applies reads UNSCANNED there, exactly as a never-scanned entity
    # does, and only the record tells the two apart. On a patient or a
    # study the revision moves on an assignment of a field the export
    # writes or a scan reads (`entities._assign_tracked_field`); on an
    # instance, on any change through its methods or to an item nested in
    # it (`DicomItem.mark_modified`), on an assignment of its
    # `sop_instance_uid`, and on an assignment of a field of its series.
    # On the ordinary paths everything the library itself writes after a
    # scan records a status after it: remediation stamps what it wrote, a
    # scan records what it read, and redaction and the reversible lock
    # carry the status they found when it still applied.
    status, recorded_at = entity._phi_status, entity._phi_status_revision
    return (status is not None and status is not PhiStatus.UNSCANNED
            and recorded_at != entity._revision)

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
                Defaults to `ISOCENTER_DB_PATH`, then `"isocenter.db"`.
                `":memory:"` is accepted and is part of the frozen surface:
                the index lives in memory and the pixel sidecar in
                a temporary file the store unlinks on `close()`. On such a
                store `redact()` runs in threads on every interpreter,
                because its worker writes to the store and a process
                cannot share an in-memory database; with
                `ISOCENTER_MAX_TASKS_PER_CHILD` set, recycling overrides
                that and the call fails -- see that variable's row in
                `docs/environment.md`.
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

        # Detect descriptor damage an earlier release persisted
        # at the moment the store opens, so a session holding it
        # cannot run to a clean-looking export first. Detection only, on
        # purpose -- the sidecar's bytes are shape-free, so any repair
        # would be a best-effort guess; the remedy is in the message and
        # the same result reaches `generate_report`, where it costs the
        # run its PASS through the COMPLIANCE_CHECK channel.
        for uid, _path, details in self.store_backend.check_pixel_geometry():
            get_logger().warning(f"{uid}: {details}")

        self._audit_pre_1_0_id_less_groups()

        # Initialize Configuration Object
        self.configuration = IsocenterConfiguration()

        # Reversibility
        self.key_manager = None
        self.reversibility_service = None
        # The policy the last `audit()` resolved, whichever door it came
        # by. `audit(config_path=)` does not assign
        # `configuration.phi_tags`, and the lock judges a name or ID by the
        # rule that wrote it. None until an audit runs.
        self._audited_phi_tags = None

        # What the last DICOM export delivered, so the compliance report
        # can say how many instances were written beside how many are
        # indexed. None means "no export has run in this
        # session", which is not the same as "nothing was written" --
        # the report omits the row rather than claiming a zero.
        self._last_export_written = None
        self._last_export_requested = None
        # What the last `audit()` raised, per scan-time entity uid, so a
        # partial `anonymize(findings=...)` cannot stamp REMEDIATED over
        # identifiers it was not handed. None until an audit runs,
        # and not persisted: a reopened session keeps pass accounting.
        self._scan_tally = None
        # This session's working copy of each kept report's scan tally:
        # one per *audit*, keyed by
        # the audit token its tally carries (`_ScanTally._audit`), so
        # partial passes over one audit's reports -- copied, deep-copied,
        # pickled or loaded again -- complete one another as they would in
        # the session that scanned it (a patient-level pass now, the
        # instances later), while another session starts from its own
        # copy. A plain dict: nothing else holds a working copy, so a weak
        # mapping would drop it between passes. It keeps one entry per
        # audit handed to this session, two ints per uid it has not
        # completed plus the handled keys of any uid a pass left partial,
        # for the life of the session.
        self._report_tallies = {}
        # Every policy an `audit()` in this session scanned under,
        # fingerprint -> base. A status recorded under one of these
        # raises no export notice: the user chose it in
        # this session, by `audit(config_path=)` or by the configuration
        # then in force. Not persisted, deliberately: the notice is about
        # a policy change across sessions, and a fresh session has
        # scanned nothing.
        self._scanned_policies = {}

        # The verbs this session actually performed ("REDACTION",
        # "ANONYMIZE"), so `generate_report` can demand action-specific
        # evidence: a redacting run whose REDACTION rows were lost to a
        # second defect must not grade PASS on the strength of unrelated
        # rows. Transient, in-memory and session-scoped on
        # purpose -- persisted, this would be a second durable answer to
        # "what happened" that can disagree with the audit log. A verb
        # is recorded only where the run would have emitted
        # its audit rows, so a call that performed no work demands no
        # evidence; see the two recording sites.
        self._actions_performed: Set[str] = set()

        # Each `scan_pixel_content()` call this session made, for the
        # report's section 5, so it says whether pixel data was scanned.
        # Transient for `_actions_performed`'s
        # reason above, which is also why section 5 says "in this
        # session": a scan an earlier session ran over this store is not
        # known here, and the report says so rather than guessing.
        self._pixel_scans: List[PixelScanSummary] = []

        if os.path.exists("isocenter.key"):
            self.enable_reversible_anonymization("isocenter.key")

        # Shared Global Executor for Process Consistency.
        #
        # Spawn, not fork -- the same pin, for the same reason, as both
        # pools in parallel.py: a forked worker inherits the parent's
        # open SQLite handles and its sidecar file position, and this
        # session's threads (persistence drain, audit writer) can be
        # mid-write at any fork. Linux 3.12 defaults to fork; macOS to
        # spawn, so a fork-only failure does not show on a Mac.
        # How wide the pool below was built, and how many `ingest()`
        # calls are running on it right now. The width is recorded
        # rather than read back from the executor's private
        # `_max_workers`, and it moves with **every** assignment of
        # `self._executor` -- here, `_restart_executor()` and
        # `_ingest_executor()` -- because a stale number makes the next
        # ingest either skip a rebuild it needed or rebuild for nothing.
        # The counter exists so a resize never touches a pool a peer
        # ingest is mid-flight on; `_ingest_executor()` is the only
        # reader of both.
        #
        # `_ingest_lock` guards exactly those two fields and the swap.
        # It is **never held while any other lock is taken** -- not the
        # sidecar pass-lock, not the gate, not sqlite, and not a logging
        # handler's own lock, which the `PIXEL_STATE_LOCK` convention
        # counts as one -- so it is not part of the documented
        # lock order in CLAUDE.md and must not be made part of it: it is
        # taken and released at the top of `ingest()` with nothing held,
        # and again in that call's `finally` after the pass-lock has been
        # released, and once more after `import_files` returns, when the
        # pool broke during it, to replace it (`_restart_executor`)
        # -- after the pass-lock is released there too.
        # `_ingest_executor()` keeps that true by capturing its decision
        # as data and logging it, and retiring the pool it swapped out,
        # only after the lock is released; `_restart_executor()` does the
        # same with its log line and teardown. The one thing
        # under it that takes any lock at all is the replacement pool's
        # constructor, which takes the stdlib's own internal locks.
        self._executor_width = resolve_max_workers()
        self._ingest_lock = threading.Lock()
        self._ingests_in_flight = 0

        self._executor = concurrent.futures.ProcessPoolExecutor(
            # Sized by the resolver `run_parallel` uses, so
            # `ISOCENTER_MAX_WORKERS` narrows `ingest()` as it narrows
            # every other parallel step; not `None`, which is the
            # stdlib's one worker per CPU whatever the variable says.
            # Each `ingest()` re-resolves this and rebuilds the pool when
            # the width has changed, so the variable is honoured whenever
            # it is set rather than only here.
            max_workers=self._executor_width,
            mp_context=multiprocessing.get_context("spawn"),
            # The same env-gated worker setup as run_parallel's pools
            # (GC off, child-side faulthandler watchdog); resolved by
            # the one resolver so the session's own pool cannot drift
            # from the per-call ones.
            initializer=resolve_worker_initializer())

        if db_exists:
            print(f"Loaded session from {self.persistence_file}")

        get_logger().info(f"Session started. {len(self.store.patients)} patients loaded.")

    def __enter__(self) -> "DicomSession":
        """Support `with DicomSession(...) as session:`.

        The block calls `close()` on exit, which releases the process pool
        and the two threads holding sqlite handles.

        Returns:
            DicomSession: This session.
        """
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        """Close the session, including when the body raised.

        Exceptions from the body propagate.

        Args:
            exc_type (Optional[type]): The exception type raised in the
                body, or None.
            exc (Optional[BaseException]): The exception raised in the
                body, or None.
            traceback (Optional[TracebackType]): Its traceback, or None.
        """
        self.close()

    def close(self):
        """
        Shut the session down: the persistence-manager thread, the audit
        thread that owns the sqlite connection, and the process pool. A
        session that is never closed leaks its worker subprocesses for the
        life of the process.

        All three steps run even if an earlier one raises. If more than
        one fails, the first failure is raised and the later ones are
        logged. A second call is safe; it repeats the shutdown messages and
        the unsaved-instances WARNING.
        """
        print("Closing session persistence...")
        first_exception = None

        def _run_step(step):
            nonlocal first_exception
            try:
                step()
            except Exception as exc:  # pylint: disable=broad-except
                get_logger().error(f"Error during session close(): {describe_exception(exc)}", exc_info=True)
                if first_exception is None:
                    first_exception = exc

        # Order is load-bearing. `shutdown()` reconciles a save its
        # worker never finished and records an ERROR audit row when it
        # cannot; `stop()` flushes the audit queue, so those rows
        # settle before close() returns. Reversed, they would be written
        # into a queue nothing drains again.
        if hasattr(self, 'persistence_manager'):
            _run_step(self.persistence_manager.shutdown)

        # After `shutdown()`, and deliberately not through `_run_step`.
        # Order: `shutdown()` reconciles a save its worker never finished,
        # so asking before it would report instances that were
        # about to be marked persisted. Not `_run_step`, because that
        # records the first exception for `close()` to re-raise -- and a
        # bug in a diagnostic that turned `close()` into a raise would
        # abort before the executor shut down, leaking worker
        # subprocesses for the life of the interpreter, which is the
        # exact failure this method's ordering exists to prevent. It
        # swallows its own errors instead.
        self._warn_about_unsaved_instances()

        if hasattr(self, 'store_backend'):
            _run_step(self.store_backend.stop)  # Stops audit thread

        if hasattr(self, '_executor'):
            print("Shutting down process pool...")
            _run_step(lambda: self._executor.shutdown(wait=True))

        if first_exception is not None:
            raise first_exception

    def _warn_about_unsaved_instances(self):
        """Log a WARNING when `close()` is about to drop unsaved edits.

        Names up to three unsaved instances and counts the rest. Silent
        when every instance is saved; a second `close()` warns again. The
        message is a log line only, so under `ISOCENTER_LOG_LEVEL=ERROR` or
        above it is not shown. Never raises.
        """
        # Instances only. `SqliteStore.save_all` calls `mark_persisted()`
        # on instances and on nothing else, so every built-then-saved
        # patient, study and series reports `has_unsaved_changes` forever;
        # warning on those would fire on every correct session. Widening
        # this means fixing the save walk first, with per-parent revision
        # capture, not widening the walk here.
        #
        # A warning and not an audit row: a row written here would land
        # between `persistence_manager.shutdown()` and `store_backend.stop()`
        # and could only be read by a later session's report. It is not
        # also `print`ed, because the console handler already shows it.
        try:
            unsaved = [inst
                       for p in self.store.patients
                       for st in p.studies
                       for se in st.series
                       for inst in se.instances
                       if inst.has_unsaved_changes]
            if not unsaved:
                return

            named = ", ".join(
                # `sop_instance_uid` is `""` by dataclass default and can
                # be `None` on a hand-built or partly-ingested instance.
                # `""` joins fine and names nothing ("Affected: ."); `None`
                # raises `TypeError` into the guard below and the warning
                # is lost outright -- for the graph most in need of one,
                # since an instance with no UID is the one a caller can
                # least easily find again.
                #
                # The fallback has to *locate*, not merely be joinable:
                # three instances rendered as "<unknown>, <unknown>,
                # <unknown>" tell the caller nothing.
                # `source_path` and not `file_path` -- redaction detaches
                # `file_path` (see the field in `entities.py`), and
                # redaction is exactly what leaves an instance dirty at
                # close. `instance_number` is `int = 0` and never `None`,
                # so the last arm is total and the chain cannot raise.
                i.sop_instance_uid or i.source_path
                or f"<unidentified instance {i.instance_number}>"
                for i in unsaved[:3])
            if len(unsaved) > 3:
                named += f", and {len(unsaved) - 3} more"
            message = (
                f"Closing with {len(unsaved)} instance(s) holding unsaved "
                f"changes; they will not reach the store. Affected: "
                f"{named}. Call save(sync=True) before close() to keep "
                f"them.")
            get_logger().warning(message)
        except Exception:  # pylint: disable=broad-except
            # A diagnostic that cannot run is a diagnostic that is
            # missing, which is what the caller had before this existed.
            # A diagnostic that raises is a close() that leaks an
            # executor.
            get_logger().debug(
                "Could not check for unsaved instances during close().",
                exc_info=True)

    def save(self, sync: bool = False):
        """
        Persist the current session state to the store.

        Args:
            sync (bool): If True, block until the save is complete. A
                synchronous save first drains the persistence manager, as
                `audit()` and `redact()` do, and never returns early: a
                background save that does not finish blocks it.
        """
        if sync and hasattr(self, 'store_backend'):
            get_logger().info("Saving session (Synchronous)...")
            if hasattr(self, 'persistence_manager'):
                self.persistence_manager.flush()
            self.store_backend.save_all(
                self.store.patients, prune_absent_patients=True)
        elif hasattr(self, 'persistence_manager'):
            # The session owns the whole store, so rows for patients it no
            # longer holds are stale -- including the pre-anonymisation row
            # of a patient whose identifier has since changed.
            self.persistence_manager.save_async(
                self.store.patients, prune_absent_patients=True)

    def _restart_executor(self, max_workers=None, *, broken=None):
        """Replace a broken shared process pool.

        `ingest()` calls this, with `broken=`, after a worker of the shared
        pool ended, so a later `ingest()` runs on a working pool. Work
        still queued on the old pool is cancelled, so a healthy resize
        must go through `_ingest_executor()` instead. Thread-safe.

        Args:
            max_workers (int, optional): The new pool's width. Defaults to
                `ISOCENTER_MAX_WORKERS`, else one per CPU, read now.
            broken (concurrent.futures.Executor, optional): The pool the
                caller saw break. When given, the pool is replaced only if
                it is still the session's.

        Raises:
            OSError: When the new pool cannot be built; the session keeps
                the pool it had.
        """
        # `broken` makes the swap a compare-and-swap: two `ingest()` calls
        # on two threads share the pool, so both see it break and both
        # call this; without the check the second would shut down the
        # replacement the first had just built. The check and the swap are
        # one critical section under `_ingest_lock`; the log line and the
        # teardown run after it is released. The replacement is built
        # before the old pool is retired, so a constructor that raises
        # `OSError` (EMFILE, ENOMEM) leaves the session as it was.
        if max_workers is None:
            # Not the stdlib's `None`, which is one per CPU. An OOM restart
            # that widened the pool back past `ISOCENTER_MAX_WORKERS`
            # would undo the setting at exactly the moment memory is short.
            max_workers = resolve_max_workers()
        with self._ingest_lock:
            if broken is not None and self._executor is not broken:
                # Another caller has replaced the pool that broke already;
                # the one here now is live.
                return
            # Re-init, with the same spawn pin as construction: an OOM
            # recovery must not quietly downgrade the pool to fork,
            # nor drop the worker setup construction resolved.
            replacement = concurrent.futures.ProcessPoolExecutor(
                max_workers=max_workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=resolve_worker_initializer())
            retired, self._executor = self._executor, replacement
            # The recorded width follows every swap. A restart
            # that narrowed the pool for an OOM recovery and left this at
            # the old number would make the next `ingest()` either
            # rebuild for nothing or -- with the variable narrowed to
            # match -- skip the rebuild it needed and run at the recovery
            # width in silence.
            self._executor_width = max_workers
        get_logger().warning(f"Restarting ProcessPoolExecutor (max_workers={max_workers})...")
        if retired:
            try:
                # Force kill old processes if they are stuck/broken
                retired.shutdown(wait=False, cancel_futures=True)
            except (RuntimeError, OSError) as exc:
                # The executor is being replaced regardless; a failure to
                # shut the old one down is worth a line in the log, not a
                # crash.
                get_logger().debug("Could not shut down prior executor: %s", describe_exception(exc))

    @contextlib.contextmanager
    def _ingest_executor(self):
        """The shared pool for one `ingest()`, at the width asked for now.

        A context manager. `ISOCENTER_MAX_WORKERS` is re-read on entry and the
        pool is rebuilt when the width has changed; an unchanged width costs
        nothing. When another `ingest()` of this session is already running on
        the pool, a changed width is logged as a WARNING and this call runs on
        the pool as it stands. Thread-safe.

        Yields:
            concurrent.futures.ProcessPoolExecutor: The pool to dispatch on.
                Use it rather than re-reading `self._executor`.

        Raises:
            OSError: When a rebuild cannot create the new pool; the session
                keeps the pool it had.
        """
        # A peer ingest is never disturbed. The pool is the only thing two
        # concurrent ingests share. Cancelling the peer's futures (what
        # `_restart_executor()` does for a broken pool) would drop the files it
        # has not started with nothing raised; `shutdown(wait=True)` would make
        # a public method wait out the peer's whole ingest; a second pool
        # beside the first doubles the process count on a box whose operator is
        # narrowing workers because memory is short.
        #
        # The check, the swap and the snapshot are one critical section, and
        # the dispatch uses the snapshot: split, a peer can read the old pool,
        # this call shuts it down, and the peer's `map()` raises `cannot
        # schedule new futures after shutdown`.
        #
        # The replacement is built before the old pool is retired, and the
        # counter moves after the swap. A rebuild can raise `OSError` (EMFILE,
        # ENOMEM), the failure mode of the memory-short machine being narrowed.
        # Built first, nothing has been mutated when it raises. Retired first,
        # `self._executor` would point at a dead pool and, with the width left
        # stale, every later `ingest()` would fail. The counter moves after the
        # swap because an exception before the `try` escapes `__enter__` and
        # the `finally` never runs; a counter left at 1 is a phantom peer that
        # blocks every later resize. Building first does not double the process
        # count: a `ProcessPoolExecutor` spawns no worker until the first task
        # is submitted, and the retirement completes before this call
        # dispatches anything.
        #
        # Neither the log lines nor the teardown run under `_ingest_lock`:
        # `shutdown(wait=True)` under it would block every later `ingest()`
        # behind a wedged worker, and a logging handler takes its own lock,
        # which `_ingest_lock` is never held across. The `try` begins the
        # instant the lock is released, so everything between the increment and
        # the decrement is inside it; only the snapshot, which cannot raise,
        # sits between the increment and the release.
        requested = resolve_max_workers()
        retired = None
        resized = None
        skipped = None
        with self._ingest_lock:
            # Not `- 1` after an increment: the increment is below, after
            # the fallible rebuild.
            peers = self._ingests_in_flight
            if requested != self._executor_width:
                if peers:
                    skipped = (requested, self._executor_width, peers)
                else:
                    replacement = concurrent.futures.ProcessPoolExecutor(
                        max_workers=requested,
                        mp_context=multiprocessing.get_context("spawn"),
                        initializer=resolve_worker_initializer())
                    resized = (self._executor_width, requested)
                    retired, self._executor = self._executor, replacement
                    self._executor_width = requested
            self._ingests_in_flight += 1
            executor = self._executor

        try:
            if skipped:
                get_logger().warning(
                    "ISOCENTER_MAX_WORKERS asks for %s worker(s) and "
                    "this session's shared process pool has %s, but "
                    "%s other ingest() is already running on that "
                    "pool in this session, so this ingest() ran at "
                    "%s. Resizing the pool now would cancel the "
                    "files the other ingest() has not started yet. "
                    "The next ingest() that starts with no other "
                    "ingest() in flight is built at %s.",
                    skipped[0], skipped[1], skipped[2], skipped[1],
                    skipped[0])
            elif resized:
                get_logger().info(
                    "Resizing the session's shared process pool from "
                    "%s to %s worker(s): ISOCENTER_MAX_WORKERS has "
                    "changed since the pool was built.",
                    resized[0], resized[1])
                self._retire_shared_executor(retired)
            yield executor
        finally:
            with self._ingest_lock:
                self._ingests_in_flight -= 1

    def _retire_shared_executor(self, executor):
        """Shut a swapped-out shared pool down, waiting for its workers and
        cancelling nothing.

        The caller must have established that no `ingest()` is running on it.

        Args:
            executor (concurrent.futures.Executor): The pool to retire, or None
                to do nothing. Passed in because `self._executor` is already
                the replacement by the time this is called.
        """
        if not executor:
            return
        try:
            executor.shutdown(wait=True)
        except (RuntimeError, OSError) as exc:
            # The pool has been replaced regardless; a failure to shut
            # the old one down is worth a line in the log, not a crash.
            get_logger().debug("Could not shut down the prior shared "
                               "executor: %s", describe_exception(exc))

    def release_memory(self):
        """
        Release cached pixel and waveform data from every instance.

        Each instance goes through `Instance.unload_pixel_data()`, with its
        precondition: an array replaced through `set_pixel_data()` and not
        since written is kept. An array mutated **in place** is not
        tracked, so it is dropped here, and the next `get_pixel_data()`
        returns the frame from before the mutation. Only an array a save
        has already written can be mutated in place: a frame read from a
        file or from the store is read-only.

        Cached waveform samples are int16 of shape (num_samples,
        num_channels): about 80 KB for a 10-second 12-lead, about 104 MB
        for a 24-hour 3-channel Holter.

        Its progress bar follows `ISOCENTER_SHOW_PROGRESS`.
        """
        self._release_memory(show_progress=True)

    def _release_memory(self, show_progress: bool):
        """`release_memory()`, with the caller's `show_progress`.

        `_export_dicom` calls this so `export(show_progress=False)` draws no
        "Releasing Memory" bar.

        Args:
            show_progress (bool): Whether to draw the progress bar.
        """
        # Private so the public method keeps its frozen, parameterless
        # signature.
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

        with progress_bar(total=total_instances, show=show_progress,
                          desc="Releasing Memory", unit="inst") as pbar:
            for p in self.store.patients:
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            count += 1
                            # Both unloads report True when there was
                            # nothing cached, so the return value alone
                            # cannot tell "released" from "there was
                            # none". Counting it as freed would report
                            # every instance in a session holding nothing
                            # as reclaimed.
                            had_pixels = inst.pixel_array is not None
                            had_waveform = inst.waveform_array is not None

                            # `unload_pixel_data`, deliberately: an array
                            # replaced through `set_pixel_data()` and not
                            # yet written is refused here and stays
                            # resident. Do not "optimise" this to
                            # `discard_pixel_data()`; that would free more
                            # memory by throwing away pixels no one else
                            # holds.
                            #
                            # An array mutated in place is not tracked,
                            # so it is still dropped here silently --
                            # the limit `unload_pixel_data()`'s own
                            # docstring states.
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
        Rewrite the pixel sidecar (`_pixels.bin`) to reclaim the space of
        frames no instance references, and point every loader at the new
        offsets.

        An expensive I/O operation. It starts with `save(sync=True)`, so it
        waits for a background save that is running.

        Two behaviours are contract, observable from any thread of this
        session:

        1. It **raises `RuntimeError`** while a `redact()` or `ingest()`
           pass is open on the same store. The check comes first, before
           the leading save, so a refused call has done nothing.
        2. A `redact()` or `ingest()` that starts while it is saving or
           rewriting **waits**, bounded by 180 s, and then proceeds.

        Every frame writer, and this method for the whole rewrite, holds
        the sidecar lock (`<sidecar>.lock`, a cross-process `fcntl.flock`),
        so a frame written while compaction runs lands in the compacted
        file. A writer that cannot take the lock within 180 s raises
        `RuntimeError` naming the lock file; a background save that expires
        this way is logged as `Background save failed` and its instances
        stay unsaved for the next save.

        The rewrite holds the lock for its whole length, about 0.2 s per GB
        on local SSD. A `close()` whose persistence worker is queued behind
        a compaction longer than 30 s reports that worker as wedged.

        Raises:
            RuntimeError: While a `redact()` or `ingest()` pass is open on
                this store; when a save is still queued on the persistence
                manager after the leading save; or when the sidecar lock
                cannot be taken within 180 s.
        """
        if hasattr(self, 'store_backend'):
            print("Beginning Sidecar Compaction (this may take a while)...")

            # The refusal comes FIRST, before the leading save,
            # holding nothing, and is held through the rewire. Placed
            # between the save and the rewrite, it would admit a pass
            # that opens after the save's rows are written and closes
            # before the rewrite, with its `instances` rows on the old
            # UIDs and its blob rows on the regenerated ones (`redact()`
            # does not save at its end), and `_read_blob_index`'s
            # EXISTS predicate would reclaim every worker frame. Taken
            # here, a pass opening at any point of this method waits at
            # its own SH until the rewire is done. `LOCK_NB`: the
            # refusal is an answer, not a wait. Lock order
            # stays acyclic -- EX holding nothing, then the gate via
            # site 6 and below; passes take SH holding nothing; nothing
            # takes the pass-lock under the gate.
            with self.store_backend._refuse_while_pass_open():
                # 1. Sync DB so compaction knows true state
                self.save(sync=True)

                # The pending-save check, between the save and the
                # rewrite. What it does NOT cover: `has_pending_saves()`
                # reads the persistence manager's queue and in-flight set,
                # so a `save(sync=True)` on another thread and a
                # redaction's `persist_pixel_data` are both invisible to
                # it. The gate
                # and the pass-lock are what stop that population.
                if (hasattr(self, 'persistence_manager')
                        and self.persistence_manager.has_pending_saves()):
                    raise RuntimeError(
                        "compact() requires that no pending save be "
                        "outstanding: a background save writing pixel "
                        "state while the sidecar is rewritten leaves "
                        "loaders on offsets that no longer exist. Flush "
                        "the persistence manager and stop other writers "
                        "first (#295).")

                # The gate, taken AFTER the leading save above --
                # that save runs site 6 on this thread and would deadlock
                # against a gate already held -- and released only after
                # the rewire below: release it at the end of
                # `compact_sidecar()` and a writer slipping in before
                # `_rewire_sidecar_loaders` has a correct loader
                # overwritten from a map computed before its write
                # existed. `compact_sidecar()` itself is not gated, so
                # this method is the only place the hold and the rewire
                # are tied together.
                with self.store_backend._hold_sidecar_gate():
                    self._compact_under_gate()

        else:
            print("Persistence backend does not support compaction.")

    def _compact_under_gate(self):
        """`compact()`'s rewrite and rewire; the caller holds the gate."""
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

        # Nested pixel payloads are in exactly the same position, and
        # cannot ride `updates` for a sharper version of the same
        # reason: that map is keyed by UID alone, and one instance can
        # carry a bare `pixels` blob plus a row per icon. Keyed
        # `(uid, kind)` instead.
        nested_updates = self.store_backend.get_nested_pixel_refs()

        if not updates and not wave_updates and not nested_updates:
            print("Compaction finished (no changes or empty).")
            return

        # 3. Patch In-Memory Instances (Preserve References)
        print(f"Updating {len(updates)} in-memory instances...")
        count = self._rewire_sidecar_loaders(updates, wave_updates,
                                             nested_updates)

        print(f"Patched {count} active objects.")

    def _rewire_sidecar_loaders(self, updates, wave_updates,
                                nested_updates=None) -> int:
        """Point every in-memory sidecar loader at its post-compaction bytes.

        Takes `store_backend._pixel_swap_lock` per instance, around that
        instance's rebinds; the caller must not hold `_memory_lock` or
        `_audit_write_lock`.

        Args:
            updates (dict): `{sop_instance_uid: (offset, length)}` for pixels,
                as `compact_sidecar()` returns it.
            wave_updates (dict): The same for waveforms, read from the blob
                table because `compact_sidecar`'s map is pixels-only.
            nested_updates (dict): `{(uid, kind): (offset, length)}` for nested
                pixel payloads; one instance can carry several.

        Returns:
            int: How many loaders were rebound.
        """
        # The lock is taken per instance, around all its rebinds together:
        # `offset` and `length` are two assignments, and a reader landing
        # between them gets the wrong bytes or runs off the end of the sidecar.
        # Per instance rather than once around the loop because the offset map
        # is fully in hand before the loop starts, so the critical section
        # never spans a sqlite read. The lock is a leaf here: nothing is
        # acquired inside it.
        count = 0
        swap_lock = self.store_backend._pixel_swap_lock

        # We must traverse the whole graph.
        # DicomStore doesn't index by UID (yet).
        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        pixel_ref = updates.get(inst.sop_instance_uid)
                        # Note: a `_pixel_loader` of None (e.g. an
                        # instance loaded from its original DICOM file)
                        # does not use the sidecar, so there is nothing
                        # to update. Anything that was persisted has a
                        # loader, because `persist_pixel_data` creates
                        # one.
                        pixel_due = (
                            pixel_ref is not None
                            and isinstance(inst._pixel_loader,
                                           SidecarPixelLoader))

                        # Waveform loaders are patched from the blob
                        # table, not from `updates`: see the note in
                        # compact().
                        wave_ref = wave_updates.get(inst.sop_instance_uid)
                        wave_due = (
                            wave_ref is not None
                            and isinstance(inst._waveform_loader,
                                           SidecarWaveformLoader))

                        # Rebound under the same lock as the other two,
                        # for the same reason: `offset` and `length` are
                        # two assignments and a reader landing between them
                        # gets the wrong bytes or runs off the end of the
                        # sidecar.
                        nested_due = []
                        if nested_updates:
                            for key, ref in inst._nested_pixel_refs.items():
                                path, terminal_tag = key
                                moved = nested_updates.get((
                                    inst.sop_instance_uid,
                                    serialize_blob_kind(
                                        'pixels', path, terminal_tag)))
                                if moved is not None:
                                    nested_due.append((ref, moved))

                        if not pixel_due and not wave_due and not nested_due:
                            continue

                        with swap_lock:
                            if pixel_due:
                                # Deliberately does NOT clear
                                # `_pixel_array_unwritten`: compaction
                                # rewrites where a frame lives, not which
                                # frame it is, so a resident array that had
                                # diverged from the stored one still has
                                # diverged. Clearing here would make an
                                # unwritten array look saved because the
                                # sidecar was tidied.
                                inst._pixel_loader.offset = pixel_ref[0]
                                inst._pixel_loader.length = pixel_ref[1]
                                count += 1
                            if wave_due:
                                inst._waveform_loader.offset = wave_ref[0]
                                inst._waveform_loader.length = wave_ref[1]
                                count += 1
                            for ref, moved in nested_due:
                                ref.offset, ref.length = moved
                                count += 1

        return count

    def reconcile_private_tags(self) -> int:
        """Delete stored private-tag rows that the store's core attributes do not hold.

        A repair for a store de-identified before 0.9.1. There, a
        `remove_private_tags: true` pass removed the private tags from the
        graph but left their rows in the store's `instance_attributes`
        table; opening the store puts them back on the graph, and an export
        then carries them.

        It deletes every `instance_attributes` row whose tag is absent from
        its instance's stored core attributes, removes the same tags from
        the in-memory graph, and writes one `RECONCILE_PRIVATE` audit row
        per affected instance. The graph edit advances no revision: store
        and graph agree afterwards, nothing reads as unsaved, and stored
        PHI statuses are kept.

        **Call it only for a store you know was de-identified before
        0.9.1.** In a store that keeps its private tags
        (`remove_private_tags: false`, saved by 0.9.1 or later), those rows
        are the private data, and this deletes all of them. If unsure, run
        `anonymize()` and `save()` instead: a save removes the rows of tags
        the graph no longer holds. Nothing makes this choice automatically.

        Returns:
            int: `instance_attributes` rows deleted, not tags: a value
                with multiplicity 3 is three rows, and a tag holding an
                empty value is one row. 0 means nothing changed.
        """
        rows_deleted, dropped = self.store_backend.reconcile_private_tags()
        if not dropped:
            get_logger().info(
                "reconcile_private_tags: nothing to reconcile; every "
                "stored private row matches the core attributes.")
            return 0

        by_uid = {
            inst.sop_instance_uid: inst
            for p in self.store.patients
            for st in p.studies for se in st.series for inst in se.instances}
        for uid, tags in dropped.items():
            inst = by_uid.get(uid)
            if inst is not None:
                for tag in tags:
                    inst.attributes.pop(tag, None)
            self.store_backend.log_audit(
                action_type="RECONCILE_PRIVATE",
                entity_uid=uid,
                details=(
                    f"Dropped stored private tag(s) {', '.join(tags)}: "
                    f"absent from the instance's core attributes, so a "
                    f"pre-0.9.1 session never saw or exported them. "
                    f"Explicitly requested via "
                    f"reconcile_private_tags() (#172)."))

        get_logger().warning(
            f"reconcile_private_tags: dropped {rows_deleted} stored "
            f"private-tag row(s) across {len(dropped)} instance(s). "
            f"This is the opt-in repair for a store de-identified "
            f"before the 0.9.1 upgrade; if this store was meant to "
            f"keep its vendor block, restore from backup and do not "
            f"call this again.")
        return rows_deleted

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
        Ingest every DICOM file under a directory into the session store.

        Walks `directory` recursively, reads each DICOM file into the
        Patient -> Study -> Series -> Instance hierarchy, and saves the
        session when it finishes.

        A file that cannot be ingested does not raise. It is counted in the
        returned summary and gets an `ERROR` audit row naming the path and
        the reason, which bars a `PASS` grade. Check the return value: a
        run that rejected files completes normally.

        **A file that ends the worker process reading it** (the
        out-of-memory killer, a decoder crash, `SIGKILL`) is handled the
        same way. Results already returned are kept, and the files not yet
        returned are read again one at a time on a fresh one-worker process
        pool. A file is rejected only when a fresh worker ends on it as the
        first file it was given, with the reason "An ingest worker process
        ended before this file was returned, and a fresh worker process
        given this file alone, as its first file, ended while reading it".
        The rest are read at full width, the call saves as usual, and the
        session's pool is replaced. A death that does not recur costs no
        file and writes no row; a `WARNING` log line records it. If two
        fresh workers in a row cannot run a trivial task, every file left is
        rejected as "Not read", with a reason naming the causes that do this
        (a script without the main guard among them), and the call returns.
        A worker that ends on a later file had read others first, so that
        file is not blamed: reading starts again from it on another fresh
        worker. Any other failure of the worker pool raises.

        Each worker death costs a fresh pool, a few tenths of a second, so
        a run whose deaths do not recur can pay for several. A fatal file
        costs two or three, and up to 2 x `ISOCENTER_MAX_WORKERS` + 1 files
        read one at a time.

        **Duplicate SOP Instance UIDs.** A file whose SOP Instance UID an
        instance in this session already holds (ingested earlier in this
        call, by an earlier call, or loaded from the store) is declined:
        the instance is kept, the file is not read into the store, it is
        counted in `IngestSummary.declined`, and a `WARNING` audit row names
        the UID, the file, and the file the instance was ingested from. An
        instance the session already holds is always kept over a new file.
        Among files new to this call, the one whose path sorts first is
        kept, whatever order the filesystem lists them in; the sort is on
        the path string as walked (`os.path.join` of the directory and the
        name), the one the `WARNING` row prints. A declined file is not
        recorded as imported, so ingesting the same folder again declines
        it again.

        **Byte order.** A big-endian source's values in words wider than a
        byte (`OW`, `OL`, `OF`, `OD`, `OV` and the waveform samples) are
        stored little-endian, as its pixels are. What cannot be converted
        whole (a `UN` value, a length that is not a whole number of words,
        samples with no usable Waveform Bits Allocated) is kept as read and
        gets one `WARNING` audit row per element.

        **Environment.** `ISOCENTER_FORCE_THREADS` and
        `ISOCENTER_MAX_TASKS_PER_CHILD` have no effect: ingest runs on the
        session's own process pool, which has no threads mode and never
        recycles a worker. Each call that has files to read logs one
        `WARNING` when either is set. `ISOCENTER_MAX_WORKERS` is read on
        every call, and the pool is rebuilt when the width has changed; an
        unchanged width rebuilds nothing. While another `ingest()` in this
        session is running on the pool, a changed width is reported in one
        `WARNING` and this call runs at the pool's current width.

        **Concurrency.** An ingest holds the sidecar pass-lock, shared, for
        the whole import, and `compact()` on any thread of this session
        raises while it is held. While a `compact()` is saving or
        rewriting, this call waits (bounded, see `Raises`) and then
        proceeds. A result whose frame write cannot take the sidecar lock
        in time is rejected like any other failed file, with an `ERROR`
        audit row naming the path and the reason.

        Args:
            directory (str): The path to the directory containing DICOM files.

        Returns:
            IngestSummary: How many files reached the store, and
                `(path, reason)` for each one that did not.

        Raises:
            RuntimeError: If the ingest cannot start within 180 s because a
                `compact()` is still saving or rewriting the sidecar.
        """
        print(f"Ingesting from '{directory}'...")
        # The pass-lock, shared, around the import and not the
        # save after it. Ingest appends every
        # result's frames -- and commits the nested-icon and waveform
        # blob rows -- before the `instances` row that references them
        # exists, so a compaction inside this call would reclaim freshly
        # ingested frames as orphans. While this is held `compact()`
        # refuses; behind a running compaction this waits (bounded)
        # before the first worker is dispatched.
        # Outside the pass-lock, and entered before it: this re-resolves
        # `ISOCENTER_MAX_WORKERS` and rebuilds the shared pool when the
        # width has changed, and its own lock is never held while
        # the pass-lock is taken. The pool it yields is used as given --
        # `self._executor` is deliberately not read again below, so a
        # peer ingest that resizes between here and the dispatch cannot
        # pull this call's pool out from under it.
        #
        # `broken` receives that pool if a worker process of it ended
        # during the import. The import carries on by itself, on
        # pools of its own; the shared pool is replaced below, outside
        # both blocks, because `_restart_executor` takes `_ingest_lock`,
        # which is never taken with the pass-lock held.
        broken = []
        with self._ingest_executor() as executor:
            with self.store_backend._hold_pass_lock():
                # Pass Sidecar Manager for eager pixel writing
                summary = DicomImporter.import_files(
                    [directory],
                    self.store,
                    executor=executor,
                    sidecar_manager=self.store_backend.sidecar,
                    store_backend=self.store_backend,
                    on_executor_broken=broken.append)

        self.save(sync=True)
        # Save first, then replace the pool -- not the natural order of
        # "fix the pool, then save". A rebuild that came first and raised
        # anything the guard below does not catch would skip this save,
        # and leave every instance the import linked unsaved with its
        # frames unreferenced in the sidecar. `OSError` is what the
        # constructor raises on a
        # box out of descriptors or memory (EMFILE, ENOMEM); the import
        # has completed and saved by then, so it is a log line, and the
        # next `ingest()` finds the pool broken at its first file and
        # heals through the same retry and this same call.
        if broken:
            try:
                self._restart_executor(broken=broken[0])
            except OSError as exc:
                get_logger().warning(
                    "A worker process of this session's shared ingest pool "
                    "ended during ingest(), and the pool could not be "
                    "replaced (%s). This ingest() completed and was saved; "
                    "the next ingest() reads on fresh pools of its own and "
                    "tries the replacement again.",
                    describe_exception(exc))

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
        if summary.failed:
            # Declined files are new files this call read, so they are
            # in the total.
            new_files = summary.ingested + summary.failed + summary.declined
            print(f"  - {summary.failed} file(s) REJECTED -- ingested "
                  f"{summary.ingested} of {new_files} new files; see the "
                  f"returned IngestSummary.failures and the ERROR audit "
                  f"rows for the paths and reasons.")
        if summary.declined:
            print(f"  - {summary.declined} file(s) DECLINED -- see the "
                  f"returned IngestSummary.declined and the WARNING audit "
                  f"rows.")

        return summary

    # =========================================================================
    # CONFIGURATION
    # =========================================================================

    def load_config(self, config_file: str):
        """
        Load a configuration file as the session's configuration.

        Loading changes no data. `preview_config()` shows which instances
        its redaction rules match; `audit()`, `anonymize()` and `redact()`
        apply it.

        Args:
            config_file (str): Path to the YAML configuration file.

        Raises:
            FileNotFoundError: If `config_file` does not exist.
            ValueError: If the file fails validation: not `.yaml`/`.yml`,
                YAML syntax, a root that is not a mapping, a `version` this
                library does not read, a key the schema does not have at
                any level, a value of the wrong type, an unknown
                `privacy_profile` (including a `<profile>@<edition>` this
                version does not ship), an unknown `action`, a `phi_tags`,
                `date_jitter` or `machines` of the wrong shape, an invalid
                machine rule, or a `phi_tags` rule the pipeline cannot
                honour. After either error the configuration is exactly
                what it was before the call.
        """
        get_logger().info(f"Loading configuration from {config_file}...")
        print(f"Loading configuration from {config_file}...")

        # No `try`, and nothing assigned until the loader has returned: a
        # file that fails validation must raise and leave the policy the
        # session already had. The loader validates every shape it
        # returns, so the prints below cannot fail after the assignments
        # either and leave half of a failed file in the session.
        (tags, rules, jitter, remove_private,
         base) = ConfigLoader.load_unified_config(config_file)

        self.configuration.phi_tags = tags
        self.configuration.rules = rules
        self.configuration.date_jitter = jitter
        self.configuration.remove_private_tags = remove_private
        self.configuration.config_path = config_file
        # Both, on every load: the floor and `none` both leave
        # `privacy_profile` at None, and a flag set only when true would
        # still call a later `none` load the floor.
        floor = base is profiles.FLOOR
        self.configuration.privacy_profile = None if floor else base
        self.configuration._floor = floor
        # The file now holds what memory holds, so the next change that
        # stays in memory says so again. `auto_save` is not
        # assigned: it is the session's choice, and survives the load.
        self.configuration._file_in_sync = True

        get_logger().info(
            f"Loaded {len(self.configuration.rules)} machine rules and {len(self.configuration.phi_tags)} PHI tags.")
        print(
            f"Configuration Loaded:\n - Privacy Profile: {self.configuration._policy_base}\n - {len(self.configuration.rules)} Machine Redaction Rules\n - {len(self.configuration.phi_tags)} PHI Tags")
        print(
            f" - Date Jitter: {
                self.configuration.date_jitter['min_days']} to {
                self.configuration.date_jitter['max_days']} days")
        print(f" - Remove Private Tags: {self.configuration.remove_private_tags}")
        print("Tip: Run .audit() to check PHI, or .redact() to apply redaction.")

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
        base = profiles.FLOOR_BASE

        data = {
            # The module attribute, read now: one home for the number both
            # writers stamp.
            "version": config_manager.CONFIG_VERSION,
            # The floor's base, which `_scaffold_phi_tags` diffs against:
            # one constant, read once, so the file cannot name one table
            # and carry the overrides of another.
            "privacy_profile": base,
            "phi_tags": self._scaffold_phi_tags(base),
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
            get_logger().error("Failed to write scaffold: %s", describe_exception(exc))

    def _scaffold_machine_rules(self) -> List[Dict[str, Any]]:
        """Build a redaction rule for every machine not already configured.

        Each machine is matched against the knowledge bases in priority order
        and annotated with a burned-in-annotation warning when its images
        declare one. A machine that matches nothing still gets an entry, with
        empty zones for the user to fill in.

        Returns:
            List[Dict[str, Any]]: One rule per unconfigured machine.
        """
        configured_serials = {
            rule.get("serial_number") for rule in self.configuration.rules}

        # Both knowledge bases are read once here, not inside the
        # per-machine loop.
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
        """A warning when a machine's images declare burned-in annotations.

        Args:
            service (RedactionService): The service whose index lists the
                machine's instances.
            serial_number (str): The machine's Device Serial Number.

        Returns:
            str: The warning, or `""` when no image sets (0028,0301) to YES.
        """
        # (0028,0301) is the scanner's own claim that PHI is drawn into the
        # pixels. It is advisory -- absence proves nothing -- but its presence
        # means the zones need checking rather than trusting.
        flagged = sum(
            1 for inst in service.index.get_by_machine(serial_number)
            if isinstance(inst.attributes.get("0028,0301", "NO"), str)
            and "YES" in inst.attributes.get("0028,0301", "NO").upper())

        if not flagged:
            return ""
        return (f"WARNING: {flagged} images have 'Burned In Annotation' "
                f"flag. Verify pixel redaction.")

    def _scaffold_phi_tags(self, base: str) -> Dict[str, Any]:
        """The PHI tag section of a scaffolded config.

        Every entry of the session's policy whose action differs from the
        built-in profile `base`'s: the scaffold names `base` as its
        `privacy_profile`, so a line repeating the profile would change
        nothing. On a bare session the policy is the floor, and the difference
        is exactly `profiles.RESEARCH_DEFAULTS`.

        Args:
            base (str): The pinned profile name the scaffold writes as its
                `privacy_profile`; `create_config` passes
                `profiles.FLOOR_BASE`.

        Returns:
            Dict[str, Any]: Tag to rule, each written structured.
        """
        # Derived rather than listed, so a bare session's scaffold loads back
        # to exactly the floor. It is exact only because the policy it diffs is
        # a superset of the basic profile: a session under `privacy_profile:
        # none` is still scaffolded under `basic@2026c`, and its file reloads
        # with that profile beneath its own tags -- more protection than the
        # session had, never less.
        #
        # A plain-string value is a tag's display name and leaves the
        # inspector's action at REPLACE (`PhiInspector.__init__`), so it is
        # written structured, as the REPLACE it is.
        table = profiles.PRIVACY_PROFILES[base]
        structured = {}
        for tag, val in self.configuration.phi_tags.items():
            rule = dict(val) if isinstance(val, dict) else {
                "name": str(val), "action": "REPLACE"}
            action = table.get(tag, {}).get("action")
            if str(rule.get("action", "REPLACE")).upper() != action:
                structured[tag] = rule
        return structured

    # =========================================================================
    # AUDIT & ANALYSIS
    # =========================================================================

    def audit(self, config_path: str = None) -> "PhiReport":
        """
        Scan every patient in the session for PHI under a tag policy.

        The policy is the file at `config_path` when one is given, and
        otherwise `session.configuration.phi_tags`. The scan runs in
        parallel worker processes.

        Before the scan, two `Patient` objects holding one Patient ID are
        merged into the one that was in the session first, so
        `store.patients` can get shorter, as after `anonymize()`.

        Every status the scan records is recorded with the policy it ran
        under: the configuration's, or, with `config_path`, that file's
        rules under this session's `remove_private_tags`. An entity whose
        status is unchanged but whose policy differs is re-recorded, so the
        next `save()` writes it.

        Args:
            config_path (str, optional): Path to a configuration file
                whose PHI rules to scan with.

        Returns:
            PhiReport: The findings: iterable, indexable, and convertible
                with `to_dataframe()`.

        Raises:
            ValueError: When the file at `config_path` fails any check
                `load_config()` makes, or the policy (that file's, or
                `configuration.phi_tags`) holds a rule the pipeline cannot
                honour. Raised before a project secret is created.
            RuntimeError: When patients sharing a Patient ID were
                de-identified under different date-offset schemes, so they
                cannot be merged; raised after the policy is validated and
                before anything is scanned or a project secret is created.
                Also on a store holding dates shifted under a project
                secret it no longer has.
        """

        # A scan ENDS by advancing `_revision` on every entity it
        # touched (`_record_scan_results` -> `record_phi_status`), and
        # `save()` without `sync=True` returns with `save_all` still
        # running on the persistence manager's thread, for as long as
        # all of the save's pixel I/O. An instance
        # dirtied inside it is dropped from the frozen dirty set, left
        # dirty, and never saved -- `close()` shuts the manager down and
        # does not enqueue a save, so nothing says so. The documented
        # order in README and the quickstart is `save()` then `audit()`,
        # which is exactly this window.
        #
        # Entry is the right place ONLY because nothing inside `audit()`
        # enqueues a save. If that ever changes, this moves to
        # immediately before `_record_scan_results`.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()

        # Default to current config
        tags_to_use = self.configuration.phi_tags

        if config_path:
            # The same loader, and the same exceptions, as `load_config`.
            # No fallback to reading the file as a plain tag list: a file
            # `load_config` refuses must be refused here too.
            tags_to_use, _, _, _, base = ConfigLoader.load_unified_config(config_path)
            # The policy this scan runs with: the file's rules, and
            # the session's `remove_private_tags` -- the flag the inspector
            # below is given, not the file's -- under the file's base,
            # spelled by the helper the configuration spells its own with.
            policy = _scan_policy_for(tags_to_use,
                                      self.configuration.remove_private_tags,
                                      _policy_base_label(base))
        else:
            policy = self.configuration._scan_policy()
            # `configuration.phi_tags` can be assigned directly, which no
            # loader sees. The same refusal the loader raises, and before
            # the project secret below, so a refused policy leaves no new
            # secret in the store.
            validate_phi_policy(tags_to_use, "session.configuration.phi_tags")

        # Two `Patient` objects holding one Patient ID are merged before the
        # scan, as `anonymize()` and a restore merge them. The scan
        # cannot see them as two: `_rehydrate_findings` binds a patient
        # finding by Patient ID, so every finding raised on either would
        # land on the last object, and both carry the same dedup key
        # `(uid, path, attr)`, so `anonymize()` would replace the ID on
        # one and leave the other's original in place. After the policy is
        # validated, so a refused policy leaves the pair as it was; before
        # `_audited_phi_tags` is recorded and before the project secret, so
        # a merge refused across date-offset schemes leaves neither a
        # policy no audit resolved (which the lock reads) nor a new secret
        # in the store. After the entry drain above, which
        # the merge's own `drain` repeats only when there is a pair.
        self.store._merge_patients_sharing_an_id(
            drain=(self.persistence_manager.flush
                   if hasattr(self, 'persistence_manager') else None))
        self._audited_phi_tags = tags_to_use

        # The project secret, once, in the parent, before any work: a
        # store holding dates shifted under a secret it no longer has
        # refuses here, with nothing scanned. After the config is resolved,
        # not before: on a store with no secret this call generates and
        # commits one, and a config that then raised would have left the
        # store changed by a call that did nothing. Read from the
        # store rather than held on the session, so no session can hold a
        # stale one. Workers get it by value in their tuple.
        project_secret = self.store_backend._project_secret_for_use()

        # Uses IsocenterConfiguration derived tags
        inspector = PhiInspector(config_tags=tags_to_use,
                                 remove_private_tags=self.configuration.remove_private_tags,
                                 project_secret=project_secret)
        if not inspector.phi_tags:
            # Reachable only when a config said `privacy_profile: none`
            # and listed no tags: a session with no config applies the
            # floor policy. The scan still runs the hardcoded
            # patient/study checks and the private-tag sweep.
            get_logger().warning(
                "PHI Scan Warning: No PHI tags defined (privacy_profile: none "
                "with no phi_tags). Only patient name, patient ID, study date "
                "and private tags will be checked.")

        get_logger().info("Scanning for PHI (Parallel)...")

        # Hybrid Approach:
        # Pass lightweight object CLONES to avoid "Assert left > 0" IPC error
        # AND to ensure we audit in-memory (unsaved) changes.
        worker_args = []
        for p in self.store.patients:
            # Strip pixels to reduce size
            light_p = self._make_lightweight_copy(p)
            worker_args.append((light_p, tags_to_use,
                                self.configuration.remove_private_tags,
                                project_secret))

        results = run_parallel(scan_worker, worker_args, desc="Scanning PHI")

        all_findings = []
        for findings in results:
            all_findings.extend(findings)

        # Rehydrate Entities!
        self._rehydrate_findings(all_findings)
        self._record_scan_results(all_findings, policy)
        self._scanned_policies[policy.fingerprint] = policy.base

        get_logger().info(f"PHI Scan Complete. Found {len(all_findings)} issues.")

        report = PhiReport(all_findings)
        # The policy the report's findings were raised under, for
        # `anonymize()` handed this report after a reopen, when the
        # entities it remediates carry no policy of their own. Private and
        # set here rather than a constructor argument, so `PhiReport`'s
        # shape is unchanged; a report rebuilt from its findings has none.
        report._scan_policy = policy
        # And the scan's tally, as the audit left it: a pass over
        # this report in a session that has no audit of its own -- the
        # report kept across `close()`, pickled or not -- settles against
        # a copy of it, demoting what it leaves unsettled as the same pass
        # would in this session, and gives the policy only to the entities
        # this scan raised under. A copy, not the session's object,
        # which this session's own passes drain.
        report._scan_tally = self._scan_tally.copy()
        return report

    def _audit_pre_1_0_id_less_groups(self):
        """Warn when the store holds a pre-1.0 group of ID-less files.

        Writes one count-only `WARNING` audit row and log line at every open
        when any patient's ID is `''` or `UnknownPatient`. Names no Patient ID
        and splits nothing.
        """
        # Before 1.0 ingest grouped every file with an empty Patient ID under
        # `''` and every file without one under `UnknownPatient`, so such a
        # patient may be several subjects. Splitting on open would give
        # already-shifted dates a second offset. An `UnknownPatient` group
        # shares one pseudonym and one offset and would otherwise grade PASS
        # silently. A real Patient ID `UnknownPatient` is counted too, which
        # "may" covers. Every open, not once: the row is about the store's
        # contents, and a report over any later session of this store has to
        # carry it.
        count = sum(1 for p in self.store.patients
                    if p.patient_id in ("", "UnknownPatient"))
        if not count:
            return
        detail = (f"{count} patient{' was' if count == 1 else 's were'} "
                  "grouped by a release before 1.0 from files with no Patient "
                  "ID and may be more than one subject; re-ingest their source "
                  "files into a new store to separate them (#584).")
        get_logger().warning(detail)
        self.store_backend.log_audit(action_type="WARNING",
                                     entity_uid=self.persistence_file,
                                     details=detail)

    def phi_status_summary(self) -> Dict[str, Counter]:
        """What the session currently knows about the PHI in each entity.

        Counts `PhiStatus` per level, as each entity stands now rather than
        as the last scan left it: an entity edited since it was scanned
        counts as UNSCANNED.

        Series are not counted: the scan records a status on patients,
        studies and instances only.

        `redact()` is the one edit that keeps an instance's status: an
        instance REMEDIATED or CLEARED before the pass reads the same after
        it, provided nothing but redaction's own writes changed it. See
        `PhiStatus`.

        A status is counted whatever policy it was recorded under;
        `phi_status_policy` names that policy, and an `export()` of
        statuses recorded under a policy other than the one in force says
        so.

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

    def _record_scan_results(self, findings, policy):
        """Record what the scan concluded on every entity it scanned.

        Each patient, study and instance gets IDENTIFIED where a finding names
        it and CLEARED otherwise, at its current revision, so a later edit
        invalidates it. An instance also reads IDENTIFIED while its Series has
        a finding. Replaces the session's scan tally. Call it after
        rehydration: it needs the live objects, not worker copies.

        Args:
            findings (List[PhiFinding]): The rehydrated findings.
            policy (ScanPolicy): The policy the scan ran under.
        """
        # A Series has no status of its own (no store column, and nothing the
        # grade reads), so its instances bear its finding, and a Series finding
        # never acted on keeps the grade from PASS.
        from .remediation import _ScanTally

        # `is not None`, not truthiness: `''` is a Patient ID a store
        # written before 1.0 can hold, and a hand-built graph too (ingest
        # keys an ID-less subject on its study), and a
        # falsy filter would stamp such a patient CLEARED with its name
        # finding outstanding and let its instances through the safe
        # export. The same test in `_scan_before_export` and `_ScanTally`.
        identified = {f.entity_uid for f in findings if f.entity_uid is not None}
        # Keyed on the same scan-time uids as `identified`, and replaced
        # by every audit, so a new report settles against its own scan.
        self._scan_tally = _ScanTally(findings)

        series_identified = {f.entity_uid for f in findings
                             if f.entity_type == "Series" and f.entity_uid is not None}

        def record(entity, uid, carried=False):
            entity.record_phi_status(
                PhiStatus.IDENTIFIED if carried or uid in identified
                else PhiStatus.CLEARED, policy=policy)

        for patient in self.store.patients:
            record(patient, patient.patient_id)
            for study in patient.studies:
                record(study, study.study_instance_uid)
                for series in study.series:
                    carried = series.series_instance_uid in series_identified
                    for instance in series.instances:
                        record(instance, instance.sop_instance_uid, carried)

    def scan_pixel_content(self, serial_number: str = None) -> "PhiReport":
        """
        Scan instances for burned-in text with OCR, and report the text no
        configured redaction zone covers.

        Only instances of machines (by Device Serial Number) the current
        configuration has a rule for are scanned; other machines are
        skipped.

        Args:
            serial_number (str, optional): Scan only the machine with this
                serial number.

        Returns:
            PhiReport: Findings for burned-in text no zone covers. Each
                finding's `entity` is the live `Instance` in
                `session.store`, whether the scan ran in threads or in
                processes, or `None` when that instance cannot be found in
                the graph; never a worker's copy. Its `failures` lists
                `(entity_uid, reason)` for each instance whose pixels could
                not be loaded or whose OCR raised on any frame, and a
                `WARNING` log line gives the count. Each failure is also
                one `WARNING` audit row naming the instance and the reason,
                so the report grades `REVIEW_REQUIRED`. An instance with no
                pixel element is neither scanned nor a failure. A worker
                process runs the `pytesseract.pytesseract.tesseract_cmd`
                the caller set.

        Raises:
            RuntimeError: `pixel_analysis.OcrUnavailableError` when the `ocr`
                extra is not installed or the `tesseract` binary does not
                answer in the calling process, before any worker is
                dispatched and before the graph is read.
                `pixel_analysis.PixelScanError`, carrying `.failures` and
                `.attempted`, after the pass, the audit rows and the
                warning, when at least one instance failed and none could
                be read.
        """
        # First, before the graph is read: a scaffolded config would
        # otherwise answer "nothing to scan" without OCR, and the missing
        # extra would surface only once zones were filled in.
        pixel_analysis._require_ocr("scan_pixel_content()")  # pylint: disable=protected-access
        # Right after the probe, so the workers run the binary it checked
        # (see `_caller_tesseract_cmd`).
        tesseract_cmd = _caller_tesseract_cmd()
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
                        worker_items.append((inst, equip, current_rules, tesseract_cmd))

        if not worker_items:
            msg = "No matching configured instances found to scan."
            if skipped_count > 0:
                msg += f" (Skipped {skipped_count} unconfigured instances)"
            print(msg)
            # Recorded: a call that found nothing configured to read still
            # ran, and "no scan ran" would be the wrong thing for section 5
            # to say about it.
            self._pixel_scans.append(PixelScanSummary(
                serial_number=serial_number, attempted=0, read=0, unread=0,
                findings=0, skipped=skipped_count))
            return PhiReport([])

        outcomes = run_parallel(_verify_worker, worker_items, desc="OCR Verification")

        all_findings = []
        failures = []
        read = 0
        for outcome in outcomes:
            all_findings.extend(outcome.findings)
            if outcome.failure is not None:
                failures.append((outcome.entity_uid, outcome.failure))
            if outcome.read:
                read += 1

        # Here, before anything below can raise: a pass that read nothing
        # ends in `PixelScanError`, and recorded after that raise it would
        # read in section 5 as "no scan ran" beside section 4's rows for
        # the very instances it could not read.
        self._pixel_scans.append(PixelScanSummary(
            serial_number=serial_number, attempted=len(worker_items),
            read=read, unread=len(failures), findings=len(all_findings),
            skipped=skipped_count))

        # Unconditionally, in both strategies: the worker strips the
        # entity, and this is the one path that puts it back, so
        # `PhiFinding.entity` has one meaning however `run_parallel()`
        # resolved. Deliberately not `_record_scan_results`: OCR
        # findings say nothing about an entity's metadata PHI status.
        self._rehydrate_findings(all_findings)

        # Before the warning and the raise, so every exit from here --
        # the report returned or `PixelScanError` -- leaves the rows.
        _audit_unread_instances(self.store_backend, "scan_pixel_content()",
                                failures)
        _warn_unread_instances("scan_pixel_content()", failures,
                               len(worker_items), "report.failures")
        summary = f"OCR Scan Complete. Found {len(all_findings)} suspicious regions (Uncovered)"
        if failures:
            summary += (f"; {len(failures)} instance(s) could not be read -- "
                        "see report.failures")
        print(summary + ".")
        # Last, after the warning, as `export()` raises `ExportError`: a
        # caller who catches this has heard everything the pass produced.
        # `read == 0` and not "no findings": a scan that read instances
        # and found nothing on them is a clean result, and an all-SR
        # series has neither reads nor failures.
        if failures and read == 0:
            raise pixel_analysis.PixelScanError(failures, len(worker_items))
        return PhiReport(all_findings, failures)

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
            # `.save()` writes to `configuration.config_path`, which only
            # `load_config()` sets, so a session configured by
            # `create_config()` reaches here with nothing to save to. The
            # tip names the attribute rather than hoping. `save()` raises
            # ValueError without one, and the suggestions above changed
            # memory only, as every change does without auto-save.
            print("Tip: Run .scan_pixel_content() again to verify the fix, "
                  "then .configuration.save() to write it to the loaded file "
                  "(save() raises ValueError when no file was loaded; set "
                  ".configuration.config_path first).")

        return count

    def discover_redaction_zones(self, serial_number: str, sample_size: int = 50, min_confidence: float = 80.0):
        """
        OCR a random sample of one machine's instances and collect where
        burned-in text appears.

        Args:
            serial_number (str): The Device Serial Number of the machine.
            sample_size (int): The most instances to read; a machine with
                more is sampled at random.
            min_confidence (float): The lowest OCR confidence, 0 to 100, a
                text candidate needs to be kept.

        Returns:
            DiscoveryResult: Every text candidate found. Call `to_zones()`
                on it for grouped redaction zones. `n_sources` counts only
                the sampled instances that were read (at least one frame
                through OCR), so an instance that could not be read does not
                dilute a zone's occurrence rate. Each one that failed is
                logged at ERROR, counted in a WARNING, and written as one
                `WARNING` audit row naming the instance and the reason,
                which grades the run `REVIEW_REQUIRED`. A worker process
                runs the caller's `tesseract_cmd`.

        Raises:
            RuntimeError: `pixel_analysis.OcrUnavailableError` when the `ocr`
                extra is not installed or the `tesseract` binary does not
                answer, before any worker is dispatched and before the graph
                is read. `pixel_analysis.PixelScanError`, carrying
                `.failures` and `.attempted`, after the pass, the audit rows
                and the warning, when at least one sampled instance failed
                and none could be read.
        """
        # First, and read through the module at call time -- never a copy
        # of `HAS_OCR` imported into this module, which a patch or a later
        # install would not reach.
        pixel_analysis._require_ocr("discover_redaction_zones()")  # pylint: disable=protected-access
        # Not only for show: `force_threads=True` below does not hold under
        # `ISOCENTER_MAX_TASKS_PER_CHILD` (see `_discover_worker`).
        tesseract_cmd = _caller_tesseract_cmd()
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
        outcomes = run_parallel(
            _discover_worker,
            [(inst, tesseract_cmd) for inst in sample],
            desc="Discovery Scan",
            force_threads=True
        )

        candidates = []
        failures = []
        n_read = 0

        for uid, ocr in outcomes:
            if ocr.failure is not None:
                # Kept at ERROR, as `analyze_pixels` logged it: with no
                # failure field on `DiscoveryResult`, the log is where each
                # one is named, and the warning below counts them.
                get_logger().error(f"Failed to analyze pixels for {uid}: {ocr.failure}")
                failures.append((uid, ocr.failure))
            # Only a read instance is a source. One that failed outright,
            # or carries no pixel element, saw no text because nobody
            # looked, and counting it in `n_sources` would lower every zone's
            # occurrence rate.
            if not ocr.read:
                continue
            i = n_read
            n_read += 1
            for r in ocr.regions:
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

        # Before the warning and the raise, as in `scan_pixel_content()`.
        # Discovery reads nothing into the configuration, but a
        # session whose discovery could not read an instance has the same
        # gap in what it looked at.
        _audit_unread_instances(self.store_backend,
                                "discover_redaction_zones()", failures)
        _warn_unread_instances("discover_redaction_zones()", failures,
                               len(sample), "the ERROR log above")
        # After the warning, and on `ExportError`'s rule, as in
        # `scan_pixel_content()`: nothing read at all is not a result.
        if failures and n_read == 0:
            raise pixel_analysis.PixelScanError(failures, len(sample))
        result = DiscoveryResult(candidates, n_read)
        print(f"Discovery complete. Found {len(candidates)} raw candidates.")
        return result

    def get_cohort_report(self,
                          expand_metadata: bool = False,
                          patient_ids: Optional[List[str]] = None) -> 'pd.DataFrame':
        """
        Return a pandas DataFrame of the cohort, one row per instance.

        Args:
            expand_metadata (bool): If True, add a column for every DICOM
                attribute.
            patient_ids (Iterable[str], optional): Restrict the report to
                these Patient IDs, read exactly as `export()` reads its
                `patient_ids`. `None` means every patient in the session. An
                empty iterable matches nobody: it is a filter that selected
                nothing, not an absent filter. An iterator is read once. An
                ID no patient holds selects nothing and is counted, never
                named, in one `WARNING` log line; a report is a read and
                writes no audit row. A subject whose files carried no
                Patient ID is selected by the key this report's `PatientID`
                column shows for it, not by `""`.

        Returns:
            pd.DataFrame: One row per instance.

        Raises:
            TypeError: If `patient_ids` is a bare `str` (wrap one ID in a
                list), bytes-like, not iterable, or holds an element that
                is not a `str` (named by its position and type, never its
                value).
        """
        import pandas as pd
        # Before anything is read, so a refusal is the whole answer.
        selection = select_patient_ids(patient_ids, self.store.patients)
        if selection.unmatched:
            get_logger().warning(unmatched_patient_ids_sentence(selection))
        rows = []
        for p in self.store.patients:
            # `is not None` rather than a truth test: an empty selection
            # must exclude everyone. A caller computing a cohort that came
            # back empty would otherwise export the whole dataset.
            if selection.ids is not None and p.patient_id not in selection.ids:
                continue
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

        # Name the columns explicitly so an empty cohort still has a
        # schema. `pd.DataFrame([])` has no columns at all, so the
        # obvious downstream `df[df.Modality == "CT"]` breaks only when
        # the filter happened to match nothing -- the case least likely
        # to be exercised before it reaches production.
        #
        # Only when there are no rows: with rows, pandas takes the union
        # of the dicts' keys, and passing `columns` here would clip the
        # `expand_metadata` attributes back out.
        if not rows:
            return pd.DataFrame(rows, columns=COHORT_REPORT_COLUMNS)

        return pd.DataFrame(rows)

    def _resolve_scan_gaps(self, rows: list) -> list:
        """Say, per `SCAN_GAP` row, whether the element is still held.

        A presence test on the graph, at every depth: an element still held is
        one the next `export()` writes.

        Args:
            rows (list): `(timestamp, entity_uid, details, element_tag)` from
                `SqliteStore.get_audit_scan_gaps`.

        Returns:
            list: The same rows with `element_tag` replaced by a disposition:
                `GAP_REMOVED`, `GAP_RETAINED` or `GAP_UNRESOLVED`.
        """
        # The row is written at ingest, where nothing knows what the export
        # will carry: `remove_private_tags` is applied later, by the sweep in
        # `PhiInspector`, which deletes the element from the graph. The graph
        # is a sound oracle because `remove_private_tags` has exactly one
        # consumer and the exporter applies no private filtering of its own.
        # This never re-runs `_sequence_from_un_bytes`: which elements the gate
        # refused was settled at ingest and is read back off the row.
        # Only the instances a gap row names are walked. The rows are
        # few and the graph is not; walking every instance to answer a
        # question about three of them is how a report starts costing
        # what an export costs.
        wanted = {row[1] for row in rows}
        held = {}
        if wanted:
            for patient in self.store.patients:
                for study in patient.studies:
                    for series in study.series:
                        for inst in series.instances:
                            if inst.sop_instance_uid not in wanted:
                                continue
                            # Every depth: the gate runs on the way down
                            # through sequences too, so a gap can name an
                            # element that only exists inside an item.
                            held[inst.sop_instance_uid] = {
                                tag
                                for item, _path in iter_item_tree(inst)
                                for tag in item.attributes}

        resolved = []
        for timestamp, uid, details, tag in rows:
            if not tag or uid not in held:
                disposition = GAP_UNRESOLVED
            elif tag in held[uid]:
                disposition = GAP_RETAINED
            else:
                disposition = GAP_REMOVED
            resolved.append((timestamp, uid, details, disposition))
        return resolved

    @staticmethod
    def _review_reasons(*, audit_summary, exceptions, graded_losses,
                        open_gaps, declined_remediations,
                        unattested, unacted, edited) -> List[str]:
        """Why a run is not PASS, one entry per grade condition that holds.

        `generate_report` grades PASS exactly when the list is empty, and
        section 5 of the report renders it.

        Args:
            audit_summary (dict): Audit action to row count; empty when the
                trail holds no rows.
            exceptions (list): The report's section 4 rows.
            graded_losses (list): Data-loss rows in a graded scope.
            open_gaps (list): Unscanned elements not removed before export.
            declined_remediations (list): Declined remediation rows.
            unattested (list): Verbs that ran with no audit row of their own.
            unacted (Dict[str, int]): Per level (`patients`, `studies`,
                `instances`), the count of entities that read IDENTIFIED.
            edited (Dict[str, int]): Per level (`patients`, `studies`,
                `instances`), the count of entities edited after their
                status was recorded.

        Returns:
            List[str]: One sentence per condition that keeps the run from PASS.
        """
        # The grade is this list, so a new grade term cannot move the grade
        # without also appearing in section 5: there is no second expression
        # for it to live in. The conditions are numbered in
        # `docs/analytics.md`, "How the grade is decided", in the order they
        # are appended here, and they are a 1.x promise: none is removed or
        # narrowed, and one may be added with a CHANGELOG entry.
        review_reasons = []
        if not audit_summary:
            review_reasons.append(
                "the audit trail holds no rows, so nothing this run did is "
                "attested (section 2)")
        if exceptions:
            review_reasons.append(
                f"{len(exceptions)} row(s) in section 4 (Exceptions & Errors)")
        if graded_losses:
            review_reasons.append(
                f"{len(graded_losses)} graded data loss(es) in section 3.1 "
                f"(scope {' or '.join(sorted(GRADED_LOSS_SCOPES))})")
        if open_gaps:
            review_reasons.append(
                f"{len(open_gaps)} unscanned element(s) in section 3.2 not "
                "removed before export")
        if declined_remediations:
            review_reasons.append(
                f"{len(declined_remediations)} declined remediation(s) in "
                "section 3.3")
        for verb in unattested:
            review_reasons.append(
                f"{verb} ran in this session and the audit trail holds none "
                "of the rows it writes")
        # Condition 7. "The policy it ran with", not "the policy in
        # force": a status is recorded by a scan, and a later session may
        # load another configuration without rescanning.
        n_unacted = sum(unacted.values())
        if n_unacted:
            noun = "entity" if n_unacted == 1 else "entities"
            review_reasons.append(
                f"{n_unacted} {noun} read IDENTIFIED: the last PHI scan "
                "raised a finding under the policy it ran with, and no "
                "`anonymize()` pass since acted on it "
                f"(patients {unacted['patients']}, "
                f"studies {unacted['studies']}, "
                f"instances {unacted['instances']})")
        # Condition 8. A status an edit made stale, not the absence
        # of one: an entity never scanned is UNSCANNED and does not grade.
        # Every edit made after a scan costs the PASS until a scan
        # reads it (instances, nested items and series fields included).
        # Its own line, not folded into condition 7's:
        # that one names a finding, this one the absence of a measurement
        # a scan once made.
        n_edited = sum(edited.values())
        if n_edited:
            noun = "entity" if n_edited == 1 else "entities"
            review_reasons.append(
                f"{n_edited} {noun} edited after the last PHI scan: its "
                "content was changed after its PHI status was recorded, "
                "and no scan has read the change; `audit()` reads it "
                f"(patients {edited['patients']}, "
                f"studies {edited['studies']}, "
                f"instances {edited['instances']})")
        return review_reasons

    def generate_report(self, output_path: str, format: str = "markdown") -> None:
        """
        Write the compliance report for the session's store.

        The report holds the grade (`PASS` or `REVIEW_REQUIRED`), decided
        from the audit trail the store holds and the graph's PHI statuses
        (docs/analytics.md, "How the grade is decided"), with the session's counts,
        the audit actions, data loss, exceptions, and the policy in force.
        Generate it after `export()`: an export writes rows of its own, and
        a report generated before any export carries a note saying so.

        Args:
            output_path (str): The file path where the report should be saved.
            format (str): `'markdown'`, the one spelling accepted. Defaults
                to `'markdown'`.

        Raises:
            ValueError: For any other `format`, `'md'` and case variants
                included; no file is written.
        """
        # The spelling first: a refused format is
        # known before a report over the whole store is built.
        renderer = get_renderer(format)
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
        data_losses = self.store_backend.get_audit_losses()
        scan_gaps = self._resolve_scan_gaps(
            self.store_backend.get_audit_scan_gaps())
        declined_remediations = self.store_backend.get_audit_declines()

        # Check for unsafe attributes (BurnedInAnnotation)
        unsafe_items = self.store_backend.check_unsafe_attributes()
        if unsafe_items:
            for uid, _path, msg in unsafe_items:
                exceptions.append(
                    (datetime.datetime.now().isoformat(),
                     "COMPLIANCE_CHECK",
                     f"{msg} - {uid}"))

        # Descriptor damage an earlier release persisted.
        # Same channel as the BurnedInAnnotation hit above because it is
        # the same claim one attribute over: the store holds an instance
        # whose export cannot be trusted, and a report over such a store
        # must not say PASS. Re-derived from the store on every report
        # rather than remembered from open, so a report generated by any
        # session over this store carries it.
        for uid, _path, msg in self.store_backend.check_pixel_geometry():
            exceptions.append(
                (datetime.datetime.now().isoformat(),
                 "COMPLIANCE_CHECK",
                 f"{msg} - {uid}"))

        # Audit rows a failed batch write dropped. Filed as an
        # exception -- not merely rendered -- because everything above
        # was read from a table those rows never reached: any count,
        # loss, or error here may under-state what happened, and a
        # report that cannot vouch for its own inputs must not PASS.
        # The rows are dropped rather than retried; the reasoning lives
        # on `SqliteStore.log_audit_batch`.
        dropped_audit_rows = self.store_backend.get_audit_drops()
        if dropped_audit_rows:
            exceptions.append(
                (datetime.datetime.now().isoformat(),
                 "AUDIT_DROP",
                 f"{dropped_audit_rows} audit row(s) failed to write and "
                 "were dropped; this report under-counts the actions "
                 "actually taken"))

        # Export-side DATA_LOSS rows are written during `export()`, so
        # a report generated first cannot contain them, and can grade
        # PASS on a run that then drops a private element; this note
        # says so. The audit log is the arbiter, not
        # a session flag: an EXPORT row survives a session
        # reopened on this store. A log line only, never an audit row
        # -- a WARNING row would flip the grade this note deliberately
        # leaves alone.
        export_recorded = "EXPORT" in audit_summary
        if not export_recorded:
            get_logger().warning(
                "Report generated before any export was recorded: "
                "export-time data losses cannot appear in it. If this "
                "session exports, regenerate the report afterwards.")

        # 3. Determine Context
        #
        # Describe what was configured, never what standard it might
        # satisfy: no report asserts a standard such as HIPAA Safe Harbor.
        # The tag count is what `audit()` scans with: the session's
        # policy, with no fallback, so the count and the scan cannot
        # disagree. A bare session carries the floor; an empty policy
        # means `privacy_profile: none` with no tags, and "0 tag rules"
        # is then the truth.
        effective_tags = self.configuration.phi_tags

        # Which table the rules were built from, never a claim that the
        # output conforms to it. The floor and `privacy_profile:
        # none` both leave `privacy_profile` at None; `_floor` is what
        # tells them apart. The edition is read from the pinned name, not
        # from a second table that could disagree with it.
        #
        # A name assigned into the public field in code (`"basic"`) goes
        # through the aliases as a file would; it is a built-in, not a
        # custom profile.
        profile_name = self.configuration.privacy_profile
        if isinstance(profile_name, str):
            profile_name = profiles.PROFILE_ALIASES.get(profile_name, profile_name)
        if not profile_name and self.configuration._floor:
            floor_base = profiles.FLOOR_BASE
            source = _profile_source(floor_base)
            privacy_profile = (
                f"None (session defaults: the floor policy over {floor_base})")
            method = f"Session defaults, the floor policy over '{floor_base}' {source}"
        elif not profile_name:
            # `privacy_profile: none`, or an external profile that
            # contributed no rules (the loader drops its path). Worded to be
            # true of both: this row does not quote a line the file may not
            # have.
            privacy_profile = "None (no base profile)"
            method = "No profile"
        elif profile_name in profiles.PRIVACY_PROFILES:
            privacy_profile = profile_name
            method = f"Profile '{profile_name}' {_profile_source(profile_name)}"
        else:
            # Resolved, but from a file rather than a built-in name.
            privacy_profile = profile_name
            method = f"Custom profile '{profile_name}'"

        deid_method = (
            f"{method}: {len(effective_tags)} tag rules, "
            f"{len(self.configuration.rules)} pixel redaction rules")
        # The rows above describe the configuration in force, and an
        # `audit(config_path=)` scans under another policy without
        # changing it, so a status this session recorded can name a
        # policy these rows do not. Said here, not
        # counted: statuses by recorded policy are a report row of their
        # own. Fingerprints, not bases, decide "another".
        in_force = self.configuration._scan_policy().fingerprint
        also = [f"{base} ({fingerprint[:15]})"
                for fingerprint, base in sorted(self._scanned_policies.items())
                if fingerprint != in_force]
        if also:
            deid_method += (
                "; this session also scanned under " + "; ".join(also)
                + ", and a status recorded by that scan names it "
                "(`phi_status_policy`), not the configuration above")
            deid_method = deid_method.replace("|", "\\|")

        try:
            from importlib.metadata import version, PackageNotFoundError
            ver = version("isocenter")
        except PackageNotFoundError:
            # Running from a source tree that was never installed.
            ver = "0.0.0"

        # 4. Grade the run
        #
        # A dropped *private* element fails the grade; a dropped
        # standard one does not. The asymmetry is deliberate rather than
        # a rule half-applied; the argument is in CHANGELOG.md.
        # The one loss parity sits badly on -- the discarded waveform
        # multiplex group, standard-group and not remotely routine -- is
        # scoped SIGNAL by its emitter and grades here too.
        #
        # Membership in GRADED_LOSS_SCOPES, never a wider test: the
        # scope is set by the emitter, and grading STANDARD here would
        # take every overlay with it.
        #
        # `row[3]` is `loss_scope`. NULL for rows written before the
        # column existed, which read as ungraded rather than as
        # standard, because nothing here can know which they were.
        graded_losses = [row for row in data_losses
                         if row[3] in GRADED_LOSS_SCOPES]

        # A scan gap is graded on its disposition, not on its existence.
        # The row says the scan could not read an element; that only
        # costs the run its PASS if the element is still there to be
        # written. A gap the sweep removed is content the export does
        # not carry, which is the same test applied to DATA_LOSS --
        # and grading it REVIEW_REQUIRED would make the report disagree
        # with itself, because a *parseable* private sequence swept by
        # the same default configuration grades PASS.
        #
        # `GAP_UNRESOLVED` grades like a retained one. A disposition
        # nothing could establish is not a clean one.
        open_gaps = [row for row in scan_gaps if row[3] != GAP_REMOVED]

        # A declined remediation grades exactly like an open gap, and
        # for the same reason: the value it targeted is **still in the
        # graph** and reaches the exported file, so a report that graded
        # PASS over one would be asserting the removal happened. Graded
        # on the row existing rather than on any property of it -- there
        # is no disposition to resolve here, because unlike a `SCAN_GAP`
        # nothing downstream removes a value a remediation declined to
        # touch.
        #
        # An empty date writes no row at all, so this cannot fire over a
        # graph with nothing wrong in it; that gate is in
        # `_apply_single_remediation`, where the value is in hand.

        # Action-specific evidence. The `audit_summary` arm below
        # asks whether the audit log heard about *anything*; this asks
        # whether it heard about what this session did. Without it, a
        # session that redacted and whose REDACTION rows were lost (a
        # dropped batch write) would grade PASS on the strength of its
        # other rows. `_actions_performed` is the session's
        # transient memory of its own verbs; see `__init__` for why it
        # is deliberately not persisted.
        from .remediation import REMEDIATION_ACTION_TYPES
        expected_evidence = {
            "REDACTION": frozenset({"REDACTION"}),
            "ANONYMIZE": REMEDIATION_ACTION_TYPES,
        }
        unattested = [verb for verb in sorted(self._actions_performed)
                      if not expected_evidence[verb] & audit_summary.keys()]

        # Findings raised and never acted on (condition 7). `audit()`
        # writes no audit row for a finding, so every term above is blind
        # to one nobody passed to `anonymize()`: without this, an audit
        # followed by an export, or a pass handed part of a report, would
        # grade PASS with the identifiers the user's own policy names
        # still in the file.
        #
        # Read from `phi_status`, never from the session's scan: the status
        # is persisted per entity, so a session reopened on this store
        # grades as the one that scanned did, like every store-wide term.
        # IDENTIFIED only. UNSCANNED -- never scanned, or edited since --
        # is the absence of a measurement, not a finding: it is
        # counted for section 5 and does not grade. Series are left out
        # for the reason `phi_status_summary` gives: nothing scans one.
        unacted = {"patients": 0, "studies": 0, "instances": 0}
        unscanned_instances = 0
        # Condition 8: an entity edited
        # after its status was recorded. Counted beside condition 7 in
        # the same walk, per level, at the levels condition 7 counts.
        # **Not series**: the scan records
        # nothing on a Series, so a Series status an edit made stale could
        # never be cleared by `audit()`, the one thing the line says does.
        # A Series field edit is counted where it is read -- the cascade
        # marks every instance of the series changed -- and an edit of a
        # nested item moves its instance the same way.
        edited = {"patients": 0, "studies": 0, "instances": 0}
        for patient in self.store.patients:
            unacted["patients"] += patient.phi_status is PhiStatus.IDENTIFIED
            edited["patients"] += _edited_since_its_status(patient)
            for study in patient.studies:
                unacted["studies"] += study.phi_status is PhiStatus.IDENTIFIED
                edited["studies"] += _edited_since_its_status(study)
                for series in study.series:
                    for instance in series.instances:
                        status = instance.phi_status
                        unacted["instances"] += status is PhiStatus.IDENTIFIED
                        unscanned_instances += status is PhiStatus.UNSCANNED
                        edited["instances"] += _edited_since_its_status(instance)

        # The grade IS this list: PASS exactly when it is empty. See
        # `_review_reasons` for why it is a list and not a boolean.
        # Keyword-only: seven lists in a row is an easy pair to transpose,
        # and a transposed pair would still grade -- it would name the
        # wrong section.
        review_reasons = self._review_reasons(
            audit_summary=audit_summary, exceptions=exceptions,
            graded_losses=graded_losses, open_gaps=open_gaps,
            declined_remediations=declined_remediations,
            unattested=unattested, unacted=unacted, edited=edited)

        # 5. Build Report DTO
        report = ComplianceReport(
            isocenter_version=ver,
            project_name=os.path.basename(self.persistence_file),
            privacy_profile=privacy_profile,
            deid_method=deid_method,
            total_patients=n_p,
            total_studies=n_st,
            total_series=n_se,
            total_instances=n_i,
            instances_written=self._last_export_written,
            instances_requested=self._last_export_requested,
            audit_summary=audit_summary,
            exceptions=exceptions,
            data_losses=data_losses,
            scan_gaps=scan_gaps,
            declined_remediations=declined_remediations,
            export_recorded=export_recorded,
            validation_status="REVIEW_REQUIRED" if review_reasons else "PASS",
            review_reasons=review_reasons,
            metadata_remediations=sum(
                audit_summary.get(action, 0)
                for action in REMEDIATION_ACTION_TYPES),
            pixel_scans=list(self._pixel_scans),
            unacted_findings=unacted,
            unscanned_instances=unscanned_instances,
        )

        renderer.render(report, output_path)

    @staticmethod
    def _manifest_anonymized(patient, study, instance) -> bool:
        """The manifest's `anonymized` for one instance.

        Args:
            patient (Patient): The instance's patient.
            study (Study): The instance's study.
            instance (Instance): The instance.

        Returns:
            bool: True when the patient, the study and the instance each read
                REMEDIATED or CLEARED; `phi_status` reads UNSCANNED for an
                entity edited since its scan.
        """
        # The series is not consulted: the inspector never scans one, so it is
        # UNSCANNED in every session and would make every item False.
        #
        # REMEDIATED is not required: a re-audit of an anonymized graph records
        # CLEARED over it, and requiring it would call a re-checked graph
        # un-anonymized. So an input the scan found clean reads True after
        # `audit()` alone, as `generate_manifest` documents.
        #
        # The study matters: a declined study-date remediation leaves the study
        # IDENTIFIED while its instances read CLEARED, and consulting the
        # instance alone would say True over a date that reaches the export
        # unshifted.
        return all(entity.phi_status in (PhiStatus.REMEDIATED, PhiStatus.CLEARED)
                   for entity in (patient, study, instance))

    def generate_manifest(self, output_path: str, format: str = "html") -> None:
        """
        Write an HTML or JSON manifest of every instance in the session.

        One entry per SOP Instance in the session, with its source file
        path and key metadata (Modality, Manufacturer, and so on).

        Each JSON item's `anonymized` is True when the last tag-policy PHI
        scan left no identifier unremediated on that instance's patient,
        study or instance, and none of the three has been edited since. It
        does not mean "`anonymize()` ran" (a clean input reads True after
        `audit()` alone), and it says nothing about burned-in pixel text.

        After `anonymize()` the UIDs listed are the replacements, beside
        each instance's source file path, so the manifest maps source files
        to exported UIDs: keep it with the store, not with an export.

        Args:
            output_path (str): The file path where the manifest should be saved.
            format (str): `'html'` or `'json'`, exactly. Defaults to
                `'html'`.

        Raises:
            ValueError: For any other `format`, a case variant included; no
                file is written.
        """
        # The spelling first, before every instance
        # is walked.
        renderer = get_manifest_renderer(format)
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
                            model_name=model,
                            anonymized=self._manifest_anonymized(p, st, inst),
                        )
                        items.append(item)

        manifest = Manifest(
            generated_at=datetime.datetime.now().isoformat(),
            items=items,
            project_name=os.path.basename(self.persistence_file)
        )

        renderer.render(manifest, output_path)

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
                        *,
                        verbose: bool = True,
                        tags_to_lock: Optional[List[str]] = None
                        ) -> Union[List["Instance"], LockingResult]:
        """
        Encrypt each instance's original identifiers into an identity
        token carried in the instance, for reversible anonymization.

        The token is written into the Encrypted Attributes Sequence
        `(0400,0500)` under the key `enable_reversible_anonymization()`
        names. Values are captured from each instance, and one token is
        written per distinct set of them: under the default tags a patient
        with several studies carries about one token per study, and each
        instance's token holds that instance's own values.

        Call it **before** `anonymize()` if recovery is required:
        afterwards the identifying tags no longer hold their original
        values, and the lock refuses to capture what a remediation wrote.

        Anything but a `str` (an iterable of Patient IDs, a `PhiReport` or
        a list of findings) is handed to `lock_identities_batch()`, with
        the same `persist`, `verbose` and `tags_to_lock`.
        `auto_persist_chunk_size` is that method's own argument.

        **Refusals.** The lock raises `RuntimeError` and writes no token
        when the patient cannot be locked as asked:

        - a value the lock would capture was written by a remediation
          (`ANONYMIZED`, `ANON_...`, a rule's `value:`, a shifted date);
        - a tag it names was emptied or removed by `anonymize()`; the
          message names the `tags_to_lock` that works without it;
        - a re-lock would lose a value the existing token holds;
        - the patient carries an identity token this library wrote that the
          key at the path given to `enable_reversible_anonymization()` does
          not decrypt, or that opens to no identity record, or that is in
          the layout releases before 1.0 wrote, which 1.x does not read;
        - a re-lock over a token this store did not write (one that arrived
          inside a file, or one a release before 0.9.8 wrote) would change
          a value it holds, naming the tag, which need not be one
          `tags_to_lock` names; `recover_patient_identity(...,
          restore=True)` followed by a lock is the way through;
        - a value it would capture is one no token can hold (`bytes`),
          naming the tag;
        - Patient's Name is blank under a rule of EMPTY or REMOVE on it;
        - the patient has instances and any of them holds no value in any
          tag `tags_to_lock` names (or it names none), counted. A tag held
          blank is a value, and a patient with no instances locks as 0
          instances.

        Each of these is judged on every instance's own values: a value a
        pass wrote on any study refuses the lock. An existing token is
        judged against the first instance that carries it. Before any
        patient is planned, the lock also refuses when no key file exists
        at the path and an instance in the session carries a token this
        library wrote; no key is created. Given a list or a report, every
        patient is checked first and, if any is refused, one error lists
        each and no patient is locked.

        No message carries a Patient ID: a message says "this patient", its
        advice spells the ID `<its Patient ID>`, and a replaced Patient ID
        is described, not quoted. When the lock creates the key file (the
        first lock under a path with none), the file is created already
        written, with mode 0600.

        Args:
            patient_id (str): The ID of the patient to lock; anything that
                is not a `str` (a list, set, frozenset or iterator of IDs,
                or a report) is handed to `lock_identities_batch()`.
            persist (bool): If True, writes each instance's token into the
                row the store holds for it, immediately; an instance the
                store holds no row for raises (see `RuntimeError`). If
                False, returns the modified instances for a later `save()`.
            verbose (bool): If True, logs debug information.
            tags_to_lock (List[str], optional): The tags whose original values
                are embedded. When omitted: PatientName, PatientID,
                PatientBirthDate, PatientSex and AccessionNumber.

        Returns:
            Union[List[Instance], LockingResult]: A list of modified instances.

        Raises:
            RuntimeError: When reversible anonymization is not enabled, or
                for a refusal above, before any token is written. Also, with
                `persist=True`, when some instance's current SOP Instance UID
                has no row in the store to write its token into: a patient
                built by hand and never saved, or a UID `regenerate_uid()`
                moved (as `redact()` does) since the last save. That one is
                raised **after** the tokens are embedded: they are in
                memory, marked modified, so a later `save(sync=True)` stores
                them; this write stored none of them, and one `ERROR` audit
                row gives the counts. `ingest()` writes the rows itself, and
                the lock drains a save queued by `save()` before it embeds
                anything, so neither ingest-then-lock nor `save()`-then-lock
                raises it.
            TypeError: When `patient_id` is not a `str` and is not a
                selection `lock_identities_batch()` accepts: `None`,
                bytes-like, not iterable, or an iterable holding an item
                that is neither a `str` nor a finding. Raised before the key
                is loaded or created.
            ValueError: The key file at the path is empty (the message
                names the path) or is not a Fernet key. Neither is cached:
                a later call reads the file again.
            sqlite3.Error: With `persist=True`, the store refused the
                write. The instances already carry the new token in memory,
                marked modified, so a later `save()` writes them; this write
                stored none of them, and one `ERROR` audit row says so. The
                row speaks for the write, not the store, which keeps
                whatever an earlier write put there. Given a list or a
                report, the batch form's `sqlite3.Error` applies.
        """
        if not self.reversibility_service:
            raise RuntimeError(
                "Reversible anonymization not enabled. Call enable_reversible_anonymization() first.")

        # A `str` is the single-id spelling; everything else goes to the
        # batch, which locks a selection or refuses what is not one. Not an
        # `isinstance` check against a few container types: a `frozenset`
        # or a generator would be looked up as one Patient ID and lock
        # nobody, and a patient left unlocked loses its identity at
        # `anonymize()`.
        if not isinstance(patient_id, str):
            # Read here, under the name the caller used, so a refusal says
            # `patient_id`; the batch gets the tuple and
            # reads it again, which a tuple survives.
            if not hasattr(patient_id, 'findings'):
                patient_id = _lock_selection(patient_id, "patient_id")
            return self.lock_identities_batch(
                patient_id, persist=persist, verbose=verbose, tags_to_lock=tags_to_lock)

        patient = next((p for p in self.store.patients if p.patient_id == patient_id), None)
        if not patient:
            # The ID is not logged: it may be an original Patient ID, and
            # the log file is not guarded as the store is.
            get_logger().error("lock_identities: no patient in the session "
                               "has the Patient ID given.")
            return LockingResult([])

        self._key_for_locking()
        # Drained before anything is embedded, as `audit()` and `redact()`
        # drain on entry: a queued `save()` may be capturing these
        # instances, and with `persist=True` the write below updates rows
        # that save has not committed yet -- it would find none, raise,
        # and leave an ERROR row saying the store held no row for
        # instances whose save the caller had already asked for. The
        # batch form drains in `lock_identities_batch`.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()
        return self._lock_patient_identity(patient, persist, verbose, tags_to_lock)

    def _key_for_locking(self) -> None:
        """Load the lock's key, creating it if allowed, and build its engine.

        Call it before any patient's lock is planned. The key is created only
        when no file exists at the path and no instance in the session carries
        a token this library wrote. Writes no token.

        Raises:
            RuntimeError: No key file at the path, and an instance in the
                session carries a token this library wrote. No key is created.
                The message names the key path and no patient.
            ValueError: The key file is empty or not a Fernet key.
        """
        # The engine is built here and not left to the plan: the plan builds
        # the token inside `except (TypeError, ValueError)` and reports that as
        # a value no token can hold, and the batch collects every plan's
        # `RuntimeError` as a refusal, so a malformed key or a key never loaded
        # would be misreported as either.
        #
        # A key created under a path with no file opens nothing locked before
        # it existed, so where a token of ours is in the session the lock
        # refuses rather than leave a valid key that opens nothing. Session
        # scope, not the patient's: a key minted here would be the session's
        # key from then on.
        #
        # The single lock calls this only once its patient is found, so a lock
        # of an ID no patient holds creates no key file. The batch calls it
        # after reading its selection and before it plans, found or not,
        # because it cannot plan without the engine; a batch of IDs that match
        # no patient therefore creates the key, as does a lock then refused
        # for any other reason.
        try:
            self.key_manager.load_key()
        except FileNotFoundError:
            # `holds_a_token_of_ours`, not `token_of_ours`: a token in
            # the layout releases before 1.0 wrote is one a key created
            # here could not open either, and the patient's own plan
            # refuses it by name; raising that here would refuse every
            # patient in the session over one.
            if any(self.reversibility_service.holds_a_token_of_ours(inst)
                   for p in self.store.patients for st in p.studies
                   for se in st.series for inst in se.instances):
                raise RuntimeError(
                    "lock_identities: there is no key file at "
                    f"{self.key_manager.key_path}, and this session holds an "
                    "identity token this library wrote, which a key created "
                    "here could not open and a lock would replace. Enable "
                    "reversible anonymization with the key the identities "
                    "were locked with; no key was created, and the token this "
                    "call would have written is unchanged.") from None
            self.key_manager.load_or_generate_key()
        self.reversibility_service.engine  # pylint: disable=pointless-statement

    def _lock_patient_identity(self, patient: "Patient", persist: bool,
                               verbose: bool, tags_to_lock: Optional[List[str]]
                               ) -> LockingResult:
        """Plan and write one patient's identity lock.

        Args:
            patient (Patient): The patient, already resolved; the lookup by ID
                is `lock_identities`' alone.
            persist (bool): Write each token to the store immediately.
            verbose (bool): Log a debug line per patient.
            tags_to_lock (Optional[List[str]]): The tags to capture; None for
                the defaults.

        Returns:
            LockingResult: The instances modified.

        Raises:
            RuntimeError: For any refusal `_planned_identity_lock` makes, or a
                persist `_write_identity_lock` cannot complete.
        """
        plan = self._planned_identity_lock(patient, tags_to_lock)
        return self._write_identity_lock(patient, plan, persist, verbose)

    def _planned_identity_lock(self, patient: "Patient",
                               tags_to_lock: Optional[List[str]]
                               ) -> Tuple[List[str], List[Tuple[Dict[str, Any], bytes,
                                                                List["Instance"]]]]:
        """Every refusal of one patient's lock, and the tokens it would write.

        Reads the graph and the existing tokens and writes nothing, so the
        batch can plan every patient before it locks any. One token is built
        per distinct record, captured per instance: a restore never writes one
        study's or one instance's values onto another. Every refusal is judged
        on every value-set, and an existing token against its first holder's
        capture.

        Args:
            patient (Patient): The patient to plan.
            tags_to_lock (Optional[List[str]]): The tags to capture; None for
                the defaults.

        Returns:
            Tuple: `(tags, value_sets)`. `tags` names every tag any record
                holds, in `tags_to_lock` order, for the log. Each value-set is
                `(record, token, instances)`: the record all those instances
                share, and the one token that holds it.

        Raises:
            RuntimeError: When the lock would stash what `anonymize()` left,
                lose what the existing token holds, or stash a value no token
                can hold.
        """
        patient_id = patient.patient_id
        if tags_to_lock is None:
            tags_to_lock = list(_DEFAULT_TAGS_TO_LOCK)

        instances = [inst for st in patient.studies for se in st.series
                     for inst in se.instances]

        # A name or ID an instance no longer carries is stashed from the
        # patient, as the no-instances arm below does. The
        # floor's instance rules remove those copies, so after an
        # instance-only anonymize() the copies are gone while the patient
        # still holds the originals, nothing reads as a replacement, and a
        # stash of the copies alone would write a token holding only
        # {'0010,0040': 'O'} over the good one. Where the
        # patient is itself a replacement, the refusal below names it.
        entity_fallback = {"0010,0010": patient.patient_name,
                           # What the export writes: never the synthetic
                           # key of a subject with no Patient ID.
                           "0010,0020": exported_patient_id(patient)}

        def captured(inst, tag):
            """The value the lock stashes for `tag` on `inst`.

            Args:
                inst (Instance): The instance.
                tag (str): The tag, as `gggg,eeee`.

            Returns:
                tuple: `(value, from_patient)`; `from_patient` is True when the
                    value came from the patient rather than the instance.
            """
            val = inst.attributes.get(tag)
            if val is None and tag in entity_fallback:
                return entity_fallback[tag], True
            return val, False

        # Grouped by the JSON the token would hold, sorted keys: two
        # instances whose records spell the same JSON share one token and
        # one encryption. A value JSON cannot hold (`bytes`) is keyed by
        # its type and repr, so its group still reaches the refusal at the
        # token build below, which names the tag. Graph order within and
        # across groups, so "first" is the first holder in the graph.
        groups: List[Tuple[Dict[str, Any], List["Instance"]]] = []
        by_key: Dict[str, Tuple[Dict[str, Any], List["Instance"]]] = {}
        for inst in instances:
            record = {}
            for tag in tags_to_lock:
                val, _ = captured(inst, tag)
                if val is not None:
                    record[tag] = val
            key = json.dumps(record, sort_keys=True, default=_unheld_spelling)
            if key not in by_key:
                by_key[key] = (record, [])
                groups.append(by_key[key])
            by_key[key][1].append(inst)
        if not instances:
            # Fallback to Patient object properties if no instances
            # (unlikely): judged by the refusals below like any record,
            # and embedded on nothing.
            record = {}
            if "0010,0010" in tags_to_lock:
                record["0010,0010"] = patient.patient_name
            if "0010,0020" in tags_to_lock:
                record["0010,0020"] = exported_patient_id(patient)
            groups.append((record, []))

        # A replacement is not an identity to keep. The
        # instance carries `anonymize()`'s replacement in its own tags,
        # so a lock taken after it -- the reverse of the documented
        # order, or a re-lock of an already-anonymized patient -- would
        # stash `ANONYMIZED`/`ANON_<hash>` over a good token, report
        # success, and `recover_patient_identity()` would then restore
        # the replacements everywhere while export prints that the
        # originals are recoverable. Refused, naming the value, rather
        # than skipped: a lock must never silently keep nothing. A
        # re-lock of still-original values is allowed and is what
        # recovery answers with.
        #
        # What is refused, exactly: any value about to be stashed
        # -- each tag's copy on every instance, and for name and ID the
        # patient's own value where that copy is absent (above). A copy
        # that is present is what gets stashed, so it alone is checked: a
        # patient reading ANONYMIZED beside copies that still hold the
        # originals has originals to stash.
        #
        # **What a pass wrote is read off the instance, never off a
        # policy.** Once the rule governs the name, the ID's KEEP
        # and every `value:`, `anonymize()` can leave a custom value, an
        # empty one, a shifted date or no element at all, on any tag, and
        # the constants catch none of it. The rule the session holds is
        # gone after a reopen and replaced by a re-audit or
        # `load_config()`, so a predicate on it would let a re-lock after
        # `audit(config_path=)` under a changed rule stash `Project-X`
        # over a held name, or a first lock after a reopen stash an
        # empty one. So each
        # remediation records on the instance what it left, before it
        # writes (`Instance.record_remediation`), and a value is a
        # replacement when it reads as a constant or when the instance it
        # was read from vouches for it -- that record, or `__shifted__`
        # for a shifted date. A name or ID read from the patient is on no
        # instance, so any instance whose copy the patient's write reached
        # vouches for it. The record is keyed on the value, so a restore
        # puts back originals that nothing vouches for.
        #
        # **Per value-set, not per first instance.** Each group's
        # instances are asked about the value they hold, so a patient
        # whose study 2 holds a pass's Accession Number beside a raw study
        # 1 is refused. The
        # patient's own value is asked of every instance **once per tag**
        # and remembered: it is one value, and asking it for each instance
        # that lacks a copy would make the plan quadratic.
        patient_vouched: Dict[str, bool] = {}

        def vouches(inst, tag, val, blank):
            """Whether a remediation or date shift wrote `val` on `inst`.

            Args:
                inst (Instance): The instance holding the value.
                tag (str): The tag, as `gggg,eeee`.
                val (Any): The value.
                blank (bool): Whether `val` is blank; a blank value is
                    never a shifted date.

            Returns:
                bool: True when the instance vouches for the value.
            """
            return (inst.remediation_vouches_for(tag, val)
                    or (not blank and inst.date_shift_vouches_for(tag, val)))

        def written_by_a_pass(tag, val, members):
            """Whether a pass wrote `val` at `tag` on `members`.

            Args:
                tag (str): The tag, as `gggg,eeee`.
                val (Any): The value every member holds.
                members (list): A value-set's instances.

            Returns:
                bool: True when a remediation or date shift vouches for the
                    value on an instance that holds it, or on any of the
                    patient's instances where some member holds the patient's
                    own value.
            """
            if not members:
                return False
            blank = not str(val if val is not None else "").strip()
            own = members
            if tag in entity_fallback:
                own = [inst for inst in members if inst.attributes.get(tag) is not None]
                if len(own) < len(members):
                    # Some hold the patient's value, so `val` is it.
                    if tag not in patient_vouched:
                        patient_vouched[tag] = any(vouches(inst, tag, val, blank)
                                                   for inst in instances)
                    if patient_vouched[tag]:
                        return True
            return any(vouches(inst, tag, val, blank) for inst in own)

        def is_replacement(tag, val, members):
            """Whether `val` at `tag` is a replacement rather than an original.

            Args:
                tag (str): The tag, as `gggg,eeee`.
                val (Any): The value every member holds.
                members (list): A value-set's instances.

            Returns:
                bool: True for an `ANON_` name or ID, or a value a pass
                    wrote.
            """
            return (_is_replacement_name(val) or _is_replacement_id(val)
                    or written_by_a_pass(tag, val, members))

        # **No message below names the patient.** Before `anonymize()`
        # its Patient ID is the original, and after it the pseudonym, which
        # is kept off the console; so "this patient", and the advice
        # spells the ID as a placeholder -- the caller holds the one it
        # passed, and the batch numbers each refusal. A replaced Patient ID
        # is described rather than quoted, because the validator refuses
        # any literal on it: the value is always the patient's own ID.
        # Every other replacement is quoted; it is what says which pass
        # wrote it.
        for record, members in groups:
            for tag, val in record.items():
                if not (str(val).strip() and is_replacement(tag, val, members)):
                    continue
                shown = ("a replacement Patient ID"
                         if tag == "0010,0020" or str(val) == patient_id else repr(val))
                raise RuntimeError(
                    "lock_identities: this patient already "
                    f"carries a replacement in {tag} ({shown}), so there "
                    "is no original identity left to stash. Lock "
                    "identities before anonymize(), and do not re-lock a "
                    "patient after it; the token this call would have "
                    "written is unchanged.")

        # A re-lock may not stash less than the token it replaces.
        # Under an EMPTY or REMOVE rule, `anonymize()` leaves `""` or
        # nothing, which no replacement test catches, and a lock taken
        # after it would write a token without the value over one that
        # held it. Every tag the existing token holds non-blank is
        # checked, whether or not `tags_to_lock` names it: a narrower
        # re-lock after `anonymize()` drops the held name just as a blank
        # does. A tag this lock does not name is lost
        # only where the instance no longer carries an original for it --
        # blank, absent, or a replacement -- so a narrower re-lock of
        # still-original values is allowed, and a wider re-lock
        # (a tag the token never held) loses nothing.
        #
        # **Every distinct token on the patient is read, not the first
        # instance's**, because the write embeds the new token on
        # every instance: a token on a second study -- a pair merged by
        # `audit()`, a study ingested after the lock -- would otherwise
        # be replaced unexamined. Read through the strict `held_identity`,
        # so a token of ours this key cannot open is a refusal rather
        # than "no token": a tolerant read answers None for both, and a
        # re-lock under a mistyped key path would replace a token the
        # real key opens. A foreign `(0400,0500)` is "no token" and
        # is replaced. The instances are grouped by
        # token bytes first and each distinct token is decrypted once: a
        # value-set's instances share one, and a 100k-instance patient
        # must not pay 100k decrypts.
        #
        # **Each token is judged against its first holder's capture.**
        # The token's own first instance, in graph order,
        # stands where the patient's first instance stood. Not every
        # holder: a store a release before 0.9.8 wrote carries one token
        # holding study 1's values on every study, and a re-lock of its
        # raw data would then be refused on study 2 -- whose own
        # `ACC-TWO` is not the `ACC-ONE` the token holds -- with advice
        # (restore, then lock) that writes `ACC-ONE` over it. Judged by
        # the first holder, that store re-locks, and study 2
        # gets a token of its own. The residual: on
        # such a store *anonymized*, a holder outside study 1 holds the
        # pass's value, which only a record written since 0.9.8
        # could vouch for, so its new token can stash that value -- one
        # the old token never held either.
        if instances:
            carrying: Dict[bytes, List["Instance"]] = {}
            for inst in instances:
                try:
                    content = self.reversibility_service.token_of_ours(inst)
                except _TokenOfAnEarlierLayout:
                    # Not foreign: read as foreign it would be replaced
                    # and the identity lost under a lock that
                    # reported success.
                    # 1.x does not read it, so nothing says what a
                    # replacement would lose.
                    raise RuntimeError(
                        "lock_identities: this patient carries an identity "
                        "token in the layout Isocenter wrote before 1.0, "
                        "which 1.x does not read, and this lock would "
                        "replace it; recover it with Isocenter 0.9.x and the "
                        "key it was locked with. The token this call would "
                        "have written is unchanged.") from None
                if content is not None:
                    carrying.setdefault(content, []).append(inst)
            tokens = []
            for content, holders in carrying.items():
                try:
                    values = self.reversibility_service.open_token(content)
                except _TokenOfALaterScheme:
                    # It holds a record, so the no-record text below would
                    # be false; a later release's scheme is not this one's
                    # to read, and replacing it unread is what the next
                    # refusal exists to stop.
                    raise RuntimeError(
                        "lock_identities: this patient carries an identity "
                        f"token that the key at {self.key_manager.key_path} "
                        "opens but that was written by a later release of "
                        "this library, so what it holds cannot be read here, "
                        "and this lock would replace it unread; the token "
                        "this call would have written is unchanged.") from None
                except _TokenHoldsNoRecord:
                    # The key *opens* this one, so the wrong-key text
                    # below would be false. Refused all the same: what it
                    # holds cannot be read, so nothing says what a
                    # replacement would lose. No
                    # advice that works: a key that opens it is already
                    # in hand, and there is no way to replace a token
                    # the lock cannot read.
                    raise RuntimeError(
                        "lock_identities: this patient carries an identity "
                        f"token that the key at {self.key_manager.key_path} "
                        "opens but that holds no identity record this "
                        "library writes, so what it holds cannot be "
                        "recovered, and this lock would replace it unread. "
                        "Nothing replaces a token the lock cannot read; the "
                        "token this call would have written is unchanged.") from None
                except RuntimeError:
                    raise RuntimeError(
                        "lock_identities: this patient carries an identity "
                        f"token that the key at {self.key_manager.key_path} "
                        "does not decrypt, and this lock would replace it. "
                        "Enable reversible anonymization with the key the "
                        "identity was locked with; the token this call would "
                        "have written is unchanged.") from None
                # `all`, not `any`: an instance carrying the same bytes
                # without a stamp is a file that arrived carrying it.
                stamped = all(inst.identity_token_is_this_stores(content)
                              for inst in holders)
                tokens.append((values, stamped, holders[0]))

            for held, _, holder in tokens:
                for tag, kept in held.items():
                    if not str(kept or "").strip():
                        continue
                    named = tag in tags_to_lock
                    new, _ = captured(holder, tag)
                    if not named and new is not None and str(new).strip() \
                            and is_replacement(tag, new, [holder]):
                        new = None
                    if new is None or not str(new).strip():
                        lost = ("nothing" if new is None else "an empty value") if named \
                            else "nothing (tags_to_lock does not name it)"
                        raise RuntimeError(
                            "lock_identities: this patient already has a "
                            f"locked identity holding {tag}, and this lock would "
                            f"replace it with {lost}; lock identities before "
                            "anonymize(), and do not re-lock a patient after it; "
                            "the token this call would have written is unchanged.")

            # A token this store did not write may not be replaced with a
            # value that differs from what it holds. A file
            # exported with a locked identity carries the token and the
            # pass's values but no record -- a record is never a written
            # byte -- so on a store that ingested it nothing says whether
            # a `value:` or `KEEP` rule's output is an original, and a
            # re-lock would stash `Project-X`, or a shifted birth date,
            # over the held one. The lock stamps every token it embeds
            # (`Instance.record_identity_token`, persisted as
            # `__locked__`), and a token nothing vouches for is judged by
            # what it holds: each non-blank value must be exactly what
            # this lock would stash. For a token this store wrote, a
            # deliberate `set_attr` then a re-lock
            # stashes the new value, and a tag the token never held is
            # not protected. After the loss check above, so
            # `floor_birth_only`'s message stays what it was: a blank is
            # a loss, not a different value. The message names no value:
            # the held one is an original, and the current one may be.
            for held, stamped, holder in tokens:
                if stamped:
                    continue
                for tag, kept in held.items():
                    if not str(kept or "").strip():
                        continue
                    named = tag in tags_to_lock
                    new = captured(holder, tag)[0]
                    if not _same_stashed_value(new, kept):
                        # A tag this lock does not name leaves the token
                        # altogether, and the instance keeps whatever the
                        # pass left there: "nothing", not "a different
                        # one". So the tag named is the
                        # token's own, which the caller need not have
                        # named.
                        lost = "a different one" if named \
                            else "nothing (tags_to_lock does not name it)"
                        raise RuntimeError(
                            "lock_identities: this patient's identity token did "
                            "not come from this store, so the value it holds in "
                            f"{tag} cannot be told from what anonymize() left, and "
                            f"this lock would replace it with {lost}. "
                            "recover_patient_identity(<its Patient ID>, "
                            "restore=True) puts the held values back, and a lock "
                            "after that is accepted; the token this call would "
                            "have written is unchanged.")

            # A first lock of a tag the pass emptied or removed has no
            # token to lose, but would report success over a token without
            # the value it exists to hold: a lock that silently
            # kept nothing. The record names the
            # tags themselves, on any tag and after a reopen. The advice is
            # the caller's `tags_to_lock` less those tags.
            blanked = [tag for tag in tags_to_lock
                       if any(not str(record.get(tag) or "").strip()
                              and written_by_a_pass(tag, record.get(tag), members)
                              for record, members in groups)]
            if blanked:
                rest = [tag for tag in tags_to_lock if tag not in blanked]
                advice = (f"To lock this patient without "
                          f"{'it' if len(blanked) == 1 else 'them'}, call "
                          f"lock_identities(<its Patient ID>, tags_to_lock={rest!r})"
                          if rest else
                          "tags_to_lock names no other tag, so there is nothing "
                          "else to lock")
                raise RuntimeError(
                    "lock_identities: this patient holds no value in "
                    f"{', '.join(blanked)}, which anonymize() emptied or removed, "
                    "so there is no original left to stash. "
                    f"{advice}; the token this call would have written is unchanged.")

        # A blank Patient's Name under a rule that blanks it is refused
        # even where no record says a pass wrote the blank. On a store
        # this release wrote, that blank may be the source's own, and it
        # is refused all the same. It is also what stands over a store
        # with no record that left `""` on each copy under an EMPTY rule:
        # with the rule loaded this refuses it. **Do not gate it on a
        # status.** `anonymize() -> audit() -> lock_identities()`
        # re-records CLEARED over the REMEDIATED, so a gate on REMEDIATED
        # would read "not anonymized", and the lock would write a token
        # without the original name. The rule is every policy that can
        # have written the name:
        # the one the last `audit()` resolved, and
        # `configuration.phi_tags`, which `audit(config_path=)` does not
        # assign; the audited one is named first.
        if "0010,0010" in tags_to_lock and any(
                not str(record.get("0010,0010") or "").strip() for record, _ in groups):
            policies = [policy for policy in (self._audited_phi_tags,
                                              self.configuration.phi_tags)
                        if policy is not None]
            emptying = [_owned_rule(policy, "0010,0010")[0] for policy in policies]
            emptying = [action for action in emptying if action in ("EMPTY", "REMOVE")]
            if emptying:
                rest = [tag for tag in tags_to_lock if tag != "0010,0010"]
                advice = (f"To lock this patient without the name, call "
                          f"lock_identities(<its Patient ID>, tags_to_lock={rest!r})"
                          if rest else
                          "tags_to_lock names no other tag, so there is nothing "
                          "else to lock")
                raise RuntimeError(
                    "lock_identities: this patient holds no value in "
                    f"0010,0010 under a rule of {emptying[0]} on it, and a blank "
                    "Patient's Name is not locked under a rule that blanks it. "
                    f"{advice}; the token this call would have written is unchanged.")

        # Instances to secure and nothing to stash. An empty record
        # builds no token (`generate_identity_token` returns `b""`) and
        # `embed_identity_token` embeds nothing for it, so the lock
        # would touch no instance -- a re-lock would leave the earlier
        # token in place -- and still return `N instances secured`.
        # Last among the refusals, so every more
        # specific one keeps its message. Not for a patient with no
        # instances, whose `0 instances secured` is already true; and not
        # for a record holding a blank, which is not empty (blanks are the
        # loss checks' concern above). The tags are the caller's own
        # argument; no patient, no value. **Counted per instance:** any
        # instance with nothing to stash refuses the lock, and the
        # message counts them: a patient partly locked would be reported
        # secured.
        if instances:
            empty = sum(len(members) for record, members in groups if not record)
            if empty and tags_to_lock:
                raise RuntimeError(
                    f"lock_identities: {empty} of {len(instances)} instances of this "
                    f"patient hold no value in {', '.join(tags_to_lock)}, every tag "
                    "tags_to_lock names, so there is nothing to stash on them and "
                    "the lock would secure nothing there. Name tags every instance "
                    "of this patient carries; the token this call would have "
                    "written is unchanged.")
            if empty:
                raise RuntimeError(
                    "lock_identities: tags_to_lock names no tag, so there is "
                    "nothing to stash and the lock would secure nothing. Name a "
                    "tag this patient's instances carry; the token this call "
                    "would have written is unchanged.")

        # The token is built here, in the plan, and not where it is
        # embedded: it is `json.dumps` of the values, and a value JSON
        # cannot hold (an OB element is ingested as `bytes`) would raise
        # `TypeError` from the write, after every earlier patient of a
        # batch was locked and persisted. Building it reads and writes
        # nothing, so
        # the plan still locks no one, and the failure is a refusal like
        # any other, naming the tag. One encryption per value-set, not per
        # instance: equal records share the bytes.
        value_sets = []
        for record, members in groups:
            try:
                token = self.reversibility_service.generate_identity_token(
                    original_attributes=record)
            except (TypeError, ValueError):
                unheld = []
                for tag, val in record.items():
                    try:
                        json.dumps(val)
                    except (TypeError, ValueError):
                        unheld.append(tag)
                unheld = unheld or list(record)
                rest = [tag for tag in tags_to_lock if tag not in unheld]
                kinds = sorted({type(record[tag]).__name__ for tag in unheld})
                advice = (f"To lock this patient without "
                          f"{'it' if len(unheld) == 1 else 'them'}, call "
                          f"lock_identities(<its Patient ID>, tags_to_lock={rest!r})"
                          if rest else
                          "tags_to_lock names no other tag, so there is nothing "
                          "else to lock")
                raise RuntimeError(
                    "lock_identities: this patient holds a value in "
                    f"{', '.join(unheld)} that no token can hold ({', '.join(kinds)}), "
                    "so there is nothing to stash for it. "
                    f"{advice}; the token this call would have written is unchanged."
                ) from None
            value_sets.append((record, token, members))

        tags = [tag for tag in dict.fromkeys(tags_to_lock)
                if any(tag in record for record, _ in groups)]
        return tags, value_sets

    def _write_identity_lock(self, patient: "Patient",
                             plan: Tuple[List[str], List[Tuple[Dict[str, Any], bytes,
                                                              List["Instance"]]]],
                             persist: bool, verbose: bool) -> LockingResult:
        """Embed each token of a plan in its value-set's instances.

        Args:
            patient (Patient): The patient the plan is for.
            plan (Tuple): What `_planned_identity_lock` returned for it.
            persist (bool): Write each token to the store immediately.
            verbose (bool): Log a debug line for the patient.

        Returns:
            LockingResult: The instances modified.
        """
        if verbose:
            # Counts, not the ID: see `lock_identities`. Here and not in
            # the plan, so the batch logs each patient as it locks it.
            get_logger().debug(
                f"Preserving identity for a patient of {len(patient.studies)} "
                f"stud{'y' if len(patient.studies) == 1 else 'ies'}...")
        # Encrypted once per value-set, in the plan (see its end).
        tags, value_sets = plan
        token_of = {id(inst): token for _, token, members in value_sets
                    for inst in members}
        modified_instances = []

        # Graph order, as before: every instance the plan read is in
        # exactly one value-set, and nothing runs between plan and write.
        for st in patient.studies:
            for se in st.series:
                for inst in se.instances:
                    self.reversibility_service.embed_identity_token(
                        inst, token_of[id(inst)])
                    modified_instances.append(inst)

        if persist and modified_instances:
            self.store_backend.update_attributes(modified_instances)
            get_logger().info(
                f"Secured identity (tags: {tags}) in "
                f"{len(modified_instances)} instances of one patient.")

        return LockingResult(modified_instances)

    def lock_identities_batch(self,
                              patient_ids: Union[List[str],
                                                 "PhiReport",
                                                 List["PhiFinding"]],
                              auto_persist_chunk_size: int = 0,
                              tags_to_lock: Optional[List[str]] = None,
                              *,
                              persist: bool = False,
                              verbose: bool = True
                              ) -> Union[List["Instance"], LockingResult]:
        """
        Lock the identities of several patients; see `lock_identities()`.

        Args:
            patient_ids (Union[Iterable[str], PhiReport]): The patients to
                lock: an iterable of Patient IDs (read once, so an iterator
                works), a `PhiReport`, or an iterable of findings, which
                may be mixed with IDs. Read as every other method reads
                `patient_ids`, except that `None` is refused: there is no
                spelling for "lock every patient", and the report `audit()`
                returns locks every patient its scan found.
            auto_persist_chunk_size (int): If > 0, persists changes and
                releases memory every N instances, and the call returns an
                empty list.
            tags_to_lock (List[str], optional): Passed to every patient's
                lock; `lock_identities()`'s five default tags when omitted.
            persist (bool): Passed to every patient's lock: each patient's
                instances are written as they are locked. With
                `auto_persist_chunk_size > 0` as well, an instance is
                written twice (with its patient, then with its chunk),
                which is redundant, not wrong.
            verbose (bool): Passed to every patient's lock: one debug line
                per patient.

        Returns:
            Union[List[Instance], LockingResult]: All modified instances, or
                an empty list when `auto_persist_chunk_size > 0`.

        Raises:
            RuntimeError: When reversible anonymization is not enabled, or
                when any patient found cannot be locked as asked (the
                refusals `lock_identities()` lists). Every patient is
                checked before any is locked, so the one error lists each
                refused patient with its own message, in Patient ID order,
                and no patient is locked, whatever `persist` or
                `auto_persist_chunk_size` says. A Patient ID that matches no
                patient is logged, not raised. The promise is about
                refusals, not the store: see `sqlite3.Error`. No message
                names a patient: each refusal is prefixed `[n of m]`, its
                place among the `m` patients found, in Patient ID order, so
                the refused patient is `sorted(ids that matched a
                patient)[n - 1]`. The refusal raised before any plan (no key
                file, and a token this library wrote somewhere in the
                session) is one message with no number, and creates no key.
            TypeError: When `patient_ids` is `None`, a bare `str` (the
                single-ID spelling is `lock_identities(patient_id)`),
                bytes-like, not iterable, or holds an item that is neither a
                `str` nor a finding, named by its position and type, never
                its value. Raised before the key is loaded or created and
                before any patient is planned.
            ValueError: The key file is empty or is not a Fernet key, as
                for `lock_identities()`.
            sqlite3.Error: A store write failed while tokens were being
                persisted (`persist=True` writes per patient,
                `auto_persist_chunk_size` per chunk), after one `ERROR`
                audit row; or, as `RuntimeError`, a store write found no row
                for an instance (see `lock_identities()`), with the same
                shape and timing: raised after the tokens are embedded,
                unlike the refusals above. **Nothing is rolled back across
                writes**: patients written before the failure stay locked
                in the store, the failed write stored none of its instances
                (they hold their new tokens in memory, marked modified, and
                a later `save()` writes them), and the patients after it in
                Patient ID order are not locked. One write is one
                transaction. With both `persist=True` and
                `auto_persist_chunk_size`, each instance is written with its
                patient and again with its chunk, so where a chunk write
                fails, its instances were already stored with their
                patients.
        """
        if not self.reversibility_service:
            raise RuntimeError("Reversible anonymization not enabled.")

        # The selection is read before the key, so a refused argument
        # creates no key file. Its shape through the one helper
        # every door uses, with the two knobs that exist for this caller:
        # `None` is refused rather than read as every patient -- which
        # would lock the whole session -- and an item may be a finding,
        # this method's documented input. A bare `str` or `bytes` is
        # refused, not iterated.
        if hasattr(patient_ids, 'findings'):  # PhiReport
            iterable_data = patient_ids.findings
        else:
            iterable_data = _lock_selection(patient_ids, "patient_ids")
        self._key_for_locking()

        # Normalize input to a set of strings
        normalized_ids = set()

        for item in iterable_data:
            if isinstance(item, str):
                normalized_ids.add(item)
            # `is not None`, not truthiness: an empty Patient ID is a
            # patient, and a report that names one must lock it --
            # skipped, its name would be unrecoverable after `anonymize()`
            # and the `[n of m]` numbering below would name the wrong
            # patient.
            elif hasattr(item, 'patient_id') and item.patient_id is not None:
                normalized_ids.add(item.patient_id)

        # Sorted, not a set's hash order: the refusal below names patients
        # in this order, and the locks run in it.
        start_ids = sorted(normalized_ids)

        modified_instances = []  # Only used if auto_persist_chunk_size == 0
        current_chunk = []      # Used if auto_persist_chunk_size > 0

        count_patients = 0
        count_instances_chunked = 0

        # Optimization: Create a lookup map for O(1) access
        patient_map = {p.patient_id: p for p in self.store.patients}

        # Every patient is planned before any is locked: locking as it
        # planned, a refusal part-way would leave an arbitrary subset
        # locked, and nothing would say which. A plan reads and writes
        # nothing, so a refusal leaves no token, whatever `persist` or
        # `auto_persist_chunk_size` says.
        #
        # A refusal names no patient (`_planned_identity_lock`), so each
        # is numbered by its place among the patients found, in Patient ID
        # order -- the order they are planned and locked in -- and
        # `sorted(found)[n - 1]` is the patient. An ID that matched no
        # patient is not counted.
        plans, refusals = {}, []
        found = [pid for pid in start_ids if pid in patient_map]
        missing_ids = len(start_ids) - len(found)
        for place, pid in enumerate(found, start=1):
            try:
                plans[pid] = self._planned_identity_lock(patient_map[pid], tags_to_lock)
            except RuntimeError as refusal:
                refusals.append(f"[{place} of {len(found)}] {refusal}")

        if missing_ids:
            # Counted, not named: see `lock_identities`. Before the refusal
            # below, which would otherwise swallow it.
            get_logger().error(
                f"lock_identities: {missing_ids} Patient ID"
                f"{'' if missing_ids == 1 else 's'} given matched no patient "
                "in the session (batch processing).")

        if refusals:
            raise RuntimeError(
                f"lock_identities: {len(refusals)} of {len(found)} "
                "patients cannot be locked as asked, so no patient was locked. "
                "Each is numbered by its place among the patients found, in "
                "Patient ID order. Lock the others without these, and each of "
                "these as its message says:\n" + "\n".join(refusals))

        # Drained after every plan and before the first token is embedded,
        # for the reason `lock_identities` gives. Once, here: nothing
        # below enqueues a save, so the
        # per-patient writes (`persist=True`) and the chunk flushes
        # (`auto_persist_chunk_size`) all find the rows a queued save was
        # about to write.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()

        with progress_bar(plans, desc="Locking Identities",
                          unit="patient") as pbar:
            for pid in pbar:
                # Forwarded, not hardcoded: a `PhiReport` is the README's
                # form of `lock_identities`, and a loop that writes
                # `persist=False` here turns `persist=True` on that call
                # into one that writes nothing and says nothing.
                res = self._write_identity_lock(
                    patient_map[pid], plans[pid], persist=persist, verbose=verbose)

                if auto_persist_chunk_size > 0:
                    current_chunk.extend(res)
                    if len(current_chunk) >= auto_persist_chunk_size:
                        self.store_backend.update_attributes(current_chunk)
                        count_instances_chunked += len(current_chunk)
                        current_chunk = []  # Release memory
                else:
                    modified_instances.extend(res)

                count_patients += 1

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

    def recover_patient_identity(self, patient_id: str,
                                 restore: bool = True) -> Dict[str, Dict[str, Any]]:
        """
        Decrypt a patient's identity tokens and return the identity they
        hold; with `restore=True`, also write it back onto the patient.

        **Which token speaks for the patient.** The first token of ours in
        study, series and instance order: a study without a token, or with
        an Encrypted Attributes Sequence this library did not write, is
        walked past. For a patient with a Patient ID, a first token holding
        a blank Patient ID is passed over for the first token holding a
        non-blank one, when there is one. A subject with no Patient ID is
        spoken for by its first token always, and keeps its key.

        **Every distinct token is opened before anything is written**, with
        `restore=False` too: a token of ours on any study that this key
        cannot open, or that holds no record, raises and writes nothing. So
        `restore=False` also checks that the patient is recoverable under
        this key. Every failure raises; nothing is printed, and no message
        names a Patient ID.

        **What `restore=True` writes.** Every instance of the patient
        in memory takes the locked identity tags from the token it
        carries. The restore is recorded, so a later `save()` stores it, and
        a patient already holding the restored Patient ID is merged into
        whichever of the two was in the session first.

        - Only the locked tags are put back. Every other date stays shifted
          by the patient's offset, so intervals are intact and a later
          `audit()` does not shift it again. A date among the locked tags is
          put back like any locked tag, and a later `audit()` raises it
          again.
        - A restored Study Date is also put back on each `Study`, which is
          where `export()` reads it, from that study's own token, when the
          restored value reads as a date. A blank or unreadable one leaves
          the `Study` as it is, with one WARNING per such study that carries
          no date.
        - An instance carrying no token takes only the patient-level
          identifiers (group 0010, which include Patient's Age, Size and
          Weight, and so may be another study's) of the token that speaks
          for the patient, and keeps its other locked identifiers as the
          pass left them; one WARNING gives the count. For a patient with no
          Patient ID, such an instance keeps a non-blank Patient ID of its
          own rather than take the token's blank one, and a second WARNING
          counts those.
        - A token a release before 0.9.8 shared across studies, holding a
          non-blank value outside group 0010 and not stamped by this store,
          is restored in full on the first study carrying it and as group
          0010 elsewhere, with a WARNING giving the count. A shared token a
          0.9.8 pre-release stamped is not told apart and is restored in
          full everywhere.
        - Where tokens disagree on Patient's Name or Patient ID, each
          instance keeps its own and the `Patient` takes the speaking
          token's, with a WARNING. A token whose Patient ID is blank does
          not disagree on it.

        Args:
            patient_id (str): The Patient ID the patient holds now
                (normally its pseudonym).
            restore (bool): If True, write the recovered identity back, as
                above. If False, write nothing.

        Returns:
            Dict[str, Dict[str, Any]]: The identity recovered. Each key is
                the SOP Instance UID of an instance carrying an identity
                token of ours, as it was when the call began; each value is
                a deep copy of the values that instance's token holds, keyed
                `"gggg,eeee"`, in study, series and instance order. An
                instance carrying no token is absent, and the dict is never
                empty (a patient with no token raises). Both modes return
                the same mapping, taken before `restore=True` writes
                anything, and it is **what the tokens hold, not what the
                restore wrote**: a tokenless instance a restore gives group
                0010 of the first token is absent, and a pre-0.9.8 shared
                token is returned whole on every holder though a restore
                writes only its group 0010 outside the first study. The
                patient-level answer (the token whose name and ID a restore
                stamps on the `Patient`) is `next(iter(result.values()))`.
                Two instances sharing one SOP Instance UID, which only a
                hand-built graph can hold, share one key, and the later
                one's token is the value. These are the original
                identifiers, handed to the holder of the key; nothing prints
                or logs them.

        Raises:
            FileNotFoundError: No key file at the path given to
                `enable_reversible_anonymization()`. Checked first, before
                the patient is looked up, and no key is created.
            ValueError: No patient in this session holds `patient_id`; or
                the key file is empty or is not a Fernet key, which is read
                before the patient is looked up and is not cached.
            RuntimeError: When reversibility is not enabled; the patient
                has no instances, or no instance carrying an identity token
                (an Encrypted Attributes Sequence that did not come from
                this library counts as no token, not as the wrong key); the
                key does not decrypt the token, or the key opens it but it
                holds no identity record this library writes, or was written
                by a later release of this library; an instance's token is in
                the layout releases before 1.0 wrote, the token in
                `(0400,0510)`, which 1.x does not read; or, with
                `restore=True`, a patient holding the restored Patient ID was
                de-identified under a different date-offset scheme (raised
                before anything is restored).
        """
        if not self.reversibility_service:
            raise RuntimeError("Reversibility not enabled.")

        # The key before the patient: a typo'd key path is the answer
        # whatever ID was given, and `load_key` never creates the file.
        self.key_manager.load_key()

        p = next((x for x in self.store.patients if x.patient_id == patient_id), None)
        if not p:
            raise ValueError("recover_patient_identity: no patient in this "
                             "session holds the Patient ID given")

        # Every instance of the patient, in study, series and instance
        # order, with the token of ours it carries. The whole walk, so a
        # patient whose token sits on an earlier study -- a pair merged
        # by `audit()`, a study ingested after the lock -- is found.
        # `token_of_ours`, not "any Encrypted Attributes
        # Sequence": a foreign sequence is "no token". A
        # patient with instances and no token of ours gets
        # `recover_or_raise`'s "no token", one with none gets the raise
        # below. An instance whose token is in the layout releases before
        # 1.0 wrote raises `_TokenOfAnEarlierLayout` out of this walk,
        # before anything is opened or written.
        rs = self.reversibility_service
        walk = [(st, inst, rs.token_of_ours(inst))
                for st in p.studies for se in st.series for inst in se.instances]
        if not walk:
            raise RuntimeError("recover_patient_identity: the patient has no "
                               "instances to recover an identity from")
        carrying: Dict[bytes, List[Tuple["Study", "Instance"]]] = {}
        for st, inst, content in walk:
            if content is not None:
                carrying.setdefault(content, []).append((st, inst))
        if not carrying:
            rs.recover_or_raise(walk[0][1])  # raises "no token"

        # **Every distinct token is opened before anything is
        # written.** Since the lock writes one token per value-set, a
        # patient carries several, and each instance is restored from its
        # own. A token on study 2 this key
        # cannot open raises the wrong-key text with nothing written,
        # under `restore=False` too, which is how that call answers "is
        # this patient recoverable under this key". One decrypt per
        # distinct token, in the order found, so a token on study 1 the
        # key cannot open is still the one the message is about.
        #
        # The scheme each token names comes from **that same decrypt**:
        # `schemes` is filled beside `opened` and never by a
        # second read. `open_token_with_scheme(content)` is what
        # `recover_or_raise(holders[0][1])` read, since `carrying` holds
        # only `token_of_ours(inst)` of its holders.
        opened: Dict[bytes, Dict[str, Any]] = {}
        schemes: Dict[bytes, int] = {}
        for content in carrying:
            opened[content], schemes[content] = rs.open_token_with_scheme(content)
        # **What the call returns**: each instance carrying a token
        # of ours, by the SOP Instance UID it holds now, mapped to a copy
        # of what its own token holds, in graph order. Taken here, before
        # the restore writes anything, and from `opened`, not from the
        # instances: it reports what the tokens hold, not what the
        # restore below writes (group 0010 only, for a tokenless instance
        # or a pre-0.9.8 shared token outside its first study). A **deep**
        # copy per key: instances sharing a token share one `opened` dict,
        # and the restore below writes that dict's very values onto them,
        # so a multi-valued tag (a list once the token is read, e.g. Other
        # Patient Names) copied shallowly would be the list the graph
        # holds -- an in-place edit of the result would reach the
        # sibling's entry and the graph, and move no revision.
        # From `walk`, not `carrying`: `carrying` groups by token, and a
        # token reappearing after another would put its later holder out
        # of graph order.
        recovered: Dict[str, Dict[str, Any]] = {
            inst.sop_instance_uid: copy.deepcopy(opened[content])
            for _, inst, content in walk if content is not None}
        # One token speaks for the patient -- its name and ID, the merge's
        # scheme check, and the instances carrying no token. The first found,
        # unless its Patient ID is blank and a later token's is not: a patient
        # whose first file had an empty ID and whose second carried `PA` (a
        # re-key, or a join) holds a token of `''` first, since the lock
        # stashes each copy as it is, and a restore from it would write `''`
        # over `PA`. **Not for a subject with no Patient ID**: its first token
        # is the file that made it, and a later token holding a real ID is a
        # file that linked under it (the WARNING case) -- taking that ID would
        # rename the patient after values were derived under its key.
        speaker = next(iter(carrying))
        if not is_synthetic_patient_id(p.patient_id) and not str(
                opened[speaker].get("0010,0020") or "").strip():
            speaker = next((content for content in carrying
                            if str(opened[content].get("0010,0020") or "").strip()),
                           speaker)
        original_attrs = opened[speaker]
        # The Patient ID a restore gives the patient. A subject with no
        # Patient ID locked a blank one; writing that back would make every
        # restored ID-less subject `''`, and `audit()`'s shared-ID merge
        # would collapse them into one. So a blank token ID leaves the
        # synthetic key in place.
        restored_id = original_attrs.get("0010,0020", p.patient_id)
        if (is_synthetic_patient_id(p.patient_id)
                and not str(restored_id or "").strip()):
            restored_id = p.patient_id

        if original_attrs:
            if restore:
                # Asked before anything is written: the merge below
                # refuses a group mixing jitter schemes too, but after the
                # loop every instance already holds the original values,
                # and a refusal there would leave two patients with one
                # ID under two schemes in the graph.
                self.store._refuse_a_merge_across_schemes(
                    renamed=(p, restored_id))
                # Drained before the first write, as `audit()` and
                # `redact()` drain on entry: the loop below writes onto
                # every instance and the patient, and a queued save could
                # be walking them. After the refusal, so a refused
                # restore leaves the queue as it found it. The merge's own
                # drain cannot stand in -- it runs only on a collision,
                # after these writes.
                self.persistence_manager.flush()

                def patient_level(values):
                    """The group 0010 entries of a token's values.

                    Args:
                        values (dict): Tag to value.

                    Returns:
                        dict: The entries whose tag is in group 0010.
                    """
                    return {tag: val for tag, val in values.items()
                            if tag.startswith("0010,")}

                # **A token a release before 0.9.8 shared across
                # studies.** That lock captured study 1's values and
                # embedded them on every study, so its token, on study 2,
                # holds study 1's Accession Number. It is told from a
                # token this release writes by three things together: it
                # is shared across studies, it holds a non-blank value
                # outside group 0010 (a blank one -- CT_small's Accession
                # Number, locked by the defaults -- is no study's), and it
                # is not stamped by this store on every holder. Its first
                # holding study, in graph order, takes it in full; the
                # others take its group 0010 only. **And it names no
                # scheme**: every token 1.0 writes carries
                # `"__isocenter_token__": 2` inside its encryption, which
                # says it was captured per value-set, so a marked token is
                # restored in full on every holder in any store -- the
                # stamp never reaches a file, the scheme travels with the
                # token. The stamp check stays for unmarked tokens, because
                # a 0.9.8 pre-release stamped its shared token (case (i)
                # below). The WARNING names no release: a token 0.9.8 wrote
                # per value-set carries no scheme, so exported and
                # re-ingested over values equal across studies it still
                # reads as one an earlier release shared.
                #
                # **"First in graph order" is the owner only in the store
                # that locked.** There graph order is the lock's order, so
                # the first holding study is the one the token was captured
                # from. A store built by ingesting an export loads studies
                # in path order, and export folders are `Study_<date>_...`,
                # so the full restore goes to the earliest-dated study,
                # which may not be the owner: that study then holds another
                # study's values, and the owner keeps the pass's and is the
                # one the WARNING counts. Kept, and disclosed.
                #
                # What this cannot tell: (i) a 0.9.8
                # pre-release stamped its shared token, and reads as this
                # release's; (ii) two studies whose values were equal read
                # as two that differed; (iii) a token shared inside one
                # study, over series- or instance-level tags, is not shared
                # across studies at all; (iv) an earlier release's shared
                # token whose other studies are not in the session -- one
                # of its studies ingested from its export -- is shared
                # across none, and is restored in full with values that may
                # be another study's, silently.
                partial: Dict[bytes, "Study"] = {}
                for content, holders in carrying.items():
                    values = opened[content]
                    if (schemes[content] < ReversibilityService.TOKEN_SCHEME
                            and len({id(st) for st, _ in holders}) > 1
                            and any(not tag.startswith("0010,")
                                    and str(val if val is not None else "").strip()
                                    for tag, val in values.items())
                            and not all(inst.identity_token_is_this_stores(content)
                                        for _, inst in holders)):
                        partial[content] = holders[0][0]

                tokenless = elsewhere = count = kept_ids = 0
                study_dates: Dict[int, Tuple["Study", Any]] = {}
                fallback = patient_level(original_attrs)
                for st, inst, content in walk:
                    if content is None:
                        # **An instance carrying no token takes the
                        # patient-level identifiers only**:
                        # group 0010, from the token that speaks for the
                        # patient (`original_attrs`, above). Every value of
                        # that token would give a study ingested after the
                        # lock, or the unlocked half of a pair `audit()`
                        # merged, another study's Accession Number.
                        # Nothing at all would leave a half-restored file:
                        # `export()` stamps the name and ID from the
                        # `Patient` whatever the instance holds, and birth
                        # date and sex from the instance. Group 0010
                        # includes the Patient Study module's Age, Size and
                        # Weight, which can differ by study; disclosed.
                        values = fallback
                        tokenless += 1
                        # **Not a blank token ID over a real one.**
                        # An ID-less patient's token holds the ID the
                        # export writes, `''`. A file carrying a Patient ID
                        # that linked under it after the lock (the WARNING
                        # case) holds its own; writing `''` over that would
                        # lose it, not restore it. The instance mirror of
                        # the `restored_id` guard; only this tag, only a
                        # blank token value, only a non-blank copy.
                        if (is_synthetic_patient_id(p.patient_id)
                                and "0010,0020" in values
                                and not str(values["0010,0020"] or "").strip()
                                and str(inst.attributes.get("0010,0020") or "").strip()):
                            values = {tag: val for tag, val in values.items()
                                      if tag != "0010,0020"}
                            kept_ids += 1
                    elif content in partial and st is not partial[content]:
                        values = patient_level(opened[content])
                        elsewhere += 1
                    else:
                        values = opened[content]
                        if "0008,0020" in values and id(st) not in study_dates:
                            study_dates[id(st)] = (st, values["0008,0020"])
                    for tag, val in values.items():
                        inst.set_attr(tag, val)
                    count += 1
                # Log lines, not audit rows: a restore is not a
                # de-identification step. Counts only, never an ID.
                if tokenless:
                    get_logger().warning(
                        "%d of %d instances of this patient carry no identity "
                        "token, so they took only the patient-level identifiers "
                        "(group 0010) of the token the patient's identity was "
                        "restored from, and their other "
                        "locked identifiers keep what anonymize() left (#583).",
                        tokenless, count)
                if kept_ids:
                    get_logger().warning(
                        "%d of them kept their own Patient ID: the token holds "
                        "the blank one a subject with no Patient ID exports, "
                        "and a restore does not write it over a real one (#584).",
                        kept_ids)
                if elsewhere:
                    get_logger().warning(
                        "%d of %d instances of this patient carry an identity "
                        "token shared across studies that this store did not "
                        "stamp, which may hold one study's values, so outside "
                        "the first study carrying it they took only its "
                        "patient-level identifiers (group 0010), and their other "
                        "locked identifiers keep what anonymize() left (#583).",
                        elsewhere, count)
                # **Tokens that disagree on the name or ID.**
                # Each instance keeps its own token's, so a re-lock after
                # the restore stashes each again; the `Patient` has one
                # name and one ID, and takes the speaking token's
                # (`original_attrs`), which is what `export()` stamps on
                # every study -- as the shared-ID merge stamps the
                # surviving patient's. The speaker is not always the first
                # token found: the WARNING names it, and a token
                # whose Patient ID is blank does not count as disagreeing
                # on the ID -- for a patient with one, the speaker rule
                # passed that token over on purpose, and its `''` is the
                # file's own empty copy, not a rival ID. A later token
                # holding `PA` under an ID-less patient still disagrees:
                # the patient keeps its key.
                def disagrees(values, tag):
                    """Whether a token disagrees with the speaker at `tag`.

                    Args:
                        values (dict): The token's values.
                        tag (str): The tag, as `gggg,eeee`.

                    Returns:
                        bool: True when both hold the tag with different
                            values; a blank Patient ID never disagrees.
                    """
                    if tag not in values or tag not in original_attrs:
                        return False
                    if tag == "0010,0020" and not str(values[tag] or "").strip():
                        return False
                    return values[tag] != original_attrs[tag]
                disagreeing = sum(
                    1 for values in opened.values()
                    if any(disagrees(values, tag)
                           for tag in ("0010,0010", "0010,0020")))
                if disagreeing:
                    get_logger().warning(
                        "%d of %d identity tokens of this patient hold a "
                        "Patient's Name or Patient ID different from the token "
                        "the patient's identity was restored from (the first "
                        "found, or the first holding a Patient ID); the patient "
                        "takes that token's, which export() stamps on every "
                        "study (#583).",
                        disagreeing, len(opened))

                # Update Patient Object top-level properties if Name/ID changed
                before = (p.patient_name, p.patient_id)
                if "0010,0010" in original_attrs:
                    p.patient_name = original_attrs["0010,0010"]
                if "0010,0020" in original_attrs:
                    p.patient_id = restored_id
                # Recorded, because `Patient` tracks no assignment: a
                # restore that marked nothing would be skipped by the next
                # save, which would then delete the pseudonym's row and
                # every study under it. It also retires the patient's stale
                # REMEDIATED -- a status recorded at an earlier revision
                # reads UNSCANNED, which is the truth for a patient holding
                # its original identifiers again.
                if (p.patient_name, p.patient_id) != before:
                    p.mark_modified()
                # Study Date is written onto the instances above, but the
                # exporter stamps it from the `Study` (`export_stamp_attributes`),
                # so an instance-only restore would never reach the file.
                # **Each study from its own token**: the first of
                # its instances restored in full from a token holding
                # `0008,0020`. A study whose instances carry no token, or
                # only a shared pre-0.9.8 token's group 0010, keeps its date.
                # Taken before the merge below, which can move a raw
                # patient's studies onto `p`. `_shifted_study_date` is left
                # as it is: it vouches only for the value the shift wrote,
                # so the next `audit()` raises the restored date again.
                for study, restored in study_dates.values():
                    # Read as `Study` would hold it, through the one parser
                    # its setter and hydration share: ingest maps a blank
                    # or unreadable Study Date to `None`, and writing the
                    # token's `''` or `'20041399'` over it would be saved,
                    # exported, and raised by the next `audit()` as a
                    # Study-level date to shift. A value that is not
                    # a date is not written; the instances above still take
                    # it, which is what the source held, so the instance's
                    # unreadable copy is still raised, as the source's own
                    # was. `isinstance`, not
                    # truthiness: `'20041399'` is truthy. One WARNING per
                    # such study. No date in the text, and no ID.
                    restored_date = entities.normalize_study_date(restored)
                    if not isinstance(restored_date, datetime.date):
                        get_logger().warning(
                            "The restored Study Date could not be read as "
                            "a date, so the Study keeps its de-identified "
                            "Study Date "
                            "(#619).")
                    # A restore onto a date that never moved records
                    # no change.
                    elif study.study_date != restored_date:
                        study.study_date = restored
                        study.mark_modified()
                # A raw study for the restored ID, ingested before the
                # restore, is a second `Patient` holding it: the same
                # subject by construction, so the two are merged as
                # `anonymize()` merges them, or refused if they were
                # de-identified under different date-offset schemes.
                self.store._merge_patients_sharing_an_id(
                    drain=self.persistence_manager.flush)

                get_logger().info(f"Restored identity attributes to {count} instances.")

        return recovered

    def enable_reversible_anonymization(self, key_path: str = "isocenter.key"):
        """
        Turn on reversible anonymization with the key file at `key_path`.

        Loads the key when a file is there. This call never creates the
        key file, and neither does recovery: the first `lock_identities()`
        creates it when none exists. So a mistyped path before
        `recover_patient_identity()` fails there with `FileNotFoundError`
        rather than minting a key the data was never locked under.

        Args:
            key_path (str): Path to the key file.

        Raises:
            ValueError: The file at `key_path` is not a Fernet key, or is
                empty (the message names the path). Nothing is cached by a
                failed enable: fix the file and enable again.
        """
        key_manager = KeyManager(key_path)
        service = ReversibilityService(key_manager)
        if os.path.exists(key_manager.key_path):
            key_manager.load_key()
            service.engine  # pylint: disable=pointless-statement
        self.key_manager = key_manager
        self.reversibility_service = service
        get_logger().info(f"Reversible anonymization enabled. Key: {key_path}")

    # =========================================================================
    # REDACTION & REMEDIATION
    # =========================================================================

    def redact(self, show_progress=True, force=False):
        """
        Apply the configured pixel redaction zones to every matching image.

        Uses `session.configuration.rules`: every image of a series whose
        Device Serial Number a rule matches has each of the rule's zones
        set to zero. This changes pixel data in memory (and the sidecar,
        for persistence); call `save()` afterwards to persist it. A
        redacted instance takes a new SOP Instance UID.

        **Concurrency.** A pass holds the sidecar pass-lock, shared, from
        before the first worker runs until every outcome has been applied,
        and `compact()` on any thread of this session raises while it is
        held. While a `compact()` is saving or rewriting, this call waits
        (bounded, see `Raises`) and then proceeds. A worker whose sidecar
        write cannot take the sidecar lock in time comes back as a failed
        redaction, with an `ERROR` audit row.

        Args:
            show_progress (bool): If True, displays a progress bar.
            force (bool): Redact again the instances whose
                `_ISOCENTER_REDACTION_HASH` already matches this
                configuration, instead of skipping them. For a store
                redacted with a rule of two or more zones, saved and
                reopened, on 0.9.0 or earlier, which applied only the last
                zone and still recorded the pass as complete:
                `session.redact(force=True)` then `session.save()` repairs
                it from the store's own pixels. Every instance the rules
                match is redacted again and takes a **new SOP Instance
                UID**, a new exported filename and `file_path = None`.

        Returns:
            int: How many instances had at least one configured zone
                applied to their pixels. An instance a rule *matched* but
                whose every zone fell outside the image is **not** counted;
                a zone with no area fails its instance and the pass raises
                `RedactionError`, so it is never counted. Zero means nothing
                was redacted: no rules loaded, no image matched one, every
                match was already redacted under this configuration, or no
                zone landed.

        Raises:
            RedactionError: If any instance's zone could not be applied.
                Raised at the *end* of the pass, not at the first failure:
                the instances that could be redacted are redacted, the
                failures are already `ERROR` rows in the audit log, and the
                console summary has been printed, so a caller that catches
                it still has a correct object graph and a compliance report
                that grades `REVIEW_REQUIRED`. `.failures` carries
                `(sop_uid, detail)` per failed instance. It subclasses
                `RuntimeError`, so `except RuntimeError` catches it and the
                `RuntimeError`s below alike. A failed instance is
                left exactly as it was found: no `DERIVED` flag, no
                `_ISOCENTER_REDACTION_HASH`, nothing persisted, so a
                corrected configuration retries it.
            Exception: Whatever the redaction backend raised, after logging
                it.
            RuntimeError: If the pass cannot start within 180 s because a
                `compact()` is still saving or rewriting the sidecar.
                Raised before any worker is dispatched and before any UID
                is regenerated, so there is nothing to undo.
            RuntimeError: On a `":memory:"` store when the environment asks
                for worker recycling (`ISOCENTER_MAX_TASKS_PER_CHILD`): a
                recycled worker is a process, and a process cannot reach an
                in-memory database. The message names the store, the
                variable, the cause and two remedies. Raised after the
                persistence drain and before the pass-lock: no task
                prepared, no SOP Instance UID regenerated, no pixel touched,
                no audit row. Not a `RedactionError`, since nothing was
                attempted. `ISOCENTER_FORCE_PROCESSES` does not raise: this
                call asks for threads, which outranks the variable, and a
                `WARNING` names the variable instead.
        """
        if not self.configuration.rules:
            get_logger().warning("No configuration loaded. Use .load_config() first.")
            print("No configuration loaded. Use .load_config() first.")
            return 0

        # A redaction pass must not run concurrently with a background
        # save that is serializing the very pixels it is about to
        # replace: `save()` without `sync=True` returns with `save_all`
        # still running on the persistence manager's thread, against
        # these same instances. The store's `_pixel_swap_lock` is
        # what protects direct `RedactionService` users; the pipeline
        # can simply refuse to open the window at all.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()

        # Resolved here, once, and carried to the pool. The console line
        # a few lines into `_apply_redaction_rules` names the strategy
        # off this object and `run_parallel` is handed the same object,
        # so the sentence a user reads and the pool that runs are two
        # readings of one decision rather than two guesses at it.
        # A second reading of the environment at the print site would be
        # a second implementation of `_resolve_execution_choice`'s four
        # ranks, and a second implementation is a second thing that can
        # disagree.
        #
        # Threads for a `:memory:` store, asked for per call. The
        # redaction worker is the only one that *writes to the store*
        # from inside the child (`execute_redaction_task` ->
        # `persist_pixel_data`), and `SqliteStore.__setstate__` hands a
        # spawned child `_memory_conn = None`, so a process opens a
        # fresh, empty in-memory database with no `instance_blobs`
        # table. Threads share the parent's connection. The argument
        # rather than `ISOCENTER_FORCE_THREADS`: the variable is
        # process-global and does not reach every pool.
        strategy = _resolve_strategy(
            max_workers=_redaction_worker_count(),
            chunksize=1,
            maxtasksperchild=None,
            disable_gc=False,
            force_threads=self.store_backend.db_path == ":memory:",
            show_progress=show_progress,
            desc="Redacting Pixels",
            total=None)
        # After the drain, so `docs/api/stability.md`'s frozen "`audit()`
        # and `redact()` drain the persistence manager on entry" stays
        # true verbatim of every call including a refused one -- the
        # drain mutates nothing a caller can observe, is idempotent, and
        # a caller who fixes their environment and retries wants it to
        # have happened. Outside the `try` below, so a refusal is not
        # preceded by `Redaction failed. Images already processed are
        # still redacted in memory; ...`, a sentence about work that
        # never started. Before task preparation, so a configuration
        # that cannot run is refused whether or not it had work to do.
        _report_processes_lever_on_a_memory_store(
            self.store_backend.db_path, strategy)

        # The project secret each redacted UID is derived under,
        # read once, in the parent, and handed to task preparation: no
        # worker and no task ever holds it. Created on a store that has
        # none, as `audit()` and `anonymize()` create it, and refused on a
        # store that lost the one its dates or UIDs were derived under.
        # After the refusal above, which changes nothing, and before the
        # pass-lock and any task.
        project_secret = self.store_backend._project_secret_for_use(diagnose=False)

        service = RedactionService(self.store, self.store_backend)
        try:
            # The pass-lock, shared, held from before the first
            # worker can call `regenerate_uid()` until after
            # `_apply_redaction_outcomes` has bound every loader --
            # `_apply_redaction_rules` is both -- and released by this
            # `with` on every exit, including the `RedactionError` it
            # raises at the end of a pass. While it is held, `compact()`
            # refuses; while a compaction holds it exclusive, this waits
            # (bounded) before dispatching anything. Taken holding
            # nothing: the drain above has already returned. Direct
            # `RedactionService` callers are not covered -- the pass is
            # a `Session` concept -- and `redact_by_machine` goes
            # through here, so it does not open a second one.
            with self.store_backend._hold_pass_lock():
                return self._apply_redaction_rules(service, strategy, force,
                                                   project_secret)
        except Exception:
            get_logger().exception(
                "Redaction failed. Images already processed are still redacted "
                "in memory; the rest are untouched.")
            raise

    def _apply_redaction_rules(self, service, strategy, force=False,
                               project_secret=None):
        """Run every loaded rule and apply the results to the store.

        The caller logs and re-raises a failure.

        Args:
            service (RedactionService): Prepares and runs the tasks.
            strategy (_Strategy): What `redact()` resolved before it took
                the pass-lock: worker count, progress bar, and threads or
                processes.
            force (bool): Redact instances already attested under this
                configuration; read only by the attestation skip.
            project_secret (bytes, optional): The store's secret, used in this
                process to derive each redacted instance's new UID.

        Returns:
            int: How many instances had a zone applied to their pixels.

        Raises:
            RedactionError: When any instance could not be redacted, at the end
                of the pass.
        """
        # `strategy` carries every execution decision so there is no second
        # spelling of any of them here, nothing to keep in sync with what the
        # pool is built from.
        tasks = []
        get_logger().info("Analyzing workload...")
        for pass_key, rule in enumerate(self.configuration.rules):
            rule_tasks = service.prepare_redaction_tasks(
                rule, force=force, project_secret=project_secret)
            # The audit accounting's unit is the rule-pass, and the rule
            # index is the only thing that can key it: `load_config`
            # takes rules verbatim from user YAML with no serial
            # de-duplication, so two rules can share one serial spelling
            # -- keyed on the serial they collapse into one row carrying
            # the first rule's zone count.
            for task in rule_tasks:
                task['pass_key'] = pass_key
            tasks.extend(rule_tasks)

        if not tasks:
            get_logger().warning("No matching images found for any loaded rules.")
            print("No matching images found for any loaded rules.")
            return 0

        print(f"Queued {len(tasks)} redaction tasks across "
              f"{len(self.configuration.rules)} rules.")
        # The parenthetical is read off the resolved strategy -- the very
        # object the pool below is built from -- and not derived a second
        # time here. It says *what*, never *why*: a
        # `(threads: the store is in memory)` would put the four-rank
        # order in a second place, and the `why` is what
        # `_report_processes_lever_on_a_memory_store`'s warning and
        # refusal are for, said only when it matters. The recycling pool
        # is processes and says so.
        named_strategy = "threads" if strategy.use_threads else "processes"
        print(f"Executing using {strategy.max_workers} workers "
              f"({named_strategy})...")
        get_logger().info(
            f"Starting granular redaction ({len(tasks)} tasks, "
            f"workers={strategy.max_workers}, strategy={named_strategy})...")

        # Keyed before any worker starts. A redacted image gets a new SOP
        # UID, and `run_parallel` uses threads on a free-threaded build --
        # where the workers share these very objects, not copies of them.
        # A map built after dispatch would be keyed on the post-redaction
        # UIDs and match none of the results coming back, so every image
        # would be dropped by a run that reported no error.
        #
        # Keyed on the *task's* capture, not on a fresh read of the live
        # attribute: `prepare_redaction_tasks` recorded each instance's
        # pre-dispatch UID on its task, the worker keys its mutation on
        # that same value, and one authority for "what was this
        # instance called before redaction" is what keeps the two sides
        # of the round-trip agreeing. Two rules on one instance put the
        # same key here twice; the map deduplicates to the one object.
        instances = {t['original_sop_uid']: t['instance'] for t in tasks}

        # Audit accounting per rule-pass. `targeted` is countable
        # here; `applied` is tallied by `_apply_redaction_outcomes` from
        # each mutation's own `pass_key`, because outcomes carry no
        # order and a UID join back to tasks has the two-rules-one-
        # instance ambiguity `execute_redaction_task` documents.
        passes = {}
        for t in tasks:
            acct = passes.setdefault(
                t['pass_key'], {'machine_sn': t['machine_sn'],
                                'zones': len(t['rois']),
                                'targeted': 0, 'applied': 0})
            acct['targeted'] += 1

        # Pixel I/O and NumPy ops release the GIL. The generator is consumed
        # incrementally so each worker's image is applied and released rather
        # than held until the end.
        # `yield_exceptions=True` is what makes `_apply_redaction_outcomes`'
        # Exception arm reachable. Without it every strategy re-raises a
        # lost worker at the point of iteration, so the `for` below would
        # end mid-pass: every mutation still queued discarded unapplied,
        # no ERROR row written for anything, and a bare
        # `BrokenProcessPool` raised instead of `RedactionError`.
        # The strategy `redact()` resolved, handed over rather than
        # resolved again: `max_workers`, `chunksize`, `desc`, the
        # progress-bar setting and the threads-or-processes decision are
        # all inside it, and the console line above printed from the same
        # object. The `:memory:` store's request for threads is on
        # the `_resolve_strategy` call in `redact()`, with the comment
        # explaining why it has to be an argument; it beats
        # `ISOCENTER_FORCE_PROCESSES` by the documented order and loses to
        # worker recycling, which is why that combination is refused
        # before this point rather than failing here.
        # Before dispatch, not when each outcome lands: under threads the
        # worker writes to the live instance, so by the time the parent
        # sees an outcome the status it would read is already UNSCANNED.
        # See `capture_phi_status_for_redaction` for what is kept and why.
        captured = {sop: capture_phi_status_for_redaction(inst)
                    for sop, inst in instances.items()}

        mutations = run_parallel(
            service.execute_redaction_task,
            tasks,
            strategy=strategy,
            return_generator=True,
            yield_exceptions=True)

        applied, failures = self._apply_redaction_outcomes(
            mutations, instances, self.store_backend, passes)

        # Every instance, landed or not, and before the raise below: a
        # skipped instance was not written to and records the status it
        # already carries, which `record_phi_status` ignores; a failed one
        # may have been half-written under threads, and the guard decides
        # on what actually changed, not on the outcome's word for it.
        for sop, inst in instances.items():
            carry_phi_status_across_redaction(inst, captured[sop])

        # The audit row for every pass that targeted anything, written in
        # the parent and before the failure raise below, exactly
        # as the serial path orders it: a caller that catches
        # `RedactionError` still holds a report whose section 2 accounts
        # for this run. Before `scan_burned_in_annotations` too,
        # so a crash in the risk scan cannot cost the run its redaction
        # accounting.
        for acct in passes.values():
            service.record_redaction_pass(
                acct['machine_sn'], acct['zones'],
                acct['targeted'], acct['applied'])
        # Recorded beside the emitter, not at the top of `redact()`: the
        # grade demands REDACTION evidence only from a session that
        # would have emitted it, and a call with no rules loaded or no
        # matching images returned before this point.
        self._actions_performed.add("REDACTION")

        if applied < len(tasks):
            get_logger().warning(
                f"Redaction updated {applied} of {len(tasks)} targeted images. "
                "The remainder returned no change: already redacted under this "
                "configuration, pixel data that would not load, no configured "
                "zone that landed inside the image, or a worker that failed "
                "-- see the entries above for which.")

        service.scan_burned_in_annotations()

        print(f"Redaction complete: {applied} of {len(tasks)} images updated. "
              "Remember to call .save() to persist.")

        # Last, deliberately. The RISK rows `scan_burned_in_annotations`
        # writes and the summary a user reads have to be in place whether
        # or not the caller catches this, and every successful mutation is
        # already on the graph -- so a caller that catches `RedactionError`
        # still has a correct object graph and a report that grades
        # REVIEW_REQUIRED.
        if failures:
            raise RedactionError(failures, len(tasks))
        return applied

    @staticmethod
    def _apply_redaction_outcomes(outcomes, instances, store_backend=None,
                                  passes=None):
        """Copy each worker's result back onto the in-memory instance.

        A mutation is applied whole, including the new SOP Instance UID the
        worker regenerated; an instance whose zones all missed returns no
        mutation and keeps its identity. A failed or unrecognised result
        (`Exception`, `None`, anything but a `RedactionOutcome`) becomes a
        failure row. Must run in the parent process, which writes the audit
        rows.

        Args:
            outcomes (Iterable): What the workers returned.
            instances (dict): Pre-redaction SOP UID to the instance in this
                process.
            store_backend (SqliteStore, optional): The store the failure rows
                go to, and whose pixel-swap lock guards each write.
            passes (dict, optional): Rule-pass key to its audit accounting.

        Returns:
            Tuple[int, List[Tuple[str, str]]]: How many mutations landed, and
                `(entity_uid, details)` per failure.
        """
        # The new identity must be applied here: `execute_redaction_task` calls
        # `regenerate_uid()` in the worker, which under threads is the parent's
        # object and under processes a copy, so without this the SOP Instance
        # UID (and the exported filename) would depend on which executor ran.
        #
        # The gate is the existence of the mutation: `execute_redaction_task`
        # builds it only inside `if modified:`. There is no second gate on the
        # UIDs differing, because two gates on one question can disagree.
        #
        # The audit write stays in the parent: `SqliteStore.__setstate__`
        # starts a new audit thread in a child that is torn down at pool
        # shutdown without `stop()`, so a queued row can be lost, and for a
        # `:memory:` database the child writes nowhere at all.
        applied = 0
        failures = []

        for outcome in outcomes:
            if isinstance(outcome, RedactionOutcome):
                if not outcome.ok:
                    sop = outcome.sop_instance_uid or "UNKNOWN"
                    failures.append(
                        (sop, f"Redaction failed for {sop}: {outcome.error}"))
                    continue
                mutation = outcome.mutation
                if not mutation:
                    # A legitimate skip: already redacted under this
                    # configuration, no pixel data to redact, or no
                    # configured zone that landed inside the image. The
                    # shortfall is summarised by the caller.
                    continue
            elif isinstance(outcome, Exception):
                # `run_parallel` handing back a worker that died -- a shape
                # that only exists because the dispatch above asks for it
                # with `yield_exceptions=True`. There is no outcome
                # to name the instance with, and the row still has to
                # exist.
                failures.append(
                    ("UNKNOWN",
                     f"Redaction worker failed: {describe_exception(outcome)}"))
                continue
            else:
                failures.append(
                    ("UNKNOWN", "Redaction worker returned an unrecognised "
                                f"result: {outcome!r}"))
                continue

            # `original_sop_uid` only. `instances` is keyed on
            # **pre**-redaction UIDs (see `_apply_redaction_rules`), and
            # `sop_uid` is the **post**-redaction one, so a fallback to
            # `sop_uid` could never find anything, and one name meaning
            # both the lookup key and the new identity would read wrong.
            sop = mutation.get('original_sop_uid')
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
            if loader or mutation.get('pixel_hash'):
                # Under the store's pixel-swap lock: this rebind is the
                # process-executor arm of the same straddle
                # `persist_pixel_data` closes -- a background save
                # (`_persist_pixels`) that read this instance's resident
                # array before the worker redacted its copy must not
                # publish its loader *after* this one lands, or the
                # instance reads back unredacted pixels under a full
                # redaction attestation. A store-less call (unit
                # tests drive this method directly) has no second writer
                # to race, so it also needs no lock.
                lock = (store_backend._pixel_swap_lock
                        if store_backend is not None
                        else contextlib.nullcontext())
                # And the pixel-state leaf inside it: the rebind,
                # the record clear and the null below land wholly before or
                # after a `set_pixel_data()` or `discard_pixel_data()` on
                # another thread. Nothing under it logs or takes a lock.
                with lock, entities.PIXEL_STATE_LOCK:
                    if loader:
                        instance._pixel_loader = loader
                        # And the label of the frame it reads. The
                        # worker's read relabelled its own copy wherever
                        # the decode converted, so this is `RGB` over a YBR
                        # file that pydicom or the handler returned as RGB.
                        # Under threads the worker *is* this instance and
                        # the write repeats what is already there.
                        #
                        # Not `Instance._relabel_to_decoded_colour`, which
                        # is a *read's* relabel, called only by a read that
                        # publishes into an empty slot: this copies
                        # a result across, beside the loader it describes,
                        # whatever is resident. A dict
                        # write takes no lock and logs nothing, so the leaf
                        # stays a leaf. The `mark_modified()` after the
                        # lock moves the revision.
                        #
                        # Inside `if loader:`, beside the rebind, for the
                        # reason the `_pixel_descriptors_replaced` null
                        # below gives: the label describes the frame the
                        # loader reads, and a mutation carrying only a hash
                        # leaves the loader, and so the label, where they
                        # were. That placement is reasoned, not pinned by
                        # a test.
                        label = mutation.get('photometric_interpretation')
                        if label is not None:
                            instance.attributes["0028,0004"] = label
                        # The loader reads the worker's frame now, and the
                        # descriptors in `attributes` describe it: a record
                        # from a `set_pixel_data()` made before the pass
                        # describes the frame this rebind replaced, and a
                        # discard restoring it would put those over the
                        # redacted frame. Inside `if loader:`, never
                        # one indent out, for the reason the null below
                        # gives: a mutation carrying only a hash leaves the
                        # loader on the frame the record describes.
                        instance._pixel_descriptors_replaced = None
                        # And drop whatever this process is still holding.
                        # Under processes the worker redacted a
                        # *copy*: its `discard_pixel_data()` freed the
                        # child's array, and the parent kept the
                        # pre-redaction one. Rebinding the loader without
                        # this line leaves the instance with two answers
                        # to "what are my pixels" -- a resident stale
                        # array and a loader on the redacted frame -- and
                        # everything that asks the instance takes the
                        # stale one: `_persist_pixels` hashes it, misses
                        # its `_pixel_hash == digest` dedup guard (that
                        # hash is the redacted one), appends the stale
                        # frame to the sidecar and rewires the loader to
                        # it, so the corruption becomes durable and every
                        # later session exports pre-redaction pixels under
                        # a full redaction attestation.
                        #
                        # Inside `if loader:`, never one indent out. A
                        # mutation carrying `pixel_hash` and no loader has
                        # no redacted frame to fall back on, so nulling
                        # there would leave `_persist_pixels` recording
                        # the *stale* loader frame stamped with the *new*
                        # hash -- store, sidecar and `_pixel_hash` all
                        # agreeing on the wrong frame with every integrity
                        # check passing.
                        #
                        # Inside the lock, and it belongs there: this is
                        # the same `_pixel_swap_lock` `_persist_pixels`
                        # reads under, so the null is atomic against a
                        # concurrent background save rather than a second
                        # window into it. The
                        # `mark_modified()` below runs after the lock and
                        # the null does not depend on that ordering; do
                        # not tidy the null out to join it.
                        #
                        # Not a memory hazard: nulling the instance's
                        # reference does not invalidate an array the
                        # caller already holds. It only stops the
                        # *instance* serving pre-redaction pixels.
                        #
                        # This is the third `pixel_array = None` site, and
                        # `_persist_pixels`' `arr is None` arm enumerates
                        # them -- it is updated with this one.
                        #
                        # The null is also what makes the divergence flag
                        # irrelevant here, so no `_pixel_array_unwritten =
                        # False` stands beside it: `unload_pixel_data()`
                        # returns True on a `None` array *before* it
                        # consults the flag, and `get_pixel_data()`'s
                        # loader arm clears it on the next read. Restore
                        # one without the other -- put the array back and
                        # leave the flag set -- and the instance becomes
                        # silently unfreeable: `release_memory()` refuses
                        # it for the rest of the session and only logs a
                        # count.
                        #
                        # On the threads path the worker mutated this very
                        # instance and its `finally` already discarded the
                        # array, so this null lands on `None`; both
                        # executors leave identical state.
                        instance.pixel_array = None
                    if mutation.get('pixel_hash'):
                        instance._pixel_hash = mutation['pixel_hash']

            new_uid = mutation.get('sop_uid')
            if new_uid:
                # A `None`-safety guard on a `dict.get`, not a gate. The
                # gate was passed above: reaching here means the worker
                # returned a mutation, which it does only after
                # `regenerate_uid()`. Do not add `and new_uid != sop`: it
                # is always true, and a condition that reads as a gate
                # while deciding nothing is a second answer to the one
                # question. A mutation built for an instance nothing was
                # applied to is caught by the tests, not by this line.
                #
                # Assign all three or none. Under processes the child
                # mutated a copy, so without this the parent would keep
                # the source's identity while carrying `DERIVED` and a
                # Derivation Code Sequence -- and the blob the worker
                # persisted under the regenerated UID would be stranded.
                instance.sop_instance_uid = new_uid
                instance.attributes["0008,0018"] = new_uid
                # Recorded here as well as in `regenerate_uid()`, and
                # both are needed. Under threads the worker *is* this
                # object and has already written it; under processes it
                # wrote it on a copy that is discarded, and `sop` --
                # `mutation['original_sop_uid']` -- is this process's own
                # authority for the same fact. `setdefault` makes the
                # two paths agree and keeps a `force=True` re-redaction
                # from replacing the original identity with a generated
                # one. Same shape and same reason as the
                # `file_path = None` below it.
                instance.attributes.setdefault(SOURCE_SOP_UID_ATTR, sop)
                # `regenerate_uid()` ends the same way, deliberately: the
                # instance no longer matches the file it was read from.
                instance.file_path = None

            instance.mark_modified()
            applied += 1
            # Attributed by the mutation's own `pass_key` rather than
            # by joining the UID back to a task: two rules matching one
            # instance produce two mutations under one pre-redaction UID,
            # and each belongs to its own pass's row.
            if passes is not None:
                acct = passes.get(mutation.get('pass_key'))
                if acct is not None:
                    acct['applied'] += 1

        return applied, _report_redaction_failures(failures, store_backend)

    def redact_by_machine(self, serial_number: str, roi: List[int]):
        """
        Redact one zone on one machine's images, without editing the
        configuration.

        Replaces the configuration's rules with one rule for
        `serial_number` holding `roi`, runs `redact()`, and restores the
        original rules afterwards, also when `redact()` raises.

        Args:
            serial_number (str): The device serial number to target.
            roi (List[int]): The Region of Interest as [y1, y2, x1, x2].

        Raises:
            RedactionError: Propagated from `redact()` when the zone could
                not be applied. The original rules are restored first.
            RuntimeError: Propagated from `redact()`, which refuses a
                `":memory:"` store whose environment asks for worker
                recycling and a pass-lock wait that expires. The original
                rules are restored first.
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
        Apply the remediation each PHI finding proposes (tag anonymization).

        With `findings`, only those findings are remediated. An empty list,
        tuple, `PhiReport` or iterator applies nothing and returns 0: a
        filtered report that matched nothing is not a request to remediate
        everything. Only `findings=None`, or no argument, runs a full
        `audit()` under the current configuration and remediates every
        finding it raises. An empty call still reads the store's project
        secret, so a store that has lost it refuses rather than returning 0.

        Nothing outside `session.store` is written. A finding whose
        `entity` is itself in the graph is acted on as it is, except a
        `REMOVE_TAG`, whose "already gone" is read on the object at the
        finding's address, and which declines where the two differ. Any
        other finding is resolved against the live graph at its
        `entity_uid` and `entity_path` (an instance's UID from before
        `redact()`, and a patient's original Patient ID after the pseudonym
        this store minted for it, included) and acts on the object found
        there, so a report kept across `close()` and a reopen cleans the
        graph `export()` writes. A finding whose address names no single
        object declines; one inside a sequence a pass already removed or
        emptied is satisfied. A Patient ID is written, and a date shifted,
        only with a value that belongs to the live patient holding it (this
        store's pseudonym, and an offset seeded on that patient under its
        own scheme), and declines otherwise. The findings passed are not
        modified.

        Two patients left holding one Patient ID (a study ingested under a
        patient's original ID after that patient was anonymized) are merged
        into whichever was in the session first, and the other is removed
        from `store.patients`.

        Args:
            findings (List[PhiFinding], optional): Specific findings to clean.

        Returns:
            int: How many remediations were applied. Failures are logged and
                excluded, so a caller can tell a clean run from a partial
                one.

        Raises:
            RuntimeError: When two patients left holding one Patient ID
                were de-identified under different date-offset schemes.
                Raised at the merge, after the remediations are applied.
                Unreachable on a graph the library built, and reachable on
                one built in user code.
        """
        from .remediation import RemediationService

        if findings is None:
            # Blind execution: scan with the current configuration, then
            # remediate. First, so the secret below is read after the
            # scan's own first use and its notices are written once.
            #
            # `is None`, not `not findings`: `PhiReport` has `__len__`,
            # so an empty report is falsy, and `anonymize([])` or a
            # filtered report that matched nothing would run a full audit
            # and a full pass, while an empty *iterator*, which is
            # truthy, would apply 0. An empty argument is a request for
            # nothing, whatever container it arrives in.
            findings = self.audit()

        # Asked for here as well as in `audit()`, and never skipped: the
        # findings-given path does not enter `audit()`, and a store that
        # lost its secret has to refuse before anything is shifted.
        # `diagnose=False` because the audit that produced these findings
        # already wrote its notices.
        project_secret = self.store_backend._project_secret_for_use(
            diagnose=False)
        remediator = RemediationService(
            store_backend=self.persistence_manager.store_backend,
            date_jitter_config=self.configuration.date_jitter,
            project_secret=project_secret,
        )

        # What `audit()` put on this report: the policy it scanned
        # under and its tally. Read before `findings` is rebound below.
        report_policy = getattr(findings, "_scan_policy", None)
        report_tally = getattr(findings, "_scan_tally", None)
        # The session's own tally when it has one;
        # otherwise this session's working copy of the report's, so a pass
        # over a kept report records what the same pass would have in the
        # session that scanned it, and the report stays as its audit left
        # it.
        from_report = self._scan_tally is None and report_tally is not None
        tally = (self._working_tally(report_tally) if from_report
                 else self._scan_tally)

        count = 0
        if findings:
            # Which revision each status was recorded at before the pass,
            # so the statuses the pass records can be told from the rest.
            recorded_at = {id(entity): entity._phi_status_revision
                           for entity in self._status_bearers()}
            # The entities the report's scan raised under, read before the
            # pass can replace a patient's ID. Only when the tally settling
            # this pass is the report's own: under another audit's tally,
            # the report's policy is not the scan being settled against.
            named = (self._named_by(tally)
                     if from_report and report_policy is not None
                     else frozenset())
            # Resolved against the live graph before the service sees them,
            # and only here, after the blind-execution check above:
            # a report every finding of which a pass already settled
            # resolves to an empty list, and that is not the `None` that
            # asks for a full audit and pass. An argument that
            # arrived empty reaches here too, and what this guard saves
            # then is cost, not behaviour: the three resolvers would walk
            # the whole graph for nothing and `apply_remediation([])`
            # would return 0 either way. One UID map for the three
            # readers, so "the instance at this address" cannot mean one
            # thing to the resolver and another to the owners or the
            # removal targets.
            by_uid = self._instances_by_uid()
            findings, gone = self._live_findings(list(findings), project_secret, by_uid)
            owners = self._nested_finding_owners(findings, by_uid)
            remediator._use_gone_keys(gone)
            remediator._use_instance_owners(owners)
            remediator._use_holders(self._finding_holders(findings, owners))
            remediator._use_copy_owners(
                self._copy_owners(),
                {(None if f.entity is None else id(f.entity),
                  f.remediation_proposal.target_attr) for f in findings
                 if f.entity_type in ("Patient", "Study", "Series")
                 and f.remediation_proposal is not None})
            remediator._use_removal_targets(
                self._removal_targets(findings, by_uid, project_secret))
            remediator._use_scan_tally(tally, findings)
            remediator._use_series(
                (series for patient in self.store.patients
                 for study in patient.studies for series in study.series),
                # The policy the lock reads a rule by: the
                # last audit's, which `audit(config_path=)` does not put on
                # the configuration, else the configuration's.
                (self._audited_phi_tags if self._audited_phi_tags is not None
                 else self.configuration.phi_tags))
            # The pass's own Series writes do not cascade to its instances
            # (`entities.PASS_WRITING`); set here rather than in
            # `apply_remediation`, whose five line-pinned `mark_modified()`
            # calls would move.
            passing = PASS_WRITING.set(True)
            try:
                count = remediator.apply_remediation(findings)
            finally:
                PASS_WRITING.reset(passing)
            if named:
                self._adopt_the_reports_policy(report_policy, recorded_at, named)

        # A patient ingested under its original ID after that patient was
        # anonymized has just been given the pseudonym the stored patient
        # already carries: two objects, one row, and the next save's
        # scoped deletes would remove each other's studies. Merged
        # here, after every proposal in the report has been applied
        # under the scheme its scan stamped -- never inside
        # `apply_remediation`, which would move the five line-pinned
        # `mark_modified()` calls. The drain
        # runs only when there is something to merge: `audit()` drains on
        # entry and this path, handed its findings, does not.
        self.store._merge_patients_sharing_an_id(
            drain=self.persistence_manager.flush)

        if count:
            # A nonzero count is the session claiming remediations were
            # applied, and an applied remediation queues its audit row
            # -- so this is where "performed" and "would have emitted"
            # coincide. A call that applied nothing records
            # nothing and owes the summary no evidence.
            self._actions_performed.add("ANONYMIZE")

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
            **options (dict): Passed through to the selected exporter. The
                DICOM format's options are listed under "DICOM export
                options".

        Returns:
            Any: The selected format's own result object. The DICOM
                exporter returns an `io_handlers.ExportSummary`, whose
                `written` counts the files that reached disk and whose
                `failures` names the instances that did not. `written` is
                counted over *de-duplicated* UIDs, because the UID names the
                output file: two instances sharing one are two successful
                write operations and one file, the second having overwritten
                the first. The WFDB exporter returns a `List[str]` of paths;
                an empty list means nothing was attempted, because an export
                that attempted records and wrote none raises instead.

        Raises:
            ValueError: If `format` is not a registered export format.
                Also on `dicom`, before anything is written, for a `subset`
                query that does not run, or a `subset` DataFrame with none of
                SOPInstanceUID, SeriesInstanceUID, StudyInstanceUID and
                PatientID.
            TypeError: For an option name the selected exporter does not
                recognise; nothing is written. The two formats do not accept
                the same options, so a caller forwarding one dict to both
                must split it. Also, on both formats and before anything is
                written, for a `patient_ids` that is a bare `str` (wrap one
                ID in a list), bytes-like, not iterable, or holds an element
                that is not a `str`; and on `dicom` for a `subset` that is
                bytes-like, not iterable, or holds an element that is not a
                `str`.
            io_handlers.ExportError: From either exporter, when zero of N
                attempted instances reached disk and at least one failed.
                An empty plan (zero of zero) does not raise: a subset that
                matched nothing is a fact about the run, and the `EXPORT`
                audit row already carries it. Nor does a DICOM export whose
                every instance the pre-export scan withheld: nothing was
                attempted, and its `WARNING` rows grade the run.

        Either format writes one `WARNING` audit row, and changes nothing it
        writes, when the instances it writes carry PHI statuses recorded
        under a policy that is neither the one in force nor one this
        session scanned under, or with no recorded policy (a store written
        before 1.0); the report then grades `REVIEW_REQUIRED`.
        `check_burned_in=True` re-audits first, so it never does.

        Either format also writes one `WARNING` audit row, and logs one
        `WARNING` line, when `patient_ids` names an ID no patient in the
        session holds: counted by position, never named, and the report
        then grades `REVIEW_REQUIRED`. The patients that match are exported
        as asked; nothing raises. The `dicom` format does the same for a
        `subset` value that names nothing in the session at any level.

        A format served by any exporter other than the two built-in classes
        (one registered through `exporters.register`, a subclass of a
        built-in, or another class registered as `dicom`) writes one
        `WARNING` audit row before it runs, saying its output is not
        attested by Isocenter, so the report grades `REVIEW_REQUIRED`. None
        of the gates above runs for it, and it writes no `EXPORT` row. The
        registry is provisional until 1.1; see the exporter registry page
        in the API reference.
        """
        # Cleared first, before the exporter is even resolved. These are
        # session-scoped, and assigning them only on success would let an
        # export with an empty plan -- or one whose batch died at the
        # pool -- leave a *previous* export's numbers standing. None
        # makes the report omit the row, and an
        # absent row says "not answered here" -- which is the truth
        # about an export that never completed, where a zero would say
        # "nothing was written" and a stale pair answers for the wrong
        # export.
        #
        # Here and not in `_export_dicom`: a call that
        # raises before any exporter runs (an unknown format), inside one
        # (an option it does not take), or that goes through an exporter
        # which does not report delivery (`wfdb`) answers nothing about
        # DICOM delivery either, and must not leave the last DICOM
        # export's pair answering for it. `_export_dicom` is private; a
        # caller reaching it directly bypasses this and inherits the pair.
        self._last_export_written = None
        self._last_export_requested = None

        from . import exporters

        exporter = exporters.get_exporter(format)
        # Every export gate lives inside the two built-ins, so an
        # exporter that is neither runs behind none of them and its
        # output is not something this report can vouch for. One
        # `WARNING` row, so the run grades `REVIEW_REQUIRED`.
        # By class identity: `__module__` is whatever a plugin spells and
        # a subclass inherits it, a subclass may override anything, and a
        # format name is whatever was registered over. `is not`, never
        # `not in (...)`: a tuple's `in` falls back to `==`, and a
        # metaclass whose `__eq__` answers True would pass for a built-in.
        # Before dispatch, so a plugin that raises still leaves the row.
        cls = type(exporter)
        if (cls is not exporters.dicom.DicomFormatExporter
                and cls is not exporters.wfdb.WfdbExporter):
            detail = (
                f"Export to {folder} in format {format!r} ran "
                f"{cls.__module__}.{cls.__qualname__}, an exporter "
                "Isocenter does not ship: its output is not attested by "
                "Isocenter. None of the export gates ran for it (the "
                "burned-in re-audit, the configured redaction zones, the "
                "drop of nested icons that may show redacted pixels, the "
                "filter that drops the underscore bookkeeping keys, one of "
                "which holds the source SOP Instance UID, the "
                "recoverable-identity disclosure, the de-identification "
                "markers, the owner stamps, the EXPORT and DATA_LOSS "
                "rows), so this report does not know what it wrote "
                "(#527).")
            get_logger().warning(detail)
            self.store_backend.log_audit(action_type="WARNING",
                                         entity_uid=folder, details=detail)
        return exporter.export(self, folder, **options)

    def _export_dicom(self, folder: str, use_compression=True,
                      check_burned_in=False, check_reversibility=True,
                      patient_ids: List[str] = None, show_progress=True,
                      subset=None, verify_readback=False):
        """
        Export the session as DICOM, one folder per patient, study and series.

        Args:
            folder (str): The output directory path.
            use_compression (bool): If True, compresses output images using
                JPEG 2000 (lossless). A 16-bit image with more than one
                sample is written this way too, and pydicom with only its
                Pillow plugin cannot decode it; the export names each such
                instance at INFO.
            check_burned_in (bool): If True, scans for PHI before exporting and
                withholds every instance that still carries an identifier,
                at any level of its hierarchy. Each withheld instance
                writes one `WARNING` audit row naming it and the level
                (patient, study, series or instance) that carried the
                identifier, never the value, so the report grades
                `REVIEW_REQUIRED` and lists them in section 4. Withheld
                instances count as requested and not written ("1 of 2
                requested"), and the `EXPORT` row says how many were
                withheld. An export that withheld everything returns an
                empty summary and does not raise: nothing failed. An
                instance outside `subset` is not withheld; it was never
                asked for.
            check_reversibility (bool): If True (the default), warn when the
                files this export wrote still carry the encrypted originals
                that `lock_identities()` embeds, and record the disclosure in
                the audit log. The check runs after the write, against what
                reached disk. Those identities are recoverable by anyone
                holding the key, which a recipient of the cohort cannot see
                for themselves. Passing False silences the warning and skips
                the audit entry. The export itself is unchanged either way:
                this reports, it does not withhold.
            patient_ids (Iterable[str], optional): Limit export to
                specific Patient IDs. Only `None`, or the parameter
                omitted, means every patient: an empty list, tuple or
                set is a filter that selected nobody and nothing is
                written. An iterator is read once before the walk. A bare
                `str` (wrap one ID in a list), a bytes-like value, a
                non-iterable, or an element that is not a `str` is refused
                with `TypeError` before anything is flushed or written. An
                ID no patient in the session holds selects nothing and is
                counted, never named: one `WARNING` log line and one
                `WARNING` audit row, so the report grades
                `REVIEW_REQUIRED`, while the patients that do match are
                exported as asked. After `anonymize()` a patient is selected
                by its replacement ID. The `wfdb` format and
                `get_cohort_report()` read `patient_ids` the same way.
            show_progress (bool): If True, shows progress bar.
            subset (Union[str, pd.DataFrame, Iterable[str]]): Filter the
                export: a pandas query string run against
                `get_cohort_report(expand_metadata=True)`, a DataFrame
                (read by the first of SOPInstanceUID, SeriesInstanceUID,
                StudyInstanceUID and PatientID it carries; one with none
                of them raises `ValueError`), or any other iterable of
                UIDs at any level (list, tuple, set, a generator), read as
                `patient_ids` is. Only `None` means no filter; an empty one
                selects nothing. A bytes-like value, a non-iterable, or an
                element that is not a `str` raises `TypeError` before
                anything is scanned, flushed or written. A value that names
                nothing in the session at any level (itself, the UID this
                store replaced it with, or, for a SOP Instance UID taken
                before `redact()`, the instance's redacted UID) is counted
                by position and never named: one `WARNING` log line and one
                `WARNING` audit row, so the report grades
                `REVIEW_REQUIRED`, while the rest is exported as asked. A
                query cannot name anything the session lacks, so it is never
                counted.
            verify_readback (bool): If True, each worker re-reads the file
                it just wrote before it is published under its real name,
                and holds it against what it meant to write: Rows,
                Columns, SamplesPerPixel, NumberOfFrames and
                BitsAllocated against the dataset it serialized; then the
                file's `PhotometricInterpretation` against the transfer
                syntax the file itself carries, which must admit it and be
                a single value; then every pixel sample, decoded the way
                `ingest()` decodes it and compared bit for bit with the
                samples written, after redaction; and a DICOM waveform's
                `WaveformData` bytes. The stored samples are compared, not
                a colour conversion of them; a file labelled `YBR_FULL` or
                `YBR_FULL_422` over samples that are not unsigned 8-bit is
                also decoded with pydicom's colour conversion, as `ingest()`
                decodes it, and fails when that decode raises, which it does
                for 16-bit and int8 samples. A value outside the declared
                BitsStored fails an uncompressed file, because every
                conformant reader masks it (-3024 at BitsStored 12 reads as
                1072).

                **Passing True can cost a file the default export
                delivers.** By default a Photometric Interpretation the
                written syntax does not admit (`YBR_ICT` or `YBR_RCT` on an
                uncompressed file, `YBR_PARTIAL_422`/`_420` on any this
                exporter writes) is written exactly as the instance declared
                it, with a `WARNING` audit row and a `REVIEW_REQUIRED`
                grade. With True that instance fails, gets an `ERROR` row,
                and no file for it reaches the output folder; the reason
                names the label, the syntax and a remedy. The check does not
                ask whether the samples are really in the colour space the
                label names: `RGB` over YBR samples passes, and so does a
                file under a transfer syntax the check has no row for.

                An unreadable or undecodable file, or any mismatch, fails
                that instance's export: it is counted out of "Instances
                Written", gets an `ERROR` audit row and takes the grade to
                `REVIEW_REQUIRED`; when every instance fails the call raises
                `ExportError`. Off by default because it costs a second
                parse and a full decode per instance: about twice the time
                of a default (JPEG 2000) export, and little more for an
                uncompressed one. Each worker holds one more decoded array
                while it checks.

        Returns:
            ExportSummary: What reached disk and what did not; empty when
                nothing was attempted.

        Raises:
            TypeError: `patient_ids` or `subset` is of a type the selection
                does not take, before anything is flushed or written.
            ValueError: `subset` is a DataFrame carrying none of
                SOPInstanceUID, SeriesInstanceUID, StudyInstanceUID and
                PatientID.
            ExportError: Every planned instance failed (nothing written and
                at least one failure), raised last, after the audit rows
                and the `EXPORT` row are written. A partial export and an
                empty plan return a summary instead.
        """
        # One helper for every door that selects patients, so
        # `patient_ids` means the same thing whichever format was named
        # and whichever door was used: only `None` is every
        # patient, an iterator is materialised before the walk can eat
        # it, and a bare `str`, a bytes-like value, a non-iterable or a
        # non-`str` element is refused. The wfdb door and
        # `get_cohort_report` call the same function; a door that reads
        # the argument its own way would disagree with the others.
        #
        # First statement in the method, and before the `save(sync=True)`
        # below on purpose: a refusal that arrives after the flush has
        # already moved the session for an export that will not happen.
        selection = select_patient_ids(patient_ids, self.store.patients)
        target_ids = selection.ids
        if target_ids is None:
            target_ids = frozenset(p.patient_id for p in self.store.patients)

        # The subset, read whole -- shape, query and count -- before the
        # pre-export scan: read after it, a subset refused for its shape
        # would arrive once `check_burned_in=True` had re-recorded every
        # status, and an export that never ran would have moved the
        # session.
        subset_selection = self._resolve_subset(subset)
        allowed_uids = subset_selection.uids

        # None means "no safety filter"; an empty set means "the scan ran and
        # found nothing". The two are not the same and the walk treats them
        # differently, so they must not collapse into one falsy value.
        identifying_uids = (self._scan_before_export()
                            if check_burned_in else None)

        get_logger().info("Exporting session to: %s", folder)
        print("Preparing export plan...")

        # Flush before the walk: a large export loads pixels back in, and
        # holding both the pending edits and the frames being written has
        # been enough to run a redaction session out of memory.
        #
        # `sync=True`, and it must stay. A plain `save()` enqueues on the
        # persistence worker and returns, so it would flush *concurrently
        # with* the walk: `release_memory()` frees an instance only once
        # the background save has attached its `_pixel_loader`, so which
        # instances were swept would depend on which thread got there
        # first, and one whose `pixel_array` was assigned directly with
        # no Rows/Columns would reload as an empty image. `audit()` and
        # `redact()` drain on entry for the same reason. The price is
        # `save()`'s documented one: a wedged worker wedges the export
        # instead of racing it.
        print("Saving pending changes to free memory...")
        self.save(sync=True)
        self._release_memory(show_progress)

        tasks, patient_count, withheld = self._build_export_plan(
            _ExportOptions(folder, identifying_uids, allowed_uids,
                           use_compression, verify_readback),
            target_ids)

        # Before the empty-plan branch, so an export that withheld
        # everything still leaves one row per instance it held back,
        # rather than reading as an empty plan under PASS.
        _audit_withheld_instances(self.store_backend, folder, withheld)

        # The ids that selected nobody, counted and never named,
        # and the subset's values that name nothing, the same way.
        # Here and not at the selections above: `_scan_before_export` can
        # still refuse after them, and a row saying an export selected
        # short, for an export that never ran, is a fabrication. Before
        # both empty-plan branches and
        # outside the `if tasks:` below, so an export whose every id was
        # unknown -- which plans nothing -- still says why. `WARNING`, so
        # the report grades `REVIEW_REQUIRED`: a short export under PASS
        # would read as a complete one.
        if selection.unmatched:
            sentence = unmatched_patient_ids_sentence(selection)
            get_logger().warning(sentence)
            self.store_backend.log_audit(
                action_type="WARNING", entity_uid=folder,
                details=f"DICOM export to {folder}: {sentence}")
        if subset_selection.unmatched:
            sentence = unmatched_subset_uids_sentence(subset_selection)
            get_logger().warning(sentence)
            self.store_backend.log_audit(
                action_type="WARNING", entity_uid=folder,
                details=f"DICOM export to {folder}: {sentence}")

        # Over the instances this export will write, after the pre-export
        # scan -- which re-records every status under the policy in force,
        # so `check_burned_in=True` never draws it -- and only when there
        # are some. The plan holds instances; the notice reads
        # their patient and study too.
        if tasks:
            planned = {id(task.instance) for task in tasks}
            self._report_statuses_under_another_policy(
                [(patient, study, instance)
                 for patient in self.store.patients
                 for study in patient.studies
                 for series in study.series
                 for instance in series.instances
                 if id(instance) in planned],
                folder, "DICOM")

        if not tasks and withheld:
            get_logger().warning(
                "No instances exported: all %d were withheld by the "
                "pre-export scan.", len(withheld))
            # Counted, unlike a true empty plan: the caller asked for
            # these instances and none was written, which is exactly what
            # "0 of K requested" says. A zero here is a fact about this
            # export, not a stand-in for "not answered".
            self._last_export_written = 0
            self._last_export_requested = len(withheld)
            self.store_backend.log_audit(
                action_type="EXPORT",
                entity_uid=folder,
                details=(f"DICOM export to {folder}: wrote 0 of "
                         f"{len(withheld)} requested instances; all "
                         f"{len(withheld)} were withheld by the pre-export "
                         f"scan (check_burned_in=True)."))
            # Returned, not `ExportError`: nothing was attempted and
            # nothing failed. The rows above grade the run and the
            # counters say none of it was written.
            return ExportSummary()

        if not tasks:
            get_logger().warning("No instances found to export.")
            # Still an export run, so it still writes its row: a
            # subset that matched nothing is a fact about this run the
            # audit trail has to carry, and the report's export boundary
            # keys on the row's existence, not on files.
            self.store_backend.log_audit(
                action_type="EXPORT",
                entity_uid=folder,
                details=(f"DICOM export to {folder}: wrote 0 of 0 planned "
                         f"instances; nothing matched the export plan."))
            # Zero of zero, and deliberately not an `ExportError`: a
            # plan that matched nothing is not an export that failed,
            # and the row above already says so. The caller still gets a
            # summary rather than `None`, so `.written` and `.failures`
            # are askable on every return path.
            return ExportSummary()

        print(f"Exporting {len(tasks)} images from {patient_count} patients...")
        summary = self._run_export_batch(tasks, show_progress,
                                         self.store_backend)

        self._report_export_collisions(tasks, summary.written_uids)

        # After the batch, not before it. The disclosure is a statement
        # about files a recipient holds, so it has to be made from what
        # was written rather than from what was planned.
        if check_reversibility:
            self._report_recoverable_identities(tasks, summary.written_uids)

        # Recorded for `generate_report`, so the report counts what was
        # written, not only the object graph.
        self._last_export_written = summary.written
        # Withheld instances were requested and not written, so they are
        # in the denominator: the count answers for the cohort, not for
        # the plan.
        self._last_export_requested = len(tasks) + len(withheld)

        # The run itself is an audited action, not only its failures.
        # One row per run rather than per instance -- the
        # per-instance record is the output tree itself; this row says
        # how much of the plan reached it, and its existence is what
        # `generate_report` keys the export boundary on, durably
        # across a session reopened on this store.
        #
        # The withheld clause is appended only when something was
        # withheld, so a run that withheld nothing writes the row
        # without it, byte for byte.
        withheld_clause = (f"; {len(withheld)} more withheld by the "
                           f"pre-export scan (check_burned_in=True)"
                           if withheld else "")
        self.store_backend.log_audit(
            action_type="EXPORT",
            entity_uid=folder,
            details=(f"DICOM export to {folder}: wrote {summary.written} "
                     f"of {len(tasks)} planned instances from "
                     f"{patient_count} patients{withheld_clause}."))
        print("Done.")

        # Last, after all five records -- the collision report, the
        # recoverable-identity disclosure, the delivery counters, the
        # `EXPORT` row and `Done.` -- and in that order for the reason
        # `_apply_redaction_rules` raises `RedactionError` last: a caller
        # who catches this still holds a correct graph, a complete audit
        # trail and a report that grades REVIEW_REQUIRED. Raising earlier
        # would trade all of that for the exception.
        #
        # `written == 0 **and** failures`, never `written == 0` alone: an
        # empty plan reaching here would otherwise raise, and zero of
        # zero is not a failure. A *partial* export does not raise
        # either -- two files out of three is a real result, and raising
        # would discard the summary naming which two.
        if summary.written == 0 and summary.failures:
            raise ExportError(summary.failures, len(tasks), folder)

        return summary

    def _report_recoverable_identities(self, tasks, written_uids) -> int:
        """Report delivered instances whose file still carries its originals.

        An instance carrying an Encrypted Attributes Sequence (0400,0500) from
        `lock_identities()` is recoverable by any holder of the key. When any
        delivered instance carries one, logs a WARNING and writes one
        `REVERSIBLE_EXPORT` audit row; a token in the layout releases before
        1.0 wrote adds a `WARNING` row. An instance counts as delivered when a
        worker wrote it or its planned file exists on disk.

        Args:
            tasks (list): The export plan: each task's instance and output
                path.
            written_uids (Iterable[str]): The UID of every instance the workers
                wrote.

        Returns:
            int: How many delivered instances carry recoverable identities; 0,
                with no audit row, when none does.
        """
        # Keyed on the data rather than on `self.reversibility_service`: a
        # store can hold tokens embedded by an earlier session that never
        # enabled the service in this one.
        #
        # Delivered means a file is there, not that a worker said so. The union
        # with the files on disk covers a worker that renamed its file and then
        # died before answering, and a re-identifiable file left by an earlier
        # export into the same folder. Both would otherwise be under-claims,
        # which get a re-identifiable file treated as safe; an over-claim costs
        # only a disclosure. Only instances not already known to be written are
        # stat-ed, so a clean export does no filesystem work here.
        delivered = set(written_uids)
        delivered |= {task.instance.sop_instance_uid for task in tasks
                      if task.instance.sop_instance_uid not in delivered
                      and os.path.exists(task.output_path)}
        # A set, like `delivered`: the numerator and the denominator
        # must be counted over the same collection, or a duplicate SOP
        # Instance UID renders "2 of 1 exported instances". One UID is
        # one file, whatever wrote it.
        affected = {
            task.instance.sop_instance_uid
            for task in tasks
            if (task.instance.sop_instance_uid in delivered
                and task.instance.sequences.get(
                    ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ) is not None
                and task.instance.sequences[
                    ReversibilityService.TAG_ENCRYPTED_ATTRS_SEQ].items)
        }
        if not affected:
            return 0

        detail = (
            f"{len(affected)} of {len(delivered)} exported instances carry "
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
                entity_uid=(next(iter(affected)) if len(affected) == 1
                            else "MULTIPLE"),
                details=detail)

        # A token in the layout releases before 1.0 wrote is
        # written as the graph holds it -- the file 0.9.x would have
        # written, which 0.9.x recovers with its key -- and counted above,
        # because it is re-identifiable all the same. 1.x does not read
        # it, so the export says so: a `WARNING` row, which grades the
        # report REVIEW_REQUIRED. Passed through rather than refused or
        # dropped: refusing would leave a 0.9.x
        # store that holds a lock with no export at all, and dropping
        # the item would lose the one way back to the identity silently.
        # A set over `affected`, for `affected`'s reason.
        earlier = {
            task.instance.sop_instance_uid for task in tasks
            if task.instance.sop_instance_uid in affected
            and ReversibilityService.holds_an_earlier_layout_token(task.instance)}
        if earlier:
            sentence = (
                f"{len(earlier)} of {len(delivered)} exported instances carry an "
                "identity token in the layout Isocenter wrote before 1.0, "
                "which 1.x cannot recover; Isocenter 0.9.x recovers it with "
                "its key.")
            get_logger().warning(sentence)
            if getattr(self, "store_backend", None) is not None:
                self.store_backend.log_audit(
                    action_type="WARNING",
                    entity_uid=(next(iter(earlier)) if len(earlier) == 1
                                else "MULTIPLE"),
                    details=sentence)
        return len(affected)

    def _report_export_collisions(self, tasks, written_uids) -> int:
        """Audit every output path that more than one instance was written to.

        Files are named by SOP Instance UID, so two delivered instances sharing
        a path leave one file. Each such path gets one `ERROR` audit row, which
        grades the run `REVIEW_REQUIRED`. Grouped by output path, not by UID; a
        path every write to failed is not reported, since its failures carry
        their own rows.

        Args:
            tasks (list): The export plan: each task's instance and output
                path.
            written_uids (Iterable[str]): The UID of every instance the workers
                wrote.

        Returns:
            int: How many colliding paths were reported.
        """
        # `ERROR`, not `DATA_LOSS`: the end state is a requested instance that
        # is not in the folder, which is what `_report_export_failures` files
        # `ERROR` for. A `DATA_LOSS` row is graded by `loss_scope`, and
        # `STANDARD` leaves the run at PASS, while nothing here can say whether
        # the colliding instances were identical copies or two images wrongly
        # sharing a UID.
        by_path = {}
        for task in tasks:
            by_path.setdefault(task.output_path, []).append(task)

        written = set(written_uids)
        collisions = 0
        for path, group in by_path.items():
            if len(group) < 2:
                continue
            uid = group[0].instance.sop_instance_uid
            if uid not in written and not os.path.exists(path):
                continue
            # Flattened and pipe-escaped for the same reason as
            # `_report_export_failures`: the detail is rendered straight
            # into a markdown table row. No `path`: it is
            # `<folder>/Subject_<Patient ID>/...`, and the UID already
            # names the file.
            detail = " ".join(
                f"{len(group)} exported instances share SOP Instance UID "
                f"{uid} and were written to one file: each "
                f"successful write overwrote the previous one, and the "
                f"folder holds one file for all {len(group)} of "
                f"them.".split()).replace("|", "\\|")
            get_logger().error("%s: %s", uid, detail)
            collisions += 1
            if getattr(self, "store_backend", None) is not None:
                # `log_audit`, not `log_audit_batch` -- see the note in
                # `_report_export_losses`.
                self.store_backend.log_audit(
                    action_type="ERROR", entity_uid=uid, details=detail)
        return collisions

    def _scan_before_export(self) -> Set[str]:
        """Scan for PHI and report what it found, before anything is written.

        Returns:
            Set[str]: The UID of every entity carrying an identifier, at any
                level. An instance is withheld when its own UID or any of its
                parents' is here.
        """
        get_logger().info("Performing pre-export safety scan...")
        findings = self.audit()
        if not findings:
            return set()

        _report_phi_findings(findings)
        get_logger().warning(
            "Safe export: identifiers detected. Exporting only the instances "
            "that carry none, and skipping the rest.")
        # `''` is a uid: an empty Patient ID (see `_record_scan_results`).
        return {f.entity_uid for f in findings if f.entity_uid is not None}

    def _resolve_subset(self, subset) -> _SubsetSelection:
        """Turn a subset argument into the UIDs allowed through the walk.

        Accepts a pandas query string, a DataFrame, or any other iterable of
        UIDs at any level, read by `normalize_id_filter` as `patient_ids` is. A
        value is unmatched when it names nothing in the session at any level,
        itself or anything it stands for (`_subset_names`). A query never
        counts as unmatched. Logs and writes nothing; `_export_dicom` reports
        the count once the export is certain to run.

        Args:
            subset (Union[str, pd.DataFrame, Iterable[str]], optional): The
                caller's `subset`.

        Returns:
            _SubsetSelection: `uids` (None when no subset was given, an empty
                set when it matched nothing), how many values were read, and
                the positions of the unmatched ones.

        Raises:
            TypeError: For a bytes-like value, a non-iterable, or an element
                that is not a `str` (a DataFrame column's too), naming the
                position.
            ValueError: If a query string does not run against the cohort
                report, or a DataFrame has none of `_SUBSET_FRAME_COLUMNS`.
        """
        # A non-iterable is refused because, ignored, a mistyped filter would
        # become a full export. A query error is not treated as a query that
        # matched nothing. Unmatched is judged against the whole graph, not
        # this export's `patient_ids`, as `select_patient_ids` counts.
        if subset is None:
            return _SubsetSelection(None, 0, ())

        # pandas is imported only on the paths that need it, to keep it
        # off `import isocenter`'s cost.
        import pandas as pd

        if isinstance(subset, str):
            report = self.get_cohort_report(expand_metadata=True)
            try:
                frame = report.query(subset)
            except Exception as exc:
                raise ValueError(
                    f"subset query {subset!r} could not be run against the "
                    f"cohort report: {exc}") from exc
            subset = _uids_from_frame(frame)
        elif isinstance(subset, pd.DataFrame):
            subset = _uids_from_frame(subset)
        # The one reading of a list of UIDs, the one `patient_ids` has. A
        # str was a query above, so the helper's bare-str refusal is never
        # reached from here.
        values = normalize_id_filter(
            subset, "subset", kind="UID",
            takes="None, a query str, a DataFrame, or an iterable of UIDs")

        held, names_of = self._subset_names()
        allowed, unmatched = set(), []
        for position, value in enumerate(values, start=1):
            names = names_of(value)
            allowed |= names
            if held.isdisjoint(names):
                unmatched.append(position)
        return _SubsetSelection(allowed, len(values), tuple(unmatched))

    def _subset_names(self):
        """What the session holds, and what one subset value names.

        A value names itself; the UID this store replaces it with; and the
        current SOP Instance UID of an instance whose recorded source UID
        (`SOURCE_SOP_UID_ATTR`) it is, or whose source's replacement it is. So
        a subset taken before `anonymize()` or `redact()` still selects. Reads
        the project secret, if any, once.

        Returns:
            tuple: `(held, names_of)`. `held` is every Patient ID, Study,
                Series and SOP Instance UID in the graph; `names_of(value)` is
                the set of UIDs the value may stand for. A value is unknown
                only when none of them is in `held`.
        """
        # A Patient ID's replacement names nothing, so adding it is harmless,
        # and a source Patient ID is counted as unknown, because the pseudonym
        # is keyed, not a UID replacement. Only the first SOP UID move is
        # recorded (`_take_sop_uid`), so a UID taken between a first redaction
        # and a `force=True` second one still names nothing, and is counted.
        # The source map widens what a value names, never `held`: a UID no
        # instance here was ever ingested under stays unknown.
        secret = self.store_backend._project_secret_if_present()
        if secret:
            from .privacy import _replacement_uid_for  # pylint: disable=import-outside-toplevel
            replacement = lambda uid: _replacement_uid_for(uid, secret)  # noqa: E731
        else:
            replacement = None
        held, moved = set(), {}
        for patient in self.store.patients:
            held.add(patient.patient_id)
            for study in patient.studies:
                held.add(study.study_instance_uid)
                for series in study.series:
                    held.add(series.series_instance_uid)
                    for instance in series.instances:
                        current = instance.sop_instance_uid
                        held.add(current)
                        source = instance.attributes.get(SOURCE_SOP_UID_ATTR)
                        if not source or source == current:
                            continue
                        # A set per key: a hand-built graph can give two
                        # instances one source, and both are named.
                        moved.setdefault(source, set()).add(current)
                        if replacement is not None:
                            moved.setdefault(replacement(source),
                                             set()).add(current)

        def names_of(value):
            """The UIDs one subset value may stand for.

            Args:
                value (str): A subset value.

            Returns:
                set: The value, its replacement, and the current SOP
                    Instance UID of each instance moved off either.
            """
            names = {value}
            if replacement is not None and value:
                names.add(replacement(value))
            for name in tuple(names):
                names |= moved.get(name, set())
            return names

        return held, names_of

    #: The substring the export notice is pinned by.
    _OTHER_POLICY_NOTICE = ("recorded under a policy other than the one "
                            "in force")

    def _accepted_policy_fingerprints(self, in_force) -> Set[str]:
        """The fingerprints a status may be recorded under and still speak for
        what `export()` writes.

        The one answer for both the other-policy notice, which is silent
        exactly on these, and the de-identification markers, which are written
        exactly on these.

        Args:
            in_force (ScanPolicy): The policy in force, as the caller read it.

        Returns:
            Set[str]: Its fingerprint and that of every policy this session
                audited under.
        """
        # Fingerprints, never bases: a scaffold and the bare floor are one
        # policy under two labels.
        return set(self._scanned_policies) | {in_force.fingerprint}

    @staticmethod
    def _deid_marker_policy(patient, study, instance, accepted) -> Optional[ScanPolicy]:
        """The policy an instance's file may say it was de-identified under.

        Args:
            patient (Patient): The instance's patient.
            study (Study): The instance's study.
            instance (Instance): The instance.
            accepted (Set[str]): From `_accepted_policy_fingerprints`.

        Returns:
            Optional[ScanPolicy]: The instance's recorded policy when the
                patient, the study and the instance each read REMEDIATED or
                CLEARED, all under one fingerprint in `accepted`; otherwise
                None.
        """
        # The condition is a fact the graph already holds, so every way the
        # file can differ from the pass moves a status off it: never audited or
        # edited after the pass reads UNSCANNED; a finding declined, not handed
        # in, or a Series finding left open reads IDENTIFIED; another policy
        # gives a fingerprint outside `accepted`, or three that disagree; a
        # store written before 1.0 has no policy.
        #
        # All three are read, because each is written into the file: the
        # instance alone would miss a patient whose name was set back after the
        # pass. Nested items are not read: the instance's status carries their
        # outcome. A stand-in that is no `TrackedEntity` records no status, so
        # it gets none. A declared burned-in annotation is the writer's to
        # read, from the file itself. The recorded policy is returned, not the
        # one in force, because its base is what the scan ran under.
        policies = []
        for entity in (patient, study, instance):
            if not isinstance(entity, entities.TrackedEntity):
                return None
            # One read of the pair, as the save thread reads it.
            status, policy = entity._phi_status_record()
            if status not in (PhiStatus.REMEDIATED, PhiStatus.CLEARED) \
                    or policy is None:
                return None
            policies.append(policy)
        if len({policy.fingerprint for policy in policies}) != 1 \
                or policies[-1].fingerprint not in accepted:
            return None
        return policies[-1]

    def _deid_markers_planner(self):
        """Build the de-identification marker plan for one export.

        The plan is built once per export, because the policy in force is
        hashed to read it. A rule of any action on a marker tag means that tag
        is not stamped (KEEP over a source `NO` stays `NO`).

        Returns:
            Callable: `plan(patient, study, instance, stamps)`, returning the
                `DeidMarkers` the instance's file carries, or None.
        """
        accepted = self._accepted_policy_fingerprints(
            self.configuration._scan_policy())
        ruled = set(self.configuration.phi_tags or {})

        def plan(patient, study, instance, stamps):
            """The de-identification markers one instance's file carries.

            Args:
                patient (Patient): The instance's patient.
                study (Study): The instance's study.
                instance (Instance): The instance.
                stamps (dict): The values the export stamps on the file.

            Returns:
                Optional[DeidMarkers]: The markers, or None when the
                    instance's statuses do not qualify.
            """
            policy = self._deid_marker_policy(patient, study, instance, accepted)
            if policy is None:
                return None
            method = None
            if _DEID_METHOD not in ruled:
                method = _deid_method_value(policy, __version__)
            temporal = None
            if _TEMPORAL_MODIFIED not in ruled:
                temporal = _longitudinal_temporal_marker(study, instance, stamps)
            return DeidMarkers(identity_removed=_IDENTITY_REMOVED not in ruled,
                               method_value=method, temporal=temporal)
        return plan

    def _report_statuses_under_another_policy(self, triples, folder, fmt):
        """Warn when an export writes statuses recorded under another policy.

        Writes one `WARNING` audit row and one log line per call, which grades
        the report `REVIEW_REQUIRED`; nothing is reinterpreted. An instance
        counts once when its patient, study or itself reads a status other than
        UNSCANNED with no policy (written before 1.0, or remediated from
        findings that are not a whole `audit()` report) or with a fingerprint
        outside `_accepted_policy_fingerprints`.

        Args:
            triples (list): `(patient, study, instance)` for every instance the
                export will attempt. Empty says nothing.
            folder (str): The export's folder, the row's `entity_uid`.
            fmt (str): `"DICOM"` or `"WFDB"`, the lead word of the row.
        """
        # A store remediated under one policy and reopened under another reads
        # REMEDIATED and exports what the first policy left; this says so where
        # the harm happens. Nested items are not read, because the instance
        # carries their outcome.
        in_force = self.configuration._scan_policy()
        accepted = self._accepted_policy_fingerprints(in_force)
        others = {}
        legacy = written = 0
        for triple in triples:
            policies = set()
            for entity in triple:
                # A stand-in built in user code (a mock graph) records no
                # status, so it has none to disagree with.
                if not isinstance(entity, entities.TrackedEntity):
                    continue
                # One read of the pair, as the save thread reads it.
                status, policy = entity._phi_status_record()
                if status is PhiStatus.UNSCANNED:
                    continue
                if policy is None or policy.fingerprint not in accepted:
                    policies.add(policy)
            if not policies:
                continue
            written += 1
            if None in policies:
                legacy += 1
            for policy in policies - {None}:
                others.setdefault(policy.fingerprint, set()).add(policy.base)
        if not written:
            return
        named = [f"{' or '.join(sorted(bases))} ({fingerprint[:15]})"
                 for fingerprint, bases in sorted(others.items())]
        if legacy:
            # True of all three routes to None: a store written before
            # 1.0, a findings list with no scan behind
            # it, and a report that scan did run for but that was rebuilt
            # or narrowed, so no longer speaks for it.
            named.append(f"{legacy} with no recorded policy (written "
                         f"before 1.0, or remediated from findings that are "
                         f"not a whole audit() report)")
        detail = (f"{fmt} export to {folder} writes {written} instance(s) "
                  f"whose PHI status was {self._OTHER_POLICY_NOTICE} "
                  f"({in_force.base}, {in_force.fingerprint[:15]}): "
                  f"{'; '.join(named)}. A status says what the scan it came "
                  f"from concluded; export() writes the graph as it holds "
                  f"it. To apply the policy in force, run audit() and then "
                  f"anonymize() (#555).")
        detail = " ".join(detail.split()).replace("|", "\\|")
        get_logger().warning(detail)
        if self.store_backend is not None:
            # `log_audit`, one call, and the action word spelled at the
            # call: the frozen-vocabulary pin reads the keyword at the site.
            self.store_backend.log_audit(action_type="WARNING",
                                         entity_uid=folder, details=detail)

    def _build_export_plan(self, options: '_ExportOptions', target_ids):
        """Walk the store and build one ExportContext per instance to write.

        Writes nothing. Applies the patient, subset and burned-in filters in
        one place, so the count is known before the parallel batch starts.

        Args:
            options (_ExportOptions): The export's parsed options.
            target_ids (FrozenSet[str]): The Patient IDs to export.

        Returns:
            tuple: `(contexts, patients_visited, withheld)`, where `withheld`
                lists `(sop_instance_uid, level)` for every instance the subset
                selected and the pre-export scan held back. An instance outside
                the subset is in neither list.
        """
        tasks = []
        withheld = []
        patient_count = 0

        drop_foreign = self._foreign_icon_gate()
        # Here, in the parent, and never in the graph or the worker:
        # the statuses it rests on are not on a process worker's copy, and
        # a stamp in the graph would be a second answer that goes stale.
        deid_markers = self._deid_markers_planner()

        for patient in self.store.patients:
            if patient.patient_id not in target_ids:
                continue
            patient_count += 1

            for study in patient.studies:
                for series in study.series:
                    # Hybrid naming: shared with every other export format
                    # (see `export_folder_names` in io_handlers.py) so trees
                    # stay co-located.
                    series_path = os.path.join(
                        options.folder,
                        *export_folder_names(patient, study, series))
                    # The one stamping helper both write doors call:
                    # `write_tree` gets exactly these.
                    patient_attrs, study_attrs, series_attrs = \
                        export_stamp_attributes(patient, study, series)
                    # What the worker merges over each instance's own,
                    # in its order, for the temporal marker's walk.
                    stamps = {**patient_attrs, **study_attrs, **series_attrs}
                    zones = self._redaction_zones_for(series)

                    for instance in series.instances:
                        why = _why_excluded(
                            options, patient, study, series, instance)
                        if why not in (None, OUTSIDE_THE_SUBSET):
                            withheld.append((instance.sop_instance_uid, why))
                        if why is not None:
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
                            redaction_zones=zones,
                            drop_foreign_icons=drop_foreign,
                            verify_readback=options.verify_readback,
                            deid_markers=deid_markers(patient, study,
                                                      instance, stamps)))

        return tasks, patient_count, withheld

    def _foreign_icon_gate(self) -> bool:
        """Whether to drop every nested icon that is not its carrier's own.

        One boolean for the whole run, computed before the walk over the whole
        store: True when any instance carries a redaction attestation or a
        zones rule matches a series in the store. Each carrier's own depth-1
        icon is decided per instance in the worker.

        Returns:
            bool: True to drop foreign icons.
        """
        # Store-wide and not per instance, because an icon under Referenced
        # Image Sequence is a thumbnail of a different SOP instance and
        # redaction's `regenerate_uid()` makes following the reference fail
        # open. Over `self.store.patients` rather than the export's
        # `target_ids` or subset: a subset that excludes the redacted instances
        # must not turn the gate off for the ones it keeps. A rule for a
        # scanner nobody has is not counted: counting it would strip every icon
        # from every file.
        every_series = [series for patient in self.store.patients
                        for study in patient.studies
                        for series in study.series]
        return redaction_in_effect(
            instance for series in every_series
            for instance in series.instances) or any(
                self._redaction_zones_for(series) for series in every_series)

    def _redaction_zones_for(self, series) -> list:
        """The configured pixel-redaction zones for a series' scanner.

        Every matching rule's zones, exact or `"*"`, in rule order, each parsed
        to a 4-tuple with the matcher and parser `redact()` uses. Logs nothing
        for an invalid zone.

        Args:
            series (Series): The series whose equipment's serial is matched.

        Returns:
            list: `(y1, y2, x1, x2)` per zone; empty when no rule matches.
        """
        # Not `Configuration.get_rule`, which is exact and first-match and
        # returns raw zones: through it a `"*"` rule or a second rule on the
        # same serial would export unredacted pixels, and a `{"roi": ...}` zone
        # would fail the export. A series with no equipment or no serial is
        # handed a `None` rather than answered here, so `rule_applies_to` stays
        # the one answer.
        serial = (series.equipment.device_serial_number
                  if series.equipment else None)
        return [roi
                for rule in rules_matching(self.configuration.rules, serial)
                for roi in zone_rois(rule.get("redaction_zones", []))]

    @staticmethod
    def _run_export_batch(tasks, show_progress,
                          store_backend=None) -> ExportSummary:
        """Run the export in worker processes and report the outcome.

        Uses `export_batch`'s own pool rather than `self._executor`, recycling
        each worker every 25 tasks, and always runs in processes;
        `ISOCENTER_FORCE_THREADS` is reported, not obeyed. Export losses and
        failed writes come back from the workers and are audited here, in the
        parent. Logs a WARNING when any instance was not written.

        Args:
            tasks (list): The `ExportContext` per instance.
            show_progress (bool): Whether to draw the progress bar.
            store_backend (SqliteStore, optional): Where the audit rows go.

        Returns:
            ExportSummary: What reached disk and what did not.

        Raises:
            Exception: Whatever `export_batch` raised, after logging it.
        """
        # Processes are a decision: workers are recycled so memory leaked by
        # the imaging C libraries is reclaimed, and only `multiprocessing.Pool`
        # recycles on 3.12 (`ProcessPoolExecutor`'s `max_tasks_per_child`
        # deadlocks `map` at the first replacement there). Asking for
        # `maxtasksperchild` rules threads out in `_resolve_execution_choice`,
        # so this runs in processes even on a free-threaded build. The cost is
        # pickling each `ExportContext` across a pipe. `store_backend` is
        # passed explicitly because this is a static method and the handle
        # cannot cross into a worker.
        try:
            summary = DicomExporter.export_batch(
                tasks,
                show_progress=show_progress,
                total=len(tasks),
                maxtasksperchild=25,
                disable_gc=True,
                store_backend=store_backend)
        except Exception as exc:
            get_logger().error("Export Failed! Error: %s", describe_exception(exc))
            raise
        finally:
            gc.collect()

        if summary.written < len(tasks):
            # A partial failure must be said here, not only counted: the
            # per-file errors are in the audit log, and this is the
            # summary that says to go and read them.
            get_logger().warning(
                "Export finished with failures: %d of %d instances exported. "
                "See the audit log for per-instance errors.",
                summary.written, len(tasks))
            print(f"Export finished with failures: "
                  f"{summary.written}/{len(tasks)} instances exported.")
        else:
            get_logger().info("Export complete.")

        return summary

    def export_dataframe(
            self,
            output_path: str = "export_metadata.csv",
            expand_metadata: bool = False,
            patient_ids: Optional[List[str]] = None):
        """
        Write the cohort report to CSV or Parquet, and return it.

        The format is chosen from the extension: ``.parquet`` writes
        Parquet, anything else writes CSV.

        It reports the session's in-memory graph and does not `save()`
        first: pending edits are not committed as a side effect.

        Args:
            output_path (str): The output file path (ends with .csv or .parquet).
            expand_metadata (bool): If True, includes all DICOM attributes as columns.
            patient_ids (Iterable[str], optional): Restrict the export to
                these Patient IDs, read by `get_cohort_report()`, which
                this calls: ``None`` means every patient in the session,
                and an ID no patient holds is counted in a `WARNING` log
                line.

        Returns:
            pd.DataFrame: The frame that was written.

        Raises:
            ImportError: If pandas (or, for Parquet, a Parquet engine) is
                not installed.
            TypeError: For a `patient_ids` `get_cohort_report()` refuses
                (a bare `str`, bytes-like, not iterable, or a non-`str`
                element), before the directory is created or any file
                is written.
            Exception: Whatever the Parquet write raised, after logging it.
        """
        try:
            # Guarded here purely for the message. `get_cohort_report`
            # imports pandas unguarded a moment later, so without this
            # the caller gets a bare ModuleNotFoundError naming neither
            # the extra to install nor the Parquet engine they will need
            # next.
            import pandas  # noqa: F401  pylint: disable=unused-import
        except ImportError as e:
            get_logger().error("export_dataframe requires 'pandas' installed.")
            raise ImportError(
                "Please install pandas to use this feature: "
                "pip install pandas pyarrow") from e

        # Before `makedirs` below: a refused `patient_ids` must
        # leave no directory and no file behind.
        df = self.get_cohort_report(
            expand_metadata=expand_metadata, patient_ids=patient_ids)

        # Create the destination directory. pandas raises a bare
        # "Cannot save file into a non-existent directory" otherwise,
        # which names the wrong problem for a caller passing a nested
        # report path.
        parent = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(parent, exist_ok=True)

        get_logger().info(f"Writing {len(df)} rows to {output_path}...")

        if output_path.endswith(".parquet"):
            try:
                # Requires pandas plus pyarrow or fastparquet
                df.to_parquet(output_path, index=False)
            except ImportError as e:
                get_logger().error(
                    "Parquet engine (pyarrow or fastparquet) missing.")
                raise ImportError(
                    "Please install a Parquet engine to write .parquet: "
                    "pip install pyarrow") from e
            except Exception as e:
                get_logger().error(f"Failed to export parquet: {describe_exception(e)}")
                raise
        else:
            df.to_csv(output_path, index=False)

        print(f"Exported metadata to {output_path}")
        return df

    # =========================================================================
    # INTERNAL HELPERS
    # =========================================================================

    def _rehydrate_findings(self, findings):
        """Point findings at the live objects in `self.store`, in place.

        Args:
            findings (List[PhiFinding]): Findings unpickled from workers.
        """
        patient_map = {p.patient_id: p for p in self.store.patients}
        study_map = {}
        series_map = {}
        instance_map = {}

        for p in self.store.patients:
            for s in p.studies:
                study_map[s.study_instance_uid] = s
                for se in s.series:
                    # A Series is a finding entity: it owns `0020,000e`.
                    series_map[se.series_instance_uid] = se
                    for i in se.instances:
                        instance_map[i.sop_instance_uid] = i

        for f in findings:
            if f.entity_type == "Patient":
                if f.entity_uid in patient_map:
                    f.entity = patient_map[f.entity_uid]
            elif f.entity_type == "Study":
                if f.entity_uid in study_map:
                    f.entity = study_map[f.entity_uid]
            elif f.entity_type == "Series":
                if f.entity_uid in series_map:
                    f.entity = series_map[f.entity_uid]
            elif f.entity_type == "Instance":
                f.entity = self._live_target(instance_map.get(f.entity_uid), f)

    def _instances_by_uid(self) -> dict:
        """Map each UID to the instances of the graph it may address.

        Each instance is filed under its SOP Instance UID and, when `redact()`
        replaced that UID, under the source UID (`SOURCE_SOP_UID_ATTR`),
        because a report raised before the redaction names it.

        Returns:
            dict: `uid -> [Instance, ...]`; a list, because a hand-built graph
                can give two instances one UID, and an ambiguous address must
                stay visible as one.
        """
        # Only the first redaction's UID is recorded (`regenerate_uid`), so a
        # report taken between a first and a `force=True` second one still
        # names nothing.
        by_uid = {}
        for patient in self.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for inst in series.instances:
                        by_uid.setdefault(inst.sop_instance_uid, []).append(inst)
                        source = inst.attributes.get(SOURCE_SOP_UID_ATTR)
                        if source and source != inst.sop_instance_uid:
                            by_uid.setdefault(source, []).append(inst)
        return by_uid

    def _finding_holders(self, findings, owners) -> dict:
        """The live patient holding each finding's entity.

        Read before the pass can replace an ID. What the service checks a
        Patient ID REPLACE and a SHIFT's seed against, so a value that does not
        belong to the holder is not written.

        Args:
            findings (List[PhiFinding]): The findings of the pass.
            owners (dict): From `_nested_finding_owners`.

        Returns:
            dict: `id(entity) -> (patient_id, jitter_scheme)` for every
                patient, study, series and instance, and for an item no owner
                names.
        """
        # A resolved report can reach a patient other than the one it was
        # raised for (another store, another site's IDs, a legacy store's
        # scheme). A nested item is found through `owners` by the service; an
        # item no owner names is filed here by walking the item trees, and only
        # when such a finding is present.
        holders = {}
        for patient in self.store.patients:
            mine = (patient.patient_id, patient._jitter_scheme)
            holders[id(patient)] = mine
            for study in patient.studies:
                holders[id(study)] = mine
                for series in study.series:
                    holders[id(series)] = mine
                    for inst in series.instances:
                        holders[id(inst)] = mine
        loose = {id(f.entity) for f in findings if f.entity is not None
                 and id(f.entity) not in holders and id(f.entity) not in owners}
        if loose:
            for patient in self.store.patients:
                for study in patient.studies:
                    for series in study.series:
                        for inst in series.instances:
                            for item, _ in iter_item_tree(inst):
                                if id(item) in loose:
                                    holders[id(item)] = holders[id(inst)]
        return holders

    def _live_findings(self, findings, secret, by_uid) -> tuple:
        """Resolve each finding against the live graph, and name those a pass
        already settled.

        For each finding with a proposal and an entity, in order: at its
        address (the object at `entity_uid` and `entity_path` is the entity
        itself), it is handed over unchanged; live but not at its address, it
        is handed over unchanged and a REMOVE on it declines; dead, it is
        replaced by a copy bound to the one object its address names. Where the
        address reaches nothing because a pass removed or emptied the sequence,
        a REMOVE is bound to an empty item and a REPLACE or SHIFT is left out
        and its key returned in `gone`. Where the address names no object or
        two, the copy has no entity and declines. Owners are looked up by
        `_owner_candidates`. The caller's findings are never modified.

        Args:
            findings (list): The findings handed to `anonymize()`.
            secret (bytes): This store's project secret, for the pseudonym and
                UID lookups.
            by_uid (dict): `_instances_by_uid()`.

        Returns:
            tuple: `(findings, gone)`: the resolved findings, and a frozenset
                of the remediation keys of those already settled.
        """
        # `anonymize(findings)` does not rehydrate `finding.entity`, and every
        # remediation arm writes to that object, so a report kept across
        # `close()` and a reopen would otherwise write to objects nothing
        # exports and file a success row for each write. Here rather than in
        # the service, because every reader downstream reads `finding.entity`,
        # and because nothing may be added above the service's line-cited
        # `mark_modified()` calls.
        #
        # An instance's path is walked strictly, by `_removal_address` and
        # never by `resolve_item_path`: `-1` and `True` read as positions
        # there, and a misspelt segment as a removed sequence. Copies, never in
        # place: a finding bound to None in place would stay unresolvable in a
        # later session that could resolve it, and a report passed twice would
        # behave differently the second time.
        #
        # Imports are local so no module-level line of this file moves
        # (`tests/test_packaging_contract.py` cites one by number).
        import dataclasses  # pylint: disable=import-outside-toplevel
        from .remediation import _remediation_key  # pylint: disable=import-outside-toplevel

        by_pid, by_study, by_series, instances, top = {}, {}, {}, [], set()
        for patient in self.store.patients:
            by_pid.setdefault(patient.patient_id, []).append(patient)
            top.add(id(patient))
            for study in patient.studies:
                by_study.setdefault(study.study_instance_uid, []).append(study)
                top.add(id(study))
                for series in study.series:
                    by_series.setdefault(series.series_instance_uid, []).append(series)
                    top.add(id(series))
                    for inst in series.instances:
                        top.add(id(inst))
                        instances.append(inst)
        items = None
        resolved, gone = [], set()
        for finding in findings:
            proposal, entity = finding.remediation_proposal, finding.entity
            if proposal is None or entity is None:
                resolved.append(finding)
                continue
            uid = finding.entity_uid
            if finding.entity_type == "Instance":
                candidates = by_uid.get(uid, ())
                if any(resolve_item_path(inst, finding.entity_path) is entity
                       for inst in candidates):
                    resolved.append(finding)
                    continue
            else:
                candidates = self._owner_candidates(finding, by_pid, by_study,
                                                    secret, by_series)
            unique = {id(c): c for c in candidates}
            if id(entity) in unique:
                resolved.append(finding)
                continue
            if id(entity) not in top and items is None:
                # Built once, and only when a finding's entity is neither
                # at its address nor a top-level object.
                items = {id(item) for inst in instances
                         for item, _ in iter_item_tree(inst)}
            if id(entity) in top or id(entity) in items:
                resolved.append(finding)
                continue
            if len(unique) != 1:
                resolved.append(dataclasses.replace(finding, entity=None))
                continue
            (target,) = unique.values()
            if finding.entity_type == "Instance":
                item = self._removal_address(target, finding.entity_path,
                                             proposal.target_attr)
                if (item is not None and proposal.action_type != "REMOVE_TAG"
                        and resolve_item_path(target, finding.entity_path) is not item):
                    gone.add(_remediation_key(finding))
                    continue
                target = item
            resolved.append(dataclasses.replace(finding, entity=target))
        return resolved, frozenset(gone)

    @staticmethod
    def _owner_candidates(finding, by_pid, by_study, secret, by_series=None) -> list:
        """The live `Patient`s, `Study`s or `Series` a finding's address names.

        A patient under its `entity_uid`, under this store's keyed pseudonym
        for that ID, and under the unkeyed one only for a patient its store
        classed legacy. A study or a series under its UID, or under the UID
        this store replaced it with. Any other type names none.

        Args:
            finding (PhiFinding): The finding whose address is read.
            by_pid (dict): Patient ID to patients.
            by_study (dict): Study Instance UID to studies.
            secret (bytes): This store's project secret, for the pseudonym and
                UID lookups.
            by_series (dict, optional): Series Instance UID to series.

        Returns:
            list: The candidates; empty for any other entity type.
        """
        # Shared by `_live_findings` and `_removal_targets`, so a removal
        # satisfied because the field at its address is gone, and a finding
        # rebound because its entity is dead, agree about which object that
        # address names.
        #
        # Imports are local so no module-level line of this file moves
        # (`tests/test_packaging_contract.py` cites one by number).
        from .entities import JITTER_SCHEME_UNKEYED  # pylint: disable=import-outside-toplevel
        from .privacy import (  # pylint: disable=import-outside-toplevel
            _replacement_id_for, _replacement_uid_for, _unkeyed_replacement_id_for)

        uid = finding.entity_uid
        if finding.entity_type == "Patient":
            candidates = list(by_pid.get(uid, ()))
            if uid and not _is_replacement_id(uid):
                candidates += by_pid.get(_replacement_id_for(uid, secret), ())
                candidates += [
                    p for p in by_pid.get(_unkeyed_replacement_id_for(uid), ())
                    if p._jitter_scheme == JITTER_SCHEME_UNKEYED]
            return candidates
        # A Study or a Series under its UID, and under the UID this store
        # replaced it with: a pass since the report may have replaced the
        # UID the report names. A Series is a finding entity.
        owners = {"Study": by_study, "Series": by_series or {}}.get(finding.entity_type)
        if owners is None:
            return []
        candidates = list(owners.get(uid, ()))
        if uid and secret:
            candidates += owners.get(_replacement_uid_for(uid, secret), ())
        return candidates

    def _status_bearers(self):
        """Every patient, study and instance: what a status column holds.

        Yields:
            TrackedEntity: Each patient, then its studies and their
                instances, in graph order.
        """
        for patient in self.store.patients:
            yield patient
            for study in patient.studies:
                yield study
                for series in study.series:
                    yield from series.instances

    def _working_tally(self, report_tally):
        """This session's working copy of a kept report's tally.

        Made on first use, one copy per audit token (`_ScanTally._audit`), so
        successive passes accumulate progress on one copy: a report narrowed to
        everything but one tag and then to that tag completes as it would in
        the scanning session. Not thread-safe; two threads calling
        `anonymize()` on one session is not supported.

        Args:
            report_tally (_ScanTally): The tally the report carries.

        Returns:
            _ScanTally: The session's copy for that audit.
        """
        # Keyed by the token, not per call, per report or per tally object, so
        # the two halves of a `copy.copy` split and tallies that are equal but
        # distinct objects (deep-copied or pickled) share one copy. Two audits
        # carry two tokens, and their reports never complete one another here.
        tally = self._report_tallies.get(report_tally._audit)
        if tally is None:
            tally = self._report_tallies[report_tally._audit] = report_tally.copy()
        return tally

    def _copy_owners(self) -> dict:
        """The owners the export stamps each instance's copies from.

        Returns:
            dict: `id(Instance) -> (Patient, Study, Series)` for every
                instance, for `RemediationService._use_copy_owners`.
        """
        return {id(inst): (patient, study, series)
                for patient in self.store.patients for study in patient.studies
                for series in study.series for inst in series.instances}

    def _named_by(self, tally) -> frozenset:
        """The patients, studies and instances `tally` raised findings under.

        Read before a pass: an instance by its SOP Instance UID, a study by its
        Study Instance UID, a patient by the Patient ID it holds now. An entity
        the scan never saw is not named.

        Args:
            tally (_ScanTally): The report's tally.

        Returns:
            frozenset: The `id` of each entity named.
        """
        named = set()
        for patient in self.store.patients:
            if tally.raised_under(patient.patient_id):
                named.add(id(patient))
            for study in patient.studies:
                if tally.raised_under(study.study_instance_uid):
                    named.add(id(study))
                for series in study.series:
                    for inst in series.instances:
                        if tally.raised_under(inst.sop_instance_uid):
                            named.add(id(inst))
        return frozenset(named)

    def _adopt_the_reports_policy(self, policy, recorded_at, named):
        """Give the report's policy to a status this pass recorded with none.

        Only for an entity in `named` whose status this pass recorded (its
        status revision moved). This gives a reopened store's statuses the
        policy the scanning session's would carry.

        Args:
            policy (ScanPolicy): The policy the report's scan ran under.
            recorded_at (dict): `id(entity) -> status revision` before the
                pass.
            named (frozenset): From `_named_by`.
        """
        # Remediation records a status under the policy the entity was last
        # scanned under. An entity reopened from the store with no scan behind
        # it (its audit never saved, the report kept across `close()`) has
        # none. The pass settled against the report's tally, so what it
        # recorded is what the scanning session's pass would have. An entity
        # the scan never saw keeps no policy, and a status recorded before the
        # pass is not this pass's.
        for entity in self._status_bearers():
            if id(entity) not in named:
                continue
            if recorded_at.get(id(entity)) == entity._phi_status_revision:
                continue
            status, recorded = entity._phi_status_record()
            if status is not PhiStatus.UNSCANNED and recorded is None:
                entity.record_phi_status(status, policy=policy)

    def _nested_finding_owners(self, findings, by_uid) -> dict:
        """The instance holding each finding raised inside a sequence.

        Found by the finding's UID in `by_uid` and confirmed by following its
        `entity_path` from the candidate back to the item the finding carries.
        A finding whose item is under no instance names no owner, and is
        remediated on the item alone.

        Args:
            findings (list): The resolved findings.
            by_uid (dict): `_instances_by_uid()`, the map the findings were
                resolved with.

        Returns:
            dict: `id(item) -> Instance`, for
                `RemediationService._use_instance_owners`.
        """
        # Confirmed by path because a hand-built graph can give two instances
        # one UID, and the UID alone would stamp and dirty the wrong one.
        # `by_uid` carries the UID `redact()` replaced; without it a nested
        # finding from before the redaction would have its item written and its
        # instance neither marked modified nor stamped, and the next save would
        # skip the write.
        nested = [f for f in findings
                  if f.entity_path and f.entity is not None
                  and f.entity_type == "Instance"]
        if not nested:
            return {}
        owners = {}
        for f in nested:
            for inst in by_uid.get(f.entity_uid, ()):
                if resolve_item_path(inst, f.entity_path) is f.entity:
                    owners[id(f.entity)] = inst
                    break
        return owners

    def _removal_targets(self, findings, by_uid, secret) -> dict:
        """Where each `REMOVE_TAG` finding's "already gone" is read.

        The object at the finding's `entity_uid` and `entity_path` in this
        session's graph, never the finding's own entity: an `Instance` finding
        among the instances (an empty item where a nested path breaks at what a
        pass removed, per `_removal_address`), a `Patient` or `Study` finding
        among its own collection (`_owner_candidates`). Where two instances
        share a UID, the one whose path leads to the finding's entity wins;
        otherwise a UID only one instance holds is followed. An owner resolves
        to its own entity where that is a candidate, else to the single
        candidate. A `Series` or any other type, and an address naming none or
        two, maps to None and declines.

        Args:
            findings (list): The findings, already resolved by
                `_live_findings`.
            by_uid (dict): `_instances_by_uid()`.
            secret (bytes): This store's project secret, for the owner lookups.

        Returns:
            dict: `id(finding) -> object or None`, for
                `RemediationService._use_removal_targets`.
        """
        # `anonymize(findings)` does not rehydrate `finding.entity`, so a
        # report kept across a reopen points at the first session's objects,
        # which a first pass may have cleaned while the graph `export()` writes
        # still holds each value. By address, never by identity alone: an
        # entity that is itself live but filed under another instance's UID is
        # misaddressed. Never looked up across collections: among the
        # instances, a Study whose UID a hand-built instance shares would read
        # that instance's absence.
        removes = [f for f in findings
                   if f.remediation_proposal is not None and f.entity is not None
                   and f.remediation_proposal.action_type == "REMOVE_TAG"]
        if not removes:
            return {}
        # A Patient's and a Study's own collections, built only when a
        # removal names one: the walk is the graph's patients and their
        # studies, which an instance-only pass has no reason to pay for.
        by_pid, by_study = {}, {}
        if any(f.entity_type in ("Patient", "Study") for f in removes):
            for patient in self.store.patients:
                by_pid.setdefault(patient.patient_id, []).append(patient)
                for study in patient.studies:
                    by_study.setdefault(study.study_instance_uid, []).append(study)
        targets = {}
        for f in removes:
            target = None
            if f.entity_type in ("Patient", "Study"):
                unique = {id(c): c for c in
                          self._owner_candidates(f, by_pid, by_study, secret)}
                if id(f.entity) in unique:
                    target = f.entity
                elif len(unique) == 1:
                    (target,) = unique.values()
                targets[id(f)] = target
                continue
            candidates = by_uid.get(f.entity_uid, ()) if f.entity_type == "Instance" else ()
            for inst in candidates:
                if resolve_item_path(inst, f.entity_path) is f.entity:
                    target = f.entity
                    break
            else:
                if len(candidates) == 1:
                    target = self._removal_address(
                        candidates[0], f.entity_path,
                        f.remediation_proposal.target_attr)
            targets[id(f)] = target
        return targets

    @staticmethod
    def _removal_address(instance, path, tag):
        """The object a removal's absence is read on.

        Used for a finding whose entity is not the item at its address. `path`
        is walked as deep as it resolves. If it resolves, the item at its end.
        If it breaks at a sequence the deepest live parent does not hold, named
        by a well-formed lower-case `gggg,eeee` key, an empty `DicomItem`: the
        removal's end state holds. If it breaks at an index past the items the
        sequence still holds, an empty `DicomItem` only when no remaining item
        holds `tag` anywhere beneath it, and None otherwise. Anything else,
        including a segment spelt another way or an index that is not a
        non-negative `int`, is None. Never the instance itself for a nested
        path.

        Args:
            instance (Instance): The instance the finding's UID resolved to.
            path (Sequence): The finding's `entity_path`.
            tag (str): The tag being removed, as `gggg,eeee`.

        Returns:
            Optional[DicomItem]: The object to read, or None.
        """
        # The walk exists for the clean reuse: the floor's private sweep
        # removes a private sequence after the tags inside it, and a second
        # pass must read the nested removals inside it as done. A shorter
        # sequence is not "gone": an item that shifted into a lower index still
        # carries the value into the export, and an address names a position,
        # not a value. Judged only within the sequence the address names; a
        # value held elsewhere is that element's own finding. Reading the
        # instance's top-level element under the same tag would call a nested
        # removal done, or decline one, over a different element.
        #
        # Imports are local so no module-level line of this file moves
        # (`tests/test_packaging_contract.py` cites one by number).
        from .entities import DicomItem, _canonical_tag  # pylint: disable=import-outside-toplevel

        item = instance
        for sequence_tag, index in path or ():
            # A position is a non-negative int. `-1` would read the last
            # item and `True` the second, each a different element from
            # the one the address was raised on.
            # `type() is int` because `bool` is an `int`.
            if type(index) is not int or index < 0:  # pylint: disable=unidiomatic-typecheck
                return None
            sequence = item.sequences.get(sequence_tag)
            if sequence is None:
                # Removed only if a sequence could ever have been stored
                # under this key: `0040,A730`, `(0008,1140)` or a keyword
                # misses because the graph never uses that spelling, not
                # because a pass removed anything.
                if (isinstance(sequence_tag, str) and _is_tag_key(sequence_tag)
                        and sequence_tag == _canonical_tag(sequence_tag)):
                    return DicomItem()
                return None
            if index >= len(sequence.items):
                key = _canonical_tag(tag)
                for remaining in sequence.items:
                    for nested, _ in iter_item_tree(remaining):
                        if key in nested.attributes or key in nested.sequences:
                            return None
                return DicomItem()
            item = sequence.items[index]
        return item

    @staticmethod
    def _live_target(instance, finding):
        """The live object a finding should be remediated against.

        A finding raised inside a sequence carries the path down to its item,
        the only way to find a sequence item again in this process. Logs a
        WARNING when it cannot be resolved.

        Args:
            instance (Instance): The live instance the finding's UID names, or
                None.
            finding (PhiFinding): The finding.

        Returns:
            Optional[DicomItem]: The live instance or nested item, or None when
                the address cannot be resolved.
        """
        # None rather than the enclosing instance: remediation skips a finding
        # with no entity, whereas writing a nested tag onto the instance
        # fabricates a top-level element that was never in the file and leaves
        # the real value inside the sequence, an export carrying the PHI plus a
        # decoy. The warnings say what happens to the finding, not what
        # remediation will do, because an OCR finding carries no proposal and
        # `auto_remediate_config()` still acts on it through its metadata.
        if instance is None:
            get_logger().warning(
                f"Finding for {finding.entity_uid} has no matching instance "
                "in the session; its entity will be None.")
            return None

        target = resolve_item_path(instance, finding.entity_path)
        if target is None:
            get_logger().warning(
                f"The sequence item behind {finding.field_name} on "
                f"{finding.entity_uid} is gone (path {finding.entity_path}); "
                "its entity will be None.")
        return target

    def _make_lightweight_copy(self, patient: "Patient") -> "Patient":
        """Clone a patient's graph without its pixel data, for a scan worker.

        Each instance keeps its `file_path`, so a worker can reload pixels.

        Args:
            patient (Patient): The live patient.

        Returns:
            Patient: The clone.
        """
        from .entities import Patient, Study, Series, Instance

        # Clone Patient
        p_new = Patient(
            patient_name=patient.patient_name,
            patient_id=patient.patient_id
        )
        # The patient's jitter scheme travels too. `scan_patient` mints
        # the replacement and stamps every SHIFT_DATE proposal from this
        # clone, so without it every worker sees a legacy patient as
        # keyed and gives its dates a second offset -- on both parallel
        # paths at once.
        p_new._jitter_scheme = patient._jitter_scheme

        for s in patient.studies:
            s_new = Study(
                study_instance_uid=s.study_instance_uid,
                study_date=s.study_date
            )
            if hasattr(s, "date_shifted"):
                s_new.date_shifted = s.date_shifted
            # The study's own date record travels too.
            # `_scan_study` runs inside `scan_patient`, which the worker
            # calls on this clone, so without this line every worker
            # sees a shifted study with no shift record, which counts as
            # shifted whatever its date, and raises nothing for it -- on
            # both parallel paths at once.
            s_new._shifted_study_date = s._shifted_study_date

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
                    # `source_path` is not carried across deliberately.
                    # `__post_init__` derives it from the `file_path`
                    # above, which is what a scan worker would see
                    # anyway; the clone is read by `scan_worker` and
                    # discarded, and no finding carries provenance back.
                    # If a clone is ever written to the store, this is
                    # the line that has to change first.
                    #
                    # Key: Ensure attributes are copied so workers can scan tags
                    if hasattr(i, 'attributes'):
                        i_new.attributes = i.attributes.copy()

                    # Sequences travel too: without them the worker gets
                    # a top-level-only instance, and the scan reports
                    # clean on every nested tag -- report text,
                    # annotations, anything below the first level.
                    i_new.sequences = clone_sequences(i, i_new)

                    # `Instance` has no `date_shifted`: the scan reads the
                    # per-value records below instead, and the study's
                    # flag rides `s_new.date_shifted` above.
                    #
                    # The per-value date records and the store's own
                    # provenance travel too. `audit()`
                    # scans this clone unconditionally, threads and
                    # processes alike, so without these two lines every
                    # worker sees an instance with no record against any
                    # of its dates, raises them all, and the arm shifts
                    # each a second time. The nested half is
                    # `clone_sequences`, above.
                    if i._shifted_dates:
                        i_new._shifted_dates = dict(i._shifted_dates)
                    i_new._legacy_shift_provenance = i._legacy_shift_provenance

                    se_new.instances.append(i_new)

        return p_new

