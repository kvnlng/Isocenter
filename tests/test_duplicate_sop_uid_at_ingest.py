"""A second file carrying an SOP Instance UID the session holds is declined (#431).

Measured on 4d34c64 with a valid `generate_uid()` UID and three files
sharing it: `ingest()` returned `ingested=3` and no row, the live graph
held three instances, and `save()` wrote ONE `instances` row -- the table
is keyed on the UID and its upsert is `ON CONFLICT(sop_instance_uid) DO
UPDATE`, so the last instance linked won, and the series the others sat
in was stored with no instances at all. A reload held one instance
where the session had reported three, and nothing said so.

The ruling: keep the first, decline the rest, each with a `WARNING` row
naming the UID and both paths. The same holds across calls and across
sessions, because both reduce to "the graph already holds this UID".

Which file is kept is pinned: the one whose path sorts first among the
files new to the call (#450), and an instance the session already holds
always wins. Before #450 it was the first in the filesystem's listing
order, which APFS does not sort; the tests that pin it control
`os.walk` rather than trusting the volume, because a filesystem that
happens to list sorted would let an unsorted ingest pass. The other
tests still read the holder from the row rather than assuming it, and
check that the instance kept holds the pixels of the file the row names
-- a property of the #431 fix, whichever file is first.

Each file's pixels are a constant array of a distinct value, so the
kept instance's pixels say which file it came from, and so every file
compresses to the same number of sidecar bytes (U5 compares sizes).
Paths are `src/one/x.dcm` and `src/two/y.dcm`: neither is a substring of
the other, so "the detail names both paths" cannot be satisfied by one.
"""
import os
import sqlite3

import numpy as np
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter.session import DicomSession

SC = "1.2.840.10008.5.1.4.1.1.7"
X_VALUE, Y_VALUE = 7, 9


def _write(path, sop_uid, study_uid, series_uid, value):
    """A native 4x4 8-bit file whose every pixel is `value`."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "P431", "DOE^JANE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = study_uid, series_uid
    ds.SOPInstanceUID, ds.SOPClassUID = sop_uid, SC
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20240115"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.full((4, 4), value, dtype=np.uint8).tobytes()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds.save_as(path, enforce_file_format=True)
    return os.path.abspath(path)


def _pair(root, same_series=True, uid=None):
    """Two files, one UID: `src/one/x.dcm` (7s) and `src/two/y.dcm` (9s)."""
    uid = uid or generate_uid()
    study = generate_uid()
    series_x = generate_uid()
    series_y = series_x if same_series else generate_uid()
    x = _write(os.path.join(root, "one", "x.dcm"), uid, study, series_x,
               X_VALUE)
    y = _write(os.path.join(root, "two", "y.dcm"), uid, study, series_y,
               Y_VALUE)
    return uid, {x: X_VALUE, y: Y_VALUE}


def _instances(session):
    return [inst for p in session.store.patients for st in p.studies
            for se in st.series for inst in se.instances]


def _series(session):
    return [se for p in session.store.patients for st in p.studies
            for se in st.series]


def _warning_rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details, loss_scope FROM audit_log "
            "WHERE action_type='WARNING'").fetchall()


def _declined_and_holder(detail, paths):
    """Which path the row declines and which it names as the holder.

    Read from the row itself, so the test never assumes an order.
    """
    declined = [p for p in paths if detail.startswith(f"Not importing {p}:")]
    assert len(declined) == 1, detail
    holder = [p for p in paths if p != declined[0]]
    assert len(holder) == 1
    assert f"held by the instance ingested from {holder[0]}" in detail, detail
    return declined[0], holder[0]


def _assert_the_holder_is_what_is_held(session, uid, holder, values):
    insts = _instances(session)
    assert len(insts) == 1, [i.sop_instance_uid for i in insts]
    (inst,) = insts
    assert inst.sop_instance_uid == uid
    assert inst.source_path == holder
    # From the store, not a resident array.
    inst.unload_pixel_data()
    got = inst.get_pixel_data()
    assert got.tolist() == np.full((4, 4), values[holder],
                                   dtype=np.uint8).tolist()


# ---------------------------------------------------------------------------
# U1 -- same series, one call
# ---------------------------------------------------------------------------

def test_a_duplicate_in_one_series_is_declined_with_a_row_naming_both_paths(
        tmp_path):
    """U1, the issue's case: `ingested=2` became one row and no word."""
    uid, values = _pair(str(tmp_path / "src"))
    db = str(tmp_path / "s.db")

    session = DicomSession(persistence_file=db)
    try:
        summary = session.ingest(str(tmp_path / "src"))
        assert summary.ingested == 1
        assert summary.declined == 1
        assert summary.failures == []
        assert summary.skipped == 0

        rows = _warning_rows(session)
        assert len(rows) == 1, rows
        entity, detail, scope = rows[0]
        assert entity == uid
        assert scope is None
        assert uid in detail
        _declined, holder = _declined_and_holder(detail, list(values))
        _assert_the_holder_is_what_is_held(session, uid, holder, values)
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        _assert_the_holder_is_what_is_held(reopened, uid, holder, values)
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# U2 -- different series, one call
# ---------------------------------------------------------------------------

