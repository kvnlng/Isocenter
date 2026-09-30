"""A date the pipeline shifted is not shifted again, at any depth (#513).

`date_shifted` was a field on `Instance` and on `Study` and on nothing
else -- `DicomItem` is a slots dataclass without it -- so the scan's
"already shifted" shortcut could not see a date living inside a sequence.
Measured on `927cb2b` on 3.12.14 (threads and processes) and 3.14.7t: a
study with `study_date=None`, an instance with no top-level date, one
sequence item holding `0008,0023='20230515'` and a `SHIFT` rule on that
tag, over three `audit()` + `anonymize()` passes, gave

    20230515 -> 20220802 -> 20220616 -> 20220430

one finding and one `REMEDIATION_SHIFT_DATE` row per pass, each row
claiming a legitimate shift while the value drifted away from its
top-level siblings. The drift was not even uniform -- pass 1 moved -286
days and later passes -47 -- because the jitter was seeded on a PatientID
pass 1 had replaced (#517, fixed first).

**The rule these tests hold.** Shifted-ness is recorded **per value**:
`DicomItem._shifted_dates` maps a tag to the string the `SHIFT_DATE` arm
wrote there, and the scan skips a value only while its tag still holds
that string. So the record is self-invalidating -- overwrite the tag with
a fresh original and it stops being vouched for structurally, with no
invalidation pass and no convention to remember -- and one mechanism
answers the question at every depth, which is the point: #513 and #510
are the same question one level apart.

The record has to reach three places or the fix is invisible: the worker
clones the scan actually reads (`clone_sequences` for nested items,
`_make_lightweight_copy` for the root), and the store, as `__shifted__`
inside the serialized item dict beside `__sequences__` and `__vrs__`.
Each has its own test here, and the parallel ones run on both paths --
`audit()` scans a clone unconditionally, so threads and processes are the
same line of code but not the same pickle.

**Why this file imports what it does.** It reaches the record through
`isocenter.entities`, the scan through `isocenter.privacy`, the arm
through `isocenter.remediation`, the clone through `isocenter.session`
and the round trip through `isocenter.persistence`, so it charges all
five modules' probe rows; see `test_mutation_probe_targets.py`.
"""
import sqlite3
from datetime import date

import pytest

from isocenter.entities import (DicomItem, DicomSequence, Instance, Patient,
                                PhiStatus, Series, Study)
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret

SC_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"
CONTENT_DATE = "0008,0023"
ACQ_DATE = "0008,0022"
SEQ_TAG = "0040,a730"

SHIFT_CONTENT = {CONTENT_DATE: {"name": "ContentDate", "action": "SHIFT"}}
SHIFT_ACQ = {ACQ_DATE: {"name": "AcquisitionDate", "action": "SHIFT"}}
SHIFT_BOTH = {**SHIFT_CONTENT, **SHIFT_ACQ}

#: The PatientID every case here uses. `P1` rather than `P513`, so the
#: values these tests assert are literally the values the repro measured
#: on `927cb2b` and quoted in the docstring above -- an id chosen for
#: tidiness would have made the recorded measurement unreproducible from
#: this file.
PID = "P1"

#: The offset `P1` seeds, with the default jitter config
#: (`min_days=-365`, `max_days=-1`) and the fixed project secret
#: `FIXED_A` every session here loads (it was -286 before the offset was
#: keyed in 0.9.7). One number for the whole file: every
#: value of this patient moves by it, in whichever pass it is first
#: shifted, and it does not change between passes (#517). The arithmetic
#: that produces it is asserted as a literal once, in
#: `tests/test_the_jitter_seed_survives_anonymize.py`.
OFFSET = -364

#: Both parallel paths. `audit()` clones the graph whichever it picks, so
#: the same line of code carries the record -- through a `copy` in one
#: and a pickle in the other, which is exactly the difference that hides
#: a dropped field.
MODES = ["threads", "processes"]


