import sqlite3

import numpy as np
import pytest

from isocenter.io_handlers import ingest_worker
from scripts.generate_waveform_test_data import write_fixture, LEADS


@pytest.fixture
def ecg_file(tmp_path):
    return write_fixture(str(tmp_path / "ecg.dcm"), num_samples=500)


def test_ingest_worker_returns_waveform_bytes(ecg_file):
    meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err = ingest_worker(ecg_file)
    assert err is None
    assert meta["modality"] == "ECG"
    assert w_bytes is not None
    assert len(w_bytes) == 500 * len(LEADS) * 2


def test_waveform_hash_is_sha256_of_raw_bytes(ecg_file):
    import hashlib
    _, _, _, _, _, w_bytes, w_hash, _ = ingest_worker(ecg_file)
    assert w_bytes is not None
    assert w_hash == hashlib.sha256(w_bytes).hexdigest()


def test_waveform_metadata_survives_ingest(ecg_file):
    _, inst, _, _, _, _, _, _ = ingest_worker(ecg_file)
    from isocenter.waveform import Waveform
    seq = inst.sequences.get("5400,0100")
    assert seq is not None and seq.items
    wf = Waveform.from_dicom_item(seq.items[0])
    assert wf.num_channels == len(LEADS)
    assert wf.num_samples == 500
    assert wf.channels[0].source_code == "MDC_ECG_LEAD_I"


def test_waveform_data_element_is_not_kept_in_attributes(ecg_file):
    """Bulk samples belong in the sidecar, not the JSON core attributes."""
    _, inst, _, _, _, _, _, _ = ingest_worker(ecg_file)
    seq = inst.sequences["5400,0100"]
    assert "5400,1010" not in seq.items[0].attributes


def test_pixel_only_file_yields_no_waveform(tmp_path):
    """A CT instance must still ingest with waveform fields empty."""
    import pydicom
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = "CT"
    ds.PatientID = "CT1"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.zeros((4, 4), dtype=np.uint16).tobytes()

    path = str(tmp_path / "ct.dcm")
    pydicom.dcmwrite(path, ds, enforce_file_format=True)

    _, _, p_bytes, _, _, w_bytes, w_hash, err = ingest_worker(path)
    assert err is None
    assert p_bytes is not None
    assert w_bytes is None
    assert w_hash is None


def test_instance_waveform_accessors_roundtrip():
    from isocenter.entities import Instance
    inst = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    arr = np.arange(12, dtype=np.int16).reshape(6, 2)

    inst.waveform_array = arr
    assert inst.get_waveform_data() is arr

    # No loader and no file path: unloading would lose data.
    assert inst.unload_waveform_data() is False
    assert inst.waveform_array is arr

    inst._waveform_loader = lambda: arr
    assert inst.unload_waveform_data() is True
    assert inst.waveform_array is None
    np.testing.assert_array_equal(inst.get_waveform_data(), arr)


def test_unload_waveform_refuses_when_only_a_file_path_backs_it():
    """A file_path is not a recovery route for waveforms.

    `get_waveform_data` has no dcmread fallback -- it returns the cached
    array, else the loader, else None. So treating `file_path` as proof the
    samples are recoverable would report a safe unload and then hand back
    None forever. The two methods must agree on the one route that exists.
    """
    from isocenter.entities import Instance
    inst = Instance("1.2.4", "1.2.840.10008.5.1.4.1.1.9.1.1", 1,
                    file_path="/nonexistent/ecg.dcm")
    arr = np.arange(8, dtype=np.int16).reshape(4, 2)
    inst.waveform_array = arr

    # Preconditions: the only thing that could justify unloading is the
    # file path, and there is genuinely no loader to fall back on.
    assert inst.file_path
    assert inst._waveform_loader is None
    assert inst.waveform_array is arr

    assert inst.unload_waveform_data() is False
    assert inst.waveform_array is arr

    # And the reason it must refuse: nothing would have restored it.
    inst.waveform_array = None
    assert inst.get_waveform_data() is None


def test_ingest_registers_the_waveform_blob_reference(tmp_path):
    """Without a blob-table row, compaction reclaims the waveform."""
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=300)

    session = DicomSession(persistence_file=str(tmp_path / "ref.db"))
    try:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        ref = session.store_backend.get_blob_ref(inst.sop_instance_uid, "waveform")
        assert ref is not None
        assert ref["length"] > 0
    finally:
        session.close()


def test_waveform_survives_a_session_reload(tmp_path):
    """Isocenter's pause/resume promise must hold for waveforms, not just pixels."""
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=300)
    db = str(tmp_path / "reload.db")

    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        original = session.store.patients[0].studies[0].series[0].instances[0]
        expected = original.get_waveform_data().copy()
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        inst = reopened.store.patients[0].studies[0].series[0].instances[0]
        assert inst.waveform_array is None, "should be lazy, not eagerly loaded"
        np.testing.assert_array_equal(inst.get_waveform_data(), expected)
    finally:
        reopened.close()


