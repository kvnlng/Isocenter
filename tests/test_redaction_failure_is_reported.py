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
import pydicom
import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.services import RedactionError, RedactionService
from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"

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
    silent discard -- and there is no way to read the stored pixels back at
    all. Only a *reloaded* instance carries the `SidecarPixelLoader` these
    tests need in order to drop the resident array and see what the store
    actually holds.

    **A reloaded instance can now hold a partial mutation, which it could
    not before #229**, and the change is mechanical rather than anything
    about this helper: the read-only array used to be copied afresh for
    every zone, so zones 1..k-1 were never in the array the instance ended
    up with when zone k raised. `_redact_instance_pixels` copies once, so
    they are. What keeps them out of the store is the persist gate plus
    the unconditional `unload_pixel_data()`.

    No caller of this helper exercises that: `_session()` gives each
    machine a **one-zone** rule, deliberately (see its docstring), and a
    single zone cannot fail after another has applied. The reloaded case
    that does ask the question is test 18, which builds its own instance
    through `reloaded_redaction_session` rather than through here. See
    `_write_source` for the file-backed shape, which decided this before a
    reloaded one could.
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


# --- 12 --------------------------------------------------------------------

def test_the_serial_path_reports_the_same_failure_as_the_parallel_one(
        tmp_path):
    """`redact_machine_instances` is public and swallowed just as quietly.

    It is what `process_machine_rules` calls, it is exercised directly by
    five test files, and its `except Exception` logged and moved on --
    the same silence in the same operation, one method over.
    """
    session = _session(tmp_path, name="serial")
    db_path = session.store_backend.db_path
    service = RedactionService(session.store, session.store_backend)
    try:
        with pytest.raises(RedactionError) as excinfo:
            service.process_machine_rules(
                {"serial_number": SN_BAD, "redaction_zones": [BAD_ZONE]},
                show_progress=False)
        assert len(excinfo.value.failures) == 1
    finally:
        session.close()

    rows = _audit(db_path)
    assert len(rows) == 1, rows
    assert rows[0][0] == "1.2.3.bad.1"
    assert "ValueError" in rows[0][1], rows[0][1]


def test_the_serial_path_also_leaves_a_failed_instance_as_it_was_found(
        tmp_path):
    """`redact_machine_instances` has the same unconditional persist.

    Its `finally` called `persist_pixel_data` whatever happened, so a
    partially-zeroed array became durable and the instance picked up a
    sidecar loader pointing at it -- with no hash and no `DERIVED` to say
    a redaction had been attempted. The parallel path's gate would not
    have covered this one; it is a second `finally`, in a different
    method, and it needs its own test. Same file-backed fixture as test
    14, for the same reason (see `_write_source`).
    """
    source = tmp_path / "src_serial.dcm"
    _write_source(source)

    session = DicomSession(str(tmp_path / "serial_partial.db"))
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    series = Series("SE_1", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", SN_BAD)
    inst = Instance("1.2.3.partial", SC_STORAGE, 1)
    inst.file_path = str(source)
    for tag, value in (("0028,0010", 32), ("0028,0011", 32),
                       ("0028,0002", 1), ("0028,0100", 8)):
        inst.set_attr(tag, value)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)

    assert inst.get_pixel_data().flags.writeable, (
        "the fixture stopped being file-backed; this test is written "
        "against the writeable arm, which mutates the instance's own "
        "array in place -- see `_write_source` for why that shape was "
        "chosen, and test 18 for the reloaded one")
    inst.unload_pixel_data()

    service = RedactionService(session.store, session.store_backend)
    try:
        with pytest.raises(RedactionError):
            service.process_machine_rules(
                {"serial_number": SN_BAD,
                 "redaction_zones": [[0, 8, 0, 8], [1, "abc", 0, 8]]},
                show_progress=False)

        inst.unload_pixel_data()
        arr = inst.get_pixel_data()
        assert int(arr[0:8, 0:8].sum()) == 8 * 8 * 200, (
            "the serial path persisted a partially-applied redaction")
        assert int(arr.sum()) == 32 * 32 * 200
        assert inst._pixel_loader is None
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
    finally:
        session.close()


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


