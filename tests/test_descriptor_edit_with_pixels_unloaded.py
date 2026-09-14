"""A descriptor edit made with the pixels unloaded is honoured by the live session (#417).

`SidecarPixelLoader` captured Rows, Columns, SamplesPerPixel,
NumberOfFrames, BitsAllocated, PixelRepresentation and the
`_ISOCENTER_PIXEL_DTYPE` carrier **once**, when it was built, and every
later read rebuilt the frame from that capture rather than from
`instance.attributes`. So after `set_attr` on an unloaded instance:

* a PixelRepresentation 0 -> 1 edit still read as `uint16`;
* a Rows/Columns 4x4 -> 2x8 edit still read as (4, 4), and `export()`
  wrote Rows 4 / Columns 4 back out -- the edit silently reverted;
* an edit the stored bytes cannot satisfy (BitsAllocated 16 -> 8) read
  and exported as though it had not happened.

The same store, reopened, honoured every one of those edits or refused
it, because a reopened session builds its loader from the attributes as
they now are. The live session and the reopened one disagreed about the
same bytes. #417's issue text says the *exported file* carried a
container that did not match its bytes; measured, it did not -- the
export was consistent, and consistently wrong about the edit.

The fix compares the loader's capture with the instance on every read
and, when they differ, reads the same stored, hash-checked bytes through
a loader built from the instance as it is now. That loader is used for
the one read and **not stored back** (see
`test_the_stored_loader_object_is_never_replaced_by_a_read`).

The fixture's values are >= 32768 throughout. Below that a `uint16` and
an `int16` reading agree and every signedness comparison collapses to a
tautology; `min() < 0` is asserted separately for that reason. Every test
also asserts that the array is **not resident** before it reads, since a
resident array is handed back without consulting the loader and every
test would then pass on unfixed code.
"""
import contextlib
import copy
import glob
import hashlib
import os
import pickle
import sqlite3
import sys
import threading

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import entities as entities_module
from isocenter.entities import Equipment
from isocenter.io_handlers import ExportError, SidecarPixelLoader
from isocenter.services import RedactionError, RedactionOutcome, RedactionService
from isocenter.session import DicomSession
from isocenter.sidecar import SidecarManager

ROWS, COLS, PR, BITS = "0028,0010", "0028,0011", "0028,0103", "0028,0100"
ORIGINAL = (np.arange(16, dtype=np.uint16) + 40000).reshape(4, 4)


def _write_src(folder):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT417", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows, ds.Columns = ORIGINAL.shape
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.PixelData = ORIGINAL.tobytes()
    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)


def _only_instance(session):
    for pt in session.store.patients:
        for st in pt.studies:
            for se in st.series:
                for inst in se.instances:
                    return inst
    raise AssertionError("the fixture ingested no instance")


@pytest.fixture
def ingested(tmp_path):
    """An ingested, saved, *unloaded* instance and its session."""
    src = tmp_path / "src"
    src.mkdir()
    _write_src(str(src))
    db = str(tmp_path / "s.db")
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        session.save(sync=True)
        inst = _only_instance(session)
        assert isinstance(inst._pixel_loader, SidecarPixelLoader)
        assert inst.unload_pixel_data() is True
        assert inst.pixel_array is None
        yield session, inst, db
    finally:
        session.close()


def _reopened_read(db):
    """What a fresh session over the same store reads for the instance."""
    session = DicomSession(persistence_file=db)
    try:
        inst = _only_instance(session)
        assert inst.pixel_array is None
        return inst.get_pixel_data(), inst
    finally:
        session.close()


def _not_resident(inst):
    assert inst.unload_pixel_data() is True
    assert inst.pixel_array is None


# ---------------------------------------------------------------------------
# A1-A4 -- the live read honours the edit, before and after a save
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("save_between", [True, False],
                         ids=["saved", "unsaved"])
def test_a_pixel_representation_edit_reads_as_signed(ingested, save_between):
    """A1 (and A4 unsaved): the dtype is re-derived from the instance."""
    session, inst, db = ingested
    inst.set_attr(PR, 1)
    if save_between:
        session.save(sync=True)
    _not_resident(inst)

    got = inst.get_pixel_data()

    assert got.dtype == np.int16
    assert got.min() < 0
    assert np.array_equal(got, ORIGINAL.view(np.int16))
    if save_between:
        reopened, _ = _reopened_read(db)
        assert reopened.dtype == got.dtype
        assert np.array_equal(reopened, got)


@pytest.mark.parametrize("save_between", [True, False],
                         ids=["saved", "unsaved"])
def test_a_rows_and_columns_edit_reads_at_the_new_geometry(ingested,
                                                          save_between):
    """A2 (and A4 unsaved): geometry is compared, not only the dtype.

    Every other descriptor edit changes a compared field *other than*
    Rows/Columns, so a check that forgot geometry would still re-derive
    for them. This is the test that sees it.
    """
    session, inst, db = ingested
    inst.set_attr(ROWS, 2)
    inst.set_attr(COLS, 8)
    if save_between:
        session.save(sync=True)
    _not_resident(inst)

    got = inst.get_pixel_data()

    assert got.shape == (2, 8)
    assert got.dtype == np.uint16
    assert np.array_equal(got, ORIGINAL.reshape(2, 8))
    if save_between:
        reopened, _ = _reopened_read(db)
        assert reopened.shape == got.shape
        assert np.array_equal(reopened, got)


def test_an_edit_the_stored_bytes_cannot_satisfy_is_refused(ingested):
    """A3: BitsAllocated 16 -> 8 names 32 samples where 4x4 needs 16.

    The refusal is #373's Integrity Error, surfaced as `Pixel Loader
    failed` -- the same one a reopened session gives. No new channel.
    """
    session, inst, db = ingested
    inst.set_attr(BITS, 8)
    session.save(sync=True)
    _not_resident(inst)

    with pytest.raises(RuntimeError, match="Integrity Error") as live:
        inst.get_pixel_data()
    assert "Pixel Loader failed" in str(live.value)
    assert "holds 32 samples" in str(live.value)

    with pytest.raises(RuntimeError, match="Integrity Error"):
        _reopened_read(db)


# ---------------------------------------------------------------------------
# A5-A6 -- export, which runs in worker processes
# ---------------------------------------------------------------------------