def test_a_duplicate_in_another_series_leaves_no_empty_series_behind(tmp_path):
    """U2: the first series used to be stored with no instances."""
    uid, values = _pair(str(tmp_path / "src"), same_series=False)
    db = str(tmp_path / "s.db")

    session = DicomSession(persistence_file=db)
    try:
        summary = session.ingest(str(tmp_path / "src"))
        assert (summary.ingested, summary.declined) == (1, 1)
        (row,) = _warning_rows(session)
        _declined, holder = _declined_and_holder(row[1], list(values))
        assert len(_series(session)) == 1
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        series = _series(reopened)
        assert len(series) == 1, [s.series_instance_uid for s in series]
        assert [len(s.instances) for s in series] == [1]
        _assert_the_holder_is_what_is_held(reopened, uid, holder, values)
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# U3, U4 -- across calls, and across sessions
# ---------------------------------------------------------------------------

def test_a_second_call_duplicating_an_instance_in_the_graph_is_declined(
        tmp_path):
    """U3: the UID is in the graph from an earlier call of this session."""
    uid = generate_uid()
    study, series = generate_uid(), generate_uid()
    x = _write(str(tmp_path / "first" / "one" / "x.dcm"), uid, study, series,
               X_VALUE)
    y = _write(str(tmp_path / "second" / "two" / "y.dcm"), uid, study,
               series, Y_VALUE)

    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        first = session.ingest(str(tmp_path / "first"))
        assert (first.ingested, first.declined) == (1, 0)
        second = session.ingest(str(tmp_path / "second"))
        assert (second.ingested, second.declined) == (0, 1)
        assert second.failures == []

        (row,) = _warning_rows(session)
        assert row[1].startswith(f"Not importing {y}:"), row[1]
        assert f"held by the instance ingested from {x}" in row[1]
        _assert_the_holder_is_what_is_held(session, uid, x,
                                           {x: X_VALUE, y: Y_VALUE})
    finally:
        session.close()


def test_a_new_session_declines_a_duplicate_of_a_stored_instance(tmp_path):
    """U4: the UID reached the graph from the store, not from this call."""
    uid = generate_uid()
    study, series = generate_uid(), generate_uid()
    x = _write(str(tmp_path / "first" / "one" / "x.dcm"), uid, study, series,
               X_VALUE)
    y = _write(str(tmp_path / "second" / "two" / "y.dcm"), uid, study,
               series, Y_VALUE)
    db = str(tmp_path / "s.db")

    session = DicomSession(persistence_file=db)
    try:
        assert session.ingest(str(tmp_path / "first")).ingested == 1
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        summary = reopened.ingest(str(tmp_path / "second"))
        assert (summary.ingested, summary.declined) == (0, 1)
        (row,) = _warning_rows(reopened)
        assert row[1].startswith(f"Not importing {y}:"), row[1]
        # The holder is a hydrated instance: its path comes back from the
        # store's `source_path` column, and the row must name it rather
        # than say it has none.
        assert f"held by the instance ingested from {x}" in row[1], row[1]
        _assert_the_holder_is_what_is_held(reopened, uid, x,
                                           {x: X_VALUE, y: Y_VALUE})
    finally:
        reopened.close()


# ---------------------------------------------------------------------------
# U5 -- a declined file writes nothing to the sidecar
# ---------------------------------------------------------------------------

def _sidecar_bytes(tmp_path, name, folder):
    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    try:
        summary = session.ingest(folder)
        return summary, os.path.getsize(session.store_backend.sidecar.filepath)
    finally:
        session.close()


