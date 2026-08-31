"""A redaction zone that could not be applied is a failure, not a shrug.

`apply_redaction_to_array` re-raises deliberately, under a comment saying
that a silent skip ships the unredacted image. One frame up, the
enclosing loop caught bare `Exception` and only logged -- so the raise
travelled exactly one stack frame. `modified` stayed `False`, no
`DERIVED` flag was written, no `_ISOCENTER_REDACTION_HASH` was set, **no
audit row existed**, `session.redact()` returned a number, and the
instance exported with its burned-in identifier intact and graded `PASS`.

The whole of the defect is that the burned-in identifier is still in the
pixels and nothing anywhere says so. These tests state what a caller and
a compliance report can now rely on: an `ERROR` row per failed instance,
a `REVIEW_REQUIRED` grade, a `RedactionError` after the pass, the
successful instances still redacted, and the failed one left exactly as
it was found -- on **both** gate interpreters, which used to disagree
(#213).
"""
import sqlite3

import numpy as np
import pytest

from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.services import RedactionError, RedactionService
from isocenter.session import DicomSession

SN_OK = "SERIAL_OK"
SN_BAD = "SERIAL_BAD"

#: A zone whose second element cannot become an int. This is the original
#: #66 trigger and depends on nothing #215 added.
BAD_ZONE = [0, "abc", 0, 8]
GOOD_ZONE = [0, 8, 0, 8]


def _series(serial, uids):
    series = Series(f"SE_{serial}", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", serial)
    for n, uid in enumerate(uids):
        inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", n + 1)
        inst.file_path = None
        inst.set_pixel_data(np.full((32, 32), 200, dtype=np.uint8))
        series.instances.append(inst)
    return series


def _hydrated(db_file, build):
    """Build a graph, save it, and hand back a session that reloaded it.

    **The reopen is load-bearing and `save()` alone will not do.** Measured:
    after `session.save()` an instance built in memory still has
    `_pixel_loader is None` and `file_path is None`, so
    `unload_pixel_data()` refuses -- correctly, since clearing would be a
    silent discard -- and the mutated array stays resident for the next
    `save()` to write. Only a *reloaded* instance carries the
    `SidecarPixelLoader` that makes "drop the array and read the original
    back" possible, which is what leaves a failed instance as it was found.
    An ingested instance has a `file_path` and behaves the same way; the
    build-save-redact-in-one-session shape is the case §3.7 of the design
    states it cannot reach.
    """
    session = DicomSession(str(db_file))
    build(session)
    session.save()
    session.close()
    return DicomSession(str(db_file))


def _build_two_machines(session):
    """Two machines: two sound instances, and one to be given a bad zone."""
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    study.series.append(_series(SN_OK, ["1.2.3.ok.1", "1.2.3.ok.2"]))
    study.series.append(_series(SN_BAD, ["1.2.3.bad.1"]))
    patient.studies.append(study)
    session.store.patients.append(patient)


def _session(tmp_path, bad_zone=BAD_ZONE, name="redactfail"):
    """The standard fixture: three saved instances, one unapplicable zone.

    One rule per machine, deliberately: `prepare_redaction_tasks` hashes
    its configuration with `sorted(valid_rois)`, and two zones in one rule
    whose first differing element is a string against an int raise
    `TypeError` out of `sorted` before any worker runs. That is a
    different, loud defect (filed separately) and it would mask this one.
    """
    session = _hydrated(tmp_path / f"{name}.db", _build_two_machines)
    session.configuration.rules = [
        {"serial_number": SN_OK, "redaction_zones": [GOOD_ZONE]},
        {"serial_number": SN_BAD, "redaction_zones": [bad_zone]},
    ]
    return session


def _instances(session):
    """Map pre-redaction SOP UID to instance -- call this *before* redacting.

    A successful redaction calls `regenerate_uid()`, and on the threads
    path (3.14t's default) the worker mutates the parent's own object, so
    the UID a test looked one up by is gone by the time it wants to assert
    on it. The object references are stable in both modes; the keys are
    not.
    """
    return {i.sop_instance_uid: i
            for p in session.store.patients
            for st in p.studies
            for se in st.series
            for i in se.instances}


def _audit(db_path, action_type="ERROR"):
    """Read the rows straight out of sqlite, after the session is closed.

    Not `get_audit_errors()`: #218 is open on that reader missing a row
    still in flight on the writer thread, and this file must not depend on
    it. `generate_report()` is the other safe path -- it calls `stop()`,
    which joins the thread and drains the queue -- and test 11 uses it.
    """
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


# --- 8, 9 ------------------------------------------------------------------

def test_a_zone_that_cannot_be_applied_raises_instead_of_returning_a_count(
        tmp_path):
    """The count was the entire report, and it counted the successes."""
    session = _session(tmp_path)
    try:
        with pytest.raises(RedactionError) as excinfo:
            session.redact(show_progress=False)

        assert len(excinfo.value.failures) == 1
        assert "1.2.3.bad.1" in str(excinfo.value)
        assert excinfo.value.attempted == 3
    finally:
        session.close()


def test_the_instances_that_could_be_redacted_still_were(tmp_path):
    """The raise comes after the pass, not at the first failure.

    An implementation that raises from the worker, or on the first
    failure, terminates the result generator mid-iteration and loses every
    mutation queued behind it -- so the images that *were* redacted
    silently never reach the graph.
    """
    session = _session(tmp_path)
    insts = _instances(session)
    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)

        for uid in ("1.2.3.ok.1", "1.2.3.ok.2"):
            inst = insts[uid]
            assert inst.attributes.get("_ISOCENTER_REDACTION_HASH")
            assert "DERIVED" in inst.attributes.get("0008,0008", [])
            # Through the loader, not the resident array: on the processes
            # path the parent never sees the worker's array, only the
            # sidecar handle the mutation carried back.
            inst.unload_pixel_data()
            assert inst.get_pixel_data()[0:8, 0:8].sum() == 0

        bad = insts["1.2.3.bad.1"]
        assert "_ISOCENTER_REDACTION_HASH" not in bad.attributes
        assert "DERIVED" not in bad.attributes.get("0008,0008", [])
    finally:
        session.close()