def test_a_worker_result_that_is_not_an_outcome_is_a_failure(
        tmp_path, monkeypatch):
    """A bare `None` from the worker is a failure row, not a silent skip.

    This is the third result shape `_apply_redaction_outcomes` has to
    survive, and the one with no production caller: `execute_redaction_task`
    always returns a `RedactionOutcome` today. It is pinned anyway because
    tolerating an unrecognised result is exactly the conflation #213
    removes -- `None` used to mean "already redacted", "no pixels" and "it
    blew up" at once -- and because the only place a bare `None` ever
    reached the parent was a test stub, which this change had to edit. A
    stub is precisely what would go on passing against a contract the code
    no longer implements, so the clause needs a guard that is not a stub's
    incidental behaviour.

    `run_parallel` is replaced with a serial map rather than forcing
    threads, the way `test_redact_reports_outcome.py` does it: a
    monkeypatched worker cannot reach a separate process, and this way the
    test does not depend on which executor the interpreter picks.
    """
    import isocenter.services as services

    session = _session(tmp_path, bad_zone=GOOD_ZONE, name="unrecognised")
    db_path = session.store_backend.db_path
    monkeypatch.setattr(services.RedactionService, "execute_redaction_task",
                        lambda self, task: None)
    monkeypatch.setattr("isocenter.session.run_parallel",
                        lambda fn, items, **_kwargs: [fn(i) for i in items])

    try:
        with pytest.raises(RedactionError) as excinfo:
            session.redact(show_progress=False)
        assert len(excinfo.value.failures) == 3, (
            "a worker result the parent cannot read was skipped, not counted")
    finally:
        session.close()

    rows = _audit(db_path)
    assert len(rows) == 3, rows
    assert all(uid == "UNKNOWN" for uid, _ in rows), rows
    assert all("unrecognised" in detail for _, detail in rows), rows


# --- 14 --------------------------------------------------------------------

def _write_source(path, uid="1.2.3.partial"):
    """A real one-instance DICOM file, 32x32 uint8 all 200.

    **The instance under test is backed by a source file, and that was the
    whole design of this test when it was written.** Measured three
    shapes, on `4507d48`:

    * *reloaded from the sidecar* -- `SidecarPixelLoader` returns a
      **read-only** array, and the redaction wrapper copied it afresh for
      every ROI, so the instance ended holding a copy of the *pristine*
      original. A partial mutation could never accumulate, so this shape
      could not detect the persist at all. **That same mechanism was a
      live defect in its own right and not only an inconvenience here**: a
      copy taken from the pristine original per ROI discarded the zones
      already applied, so a rule with N zones applied only the Nth to a
      reloaded instance and reported success. That was #229, and it is
      fixed: `_redact_instance_pixels` copies **once** and applies the
      whole zone list to that copy. **The consequence for this file is
      that the reloaded shape now behaves like this one** -- zone 1 really
      is zeroed when zone 2 raises -- so it detects the persist too, and
      test 18 uses it. What keeps the partial mutation out of the store
      there is the same `finally` gate this test pins, plus the
      unconditional `unload_pixel_data()`, which succeeds because
      `set_pixel_data` does not clear `_pixel_loader`.
    * *built in memory and never reloaded* -- writeable and mutated in
      place, but `unload_pixel_data()` refuses (no loader, no file path,
      clearing would be a silent discard), so the mutated array stays
      resident whatever the persist does. This is the case the design
      states it cannot reach.
    * *backed by a source file* -- pydicom hands back a **writeable**
      array, so zone 1 really is zeroed in place when zone 2 raises, and
      `unload_pixel_data()` succeeds because the file can restore it. This
      is the only shape in which the persist decides the outcome, and it is
      the ordinary one: every ingested instance is this shape.
    """
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = SC_STORAGE
    ds.file_meta.MediaStorageSOPInstanceUID = uid
    ds.SOPClassUID = SC_STORAGE
    ds.SOPInstanceUID = uid
    ds.Rows = 32
    ds.Columns = 32
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((32, 32), 200, dtype=np.uint8).tobytes()
    ds.save_as(str(path), enforce_file_format=True)


@pytest.mark.parametrize("lever", ["ISOCENTER_FORCE_THREADS",
                                   "ISOCENTER_FORCE_PROCESSES"])
