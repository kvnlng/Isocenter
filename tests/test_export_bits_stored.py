"""The exported BitsStored is a width the pixel values fit (#468).

The integer branch of `_export_instance_worker` wrote `BitsStored` and
`HighBit` from a default of 8 or 16 when the instance declared none, and
from the declaration when it did, and neither was held against the
array being written beside them. Measured on 168fdd6, 3.12 and 3.14t:

- an `int32` array with no declared width left with BitsStored 16 beside
  32-bit bytes, and a reader got 27734 where -1103401898 was written;
- `int16 [-3024, 3000]` under a declared BitsStored 12 was written as
  declared, and every conformant reader masks a native sample to
  BitsStored, so it read back 1072 and -1096. Under JPEG 2000 the
  codestream carried the values exactly beneath a header claiming a
  width they exceed;
- a declared BitsStored 17 over 16-bit bytes was written, and pydicom
  refused to decode the file at all.

`verify_readback=True` caught the first two after the fact (#449); the
default export wrote each in silence.

Now the width is held against the array. Absent, it is the array's own
width. Declared, it is kept when every sample fits it, for the array's
own signedness; when a sample does not fit, or the declaration exceeds
BitsAllocated, the file gets `BitsStored = BitsAllocated` and
`HighBit = BitsStored - 1`, and the graph is not touched. Worker-level
arms call `_export_instance_worker` in this process, as
`tests/test_export_readback.py` does; the two public paths are checked
against each other at the end.
"""
import itertools
import logging
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.pixels import get_decoder

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   ExportOutcome, _export_instance_worker)
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)
BITS_STORED_12 = (("0028,0101", 12), ("0028,0102", 11))
BITS_STORED_7 = (("0028,0101", 7), ("0028,0102", 6))

_serial = itertools.count(1)


def _image(arr, attrs=(), *, before=(), sop=CT_STORAGE, modality="CT"):
    """A hand-built instance carrying `arr` and, after it, `attrs`.

    `attrs` go on after the pixels because `set_pixel_data()` rewrites
    BitsAllocated and PixelRepresentation from the array; `before` goes
    on first, for NumberOfFrames, which decides how the array's shape is
    read.
    """
    inst = Instance(f"1.2.826.0.1.468.{next(_serial)}", sop, 1)
    inst.file_path = None
    for tag, value in CT_REQUIRED:
        inst.set_attr(tag, value)
    inst.set_attr("0008,0060", modality)
    for tag, value in before:
        inst.set_attr(tag, value)
    inst.set_pixel_data(arr)
    for tag, value in attrs:
        inst.set_attr(tag, value)
    return inst


def _export(tmp_path, inst, **kwargs):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs))


def _stored(path):
    """The written file's samples as stored, no colour conversion."""
    ds = pydicom.dcmread(path)
    return get_decoder(ds.file_meta.TransferSyntaxUID).as_array(
        ds, as_rgb=False)[0]


def _width(path):
    ds = pydicom.dcmread(path)
    return (ds.BitsAllocated, ds.BitsStored, ds.HighBit,
            ds.PixelRepresentation)


def _assert_exact(outcome, arr):
    """The file decodes to `arr`, bit for bit, under its own descriptors."""
    assert outcome.ok, outcome.error
    read = _stored(outcome.output_path)
    written = arr.view(np.uint8) if arr.dtype == np.bool_ else arr
    assert read.dtype == written.dtype, (read.dtype, written.dtype)
    assert np.array_equal(read.reshape(-1), written.reshape(-1)), \
        (read.reshape(-1)[:4].tolist(), written.reshape(-1)[:4].tolist())


