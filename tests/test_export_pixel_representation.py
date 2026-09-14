"""The exported PixelRepresentation is the signedness of the bytes (#499).

The integer branch of `_export_instance_worker` wrote
`ds.PixelRepresentation = inst.attributes.get("0028,0103", 0)` three
lines after `ds.PixelData = arr.tobytes()`, so a declared value that
disagreed with the array was written over the array's own bytes.
Measured on a50632d, 3.12.14 and 3.14.7t, in-process:

- `int16 [-1, -2, -3, 4]` declared PixelRepresentation 0 was written
  0/16/16/15, and a reader got `uint16 [65535, 65534, 65533, 4]`;
- the same array under JPEG 2000 read `uint16 [32767, 32766, 32765,
  32772]` -- the codestream is signed, so the header's lie moves every
  sample;
- `uint16 [65535, 0, 1, 2]` declared 1 read back `int16 [-1, 0, 1, 2]`.

`ExportOutcome.corrections` was empty in all four; `verify_readback=True`
failed all four (#449). So the opt-in check already had teeth and the
default export was the silent path.

The project had already ruled this twice in code: `set_pixel_data()`
writes `0028,0103` from `dtype.kind` (`entities.py`, #386), and
`_readback_pixel_mismatch` treats the array's dtype as the truth and
PixelRepresentation as the thing that is wrong about it (#449). Now the
writer does too: the value is derived from `arr.dtype.kind`, a
*declared* value that disagrees is handed back on
`ExportOutcome.corrections` for the parent to log at INFO, and the graph
is not touched. This is the capability class of the write-path ruling --
an exact, value-preserving correction of our own making -- so it takes
no audit row and does not move the grade.

Worker-level arms call `_export_instance_worker` in this process, as
`tests/test_export_bits_stored.py` does; the public paths are checked at
the end.
"""
import inspect
import itertools
import logging
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.pixels import get_decoder

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _export_instance_worker)
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
SIGNED = np.array([[-1, -2, -3, 4]] * 4, np.int16)
UNSIGNED = np.array([[65535, 0, 1, 2]] * 4, np.uint16)

_serial = itertools.count(1)


def _image(arr, attrs=(), *, sop=CT_STORAGE, modality="CT"):
    """A hand-built instance carrying `arr` and, after it, `attrs`.

    `attrs` go on after the pixels because `set_pixel_data()` rewrites
    PixelRepresentation from the array (#386) -- a declaration set first
    would be replaced, which is the order `tests/test_export_readback.py`
    already notes.

    **Written into `attributes`, not through `set_attr`.** Since #531 a
    `set_attr` on PixelRepresentation over resident pixels makes them
    read as the edit declares -- `int16 [-1, ...]` declared 0 becomes
    `uint16 [65535, ...]` at the edit -- or refuses a declaration that is
    not an integer, so it can no longer build the disagreement these
    tests hand the writer. A writer that edits `attributes` directly
    still can (#417 lists them), and the export's correction is what
    stands between that and the file.
    """
    inst = Instance(f"1.2.826.0.1.499.{next(_serial)}", sop, 1)
    inst.file_path = None
    for tag, value in CT_REQUIRED:
        inst.set_attr(tag, value)
    inst.set_attr("0008,0060", modality)
    inst.set_pixel_data(arr)
    for tag, value in attrs:
        inst.attributes[tag] = value
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


def _written(path):
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