def test_a_declined_file_strands_no_frame_in_the_sidecar(tmp_path):
    """U5, the only guard on the decline's placement above sidecar site 1.

    `persist_pixel_data` does not de-duplicate, so a check placed after
    the write would decline the file and leave its frame behind (#235).
    Both files hold constant arrays of one size, so either one alone
    compresses to the same number of bytes.
    """
    _uid, values = _pair(str(tmp_path / "pair"))
    alone = tmp_path / "alone"
    alone.mkdir()
    x = next(p for p, v in values.items() if v == X_VALUE)
    with open(x, "rb") as src, open(alone / "x.dcm", "wb") as dst:
        dst.write(src.read())

    one, size_one = _sidecar_bytes(tmp_path, "alone", str(alone))
    both, size_both = _sidecar_bytes(tmp_path, "pair", str(tmp_path / "pair"))
    assert (one.ingested, one.declined) == (1, 0)
    assert (both.ingested, both.declined) == (1, 1)
    assert size_one > 0  # or 0 == 0 would pass with nothing written
    assert size_both == size_one


# ---------------------------------------------------------------------------
# U6 -- ingesting the same folder again
# ---------------------------------------------------------------------------

def test_re_ingesting_the_folder_declines_the_duplicate_again(tmp_path):
    """U6: a declined file is not recorded as imported, by choice.

    The kept file is skipped as already imported; the declined one is
    read again and declined again, with a second row. Recording the
    declined path instead would make a re-run silent about it.
    """
    _uid, _values = _pair(str(tmp_path / "src"))
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        first = session.ingest(str(tmp_path / "src"))
        assert (first.ingested, first.declined, first.skipped) == (1, 1, 0)
        second = session.ingest(str(tmp_path / "src"))
        assert (second.ingested, second.declined, second.skipped) == (0, 1, 1)
        assert len(_warning_rows(session)) == 2
        assert len(_instances(session)) == 1
    finally:
        session.close()


# ---------------------------------------------------------------------------
# U7 -- the grade
# ---------------------------------------------------------------------------

def test_a_declined_duplicate_is_in_the_report_and_bars_pass(tmp_path):
    """U7: a WARNING row is an exception, and exceptions bar PASS."""
    uid, _values = _pair(str(tmp_path / "src"))
    report = tmp_path / "report.md"
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path / "src"))
        # So the audit summary is non-empty and the baseline grade would
        # be PASS: an empty summary grades REVIEW_REQUIRED on its own.
        session.anonymize()
        session.generate_report(str(report))
    finally:
        session.close()
    content = report.read_text(encoding="utf-8")
    assert "| **Validation Status** | **REVIEW_REQUIRED** |" in content
    exceptions = content.split("## 4. Exceptions & Errors", 1)[1]
    exceptions = exceptions.split("## 5.", 1)[0]
    assert "| WARNING |" in exceptions, exceptions
    assert f"SOP Instance UID {uid} is already held" in exceptions


def test_a_clean_pair_of_distinct_uids_declines_nothing(tmp_path):
    """No false positive: two files, two UIDs, both ingested, no row."""
    study, series = generate_uid(), generate_uid()
    _write(str(tmp_path / "src" / "one" / "x.dcm"), generate_uid(), study,
           series, X_VALUE)
    _write(str(tmp_path / "src" / "two" / "y.dcm"), generate_uid(), study,
           series, Y_VALUE)
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        summary = session.ingest(str(tmp_path / "src"))
        assert (summary.ingested, summary.declined) == (2, 0)
        assert _warning_rows(session) == []
    finally:
        session.close()


# ---------------------------------------------------------------------------
# U8 -- a file that failed holds nothing
# ---------------------------------------------------------------------------

def test_a_file_whose_linkage_failed_does_not_hold_the_uid(tmp_path,
                                                           monkeypatch):
    """U8: the UID is held only once the instance is linked.

    The first frame write raises, whichever file reaches it first, so
    that file is a failure. The other must then be ingested, not
    declined against an instance the session never kept. Moving
    `held[uid] = inst` above sidecar site 1 declines it: ingested=0,
    declined=1, and no instance at all. That mutant survived the full
    suite on both interpreters until this test (review of #451).
    """
    uid, values = _pair(str(tmp_path / "src"))
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        sidecar = session.store_backend.sidecar
        real_write = sidecar.write_frame
        calls = []

        def first_write_fails(*args, **kwargs):
            calls.append(None)
            if len(calls) == 1:
                raise OSError("injected: the first frame write fails")
            return real_write(*args, **kwargs)

        monkeypatch.setattr(sidecar, "write_frame", first_write_fails)
        summary = session.ingest(str(tmp_path / "src"))
        assert (summary.ingested, summary.declined) == (1, 0)
        assert len(summary.failures) == 1, summary.failures
        ((failed_path, reason),) = summary.failures
        assert "injected" in reason, reason
        assert failed_path in values, (failed_path, list(values))
        assert _warning_rows(session) == []
        kept = next(p for p in values if p != failed_path)
        _assert_the_holder_is_what_is_held(session, uid, kept, values)
    finally:
        session.close()


