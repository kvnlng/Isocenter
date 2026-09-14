"""A pixel-descriptor edit made with the pixels resident reads the same across a save (#531).

`get_pixel_data()` hands back a resident array before it consults
anything, so a `set_attr` on a descriptor the sidecar loader reads by --
Rows, Columns, SamplesPerPixel, NumberOfFrames, BitsAllocated,
PixelRepresentation -- left the array exactly as it was while the store
recorded the edit. The save wrote the array's bytes; the reload read them
under the edit. Measured on d721141 (3.12):

* an int16 array set, then `set_attr(PixelRepresentation, 0)`: resident
  int16 `[-1, -2, -3, 4]`, reloaded, exported and reopened uint16
  `[65535, 65534, 65533, 4]`;
* a uint16 array set, then PixelRepresentation 1: the mirror;
* a uint16 array set, then BitsAllocated 8: the reload raises an
  Integrity Error and export writes 0 of 1;
* a resident array a save had already written, then PixelRepresentation
  0: resident int16, reloaded uint16.

#417 closed the same split for an *unloaded* instance, at the read. #531
is the resident half, and the owner's ruling (Q9) is that **the
declaration wins**: a written array is dropped so the next read is
#417's rebuild, an unsaved array is republished as the declaration reads
its bytes, and an edit those bytes cannot satisfy is refused before it is
written.

Values are chosen so that a signed and an unsigned reading disagree: a
test whose values sit below 32768 passes on the unfixed tree.
"""
import glob
import os
import sys
import threading

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import entities as entities_module
from isocenter.entities import DicomItem, Instance
from isocenter.io_handlers import SidecarPixelLoader
from isocenter.persistence import SqliteStore
from isocenter.pixel_geometry import PIXEL_DTYPE_ATTR
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

ROWS, COLS, SAMPLES, FRAMES = "0028,0010", "0028,0011", "0028,0002", "0028,0008"
BITS, PR = "0028,0100", "0028,0103"
ORIGINAL = (np.arange(16, dtype=np.uint16) + 40000).reshape(4, 4)
PATIENT_NAME = "DOE^JOHN531"
INSTANCE_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"


def _write_src(folder):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = INSTANCE_SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT531", PATIENT_NAME
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
        yield session, inst, db
    finally:
        session.close()


def _reopened_read(db):
    session = DicomSession(persistence_file=db)
    try:
        inst = _only_instance(session)
        assert inst.pixel_array is None
        return inst.get_pixel_data()
    finally:
        session.close()


def _exported_read(session, folder):
    session.export(folder, show_progress=False, use_compression=False)
    files = glob.glob(os.path.join(folder, "**", "*.dcm"), recursive=True)
    assert len(files) == 1, files
    return pydicom.dcmread(files[0]).pixel_array


def _same(got, expected):
    assert got.dtype == expected.dtype, (got.dtype, expected.dtype)
    assert got.shape == expected.shape, (got.shape, expected.shape)
    assert np.array_equal(got, expected)


# ---------------------------------------------------------------------------
# An unsaved array is republished as the declaration reads its bytes
# ---------------------------------------------------------------------------

SIGNED = (np.arange(16, dtype=np.int16) - 3).reshape(4, 4) * -1
UNSIGNED = np.flipud(ORIGINAL).copy()


@pytest.mark.parametrize("array, edit, expected", [
    (SIGNED, 0, SIGNED.view(np.uint16)),
    (UNSIGNED, 1, UNSIGNED.view(np.int16)),
], ids=["signed set, then unsigned", "unsigned set, then signed"])
def test_a_pixel_representation_edit_after_a_set_reads_the_same_live_after_save_and_reopened(
        ingested, tmp_path, array, edit, expected):
    """M24: every door gives one answer, and it is the declaration's.

    Resident, saved-and-reloaded, exported and reopened. On the unfixed
    tree the resident read kept the set's dtype and every later door read
    the edit, so the first assertion is the one that goes red.
    """
    session, inst, db = ingested
    assert expected.min() < 0 or expected.max() >= 32768
    inst.set_pixel_data(array.copy())
    assert inst.attributes[PR] == 1 - edit
    inst.set_attr(PR, edit)

    _same(inst.get_pixel_data(), expected)
    assert inst._pixel_array_unwritten, "the republish must stay unsaved"

    session.save(sync=True)
    assert not inst._pixel_array_unwritten
    assert inst.unload_pixel_data() is True
    _same(inst.get_pixel_data(), expected)
    _same(_exported_read(session, str(tmp_path / "out")), expected)
    _same(_reopened_read(db), expected)