def test_export_writes_the_edited_geometry(ingested, tmp_path):
    """A5: Rows and Columns are asserted, not only the pixel values.

    Before the fix the exported file was internally consistent -- Rows 4,
    Columns 4 and 16 samples -- because the writer takes the geometry
    from the array. So a pixel-only assertion that reshaped the output
    would pass on unfixed code; the header is where the reversion shows.
    """
    session, inst, _db = ingested
    inst.set_attr(ROWS, 2)
    inst.set_attr(COLS, 8)
    session.save(sync=True)
    _not_resident(inst)

    out = str(tmp_path / "out")
    session.export(out, show_progress=False, use_compression=False)

    files = glob.glob(os.path.join(out, "**", "*.dcm"), recursive=True)
    assert len(files) == 1
    written = pydicom.dcmread(files[0])
    assert written.Rows == 2
    assert written.Columns == 8
    assert written.pixel_array.shape == (2, 8)
    assert written.pixel_array.min() >= 32768
    assert np.array_equal(written.pixel_array, ORIGINAL.reshape(2, 8))


def test_export_refuses_an_edit_the_stored_bytes_cannot_satisfy(ingested,
                                                               tmp_path):
    """A6: the refusal reaches the export's ERROR row, not a clean file."""
    session, inst, db = ingested
    inst.set_attr(BITS, 8)
    session.save(sync=True)
    _not_resident(inst)

    with pytest.raises(ExportError):
        session.export(str(tmp_path / "out"), show_progress=False,
                       use_compression=False)
    session.store_backend.flush_audit_queue()

    conn = sqlite3.connect(db)
    try:
        errors = [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type='ERROR'")]
    finally:
        conn.close()
    assert any("Integrity Error" in d for d in errors), errors


# ---------------------------------------------------------------------------
# A7-A9 -- the mechanism: no store-back, no spurious rebuild, the hash
# ---------------------------------------------------------------------------

def test_the_stored_loader_object_is_never_replaced_by_a_read(ingested):
    """A7: a read that re-derives uses the fresh loader once and drops it.

    Behaviour cannot see a store-back -- the next read would re-derive to
    the same answer -- so identity is the only pin. It matters because
    `Session._apply_redaction_outcomes` rebinds `_pixel_loader` under
    `_pixel_swap_lock`; a read writing the same slot outside that lock
    can publish a stale loader after the redacted one, which is #274's
    shape (unredacted pixels under a full redaction attestation).
    """
    _session, inst, _db = ingested
    loader = inst._pixel_loader
    inst.set_attr(PR, 1)
    _not_resident(inst)
    assert not loader.describes(inst)

    got = inst.get_pixel_data()

    assert got.dtype == np.int16  # the read did need the rebuild
    assert inst._pixel_loader is loader, (
        "get_pixel_data() stored the rebuilt loader back on the instance; "
        "reads must not write `_pixel_loader` -- only "
        "`_apply_redaction_outcomes` does, under `_pixel_swap_lock` (#274)")


def test_an_edit_to_no_pixel_descriptor_rebuilds_nothing(ingested,
                                                         monkeypatch):
    """A8: the control. A SeriesDescription edit changes no reading.

    Asserted by counting `for_instance` calls, because a rebuild on
    every read returns the same array and no behavioural assertion can
    tell it from the fix.
    """
    session, inst, db = ingested
    calls = []
    real = SidecarPixelLoader.for_instance

    def counting(self, instance):
        calls.append(instance.sop_instance_uid)
        return real(self, instance)

    monkeypatch.setattr(SidecarPixelLoader, "for_instance", counting)

    inst.set_attr("0008,103e", "an edit to no pixel descriptor")
    assert inst._pixel_loader.describes(inst)
    got = inst.get_pixel_data()
    assert got.shape == (4, 4) and got.dtype == np.uint16
    assert np.array_equal(got, ORIGINAL)
    assert calls == []

    # And a reopened session's loader, built from the stored attributes,
    # describes its instance: no read after reopen rebuilds either.
    session.save(sync=True)
    reopened, reopened_inst = _reopened_read(db)
    assert reopened_inst._pixel_loader.describes(reopened_inst)
    assert np.array_equal(reopened, ORIGINAL)
    assert calls == []


def test_a_rebuilt_loader_carries_the_old_hash_verbatim(ingested):
    """A9: the bytes did not move, so the hash question does not either.

    Built from the instance, a loader falls back to `inst._pixel_hash`
    when it is handed no hash -- and `_pixel_hash` can drift from the
    bytes at this offset (it is set outside `if loader:` in
    `_apply_redaction_outcomes`). That fallback caused #212 once already.
    So `for_instance` copies the old loader's hash, `None` included, and
    never consults the instance.
    """
    session, inst, _db = ingested
    ingested_loader = inst._pixel_loader
    # The fixture guard. Since #436 the loader `ingest()` builds carries
    # the frame's hash (this asserted `is None` until then, which was the
    # defect itself). A drifted `_pixel_hash` on the instance must not
    # leak into a rebuild of it.
    assert ingested_loader.pixel_hash is not None
    assert ingested_loader.pixel_hash == inst._pixel_hash
    stored_hash = inst._pixel_hash
    inst._pixel_hash = "0" * 64
    assert ingested_loader.for_instance(inst).pixel_hash == stored_hash
    inst._pixel_hash = stored_hash

    # A loader with no hash rebuilds to one with no hash, however the
    # instance's `_pixel_hash` reads: the fallback is not reached.
    unhashed = copy.copy(ingested_loader)
    unhashed.pixel_hash = None
    assert unhashed.for_instance(inst).pixel_hash is None

    # A loader a save built, which does carry one; then a different
    # `_pixel_hash` on the instance, which must not leak in.
    inst.set_pixel_data((ORIGINAL + 1).astype(np.uint16))
    session.save(sync=True)
    hashed = inst._pixel_loader
    assert hashed.pixel_hash is not None
    inst._pixel_hash = "0" * 64
    assert hashed.for_instance(inst).pixel_hash == hashed.pixel_hash


def test_describes_names_every_field_the_loader_reads(ingested):
    """Each captured descriptor, one at a time, and the SOP Instance UID.

    The UID is compared so that after `regenerate_uid` the loader's
    Integrity Error names the UID the caller now knows the instance by.
    """
    _session, inst, _db = ingested
    loader = inst._pixel_loader
    assert loader.describes(inst)
    for tag, value in ((ROWS, 2), (COLS, 8), ("0028,0002", 3),
                       ("0028,0008", 2), (BITS, 8), (PR, 1)):
        before = inst.attributes.get(tag)
        inst.set_attr(tag, value)
        assert not loader.describes(inst), tag
        inst.set_attr(tag, before)
        assert loader.describes(inst), tag

    # The float carrier, which `set_attr` cannot reach (it lowercases).
    from isocenter.pixel_geometry import PIXEL_DTYPE_ATTR
    inst.attributes[PIXEL_DTYPE_ATTR] = "float32"
    assert not loader.describes(inst)
    del inst.attributes[PIXEL_DTYPE_ATTR]
    assert loader.describes(inst)

    # And `Instance.set_attr` reconciles a resident array on exactly the
    # tags the capture compares (#531): one the loader reads and the
    # reconciliation misses is written over resident pixels a save then
    # reloads another way. Every pixel-module tag and the carrier, each
    # asked of both. Written directly, so no reconciliation runs here.
    candidates = {"0028,%04x" % element for element in range(0x0200)}
    candidates |= {PIXEL_DTYPE_ATTR, "0008,0008", "7fe0,0010"}
    compared = set()
    for tag in sorted(candidates):
        had, before = tag in inst.attributes, inst.attributes.get(tag)
        inst.attributes[tag] = "float32" if tag == PIXEL_DTYPE_ATTR else 3
        if not loader.describes(inst):
            compared.add(tag)
        if had:
            inst.attributes[tag] = before
        else:
            del inst.attributes[tag]
        assert loader.describes(inst), tag
    assert compared == set(entities_module._LOADER_DESCRIBED_TAGS)

    original_uid = inst.sop_instance_uid
    inst.sop_instance_uid = generate_uid()
    assert not loader.describes(inst)
    assert loader.for_instance(inst).sop_instance_uid == inst.sop_instance_uid
    inst.sop_instance_uid = original_uid


class _TornAttributes(dict):
    """Attributes a concurrent writer flips between two valid layouts.

    Both 4x4 and 2x8 hold the stored 16 samples. Each `.get("0028,0010")`
    moves the dict to the other layout, which is what an
    `attributes.update(...)` on another thread does between two reads --
    deterministically, instead of 10% of the time on a free-threaded
    build. A reader that asks for Rows and then Columns separately gets
    one layout's Rows and the other's Columns: (4, 8) or (2, 4), which is
    neither, and the loader refuses it as an Integrity Error.
    """

    LAYOUTS = ({ROWS: 4, COLS: 4}, {ROWS: 2, COLS: 8})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._turn = 0

    def get(self, key, default=None):
        value = super().get(key, default)
        if key == ROWS:
            self._turn ^= 1
            dict.update(self, self.LAYOUTS[self._turn])
        return value


def test_the_descriptors_are_read_from_one_snapshot(ingested):
    """A read sees one layout or the other, never half of each.

    Measured on 3.14t with the GIL off, before the snapshot: a writer
    flipping `attributes.update(...)` between 4x4 and 2x8 made 10.3% of
    reads raise an Integrity Error. The stand-in forces the same tear
    deterministically; it does not reproduce a rate.
    """
    _session, inst, _db = ingested
    inst.attributes = _TornAttributes(inst.attributes)

    rows, cols = SidecarPixelLoader._descriptors_from(inst)[1:3]
    assert (rows, cols) in ((4, 4), (2, 8)), (rows, cols)

    got = inst.get_pixel_data()
    assert got.shape in ((4, 4), (2, 8))
    assert np.array_equal(got, ORIGINAL.reshape(got.shape))


# ---------------------------------------------------------------------------
# A10 -- a descriptor edit after a discard: one answer, whatever the save state
# ---------------------------------------------------------------------------

def test_a_descriptor_edit_after_a_discard_is_read_ungated(ingested):
    """A10: no gate on `_pixel_array_unwritten`.

    Since #434 `discard_pixel_data()` puts back the descriptors
    `set_pixel_data()` wrote, so a set -> discard no longer changes how
    the stored bytes are read (see the R tests below). What still needs
    no gate is a descriptor edit made *after* the discard, while the flag
    is still set: PixelRepresentation 0 -> 1 there must read as `int16`
    live, after an unload, after a save and reopened. A check filtered on
    "not unwritten" would read the first two as `uint16` and flip to
    `int16` after the save -- two answers to one question.
    """
    session, inst, db = ingested
    inst.set_pixel_data(inst.get_pixel_data().view(np.int16))
    assert inst.discard_pixel_data() is True
    assert inst.pixel_array is None
    # The fixture guard: the flag a gate would consult is still set, and
    # the discard put PixelRepresentation back to 0.
    assert inst._pixel_array_unwritten
    assert inst.attributes[PR] == 0
    inst.set_attr(PR, 1)

    reads = [inst.get_pixel_data()]
    _not_resident(inst)
    reads.append(inst.get_pixel_data())
    session.save(sync=True)
    _not_resident(inst)
    reads.append(inst.get_pixel_data())
    reopened, _ = _reopened_read(db)
    reads.append(reopened)

    for i, got in enumerate(reads):
        assert got.dtype == np.int16, i
        assert got.min() < 0, i
        assert np.array_equal(got, ORIGINAL.view(np.int16)), i


# ---------------------------------------------------------------------------
# R -- discard undoes the whole set_pixel_data(): pixels and descriptors (#434)
# ---------------------------------------------------------------------------
#
# Every tag `set_pixel_data()` can write. Spelled out here rather than
# imported from `entities`, so a tag dropped from the package's list is a
# difference these tests see rather than one they share.
SET_TAGS = ("0028,0010", "0028,0011", "0028,0002", "0028,0008", "0028,0004",
            "0028,0006", "0028,0100", "0028,0103", "_ISOCENTER_PIXEL_DTYPE")


def _descriptors(inst):
    """The set's tags as they stand, absence included (absent = not a key)."""
    return {t: inst.attributes[t] for t in SET_TAGS if t in inst.attributes}


REPLACEMENTS = {
    # PixelRepresentation 0 -> 1 over the same bytes: no error, and the
    # stored uint16 frame read as int16 [-25536, ...] (measured).
    "signed view": lambda inst: inst.get_pixel_data().view(np.int16),
    # Rows, Columns and BitsAllocated: the read raised an Integrity Error.
    "8x8 uint8": lambda inst: np.full((8, 8), 7, np.uint8),
    # NumberOfFrames, absent before: must be deleted, not zeroed.
    "three frames": lambda inst: np.zeros((3, 4, 4), np.uint16),
    # BitsAllocated 32 and the float carrier, absent before.
    "float32": lambda inst: np.full((4, 4), 1.5, np.float32),
    # SamplesPerPixel, PhotometricInterpretation, PlanarConfiguration.
    "rgb": lambda inst: np.zeros((4, 4, 3), np.uint8),
}


@pytest.mark.parametrize("kind", sorted(REPLACEMENTS))
def test_discard_restores_every_descriptor_the_set_wrote(ingested, kind):
    """R1: the descriptors from before the set, exactly -- and they stay so.

    Measured on c9e9938, a save after the discard wrote the set's
    descriptors over the stored frame, so a reopened session raised too:
    the damage was durable, not cosmetic.
    """
    session, inst, db = ingested
    before = _descriptors(inst)
    inst.set_pixel_data(REPLACEMENTS[kind](inst))
    # The fixture guard: this set did change what the tests compare.
    assert _descriptors(inst) != before

    assert inst.discard_pixel_data() is True
    assert inst.pixel_array is None
    assert _descriptors(inst) == before
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)
    # A set and a discard are still a change: no un-dirtying.
    assert inst.has_unsaved_changes

    session.save(sync=True)
    reopened, reopened_inst = _reopened_read(db)
    assert reopened.dtype == np.uint16 and np.array_equal(reopened, ORIGINAL)
    assert _descriptors(reopened_inst) == before


