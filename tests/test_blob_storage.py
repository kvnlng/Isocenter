import os
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


# ---------------------------------------------------------------------------
# Regression guards for the compaction hazards.
#
# These reproduce failure modes that are silent: nothing raises, no test goes
# red at the time the damage is done, and the corruption only surfaces later
# as wrong pixel bytes. They are deliberately low-level -- they drive the
# store directly so the exact table state that triggers each bug can be built.
# ---------------------------------------------------------------------------


def _graph(uid, arr, pid="P1"):
    """Build a minimal Patient->Study->Series->Instance graph around `arr`."""
    from gantry.entities import Patient, Study, Series

    inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.set_pixel_data(arr)
    pat = Patient(pid, "Test Patient")
    st = Study("ST-" + pid, "20230101")
    se = Series("SE-" + pid, "CT", 1)
    pat.studies.append(st)
    st.series.append(se)
    se.instances.append(inst)
    return pat, inst


def test_compaction_does_not_resurrect_pre_redaction_pixels(tmp_path):
    """CRITICAL: compaction must never restore pixels that redaction destroyed.

    `save_all` writes instances.pixel_* directly. If it does not also write
    instance_blobs, the blob row keeps pointing at the PRE-redaction frame
    while instances points at the redacted one. Compaction reads the blob
    table, so it copies the original bytes forward, discards the redacted
    ones, and repoints instances at the resurrected original -- silently
    undoing de-identification.
    """
    db_path = str(tmp_path / "redact.db")

    # Distinct payloads with distinct compressed lengths, so a mismatched
    # (offset, length) pair also produces a detectable truncation.
    original = np.tile(np.arange(64, dtype=np.uint8), (64, 1))
    redacted = np.zeros((64, 64), dtype=np.uint8)

    # 1. Persist the original through the normal save path.
    s = SqliteStore(db_path)
    pat, _ = _graph("redact.1", original)
    s.save_all([pat])
    s.stop()

    # 2. Reopen: the legacy back-fill seeds instance_blobs from pixel_*.
    s2 = SqliteStore(db_path)
    try:
        pat2 = s2.load_patient("P1")
        inst2 = pat2.studies[0].series[0].instances[0]

        # 3. Redact and save again. save_all appends the new frame and
        #    repoints instances.pixel_offset at it.
        inst2.set_pixel_data(redacted)
        s2.save_all([pat2])

        # 4. Compact.
        s2.compact_sidecar()

        ref = s2.get_blob_ref("redact.1", "pixels")
        assert ref is not None
        survived = s2.sidecar.read_frame(
            ref["offset"], ref["length"], ref["compress_alg"])
        assert survived != original.tobytes(), (
            "compaction resurrected the pre-redaction pixels")
        assert survived == redacted.tobytes()

        # The legacy columns must also land on the redacted generation, with
        # offset and length taken from the SAME generation (a mixed pair
        # yields a truncated zlib stream rather than wrong-but-valid bytes).
        reloaded = s2.load_patient("P1")
        r_inst = reloaded.studies[0].series[0].instances[0]
        arr = r_inst.get_pixel_data()
        assert arr is not None, "legacy pixel_offset/pixel_length disagree"
        assert arr.tobytes() == redacted.tobytes(), (
            "instances.pixel_* still points at the un-redacted frame")
    finally:
        s2.stop()