@pytest.mark.parametrize("written_by", ["ingest", "a set since saved"])
def test_a_pixel_representation_edit_over_a_written_resident_array_reads_as_declared(
        ingested, written_by):
    """M25: a written array is released, and the next read is #417's rebuild.

    Its bytes are stored, so dropping it loses nothing; keeping it keeps
    the set's reading resident beside a declaration the store reads the
    other way.
    """
    session, inst, db = ingested
    if written_by == "ingest":
        stored, edit = ORIGINAL, 1
        inst.get_pixel_data()
    else:
        stored, edit = SIGNED, 0
        inst.set_pixel_data(SIGNED.copy())
        session.save(sync=True)
    assert inst.pixel_array is not None
    assert not inst._pixel_array_unwritten
    declared = stored.view(np.int16 if edit else np.uint16)

    inst.set_attr(PR, edit)

    assert inst.attributes[PR] == edit
    _same(inst.get_pixel_data(), declared)
    session.save(sync=True)
    _same(_reopened_read(db), declared)


def test_a_rows_edit_republishes_the_same_bytes_in_the_declared_shape(ingested):
    """The reshape half of the republish, from a descriptor a bypass wrote.

    A single-tag edit from a consistent state changes the element count
    whenever it changes the shape, and is refused. A shape the bytes fit
    is reached when another writer has already moved a descriptor without
    `set_attr` (#417's bypass writers): Columns 8 written directly, then
    Rows 2 through `set_attr`. Float32, so the carrier fixes the dtype and
    the geometry still follows the declaration.
    """
    session, inst, db = ingested
    floats = np.linspace(-2.0, 2.0, 16, dtype=np.float32).reshape(4, 4)
    inst.set_pixel_data(floats.copy())
    assert inst.attributes[PIXEL_DTYPE_ATTR] == "float32"
    inst.attributes[COLS] = 8

    inst.set_attr(ROWS, 2)

    _same(inst.get_pixel_data(), floats.reshape(2, 8))
    session.save(sync=True)
    assert inst.unload_pixel_data() is True
    _same(inst.get_pixel_data(), floats.reshape(2, 8))
    _same(_reopened_read(db), floats.reshape(2, 8))


def test_a_float_array_is_not_reinterpreted_by_a_pixel_representation_edit(
        ingested):
    """M30: the carrier, not PixelRepresentation, decides a float frame's dtype."""
    session, inst, db = ingested
    floats = np.linspace(-2.0, 2.0, 16, dtype=np.float32).reshape(4, 4)
    inst.set_pixel_data(floats)
    resident = inst.pixel_array

    inst.set_attr(PR, 1)

    assert inst.pixel_array is resident
    _same(inst.get_pixel_data(), floats)
    session.save(sync=True)
    _same(_reopened_read(db), floats)


# ---------------------------------------------------------------------------
# An edit the unsaved bytes cannot satisfy is refused before it is written
# ---------------------------------------------------------------------------

def test_a_bits_allocated_edit_the_unsaved_array_cannot_satisfy_is_refused_and_leaves_the_revision(
        ingested):
    """M26, M27: refused, in these words, with nothing written.

    Written, the edit saves a frame no later read, export or reopen can
    load (#531's third row). The message names tags, dtypes, shapes and
    byte counts, and nothing a patient's file carried.
    """
    session, inst, db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    resident = inst.pixel_array
    attributes = dict(inst.attributes)
    revision = inst._revision

    with pytest.raises(ValueError) as refused:
        inst.set_attr(BITS, 8)

    assert str(refused.value) == (
        "BitsAllocated 8 would read the unsaved uint16 (4, 4) array set by "
        "set_pixel_data() as (4, 4) uint8: 16 1-byte samples, 16 bytes, and "
        "the array holds 32. Pass the array you mean to set_pixel_data(), "
        "which writes its own descriptors.")
    assert inst.attributes == attributes
    assert inst._revision == revision
    assert inst.pixel_array is resident
    assert inst._pixel_array_unwritten
    session.save(sync=True)
    _same(_reopened_read(db), UNSIGNED)