def _assert_widened(outcome, arr, inst, declared):
    """What an overflowed declaration comes out as.

    The one place that says what happens to a declared width the values
    do not fit, so the ruling on #468 lives in one helper: the file
    carries the array's own width, the samples read back exact, and the
    graph still says what it said.
    """
    allocated = arr.itemsize * 8
    assert _width(outcome.output_path)[:3] == (
        allocated, allocated, allocated - 1), _width(outcome.output_path)
    _assert_exact(outcome, arr)
    assert inst.attributes["0028,0101"] == declared, \
        "the export must not write the graph"
    # The rewrite travels back to the parent, which is the only process
    # whose log has a handler (see the logging test at the end).
    assert len(outcome.corrections) == 1, outcome.corrections
    assert f"BitsStored {declared} " in outcome.corrections[0], \
        outcome.corrections


# ---------------------------------------------------------------------------
# No declared width: the array's own.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arr, compression", [
    (np.array([[-128, 127, -1, 0]] * 4, np.int8), None),
    (np.array([[-128, 127, -1, 0]] * 4, np.int8), "j2k"),
    (np.array([[255, 0, 1, 2]] * 4, np.uint8), None),
    (np.array([[255, 0, 1, 2]] * 4, np.uint8), "j2k"),
    (np.array([[-32768, 32767, -1, 0]] * 4, np.int16), None),
    (np.array([[-32768, 32767, -1, 0]] * 4, np.int16), "j2k"),
    (np.array([[65535, 0, 1, 2]] * 4, np.uint16), None),
    (np.array([[65535, 0, 1, 2]] * 4, np.uint16), "j2k"),
    # 32-bit is uncompressed only: JPEG 2000 refuses it by name (#404).
    (np.array([[-1103401898, 2 ** 30, -1, 0]] * 4, np.int32), None),
    (np.array([[2 ** 32 - 1, 2 ** 31, 1, 0]] * 4, np.uint32), None),
], ids=["int8", "int8-j2k", "uint8", "uint8-j2k", "int16", "int16-j2k",
        "uint16", "uint16-j2k", "int32", "uint32"])
def test_an_undeclared_width_is_the_arrays_own(tmp_path, arr, compression):
    """BitsStored = BitsAllocated = itemsize * 8, HighBit one less (#468).

    The default was 8 for one-byte arrays and 16 for every other, so
    `int32` and `uint32` left under a 16-bit claim; the reader masked
    every sample to it. Killing mutation: the default back to 16 for
    any array wider than one byte.
    """
    outcome = _export(tmp_path, _image(arr), compression=compression)

    bits = arr.itemsize * 8
    assert _width(outcome.output_path) == (
        bits, bits, bits - 1, 1 if arr.dtype.kind == "i" else 0)
    _assert_exact(outcome, arr)


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_bool_mask_is_eight_bits_wide(tmp_path, compression):
    """`bool` is written one byte per sample, so its width is 8 (#468)."""
    mask = np.arange(16).reshape(4, 4) % 3 == 0

    outcome = _export(tmp_path, _image(mask, sop=SC_STORAGE, modality="OT"),
                      compression=compression)

    assert _width(outcome.output_path) == (8, 8, 7, 0)
    _assert_exact(outcome, mask)


# ---------------------------------------------------------------------------
# A declared width the values fit is kept, to the last value.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("arr, declared", [
    (np.array([[-2048, 2047, -1, 0]] * 4, np.int16), BITS_STORED_12),
    (np.array([[4095, 0, 1, 2]] * 4, np.uint16), BITS_STORED_12),
    (np.array([[-64, 63, -1, 0]] * 4, np.int8), BITS_STORED_7),
    (np.array([[127, 0, 1, 2]] * 4, np.uint8), BITS_STORED_7),
    # Equal to BitsAllocated: nothing can overflow, and the full range
    # of the dtype is exactly the declared width.
    (np.array([[-32768, 32767, -1, 0]] * 4, np.int16),
     (("0028,0101", 16), ("0028,0102", 15))),
], ids=["int16@12", "uint16@12", "int8@7", "uint8@7", "int16@16"])
def test_a_declared_width_the_values_fit_is_written_as_declared(
        tmp_path, arr, declared, compression):
    """Values exactly at the bound fit, for the array's own signedness (#468).

    -2048 and 2047 are the ends of a signed 12-bit range; 4095 is the end
    of the unsigned one. Killing mutations: the signed range applied to
    an unsigned array (4095 is past 2047); the bound off by one at either
    end; a fit declaration widened anyway.
    """
    outcome = _export(tmp_path, _image(arr, declared), compression=compression)

    bits = dict(declared)
    assert _width(outcome.output_path)[1:3] == (
        bits["0028,0101"], bits["0028,0102"])
    _assert_exact(outcome, arr)