def test_discard_puts_back_a_float_carrier_the_set_removed(ingested):
    """R2: the carrier is uppercase, and `set_attr` lowercases.

    A restore through `set_attr` would write `_isocenter_pixel_dtype`, a
    ghost nothing reads, and leave the float frame decoding as integers.
    """
    session, inst, _db = ingested
    floats = np.full((4, 4), 1.5, np.float32)
    inst.set_pixel_data(floats)
    session.save(sync=True)
    before = _descriptors(inst)
    assert before["_ISOCENTER_PIXEL_DTYPE"] == "float32"

    inst.set_pixel_data(np.full((4, 4), 2, np.uint16))
    assert "_ISOCENTER_PIXEL_DTYPE" not in inst.attributes
    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == before
    assert "_isocenter_pixel_dtype" not in inst.attributes
    got = inst.get_pixel_data()
    assert got.dtype == np.float32 and np.array_equal(got, floats)


def test_a_second_set_keeps_the_first_record(ingested):
    """R3: two sets, one discard, back to before the first.

    The second set's prior values are the first set's writes, which
    describe pixels that were never stored.
    """
    _session, inst, _db = ingested
    before = _descriptors(inst)
    inst.set_pixel_data(inst.get_pixel_data().view(np.int16))
    inst.set_pixel_data(np.full((8, 8), 7, np.uint8))
    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == before
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)


