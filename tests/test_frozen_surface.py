"""The 1.0 surface, pinned (#379, for #26).

`docs/api/stability.md` says what the tag promises, in three tiers. This
file holds the tier-1 half of that page still: the set of public
`Session` methods equals the frozen list in **both directions** -- a new
public method must be added to the freeze (a CHANGELOG entry and a row
on the page) or given a leading underscore, and a removed one is a 2.0
-- every parameter name and default matches, the frozen shapes' fields
match, and the API reference renders every frozen method.

**Why its own file, and not `tests/test_api_coherence.py`**, which #379
named as the natural home: that file is listed under `io_handlers.py`
in `scripts/mutation_probe.py`'s `TARGETS`, so every test in it re-runs
against every mutant of that module. A pure `inspect.signature` pin buys
zero kill signal there and costs on every mutant.
`tests/test_documented_api_exists.py` records the same reasoning for
#234. This file imports no probe target's module -- the return shapes it
pins are reached through `isocenter.session`, which binds them, or
through the facade itself -- and needs no `TARGETS` entry.

The literals below are transcribed from the spec's §5.3 table, as
amended for Q7 (`lock_identities` stripped of `_patient_obj` and
`**kwargs` before the tag). They are literals on purpose: a pin derived
from the code would be green on any code.
"""
import ast
import dataclasses
import inspect
import pathlib
import re

import pytest

import isocenter
from isocenter import session as session_module
from isocenter.discovery import DiscoveryResult
from isocenter.entities import (NO_PATIENT_ID_PREFIX, DicomItem, Equipment,
                                Instance, Patient, Series, Study,
                                TrackedEntity, is_synthetic_patient_id)
from isocenter.session import DicomSession

REPO = pathlib.Path(__file__).resolve().parent.parent

_NO_DEFAULT = inspect.Parameter.empty

#: `name -> parameters`, spelled exactly as `docs/api/stability.md`'s
#: table spells them (`_spell` renders `inspect.signature` that way):
#: `self` omitted, defaults as `repr`, `*` before the first keyword-only
#: parameter, `**options` for the open export options. The kind is part
#: of the pin: a parameter moved across the `*` in either direction is a
#: different call and a red test, which `(name, default)` pairs missed.
FROZEN_SESSION_METHODS = {
    "ingest": "directory",
    "save": "sync=False",
    "close": "",
    "examine": "",
    "create_config": "output_path",
    "load_config": "config_file",
    "preview_config": "",
    "audit": "config_path=None",
    "auto_remediate_config": "report",
    "anonymize": "findings=None",
    "enable_reversible_anonymization": "key_path='isocenter.key'",
    "lock_identities": "patient_id, persist=False, *, verbose=True, tags_to_lock=None",
    "lock_identities_batch": (
        "patient_ids, auto_persist_chunk_size=0, tags_to_lock=None, "
        "*, persist=False, verbose=True"),
    "recover_patient_identity": "patient_id, restore=True",
    "redact": "show_progress=True, force=False",
    "redact_by_machine": "serial_number, roi",
    "scan_pixel_content": "serial_number=None",
    "discover_redaction_zones": "serial_number, sample_size=50, min_confidence=80.0",
    "reconcile_private_tags": "",
    "export": "folder, format='dicom', **options",
    "export_dataframe": "output_path='export_metadata.csv', expand_metadata=False, patient_ids=None",
    "get_cohort_report": "expand_metadata=False, patient_ids=None",
    "phi_status_summary": "",
    "generate_report": "output_path, format='markdown'",
    "generate_manifest": "output_path, format='html'",
    "save_analysis": "report",
    "compact": "",
    "release_memory": "",
}

#: `export(format="dicom", **options)`: the names `_export_dicom` accepts
#: after `folder`. `**options` hides them from the pin above, so they
#: are pinned here (spec §9 item 6).
FROZEN_DICOM_EXPORT_OPTIONS = (
    "use_compression=True, check_burned_in=False, check_reversibility=True, "
    "patient_ids=None, show_progress=True, subset=None, verify_readback=False")

#: stability.md's second table, "Other frozen callables" (#26): every
#: tier-1 callable that is not one of `Session`'s 28 methods, spelled as
#: `_spell` spells it and keyed `Owner.name`. Until #26 these were pinned
#: by `callable()` alone, so a renamed or re-defaulted parameter on any of
#: them was green -- a tier-1 method with unpinned parameters is not
#: frozen. Each value is read from the `def` line itself, not from `_spell`:
#: the first version of this table took `start_patient`'s row from
#: `_spell`'s output, which dropped the staticmethod's first parameter and
#: froze `name` for `def start_patient(id, name)` (review of #787).
#:
#: Three were renamed before the freeze rather than frozen as they stood
#: (owner rulings on #787): `start_patient(id=)` to `patient_id` (it
#: shadowed the builtin), `add_rule(model=, zones=)` to the file's own
#: keys `model_name`/`redaction_zones`, and `set_phi_tag(replacement=)`
#: to `value`, the key #538 renamed in the file.
#:
#: `entities.is_synthetic_patient_id` is the one module-level function,
#: and `Builder.start_patient` the one staticmethod: `_owner_and_callable`
#: keeps the first parameter of both, which `_spell` drops for a method.
#: `DiscoveryResult.filter`'s `predicate` takes a minimum
#: confidence (the float default) or a callable, as its docstring says,
#: and is frozen that way (coordinator ruling, 2026-09-23).
FROZEN_OTHER_CALLABLES = {
    "Session.__init__": "persistence_file=None",
    "IsocenterConfiguration.save": "",
    "IsocenterConfiguration.add_rule": (
        "serial_number, manufacturer='Unknown', model_name='Unknown', "
        "redaction_zones=None"),
    "IsocenterConfiguration.update_rule": "serial_number, updates",
    "IsocenterConfiguration.delete_rule": "serial_number",
    "IsocenterConfiguration.set_phi_tag": "tag, action, value=None",
    "IsocenterConfiguration.get_rule": "serial_number",
    "DicomItem.set_attr": "tag, value",
    "Instance.set_attr": "tag, value",
    "Instance.get_pixel_data": "",
    "Instance.set_pixel_data": "array",
    "Instance.unload_pixel_data": "",
    "Instance.discard_pixel_data": "",
    "Instance.get_waveform_data": "",
    "Builder.start_patient": "patient_id, name",
    "PhiReport.__init__": "findings, failures=None",
    "PhiReport.to_dataframe": "",
    "DiscoveryResult.filter": "predicate=0.0",
    "DiscoveryResult.to_zones": "pad_x=20, pad_y=10, min_occurrence=0.1",
    "DiscoveryResult.to_dataframe": "",
    "entities.is_synthetic_patient_id": "value",
}

#: The key a subject with no Patient ID is held under (#584), frozen by
#: value: `get_cohort_report` shows it and `patient_ids` selects by it.
FROZEN_NO_PATIENT_ID_PREFIX = "\\no-patient-id\\"

#: Every public name on each class tier 1 names, split by tier, keyed by
#: class name (#26, spec §1.2). A name on the class and in neither dict
#: belongs to no tier -- frozen by default the day someone relies on it --
#: and `test_every_public_name_on_a_tier_one_class_is_classified` is red
#: until it is placed on the page or underscored. A class is checked
#: against its own names and every base's, so `TrackedEntity`'s
#: bookkeeping is listed once and an override (`Instance.set_attr`) is
#: classified by the class that first defined it.
TIER_ONE_NAMES = {
    "TrackedEntity": set(),
    "DicomItem": {"attributes", "sequences", "attribute_vrs", "set_attr"},
    "Instance": {"sop_instance_uid", "sop_class_uid", "instance_number",
                 "file_path", "source_path", "get_pixel_data", "set_pixel_data",
                 "unload_pixel_data", "discard_pixel_data", "get_waveform_data"},
    "Patient": {"patient_id", "patient_name", "studies"},
    "Study": {"study_instance_uid", "study_date", "series", "date_shifted",
              "study_time"},
    "Series": {"series_instance_uid", "modality", "series_number", "equipment",
               "instances"},
    "Equipment": {"manufacturer", "model_name", "device_serial_number"},
    "IsocenterConfiguration": {
        "save", "add_rule", "update_rule", "delete_rule", "set_phi_tag",
        "get_rule", "rules", "phi_tags", "date_jitter", "remove_private_tags",
        "privacy_profile", "config_path", "auto_save"},
    "PhiReport": {"findings", "failures", "to_dataframe"},
    "PhiFinding": {"entity_uid", "entity_type", "field_name", "value", "reason",
                   "tag", "patient_id", "entity", "remediation_proposal",
                   "metadata", "entity_path"},
    "ExportSummary": {"written_uids", "failures", "written", "failed"},
    "IngestSummary": {"ingested", "failures", "declined", "skipped", "failed"},
    "DiscoveryResult": {"filter", "to_zones", "to_dataframe"},
    "DicomBuilder": {"start_patient"},
    "DicomStore": {"patients"},
    "LockingResult": set(),
    "RedactionError": {"failures", "attempted"},
    "ExportError": {"failures", "attempted"},
}

#: The tier-2 names on the same classes, each named on stability.md's
#: "Documented but internal" list. The seven recording helpers
#: (`record_remediation` to `clear_sequence_items`) were on no list until
#: #26: the scan, remediation and the reversible lock call them across
#: modules, so they stay public, and tier 2 says a program should not.
#: `DicomStore`'s five methods, `DiscoveryResult.candidates` and
#: `n_sources` were on no list either (review of #787); the owner ruled
#: them tier 2, `n_sources` although the README teaches it.
TIER_TWO_NAMES = {
    "TrackedEntity": {"has_unsaved_changes", "phi_status", "phi_status_policy",
                      "mark_modified", "mark_persisted", "mark_subtree_persisted",
                      "record_phi_status"},
    "DicomItem": {"add_sequence", "add_sequence_item", "record_attr_vr",
                  "clear_sequence_items", "record_date_shift",
                  "date_shift_vouches_for"},
    "Instance": {"regenerate_uid", "get_waveform_bytes", "unload_waveform_data",
                 "pixel_array", "waveform_array", "record_remediation",
                 "remediation_vouches_for", "record_identity_token",
                 "identity_token_is_this_stores"},
    "Study": {"record_date_shift", "date_shift_vouches_for"},
    "Equipment": {"from_parts"},
    "DiscoveryResult": {"get_density_matrix", "visualize_heatmap",
                        "analyze_temporal_stability", "inspect_clusters",
                        "candidates", "n_sources"},
    "DicomStore": {"get_unique_equipment", "get_ingested_paths",
                   "get_superseded_uids", "save_state", "load_state"},
}

#: The conditions that grade a run `REVIEW_REQUIRED`, in the order
#: `docs/analytics.md` numbers them and `_review_reasons` appends them, each
#: with the test that goes red when its `append` is deleted (spec §1.5).
#: A condition is never removed or narrowed in 1.x (stability.md, Output
#: vocabularies), and "narrowed" has no structural test: the named
#: behavioural test is the pin for it. Measured at 3fe24bee by deleting
#: each `append` in turn and running all eight: each named test is red
#: under its own condition's deletion and green under the other seven.
#: (`test_an_edit_after_a_pass_with_no_pass_since_is_not_pass`, the first
#: choice for condition 8, is also red under condition 7's deletion, so it
#: did not tell the two apart.)
FROZEN_GRADE_CONDITIONS = [
    ("nothing attested",
     "tests/test_report_section5_says_what_happened.py::"
     "test_an_empty_audit_trail_is_named_as_the_reason"),
    ("something failed, or the source could not be honoured",
     "tests/test_report_section5_says_what_happened.py::"
     "test_every_reason_the_run_is_not_pass_is_in_section_5[section-4-row]"),
    ("graded data was lost",
     "tests/test_report_section5_says_what_happened.py::"
     "test_every_reason_the_run_is_not_pass_is_in_section_5[graded-loss]"),
    ("the scan could not read something the export carries",
     "tests/test_data_loss_reporting.py::"
     "test_a_scan_gap_alone_grades_review_required"),
    ("a remediation was proposed and did not run",
     "tests/test_report_section5_says_what_happened.py::"
     "test_every_reason_the_run_is_not_pass_is_in_section_5[decline]"),
    ("a verb left no evidence",
     "tests/test_report_section5_says_what_happened.py::"
     "test_every_reason_the_run_is_not_pass_is_in_section_5[unattested-verb]"),
    ("a finding raised under the policy was not acted on",
     "tests/test_the_grade_counts_findings_not_acted_on.py::"
     "test_an_audit_nobody_acted_on_grades_review_required"),
    ("edited after its PHI status was recorded",
     "tests/test_an_edit_below_or_beside_an_instance_is_seen.py::"
     "test_an_instance_attribute_edited_after_the_pass_is_not_pass"),
]