@pytest.fixture
def mode(request, monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)
    if request.param == "threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    else:
        monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
        monkeypatch.delenv("ISOCENTER_FORCE_THREADS", raising=False)
    return request.param


@pytest.fixture(autouse=True)
def _threads_by_default(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _session(tmp_path, *, study_date=None, top_date=None, nested_date=None,
             tags=None, name="m.db"):
    session = DicomSession(str(tmp_path / name))
    # The store is given a fixed project secret, so OFFSET is a literal
    # a reader can check (0.9.7): a generated secret would make it random.
    load_fixed_secret(session, tmp_path, FIXED_A)
    patient = Patient(PID, "Orig^Name")
    study = Study("1.2.826.0.1.513", study_date)
    series = Series("1.2.826.0.1.513.1", "OT", 1)
    instance = Instance("1.2.826.0.1.513.1.0", SC_SOP_CLASS, 1)
    if top_date is not None:
        instance.set_attr(ACQ_DATE, top_date)
    if nested_date is not None:
        item = DicomItem()
        item.set_attr(CONTENT_DATE, nested_date)
        instance.sequences[SEQ_TAG] = DicomSequence(tag=SEQ_TAG, items=[item])
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.phi_tags = dict(tags or {})
    return session


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _nested(session):
    return _instance(session).sequences[SEQ_TAG].items[0]


def _days(after, before):
    """The interval between two DA strings, in days."""
    def _as_date(text):
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    return (_as_date(after) - _as_date(before)).days


def _shift_rows(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log "
            "WHERE action_type='REMEDIATION_SHIFT_DATE'")]


def test_a_nested_date_moves_once_over_three_passes(tmp_path):
    """The issue as filed. Red before: three findings, three audit rows
    and three different values."""
    session = _session(tmp_path, nested_date="20230515", tags=SHIFT_CONTENT)
    raised, seen = [], ["20230515"]
    with session:
        for _ in range(3):
            report = session.audit()
            raised.append(len([f for f in report.findings if f.tag == CONTENT_DATE]))
            session.anonymize(report)
            seen.append(_nested(session).attributes[CONTENT_DATE])

    assert raised == [1, 0, 0], raised
    assert seen == ["20230515", "20220516", "20220516", "20220516"], seen
    assert _days(seen[1], "20230515") == OFFSET, seen
    rows = _shift_rows(tmp_path / "m.db")
    assert len(rows) == 1, rows


def test_the_first_passs_value_is_the_one_the_offset_says(tmp_path):
    """Not only "it stopped moving": it stopped on the right value.

    A fix that recorded the *original* rather than the shifted value
    would also stop the drift, and would leave the real date in the file.
    """
    session = _session(tmp_path, nested_date="20230515", tags=SHIFT_CONTENT)
    with session:
        session.anonymize(session.audit())
        after = _nested(session).attributes[CONTENT_DATE]
    assert after != "20230515"
    assert _days(after, "20230515") == OFFSET, after


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_worker_sees_a_nested_items_record(tmp_path, mode):
    """`clone_sequences` carries `_shifted_dates`. Without it every
    nested record is invisible to the scan and #513 stands with the fix
    in place -- on both paths, since `audit()` always scans the clone."""
    session = _session(tmp_path, nested_date="20230515", tags=SHIFT_CONTENT)
    with session:
        session.anonymize(session.audit())
        shifted = _nested(session).attributes[CONTENT_DATE]
        assert shifted != "20230515"
        again = session.audit()
        assert [f for f in again.findings if f.tag == CONTENT_DATE] == [], mode
        session.anonymize(again)
        assert _nested(session).attributes[CONTENT_DATE] == shifted