@pytest.mark.parametrize("kind", ["new bytes", "same bytes, new dtype"])
def test_once_saved_there_is_nothing_to_discard(ingested, kind):
    """R4: a written replacement is the stored frame; discard keeps it.

    Two cases because `_persist_pixels` has two publishing arms: new
    bytes append a frame, and the same bytes under a new dtype take the
    dedup arm, which rebuilds the loader at the old offset. Each clears
    the record separately.
    """
    session, inst, _db = ingested
    offset = inst._pixel_loader.offset
    new = (np.full((8, 8), 7, np.uint8) if kind == "new bytes"
           else inst.get_pixel_data().view(np.int16))
    inst.set_pixel_data(new)
    after_set = _descriptors(inst)
    session.save(sync=True)
    # Which arm ran: only the dedup keeps the offset. Without this guard
    # both cases could pass through one arm and pin only that one.
    assert (inst._pixel_loader.offset == offset) == (kind != "new bytes")

    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == after_set
    got = inst.get_pixel_data()
    assert got.dtype == new.dtype and np.array_equal(got, new)


def test_a_pixel_swap_leaves_nothing_to_discard(ingested):
    """R5: `persist_pixel_data` (the redaction swap) publishes too."""
    session, inst, _db = ingested
    new = np.full((8, 8), 7, np.uint8)
    inst.set_pixel_data(new)
    after_set = _descriptors(inst)
    session.store_backend.persist_pixel_data(inst)

    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == after_set
    got = inst.get_pixel_data()
    assert got.dtype == np.uint8 and np.array_equal(got, new)


def test_a_redaction_rebind_leaves_nothing_to_discard(ingested):
    """R6: the processes-path rebind binds the redacted frame.

    After `_apply_redaction_outcomes` the loader reads the worker's frame
    and the *current* descriptors describe it, so a record left from the
    user's earlier set would restore descriptors over the wrong frame.
    Also #437: the rebind no longer hangs the parent's instance off the
    loader.
    """
    session, inst, _db = ingested
    inst.set_pixel_data(np.full((8, 8), 7, np.uint8))
    worker = pickle.loads(pickle.dumps(inst))
    redacted = np.full((8, 8), 5, np.uint8)
    worker.set_pixel_data(redacted)
    session.store_backend.persist_pixel_data(worker)
    uid = inst.sop_instance_uid

    _applied, failures = DicomSession._apply_redaction_outcomes(
        [RedactionOutcome(ok=True, sop_instance_uid=uid, mutation={
            "original_sop_uid": uid, "pixel_loader": worker._pixel_loader,
            "pixel_hash": worker._pixel_hash})],
        {uid: inst}, store_backend=session.store_backend)
    assert failures == []
    assert inst.pixel_array is None
    assert not hasattr(inst._pixel_loader, "instance")   # #437

    after = _descriptors(inst)
    assert np.array_equal(inst.get_pixel_data(), redacted)
    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == after
    assert np.array_equal(inst.get_pixel_data(), redacted)


@pytest.mark.parametrize("kind", ["8x8 uint8", "same geometry, new bytes"])
def test_a_discard_inside_a_save_leaves_the_instance_dirty(ingested, kind):
    """R8: a discard that lands while a save is writing the replacement.

    `_persist_pixels` read the array before the discard and publishes
    after it; its #274 revision guard is what stops it, and only if the
    discard moved the revision. The same-geometry case changes no
    descriptor, so a bump taken only when the restore changed something
    leaves the guard passing: the discarded replacement is published and
    the instance marked persisted.
    """
    session, inst, db = ingested
    before = _descriptors(inst)
    new = (np.full((8, 8), 7, np.uint8) if kind == "8x8 uint8"
           else (ORIGINAL + 1).astype(np.uint16))
    inst.set_pixel_data(new)
    sidecar = session.store_backend.sidecar
    real = sidecar.write_frame
    entered = []

    def discard_first(*args, **kwargs):
        sidecar.write_frame = real
        entered.append(1)
        assert inst.discard_pixel_data() is True
        return real(*args, **kwargs)

    sidecar.write_frame = discard_first
    session.save(sync=True)
    # Exactly one interception, and it was this instance's frame: the
    # fixture has one instance and no waveform or nested frame.
    assert entered == [1]
    assert inst.has_unsaved_changes
    assert _descriptors(inst) == before
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)

    session.save(sync=True)
    assert not inst.has_unsaved_changes
    reopened, reopened_inst = _reopened_read(db)
    assert np.array_equal(reopened, ORIGINAL)
    assert _descriptors(reopened_inst) == before


