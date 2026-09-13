import gc
import os
import re
import json
import contextlib
import threading
import datetime
import multiprocessing
import concurrent.futures
from collections import Counter
from typing import (List, Union, Dict, Any, Optional, Set, Tuple,
                    NamedTuple)

import yaml
from tqdm import tqdm

from .io_handlers import (DicomImporter, DicomExporter, ExportContext,
                          ExportError, ExportSummary, SidecarPixelLoader,
                          SidecarWaveformLoader, export_folder_names,
                          GRADED_LOSS_SCOPES, redaction_in_effect)
from .store import DicomStore
from .services import (RedactionService, RedactionOutcome, RedactionError,
                       capture_phi_status_for_redaction,
                       carry_phi_status_across_redaction,
                       _report_redaction_failures)
from .config_manager import ConfigLoader, require_package_resource
from .privacy import (PhiInspector, PhiFinding, PhiReport, _is_replacement_id,
                      _is_replacement_name)
from .logger import configure_logger, describe_exception, get_logger
from .reporting import (ComplianceReport, PixelScanSummary, get_renderer, GAP_REMOVED,
                        GAP_RETAINED, GAP_UNRESOLVED)
from .manifest import Manifest, ManifestItem, generate_manifest_file
from .blob_kind import serialize_blob_kind
from .persistence import SqliteStore
from .crypto import KeyManager
from .reversibility import ReversibilityService
from .persistence_manager import PersistenceManager
from .parallel import (run_parallel, _env_int, _resolve_strategy,
                       resolve_max_workers, resolve_worker_initializer)
from .configuration import IsocenterConfiguration, FlowList
from .entities import (Patient, PhiStatus, SOURCE_SOP_UID_ATTR, clone_sequences,
                       resolve_item_path, iter_item_tree)
from .profiles import BASIC_PROFILE, FLOOR_POLICY, PRIVACY_PROFILES
from . import entities
from . import pixel_analysis
from .automation import ConfigAutomator

def scan_worker(args):
    """
    Worker function for parallel PHI scanning.
    Args:
        args: Tuple of (patient_obj, config_source, remove_private,
              project_secret), exactly.

    Returns: List[PhiFinding] (WITHOUT entities)

    The project secret travels **by value**, in the tuple. A worker
    cannot read it back out of the store: a spawned process cannot reach
    a `:memory:` database at all, and a second place the worker could
    get a secret from is a second answer to "which secret keys this
    patient". There is no default for the same reason -- a worker handed
    no secret raises at the first pseudonym it has to mint rather than
    falling back to an unkeyed one.

    A database-path form, `(db_path, patient_id, config_source,
    remove_private)`, was accepted until 0.9.7 and had no caller; it is
    gone rather than taught to find a secret.
    """
    patient, config_source, remove_private, project_secret = args
    if not isinstance(patient, Patient):
        raise TypeError(
            f"scan_worker expects (Patient, config_source, remove_private, "
            f"project_secret); got a {type(patient).__name__} first")

    if isinstance(config_source, dict):
        inspector = PhiInspector(config_tags=config_source,
                                 remove_private_tags=remove_private,
                                 project_secret=project_secret)
    else:
        inspector = PhiInspector(config_path=config_source,
                                 remove_private_tags=remove_private,
                                 project_secret=project_secret)

    findings = inspector.scan_patient(patient)

    # Strip heavy entity objects before returning across process boundary
    for f in findings:
        f.entity = None

    return findings




class _ScanOutcome(NamedTuple):
    """What `_verify_worker` sends back for one instance (#423).

    Module scope, so it pickles across the process pool. It carries its
    own UID because the recycling pool is `imap_unordered`: outcomes
    arrive in completion order, and zipping them back onto the items that
    were dispatched would pin each failure on the wrong instance.
    """
    entity_uid: Optional[str]
    findings: List[PhiFinding]
    read: bool
    failure: Optional[str]


def _caller_tesseract_cmd() -> Optional[str]:
    """The `tesseract_cmd` this process's pytesseract runs, or `None` (#458).

    Read in the caller, after `_require_ocr` has probed that very binary,
    and sent with each work item: a spawned worker imports a fresh
    pytesseract whose `tesseract_cmd` is the bare `"tesseract"`, looked
    up on `PATH`, so a caller who configured the binary rather than
    installing it on `PATH` passed the probe and then had every worker
    fail. `getattr` all the way down because pytesseract is optional and
    a stand-in for it need not carry the submodule; `None` means "adopt
    nothing".
    """
    inner = getattr(pixel_analysis.pytesseract, "pytesseract", None)
    return getattr(inner, "tesseract_cmd", None)


def _adopt_tesseract_cmd(cmd: Optional[str]) -> None:
    """Point this process's pytesseract at the caller's binary (#458).

    **Writes only when the value differs**, and that is what keeps the
    threads path untouched rather than an optimisation. A worker thread
    shares the caller's module, so it already reads the caller's value
    and the comparison is equal; writing it back from every worker thread
    would be worker threads mutating module state the caller owns, which
    is the shape that leaked a stand-in across tests in #466. In a
    spawned child the module is the child's own, and the write is the fix.
    Guarded like `_caller_tesseract_cmd`: a worker whose `pytesseract` is
    `None` or a stand-in without the submodule adopts nothing, and its
    OCR fails -- or not -- exactly as it would have.
    """
    if cmd is None:
        return
    inner = getattr(pixel_analysis.pytesseract, "pytesseract", None)
    if inner is not None and getattr(inner, "tesseract_cmd", None) != cmd:
        inner.tesseract_cmd = cmd