#: The `Instance` fields that are frozen (`pixel_array` and
#: `waveform_array` are public fields too, and tier 2).
#:
#: `date_shifted` was on this list until 0.9.6 and was cut with the field
#: (#510). It is pinned as *absent* below, in both directions, because
#: the `<=` check that guards this list would be green for a field that
#: came back.
FROZEN_INSTANCE_FIELDS = [
    "sop_instance_uid", "sop_class_uid", "instance_number", "file_path",
    "source_path", "attributes", "sequences", "attribute_vrs"]

FROZEN_ALL = ["Session", "Builder", "Equipment", "RedactionError", "ExportError"]

#: Words that reach users through tier-1 outputs (spec Q9): never renamed
#: or removed in 1.x. These are **three** vocabularies and not one, which
#: `docs/api/stability.md` had conflated -- and the page's single
#: category was wrong for four of the thirteen words it listed (#396).
#: One name per vocabulary; T-F4 reads the union.

#: Written to the audit table, as the `action_type` column. The first
#: nine reach `log_audit` as literal arguments; the four `REMEDIATION_*`
#: words are written by remediation through a local variable and a
#: module constant, and joined the freeze at 1.0 (#411) because each is
#: counted by type in section 2 of the report exactly as the nine are.
FROZEN_AUDIT_ACTION_TYPES = {
    "DATA_LOSS", "ERROR", "EXPORT", "RECONCILE_PRIVATE", "REDACTION",
    "REVERSIBLE_EXPORT", "RISK", "SCAN_GAP", "WARNING",
    "REMEDIATION_REPLACE", "REMEDIATION_SHIFT_DATE", "REMEDIATION_REMOVE",
    "REMEDIATION_DECLINED"}

#: `PhiRemediation.action_type` -- what a *proposal* says it will do,
#: reaching a user through the frozen `PhiFinding.remediation_proposal`.
#: Never an audit row: acting on one writes `REMEDIATION_REPLACE`,
#: `REMEDIATION_SHIFT_DATE` or `REMEDIATION_REMOVE` instead.
FROZEN_PROPOSAL_ACTION_TYPES = {"REMOVE_TAG", "REPLACE_TAG", "SHIFT_DATE"}

#: Report exception categories. Synthesised at report time into the
#: `exceptions` list; never written to the audit table at all. Both
#: cost a run its PASS; `AUDIT_DROP` joined the freeze at 1.0 (#411).
FROZEN_REPORT_EXCEPTIONS = {"COMPLIANCE_CHECK", "AUDIT_DROP"}

FROZEN_LOSS_SCOPES = ["STANDARD", "PRIVATE", "SIGNAL"]
FROZEN_GRADES = ["PASS", "REVIEW_REQUIRED"]

#: The union T-F4 requires the page to name.
FROZEN_VOCABULARY = (
    FROZEN_AUDIT_ACTION_TYPES | FROZEN_PROPOSAL_ACTION_TYPES
    | FROZEN_REPORT_EXCEPTIONS | set(FROZEN_LOSS_SCOPES) | set(FROZEN_GRADES))


def _spell(method, *, drop_self=True):
    """`inspect.signature(method)` rendered the way the stability page's
    table spells it: `self` dropped, `name` or `name=<repr>`, `/` after
    positional-only parameters, `*` before the first keyword-only one
    (`*args` plays that role when present), `**name` last.

    `drop_self=False` for a module-level function, whose first parameter
    is a real one: dropping it would pin `is_synthetic_patient_id` as
    taking nothing."""
    kinds = inspect.Parameter
    out = []
    params = list(inspect.signature(method).parameters.values())[1 if drop_self else 0:]
    star_seen = False
    for i, param in enumerate(params):
        if param.kind is kinds.KEYWORD_ONLY and not star_seen:
            out.append("*")
            star_seen = True
        if param.kind is kinds.VAR_POSITIONAL:
            out.append(f"*{param.name}")
            star_seen = True
        elif param.kind is kinds.VAR_KEYWORD:
            out.append(f"**{param.name}")
        elif param.default is _NO_DEFAULT:
            out.append(param.name)
        else:
            out.append(f"{param.name}={param.default!r}")
        if param.kind is kinds.POSITIONAL_ONLY and (
                i + 1 == len(params) or params[i + 1].kind is not kinds.POSITIONAL_ONLY):
            out.append("/")
    return ", ".join(out)


def _package_trees():
    """Every package module, parsed -- **read by path, never imported**.

    Not a style preference. `tests/test_mutation_probe_targets._importers`
    matches the *text* of a dotted module name anywhere in a test file,
    comments included, and would drag this file into that module's
    `TARGETS` row: re-run against every one of its mutants for zero kill
    signal, and falsifying this file's own docstring. Package modules are
    named in prose here, or spelled as separate path segments.
    """
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        yield ast.parse(path.read_text(encoding="utf-8"))


def _callee_name(call):
    fn = call.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)


