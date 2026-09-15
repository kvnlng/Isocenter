"""`anonymize(findings)` acts on the object `session.store` holds at each
finding's address, and never on an object outside the graph (#644).

`anonymize(findings)` does not rehydrate `finding.entity`, and until this
change every remediation arm wrote to that object whatever it was. A
report kept in a notebook across `close()` and a reopen, or a hand-built
finding whose `entity` was a copy, therefore wrote to objects nothing
exports and filed a success row for each write. Measured on 55ee01d: a CT
and an MR saved, audited and closed without anonymizing, reopened and
handed that report, wrote 231 success rows and 0 declines, graded `PASS`,
and exported Institution Name `JFK IMAGING CENTER`, both patients' names
and IDs, the source dates, a nested Other Patient IDs and 181 private
elements.

The session now resolves each finding before the service sees it, at its
`entity_uid` (an instance's UID from before `redact()` included, and a
patient's original Patient ID after its pseudonym) and its `entity_path`,
walked as #626's removals are. Live at its address, the finding is handed
over as it is; dead, a copy bound to the live object at the address is;
where the address names no single object, a copy with no entity is, which
declines. A REPLACE or SHIFT whose container a pass removed is satisfied.

**The oracle for "clean"** is the export of a fresh single pass over the
same sources under the same secret, compared element by element (DS and
IS numerically: a reloaded store re-spells them, #662). Every assertion
reads the exported file or the grade, never only the rows.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the arms through `isocenter.remediation`, the
pseudonym through `isocenter.privacy` and the graph through
`isocenter.entities`, so it charges those modules' probe rows; see
`test_mutation_probe_targets.py`.
"""
import copy
import dataclasses
import json
import re
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import (JITTER_SCHEME_KEYED, PhiStatus, normalize_study_date,
                                resolve_item_path)
from isocenter.privacy import _replacement_id_for
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, FIXED_B, load_fixed_secret

MODES = ["threads", "processes"]
UNRESOLVED = "could not be resolved against the live graph"
FOREIGN = "not minted under this store's project secret"
IMAGE_SEQ, SERIES_SEQ, PRIVATE_SEQ = "0008,1140", "0008,1115", "0029,1150"
NESTED = ((IMAGE_SEQ, 0),)
EMPTIED = ((SERIES_SEQ, 0),)
HELD = ("JFK IMAGING CENTER", "NESTED-INST", "NESTED-OPID", "EMPTIED-INST",
        "CompressedSamples", "20040119", "20010101", "1CT1")
SENTINEL = "VALUE-SENTINEL-644"

#: The floor, plus a rule on each shape a finding can live in: a
#: top-level REPLACE, a SHIFT at both levels, a sequence kept (so its
#: items are addressed) and a sequence emptied (so its items are
#: detached by the pass).
RULES = {
    "0008,0080": {"name": "Institution", "action": "REPLACE", "value": "RULE-INST"},
    "0008,0021": {"name": "Series Date", "action": "SHIFT"},
    IMAGE_SEQ: {"name": "Referenced Image Sequence", "action": "KEEP"},
    SERIES_SEQ: {"name": "Referenced Series Sequence", "action": "EMPTY"},
}


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def mode(request, monkeypatch):
    if request.param == "processes":
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


# ---------------------------------------------------------------------------
# Sources, sessions, and what reaches the file
# ---------------------------------------------------------------------------

def _write_sources(src):
    """CT_small with a Study Description, a Referenced Image Sequence item
    holding four identifiers, a Referenced Series Sequence item the rules
    empty, and a private sequence; MR_small with a nested Institution Name
    of its own."""
    src.mkdir(parents=True)
    ct = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ct.StudyDescription = "LIVE-STUDY-DESC"
    ct.SeriesDate = "20040119"
    ref = Dataset()
    ref.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ref.ReferencedSOPInstanceUID = "1.2.3.4.5"
    ref.InstitutionName = "NESTED-INST"
    ref.OtherPatientIDs = "NESTED-OPID"
    ref.SeriesDate = "20010101"
    ct.ReferencedImageSequence = Sequence([ref])
    emptied = Dataset()
    emptied.SeriesInstanceUID = "1.2.3.4.6"
    emptied.InstitutionName = "EMPTIED-INST"
    ct.ReferencedSeriesSequence = Sequence([emptied])
    block = ct.private_block(0x0029, "J5PRIV", create=True)
    item = Dataset()
    item.private_block(0x0029, "J5PRIV", create=True).add_new(0x51, "LO", "PRIV-SEQ-PHI")
    block.add_new(0x50, "SQ", Sequence([item]))
    ct.save_as(str(src / "ct.dcm"))
    mr = pydicom.dcmread(get_testdata_file("MR_small.dcm"))
    mr_ref = Dataset()
    mr_ref.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
    mr_ref.ReferencedSOPInstanceUID = "1.2.3.4.7"
    mr_ref.InstitutionName = "MR-NESTED-INST"
    mr.ReferencedImageSequence = Sequence([mr_ref])
    mr.save_as(str(src / "mr.dcm"))