@pytest.mark.parametrize("mode", MODES, indirect=True)
def test_a_worker_sees_the_roots_record(tmp_path, mode):
    """`_make_lightweight_copy` carries the root instance's record, the
    same way and for the same reason."""
    session = _session(tmp_path, top_date="20230601", tags=SHIFT_ACQ)
    with session:
        session.anonymize(session.audit())
        shifted = _instance(session).attributes[ACQ_DATE]
        assert shifted != "20230601"
        again = session.audit()
        assert [f for f in again.findings if f.tag == ACQ_DATE] == [], mode
        session.anonymize(again)
        assert _instance(session).attributes[ACQ_DATE] == shifted


def test_a_value_replaced_after_a_shift_is_raised_again(tmp_path):
    """The record is keyed on the value, so it stops speaking for a tag
    the moment that tag changes. A boolean per item -- the obvious
    alternative -- would hide a fresh original for ever."""
    session = _session(tmp_path, nested_date="20230515", top_date="20230601",
                       tags=SHIFT_BOTH)
    with session:
        session.anonymize(session.audit())
        assert _nested(session).attributes[CONTENT_DATE] != "20230515"

        _nested(session).set_attr(CONTENT_DATE, "20240704")
        _instance(session).set_attr(ACQ_DATE, "20240704")
        report = session.audit()
        raised = sorted(f.tag for f in report.findings
                        if f.tag in (CONTENT_DATE, ACQ_DATE))
        assert raised == [ACQ_DATE, CONTENT_DATE], raised
        session.anonymize(report)
        assert _nested(session).attributes[CONTENT_DATE] != "20240704"
        assert _instance(session).attributes[ACQ_DATE] != "20240704"


@pytest.mark.parametrize("replacement", [None, ""], ids=["anon-id", "emptied-id"])
def test_a_value_shifted_before_the_patients_id_changed_is_still_vouched_for(
        tmp_path, replacement):
    """The record is keyed on the value, not on the identity that seeded
    the shift, so it survives the PatientID changing under it.

    `anonymize()` replaces PatientID in its first pass, and the
    `SHIFT_DATE` arm declines outright on an id it cannot resolve at all
    -- which is the arm's *second* decline, the one
    `remediation._date_shift_declines` deliberately does not model
    (#498). Across passes that decline is reachable, so it matters that
    the record answers before the arm is ever asked: measured on
    `a50632d` and on this branch, both spellings give pass 2 raising
    nothing, the value unchanged, the instance CLEARED and no
    `REMEDIATION_DECLINED` row. `None` here means the ordinary
    `ANON_<digest>` replacement pass 1 writes; `""` is the id emptied by
    hand, which is what makes the arm's PatientID decline reachable.
    """
    session = _session(tmp_path, top_date="20230601", tags=SHIFT_ACQ)
    with session:
        session.anonymize(session.audit())
        shifted = _instance(session).attributes[ACQ_DATE]
        patient = session.store.patients[0]
        assert shifted != "20230601"
        if replacement is None:
            assert patient.patient_id.startswith("ANON_")
        else:
            patient.patient_id = replacement

        report = session.audit()
        assert [f for f in report.findings if f.tag == ACQ_DATE] == []
        session.anonymize(report)
        assert _instance(session).attributes[ACQ_DATE] == shifted
        assert _instance(session).phi_status is PhiStatus.CLEARED

    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        declines = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type='REMEDIATION_DECLINED'").fetchone()[0]
    assert declines == 0
    assert len(_shift_rows(tmp_path / "m.db")) == 1