def test_a_holder_with_no_source_file_is_called_that(tmp_path):
    """The row does not say "ingested from an instance with no source file".

    A graph built or edited by hand can hold an instance with neither
    path. The row still declines the file, and names the holder by the
    only thing known about it.
    """
    uid = generate_uid()
    study, series = generate_uid(), generate_uid()
    _write(str(tmp_path / "first" / "one" / "x.dcm"), uid, study, series,
           X_VALUE)
    y = _write(str(tmp_path / "second" / "two" / "y.dcm"), uid, study,
               series, Y_VALUE)
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        session.ingest(str(tmp_path / "first"))
        (holder,) = _instances(session)
        holder.source_path = None
        holder.file_path = None
        summary = session.ingest(str(tmp_path / "second"))
        assert (summary.ingested, summary.declined) == (0, 1)
        (row,) = _warning_rows(session)
        detail = row[1]
        assert detail.startswith(f"Not importing {y}:"), detail
        assert ("is already held by an instance in this session that has "
                "no source file.") in detail, detail
        assert "ingested from" not in detail, detail
    finally:
        session.close()


def test_the_printout_counts_declined_files_among_the_new_files(tmp_path,
                                                                capsys):
    """"ingested X of N new files" counts every new file it read.

    One duplicate pair and one file that is not DICOM: one ingested, one
    declined, one rejected, three new files. The total used to leave the
    declined file out and read "1 of 2".
    """
    _pair(str(tmp_path / "src"))
    (tmp_path / "src" / "three").mkdir()
    (tmp_path / "src" / "three" / "z.dcm").write_bytes(b"not a DICOM file")
    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        capsys.readouterr()
        summary = session.ingest(str(tmp_path / "src"))
        out = capsys.readouterr().out
        assert (summary.ingested, summary.declined, summary.failed) == \
            (1, 1, 1), summary
        assert "ingested 1 of 3 new files" in out, out
    finally:
        session.close()


# ---------------------------------------------------------------------------
# #450 -- which file is kept does not depend on the listing order
# ---------------------------------------------------------------------------

_REAL_WALK = os.walk


def _reverse_sorted_walk(top, *args, **kwargs):
    """`os.walk`, listing directories and files in reverse name order.

    The listing order is the thing under test, so the test sets it rather
    than trusting the filesystem: APFS measured neither sorted nor
    creation order, HFS+ sorted, and ext4 hashes -- any of which could
    list these fixtures sorted and let an unsorted ingest pass. `dirs` is
    reversed *in place*, which is what top-down `os.walk` reads when it
    descends, so the recursion order follows too.
    """
    for root, dirs, files in _REAL_WALK(top, *args, **kwargs):
        dirs.sort(reverse=True)
        yield root, dirs, sorted(files, reverse=True)


def _same_directory(root):
    """Two files, one UID, side by side: `src/a.dcm` (7s), `src/b.dcm` (9s)."""
    uid, study, series = generate_uid(), generate_uid(), generate_uid()
    first = _write(os.path.join(root, "a.dcm"), uid, study, series, X_VALUE)
    second = _write(os.path.join(root, "b.dcm"), uid, study, series, Y_VALUE)
    return uid, first, second, {first: X_VALUE, second: Y_VALUE}


def _across_directories(root):
    """`src/one/x.dcm` (7s) and `src/two/y.dcm` (9s), from `_pair`."""
    uid, values = _pair(root)
    first, second = sorted(values)
    return uid, first, second, values


def _full_path_decides(root):
    """`src/one/y.dcm` (7s) and `src/two/x.dcm` (9s).

    The whole path sorts `one/y` first; the basenames alone sort `x`
    first. A sort on the basename keeps the other file.
    """
    uid, study, series = generate_uid(), generate_uid(), generate_uid()
    first = _write(os.path.join(root, "one", "y.dcm"), uid, study, series,
                   X_VALUE)
    second = _write(os.path.join(root, "two", "x.dcm"), uid, study, series,
                    Y_VALUE)
    return uid, first, second, {first: X_VALUE, second: Y_VALUE}