def test_a_failed_instance_is_left_as_it_was_found(tmp_path, monkeypatch,
                                                   lever):
    """The two executors used to disagree about what a failure leaves behind.

    `apply_redaction_to_array` raises *mid-loop*, so zone 1 is already
    zeroed when zone 2 fails, and `execute_redaction_task`'s `finally`
    called `persist_pixel_data` unconditionally. Under threads -- 3.14t's
    default -- the worker mutates the parent's own array and then wrote it
    to the sidecar, attaching a loader, so the partial redaction became
    **durable**: an instance carrying half a redaction, no
    `_ISOCENTER_REDACTION_HASH`, no `DERIVED`, and nothing saying so.
    Under processes -- 3.12's default -- the child mutated a copy the
    parent never sees, so the instance was untouched. The same failed
    redaction, two different stores, decided by the interpreter. Measured
    with the persist gate removed and everything else in place: zone 1
    comes back `0` on threads and `12800` on processes.

    The `monkeypatch.setenv` is legitimate here and would not be on the
    export path: `_resolve_strategy` reads these variables **in the
    parent** when it picks the executor, and `_apply_redaction_rules`
    passes no `maxtasksperchild`, so the parent's environment decides.
    `session.export()`'s workers are always separate processes and a
    parent monkeypatch is invisible to them.
    """
    monkeypatch.setenv(lever, "1")
    source = tmp_path / f"src_{lever}.dcm"
    _write_source(source)

    session = DicomSession(str(tmp_path / f"leftalone_{lever}.db"))
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    series = Series("SE_1", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", SN_BAD)
    inst = Instance("1.2.3.partial", SC_STORAGE, 1)
    inst.file_path = str(source)
    for tag, value in (("0028,0010", 32), ("0028,0011", 32),
                       ("0028,0002", 1), ("0028,0100", 8)):
        inst.set_attr(tag, value)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)

    assert inst.get_pixel_data().flags.writeable, (
        "the fixture stopped being file-backed; this test is written "
        "against the writeable arm, which mutates the instance's own "
        "array in place -- see `_write_source` for why that shape was "
        "chosen, and test 18 for the reloaded one")
    inst.unload_pixel_data()

    # First elements differ, so `sorted(valid_rois)` never compares the
    # string to an int. Zone 1 applies; zone 2 raises.
    session.configuration.rules = [{
        "serial_number": SN_BAD,
        "redaction_zones": [[0, 8, 0, 8], [1, "abc", 0, 8]],
    }]

    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)

        inst.unload_pixel_data()
        arr = inst.get_pixel_data()
        assert arr is not None, "the fixture lost its pixels; the test is vacuous"
        assert int(arr[0:8, 0:8].sum()) == 8 * 8 * 200, (
            "a partially-applied redaction was persisted; the failed "
            "instance is not as it was found")
        assert int(arr.sum()) == 32 * 32 * 200
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        assert "DERIVED" not in inst.attributes.get("0008,0008", [])
        assert inst._pixel_loader is None, (
            "a failed instance was given a sidecar loader, which means its "
            "partial redaction reached the store")
    finally:
        session.close()


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


# --- 17 --------------------------------------------------------------------

def _file_backed(serial, uid, source):
    """One file-backed instance under its own series, keyed by scanner."""
    series = Series(f"SE_{serial}", "OT", 1)
    series.equipment = Equipment("Acme", "Scanner", serial)
    inst = Instance(uid, SC_STORAGE, 1)
    inst.file_path = str(source)
    for tag, value in (("0028,0010", 32), ("0028,0011", 32),
                       ("0028,0002", 1), ("0028,0100", 8)):
        inst.set_attr(tag, value)
    series.instances.append(inst)
    return series