def test_the_records_survive_a_save_and_a_reload(tmp_path):
    """`Instance.date_shifted` never had a column -- which is one of the
    reasons it is now gone -- so before this a reloaded instance
    reported `False` however many of its dates had been shifted and the
    next `audit()` re-shifted every one of them. Measured on
    `927cb2b`: `20230601` -> `20220819` in the first session, then
    `20220703` after a reopen.

    Both depths, because the root and the nested item reach
    `attributes_json` through different serialisers.
    """
    session = _session(tmp_path, top_date="20230601", nested_date="20230515",
                       tags=SHIFT_BOTH)
    with session:
        session.anonymize(session.audit())
        expected = (_instance(session).attributes[ACQ_DATE],
                    _nested(session).attributes[CONTENT_DATE])
        assert expected != ("20230601", "20230515")
        session.save(sync=True)

    reopened = DicomSession(str(tmp_path / "m.db"))
    with reopened:
        reopened.configuration.phi_tags = dict(SHIFT_BOTH)
        instance = _instance(reopened)
        nested = _nested(reopened)
        assert (instance.attributes[ACQ_DATE],
                nested.attributes[CONTENT_DATE]) == expected
        report = reopened.audit()
        assert [f for f in report.findings
                if f.tag in (CONTENT_DATE, ACQ_DATE)] == []
        reopened.anonymize(report)
        assert (instance.attributes[ACQ_DATE],
                nested.attributes[CONTENT_DATE]) == expected


def test_the_record_does_not_leak_into_attributes(tmp_path):
    """`__shifted__` is popped before `attributes.update(data)`, exactly
    as `__vrs__` is. Left in, it would be a key in `attributes` that is
    not a tag, and would reach every reader of it -- the exporter's merge
    and `export_dataframe(expand_metadata=True)` among them."""
    session = _session(tmp_path, top_date="20230601", nested_date="20230515",
                       tags=SHIFT_BOTH)
    with session:
        session.anonymize(session.audit())
        session.save(sync=True)

    reopened = DicomSession(str(tmp_path / "m.db"))
    with reopened:
        instance = _instance(reopened)
        assert "__shifted__" not in instance.attributes
        assert "__shifted__" not in _nested(reopened).attributes
        assert instance._shifted_dates, "the record itself did not come back"
        frame = reopened.export_dataframe("export_metadata.csv", expand_metadata=True)
        assert "__shifted__" not in frame.columns
        assert not [c for c in frame.columns if "shifted" in str(c).lower()]


def test_a_blank_shift_value_is_not_a_finding(tmp_path):
    """A blank date under `SHIFT` is not a finding at all, so the
    instance ends CLEARED.

    Before the per-value record the flag hid this after pass 1; with a
    record every pass looks like pass 1, so the asymmetry had to go one
    way. `EMPTY` already tests `val != ""` and `REPLACE` tests `val !=
    "ANONYMIZED" and val != ""`, and the arm's own reasoning is that an
    empty value is not retained PHI -- it is the one non-success path
    that deliberately writes no decline row. Previously such an instance
    raised a finding every pass, the arm skipped it, the pass counted a
    failure, and #491's demotion left the instance IDENTIFIED.
    """
    session = _session(tmp_path, top_date="", tags=SHIFT_ACQ)
    with session:
        report = session.audit()
        assert [f for f in report.findings if f.tag == ACQ_DATE] == []
        session.anonymize(report)
        assert _instance(session).phi_status is PhiStatus.CLEARED
    assert _shift_rows(tmp_path / "m.db") == []
    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        declines = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE action_type='REMEDIATION_DECLINED'").fetchone()[0]
    assert declines == 0


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_whitespace_only_shift_value_is_not_a_finding_either(tmp_path, blank):
    """Spelled as the arm spells it (`str(value).strip()`), so the two
    halves cannot disagree about what blank means."""
    session = _session(tmp_path, top_date=blank, tags=SHIFT_ACQ)
    with session:
        report = session.audit()
        assert [f for f in report.findings if f.tag == ACQ_DATE] == []


def test_a_multi_valued_date_is_not_mistaken_for_a_blank_one(tmp_path):
    """`['', '']` is not blank: `str(...)` of it is `"['', '']"`. The
    element is still raised, and the arm still declines it -- which is
    what keeps #498's contract for a value no parser can read."""
    session = _session(tmp_path, tags=SHIFT_ACQ)
    _instance(session).set_attr(ACQ_DATE, ["20230101", "20230202"])
    with session:
        report = session.audit()
        assert len([f for f in report.findings if f.tag == ACQ_DATE]) == 1
