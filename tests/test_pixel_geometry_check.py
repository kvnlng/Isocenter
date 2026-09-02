"""A store whose geometry #186 already corrupted must not grade PASS (#214).

#186's defect wrote its guessed geometry into SQLite: `get_pixel_data()`
ended in `mark_modified()`, so the next `save()` persisted
`SamplesPerPixel=3, PhotometricInterpretation=RGB, Rows=3, Columns=8`
for a 3-frame 8x3 MONOCHROME2 instance. The fix stopped the write; it
could not undo the ones already made, and nothing detected them: the
corrupted descriptors made the loader fall through to its 1-D return,
the export resolver honestly reported a rank-1 array as `rows=1,
cols=72`, the file was written, `ExportSummary.failed` was 0, and the
report graded PASS. Every step behaved correctly on input that was
already wrong.

Repair needs a source of truth the store does not have (the sidecar's
bytes are shape-free), so this is a DETECTOR, deliberately: a
best-effort migration that silently half-works is worse. The check is
arithmetic -- Rows x Columns x SamplesPerPixel x NumberOfFrames x
bytes-per-sample against the stored frame length -- and runs at store
open plus report time. The remedy lives in the warning: re-ingest from
source, or `export(verify_readback=True)` (#209) to catch what the
damaged instance produces at the far end.
"""
import hashlib
import json
import logging
import sqlite3

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore


def _store_with_frame(db_path, attrs, raw_len, compress_alg='raw'):
    """A store holding one sidecar-backed instance with the given
    descriptors and a frame of `raw_len` bytes.

    Built by hand because the current code cannot produce the damaged
    state: the #186 write is fixed, and ingest stores zlib frames. The
    hand-built store stands in for one written by the defective release.
    """
    inst = Instance("1.2.3.214", "1.2.840.10008.5.1.4.1.1.7", 1)
    for tag, value in attrs.items():
        inst.set_attr(tag, value)

    patient = Patient("GEO1", "Geometry^Case")
    study = Study("GEO1.STUDY", "20230101")
    series = Series("GEO1.SERIES", "OT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    store = SqliteStore(db_path)
    store.save_all([patient])

    raw = bytes(raw_len)
    payload_offset, length = store.sidecar.write_frame(raw, compress_alg)
    store.stop()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE instances SET pixel_offset=?, pixel_length=?, "
            "compress_alg=?, pixel_hash=? WHERE sop_instance_uid=?",
            (payload_offset, length, compress_alg,
             hashlib.sha256(raw).hexdigest(), "1.2.3.214"))
    return "1.2.3.214"


#: The state #186 persisted for a 3-frame 8x3 uint16 MONOCHROME2 source
#: (spec section 1.1's second row): 144 real bytes under descriptors
#: that imply 3*8*3*3*2 = 432.
_CORRUPTED = {
    "0028,0010": 3,      # Rows (was 8)
    "0028,0011": 8,      # Columns (was 3)
    "0028,0002": 3,      # SamplesPerPixel (was 1)
    "0028,0004": "RGB",  # PhotometricInterpretation (was MONOCHROME2)
    "0028,0008": "3",    # NumberOfFrames -- str, as pydicom IS arrives
    "0028,0100": 16,     # BitsAllocated
}

_HEALTHY = {
    "0028,0010": 8,
    "0028,0011": 3,
    "0028,0002": 1,
    "0028,0004": "MONOCHROME2",
    "0028,0008": "3",
    "0028,0100": 16,
}

_RAW_LEN = 3 * 8 * 3 * 2  # 144 bytes: 3 frames of 8x3 uint16


def test_corrupted_descriptors_are_detected(tmp_path):
    db = str(tmp_path / "damaged.db")
    uid = _store_with_frame(db, _CORRUPTED, _RAW_LEN)

    flagged = SqliteStore(db).check_pixel_geometry()

    assert len(flagged) == 1, flagged
    assert flagged[0][0] == uid
    # The details carry both sides of the arithmetic, so the reader can
    # see the disagreement rather than being told to trust it.
    assert "432" in flagged[0][2] and "144" in flagged[0][2], flagged