def _verify_worker(args):
    """
    Worker for pixel verification.
    Args:
        args: Tuple(Instance, Equipment, List[Rules], Optional[str]) --
            the last is the caller's `tesseract_cmd` (#458).

    Returns: `_ScanOutcome` -- the instance's findings (WITHOUT entities),
    whether any frame was read, and why it could not be read in full.
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
    # does; `scan_pixel_content` puts the live one back (#412). The strip
    # is not tidiness. OCR decodes the frame first, `get_pixel_data()`
    # caches it on the instance, and `_findings_for` attaches that
    # instance to every finding -- so the result carried the decoded
    # frame back to the parent, once per scanned instance with a finding
    # (pickle memoises the shared instance, so a second finding on the
    # same frame adds ~150 bytes, not a frame). Real tesseract, one
    # 256x256 16-bit frame, two findings, locally: 132519 bytes against
    # 542 (3.12.14). Rehydration alone would
    # hide this: it overwrites the copy, so the entity the caller sees is
    # right while the frame still crosses the pipe. Only
    # `tests/test_scan_pixel_content_dispatches_its_worker.py`'s T-394a
    # is red without this loop. Since #428 `_ocr_instance` frees a
    # loader-backed frame before this result is built, so what the strip
    # still guards is the entity's identity (one meaning for
    # `finding.entity`, which `_rehydrate_findings` restores) and an
    # in-memory instance whose frame was resident before the scan.
    for f in findings:
        f.entity = None
    return _ScanOutcome(uid, findings, ocr.read, ocr.failure)


def _discover_worker(args):
    """Worker for zone discovery: `(entity_uid, _InstanceOcr)` for one instance.

    `args` is `(instance, tesseract_cmd)`. Discovery passes
    `force_threads=True`, and #458 first read that as "always threads, so
    it needs no `tesseract_cmd`"; but `ISOCENTER_MAX_TASKS_PER_CHILD`
    outranks `force_threads=True` (`parallel._resolve_execution_choice`),
    and under it discovery runs in spawned processes and failed every
    instance exactly as the scan did -- measured on both gate builds.

    Discovery read through `pixel_analysis.analyze_pixels` until #423's
    rule reached it, and that function logs a failed load or frame and
    returns `[]`, so an instance nobody read counted as a source with no
    text, and diluted every zone's occurrence rate. Module scope, and
    `_ocr_instance` reached through the module, for the same reasons as
    `_verify_worker`; the same boundary catch, so one unexpected error
    costs one instance rather than the pass.
    """
    instance, tesseract_cmd = args
    uid = instance.sop_instance_uid
    _adopt_tesseract_cmd(tesseract_cmd)
    try:
        return uid, pixel_analysis._ocr_instance(instance)  # pylint: disable=protected-access
    except Exception as e:  # pylint: disable=broad-exception-caught
        return uid, pixel_analysis._InstanceOcr(  # pylint: disable=protected-access
            [], False, describe_exception(e))


def _warn_unread_instances(operation, failures, attempted, where):
    """Warn how many instances an OCR pass could not read (#423).

    The warning is what reaches a caller who reads neither the report's
    `failures` nor the log file: the `isocenter` logger's console handler
    prints WARNING and above. Silent when nothing failed.
    """
    if not failures:
        return
    uid, reason = failures[0]
    get_logger().warning(
        f"{operation}: {len(failures)} of {attempted} instance(s) could not "
        f"be read in full, so their text was not (or not all) scanned; see "
        f"{where}. First: {uid}: {reason}")


def _audit_unread_instances(store_backend, operation, failures):
    """One `WARNING` audit row per instance an OCR pass could not read (#479).

    The owner's ruling on #423's third question, and the mirror of
    `DicomExporter._report_export_failures`: a failure the caller was told
    about but the audit log was not left the compliance report grading a
    run `PASS` whose verification never looked at some of its pixels,
    beside "No exceptions or errors were recorded".

    `WARNING` rather than export's `ERROR`, as the ruling names it: a
    pass that read some instances returns a result, and nothing was
    written wrong. The two grade alike -- `get_audit_errors()` selects
    both, and any row it returns costs the run its PASS -- and both are in
    the frozen audit vocabulary, so this adds no word.

    The UID is in `details` as well as `entity_uid` because the report
    renders `(timestamp, action_type, details)` and nothing else; without
    it the exceptions section would say an instance failed and not which.
    Flattened and pipe-escaped for the same markdown table row export's
    rows go into. Called before the warning and before any raise, so a
    caller who catches `PixelScanError` has an audit log that already
    holds every row.
    """
    for uid, reason in failures:
        detail = (f"{operation} could not read {uid} in full, so its "
                  f"burned-in text was not (or not all) checked: {reason}")
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
# privacy_profile: "basic"
#   - Standard profile handling common PHI (Name, ID, etc).
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


def _load_redaction_knowledge_base() -> List[Dict[str, Any]]:
    """Machine redaction rules shipped with the package, keyed by serial."""
    # Before the `try`, and that placement is load-bearing: the handler
    # below catches `OSError`, and `FileNotFoundError` is one -- a
    # refusal that drifted inside would be caught and turned straight
    # back into the `return []` this replaces, with every test still
    # green (#388). `require_package_resource` raises `RuntimeError`
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
    """
    # The YAML keeps its `os.path.exists`, deliberately: `ctp_rules.yaml`
    # is *not* shipped, so its absence is the ordinary case and routing it
    # through the helper would make every correct installation a broken
    # one. Only the JSON fallback -- which `setup.py` does package and
    # `publish.yml` does gate on -- is required (#388).
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
    verify_readback: bool = False


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
        "0020,000d": study.study_instance_uid,
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
        "0020,000e": series.series_instance_uid,
        "0008,0060": series.modality,
        "0020,0011": str(series.series_number),
    }
    if getattr(series, 'series_description', None):
        attributes["0008,103e"] = series.series_description
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
    """A readable name for a flagged tag, from the floor policy.

    This recognised three tags by hand and called everything else
    `unknown_tag`, then read the six-tag `resources/phi_tags.json` plus a
    three-entry supplement. Since #495 it reads `profiles.FLOOR_POLICY`,
    the one table that names every tag a bare session or the scaffold
    applies, so the name here is the name the config file uses.

    Falls back to the tag itself rather than to `unknown_tag`: the name is
    a comment to the reader, and a tag repeated is at least true, where
    three rules all called `unknown_tag` are indistinguishable.
    """
    entry = FLOOR_POLICY.get(tag)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return tag


class LockingResult(list):
    """
    A list subclass that suppresses verbose REPL output for large datasets.
    """

    def __repr__(self):
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

    Half the CPUs, capped at eight. Each worker holds a decoded image, so
    this cap is a memory ceiling rather than a throughput choice --
    `run_parallel`'s own default of one worker per CPU has exhausted
    memory on large studies.

    `ISOCENTER_MAX_WORKERS` overrides it. A malformed value used to raise
    inside the handler that swallowed everything, so a typo in a shell
    profile turned redaction into a no-op that reported success; now it
    warns and falls back to the default. A value below 1 is reported and
    replaced by the same default (#341): until then this read clamped
    `0` and every negative to a single worker with `max(1, override)`
    and said nothing, while `run_parallel`'s read of the same variable
    had been warning since #335 -- one variable, two answers. The floor
    is `_env_int`'s now, so there is no clamp here to read as the guard.
    The `max(1, ...)` on the default is a different thing and stays:
    `cpu_count() // 2` is `0` on a one-CPU box.
    """
    override = _env_int("ISOCENTER_MAX_WORKERS", minimum=1)
    if override is not None:
        return override
    return max(1, min((os.cpu_count() or 1) // 2, 8))


#: Why processes can never redact a `:memory:` store. Shared by the
#: warning and the refusal below, because it is one fact and two
#: spellings of one fact is what this project's conventions forbid.
_WHY_PROCESSES_CANNOT_REDACT_A_MEMORY_STORE = (
    "a spawned worker is handed _memory_conn=None by "
    "SqliteStore.__setstate__ and opens a fresh, empty in-memory database "
    "with no instance_blobs table")


def _report_processes_lever_on_a_memory_store(db_path, strategy):
    """Says something when a `:memory:` `redact()` was asked for processes.

    Two silences, and they are not the same silence (#400). The
    discriminator is **`strategy.use_threads`**, never the lever's name:

    - `use_threads` is `True` -- the request was **discarded** and the
      pass is about to run correctly in threads. There is a correct run
      to annotate, which is exactly #185's case, so this **warns**.
    - `use_threads` is `False` -- the request was **obeyed**, and there
      is no execution in which obeying it is correct: every redaction
      worker ends in `persist_pixel_data`, so all of them fail with
      `no such table: instance_blobs`, measured three of three on both
      gate interpreters. Nothing to annotate, so this **refuses**.

    On the redaction path only two rows are reachable and that is a
    consequence of the ranking rather than a coincidence: `redact()`
    passes `force_threads=True` for a `:memory:` store, which sits at
    rank 2, so the only lever that can make `use_threads` false is rank
    1, worker recycling. Written on `use_threads` all the same -- a
    fifth lever added at some future rank is then classified correctly
    without touching this function, where `if lever == "..."` would work
    today and be wrong the moment such a lever exists.

    `strategy.processes_requested_by` is read, never re-derived. A
    session-side `_env_is("ISOCENTER_FORCE_PROCESSES", ...)` would
    re-encode the rank-2-beats-rank-3 ordering in a second file and
    would speak up for an operator who set **both** force levers, whose
    effective request is threads and who is being denied nothing.

    Raises:
        RuntimeError: When the store is `:memory:` and a lever obtained
            processes. Plain, not `RedactionError`: nothing was
            attempted, and `RedactionError` **is** a `RuntimeError`, so
            a caller writing `except RedactionError` around `redact()`
            to handle a partial pass would otherwise read "your
            environment cannot run this" as "some images failed".
    """
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
        # Four properties this message holds, and #185's does not.
        # It names no knob the reader did not set (`force_threads`
        # appears nowhere -- redact() set that, not them); it says in as
        # many words that the result is correct, because a warning in
        # front of a correct result that does not say so sends the
        # reader looking for damage that is not there; it bounds itself,
        # since the fact an operator needs is that their variable works
        # at every step but this one; and it is emitted once per
        # `redact()` call rather than once per `run_parallel`.
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
                `":memory:"` is accepted and is part of the frozen surface
                (#379): the index lives in memory and the pixel sidecar in
                a temporary file the store unlinks on `close()`. On such a
                store `redact()` runs in threads on every interpreter,
                because its worker writes to the store and a process
                cannot share an in-memory database (#381); with
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

        # Detect descriptor damage a pre-fix release persisted (#186,
        # #214) at the moment the store opens, so a session holding it
        # cannot run to a clean-looking export first. Detection only, on
        # purpose -- the sidecar's bytes are shape-free, so any repair
        # would be a best-effort guess; the remedy is in the message and
        # the same result reaches `generate_report`, where it costs the
        # run its PASS through the COMPLIANCE_CHECK channel.
        for uid, _path, details in self.store_backend.check_pixel_geometry():
            get_logger().warning(f"{uid}: {details}")

        # Initialize Configuration Object
        self.configuration = IsocenterConfiguration()

        # Reversibility
        self.key_manager = None
        self.reversibility_service = None

        # What the last DICOM export delivered, so the compliance report
        # can say how many instances were written beside how many are
        # indexed (#181). None means "no export has run in this
        # session", which is not the same as "nothing was written" --
        # the report omits the row rather than claiming a zero.
        self._last_export_written = None
        self._last_export_requested = None

        # The verbs this session actually performed ("REDACTION",
        # "ANONYMIZE"), so `generate_report` can demand action-specific
        # evidence: a redacting run whose REDACTION rows were lost to a
        # second defect must not grade PASS on the strength of unrelated
        # rows (#254). Transient, in-memory and session-scoped on
        # purpose -- persisted, this would be a second durable answer to
        # "what happened" that can disagree with the audit log, the
        # shape this codebase keeps deleting (the retired `text_index`,
        # #84). A verb is recorded only where the run would have emitted
        # its audit rows, so a call that performed no work demands no
        # evidence; see the two recording sites.
        self._actions_performed: Set[str] = set()

        # Each `scan_pixel_content()` call this session made, for the
        # report's section 5 (#481), which used to say "pixel data was
        # scanned" for every session. Transient for `_actions_performed`'s
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
        # spawn, which is why nothing local ever saw the difference
        # (#220, #250).
        # How wide the pool below was built, and how many `ingest()`
        # calls are running on it right now (#511). The width is recorded
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
        # counts as one (#434) -- so it is not part of the documented
        # lock order in CLAUDE.md and must not be made part of it: it is
        # taken and released at the top of `ingest()` with nothing held,
        # and again in that call's `finally` after the pass-lock has been
        # released. `_ingest_executor()` keeps that true by capturing its
        # decision as data and logging it, and retiring the pool it
        # swapped out, only after the lock is released. The one thing
        # under it that takes any lock at all is the replacement pool's
        # constructor, which takes the stdlib's own internal locks.
        self._executor_width = resolve_max_workers()
        self._ingest_lock = threading.Lock()
        self._ingests_in_flight = 0

        self._executor = concurrent.futures.ProcessPoolExecutor(
            # Sized by the resolver `run_parallel` uses, so
            # `ISOCENTER_MAX_WORKERS` narrows `ingest()` as it narrows
            # every other parallel step. Until #501 this was `None`: the
            # stdlib gave one worker per CPU whatever the variable said,
            # under a comment claiming CPU * 1.5. Since #511 each
            # `ingest()` re-resolves this and rebuilds the pool when the
            # width has changed, so the variable is honoured whenever it
            # is set rather than only here.
            max_workers=self._executor_width,
            mp_context=multiprocessing.get_context("spawn"),
            # The same env-gated worker setup as run_parallel's pools
            # (GC off, child-side faulthandler watchdog); resolved by
            # the one resolver so the session's own pool cannot drift
            # from the per-call ones (#250).
            initializer=resolve_worker_initializer())

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
                get_logger().error(f"Error during session close(): {describe_exception(exc)}", exc_info=True)
                if first_exception is None:
                    first_exception = exc

        # Order is load-bearing. `shutdown()` reconciles a save its
        # worker never finished and records an ERROR audit row when it
        # cannot (#314); `stop()` flushes the audit queue, so those rows
        # settle before close() returns. Reversed, they would be written
        # into a queue nothing drains again.
        if hasattr(self, 'persistence_manager'):
            _run_step(self.persistence_manager.shutdown)

        # After `shutdown()`, and deliberately not through `_run_step`.
        # Order: `shutdown()` reconciles a save its worker never finished
        # (#314), so asking before it would report instances that were
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
        """Say so when `close()` is about to drop unsaved instance edits (#307).

        `close()` shut the session down in silence over a graph carrying
        changes no `save()` ever reached, and those changes were gone.
        The verbs that produce them are the ordinary ones -- `audit()`
        advances `_revision` through `record_phi_status()`, `anonymize()`
        and `redact()` mutate -- so the most common firing is
        "audited, then closed", and the message names *what* is unsaved
        rather than only how many, because "1 unsaved instance" tells a
        caller nothing they can act on.

        **Instances only.** `SqliteStore.save_all` calls
        `mark_persisted()` on instances and on nothing else; no level
        above them is marked anywhere in the save walk, so every
        built-then-saved patient, study and series reports
        `has_unsaved_changes` forever. Warning on those would fire on
        every correct session, and a warning that fires when nothing is
        wrong is one people learn to skip. Widening this means fixing the
        save walk first, with per-parent revision capture -- the
        `mark_persisted()` trap -- not widening the walk here.

        **A warning and not an audit row.** A row written here would land
        between `persistence_manager.shutdown()` and
        `store_backend.stop()`, and could only ever be read by a *later*
        session's report: a second durable answer to a question the graph
        in front of the caller already answers, which is the shape this
        project keeps deleting.

        One bounded pass over the graph and no I/O, so it costs nothing
        worth measuring even on a large store. It swallows its own
        errors: see the call site for why a raise here would be worse
        than the diagnostic being missing.

        Warning twice on a double `close()` is accepted. The graph really
        is still unsaved the second time, and remembering that it had
        already been mentioned would be state answering a question the
        graph answers. Zero unsaved instances is silent, so an ordinary
        double close says nothing extra.
        """
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
                # least easily find again (#337).
                #
                # The fallback has to *locate*, not merely be joinable:
                # three instances rendered as "<unknown>, <unknown>,
                # <unknown>" is the same non-information this message
                # exists to replace (#307), without the crash.
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
            print(f"WARNING: {message}")
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
        Persists the current session state to the database.

        :param sync: If True, blocks until save is complete.

        A synchronous save drains the persistence manager first, the
        same way `audit()` and `redact()` do. Without that it called
        `save_all` on the caller's thread while the worker could be
        inside `save_all` over the same graph and the same sidecar --
        and since #287 the two sidecar prepasses run fully in parallel,
        so #287's bound on orphaned frames became per-concurrent-save
        rather than per-store (#294).

        The price is that `save(sync=True)` **inherits `flush()`'s
        deliberate never-return-early property**: a genuinely wedged
        worker now wedges a synchronous save instead of letting it race
        one. That is the trade `flush()`'s own docstring argues for --
        callers ask for "synchronous" precisely so they can read or
        close afterwards.
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

    def _restart_executor(self, max_workers=None):
        """
        Restarts the internal process pool executor, potentially with fewer workers.
        Useful for recovering from BrokenProcessPool errors (OOM).

        With no argument the width is resolved as construction resolves
        it: `ISOCENTER_MAX_WORKERS`, else one per CPU, re-read now.

        This is the **broken-pool** path, and its `cancel_futures=True`
        below is part of that contract: the pool it is replacing is
        assumed unusable, so whatever is still queued on it is lost
        either way. A healthy resize must not come through here -- see
        `_ingest_executor()`, which is the #511 path and never cancels
        anything.
        """
        if max_workers is None:
            # Not the stdlib's `None`, which is one per CPU. An OOM restart
            # that widened the pool back past `ISOCENTER_MAX_WORKERS`
            # would undo the setting at exactly the moment memory is short
            # (#501).
            max_workers = resolve_max_workers()
        get_logger().warning(f"Restarting ProcessPoolExecutor (max_workers={max_workers})...")
        if self._executor:
            try:
                # Force kill old processes if they are stuck/broken
                self._executor.shutdown(wait=False, cancel_futures=True)
            except (RuntimeError, OSError) as exc:
                # The executor is being replaced regardless; a failure to
                # shut the old one down is worth a line in the log, not a
                # crash.
                get_logger().debug("Could not shut down prior executor: %s", describe_exception(exc))

        # Re-init, with the same spawn pin as construction: an OOM
        # recovery must not quietly downgrade the pool to fork (#220),
        # nor drop the worker setup construction resolved (#250).
        self._executor = concurrent.futures.ProcessPoolExecutor(
            max_workers=max_workers,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=resolve_worker_initializer())
        # The recorded width follows every swap (#511). A restart that
        # narrowed the pool for an OOM recovery and left this at the old
        # number would make the next `ingest()` either rebuild for
        # nothing or -- with the variable narrowed to match -- skip the
        # rebuild it needed and run at the recovery width in silence.
        self._executor_width = max_workers

    @contextlib.contextmanager
    def _ingest_executor(self):
        """The shared pool for one `ingest()`, at the width asked for now (#511).

        `ISOCENTER_MAX_WORKERS` is re-resolved on entry and the pool is
        rebuilt when the width has changed, so the documented lever
        narrows `ingest()` whenever it is set rather than only at
        `Session()`. Every other reader is per call already:
        `run_parallel` resolves it on each call, and `redact()` reads it
        in `_redaction_worker_count` on each pass. #504 made the argument
        this closes: a construction-time read is ignored in silence when
        the variable is set later.

        **A peer ingest is never disturbed.** Two `ingest()` calls on two
        threads of one session can overlap -- the sidecar pass-lock is
        taken shared for an ingest, so the lock design admits it. This
        pool is the only thing they share. When one is already in flight
        and the width has changed, this call logs a `WARNING` naming the
        variable, the pool's width and the requested one, and runs on the
        pool as it stands; the next `ingest()` that starts with no peer
        gets the new width. The alternative was rejected: the pool's
        futures are all submitted in one `executor.map` inside
        `_run_on_shared_executor`, so cancelling them (which is what
        `_restart_executor()` does, correctly, for a *broken* pool) would
        drop the files the peer has not started yet with nothing raised
        at the caller -- #232's shape. Blocking on `shutdown(wait=True)`
        instead would make a public method wait out the peer's whole
        ingest, and running a second pool beside the first *for the
        length of the peer's ingest* doubles the process count on the box
        of an operator who is narrowing workers because memory is short.
        That is about two pools with work on them, not about the moment
        below where the replacement object exists before the old one is
        shut down: an unsubmitted-to pool has no workers at all.

        The check, the swap and the snapshot are one critical section,
        and the dispatch uses the snapshot rather than re-reading
        `self._executor`. Split, there is a window: a peer reads the old
        pool, this call shuts it down, and the peer's `map()` raises
        `cannot schedule new futures after shutdown`.

        Resizing costs nothing when the width is unchanged, which is
        every call unless the variable moved.

        **The replacement is built before the old pool is retired, and
        the counter moves after the swap.** Both orderings matter, and
        the reason is a rebuild that *fails*: `ProcessPoolExecutor(...)`
        raises `OSError` when the box is out of file descriptors or
        memory for the queue pipes -- EMFILE, ENOMEM -- which is the
        failure mode of exactly the memory-short machine an operator is
        narrowing workers for. Built first, nothing has been mutated when
        it raises: the session keeps the live pool it had, at the width
        still recorded, and the exception propagates, so a failed resize
        costs the caller its `ingest()` and nothing more. Retired first
        instead, `self._executor` would point at a pool already shut
        down and every later `ingest()` would raise `cannot schedule new
        futures after shutdown` -- permanently, because the width left
        stale means the next call sees nothing to rebuild. The counter is
        incremented after the swap for the same reason: this is a
        `@contextmanager`, so an exception raised before the `yield`
        escapes `__enter__` and the `finally` below never runs, and a
        counter left at 1 is a phantom peer that makes every later
        `ingest()` skip the resize for a call that is not there.

        Building first does **not** double the process count, which is
        the objection to standing up a second pool at all: a
        `ProcessPoolExecutor` spawns no worker until the first task is
        submitted (measured, `len(pool._processes) == 0` after
        construction and 1 after one submit, on 3.12.14 and 3.14.7t), and
        the retirement completes before this call dispatches anything. So
        there are two pool *objects* for the length of a teardown and one
        set of workers throughout, and an `ingest()` that rebuilds and
        then finds nothing new to read pays only that teardown.

        Neither the log lines nor the teardown run under
        `_ingest_lock`. The lock covers the check, the swap and the
        snapshot; the decision is captured as data and acted on after it
        is released. `shutdown(wait=True)` under the lock would block
        every later `ingest()` behind a worker wedged in a C library, and
        a logging handler takes its own lock, which the comment at
        `_ingest_lock`'s definition promises this one is never held
        across (the `PIXEL_STATE_LOCK` convention, #434). The resizing
        call does still wait out its own retirement -- the same
        `shutdown(wait=True)` `close()` performs, not a new class of
        wait -- but it waits holding nothing.

        **The `try` therefore begins the instant the lock is released**,
        so nothing between the increment and the decrement sits outside
        it -- the log call and the teardown included. A generator's
        `finally` does run when its body raises before the `yield`, so
        anything that fails out here decrements on the way out; what the
        counter cannot survive is a raise *before* it is incremented
        being followed by a decrement, which is why the increment sits
        after the rebuild -- with only the snapshot, which cannot raise,
        between it and the release. The case is not hypothetical: the
        one thing out here that can block is the retirement waiting on
        that wedged worker, and `Ctrl-C` during it raises
        `KeyboardInterrupt`, which `_retire_shared_executor` does not
        catch. A retirement that fails leaves the swap in place -- the
        new pool is live and recorded -- so that too costs the caller its
        `ingest()` and not the session.
        """
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
        """Shuts a swapped-out shared pool down, cancelling nothing (#511).

        `wait=True` and no `cancel_futures`: the caller has established
        that no `ingest()` is running on it, so this returns as soon as
        the workers exit. The opposite of `_restart_executor()`'s
        teardown, deliberately -- that one is replacing a pool assumed
        broken.

        It takes the pool to retire rather than reading
        `self._executor`, because by the time it is called that
        attribute is the replacement: `_ingest_executor()` swaps first
        and retires afterwards, outside `_ingest_lock`, so that a wedged
        worker cannot block the session's next `ingest()`.
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
        Attempts to release memory by unloading cached pixel and waveform
        data from all instances.

        Each unload goes through `Instance.unload_pixel_data()` and
        inherits its precondition exactly, **limit included**: it
        refuses an array replaced through `set_pixel_data()` and not
        since written, and an array mutated **in place** is not tracked,
        so it is dropped here and the next `get_pixel_data()` returns
        the frame from before the mutation. Reaching that needs a
        writeable array, which means a replacement a save has already
        written: a frame read from a file or the sidecar is
        `np.frombuffer`-backed and read-only, so mutating one raises
        instead of diverging. `unload_pixel_data()` states the
        precondition in full and this does not restate it.

        **Redaction is not an instance of it**, and the measurement is
        recorded because two docstrings have now said it was.
        `RedactionService._redact_instance_pixels`' writeable arm does
        mutate in place and never calls `set_pixel_data()`, but on a
        reloaded instance that arm is never entered -- the array is
        read-only, so every pass takes the copying arm -- and when it
        *is* entered both callers persist the pixels and then call
        `discard_pixel_data()` unconditionally in their `finally`, so
        nothing survives the pass for this sweep to drop. Without a
        `store_backend` that discard loses the mutation on its own,
        which is a different defect with a different fix. The shape this
        limit belongs to is any caller that mutates a written array in
        place, which is what `tests/test_pixel_divergence.py` does.

        This used to say flatly that nothing is discarded (#323). It
        was never true of an in-place mutation, and detection is the
        wrong axis for making it true: hashing the resident array here
        was rejected by #293 for this exact call site -- a full pass
        over pixel bytes on the path the 100GB scaling story depends on
        -- and `_pixel_hash` is not always populated, since hydration
        wires a loader without one, so a `None` would have to mean
        either "refuse everything hydrated", turning the only
        RAM-reclaiming operation this library has into a no-op after a
        reload, or "allow", which is the hole again. What closes it is
        making in-place mutation impossible: `get_pixel_data()`
        returning a copy, or a `writeable=False` view, so divergence
        can arise only through `set_pixel_data()`. That is breaking, it
        has an in-tree caller in `_redact_instance_pixels`' writeable
        arm, and it puts a copy on every read of the memory-critical
        path -- so it is stated here rather than done quietly.

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

                            # `unload_pixel_data`, deliberately: since
                            # #293 an array replaced through
                            # `set_pixel_data()` and not yet written is
                            # refused here and stays resident. Do not
                            # "optimise" this to `discard_pixel_data()`;
                            # that would free more memory by throwing
                            # away pixels no one else holds.
                            #
                            # An array mutated in place is not tracked,
                            # so it is still dropped here silently --
                            # the limit `unload_pixel_data()`'s own
                            # docstring states. #293 did not make that
                            # true; #323 narrowed this method's
                            # docstring to say so, rather than leave it
                            # promising that nothing is discarded, and
                            # what would actually close it is stated
                            # there.
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
        WARNING: This is an expensive I/O operation. It leads with
        `save(sync=True)`, so it inherits that call's never-return-early
        wait on the persistence manager (#294): a wedged background
        worker wedges a compaction rather than letting one race it.

        **Concurrent writers are serialised, and a pass is refused
        (#368).** Two behaviours are contract, observable from any
        thread of this session:

        1. This method **raises `RuntimeError`** while a `redact()` or
           `ingest()` pass is open on the same store (`"compact()
           refused: a redact() or ingest() pass is open on
           <sidecar>.pass.lock; wait for it to return"`). During a pass
           the graph carries references the store has not been told
           about yet -- a redaction worker commits its blob row under a
           regenerated UID before any `instances` row names it -- and
           compaction's orphan predicate would reclaim exactly those
           rows. Measured on 0.9.3 through this very method: it
           returned success and deleted every redacted frame. The
           refusal is the **first** thing this method does, before its
           leading save, so a refused call has done nothing at all. It
           sat between the save and the rewrite until the review of PR
           #385 showed the window that leaves: a pass that opens after
           the save's rows are written and closes before the rewrite
           (`redact()` does not save at its end) is admitted with its
           `instances` rows on the old UIDs and its blob rows on the
           regenerated ones, and the rewrite reclaims every worker
           frame -- reproduced on 3.12 and 3.14t, all readbacks raising
           afterwards. Taken first, such a pass waits at its `LOCK_SH`
           and lands after the rewire.
        2. A `redact()` or `ingest()` that starts while this method is
           saving or rewriting **waits**, bounded by `_SIDECAR_GATE_TIMEOUT_S`
           (180 s), and then proceeds.

        Underneath both is the sidecar gate, `<sidecar>.lock`: held by
        every one of the **six** places that append a frame and commit
        its row (ingest's pixel, nested-icon and waveform frames,
        `persist_blob`, `persist_pixel_data`, `save_all`) and by this
        method across `compact_sidecar()` **plus** `_rewire_sidecar_
        loaders`. A frame writer that starts after the check below no
        longer runs concurrently with the rewrite: it blocks on the
        gate and lands in the compacted file. A writer that cannot take
        the gate within the deadline raises `RuntimeError` naming the
        lock file; a background save that expires that way is logged
        as `Background save failed` with its instances left dirty for
        the next save (owner's decision C1). The gate is cross-process
        (`fcntl.flock` on a stable path beside the sidecar), which is
        what reaches the spawned redaction workers.

        **The #295 refusal, kept, and narrower than it reads (#320).**
        What it refuses is a save **queued on the persistence manager**
        -- after the synchronous save below, `has_pending_saves()` is
        consulted and a `RuntimeError` is raised if anything is in that
        manager's queue or in-flight set. It does **not** see a
        `Session.save(sync=True)` running on another thread, which
        executes `save_all` on its *caller's* thread and enters neither
        structure, nor a redaction's `store.persist_pixel_data(...)`,
        which is not a save at all; measured `False` in every ordering
        `tests/test_compaction_races_a_concurrent_write.py` forces. That
        population is now stopped by the gate instead. The rewiring
        rebinds each loader under `SqliteStore._pixel_swap_lock`, so no
        reader can land between the offset and the length assignments
        (#295), and the gate is taken *outside* that lock.

        **The liveness cost, stated.** The gate is held for the whole
        rewrite (0.217 s/GB live on local SSD measured). A `close()`
        whose persistence worker is queued behind a compaction longer
        than `_SHUTDOWN_JOIN_TIMEOUT_S` (30 s) has #314's wedged-worker
        machinery misfire on a healthy compaction; the same ordering
        used to corrupt the save. Loud and late beats silent and wrong;
        the structural fix (a two-phase compaction holding the gate
        only for the tail) is a filed follow-up.
        """
        if hasattr(self, 'store_backend'):
            print("Beginning Sidecar Compaction (this may take a while)...")

            # The refusal (#368) comes FIRST, before the leading save,
            # holding nothing, and is held through the rewire. It sat
            # between the save and the rewrite until the review of PR
            # #385 showed the window that leaves: a pass that opens
            # after the save's rows are written and closes before the
            # rewrite is admitted with its `instances` rows on the old
            # UIDs and its blob rows on the regenerated ones (`redact()`
            # does not save at its end), and `_read_blob_index`'s
            # EXISTS predicate reclaims every worker frame. Taken here,
            # a pass opening at any point of this method waits at its
            # own SH until the rewire is done. `LOCK_NB` still: the
            # refusal is an answer, not a wait (owner's A1). Lock order
            # stays acyclic -- EX holding nothing, then the gate via
            # site 6 and below; passes take SH holding nothing; nothing
            # takes the pass-lock under the gate any more.
            with self.store_backend._refuse_while_pass_open():
                # 1. Sync DB so compaction knows true state
                self.save(sync=True)

                # The #295 check, between the save and the rewrite. What
                # it does NOT cover: `has_pending_saves()` reads the
                # persistence manager's queue and in-flight set, so a
                # `save(sync=True)` on another thread and a redaction's
                # `persist_pixel_data` are both invisible to it, measured
                # `False` in every corrupting ordering (#320). The gate
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

                # The gate (#368), taken AFTER the leading save above --
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
        # `(uid, kind)` instead (#183).
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

        Args:
            updates: `{sop_instance_uid: (offset, length)}` for pixels,
                as `compact_sidecar()` returns it.
            wave_updates: the same for waveforms, read from the blob
                table because `compact_sidecar`'s map is pixels-only.
            nested_updates: `{(uid, kind): (offset, length)}` for nested
                pixel payloads (#183). Keyed by `(uid, kind)` because one
                instance can carry several, which is the same reason
                `compact_sidecar`'s UID-keyed map cannot carry them.

        Returns:
            int: how many loaders were rebound.

        Extracted from `compact()` so it can be exercised without
        running a compaction -- through the front door, `compact()`'s
        own `save(sync=True)` also takes `_pixel_swap_lock`, so a test
        could not tell this loop's acquisition from the save's.

        **The lock is taken per instance, around both rebinds
        together.** `offset` and `length` are two assignments and a
        reader landing between them gets the wrong bytes or runs off the
        end of the sidecar -- the same shape as #274, where a loader and
        its hash were swapped separately and the instance read back
        unredacted pixels under a full redaction attestation. Per
        instance rather than once around the loop because the offset map
        is fully in hand before the loop starts, so the critical section
        is two attribute assignments and never spans a sqlite read.

        The lock is a **leaf** here: nothing is acquired inside it, and
        it is never co-held with `_memory_lock` or `_audit_write_lock`.
        (This claimed to preserve a `_pixel_swap_lock` ->
        `sidecar._lock` order. That order named a lock nothing ever
        acquired, and it is gone -- #366.)
        """
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
                                # (#293). Clearing here would make an
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
        """Drop stored private-tag rows for a store de-identified before 0.9.1.

        **Opt-in repair for one specific history; read before calling.**
        Before #158, private (odd-group) tags written to the
        `instance_attributes` tier were never read back, and the writer
        did not mirror deletions -- so a session that ran
        `remove_private_tags: true`, anonymized and saved deleted the
        vendor block from the graph and left every row of it in the
        store, inert. #158 wired the tier into hydration (the fix that
        makes `remove_private_tags: false` survive a reload), and the
        first open of such a store after upgrading puts the stripped
        rows back on the graph; an export taken from that session
        carries them (#172).

        The library cannot decide which rows are stale: a stale row and
        a legitimate one are byte-identical, and the tier holds values,
        not tombstones. What the store does record is what every
        pre-#158 session actually saw -- the core `attributes_json`,
        which WAS the whole graph while nothing read the tier. This call
        opts into reading it that way: it deletes every tier row whose
        tag is absent from its instance's core stored attributes,
        removes the same tags from the live in-memory graph (undoing the
        resurrection this session's open performed), and writes one
        `RECONCILE_PRIVATE` audit row per affected instance so the
        repair is in the compliance trail. The graph edit is direct --
        no `set_attr`, no revision bump -- because the store and graph
        change together and agree afterwards; nothing reads as unsaved
        and stored PHI statuses survive, exactly as hydration's own
        writes do.

        **The cost, stated plainly (same grain as `redact(force=True)`:
        the repair exists in the API, nothing changes silently, and the
        caller chooses).** For a store that legitimately keeps its
        vendor block -- `remove_private_tags: false`, saved by 0.9.1 or
        later -- the tier IS the private data, held out of the core by
        design, and this call deletes all of it. Call this only for a
        store you KNOW was de-identified before upgrading. A site unsure
        of its history should re-run the privacy pipeline instead:
        since #158 the writer mirrors deletions, so anonymize + save
        heals the tier without trusting the core.

        There is deliberately no schema-version stamp deciding this
        automatically: which answer a store needs depends on what the
        site ran, which the site knows and no stamp records -- and the
        version-stamped attestation is already filed to be decided once
        for #168, #172 and #237 together (see #237's CHANGELOG entry).

        Returns:
            int: `instance_attributes` rows deleted -- rows, not tags
            (a VM=3 value is three rows, and a tag holding an empty
            value is the one placeholder row that carries its zero
            length -- #328). 0 means the tier already agreed with the
            core and nothing changed.
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
        Ingests DICOM files from a directory into the session store.

        Recursively scans the provided directory for valid DICOM files.
        Files are parsed and organized into the Patient -> Study -> Series -> Instance hierarchy.
        This operation automatically saves the session state upon completion.

        A file that cannot be ingested does not raise: it is counted in
        the returned summary and gets an `ERROR` audit row naming the
        path and the reason, which the compliance report surfaces and
        which bars the `PASS` grade -- the same treatment a failed
        export write gets (#181, #211). Check the return value: a run
        that rejected files completes normally.

        A file whose SOP Instance UID an instance in this session already
        holds -- ingested earlier in this call, by an earlier call, or
        loaded from the store -- is **declined** (#431): the first
        instance is kept, the file is not read into the store, it is
        counted in `IngestSummary.declined`, and a `WARNING` audit row
        names the UID, the file, and the file the instance was ingested
        from. The store is keyed on that UID, so admitting the second
        used to let the next save overwrite the first. **Which file is
        kept is promised** (#450): an instance the session already holds
        is always kept over a new file, and among the files new to this
        call, the one whose path sorts first is kept -- whatever order
        the filesystem lists them in. The sort is on the path string as
        walked (`os.path.join` of the directory and the name), the one
        the `WARNING` row prints. A declined file is not recorded as
        imported, so ingesting the same folder again declines it again.

        Neither `ISOCENTER_FORCE_THREADS` nor
        `ISOCENTER_MAX_TASKS_PER_CHILD` has any effect here: `ingest()`
        runs on the session's own process pool, which has no threads
        mode and never recycles a worker. Each call that has files to
        read logs one `WARNING` naming whichever is set (#393, #471);
        with both set, #185's line is that one.

        `ISOCENTER_MAX_WORKERS` **does** reach this call. It is
        re-resolved here and the pool is rebuilt when the width has
        changed since it was built, so the lever narrows an ingest
        whenever it is set and not only at `Session()` (#511). An
        unchanged width rebuilds nothing. While another `ingest()` is
        running on that pool in this session -- two threads can overlap,
        since the pass-lock is taken shared -- a changed width is
        reported in one `WARNING` and this call runs at the pool's
        current width, because shutting the pool down would cancel the
        peer's queued files; see `_ingest_executor`.

        Args:
            directory (str): The path to the directory containing DICOM files.

        Returns:
            IngestSummary: how many files reached the store, and
                `(path, reason)` for each one that did not. Returned
                nothing until #211, which left a caller no programmatic
                way to learn that a directory ingest silently rejected
                some of its files.

        Raises:
            RuntimeError: If the ingest cannot start within
                `_SIDECAR_GATE_TIMEOUT_S` (180 s) because a `compact()`
                is still saving or rewriting the sidecar (#368).

        **Concurrency (#368).** An ingest holds the sidecar pass-lock,
        shared, for the whole import. While it is held, `compact()` on
        any thread of this session **raises**: ingest appends each
        result's frames before the `instances` row that references them
        exists, and a compaction in that window reclaimed them as
        orphans. While a `compact()` is saving or rewriting -- it holds
        the pass-lock exclusive from before its leading save until its
        loaders are rewired -- this call **waits**
        (bounded as above) and then proceeds. Each frame is appended
        under the sidecar gate; a result whose write cannot get the gate
        in time is rejected like any other failed file, with an ERROR
        audit row naming the path and the reason.
        """
        print(f"Ingesting from '{directory}'...")
        # The pass-lock (#368), shared, around the import and not the
        # save after it (owner's decision D1). Ingest appends every
        # result's frames -- and commits the nested-icon and waveform
        # blob rows -- before the `instances` row that references them
        # exists, so a compaction inside this call reclaimed freshly
        # ingested frames as orphans. While this is held `compact()`
        # refuses; behind a running compaction this waits (bounded)
        # before the first worker is dispatched.
        # Outside the pass-lock, and entered before it: this re-resolves
        # `ISOCENTER_MAX_WORKERS` and rebuilds the shared pool when the
        # width has changed (#511), and its own lock is never held while
        # the pass-lock is taken. The pool it yields is used as given --
        # `self._executor` is deliberately not read again below, so a
        # peer ingest that resizes between here and the dispatch cannot
        # pull this call's pool out from under it.
        with self._ingest_executor() as executor:
            with self.store_backend._hold_pass_lock():
                # Pass Sidecar Manager for eager pixel writing
                summary = DicomImporter.import_files(
                    [directory],
                    self.store,
                    executor=executor,
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
        if summary.failed:
            # Declined files are new files this call read, so they are
            # in the total; leaving them out read "1 of 2" for three.
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
        Loads a configuration file into memory without applying it.

        This allows the user to validate the configuration or run a preview using
        `preview_config()` before performing any destructive actions.

        Args:
            config_file (str): Path to the YAML configuration file.

        Raises:
            FileNotFoundError: If `config_file` does not exist.
            ValueError: If the file fails validation -- not `.yaml`/`.yml`,
                YAML syntax, a root that is not a mapping, an unknown
                `privacy_profile`, an unknown `action`, a `phi_tags`,
                `date_jitter` or `machines` of the wrong shape, or a rule
                `_validate_rule` rejects. Either way the configuration is
                exactly what it was before the call.
        """
        get_logger().info(f"Loading configuration from {config_file}...")
        print(f"Loading configuration from {config_file}...")

        # No `try`, and nothing assigned until the loader has returned
        # (#456). This caught every exception, printed `Load failed` beside
        # an ERROR log of the same text, reset `rules`, `phi_tags` and
        # `privacy_profile` to empty, and returned normally -- so a file
        # that failed validation wiped the policy the session already had
        # and the caller was told nothing. The loader validates every
        # shape it returns, so the prints below cannot fail after the
        # assignments either (`date_jitter: soon` used to, leaving half
        # of a failed file in the session).
        (tags, rules, jitter, remove_private,
         profile) = ConfigLoader.load_unified_config(config_file)

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
            get_logger().error("Failed to write scaffold: %s", describe_exception(exc))

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

        Every entry of the session's policy whose action differs from the
        basic profile's -- the scaffold sets `privacy_profile: basic`, so
        a line repeating the profile would change nothing. On a bare
        session the policy is the floor, and the difference is exactly
        `profiles.RESEARCH_DEFAULTS`: a jittered study date, and sex and
        age kept.

        Derived rather than listed (#495). The research defaults used to
        live here as their own table (`_default_action_for_tag` and a
        supplement of names) beside a floor that did not exist, so the
        scaffold and what a bare session applied could not be checked
        against each other. Now the floor is the one table and this is a
        diff of it, so a bare session's scaffold loads back to exactly the
        floor. It is exact only because the policy it diffs is a superset
        of the basic profile: a session under `privacy_profile: none`
        (or one whose basic tags were deleted) is still scaffolded under
        `basic`, and its file reloads with the basic profile beneath its
        own tags -- more protection than the session had, never less.

        A plain-string value is a tag's display name and leaves the
        inspector's action at REPLACE (`PhiInspector.__init__`), so it is
        written structured, as the REPLACE it is.
        """
        structured = {}
        for tag, val in self.configuration.phi_tags.items():
            rule = dict(val) if isinstance(val, dict) else {
                "name": str(val), "action": "REPLACE"}
            base = BASIC_PROFILE.get(tag, {}).get("action")
            if str(rule.get("action", "REPLACE")).upper() != base:
                structured[tag] = rule
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

        # A scan ENDS by advancing `_revision` on every entity it
        # touched (`_record_scan_results` -> `record_phi_status`), and
        # `save()` without `sync=True` returns with `save_all` still
        # running on the persistence manager's thread. Since #287 that
        # window is as long as all of the save's pixel I/O. An instance
        # dirtied inside it is dropped from the frozen dirty set, left
        # dirty, and never saved -- `close()` shuts the manager down and
        # does not enqueue a save, so nothing says so. The documented
        # order in README and the quickstart is `save()` then `audit()`,
        # which is exactly this window (#297).
        #
        # Entry is the right place ONLY because nothing inside `audit()`
        # enqueues a save. If that ever changes, this moves to
        # immediately before `_record_scan_results`.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()

        # Default to current config
        tags_to_use = self.configuration.phi_tags

        if config_path:
            # The same loader, and the same exceptions, as `load_config`
            # (#456). A fallback here read the file as a plain tag list
            # whenever the unified loader refused it, so a rule with no
            # serial number -- rejected one call earlier -- was audited
            # against happily, a `.json` file `load_config` refuses was
            # accepted, and a root-level tag file loaded tags the scan
            # then never matched.
            tags_to_use, _, _, _, _ = ConfigLoader.load_unified_config(config_path)

        # The project secret, once, in the parent, before any work: a
        # store holding dates shifted under a secret it no longer has
        # refuses here, with nothing scanned. After the config is resolved,
        # not before: on a store with no secret this call generates and
        # commits one, and a config that then raised would have left the
        # store changed by a call that did nothing (#456). Read from the
        # store rather than held on the session, so a secret loaded between
        # two calls is the one used. Workers get it by value in their tuple.
        project_secret = self.store_backend._project_secret_for_use()

        # Uses IsocenterConfiguration derived tags
        inspector = PhiInspector(config_tags=tags_to_use,
                                 remove_private_tags=self.configuration.remove_private_tags,
                                 project_secret=project_secret)
        if not inspector.phi_tags:
            # Reachable only when a config said `privacy_profile: none`
            # and listed no tags: a session with no config applies the
            # floor policy (#495). The scan still runs the hardcoded
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

        `redact()` is the one edit that keeps an instance's status: an
        instance REMEDIATED or CLEARED before the pass reads the same
        after it, provided nothing but redaction's own writes changed
        (#486; confirmed by the owner on 2026-09-11). See `PhiStatus`.

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
                Each finding's `entity` is the live `Instance` in
                `session.store`, whether the scan ran in threads or in
                processes, or `None` when that instance cannot be found in
                the graph; never a worker's copy (#412). Its `failures`
                lists `(entity_uid, reason)` for each instance whose pixels
                could not be loaded or whose OCR raised on any frame, and a
                WARNING gives the count (#423). Each failure is also written
                as one `WARNING` audit row naming the instance and the
                reason, so the compliance report grades the run
                `REVIEW_REQUIRED` (#479). An instance with no pixel element
                is neither scanned nor a failure. A worker process runs the
                `pytesseract.pytesseract.tesseract_cmd` the caller set, the
                binary the up-front check probed (#458).

        Raises:
            RuntimeError: `pixel_analysis.OcrUnavailableError` when the `ocr`
                extra is not installed or the `tesseract` binary does not
                answer in the calling process, before any worker is
                dispatched and before the graph is read (#422).
                `pixel_analysis.PixelScanError`, carrying `.failures` and
                `.attempted`, after the pass, the audit rows and the
                warning, when at least one instance failed and none could
                be read (#423, #479).
        """
        # First, before the graph is read: a scaffolded config would
        # otherwise answer "nothing to scan" without OCR, and the missing
        # extra would surface only once zones were filled in (#422).
        pixel_analysis._require_ocr("scan_pixel_content()")  # pylint: disable=protected-access
        # Right after the probe, so the workers run the binary it checked
        # (#458); see `_caller_tesseract_cmd`.
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
            # to say about it (#481).
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
        # the very instances it could not read (#481, #479).
        self._pixel_scans.append(PixelScanSummary(
            serial_number=serial_number, attempted=len(worker_items),
            read=read, unread=len(failures), findings=len(all_findings),
            skipped=skipped_count))

        # Unconditionally, in both strategies: the worker strips the
        # entity, and this is the one path that puts it back, so
        # `PhiFinding.entity` has one meaning however `run_parallel()`
        # resolved (#412). Deliberately not `_record_scan_results`: OCR
        # findings say nothing about an entity's metadata PHI status.
        self._rehydrate_findings(all_findings)

        # Before the warning and the raise, so every exit from here --
        # the report returned or `PixelScanError` -- leaves the rows (#479).
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
            # `.save()` writes to `configuration.config_path` and returns
            # silently when that is unset -- and only `load_config()` sets
            # it, so a session configured by `create_config()` reaches
            # here with nothing to save to. The tip says so rather than
            # naming the attribute and hoping: swapping a loud
            # AttributeError for a quiet no-op would be a worse tip than
            # the wrong one it replaces (#234).
            print("Tip: Run .scan_pixel_content() again to verify fix, "
                  "then .configuration.save() to persist (set "
                  ".configuration.config_path first if no config file was "
                  "loaded -- save() returns silently without one).")

        return count

    def discover_redaction_zones(self, serial_number: str, sample_size: int = 50, min_confidence: float = 80.0):
        """
        Scans a random sample of instances from a specific machine to discover
        common locations of burned-in text.

        Returns:
            DiscoveryResult: Object containing all detected text candidates.
            Call .to_zones() on the result to get grouped redaction zones.

            `n_sources` counts only the sampled instances that were read
            (at least one frame through OCR), so an instance that could
            not be read does not dilute a zone's occurrence rate. Each one
            that failed is logged at ERROR and counted in a WARNING (#423),
            and written as one `WARNING` audit row naming the instance and
            the reason, which grades the run `REVIEW_REQUIRED` (#479). A
            worker process runs the caller's `tesseract_cmd` (#458).

        Raises:
            RuntimeError: `pixel_analysis.OcrUnavailableError` when the `ocr`
                extra is not installed or the `tesseract` binary does not
                answer, before any worker is dispatched and before the graph
                is read (#422). `pixel_analysis.PixelScanError`, carrying
                `.failures` and `.attempted`, after the pass, the audit rows
                and the warning, when at least one sampled instance failed
                and none could be read (#423, #479).
        """
        # First, and read through the module at call time -- never a copy
        # of `HAS_OCR` imported into this module, which a patch or a later
        # install would not reach (#422).
        pixel_analysis._require_ocr("discover_redaction_zones()")  # pylint: disable=protected-access
        # Not only for show: `force_threads=True` below does not hold under
        # `ISOCENTER_MAX_TASKS_PER_CHILD` (#458; see `_discover_worker`).
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
            # looked, and counting it in `n_sources` lowered every zone's
            # occurrence rate (#423).
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

        # Before the warning and the raise, as in `scan_pixel_content()`
        # (#479). Discovery reads nothing into the configuration, but a
        # session whose discovery could not read an instance has the same
        # gap in what it looked at, and the ruling covers both methods.
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
        Returns a Pandas DataFrame containing flattened metadata for the current cohort.
        Useful for analysis and QA.

        Args:
            expand_metadata (bool): If True, includes all DICOM attributes as columns.
            patient_ids (List[str], optional): Restrict the report to these
                Patient IDs. ``None`` means every patient in the session.
                An empty list matches nobody -- it is a filter that
                selected nothing, not an absent filter.
        """
        import pandas as pd
        rows = []
        for p in self.store.patients:
            # `is not None` rather than a truth test: `[]` must exclude
            # everyone. A caller computing a cohort that came back empty
            # would otherwise export the whole dataset.
            if patient_ids is not None and p.patient_id not in patient_ids:
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
        """Says, per `SCAN_GAP` row, whether the element is still held.

        The row is written by `DicomImporter` at ingest, where nothing
        knows what the export will carry: `remove_private_tags` is
        applied later, by the sweep in `PhiInspector`, and it deletes
        the element from the object graph. So the row states ingest
        knowledge and this resolves the rest of it (#167).

        The graph is a sound oracle for the question. `remove_private_
        tags` has exactly one consumer -- `PhiInspector` -- and the
        exporter applies no private filtering of its own, so an element
        still in the graph is one the next `export()` writes.

        This is a presence test, not a second classification. It never
        re-runs `_sequence_from_un_bytes`: which elements the gate
        refused was settled once, at ingest, and is read back off the
        row rather than decided again (#84).

        Args:
            rows (list): `(timestamp, entity_uid, details, element_tag)`
                from `SqliteStore.get_audit_scan_gaps`.

        Returns:
            list: The same rows with `element_tag` replaced by a
            disposition -- `GAP_REMOVED`, `GAP_RETAINED` or
            `GAP_UNRESOLVED`.
        """
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
                        unattested) -> List[str]:
        """Why a run is not PASS, one entry per term of the grade (#481).

        The grade IS this list: `generate_report` grades PASS exactly when
        it is empty. It used to be a boolean, with section 5 rendering a
        count nothing set, so every report said "Identified Issues: 0" --
        beside a REVIEW_REQUIRED whose section 4 listed the issue. One list
        means a new grade term cannot move the grade without also appearing
        in section 5, because there is no second expression for it to live
        in. Every term is here, including the two with no row anywhere else
        in the report: an empty audit trail and an unattested verb.
        """
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
        return review_reasons

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

        # Descriptor damage a pre-fix release persisted (#186, #214).
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

        # Audit rows a failed batch write dropped (#219). Filed as an
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
        # a report generated first cannot contain them and used to
        # grade PASS on a run that then dropped a private element --
        # same session, same loss, PASS or REVIEW_REQUIRED depending
        # only on call order (#153). The audit log is the arbiter, not
        # a session flag: an EXPORT row (#166) survives a session
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
        # satisfy. `deid_method` used to be a dataclass default reading
        # "Safe Harbor (Basic Profile)" that nothing assigned, so every
        # report -- including a bare session's, scanning six tags --
        # asserted HIPAA Safe Harbor above a DPO signature line.
        # The tag count is what `audit()` scans with: the session's
        # policy, with no fallback. This fell back to the shipped
        # `phi_tags.json` when `phi_tags` was empty, while `audit()` used
        # the empty dict -- so a bare session's report said "6 tag rules"
        # over a scan that applied none (#495). A bare session now
        # carries the floor; an empty policy means `privacy_profile:
        # none` with no tags, and "0 tag rules" is then the truth.
        effective_tags = self.configuration.phi_tags

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

        # 4. Grade the run
        #
        # A dropped *private* element fails the grade; a dropped
        # standard one does not. The asymmetry is deliberate rather than
        # a rule half-applied, and is argued once -- CHANGELOG.md, #146.
        # The one loss parity sat badly on -- the discarded waveform
        # multiplex group, standard-group and not remotely routine -- is
        # scoped SIGNAL by its emitter since #150 and grades here too.
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
        # not carry, which is the same test #146 applies to DATA_LOSS --
        # and grading it REVIEW_REQUIRED made the report disagree with
        # itself, because a *parseable* private sequence swept by the
        # same default configuration graded PASS. Measured on both:
        # same config, both absent from the export, PASS and
        # REVIEW_REQUIRED. That asymmetry had no argument behind it.
        #
        # `GAP_UNRESOLVED` grades like a retained one. A disposition
        # nothing could establish is not a clean one (#167).
        open_gaps = [row for row in scan_gaps if row[3] != GAP_REMOVED]

        # A declined remediation grades exactly like an open gap, and
        # for the same reason: the value it targeted is **still in the
        # graph** and reaches the exported file, so a report that graded
        # PASS over one would be asserting the removal happened. Graded
        # on the row existing rather than on any property of it -- there
        # is no disposition to resolve here, because unlike a `SCAN_GAP`
        # nothing downstream removes a value a remediation declined to
        # touch (#301).
        #
        # An empty date writes no row at all, so this cannot fire over a
        # graph with nothing wrong in it; that gate is in
        # `_apply_single_remediation`, where the value is in hand.

        # Action-specific evidence (#254). The `audit_summary` arm below
        # asks whether the audit log heard about *anything*; this asks
        # whether it heard about what this session did. Without it, a
        # session that redacted and whose REDACTION rows were lost to a
        # second defect (a dropped batch of the #219 shape) graded PASS
        # on the strength of its other rows -- #247's second reading,
        # one defect further away. `_actions_performed` is the session's
        # transient memory of its own verbs; see `__init__` for why it
        # is deliberately not persisted.
        from .remediation import REMEDIATION_ACTION_TYPES
        expected_evidence = {
            "REDACTION": frozenset({"REDACTION"}),
            "ANONYMIZE": REMEDIATION_ACTION_TYPES,
        }
        unattested = [verb for verb in sorted(self._actions_performed)
                      if not expected_evidence[verb] & audit_summary.keys()]

        # The grade IS this list: PASS exactly when it is empty (#481). See
        # `_review_reasons` for why it is a list and not a boolean.
        # Keyword-only: six lists in a row is an easy pair to transpose,
        # and a transposed pair would still grade -- it would name the
        # wrong section.
        review_reasons = self._review_reasons(
            audit_summary=audit_summary, exceptions=exceptions,
            graded_losses=graded_losses, open_gaps=open_gaps,
            declined_remediations=declined_remediations,
            unattested=unattested)

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
        )

        renderer = get_renderer(format)
        renderer.render(report, output_path)

    @staticmethod
    def _manifest_anonymized(patient, study, instance) -> bool:
        """The manifest's `anonymized` for one instance (#486).

        True when the patient, the study and the instance each carry
        REMEDIATED or CLEARED at their current revision: the last
        tag-policy scan left nothing unremediated on any of the three, and
        nothing has edited them since. `phi_status` reads UNSCANNED for an
        entity edited after its scan, so the revision check is structural
        rather than repeated here.

        **The series is deliberately not consulted.** The inspector never
        scans one (`_record_scan_results` leaves it alone), so it is
        UNSCANNED in every session and would make every item False.

        **REMEDIATED is not required anywhere.** A re-audit of an
        anonymized graph records CLEARED over it, and a rule that required
        it would call a re-checked graph un-anonymized. The consequence is
        that an input the scan found clean reads True after `audit()`
        alone -- stated where the key is documented, not hidden.

        **The study matters.** A declined study-date remediation leaves the
        study IDENTIFIED while its instances read CLEARED; consulting the
        instance alone would say True over a date that reaches the export
        unshifted.
        """
        return all(entity.phi_status in (PhiStatus.REMEDIATED, PhiStatus.CLEARED)
                   for entity in (patient, study, instance))

    def generate_manifest(self, output_path: str, format: str = "html") -> None:
        """
        Generates a visual (HTML) or machine-readable (JSON) manifest of all instances.

        This manifest lists every SOP Instance currently tracked in the session,
        along with its file path and key metadata (Modality, Manufacturer, etc.).

        Each JSON item's `anonymized` is True when the last tag-policy PHI
        scan left no identifier unremediated on that instance's patient,
        study or instance, and none of the three has been edited since.
        It is not "`anonymize()` ran" -- a clean input reads True after
        `audit()` alone -- and it says nothing about burned-in pixel text
        (#486). See `docs/api/stability.md`.

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
                            model_name=model,
                            anonymized=self._manifest_anonymized(p, st, inst),
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
                        *,
                        verbose: bool = True,
                        tags_to_lock: Optional[List[str]] = None
                        ) -> Union[List["Instance"], LockingResult]:
        """
        Securely embeds the original patient name/ID into a private DICOM tag.

        This mechanism allows for "Reversible Anonymization". The original identity
        is encrypted using a symmetric key and stored in a private attribute
        before the visible public attributes are anonymized.

        Must be called BEFORE anonymization/redaction if recovery is required.

        A list of patient IDs, a `PhiReport` or a list of findings is
        dispatched to `lock_identities_batch()` with the same `persist`,
        `verbose` and `tags_to_lock`; chunked persistence
        (`auto_persist_chunk_size`) is that method's own argument, because
        it means nothing for one patient. Until 0.9.4 the batch loop
        hardcoded `persist=False, verbose=False`, so the README's form
        with `persist=True` added -- `lock_identities(report,
        persist=True)` -- wrote nothing and said nothing (#379, Q10).
        Until 0.9.4 this method also took `**kwargs` and forwarded them,
        and the batch method did not accept `tags_to_lock`, so the call
        the README teaches -- `lock_identities(report,
        tags_to_lock=[...])` -- raised `TypeError`; a misspelled keyword
        on the single-patient path was swallowed (#379, Q7). `verbose`
        and `tags_to_lock` are keyword-only because the third positional
        slot used to be `_patient_obj`: a caller still filling it would
        otherwise have a `Patient` silently read as `verbose`.

        Args:
            patient_id (str): The ID of the patient to preserve (or a list/report for batch processing).
            persist (bool): If True, writes changes to the database immediately.
                            If False, returns modified instances (useful for batch buffering).
            verbose (bool): If True, logs debug information.
            tags_to_lock (List[str], optional): The tags whose original values
                are embedded. When omitted: PatientName, PatientID,
                PatientBirthDate, PatientSex and AccessionNumber.

        Returns:
            Union[List[Instance], LockingResult]: A list of modified instances.
        """
        if not self.reversibility_service:
            raise RuntimeError(
                "Reversible anonymization not enabled. Call enable_reversible_anonymization() first.")

        # Dispatch to batch method if a list is provided
        if isinstance(patient_id, (list, tuple, set)) or hasattr(patient_id, 'findings'):
            return self.lock_identities_batch(
                patient_id, persist=persist, verbose=verbose, tags_to_lock=tags_to_lock)

        patient = next((p for p in self.store.patients if p.patient_id == patient_id), None)
        if not patient:
            # The ID is not logged: it may be an original Patient ID, and
            # the log file is not guarded as the store is (0.9.7).
            get_logger().error("lock_identities: no patient in the session "
                               "has the Patient ID given.")
            return LockingResult([])

        return self._lock_patient_identity(patient, persist, verbose, tags_to_lock)

    def _lock_patient_identity(self, patient: "Patient", persist: bool,
                               verbose: bool, tags_to_lock: Optional[List[str]]
                               ) -> LockingResult:
        """Embeds one resolved patient's identity token into every instance.

        The batch path already holds the `Patient` from its own O(1) map,
        so this takes the object: the O(N) lookup by ID lives in
        `lock_identities` alone. This used to be `lock_identities`'s
        `_patient_obj` parameter -- a private name in a public,
        soon-frozen signature.
        """
        patient_id = patient.patient_id
        if verbose:
            # Counts, not the ID: see `lock_identities`.
            get_logger().debug(
                f"Preserving identity for a patient of {len(patient.studies)} "
                f"stud{'y' if len(patient.studies) == 1 else 'ies'}...")

        modified_instances = []
        if tags_to_lock is None:
            tags_to_lock = list(_DEFAULT_TAGS_TO_LOCK)

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

        # A name or ID the first instance no longer carries is stashed from
        # the patient (#495), as the no-instances arm below always did. The
        # floor's instance rules remove those copies, so after an
        # instance-only anonymize() the copies are gone while the patient
        # still holds the originals, nothing reads as a replacement, and a
        # stash of the copies alone wrote a token holding only
        # {'0010,0040': 'O'} over the good one (review of #509). Where the
        # patient is itself a replacement, the refusal below names it.
        entity_fallback = {"0010,0010": patient.patient_name,
                           "0010,0020": patient.patient_id}
        if first_instance:
            for tag in tags_to_lock:
                val = first_instance.attributes.get(tag)
                if val is None:
                    val = entity_fallback.get(tag)
                if val is not None:
                    original_attrs[tag] = val
        else:
            # Fallback to Patient object properties if no instances (unlikely)
            if "0010,0010" in tags_to_lock:
                original_attrs["0010,0010"] = patient.patient_name
            if "0010,0020" in tags_to_lock:
                original_attrs["0010,0020"] = patient.patient_id

        # A replacement is not an identity to keep. Since #492 the
        # instance carries `anonymize()`'s replacement in its own tags,
        # so a lock taken after it -- the reverse of the documented
        # order, or a re-lock of an already-anonymized patient -- would
        # stash `ANONYMIZED`/`ANON_<hash>` over a good token, report
        # success, and `recover_patient_identity()` would then restore
        # the replacements everywhere while export prints that the
        # originals are recoverable. Before #492 the same call stashed
        # the originals by accident, because the instance still carried
        # them. Refused, naming the value, rather than skipped: a lock
        # that silently kept nothing is #399's shape again. The
        # predicate is `scan_patient`'s own: privacy's
        # `_is_replacement_name` / `_is_replacement_id`, not a copy.
        # A re-lock of still-original values is unchanged and is what
        # recovery answers with (#399).
        #
        # What is refused, exactly (#495): any value about to be stashed
        # that reads ANONYMIZED or starts ANON_ -- each tag's
        # first-instance copy, and for name and ID the patient's own
        # value where that copy is absent (above). Under the floor
        # `anonymize()` removes the instance's own name and ID, so a
        # refusal reading only the copies saw nothing: measured on
        # CT_small, bare session, lock -> anonymize -> lock again raised
        # nothing and wrote a token holding only {'0010,0040': 'O'} over
        # the good one -- #492's defect by another route. A copy that is
        # present is what gets stashed, so it alone is checked: a patient
        # reading ANONYMIZED beside copies that still hold the originals
        # has originals to stash.
        for tag, val in original_attrs.items():
            if _is_replacement_name(val) or _is_replacement_id(val):
                raise RuntimeError(
                    f"lock_identities: patient {patient_id!r} already "
                    f"carries a replacement in {tag} ({val!r}), so there "
                    "is no original identity left to stash. Lock "
                    "identities before anonymize(), and do not re-lock a "
                    "patient after it; the token this call would have "
                    "written is unchanged.")

        # Optimization: Encrypt once per patient
        token = self.reversibility_service.generate_identity_token(
            original_attributes=original_attrs)

        # Iterate deep
        for st in patient.studies:
            for se in st.series:
                for inst in se.instances:
                    self.reversibility_service.embed_identity_token(inst, token)
                    modified_instances.append(inst)

        if persist and modified_instances:
            self.store_backend.update_attributes(modified_instances)
            get_logger().info(
                f"Secured identity (tags: {list(original_attrs.keys())}) in "
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
        Batch process multiple patients to lock identities.

        Args:
            patient_ids (Union[List[str], PhiReport]): List of PatientIDs to process.
            auto_persist_chunk_size (int): If > 0, persists changes and releases memory every N instances.
                                           IMPORTANT: Returns an empty list if enabled to prevent OOM.
            tags_to_lock (List[str], optional): Passed to every patient's
                lock; `lock_identities()`'s five default tags when omitted.
            persist (bool): Passed to every patient's lock: each patient's
                instances are written as they are locked. With
                `auto_persist_chunk_size > 0` as well, an instance is
                written twice (with its patient, then with its chunk) --
                redundant, not wrong. Until 0.9.4 the loop hardcoded
                `False`, so `lock_identities(report, persist=True)` wrote
                nothing in silence (#379, Q10).
            verbose (bool): Passed to every patient's lock: one debug line
                per patient. Until 0.9.4 the loop hardcoded `False`.

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
        missing_ids = 0

        from tqdm import tqdm

        # Optimization: Create a lookup map for O(1) access
        patient_map = {p.patient_id: p for p in self.store.patients}

        with tqdm(start_ids, desc="Locking Identities", unit="patient") as pbar:
            for pid in pbar:
                p_obj = patient_map.get(pid)
                if p_obj:
                    # Forwarded, not hardcoded: a `PhiReport` is the
                    # README's form of `lock_identities`, and a loop that
                    # writes `persist=False` here turns `persist=True` on
                    # that call into one that writes nothing and says
                    # nothing (Q10).
                    res = self._lock_patient_identity(
                        p_obj, persist=persist, verbose=verbose,
                        tags_to_lock=tags_to_lock)

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
                    missing_ids += 1

        if missing_ids:
            # Counted, not named: see `lock_identities`.
            get_logger().error(
                f"lock_identities: {missing_ids} Patient ID"
                f"{'' if missing_ids == 1 else 's'} given matched no patient "
                "in the session (batch processing).")

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
                            in-memory instances for this patient. The restore
                            is recorded, so a later `save()` stores it, and a
                            patient already holding the restored Patient ID
                            is merged into whichever of the two was in the
                            session first (#552, #548).

        Raises:
            RuntimeError: With `restore=True`, when a patient holding the
                restored Patient ID was de-identified under a different
                date-offset scheme. Raised before anything is restored.
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
                # Asked before anything is written: the merge below
                # refuses a group mixing jitter schemes too, but after the
                # loop every instance already holds the original values,
                # and a refusal there would leave two patients with one
                # ID under two schemes in the graph (#548).
                self.store._refuse_a_merge_across_schemes(
                    renamed=(p, original_attrs.get("0010,0020", p.patient_id)))
                count = 0
                for st in p.studies:
                    for se in st.series:
                        for inst in se.instances:
                            for tag, val in original_attrs.items():
                                inst.set_attr(tag, val)
                            count += 1

                # Update Patient Object top-level properties if Name/ID changed
                before = (p.patient_name, p.patient_id)
                if "0010,0010" in original_attrs:
                    p.patient_name = original_attrs["0010,0010"]
                if "0010,0020" in original_attrs:
                    p.patient_id = original_attrs["0010,0020"]
                # Recorded, because `Patient` tracks no assignment: a
                # restore that marked nothing was skipped by the next save,
                # which then deleted the pseudonym's row and every study
                # under it (#552). It also retires the patient's stale
                # REMEDIATED -- a status recorded at an earlier revision
                # reads UNSCANNED, which is the truth for a patient holding
                # its original identifiers again.
                if (p.patient_name, p.patient_id) != before:
                    p.mark_modified()
                # A raw study for the restored ID, ingested before the
                # restore, is a second `Patient` holding it: the same
                # subject by construction, so the two are merged as
                # `anonymize()` merges them, or refused if they were
                # de-identified under different date-offset schemes (#548).
                self.store._merge_patients_sharing_an_id(
                    drain=self.persistence_manager.flush)

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

    def redact(self, show_progress=True, force=False):
        """
        Applies pixel redaction rules to the current session.

        Uses the currently loaded configuration (`self.configuration.rules`) to
        find and redact sensitive regions in the pixel data. This operation
        modifies the pixel data in memory (and via Sidecar for persistence);
        call `.save()` afterwards to persist it.

        Args:
            show_progress (bool): If True, displays a progress bar.
            force (bool): Re-redact instances whose
                `_ISOCENTER_REDACTION_HASH` already matches this
                configuration, instead of skipping them.

                This exists for one population: stores redacted with a
                rule carrying **two or more zones**, against a store that
                had been saved and reopened, on **0.9.0 or earlier**. That
                release applied only the last applicable zone (#229) and
                still wrote a full attestation, and the attestation is
                computed over the configuration rather than over the
                pixels -- so the corrected code reads a hash it agrees
                with and declines to look. `force=True` is what makes such
                a store repairable without hand-editing a private tag
                (#237). The burned-in identifier is still in the store's
                own pixels, so no source file is needed:
                `session.redact(force=True)` then `session.save()`.

                **Its cost, because you are choosing it.** Every instance
                the rules match is redacted again, and every one of them
                takes a **new SOP Instance UID**, a new exported filename
                (#78) and `file_path = None` -- which widens #238's
                exposure to instances that had already been redacted once.
                That is why it is opt-in rather than automatic: an
                attestation epoch would impose all of it on every store in
                existence, including the ones that were never damaged.

        Returns:
            int: How many instances had at least one configured zone
                applied to their pixels. An instance a rule *matched* but
                whose every zone fell outside the image is **not** counted;
                a zone with no area fails its instance and the pass raises
                `RedactionError` (#244), so it is never counted. Zero means
                nothing was redacted -- no rules loaded, no image matched
                one, every match was already redacted under this
                configuration, or no zone landed.

        Raises:
            RedactionError: If any instance's zone could not be applied.
                Raised at the *end* of the pass, not at the first failure:
                the instances that could be redacted are redacted, the
                failures are already `ERROR` rows in the audit log, and the
                console summary has been printed -- so a caller that catches
                it still has a correct object graph and a compliance report
                that grades `REVIEW_REQUIRED`. `.failures` carries
                `(sop_uid, detail)` per failed instance. A failed instance is
                left exactly as it was found: no `DERIVED` flag, no
                `_ISOCENTER_REDACTION_HASH`, nothing persisted, so a
                corrected configuration retries it.
            Exception: Whatever the redaction backend raised, after logging it.
                Redaction is the step that removes burned-in PHI, so a failure
                here must reach the caller. This used to be caught, printed as
                `Execution interrupted`, and followed by `Execution Complete`,
                which left a half-redacted session looking like a finished one.
            RuntimeError: If the pass cannot start within
                `_SIDECAR_GATE_TIMEOUT_S` (180 s) because a `compact()` is
                still saving or rewriting the sidecar. Raised before any
                worker is dispatched and before any UID is regenerated,
                so there is nothing to undo (#368).
            RuntimeError: On a `":memory:"` store when the environment
                asks for worker recycling -- `ISOCENTER_MAX_TASKS_PER_CHILD`
                -- because on 3.12, the floor, only
                `multiprocessing.Pool` recycles workers, and a spawned
                worker cannot reach an in-memory database, so
                every task would fail with `no such table:
                instance_blobs`. The message names the store, the
                variable, why processes cannot work here, and two
                remedies. Raised **after** the persistence drain and
                **before** the pass-lock: no task prepared, no SOP
                Instance UID regenerated, no pixel touched, no audit row,
                no attestation. Deliberately not `RedactionError` --
                nothing was attempted, so the exception meaning "these
                instances failed" would be the wrong report -- though
                `except RuntimeError` catches both. `ISOCENTER_FORCE_PROCESSES`
                does **not** raise: this call asks for threads per call
                and that request outranks the variable, so the pass is
                correct and gets a `WARNING` naming the variable instead
                (#400).

        **Concurrency (#368).** A pass holds the sidecar pass-lock,
        shared, from before the first worker runs until every outcome
        has been applied. While it is held, `compact()` on any thread
        of this session **raises** rather than reclaiming the frames
        the workers have committed under UIDs the store does not yet
        carry; while a `compact()` is saving or rewriting -- it holds
        the pass-lock exclusive from before its leading save until its
        loaders are rewired, so a pass cannot open and close inside
        that save unseen -- this call **waits**
        (bounded as above) and then proceeds. Each worker's sidecar
        write also takes the sidecar gate, so a worker that cannot get
        it in time comes back as a failed redaction with an ERROR audit
        row, not a silent skip.
        """
        if not self.configuration.rules:
            get_logger().warning("No configuration loaded. Use .load_config() first.")
            print("No configuration loaded. Use .load_config() first.")
            return 0

        # A redaction pass must not run concurrently with a background
        # save that is serializing the very pixels it is about to
        # replace: `save()` without `sync=True` returns with `save_all`
        # still running on the persistence manager's thread, against
        # these same instances (#274). The store's `_pixel_swap_lock` is
        # what protects direct `RedactionService` users; the pipeline
        # can simply refuse to open the window at all.
        if hasattr(self, 'persistence_manager'):
            self.persistence_manager.flush()

        # Resolved here, once, and carried to the pool. The console line
        # a few lines into `_apply_redaction_rules` names the strategy
        # off this object and `run_parallel` is handed the same object,
        # so the sentence a user reads and the pool that runs are two
        # readings of one decision rather than two guesses at it (#384).
        # A second reading of the environment at the print site would be
        # a second implementation of `_resolve_execution_choice`'s four
        # ranks, and a second implementation is a second thing that can
        # disagree.
        #
        # Threads for a `:memory:` store, asked for per call (#381). The
        # redaction worker is the only one that *writes to the store*
        # from inside the child (`execute_redaction_task` ->
        # `persist_pixel_data`), and `SqliteStore.__setstate__` hands a
        # spawned child `_memory_conn = None`, so a process opens a
        # fresh, empty in-memory database with no `instance_blobs`
        # table. Threads share the parent's connection. The argument
        # rather than `ISOCENTER_FORCE_THREADS`: the variable is
        # process-global and does not reach every pool (#390).
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
        # that cannot run is refused whether or not it had work to do
        # (#400).
        _report_processes_lever_on_a_memory_store(
            self.store_backend.db_path, strategy)

        service = RedactionService(self.store, self.store_backend)
        try:
            # The pass-lock (#368), shared, held from before the first
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
                return self._apply_redaction_rules(service, strategy, force)
        except Exception:
            get_logger().exception(
                "Redaction failed. Images already processed are still redacted "
                "in memory; the rest are untouched.")
            raise

    def _apply_redaction_rules(self, service, strategy, force=False):
        """Runs every loaded rule and applies the results to the store.

        Returns the number of instances whose pixels a zone was applied
        to. Raises on failure; the caller logs and re-raises. `force` is
        threaded into every task and read only by the attestation skip
        (#237).

        `strategy` is the `_Strategy` `redact()` resolved before it took
        the pass-lock. It carries the worker count, the progress-bar
        setting and the threads-or-processes decision, so there is no
        second spelling of any of them here and nothing to keep in sync
        with what the pool is built from (#384).
        """
        tasks = []
        get_logger().info("Analyzing workload...")
        for pass_key, rule in enumerate(self.configuration.rules):
            rule_tasks = service.prepare_redaction_tasks(rule, force=force)
            # The audit accounting's unit is the rule-pass, and the rule
            # index is the only thing that can key it: `load_config`
            # takes rules verbatim from user YAML with no serial
            # de-duplication, so two rules can share one serial spelling
            # -- keyed on the serial they collapse into one row carrying
            # the first rule's zone count (#247).
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
        # time here (#384). It says *what*, never *why*: a
        # `(threads: the store is in memory)` would put the four-rank
        # order in a second place, and the `why` is what #400's warning
        # and refusal are for, said only when it matters. The retired
        # `(Process Isolation)` named an implementation property rather
        # than the choice and left the recycling pool ambiguous; the
        # recycling pool is processes and says so.
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
        # that same value (#257), and one authority for "what was this
        # instance called before redaction" is what keeps the two sides
        # of the round-trip agreeing. Two rules on one instance put the
        # same key here twice; the map deduplicates to the one object.
        instances = {t['original_sop_uid']: t['instance'] for t in tasks}

        # Audit accounting per rule-pass (#247). `targeted` is countable
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
        # lost worker at the point of iteration, so the `for` below
        # terminated mid-pass: every mutation still queued was discarded
        # unapplied, no ERROR row was written for anything, and the caller
        # got a bare `BrokenProcessPool` instead of `RedactionError` (#232).
        # The strategy `redact()` resolved, handed over rather than
        # resolved again: `max_workers`, `chunksize`, `desc`, the
        # progress-bar setting and the threads-or-processes decision are
        # all inside it, and the console line above printed from the same
        # object (#384). The `:memory:` store's request for threads is on
        # the `_resolve_strategy` call in `redact()`, with the comment
        # explaining why it has to be an argument (#381, #390); it beats
        # `ISOCENTER_FORCE_PROCESSES` by the documented order and loses to
        # worker recycling, which is why that combination is refused
        # before this point rather than failing here (#400).
        # Before dispatch, not when each outcome lands: under threads the
        # worker writes to the live instance, so by the time the parent
        # sees an outcome the status it would read is already UNSCANNED.
        # See `capture_phi_status_for_redaction` for what is kept and why
        # (#486; confirmed by the owner on 2026-09-11).
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
        # the parent (#126) and before the failure raise below, exactly
        # as the serial path orders it: a caller that catches
        # `RedactionError` still holds a report whose section 2 accounts
        # for this run (#247). Before `scan_burned_in_annotations` too,
        # so a crash in the risk scan cannot cost the run its redaction
        # accounting.
        for acct in passes.values():
            service.record_redaction_pass(
                acct['machine_sn'], acct['zones'],
                acct['targeted'], acct['applied'])
        # Recorded beside the emitter, not at the top of `redact()`: the
        # grade demands REDACTION evidence only from a session that
        # would have emitted it, and a call with no rules loaded or no
        # matching images returned before this point (#254).
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
        # REVIEW_REQUIRED (#213).
        if failures:
            raise RedactionError(failures, len(tasks))
        return applied

    @staticmethod
    def _apply_redaction_outcomes(outcomes, instances, store_backend=None,
                                  passes=None):
        """Copies each worker's result back onto the in-memory instance.

        `instances` maps pre-redaction SOP UID to the instance in this
        process. Workers operate on copies, so a mutation that is never
        applied here is a redaction that did not happen.

        **The new identity is applied here too, and must be.**
        `execute_redaction_task` calls `regenerate_uid()` in the worker.
        Under threads the worker *is* the parent's object and the new UID
        lands by itself; under processes it lands on a copy and used to
        be dropped, so the same input produced a different SOP Instance
        UID -- and a different exported filename, since files are named
        by it -- depending only on which executor ran (#228).

        **The gate is the existence of the mutation.** It used to be
        `sop_uid != original_sop_uid`, because the worker built its
        mutation dict unconditionally and a mutation therefore came back
        for an instance whose zones all missed, which must keep its
        identity. That is no longer true: `execute_redaction_task` builds
        the dict only inside `if modified:`, so a mutation is now itself
        the claim that pixels changed and the inequality it was checked
        against became a condition that decided nothing (#235). Two gates
        on one question is how #228 happened; there is one.

        Three result shapes have to survive this, mirroring
        `_report_export_failures`: a `RedactionOutcome`, an `Exception` from
        a worker that died before it could answer, and anything else --
        including a bare `None`, which is a failure row rather than a silent
        skip. Tolerating `None` here would re-create exactly the conflation
        #213 removes, and would let a stubbed test go on passing against a
        contract it no longer implements.

        The audit write is **in the parent** and must stay there.
        `SqliteStore.__getstate__` drops the queue, the stop event and the
        audit thread, and `__setstate__` starts a *new* thread in the child
        that is torn down at pool shutdown without `stop()` -- so a queued
        row can be lost, and for a `:memory:` database the child writes
        nowhere at all. Same reason `_report_export_failures` runs here
        (#126).

        Returns:
            Tuple[int, List[Tuple[str, str]]]: how many mutations landed,
            and `(entity_uid, details)` per failure.
        """
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
                # with `yield_exceptions=True` (#232). There is no outcome
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
            # `sop_uid` is the **post**-redaction one, so the old
            # `or mutation.get('sop_uid')` fallback could never find
            # anything -- and now that `sop_uid` is assigned below, one
            # name meaning both the lookup key and the new identity is
            # how #228 reads wrong (#228).
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
                # redaction attestation (#274). A store-less call (unit
                # tests drive this method directly) has no second writer
                # to race, so it also needs no lock.
                lock = (store_backend._pixel_swap_lock
                        if store_backend is not None
                        else contextlib.nullcontext())
                # And the pixel-state leaf inside it (#434, Q6): the rebind,
                # the record clear and the null below land wholly before or
                # after a `set_pixel_data()` or `discard_pixel_data()` on
                # another thread. Nothing under it logs or takes a lock.
                with lock, entities.PIXEL_STATE_LOCK:
                    if loader:
                        instance._pixel_loader = loader
                        # And the label of the frame it reads (#482). The
                        # worker's read relabelled its own copy wherever
                        # the decode converted, so this is `RGB` over a YBR
                        # file that pydicom or the handler returned as RGB.
                        # Under threads the worker *is* this instance and
                        # the write repeats what is already there.
                        #
                        # Not `Instance._relabel_to_decoded_colour`, which
                        # is a *read's* relabel, called only by a read that
                        # publishes into an empty slot (#465): this copies
                        # a result across, beside the loader it describes,
                        # whatever is resident. (That helper used to take
                        # `PIXEL_STATE_LOCK` itself, which this block holds,
                        # and calling it here would have deadlocked; since
                        # #465 its caller holds the lock instead.) A dict
                        # write takes no lock and logs nothing, so the leaf
                        # stays a leaf. The `mark_modified()` after the
                        # lock moves the revision.
                        #
                        # Inside `if loader:`, beside the rebind, for the
                        # reason the `_pixel_descriptors_replaced` null
                        # below gives: the label describes the frame the
                        # loader reads, and a mutation carrying only a hash
                        # leaves the loader, and so the label, where they
                        # were. That placement is reasoned, not pinned: no
                        # test here sends a hash-only mutation that
                        # carries a label.
                        label = mutation.get('photometric_interpretation')
                        if label is not None:
                            instance.attributes["0028,0004"] = label
                        # The loader reads the worker's frame now, and the
                        # descriptors in `attributes` describe it: a record
                        # from a `set_pixel_data()` made before the pass
                        # describes the frame this rebind replaced, and a
                        # discard restoring it would put those over the
                        # redacted frame (#434). Inside `if loader:`, never
                        # one indent out, for the reason the null below
                        # gives: a mutation carrying only a hash leaves the
                        # loader on the frame the record describes.
                        instance._pixel_descriptors_replaced = None
                        # And drop whatever this process is still holding
                        # (#322). Under processes the worker redacted a
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
                        # check passing, which is #293's shape exactly.
                        #
                        # Inside the lock, and it belongs there: this is
                        # the same `_pixel_swap_lock` `_persist_pixels`
                        # reads under, so the null is atomic against a
                        # concurrent background save rather than a second
                        # window into it -- it strengthens #274. The
                        # `mark_modified()` below runs after the lock and
                        # the null does not depend on that ordering; do
                        # not tidy the null out to join it.
                        #
                        # Not a memory hazard, whatever the issue's
                        # caveat says: nulling the instance's reference
                        # does not invalidate an array the caller already
                        # holds. It only stops the *instance* serving
                        # pre-redaction pixels.
                        #
                        # This is the third `pixel_array = None` site, and
                        # `_persist_pixels`' `arr is None` arm enumerates
                        # them -- it is updated with this one.
                        #
                        # The null is also what makes the divergence flag
                        # irrelevant here, which is why the
                        # `_pixel_array_unwritten = False` that used to
                        # stand on this line is deleted rather than kept
                        # (#326): `unload_pixel_data()` returns True on a
                        # `None` array *before* it consults the flag, and
                        # `get_pixel_data()`'s loader arm clears it on the
                        # next read. Restore one without the other -- put
                        # the array back and leave the flag set -- and
                        # #293's silently-unfreeable instance returns:
                        # `release_memory()` refuses it for the rest of
                        # the session and only logs a count.
                        #
                        # On the threads path the worker mutated this very
                        # instance and its `finally` already discarded the
                        # array, so this null lands on `None`; both
                        # executors now leave identical state.
                        instance.pixel_array = None
                    if mutation.get('pixel_hash'):
                        instance._pixel_hash = mutation['pixel_hash']

            new_uid = mutation.get('sop_uid')
            if new_uid:
                # A `None`-safety guard on a `dict.get`, not a gate. The
                # gate was passed above: reaching here means the worker
                # returned a mutation, which it does only after
                # `regenerate_uid()` (#235). `and new_uid != sop` used to
                # stand here as #228's gate, back when the mutation dict
                # was built outside `if modified:` -- it is provably
                # always true now, and a condition that reads as a gate
                # while deciding nothing is the second answer #235 was
                # deferred to avoid.
                #
                # Re-widening the mutation construction is what that
                # inequality guarded against, and it is still guarded --
                # measured, by re-widening it on this tree.
                # `tests/test_redaction_attestation.py` catches it four
                # ways (the count, the absent attributes, the absent
                # exported elements, the risk-scan crash), and
                # `test_an_instance_nothing_was_applied_to_keeps_its_identity`
                # catches it on `file_path`: the two UID assignments below
                # become no-ops when `new_uid == sop`, but the third
                # statement beside them does not.
                #
                # Assign all three or none. Under processes the child
                # mutated a copy, so without this the parent kept the
                # source's identity while carrying `DERIVED` and a
                # Derivation Code Sequence -- and the blob the worker
                # persisted under the regenerated UID was stranded.
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
                # one (#237, #238). Same shape and same reason as the
                # `file_path = None` below it (#228).
                instance.attributes.setdefault(SOURCE_SOP_UID_ATTR, sop)
                # `regenerate_uid()` ends the same way, deliberately: the
                # instance no longer matches the file it was read from.
                instance.file_path = None

            instance.mark_modified()
            applied += 1
            # Attributed by the mutation's own `pass_key` rather than
            # by joining the UID back to a task: two rules matching one
            # instance produce two mutations under one pre-redaction UID,
            # and each belongs to its own pass's row (#247).
            if passes is not None:
                acct = passes.get(mutation.get('pass_key'))
                if acct is not None:
                    acct['applied'] += 1

        return applied, _report_redaction_failures(failures, store_backend)

    def redact_by_machine(self, serial_number: str, roi: List[int]):
        """
        Helper to run redaction for a single machine interactively.

        Temporarily overrides the configuration to apply a single ROI to a specific device.

        Args:
            serial_number (str): The device serial number to target.
            roi (List[int]): The Region of Interest as [y1, y2, x1, x2].

        Raises:
            RedactionError: Propagated from `redact()` when the zone could
                not be applied. The `finally` restores the original rules
                first, so the configuration is intact when it reaches the
                caller (#213).
            RuntimeError: Propagated from `redact()`, which refuses a
                `":memory:"` store whose environment asks for worker
                recycling (#400) and refuses a pass-lock wait that
                expires (#368). The `finally` restores the original rules
                first here too, so a caller who fixes the environment and
                retries is not also repairing their configuration.
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

        Two patients left holding one Patient ID -- a study ingested under
        a patient's original ID after that patient was anonymized -- are
        merged into whichever was in the session first, and the other is
        removed from `store.patients` (#548).

        Args:
            findings (List[PhiFinding], optional): Specific findings to clean.

        Returns:
            int: How many remediations were applied. Failures are logged and
                excluded, so a caller can tell a clean run from a partial
                one -- this used to be unreported, and the console line
                below printed the literal "None".
        """
        from .remediation import RemediationService

        if not findings:
            # Blind execution: scan with the current configuration, then
            # remediate. First, so the secret below is read after the
            # scan's own first use and its notices are written once.
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

        count = 0
        if findings:
            remediator._use_instance_owners(self._nested_finding_owners(findings))
            count = remediator.apply_remediation(findings)

        # A patient ingested under its original ID after that patient was
        # anonymized has just been given the pseudonym the stored patient
        # already carries: two objects, one row, and the next save's
        # scoped deletes removed each other's studies (#548). Merged
        # here, after every proposal in the report has been applied
        # under the scheme its scan stamped -- never inside
        # `apply_remediation`, which would move the five `mark_modified()`
        # line pins in `tests/test_remediation_invariants.py`. The drain
        # runs only when there is something to merge: `audit()` drains on
        # entry and this path, handed its findings, does not.
        self.store._merge_patients_sharing_an_id(
            drain=self.persistence_manager.flush)

        if count:
            # A nonzero count is the session claiming remediations were
            # applied, and an applied remediation queues its audit row
            # -- so this is where "performed" and "would have emitted"
            # coincide (#254). A call that applied nothing records
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
            **options: Passed through to the selected exporter. See
                `_export_dicom` for the DICOM format's options.

        Returns:
            The selected format's own result object. The DICOM exporter
            returns an `io_handlers.ExportSummary`, whose `written`
            counts the files that reached disk and whose `failures`
            names the instances that did not. `written` is counted over
            *de-duplicated* UIDs, because the UID names the output file:
            two instances sharing one are two successful write
            operations and one file, the second having overwritten the
            first (#197). The WFDB exporter returns its own
            `List[str]` of paths and is unchanged (#191 scopes it out).
            Every format's result must let a caller detect that nothing
            was written.

        Raises:
            ValueError: If `format` is not a registered export format.
            TypeError: For an option name the selected exporter does not
                recognise. The `dicom` path has always raised this,
                because `_export_dicom` has a real signature; the `wfdb`
                path raises it as of #410, where it previously dropped
                the option in silence and a mistyped `patient_ids`
                exported every patient. Nothing is written either way.
                The two formats do not accept the same options, so a
                caller forwarding one dict to both must split it.
            io_handlers.ExportError: From the DICOM exporter, when zero
                of N planned instances reached disk and at least one
                failed. An empty plan -- zero of zero -- does not raise:
                a subset that matched nothing is a fact about the run,
                and the `EXPORT` audit row already carries it.
        """
        from . import exporters

        exporter = exporters.get_exporter(format)
        return exporter.export(self, folder, **options)

    def _export_dicom(self, folder: str, use_compression=True,
                      check_burned_in=False, check_reversibility=True,
                      patient_ids: List[str] = None, show_progress=True,
                      subset=None, verify_readback=False):
        """
        Exports the current session to a directory, structured by Patient/Study/Series.

        Args:
            folder (str): The output directory path.
            use_compression (bool): If True, compresses output images using JPEG2000 (Lossless).
            check_burned_in (bool): If True, scans for PHI before exporting and
                skips every instance that still carries an identifier.
            check_reversibility (bool): If True (the default), warn when the
                files this export wrote still carry the encrypted originals
                that `lock_identities()` embeds, and record the disclosure in
                the audit log. The check runs after the write, against what
                reached disk, so it describes the cohort as delivered rather
                than as planned (#187). Those identities are recoverable by anyone
                holding `isocenter.key`, which a recipient of the cohort has
                no way to see for themselves. Passing False is the caller
                stating they already know; it silences the warning and skips
                the audit entry. The export itself is unchanged either way --
                this reports, it does not withhold.
            patient_ids (List[str], optional): Limit export to specific Patient IDs.
            show_progress (bool): If True, shows progress bar.
            subset (Union[str, list, pd.DataFrame]): Filter the export
                using a query string, a list of UIDs, or a DataFrame.
            verify_readback (bool): If True, each worker re-reads the file
                it just wrote before it is published under its real name,
                and holds it against what it meant to write: Rows,
                Columns, SamplesPerPixel, NumberOfFrames and
                BitsAllocated against the dataset it serialized (#209);
                then the file's `PhotometricInterpretation` against the
                transfer syntax the file itself carries, which must
                admit it and be a single value (#507); then every pixel
                sample, decoded through the same door `ingest()` reads
                through and compared bit for bit with the samples
                written, after redaction (#449); and a DICOM waveform's
                `WaveformData` bytes. The stored samples are compared,
                not a colour conversion of them. A value outside the
                declared BitsStored fails an uncompressed file, because
                every conformant reader masks it (-3024 at BitsStored 12
                reads as 1072).

                **True since #507: passing True can cost you a file the
                default export delivers.** The label check is the first
                of these on which verification refuses something the
                export worker wrote *on purpose*. By default a
                Photometric Interpretation the written syntax does not
                admit -- `YBR_ICT` or `YBR_RCT` on an uncompressed file,
                `YBR_PARTIAL_422`/`_420` on any this exporter writes --
                is written exactly as the instance declared it, with a
                `WARNING` audit row and a `REVIEW_REQUIRED` grade, on
                the reasoning that a de-identified copy the caller can
                fix beats no copy. Passing True is asking for the
                stronger claim instead, so that same instance fails,
                gets an `ERROR` row, and **no file for it reaches the
                output folder**; the reason names the label, the syntax
                and a remedy. There is deliberately no third setting.
                What the check does *not* ask is whether the samples are
                really in the colour space the label names -- three
                samples are equally RGB and YBR_FULL, so `RGB` over YBR
                samples passes, and so does a file under a transfer
                syntax the table has no measured row for.

                An unreadable or
                undecodable file, or any mismatch, fails that instance's
                export: it is counted out of "Instances Written", files
                an `ERROR` audit row and takes the compliance grade to
                `REVIEW_REQUIRED` (#181); when every instance fails the
                call raises `ExportError`. Off by default because it
                costs a second parse and a full decode per instance.
                Measured on 200 CT-like 512x512 slices, 14 workers,
                Python 3.12: the default JPEG 2000 export took 1.18 s
                with it and 0.58 s without (x2.02; the descriptor-only
                check before #449 cost x1.03), mostly pydicom's Pillow
                decode; an uncompressed export 0.42 s against 0.40 s
                (x1.06). Each worker holds one more decoded array while
                it checks.
        """
        # Cleared before anything can return early or raise. These are
        # session-scoped, and assigning them only on success let an
        # export with an empty plan -- or one whose batch died at the
        # pool -- leave a *previous* export's numbers standing: the
        # report read "3 of 3 requested" under a PASS beside an empty
        # folder (#196). None makes the report omit the row, and an
        # absent row says "not answered here" -- which is the truth
        # about an export that never completed, where a zero would say
        # "nothing was written" and a stale pair answers for the wrong
        # export.
        self._last_export_written = None
        self._last_export_requested = None

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
        #
        # `sync=True`, and it is the whole fix for #343. A plain `save()`
        # enqueues on the persistence worker and returns, so this did
        # not flush before the walk, it flushed *concurrently with* it:
        # `release_memory()` frees an instance only once the background
        # save has attached its `_pixel_loader`, so which instances were
        # swept depended on which thread got there first. Idle, the
        # sweep won and the resident arrays reached the workers; under
        # load the save won for some subset and those were reloaded
        # through the loader instead -- correct for an ingested
        # instance, and an empty image for one whose `pixel_array` was
        # assigned directly with no Rows/Columns (measured 0/200 idle,
        # 2/300 under two concurrent suites). CHANGELOG's #183 entry is
        # the first sighting ("once the save won that race"); it fixed
        # the dtype the reload came back with and left the ordering.
        # `audit()` and `redact()` already drain on entry; export was
        # the third verb and did not. The price is `save()`'s documented
        # one: a wedged worker now wedges the export instead of racing
        # it. `tests/test_export_flushes_before_it_sweeps.py` holds the
        # order.
        print("Saving pending changes to free memory...")
        self.save(sync=True)
        self.release_memory()

        tasks, patient_count = self._build_export_plan(
            _ExportOptions(folder, identifying_uids, allowed_uids,
                           use_compression, verify_readback),
            target_ids)

        if not tasks:
            get_logger().warning("No instances found to export.")
            # Still an export run, so it still writes its row (#166): a
            # subset that matched nothing is a fact about this run the
            # audit trail has to carry, and the report's export boundary
            # keys on the row's existence, not on files (#153).
            self.store_backend.log_audit(
                action_type="EXPORT",
                entity_uid=folder,
                details=(f"DICOM export to {folder}: wrote 0 of 0 planned "
                         f"instances; nothing matched the export plan."))
            # Zero of zero, and deliberately not an `ExportError`: a
            # plan that matched nothing is not an export that failed,
            # and the row above already says so. The caller still gets a
            # summary rather than `None`, so `.written` and `.failures`
            # are askable on every return path (#191).
            return ExportSummary()

        print(f"Exporting {len(tasks)} images from {patient_count} patients...")
        summary = self._run_export_batch(tasks, show_progress,
                                         self.store_backend)

        self._report_export_collisions(tasks, summary.written_uids)

        # After the batch, not before it. The disclosure is a statement
        # about files a recipient holds, so it has to be made from what
        # was written rather than from what was planned (#187).
        if check_reversibility:
            self._report_recoverable_identities(tasks, summary.written_uids)

        # Recorded for `generate_report`, which counted the object graph
        # and nothing else: a run that wrote none of its three instances
        # still reported "Total Instances | 3" under a PASS (#181).
        self._last_export_written = summary.written
        self._last_export_requested = len(tasks)

        # The run itself is an audited action, not only its failures.
        # 'EXPORT' had been `log_audit`'s first documented example since
        # the docstring was written, and nothing ever wrote it: the
        # report's Audit Trail counted Anonymize and Redact and never an
        # Export (#166). One row per run rather than per instance -- the
        # per-instance record is the output tree itself; this row says
        # how much of the plan reached it, and its existence is what
        # `generate_report` keys the export boundary on (#153), durably
        # across a session reopened on this store.
        self.store_backend.log_audit(
            action_type="EXPORT",
            entity_uid=folder,
            details=(f"DICOM export to {folder}: wrote {summary.written} "
                     f"of {len(tasks)} planned instances from "
                     f"{patient_count} patients."))
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

        Runs against the *delivered* instances, not the export plan. It
        ran against the plan until #187, on the reasoning that the plan
        is what survives the subset filter and the burned-in scan --
        which is true of those two filters and silent about the third
        thing that removes instances, the write itself. Its own prose
        commits to the stronger claim, "N of M exported instances" and
        "treat the export as re-identifiable", and those are statements
        about files: with the write failing, the report asserted that
        three re-identifiable files had been released when none existed.

        **Delivered means a file is there, not that a worker said so**
        (#198). When this union was added, `ok=False` routinely left a
        readable partial behind: `save_as` streams elements in
        ascending tag order, so a failure past group `0400` left a
        short file carrying the encrypted originals in full, and keying
        on the worker's verdict disclosed "2 of 2" beside three files
        on disk. #199 closed that source -- the worker now writes to a
        temporary name and renames only on success -- but the union is
        deliberately *not* reverted with it, because it still covers
        the directions the rename cannot: a worker that renamed its
        file and then died before answering (`run_parallel` hands back
        an exception, not an outcome, #232), and a re-identifiable file
        left by an earlier export into the folder being released. Both
        are under-claims, and an under-claim is what gets a
        re-identifiable file treated as safe: the over-claim it
        replaced costs a site a disclosure process for an export that
        did not happen; the under-claim costs the recipient.

        So a planned path that exists on disk is delivered whatever the
        worker concluded, and the union runs the safe way in both
        directions: an instance the worker wrote is delivered even if
        the file has since been removed.

        Only the instances *not* already known to be written are
        stat-ed, so a clean export does no filesystem work here and a
        failed one does one call per failure.

        Matching is on SOP Instance UID, which the export plan
        guarantees: it names each output file after one.

        Args:
            tasks: The export plan, for the instances, their tokens and
                the paths their files were to be written to.
            written_uids: The UID of every instance the workers wrote.

        Returns:
            int: How many *written* instances carry recoverable
                identities. Zero when nothing was written, and no audit
                entry is made -- an export that delivered nothing has
                disclosed nothing.
        """
        delivered = set(written_uids)
        delivered |= {task.instance.sop_instance_uid for task in tasks
                      if task.instance.sop_instance_uid not in delivered
                      and os.path.exists(task.output_path)}
        # A set, like `delivered`: the numerator and the denominator
        # must be counted over the same collection. Counting `affected`
        # over the tasks while `delivered` collapsed a duplicate SOP
        # Instance UID rendered "2 of 1 exported instances" -- arithmetic
        # that cannot be true under any reading, in the row whose job is
        # telling a recipient how many re-identifiable files they hold
        # (#197). One UID is one file, whatever wrote it.
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
        return len(affected)

    def _report_export_collisions(self, tasks, written_uids) -> int:
        """Audit every output path that more than one instance was written to.

        Filenames are the SOP Instance UID, so two instances sharing one
        map to the same path and each successful write silently replaces
        the one before it. The folder then holds one file where the plan
        held several -- and until #197 the counters described that
        overwrite as several delivered files.

        `ERROR`, not `DATA_LOSS`, and not a new vocabulary: the end
        state is an instance that was requested and is not in the
        folder, which is exactly what `_report_export_failures` files
        `ERROR` for (#181) -- so the row lands in `get_audit_errors()`,
        the report's Exceptions section names it, and the run grades
        `REVIEW_REQUIRED` the same as any other undelivered instance.
        A `DATA_LOSS` row would be graded by `loss_scope`, and
        `STANDARD` leaves the run at `PASS` -- a silent overwrite is
        precisely the thing a reviewer has to look at, because nothing
        can say here whether the colliding instances were identical
        copies or two different images wrongly sharing a UID.

        Grouped by output path, not by UID: the same UID under two
        different series lands in two different directories and
        collides with nothing.

        Keyed on the outcome, like the disclosure above (#187): a path
        every write to failed has no file and no overwrite, and its
        failures already carry their own `ERROR` rows.

        Returns:
            int: How many colliding paths were reported.
        """
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
            # into a markdown table row.
            detail = " ".join(
                f"{len(group)} exported instances share SOP Instance UID "
                f"{uid} and were written to the same path ({path}): each "
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
        """Scans for PHI and reports what it found, before anything is written.

        Returns:
            Set[str]: The UID of every entity carrying an identifier, at any
            level of the hierarchy. An instance is skipped if its own UID or
            any of its parents' appears here, so a patient whose name is
            still present excludes every image beneath them.
        """
        get_logger().info("Performing pre-export safety scan...")
        findings = self.audit()
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

        # One boolean for the whole run, computed before the walk (#183).
        # Store-wide and not per instance, because an icon under Referenced
        # Image Sequence is a thumbnail of a *different* SOP instance and
        # redaction's `regenerate_uid()` makes following the reference fail
        # open. Over `self.store.patients` rather than `target_ids`,
        # deliberately: a subset that excludes the redacted instances must
        # not turn the gate off for the ones it keeps.
        drop_icons = redaction_in_effect(
            (instance
             for patient in self.store.patients
             for study in patient.studies
             for series in study.series
             for instance in series.instances),
            rules=self.configuration.rules)

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
                            redaction_zones=zones,
                            drop_nested_icons=drop_icons,
                            verify_readback=options.verify_readback))

        return tasks, patient_count

    def _redaction_zones_for(self, series) -> list:
        """The configured pixel-redaction zones for this series' scanner."""
        if not (series.equipment and series.equipment.device_serial_number):
            return []
        rule = self.configuration.get_rule(
            series.equipment.device_serial_number)
        return rule.get("redaction_zones", []) if rule else []

    @staticmethod
    def _run_export_batch(tasks, show_progress,
                          store_backend=None) -> ExportSummary:
        """Runs the export in worker processes and reports the outcome.

        Uses `export_batch`'s own pool rather than `self._executor`: workers
        are recycled every 25 tasks so memory leaked by the imaging C
        libraries is reclaimed, which `ProcessPoolExecutor` cannot do on
        3.12 (its `max_tasks_per_child` deadlocks `map` at the first
        replacement there; #501).

        **Processes here are a decision, not an accident (#185).** Asking
        for `maxtasksperchild` rules threads out in
        `_resolve_execution_choice` --
        on 3.12 only `multiprocessing.Pool` recycles workers -- so this, the
        heaviest path in the library and the one that pickles the most,
        runs in processes on **every** interpreter, including a
        free-threaded build where every other `run_parallel` call site
        takes threads. `ISOCENTER_FORCE_THREADS` cannot change it, and
        as of #185 says so rather than being dropped in silence.

        The trade was weighed and taken: a leak in a JPEG 2000 encoder
        on a 100GB+ run is a real thing to defend against, a thread pool
        has no process to recycle, and the cost is pickling an
        `ExportContext` -- attributes, sequences, and a numpy array per
        task where pixels are resident -- across a pipe. Reversing it
        means revisiting eight test files which assume this subprocess
        boundary; `test_export_runs_in_processes_by_decision` names them
        and pins the `25` below, and every one of the eight runs on
        every push. (An uncollected ninth, `tests/profile_memory.py`,
        was named here until #347 deleted it: it asserted `10` against
        this `25` and nothing ever ran it.)

        `store_backend` is passed explicitly because this is a static
        method and the workers may be in subprocesses: the handle cannot
        cross that boundary, so the losses come back instead and are
        audited here, in the parent (#126). A failed *write* travels the
        same way and is audited on the same trip (#181).

        Returns:
            ExportSummary: what reached disk and what did not. Returned
                None until #181, which is why the caller had nothing to
                report and the count was thrown away here.
        """
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
            # Partial failure used to be invisible here: the count came back
            # and was dropped, and "Export complete." printed whether 1200 of
            # 1200 instances survived or 3 did. Per-file errors are in the
            # audit log as of #181 -- when this line was written they were
            # not, so it told the reader to go and read rows that did not
            # exist. This is the summary that says to go and read them.
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
        Exports flat validation metadata to CSV or Parquet.

        The format is chosen from the extension: ``.parquet`` writes
        Parquet, anything else writes CSV.

        Reports the session's **in-memory graph**, which is what the rest
        of the pipeline operates on. It deliberately does not `save()`
        first: an export is a read, and a method whose name says
        "dataframe" must not commit pending edits to the database as a
        side effect.

        Args:
            output_path (str): The output file path (ends with .csv or .parquet).
            expand_metadata (bool): If True, includes all DICOM attributes as columns.
            patient_ids (List[str], optional): Restrict the export to these
                Patient IDs. ``None`` means every patient in the session.

        Returns:
            pd.DataFrame: The frame that was written.

        Raises:
            ImportError: If pandas (or, for Parquet, a Parquet engine) is
                not installed.
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

    def _nested_finding_owners(self, findings) -> dict:
        """`id(item) -> Instance` for each finding raised inside a sequence.

        What `RemediationService._use_instance_owners` needs so that a
        remediation inside a sequence reaches the instance holding it
        (#494). Found by the finding's UID and then confirmed by
        following its `entity_path` from the candidate back to the very
        item the finding carries: a hand-built graph can give two
        instances one UID (`docs/api/stability.md`), and the UID alone
        would stamp and dirty the wrong one. A finding whose item is under
        no instance in the session names no owner, and is remediated on
        the item alone, as before.
        """
        nested = [f for f in findings
                  if f.entity_path and f.entity is not None
                  and f.entity_type == "Instance"]
        if not nested:
            return {}
        by_uid = {}
        for p in self.store.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        by_uid.setdefault(inst.sop_instance_uid, []).append(inst)
        owners = {}
        for f in nested:
            for inst in by_uid.get(f.entity_uid, ()):
                if resolve_item_path(inst, f.entity_path) is f.entity:
                    owners[id(f.entity)] = inst
                    break
        return owners

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

        Two callers since #412: `audit()` and `scan_pixel_content()`. The
        warnings say what happens to the *finding* -- its entity is None --
        and not what remediation will do, because an OCR finding carries
        no proposal and `auto_remediate_config()` still acts on it through
        its metadata.
        """
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
            # The study's own date record travels too (#518).
            # `_scan_study` runs inside `scan_patient`, which the worker
            # calls on this clone, so without this line every worker
            # sees a study that looks pre-0.9.6 and raises nothing --
            # the whole fix invisible on both parallel paths at once.
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
                    # the line that has to change first (#238).
                    #
                    # Key: Ensure attributes are copied so workers can scan tags
                    if hasattr(i, 'attributes'):
                        i_new.attributes = i.attributes.copy()

                    # Sequences travel too. Dropping them was #57: the
                    # worker got a top-level-only instance, so the scan
                    # reported clean on every nested tag -- report text,
                    # annotations, anything below the first level.
                    i_new.sequences = clone_sequences(i)

                    # `date_shifted` is not carried because `Instance` no
                    # longer has one (#510): the scan reads the per-value
                    # records below instead, and the study's flag rides
                    # `s_new.date_shifted` above.
                    #
                    # The per-value date records and the store's own
                    # provenance travel too (#510, #513). `audit()`
                    # scans this clone unconditionally, threads and
                    # processes alike, so without these two lines every
                    # worker sees an instance with no record against any
                    # of its dates, raises them all, and the arm shifts
                    # each a second time -- the defect, with the fix in
                    # place. The nested half is `clone_sequences`, above.
                    if i._shifted_dates:
                        i_new._shifted_dates = dict(i._shifted_dates)
                    i_new._legacy_shift_provenance = i._legacy_shift_provenance

                    se_new.instances.append(i_new)

        return p_new