# --- 10 --------------------------------------------------------------------

def test_a_failed_redaction_writes_one_error_audit_row(tmp_path):
    """Zero rows meant `get_audit_errors()` was empty and the grade `PASS`."""
    session = _session(tmp_path)
    db_path = session.store_backend.db_path
    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)
    finally:
        session.close()

    rows = _audit(db_path)
    assert len(rows) == 1, rows
    uid, details = rows[0]
    assert uid == "1.2.3.bad.1", "the row must name the instance, pre-redaction"
    assert "ValueError" in details, details
    assert "abc" in details or "int()" in details, details


# --- 11 --------------------------------------------------------------------

@pytest.mark.parametrize("arm,expected", [("failure", "REVIEW_REQUIRED"),
                                          ("control", "PASS")])
def test_a_failed_redaction_grades_the_report_review_required(
        tmp_path, arm, expected):
    """The control arm is not optional.

    An empty `audit_summary` grades `REVIEW_REQUIRED` on its own, so a
    test without a `PASS` control passes with the whole fix deleted. Both
    arms anonymize first for exactly that reason.
    """
    zone = BAD_ZONE if arm == "failure" else GOOD_ZONE
    session = _session(tmp_path, bad_zone=zone, name=f"grade_{arm}")
    report = tmp_path / f"report_{arm}.md"
    try:
        session.anonymize()
        if arm == "failure":
            with pytest.raises(RedactionError):
                session.redact(show_progress=False)
        else:
            assert session.redact(show_progress=False) == 3
        session.generate_report(str(report))
    finally:
        session.close()

    assert expected in report.read_text(encoding="utf-8")


# --- 13 --------------------------------------------------------------------

def test_a_task_with_nothing_to_do_is_not_a_failure(tmp_path):
    """The guard against implementing this as "no mutation means failure".

    That would invert the very conflation the change removes: an instance
    already redacted under this configuration, and one with no pixel data,
    are legitimate skips and always were.
    """
    session = _session(tmp_path, bad_zone=GOOD_ZONE, name="idempotent")
    db_path = session.store_backend.db_path
    try:
        assert session.redact(show_progress=False) == 3
        assert session.redact(show_progress=False) == 0, (
            "a second pass under the same configuration redacts nothing")
    finally:
        session.close()

    assert _audit(db_path) == []


def test_an_instance_with_no_pixel_data_is_skipped_not_failed(
        tmp_path, monkeypatch):
    """`get_pixel_data()` returning None has always been a silent skip."""
    session = _session(tmp_path, bad_zone=GOOD_ZONE, name="nopixels")
    db_path = session.store_backend.db_path
    monkeypatch.setattr(Instance, "get_pixel_data", lambda self: None)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    try:
        assert session.redact(show_progress=False) == 0
    finally:
        session.close()

    assert _audit(db_path) == []