def _string(node):
    """The value of a `str` constant, or `None` for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _calls_named(tree, name):
    return (n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and _callee_name(n) == name)


def _module_string_constants(tree):
    """`NAME -> "value"` for every module-level `NAME = "value"`."""
    return {target.id: _string(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            and _string(node.value) is not None
            for target in node.targets if isinstance(target, ast.Name)}


def _enclosing_function(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _action_type_words(node, module_constants, parents, where):
    """The word(s) an `action_type` argument can carry, read from the source.

    A string constant is its own word. A **name** is resolved, because
    remediation passes its four words that way (#411): first as a
    module-level `NAME = "str"` (`REMEDIATION_DECLINED`), otherwise as
    every string constant assigned to that name inside the enclosing
    function (the local `action_type` in `_apply_single_remediation`).

    Anything else **raises** rather than being skipped. A collector that
    skips what it cannot read goes quietly vacuous at exactly the site
    that stopped being readable -- which is how four frozen-in-all-but-
    name words sat outside this pin until #411 -- so a new call shape is
    a red test and a decision, not a silence.
    """
    if _string(node) is not None:
        return {_string(node)}
    if isinstance(node, ast.Name):
        if node.id in module_constants:
            return {module_constants[node.id]}
        function = _enclosing_function(node, parents)
        # A parameter is a binding the walk below cannot see: its value
        # is whatever the caller passes, or a default that lives on an
        # `ast.arg`, not a `Name`. Reading only the body's assignments
        # would report `{"REMEDIATION_REMOVE"}` for
        # `def helper(finding, action_type="REMEDIATION_RMV")` and call
        # the pin complete (measured synthetically on PR #430; no site in
        # the package has this shape).
        if function is not None:
            params = function.args
            names = [a.arg for a in params.posonlyargs + params.args + params.kwonlyargs]
            names += [a.arg for a in (params.vararg, params.kwarg) if a is not None]
            if node.id in names:
                raise AssertionError(
                    f"Pin A cannot read the action_type passed at {where}: "
                    f"{node.id} is a parameter of {function.name}(), so its "
                    f"word comes from the caller. Teach _action_type_words "
                    f"the new shape; do not skip it")
        assigned = set()
        for sub in ast.walk(function) if function is not None else ():
            if not (isinstance(sub, ast.Name) and sub.id == node.id
                    and isinstance(sub.ctx, ast.Store)):
                continue
            # **Every** binding of the name is read, and one that is not a
            # plain `name = "WORD"` raises. Reading only the constant ones
            # and skipping the rest was the fail-loud rule applied to the
            # argument and not to what flows into it:
            # `action_type = str("REMEDIATION_SET")` and
            # `action_type = "REMEDIATION_" + "SET"` both survived this
            # file on both interpreters (measured on PR #430). The same
            # rule covers `+=` (an `AugAssign`), tuple unpacking, a `for`
            # target and a walrus: none is an `Assign` or `AnnAssign` with
            # the name as a whole target, so each raises.
            binding = parents.get(sub)
            whole_target = (
                (isinstance(binding, ast.Assign) and sub in binding.targets)
                or (isinstance(binding, ast.AnnAssign) and binding.target is sub))
            if not whole_target or _string(binding.value) is None:
                raise AssertionError(
                    f"Pin A cannot read the action_type passed at {where}: "
                    f"{node.id} is bound at line {sub.lineno} by "
                    f"{ast.unparse(binding)!r}, which is not `{node.id} = "
                    f"\"WORD\"`. Teach _action_type_words the new shape; do "
                    f"not skip it")
            assigned.add(_string(binding.value))
        # `""` is excluded by name, not by truthiness. It is the
        # `action_type = ""` initialiser at the top of
        # `_apply_single_remediation`'s dispatch, and the `if action_type:`
        # guard in front of both audit writes means it is never written:
        # an arm that sets no word falls through to the decline instead.
        # Filtering on truthiness would say the same thing today and
        # would also swallow any other falsy constant someone assigned.
        assigned.discard("")
        if assigned:
            return assigned
    raise AssertionError(
        f"Pin A cannot read the action_type passed at {where}: "
        f"{ast.unparse(node)!r} is neither a string constant, a module-level "
        f"string constant, nor a name assigned string constants in its "
        f"function. Teach _action_type_words the new shape; do not skip it")


def _audit_action_types():
    """Pin A: every `action_type` word that reaches the audit table, by AST.

    Three kinds of site:

    - `log_audit(action_type="WORD", ...)`, the keyword form most sites
      use;
    - `log_audit("WORD", ...)`, positionally: `log_audit(self,
      action_type, ...)` makes argument 0 the same parameter, and a
      keyword-only collector would leave those sites respellable with
      this pin green -- the M2 shape;
    - `audit_buffer.append((WORD, ...))`, remediation's batched path,
      whose tuples `log_audit_batch` writes with element 0 as the
      `action_type` column. A batched-only respelling is seen **only**
      here: the `log_audit(action_type, ...)` fallback beside each append
      carries the local variable, not the tuple (measured on PR #430:
      respelling the tuple alone is red here, and green with this
      collection removed).

    That last collection is anchored on the name `audit_buffer`, so
    `_audit_buffer_words` refuses every use of the name it cannot read
    rather than trusting the anchor: an alias (`rows = audit_buffer`), a
    method other than `.append` (`.extend`), `+=`, or a slice write all
    raise, and so does the name vanishing from the package altogether.
    Each of those survived before (PR #430). The buffer may be handed
    only to `len()` and to the three callees in `_AUDIT_BUFFER_CALLEES`,
    matched by name. The residual, accepted, is exactly what a callee
    does with it out of this collector's sight: a method by one of
    those names whose parameter is not called `audit_buffer` (today
    both remediation methods call it that, so their appends are read),
    and `log_audit_batch` itself, which writes the tuples it is given
    and is not read here.

    Remediation passes its words through a local variable and a module
    constant, not a literal. Until #411 this collector read literals
    only, so all four `REMEDIATION_*` words were invisible to it and
    respelling any of them was green; `_action_type_words` resolves the
    names, and raises on one it cannot resolve.
    """
    found = set()
    buffer_sites = 0
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        words, sites = _audit_words_in(
            ast.parse(path.read_text(encoding="utf-8")), path.relative_to(REPO))
        found |= words
        buffer_sites += sites
    # The floor: a collector anchored on a name is vacuous the day the
    # name changes, and green while it is.
    assert buffer_sites >= 1, (
        "Pin A found no `audit_buffer.append((...))` in the package; the "
        "batch buffer was renamed or removed, and a batched-only respelling "
        "is now invisible. Re-anchor _audit_buffer_words")
    return found


def _audit_words_in(tree, rel):
    """Pin A's reading of one module: `(words, audit_buffer append sites)`.

    The per-file half of `_audit_action_types`, split out so the #429 pin
    reads remediation's words through the same sites Pin A reads, and the
    two can never disagree about what a module writes. The package-level
    `buffer_sites >= 1` floor stays in the fold, where it means something.
    """
    constants = _module_string_constants(tree)
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    found = set()
    for call in _calls_named(tree, "log_audit"):
        where = f"{rel}:{call.lineno}"
        for kw in call.keywords:
            if kw.arg == "action_type":
                found |= _action_type_words(kw.value, constants, parents, where)
        if call.args:
            found |= _action_type_words(call.args[0], constants, parents, where)
    words, sites = _audit_buffer_words(tree, constants, parents, rel)
    return found | words, sites


def _audit_buffer_words(tree, constants, parents, rel):
    """Words appended to `audit_buffer`, and how many append sites there were.

    Every `audit_buffer` in the module is classified against an
    **allow-list**, and anything not on it raises (see Pin A). The list
    is what the package does with the buffer today:

    - `audit_buffer.append((WORD, ...))`, which is read;
    - `audit_buffer = []`, the initialiser;
    - an argument to `len()` or to one of `_AUDIT_BUFFER_CALLEES`
      (`log_audit_batch`, `_apply_single_remediation`,
      `_record_decline`), positional or keyword -- the accepted
      residual Pin A names;
    - `audit_buffer is None` / `is not None`;
    - a truth test that is the `test` of an `if`/`while`, directly or
      through `and`/`or`/`not` -- `if self.store_backend and
      audit_buffer:`.

    A block-list was the first version, and it saw an alias only when
    `audit_buffer` was the whole right-hand side:
    `rows = audit_buffer if audit_buffer is not None else []` and
    `rows, _unused = audit_buffer, None` both survived on both
    interpreters (PR #430). The truth-test entry is bounded at the
    statement's test for the same reason -- `rows = audit_buffer or []`
    is a `BoolOp` too, and must raise.
    """
    found, sites = set(), 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id == "audit_buffer"):
            continue
        parent = parents.get(node)
        where = f"{rel}:{node.lineno}"
        if isinstance(parent, ast.Attribute):
            call = parents.get(parent)
            assert (parent.attr == "append" and isinstance(call, ast.Call)
                    and call.func is parent), (
                f"audit_buffer.{parent.attr} at {where}: Pin A reads only "
                f"`audit_buffer.append((WORD, ...))`")
            assert len(call.args) == 1 and isinstance(call.args[0], ast.Tuple), (
                f"audit_buffer.append at {where} is not handed one tuple; "
                f"Pin A cannot read its action_type")
            found |= _action_type_words(call.args[0].elts[0], constants, parents, where)
            sites += 1
        elif not _an_allowed_audit_buffer_use(node, parent, parents):
            raise AssertionError(
                f"audit_buffer at {where} is used as "
                f"{ast.unparse(parent)!r}, which is not on Pin A's "
                f"allow-list (append a tuple, `= []`, an argument to len() or "
                f"{sorted(_AUDIT_BUFFER_CALLEES)}, `is (not) None`, an "
                f"if/while truth test)")
    return found, sites


#: The calls the package hands `audit_buffer` to today, enumerated from
#: the tree: `len()` reads it, `log_audit_batch` writes its tuples, and
#: the two remediation methods take it as a parameter *named*
#: `audit_buffer`, so their own appends are read here too. "Any call"
#: was the first version, and `list.append(audit_buffer, ("WORD", ...))`
#: -- an append this collector does not read -- survived on both
#: interpreters (PR #430).
_AUDIT_BUFFER_CALLEES = {
    "log_audit_batch", "_apply_single_remediation", "_record_decline"}


def _a_listed_buffer_callee(call):
    if isinstance(call.func, ast.Name) and call.func.id == "len":
        return True
    return (isinstance(call.func, ast.Attribute)
            and call.func.attr in _AUDIT_BUFFER_CALLEES)


def _an_allowed_audit_buffer_use(node, parent, parents):
    """The non-append entries of `_audit_buffer_words`' allow-list."""
    if isinstance(node.ctx, ast.Store):
        return (isinstance(parent, ast.Assign) and parent.targets == [node]
                and isinstance(parent.value, ast.List) and not parent.value.elts)
    if isinstance(parent, ast.Call) and node in parent.args:
        return _a_listed_buffer_callee(parent)
    if isinstance(parent, ast.keyword) and isinstance(parents.get(parent), ast.Call):
        return _a_listed_buffer_callee(parents[parent])
    if isinstance(parent, ast.Compare):
        return (all(isinstance(op, (ast.Is, ast.IsNot)) for op in parent.ops)
                and all(n is node or (isinstance(n, ast.Constant) and n.value is None)
                        for n in [parent.left, *parent.comparators]))
    # A truth test: climb through `and`/`or`/`not` to the statement.
    child, up = node, parent
    while isinstance(up, ast.BoolOp) or (
            isinstance(up, ast.UnaryOp) and isinstance(up.op, ast.Not)):
        child, up = up, parents.get(up)
    return isinstance(up, (ast.If, ast.While)) and up.test is child


def _proposal_action_types():
    """Pin B: `action_type` string constants passed to `PhiRemediation`."""
    found = set()
    for tree in _package_trees():
        for call in _calls_named(tree, "PhiRemediation"):
            for kw in call.keywords:
                if kw.arg == "action_type" and _string(kw.value) is not None:
                    found.add(_string(kw.value))
    return found


def _module_tree(*parts):
    return ast.parse((REPO.joinpath(*parts)).read_text(encoding="utf-8"))


def _loss_scope_values():
    """Pin C: the *values* of the module-level `LOSS_SCOPE_*` assignments.

    The names are deliberately not asserted. That module is tier 3
    wholesale on `docs/api/stability.md`; what a user reads is the three
    strings, not the identifiers the package spells them with, so
    renaming a constant is a green mutation and should be. The prefix
    dependence is a red-and-update of the `test_source_citations` kind --
    the cheap price of not pinning a private name.
    """
    tree = _module_tree("isocenter", "io_handlers.py")
    return {_string(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
            and target.id.startswith("LOSS_SCOPE_")
            and _string(node.value) is not None}


def _grade_values():
    """Pin D: string constants inside a `validation_status=` argument.

    The value is an `IfExp`, so this walks the subtree rather than
    reading a constant directly.
    """
    found = set()
    for call in (n for n in ast.walk(_module_tree("isocenter", "session.py"))
                 if isinstance(n, ast.Call)):
        for kw in call.keywords:
            if kw.arg != "validation_status":
                continue
            for node in ast.walk(kw.value):
                if _string(node) is not None:
                    found.add(_string(node))
    return found


def _report_exception_categories():
    """Pin E: string constants that are direct `Tuple.elts` of an
    `exceptions.append(...)` argument.

    `ast.walk` is wrong here and would be the over-broad collector this
    file exists to remove: each of those tuples carries an f-string whose
    `JoinedStr` parts are `Constant` nodes, so a walk collects `' - '`
    and a sentence of prose alongside the two categories, and the
    expected set becomes soup nobody can read. Measured, not guessed.
    """
    found = set()
    for call in (n for n in ast.walk(_module_tree("isocenter", "session.py"))
                 if isinstance(n, ast.Call) and _callee_name(n) == "append"):
        target = call.func.value if isinstance(call.func, ast.Attribute) else None
        if not (isinstance(target, ast.Name) and target.id == "exceptions"):
            continue
        for arg in call.args:
            if isinstance(arg, ast.Tuple):
                found.update(v for v in map(_string, arg.elts) if v is not None)
    return found


#: One row of either of `docs/api/stability.md`'s two signature tables:
#: a backticked name -- a bare method name in the Session table, or one
#: qualified by its owner (`Instance.set_pixel_data`) in the second -- and
#: a backticked parameter list, or `—` for none. Shared by
#: `_signature_rows` and `_unrecognised_table_lines` so that what the
#: parser reads and what the companion accepts cannot drift apart.
_SIGNATURE_ROW = re.compile(
    r"^\| `(\w+(?:\.\w+)?)` \| (?:`([^`]*)`|—) \|$", re.MULTILINE)

#: The table lines that are not rows, spelled literally: a changed header
#: is a changed table, and should be red until someone reads it. The
#: Session table's header, the second table's, and the separator both use.
_SESSION_TABLE_HEADER = "| Method | Parameters |"
_OTHER_TABLE_HEADER = "| Callable | Parameters |"
_TABLE_SEPARATOR = "| --- | --- |"


def _frozen_section(page: str) -> str:
    """The page from `## Frozen at 1.0` to the next `## ` heading.

    Fails rather than returning `""` when the heading is gone: a renamed
    section would otherwise hand every caller an empty string, and an
    empty string has no unrecognised lines in it.
    """
    heading = "## Frozen at 1.0"
    assert heading in page, f"stability.md has no {heading!r} section"
    section = page.split(heading, 1)[1]
    return section.split("\n## ", 1)[0]


def _table_run(page: str, header: str) -> str:
    """The frozen section's table under `header`: the unbroken run of
    non-blank lines from that header, as python-markdown reads a table.

    Each table is parsed on its own run, so the Session table's rows and
    the second table's cannot be read as one dict. Fails when the header
    is gone, rather than returning `""`: an empty run has no rows to
    disagree with the pins.
    """
    lines = _frozen_section(page).splitlines()
    assert header in lines, f"stability.md's frozen section has no {header!r} line"
    start = end = lines.index(header)
    while end < len(lines) and lines[end].strip():
        end += 1
    return "\n".join(lines[start:end]) + "\n"


def _unrecognised_table_lines(page: str,
                              headers=(_SESSION_TABLE_HEADER, _OTHER_TABLE_HEADER)) -> list:
    """Every pipe-table line in the frozen section `_SIGNATURE_ROW` cannot read.

    **The companion to `_signature_rows`, and the fix for #415.** The
    regex is also the definition of a row, so a row it does not match is
    not a failure, it is nothing: `| `save` | sync=True |` placed above
    the true row -- parameters unbackticked -- left this file green while
    the published page showed a `save` signature no test had read
    (measured on 0.9.5). The same holds for a trailing space, an
    unbackticked method name, and an indented row. Here any line that
    looks like a table line and is neither the header, the separator,
    nor a row the parser reads is reported, so the next formatting
    variant fails loud as well as this one.

    **The table is the unbroken run of non-blank lines from its header**,
    which is how python-markdown's tables extension reads it, and every
    line in that run other than the header and separator must be a row
    the parser reads. Looking for lines that *start* with `|` was not
    enough: `` `save` | sync=True ``, with no leading pipe, renders as a
    real row and survived on both interpreters (PR #430). Outside the
    run, any line containing `|` is reported, so a second table -- a
    pipe-less one included -- or a stray row cannot sit elsewhere in the
    section unread.

    The header is looked up literally and its absence fails, rather than
    returning `[]`: a changed header is a changed table. Every header in
    `headers` must be present, and each opens its own run (#26 added the
    second, "Other frozen callables").

    Rejected: a structural parser splitting on `|`. It would read this
    row, but it would also silently normalise the next variant, which
    is the failure this function exists to prevent.
    """
    lines = _frozen_section(page).splitlines()
    runs = []
    for header in headers:
        assert header in lines, f"stability.md's frozen section has no {header!r} line"
        start = end = lines.index(header)
        while end < len(lines) and lines[end].strip():
            end += 1
        runs.append((start, end))
    flagged = []
    for i, line in enumerate(lines):
        run = next(((start, end) for start, end in runs if start <= i < end), None)
        if run is not None:
            start = run[0]
            if i == start or (i == start + 1 and line == _TABLE_SEPARATOR):
                continue
            if not _SIGNATURE_ROW.fullmatch(line):
                flagged.append(line)
        elif "|" in line:
            # Any pipe outside the recognised table, not only a leading
            # one: python-markdown renders a pipe-less table
            # (`Method | Parameters` / `--- | ---` / `` `save` | sync=True ``)
            # placed after a blank line below the real one, and that
            # survived a leading-pipe rule on both interpreters (PR #430).
            # Every `|` in this section today is on a table line.
            flagged.append(line)
    return flagged


def _output_vocabulary_block(page: str) -> str:
    """The page from `**Output vocabularies.**` to the next bold lead-in or heading.

    Bounded at whichever of `\\n**` and `\\n## ` comes first, so the
    block cannot run on into the next section, where a backticked
    frozen word would be a second mention for the wrong reason. Fails
    rather than returning `""` when the lead-in is gone.
    """
    lead = "**Output vocabularies.**"
    assert lead in page, f"stability.md has no {lead!r} block"
    block = page.split(lead, 1)[1]
    ends = [i for i in (block.find("\n**"), block.find("\n## ")) if i != -1]
    return block[:min(ends)] if ends else block


def _signature_rows(page: str) -> dict:
    """One of `docs/api/stability.md`'s signature tables as `name -> params`
    (pass it a `_table_run`; the Session table's names are bare, the
    second table's qualified).

    The duplicate check is here and not in the caller on purpose.
    `dict()` keeps the *last* match for a repeated key, so a false row
    placed above the true one leaves the page carrying a wrong signature
    with the pin green (#401, measured on 0.9.4: `6 passed`). Asserting
    on the pairs before the dict exists is the only place the second row
    is still visible -- the natural one-liner
    `len(rows) == len(FROZEN_SESSION_METHODS)` is green *with* the
    duplicate present, because `dict()` collapsed the two rows before
    anything counted them.

    Row *order* stays unpinned: the comparison is a dict and the page
    claims no order, so two swapped rows are green and correctly so.
    """
    pairs = _SIGNATURE_ROW.findall(page)
    names = [name for name, _ in pairs]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    assert not duplicated, (
        f"stability.md's Session table repeats {duplicated}; `dict()` keeps "
        f"the last, so a false row above the true one would be invisible")
    return dict(pairs)


def _owner_and_callable(qualified):
    """`"Owner.name"` from `FROZEN_OTHER_CALLABLES` -> `(callable,
    drop_self)`.

    The owners are classes this file already reaches without naming a
    probe target's module, plus `entities`, whose one frozen function is
    imported by name. An owner or a module-level name not listed here
    raises, so a new row cannot be skipped. `vars`, not `getattr`, for the
    presence check: a row names the class that defines the method, so an
    inherited one cannot stand in for it.
    """
    owners = {
        "Session": DicomSession,
        "IsocenterConfiguration": session_module.IsocenterConfiguration,
        "DicomItem": DicomItem,
        "Instance": Instance,
        "Builder": isocenter.Builder,
        "PhiReport": session_module.PhiReport,
        "DiscoveryResult": DiscoveryResult,
    }
    owner, name = qualified.split(".")
    if owner == "entities":
        assert name == "is_synthetic_patient_id", (
            f"{qualified}: teach _owner_and_callable this module-level name")
        return is_synthetic_patient_id, False
    assert owner in owners, f"{qualified}: teach _owner_and_callable its owner"
    assert name in vars(owners[owner]), f"{qualified} is not defined on {owner}"
    # A staticmethod has no `self`: its first parameter is a real one.
    # Dropping it anyway froze `start_patient(id, name)` as taking `name`
    # alone, and a rename of `id` was green (review of #787, mutant m1).
    return (getattr(owners[owner], name),
            not isinstance(vars(owners[owner])[name], staticmethod))


def _frozen_instance_fields_in_dataclass_order():
    return [f for f in _public_fields(Instance) if f in FROZEN_INSTANCE_FIELDS]


def _public_fields(cls):
    return [f.name for f in dataclasses.fields(cls) if not f.name.startswith("_")]


def test_the_frozen_session_surface_is_exactly_this():
    """T-F1: the method set, both directions, and every parameter.

    Killing mutations: any public method renamed, added or removed; any
    parameter renamed, reordered, or given a different default.
    """
    public = {n for n in vars(DicomSession) if not n.startswith("_")}
    assert public == set(FROZEN_SESSION_METHODS), (
        f"new public names must be frozen (a row here and on "
        f"docs/api/stability.md) or underscored; missing from the freeze: "
        f"{sorted(public - set(FROZEN_SESSION_METHODS))}; frozen but gone "
        f"(a 2.0): {sorted(set(FROZEN_SESSION_METHODS) - public)}")

    for name, params in FROZEN_SESSION_METHODS.items():
        assert _spell(getattr(DicomSession, name)) == params, (
            f"Session.{name}'s parameters changed (name, order, default or "
            f"kind); that is a 2.0")

    assert _spell(DicomSession._export_dicom).split(", ", 1) == [
        "folder", FROZEN_DICOM_EXPORT_OPTIONS], (
        "export(format='dicom', **options) accepts different option names")

    assert list(isocenter.__all__) == FROZEN_ALL
    assert isocenter.Session is DicomSession
    assert isinstance(isocenter.__version__, str) and isocenter.__version__

    # The second table (#26): every other tier-1 callable's parameters.
    # `callable()` was the whole pin for these until then.
    for qualified, params in FROZEN_OTHER_CALLABLES.items():
        target, drop_self = _owner_and_callable(qualified)
        spelled = _spell(target, drop_self=drop_self)
        assert spelled == params, (
            f"{qualified}'s parameters changed (name, order, default or "
            f"kind): {spelled!r}; that is a 2.0")
    assert NO_PATIENT_ID_PREFIX == FROZEN_NO_PATIENT_ID_PREFIX, (
        "the key a subject with no Patient ID is held under changed; "
        "get_cohort_report shows it and patient_ids selects by it (#584)")


def test_the_frozen_shapes_have_these_fields(tmp_path):
    """T-F2: the return shapes, the entity graph, the two exceptions.

    `IngestSummary` is reached through the facade -- `ingest()` on an
    empty directory returns one before any pool starts -- because the
    class is bound only in `io_handlers`, a probe target this file must
    not name. Killing mutation: any field renamed.
    """
    (tmp_path / "empty").mkdir()
    with DicomSession(str(tmp_path / "shapes.db")) as session:
        summary = session.ingest(str(tmp_path / "empty"))
        # The `wfdb` format's result (#26): a `List[str]` of the paths
        # written, empty when nothing was attempted. The page promised
        # every other shape the frozen methods return, and not this one.
        written = session.export(str(tmp_path / "wfdb"), format="wfdb")
    assert type(written) is list and written == [], written
    assert _public_fields(type(summary)) == ["ingested", "failures", "declined", "skipped"]
    assert hasattr(summary, "failed")

    assert _public_fields(session_module.ExportSummary) == ["written_uids", "failures"]
    for prop in ("written", "failed"):
        assert isinstance(getattr(session_module.ExportSummary, prop), property)

    assert _public_fields(session_module.PhiFinding) == [
        "entity_uid", "entity_type", "field_name", "value", "reason", "tag",
        "patient_id", "entity", "remediation_proposal", "metadata", "entity_path"]
    for dunder in ("__len__", "__iter__", "__getitem__"):
        assert dunder in vars(session_module.PhiReport)
    assert callable(session_module.PhiReport.to_dataframe)
    # `failures` joined the shape with #423. Always a list: omitted, it
    # is `[]`, never `None`, which every caller iterating it would trip on.
    assert session_module.PhiReport([]).failures == []
    assert session_module.PhiReport([], [("u", "r")]).failures == [("u", "r")]

    for method in ("filter", "to_zones", "to_dataframe"):
        assert callable(getattr(DiscoveryResult, method))
    assert issubclass(session_module.LockingResult, list)

    assert _public_fields(Equipment) == ["manufacturer", "model_name", "device_serial_number"]
    assert _public_fields(Patient) == ["patient_id", "patient_name", "studies"]
    assert _public_fields(Study) == [
        "study_instance_uid", "study_date", "series", "date_shifted", "study_time"]
    assert _public_fields(Series) == [
        "series_instance_uid", "modality", "series_number", "equipment", "instances"]
    # A superset, not equality: `pixel_array` and `waveform_array` are
    # public fields of `Instance` and tier 2 (stability.md).
    assert set(FROZEN_INSTANCE_FIELDS) <= set(_public_fields(Instance))
    # `Instance.date_shifted` was cut in 0.9.6 (#510) and must not come
    # back: it was a transient boolean standing for an unbounded set of
    # values, and the superset check above is green for a field that
    # reappears. Asserted in both directions -- gone from `Instance`,
    # still on `Study`, where it answers the entity-level question
    # honestly and `exporters/wfdb.py` reads it.
    assert "date_shifted" not in _public_fields(Instance)
    assert "date_shifted" in _public_fields(Study)
    # Which of them a constructor call can set is part of the shape: the
    # page used to write `Instance(..., attributes, sequences,
    # attribute_vrs, date_shifted)`, and none of those was an argument of
    # `__init__` -- nor is `date_shifted` a field at all since 0.9.6.
    init = {f.name: f.init for f in dataclasses.fields(Instance)}
    assert [n for n in FROZEN_INSTANCE_FIELDS if init[n]] == [
        "sop_instance_uid", "sop_class_uid", "instance_number", "file_path", "source_path"]
    assert [n for n in FROZEN_INSTANCE_FIELDS if not init[n]] == [
        "attributes", "sequences", "attribute_vrs"]
    for method in ("get_pixel_data", "set_pixel_data", "unload_pixel_data",
                   "discard_pixel_data", "get_waveform_data"):
        assert callable(getattr(Instance, method))
    assert callable(DicomItem.set_attr)

    config = session_module.IsocenterConfiguration
    for method in ("save", "add_rule", "update_rule", "delete_rule",
                   "set_phi_tag", "get_rule"):
        assert callable(getattr(config, method))
    assert {"rules", "phi_tags", "date_jitter", "remove_private_tags",
            "privacy_profile", "config_path", "auto_save"} <= set(_public_fields(config))

    assert issubclass(isocenter.RedactionError, RuntimeError)
    assert list(inspect.signature(isocenter.RedactionError.__init__).parameters) == [
        "self", "failures", "attempted"]
    assert issubclass(isocenter.ExportError, RuntimeError)
    assert list(inspect.signature(isocenter.ExportError.__init__).parameters) == [
        "self", "failures", "attempted", "folder"]


def test_the_api_reference_renders_every_frozen_session_method():
    """T-F3: `docs/api/session.md`'s `members:` block is a superset of the freeze.

    **Red on 0.9.3**: the page listed 16 of the 28, omitting `anonymize`,
    `lock_identities`, `enable_reversible_anonymization`,
    `recover_patient_identity`, `generate_report`, `export_dataframe` and
    six more -- half the pipeline the README teaches. Killing mutation: a
    method removed from `members:`.
    """
    page = (REPO / "docs" / "api" / "session.md").read_text(encoding="utf-8")
    block = page.split("members:", 1)[1]
    rendered = set(re.findall(r"^\s+-\s+(\w+)\s*$", block, re.M))

    missing = set(FROZEN_SESSION_METHODS) - rendered
    assert not missing, (
        f"docs/api/session.md does not render these frozen methods: "
        f"{sorted(missing)}")


def test_the_stability_page_names_every_tier_one_session_method():
    """T-F4: the page users read names each frozen method, and mkdocs renders it.

    One direction only -- the page may name more. `mkdocs.yml`'s nav must
    list it, because `tests/test_doc_anchors.py` renders nav pages and an
    unlisted page is unchecked.
    """
    page = (REPO / "docs" / "api" / "stability.md").read_text(encoding="utf-8")
    unnamed = [name for name in FROZEN_SESSION_METHODS if f"`{name}`" not in page]
    assert not unnamed, f"docs/api/stability.md does not name {unnamed}"

    # The page's table *is* the pin, row for row: a `| `name` | `params` |`
    # row per method, `—` for no parameters.
    rows = _signature_rows(_table_run(page, _SESSION_TABLE_HEADER))
    assert rows == FROZEN_SESSION_METHODS, (
        "stability.md's Session table and the pins disagree: "
        f"{ {k: (rows.get(k), v) for k, v in FROZEN_SESSION_METHODS.items() if rows.get(k) != v} }")
    # The second table, "Other frozen callables" (#26), row for row too.
    other = _signature_rows(_table_run(page, _OTHER_TABLE_HEADER))
    assert other == FROZEN_OTHER_CALLABLES, (
        "stability.md's Other frozen callables table and the pins disagree: "
        f"{ {k: (other.get(k), FROZEN_OTHER_CALLABLES.get(k)) for k in set(other) | set(FROZEN_OTHER_CALLABLES) if other.get(k) != FROZEN_OTHER_CALLABLES.get(k)} }")
    # Prose wraps at 72 columns; a list of names may cross a line break.
    flat = " ".join(page.split())
    assert f"`{FROZEN_DICOM_EXPORT_OPTIONS}`" in flat, "the dicom export options on the page moved"

    # Entity fields as the page lists them: dataclass order, which is
    # `dataclasses.fields` order, not the order someone remembers.
    for cls in (Equipment, Patient, Study, Series):
        assert f"`{', '.join(_public_fields(cls))}`" in flat, (
            f"stability.md does not list {cls.__name__}'s fields in dataclass order: "
            f"{_public_fields(cls)}")
    ordered = _frozen_instance_fields_in_dataclass_order()
    assert ordered == ["attributes", "sequences", "attribute_vrs", "sop_instance_uid",
                       "sop_class_uid", "instance_number", "file_path",
                       "source_path"], ordered
    for group in (ordered[:3], ordered[3:]):
        assert f"`{', '.join(group)}`" in flat, f"stability.md does not list {group} together"
    # The page has to *say* the field is gone, not merely stop listing
    # it: a reader who programmed against `instance.date_shifted` needs
    # to be told, and a page that simply omitted it would read as an
    # oversight. The other half of mutant M23 (#510).
    assert "`Instance` carried a `date_shifted` field until 0.9.6" in flat, (
        "stability.md does not say that Instance.date_shifted is gone")
    # The report's shape as the page spells it, so the page and the class
    # cannot drift apart (#423 added `failures` to both).
    assert "`PhiReport(findings, failures)`" in flat, (
        "stability.md does not list PhiReport's fields as (findings, failures)")
    # The unload/discard rule as frozen, including what a discard undoes
    # (#434): the descriptors `set_pixel_data()` wrote go with the pixels.
    # The behaviour is pinned by the R tests in
    # `tests/test_descriptor_edit_with_pixels_unloaded.py`; this pins that
    # the page a 1.0 user reads says so, rather than leaving both
    # readings of "throws it away" open.
    assert ("`discard` throws it away, with the descriptors "
            "`set_pixel_data()` wrote for it") in flat, (
        "stability.md's unload/discard rule no longer says discard puts "
        "back the descriptors set_pixel_data() wrote (#434)")
    # And a descriptor edit made between the set and the discard goes back
    # with them (#434, Q3 (a)): while the replacement is resident, that edit
    # describes the replacement. The behaviour is pinned by
    # `test_a_descriptor_edit_between_the_set_and_the_discard_is_reverted_too`.
    assert ("and a `set_attr()` edit to any of those descriptors made "
            "since the set") in flat, (
        "stability.md's unload/discard rule no longer says discard also "
        "reverts a descriptor edit made since the set (#434, Q3)")
    # One reading rule for both places samples live (#595, Q1 (A)): a
    # file-backed instance reads under its own descriptors, as a stored
    # one has since #417. Pinned in behaviour by
    # `tests/test_a_file_backed_instance_reads_as_its_descriptors_declare.py`;
    # this pins that the promise a 1.0 user reads was not narrowed back to
    # the store alone.
    assert ("whether the samples are in the store or in the file an "
            "`Instance(file_path=...)` names") in flat, (
        "stability.md no longer says get_pixel_data() reads a file-backed "
        "instance under its own descriptors (#595)")

    # The *union* is what is frozen, so the union is what this checks:
    # each word appears **exactly once**, backticked, inside the Output
    # vocabularies block.
    #
    # Not "somewhere on the page", which is what this asked until #415:
    # `DATA_LOSS` is also named in the Behaviours paragraph, so deleting
    # it from the list of frozen audit words left this file green
    # (measured). Scoped to the block, and exactly once within it, a word
    # dropped from its bullet is red whatever else the page says. One
    # direction only: the block may backtick more than the freeze (it
    # names `FAIL` to say there is none).
    #
    # The residual, accepted: two coordinated edits -- a second mention
    # added inside the block, then the one in the list deleted -- stay
    # green, because the word is still named in the frozen block, which
    # is what the tag promises.
    #
    # Which bullet a word sits under is prose, and deliberately unpinned.
    # Moving `REMOVE_TAG`/`REPLACE_TAG`/`SHIFT_DATE` back into the audit
    # bullet -- reinstating the exact miscategorisation #396 corrected --
    # leaves this file green, measured. That is the correct scope (the
    # tag promises the words, not the paragraph they are filed under),
    # but do not read the five bullets as machine-checked: only
    # `_audit_action_types` and `_proposal_action_types` know the
    # difference, and they read the package, not the page.
    block = _output_vocabulary_block(page)
    for word in sorted(FROZEN_VOCABULARY):
        assert block.count(f"`{word}`") == 1, (
            f"stability.md's Output vocabularies block names the frozen word "
            f"{word} {block.count(f'`{word}`')} times, backticked; it must "
            f"name it exactly once")

    nav = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    assert "api/stability.md" in nav, "docs/api/stability.md is not in mkdocs.yml's nav"


def test_a_duplicate_signature_row_is_not_silently_collapsed():
    """T-F4's parser must see a second row for a method, not keep the last.

    The defect this pins (#401): `dict(re.findall(...))` keeps the last
    match, so a false `| `save` | `wrong=True` |` row inserted *above*
    the true one left this file at `6 passed` while the page a user
    reads carried a signature the code does not have.

    The clean half is not decoration: a `_signature_rows` that raised
    unconditionally would satisfy the `pytest.raises` half alone, and a
    duplicate-detector that rejects every page detects nothing.
    """
    clean = "| `save` | `sync=False` |\n| `close` | — |\n"
    assert _signature_rows(clean) == {"save": "sync=False", "close": ""}

    duplicated = "| `save` | `wrong=True` |\n| `save` | `sync=False` |\n"
    with pytest.raises(AssertionError, match="repeats"):
        _signature_rows(duplicated)


def test_every_pipe_line_in_the_frozen_section_is_a_recognised_row():
    """#415: the Session table holds nothing `_signature_rows` did not read.

    The live page, whole: `_signature_rows` is what compares the rows to
    the pins, and this is what says there was nothing else to compare.
    Killing mutation: `| `save` | sync=True |` inserted above the true
    row, which T-F4 alone reads past (#415, measured on 0.9.5).
    """
    page = (REPO / "docs" / "api" / "stability.md").read_text(encoding="utf-8")
    assert _unrecognised_table_lines(page) == [], (
        "stability.md's frozen section has table lines the signature parser "
        "cannot read, so no test checked what they say")


def test_an_unrecognised_row_is_reported_not_skipped():
    """#415: the companion flags what the parser skips, and only that.

    Two halves, the #401 shape. The bad half alone is satisfied by a
    helper that flags every line; the clean half alone by one that flags
    nothing. The clean table carries the frame lines and a `—` row, so
    a helper that forgot either exception is red here too.
    """
    heading = "## Frozen at 1.0\n\n"
    clean = (heading + "| Method | Parameters |\n| --- | --- |\n"
             "| `save` | `sync=False` |\n| `close` | — |\n\n## Next\n")
    assert _one_table(clean) == []

    bad_row = "| `save` | sync=True |"
    bad = clean.replace("| `save` | `sync=False` |",
                        f"{bad_row}\n| `save` | `sync=False` |", 1)
    assert _signature_rows(bad) == {"save": "sync=False", "close": ""}, (
        "the parser read the unbackticked row; this test's premise is gone")
    assert _one_table(bad) == [bad_row]

    # Indented: found despite the indent, and reported rather than read.
    indented = clean.replace("| `close` | — |", "  | `close` | — |", 1)
    assert _one_table(indented) == ["  | `close` | — |"]

    # No leading pipe: python-markdown renders it as a row, so it is one
    # (PR #430). Caught because it sits in the table's run of lines.
    pipeless_row = "`save` | sync=True"
    pipeless = clean.replace("| `save` | `sync=False` |",
                             f"{pipeless_row}\n| `save` | `sync=False` |", 1)
    assert _one_table(pipeless) == [pipeless_row]

    # In the section but after the table's run: still a table line, and
    # found through its indent.
    stray = clean.replace("\n\n## Next", "\n\nProse.\n  | stray |\n\n## Next", 1)
    assert _one_table(stray) == ["  | stray |"]

    # A second table after a blank line, written without pipes at either
    # end: python-markdown renders it, so every line of it is reported.
    second = clean.replace(
        "| `close` | — |\n",
        "| `close` | — |\n\nMethod | Parameters\n--- | ---\n`save` | sync=True\n", 1)
    assert _one_table(second) == [
        "Method | Parameters", "--- | ---", "`save` | sync=True"]

    # Outside the frozen section is outside the promise.
    assert _one_table(clean + f"{bad_row}\n") == []

    with pytest.raises(AssertionError, match="Frozen at 1.0"):
        _one_table(clean.replace("Frozen at 1.0", "Frozen"))
    with pytest.raises(AssertionError, match="Method"):
        _one_table(clean.replace("| Parameters |", "| Signature |"))


def _one_table(page):
    """The companion over a synthetic page holding the Session table alone."""
    return _unrecognised_table_lines(page, headers=(_SESSION_TABLE_HEADER,))


_TWO_TABLES = (
    "## Frozen at 1.0\n\n"
    "| Method | Parameters |\n| --- | --- |\n| `save` | `sync=False` |\n\n"
    "Prose.\n\n"
    "| Callable | Parameters |\n| --- | --- |\n"
    "| `Instance.set_pixel_data` | `array` |\n| `Instance.get_pixel_data` | — |\n"
    "\n## Next\n")


def test_the_second_table_is_read_and_checked_on_its_own_run():
    """#26: the dotted table is a table, and each table is parsed alone.

    Both halves, the #401 shape again: the clean page reads as two dicts
    that do not bleed into each other, and an unbackticked row in the
    second table, or its header respelled, is reported. Killing
    mutations: the widened row pattern narrowed back to `\\w+` (the second
    table's rows go unread and every one is flagged), and `_table_run`
    running past the blank line (the two dicts merge).
    """
    assert _unrecognised_table_lines(_TWO_TABLES) == []
    assert _signature_rows(_table_run(_TWO_TABLES, _SESSION_TABLE_HEADER)) == {
        "save": "sync=False"}
    assert _signature_rows(_table_run(_TWO_TABLES, _OTHER_TABLE_HEADER)) == {
        "Instance.set_pixel_data": "array", "Instance.get_pixel_data": ""}

    bad_row = "| `Instance.set_pixel_data` | array |"
    bad = _TWO_TABLES.replace("| `Instance.set_pixel_data` | `array` |", bad_row, 1)
    assert _unrecognised_table_lines(bad) == [bad_row]

    with pytest.raises(AssertionError, match="Callable"):
        _unrecognised_table_lines(_TWO_TABLES.replace("| Callable |", "| Name |"))
    with pytest.raises(AssertionError, match="Callable"):
        _table_run(_TWO_TABLES.replace("| Callable |", "| Name |"), _OTHER_TABLE_HEADER)


def test_a_staticmethod_row_keeps_its_first_parameter():
    """Review of #787, finding 1: `_owner_and_callable` said `drop_self`
    for every class owner, so `_spell` dropped a staticmethod's first real
    parameter and `start_patient(id, name)` was pinned as `name`; renaming
    `id` was green. The helper's answer for each kind of row, and the
    spelling it leads to for the one staticmethod."""
    assert _owner_and_callable("Builder.start_patient")[1] is False
    assert _owner_and_callable("Instance.set_pixel_data")[1] is True
    assert _owner_and_callable("entities.is_synthetic_patient_id")[1] is False
    target, drop_self = _owner_and_callable("Builder.start_patient")
    assert _spell(target, drop_self=drop_self).split(", ")[0] == "patient_id"


def test_the_wfdb_result_is_a_list_of_path_strings(tmp_path):
    """`export(format='wfdb')` -> `List[str]`, the paths written (#26).

    T-F2 pins the empty case (`[]`), which says nothing about the
    elements; this exports pydicom's bundled `waveform_ecg.dcm`, so the
    list is not empty, and pins each element as a `str` naming a file
    that exists. Killing mutation: the exporter returning `Path` objects,
    or a tuple.
    """
    import shutil
    from pydicom.data import get_testdata_file
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(get_testdata_file("waveform_ecg.dcm"), src / "ecg.dcm")
    with DicomSession(str(tmp_path / "wfdb.db")) as session:
        session.ingest(str(src))
        written = session.export(str(tmp_path / "out"), format="wfdb")
    assert type(written) is list and written, written
    assert all(type(path) is str and pathlib.Path(path).exists() for path in written), written


def _tier_one_class_instances(session, summary):
    """`class name -> (class, instance or None)` for every class
    `TIER_ONE_NAMES` lists.

    An instance is built wherever the class keeps a per-object `__dict__`,
    because an attribute set in `__init__` (`DiscoveryResult.n_sources`,
    `PhiReport.findings`) is on the object and not in `vars(cls)`; reading
    the class alone left four such names in no tier with this test green
    (review of #787, mutant m5). A slots class holds a descriptor per
    attribute in `vars(cls)` and needs none. `IngestSummary` and
    `DicomStore` come through the facade (`ingest()`'s result and
    `session.store`), because each is bound only in a probe target this
    file must not name.
    """
    instances = {
        session_module.IsocenterConfiguration: session.configuration,
        session_module.PhiReport: session_module.PhiReport([]),
        session_module.ExportSummary: session_module.ExportSummary([], []),
        DiscoveryResult: DiscoveryResult([], 0),
        type(summary): summary,
        type(session.store): session.store,
        isocenter.RedactionError: isocenter.RedactionError([], 0),
        isocenter.ExportError: isocenter.ExportError([], 0),
    }
    classes = (TrackedEntity, DicomItem, Instance, Patient, Study, Series,
               Equipment, session_module.IsocenterConfiguration,
               session_module.PhiReport, session_module.PhiFinding,
               session_module.ExportSummary, DiscoveryResult,
               isocenter.Builder, type(summary), type(session.store),
               session_module.LockingResult, isocenter.RedactionError,
               isocenter.ExportError)
    return {cls.__name__: (cls, instances.get(cls)) for cls in classes}


def _public_names(cls, instance=None):
    """What a program can reach on `cls` by name: the public names in
    `vars(cls)`, the public dataclass fields, and the public attributes an
    instance carries in its own `__dict__`. `Instance` is a slots
    dataclass, so `vars` holds a member descriptor per field; the set
    dedupes the two."""
    names = {n for n in vars(cls) if not n.startswith("_")}
    if dataclasses.is_dataclass(cls):
        names |= set(_public_fields(cls))
    if instance is not None:
        assert hasattr(instance, "__dict__"), f"{cls.__name__}: no __dict__ to read"
        names |= {n for n in vars(instance) if not n.startswith("_")}
    return names


#: How stability.md spells an owner whose class name is not the one a user
#: types: `Builder` is `DicomBuilder`'s exported name.
_PAGE_OWNER = {"DicomBuilder": "Builder"}


def _page_paragraphs(section):
    """`section` cut into paragraphs, and each bullet its own paragraph, so
    an owner named in one bullet cannot vouch for a name in the next."""
    out = []
    for block in section.split("\n\n"):
        out += re.split(r"\n(?=- )", block)
    return out


def _named_under_owner(paragraphs, owner, name):
    """True when some paragraph that names `owner` also backticks `name`
    as a word. A bare word match anywhere on the page was the first
    version, and `written` -- an `ExportSummary` property -- vouched for a
    method of that name added to `Instance` (review of #787, m2)."""
    owner_word = re.compile(rf"\b{_PAGE_OWNER.get(owner, owner)}\b")
    name_word = re.compile(rf"\b{name}\b")
    return any(owner_word.search(para)
               and any(name_word.search(span) for span in re.findall(r"`([^`]+)`", para))
               for para in paragraphs)


def _tier_sections(page):
    """`(tier 1 section, tier 2 section)` of stability.md: `## Frozen at
    1.0` and `## Documented but internal`, each to the next `## `."""
    internal = "## Documented but internal"
    assert internal in page, f"stability.md has no {internal!r} section"
    return (_frozen_section(page),
            page.split(internal, 1)[1].split("\n## ", 1)[0])


def test_every_public_name_on_a_tier_one_class_is_classified(tmp_path):
    """#26: a public name on a tier-1 class is in tier 1 or tier 2, by name,
    and the page names it in that tier's section.

    T-F1 works in both directions for `Session` only. On every other
    class tier 1 names, a new public method belonged to no tier -- the
    page's tier 3 is "a leading underscore, and every module not listed"
    -- and so became frozen by default the moment someone relied on it.
    Measured at 3fe24bee: seven names on `Instance`, `DicomItem` and
    `Study` were on no list; review of #787 found four instance attributes
    (`DiscoveryResult.candidates`, `n_sources`, `PhiReport.findings`,
    `failures`) and `DicomStore`'s five methods besides.

    Both directions: a public name no list classifies is red (classify it
    on the page and here, or underscore it), and a listed name the class
    no longer defines is red (a tier-1 one is a 2.0; a tier-2 one needs
    its CHANGELOG entry and its line here removed).

    **The tier is read from the page too.** A tier-1 name must be
    backticked in a paragraph of `## Frozen at 1.0` that names its owner,
    and a tier-2 name in a paragraph of `## Documented but internal` that
    does. Checked anywhere on the page, deleting `Instance.regenerate_uid()`
    from the tier-2 list stayed green because the tier-1 section names it
    in the `subset=` sentence -- a tier-2 method readable only as a tier-1
    promise (review of #787, m3). A base's names are looked for under the
    base (`TrackedEntity`) or under the subclass that carries them.

    Killing mutations: a new public method or instance attribute on any of
    these classes; a listed name removed from the class; a name removed
    from its tier's section, or named only in the other one.
    """
    (tmp_path / "empty").mkdir()
    with DicomSession(str(tmp_path / "classify.db")) as session:
        summary = session.ingest(str(tmp_path / "empty"))
        classes = _tier_one_class_instances(session, summary)
        public_by_class = {name: _public_names(cls, instance)
                           for name, (cls, instance) in classes.items()}
    assert set(classes) == set(TIER_ONE_NAMES), sorted(set(classes) ^ set(TIER_ONE_NAMES))
    assert set(TIER_TWO_NAMES) <= set(TIER_ONE_NAMES)

    page = (REPO / "docs" / "api" / "stability.md").read_text(encoding="utf-8")
    tier_paragraphs = [_page_paragraphs(section) for section in _tier_sections(page)]
    for name, (cls, _instance) in sorted(classes.items()):
        tiers = (TIER_ONE_NAMES[name], TIER_TWO_NAMES.get(name, set()))
        assert not tiers[0] & tiers[1], f"{name}: a name is in both tiers"
        allowed = set().union(*(TIER_ONE_NAMES.get(base.__name__, set())
                                | TIER_TWO_NAMES.get(base.__name__, set())
                                for base in cls.__mro__))
        public = public_by_class[name]
        unclassified = sorted(public - allowed)
        assert not unclassified, (
            f"{name} has public names in no tier: {unclassified}. Classify "
            f"each on docs/api/stability.md (tier 1 or tier 2) and in this "
            f"file's TIER_ONE_NAMES/TIER_TWO_NAMES, or give it a leading "
            f"underscore (#26)")
        gone = sorted((tiers[0] | tiers[1]) - public)
        assert not gone, f"{name} no longer defines {gone}, which the tiers list"
        # The owners a name may be filed under: the class, and for an
        # inherited name each subclass checked here that carries it.
        owners = {name} | {other for other, (sub, _i) in classes.items()
                           if sub is not cls and issubclass(sub, cls)}
        for tier, (names, paragraphs) in enumerate(zip(tiers, tier_paragraphs), 1):
            unnamed = sorted(n for n in names
                             if not any(_named_under_owner(paragraphs, owner, n)
                                        for owner in owners))
            assert not unnamed, (
                f"stability.md's tier-{tier} section does not name {name}'s "
                f"{unnamed} in a paragraph that names {sorted(owners)}")


def _analytics_grade_conditions():
    """The numbered list under `### How the grade is decided`, as
    `(number, bold lead)` pairs. Only top-level items: condition 2's
    sub-bullets are indented and are not conditions."""
    page = (REPO / "docs" / "analytics.md").read_text(encoding="utf-8")
    heading = "### How the grade is decided"
    assert heading in page, f"docs/analytics.md has no {heading!r}"
    section = page.split(heading, 1)[1]
    end = "**What `PASS` does not mean.**"
    assert end in section, f"docs/analytics.md's grade section no longer ends at {end!r}"
    section = section.split(end, 1)[0]
    return re.findall(r"^(\d+)\. \*\*(.+?)\*\*", section, re.MULTILINE)


def test_the_documented_grade_conditions_are_the_frozen_count():
    """#26, spec §1.5: the doc's list has exactly the frozen conditions.

    A condition removed from the page is red, and so is one added to it
    without a row here -- which is the row that names its killing test.
    The numbers must run 1..N, because stability.md and the report's
    Grade Basis refer to conditions by number.
    """
    items = _analytics_grade_conditions()
    assert [int(n) for n, _ in items] == list(range(1, len(FROZEN_GRADE_CONDITIONS) + 1)), items


def _review_reasons_appends(grader):
    """The `review_reasons.append(...)` calls in `grader`, after checking
    that every other use of the name is on an allow-list: its one
    `review_reasons = []`, and `return review_reasons`.

    Counting `append` calls alone read past every other way a list grows:
    `review_reasons.extend([...] if ... else [])` before the `return` is a
    ninth condition, and all of this file was green under it (review of
    #787, m4). Anything else -- `extend`, `insert`, `+=`, a slice write, an
    alias, the list handed to a helper -- raises, naming the shape, so a
    condition must be added as an `append`.
    """
    parents = {child: node for node in ast.walk(grader)
               for child in ast.iter_child_nodes(node)}
    appends, initialisers = [], 0
    for node in ast.walk(grader):
        if not (isinstance(node, ast.Name) and node.id == "review_reasons"):
            continue
        parent = parents.get(node)
        if (isinstance(parent, ast.Attribute) and parent.attr == "append"
                and isinstance(parents.get(parent), ast.Call)
                and parents[parent].func is parent):
            appends.append(parents[parent])
        elif (isinstance(parent, ast.Assign) and parent.targets == [node]
              and isinstance(parent.value, ast.List) and not parent.value.elts):
            initialisers += 1
        elif isinstance(parent, ast.Return) and parent.value is node:
            pass
        else:
            raise AssertionError(
                f"review_reasons is used as {ast.unparse(parent)!r} at line "
                f"{node.lineno}; a grade condition is added as one "
                f"`review_reasons.append(...)`, which this pin counts")
    assert initialisers == 1, f"review_reasons is bound {initialisers} times"
    return appends


def _grader(tree):
    (grader,) = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_review_reasons"]
    return grader


def test_the_grading_function_appends_one_reason_per_frozen_condition():
    """#26, spec §1.5: `_review_reasons` holds one `append` per condition,
    and grows its list no other way.

    Read by path and AST, found by name rather than by line. Condition 6
    appends inside a `for` over the verbs, and is one site. A deleted
    site is red here as well as in its killing test; an added one is red
    until the page, `FROZEN_GRADE_CONDITIONS` and a test are added with it.
    """
    sites = _review_reasons_appends(_grader(_module_tree("isocenter", "session.py")))
    assert len(sites) == len(FROZEN_GRADE_CONDITIONS), [s.lineno for s in sites]


@pytest.mark.parametrize("growth", [
    'review_reasons.extend(["ninth"] if audit_summary else [])',
    'review_reasons += ["ninth"]',
    'review_reasons.insert(0, "ninth")',
    'review_reasons[:0] = ["ninth"]',
    'reasons = review_reasons',
    '_more(review_reasons)',
])
def test_the_grade_reader_refuses_a_list_it_cannot_count(growth):
    """The review's m4 and its relatives, synthetic: each raises. The
    control, the shape the grader has today, reads back one `append`."""
    src = ("def _review_reasons(*, audit_summary):\n"
           "    review_reasons = []\n"
           "    if not audit_summary:\n"
           "        review_reasons.append('none')\n"
           "    # GROW\n"
           "    return review_reasons\n")
    assert len(_review_reasons_appends(_grader(ast.parse(src)))) == 1
    with pytest.raises(AssertionError, match="review_reasons is used as"):
        _review_reasons_appends(_grader(ast.parse(src.replace("# GROW", growth))))


def test_each_grade_condition_names_a_test_that_exists():
    """#26, spec §1.5: each condition's killing test is a real test.

    Parsed by AST from the named file, so a renamed or deleted test is
    red here rather than leaving a condition pinned by nothing. A
    `[id]` suffix must be one of that test's `parametrize` ids, spelled as
    a string constant in the file.
    """
    keys = [key for key, _ in FROZEN_GRADE_CONDITIONS]
    assert len(set(keys)) == len(keys), keys
    targets = [target for _, target in FROZEN_GRADE_CONDITIONS]
    assert len(set(targets)) == len(targets), "two conditions name one test"
    for key, target in FROZEN_GRADE_CONDITIONS:
        path, _, test = target.partition("::")
        name, _, param_id = test.partition("[")
        tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
        functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        assert name in functions, f"{key!r}: {path} has no test {name}"
        if param_id:
            ids = {_string(n) for n in ast.walk(tree)}
            assert param_id.rstrip("]") in ids, f"{key!r}: {target} names no such id"


def test_the_stability_page_says_what_the_freeze_covers():
    """#26: the rest of spec §1 as the page states it.

    - Module-level names: `entities.NO_PATIENT_ID_PREFIX` and
      `entities.is_synthetic_patient_id()` are tier 1 (the function's row
      is in the second table, pinned by T-F1 and T-F4), and the constant's
      value is spelled on the page.
    - `profiles.FLOOR_POLICY` is tier 2 and `profiles.py` is otherwise
      private: the tier-1 prose used the name while the tier-3 rule
      claimed its module.
    - The `wfdb` result and the accepted report and manifest formats.
    - The `subset=` limit, generalised to `regenerate_uid()` (review of
      #780), is pinned in behaviour by
      `test_an_unknown_subset_uid_is_counted.py`; this pins the sentence.

    Killing mutations: each sentence below deleted or moved to the
    other tier.
    """
    page = (REPO / "docs" / "api" / "stability.md").read_text(encoding="utf-8")
    frozen = " ".join(_frozen_section(page).split())
    internal = " ".join(page.split("## Documented but internal", 1)[1]
                        .split("\n## ", 1)[0].split())
    private = " ".join(page.split("\n## Private", 1)[1].split())

    assert ("**Module-level names.** `entities.NO_PATIENT_ID_PREFIX` "
            "(`\"\\\\no-patient-id\\\\\"`) and `entities.is_synthetic_patient_id()`"
            ) in frozen, "the module-level names are not tier 1 on the page"
    assert "`profiles.FLOOR_POLICY`" in internal
    assert "`profiles.FLOOR_POLICY`" not in frozen, (
        "FLOOR_POLICY is tier 2; the tier-1 prose may describe the floor, not "
        "promise the name")
    assert "`profiles.py` except `FLOOR_POLICY` (tier 2)" in private

    assert ("`export(format='wfdb')` → `List[str]`, the paths written, empty "
            "when nothing was attempted") in frozen
    assert ("`generate_report(format=)` accepts `'markdown'` only, and "
            "`generate_manifest(format=)` `'html'` and `'json'`; any other "
            "spelling raises `ValueError`") in frozen
    # Owner rulings Q1 and Q2 on #789, frozen here: the lock's report arm
    # (the `patient_ids` paragraph said every non-`str` element raised),
    # and the constructor's cwd key. Their behaviour is pinned in
    # `test_lock_identities_signature.py`.
    assert ("with one exception: the two lock methods also take a "
            "`PhiReport`, and `PhiFinding` elements mixed with IDs, and lock "
            "the patients those findings name") in frozen
    assert ("When a file named `isocenter.key` exists in the current working "
            "directory at construction, `Session()` calls "
            "`enable_reversible_anonymization()` with it") in frozen
    assert ("With no such file, reversible anonymization stays off and no "
            "key is created.") in frozen
    # D1 of the delta review: the third arm, pinned in behaviour by
    # `test_a_malformed_key_in_the_working_directory_makes_session_raise`.
    assert ("A malformed one makes `Session()` raise its `ValueError`."
            ) in frozen
    # D2 and E1: the report arm locks the patients with a finding, not
    # every patient scanned. After `anonymize()` it names no patient whose
    # ID was replaced; an ID-less subject (#584) or a KEEP on `0010,0020`
    # is not replaced, so the sentence says only what holds for all three.
    assert ("The report `audit()` returns locks every patient with at least "
            "one finding, by the Patient ID the finding holds. After "
            "`anonymize()` has replaced a patient's ID, the report no longer "
            "names that patient; lock before `anonymize()`.") in frozen
    assert ("except a SOP Instance UID that is none of these three: the one "
            "the first move of the instance's UID (by `anonymize()`, "
            "`redact()` or `Instance.regenerate_uid()`) left, which is the "
            "ingested one unless `sop_instance_uid` was assigned before it; "
            "that UID's `anonymize()` replacement; and the current one. A UID "
            "taken between a first redaction and a `force=True` second, or "
            "between two `Instance.regenerate_uid()` calls, is such a UID"
            ) in frozen


def test_the_plugins_pillar_says_what_the_registry_subsection_says():
    """#26 x #527: the opening promise and the registry subsection agree.

    #786 made the exporter registry provisional until 1.1 (1.1 may replace
    the four names, a plugin pins `<1.1`, no third-party export grades
    PASS) in its own subsection of tier 2. "What 1.0 promises" had been
    written against the plain tier-2 wording ("changeable in 1.x"), so
    its Plugins bullet must link to that subsection and use its terms,
    or the page's first screen promises less caution than its body.

    `entities.exported_patient_id()` is what the registry page tells a
    plugin author to call; it is tier 2 on the same terms, so it is
    listed with the other `entities` helpers and is not tier 1.

    Killing mutations: the bullet's link pointed back at the tier-2
    heading; "provisional", the `<1.1` pin or the PASS sentence dropped
    from the bullet; `exported_patient_id` dropped from the helpers
    bullet or promoted into the module-level names.
    """
    page = (REPO / "docs" / "api" / "stability.md").read_text(encoding="utf-8")
    promises = page.split("## What 1.0 promises", 1)[1].split("\n## ", 1)[0]
    bullet = " ".join(
        promises.split("- **Plugins, provisionally.**", 1)[1]
        .split("\n- ", 1)[0].split())
    assert "(#the-exporter-registry-provisional-until-11)" in bullet
    assert "(#documented-but-internal)" not in bullet
    assert "provisional until 1.1" in bullet
    assert "`isocenter>=1.0,<1.1`" in bullet
    assert "no third-party export grades `PASS`" in bullet

    headings = [line for line in page.splitlines() if line.startswith("### ")]
    assert "### The exporter registry: provisional until 1.1" in headings

    frozen = " ".join(_frozen_section(page).split())
    internal = " ".join(page.split("## Documented but internal", 1)[1]
                        .split("\n## ", 1)[0].split())
    assert "exported_patient_id" not in frozen
    assert ("**`entities` helpers** `clone_sequences`, `exported_patient_id`, "
            "`iter_item_tree`") in internal
    # The key a plugin must not write (docs/api/exporters.md names it, so
    # tier 2 by the page's own definition; ruling on #793).
    assert "SOURCE_SOP_UID_ATTR" not in frozen
    assert "`SOURCE_SOP_UID_ATTR`" in internal


def test_the_audit_action_types_written_are_exactly_the_frozen_thirteen():
    """Pin A (#396, #411): the words the audit table is handed, by AST.

    Nine were pinned by #396; #411 froze the four `REMEDIATION_*` words
    and taught the collector to read a word passed as a name, which is
    how remediation passes all four. Killing mutations include a
    respelling of the local `action_type` in any remediation arm, or of
    the `REMEDIATION_DECLINED` module constant.

    **This replaces a grep**, and the replacement is the fix. The old
    test asked only that each word appear as a quoted literal somewhere
    under the package -- which a docstring, a comment, a SQL string or a
    log-level map satisfies. Measured on 0.9.4: respelling
    `action_type="WARNING"` at its *only* write site left that test at
    `6 passed`, because the level map in the logging module still spelled
    the word.

    Set equality, so a single-site respelling of a word other sites still
    spell is red: the mutant word enters the collected set even though
    the original stays in it.

    **A set, not a census.** A census (`word -> number of sites`) would
    also catch a deleted write site, and would go red on every honest
    refactor that adds or merges one. Deleting a write site is a
    *behavioural* change -- an audit row that stops being written -- and
    belongs to the test asserting that row exists, not to a pin on how
    the word is spelled.
    """
    assert _audit_action_types() == FROZEN_AUDIT_ACTION_TYPES


def _synthetic(src):
    tree = ast.parse(src)
    return tree, {child: node for node in ast.walk(tree)
                  for child in ast.iter_child_nodes(node)}


@pytest.mark.parametrize("signature", [
    'finding, action_type="REMEDIATION_RMV"',
    'finding, *, action_type="REMEDIATION_RMV"',
])
def test_pin_a_refuses_an_action_type_that_is_a_parameter(signature):
    """Pin A raises when the resolved name is a parameter of its function.

    Synthetic, because no site in the package has this shape, so there
    is no production line to mutate. Reading only the body's
    assignments, the resolver reported `{"REMEDIATION_REMOVE"}` here and
    never saw the default `REMEDIATION_RMV` a caller would get (PR #430).
    The keyword-only case keeps the check from reading `args` alone. The
    control: without the parameter, the same body resolves to its word.
    """
    src = (f"def helper({signature}):\n"
           f"    if finding:\n"
           f"        action_type = \"REMEDIATION_REMOVE\"\n"
           f"    log_audit(action_type, 1, 2)\n")
    tree, parents = _synthetic(src)
    arg = next(_calls_named(tree, "log_audit")).args[0]
    with pytest.raises(AssertionError, match="parameter of helper"):
        _action_type_words(arg, {}, parents, "synthetic")

    tree, parents = _synthetic(src.replace(signature, "finding"))
    arg = next(_calls_named(tree, "log_audit")).args[0]
    assert _action_type_words(arg, {}, parents, "synthetic") == {"REMEDIATION_REMOVE"}


#: Remediation's module, as path segments: this file never spells a probe
#: target's dotted name (see `_package_trees`).
_REMEDIATION = REPO / "isocenter" / "remediation.py"


def _remediation_evidence_set(tree):
    """`REMEDIATION_ACTION_TYPES`'s members, read from the module's AST.

    Only `NAME = frozenset({"WORD", ...})` at module level is read, and
    anything else **raises**, naming the shape. Returning an empty set for
    a shape it cannot read would make the pin below compare against
    nothing: red for the wrong reason, or green if the collector also went
    empty. The same fail-loud rule `_action_type_words` follows.
    """
    bindings = [node for node in tree.body
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(isinstance(t, ast.Name) and t.id == "REMEDIATION_ACTION_TYPES"
                        for t in (node.targets if isinstance(node, ast.Assign)
                                  else [node.target]))]
    assert len(bindings) == 1, (
        f"expected exactly one module-level `REMEDIATION_ACTION_TYPES = ...` "
        f"in the remediation module, found {len(bindings)}; the #429 pin "
        f"cannot read the ANONYMIZE evidence set")
    value = bindings[0].value
    readable = (isinstance(bindings[0], ast.Assign)
                and isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name) and value.func.id == "frozenset"
                and not value.keywords and len(value.args) == 1
                and isinstance(value.args[0], ast.Set)
                and all(_string(e) is not None for e in value.args[0].elts))
    assert readable, (
        f"REMEDIATION_ACTION_TYPES is bound as {ast.unparse(bindings[0])!r}; "
        f"the #429 pin reads only `REMEDIATION_ACTION_TYPES = "
        f"frozenset({{\"WORD\", ...}})` with string-constant members. Teach "
        f"_remediation_evidence_set the new shape; do not skip it")
    return {_string(e) for e in value.args[0].elts}


def test_the_anonymize_evidence_set_is_exactly_what_remediation_writes():
    """The report's ANONYMIZE evidence set equals remediation's audit words (#429).

    `REMEDIATION_ACTION_TYPES` is what `generate_report` checks a session
    that anonymized against (#254): at least one of its words must be in
    the audit summary, or the run grades REVIEW_REQUIRED. It was a
    hand-kept copy of the words remediation writes, and nothing compared
    the two. Measured at 0e3e38c across the 135 tests that could notice
    (the remediation `TARGETS` row, this file, and the report's own
    tests), every drift was green: dropping any one word from the set,
    adding `EXPORT` to it, and the honest case -- a new emitter word
    added to the emitter, to `FROZEN_AUDIT_ACTION_TYPES` and to the
    stability page as the red tests there demand, with the frozenset
    forgotten.

    Equality, in both directions: a word written and missing from the set
    grades a clean run REVIEW_REQUIRED; a word in the set that remediation
    never writes is evidence nothing can produce.

    `REMEDIATION_DECLINED` is subtracted **by the module constant's
    value**, read from the same tree, because it is written by remediation
    and deliberately is not evidence: a run in which every remediation
    declined must not satisfy the check (#301,
    `tests/test_declined_remediation_is_recorded.py`). The words come from
    `_audit_words_in`, Pin A's own per-module reader, so the two pins read
    the same sites. No probe operator mutates a set literal's members, so
    this adds no kill signal to any `TARGETS` row; it is a guard against
    the drift above, not a probe witness.
    """
    tree = ast.parse(_REMEDIATION.read_text(encoding="utf-8"))
    written, _sites = _audit_words_in(tree, _REMEDIATION.relative_to(REPO))
    declined = _module_string_constants(tree).get("REMEDIATION_DECLINED")
    assert declined is not None, (
        "the remediation module no longer binds `REMEDIATION_DECLINED = "
        "\"...\"` at module level; the #429 pin cannot tell a decline from "
        "evidence")
    evidence = _remediation_evidence_set(tree)

    assert declined not in evidence, (
        f"{declined} is in REMEDIATION_ACTION_TYPES; a decline is not "
        f"evidence that anything was anonymized (#301)")
    missing = sorted(written - {declined} - evidence)
    extra = sorted(evidence - written)
    assert not missing and not extra, (
        f"REMEDIATION_ACTION_TYPES disagrees with what remediation writes "
        f"to the audit table: written but not evidence {missing} (a clean "
        f"run whose rows carry only these grades REVIEW_REQUIRED -- add "
        f"them to the frozenset); evidence never written {extra} (remove "
        f"them). {declined} is written and excluded on purpose (#429)")


@pytest.mark.parametrize("binding", [
    'frozenset(["A"])',
    'frozenset({"A", NAME})',
    '{"A"}',
    None,
], ids=["list-argument", "non-constant-member", "bare-set", "absent"])
def test_the_evidence_set_reader_refuses_a_shape_it_cannot_read(binding):
    """`_remediation_evidence_set` raises, naming the shape, on anything else.

    Synthetic, like `_synthetic` above: no production line has these
    shapes, so there is nothing to mutate. The control is the shape the
    module uses today, which reads back as its members.
    """
    src = (f"REMEDIATION_ACTION_TYPES = {binding}\n" if binding is not None
           else 'OTHER = frozenset({"A"})\n')
    with pytest.raises(AssertionError, match="REMEDIATION_ACTION_TYPES"):
        _remediation_evidence_set(ast.parse(src))

    control = ast.parse('REMEDIATION_ACTION_TYPES = frozenset({"A"})\n')
    assert _remediation_evidence_set(control) == {"A"}


_ALLOWED_BUFFER_USES = '''
def apply(self, finding, audit_buffer=None):
    audit_buffer = []
    if self.store and audit_buffer:
        count = len(audit_buffer)
    if not audit_buffer:
        pass
    if audit_buffer is not None:
        audit_buffer.append(("REMEDIATION_REMOVE", 1, 2, None, None))
    self._record_decline(finding, "reason", audit_buffer)
    self._apply_single_remediation(finding, audit_buffer=audit_buffer)
    self.store.log_audit_batch(audit_buffer)
    # INSERT
'''


@pytest.mark.parametrize("use", [
    "rows = audit_buffer if audit_buffer is not None else []",
    "rows, unused = audit_buffer, None",
    "rows = audit_buffer or []",
    "rows = [audit_buffer]",
    "audit_buffer.extend([])",
    "audit_buffer += []",
    "audit_buffer[:] = []",
    "same = audit_buffer == []",
    "audit_buffer = list()",
    'list.append(audit_buffer, ("REMEDIATION_RMV", 1, 2, None, None))',
    "self.record(finding, buffer=audit_buffer)",
])
def test_the_batch_buffer_is_read_through_an_allow_list(use):
    """`_audit_buffer_words` accepts the package's uses and raises on the rest.

    The allowed shapes are the ones remediation uses today; the refused
    ones are aliases and writes the first, block-list version of this
    check read past (the first two survived on both interpreters, PR
    #430). `rows = audit_buffer or []` is the case that bounds the truth
    test at an `if`/`while`: it is a `BoolOp` like the allowed
    `self.store and audit_buffer`, one level up from a binding.
    """
    tree, parents = _synthetic(_ALLOWED_BUFFER_USES)
    assert _audit_buffer_words(tree, {}, parents, "synthetic") == ({"REMEDIATION_REMOVE"}, 1)

    tree, parents = _synthetic(_ALLOWED_BUFFER_USES.replace("# INSERT", use))
    with pytest.raises(AssertionError, match="audit_buffer"):
        _audit_buffer_words(tree, {}, parents, "synthetic")


def test_the_remediation_proposal_action_types_are_exactly_these_three():
    """Pin B (#396): `PhiRemediation.action_type`, a different vocabulary.

    The page called these "the audit `action_type` strings" until #396.
    They are never an audit row; they are what a proposal says it will
    do, reaching a user on the frozen `PhiFinding.remediation_proposal`.
    """
    assert _proposal_action_types() == FROZEN_PROPOSAL_ACTION_TYPES


def test_the_loss_scope_values_are_exactly_the_frozen_three():
    """Pin C (#396): the `loss_scope` strings, by value.

    Renaming a `LOSS_SCOPE_*` constant is deliberately green -- see
    `_loss_scope_values`. Respelling one of the three strings is not.
    """
    assert _loss_scope_values() == set(FROZEN_LOSS_SCOPES)


def test_the_grades_session_assigns_when_it_grades_are_exactly_these_two():
    """Pin D (#396): `validation_status=`'s strings. There is no `FAIL`.

    Named for what it collects. The report can also *carry*
    `"PENDING"` -- `reporting.py`'s field default, which renders into
    section 1 of the report whenever nothing has graded yet -- and this
    pin does not see it, because the collector reads `validation_status=`
    keyword arguments in `isocenter/session.py` and `PENDING` is a
    dataclass field default in another module. Respelling it is green
    here, measured.

    That is the right scope, not a gap to close: `PENDING` is the absence
    of a grade, `docs/api/stability.md` freezes the two grades, and
    `reporting.py` is careful to say so where the default is written.
    Unlike `AUDIT_DROP` in Pin E, which #411 froze because it costs a
    run its PASS, it is not a candidate for the freeze. If you widen
    this collector to other modules, widen `FROZEN_GRADES` with it or
    this goes red for the wrong reason.
    """
    assert _grade_values() == set(FROZEN_GRADES)


def test_the_report_exception_categories_are_exactly_these_two():
    """Pin E (#396): the categories synthesised into the `exceptions` list.

    Both are frozen: `COMPLIANCE_CHECK` since #396 and `AUDIT_DROP` since
    #411, which ruled that a category costing the run its PASS is
    something a user reads, not an internal one.
    """
    assert _report_exception_categories() == FROZEN_REPORT_EXCEPTIONS


def test_the_two_pass_behaviours_are_stated_as_contract():
    """T-F5: the #368 behaviours are written where a user reads them.

    Weak by design: it pins that the promise is *stated* in the three
    docstrings, as the #368 CHANGELOG entry says it is; the behaviour
    itself is pinned by `tests/test_compact_refuses_during_a_pass.py`.
    The #422 OCR refusal is held to the same standard in the `Raises:`
    of `scan_pixel_content` and `discover_redaction_zones`; its
    behaviour is pinned by `tests/test_ocr_unavailable_refuses.py`.
    """
    for name in ("compact", "redact", "ingest",
                 "scan_pixel_content", "discover_redaction_zones"):
        doc = inspect.getdoc(getattr(DicomSession, name)) or ""
        assert "RuntimeError" in doc, f"Session.{name}'s docstring does not name RuntimeError"
    assert "pass" in inspect.getdoc(DicomSession.compact)