def _case_decides(root):
    """`src/B.dcm` (7s) and `src/a.dcm` (9s), side by side.

    By code point `B` (0x42) sorts before `a` (0x61); case-folded, `a`
    sorts first. A case-insensitive sort keeps the other file.

    Not a pair that differs *only* in case, such as `A.dcm` and `a.dcm`:
    APFS is case-insensitive by default, so that pair cannot exist on
    this volume at all. Moving the two into separate directories does not
    help either, because the directory component then decides the order
    before case is ever compared. This pair can coexist on any volume, and
    the two orders disagree about it.
    """
    uid, study, series = generate_uid(), generate_uid(), generate_uid()
    first = _write(os.path.join(root, "B.dcm"), uid, study, series, X_VALUE)
    second = _write(os.path.join(root, "a.dcm"), uid, study, series, Y_VALUE)
    return uid, first, second, {first: X_VALUE, second: Y_VALUE}


@pytest.mark.parametrize("layout", [_same_directory, _across_directories,
                                    _full_path_decides, _case_decides],
                         ids=["same-directory", "across-directories",
                              "full-path-not-basename",
                              "code-point-not-case-folded"])
def test_the_file_kept_is_the_one_whose_path_sorts_first_whatever_the_listing_order(
        tmp_path, monkeypatch, layout):
    """The file kept is the one whose path sorts first, as walked (#450).

    Before #450 `import_files` dispatched in `os.walk` order, and the
    shared executor yields in submission order, so the file kept was the
    first the *filesystem* listed: `f001.dcm` over `f000.dcm` on APFS,
    `f000.dcm` on HFS+, measured on one folder. The listing is now sorted
    before dispatch, so the path that sorts first is linked first and
    kept. "Sorts first" means the whole path string as `os.walk` reports
    it, compared by code point, with no key.

    Killing mutations:
    - the sort deleted: the reversed listing keeps the second file;
    - a basename key (`key=os.path.basename`): `full-path-not-basename`
      keeps `two/x.dcm`;
    - a case-folded key (`key=str.lower`): `code-point-not-case-folded`
      keeps `a.dcm`.
    """
    uid, first, second, values = layout(str(tmp_path / "src"))
    assert sorted((first, second)) == [first, second]
    monkeypatch.setattr(os, "walk", _reverse_sorted_walk)

    session = DicomSession(persistence_file=str(tmp_path / "s.db"))
    try:
        summary = session.ingest(str(tmp_path / "src"))
        assert (summary.ingested, summary.declined) == (1, 1), summary

        rows = _warning_rows(session)
        assert len(rows) == 1, rows
        declined, holder = _declined_and_holder(rows[0][1], list(values))
        assert (holder, declined) == (first, second), (
            f"kept {holder}, declined {declined}: the path that sorts first "
            f"must be the one kept")
        _assert_the_holder_is_what_is_held(session, uid, first, values)
    finally:
        session.close()


def test_import_files_hands_run_parallel_sorted_files_and_asks_for_order(
        tmp_path, monkeypatch):
    """The dispatch itself: sorted items, `ordered=True`, one strategy (#450, #393).

    `ordered=True` matters only on the recycling pool, which a direct
    `import_files(executor=None)` reaches under
    `ISOCENTER_MAX_TASKS_PER_CHILD`; the session's shared executor is
    ordered already. The strategy is resolved in `import_files` and handed
    in, because that is where #393's warning reads it. Killing mutations:
    the sort deleted or moved after the dispatch; `ordered=True` dropped.
    """
    from isocenter.parallel import _Strategy

    for name in ("c.dcm", "a.dcm", "b.dcm"):
        (tmp_path / "src").mkdir(exist_ok=True)
        (tmp_path / "src" / name).write_bytes(b"never read")
    monkeypatch.setattr(os, "walk", _reverse_sorted_walk)

    recorded = {}

    def spy(func, items, **kwargs):
        recorded.update(kwargs)
        recorded["items"] = list(items)
        return []

    monkeypatch.setattr("isocenter.io_handlers.run_parallel", spy)

    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "src"))

    items = recorded["items"]
    assert len(items) == 3, items
    assert items == sorted(items), items
    assert recorded.get("ordered") is True, recorded
    assert isinstance(recorded.get("strategy"), _Strategy), recorded