# ---------------------------------------------------------------------------
# A declared width the values overflow.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("arr, declared", [
    (np.array([[-2049, 2047, -1, 0]] * 4, np.int16), BITS_STORED_12),
    (np.array([[-2048, 2048, -1, 0]] * 4, np.int16), BITS_STORED_12),
    (np.array([[-3024, 3000, -1, 0]] * 4, np.int16), BITS_STORED_12),
    (np.array([[4096, 0, 1, 2]] * 4, np.uint16), BITS_STORED_12),
    (np.array([[65535, 4096, 4095, 0]] * 4, np.uint16), BITS_STORED_12),
    (np.array([[-65, 63, -1, 0]] * 4, np.int8), BITS_STORED_7),
    (np.array([[128, 127, 0, 1]] * 4, np.uint8), BITS_STORED_7),
], ids=["int16 -2049@12", "int16 2048@12", "int16 issue@12",
        "uint16 4096@12", "uint16 issue@12", "int8 -65@7", "uint8 128@7"])
def test_a_value_one_past_the_declared_width_is_not_written_under_it(
        tmp_path, arr, declared, compression):
    """One past the bound, at either end, is an overflow (#468).

    Natively a conformant reader masks -2049 at BitsStored 12 to 2047
    and 4096 to 0; under JPEG 2000 the codestream is exact beneath a
    header that says otherwise. Neither file is written any more.
    Killing mutations: the bound check dropped; the upper bound off by
    one (2048 admitted at 12 bits); HighBit left as declared beside the
    widened BitsStored; the unsigned range applied to a signed array
    (-2049 is under 4095 in magnitude only).
    """
    inst = _image(arr, declared)

    outcome = _export(tmp_path, inst, compression=compression)

    _assert_widened(outcome, arr, inst, dict(declared)["0028,0101"])


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_an_overflow_in_one_frame_of_many_is_an_overflow(tmp_path, compression):
    """The bound is held over every frame, not the first (#468).

    Measured: frame 1 fits, frame 2 holds 3000 under BitsStored 12, and
    the native file read frame 2 back as -1096. Killing mutation: the
    check run over `arr[0]` only.
    """
    frames = np.stack([np.full((4, 4), 100, np.int16),
                       np.full((4, 4), 3000, np.int16)])
    inst = _image(frames, BITS_STORED_12, before=(("0028,0008", 2),))

    outcome = _export(tmp_path, inst, compression=compression)

    _assert_widened(outcome, frames, inst, 12)
    assert pydicom.dcmread(outcome.output_path).NumberOfFrames == 2


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_declared_width_wider_than_the_bytes_is_not_written(
        tmp_path, compression):
    """BitsStored 17 over 16-bit bytes is a file pydicom refuses (#468).

    Measured: `ValueError: A (0028,0101) 'Bits Stored' value of '17' is
    invalid ... no greater than the (0028,0100) 'Bits Allocated' value of
    '16'`. No value overflows a 17-bit range, so the value scan alone
    would keep it. Killing mutation: the `declared > allocated` arm
    dropped.
    """
    arr = np.array([[-32768, 32767, -1, 0]] * 4, np.int16)
    inst = _image(arr, (("0028,0101", 17), ("0028,0102", 16)))

    outcome = _export(tmp_path, inst, compression=compression)

    _assert_widened(outcome, arr, inst, 17)


