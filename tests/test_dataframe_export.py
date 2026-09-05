import pytest
import pandas as pd
import os
import sqlite3
import threading
from isocenter.session import DicomSession
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.persistence import IsocenterJSONEncoder
import json

@pytest.fixture
def session_with_data(tmp_path):
    db_path = tmp_path / "isocenter_test.db"
    session = DicomSession(str(db_path))

    # Manually populate the database with some hierarchical data
    # We use SQL directly to simulate a populated state, or use the object model if possible.
    # Using SQL directly is more robust for testing the persistence Read path specifically.

    # Or better: Create objects and use save_all.
    p = Patient("P1", "Test Patient")
    st = Study("ST1", "20230101")
    se = Series("SE1", "CT", 101, equipment=None)
    inst = Instance("I1", "1.2.840.123", 1)
    inst.file_path = "/tmp/fake.dcm"
    inst.attributes = {"PatientName": "Test Patient", "Modality": "CT", "SliceThickness": 1.5}

    se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)

    session.store.patients = [p]
    session.save() # This triggers the full save pipeline
    session.persistence_manager.flush() # Wait for DB write

    yield session
    session.close()

def test_export_dataframe_basic(session_with_data):
    df = session_with_data.export_dataframe()
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 1
    assert df.iloc[0]['PatientID'] == "P1"
    assert df.iloc[0]['SOPInstanceUID'] == "I1"
    # Check default columns exist
    expected_cols = ['PatientID', 'StudyInstanceUID', 'SeriesInstanceUID', 'SOPInstanceUID']
    for col in expected_cols:
        assert col in df.columns

def test_export_dataframe_parquet(session_with_data, tmp_path):
    output_path = tmp_path / "export.parquet"
    df = session_with_data.export_dataframe(str(output_path))

    assert os.path.exists(output_path)

    # Verify we can read it back
    df_read = pd.read_parquet(output_path)
    assert len(df_read) == 1
    assert df_read.iloc[0]['PatientID'] == "P1"

def test_export_dataframe_expand_metadata(session_with_data):
    # This requires us to modify the implementation to actually parse the JSON if expand_metadata=True
    # For now, let's assume we implement it or at least call it.
    df = session_with_data.export_dataframe(expand_metadata=True)

    # If expansion works, we should see "SliceThickness" as a column or at least check logic
    # The current plan is to implement it, so let's assert it.

    # Note: sqlite persistence stores attributes_json.
    # Our mocked data had SliceThickness = 1.5

    assert 'SliceThickness' in df.columns
    assert df.iloc[0]['SliceThickness'] == 1.5

def test_get_flattened_instances_still_has_a_caller_holding_its_coverage(
        session_with_data):
    """`SqliteStore.get_flattened_instances` has no production caller (#142).

    It had exactly one, `DicomSession.export_to_parquet`, deleted in #55
    when Parquet export was collapsed onto `export_dataframe`. This is
    the second time the method has been left uncovered by a deletion
    elsewhere: `DicomExporter.generate_export_from_db` went first, as
    dead code, and took the only test exercising the method with it.

    So the coverage is anchored here, on the method itself, rather than
    on whatever happens to call it this month. Whether the method should
    survive with no caller is #142's question -- but it must not be
    possible to delete it *by accident*, which is what losing the last
    test would amount to.
    """
    rows = list(session_with_data.store_backend.get_flattened_instances(["P1"]))

    assert len(rows) == 1
    # The DB path speaks SQL column names, unlike `get_cohort_report`'s
    # DICOM keywords -- the divergence that made two Parquet writers
    # produce two different frames.
    assert rows[0]["patient_id"] == "P1"
    assert rows[0]["sop_instance_uid"] == "I1"
    assert rows[0]["modality"] == "CT"


def test_get_flattened_instances_filters_by_patient_id(session_with_data):
    assert list(session_with_data.store_backend.get_flattened_instances(["NOPE"])) == []


