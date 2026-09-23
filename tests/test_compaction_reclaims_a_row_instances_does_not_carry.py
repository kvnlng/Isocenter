"""Compaction reclaims a blob row no `instances` row names -- by design (#368).

A characterization test, green before and after #368, and it is meant
to stay green: `compact_sidecar()`'s orphan predicate (`EXISTS (SELECT 1
FROM instances i WHERE i.sop_instance_uid = b.instance_uid)`) is
*correct*. A row nothing references is, by the store's own definition,
unreferenced, and reclaiming it is the operation's whole purpose.

What this pins is the shape of the hazard the facade's pass-lock exists
for. A redaction worker's exact sequence is `regenerate_uid()` +
`set_pixel_data()` + `persist_pixel_data()`: the blob row lands under
the new UID, the `instances` row still carries the old one until the
next save, and a compaction in between reclaims the redacted frame.
Measured on 0.9.3 at this layer: sidecar 12347 -> 12321 bytes, the row
under the new UID gone, `get_pixel_data()` raising. Through the front
door that is `tests/test_compact_refuses_during_a_pass.py`, where
`Session.compact()` now refuses. Direct callers of `compact_sidecar()`
are not covered -- the pass is a `Session` concept -- and this test is
the record of what they get.

**The mutation it kills** is the rejected fix: a predicate rewritten to
keep unreferenced rows (a pending-UID table, a generation column). That
turns this red, which is the point -- it would make the store carry a
second answer to "what is live", and would still not stop the row's
*offset* moving under a loader the parent has not yet bound (§7.2).
"""
import os
import sqlite3

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.persistence import SqliteStore

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"


def _instance(uid, seed):
    inst = Instance(uid, CT_STORAGE, 1, file_path=None)
    inst.set_attr("0028,0010", 64)
    inst.set_attr("0028,0011", 64)
    inst.set_attr("0028,0002", 1)
    inst.set_attr("0028,0100", 8)
    inst.set_attr("0028,0103", 0)
    inst.set_pixel_data(np.random.default_rng(seed).integers(
        0, 256, (64, 64), dtype=np.uint8))
    return inst


def test_a_row_under_a_regenerated_uid_is_reclaimed_at_the_store_layer(
        tmp_path):
    store = SqliteStore(str(tmp_path / "reclaim.db"))
    try:
        patient = Patient("P_RECLAIM", "Reclaim Test")
        study = Study("S_RECLAIM", "20230101")
        series = Series("SE_RECLAIM", "CT", 1)
        patient.studies.append(study)
        study.series.append(series)
        live = [_instance(f"1.2.3.R{i}", i + 1) for i in range(3)]
        series.instances.extend(live)
        for inst in live:
            store.persist_pixel_data(inst)
        store.save_all([patient])

        # The worker's exact sequence, with no save in between.
        target = live[1]
        old_uid = target.sop_instance_uid
        target.regenerate_uid("2.25.1001")
        new_uid = target.sop_instance_uid
        assert new_uid != old_uid
        target.set_pixel_data(np.zeros((64, 64), dtype=np.uint8))
        store.persist_pixel_data(target)

        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute(
                "SELECT 1 FROM instances WHERE sop_instance_uid = ?",
                (new_uid,)).fetchone() is None, (
                "the instances row already carries the new UID; the "
                "hazard needs it to lag until the next save")
            assert conn.execute(
                "SELECT 1 FROM instance_blobs WHERE instance_uid = ?",
                (new_uid,)).fetchone() is not None

        before = os.path.getsize(store.sidecar_path)
        store.compact_sidecar()
        after = os.path.getsize(store.sidecar_path)

        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM instance_blobs WHERE instance_uid = ?",
                (new_uid,)).fetchone()
        assert row is None, (
            "the blob row under the regenerated UID survived compaction: "
            "the orphan predicate has been taught to keep unreferenced "
            "rows, which is the rejected fix (2026-09-08 spec §4.3)")
        assert after < before, "compaction reclaimed nothing"

        target.discard_pixel_data()
        with pytest.raises(RuntimeError):
            target.get_pixel_data()
    finally:
        store.stop()