def test_a_zero_declared_width_is_not_written(tmp_path):
    """BitsStored 0 holds no value at all (#468).

    A signed array, deliberately: its range is built with
    `1 << (declared - 1)`, and `1 << -1` raises, so a width below 1 has
    to be caught before the range is built or the export fails instead
    of widening. (An unsigned array would be caught by the value scan
    anyway -- an unsigned 0-bit range is 0..0 -- which is why that
    shape proves nothing here.) Killing mutation: the lower bound of
    the arm dropped.
    """
    arr = np.array([[1, 0, -1, 0]] * 4, np.int8)
    inst = _image(arr, (("0028,0101", 0), ("0028,0102", 0)))

    outcome = _export(tmp_path, inst)

    _assert_widened(outcome, arr, inst, 0)


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_readback_agrees_with_the_written_width(tmp_path, compression):
    """`verify_readback=True` on the issue's own instance now passes (#468).

    Before the fix this was the arm #449 measured: the native file
    failed its readback with `1072 read back where -3024 was written`.
    The readback holds the file against the array, so a width the
    values fit is a file that passes it.
    """
    arr = np.array([[-3024, 3000, -1, 0]] * 4, np.int16)

    outcome = _export(tmp_path, _image(arr, BITS_STORED_12),
                      compression=compression, verify_readback=True)

    assert outcome.ok, outcome.error
    assert _width(outcome.output_path)[:3] == (16, 16, 15)


@pytest.mark.parametrize("arr", [
    np.array([[2 ** 31, 0, 1, 2]] * 4, np.uint32),
    np.array([[-(2 ** 23) - 1, 2 ** 23 - 1, -1, 0]] * 4, np.int32),
], ids=["uint32 2**31@24", "int32 -(2**23)-1@24"])
def test_a_32_bit_overflow_widens_to_32_bits(tmp_path, arr):
    """A declared 24 the values overflow is written as 32, not 16 (#468).

    Uncompressed only: JPEG 2000 refuses 32-bit by name (#404). Every
    other overflow row is 8- or 16-bit, where "the array's own width"
    and "16" are the same number, so this is the row that tells them
    apart. Killing mutation (the review's O8): the widened width capped
    at 16 bits.
    """
    inst = _image(arr, (("0028,0101", 24), ("0028,0102", 23)))

    outcome = _export(tmp_path, inst)

    _assert_widened(outcome, arr, inst, 24)


@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("arr, declared, written", [
    # A kept BitsStored with a HighBit that is not one less than it.
    (np.array([[-2048, 2047, -1, 0]] * 4, np.int16),
     (("0028,0101", 12), ("0028,0102", 15)), (16, 12, 11)),
    (np.array([[-32768, 32767, -1, 0]] * 4, np.int16),
     (("0028,0101", 16), ("0028,0102", 11)), (16, 16, 15)),
    # A HighBit with no BitsStored beside it: 16/16/11 before #468.
    (np.array([[-32768, 32767, -1, 0]] * 4, np.int16),
     (("0028,0102", 11),), (16, 16, 15)),
], ids=["12/15", "16/11", "HighBit 11 alone"])
def test_high_bit_is_one_less_than_the_written_bits_stored(
        tmp_path, arr, declared, written, compression):
    """HighBit is written as BitsStored - 1 whenever BitsStored is (#468).

    The array holds right-aligned values, so its most significant stored
    bit is BitsStored - 1 whatever the source declared. A declared 12/15
    used to go to disk as declared, beside samples that are not
    left-aligned. Killing mutation (the converse of the review's O5): the
    declared HighBit read back onto the file.

    Since #597 the rewrite is not silent: one INFO correction names the
    declared and the written HighBit. It was `corrections == []` here.
    """
    outcome = _export(tmp_path, _image(arr, declared), compression=compression)

    assert _width(outcome.output_path)[:3] == written
    _assert_exact(outcome, arr)
    assert len(outcome.corrections) == 1, outcome.corrections
    assert "HighBit" in outcome.corrections[0], outcome.corrections


