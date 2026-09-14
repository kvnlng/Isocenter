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


def test_a_read_in_flight_at_the_edit_is_re_read_under_the_new_declaration(
        ingested, monkeypatch):
    """A frame read under a capture the edit has since left is not published.

    The edit reconciles what is resident *at the edit*, and an empty slot
    is a plain write. A read that has already passed `describes()` and is
    inside `loader()` then publishes the frame it read under the old
    declaration into the empty slot, and it sticks: every later read
    returns it, and a save dedups the same bytes under the new
    descriptors, so the reopened store disagrees with the live session.
    Measured on PR #628's first push (5244480) with four reader threads
    and a writer flipping PixelRepresentation: 92 of 200 trials ended
    with the resident dtype against the declaration on 3.14t. The window
    is the written arm's own hand-off -- `unload_pixel_data()` empties
    the slot so that the next read rebuilds -- so it opens on every edit
    over written pixels.

    Driven with no race: the loader's `__call__` makes the edit just
    before it returns, which is the interleaving a second thread
    produces. The publish must see the capture it read through go stale
    and re-read, so the first read already answers as the declaration
    reads, and so do the saved and the reopened store.
    """
    session, inst, db = ingested
    real = SidecarPixelLoader.__call__
    fired = []

    def edit_in_flight(loader):
        arr = real(loader)
        if not fired:
            fired.append(1)
            inst.set_attr(PR, 1)
        return arr

    monkeypatch.setattr(SidecarPixelLoader, "__call__", edit_in_flight)

    got = inst.get_pixel_data()

    assert fired == [1], "the edit never landed inside the read"
    assert inst.attributes[PR] == 1
    _same(got, ORIGINAL.view(np.int16))
    assert inst.pixel_array is got
    _same(inst.get_pixel_data(), ORIGINAL.view(np.int16))
    session.save(sync=True)
    _same(_reopened_read(db), ORIGINAL.view(np.int16))


def test_a_stale_pass_re_reads_through_the_slot(ingested, monkeypatch):
    """The re-read after a stale publish goes through `_pixel_loader` as it is now.

    A redaction pass rebinds the slot (`_apply_redaction_outcomes`) to the
    redacted frame's loader. A read whose capture went stale re-reads
    through the slot rather than the loader it read through, or the
    rebound frame is published one pass late (review of #628 round 2, P1).
    Driven with no race: the in-flight hook makes the edit and rebinds
    the slot before returning, and the rebound loader's array is what
    publishes.
    """
    _session, inst, _db = ingested
    real = SidecarPixelLoader.__call__
    rebound = np.full((4, 4), 7, dtype=np.int16)
    fired = []

    def edit_and_rebind_in_flight(loader):
        arr = real(loader)
        if not fired:
            fired.append(1)
            inst.set_attr(PR, 1)
            inst._pixel_loader = lambda: rebound
        return arr

    monkeypatch.setattr(SidecarPixelLoader, "__call__", edit_and_rebind_in_flight)

    got = inst.get_pixel_data()

    assert fired == [1], "the edit never landed inside the read"
    assert got is rebound and inst.pixel_array is rebound


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
    load (#531's third row). The message names the tag, the dtypes and
    the byte counts, and neither the value nor anything a patient's file
    carried.
    """
    session, inst, db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    resident = inst.pixel_array
    attributes = dict(inst.attributes)
    revision = inst._revision

    with pytest.raises(ValueError) as refused:
        inst.set_attr(BITS, 8)

    assert str(refused.value) == (
        "BitsAllocated would read the unsaved uint16 (4, 4) array set by "
        "set_pixel_data() as 16 1-byte uint8 samples, 16 bytes, and the "
        "array holds 32. Pass the array you mean to set_pixel_data(), which "
        "writes its own descriptors.")
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
        f"{keyword} would read the unsaved uint16 (4, 4) array set by "
        f"set_pixel_data() as {samples} {itemsize}-byte {dtype} samples, "
        f"{samples * itemsize} bytes, and the array holds 32. Pass the "
        f"array you mean to set_pixel_data(), which writes its own "
        f"descriptors.")
    assert inst.attributes == attributes
    assert f"as {shape}" not in str(refused.value), "the target shape repeats the value"


@pytest.mark.parametrize("value", [PATIENT_NAME, [4], {"a": 1}],
                         ids=["a name", "a list", "a mapping"])
def test_an_unparseable_descriptor_over_an_unsaved_array_is_refused_without_its_text(
        ingested, value):
    """No reading, no write, and the refused value is not echoed.

    A descriptor that does not parse as an integer gives the loader
    nothing to read by, so a save would store a frame nothing can load.
    The value is left out of the message: it came from a caller and may
    be anything, and this text reaches audit rows. A list is what a
    pydicom MultiValue arrives as, and `int()` of one is a `TypeError`,
    not a `ValueError`; both are the same refusal.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError) as refused:
        inst.set_attr(ROWS, value)

    assert str(refused.value) == (
        "Rows does not parse as an integer, so the unsaved uint16 (4, 4) "
        "array set by set_pixel_data() has no reading under it. Pass the "
        "array you mean to set_pixel_data(), which writes its own "
        "descriptors.")
    assert inst.attributes == attributes