# ---------------------------------------------------------------------------
# A declaration that disagrees with the array.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_signed_array_declared_unsigned_is_written_signed(
        tmp_path, compression):
    """PixelRepresentation 1 beside `int16` bytes, and the note says so (#499).

    The measured defect: declared 0 was written, and a reader got
    65535, 65534, 65533 for -1, -2, -3 uncompressed and 32767, 32766,
    32765 under JPEG 2000. Killing mutation: the derivation reverted to
    `inst.attributes.get("0028,0103", 0)`.
    """
    inst = _image(SIGNED, (("0028,0103", 0),))

    outcome = _export(tmp_path, inst, compression=compression)

    assert _written(outcome.output_path) == (16, 16, 15, 1)
    _assert_exact(outcome, SIGNED)
    assert len(outcome.corrections) == 1, outcome.corrections
    assert outcome.corrections[0] == (
        "PixelRepresentation 0 (unsigned) is not the signedness of int16 "
        "samples; written with PixelRepresentation 1 (signed), the array's "
        "own")


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_an_unsigned_array_declared_signed_is_written_unsigned(
        tmp_path, compression):
    """PixelRepresentation 0 beside `uint16` bytes (#499).

    The other direction, which is not symmetrical in what it costs a
    reader: declared 1 over `uint16 [65535, 0, 1, 2]` read back
    `int16 [-1, 0, 1, 2]`. Killing mutation: `1 if kind == 'i' else 0`
    replaced by the constant 1.
    """
    inst = _image(UNSIGNED, (("0028,0103", 1),))

    outcome = _export(tmp_path, inst, compression=compression)

    assert _written(outcome.output_path) == (16, 16, 15, 0)
    _assert_exact(outcome, UNSIGNED)
    assert len(outcome.corrections) == 1, outcome.corrections
    assert outcome.corrections[0] == (
        "PixelRepresentation 1 (signed) is not the signedness of uint16 "
        "samples; written with PixelRepresentation 0 (unsigned), the array's "
        "own")


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_bool_mask_is_written_unsigned(tmp_path, compression):
    """`bool` is unsigned, whatever a declaration says (#499).

    `bool` is the one `dtype.kind` that is neither `'i'` nor `'u'`, and
    it reaches the descriptor writes as `'b'` -- the `view(np.uint8)`
    that widens it for the encoder is inside `_compress_j2k`, below
    them. So the rule has to be "signed is `'i'`" and not "unsigned is
    `'u'`". Killing mutation: `kind == 'i'` to `kind != 'u'`, which
    writes PixelRepresentation 1 for a mask.
    """
    mask = np.arange(16).reshape(4, 4) % 3 == 0
    inst = _image(mask, (("0028,0103", 1),), sop=SC_STORAGE, modality="OT")

    outcome = _export(tmp_path, inst, compression=compression)

    assert _written(outcome.output_path) == (8, 8, 7, 0)
    _assert_exact(outcome, mask)
    assert len(outcome.corrections) == 1, outcome.corrections
    assert "of bool samples" in outcome.corrections[0], outcome.corrections


# ---------------------------------------------------------------------------
# A declaration that agrees, and none at all: no note.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arr, declared", [
    (SIGNED, 1), (UNSIGNED, 0),
    (np.array([[-128, 127, -1, 0]] * 4, np.int8), 1),
    (np.array([[255, 0, 1, 2]] * 4, np.uint8), 0),
], ids=["int16", "uint16", "int8", "uint8"])
def test_a_declaration_that_agrees_is_written_unchanged_and_notes_nothing(
        tmp_path, arr, declared):
    """Nothing was corrected, so nothing is reported (#499).

    Killing mutation: the note appended unconditionally, which would put
    an INFO line on every export this library makes.
    """
    inst = _image(arr, (("0028,0103", declared),))

    outcome = _export(tmp_path, inst)

    assert _written(outcome.output_path)[3] == declared
    _assert_exact(outcome, arr)
    assert outcome.corrections == []


@pytest.mark.parametrize("arr, expected", [(SIGNED, 1), (UNSIGNED, 0)],
                         ids=["int16", "uint16"])
def test_nothing_declared_derives_and_notes_nothing(tmp_path, arr, expected):
    """The array's own answer where there was no declaration is not a
    correction of anything (#468's rule, kept).

    Reached by deleting the tag `set_pixel_data()` writes, which is what
    a hand-built instance carrying no descriptors looks like by the time
    it gets here. Killing mutation: the "declared is None" guard dropped
    from the note, which reports a correction for an instance that
    declared nothing.
    """
    inst = _image(arr)
    del inst.attributes["0028,0103"]

    outcome = _export(tmp_path, inst)

    assert _written(outcome.output_path)[3] == expected
    _assert_exact(outcome, arr)
    assert outcome.corrections == []