@pytest.fixture
def session_with_two_patients(tmp_path):
    """Two patients, so a filter has something to exclude."""
    session = DicomSession(str(tmp_path / "cohort.db"))

    for pid in ("P1", "P2"):
        p = Patient(pid, f"Patient {pid}")
        st = Study(f"ST-{pid}", "20230101")
        se = Series(f"SE-{pid}", "CT", 101, equipment=None)
        inst = Instance(f"I-{pid}", "1.2.840.123", 1)
        inst.attributes = {"Modality": "CT"}
        se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        session.store.patients.append(p)

    session.save()
    session.persistence_manager.flush()

    yield session
    session.close()


def test_get_cohort_report_filters_by_patient_ids(session_with_two_patients):
    df = session_with_two_patients.get_cohort_report(patient_ids=["P1"])

    assert list(df["PatientID"]) == ["P1"]


def test_get_cohort_report_without_a_filter_returns_everyone(session_with_two_patients):
    df = session_with_two_patients.get_cohort_report()

    assert sorted(df["PatientID"]) == ["P1", "P2"]


def test_an_empty_patient_id_list_selects_nobody(session_with_two_patients):
    """`[]` is a filter that matched nothing, not a missing filter.

    `None` means "no filter given". Collapsing `[]` into it would make
    a caller passing a computed-and-empty cohort export the whole
    dataset -- the one outcome a filter exists to prevent.
    """
    df = session_with_two_patients.get_cohort_report(patient_ids=[])

    assert df.empty


def test_export_dataframe_filters_by_patient_ids(session_with_two_patients, tmp_path):
    output_path = tmp_path / "one.csv"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["P2"])

    assert list(df["PatientID"]) == ["P2"]
    assert list(pd.read_csv(output_path)["PatientID"]) == ["P2"]


def test_export_dataframe_creates_a_missing_output_directory(session_with_data, tmp_path):
    """Carried over from `export_to_parquet`, which did this and
    `export_dataframe` did not. Without it a caller migrating a nested
    path gets a bare `FileNotFoundError` out of pandas."""
    output_path = tmp_path / "reports" / "2026" / "cohort.parquet"

    session_with_data.export_dataframe(str(output_path))

    assert os.path.exists(output_path)


def test_an_empty_cohort_still_writes_a_file(session_with_two_patients, tmp_path):
    """`export_to_parquet` returned early and wrote nothing here (#55).

    A scheduled job reading this path would then pick up the *previous*
    run's file and treat stale rows as current. Writing the empty frame
    makes "nothing matched" visible downstream.
    """
    output_path = tmp_path / "empty.csv"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["NO-SUCH-PATIENT"])

    assert df.empty
    assert os.path.exists(output_path)


def test_an_empty_cohort_keeps_its_columns(session_with_two_patients, tmp_path):
    """An empty result must still have a schema.

    Writing the empty frame is only useful if a reader can treat it as
    "no rows". A bare `pd.DataFrame([])` has no columns at all, so the
    obvious downstream `df[df.Modality == "CT"]` raises `AttributeError`
    on an empty export while working on every non-empty one -- a failure
    that shows up only when the filter matched nothing.
    """
    output_path = tmp_path / "empty.parquet"
    df = session_with_two_patients.export_dataframe(
        str(output_path), patient_ids=["NO-SUCH-PATIENT"])

    assert df.empty
    assert list(df["PatientID"]) == []
    assert list(pd.read_parquet(output_path).columns) == list(df.columns)


def test_expand_metadata_still_adds_columns_beyond_the_fixed_set(session_with_data):
    """The fixed column list must not become a whitelist that clips
    expanded attributes back out."""
    df = session_with_data.export_dataframe(expand_metadata=True)

    assert "SliceThickness" in df.columns
    assert "PatientID" in df.columns