# --- 14 --------------------------------------------------------------------

@pytest.mark.parametrize("lever", ["ISOCENTER_FORCE_THREADS",
                                   "ISOCENTER_FORCE_PROCESSES"])
def test_a_failed_instance_is_left_as_it_was_found(tmp_path, monkeypatch,
                                                   lever):
    """The two executors used to disagree about what a failure leaves behind.

    `apply_redaction_to_array` raises *mid-loop*, so zone 1 is already
    zeroed when zone 2 fails, and `execute_redaction_task`'s `finally`
    persisted unconditionally. Measured on `258331c`: under threads
    (3.14t's default) zone 1 came back zeroed through save/close/reopen;
    under processes (3.12's default) the instance was untouched, and the
    worker's mutated copy went into the shared sidecar as orphan bytes.
    The same failed redaction, two different stores, decided by the
    interpreter.

    The `monkeypatch.setenv` is legitimate here and would not be on the
    export path: `_resolve_strategy` reads these variables **in the
    parent** when it picks the executor, and `_apply_redaction_rules`
    passes no `maxtasksperchild`, so the parent's environment decides.
    `session.export()`'s workers are always separate processes and a
    parent monkeypatch is invisible to them.

    Fails on `258331c` under threads; passes there under processes.
    """
    monkeypatch.setenv(lever, "1")
    db_file = tmp_path / f"leftalone_{lever}.db"

    def build(sess):
        patient = Patient("P1", "Test^Patient")
        study = Study("ST_1", "20230101")
        study.series.append(_series(SN_BAD, ["1.2.3.partial"]))
        patient.studies.append(study)
        sess.store.patients.append(patient)

    session = _hydrated(db_file, build)

    # First elements differ, so `sorted(valid_rois)` never compares the
    # string to an int. Zone 1 applies; zone 2 raises.
    session.configuration.rules = [{
        "serial_number": SN_BAD,
        "redaction_zones": [[0, 8, 0, 8], [1, "abc", 0, 8]],
    }]

    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)
        session.save()
    finally:
        session.close()

    reopened = DicomSession(str(db_file))
    try:
        reloaded = _instances(reopened)["1.2.3.partial"]
        arr = reloaded.get_pixel_data()
        assert arr is not None, "the fixture lost its pixels; the test is vacuous"
        assert arr[0:8, 0:8].sum() == 8 * 8 * 200, (
            "a partially-applied redaction was persisted; the failed "
            "instance is not as it was found")
        assert int(arr.sum()) == 32 * 32 * 200
        assert "_ISOCENTER_REDACTION_HASH" not in reloaded.attributes
        assert "DERIVED" not in reloaded.attributes.get("0008,0008", [])
    finally:
        reopened.close()


# --- 15 --------------------------------------------------------------------

def test_a_failed_zone_can_be_retried(tmp_path):
    """Reporting the failure must not make it permanent."""
    session = _session(tmp_path, name="retry")
    bad = _instances(session)["1.2.3.bad.1"]
    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)

        session.configuration.rules[1]["redaction_zones"] = [GOOD_ZONE]
        assert session.redact(show_progress=False) == 1

        assert bad.attributes.get("_ISOCENTER_REDACTION_HASH")
        bad.unload_pixel_data()
        assert bad.get_pixel_data()[0:8, 0:8].sum() == 0
    finally:
        session.close()


# --- 16 --------------------------------------------------------------------

def test_the_geometry_contradiction_reaches_the_same_report(tmp_path):
    """The second, independent entry into the same sink.

    `resolve_pixel_geometry` raises `ValueError` when the array's shape
    cannot be reconciled with the instance's declared descriptors (#215),
    and the changelog filed that as "a second path into #213 ... filed,
    not fixed here". It closes here. Kept as a second arm rather than the
    primary fixture because a test built on it could go green on a change
    to the resolver rather than on the clause under test.
    """
    db_file = tmp_path / "contradiction.db"
    session = DicomSession(str(db_file))
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    series = Series("SE_1", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", SN_BAD)
    inst = Instance("1.2.3.contradiction", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.file_path = None
    inst.pixel_array = np.full((5, 8, 4), 200, dtype=np.uint8)
    inst.set_attr("0028,0002", 3)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)

    session.configuration.rules = [
        {"serial_number": SN_BAD, "redaction_zones": [GOOD_ZONE]}]

    db_path = session.store_backend.db_path
    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)
    finally:
        session.close()

    rows = _audit(db_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == "1.2.3.contradiction"
    assert "ValueError" in rows[0][1], rows[0][1]