def _store(root, extra=None, secret=FIXED_A):
    """A new store under `root`: sources written, secret fixed, config
    written, ingested and saved. Returns the open session."""
    _write_sources(root / "src")
    (root / "cfg.yaml").write_text(json.dumps({"phi_tags": {**RULES, **(extra or {})}}),
                                   encoding="utf-8")
    session = DicomSession(str(root / "m.db"))
    load_fixed_secret(session, root, secret)
    session.load_config(str(root / "cfg.yaml"))
    session.ingest(str(root / "src"))
    session.save(sync=True)
    return session


def _reopen(root):
    session = DicomSession(str(root / "m.db"))
    session.load_config(str(root / "cfg.yaml"))
    return session


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _by_modality(session):
    return {se.modality: (p, st, se.instances[0]) for p in session.store.patients
            for st in p.studies for se in st.series}


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return list(conn.execute("SELECT action_type, details FROM audit_log ORDER BY rowid"))


def _declined(session):
    return [d for a, d in _rows(session) if a == "REMEDIATION_DECLINED"]


def _reason(row):
    """A decline row's reason, without the `Remediation declined for <uid>:`
    prefix -- a UID can contain a date (CT_small's does), and a Patient's
    uid is its ID, so a value search reads the reason alone."""
    prefix, _, reason = row.partition(": ")
    assert prefix.startswith("Remediation declined for "), row
    return reason


_REPORTS = []