def test_healthy_descriptors_are_not_flagged(tmp_path):
    db = str(tmp_path / "healthy.db")
    _store_with_frame(db, _HEALTHY, _RAW_LEN)

    assert SqliteStore(db).check_pixel_geometry() == []


def test_a_zlib_frame_is_outside_the_checks_scope(tmp_path):
    """The documented boundary, pinned: the arithmetic binds raw frames only.

    A zlib frame's stored length is post-compression, so equality
    against the descriptor product holds for no store, damaged or
    healthy. Damage behind a zlib frame is caught where the bytes are
    decoded: `export(verify_readback=True)` (#209).
    """
    db = str(tmp_path / "zlib.db")
    _store_with_frame(db, _CORRUPTED, _RAW_LEN, compress_alg='zlib')

    assert SqliteStore(db).check_pixel_geometry() == []


def test_opening_a_damaged_store_warns_naming_the_instance(tmp_path, caplog):
    from isocenter.session import DicomSession

    db = str(tmp_path / "damaged.db")
    uid = _store_with_frame(db, _CORRUPTED, _RAW_LEN)

    with caplog.at_level(logging.WARNING):
        session = DicomSession(persistence_file=db)
        session.close()

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    matching = [m for m in warnings if uid in m]
    assert matching, warnings
    # The remedy rides the warning: no best-effort repair exists, so
    # the message must say what to do instead.
    assert any("re-ingest" in m.lower() for m in matching), matching
    assert any("verify_readback" in m for m in matching), matching


def test_a_damaged_store_grades_review_required(tmp_path):
    """The condition must reach the compliance report, not only the log.

    Rides the COMPLIANCE_CHECK channel `check_unsafe_attributes` already
    feeds into Exceptions & Errors -- the same claim, one attribute
    over: the store holds an instance whose export cannot be trusted.
    """
    from isocenter.session import DicomSession

    db = str(tmp_path / "damaged.db")
    uid = _store_with_frame(db, _CORRUPTED, _RAW_LEN)

    report = str(tmp_path / "report.md")
    session = DicomSession(persistence_file=db)
    try:
        session.store_backend.log_audit(
            "ANONYMIZE_METADATA", "1.2.3", "baseline row")
        session.generate_report(report)
    finally:
        session.close()

    with open(report, "r") as f:
        content = f.read()
    assert "**REVIEW_REQUIRED**" in content, content
    assert "COMPLIANCE_CHECK" in content, content
    assert uid in content, content


def test_a_healthy_store_still_grades_pass(tmp_path):
    from isocenter.session import DicomSession

    db = str(tmp_path / "healthy.db")
    _store_with_frame(db, _HEALTHY, _RAW_LEN)

    report = str(tmp_path / "report.md")
    session = DicomSession(persistence_file=db)
    try:
        session.store_backend.log_audit(
            "ANONYMIZE_METADATA", "1.2.3", "baseline row")
        session.generate_report(report)
    finally:
        session.close()

    with open(report, "r") as f:
        content = f.read()
    assert "**PASS**" in content, content


def test_an_instance_with_no_sidecar_frame_is_ignored(tmp_path):
    """Metadata-only instances have nothing to disagree with."""
    inst = Instance("1.2.3.meta", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.set_attr("0028,0010", 3)
    inst.set_attr("0028,0011", 8)

    patient = Patient("META1", "Meta^Only")
    study = Study("META1.STUDY", "20230101")
    series = Series("META1.SERIES", "OT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)

    db = str(tmp_path / "meta.db")
    store = SqliteStore(db)
    store.save_all([patient])
    store.stop()

    assert SqliteStore(db).check_pixel_geometry() == []
