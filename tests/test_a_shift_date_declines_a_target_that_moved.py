"""A date shift declines when its target moved after the scan (#569).

`SHIFT_DATE` writes a shift of `proposal.original_value`, the value the
scan read. Until 0.9.8 it wrote that without looking at the item, so a
date deleted between `audit()` and `anonymize()` came back shifted, a
blanked or edited one was overwritten with a shift of the value that had
been there before, and each got a `REMEDIATION_SHIFT_DATE` row, a
REMEDIATED status and an exported date. Measured on ac33641 and on
57400d1: 3.12.14 in threads and processes, and 3.14.7t for the deleted
case. The same arm re-created a private date the default removal had
taken out, when one report was applied twice, with no edit at all.

**The rule these tests hold.** The arm writes only when its target still
holds the value the finding was raised on, or already holds the shift
this arm would write -- `anonymize(report)` handed the same report twice
re-applies it and must stay idempotent. A target that is absent, or holds
anything else, declines with a `REMEDIATION_DECLINED` row. It declines
rather than counting as satisfied because nothing was shifted: #567's
satisfied is "the end state is already there", and #547 declines a
`REPLACE_TAG` whose target vanished for the same reason.

**Rejected, each measured:** *shift the live value* -- the second
`anonymize(report)` would shift twice; *decline whenever the live value
differs from the audited one* -- that second call would write a decline
row per date and grade REVIEW_REQUIRED; *decline only when absent* --
leaves an edited date silently replaced by a shift of the old one, the
substitution #547 closed for `REPLACE_TAG`.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, the arm through `isocenter.remediation`, the
hand-built findings through `isocenter.privacy` and the graph and the
Study's date spelling through `isocenter.entities`, so it charges those
four modules' probe rows; see `test_mutation_probe_targets.py`.
"""
import os
import sqlite3

import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.entities import Instance, Patient, PhiStatus, Series, Study
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession
from support.project_secret import FIXED_A, load_fixed_secret

CONTENT_DATE = "0008,0023"
STUDY_DATE = "0008,0020"
REF_IMAGE_SEQ = "0008,1140"
ROOT = "1.2.826.0.1.3680043.10.569"
STUDY_UID = f"{ROOT}.1"
SOP_UID = f"{ROOT}.1.1.1"
#: CT_small's own ContentDate, which the edits below move away from.
AUDITED = "19970430"
EDITED = "20200202"

MODES = ["threads", "processes"]


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


def _source(tmp_path, nested=False):
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.PatientID = "PAT-569"
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = f"{ROOT}.1.1"
    ds.SOPInstanceUID = SOP_UID
    ds.file_meta.MediaStorageSOPInstanceUID = SOP_UID
    assert ds.ContentDate == AUDITED
    if nested:
        item = Dataset()
        item.ContentDate = AUDITED
        ds.ReferencedImageSequence = Sequence([item])
    src = tmp_path / "src"
    src.mkdir()
    ds.save_as(str(src / "a.dcm"))
    return src