def test_an_edit_beside_a_descriptor_a_bypass_left_unparseable_names_that_descriptor(
        ingested):
    """The refusal blames the descriptor that does not parse, not the edit.

    A writer that goes straight to `attributes` can leave Rows holding
    text. An edit to PixelRepresentation then has no reading before or
    after it, and is refused -- not written, which would be a descriptor
    edit over unsaved pixels with no reconciliation at all -- and the
    refusal names Rows, which failed, rather than the tag that parsed.
    Review of #628, P1 and P5.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    inst.attributes[ROWS] = PATIENT_NAME
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError) as refused:
        inst.set_attr(PR, 1)

    assert str(refused.value) == (
        "Rows does not parse as an integer as it stands, so the unsaved "
        "uint16 (4, 4) array set by set_pixel_data() has no reading under "
        "the PixelRepresentation edit. Pass the array you mean to "
        "set_pixel_data(), which writes its own descriptors.")
    assert PATIENT_NAME not in str(refused.value)
    assert inst.attributes == attributes


# The loader's defaults, read from the loader: `_descriptors_of({})` is the
# six descriptors with nothing set, in the order `_descriptors_from` reads
# them. Not a second copy of a table -- a literal here pinned nothing
# (review of #628 round 2, F2: a table with BitsAllocated 0 passed).
_LOADER_READS_NOTHING_AS = dict(zip(
    (ROWS, COLS, SAMPLES, FRAMES, BITS, PR),
    SidecarPixelLoader._descriptors_of({})))


@pytest.mark.parametrize("tag", list(_LOADER_READS_NOTHING_AS))
def test_an_empty_descriptor_reads_as_the_loader_reads_nothing(tag):
    """An empty value reads as the loader's default for an absent one.

    Pins the `int(value or default)` rule the refusal's "reads as" clause
    relies on: the number it names is `_descriptors_of({})`'s, and this
    is what makes that the number an empty value is read as.
    """
    base = {ROWS: 4, COLS: 4, SAMPLES: 1, FRAMES: 1, BITS: 16, PR: 0}
    for empty in ("", b"", None):
        assert (SidecarPixelLoader.reading_of({**base, tag: empty})
                == SidecarPixelLoader.reading_of(
                    {**base, tag: _LOADER_READS_NOTHING_AS[tag]}))


@pytest.mark.parametrize("tag, keyword, reads", [
    (ROWS, "Rows", "0 2-byte uint16 samples, 0 bytes"),
    (COLS, "Columns", "0 2-byte uint16 samples, 0 bytes"),
    (BITS, "BitsAllocated", "16 1-byte uint8 samples, 16 bytes"),
], ids=["Rows", "Columns", "BitsAllocated"])
@pytest.mark.parametrize("value", ["", b"", None], ids=["str", "bytes", "None"])
def test_an_empty_descriptor_over_an_unsaved_array_is_refused_as_what_it_reads_as(
        ingested, tag, keyword, reads, value):
    """An `EMPTY` is told what the loader reads an empty value as.

    The size arithmetic was already the loader's own; a caller who wrote
    `""` was told about a `0` they never passed (review of #628, P2). The
    number named is the loader's own default, read from the loader, so a
    BitsAllocated row saying "reads as 0" over a reading of 8 is red here
    (round 2, F2). SamplesPerPixel, NumberOfFrames and PixelRepresentation
    read the same empty over this array and are plain writes.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError) as refused:
        inst.set_attr(tag, value)

    assert str(refused.value) == (
        f"An empty {keyword} reads as {_LOADER_READS_NOTHING_AS[tag]}, and "
        "would read the unsaved uint16 (4, 4) array set by set_pixel_data() "
        f"as {reads}, and the array holds 32. Pass the array you mean to "
        "set_pixel_data(), which writes its own descriptors.")
    assert inst.attributes == attributes