def test_compaction_does_not_write_offsets_across_rows(tmp_path):
    """Hazard A: `updates` carries instance_blobs.id, never instances.id.

    The two id spaces are deliberately rotated here so they overlap but do
    NOT correspond: blob id k belongs to u.k while instances.id k belongs to
    u.(k-1). An update keyed on the wrong table therefore lands on a
    neighbouring row instead of harmlessly matching nothing.
    """
    import sqlite3

    db_path = str(tmp_path / "rotate.db")
    s = SqliteStore(db_path)
    try:
        payloads = {}
        for n in range(1, 6):
            uid = "u.%d" % n
            arr = np.full((40, 40), n, dtype=np.uint8)
            payloads[uid] = arr.tobytes()
            inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", n)
            inst.set_pixel_data(arr)
            with sqlite3.connect(db_path) as c:
                c.execute(
                    "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
                    " instance_number) VALUES (?, ?, ?)",
                    (uid, "1.2.840.10008.5.1.4.1.1.2", n))
            s.persist_pixel_data(inst)
            with sqlite3.connect(db_path) as c:
                c.execute(
                    "UPDATE instances SET pixel_offset=?, pixel_length=?,"
                    " pixel_hash=?, compress_alg='zlib'"
                    " WHERE sop_instance_uid=?",
                    (inst._pixel_loader.offset, inst._pixel_loader.length,
                     inst._pixel_hash, uid))

        # Rotate instances.id by one so the id spaces misalign.
        with sqlite3.connect(db_path) as c:
            c.execute("UPDATE instances SET id = id + 1000")
            c.execute("UPDATE instances SET id = id - 999")
            blob_ids = list(c.execute(
                "SELECT id, instance_uid FROM instance_blobs ORDER BY id"))
            inst_ids = list(c.execute(
                "SELECT id, sop_instance_uid FROM instances ORDER BY id"))
        # Same id values on both sides, but mapped to different UIDs.
        assert dict(blob_ids) != dict(inst_ids), (
            "test setup failed to misalign the id spaces")
        assert set(dict(blob_ids)) & set(dict(inst_ids)), (
            "test setup failed to make the id spaces overlap")

        # Dead space plus a deletion, so every surviving offset must move.
        s.sidecar.write_frame(b"GARBAGE" * 500, 'zlib')
        with sqlite3.connect(db_path) as c:
            c.execute("DELETE FROM instances WHERE sop_instance_uid='u.1'")

        uid_map = s.compact_sidecar()

        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            rows = list(c.execute(
                "SELECT sop_instance_uid, pixel_offset, pixel_length"
                " FROM instances"))
        assert rows
        for row in rows:
            uid = row["sop_instance_uid"]
            blob = s.get_blob_ref(uid, "pixels")
            assert row["pixel_offset"] == blob["offset"], (
                "instance %s received another row's offset" % uid)
            assert row["pixel_length"] == blob["length"], (
                "instance %s received another row's length" % uid)
            assert s.sidecar.read_frame(
                row["pixel_offset"], row["pixel_length"], "zlib") == payloads[uid]
            assert s.sidecar.read_frame(
                blob["offset"], blob["length"], blob["compress_alg"]) == payloads[uid]

        for uid, (off, ln) in uid_map.items():
            assert s.sidecar.read_frame(off, ln, 'zlib') == payloads[uid]
    finally:
        s.stop()


def test_compaction_uid_map_never_returns_a_waveform_offset(tmp_path):
    """Hazard B: uid_map is keyed by UID alone and drives _pixel_loader.

    DicomSession.compact() assigns uid_map[uid] straight onto
    inst._pixel_loader.offset/.length. If a waveform blob for the same UID
    reached uid_map, the pixel loader would be pointed at waveform bytes and
    every later pixel read would be garbage -- with no error at compaction
    time. Only 'pixels' rows may be published.
    """
    import sqlite3

    db_path = str(tmp_path / "kinds.db")
    s = SqliteStore(db_path)
    try:
        uid = "both.1"
        pixels = np.full((32, 32), 3, dtype=np.uint8)
        wave = b"WAVEFORM-PAYLOAD" * 64

        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
                " instance_number) VALUES (?, ?, ?)",
                (uid, "1.2.840.10008.5.1.4.1.1.2", 1))

        inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
        inst.set_pixel_data(pixels)
        s.persist_pixel_data(inst)

        # Same UID, different kind. This is the bait.
        s.persist_blob(inst, "waveform", wave)

        wave_ref_before = s.get_blob_ref(uid, "waveform")
        assert wave_ref_before is not None

        uid_map = s.compact_sidecar()

        assert uid in uid_map, "pixel entry missing from uid_map"
        off, ln = uid_map[uid]

        pixel_ref = s.get_blob_ref(uid, "pixels")
        wave_ref = s.get_blob_ref(uid, "waveform")

        assert (off, ln) == (pixel_ref["offset"], pixel_ref["length"]), (
            "uid_map does not carry the pixel reference")
        assert (off, ln) != (wave_ref["offset"], wave_ref["length"]), (
            "uid_map published the waveform reference under the pixel UID")
        assert s.sidecar.read_frame(off, ln, 'zlib') == pixels.tobytes(), (
            "uid_map entry does not decode to the pixel payload")

        # Both kinds must still be intact and independently addressable.
        assert s.sidecar.read_frame(
            wave_ref["offset"], wave_ref["length"],
            wave_ref["compress_alg"]) == wave
    finally:
        s.stop()