def _session(tmp_path, rules, nested=False):
    session = DicomSession(str(tmp_path / "m.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    session.configuration.phi_tags = {
        tag: {"name": tag, "action": "JITTER"} for tag in rules}
    session.configuration.remove_private_tags = False
    session.ingest(str(_source(tmp_path, nested)))
    return session


def _graph(session):
    study = session.store.patients[0].studies[0]
    return study, study.series[0].instances[0]


def _rows(session, action_type):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _declines_on(session, uid, tag):
    return [d for u, d in _rows(session, "REMEDIATION_DECLINED")
            if u == uid and tag in d]


def _exported(session, tmp_path):
    out = tmp_path / "out"
    session.export(str(out), use_compression=False)
    files = [os.path.join(root, name) for root, _, names in os.walk(out)
             for name in names if name.endswith(".dcm")]
    assert len(files) == 1, files
    return pydicom.dcmread(files[0])


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_date_deleted_after_the_audit_is_not_recreated(tmp_path, mode, caplog):
    """The issue as filed. Red before: the tag came back shifted, with a
    `REMEDIATION_SHIFT_DATE` row, REMEDIATED, and in the exported file.

    The log says so too, as every other decline in the arm does, by UID
    and tag and no value."""
    session = _session(tmp_path, [CONTENT_DATE])
    with session:
        report = session.audit()
        _, instance = _graph(session)
        del instance.attributes[CONTENT_DATE]
        with caplog.at_level("WARNING", logger="isocenter"):
            session.anonymize(report)

        assert CONTENT_DATE not in instance.attributes, mode
        logged = [r.getMessage() for r in caplog.records
                  if "Date shift declined" in r.getMessage()]
        assert logged == [f"Date shift declined for {SOP_UID}: {CONTENT_DATE} "
                          "is no longer on the Instance, so there is no date "
                          "to shift"], logged
        declines = _declines_on(session, SOP_UID, CONTENT_DATE)
        assert len(declines) == 1, declines
        assert "no longer" in declines[0], declines
        assert [d for u, d in _rows(session, "REMEDIATION_SHIFT_DATE")
                if u == SOP_UID] == []
        assert instance.phi_status is not PhiStatus.REMEDIATED
        assert "ContentDate" not in _exported(session, tmp_path)


@pytest.mark.parametrize("edit", [
    pytest.param("", id="blank"),
    pytest.param(EDITED, id="edited"),
])
@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_date_changed_after_the_audit_is_not_written_over(tmp_path, mode, edit):
    """A blanked or edited date keeps the value it was given. Red before:
    both were overwritten with a shift of `AUDITED`.

    The row names the tag and neither value: a decline row is persisted
    and rendered into the report, and both values are dates of this
    patient."""
    session = _session(tmp_path, [CONTENT_DATE])
    with session:
        report = session.audit()
        _, instance = _graph(session)
        instance.attributes[CONTENT_DATE] = edit
        session.anonymize(report)

        assert instance.attributes[CONTENT_DATE] == edit, mode
        declines = _declines_on(session, SOP_UID, CONTENT_DATE)
        assert len(declines) == 1, declines
        assert "changed" in declines[0], declines
        assert AUDITED not in declines[0] and EDITED not in declines[0], declines
        assert instance.phi_status is not PhiStatus.REMEDIATED


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_nested_date_deleted_after_the_audit_is_not_recreated(tmp_path, mode):
    """The guard reads the item the finding resolved to, not the
    instance: the top-level ContentDate is untouched and still shifts."""
    session = _session(tmp_path, [CONTENT_DATE], nested=True)
    with session:
        report = session.audit()
        _, instance = _graph(session)
        item = instance.sequences[REF_IMAGE_SEQ].items[0]
        del item.attributes[CONTENT_DATE]
        session.anonymize(report)

        assert CONTENT_DATE not in item.attributes, mode
        top = instance.attributes[CONTENT_DATE]
        assert top not in (AUDITED, ""), top
        declines = _declines_on(session, SOP_UID, CONTENT_DATE)
        assert len(declines) == 1, declines
        assert instance.phi_status is not PhiStatus.REMEDIATED


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_study_date_cleared_after_the_audit_is_not_recreated(tmp_path, mode):
    """The `Study` branch, which has no `set_attr`. Red before: the study
    got a shifted date back and wrote it onto the instance copies.

    Asserted on the Study's own row, not on the instance: the instance's
    own StudyDate finding, which no longer folds into anything, shifts
    its copy by itself, which is right and not this issue."""
    session = _session(tmp_path, [STUDY_DATE])
    with session:
        report = session.audit()
        study, _ = _graph(session)
        study.study_date = None
        session.anonymize(report)

        assert study.study_date is None, mode
        declines = _declines_on(session, STUDY_UID, "study_date")
        assert len(declines) == 1, declines
        # A cleared Study date is gone, not changed: read as present, the
        # None would decline under the wrong reason.
        assert "is no longer on the Study" in declines[0], declines
        assert [d for u, d in _rows(session, "REMEDIATION_SHIFT_DATE")
                if u == STUDY_UID] == []
        assert study.phi_status is not PhiStatus.REMEDIATED


PRIVATE_DATE = "0009,1042"


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_private_date_the_first_call_removed_is_not_recreated_by_the_second(
        tmp_path, mode):
    """No hand edit: the scan's own output, applied twice.

    pydicom's JPEG2000.dcm carries the private DA `0009,1042`. Under the
    default private-tag removal plus a `JITTER` rule on that tag, the scan
    raises a REMOVE and a SHIFT on one dedup key. The first
    `anonymize(report)` runs the REMOVE and skips the SHIFT as a
    duplicate. On the second call the REMOVE matches nothing and claims no
    key, so the SHIFT runs. Red before: it re-created the removed private
    tag with a shifted date, and that date was exported."""
    src = tmp_path / "src"
    src.mkdir()
    ds = pydicom.dcmread(get_testdata_file("JPEG2000.dcm"))
    assert ds[0x0009, 0x1042].VR == "DA" and ds[0x0009, 0x1042].value
    ds.save_as(str(src / "a.dcm"))
    session = DicomSession(str(tmp_path / "m.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    tags = dict(session.configuration.phi_tags)
    tags[PRIVATE_DATE] = {"name": "private date", "action": "JITTER"}
    session.configuration.phi_tags = tags
    assert session.configuration.remove_private_tags
    with session:
        session.ingest(str(src))
        _, instance = _graph(session)
        report = session.audit()
        # Non-vacuity: both findings are on the one key.
        assert sorted(f.remediation_proposal.action_type for f in report.findings
                      if f.remediation_proposal and f.tag == PRIVATE_DATE
                      and f.entity_path in (None, ())) == ["REMOVE_TAG", "SHIFT_DATE"]
        session.anonymize(report)
        assert PRIVATE_DATE not in instance.attributes, mode
        session.anonymize(report)

        assert PRIVATE_DATE not in instance.attributes, mode
        uid = instance.sop_instance_uid
        gone = [d for d in _declines_on(session, uid, PRIVATE_DATE) if "no longer on" in d]
        assert len(gone) == 1, _rows(session, "REMEDIATION_DECLINED")
        assert [d for u, d in _rows(session, "REMEDIATION_SHIFT_DATE")
                if PRIVATE_DATE in d] == []
        assert (0x0009, 0x1042) not in _exported(session, tmp_path)


@pytest.mark.parametrize("rules", [
    pytest.param([CONTENT_DATE], id="content-date"),
    pytest.param([STUDY_DATE], id="study-date"),
])
def test_one_report_applied_twice_writes_the_same_values_and_no_decline(
        tmp_path, rules):
    """`anonymize(report)` twice is idempotent, and stays so. The second
    call meets its own shift on every target, which is admitted; a
    predicate of "decline unless the audited value is there" would write
    a decline per date here."""
    session = _session(tmp_path, rules)
    with session:
        report = session.audit()
        study, instance = _graph(session)
        before = (study.study_date, dict(instance.attributes))
        session.anonymize(report)
        first = (study.study_date, dict(instance.attributes))
        # Non-vacuity: the first call shifted the date the rule names.
        assert (first[0] != before[0] if rules == [STUDY_DATE]
                else first[1][CONTENT_DATE] != before[1][CONTENT_DATE]), first
        session.anonymize(report)

        assert (study.study_date, dict(instance.attributes)) == first
        assert _rows(session, "REMEDIATION_DECLINED") == []
        assert instance.phi_status is PhiStatus.REMEDIATED


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_the_ordinary_path_declines_nothing(tmp_path, mode):
    """audit -> anonymize under the default floor, with nothing edited
    between: every shift writes, none declines. Non-vacuous: the floor
    shifts dates on this file."""
    src = _source(tmp_path)
    session = DicomSession(str(tmp_path / "m.db"))
    load_fixed_secret(session, tmp_path, FIXED_A)
    with session:
        session.ingest(str(src))
        session.anonymize(session.audit())

        assert _rows(session, "REMEDIATION_SHIFT_DATE"), mode
        assert _rows(session, "REMEDIATION_DECLINED") == []


# --- Hand-built findings: the spellings a scan never produces -------------


class _Rows:
    def __init__(self):
        self.rows = []

    def log_audit_batch(self, rows):
        self.rows.extend(rows)

    def log_audit(self, *row):
        self.rows.append(row)


def _hand_built(entity, uid, target, original, entity_type):
    proposal = PhiRemediation("SHIFT_DATE", target, original_value=original,
                              metadata={"patient_id": "P569"})
    return PhiFinding(entity_uid=uid, entity_type=entity_type,
                      field_name=target, value=original, reason="r",
                      tag=target, entity=entity, remediation_proposal=proposal)


def _service():
    rows = _Rows()
    return RemediationService(store_backend=rows, project_secret=FIXED_A), rows


def test_an_uppercase_target_is_the_tag_the_item_holds():
    """`set_attr` lowercases, so an uppercase `target_attr` shifted the
    item's lowercase key before 0.9.8 and must still: looked up raw, the
    target would read as absent and decline a correct shift."""
    instance = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    instance.set_attr("0008,002a", "20040119120000")
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        instance, "1.2.3.4.5", "0008,002A", "20040119120000", "Instance")])

    assert applied == 1, rows.rows
    assert instance.attributes["0008,002a"] != "20040119120000"
    assert [r[0] for r in rows.rows] == ["REMEDIATION_SHIFT_DATE"]