def test_a_zone_that_could_not_be_applied_never_reaches_disk(tmp_path):
    """The failure half of the headline safety claim, which nothing asserts.

    `redact()` now raises, but a caller who catches it -- or ignores it,
    which the audit row and the `REVIEW_REQUIRED` grade exist for -- can
    still call `export()`. What keeps the burned-in identifier off disk on
    that route is not the raise: it is that `_export_instance_worker`
    re-applies `ctx.redaction_zones` to the array it is about to write, so
    the zone that failed in `redact()` fails again in the worker and the
    write is refused.

    The re-application was not wholly untested -- `test_export_contract.py
    ::test_an_export_plan_carries_the_configured_redaction_zones` asserts
    the plan carries the zones, and `test_redaction_export.py
    ::test_export_compressed_redaction` exports a *successfully* redacted
    instance and reads the zeroed pixels back. What neither says anything
    about is a zone that **raises**, which is the only case #213 is about:
    they show that a zone which works is carried and applied, not that a
    zone which fails stops the write.

    Measured with `redaction_zones=[]` left at the single population site
    (`DicomSession._build_export_tasks`) and nothing else changed: the
    failed instance exports a file, and its pixels read 200 across the
    region the rule was meant to zero -- 12800 over the 8x8 zone -- on
    both gate interpreters. That is the whole of #213 arriving on disk
    through a route the raise does not guard. The full suite under that
    mutation fails three tests: this one and the two named above.

    Two arms, and only one of them carries that polarity:

    * The **failed** instance has no `_pixel_loader` -- the persist gate
      saw to that -- so the worker re-reads the pristine source file and
      the zones are the only thing standing between it and the write. This
      arm fails under the mutation.
    * The **successful** instance is here for selectivity: a refusal that
      refuses everything would satisfy the first arm on its own. It does
      *not* fail under the mutation, and cannot -- its redaction was
      persisted, so `get_pixel_data()` comes back through a
      `SidecarPixelLoader` already zeroed and the worker's re-application
      has nothing left to do. Measured, not assumed.

    No monkeypatch anywhere: `session.export()`'s workers are separate
    processes on every interpreter (#185 pins `maxtasksperchild=25`, which
    rules threads out), so a patch installed in the parent would never
    reach the code under test. The zone is genuinely malformed instead.

    The successful instance's filename is read off the live object rather
    than written down, because the two interpreters disagree about it.
    `redact()` is *not* export -- it takes the threads path on 3.14t --
    and there the worker regenerates the UID on the parent's own object,
    so the exported file is named for the new UID; on 3.12's processes
    path the parent keeps `1.2.3.ok`. The export reads the same object, so
    reading it back is right on both. The failed instance's UID is
    asserted unchanged in the same breath: a redaction that did not happen
    regenerates nothing.

    This covers `session.export()` and only that. `write_tree()` is the
    other public way to write DICOM and carries the same leak, and it is
    not coverable from here: it takes no configuration, sets no
    `redaction_zones`, and says so in its own docstring. What closes that
    route is `redact()` raising, upstream of any call to it.
    """
    src_ok = tmp_path / "src_ok.dcm"
    src_bad = tmp_path / "src_bad.dcm"
    _write_source(src_ok, uid="1.2.3.ok")
    _write_source(src_bad, uid="1.2.3.bad")

    # If the source were already clean, the export could refuse for any
    # reason at all and this test would still pass.
    assert int(pydicom.dcmread(str(src_bad)).pixel_array.sum()) == 32 * 32 * 200

    session = DicomSession(str(tmp_path / "export_refusal.db"))
    patient = Patient("P1", "Test^Patient")
    study = Study("ST_1", "20230101")
    study.series.append(_file_backed(SN_OK, "1.2.3.ok", src_ok))
    study.series.append(_file_backed(SN_BAD, "1.2.3.bad", src_bad))
    patient.studies.append(study)
    session.store.patients.append(patient)

    session.configuration.rules = [
        {"serial_number": SN_OK, "redaction_zones": [GOOD_ZONE]},
        {"serial_number": SN_BAD,
         "redaction_zones": [[0, 8, 0, 8], [1, "abc", 0, 8]]},
    ]

    out = tmp_path / "out"
    db_path = session.store_backend.db_path
    good, bad = _instances(session)["1.2.3.ok"], _instances(session)["1.2.3.bad"]
    try:
        with pytest.raises(RedactionError):
            session.redact(show_progress=False)

        # j2k off: a compression failure is another producer of
        # `ExportOutcome(ok=False)`, and this test must not be able to pass
        # on one.
        session.export(str(out), use_compression=False, show_progress=False)
    finally:
        session.close()

    assert bad.sop_instance_uid == "1.2.3.bad", (
        "the failed instance's UID was regenerated, so it was treated as "
        "redacted somewhere")
    written = sorted(p.name for p in out.rglob("*.dcm"))
    assert written == [f"{good.sop_instance_uid}.dcm"], (
        "the instance whose zone could not be applied was written to disk "
        "with its burned-in identifier intact")

    arr = pydicom.dcmread(str(next(out.rglob(written[0])))).pixel_array
    assert int(arr[0:8, 0:8].sum()) == 0, (
        "the export refused every instance rather than the failed one")
    assert int(arr.sum()) == 32 * 32 * 200 - 8 * 8 * 200

    # `redact()` wrote an ERROR row for this instance too; the export's is
    # a second, separate row and the one under test here.
    rows = [(uid, detail) for uid, detail in _audit(db_path)
            if "Export failed for" in detail]
    assert len(rows) == 1, rows
    assert rows[0][0] == "1.2.3.bad", rows
    assert "invalid literal for int" in rows[0][1], (
        "the export refused for some other reason -- a guessed geometry, a "
        f"missing pixel element -- not the zone: {rows[0][1]}")