@pytest.mark.parametrize("tag, value, keyword, shape, dtype", [
    (ROWS, 2, "Rows", (2, 4), "uint16"),
    (COLS, 8, "Columns", (4, 8), "uint16"),
    (SAMPLES, 3, "SamplesPerPixel", (4, 4, 3), "uint16"),
    (FRAMES, 2, "NumberOfFrames", (2, 4, 4), "uint16"),
    (BITS, 32, "BitsAllocated", (4, 4), "uint32"),
])
def test_every_described_descriptor_is_reconciled(ingested, tag, value,
                                                  keyword, shape, dtype):
    """M29, behaviourally: each tag the loader reads is one the edit checks.

    A tag dropped from the reconciliation is written without a word, and
    the saved frame then reloads as an Integrity Error.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    attributes = dict(inst.attributes)
    samples = int(np.prod(shape))
    itemsize = np.dtype(dtype).itemsize

    with pytest.raises(ValueError) as refused:
        inst.set_attr(tag, value)

    assert str(refused.value) == (
        f"{keyword} {value} would read the unsaved uint16 (4, 4) array set "
        f"by set_pixel_data() as {shape} {dtype}: {samples} {itemsize}-byte "
        f"samples, {samples * itemsize} bytes, and the array holds 32. Pass "
        f"the array you mean to set_pixel_data(), which writes its own "
        f"descriptors.")
    assert inst.attributes == attributes


def test_an_unparseable_descriptor_over_an_unsaved_array_is_refused_without_its_text(
        ingested):
    """No reading, no write, and the refused value is not echoed.

    A descriptor that does not parse as an integer gives the loader
    nothing to read by, so a save would store a frame nothing can load.
    The value is left out of the message: it came from a caller and may
    be anything, and this text reaches audit rows.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError) as refused:
        inst.set_attr(ROWS, PATIENT_NAME)

    assert str(refused.value) == (
        "Rows does not parse as an integer, so the unsaved uint16 (4, 4) "
        "array set by set_pixel_data() has no reading under it. Pass the "
        "array you mean to set_pixel_data(), which writes its own "
        "descriptors.")
    assert inst.attributes == attributes


def test_a_float_rows_edit_the_unsaved_array_cannot_satisfy_is_refused(ingested):
    """The carrier fixes the dtype; it does not exempt the geometry."""
    _session, inst, _db = ingested
    inst.set_pixel_data(np.zeros((4, 4), np.float32))
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError, match=r"^Rows 2 would read the unsaved "
                       r"float32 \(4, 4\) array .* as \(2, 4\) float32: 8 "
                       r"4-byte samples, 32 bytes, and the array holds 64\."):
        inst.set_attr(ROWS, 2)
    assert inst.attributes == attributes


def test_a_two_step_geometry_edit_on_unsaved_pixels_is_refused_at_step_one(
        ingested):
    """Q9's trade-off, pinned so a change to it is a decision.

    BitsAllocated 8 then Columns 8 describes the set's 32 bytes as a 4x8
    uint8 frame, and would fit once both are written. Each edit is judged
    alone, so the first is refused. The way through is the one the message
    names: set the array you mean.
    """
    session, inst, db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    with pytest.raises(ValueError):
        inst.set_attr(BITS, 8)

    meant = UNSIGNED.reshape(-1).view(np.uint8).reshape(4, 8)
    inst.set_pixel_data(meant)
    assert (inst.attributes[BITS], inst.attributes[COLS]) == (8, 8)
    session.save(sync=True)
    _same(_reopened_read(db), meant)