def test_save_all_keeps_the_blob_table_in_step_with_instances(tmp_path):
    """save_all must write instance_blobs in the same transaction.

    Without this the blob table drifts one generation behind on every
    re-save, which is the root cause of the pixel-resurrection bug.
    """
    db_path = str(tmp_path / "instep.db")
    s = SqliteStore(db_path)
    try:
        first = np.full((16, 16), 1, dtype=np.uint8)
        second = np.full((16, 16), 2, dtype=np.uint8)

        pat, inst = _graph("instep.1", first)
        s.save_all([pat])

        import sqlite3
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT pixel_offset, pixel_length, pixel_hash, compress_alg"
                " FROM instances WHERE sop_instance_uid='instep.1'").fetchone()
        ref = s.get_blob_ref("instep.1", "pixels")
        assert ref is not None, "save_all did not record a blob reference"
        assert (ref["offset"], ref["length"]) == (
            row["pixel_offset"], row["pixel_length"])
        assert ref["hash"] == row["pixel_hash"]
        assert ref["compress_alg"] == row["compress_alg"]

        # Re-save with new pixels; both must advance together.
        inst.set_pixel_data(second)
        s.save_all([pat])

        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            row2 = c.execute(
                "SELECT pixel_offset, pixel_length, pixel_hash"
                " FROM instances WHERE sop_instance_uid='instep.1'").fetchone()
        ref2 = s.get_blob_ref("instep.1", "pixels")
        assert (ref2["offset"], ref2["length"]) == (
            row2["pixel_offset"], row2["pixel_length"]), (
            "blob table drifted a generation behind instances")
        assert ref2["offset"] != ref["offset"]
        assert s.sidecar.read_frame(
            ref2["offset"], ref2["length"],
            ref2["compress_alg"]) == second.tobytes()
    finally:
        s.stop()


def test_compaction_patches_offset_and_length_together(tmp_path):
    """CRITICAL: the legacy pair must never be assembled from two generations.

    Compaction repoints `instances.pixel_offset`. If it does not repoint
    `pixel_length` in the same statement, an instance whose blob row and
    legacy columns disagree ends up with an offset from one frame and a
    length from another -- a truncated zlib stream, which surfaces as a
    decode failure or short read far from the cause.
    """
    import sqlite3

    db_path = str(tmp_path / "pair.db")
    s = SqliteStore(db_path)
    try:
        uid = "pair.1"
        small = np.full((8, 8), 5, dtype=np.uint8)
        big = np.tile(np.arange(256, dtype=np.uint8), (128, 1))

        with sqlite3.connect(db_path) as c:
            c.execute(
                "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
                " instance_number) VALUES (?, ?, ?)",
                (uid, "1.2.840.10008.5.1.4.1.1.2", 1))

        inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", 1)
        inst.set_pixel_data(small)
        s.persist_pixel_data(inst)

        # Legacy columns pinned to the SMALL frame...
        with sqlite3.connect(db_path) as c:
            c.execute(
                "UPDATE instances SET pixel_offset=?, pixel_length=?,"
                " compress_alg='zlib' WHERE sop_instance_uid=?",
                (inst._pixel_loader.offset, inst._pixel_loader.length, uid))

        # ...while the authoritative blob row advances to the BIG frame.
        b_off, b_len = s.sidecar.write_frame(big.tobytes(), 'zlib')
        s.record_blob_ref(uid, "pixels", b_off, b_len, "bighash", "zlib")

        stale = s.get_blob_ref(uid, "pixels")
        assert stale["length"] != inst._pixel_loader.length, (
            "test setup failed to diverge the lengths")

        s.compact_sidecar()

        ref = s.get_blob_ref(uid, "pixels")
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT pixel_offset, pixel_length FROM instances"
                " WHERE sop_instance_uid=?", (uid,)).fetchone()

        assert row["pixel_offset"] == ref["offset"]
        assert row["pixel_length"] == ref["length"], (
            "compaction moved the offset but left a stale length behind")
        # The pair must decode -- a mismatched pair truncates the stream.
        assert s.sidecar.read_frame(
            row["pixel_offset"], row["pixel_length"], "zlib") == big.tobytes()
    finally:
        s.stop()