def _grade(session, root):
    """Every `**Grade Basis:**` token, read whole."""
    _REPORTS.append(None)
    path = root / f"report-{len(_REPORTS)}.md"
    session.generate_report(str(path))
    return [re.search(r"\*\*Grade Basis:\*\* ([A-Z_]+)", line).group(1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "**Grade Basis:**" in line]


#: Regenerated by `redact()`, so never equal across two stores.
_FRESH_UIDS = {"0008,0018", "0020,000d", "0020,000e"}


def _value(element):
    if element.VR in ("DS", "IS"):
        values = (element.value if isinstance(element.value, (list, tuple,
                                                              pydicom.multival.MultiValue))
                  else [element.value])
        try:
            return ("num", tuple(round(float(v), 6) for v in values))
        except (TypeError, ValueError):
            return repr(element.value)
    return repr(element.value)


def _walk(dataset, prefix, flat, skip_uids):
    for element in dataset:
        key = prefix + (f"{element.tag.group:04x},{element.tag.element:04x}",)
        if skip_uids and key[-1] in _FRESH_UIDS:
            continue
        if element.VR == "SQ":
            flat[key] = ("items", len(element.value))
            for index, item in enumerate(element.value):
                _walk(item, key + (index,), flat, skip_uids)
        else:
            flat[key] = _value(element)


def _export(session, out, skip_uids=False):
    """Every element of every exported file, keyed by modality and path."""
    session.export(str(out), use_compression=False)
    flat = {}
    files = sorted(out.rglob("*.dcm"))
    assert len(files) == 2, files
    for path in files:
        dataset = pydicom.dcmread(str(path), stop_before_pixels=True)
        _walk(dataset, (str(dataset.Modality),), flat, skip_uids)
    return flat


def _datasets(session, out):
    session.export(str(out), use_compression=False)
    return {str(ds.Modality): ds for ds in
            (pydicom.dcmread(str(p), stop_before_pixels=True) for p in sorted(out.rglob("*.dcm")))}


def _diff(got, want):
    return sorted((k, got.get(k), want.get(k)) for k in set(got) | set(want)
                  if got.get(k) != want.get(k))


def _fresh_pass(tmp_path, extra=None, secret=FIXED_A, redact_modality=None):
    """The oracle: one store, one audit, one anonymize (and the redaction
    the flow under test made, in the documented order), exported."""
    root = tmp_path / f"fresh-{secret[:1].hex()}-{redact_modality}"
    session = _store(root, extra, secret)
    with session:
        session.anonymize(session.audit())
        if redact_modality:
            _redact(session, redact_modality)
        return _export(session, root / "out", skip_uids=redact_modality is not None)


def _redact(session, modality):
    (_, _, instance), = [v for k, v in _by_modality(session).items() if k == modality]
    series = next(se for p in session.store.patients for st in p.studies
                  for se in st.series if instance in se.instances)
    serial = series.equipment.device_serial_number
    assert serial, modality
    session.configuration.add_rule(serial, zones=[[0, 4, 0, 4]])
    assert session.redact() == 1
    assert instance.sop_instance_uid != instance.attributes["_ISOCENTER_SOURCE_SOP_UID"]
    return instance


def _shifted(session, patient_id, original):
    service = RemediationService(date_jitter_config=session.configuration.date_jitter,
                                 project_secret=session.store_backend._project_secret_for_use(
                                     diagnose=False))
    return RemediationService._shift_date_string(
        original, service._get_date_shift(patient_id, JITTER_SCHEME_KEYED))


def _proposal(finding):
    return finding.remediation_proposal


def _only(report, entity_type, attr, path=(), uid=None, action=None):
    found = [f for f in report.findings if _proposal(f) and f.entity_type == entity_type
             and _proposal(f).target_attr == attr and (f.entity_path or ()) == path
             and (uid is None or f.entity_uid == uid)
             and (action is None or _proposal(f).action_type == action)]
    assert len(found) == 1, (entity_type, attr, path, found)
    return found[0]


# ---------------------------------------------------------------------------
# T1, T2, T3: a report kept across a reopen
# ---------------------------------------------------------------------------

def _assert_cleans_like_a_fresh_pass(session, report, root, tmp_path):
    live = _instances(session)
    # Non-vacuity: the report points at none of the objects this session
    # holds, and the graph it will export still carries the identifiers.
    assert not {id(f.entity) for f in report.findings} & {id(i) for i in live}
    assert "JFK IMAGING CENTER" in [i.attributes.get("0008,0080") for i in live]

    session.anonymize(report)

    assert _declined(session) == []
    got = _export(session, root / "out")
    assert _diff(got, _fresh_pass(tmp_path)) == []
    assert _grade(session, root) == ["PASS"]
    assert [i.phi_status for i in _instances(session)] == [PhiStatus.REMEDIATED] * 2


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_report_from_an_audit_only_session_cleans_the_live_graph_after_a_reopen(
        tmp_path, mode):
    """Session 1 ingests, saves, audits and closes; session 2 is handed the
    report. Red on 55ee01d: 231 success rows, 0 declines, PASS, and
    `JFK IMAGING CENTER`, `TOSHIBA` and both names exported, every instance
    UNSCANNED.

    Kills: `_live_findings` handing the findings over unchanged."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
    with _reopen(root) as session:
        _assert_cleans_like_a_fresh_pass(session, report, root, tmp_path)


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_report_kept_across_a_reopen_of_an_unsaved_pass_cleans_the_live_graph(
        tmp_path, mode):
    """Session 1 anonymizes and closes without saving the pass. #626 made
    each removal of the reopened run decline (199 rows, REVIEW_REQUIRED)
    while its REPLACE and SHIFT findings reported success on the dead
    objects, and the file carried every value. This replaces #626's
    `..._declines_its_removals`, the behaviour #644 reverses.

    Kills: `_live_findings` handing the findings over unchanged."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        first.anonymize(report)
        assert _declined(first) == []
    with _reopen(root) as session:
        _assert_cleans_like_a_fresh_pass(session, report, root, tmp_path)


def test_a_reopened_report_with_no_removals_writes_the_live_graph(tmp_path):
    """As above, handed only the REPLACE and SHIFT findings. The grade is
    PASS on both sides of the change, so the discriminator is the file:
    red on 55ee01d with `1CT1` and `20040119` exported."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        first.anonymize(report)
    kept = [f for f in report.findings
            if _proposal(f) and _proposal(f).action_type != "REMOVE_TAG"]
    assert kept and len(kept) < len(report.findings)
    fresh = _fresh_pass(tmp_path)
    with _reopen(root) as session:
        session.anonymize(kept)
        got = _datasets(session, root / "out")["CT"]
        assert got.PatientID == _replacement_id_for("1CT1", FIXED_A)
        for key in (("CT", "0010,0010"), ("CT", "0008,0020"), ("CT", "0008,0021"),
                    ("CT", "0008,0080"), ("CT", IMAGE_SEQ, 0, "0008,0021")):
            element = got
            for step in key[1:]:
                element = (element[tuple(int(x, 16) for x in step.split(","))]
                           if isinstance(step, str) else element.value[step])
            assert _value(element) == fresh[key], (key, element)
        assert got.StudyDate != "20040119"


# ---------------------------------------------------------------------------
# T4, T5: a hand-built finding whose entity is a copy
# ---------------------------------------------------------------------------

def _held(obj, attr):
    if hasattr(obj, "attributes") and "," in attr:
        return (obj.attributes.get(attr), attr in obj.attributes,
                len(obj.sequences[attr].items) if attr in obj.sequences else None)
    return getattr(obj, attr, "<missing>")


def _ct_tag(ds, *steps):
    element = ds
    for step in steps:
        if isinstance(step, int):
            element = element.value[step]
        else:
            tag = tuple(int(x, 16) for x in step.split(","))
            if tag not in element:
                return None
            element = element[tag]
    return element.value


ARMS = {
    # id: (extra rules, (entity_type, attr, path), live check, export check)
    "replace_item": (None, ("Instance", "0008,0080", ()),
                     lambda s, o: o.attributes["0008,0080"] == "RULE-INST",
                     lambda s, ds: _ct_tag(ds, "0008,0080") == "RULE-INST"),
    "replace_nested": (None, ("Instance", "0008,0080", NESTED),
                       lambda s, o: o.attributes["0008,0080"] == "RULE-INST",
                       lambda s, ds: _ct_tag(ds, IMAGE_SEQ, 0, "0008,0080") == "RULE-INST"),
    "replace_patient_id": (None, ("Patient", "patient_id", ()),
                           lambda s, o: o.patient_id == _replacement_id_for("1CT1", FIXED_A),
                           lambda s, ds: ds.PatientID == _replacement_id_for("1CT1", FIXED_A)),
    "empty_study_date": ({"0008,0020": {"name": "d", "action": "EMPTY"}},
                         ("Study", "study_date", ()),
                         lambda s, o: not o.study_date,
                         lambda s, ds: not _ct_tag(ds, "0008,0020")),
    "shift_item": (None, ("Instance", "0008,0021", ()),
                   lambda s, o: o.attributes["0008,0021"] == _shifted(s, "1CT1", "20040119"),
                   lambda s, ds: _ct_tag(ds, "0008,0021") == _shifted(s, "1CT1", "20040119")),
    "shift_nested": (None, ("Instance", "0008,0021", NESTED),
                     lambda s, o: o.attributes["0008,0021"] == _shifted(s, "1CT1", "20010101"),
                     lambda s, ds: _ct_tag(ds, IMAGE_SEQ, 0, "0008,0021")
                     == _shifted(s, "1CT1", "20010101")),
    "shift_study": (None, ("Study", "study_date", ()),
                    lambda s, o: normalize_study_date(o.study_date)
                    == normalize_study_date(_shifted(s, "1CT1", "20040119")),
                    lambda s, ds: _ct_tag(ds, "0008,0020") == _shifted(s, "1CT1", "20040119")),
    "remove_attribute": (None, ("Instance", "0009,1002", ()),
                         lambda s, o: "0009,1002" not in o.attributes,
                         lambda s, ds: _ct_tag(ds, "0009,1002") is None),
    "remove_private_sequence": (None, ("Instance", PRIVATE_SEQ, ()),
                                lambda s, o: PRIVATE_SEQ not in o.sequences,
                                lambda s, ds: _ct_tag(ds, PRIVATE_SEQ) is None),
    "remove_patient_name": ({"0010,0010": {"name": "n", "action": "REMOVE"}},
                            ("Patient", "patient_name", ()),
                            lambda s, o: o.patient_name is None,
                            lambda s, ds: not str(ds.get("PatientName", ""))),
}


def _live_at(session, entity_type, path):
    patient, study, ct = _by_modality(session)["CT"]
    return {"Patient": patient, "Study": study}.get(entity_type) or resolve_item_path(ct, path)


@pytest.mark.parametrize("arm", list(ARMS))
def test_a_finding_on_a_copy_acts_on_the_live_object_at_its_address(tmp_path, arm):
    """One scan finding, its entity replaced by a deep copy of the live
    object at its address, handed to `anonymize([finding])`. The live
    object takes the rule, the copy is untouched, and the file carries the
    change. Red on 55ee01d for every arm: one success row written to the
    copy, the live value exported.

    Kills: `_live_findings` handing the findings over unchanged; a rebind
    that forgets the Patient or the Study."""
    extra, (entity_type, attr, path), live_check, export_check = ARMS[arm]
    root = tmp_path / "store"
    with _store(root, extra) as session:
        report = session.audit()
        live = _live_at(session, entity_type, path)
        uid = {"Patient": "1CT1", "Study": getattr(live, "study_instance_uid", None)}.get(
            entity_type, _by_modality(session)["CT"][2].sop_instance_uid)
        finding = _only(report, entity_type, attr, path, uid=uid)
        assert finding.entity is live
        dead = copy.deepcopy(live)
        before = _held(dead, attr)
        assert not live_check(session, live), arm

        applied = session.anonymize([dataclasses.replace(finding, entity=dead)])

        assert applied == 1
        assert live_check(session, _live_at(session, entity_type, path)), arm
        assert _held(dead, attr) == before
        assert export_check(session, _datasets(session, root / "out")["CT"]), arm


def _no_such_uid(session, finding):
    return dataclasses.replace(finding, entity_uid="1.2.3.4.5.6.7.8.9")


def _shared_uid(session, finding):
    ct, mr = _by_modality(session)["CT"][2], _by_modality(session)["MR"][2]
    mr.sop_instance_uid = ct.sop_instance_uid
    return finding


def _index_past_the_end_still_held(session, finding):
    assert len(_by_modality(session)["CT"][2].sequences[IMAGE_SEQ].items) == 1
    return dataclasses.replace(finding, entity_path=((IMAGE_SEQ, 1),))


def _index_negative(session, finding):
    return dataclasses.replace(finding, entity_path=((IMAGE_SEQ, -1),))


SHAPES = [
    pytest.param(_no_such_uid, "0008,0080", (), id="no_such_uid_replace"),
    pytest.param(_no_such_uid, "0008,0021", (), id="no_such_uid_shift"),
    pytest.param(_shared_uid, "0008,0080", (), id="shared_uid_replace"),
    pytest.param(_shared_uid, "0008,0021", (), id="shared_uid_shift"),
    pytest.param(_index_past_the_end_still_held, "0008,0080", NESTED,
                 id="index_past_the_end_still_held_replace"),
    pytest.param(_index_past_the_end_still_held, "0008,0021", NESTED,
                 id="index_past_the_end_still_held_shift"),
    pytest.param(_index_negative, "0010,1000", NESTED, id="index_negative_remove"),
]


@pytest.mark.parametrize("shape, attr, path", SHAPES)
def test_a_copy_whose_address_names_nothing_declines_and_writes_nothing(
        tmp_path, shape, attr, path):
    """A copy whose address is no instance's, two instances', or an index
    past the items its sequence still holds while a remaining item holds
    the tag -- or an index that is not a position -- is not guessed at. One
    decline with the unresolved text and no value, nothing written to the
    copy or the graph, REVIEW_REQUIRED. Red on 55ee01d: a success row
    written to the copy (the REMOVE, #626's STALE text), PASS.

    Kills: an ambiguous UID resolved to its first instance; a rebind
    through `resolve_item_path` rather than the strict walk; a finding
    whose address resolved to nothing handed over unchanged."""
    root = tmp_path / "store"
    with _store(root) as session:
        report = session.audit()
        ct = _by_modality(session)["CT"][2]
        scan = _only(report, "Instance", attr, path, uid=ct.sop_instance_uid)
        live_before = _held(resolve_item_path(ct, path), attr)
        dead = copy.deepcopy(scan.entity)
        finding = shape(session, dataclasses.replace(scan, entity=dead, value=SENTINEL))
        before = _held(dead, attr)

        assert session.anonymize([finding]) == 0

        declines = _declined(session)
        assert len(declines) == 1 and UNRESOLVED in declines[0], declines
        assert not any(v in _reason(declines[0]) for v in HELD + (SENTINEL,)), declines
        assert _held(dead, attr) == before
        assert _held(resolve_item_path(ct, path), attr) == live_before
        assert _grade(session, root) == ["REVIEW_REQUIRED"]


# ---------------------------------------------------------------------------
# T6: a report from before `redact()`
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reopen", [False, True], ids=["same_session", "saved_reopen"])
def test_a_report_from_before_redact_resolves_through_the_uid_it_replaced(tmp_path, reopen):
    """audit, anonymize, `redact()` the MR (a new SOP Instance UID), [save,
    close, reopen], then the same report again. The MR's findings name the
    UID it had. Red on 55ee01d: 7 STALE declines, REVIEW_REQUIRED, over a
    clean graph.

    Kills: the `SOURCE_SOP_UID_ATTR` index dropped from the UID map."""
    root = tmp_path / "store"
    session = _store(root)
    report = session.audit()
    session.anonymize(report)
    _redact(session, "MR")
    if reopen:
        session.save(sync=True)
        session.close()
        session = _reopen(root)
    with session:
        session.anonymize(report)

        assert _declined(session) == []
        got = _export(session, root / "out", skip_uids=True)
        assert _diff(got, _fresh_pass(tmp_path, redact_modality="MR")) == []
        assert _grade(session, root) == ["PASS"]


def test_a_nested_finding_from_before_redact_reaches_its_instance_after_a_reopen(tmp_path):
    """Session 1 audits and redacts the MR, saves and closes; session 2 is
    handed only the MR's nested finding, saves and closes; session 3
    exports. The write lands on the live item and its instance is marked
    modified, so the save carries it. Red on 55ee01d: written to session
    1's item, `MR-NESTED-INST` exported.

    Kills: `_nested_finding_owners` not reading the UID map that carries
    the redacted UID (the item is written, its instance is not marked, and
    the save skips it); the source UID dropped from the map."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        mr = _by_modality(first)["MR"][2]
        nested = _only(report, "Instance", "0008,0080", NESTED, uid=mr.sop_instance_uid)
        _redact(first, "MR")
        first.save(sync=True)
    with _reopen(root) as second:
        assert second.anonymize([nested]) == 1
        assert _declined(second) == []
        assert _by_modality(second)["MR"][2].phi_status is PhiStatus.REMEDIATED
        second.save(sync=True)
    with _reopen(root) as third:
        mr = _datasets(third, root / "out")["MR"]
        assert _ct_tag(mr, IMAGE_SEQ, 0, "0008,0080") == "RULE-INST"


# ---------------------------------------------------------------------------
# T7, T12: a REPLACE whose container a pass emptied
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reopen", [False, True], ids=["same_session", "saved_reopen"])
def test_a_nested_replace_whose_container_a_pass_emptied_is_satisfied_on_reuse(
        tmp_path, reopen):
    """`0008,1115: EMPTY` over an item holding Institution Name under
    REPLACE. The first pass writes the item, then empties the sequence; the
    item is detached. A sentinel is written onto it, and the report is
    handed over again. Nothing is at the address, so the finding is
    satisfied: no decline, PASS, the sentinel untouched, the instance
    REMEDIATED. Red on 55ee01d: `RULE-INST` written over the sentinel and
    counted as applied.

    Kills: a gone REPLACE declined; a gone REPLACE handed over dead."""
    root = tmp_path / "store"
    session = _store(root)
    report = session.audit()
    session.anonymize(report)
    finding = _only(report, "Instance", "0008,0080", EMPTIED)
    detached = finding.entity
    assert detached.attributes["0008,0080"] == "RULE-INST"
    ct = _by_modality(session)["CT"][2]
    assert resolve_item_path(ct, EMPTIED) is None
    detached.attributes["0008,0080"] = SENTINEL
    if reopen:
        session.save(sync=True)
        session.close()
        session = _reopen(root)
    with session:
        session.anonymize(report)

        assert _declined(session) == []
        assert detached.attributes["0008,0080"] == SENTINEL
        assert _grade(session, root) == ["PASS"]
        assert [i.phi_status for i in _instances(session)] == [PhiStatus.REMEDIATED] * 2


def test_a_replace_under_a_container_an_earlier_partial_pass_emptied_settles_its_instance(
        tmp_path):
    """Pass 1 is handed only the EMPTY on the container; pass 2 the whole
    report. The REPLACE inside the emptied item is satisfied and counted as
    handled, so the scan tally reads the instance complete: REMEDIATED,
    not demoted over nothing.

    Kills: the gone keys left out of `_settle_statuses`."""
    root = tmp_path / "store"
    with _store(root) as session:
        report = session.audit()
        container = _only(report, "Instance", SERIES_SEQ, (), action="REPLACE_TAG")
        assert session.anonymize([container]) == 1
        ct = _by_modality(session)["CT"][2]
        assert resolve_item_path(ct, EMPTIED) is None

        session.anonymize(report)

        assert _declined(session) == []
        assert ct.phi_status is PhiStatus.REMEDIATED
        assert _grade(session, root) == ["PASS"]


def test_a_report_whose_every_finding_is_gone_applies_nothing_and_runs_no_audit(
        tmp_path, monkeypatch):
    """Only the findings inside the emptied item, handed over after the
    pass. Every one is gone, so nothing is applied and no row is written --
    and the empty list the resolver leaves is not the blind-execution
    `None`. Red on 55ee01d: 1 applied, a row written to the detached item.

    Kills: the resolver placed before the blind-audit check."""
    root = tmp_path / "store"
    with _store(root) as session:
        report = session.audit()
        session.anonymize(report)
        gone = [f for f in report.findings if _proposal(f) and f.entity_path == EMPTIED
                and _proposal(f).action_type != "REMOVE_TAG"]
        assert gone
        rows = len(_rows(session))

        def no_audit(*_a, **_k):
            raise AssertionError("anonymize(findings) ran a blind audit")
        monkeypatch.setattr(session, "audit", no_audit)

        assert session.anonymize(gone) == 0
        assert len(_rows(session)) == rows


# ---------------------------------------------------------------------------
# T8, T10, T11: the saved and the reused report
# ---------------------------------------------------------------------------

def test_the_callers_report_is_not_rebound(tmp_path):
    """T2's flow, the pass saved in session 2 and the same report handed to
    session 3. The findings passed are not modified: each keeps the entity
    it had, so a finding an earlier call could not resolve is not left
    unresolvable for a later one. A guard, green before.

    Kills: rebinding in place instead of copying."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        first.anonymize(report)
    before = [f.entity for f in report.findings]
    with _reopen(root) as second:
        second.anonymize(report)
        assert all(f.entity is e for f, e in zip(report.findings, before))
        second.save(sync=True)
    with _reopen(root) as third:
        rows = len(_declined(third))
        third.anonymize(report)
        assert _declined(third)[rows:] == []
        assert _grade(third, root) == ["PASS"]


def test_a_patient_whose_id_a_saved_pass_replaced_resolves_through_its_pseudonym(tmp_path):
    """The saved flow. A Patient finding names the original ID, which the
    reopened patient no longer holds; it is found through the pseudonym
    this store minted for it, and the ID is not re-hashed. A guard, green
    before (correct by accident, on the dead objects).

    Kills: the pseudonym candidates dropped (4 declines)."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        first.anonymize(report)
        first.save(sync=True)
    with _reopen(root) as session:
        ids = sorted(p.patient_id for p in session.store.patients)
        assert ids == sorted(_replacement_id_for(i, FIXED_A) for i in ("1CT1", "4MR1"))

        session.anonymize(report)

        assert _declined(session) == []
        assert sorted(p.patient_id for p in session.store.patients) == ids
        assert all(p.phi_status is PhiStatus.REMEDIATED for p in session.store.patients)
        assert _grade(session, root) == ["PASS"]


def test_a_saved_pass_reapplied_after_a_reopen_shifts_each_date_once(tmp_path):
    """The saved flow's SHIFT findings now reach live objects that already
    hold the shifted date. Each is shifted once: the arm shifts the value
    the scan read, and a target already at that result is admitted and
    rewritten with it. "Equal to a fresh pass" would not tell a double
    shift from a single one on its own, so the single-shift value is
    computed here, top level and nested. A guard, green before.

    Kills: shifting the held value instead of `proposal.original_value`."""
    root = tmp_path / "store"
    with _store(root) as first:
        report = first.audit()
        first.anonymize(report)
        first.save(sync=True)
    with _reopen(root) as session:
        shifts = [f for f in report.findings
                  if _proposal(f) and _proposal(f).action_type == "SHIFT_DATE"]
        assert len(shifts) >= 4, shifts

        session.anonymize(report)

        assert _declined(session) == []
        got = _datasets(session, root / "out")
        ct_once = _shifted(session, "1CT1", "20040119")
        assert ct_once not in (None, "20040119")
        assert _ct_tag(got["CT"], "0008,0020") == ct_once
        assert _ct_tag(got["CT"], "0008,0021") == ct_once
        assert _ct_tag(got["CT"], IMAGE_SEQ, 0, "0008,0021") == _shifted(
            session, "1CT1", "20010101")
        assert _ct_tag(got["MR"], "0008,0020") == _shifted(session, "4MR1", "20040826")
        assert _grade(session, root) == ["PASS"]


# ---------------------------------------------------------------------------
# T9: a report from another store
# ---------------------------------------------------------------------------

def test_a_report_from_another_store_writes_no_pseudonym_minted_there(tmp_path):
    """Store A (secret A) audits and anonymizes; store B (secret B, the
    same files) is handed A's report. Resolving it against B's graph must
    not write A's pseudonyms into B: each Patient ID REPLACE declines,
    naming no value, and the patient is not REMEDIATED. Every date is B's
    own shift -- the offset is derived under B's secret from the original
    ID the report carries. Red without the guard: A's `ANON_` IDs exported
    and PASS (measured on the prototype).

    Kills: the pseudonym check dropped from `_replace_attr_refused`."""
    with _store(tmp_path / "a") as a:
        report = a.audit()
        a.anonymize(report)
        minted_by_a = {p.patient_id for p in a.store.patients}
    assert minted_by_a == {_replacement_id_for(i, FIXED_A) for i in ("1CT1", "4MR1")}
    fresh_b = _fresh_pass(tmp_path, secret=FIXED_B)
    root = tmp_path / "b"
    with _store(root, secret=FIXED_B) as b:
        b.anonymize(report)

        got = _datasets(b, root / "out")
        assert {str(ds.PatientID) for ds in got.values()} == {"1CT1", "4MR1"}
        # Every element, nested ones included: a nested Patient ID REPLACE
        # writes through another arm than the top-level attribute does.
        leaked = [(modality, el.tag, el.value) for modality, ds in got.items()
                  for el in ds.iterall() if el.VR != "SQ"
                  and any(p in str(el.value) for p in minted_by_a)]
        assert not leaked, leaked
        declines = _declined(b)
        assert len(declines) == 2 and all(FOREIGN in d for d in declines), declines
        assert not any(p in _reason(d) or "1CT1" in _reason(d) for p in minted_by_a
                       for d in declines), declines
        assert all(p.phi_status is not PhiStatus.REMEDIATED for p in b.store.patients)
        for modality, ds in got.items():
            for key in ((modality, "0008,0020"), (modality, "0008,0021")):
                assert _value(ds[tuple(int(x, 16) for x in key[1].split(","))]) == fresh_b[key]
        assert (_value(got["CT"][0x00081140].value[0][0x00080021])
                == fresh_b[("CT", IMAGE_SEQ, 0, "0008,0021")])
        assert _grade(b, root) == ["REVIEW_REQUIRED"]


def test_a_date_seeded_on_another_stores_pseudonym_is_not_shifted(tmp_path):
    """Store A anonymizes with Series Date kept, then the rule becomes
    SHIFT and A audits again: the new findings seed their offset on A's
    pseudonym. Store B (secret B, the same files) is handed that report.
    B's offset over A's pseudonym is neither A's offset nor B's own for the
    patient -- measured -184 days where B's own is -359 -- so a second
    offset for one patient, derived from a value minted under A's secret.
    Each SHIFT declines, naming no value; the dates stay as the source had
    them, and the run grades REVIEW_REQUIRED.

    Kills: the seed check dropped from `_shift_target_moved`."""
    keep = {"0008,0021": {"name": "Series Date", "action": "KEEP"}}
    root_a = tmp_path / "a"
    with _store(root_a, keep) as a:
        a.anonymize(a.audit())
        a.save(sync=True)
        (root_a / "cfg.yaml").write_text(json.dumps({"phi_tags": RULES}), encoding="utf-8")
        a.load_config(str(root_a / "cfg.yaml"))
        report = a.audit()
    seeds = {_proposal(f).metadata["patient_id"] for f in report.findings
             if _proposal(f) and _proposal(f).action_type == "SHIFT_DATE"}
    assert seeds == {_replacement_id_for("1CT1", FIXED_A)}, seeds
    root = tmp_path / "b"
    with _store(root, secret=FIXED_B) as b:
        foreign = RemediationService._shift_date_string(
            "20040119", RemediationService(
                date_jitter_config=b.configuration.date_jitter,
                project_secret=FIXED_B)._get_date_shift(seeds.pop(), JITTER_SCHEME_KEYED))
        assert foreign != _shifted(b, "1CT1", "20040119")

        assert b.anonymize(report) == 0

        declines = _declined(b)
        assert len(declines) == 2 and all(FOREIGN in d for d in declines), declines
        assert not any(v in _reason(d) or "ANON_" in _reason(d)
                       for v in HELD for d in declines), declines
        ct = _datasets(b, root / "out")["CT"]
        assert _ct_tag(ct, "0008,0021") == "20040119"
        assert _ct_tag(ct, IMAGE_SEQ, 0, "0008,0021") == "20010101"
        assert _by_modality(b)["CT"][2].phi_status is not PhiStatus.REMEDIATED
        assert _grade(b, root) == ["REVIEW_REQUIRED"]


def test_a_foreign_seed_is_not_exempted_by_another_patient_holding_it(tmp_path):
    """The seed check exempts a pseudonym that is the Patient ID of the
    patient holding the target: an export from another project, ingested
    here, carries that project's pseudonym as its real ID and shifts under
    this store's secret, as 0.9.7 designed
    (`test_a_reingested_export_under_another_secret_is_warned`, which is
    the kill for the exemption dropped). Only the holder's own ID: store B
    holds the raw CT (1CT1) *and* A's export of it, re-ingested under new
    UIDs as patient `ANON_...`. A's post-pass report, seeded on that
    pseudonym, still declines on 1CT1, whose dates stay as the source had
    them.

    Kills: the exemption widened to any Patient ID the store holds."""
    keep = {"0008,0021": {"name": "Series Date", "action": "KEEP"}}
    root_a = tmp_path / "a"
    with _store(root_a, keep) as a:
        a.anonymize(a.audit())
        a.save(sync=True)
        a.export(str(root_a / "out"), use_compression=False)
        (root_a / "cfg.yaml").write_text(json.dumps({"phi_tags": RULES}), encoding="utf-8")
        a.load_config(str(root_a / "cfg.yaml"))
        report = a.audit()
    pseudonym = _replacement_id_for("1CT1", FIXED_A)
    assert {_proposal(f).metadata["patient_id"] for f in report.findings
            if _proposal(f) and _proposal(f).action_type == "SHIFT_DATE"} == {pseudonym}
    (exported,) = [ds for ds in (pydicom.dcmread(str(p)) for p in (root_a / "out").rglob("*.dcm"))
                   if str(ds.PatientID) == pseudonym]
    for keyword in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        setattr(exported, keyword, pydicom.uid.generate_uid())
    exported.file_meta.MediaStorageSOPInstanceUID = exported.SOPInstanceUID
    (tmp_path / "export").mkdir()
    exported.save_as(str(tmp_path / "export" / "ct.dcm"))
    root = tmp_path / "b"
    with _store(root, secret=FIXED_B) as b:
        b.ingest(str(tmp_path / "export"))
        assert {p.patient_id for p in b.store.patients} == {"1CT1", "4MR1", pseudonym}

        b.anonymize(report)

        declines = _declined(b)
        assert len(declines) == 2 and all(FOREIGN in d for d in declines), declines
        b.export(str(root / "out"), use_compression=False)
        (ct,) = [ds for ds in (pydicom.dcmread(str(p)) for p in (root / "out").rglob("*.dcm"))
                 if str(ds.PatientID) == "1CT1"]
        assert _ct_tag(ct, "0008,0021") == "20040119"
        assert _ct_tag(ct, IMAGE_SEQ, 0, "0008,0021") == "20010101"
        (live,) = [i for p in b.store.patients if p.patient_id == "1CT1"
                   for st in p.studies for se in st.series for i in se.instances]
        assert live.phi_status is not PhiStatus.REMEDIATED