def test_a_study_date_audited_in_iso_spelling_is_the_date_the_study_holds():
    """`Study.__setattr__` stores text through `normalize_study_date`, so
    `"2004-01-19"` and `date(2004, 1, 19)` are one Study Date. Compared
    as rendered DA text instead, the ISO spelling reads as a changed date
    and a correct shift declines."""
    patient = Patient("P569", "Doe^Jane")
    study = Study("1.2.3", "20040119")
    patient.studies.append(study)
    original = study.study_date
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        study, "1.2.3", "study_date", "2004-01-19", "Study")])

    assert applied == 1, rows.rows
    assert study.study_date != original
    assert [r[0] for r in rows.rows] == ["REMEDIATION_SHIFT_DATE"]


def test_an_unparseable_date_still_in_place_declines_once_as_unparseable():
    """The guard admits a target still holding its audited value, so an
    unparseable one reaches the arm's own invalid-format decline: one
    row, saying why, not a second from the guard."""
    instance = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    instance.set_attr(CONTENT_DATE, "notadate")
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        instance, "1.2.3.4.5", CONTENT_DATE, "notadate", "Instance")])

    assert applied == 0
    assert [r[0] for r in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert "invalid date format" in rows.rows[0][2], rows.rows
    assert instance.attributes[CONTENT_DATE] == "notadate"


def test_an_unparseable_date_that_was_deleted_declines_as_gone():
    """The guard runs before the parse is consulted, so a target that is
    gone says so whatever its audited value was. Gated on a parseable
    original instead, this row said "invalid date format ... the value is
    unchanged" about a value the item no longer holds."""
    instance = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        instance, "1.2.3.4.5", CONTENT_DATE, "notadate", "Instance")])

    assert applied == 0
    assert [r[0] for r in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert "is no longer on the Instance" in rows.rows[0][2], rows.rows
    assert CONTENT_DATE not in instance.attributes


def test_a_shift_built_with_no_original_writes_no_row():
    """`PhiRemediation`'s `original_value` defaults to None, which is a
    blank original like `""`: no date was read, so there is nothing to
    have moved, and the arm's empty-date branch writes no row, as it did
    before 0.9.8. Guarded instead, the item's date read as "changed" and
    a decline row was written over a finding that never named one."""
    instance = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    instance.set_attr(CONTENT_DATE, AUDITED)
    service, rows = _service()
    finding = _hand_built(instance, "1.2.3.4.5", CONTENT_DATE, None, "Instance")
    assert finding.remediation_proposal.original_value is None

    applied = service.apply_remediation([finding])

    assert applied == 0
    assert rows.rows == []
    assert instance.attributes[CONTENT_DATE] == AUDITED


def test_a_date_key_holding_none_is_no_longer_on_the_item():
    """`set_attr(tag, None)` keeps the key with a None value, which the scan
    skips as absent and the exporter writes empty. The decline says the
    date is gone, as for a deleted key, not that it changed."""
    instance = Instance("1.2.3.4.5", "1.2.840.10008.5.1.4.1.1.2", 1)
    instance.set_attr(CONTENT_DATE, None)
    assert CONTENT_DATE in instance.attributes
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        instance, "1.2.3.4.5", CONTENT_DATE, AUDITED, "Instance")])

    assert applied == 0
    assert [r[0] for r in rows.rows] == ["REMEDIATION_DECLINED"], rows.rows
    assert "is no longer on the Instance" in rows.rows[0][2], rows.rows
    assert instance.attributes[CONTENT_DATE] is None


def test_a_padded_study_date_is_the_date_the_study_holds():
    """The arm's parser strips a padded DA, so the Study is compared the
    same way: `" 20040119 "` is the date `20040119` and shifts. Compared
    unstripped, it read as a changed date and a correct shift declined."""
    patient = Patient("P569", "Doe^Jane")
    study = Study("1.2.3", "20040119")
    patient.studies.append(study)
    original = study.study_date
    service, rows = _service()

    applied = service.apply_remediation([_hand_built(
        study, "1.2.3", "study_date", " 20040119 ", "Study")])

    assert applied == 1, rows.rows
    assert study.study_date != original
    assert [r[0] for r in rows.rows] == ["REMEDIATION_SHIFT_DATE"]