# --- 18 --------------------------------------------------------------------

@pytest.mark.parametrize("lever", ["ISOCENTER_FORCE_THREADS",
                                   "ISOCENTER_FORCE_PROCESSES"])
def test_a_failed_reloaded_instance_is_left_as_it_was_found(
        reloaded_redaction_session, monkeypatch, lever):
    """The reloaded shape, where #229's fix created a partial mutation.

    **This is a guard on a new risk, not a reproduction of #229.** It
    passes on `4507d48` too, and for a reason worth stating: there, a
    read-only array was copied *afresh for every zone*, so zones 1..k-1
    were never in the array the instance ended up holding when zone k
    raised. No partial mutation could form. #229's fix makes the copy once,
    so the good zone **is** zeroed in the instance's array when the bad one
    raises. What this test asserts is that the end state is unchanged
    anyway, by the chain the fix relies on:

    1. `failed = True`, so the `finally` block's persist gate does not run
       and nothing partial reaches the sidecar. That gate is the whole of
       #213's "a failed instance is left as it was found".
    2. `inst.unload_pixel_data()` then runs unconditionally, and succeeds,
       because `set_pixel_data` **does not clear `_pixel_loader`**. The
       partially-zeroed copy is dropped.
    3. The next `get_pixel_data()` reloads the original through the loader.

    So this is a detection guard for a *different* mutation than #229's:
    an implementation that also moves, weakens or reorders the persist
    gate. Do not count it as evidence the multi-zone loss was fixed.

    The `monkeypatch.setenv` lever is the one
    `test_a_failed_instance_is_left_as_it_was_found` already uses and is
    legitimate for the same reason: `_resolve_strategy` reads these
    variables **in the parent**. The threads leg is the one that can
    exercise the risk at all -- under processes the child mutates a copy
    the parent never sees -- and the processes leg is kept because these
    two `finally` blocks have diverged before (#213), which is precisely
    how the interpreter-dependent store happened.

    Unlike test 14 this instance has a `SidecarPixelLoader` by
    construction, so it asserts on what the **store** holds after a
    save/close/reopen rather than on `_pixel_loader is None`.
    """
    monkeypatch.setenv(lever, "1")

    # First elements differ, so `prepare_redaction_tasks`'s
    # `sorted(valid_rois)` compares 0 against 1 and never `8` against
    # `"x"`. The `[0, "abc", 0, 8]` spelling used above raises `TypeError`
    # out of `sorted` before any worker runs when it shares a rule with an
    # int zone -- a different, louder defect that would mask this one.
    session, inst = reloaded_redaction_session(
        [[0, 8, 0, 8], [1, "x", 0, 8]], name=f"partial_{lever}")
    db_path = session.store_backend.db_path

    with pytest.raises(RedactionError):
        session.redact(show_progress=False)

    assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
    assert "DERIVED" not in inst.attributes.get("0008,0008", [])

    session.save()
    session.close()

    rows = _audit(db_path)
    assert len(rows) == 1, rows
    assert "invalid literal for int" in rows[0][1], rows

    reopened = DicomSession(db_path)
    try:
        stored = next(i
                      for p in reopened.store.patients
                      for st in p.studies
                      for se in st.series
                      for i in se.instances)
        arr = stored.get_pixel_data()
        assert arr is not None, "the fixture lost its pixels; this is vacuous"
        assert int(arr[0:8, 0:8].sum()) == 8 * 8 * 200, (
            "the zone applied before the failure was persisted; the failed "
            "instance is not as it was found")
        assert int(arr.sum()) == 32 * 32 * 200
    finally:
        reopened.close()