@pytest.mark.parametrize("value", ["12", " 12 "], ids=["'12'", "' 12 '"])
def test_a_numeric_string_bits_stored_is_that_integer(tmp_path, value):
    """`"12"` is a declared 12 (#468).

    Read by `declared_int`, as the geometry descriptors are, so a
    string a graph picked up from a buggy writer is still the one
    declaration it names, and is held against the array like any other.
    Killing mutation: the declaration read without `declared_int`'s
    coercion (a string compared against an int range raises).
    """
    arr = np.array([[-2048, 2047, -1, 0]] * 4, np.int16)
    inst = _image(arr, (("0028,0101", value), ("0028,0102", 11)))

    outcome = _export(tmp_path, inst)

    assert _width(outcome.output_path)[:3] == (16, 12, 11)
    _assert_exact(outcome, arr)
    assert outcome.corrections == []


@pytest.mark.parametrize("value", ["", [12], [12, 13], [], "twelve", None],
                         ids=["''", "[12]", "[12, 13]", "[]", "'twelve'",
                              "None"])
def test_a_bits_stored_that_is_not_one_integer_is_undeclared(tmp_path, value):
    """Anything `declared_int` does not read as an int is no declaration (#468).

    It gets the array's own width, as a missing one does, and says
    nothing: there is no width to have rewritten. `''` is the review's
    O7 arm, which failed the readback before #468. `[12]` is the
    review's regression: it was exported as 16/12/15 before #468, the
    first cut of this PR failed it with `int() argument must be ... not
    'list'`, and it is now read the way the geometry resolver reads a
    list-valued Rows -- as not declared -- rather than by a second, more
    lenient rule for this one descriptor. Killing mutations: a
    present-but-unreadable value treated as a declaration (the export
    fails, or widens with a note instead of in silence).
    """
    arr = np.array([[-2048, 2047, -1, 0]] * 4, np.int16)
    inst = _image(arr, (("0028,0101", value),))

    outcome = _export(tmp_path, inst)

    assert _width(outcome.output_path)[:3] == (16, 16, 15)
    _assert_exact(outcome, arr)
    assert outcome.corrections == []


# ---------------------------------------------------------------------------
# Both public paths.
# ---------------------------------------------------------------------------

def _graph(arrays):
    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    for arr, attrs in arrays:
        series.instances.append(_image(arr, attrs))
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _widths(root):
    return sorted(
        (p.name, _width(p)) for p in root.rglob("*.dcm"))


def test_both_public_export_paths_write_the_same_width(tmp_path):
    """`session.export()` and `DicomExporter.write_tree()` agree (#468).

    Both run `_export_instance_worker`, so this pins that the width rule
    is in the worker and not in one path's planning: the session path
    reads the array back through the sidecar loader, the serializer
    reads it from the graph, and the file is the same.
    """
    arrays = [
        (np.array([[-3024, 3000, -1, 0]] * 4, np.int16), BITS_STORED_12),
        (np.array([[-2048, 2047, -1, 0]] * 4, np.int16), BITS_STORED_12),
        (np.array([[-1103401898, 2 ** 30, -1, 0]] * 4, np.int32), ()),
    ]
    via_exporter = tmp_path / "via_exporter"
    DicomExporter.write_tree(_graph(arrays), str(via_exporter),
                             compression=None, show_progress=False)

    via_session = tmp_path / "via_session"
    with DicomSession(str(tmp_path / "w.db")) as session:
        session.store.patients.append(_graph(arrays))
        session.save()
        session.export(str(via_session), use_compression=False,
                       show_progress=False)

    exporter_widths = _widths(via_exporter)
    session_widths = _widths(via_session)
    assert len(exporter_widths) == 3, exporter_widths
    assert [w for _, w in exporter_widths] == [
        w for _, w in session_widths], (exporter_widths, session_widths)
    assert sorted(w[:3] for _, w in exporter_widths) == [
        (16, 12, 11), (16, 16, 15), (32, 32, 31)]