def test_a_remediation_replace_on_a_descriptor_over_an_unsaved_array_is_declined(
        tmp_path):
    """The refusal reaches a rule-driven write as a decline, with no patient text.

    `apply_remediation` catches a raising proposal and records it (#553);
    the decline names the action, the tag and the refusal, and the value
    stays. Nothing a patient's file carried is in the row.
    """
    store = SqliteStore(str(tmp_path / "declines.db"))
    try:
        inst = Instance("1.2.3.531", INSTANCE_SOP_CLASS, 1)
        inst.attributes["0010,0010"] = PATIENT_NAME
        inst.set_pixel_data(UNSIGNED.copy())
        finding = PhiFinding(
            entity_uid=inst.sop_instance_uid, entity_type="Instance",
            field_name=BITS, value=16, reason="test", tag=BITS, entity=inst,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr=BITS, new_value=8,
                original_value=16, metadata={}))

        RemediationService(store_backend=store).apply_remediation([finding])
        declines = store.get_audit_declines()
    finally:
        store.stop()

    assert inst.attributes[BITS] == 16
    assert len(declines) == 1, declines
    details = declines[0][2]
    assert "REPLACE_TAG on 0028,0100 raised ValueError" in details, details
    assert "BitsAllocated 8 would read the unsaved uint16" in details, details
    assert PATIENT_NAME not in details and "DOE" not in details, details


# ---------------------------------------------------------------------------
# The lock: republished in the write's own hold, and never re-entered
# ---------------------------------------------------------------------------

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

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class _ActOnAcquire(_ActOnRelease):
    """`PIXEL_STATE_LOCK`, but it runs `act` once, holding nothing, the
    first time `function` asks for the lock."""

    def acquire(self, blocking=True, timeout=-1):
        if not self.fired and self._on_stack():
            self.fired = True
            self._act()
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()


def test_the_reinterpretation_publishes_under_the_pixel_state_lock(
        ingested, monkeypatch):
    """M28: the write and the republish in one hold.

    A set lands the moment the edit lets go of the lock. Published inside
    the hold, the set's array is what stays. Published after it, the
    reinterpretation of the *previous* array overwrites the set, and the
    set's pixels are gone without a word.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(SIGNED.copy())
    newer = np.full((4, 4), 60000, np.uint16)
    lock = _ActOnRelease(lambda: inst.set_pixel_data(newer), "set_attr")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", lock)

    inst.set_attr(PR, 0)

    assert lock.fired
    assert inst.pixel_array is newer
    assert inst.attributes[PR] == 0


def test_a_set_landing_as_the_edit_asks_for_the_lock_is_the_array_judged(
        ingested, monkeypatch):
    """The slot is re-read under the lock, not trusted from before it.

    A set of a uint8 array lands as the edit asks for the lock. Judged
    against the array read before the lock, the int16 reinterpretation of
    the old pixels is published over the set. Re-read, the edit is judged
    against the uint8 array, and PixelRepresentation 1 reads it as int8.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    newer = np.full((4, 4), 200, np.uint8)
    lock = _ActOnAcquire(lambda: inst.set_pixel_data(newer), "set_attr")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", lock)

    inst.set_attr(PR, 1)

    assert lock.fired
    _same(inst.pixel_array, newer.view(np.int8))


@pytest.mark.parametrize("helper", ["_write_int_if_changed",
                                    "_write_str_if_changed"])
