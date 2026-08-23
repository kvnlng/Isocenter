import numpy as np
import pytest

from gantry.persistence import SqliteStore
from gantry.entities import Instance


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(str(tmp_path / "blobs.db"))
    yield s
    s.stop()


def _instance(uid="1.2.3.4"):
    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    inst.set_attr("0028,0010", 4)
    inst.set_attr("0028,0011", 4)
    return inst


def test_persist_and_read_back_waveform_blob(store):
    inst = _instance()
    payload = b"\x01\x02\x03\x04" * 32

    store.persist_blob(inst, "waveform", payload)

    ref = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    assert ref is not None
    assert ref["length"] > 0
    raw = store.sidecar.read_frame(ref["offset"], ref["length"], ref["compress_alg"])
    assert raw == payload


def test_kinds_are_independent(store):
    inst = _instance()
    store.persist_blob(inst, "waveform", b"WAVE" * 16)
    store.persist_blob(inst, "pixels", b"PIXL" * 16)

    wave = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    pixels = store.get_blob_ref(inst.sop_instance_uid, "pixels")
    assert wave["offset"] != pixels["offset"]
    assert store.sidecar.read_frame(wave["offset"], wave["length"], wave["compress_alg"]) == b"WAVE" * 16
    assert store.sidecar.read_frame(pixels["offset"], pixels["length"], pixels["compress_alg"]) == b"PIXL" * 16


def test_repersisting_a_kind_replaces_its_reference(store):
    inst = _instance()
    store.persist_blob(inst, "waveform", b"first" * 10)
    first = store.get_blob_ref(inst.sop_instance_uid, "waveform")

    store.persist_blob(inst, "waveform", b"second" * 10)
    second = store.get_blob_ref(inst.sop_instance_uid, "waveform")

    assert second["offset"] != first["offset"]
    assert store.sidecar.read_frame(second["offset"], second["length"], second["compress_alg"]) == b"second" * 10


def test_missing_kind_returns_none(store):
    assert store.get_blob_ref("nope", "waveform") is None


def test_blob_hash_is_sha256_of_raw_bytes(store):
    import hashlib
    inst = _instance()
    payload = b"integrity" * 8
    store.persist_blob(inst, "waveform", payload)
    ref = store.get_blob_ref(inst.sop_instance_uid, "waveform")
    assert ref["hash"] == hashlib.sha256(payload).hexdigest()


def test_legacy_pixel_columns_backfill_into_blob_table(tmp_path):
    """A 0.6.x-era DB has pixel_* columns and no instance_blobs rows."""
    import sqlite3
    db_path = str(tmp_path / "legacy.db")

    s = SqliteStore(db_path)
    s.stop()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid, instance_number,"
            " pixel_offset, pixel_length, pixel_hash, compress_alg)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("legacy.1", "1.2.840.10008.5.1.4.1.1.2", 1, 0, 128, "deadbeef", "zlib"))
        conn.execute("DELETE FROM instance_blobs")
        conn.commit()

    reopened = SqliteStore(db_path)
    try:
        ref = reopened.get_blob_ref("legacy.1", "pixels")
        assert ref is not None
        assert ref["offset"] == 0
        assert ref["length"] == 128
        assert ref["hash"] == "deadbeef"
        assert ref["compress_alg"] == "zlib"
    finally:
        reopened.stop()


def test_compaction_preserves_both_kinds(store):
    """Regression guard: compaction must not treat either kind as orphaned.

    The ingest path writes pixel frames through SidecarManager and records
    only instances.pixel_offset, so a compaction that reads instance_blobs
    alone would discard them.
    """
    import sqlite3

    pixel_payload = b"PIXELS" * 64
    wave_payload = b"WAVEFORM" * 64

    p_off, p_len = store.sidecar.write_frame(pixel_payload, 'zlib')
    w_off, w_len = store.sidecar.write_frame(wave_payload, 'zlib')

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
            " instance_number, pixel_offset, pixel_length, pixel_hash, compress_alg)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("inst.pixels", "1.2.840.10008.5.1.4.1.1.2", 1,
             p_off, p_len, "pixhash", "zlib"))
        conn.execute(
            "INSERT INTO instances (sop_instance_uid, sop_class_uid, instance_number)"
            " VALUES (?, ?, ?)",
            ("inst.wave", "1.2.840.10008.5.1.4.1.1.9.1.1", 1))
        conn.commit()

    store.record_blob_ref("inst.wave", "waveform", w_off, w_len, "wavhash", "zlib")

    store.compact_sidecar()

    pixels = store.get_blob_ref("inst.pixels", "pixels")
    wave = store.get_blob_ref("inst.wave", "waveform")
    assert pixels is not None, "ingested pixel blob was dropped by compaction"
    assert wave is not None, "waveform blob was dropped by compaction"

    assert store.sidecar.read_frame(
        pixels["offset"], pixels["length"], pixels["compress_alg"]) == pixel_payload
    assert store.sidecar.read_frame(
        wave["offset"], wave["length"], wave["compress_alg"]) == wave_payload