def test_the_declared_columns_match_the_rows_actually_built(session_with_data):
    """`COHORT_REPORT_COLUMNS` is only used when there are no rows, so
    nothing else would notice it drifting from the row dict. A column
    added to one and not the other would appear on every populated
    export and vanish on every empty one."""
    from isocenter.session import COHORT_REPORT_COLUMNS

    df = session_with_data.get_cohort_report()

    assert not df.empty, "fixture must produce rows or this pins nothing"
    assert list(df.columns) == COHORT_REPORT_COLUMNS


# --- #164: a suspended generator must not hold the store hostage ---------


def _store_with_instances(db_path, count=3):
    """A populated `SqliteStore`, built without a `DicomSession`.

    These tests suspend a generator mid-iteration and then poke the
    *store*, so they talk to `SqliteStore` directly rather than through a
    session that would keep handles of its own on it.
    """
    from isocenter.persistence import SqliteStore

    store = SqliteStore(db_path)
    p = Patient("P1", "Test Patient")
    st = Study("ST1", "20230101")
    se = Series("SE1", "CT", 101, equipment=None)
    for i in range(count):
        inst = Instance(f"I{i}", "1.2.840.123", i)
        inst.attributes = {"Modality": "CT"}
        se.instances.append(inst)
    st.series.append(se)
    p.studies.append(st)
    store.save_all([p])
    return store


def _probe_on_a_thread(fn, timeout=2.0):
    """Runs `fn` on a daemon thread and reports what happened.

    The failure mode under test is a *hang*, so the call must not happen
    on the test's own thread: a regression would wedge the whole suite
    rather than fail one test. The probe runs elsewhere, the test waits
    with a bounded join, and the caller releases the generator afterwards
    so a blocked probe comes unstuck instead of outliving the test.

    Returns `(thread, box)`. A finished thread is not proof of success --
    one that raised also finishes -- so callers assert on `box["value"]`,
    not just on `is_alive()`.
    """
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # reported to the test, not swallowed
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True, name="deadlock-probe")
    t.start()
    t.join(timeout=timeout)
    return t, box


def test_a_suspended_flattened_instances_generator_releases_the_memory_lock():
    """#164, the mechanism.

    `get_flattened_instances` used to `yield` from inside
    `with self._get_connection()`, and on a `:memory:` store that context
    manager holds `_memory_lock` -- a plain, non-reentrant
    `threading.Lock` -- across its own yield. A generator suspended
    between rows therefore held the store's only lock, and every other
    database call on it blocked forever.

    Streaming *is* partial consumption, so the one usage the method
    advertises was the one that deadlocked.

    This is the deterministic half: no threads, no timeout, just the
    lock's own state while the generator is parked.
    """
    store = _store_with_instances(":memory:")
    gen = None
    try:
        gen = store.get_flattened_instances(page_size=1)
        first = next(gen)

        assert first["sop_instance_uid"] == "I0"
        assert store._memory_lock.locked() is False
    finally:
        # Closed before `stop()`, and unconditionally: a failing assert
        # above means the lock IS held, and `stop()` flushes the audit
        # queue through `_get_connection` with no timeout. Releasing the
        # generator first is what turns a regression into one failed test
        # rather than a wedged suite.
        if gen is not None:
            gen.close()
        store.stop()


def test_a_suspended_flattened_instances_generator_does_not_block_the_store():
    """#164, the behaviour the lock state above causes: any other call on
    the same `:memory:` store while a generator is parked mid-stream.
    """
    store = _store_with_instances(":memory:")
    gen = None
    thread = None
    try:
        gen = store.get_flattened_instances(page_size=1)
        next(gen)

        thread, box = _probe_on_a_thread(store.get_total_instances)

        assert not thread.is_alive(), \
            "another store call blocked on the parked generator"
        assert box.get("error") is None, f"probe raised: {box.get('error')!r}"
        assert box.get("value") == 3
    finally:
        # Release the lock whatever the assertions did, so a blocked
        # probe thread and the store teardown both come unstuck. Without
        # this, a regression here wedges the rest of the suite.
        if gen is not None:
            gen.close()
        if thread is not None:
            thread.join(timeout=2.0)
        store.stop()