def test_a_descriptor_edit_between_the_set_and_the_discard_is_reverted_too(
        ingested):
    """R9 (#434, Q3): discard restores every recorded tag, whoever wrote it last.

    While the replacement is resident, a pixel-descriptor edit describes
    the replacement, and the discard throws the replacement away. Keeping
    the edit would leave PixelRepresentation 1 over a stored unsigned
    frame -- the read comes back signed, #434's own shape. So the edit
    goes with the set. A descriptor the set cannot write is not in the
    record and is left alone: BitsStored here.

    Rows 99 was this test's edit until #531, and is now refused before it
    is written: the set's 64 bytes cannot be read as 99 rows, and the
    declaration wins. PixelRepresentation 1 reads them as int8, so it is
    republished and recorded like any other edit.
    """
    _session, inst, _db = ingested
    before = _descriptors(inst)
    bits_stored = inst.attributes["0028,0101"]
    inst.set_pixel_data(np.full((8, 8), 200, np.uint8))
    attributes, revision = dict(inst.attributes), inst._revision
    with pytest.raises(ValueError, match=r"^Rows 99 would read the unsaved "):
        inst.set_attr(ROWS, 99)
    assert inst.attributes == attributes and inst._revision == revision
    inst.set_attr(PR, 1)
    inst.set_attr("0028,0101", 7)
    assert inst.attributes[PR] == 1
    assert inst.get_pixel_data().dtype == np.int8

    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == before
    assert inst.attributes[PR] == 0
    assert inst.attributes["0028,0101"] == 7 != bits_stored
    inst.set_attr("0028,0101", bits_stored)
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)


# ---------------------------------------------------------------------------
# Q6 -- a set that lands inside a publish stays unwritten (#434)
# ---------------------------------------------------------------------------
#
# Each publish section reads the resident array, writes or matches it, and
# then rebinds the loader and clears the unwritten flag. A
# `set_pixel_data()` landing between the read and the clear holds newer
# pixels the publish never saw; clearing the flag over them lets
# `unload_pixel_data()` drop the only copy (#293's shape). The injection is
# on the publishing thread, at the last call before the section's clears,
# which is the interleaving a second thread produces.

def test_a_set_inside_a_dedup_save_stays_unwritten(ingested):
    """The dedup arm had no revision guard, so it cleared the flag anyway."""
    session, inst, db = ingested
    store = session.store_backend
    inst.set_pixel_data(inst.get_pixel_data().view(np.int16))   # same bytes
    newer = np.full((4, 4), 3, np.uint16)
    real = store._create_pixel_loader
    entered = []

    def set_first(*args, **kwargs):
        store._create_pixel_loader = real
        entered.append(1)
        inst.set_pixel_data(newer)
        return real(*args, **kwargs)

    store._create_pixel_loader = set_first
    session.save(sync=True)
    assert entered == [1], "the dedup arm did not run"

    assert inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is False
    assert np.array_equal(inst.pixel_array, newer)
    session.save(sync=True)
    reopened, _ = _reopened_read(db)
    assert np.array_equal(reopened, newer)


def test_a_set_inside_a_pixel_swap_stays_unwritten(ingested):
    """`persist_pixel_data` cleared the flag whatever the array now was."""
    session, inst, db = ingested
    inst.set_pixel_data(np.full((4, 4), 5, np.uint16))
    newer = np.full((4, 4), 3, np.uint16)
    sidecar = session.store_backend.sidecar
    real = sidecar.write_frame
    entered = []

    def set_first(*args, **kwargs):
        sidecar.write_frame = real
        entered.append(1)
        inst.set_pixel_data(newer)
        return real(*args, **kwargs)

    sidecar.write_frame = set_first
    session.store_backend.persist_pixel_data(inst)
    assert entered == [1]

    assert inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is False
    assert np.array_equal(inst.pixel_array, newer)
    session.save(sync=True)
    reopened, _ = _reopened_read(db)
    assert np.array_equal(reopened, newer)


# ---------------------------------------------------------------------------
# Q6, at the lock -- a mutator landing as a publish asks for the leaf
# ---------------------------------------------------------------------------
#
# The two tests above inject on the publishing thread at the section's
# last call before the leaf. These inject *at* the leaf: a stand-in for
# `PIXEL_STATE_LOCK` runs the mutator, holding nothing, the moment a named
# publish section asks for the lock -- after the frame is appended and, in
# a tree that checks its guard outside the lock, after the guard. Only a
# check made under the lock sees it. A section is told apart by a local it
# has bound by then: `written` in `_persist_pixels`' new-write arm,
# `rebuilt` in its dedup arm, `swapped` in the swap. The first two tests
# are the #466 reviewer's, adapted.

class _ActOnAcquire:
    """`PIXEL_STATE_LOCK`, but it runs `act` once, first, when `function`
    asks for the lock with `local` bound."""

    def __init__(self, act, function, local):
        self._lock = threading.Lock()
        self._act = act
        self._function = function
        self._local = local
        self.fired = False

    def _asked_by_the_section(self):
        frame = sys._getframe(1)  # pylint: disable=protected-access
        while frame is not None:
            if frame.f_code.co_name == self._function:
                return self._local in frame.f_locals
            frame = frame.f_back
        return False

    def acquire(self, blocking=True, timeout=-1):
        if not self.fired and self._asked_by_the_section():
            self.fired = True
            self._act()   # before taking it: the mutator takes it itself
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def test_a_discard_after_the_append_is_seen_by_the_guard(ingested, monkeypatch):
    """The new-write arm checks its revision guard under the leaf (M1)."""
    session, inst, db = ingested
    before = _descriptors(inst)
    inst.set_pixel_data(np.full((8, 8), 7, np.uint8))
    proxy = _ActOnAcquire(inst.discard_pixel_data, "_persist_pixels", "written")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    session.save(sync=True)
    assert proxy.fired, "the new-write arm never asked for the leaf"
    monkeypatch.undo()

    assert _descriptors(inst) == before
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)
    session.save(sync=True)
    reopened, r_inst = _reopened_read(db)
    assert np.array_equal(reopened, ORIGINAL)
    assert _descriptors(r_inst) == before


def test_a_set_after_the_append_keeps_the_first_record(ingested, monkeypatch):
    """A skipped publish clears nothing, the record included (M17)."""
    session, inst, _db = ingested
    before = _descriptors(inst)
    inst.set_pixel_data(np.full((8, 8), 7, np.uint8))
    newer = np.full((2, 2), 3, np.uint16)
    proxy = _ActOnAcquire(lambda: inst.set_pixel_data(newer),
                          "_persist_pixels", "written")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    session.save(sync=True)
    assert proxy.fired, "the new-write arm never asked for the leaf"
    monkeypatch.undo()

    assert inst._pixel_array_unwritten
    assert inst.discard_pixel_data() is True
    assert _descriptors(inst) == before
    got = inst.get_pixel_data()
    assert got.dtype == np.uint16 and np.array_equal(got, ORIGINAL)