_LEVERS = ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
           "ISOCENTER_MAX_TASKS_PER_CHILD")


@pytest.mark.parametrize("path", ["session", "session-threads", "write_tree"])
def test_the_rewrite_is_logged_in_the_process_that_exported(
        tmp_path, caplog, monkeypatch, path):
    """One INFO line per widened instance, on the caller's `isocenter` log (#468).

    The ruling chose this line instead of an audit row, so it is the
    only record that a declared width was rewritten. It used to be
    logged inside the worker. `session.export()` always runs the worker
    in spawned processes (its `maxtasksperchild=25` pins them, with
    `ISOCENTER_FORCE_THREADS` set or not), and so does `write_tree()`
    on a GIL build. A spawned child's `isocenter` logger has no handler,
    so measured in the review of #506, 3 widened instances logged 0
    lines. The worker now returns the note on `ExportOutcome.corrections`,
    and the parent logs it, beside the losses.

    Read from `caplog` on the root logger, by channel, as
    `tests/test_shared_executor_lifecycle.py` reads its lines, because a
    session's `configure_logger()` resets the `isocenter` logger's own
    handlers. INFO exactly: the file is correct, so the line must not
    reach a WARNING filter or move the grade. Killing mutation (the
    review's O6): the parent's log call dropped.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if path == "session-threads":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    caplog.set_level(logging.INFO, logger="isocenter")

    arrays = [
        (np.array([[-3024, 3000, -1, 0]] * 4, np.int16), BITS_STORED_12),
        (np.array([[4096, 0, 1, 2]] * 4, np.uint16), BITS_STORED_12),
        (np.array([[-2048, 2047, -1, 0]] * 4, np.int16), BITS_STORED_12),
    ]
    graph = _graph(arrays)
    instances = graph.studies[0].series[0].instances
    widened = {instances[0].sop_instance_uid, instances[1].sop_instance_uid}

    out = tmp_path / "out"
    if path == "write_tree":
        DicomExporter.write_tree(graph, str(out), compression=None,
                                 show_progress=False)
    else:
        with DicomSession(str(tmp_path / "log.db")) as session:
            session.store.patients.append(graph)
            session.save()
            session.export(str(out), use_compression=False,
                           show_progress=False)

    lines = [r for r in caplog.records
             if r.name == "isocenter"
             and "cannot hold the pixel values" in r.getMessage()]
    assert len(lines) == 2, [r.getMessage() for r in lines]
    assert {r.levelno for r in lines} == {logging.INFO}
    assert {r.getMessage().split(":", 1)[0] for r in lines} == widened
    assert all("written with BitsStored 16 and HighBit 15" in r.getMessage()
               for r in lines), [r.getMessage() for r in lines]


def test_a_correction_is_logged_only_for_a_file_that_was_written(caplog):
    """No line for a failed outcome or a lost worker (#468).

    A correction describes a file. When the write failed after the
    width was chosen there is no file, and the failure has its own
    ERROR row. The parent's results can also hold a bare exception from
    a lost worker (#232), which has no `corrections` at all. Killing
    mutation: the `ok` filter dropped (the failed outcome's line
    appears, and the exception raises `AttributeError`).
    """
    caplog.set_level(logging.INFO, logger="isocenter")
    results = [
        ExportOutcome(ok=True, output_path="/o/a.dcm", sop_instance_uid="A",
                      corrections=["written note"]),
        ExportOutcome(ok=False, output_path="/o/b.dcm", sop_instance_uid="B",
                      corrections=["unwritten note"],
                      error=RuntimeError("disk full")),
        RuntimeError("worker lost"),
    ]

    logged = DicomExporter._report_export_corrections(results)

    lines = [r.getMessage() for r in caplog.records if r.name == "isocenter"]
    assert logged == 1
    assert lines == ["A: written note"], lines


# ---------------------------------------------------------------------------
# #597: a declared HighBit the export does not write is noted.
# ---------------------------------------------------------------------------

_FITS_12 = np.array([[0, 4095], [100, 2000]] * 2, np.uint16)


@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("arr, declared, high_bit, written", [
    (_FITS_12, (("0028,0101", 12), ("0028,0102", 15)), 15, (16, 12, 11)),
    (_FITS_12, (("0028,0101", 12), ("0028,0102", 10)), 10, (16, 12, 11)),
    (_FITS_12, (("0028,0102", 11),), 11, (16, 16, 15)),
    (np.array([[-2048, 2047], [0, -1]] * 2, np.int16),
     (("0028,0101", 12), ("0028,0102", 15)), 15, (16, 12, 11)),
], ids=["BS12/HB15", "BS12/HB10", "HB11 alone", "int16 BS12/HB15"])
def test_a_declared_high_bit_the_export_does_not_write_is_noted(
        tmp_path, arr, declared, high_bit, written, compression):
    """HighBit is rewritten to BitsStored - 1, and now it says so (#597).

    The export writes `HighBit = BitsStored - 1` (PS3.5 8.1.1), because
    the array holds right-aligned samples. A graph built by hand, or
    edited with `set_attr` after ingest, that declares another HighBit
    had it rewritten in silence. INFO on `corrections`, by ruling (Q7):
    the written file is conformant, the samples are unchanged, and an
    ingested file already carries ingest's own row for the declaration.
    The note says "written with" and names the written BitsStored, which
    for "HB11 alone" is one the caller never declared.

    Killing mutation (M21): the note block deleted.
    """
    outcome = _export(tmp_path, _image(arr, declared),
                      compression=compression, verify_readback=True)

    assert outcome.ok, outcome.error
    assert _width(outcome.output_path)[:3] == written
    assert outcome.warnings == [], outcome.warnings
    notes = [c for c in outcome.corrections if "HighBit" in c]
    assert len(notes) == 1, outcome.corrections
    assert f"HighBit {high_bit} " in notes[0], notes
    assert (f"written with BitsStored {written[1]} and HighBit "
            f"{written[2]}") in notes[0], notes


def test_a_widened_declaration_is_noted_once(tmp_path):
    """The widening note already names the written HighBit (#597).

    Values that overflow a coherent declared 12/11 are widened to 16/15
    with #468's note, which already says "written with BitsStored 16 and
    HighBit 15". The declared HighBit 11 is not the written one, so
    without the guard a second note would say the same thing twice.
    A declared 12/15 would not do: its HighBit is the widened one, and
    the guard is invisible there (measured: the mutant survived it).
    Killing mutation (M22): the `widened is None` guard dropped.
    """
    arr = np.array([[0, 65520], [1600, 32000]] * 2, np.uint16)
    outcome = _export(tmp_path, _image(
        arr, (("0028,0101", 12), ("0028,0102", 11))))

    assert outcome.ok, outcome.error
    assert _width(outcome.output_path)[:3] == (16, 16, 15)
    assert len(outcome.corrections) == 1, outcome.corrections
    assert "BitsStored 12 " in outcome.corrections[0], outcome.corrections


@pytest.mark.parametrize("declared", [
    (("0028,0101", 16), ("0028,0102", 15)),
    (("0028,0101", 12), ("0028,0102", 11)),
    (),
], ids=["BS16/HB15", "BS12/HB11", "nothing declared"])
def test_an_agreeing_high_bit_is_not_noted(tmp_path, declared):
    """The control (#597). Killing mutation (M23): the `!=` guard dropped."""
    outcome = _export(tmp_path, _image(_FITS_12, declared))

    assert outcome.ok, outcome.error
    assert outcome.corrections == [], outcome.corrections