def test_a_suspended_flattened_instances_generator_holds_no_read_snapshot(tmp_path):
    """The file-backed path never deadlocked -- `_get_connection` opens a
    fresh connection per call there -- but a parked generator still held
    one open with a stepping `SELECT` on it.

    File stores run in WAL mode (`_init_db`), so that reader does *not*
    block writers. What it blocks is checkpointing: SQLite cannot reset
    the `-wal` file past the oldest live read snapshot, so the WAL grows
    unbounded for as long as the generator stays parked. Paging ends the
    read between pages, which is what this pins -- `wal_checkpoint`
    reports busy=0 rather than busy=1.

    The audit worker reads through `_get_connection` too, so it is
    stopped first: otherwise it could hold a snapshot of its own and this
    would pin the wrong thing.
    """
    db_path = str(tmp_path / "streaming.db")
    store = _store_with_instances(db_path)
    store.stop()  # quiesce the audit worker's own connections

    probe = sqlite3.connect(db_path, timeout=1.0)
    try:
        # The WAL must have something in it or the checkpoint pins
        # nothing: closing the last connection to a WAL database
        # truncates the log, and `PRAGMA wal_checkpoint` on an empty log
        # reports busy=0 whether a reader is parked or not. This write
        # is what makes the assertion below discriminating -- deleting
        # it leaves a test that passes against the bug.
        probe.execute("PRAGMA user_version = 1")
        probe.commit()

        gen = store.get_flattened_instances(page_size=1)
        try:
            # Asserted, not merely called: a walk that yielded nothing
            # holds no snapshot either, so busy==0 below would be true
            # for the wrong reason.
            assert next(gen)["sop_instance_uid"] == "I0"

            busy, _, _ = probe.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

            assert busy == 0, "a parked generator is still pinning the WAL open"
        finally:
            gen.close()
    finally:
        probe.close()


def test_flattened_instances_pages_without_changing_its_rows():
    """Paging bounds the lock hold; it must not touch the result. A page
    size of 1 has to produce exactly what one query did.
    """
    store = _store_with_instances(":memory:")
    try:
        paged = list(store.get_flattened_instances(page_size=1))
        one_shot = list(store.get_flattened_instances(page_size=1000))

        assert paged == one_shot
        assert [r["sop_instance_uid"] for r in paged] == ["I0", "I1", "I2"]
    finally:
        store.stop()


@pytest.mark.parametrize("page_size", [1, 2, 7, 24, 25, 26, 1000])
def test_flattened_instances_walks_every_row_once_at_any_page_size(page_size):
    """The keyset walk must neither lose nor repeat a row, and page sizes
    that do not divide the cohort are where a keyset goes wrong.

    A three-row store cannot show this: at any page size above 1 it is a
    single page, so the resume path is never exercised. Twenty-five rows
    against page sizes either side of the exact divisors is what makes an
    off-by-one on the cursor (`i.id > ?` written as `>=`, or `after_id`
    advanced past the row just yielded) visible as a duplicate or a gap.
    """
    store = _store_with_instances(":memory:", count=25)
    try:
        uids = [r["sop_instance_uid"]
                for r in store.get_flattened_instances(page_size=page_size)]

        assert uids == [f"I{i}" for i in range(25)]
    finally:
        store.stop()