@pytest.mark.parametrize("value", [[1], [1, 0], "", "x", None])
def test_a_declaration_that_is_not_one_integer_is_undeclared(tmp_path, value):
    """Anything `declared_int` does not read as an int is no declaration (#499).

    It gets the array's own answer and says nothing -- there is no
    declaration to have corrected. `[1]` is the case that matters: the
    old `.get()` handed pydicom a one-element list, which it unwraps for
    a US element, so the file said 1 over `uint16` bytes. It is read
    here the way `_stored_width` reads a list-valued BitsStored and the
    geometry resolver reads a list-valued Rows -- as not declared --
    rather than by a second, more lenient rule for this one descriptor
    (#506's ruling). Killing mutation: `declared_int` swapped back for
    `.get()`, which writes 1 for `[1]` and raises `TypeError` on `[1, 0]`.
    """
    inst = _image(UNSIGNED, (("0028,0103", value),))

    outcome = _export(tmp_path, inst)

    assert _written(outcome.output_path)[3] == 0
    _assert_exact(outcome, UNSIGNED)
    assert outcome.corrections == []


# ---------------------------------------------------------------------------
# What the correction must not touch.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
def test_the_export_does_not_write_the_graph(tmp_path, compression):
    """The worker describes the file it wrote; it does not edit the instance.

    The graph-purity rule every descriptor fix on this path keeps
    (#468, #470): a correction goes on `ds` and on `corrections`, never
    on `inst`. Killing mutation: an `inst.set_attr("0028,0103", ...)`
    added beside the `ds` write, which `tests/test_export_worker_graph_
    purity.py` also guards structurally.
    """
    inst = _image(SIGNED, (("0028,0103", 0),))

    outcome = _export(tmp_path, inst, compression=compression)

    assert outcome.ok, outcome.error
    assert inst.attributes["0028,0103"] == 0, \
        "the export must not write the graph"


def test_a_float_instance_still_carries_no_pixel_representation(tmp_path):
    """PS3.5 Section 8.2 forbids the element beside float pixels (#499).

    The float arm deletes BitsStored, HighBit and PixelRepresentation,
    and the derivation must stay inside the integer arm rather than
    moving above the split. Killing mutation: the derivation hoisted, so
    the float file regains an element the standard forbids.
    """
    arr = np.array([[1.5, -2.5, 3.0, 0.0]] * 4, np.float32)
    inst = _image(np.zeros((4, 4), np.uint16), sop=SC_STORAGE, modality="OT")
    inst.set_pixel_data(arr)
    inst.set_attr("0028,0103", 1)

    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert "PixelRepresentation" not in ds
    assert "BitsStored" not in ds
    assert "HighBit" not in ds


def test_a_corrected_file_passes_the_readback(tmp_path):
    """The opt-in check and the writer now agree (#499, #449).

    Measured before the fix: `verify_readback=True` failed all four
    disagreements, so an export that a caller asked to verify wrote
    nothing. It now succeeds, because the file it verifies is coherent.
    Killing mutation: the derivation dropped (the readback fails again
    and the outcome is not ok).
    """
    inst = _image(SIGNED, (("0028,0103", 0),))

    outcome = _export(tmp_path, inst, verify_readback=True)

    assert outcome.ok, outcome.error
    assert _written(outcome.output_path)[3] == 1
    _assert_exact(outcome, SIGNED)


# ---------------------------------------------------------------------------
# Both public paths, and the parent's log.
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


def _by_uid(root):
    return {p.stem: _written(p) for p in root.rglob("*.dcm")}


def test_write_tree_corrects_a_disagreeing_declaration(tmp_path):
    """The serializer path writes the array's signedness, per instance (#499).

    Asserted per UID rather than over a sorted list of values: the three
    instances here declare 0, 1, 1 and are written 1, 0, 1, and a sorted
    comparison of the two multisets is `[0, 1, 1]` either way -- it
    passes unchanged against the defect, which is the correct-by-accident
    shape this project hunts. Killing mutation: the derivation reverted
    to `.get("0028,0103", 0)`, which writes 0, 1, 1.
    """
    arrays = [(SIGNED, (("0028,0103", 0),)),
              (UNSIGNED, (("0028,0103", 1),)),
              (SIGNED, (("0028,0103", 1),))]
    graph = _graph(arrays)
    uids = [i.sop_instance_uid
            for i in graph.studies[0].series[0].instances]

    out = tmp_path / "via_exporter"
    DicomExporter.write_tree(graph, str(out), compression=None,
                             show_progress=False)

    written = _by_uid(out)
    assert [written[uid][3] for uid in uids] == [1, 0, 1], written