def _edit_in_place_and_set_again(inst, mine, value):
    def act():
        mine[...] = value
        inst.set_pixel_data(mine)
    return act


def test_an_edit_set_again_inside_a_dedup_save_stays_unwritten(
        ingested, monkeypatch):
    """The dedup arm checks the revision, not identity alone (M7).

    `set_pixel_data()` keeps a native-order array as given
    (`_accepted_pixel_array` copies only to fix byte order), so a caller
    can edit the array in place and set it again: the same object, new
    bytes. Landing after the dedup arm hashed it, that passes an identity
    check, and the flag was cleared over bytes the sidecar never held.
    """
    session, inst, db = ingested
    mine = ORIGINAL.copy()            # the stored bytes, so the save dedups
    inst.set_pixel_data(mine)
    assert inst.pixel_array is mine
    proxy = _ActOnAcquire(_edit_in_place_and_set_again(inst, mine, 3),
                          "_persist_pixels", "rebuilt")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    session.save(sync=True)
    assert proxy.fired, "the dedup arm never asked for the leaf"
    monkeypatch.undo()

    assert inst.pixel_array is mine
    assert inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is False
    session.save(sync=True)
    reopened, _ = _reopened_read(db)
    assert np.array_equal(reopened, np.full((4, 4), 3, np.uint16))


def test_an_edit_set_again_inside_a_pixel_swap_stays_unwritten(
        ingested, monkeypatch):
    """The swap checks the revision as well as identity (review of #466)."""
    session, inst, db = ingested
    mine = np.full((4, 4), 5, np.uint16)
    inst.set_pixel_data(mine)
    assert inst.pixel_array is mine
    proxy = _ActOnAcquire(_edit_in_place_and_set_again(inst, mine, 3),
                          "_swap_pixels_under_gate", "swapped")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    session.store_backend.persist_pixel_data(inst)
    assert proxy.fired, "the swap never asked for the leaf"
    monkeypatch.undo()

    assert inst.pixel_array is mine
    assert inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is False
    session.save(sync=True)
    reopened, _ = _reopened_read(db)
    assert np.array_equal(reopened, np.full((4, 4), 3, np.uint16))


class _ActOnRelease:
    """`PIXEL_STATE_LOCK`, but it runs `act` once, holding nothing, the
    first time `function` lets go of the lock."""

    def __init__(self, act, function):
        self._lock = threading.Lock()
        self._act = act
        self._function = function
        self.fired = False

    def _on_stack(self):
        frame = sys._getframe(1)  # pylint: disable=protected-access
        while frame is not None:
            if frame.f_code.co_name == self._function:
                return True
            frame = frame.f_back
        return False

    def acquire(self, blocking=True, timeout=-1):
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()
        if not self.fired and self._on_stack():
            self.fired = True
            self._act()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def test_unload_checks_and_drops_under_one_hold(ingested, monkeypatch):
    """No set can land between unload's check and its drop (M25).

    Deterministic, no timing: the set runs the moment unload first lets
    go of the lock. Held once, that is after the drop, and the set's
    array stays. Checked under one hold and dropped under a second, the
    set lands between them and the second hold drops it, unwritten.
    """
    _session, inst, _db = ingested
    inst.get_pixel_data()
    newer = np.full((4, 4), 3, np.uint16)
    proxy = _ActOnRelease(lambda: inst.set_pixel_data(newer),
                          "unload_pixel_data")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    assert inst.unload_pixel_data() is True
    assert proxy.fired, "unload never let go of the lock"
    monkeypatch.undo()

    assert inst.pixel_array is not None, "unload dropped a set it never checked"
    assert np.array_equal(inst.pixel_array, newer)
    assert inst._pixel_array_unwritten


# ---------------------------------------------------------------------------
# S -- a set that lands during a read is kept (#465)
# ---------------------------------------------------------------------------
#
# `get_pixel_data()` loads with no lock held -- a decode can take seconds,
# and the pixel-state lock is a leaf held for microseconds -- and then
# publishes. It used to publish unconditionally: a `set_pixel_data()`
# landing during the load was overwritten by the stale stored frame, and
# the flag clear marked the lost pixels written, so `unload_pixel_data()`
# dropped the only copy and the next save dedup'd against the stored
# frame. S1-S3 inject the set on the reading thread, inside the load: the
# interleaving a second thread produces, made deterministic (the issue's
# `SetDuringRead`). S5 is a real second thread.

class _SetDuringLoad:
    """A loader that runs a set on the reading thread, inside the load.

    No `describes`, so the read makes no #417 rebuild around it."""

    def __init__(self, inst, real, newer):
        self.inst, self.real, self.newer = inst, real, newer
        self.fired = False

    def __call__(self):
        if not self.fired:
            self.fired = True
            self.inst.set_pixel_data(self.newer)
        return self.real()


#: What lands during the load: the stored geometry with new samples, and
#: a geometry and depth the stored frame cannot be read under -- the
#: case the review of #466 measured leaving a 4x4 `uint16` array resident
#: under Rows 8 / BitsAllocated 8, and a store that then refused itself.
SET_DURING_READ = {
    "same geometry": np.full((4, 4), 3, np.uint16),
    "8x8 uint8": np.full((8, 8), 7, np.uint8),
}


@pytest.mark.parametrize("kind", sorted(SET_DURING_READ))
def test_a_set_landing_during_a_loader_read_is_kept(ingested, kind):
    """S1: the sidecar loader arm, through a save and a reopen."""
    session, inst, db = ingested
    newer = SET_DURING_READ[kind]
    real = inst._pixel_loader
    load = _SetDuringLoad(inst, real, newer)
    inst._pixel_loader = load
    try:
        got = inst.get_pixel_data()
    finally:
        inst._pixel_loader = real
    assert load.fired, "the set never ran inside the load"
    assert got is inst.pixel_array
    assert got.dtype == newer.dtype and np.array_equal(got, newer)
    assert inst._pixel_array_unwritten, "the set's pixels were marked written"
    assert inst.attributes[ROWS] == newer.shape[0]
    assert inst.unload_pixel_data() is False, "unload would drop the only copy"

    session.save(sync=True)
    assert not inst._pixel_array_unwritten
    reopened, again = _reopened_read(db)
    assert reopened.dtype == newer.dtype and np.array_equal(reopened, newer)
    assert again.attributes[BITS] == newer.itemsize * 8


def _file_backed(tmp_path):
    """A hand-built instance over `_write_src`'s file: no loader, so a
    read takes the file arm."""
    src = tmp_path / "file_arm"
    src.mkdir()
    _write_src(str(src))
    path = str(src / "one.dcm")
    ds = pydicom.dcmread(path)
    inst = entities_module.Instance(ds.SOPInstanceUID, ds.SOPClassUID, 1,
                                    file_path=path)
    for tag, value in ((ROWS, 4), (COLS, 4), ("0028,0002", 1),
                       ("0028,0004", "MONOCHROME2"), (BITS, 16), (PR, 0)):
        inst.set_attr(tag, value)
    assert inst._pixel_loader is None
    return inst