def test_record_blob_ref_joins_an_existing_transaction(tmp_path):
    """The ingest path records refs from inside an open transaction.

    Opening a nested connection there is not merely untidy: on a file-backed
    DB the inner write blocks on the outer write lock for the full 900 s
    timeout before failing, and on a `:memory:` store the non-reentrant
    `_memory_lock` deadlocks outright. The connection must be passable in.
    """
    s = SqliteStore(str(tmp_path / "conn.db"))
    try:
        with s._get_connection() as conn:
            conn.execute(
                "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
                " instance_number) VALUES (?, ?, ?)",
                ("txn.1", "1.2.840.10008.5.1.4.1.1.9.1.1", 1))
            # Must reuse `conn` -- and must not hang.
            s.record_blob_ref("txn.1", "waveform", 7, 11, "h", "zlib", conn=conn)

        assert s.get_blob_ref("txn.1", "waveform") == {
            "offset": 7, "length": 11, "hash": "h", "compress_alg": "zlib"}

        # Proof it genuinely enlisted rather than committing on its own:
        # rolling the caller's transaction back must drop the row too.
        try:
            with s._get_connection() as conn:
                s.record_blob_ref("txn.2", "waveform", 1, 2, "h2", "zlib", conn=conn)
                raise RuntimeError("forced rollback")
        except RuntimeError:
            pass
        assert s.get_blob_ref("txn.2", "waveform") is None, (
            "record_blob_ref committed outside the caller's transaction")
    finally:
        s.stop()


def test_failed_compaction_commit_rolls_the_file_back(tmp_path):
    """A failure in the DB step must leave file and DB on the same generation.

    The DB step is irreversible -- it deletes orphan rows and rewrites every
    offset. The file swap therefore happens first and is rolled back if the
    DB write fails, so a crash can never leave the database describing a
    layout the sidecar does not hold.
    """
    import contextlib
    import sqlite3

    db_path = str(tmp_path / "crash.db")
    s = SqliteStore(db_path)
    try:
        payloads = {}
        for n in (1, 2, 3):
            uid = "c.%d" % n
            arr = np.full((30, 30), n, dtype=np.uint8)
            payloads[uid] = arr.tobytes()
            inst = Instance(uid, "1.2.840.10008.5.1.4.1.1.2", n)
            inst.set_pixel_data(arr)
            with sqlite3.connect(db_path) as c:
                c.execute(
                    "INSERT INTO instances (sop_instance_uid, sop_class_uid,"
                    " instance_number) VALUES (?, ?, ?)",
                    (uid, "1.2.840.10008.5.1.4.1.1.2", n))
            s.persist_pixel_data(inst)
            with sqlite3.connect(db_path) as c:
                c.execute(
                    "UPDATE instances SET pixel_offset=?, pixel_length=?,"
                    " compress_alg='zlib' WHERE sop_instance_uid=?",
                    (inst._pixel_loader.offset, inst._pixel_loader.length, uid))

        # Dead space plus a deletion, so compaction really rewrites the file.
        s.sidecar.write_frame(b"JUNK" * 900, 'zlib')
        with sqlite3.connect(db_path) as c:
            c.execute("DELETE FROM instances WHERE sop_instance_uid='c.1'")

        with open(s.sidecar_path, "rb") as f:
            before_bytes = f.read()
        with sqlite3.connect(db_path) as c:
            before_inst = sorted(c.execute(
                "SELECT sop_instance_uid, pixel_offset, pixel_length FROM instances"))
            before_blob = sorted(c.execute(
                "SELECT instance_uid, offset, length FROM instance_blobs"))

        # Fail only the DB-update step: the SELECT phase is the first
        # connection, the update block the second.
        real = s._get_connection
        state = {"n": 0}

        @contextlib.contextmanager
        def flaky():
            state["n"] += 1
            if state["n"] >= 2:
                raise sqlite3.OperationalError("simulated commit failure")
            with real() as conn:
                yield conn

        s._get_connection = flaky
        try:
            with pytest.raises(sqlite3.OperationalError):
                s.compact_sidecar()
        finally:
            s._get_connection = real

        with open(s.sidecar_path, "rb") as f:
            after_bytes = f.read()
        with sqlite3.connect(db_path) as c:
            after_inst = sorted(c.execute(
                "SELECT sop_instance_uid, pixel_offset, pixel_length FROM instances"))
            after_blob = sorted(c.execute(
                "SELECT instance_uid, offset, length FROM instance_blobs"))

        assert after_bytes == before_bytes, (
            "sidecar left compacted even though the DB write failed")
        assert after_inst == before_inst
        assert after_blob == before_blob
        assert not [f for f in os.listdir(str(tmp_path)) if ".compact." in f], (
            "temp or backup files left behind")

        # Data still readable, and a later compaction still succeeds.
        for uid in ("c.2", "c.3"):
            ref = s.get_blob_ref(uid, "pixels")
            assert s.sidecar.read_frame(
                ref["offset"], ref["length"], ref["compress_alg"]) == payloads[uid]

        s.compact_sidecar()
        for uid in ("c.2", "c.3"):
            ref = s.get_blob_ref(uid, "pixels")
            assert s.sidecar.read_frame(
                ref["offset"], ref["length"], ref["compress_alg"]) == payloads[uid]
        assert s.get_blob_ref("c.1", "pixels") is None
        assert not [f for f in os.listdir(str(tmp_path)) if ".compact." in f]
    finally:
        s.stop()