def test_waveform_loader_is_repointed_by_compaction(tmp_path):
    """Compaction relocates waveform bytes; stale loaders must not survive it.

    `compact_sidecar`'s uid_map is deliberately pixels-only, so nothing in
    the pixel patching path can repoint a waveform loader. Without an
    explicit waveform pass the loader keeps a pre-compaction offset and
    either reads the wrong bytes or runs off the end of the file.
    """
    import hashlib

    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=300)
    db = str(tmp_path / "compact.db")

    session = DicomSession(persistence_file=db)
    try:
        # A blob owned by an instance that will never exist in `instances`.
        # It occupies the head of the sidecar, so the waveform written next
        # cannot land at offset 0, and it is an orphan, so compaction is
        # forced to actually reclaim its bytes and shift the waveform down.
        dead = b"\xa5" * 8192
        d_off, d_len = session.store_backend.sidecar.write_frame(dead, 'zlib')
        session.store_backend.record_blob_ref(
            "9.9.9.orphan", "pixels", d_off, d_len,
            hashlib.sha256(dead).hexdigest(), 'zlib')
        assert d_off == 0 and d_len > 0

        session.ingest(str(src))

        inst = session.store.patients[0].studies[0].series[0].instances[0]

        # Preconditions: the loader is the sidecar-backed one, its samples
        # are not already cached in RAM (otherwise a stale offset would go
        # unnoticed), and it does not sit at offset 0 (otherwise compaction
        # would move nothing and the test could not fail).
        assert inst._waveform_loader is not None
        assert inst.waveform_array is None
        assert inst._waveform_loader.offset == d_len > 0

        expected = inst.get_waveform_data().copy()
        assert expected.shape == (300, len(LEADS))
        inst.unload_waveform_data()
        assert inst.waveform_array is None

        session.compact()

        # The orphan's bytes really were reclaimed and the waveform moved.
        ref = session.store_backend.get_blob_ref(inst.sop_instance_uid, "waveform")
        assert ref is not None
        assert ref["offset"] == 0
        assert session.store_backend.get_blob_ref("9.9.9.orphan", "pixels") is None

        # The in-memory loader must have been repointed with it.
        assert inst._waveform_loader.offset == 0
        np.testing.assert_array_equal(inst.get_waveform_data(), expected)
    finally:
        session.close()


# --- Multiplex group truncation (#36) ---------------------------------
#
# Each Waveform Sequence item is a multiplex group with its own sampling
# frequency and channel set -- how DICOM carries ECG at 500 Hz alongside
# respiration at 25 Hz. Isocenter keeps group 0 only. The defect being
# fixed here is not the missing multi-rate support, which is deliberately
# deferred; it is that the limitation used to be silent.


def _two_group_file(path, num_samples=200):
    """An ECG fixture carrying a second, slower multiplex group."""
    import copy
    import pydicom
    from scripts.generate_waveform_test_data import build_ecg_dataset

    ds = build_ecg_dataset(num_samples=num_samples)
    second = copy.deepcopy(ds.WaveformSequence[0])
    second.SamplingFrequency = 25.0
    ds.WaveformSequence.append(second)
    pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
    return str(path)


def test_ingest_worker_reports_the_multiplex_group_count(tmp_path):
    meta, *_ = ingest_worker(_two_group_file(tmp_path / "multi.dcm"))
    assert meta["waveform_groups"] == 2


def test_a_single_group_record_reports_one_group(ecg_file):
    meta, *_ = ingest_worker(ecg_file)
    assert meta["waveform_groups"] == 1


def test_a_multi_group_record_warns_that_groups_were_discarded(tmp_path, caplog):
    import logging
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    _two_group_file(src / "multi.dcm")

    session = DicomSession(persistence_file=str(tmp_path / "multi.db"))
    try:
        with caplog.at_level(logging.WARNING):
            session.ingest(str(src))
    finally:
        session.close()

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno >= logging.WARNING]
    assert any("multiplex" in m.lower() for m in warnings), warnings


def test_a_multi_group_record_records_an_audit_entry(tmp_path):
    """A log line the user may never read is not a compliance trail."""
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    _two_group_file(src / "multi.dcm")

    session = DicomSession(persistence_file=str(tmp_path / "audit.db"))
    try:
        session.ingest(str(src))
        db_path = session.store_backend.db_path
        # The audit writer batches on a background thread; close() drains it.
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT details FROM audit_log WHERE action_type='DATA_LOSS'"
        ).fetchall()

    assert len(rows) == 1
    assert "2" in rows[0][0]


def test_a_single_group_record_records_no_data_loss(tmp_path):
    """The warning must not fire for the ordinary single-group ECG."""
    from isocenter.session import DicomSession

    src = tmp_path / "src"
    src.mkdir()
    write_fixture(str(src / "ecg.dcm"), num_samples=200)

    session = DicomSession(persistence_file=str(tmp_path / "single.db"))
    try:
        session.ingest(str(src))
        db_path = session.store_backend.db_path
    finally:
        session.close()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT 1 FROM audit_log WHERE action_type='DATA_LOSS'"
        ).fetchall()

    assert rows == []