def test_a_set_landing_during_a_file_read_is_kept(tmp_path, monkeypatch):
    """S2: the file arm, the set injected inside pydicom's decode."""
    inst = _file_backed(tmp_path)
    newer = SET_DURING_READ["8x8 uint8"]
    real = entities_module._decode_from_file
    fired = []

    def set_during_decode(ds):
        fired.append(1)
        inst.set_pixel_data(newer)
        return real(ds)

    monkeypatch.setattr(entities_module, "_decode_from_file",
                        set_during_decode)
    got = inst.get_pixel_data()
    assert fired == [1], "the set never ran inside the decode"
    assert got is inst.pixel_array
    assert got.dtype == newer.dtype and np.array_equal(got, newer)
    assert inst._pixel_array_unwritten, "the set's pixels were marked written"
    assert (inst.attributes[ROWS], inst.attributes[BITS]) == (8, 8)
    assert inst.unload_pixel_data() is False, "unload would drop the only copy"


def test_a_relabelling_read_still_relabels_when_it_publishes(tmp_path,
                                                             monkeypatch):
    """S3: #482's relabel is made on the publishing branch, not dropped.

    The relabel moved into the publish when #465 folded it there; this is
    the read that publishes, so it must still say what the decode
    converted to.
    """
    inst = _file_backed(tmp_path)
    inst.set_attr("0028,0004", "YBR_FULL")
    real = entities_module._decode_from_file
    monkeypatch.setattr(entities_module, "_decode_from_file",
                        lambda ds: (real(ds)[0], "RGB"))
    before = inst._revision
    got = inst.get_pixel_data()
    assert got is inst.pixel_array and np.array_equal(got, ORIGINAL)
    assert inst.attributes["0028,0004"] == "RGB"
    assert inst._revision > before
    assert not inst._pixel_array_unwritten


def test_the_read_checks_the_slot_under_the_lock(ingested, monkeypatch):
    """S4: the check is made holding the leaf, not before taking it.

    The set runs the moment the publish asks for the lock, so only a
    check made once the lock is held sees it. A check made first and
    acted on inside -- `empty = self.pixel_array is None`, then `with
    PIXEL_STATE_LOCK: if empty:` -- publishes the stale frame over it.
    """
    _session, inst, _db = ingested
    newer = SET_DURING_READ["same geometry"]
    proxy = _ActOnAcquire(lambda: inst.set_pixel_data(newer),
                          "_publish_loaded_frame", "arr")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", proxy)
    got = inst.get_pixel_data()
    assert proxy.fired, "the read never asked for the pixel-state lock"
    monkeypatch.undo()
    assert got is inst.pixel_array and np.array_equal(got, newer)
    assert inst._pixel_array_unwritten


class _ParkInLoad:
    """A loader that parks inside the load until told to go on."""

    def __init__(self, real):
        self.real = real
        self.inside, self.go = threading.Event(), threading.Event()

    def __call__(self):
        self.inside.set()
        if not self.go.wait(30):
            raise AssertionError("the loader was never released")
        return self.real()


def test_a_set_on_another_thread_during_a_parked_load_is_kept(ingested):
    """S5: a real second thread; events, no timing."""
    _session, inst, _db = ingested
    newer = SET_DURING_READ["same geometry"]
    real = inst._pixel_loader
    park = _ParkInLoad(real)
    inst._pixel_loader = park
    got, errors = [], []

    def read():
        try:
            got.append(inst.get_pixel_data())
        except Exception as exc:  # pylint: disable=broad-except
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        assert park.inside.wait(30), "the reader never reached the load"
        inst.set_pixel_data(newer)
    finally:
        park.go.set()
        reader.join(30)
        inst._pixel_loader = real
    assert not reader.is_alive(), "the reader never finished"
    assert not errors, errors
    assert got[0] is inst.pixel_array and np.array_equal(got[0], newer)
    assert inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is False