def test_a_saved_instance_reaches_the_writer_already_agreeing(tmp_path):
    """Why `session.export()` cannot reach this correction (measured).

    **Characterisation pin, no mutant** -- it passes on f3eee9f
    unchanged, and it is here because it is the reason the logging test
    below has no `session` arm.

    `SidecarPixelLoader` derives the dtype of a reloaded frame from the
    *declared* BitsAllocated and PixelRepresentation, so a save and
    reload resolves any disagreement in the declaration's favour, by
    reinterpreting the bytes: `int16 [-1, -2, -3, 4]` declared
    PixelRepresentation 0 comes back `uint16 [65535, 65534, 65533, 4]`.
    Whatever else that is, by the time the export worker sees such an
    instance the array and the declaration agree, so there is nothing
    for this fix to correct and the written file is self-consistent.
    `session.export()` saves and releases before it writes, so the
    pipeline always presents a reconciled instance; the writer-level
    defect is reachable through `write_tree()` on a hand-built graph, and
    through a write to `attributes` that bypasses `set_attr` beside a
    resident array. A `set_attr` after `set_pixel_data()` reached it
    too, until #531 made that edit reinterpret the resident array.
    """
    inst = _image(SIGNED, (("0028,0103", 0),))
    graph = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    series.instances.append(inst)
    study.series.append(series)
    graph.studies.append(study)

    out = tmp_path / "via_session"
    with DicomSession(str(tmp_path / "w.db")) as session:
        session.store.patients.append(graph)
        session.save()
        session.export(str(out), use_compression=False, show_progress=False)

    assert inst.get_pixel_data().dtype == np.uint16
    written = _by_uid(out)[inst.sop_instance_uid]
    assert written == (16, 16, 15, 0), written
    assert pydicom.dcmread(
        next(out.rglob("*.dcm"))).pixel_array[0, 0] == 65535


_LEVERS = ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
           "ISOCENTER_MAX_TASKS_PER_CHILD")


@pytest.mark.parametrize("threads", [False, True])
def test_the_correction_note_reaches_the_callers_log(
        tmp_path, caplog, monkeypatch, threads):
    """One INFO line per corrected instance, in the parent (#499, #126).

    The trap #506's review measured: a spawned child's `isocenter`
    logger has no handler, so a line logged in the worker reached no
    one -- 3 corrections, 0 lines. The note therefore travels on
    `ExportOutcome.corrections` and the parent logs it. INFO exactly:
    the file is correct, so this must not reach a WARNING filter or
    move the grade.

    `write_tree()` only, because the correction is unreachable through
    `session.export()` -- see the characterisation test above -- and
    `write_tree()` runs the worker in spawned processes on a GIL build,
    which is the arm that kills the mutant. Killing mutation: the note
    logged inside the worker instead of returned (0 lines on 3.12).
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    caplog.set_level(logging.INFO, logger="isocenter")

    arrays = [(SIGNED, (("0028,0103", 0),)),
              (UNSIGNED, (("0028,0103", 1),)),
              (SIGNED, (("0028,0103", 1),))]
    graph = _graph(arrays)
    instances = graph.studies[0].series[0].instances
    corrected = {instances[0].sop_instance_uid, instances[1].sop_instance_uid}

    DicomExporter.write_tree(graph, str(tmp_path / "out"), compression=None,
                             show_progress=False)

    lines = [r for r in caplog.records
             if r.name == "isocenter"
             and "is not the signedness of" in r.getMessage()]
    assert len(lines) == 2, [r.getMessage() for r in lines]
    assert {r.levelno for r in lines} == {logging.INFO}
    assert {r.getMessage().split(":", 1)[0] for r in lines} == corrected


def test_the_corrections_reporter_is_given_no_store_to_write_a_row_with():
    """The capability class of the write-path ruling, structurally (#499).

    An exact, value-preserving correction of this library's own making
    is a fact about us, not about the user's data, so it takes an INFO
    line and no audit row and must not move the grade to
    REVIEW_REQUIRED -- that is the prohibition class (#502, #479).
    `_report_export_losses` takes `store_backend` because a loss is
    audited; `_report_export_corrections` is handed no store at all, so
    a row is not merely absent but unavailable. Killing mutation: a
    `store_backend` parameter added to the corrections reporter and a
    row written through it, which grades a correct export
    REVIEW_REQUIRED.
    """
    losses = inspect.signature(DicomExporter._report_export_losses)
    corrections = inspect.signature(DicomExporter._report_export_corrections)

    assert "store_backend" in losses.parameters
    assert list(corrections.parameters) == ["results"], corrections
