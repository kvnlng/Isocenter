from isocenter.services import MachinePixelIndex, RedactionService
from isocenter.io_handlers import DicomStore
import numpy as np
from support.project_secret import FIXED_A


def test_machine_index(dummy_patient):
    store = DicomStore()
    store.patients.append(dummy_patient)

    index = MachinePixelIndex()
    index.index_store(store)

    results = index.get_by_machine("SN-999")
    assert len(results) == 1
    assert results[0].sop_instance_uid == "1.2.840.111.1.1.1"


def test_redaction_service(dummy_patient):
    store = DicomStore()
    store.patients.append(dummy_patient)

    svc = RedactionService(store)

    # Original pixel check (top left is 0)
    inst = store.patients[0].studies[0].series[0].instances[0]
    # Set a value to verify it gets cleared
    inst.pixel_array[20, 20] = 500

    # Act: Redact region 10-50
    svc.redact_machine_instances("SN-999", [(10, 50, 10, 50)], project_secret=FIXED_A)

    # Assert
    # Assert
    assert inst.pixel_array[20, 20] == 0

    # Verify Redaction Flags
    # 1. UID should change
    assert inst.sop_instance_uid != "1.2.840.111.1.1.1"

    # 2. Image Type
    img_type = inst.attributes.get("0008,0008")
    assert "DERIVED" in img_type

    # 3. Description
    desc = inst.attributes.get("0008,2111")
    assert "Isocenter Pixel Redaction" in desc

    # 4. Burned In Annotation
    assert inst.attributes.get("0028,0301") == "NO"

    # 5. Code Sequence
    seq = inst.sequences.get("0008,9215")
    assert seq is not None
    assert seq.items[0].attributes["0008,0104"] == "Pixel Data modification"

# ---------------------------------------------------------------------------
# #368 (prerequisite): one redaction task appends exactly one frame
# ---------------------------------------------------------------------------

def test_one_redaction_task_appends_exactly_one_frame(dummy_patient, tmp_path,
                                                      monkeypatch):
    """`execute_redaction_task` writes one sidecar frame per redaction (#368).

    Measured on 0.9.3 with `SidecarManager.write_frame` wrapped: one
    task on a 64x64 8-bit instance made **two** calls, `[(28, 36),
    (64, 36)]`, growing the sidecar by 72 bytes for one 36-byte frame.
    The second was the `finally` persist in `execute_redaction_task`,
    guarded by `modified and not failed` -- on every path where that
    guard is true, the `persist_pixel_data` call in the `try` body has
    already run (had it raised, `failed` would be True), so the second
    call was redundant everywhere it executed. Its costs: sidecar
    growth doubled per redaction, one more orphan per redaction for
    `compact()` to reclaim, and -- the reason it is a #368 prerequisite
    -- the mutation dict's loader pointed at the *first* frame while
    the committed `instance_blobs` row pointed at the *second*, so the
    parent bound a loader whose offset disagreed with the store's row
    until the next save's dedup re-emitted it.

    Three assertions, one per cost: one `write_frame` call, sidecar
    growth of exactly that frame, and the mutation's loader offset
    equal to the committed row's.
    """
    import os
    import sqlite3

    from isocenter.persistence import SqliteStore
    from isocenter.sidecar import SidecarManager

    store = DicomStore()
    store.patients.append(dummy_patient)
    backend = SqliteStore(str(tmp_path / "one_frame.db"))
    try:
        inst = store.patients[0].studies[0].series[0].instances[0]
        inst.pixel_array[20, 20] = 500
        svc = RedactionService(store, backend)
        tasks = svc.prepare_redaction_tasks(
            {"serial_number": "SN-999", "redaction_zones": [[10, 50, 10, 50]]})
        assert len(tasks) == 1

        calls = []
        real_write = SidecarManager.write_frame

        def counting_write(self, data, compression='zlib'):
            result = real_write(self, data, compression)
            calls.append(result)
            return result

        monkeypatch.setattr(SidecarManager, "write_frame", counting_write)
        size_before = os.path.getsize(backend.sidecar_path)

        outcome = svc.execute_redaction_task(tasks[0])

        assert outcome.ok and outcome.mutation, outcome.error
        assert len(calls) == 1, (
            f"one redaction task appended {len(calls)} frames {calls}; the "
            "`finally` persist in execute_redaction_task is back, and the "
            "parent will bind a loader whose offset disagrees with the "
            "committed row (#368)")
        offset, length = calls[0]
        assert os.path.getsize(backend.sidecar_path) - size_before == length, (
            "the sidecar grew by more than the one frame that was written")

        loader = outcome.mutation["pixel_loader"]
        with sqlite3.connect(backend.db_path) as conn:
            row = conn.execute(
                "SELECT offset, length FROM instance_blobs "
                "WHERE instance_uid = ? AND kind = 'pixels'",
                (outcome.mutation["sop_uid"],)).fetchone()
        assert row is not None, "no blob row was committed under the new UID"
        assert (loader.offset, loader.length) == tuple(row) == (offset, length), (
            f"the mutation's loader is at {(loader.offset, loader.length)} "
            f"while the committed row is at {tuple(row)}: two answers to "
            "where the redacted frame lives (#368)")
    finally:
        backend.stop()