def test_flattened_instances_holds_a_connection_only_for_one_page():
    """`page_size` must actually bound the fetch, not just decorate the
    signature.

    Every other test here passes against two implementations that are not
    this one: "ignore `page_size` and always fetch 500", and the
    one-line fix the changelog disclaims -- `fetchall()` the cohort under
    the lock, then yield it. Both release the lock, both return the right
    rows, and neither streams. What separates them from a paged walk is
    the *number of times a connection is taken*, so that is what this
    counts: six rows at two per page is three full pages plus the empty
    page that ends the walk.
    """
    store = _store_with_instances(":memory:", count=6)
    # The audit worker reaches the database through `_get_connection` too
    # (`log_audit_batch`), so it is stopped before the counter goes in:
    # a batch it drains inside the counting window would add connections
    # this test would blame on the walk, and it would do so only
    # sometimes. The WAL test above stops the store for the same reason,
    # and shows the walk still works afterwards.
    store.stop()
    real = store._get_connection
    calls = []

    def counting_connection(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    store._get_connection = counting_connection
    try:
        for page_size, expected_pages in ((2, 4), (3, 3), (6, 2), (500, 1)):
            calls.clear()
            rows = list(store.get_flattened_instances(page_size=page_size))

            assert [r["sop_instance_uid"] for r in rows] == \
                ["I0", "I1", "I2", "I3", "I4", "I5"]
            assert len(calls) == expected_pages, (
                f"page_size={page_size} took {len(calls)} connections, "
                f"expected {expected_pages}")
    finally:
        store._get_connection = real
        store.stop()


def test_flattened_instances_pages_a_filtered_cohort_correctly():
    """`LIMIT` applies after the `WHERE`, and the keyset resumes from the
    last row *returned*, not the last row scanned. A filter that excludes
    rows in the middle of a page must therefore neither lose the rows
    after it nor end the walk early.
    """
    store = _store_with_instances(":memory:", count=6)
    try:
        wanted = ["I1", "I3", "I5"]
        # Two per page over three matches, deliberately: at `page_size=1`
        # every page is either full or empty, so the walk never resumes
        # from a *partial* page and the interesting path is not taken.
        # Three matches at two per page gives a full page, then a short
        # one, which is where a keyset that resumed from the last row
        # scanned rather than the last row returned goes wrong.
        rows = list(store.get_flattened_instances(
            instance_uids=wanted, page_size=2))

        assert [r["sop_instance_uid"] for r in rows] == wanted
    finally:
        store.stop()


def test_flattened_instances_pages_a_patient_filter_across_a_boundary():
    """The keyset condition is appended *after* the caller's filters, so
    its bound value has to follow theirs in the parameter list.

    A mismatch there binds a patient ID where the cursor belongs and
    returns wrong rows without raising -- and it cannot show up on a
    cohort that fits in one page, because page one binds `after_id = 0`
    and any mis-ordering still happens to look plausible. This needs a
    filter that excludes a patient *and* a page boundary inside the
    survivors, so the second page's parameters are actually exercised.
    """
    from isocenter.persistence import SqliteStore

    store = SqliteStore(":memory:")
    graph = []
    for pid in ("P0", "P1", "P2"):
        p = Patient(pid, f"Name {pid}")
        st = Study(f"ST-{pid}", "20230101")
        se = Series(f"SE-{pid}", "CT", 101, equipment=None)
        for i in range(4):
            inst = Instance(f"{pid}-I{i}", "1.2.840.123", i)
            inst.attributes = {"Modality": "CT"}
            se.instances.append(inst)
        st.series.append(se)
        p.studies.append(st)
        graph.append(p)
    store.save_all(graph)

    try:
        # P0 and P2 are not adjacent, so the survivors straddle a gap in
        # `instances.id` as well as a page boundary.
        wanted = [f"P0-I{i}" for i in range(4)] + \
                 [f"P2-I{i}" for i in range(4)]
        for page_size in (1, 2, 3, 5, 7, 8, 9):
            rows = [r["sop_instance_uid"] for r in
                    store.get_flattened_instances(
                        patient_ids=["P0", "P2"], page_size=page_size)]

            assert rows == wanted, f"page_size={page_size} gave {rows}"
    finally:
        store.stop()


def test_flattened_instances_survives_a_write_landing_between_pages():
    """The docstring promises iteration is *not* one snapshot -- each page
    is its own query -- and that is a contract, not an accident. It is the
    price of releasing the lock, and it has to be safe as well as
    documented.

    The cursor is a plain integer captured from the last row yielded, not
    a live cursor handle, so deleting the row it sits on must not derail
    the walk: `i.id > ?` compares against a value that no longer needs to
    exist. Rows appearing above the cursor are picked up; rows appearing
    below it are not. Pinning this stops a future "optimisation" back to
    one long-lived cursor -- which is #164 -- from looking like a no-op.
    """
    store = _store_with_instances(":memory:", count=6)
    gen = None
    writer = None
    try:
        gen = store.get_flattened_instances(page_size=2)
        seen = [next(gen)["sop_instance_uid"], next(gen)["sop_instance_uid"]]
        assert seen == ["I0", "I1"]

        def mutate():
            with store._get_connection() as conn:
                cursor_id = conn.execute(
                    "SELECT id FROM instances "
                    "WHERE sop_instance_uid = 'I1'").fetchone()[0]
                series_id = conn.execute(
                    "SELECT id FROM series").fetchone()[0]
                # `BELOW` takes the id `I0` just vacated, so it lands
                # under the cursor but still above the walk's `after_id`
                # floor of 0. Skipping it therefore says something about
                # the cursor. A negative rowid would be excluded by the
                # floor whatever the cursor did, and would pin a
                # different, accidental property.
                conn.execute(
                    "DELETE FROM instances WHERE sop_instance_uid = 'I0'")
                conn.execute(
                    "INSERT INTO instances "
                    "(id, series_id_fk, sop_instance_uid) "
                    "VALUES (1, ?, 'BELOW')", (series_id,))
                conn.execute(
                    "INSERT INTO instances (series_id_fk, sop_instance_uid) "
                    "VALUES (?, 'ABOVE')", (series_id,))
                conn.execute(
                    "DELETE FROM instances WHERE id = ?", (cursor_id,))
            return "written"

        # On a thread with a bounded join, not inline: writing through
        # `_get_connection` while a generator is parked is precisely what
        # #164 deadlocked on, so doing it on this thread would wedge the
        # suite against a regression instead of failing one test.
        writer, box = _probe_on_a_thread(mutate)

        assert box.get("error") is None, f"writer raised: {box.get('error')!r}"
        assert box.get("value") == "written", \
            "a mid-walk write blocked on the parked generator (#164)"

        rest = [r["sop_instance_uid"] for r in gen]

        assert rest == ["I2", "I3", "I4", "I5", "ABOVE"], (
            "the walk must resume from the deleted cursor's id without "
            "repeating or skipping a row, and must see the later insert")
        assert "BELOW" not in rest
    finally:
        if gen is not None:
            gen.close()
        if writer is not None:
            writer.join(timeout=2.0)
        store.stop()


def test_flattened_instances_row_shape_carries_no_paging_key():
    """The keyset walks `instances.id`, which means selecting a column the
    method never published. It must not leak into the row: callers read
    these dicts by key, and #55's changelog names the exact set.
    """
    store = _store_with_instances(":memory:")
    gen = None
    try:
        # Bound rather than consumed inline, so the `finally` can close
        # it: an unbound generator is only released by refcounting, and
        # if this ever parks with the lock held that timing decides
        # whether `store.stop()` returns.
        gen = store.get_flattened_instances(page_size=1)
        row = next(gen)

        assert "id" not in row
        assert sorted(row) == sorted([
            "patient_id", "patient_name",
            "study_instance_uid", "study_date",
            "series_instance_uid", "modality", "series_number",
            "manufacturer", "model_name", "device_serial_number",
            "sop_instance_uid", "sop_class_uid", "instance_number",
            "file_path", "pixel_offset", "pixel_length", "compress_alg",
            "attributes_json",
        ])
    finally:
        if gen is not None:
            gen.close()
        store.stop()


def test_a_page_size_below_one_is_rejected():
    """`LIMIT 0` returns an empty page, and an empty page is how the walk
    decides it has reached the end -- so `page_size=0` would silently
    report an empty store rather than fail. The check runs at call time,
    not on first `next()`, which is why the method is a plain function
    wrapping the generator.
    """
    store = _store_with_instances(":memory:")
    try:
        with pytest.raises(ValueError):
            store.get_flattened_instances(page_size=0)
    finally:
        store.stop()


@pytest.mark.parametrize("page_size, before", [
    (2.5, "sqlite3.IntegrityError: datatype mismatch, on the first next()"),
    (None, "TypeError from the < comparison"),
    ("5", "TypeError from the < comparison"),
    (1e9, "no error at all until sqlite refused the float"),
])
def test_a_non_integer_page_size_is_rejected_as_a_bad_argument(
        page_size, before):
    """A bad argument reads as a bad argument, and does so at the call (#202).

    The guard was `if page_size < 1`, which admits anything that compares
    to an int and lets everything else through. `None` and `"5"` raised
    `TypeError: '<' not supported between instances of ...` -- from the
    guard, so at least at the call. `2.5` was worse: it *passed* the
    guard, travelled into `LIMIT ?`, and raised
    `sqlite3.IntegrityError: datatype mismatch` on the first `next()`, a
    database error out of a public method for what is a caller's typo,
    and one page of rows later than the call that caused it.

    The float case is why this test asserts at the call rather than
    around the iteration: the method is a plain function wrapping a
    generator precisely so its checks fire when it is called, and a
    check that only fires on iteration gives that up.
    """
    store = _store_with_instances(":memory:")
    try:
        with pytest.raises(ValueError):
            store.get_flattened_instances(page_size=page_size)
    finally:
        store.stop()


def test_a_boolean_page_size_is_refused_before_it_is_read_as_an_int():
    """`bool` is refused before `int`, and the ordering is the mechanism.

    `isinstance(True, int)` is `True` and `True < 1` is `False`, so
    `page_size=True` sailed through the old guard *and* would sail
    through a naive `isinstance(page_size, int)` one -- and then page the
    walk one row at a time, which is a correctness-preserving disaster
    rather than an error. This repo refuses `bool` before `int` twice
    already for the same subclass reason, in `_fallback_encoding`'s
    `bool` arm and in `_value_fits_vr`'s ordering (#283), both with
    comments saying the ordering is what does the work.
    """
    store = _store_with_instances(":memory:")
    try:
        with pytest.raises(ValueError):
            store.get_flattened_instances(page_size=True)
    finally:
        store.stop()


def test_the_page_size_default_is_not_a_public_knob():
    """What #202 held still: the constant was renamed, not made real.

    `FLATTENED_PAGE_SIZE` was a public class attribute that
    `get_flattened_instances` took its default from -- and a default
    argument is evaluated once, when the `def` runs, so the `500` was
    baked into `__defaults__` at import and the attribute was never read
    again. Rebinding it did nothing: measured, a default-arg walk over
    six rows took one connection after setting the attribute to `2`,
    where honouring it would have taken three. That is two spellings for
    one behaviour with only one of them working, which is the failure
    *one spelling per behaviour* exists to prevent, and it was silent --
    a user bounding memory on a large export got 500-row pages and no
    error.

    The name is private now, so it stops reading as configuration to a
    reader of the class, an autocompleting editor and `dir()`;
    `page_size=` is the one spelling for the behaviour. It is *not*
    about the rendered API reference: measured on the published page,
    no class attribute renders there at all -- 33 member anchors, all
    methods and dunders -- and after the rename the private name still
    appears in the shown source and as the signature's default, which
    is correct. Pre-1.0, the public name is **deleted rather than
    aliased**, so this asserts its absence: re-adding
    `FLATTENED_PAGE_SIZE = _FLATTENED_PAGE_SIZE` for compatibility would
    restore exactly the two spellings that caused this.
    """
    from isocenter.persistence import SqliteStore

    assert not hasattr(SqliteStore, "FLATTENED_PAGE_SIZE"), (
        "the public name is back; it advertises a knob that a default "
        "argument evaluated at import cannot provide (#202)")
    assert SqliteStore._FLATTENED_PAGE_SIZE == 500