def test_set_pixel_data_with_new_descriptors_does_not_reenter_the_reconciliation(
        monkeypatch, helper):
    """M29b, M29b': `set_pixel_data`'s descriptor writes skip the override.

    They run under `PIXEL_STATE_LOCK`, a plain `threading.Lock`, and the
    override takes that lock whenever pixels are resident: a helper that
    called `self.set_attr` would deadlock on its own hold. Driven twice:
    on a private lock, on a thread, with a deadline, so a deadlock fails
    here and poisons nothing else; and with `Instance.set_attr` spied, so
    a helper that reaches it is named even where no lock is contended
    (PhotometricInterpretation is not a described tag today).
    """
    # Each case writes exactly one descriptor, through the helper it names:
    # everything else the set could write is already what it would write.
    if helper == "_write_int_if_changed":
        label = "RGB"
        replacement = np.zeros((4, 4, 3), np.uint16)        # BitsAllocated 16
        written = (BITS, 16)
    else:
        label = "MONOCHROME2"
        replacement = np.ones((4, 4, 3), np.uint8)          # PI -> RGB
        written = ("0028,0004", "RGB")

    def resident_instance():
        inst = Instance("1.2.3.531.R", INSTANCE_SOP_CLASS, 1)
        inst.attributes.update({ROWS: 4, COLS: 4, SAMPLES: 3, BITS: 8, PR: 0,
                                "0028,0004": label, "0028,0006": 0})
        inst.pixel_array = np.zeros((4, 4, 3), np.uint8)
        return inst

    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", threading.Lock())
    inst = resident_instance()
    worker = threading.Thread(target=inst.set_pixel_data, args=(replacement,),
                              daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "set_pixel_data deadlocked on its own lock"
    assert inst.attributes[written[0]] == written[1]

    calls = []

    def spy(self, tag, value):
        calls.append(tag)
        DicomItem.set_attr(self, tag, value)

    inst = resident_instance()          # before the spy: __post_init__ writes UIDs
    monkeypatch.setattr(Instance, "set_attr", spy)
    inst.set_pixel_data(replacement)
    assert inst.attributes[written[0]] == written[1]
    assert calls == [], calls


def test_a_read_racing_a_descriptor_edit_does_not_republish_the_old_declaration(
        ingested, monkeypatch):
    """M29c: written first, released second.

    A read landing between the two finds the slot empty and loads through
    the loader. After the write, it loads under the edit. Released before
    the write, it loads under the old declaration and publishes that,
    and the edit then leaves it resident.
    """
    _session, inst, _db = ingested
    inst.get_pixel_data()
    real_unload = Instance.unload_pixel_data
    reads = []

    def unload_then_read(self):
        released = real_unload(self)
        if self is inst and not reads:
            reads.append(self.get_pixel_data())
        return released

    monkeypatch.setattr(Instance, "unload_pixel_data", unload_then_read)

    inst.set_attr(PR, 1)

    assert len(reads) == 1, "the edit released nothing"
    _same(inst.get_pixel_data(), ORIGINAL.view(np.int16))


def test_a_save_whose_dedup_arm_saw_the_old_array_converges_on_the_next(
        ingested):
    """A14: the republish is a new object and a revision, and a save sees both.

    The dedup arm captures the resident array and the revision, rebuilds
    the loader, and publishes only if neither moved. An edit landing
    inside it republishes the same bytes under a new dtype: that save
    leaves the flag set and the instance dirty, and the next one writes
    what the declaration reads.
    """
    session, inst, db = ingested
    store = session.store_backend
    inst.set_pixel_data(ORIGINAL.view(np.int16))            # stored bytes
    real = store._create_pixel_loader
    entered = []

    def edit_first(*args, **kwargs):
        store._create_pixel_loader = real
        entered.append(1)
        inst.set_attr(PR, 0)
        return real(*args, **kwargs)

    store._create_pixel_loader = edit_first
    session.save(sync=True)
    assert entered == [1], "the dedup arm did not run"
    assert inst._pixel_array_unwritten
    assert inst.has_unsaved_changes
    _same(inst.pixel_array, ORIGINAL)

    session.save(sync=True)
    assert not inst._pixel_array_unwritten
    assert not inst.has_unsaved_changes
    assert inst.unload_pixel_data() is True
    _same(inst.get_pixel_data(), ORIGINAL)
    _same(_reopened_read(db), ORIGINAL)


def test_an_edit_with_no_pixels_resident_is_a_plain_write(ingested):
    """No resident array, nothing to reconcile: #417's read does the rest."""
    _session, inst, _db = ingested
    assert inst.pixel_array is None
    inst.set_attr(PR, 1)
    assert inst.attributes[PR] == 1
    assert inst.pixel_array is None