@pytest.mark.parametrize("value", [0, False], ids=["0", "False"])
def test_a_zero_bits_allocated_over_an_unsaved_array_is_refused_as_what_it_reads_as(
        ingested, value):
    """A falsy `0` is not empty, and the loader reads it as the default too.

    `int(attrs.get(tag, 8) or 8)` takes `0` and `False` to 8, so the
    reading is uint8 and the refusal was true about the reading and silent
    about the mapping (review of #628 round 2, P2). The clause names the
    mapping; the value is still not echoed -- "zero" is the class, and
    `"0"` (a string) reads as 0 and takes the plain branch.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError) as refused:
        inst.set_attr(BITS, value)

    assert str(refused.value) == (
        f"A zero BitsAllocated reads as {_LOADER_READS_NOTHING_AS[BITS]}, "
        "and would read the unsaved uint16 (4, 4) array set by "
        "set_pixel_data() as 16 1-byte uint8 samples, 16 bytes, and the "
        "array holds 32. Pass the array you mean to set_pixel_data(), "
        "which writes its own descriptors.")
    assert inst.attributes == attributes


@pytest.mark.parametrize("value", [1965, "1965"], ids=["int", "digits"])
def test_a_parsed_value_is_not_echoed_either(ingested, value):
    """The integer branch names sizes, and never the value.

    A `REPLACE` rule can carry a config- or patient-derived integer to a
    descriptor, and #560's VR check admits digits a US tag can hold, so
    this text reaches an audit row through `apply_remediation`. Neither
    branch echoes the value. Review of #628, P3.
    """
    _session, inst, _db = ingested
    inst.set_pixel_data(UNSIGNED.copy())

    with pytest.raises(ValueError) as refused:
        inst.set_attr(ROWS, value)

    assert "1965" not in str(refused.value), str(refused.value)
    assert str(refused.value).startswith("Rows would read the unsaved ")


def test_a_float_rows_edit_the_unsaved_array_cannot_satisfy_is_refused(ingested):
    """The carrier fixes the dtype; it does not exempt the geometry."""
    _session, inst, _db = ingested
    inst.set_pixel_data(np.zeros((4, 4), np.float32))
    attributes = dict(inst.attributes)

    with pytest.raises(ValueError, match=r"^Rows would read the unsaved "
                       r"float32 \(4, 4\) array .* as 8 4-byte float32 "
                       r"samples, 32 bytes, and the array holds 64\."):
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


@pytest.mark.parametrize("tag, new_value, expected, absent", [
    (BITS, 8, "BitsAllocated would read the unsaved uint16", "BitsAllocated 8"),
    (ROWS, 1965, "Rows would read the unsaved uint16", "1965"),
], ids=["BitsAllocated 8", "Rows 1965"])
def test_a_remediation_replace_on_a_descriptor_over_an_unsaved_array_is_declined(
        tmp_path, tag, new_value, expected, absent):
    """The refusal reaches a rule-driven write as a decline, with no patient text.

    `apply_remediation` catches a raising proposal and records it (#553);
    the decline names the action, the tag and the refusal, and the value
    stays. Nothing a patient's file carried is in the row, and neither is
    the value the rule carried: an integer a US tag can hold passes
    #560's VR check, so the second case is the one that reaches the row
    through the parsed branch.
    """
    store = SqliteStore(str(tmp_path / "declines.db"))
    try:
        inst = Instance("1.2.3.531", INSTANCE_SOP_CLASS, 1)
        inst.attributes["0010,0010"] = PATIENT_NAME
        inst.set_pixel_data(UNSIGNED.copy())
        original = inst.attributes[tag]
        finding = PhiFinding(
            entity_uid=inst.sop_instance_uid, entity_type="Instance",
            field_name=tag, value=original, reason="test", tag=tag,
            entity=inst,
            remediation_proposal=PhiRemediation(
                action_type="REPLACE_TAG", target_attr=tag,
                new_value=new_value, original_value=original, metadata={}))

        RemediationService(store_backend=store).apply_remediation([finding])
        declines = store.get_audit_declines()
    finally:
        store.stop()

    assert inst.attributes[tag] == original
    assert len(declines) == 1, declines
    details = declines[0][2]
    assert f"REPLACE_TAG on {tag} raised ValueError" in details, details
    assert expected in details, details
    assert PATIENT_NAME not in details and "DOE" not in details, details
    assert absent not in details.split("raised ValueError")[1], details


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


def test_the_capture_is_judged_under_the_lock_the_publish_holds(
        ingested, monkeypatch):
    """`describes()` is asked inside the publish's hold, not before it.

    The edit lands as the publish asks for the lock -- after the read,
    after any answer taken outside the hold. Judged inside, the capture
    is stale, nothing is published, and the read is made again under
    PixelRepresentation 1. Judged outside, the answer was current a
    moment ago, and the frame read unsigned is published under a
    declaration that reads it signed.
    """
    _session, inst, _db = ingested
    lock = _ActOnAcquire(lambda: inst.set_attr(PR, 1), "_publish_loaded_frame")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", lock)

    got = inst.get_pixel_data()

    assert lock.fired
    assert inst.attributes[PR] == 1
    _same(got, ORIGINAL.view(np.int16))
    assert inst.pixel_array is got


def test_the_written_arm_writes_inside_its_hold(ingested, monkeypatch):
    """The written arm's write is in the hold, and only the release is outside.

    A set of a uint8 array lands the moment the edit lets go of the lock.
    Written inside the hold, PixelRepresentation 1 is already there when
    the set writes its own descriptors over it, the release then refuses
    the set's unsaved array, and what is resident agrees with what is
    declared. Written after the hold, the stale edit overwrites the set's
    PixelRepresentation, the release still refuses, and the uint8 array
    stays resident under a declaration that reads it signed -- exactly
    the unrefused mismatch this override exists to remove. Review of
    #628, P6.
    """
    _session, inst, _db = ingested
    inst.get_pixel_data()
    assert not inst._pixel_array_unwritten
    newer = np.full((4, 4), 200, np.uint8)
    lock = _ActOnRelease(lambda: inst.set_pixel_data(newer), "set_attr")
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", lock)

    inst.set_attr(PR, 1)

    assert lock.fired
    assert inst.pixel_array is newer
    assert inst._pixel_array_unwritten
    assert (inst.attributes[PR], inst.attributes[BITS]) == (0, 8)


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