def test_resaving_a_hydrated_instance_does_not_erase_the_blob_hash(tmp_path):
    """The mirror must never NULL a recorded hash.

    Hydration wires up `_pixel_loader` but never `_pixel_hash`. Both load
    paths mark instances saved, so the trigger is the first save after a
    load in which the instance was dirtied by something OTHER than a pixel
    change -- a tag edit, which is exactly what de-identification does. Such
    an instance takes the `_pixel_loader` branch with `p_hash = None`. The
    `instances` upsert COALESCEs that away; if the blob mirror assigns it
    unconditionally, instance_blobs.hash is erased while
    instances.pixel_hash survives -- two writers of the same fact
    disagreeing, which is the shape that caused the resurrection bug.
    """
    import sqlite3

    db_path = str(tmp_path / "rehash.db")
    arr = np.full((16, 16), 9, dtype=np.uint8)

    s = SqliteStore(db_path)
    pat, _ = _graph("rehash.1", arr)
    s.save_all([pat])
    original = s.get_blob_ref("rehash.1", "pixels")
    assert original["hash"], "setup: first save should record a hash"
    original_hash = original["hash"]
    s.stop()

    # Reopen and hydrate: the instance comes back with a loader but no
    # _pixel_hash and no pixel_array.
    s2 = SqliteStore(db_path)
    try:
        pat2 = s2.load_patient("P1")
        inst2 = pat2.studies[0].series[0].instances[0]
        assert inst2.pixel_array is None
        assert getattr(inst2, "_pixel_hash", None) is None, (
            "setup: hydration unexpectedly restored _pixel_hash")

        # Dirty it the way de-identification does: edit a tag, leave the
        # pixels alone. Without this the instance is clean and save_all
        # skips it entirely, so the test would pass vacuously.
        inst2.set_attr("0010,0010", "ANON^PATIENT")
        assert inst2._dirty, "setup: instance must be dirty to be re-saved"

        s2.save_all([pat2])

        ref = s2.get_blob_ref("rehash.1", "pixels")
        with sqlite3.connect(db_path) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT pixel_hash, pixel_offset, pixel_length FROM instances"
                " WHERE sop_instance_uid='rehash.1'").fetchone()

        assert ref["hash"] == original_hash, (
            "re-saving a hydrated instance erased instance_blobs.hash")
        assert ref["hash"] == row["pixel_hash"], (
            "instance_blobs.hash and instances.pixel_hash diverged")
        # The reference itself must still be intact and correct.
        assert (ref["offset"], ref["length"]) == (
            row["pixel_offset"], row["pixel_length"])
        assert s2.sidecar.read_frame(
            ref["offset"], ref["length"], ref["compress_alg"]) == arr.tobytes()
    finally:
        s2.stop()


def test_record_blob_ref_rejects_a_half_specified_reference(store):
    """offset and length must travel together or not at all.

    A row with a real offset and a missing length pairs a new frame with a
    stale or absent length -- the truncated-read failure mode. There is no
    safe interpretation, so the single writer refuses it rather than
    recording something unrecoverable.
    """
    with pytest.raises(ValueError):
        store.record_blob_ref("half.1", "pixels", 10, None, "h", "zlib")
    with pytest.raises(ValueError):
        store.record_blob_ref("half.1", "pixels", None, 10, "h", "zlib")
    assert store.get_blob_ref("half.1", "pixels") is None