def test_a_read_after_a_discard_publishes_under_a_set_flag(ingested):
    """S6: the predicate is the slot, not the flag.

    The unwritten flag stays set after a `discard_pixel_data()`, and the
    read that follows must publish the stored frame and clear it. A
    publish gated on the flag would refuse to cache here, and every read
    would go back to the store (A10 is the same question with a
    descriptor edit between).
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(SET_DURING_READ["same geometry"])
    assert inst.discard_pixel_data() is True
    assert inst.pixel_array is None
    assert inst._pixel_array_unwritten, "the fixture guard: still set"
    got = inst.get_pixel_data()
    assert got is inst.pixel_array and np.array_equal(got, ORIGINAL)
    assert not inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is True


# ---------------------------------------------------------------------------
# T -- the loader ingest builds carries the hash of its frame (#436)
# ---------------------------------------------------------------------------
#
# Two instances with constant frames, 5 and 9. Constant 4x4 uint16 frames
# compress to the same length, which `_swap_in` asserts, so writing B's
# stored bytes over A's decompresses cleanly at A's geometry: only the
# hash can tell the frames apart. The frames are the same length on
# purpose -- a length mismatch would be caught by the decompressor or the
# geometry check, and the test would pass without the hash.

def _write_constant(folder, name, value, patient_id):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = patient_id, "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.PixelData = np.full((4, 4), value, np.uint16).tobytes()
    ds.save_as(os.path.join(folder, name), enforce_file_format=True)
    return ds.SOPInstanceUID


def _instances_by_uid(session):
    return {inst.sop_instance_uid: inst
            for pt in session.store.patients for st in pt.studies
            for se in st.series for inst in se.instances}


@pytest.fixture
def pair(tmp_path):
    """Two ingested instances, *not saved*: A holds 5s, B holds 9s."""
    src = tmp_path / "src"
    src.mkdir()
    uid_a = _write_constant(str(src), "a.dcm", 5, "PA")
    uid_b = _write_constant(str(src), "b.dcm", 9, "PB")
    db = str(tmp_path / "pair.db")
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(str(src))
        by_uid = _instances_by_uid(session)
        yield session, by_uid[uid_a], by_uid[uid_b], db
    finally:
        session.close()


def _swap_in(a_loader, b_loader):
    """Overwrite A's stored frame with B's stored bytes, in place."""
    assert a_loader.length == b_loader.length, (
        "the fixture's frames must compress to one length, or the "
        "decompressor rather than the hash would refuse the swap")
    with open(b_loader.sidecar_path, "rb") as fh:
        fh.seek(b_loader.offset)
        b_bytes = fh.read(b_loader.length)
    with open(a_loader.sidecar_path, "r+b") as fh:
        fh.seek(a_loader.offset)
        fh.write(b_bytes)


def test_the_ingested_loader_carries_the_hash_of_its_frame(pair):
    """T1: straight after `ingest()`, before any save.

    `inst._pixel_hash` was always set; the loader beside it was built
    with no hash, so its integrity check never ran (#436).
    """
    _session, a, _b, _db = pair
    loader = a._pixel_loader
    assert isinstance(loader, SidecarPixelLoader)
    raw = SidecarManager(loader.sidecar_path).read_frame(
        loader.offset, loader.length, loader.alg)
    assert loader.pixel_hash == a._pixel_hash == hashlib.sha256(raw).hexdigest()


def test_a_tampered_frame_is_refused_right_after_ingest(pair):
    """T2: another instance's bytes at this offset are refused, not read.

    Measured on c9e9938: A read back as B's 9s, in the live session and
    after a save, with no error.
    """
    _session, a, b, _db = pair
    _not_resident(a)
    assert np.all(a.get_pixel_data() == 5)
    _not_resident(a)
    _swap_in(a._pixel_loader, b._pixel_loader)
    with pytest.raises(RuntimeError, match="hash mismatch"):
        a.get_pixel_data()



def test_a_tampered_frame_is_refused_after_a_reopen(pair):
    """T3: `load_all()` builds the loader with the stored `pixel_hash`.

    Measured on c9e9938, a reopened session did not check either: both
    hydration sites built the loader with no hash, and hydration never
    sets `inst._pixel_hash` for the loader's fallback to find. The row
    had the hash all along (`SELECT *` puts it in `r['pixel_hash']`).
    """
    session, a, b, db = pair
    session.save(sync=True)
    a_loader, b_loader, uid = a._pixel_loader, b._pixel_loader, a.sop_instance_uid
    session.close()
    _swap_in(a_loader, b_loader)

    reopened = DicomSession(persistence_file=db)
    try:
        a2 = _instances_by_uid(reopened)[uid]
        assert a2.pixel_array is None
        with pytest.raises(RuntimeError, match="hash mismatch"):
            a2.get_pixel_data()
    finally:
        reopened.close()


def test_load_patient_wires_the_stored_hash(pair):
    """T4: the second hydration site, which duplicates `load_all`'s."""
    session, a, _b, _db = pair
    session.save(sync=True)
    patient = session.store_backend.load_patient("PA")
    (loaded,) = [i for st in patient.studies for se in st.series
                 for i in se.instances]
    assert loaded._pixel_loader.pixel_hash == a._pixel_hash
    assert loaded._pixel_loader.pixel_hash is not None


# ---------------------------------------------------------------------------
# H -- a failed redaction swap leaves the hash of the frame the loader reads
# ---------------------------------------------------------------------------
#
# `_swap_pixels_under_gate` assigned `_pixel_hash` before `write_frame`, so
# an append that failed left the instance holding the hash of a frame that
# was never written, beside a loader still on the original. The next save's
# `arr is None` arm stores `_pixel_hash` with the loader's offset, and since
# Q1 (#436) a reopened session checks that hash: a correct frame read back
# as `Integrity Error: Pixel data hash mismatch`. `write_frame` fails once
# here, as a full disk or an EIO would.
#
# The save comes before any read, on purpose. A read makes the original
# resident, its digest misses the stale hash, and the save re-appends the
# frame under the right one -- which would pass on the defect.

def _fail_the_next_write(sidecar):
    real = sidecar.write_frame
    fired = []

    def fail_once(*_args, **_kwargs):
        sidecar.write_frame = real
        fired.append(1)
        raise OSError(5, "EIO injected")

    sidecar.write_frame = fail_once
    return fired


def _the_original_frame_survives(session, inst, db):
    digest = hashlib.sha256(ORIGINAL.tobytes()).hexdigest()
    assert inst.pixel_array is None
    session.save(sync=True)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        stored = [r[0] for r in conn.execute("SELECT pixel_hash FROM instances")]
    assert stored == [digest], "the row carries the hash of a frame never written"
    reopened, _ = _reopened_read(db)
    assert reopened.dtype == ORIGINAL.dtype
    assert np.array_equal(reopened, ORIGINAL)
    live = inst.get_pixel_data()
    assert live.dtype == ORIGINAL.dtype and np.array_equal(live, ORIGINAL)


def test_a_failed_swap_in_a_threads_redaction_keeps_the_frame_readable(
        ingested, monkeypatch):
    """H1: the threads path, where the worker swaps the live instance."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    session, inst, db = ingested
    for patient in session.store.patients:
        for study in patient.studies:
            for series in study.series:
                series.equipment = Equipment("M", "X", "SN1")
    inst.set_attr("0018,1000", "SN1")
    session.save(sync=True)
    assert inst.unload_pixel_data() is True
    session.configuration.rules = [
        {"serial_number": "SN1", "redaction_zones": [[0, 2, 0, 2]]}]
    fired = _fail_the_next_write(session.store_backend.sidecar)

    with pytest.raises(RedactionError):
        session.redact(show_progress=False)
    assert fired == [1], "the swap never reached its write"

    _the_original_frame_survives(session, inst, db)


def test_a_failed_swap_in_a_serial_redaction_keeps_the_frame_readable(ingested):
    """H2: `redact_machine_instances`, whose persist swaps."""
    session, inst, db = ingested
    fired = _fail_the_next_write(session.store_backend.sidecar)
    service = RedactionService(session.store, session.store_backend)

    # Raised since #474: this arm used to swallow the persist failure.
    # What the failed instance is left as is pinned in
    # `tests/test_redaction_failure_is_reported.py`; this test is about
    # the hash.
    with pytest.raises(RedactionError):
        service.redact_machine_instances(
            "SN1", [(0, 2, 0, 2)], targets=[inst], show_progress=False)
    assert fired == [1], "the swap never reached its write"

    _the_original_frame_survives(session, inst, db)


# ---------------------------------------------------------------------------
# F9 -- the loader names a message-less read failure (#435)
# ---------------------------------------------------------------------------

def test_a_read_failure_with_no_message_names_its_type(ingested, monkeypatch):
    """`Failed to read/decompress frame for <uid>: ` said nothing about why."""
    _session, inst, _db = ingested

    def refuse(*_args, **_kwargs):
        raise OSError()

    monkeypatch.setattr(SidecarManager, "read_frame", refuse)
    with pytest.raises(RuntimeError) as raised:
        inst.get_pixel_data()
    assert str(raised.value).endswith(
        f"Failed to read/decompress frame for {inst.sop_instance_uid}: OSError"), (
        str(raised.value))
