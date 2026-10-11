"""A label the written transfer syntax does not admit is written, loudly (#502).

Four Photometric Interpretations named a colour space or a layout that
the syntax the file was written under cannot carry, and the export wrote
each one as declared, in silence. Measured on a50632d, 3.12.14 and
3.14.7t, over an 8x8x3 `uint8` array under Implicit VR Little Endian:
`YBR_ICT`, `YBR_RCT`, `YBR_PARTIAL_422` and `YBR_PARTIAL_420` were all
written as declared, the samples were bit-exact, `verify_readback=True`
passed every one, `get_audit_errors()` was empty and the report read
`PASS -- nothing recorded in sections 2 to 4 costs this run its PASS`.
A multi-valued label (`['YBR_ICT', 'RGB']`) was written as two values
of a VM 1 attribute and passed the readback too.

The ruling on the write path is warn and attempt the best output, not
refuse: there are formats this library can read and cannot write, and a
de-identified copy the user can fix beats no copy. So the label and the
samples are written unchanged and the run says what it could not
honour -- a `WARNING` audit row, which reaches the compliance report and
grades the run `REVIEW_REQUIRED` (#411, #479). This is the *prohibition*
class of that ruling: the source's own header is impossible under the
syntax being written, which is a fact about the user's data, unlike
#499's exact self-correction, which takes an INFO line and no row.

Why the label is preserved rather than relabelled, measured (the
reasoning is in the PR body, and these are the numbers):

- where a JPEG 2000 colour transform really was undone on the way in,
  ingest already stores `RGB` -- so an instance that reaches the writer
  still carrying `YBR_ICT`/`YBR_RCT` holds the source's own
  untransformed bytes, and `RGB` would be an invented claim;
- a genuinely packed 4:2:2 source cannot be ingested at all (refused,
  `8192 vs 12288 bytes`), so there is no subsampled population to be
  faithful to, and `YBR_FULL` would assert a full 0-255 range for
  samples PS3.3 C.7.6.3.1.2 says are 16-235.

The one refusal is the multi-valued label, and it is the ruling's own
exception: no single value can be chosen without inventing one, and such
a file -- carrying pixel data -- cannot be read back by this library at
all, so no output here would be honest.

**Scope: every arm.** The two arms that write a pixel element go through
`_write_pixel_geometry`. An instance with **no pixel element** never
reaches that function, and until #534 carried its declared `0028,0004`
to disk unexamined; the worker now judges that arm too, against the
syntax the file carries, with a remedy true of a file with no pixels,
and a multi-valued label there is warned about rather than refused
because such a file re-ingests. See the #534 section at the end.
"""
import itertools
import logging
import sqlite3
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.multival import MultiValue

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   ExportOutcome, _export_instance_worker,
                                   _photometric_warning, _PhotometricRefusal,
                                   _written_photometric)
from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
IMPLICIT_VR_LE = "1.2.840.10008.1.2"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
#: The four this issue is about, under a native syntax.
INADMISSIBLE = ("YBR_ICT", "YBR_RCT", "YBR_PARTIAL_422", "YBR_PARTIAL_420")
#: Y, Cb, Cr of one colour, so the samples are plausible for every label
#: under test and no assertion depends on them being implausible.
YBR = (100, 123, 214)

_serial = itertools.count(1)


def _image(label, *, samples=3, arr=None):
    """A hand-built colour instance declaring `label`."""
    if arr is None:
        arr = np.full((8, 8, samples), YBR[:samples], np.uint8)
    inst = Instance(f"1.2.826.0.1.502.{next(_serial)}", SC_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "OT"), ("0028,0002", samples)):
        inst.set_attr(tag, value)
    inst.set_pixel_data(arr)
    inst.set_attr("0028,0004", label)
    return inst


def _export(tmp_path, inst, **kwargs):
    return _export_instance_worker(ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / f"{inst.sop_instance_uid}.dcm"),
        patient_attributes={"0010,0010": "ANON", "0010,0020": "PAT1"},
        study_attributes={"0020,000d": "1.2.826.0.2.1"},
        series_attributes={"0020,000e": "1.2.826.0.3.1"},
        **kwargs))


def _graph(instances):
    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "OT", 1)
    series.instances.extend(instances)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def _grade(report):
    return [line.strip() for line in report.read_text().splitlines()
            if "Grade Basis" in line][0]


# ---------------------------------------------------------------------------
# The label and the bytes are written; the run says so.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", INADMISSIBLE)
def test_the_four_labels_are_written_as_declared_with_a_warning(
        tmp_path, label):
    """Best output plus a warning, per the write-path ruling (#502).

    The file exists, its label is the one the source declared, its
    samples are the array, and one sentence comes back on
    `ExportOutcome.warnings` naming the label and the syntax. Killing
    mutations: the `warnings` append dropped (silent, as before); the
    label rewritten to `RGB` or `YBR_FULL` (the rejected relabels -- the
    provenance measurements in the PR body are why neither is honest).
    """
    inst = _image(label)

    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert ds.PhotometricInterpretation == label
    assert ds.file_meta.TransferSyntaxUID == IMPLICIT_VR_LE
    assert np.array_equal(
        np.frombuffer(ds.PixelData, np.uint8)[:3], np.array(YBR, np.uint8))
    assert len(outcome.warnings) == 1, outcome.warnings
    warning = outcome.warnings[0]
    assert warning.startswith(
        f"PhotometricInterpretation '{label}' is not a label the transfer "
        f"syntax this file was written under admits"), warning
    assert IMPLICIT_VR_LE in warning, warning
    assert ("The label was written as declared, over the samples the "
            "instance held, and neither was changed.") in warning, warning
    assert outcome.corrections == [], outcome.corrections


@pytest.mark.parametrize("label", INADMISSIBLE)
def test_the_graph_is_not_touched_by_the_warning(tmp_path, label):
    """A warning describes the file; it does not edit the instance (#502).

    The graph-purity rule this path keeps (#468, #470, #499). Killing
    mutation: a `set_attr("0028,0004", ...)` added beside the warning.
    """
    inst = _image(label)

    _export(tmp_path, inst)

    assert inst.attributes["0028,0004"] == label


def test_the_remedy_sentence_differs_for_the_partial_labels(tmp_path):
    """A uniform remedy would be false for two of the four (#502).

    Compressing gets `YBR_ICT`/`YBR_RCT` a syntax that admits the label
    and applies the transform it names (#490, #516). It does nothing for
    `YBR_PARTIAL_*`, which no syntax this exporter writes admits, so
    that sentence offers only the relabel. Killing mutation: one shared
    sentence for all four.
    """
    transform = _export(tmp_path, _image("YBR_RCT")).warnings[0]
    subsampled = _export(tmp_path, _image("YBR_PARTIAL_422")).warnings[0]

    assert "use_compression=True" in transform, transform
    assert "multiple-component transform" in transform, transform
    assert "use_compression=True" not in subsampled, subsampled
    assert "subsampled layout" in subsampled, subsampled
    assert 'set_attr("0028,0004"' in transform and \
        'set_attr("0028,0004"' in subsampled


@pytest.mark.parametrize("label, expected", [
    ("YBR_FULL", "YBR_FULL"), ("RGB", "RGB"), ("HSV", "HSV"),
    ("PALETTE COLOR", "RGB"), ("YBR_FULL_422", "YBR_FULL"),
], ids=["ybr_full", "rgb", "hsv", "palette", "ybr_full_422"])
def test_no_warning_for_a_label_the_syntax_admits(tmp_path, label, expected):
    """The rule is not "warn about what I do not recognise" (#502).

    `HSV` is retired and is still a label an uncompressed file may
    carry, so it must not warn; `PALETTE COLOR` at three samples is
    already resolved to `RGB` and `YBR_FULL_422` to `YBR_FULL` (#470),
    and the check reads the label as *written*, so neither warns either.
    Killing mutation: the per-syntax table replaced by a list of the four
    labels this issue names, after which `HSV` warns; or the check
    reading `inst.attributes` instead of what reached `ds`, after which
    `YBR_FULL_422` and `PALETTE COLOR` warn about labels the file does
    not carry.
    """
    outcome = _export(tmp_path, _image(label))

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == expected
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("label", ["YBR_ICT", ["YBR_ICT", "RGB"]],
                         ids=["single", "multi-valued"])
def test_a_monochrome_export_warns_about_nothing(tmp_path, label):
    """One sample resolves to MONOCHROME2 whatever was declared (#502).

    The resolver answers before both halves of the check, so a mono
    instance cannot reach either however its label was spelled -- and
    that is why both halves read `ds` rather than `attributes`. The
    multi-valued arm is the one that matters: the file this export
    writes carries a single `MONOCHROME2`, is re-ingestible, and is
    exactly the best-effort output the ruling asks for, so refusing it
    on the strength of the declaration would be an over-refusal on the
    one path where refusal is Breaking.

    Killing mutations: either half reading `attributes.get("0028,0004")`
    instead of the written label (the multi-valued arm refuses a file
    that would have been fine; the single arm warns about a file
    labelled MONOCHROME2); the check moved above
    `_write_pixel_geometry`, which is the same two failures.
    """
    inst = _image(label, samples=1, arr=np.full((8, 8), 7, np.uint8))

    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == "MONOCHROME2"
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("label", ["YBR_ICT", "YBR_RCT"])
def test_ict_and_rct_under_jpeg_2000_warn_about_nothing(tmp_path, label):
    """JPEG 2000 admits both labels, and #516 keeps them (#502, #490).

    Measured on f3eee9f: a source already labelled `YBR_RCT`/`YBR_ICT`
    is encoded with the multiple-component transform and keeps its label,
    because its samples were inverse-transformed on the way in -- so
    under `...1.2.4.90` the label is true of the codestream and there is
    nothing to warn about. The J2K row of the table therefore admits
    `YBR_ICT` as well as `YBR_RCT`, which is the owner's ruling on #490
    and not the strict reading of `level=0`; a table narrowed to
    `YBR_RCT` alone would warn about this library's own output. Killing
    mutations: the check not keyed on the syntax being written (the
    compressed file warns too); the J2K row narrowed to `YBR_RCT`.
    """
    outcome = _export(tmp_path, _image(label), compression="j2k")

    assert outcome.ok, outcome.error
    ds = pydicom.dcmread(outcome.output_path)
    assert ds.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    assert ds.PhotometricInterpretation == label
    assert outcome.warnings == [], outcome.warnings


def test_the_label_is_judged_against_the_syntax_the_file_actually_carries(
        tmp_path):
    """One predicate for "is this file compressed", not two (#502 review,
    #605).

    `_finalize_dataset` ran `_compress_j2k` for `compression == 'j2k'`
    and for nothing else, and the worker's `written_syntax` had to key on
    the same comparison: read as truthiness it judged the label against
    the JPEG 2000 row while the file went out native, and `YBR_ICT` was
    written with no warning at all -- measured with `compression="rle"`.

    This test used to export with `"rle"` and assert the native warning.
    Since #605 there is no such export: every reader of `compression`
    asks `_compresses`, which refuses anything but None and `"j2k"`, so
    the invariant "judged against the syntax the file carries" is
    enforced by the predicate having one spelling, and the value that
    used to split the two is refused where the context is built. What
    is left to pin here is that refusal and the native half of the
    judgement. Killing mutations: `ExportContext` accepting `"rle"`;
    `written_syntax` fixed at JPEG 2000 (the native `YBR_ICT` goes
    silent).
    """
    with pytest.raises(ValueError, match="'j2k'"):
        _export(tmp_path, _image("YBR_ICT"), compression="rle")

    outcome = _export(tmp_path, _image("YBR_ICT"), compression=None)

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert written.file_meta.TransferSyntaxUID == IMPLICIT_VR_LE
    assert len(outcome.warnings) == 1, outcome.warnings
    assert IMPLICIT_VR_LE in outcome.warnings[0], outcome.warnings


def test_a_compressed_rgb_export_is_relabelled_and_warns_about_nothing(
        tmp_path):
    """#516's other case: `RGB` is transformed and relabelled `YBR_RCT`.

    This passes for a reason worth stating exactly, because the obvious
    reading of it is wrong. `_write_pixel_geometry` runs **before**
    `_compress_j2k`, so what the writer judges here is `RGB` against the
    J2K row -- admitted, hence silent -- and the relabel to `YBR_RCT`
    happens afterwards. The writer never sees the final label; the
    readback is the only reader that does (#507). Both labels are on
    that row, so the ordering is harmless, and this test is the boundary
    that says so rather than a mutant: a check reading the label after
    the encoder would pass here too. The mutant for that ordering is in
    `tests/test_readback_label_admissibility.py`.
    """
    outcome = _export(tmp_path, _image("RGB"), compression="j2k")

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == "YBR_RCT"
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("declared, notes", [
    (" ybr_ict ", 1), (["YBR_ICT"], 0)],
    ids=["padded-lowercase", "one-element-list"])
def test_a_padded_or_list_spelling_warns_too(tmp_path, declared, notes):
    """An odd spelling of an inadmissible label is still inadmissible (#502).

    Both of these reach the file as `YBR_ICT` and are warned about once.
    Until #532 the padded one was written as `' ybr_ict'` -- a CS
    right-stripped on read keeps its leading space and its case, so
    pydicom and `ingest()` refused the file -- and this test pinned that.
    It is written as the Code String it spells now, with one INFO
    correction saying so. The one-element list is a **characterisation**:
    pydicom unwraps it on assignment, the spelling is already defined,
    and there is no correction to note.
    """
    outcome = _export(tmp_path, _image(declared))

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == "YBR_ICT"
    assert len(outcome.warnings) == 1, outcome.warnings
    assert len(outcome.corrections) == notes, outcome.corrections


@pytest.mark.parametrize("declared", [" rgb ", "rgb", " RGB", ["RGB"]],
                         ids=["padded-lower", "lower", "padded", "list"])
def test_an_oddly_spelled_admitted_label_does_not_warn(tmp_path, declared):
    """An admitted label is not warned about for its spelling (#502).

    A false alarm in a compliance report costs the reader their trust in
    the true ones. Since #532 the worker writes the label upper-cased
    and stripped before the judgement, so this test no longer sees
    `_written_photometric`'s own normalization -- that killer is
    `test_the_judgement_normalizes_what_it_is_given` below.
    """
    outcome = _export(tmp_path, _image(declared))

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("declared", [" rgb ", "rgb", " RGB", "Rgb"])
def test_the_judgement_normalizes_what_it_is_given(declared):
    """`_written_photometric` is still the comparison's normalization (#502).

    The readback reads hand-built files, which the worker's #532
    normalization never touches, so the judgement keeps its own. Unit
    level because the worker no longer hands it an odd spelling.

    Killing mutation: `.strip().upper()` dropped from
    `_written_photometric` (every arm warns, naming a label that is not
    in the row).
    """
    assert _photometric_warning(
        _written_photometric(declared), IMPLICIT_VR_LE,
        has_pixels=True) is None


# ---------------------------------------------------------------------------
# The parent's half: the audit row and the grade.
# ---------------------------------------------------------------------------

_LEVERS = ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
           "ISOCENTER_MAX_TASKS_PER_CHILD")


@pytest.mark.parametrize("threads", [False, True])
def test_the_warning_reaches_the_audit_log_and_moves_the_grade(
        tmp_path, monkeypatch, threads):
    """One `WARNING` row per instance, and the run stops reading PASS (#502).

    The row is what makes a preserved false label honest rather than
    silent: `get_audit_errors()` selects `ERROR` and `WARNING`, the
    report renders both under "Exceptions & Errors", and section 4 costs
    the run its `PASS` (#479). Written in the parent, because the worker
    is usually a spawned process with no store handle and no log handler
    (#126) -- measured for `corrections` in the review of #506 as 0 rows
    from 3 workers. Killing mutations: the row written in the worker
    instead of returned on `warnings`; `action_type="DATA_LOSS"` or any
    string other than the frozen `WARNING` (#411).
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    instances = [_image("YBR_RCT"), _image("YBR_PARTIAL_420"),
                 _image("RGB")]
    report = tmp_path / "report.md"

    with DicomSession(str(tmp_path / "w.db")) as session:
        session.store.patients.append(_graph(instances))
        session.save()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))

    # `get_audit_errors()` returns (timestamp, action_type, details).
    warnings = [r for r in rows if "PhotometricInterpretation" in r[2]]
    assert len(warnings) == 2, rows
    assert {r[1] for r in warnings} == {"WARNING"}, warnings
    # One row per warned label, and none for the clean instance. The
    # `entity_uid` column is not in what `get_audit_errors()` returns --
    # `test_a_warning_is_logged_only_for_a_file_that_was_written` pins
    # that the row carries the right one.
    assert sorted(label for label in ("YBR_RCT", "YBR_PARTIAL_420", "RGB")
                  if any(f"'{label}'" in r[2] for r in warnings)) == [
        "YBR_PARTIAL_420", "YBR_RCT"], warnings
    assert "REVIEW_REQUIRED" in _grade(report), _grade(report)


def test_a_clean_export_still_grades_pass(tmp_path):
    """The control for the test above (#502).

    Without it, a mutant that writes a `WARNING` row for every export
    would pass the grade assertion. Killing mutation: the warning
    appended unconditionally.
    """
    report = tmp_path / "report.md"

    with DicomSession(str(tmp_path / "c.db")) as session:
        session.store.patients.append(_graph([_image("RGB")]))
        session.save()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        assert session.store_backend.get_audit_errors() == []
        session.generate_report(str(report))

    assert "PASS" in _grade(report), _grade(report)


def test_a_warning_is_logged_only_for_a_file_that_was_written(caplog):
    """No row for a failed outcome or a lost worker (#502).

    A warning describes a file the caller now has. When the write failed
    there is no file and the failure has its own `ERROR` row; a lost
    worker comes back as a bare exception with no `warnings` at all
    (#232). The mirror of `_report_export_corrections`' own rule.
    Killing mutation: the `ok` filter dropped, after which the failed
    instance's warning is audited and a lost worker raises
    `AttributeError` in the parent.
    """
    caplog.set_level(logging.WARNING, logger="isocenter")
    results = [
        ExportOutcome(ok=True, output_path="/o/a.dcm", sop_instance_uid="A",
                      warnings=["written warning"]),
        ExportOutcome(ok=False, output_path="/o/b.dcm", sop_instance_uid="B",
                      warnings=["unwritten warning"],
                      error=RuntimeError("disk full")),
        RuntimeError("worker lost"),
    ]

    class _Store:
        def __init__(self):
            self.rows = []

        def log_audit(self, **kwargs):
            self.rows.append(kwargs)

    store = _Store()
    reported = DicomExporter._report_export_warnings(results, store)

    assert reported == 1
    assert [r["details"] for r in store.rows] == ["written warning"]
    assert [r["action_type"] for r in store.rows] == ["WARNING"]
    assert [r["entity_uid"] for r in store.rows] == ["A"]
    assert [r.getMessage() for r in caplog.records
            if r.name == "isocenter"] == ["A: written warning"]


# ---------------------------------------------------------------------------
# The multi-valued label: the ruling's exception.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("label", [["YBR_ICT", "RGB"], ["RGB", "RGB"]],
                         ids=["two-labels", "same-label-twice"])
def test_a_multi_valued_label_is_refused_on_both_syntaxes(
        tmp_path, compression, label):
    """No output here would be honest, so there is none (#502).

    `['RGB', 'RGB']` is in the parameters so the refusal is pinned to
    the *arity* and not to the labels: two values of a VM 1 attribute is
    a file this library cannot read back at all, measured -- ingest
    refuses it before any label is examined. That is the same standard
    `_J2K_ENCODABLE_FRAMES` refuses a frame on, and it is why this one
    label is the exception to warn-and-write. Killing mutations: the
    arity clause dropped (the file is written, as it was before this
    change); the refusal downgraded to a warning (a file this library
    cannot re-ingest is delivered as the best output).
    """
    outcome = _export(tmp_path, _image(label), compression=compression)

    assert not outcome.ok
    assert isinstance(outcome.error, _PhotometricRefusal), outcome.error
    message = str(outcome.error)
    assert "PhotometricInterpretation (0028,0004) is a single value" in \
        message, message
    assert "this instance declares 2" in message, message
    assert not (tmp_path / "out").exists() or not list(
        (tmp_path / "out").glob("*.dcm"))


def test_session_export_audits_the_refusal_and_grades_it(tmp_path):
    """The refusal fails one instance and is recorded (#502).

    Raised inside the worker's own `try`, so it comes back as
    `ExportOutcome(ok=False)` and costs one instance rather than
    aborting the batch -- the shape `_J2kFrameRefusal` already has. The
    clean instance beside it is still delivered, which is the whole
    point of the ruling. Killing mutation: the refusal raised outside
    the worker's handler, after which nothing reaches disk.

    **Classified survivor, named rather than discovered later:** "the
    raise moved after `save_as`" is not killable by a disk assertion --
    the worker unlinks its temporary file on any raise and `output_path`
    is only populated by the `os.replace`, so "no file at the output
    path" holds for the original and for that mutant alike.
    """
    good, bad = _image("RGB"), _image(["YBR_ICT", "RGB"])
    report = tmp_path / "report.md"
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / "r.db")) as session:
        session.store.patients.append(_graph([good, bad]))
        session.save()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        rows = [str(tuple(r)) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))

    assert [p.stem for p in out.rglob("*.dcm")] == [good.sop_instance_uid]
    assert len(summary.failures) == 1, summary
    assert any("is a single value" in r and "ERROR" in r for r in rows), rows
    assert "REVIEW_REQUIRED" in _grade(report), _grade(report)


def test_write_tree_raises_on_the_refusal(tmp_path):
    """The serializer path's channel is unchanged (#502).

    `write_tree()` counts failures and raises `RuntimeError("Export
    incomplete. ...")`, as it does for a refused J2K frame. Killing
    mutation: the refusal swallowed into a warning, after which
    `write_tree` writes the file and raises nothing.
    """
    with pytest.raises(RuntimeError) as raised:
        DicomExporter.write_tree(_graph([_image(["YBR_ICT", "RGB"])]),
                                 str(tmp_path / "out"), compression=None,
                                 show_progress=False)

    assert "Export incomplete. 1 failed." in str(raised.value)
    assert "is a single value" in str(raised.value)


# ---------------------------------------------------------------------------
# Stability of the preserved label.
# ---------------------------------------------------------------------------

def test_the_exported_file_re_ingests_with_the_same_label(tmp_path):
    """Preserving the label does not drift or accumulate (#502).

    **Characterisation pin, no mutant:** it passes on f3eee9f
    unchanged, because today's export already preserves the label. It
    is here so that a later relabel to `RGB` or `YBR_FULL` has to be
    deliberate: pass 2 would differ from pass 1, and a user diffing an
    export against its source would see a colour claim change that no
    de-identification asked for.
    """
    out1 = tmp_path / "out1"
    DicomExporter.write_tree(_graph([_image("YBR_RCT")]), str(out1),
                             compression=None, show_progress=False)
    assert pydicom.dcmread(
        next(out1.rglob("*.dcm"))).PhotometricInterpretation == "YBR_RCT"

    out2 = tmp_path / "out2"
    with DicomSession(str(tmp_path / "ri.db")) as session:
        assert session.ingest(str(out1)).ingested == 1
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.attributes["0028,0004"] == "YBR_RCT"
        session.export(str(out2), use_compression=False, show_progress=False)

    assert pydicom.dcmread(
        next(out2.rglob("*.dcm"))).PhotometricInterpretation == "YBR_RCT"


# ---------------------------------------------------------------------------
# #525: YBR_PARTIAL_* under JPEG 2000 -- the same judgement as native.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", ("YBR_PARTIAL_422", "YBR_PARTIAL_420"))
def test_ybr_partial_under_j2k_warns(tmp_path, label):
    """A subsampled label is inadmissible under JPEG 2000 too (#525).

    A JPEG 2000 codestream holds full-sample components, and PS3.5 A.4.4
    gives it no subsampled label, so the compressed file is exactly as
    inadmissible as the native one #502 already warns about. Measured
    before the fix: `ok=True`, `warnings == []`, `verify_readback=True`
    passed -- the answer depended on `use_compression`. The label is
    still written as declared, because `YBR_FULL` would misstate the
    value range (PS3.3 C.7.6.3.1.2) and there is no bare `YBR_PARTIAL`.

    Killing mutation (M1): `YBR_PARTIAL_*` restored to the J2K row of
    `_ADMISSIBLE_PHOTOMETRICS`.
    """
    outcome = _export(tmp_path, _image(label), compression="j2k")

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert written.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    assert written.PhotometricInterpretation == label
    assert len(outcome.warnings) == 1, outcome.warnings
    assert f"({J2K_LOSSLESS})" in outcome.warnings[0], outcome.warnings
    assert "no transfer syntax this exporter writes admits it" in \
        outcome.warnings[0], outcome.warnings


@pytest.mark.parametrize("threads", [False, True])
def test_a_parent_row_for_ybr_partial_under_j2k(tmp_path, monkeypatch,
                                                threads):
    """The compressed case reaches the audit log and the grade (#525).

    Through `session.export()` with compression on, the default: one
    `WARNING` row keyed on the SOP Instance UID and naming no output
    path, and the run reads `REVIEW_REQUIRED`. Killing mutation (M2): the
    worker's `written_syntax` forced to Implicit VR LE. A native judgement
    of this label warns too, so the row count alone would not see it; the
    row naming the JPEG 2000 syntax does.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    inst = _image("YBR_PARTIAL_422")
    report = tmp_path / "report.md"
    db = tmp_path / "p.db"

    with DicomSession(str(db)) as session:
        session.store.patients.append(_graph([inst]))
        session.save()
        session.export(str(tmp_path / "out"), use_compression=True,
                       show_progress=False)
        session.generate_report(str(report))

    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='WARNING'").fetchall()
    labelled = [r for r in rows if "PhotometricInterpretation" in r[1]]
    assert len(labelled) == 1, rows
    assert labelled[0][0] == inst.sop_instance_uid, labelled
    assert f"({J2K_LOSSLESS})" in labelled[0][1], labelled
    assert "Subject_" not in labelled[0][0] + labelled[0][1], labelled
    assert "REVIEW_REQUIRED" in _grade(report), _grade(report)


# ---------------------------------------------------------------------------
# #532: the label is written as the Code String it spells.
# ---------------------------------------------------------------------------

def _mono(label):
    """A 1-sample instance declaring `label`, with a 16-bit ramp."""
    inst = _image(label, samples=1,
                  arr=np.arange(64, dtype=np.uint16).reshape(8, 8))
    return inst


@pytest.mark.parametrize("declared, expected, build", [
    (" rgb ", "RGB", _image), ("rgb", "RGB", _image),
    ("Rgb", "RGB", _image), (" RGB", "RGB", _image),
    ("monochrome2", "MONOCHROME2", _mono),
    (" ybr_full ", "YBR_FULL", _image),
    ([" rgb "], "RGB", _image), ((" rgb ",), "RGB", _image)],
    ids=["padded-lower", "lower", "mixed", "leading-space", "mono-lower",
         "ybr-padded-lower", "one-element-list", "one-element-tuple"])
@pytest.mark.parametrize("compression", [None, "j2k"])
def test_a_label_is_written_as_a_code_string(tmp_path, declared, expected,
                                             build, compression):
    """Upper case, no leading space: what PS3.5 6.2 defines a CS to be (#532).

    Measured before: `' rgb '` was written `' rgb'`, `'monochrome2'` as
    itself, and pydicom and `ingest()` refused the delivered file with
    `ValueError: Unknown (0028,0004) 'Photometric Interpretation' value`.
    The export succeeded and its file was unreadable by the most likely
    reader. Now the file carries the defined spelling, decodes, and the
    change is one INFO correction -- an exact, conformant rewrite, #506's
    class, so no row.

    Under JPEG 2000 an `RGB` spelling is then transformed and labelled
    `YBR_RCT` like any other (#516 case 1), which is why the expected
    label differs there.

    A one-element list or tuple is a label too -- it is how a VM-1 value
    can arrive through `set_attr` -- and each element is respelled.

    Killing mutations: (M4) `_label_as_written` returns the attributes
    unchanged; (R5, from the review of #609) the list/tuple/MultiValue
    arm of `_label_as_written` disabled, so `[' rgb ']` reaches `_merge`
    raw (`one-element-list`, `one-element-tuple`).
    """
    outcome = _export(tmp_path, build(declared), compression=compression)

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    if compression == "j2k" and expected == "RGB":
        expected = "YBR_RCT"
    assert written.PhotometricInterpretation == expected
    written.pixel_array  # decodes: the spelling is one pydicom knows
    assert outcome.warnings == [], outcome.warnings
    assert len(outcome.corrections) == 1, outcome.corrections
    note = outcome.corrections[0]
    for value in ([declared] if isinstance(declared, str) else declared):
        assert repr(value) in note, note
    assert "(PS3.5 6.2" in note, note


def test_normalised_before_merge_and_before_geometry(tmp_path):
    """Both readers of the declaration get the normalized copy (#532).

    `_merge` assigns `0028,0004` and pydicom warns on the caller's stream
    about an invalid CS value as it does; `_write_pixel_geometry` falls
    back to the declared value when the resolver answers None and writes
    it again. Normalizing for one of them only is silently undone by the
    other.

    Killing mutations: the copy passed to `_merge` only (M5: the geometry
    fallback writes `' rgb '` back, and the file reads `' rgb'`); to
    `_write_pixel_geometry` only (M5b: the `UserWarning` returns).
    """
    import warnings as _warnings

    with _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        outcome = _export(tmp_path, _image(" rgb "))

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == "RGB"
    assert [str(w.message) for w in caught
            if "0028,0004" in str(w.message) or "CS" in str(w.message)] \
        == [], [str(w.message) for w in caught]


def test_a_trailing_pad_is_not_a_correction(tmp_path):
    """`'RGB '` is `RGB` to every reader; there is nothing to note (#532).

    A CS is space-padded to even length in the file and right-stripped
    on read, so a trailing pad is not a change anyone sees. Killing
    mutation (M6): the note guarded on the raw value differing from the
    normalized one, instead of its right-stripped form.
    """
    outcome = _export(tmp_path, _image("RGB "))

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == "RGB"
    assert outcome.corrections == [], outcome.corrections


@pytest.mark.parametrize("threads", [False, True])
def test_export_leaves_the_graph_label_alone(tmp_path, monkeypatch, threads):
    """Only the file changes; the graph keeps the declared spelling (#532).

    Driven through `write_tree()`, whose workers run in this process
    under threads, so an in-place normalization would be visible here.
    Killing mutation (M7): the label normalized in place on
    `inst.attributes` (the value changes and the revision advances).
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    inst = _image(" rgb ")
    revision = inst._revision

    DicomExporter.write_tree(_graph([inst]), str(tmp_path / "out"),
                             compression=None, show_progress=False)
    # And the worker directly, in this thread, whatever the executor did.
    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    assert inst.attributes["0028,0004"] == " rgb "
    assert inst._revision == revision


# ---------------------------------------------------------------------------
# #534: the pixel-less arm gets the same judgement.
# ---------------------------------------------------------------------------

SR_STORAGE = "1.2.840.10008.5.1.4.1.1.88.11"


def _pixel_less(label):
    """An SR-shaped instance with no pixels, declaring `label`."""
    inst = Instance(f"1.2.826.0.1.534.{next(_serial)}", SR_STORAGE, 1)
    inst.file_path = None
    for tag, value in (("0008,0020", "20230101"), ("0008,0030", "120000"),
                       ("0008,0060", "SR")):
        inst.set_attr(tag, value)
    inst.set_attr("0028,0004", label)
    return inst


@pytest.mark.parametrize("label", ["YBR_ICT", "NONSENSE"])
def test_a_pixel_less_label_is_judged(tmp_path, label):
    """An instance with no pixel element is not a way around #502 (#534).

    Measured before: an SR-shaped instance declaring `YBR_ICT` or an
    undefined value exported `ok=True` with `warnings == []`, because
    only the two pixel-writing arms call `_write_pixel_geometry`. The
    label is still written as declared -- it is the source's own -- and
    the warning's remedy is the one true of a file with no pixels:
    "export with use_compression=True" cannot help an instance with
    nothing to compress, and there are no samples to have been written
    "over".

    Killing mutations: the pixel-less judgement deleted (M8);
    `has_pixels=True` passed there (M9, the pixel remedy returns).
    """
    outcome = _export(tmp_path, _pixel_less(label))

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert written.PhotometricInterpretation == label
    assert not any(kw in written for kw in (
        "PixelData", "FloatPixelData", "DoubleFloatPixelData"))
    assert len(outcome.warnings) == 1, outcome.warnings
    warning = outcome.warnings[0]
    assert f"'{label}'" in warning, warning
    assert "no pixel element" in warning, warning
    assert "use_compression=True" not in warning, warning
    assert "over the samples" not in warning, warning


def test_a_pixel_less_label_is_judged_under_the_written_syntax(tmp_path):
    """Against the syntax the file carries, which is never JPEG 2000 (#534).

    A file with no pixel data is written natively whatever `compression`
    says -- `_compress_j2k` has nothing to encode and leaves the syntax
    alone -- while the worker's `written_syntax` reads `j2k` as JPEG 2000.
    `YBR_ICT` is admitted by the J2K row and not by the native one, so
    judging the wrong syntax is silent.

    Killing mutation (M10): the pixel-less judgement reads
    `written_syntax` instead of `ds.file_meta.TransferSyntaxUID`.
    """
    outcome = _export(tmp_path, _pixel_less("YBR_ICT"), compression="j2k")

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert written.file_meta.TransferSyntaxUID == IMPLICIT_VR_LE
    assert len(outcome.warnings) == 1, outcome.warnings
    assert f"({IMPLICIT_VR_LE})" in outcome.warnings[0], outcome.warnings


@pytest.mark.parametrize("declared, respelled", [
    (["YBR_ICT", "RGB"], 0), ([" ybr_ict", "rgb"], 1)],
    ids=["defined-spellings", "respelled-each"])
def test_a_multi_valued_pixel_less_label_warns_and_writes(tmp_path, declared,
                                                         respelled):
    """Two labels on a file with no pixels: written, and warned about (#534).

    The pixel arms refuse this (#502) because such a file cannot be read
    back. A pixel-less one can -- measured, it re-ingests with both values
    -- so the refusal's own reason is false here, and the write-path
    ruling (warn and write) applies instead. `verify_readback=True` still
    fails it on the arity.

    Each value is a Code String (#532): `[' ybr_ict', 'rgb']` is written
    and warned about as `['YBR_ICT', 'RGB']`, with one correction.

    Killing mutations: (M11) `_PhotometricRefusal` raised on the
    pixel-less arm; (R5) the multi-value arm of `_label_as_written`
    disabled (`respelled-each`).
    """
    outcome = _export(tmp_path, _pixel_less(declared))

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert list(written.PhotometricInterpretation) == ["YBR_ICT", "RGB"]
    assert len(outcome.warnings) == 1, outcome.warnings
    warning = outcome.warnings[0]
    assert "is VM 1" in warning, warning
    assert "declares 2 values" in warning, warning
    assert "'YBR_ICT', 'RGB'" in warning, warning
    assert "no pixel element" in warning, warning
    assert len(outcome.corrections) == respelled, outcome.corrections

    with DicomSession(str(tmp_path / "re.db")) as session:
        assert session.ingest(
            str(tmp_path / "out")).ingested == 1

    strict = _export(tmp_path, _pixel_less(declared),
                     verify_readback=True)
    assert not strict.ok
    assert "is VM 1" in str(strict.error)


def _hand_built_element(label, element):
    """No pixel array, and a pixel element put in `attributes` by hand."""
    inst = _pixel_less(label)
    for tag, value in (("0028,0010", 4), ("0028,0011", 4), ("0028,0002", 1),
                       ("0028,0100", 8), ("0028,0101", 8), ("0028,0102", 7),
                       ("0028,0103", 0), (element, bytes(16))):
        inst.set_attr(tag, value)
    return inst


@pytest.mark.parametrize("element", ["7fe0,0010", "7fe0,0008"])
def test_a_hand_built_pixel_element_is_not_called_pixel_less(tmp_path,
                                                             element):
    """A file that carries a pixel element is not told it has none (#534).

    `set_attr("7fe0,0010", ...)` on an instance with no pixel array puts
    the element in the file through `_merge`, and no pixel arm runs, so
    the label is judged on the third arm -- rightly, since
    `_write_pixel_geometry` never saw it. But the file *has* a pixel
    element, and the review of #609 measured the warning saying "this
    instance, which has no pixel element" and the remedy "This file
    carries no pixel element at all" over a file carrying `PixelData`: a
    false reason, #194's class. The sentence is now the one for a file
    with pixels.

    Killing mutation (P1): `has_pixels=False` passed unconditionally on
    the third arm.
    """
    outcome = _export(tmp_path, _hand_built_element("YBR_ICT", element))

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert any(kw in written for kw in ("PixelData", "FloatPixelData"))
    assert len(outcome.warnings) == 1, outcome.warnings
    warning = outcome.warnings[0]
    assert "no pixel element" not in warning, warning
    assert ("The label was written as declared, over the samples the "
            "instance held, and neither was changed.") in warning, warning


def test_a_multi_valued_label_over_a_hand_built_pixel_element_is_refused(
        tmp_path):
    """Two labels over a pixel element: the pixel arms' refusal (#502, #534).

    The pixel-less arm writes a multi-valued label because a file with no
    pixels re-ingests; that reason is false for a file carrying a pixel
    element, which is the file `_PhotometricRefusal` exists for. Measured
    on the review of #609: written, with a warning claiming the file had
    no pixel element.

    Killing mutation (P1b): the refusal skipped when a pixel element is
    present on the third arm.
    """
    outcome = _export(tmp_path,
                      _hand_built_element(["YBR_ICT", "RGB"], "7fe0,0010"))

    assert not outcome.ok
    assert "PhotometricInterpretation (0028,0004) is a single value" in str(
        outcome.error), outcome.error
    assert not list((tmp_path / "out").glob("*.dcm"))


@pytest.mark.parametrize("label", ["MONOCHROME2", "RGB"])
def test_a_pixel_less_admitted_label_is_silent(tmp_path, label):
    """The control: an admitted label on a pixel-less file warns nothing (#534).

    Killing mutation (M12): a warning for any label present on the
    pixel-less arm.
    """
    outcome = _export(tmp_path, _pixel_less(label))

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings


def test_a_pixel_less_file_with_no_label_is_silent(tmp_path):
    """No `0028,0004`, no claim, no warning (#534)."""
    inst = _pixel_less("RGB")
    del inst.attributes["0028,0004"]

    outcome = _export(tmp_path, inst)

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings


def test_photometric_warning_requires_has_pixels():
    """A caller must say which kind of file it is judging (#534).

    Keyword-only and without a default, the `float_element`/`syntax_uid`
    precedent: the sentence and the remedy differ for a file with no
    pixels, and inheriting the pixel wording by omission is how the
    pixel-less arm's remedy was false before. Killing mutation (M13): a
    default added.
    """
    import inspect

    parameter = inspect.signature(_photometric_warning).parameters[
        "has_pixels"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# ---------------------------------------------------------------------------
# #596: the default export says what this library cannot read back.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("dtype", [np.uint16, np.int16, np.int8])
def test_16_bit_ybr_full_default_export_notes_the_limit(tmp_path,
                                                        compression, dtype):
    """Written, conformant, and noted as a limit of ours (#596, #461).

    A 16-bit or signed 8-bit `YBR_FULL` file is valid DICOM, so the
    default export writes it. This library cannot read it back --
    pydicom's colour conversion takes unsigned 8-bit samples only -- so
    the export says so at INFO. A capability, not a defect in the user's
    data: no `WARNING`, no row, the grade unmoved.

    Killing mutations: (M17) the note routed to `warnings` (a row
    appears); (Mnote8) the note keyed on `BitsAllocated > 8` rather than
    the samples' dtype, which leaves the int8 file -- BitsAllocated 8,
    and refused by `ingest()` -- silent, as it was when #609 was first
    reviewed.
    """
    if dtype == np.int8:
        arr = (np.arange(8 * 8 * 3) % 200 - 100).astype(np.int8).reshape(
            8, 8, 3)
    else:
        arr = np.arange(8 * 8 * 3, dtype=dtype).reshape(8, 8, 3) * 100
    outcome = _export(tmp_path, _image("YBR_FULL", arr=arr),
                      compression=compression)

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings
    notes = [c for c in outcome.corrections if "this library cannot read such a file back" in c]
    assert len(notes) == 1, outcome.corrections
    bits = np.dtype(dtype).itemsize * 8
    representation = 1 if np.dtype(dtype).kind == "i" else 0
    assert "YBR_FULL" in notes[0], notes
    assert (f"BitsAllocated {bits} and PixelRepresentation "
            f"{representation}") in notes[0], notes
    assert "unsigned 8-bit samples only" in notes[0], notes


@pytest.mark.parametrize("declared, arr", [
    ("RGB", np.arange(8 * 8 * 3, dtype=np.uint16).reshape(8, 8, 3)),
    ("YBR_FULL", np.full((8, 8, 3), YBR, np.uint8)),
    ("YBR_FULL", np.indices((8, 8, 3)).sum(axis=0) % 2 == 0)],
    ids=["16-bit-rgb", "8-bit-ybr-full", "bool-ybr-full"])
def test_a_readable_colour_export_notes_no_limit(tmp_path, declared, arr):
    """The control for the note above (#596).

    Each file here reads back through `ingest()`'s decode, which the
    `verify_readback=True` run asserts. A bool mask is written at
    BitsAllocated 8 and reads back `uint8`, so it converts.

    Killing mutations: the note's label or dtype guard dropped
    (`16-bit-rgb`, `8-bit-ybr-full`); the `bool` half of
    `_pydicom_converts_samples_of` dropped (`bool-ybr-full`).
    """
    outcome = _export(tmp_path, _image(declared, arr=arr))

    assert outcome.ok, outcome.error
    assert outcome.corrections == [], outcome.corrections
    verified = _export(tmp_path / "verified", _image(declared, arr=arr),
                       verify_readback=True)
    assert verified.ok, verified.error


def test_the_16_bit_ybr_note_writes_no_row(tmp_path):
    """INFO only: a session export of such an instance grades PASS (#596).

    Killing mutation (M17, the parent half): the note routed to
    `warnings`.
    """
    arr = np.arange(8 * 8 * 3, dtype=np.uint16).reshape(8, 8, 3) * 100
    with DicomSession(str(tmp_path / "n.db")) as session:
        session.store.patients.append(_graph([_image("YBR_FULL", arr=arr)]))
        session.save()
        session.export(str(tmp_path / "out"), use_compression=False,
                       show_progress=False)
        assert session.store_backend.get_audit_errors() == []


# ---------------------------------------------------------------------------
# #602: an icon's label is judged where it is written.
# ---------------------------------------------------------------------------
#
# The #502 judgement ran on the top-level pixel element and the pixel-less
# arm. An Icon Image Sequence item's label reached the file unexamined:
# measured on ce5b2b2, `YBR_ICT`, `YBR_PARTIAL_420`, `NONSENSE` and
# `['MONOCHROME2', 'RGB']` exported with no row under both compression
# settings and `verify_readback=True`. An icon is always written
# uncompressed (PS3.5 A.4 allows it), so it is judged against the
# uncompressed row whatever syntax the file carries.

CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"
#: A 2x2 icon's samples: three per pixel, or one.
ICON_SAMPLES = {3: bytes(range(12)), 1: bytes([11, 22, 33, 44])}
ICON_PATH = "0088,0200[0]"


def _icon_source(folder, *, samples, serial="SN-1"):
    """A CT file with a 4x4 monochrome frame and one native 2x2 icon."""
    import os
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.DeviceSerialNumber = serial
    ds.Manufacturer, ds.ManufacturerModelName = "ACME", "SCAN9000"
    # `IODValidator` refuses a CT Image without these.
    ds.SliceThickness, ds.KVP = "1.0", "120"
    ds.ImagePositionPatient = [0.0, 0.0, 0.0]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.PixelSpacing = [1.0, 1.0]
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()

    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.SamplesPerPixel = samples
    icon.PhotometricInterpretation = "RGB" if samples == 3 else "MONOCHROME2"
    icon.PixelRepresentation = 0
    if samples == 3:
        icon.PlanarConfiguration = 0
    icon.add_new(0x7FE00010, "OB", ICON_SAMPLES[samples])
    ds.IconImageSequence = Sequence([icon])
    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)


def _icon_image(tmp_path, label, samples=3, *, compression=False,
                verify=False, redact=False):
    """Ingest an icon source, declare `label` on the icon item, export.

    Returns `(rows, exported, grade, summary)`: `get_audit_errors()` as
    tuples, the written file read back, the report's grade line, and the
    export's summary.
    """
    import os
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=samples)
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    with DicomSession(str(tmp_path / "icon.db")) as session:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        item = inst.sequences["0088,0200"].items[0]
        item.set_attr("0028,0004", label)
        if redact:
            session.redact_by_machine("SN-1", [0, 4, 0, 4])
        summary = session.export(str(out), use_compression=compression,
                                 verify_readback=verify, show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, (written, rows)
    return rows, pydicom.dcmread(written[0]), _grade(report), summary


def _icon_label_rows(rows):
    return [r for r in rows if "on the icon at" in r[2]
            or "the icon at" in r[2]]


ICON_INADMISSIBLE = ("YBR_ICT", "YBR_RCT", "YBR_PARTIAL_420",
                     "YBR_PARTIAL_422", "NONSENSE")


@pytest.mark.parametrize("compression", [False, True],
                         ids=["uncompressed", "compressed"])
@pytest.mark.parametrize("label", ICON_INADMISSIBLE)
def test_an_inadmissible_icon_label_is_written_with_a_warning(
        tmp_path, label, compression):
    """I1: one `WARNING` naming the item; label and samples as declared.

    The remedy is the icon's own: "export with use_compression=True" is
    false for an icon, which this exporter never compresses. Killing
    mutations: (m8) the `_icon_label_warning` call deleted; (m14) the
    table's remedy kept in place of `_PHOTOMETRIC_ICON`.
    """
    rows, exported, grade, _summary = _icon_image(
        tmp_path, label, compression=compression)

    warnings = [r for r in rows if "PhotometricInterpretation" in r[2]]
    assert len(warnings) == 1, rows
    _when, action, details = warnings[0]
    assert action == "WARNING"
    assert details.startswith(
        f"PhotometricInterpretation '{label}' on the icon at {ICON_PATH} "
        f"is not a label uncompressed pixel data admits: "), details
    assert "use_compression=True, where" not in details, details
    assert "An icon is written uncompressed whatever transfer syntax" \
        in details, details
    icon = exported.IconImageSequence[0]
    assert icon.PhotometricInterpretation == label
    assert icon.PixelData == ICON_SAMPLES[3]
    assert "REVIEW_REQUIRED" in grade, grade


def test_an_icon_label_is_judged_as_uncompressed_under_a_compressed_export(
        tmp_path):
    """I2: the J2K row admits `YBR_ICT`; the icon inside that file is raw.

    Killing mutation (m9): the judgement made against the file's written
    syntax, whose row admits the label, instead of the uncompressed row.
    """
    rows, exported, _grade_line, _summary = _icon_image(
        tmp_path, "YBR_ICT", compression=True)

    assert str(exported.file_meta.TransferSyntaxUID) == \
        "1.2.840.10008.1.2.4.90"
    assert not exported.IconImageSequence[0][
        "PixelData"].is_undefined_length
    assert len([r for r in rows if f"on the icon at {ICON_PATH}" in r[2]]) \
        == 1, rows


@pytest.mark.parametrize("compression", [False, True],
                         ids=["uncompressed", "compressed"])
@pytest.mark.parametrize("label, samples", [
    ("MONOCHROME2", 1), ("MONOCHROME1", 1), ("PALETTE COLOR", 1),
    ("RGB", 3), ("YBR_FULL", 3)])
def test_an_admitted_icon_label_warns_about_nothing(tmp_path, label, samples,
                                                  compression):
    """I3: the control -- an admitted icon label writes no row.

    Killing mutation (m14b, with I1): the admitted test inverted.
    """
    rows, exported, grade, _summary = _icon_image(
        tmp_path, label, samples, compression=compression)

    assert rows == [], rows
    assert exported.IconImageSequence[0].PhotometricInterpretation == label
    assert "PASS" in grade, grade


@pytest.mark.parametrize("declared, expected, samples, noted", [
    (" rgb ", "RGB", 3, True), ("rgb", "RGB", 3, True),
    (" monochrome2 ", "MONOCHROME2", 1, True), ("RGB ", "RGB", 3, False)],
    ids=["padded-lower", "lower", "mono-padded-lower", "trailing-pad"])
def test_an_icon_label_is_written_as_a_code_string(
        tmp_path, monkeypatch, caplog, declared, expected, samples, noted):
    """I4 (Q4): #532's respelling, one depth down.

    Measured on ce5b2b2: `' rgb '` on an icon was written `' rgb'`,
    pydicom warned `Invalid value for VR CS` as `_merge_sequences`
    assigned it, and re-ingesting the file dropped the icon with the
    unrouted `DATA_LOSS` row. Now the file carries the defined spelling,
    one INFO note names the item, and the file re-ingests with its icon.
    A trailing pad is what the file does anyway: no note.

    Threads, so the export worker runs here and its `UserWarning` would
    reach `catch_warnings`. Killing mutation (m11): `_merge_sequences`
    merges `item.attributes` rather than the respelled copy.
    """

    import warnings as _warnings
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.INFO, logger="isocenter"), \
            _warnings.catch_warnings(record=True) as caught:
        _warnings.simplefilter("always")
        rows, exported, _grade_line, _summary = _icon_image(
            tmp_path, declared, samples)

    icon = exported.IconImageSequence[0]
    assert icon.PhotometricInterpretation == expected
    assert _icon_label_rows(rows) == [], rows
    assert [str(w.message) for w in caught
            if "Invalid value for VR CS" in str(w.message)] == []
    notes = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and "(0088,0200) item 0: "
             in r.getMessage()]
    if noted:
        assert len(notes) == 1, [r.getMessage() for r in caplog.records]
        assert repr(declared) in notes[0], notes
        assert "(PS3.5 6.2" in notes[0], notes
    else:
        assert notes == [], notes

    reingest = tmp_path / "reingest.db"
    exported_dir = tmp_path / "out"
    with DicomSession(str(reingest)) as session:
        session.ingest(str(exported_dir))
    with sqlite3.connect(str(reingest)) as conn:
        kinds = [k for k, in conn.execute("SELECT kind FROM instance_blobs")]
        losses = conn.execute("SELECT details FROM audit_log WHERE "
                              "action_type='DATA_LOSS'").fetchall()
    assert [k for k in kinds if "0088,0200" in k], (kinds, losses)
    assert losses == [], losses


def test_a_multi_valued_icon_label_warns_and_is_written(tmp_path):
    """I5 (Q5): warned about, written as declared, never refused.

    The pixel arms refuse a multi-valued top-level label; an icon is not a
    reason to lose the instance (#433), and the user can fix it with one
    `set_attr`. Killing mutation (m13): the arity branch deleted, so the
    label is judged as the text of a list and the wrong sentence is written.
    """
    rows, exported, grade, summary = _icon_image(
        tmp_path, ["MONOCHROME2", "RGB"])

    assert summary.failures == [], summary.failures
    assert len(summary.written_uids) == 1, summary
    warnings = [r for r in rows if "PhotometricInterpretation" in r[2]]
    assert len(warnings) == 1, rows
    assert warnings[0][1] == "WARNING"
    assert warnings[0][2].startswith(
        f"PhotometricInterpretation (0028,0004) is VM 1; the icon at "
        f"{ICON_PATH} declares 2 values ('MONOCHROME2', 'RGB'). "), warnings
    assert list(exported.IconImageSequence[0].PhotometricInterpretation) == [
        "MONOCHROME2", "RGB"]
    assert "REVIEW_REQUIRED" in grade, grade


def test_an_icon_that_is_not_written_is_not_judged(tmp_path):
    """I6: a redacted instance's icon is removed, with its SIGNAL loss row.

    It writes no pixel element, so the label door never opens for it.
    Green before #602. Killing mutation (m10): the judgement made over
    every ref, before the removals are decided.
    """

    rows, exported, _grade_line, _summary = _icon_image(
        tmp_path, "YBR_ICT", redact=True)

    assert "IconImageSequence" not in exported
    with sqlite3.connect(str(tmp_path / "icon.db")) as conn:
        scopes = [s for s, in conn.execute(
            "SELECT loss_scope FROM audit_log WHERE action_type='DATA_LOSS' "
            "AND details LIKE '%7fe0,0010%'")]
    assert scopes == ["SIGNAL"], scopes
    assert _icon_label_rows(rows) == [], rows


@pytest.mark.parametrize("threads", [False, True])
def test_export_leaves_the_graph_icon_label_alone(tmp_path, monkeypatch,
                                                  threads):
    """I7: the file is respelled; the graph item keeps what was declared.

    Driven through `write_tree()` on the session's own graph, whose
    workers run in this process under threads -- `session.export()` hands
    its workers a copy, so a write-back there would be invisible here.
    Killing mutation (m12): the respelled value written back to
    `item.attributes`.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=3)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "graph.db")) as session:
        session.ingest(str(src))
        patient = session.store.patients[0]
        item = patient.studies[0].series[0].instances[0].sequences[
            "0088,0200"].items[0]
        item.set_attr("0028,0004", " rgb ")
        revision = item._revision
        DicomExporter.write_tree(patient, str(out), studies=patient.studies,
                                 compression=None, show_progress=False)

        assert item.attributes["0028,0004"] == " rgb "
        assert item._revision == revision
    written = list(out.rglob("*.dcm"))
    assert len(written) == 1, written
    assert pydicom.dcmread(str(written[0])).IconImageSequence[
        0].PhotometricInterpretation == "RGB"


def test_icon_label_warning_unit():
    """I8: `_icon_label_warning` on normalized labels.

    None for an admitted label and for no label; otherwise the table's
    clause and the icon's own remedy, never the table's. `YBR_ICT` is
    inadmissible here though the J2K row admits it.
    """
    from isocenter.io_handlers import (_PHOTOMETRIC_ICON,
                                       _PHOTOMETRIC_INADMISSIBLE,
                                       _icon_label_warning)

    assert _icon_label_warning("RGB", ICON_PATH) is None
    assert _icon_label_warning(None, ICON_PATH) is None
    for label, key in (("YBR_ICT", "YBR_ICT"), ("YBR_PARTIAL_420",
                                                "YBR_PARTIAL_420"),
                       ("NONSENSE", None)):
        sentence = _icon_label_warning(label, ICON_PATH)
        clause, remedy = _PHOTOMETRIC_INADMISSIBLE[key]
        assert clause in sentence, sentence
        assert remedy not in sentence, sentence
        assert sentence.endswith(_PHOTOMETRIC_ICON), sentence
        assert f"'{label}' on the icon at {ICON_PATH} " in sentence


def test_a_label_two_items_deep_is_respelled_with_its_note():
    """I10: `_merge_sequences` threads the correction notes down its recursion.

    An icon inside a sequence item, declared `' rgb '`: written `RGB`, and
    the one INFO note names the item by its full path. Review J2 P7:
    the respelling was pinned only one level down, so a recursive call
    that dropped `corrections` kept the label right and lost the note
    unseen. Killing mutation (r1): `corrections` not forwarded.
    """
    from pydicom.dataset import Dataset
    from isocenter.entities import DicomItem, DicomSequence
    from isocenter.io_handlers import DicomExporter

    icon = DicomItem()
    icon.attributes.update({"0028,0004": " rgb ", "0028,0002": 3})
    outer = DicomItem()
    outer.attributes.update({"0008,1150": "1.2.840.10008.5.1.4.1.1.2"})
    outer.sequences["0088,0200"] = DicomSequence("0088,0200", [icon])
    ds, notes = Dataset(), []
    DicomExporter._merge_sequences(  # pylint: disable=protected-access
        ds, {"0008,1140": DicomSequence("0008,1140", [outer])}, [],
        corrections=notes)

    written = ds.ReferencedImageSequence[0].IconImageSequence[0]
    assert written.PhotometricInterpretation == "RGB"
    assert len(notes) == 1, notes
    assert notes[0].startswith("(0008,1140) > (0088,0200) item 0: "), notes
    assert "' rgb '" in notes[0], notes


def test_verify_readback_does_not_judge_an_icon_label(tmp_path):
    """I9 (Q6): `verify_readback=True` passes the instance, and the row stays.

    The WARNING already bars PASS; failing a verified instance over a
    thumbnail would invert #433's "an icon is not a reason to lose the
    instance". Green before #602 (the readback compares no nested label).
    """
    rows, _exported, _grade_line, summary = _icon_image(
        tmp_path, "YBR_ICT", verify=True)

    assert summary.failures == [], summary.failures
    assert len(summary.written_uids) == 1, summary
    assert len([r for r in rows if "PhotometricInterpretation" in r[2]]) \
        == 1, rows


# ---------------------------------------------------------------------------
# #646: a three-sample icon labelled `YBR_FULL_422` is written `YBR_FULL`.
# ---------------------------------------------------------------------------
#
# #470 folds the label at the top level: three full samples per pixel are
# not the subsampled layout `YBR_FULL_422` names. An icon is written from
# the same kind of array one depth down, and its label reached the file as
# declared: measured on 067982ec, 12 bytes for 4 pixels under
# `YBR_FULL_422`, no row, and this library's own re-ingest dropped the
# icon's pixels with the unrouted `DATA_LOSS` row. No source file reaches
# it (an 8-bit YBR icon is relabelled `RGB` by its decode at ingest), so
# these tests are the pin; no cohort member can hold one.

@pytest.mark.parametrize("declared", ["YBR_FULL_422", " ybr_full_422 "],
                         ids=["as-defined", "padded-lower"])
def test_a_three_sample_icon_labelled_ybr_full_422_is_written_ybr_full(
        tmp_path, declared):
    """The fold, on the pydicom item; the file re-ingests with its icon.

    Killing mutations: (f1) the icon's call to `_full_sample_label`
    deleted; (f4) the fold placed after the label is judged (no row
    either way, so only the written label shows it).
    """
    rows, exported, grade, _summary = _icon_image(tmp_path, declared)

    icon = exported.IconImageSequence[0]
    assert icon.PhotometricInterpretation == "YBR_FULL"
    assert icon.SamplesPerPixel == 3
    assert icon.PixelData == ICON_SAMPLES[3]
    # The top level writes no row for its fold, so the icon writes none.
    assert rows == [], rows
    assert "PASS" in grade, grade

    reingest = tmp_path / "reingest.db"
    with DicomSession(str(reingest)) as session:
        session.ingest(str(tmp_path / "out"))
    with sqlite3.connect(str(reingest)) as conn:
        kinds = [k for k, in conn.execute("SELECT kind FROM instance_blobs")]
        losses = conn.execute("SELECT details FROM audit_log WHERE "
                              "action_type='DATA_LOSS'").fetchall()
    assert [k for k in kinds if "0088,0200" in k], (kinds, losses)
    assert losses == [], losses


def test_a_one_sample_icon_labelled_ybr_full_422_is_not_folded(tmp_path):
    """The fold is for three full samples, as at the top level.

    At one sample the label is wrong in another way, which is not this
    rule's to answer: it is written as declared. Killing mutation (f2):
    the fold applied at any sample count.
    """
    _rows_, exported, _grade_line, _summary = _icon_image(
        tmp_path, "YBR_FULL_422", samples=1)

    assert exported.IconImageSequence[
        0].PhotometricInterpretation == "YBR_FULL_422"


def _identity(value):
    return value


_ICON_LABELS_HOLDING_BYTES = [
    (b"YBR_FULL_422", "YBR_FULL_422"),
    (b"ybr_full_422", "ybr_full_422"),
    ([b"YBR_FULL_422"], "YBR_FULL_422"),
    ((b"YBR_FULL_422",), "YBR_FULL_422"),
    ([b"ybr_full_422"], "ybr_full_422"),
    (MultiValue(_identity, [b"YBR_FULL_422"]), "YBR_FULL_422"),
]
_ICON_BYTES_IDS = ["bytes", "bytes-lower", "list-of-one", "tuple-of-one",
                   "list-of-one-lower", "multivalue-of-one"]


@pytest.mark.parametrize("compression", [False, True],
                         ids=["uncompressed", "compressed"])
@pytest.mark.parametrize("declared, in_file", _ICON_LABELS_HOLDING_BYTES,
                         ids=_ICON_BYTES_IDS)
def test_an_icon_label_held_as_bytes_is_not_folded(tmp_path, declared,
                                                   in_file, compression):
    """A `bytes` label is written as held, the fold included (#612, owner
    ruling of 2026-10-11), and so is a list, a tuple or a `MultiValue`
    holding one.

    Measured on 7325ff3a for the lone values and on c6514632 for the
    containers of one: written as held over three full samples. pydicom
    hands the item's value back as text once it is on the dataset, a
    one-value element as one value, so the fold asks the graph item what
    it holds; without that, each was folded to `YBR_FULL`. Killing
    mutations: (b6) the fold applied whatever the graph holds; (b9) the
    guard asks for a lone `bytes` value only.
    """
    _rows_, exported, _grade_line, _summary = _icon_image(
        tmp_path, declared, compression=compression)

    assert exported.IconImageSequence[
        0].PhotometricInterpretation == in_file


@pytest.mark.parametrize("declared, in_file", _ICON_LABELS_HOLDING_BYTES,
                         ids=_ICON_BYTES_IDS)
def test_write_tree_does_not_fold_an_icon_label_held_as_bytes(
        tmp_path, monkeypatch, declared, in_file):
    """The serializer alone, under threads: the same label as `export()`."""
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=3)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "graph.db")) as session:
        session.ingest(str(src))
        patient = session.store.patients[0]
        item = patient.studies[0].series[0].instances[0].sequences[
            "0088,0200"].items[0]
        item.set_attr("0028,0004", declared)
        DicomExporter.write_tree(patient, str(out), studies=patient.studies,
                                 compression=None, show_progress=False)
    written = list(out.rglob("*.dcm"))
    assert len(written) == 1, written
    assert pydicom.dcmread(str(written[0])).IconImageSequence[
        0].PhotometricInterpretation == in_file


def test_an_icon_label_in_a_list_of_bytes_is_not_folded_after_a_reopen(
        tmp_path):
    """The store gives the list back, and the reopened graph is asked."""
    import os
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=3)
    out = tmp_path / "out"
    db = str(tmp_path / "reopen.db")
    with DicomSession(db) as session:
        session.ingest(str(src))
        item = session.store.patients[0].studies[0].series[0].instances[
            0].sequences["0088,0200"].items[0]
        item.set_attr("0028,0004", [b"YBR_FULL_422"])
        session.save(sync=True)
    with DicomSession(db) as session:
        item = session.store.patients[0].studies[0].series[0].instances[
            0].sequences["0088,0200"].items[0]
        assert item.attributes["0028,0004"] == [b"YBR_FULL_422"]
        session.export(str(out), show_progress=False)
    written = [os.path.join(r, f) for r, _d, files in os.walk(str(out))
               for f in files if f.endswith(".dcm")]
    assert len(written) == 1, written
    assert pydicom.dcmread(written[0]).IconImageSequence[
        0].PhotometricInterpretation == "YBR_FULL_422"


def test_what_holds_bytes_is_asked_one_level_down():
    """`_holds_bytes`: a `bytes` value, or a container directly holding one."""
    from isocenter.io_handlers import _holds_bytes

    assert _holds_bytes(b"YBR_FULL_422") is True
    assert _holds_bytes([b"YBR_FULL_422"]) is True
    assert _holds_bytes((b"YBR_FULL_422",)) is True
    assert _holds_bytes(MultiValue(_identity, [b"YBR_FULL_422"])) is True
    assert _holds_bytes(["YBR_FULL_422", b"x"]) is True
    assert _holds_bytes("YBR_FULL_422") is False
    assert _holds_bytes(["YBR_FULL_422"]) is False
    assert _holds_bytes(("YBR_FULL_422",)) is False
    assert _holds_bytes(MultiValue(str, ["YBR_FULL_422"])) is False
    assert _holds_bytes([]) is False
    assert _holds_bytes(None) is False
    assert _holds_bytes(7) is False


@pytest.mark.parametrize("threads", [False, True])
def test_the_icon_fold_leaves_the_graph_item_alone(tmp_path, monkeypatch,
                                                   threads):
    """Only the file is folded; `write_tree()` writes the same label.

    Killing mutation (f3): the folded label written onto the graph item.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    if threads:
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    src.mkdir()
    _icon_source(str(src), samples=3)
    out = tmp_path / "out"
    with DicomSession(str(tmp_path / "graph.db")) as session:
        session.ingest(str(src))
        patient = session.store.patients[0]
        item = patient.studies[0].series[0].instances[0].sequences[
            "0088,0200"].items[0]
        item.set_attr("0028,0004", "YBR_FULL_422")
        revision = item._revision
        DicomExporter.write_tree(patient, str(out), studies=patient.studies,
                                 compression=None, show_progress=False)

        assert item.attributes["0028,0004"] == "YBR_FULL_422"
        assert item._revision == revision
    written = list(out.rglob("*.dcm"))
    assert len(written) == 1, written
    assert pydicom.dcmread(str(written[0])).IconImageSequence[
        0].PhotometricInterpretation == "YBR_FULL"


def test_the_fold_is_one_helper_for_both_depths():
    """`_full_sample_label`: the label and the sample count, nothing else.

    It spells the label as the judgement does, so the two cannot disagree
    about which label is `YBR_FULL_422`.
    """
    from isocenter.io_handlers import _full_sample_label

    assert _full_sample_label("YBR_FULL_422", 3) == "YBR_FULL"
    assert _full_sample_label(" ybr_full_422 ", 4) == "YBR_FULL"
    # `bytes` is not text: not spelled, so not folded, as before #646.
    held_bytes = b"YBR_FULL_422"
    assert _full_sample_label(held_bytes, 3) is held_bytes
    assert _full_sample_label("YBR_FULL_422", 1) == "YBR_FULL_422"
    assert _full_sample_label("YBR_FULL_422", 2) == "YBR_FULL_422"
    assert _full_sample_label("YBR_FULL", 3) == "YBR_FULL"
    assert _full_sample_label("RGB", 3) == "RGB"
    assert _full_sample_label(None, 3) is None
    # Spelled as the judgement spells it: whitespace at an end is not
    # part of the label, and a letter outside ASCII is.
    assert _full_sample_label("YBR_FULL_422\t", 3) == "YBR_FULL"
    assert _full_sample_label("YBR_FULL_422\xa0", 3) == "YBR_FULL"
    assert _full_sample_label("YBR_FULL_422\xdf", 3) == "YBR_FULL_422\xdf"
    held = ["YBR_FULL_422"]
    assert _full_sample_label(held, 3) is held


# ---------------------------------------------------------------------------
# #612: one spelling of a declared label, and it says what it changed.
# ---------------------------------------------------------------------------
#
# #532 respelled a declared label with Python's own strip and upper-casing,
# which are Unicode's: a tab, a newline and a no-break space were removed
# with no note or a note about "spaces", and `ß` became `SS`. Measured on
# 067982ec, an icon declared `'rgß'` was written `RGSS` under a WARNING
# that said "The label was written as declared". PS3.5 6.2 gives a Code
# String upper-case A-Z, digits, the space and the underscore.
#
# The rule, by the owner's rulings of 2026-10-10 and 2026-10-11:
#   (a) whitespace at an end is removed as it always was -- everything
#       Python's own strip removes -- and the note names what was removed
#       that was not a space, by code point;
#   (b) `a`-`z` are upper-cased and every other character the file can
#       hold is written as held (`'rgß'` is `RGß`);
#   (c) a label the file could not hold so spelled (a character above
#       U+00FF) is written as it was before, by Python's own upper-casing,
#       under a WARNING that says so: no file that was exported stops
#       being exported.

_NOTE = ("PhotometricInterpretation {} is not a defined Code String "
         "spelling; written as {}, the same label with {} (PS3.5 6.2: a CS "
         "value is upper case, and its leading and trailing spaces are not "
         "significant). The samples are unchanged.")
_UPPER = "its letters upper-cased"
_LEAD = "its leading spaces removed"
_WS = "the whitespace that is not a space removed from "
_FULLWIDTH = "ＲＧＢ"
_FULLWIDTH_LOWER = "ｒｇｂ"
_FELL_BACK = ("PhotometricInterpretation {} holds a character the file "
              "cannot hold ({}); it was written as {}, its upper case by "
              "Unicode's rules, and that is the label any other row about "
              "this file quotes.")
_FELL_BACK_STRIPPED = (
    "PhotometricInterpretation {} holds a character the file cannot hold "
    "({}); it was written as {}, its upper case by Unicode's rules, with "
    "the whitespace that is not a space removed from {}, and that is the "
    "label any other row about this file quotes.")


def _is_printable_ascii(text):
    return all(0x20 <= ord(char) < 0x7F for char in text)


@pytest.mark.parametrize("declared, written, changes", [
    ("monochrome2", "MONOCHROME2", _UPPER),
    (" MONOCHROME2", "MONOCHROME2", _LEAD),
    (" rgb ", "RGB", f"{_UPPER} and {_LEAD}"),
    ("RGB ", "RGB", None),
    ("monochrome\xdf", "MONOCHROME\xdf", _UPPER),
    ("rgb\xff", "RGB\xff", _UPPER),
    ("rgb\xb5", "RGB\xb5", _UPPER),
    (" RG\xdf", "RG\xdf", _LEAD),
    ("RGB\t", "RGB", f"{_WS}its end (U+0009)"),
    ("RGB\n", "RGB", f"{_WS}its end (U+000A)"),
    ("RGB\x0b", "RGB", f"{_WS}its end (U+000B)"),
    ("RGB\x0c", "RGB", f"{_WS}its end (U+000C)"),
    ("RGB\r", "RGB", f"{_WS}its end (U+000D)"),
    ("RGB\x1c", "RGB", f"{_WS}its end (U+001C)"),
    ("RGB\x1d", "RGB", f"{_WS}its end (U+001D)"),
    ("RGB\x1e", "RGB", f"{_WS}its end (U+001E)"),
    ("RGB\x1f", "RGB", f"{_WS}its end (U+001F)"),
    ("RGB\x85", "RGB", f"{_WS}its end (U+0085)"),
    ("RGB\xa0", "RGB", f"{_WS}its end (U+00A0)"),
    ("RGB\u2003", "RGB", f"{_WS}its end (U+2003)"),
    ("\u3000RGB", "RGB", f"{_WS}its start (U+3000)"),
    (" rgb\r\n", "RGB",
     f"{_UPPER} and {_LEAD} and {_WS}its end (U+000A, U+000D)"),
    ("\tRGB", "RGB", f"{_WS}its start (U+0009)"),
    ("\tRGB \n", "RGB", f"{_WS}its start (U+0009) and its end (U+000A)"),
    ("\trgb", "RGB", f"{_UPPER} and {_WS}its start (U+0009)"),
    ("RGB\t ", "RGB", f"{_WS}its end (U+0009)"),
    ("\t RGB", "RGB", f"{_LEAD} and {_WS}its start (U+0009)"),
    ("RGB\t\t", "RGB", f"{_WS}its end (U+0009)"),
    ("RGB\xa0\t", "RGB", f"{_WS}its end (U+0009, U+00A0)"),
    ("RG\xdf\n", "RG\xdf", f"{_WS}its end (U+000A)"),
    ("\t", "", f"{_WS}its end (U+0009)"),
    ("   ", "", None),
], ids=["lower", "leading-space", "padded-lower", "trailing-pad", "sharp-s",
        "y-diaeresis", "micro", "space-then-sharp-s",
        "tab", "line-feed", "vertical-tab", "form-feed", "carriage-return",
        "fs", "gs", "rs", "us", "next-line", "nbsp", "em-space",
        "ideographic-space-lead", "padded-lower-crlf", "leading-tab",
        "tab-both-ends", "tab-then-lower", "tab-then-pad", "tab-then-space",
        "two-tabs", "nbsp-then-tab", "sharp-s-line-feed",
        "only-a-tab", "only-spaces"])
def test_a_label_is_respelled_on_the_code_string_repertoire(declared, written,
                                                            changes):
    """`a`-`z` are upper-cased, whitespace is removed from the ends, and
    the note says which (#612).

    The note is whole, so a clause is said only when it is true: before,
    `'monochromeß'` got "its letters upper-cased" for a rewrite to
    `MONOCHROMESS`, `'\\tMONOCHROME2'` "its leading spaces removed" for a
    tab, and `'RGB\\t'` no note at all. Whitespace that is not a space is
    named by code point and by end, in code point order, each once, and
    no character outside printable ASCII is written into the note.

    Killing mutations: (s1) Python's own upper-casing (`sharp-s`; under it
    `y-diaeresis` and `micro` become labels no file can hold); (s2) the
    space alone stripped, or ASCII whitespace alone (`tab` to
    `ideographic-space-lead`); (s6) the upper-case clause tested with Unicode's predicate
    (`space-then-sharp-s`); (w1) the space counted as whitespace that is
    not one (`tab-then-pad`, `tab-then-space`, `only-spaces`); (w2) the
    two ends swapped or merged (`leading-tab`, `tab-both-ends`); (w3) the
    code points unsorted or repeated (`padded-lower-crlf`, `two-tabs`,
    `nbsp-then-tab`); (w4) the leading-spaces clause said for a leading
    tab (`leading-tab`, `tab-then-lower`); (w5) a value that is all
    whitespace read as having a start (`only-a-tab`, `only-spaces`).
    """
    from isocenter.io_handlers import (_label_as_written,
                                       _label_fallback_warning)

    attributes = {"0028,0004": declared, "0028,0002": 3}
    copy, note = _label_as_written(attributes)

    assert copy["0028,0004"] == written
    assert type(copy["0028,0004"]) is str
    assert attributes["0028,0004"] is declared
    if changes is None:
        assert note is None
    else:
        assert note == _NOTE.format(ascii(declared), ascii(written), changes)
        assert _is_printable_ascii(note), note
    assert _label_fallback_warning(attributes) is None
    # What is judged is what is written.
    assert _written_photometric(declared) == (written or None)
    assert _written_photometric(copy["0028,0004"]) == (written or None)


@pytest.mark.parametrize("declared", [
    "MONO\tCHROME2", "RGB\xa0X", "RG\xdf", "RGB\xff", b"rg\xdf",
    b"RG\xdf ", b"rgb", b" rgb ", b"RGB\t", b"RGB", b"monochrome2",
    _FULLWIDTH, 7],
    ids=["tab-inside", "nbsp-inside", "sharp-s-upper", "y-diaeresis-upper",
         "bytes-not-ascii", "bytes-not-ascii-padded", "bytes-lower",
         "bytes-padded", "bytes-tab", "bytes-defined", "bytes-monochrome",
         "fullwidth", "number"])
def test_a_character_outside_the_repertoire_is_left_where_it_is(declared):
    """Nothing to respell: written as held, no note, judged as held (#612).

    Whitespace inside a label is not at an end, and a letter outside
    `a`-`z` has no Code String upper case. A `bytes` label, ASCII or not,
    is not text: it is not respelled, the writer is handed it as held,
    and it is judged as the text of its `str`, all three as before #612
    (owner ruling, 2026-10-11; a file that states a binary VR for
    (0028,0004) brings one in). Fullwidth capitals are their own
    fallback: nothing to respell, and no file can hold them, as before.
    Killing mutation (b1, b2): a `bytes` label respelled as its text by
    the writer, or judged as its text.
    """
    from isocenter.io_handlers import (_cs_quoted, _label_as_written,
                                       _label_fallback_warning)

    attributes = {"0028,0004": declared}
    copy, note = _label_as_written(attributes)

    assert copy is attributes
    assert note is None
    assert _label_fallback_warning(attributes) is None
    judged = _written_photometric(declared)
    assert judged == (declared if isinstance(declared, str)
                      else str(declared).upper())
    sentence = _photometric_warning(judged, IMPLICIT_VR_LE, has_pixels=True)
    assert sentence.startswith(
        f"PhotometricInterpretation {_cs_quoted(judged)} is not")
    assert _is_printable_ascii(sentence), sentence


def test_a_label_is_quoted_in_ascii():
    """A sentence about a label never holds a character outside printable
    ASCII (#612, review finding 2).

    Quoted with `repr`, a no-break space or a `ß` reached the sentence
    raw, and the report flattens whitespace on the way to the audit log:
    the row for `'RGB\\xa0'` named `'RGB '`, a conformant spelling.
    """
    from isocenter.io_handlers import _cs_quoted

    assert _cs_quoted("RGB") == "'RGB'"
    assert _cs_quoted("RGB\t") == "'RGB\\t'"
    assert _cs_quoted("RG\xdf") == "'RG\\xdf'"
    assert _cs_quoted("RGB\xa0") == "'RGB\\xa0'"
    assert _cs_quoted("monochrome2\u0131") == "'monochrome2\\u0131'"
    assert _cs_quoted(_FULLWIDTH) == "'\\uff32\\uff27\\uff22'"
    assert _cs_quoted(b"rg\xdf") == "b'rg\\xdf'"
    assert _cs_quoted("\xdf" * 17) == \
        "'" + "\\xdf" * 16 + "' (cut at 16 characters)"


def test_a_multi_valued_label_is_respelled_value_by_value():
    """Each text value is respelled; `bytes` and a number are left as held."""
    from isocenter.io_handlers import _label_as_written

    declared = [" rgb", b"ybr_full\n", b"x\xdf", "\x0cRG\xdf\t", 5]
    copy, note = _label_as_written({"0028,0004": declared})

    assert copy["0028,0004"] == ["RGB", b"ybr_full\n", b"x\xdf", "RG\xdf", 5]
    # One clause for the element: the code points of every text value, by
    # end. The line feed is the `bytes` value's, which is not respelled.
    assert note == _NOTE.format(
        "' rgb', b'ybr_full\\n', b'x\\xdf', '\\x0cRG\\xdf\\t', 5",
        "'RGB', b'ybr_full\\n', b'x\\xdf', 'RG\\xdf', 5",
        f"{_UPPER} and {_LEAD} and {_WS}its start (U+000C) and its end "
        f"(U+0009)")
    assert declared == [" rgb", b"ybr_full\n", b"x\xdf", "\x0cRG\xdf\t", 5]

    # No `str` among them: nothing to respell, and the mapping is the
    # caller's own.
    held = {"0028,0004": [b"rgb", b"RGB "]}
    copy, note = _label_as_written(held)
    assert copy is held
    assert note is None


@pytest.mark.parametrize("label, judged", [
    ("   ", None), ("", None), (None, None), (b"  ", "B'  '"),
    ("\t", None), (b"", "B''"), (" \r\n", None), ("\xa0", None),
    ("\u3000 ", None), (" rgb ", "RGB"), (b" rgb ", "B' RGB '"),
    ("RGB\t", "RGB"), ("RGB\xa0", "RGB")])
def test_a_label_of_spaces_is_no_label(label, judged):
    assert _written_photometric(label) == judged


LABELS_OF_EVERY_KIND = [
    "RGB", " rgb ", "RGB\t", "\tRGB", "RGB\xa0", "RGB\u2003", "rg\xdf",
    "monochrome2", "MONOCHROME2\t", "monochrome2\u0131", "rgb\u017f",
    "ybr_\u0131ct", "rgb\xff", _FULLWIDTH, _FULLWIDTH_LOWER, "YBR_FULL_422",
    " ybr_full_422\n", b"rgb", b"RGB\t", b"rg\xdf", "", "   ", "\t", 7,
    ["RGB"], ["rgb", "RGB"], "NONSENSE"]


@pytest.mark.parametrize("label", LABELS_OF_EVERY_KIND, ids=ascii)
def test_the_resolver_the_writer_and_the_judge_spell_a_label_one_way(label):
    """Three readers of a declaration, one spelling (#612, review finding 3).

    The resolver (`pixel_geometry._declared_str`) kept Python's own strip
    and upper-casing when the writer and the judgement moved to
    `_cs_spelling`, so at one sample it answered "monochrome, leave it
    alone" about `'MONOCHROME2\\t'`, which the writer then wrote with its
    tab: a file pydicom and this library's own ingest refuse. Killing
    mutation (r1): `_declared_str` back on `str(raw).strip().upper()`
    (`rg\\xdf`, `rgb\\xff`). A `bytes` label is not text to any of the
    three: the resolver and the judgement read the text of its `str`,
    which names no label, and the writer leaves it as held (b3: the
    resolver reading ASCII `bytes` as its text).
    """
    from isocenter.io_handlers import _label_as_written
    from isocenter.pixel_geometry import (TAG_PHOTOMETRIC_INTERPRETATION,
                                          _declared_str)

    attributes = {"0028,0004": label}
    resolved = _declared_str(attributes, TAG_PHOTOMETRIC_INTERPRETATION)
    judged = _written_photometric(label)
    assert resolved == judged
    copy, _note = _label_as_written(attributes)
    if isinstance(label, str) and judged is not None:
        assert copy["0028,0004"] == judged
    elif isinstance(label, bytes):
        assert copy is attributes
        assert judged == str(label).upper()


@pytest.mark.parametrize("declared, written, held, stripped", [
    ("monochrome2\u0131", "MONOCHROME2I", "U+0131", None),
    ("MONOCHROME2\u0131", "MONOCHROME2I", "U+0131", None),
    ("rgb\u017f", "RGBS", "U+017F", None),
    ("rg\xdf\u0131", "RGSSI", "U+0131", None),
    (" rgb\u017f ", "RGBS", "U+017F", None),
    (" rgb\u017f\t", "RGBS", "U+017F", "its end (U+0009)"),
    ("\tmonochrome2\u0131\n", "MONOCHROME2I", "U+0131",
     "its start (U+0009) and its end (U+000A)"),
    ("\xa0 ybr_\u0131ct", "YBR_ICT", "U+0131", "its start (U+00A0)"),
    ("\u017frgb\u0131\u017f", "SRGBIS", "U+0131, U+017F", None),
    ("ybr_\u0131ct", "YBR_ICT", "U+0131", None),
], ids=["dotless-i", "dotless-i-upper", "long-s", "sharp-s-and-dotless-i",
        "padded-long-s", "long-s-tab", "tab-dotless-i-line-feed",
        "nbsp-then-defined-term", "two-kinds", "spells-a-defined-term"])
def test_a_label_the_file_cannot_hold_is_written_as_before_and_said(
        declared, written, held, stripped):
    """(c): the fallback is the spelling before #612, under a WARNING.

    A character above U+00FF cannot be encoded in a Code String, so the
    `a`-`z` spelling of such a label has no file. Measured on 7325ff3a,
    `'monochrome2ı'` was exported `MONOCHROME2I` and `'rgbſ'` as `RGBS`,
    by Python's own upper-casing; with `a`-`z` alone neither was exported
    at all. The fallback writes what was written, the whole label by
    Unicode's rules (so the `ß` beside a dotless `ı` is `SS` again), and
    says so; the INFO note, which says "the same label", is not written.
    So the WARNING is where rule (a) is met for such a label: whitespace
    other than the space that went from its ends is named there, by end
    and by code point (review of ec12e5cf, finding 2), and spaces alone
    are not (`padded-long-s`).

    Killing mutations: (u1) no fallback; (u2) the fallback's WARNING not
    raised; (u3) the INFO note raised beside it; (u4) the characters
    named unsorted or twice (`two-kinds`); (u14) the stripping not said
    in the WARNING, or said for spaces.
    """
    from isocenter.io_handlers import (_label_as_written,
                                       _label_fallback_warning)

    attributes = {"0028,0004": declared}
    copy, note = _label_as_written(attributes)

    assert copy["0028,0004"] == written
    assert note is None
    sentence = _label_fallback_warning(attributes)
    if stripped is None:
        assert sentence == _FELL_BACK.format(ascii(declared), held,
                                             ascii(written))
    else:
        assert sentence == _FELL_BACK_STRIPPED.format(
            ascii(declared), held, ascii(written), stripped)
    assert _is_printable_ascii(sentence), sentence
    assert _written_photometric(declared) == written
    assert _label_fallback_warning(copy) is None


def test_a_code_string_is_encoded_as_pydicom_encodes_it(tmp_path):
    """What "the file cannot hold" means is pydicom's writer's answer.

    The fallback is decided before the write, from a constant; this holds
    the constant to pydicom's and the boundary to the writer itself:
    U+00FF is written as held and U+0100 is not written at all.
    """
    import io

    from pydicom.charset import default_encoding

    from isocenter.pixel_geometry import _CS_ENCODING, _cs_unencodable

    assert _CS_ENCODING == default_encoding
    assert _cs_unencodable("RGB\xff\u0100\x80\u0131\u0100") == \
        "\u0100\u0131\u0100"
    ds = pydicom.Dataset()
    ds.add_new(0x00280004, "CS", "RGB\xff")
    held = io.BytesIO()
    pydicom.dcmwrite(held, ds, implicit_vr=True, little_endian=True)
    assert b"RGB\xff" in held.getvalue()
    ds.add_new(0x00280004, "CS", "RGB\u0100")
    with pytest.raises(Exception):
        pydicom.dcmwrite(io.BytesIO(), ds, implicit_vr=True,
                         little_endian=True)


def test_a_fallback_no_file_can_hold_is_left_to_fail(tmp_path):
    """Fullwidth letters: the fallback cannot be encoded either.

    Measured on 7325ff3a, `'ｒｇｂ'` was upper-cased to `'ＲＧＢ'` and the
    instance was not exported: the writer's `TypeError`. It is the same
    now, and there is no WARNING about a label "written as" something,
    because nothing is written.
    """
    from isocenter.io_handlers import (_label_as_written,
                                       _label_fallback_warning)

    attributes = {"0028,0004": _FULLWIDTH_LOWER}
    copy, note = _label_as_written(attributes)
    assert copy["0028,0004"] == _FULLWIDTH
    assert note is None
    assert _label_fallback_warning(attributes) is None

    outcome = _export(tmp_path, _pixel_less(_FULLWIDTH_LOWER))
    assert not outcome.ok
    assert isinstance(outcome.error, TypeError), outcome.error


def test_a_multi_valued_label_says_which_value_fell_back():
    from isocenter.io_handlers import (_label_as_written,
                                       _label_fallback_warning)

    attributes = {"0028,0004": [" rgb", "rgb\u017f", b"x\xdf", 5]}
    copy, note = _label_as_written(attributes)

    assert copy["0028,0004"] == ["RGB", "RGBS", b"x\xdf", 5]
    # The note speaks for the value that is the same label respelled, and
    # quotes neither the fallback value nor what it was written as: that
    # one is not "the same label" (review of ec12e5cf, finding 3; u15).
    assert note == _NOTE.format(
        "' rgb', b'x\\xdf', 5", "'RGB', b'x\\xdf', 5",
        f"{_UPPER} and {_LEAD}")
    assert _label_fallback_warning(attributes) == _FELL_BACK.format(
        "'rgb\\u017f'", "U+017F", "'RGBS'")

    # The review's shape: the fallback value first, then one that loses
    # a tab. The tab is the second value's and is named in the note; the
    # WARNING is the first value's alone.
    mixed = {"0028,0004": ["rgb" + chr(0x17F), " rgb\t"]}
    copy, note = _label_as_written(mixed)
    assert copy["0028,0004"] == ["RGBS", "RGB"]
    assert note == _NOTE.format(
        "' rgb\\t'", "'RGB'",
        f"{_UPPER} and {_LEAD} and {_WS}its end (U+0009)")
    assert _label_fallback_warning(mixed) == _FELL_BACK.format(
        "'rgb\\u017f'", "U+017F", "'RGBS'")

    # Nothing but fallback values and a conformant one: no note at all.
    only = {"0028,0004": ["rgb" + chr(0x17F), "RGB"]}
    copy, note = _label_as_written(only)
    assert copy["0028,0004"] == ["RGBS", "RGB"]
    assert note is None

    # Two such values: a sentence each, in the element's order.
    two = {"0028,0004": ["rgb" + chr(0x17F), "RGB", "mono" + chr(0x131)]}
    assert _label_fallback_warning(two) == " ".join([
        _FELL_BACK.format("'rgb\\u017f'", "U+017F", "'RGBS'"),
        _FELL_BACK.format("'mono\\u0131'", "U+0131", "'MONOI'")])


def test_the_readback_quotes_two_values_in_ascii():
    """The arity sentence quotes each value as every other sentence does."""
    from pydicom.dataset import FileMetaDataset

    from isocenter.io_handlers import _readback_label_mismatch

    readback = pydicom.Dataset()
    readback.file_meta = FileMetaDataset()
    readback.file_meta.TransferSyntaxUID = IMPLICIT_VR_LE
    readback.add_new(0x00280004, "CS", ["RG\xdf", "R\tGB"])

    assert _readback_label_mismatch(readback) == (
        "PhotometricInterpretation (0028,0004) is VM 1; the written file "
        "reads back as 2 values ('RG\\xdf', 'R\\tGB')")


def test_an_icon_label_outside_the_repertoire_is_named_as_the_file_holds_it(
        tmp_path):
    """`'rgß'` on an icon: the file and the WARNING hold the same label.

    Measured before: the file held `RGSS` and the row said `'RGSS' ... The
    label was written as declared`. Killing mutation (s3): the judgement
    left on the old spelling, so the row names a label the file does not
    hold.
    """
    rows, exported, grade, _summary = _icon_image(tmp_path, "rg\xdf")

    assert exported.IconImageSequence[0].PhotometricInterpretation == "RG\xdf"
    warnings = [r for r in rows if "PhotometricInterpretation" in r[2]]
    assert len(warnings) == 1, rows
    assert warnings[0][1] == "WARNING"
    assert warnings[0][2].startswith(
        f"PhotometricInterpretation 'RG\\xdf' on the icon at {ICON_PATH} is "
        f"not a label uncompressed pixel data admits: no value of that name "
        f"is defined for it. The label was written as declared, "), warnings
    assert _is_printable_ascii(warnings[0][2]), warnings
    assert "REVIEW_REQUIRED" in grade, grade


def test_an_icon_label_the_file_cannot_hold_is_written_as_before_and_said(
        tmp_path):
    """(c) one depth down, the rows as stored.

    Measured on 7325ff3a: an icon declared `'rgbſ'` was written `RGBS`
    under the WARNING naming `'RGBS'` as "written as declared". At
    d2ac7f1b the instance was not exported. Now the file is the one
    written before, and a second WARNING, naming the item, says what was
    declared and that the file could not hold it.
    """
    rows, exported, grade, summary = _icon_image(tmp_path, "rgb\u017f")

    assert len(summary.written_uids) == 1, rows
    assert exported.IconImageSequence[0].PhotometricInterpretation == "RGBS"
    warnings = [r[2] for r in rows if "PhotometricInterpretation" in r[2]]
    assert len(warnings) == 2, rows
    assert all(r[1] == "WARNING" for r in rows), rows
    fell_back = [w for w in warnings if "cannot hold" in w]
    assert fell_back == ["(0088,0200) item 0: " + _FELL_BACK.format(
        "'rgb\\u017f'", "U+017F", "'RGBS'")], warnings
    judged = [w for w in warnings if "cannot hold" not in w]
    assert judged[0].startswith(
        f"PhotometricInterpretation 'RGBS' on the icon at {ICON_PATH} is "
        f"not a label uncompressed pixel data admits: "), warnings
    assert "REVIEW_REQUIRED" in grade, grade


@pytest.mark.parametrize("declared, written", [
    ("monochrome\xdf", "MONOCHROME\xdf"), ("rgb\xff", "RGB\xff"),
    ("rgb\xb5", "RGB\xb5")],
    ids=["sharp-s", "y-diaeresis", "micro"])
def test_a_pixel_less_label_outside_the_repertoire_is_written_as_held(
        tmp_path, declared, written):
    """The pixel-less door: the file holds what the WARNING quotes (#612).

    Measured before: `'monochromeß'` was written `MONOCHROMESS` under a
    WARNING naming `'MONOCHROMESS'` as "written as declared". `'rgbÿ'`
    and `'rgbµ'` were not exported at all: Python's upper case of each
    (`Ÿ`, `Μ`) is a character no file can hold, and the lower-case one
    is. They are files now, as held.
    """
    outcome = _export(tmp_path, _pixel_less(declared))

    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == written
    assert len(outcome.warnings) == 1, outcome.warnings
    assert outcome.warnings[0].startswith(
        f"PhotometricInterpretation {ascii(written)} is not a label the "
        f"transfer syntax this file was written under admits "
        f"({IMPLICIT_VR_LE}): no value of that name is defined for it. The "
        f"label was written as declared."), outcome.warnings
    assert _is_printable_ascii(outcome.warnings[0]), outcome.warnings


@pytest.mark.parametrize("declared, written, held", [
    ("monochrome2\u0131", b"MONOCHROME2I", "U+0131"),
    ("rgb\u017f", b"RGBS", "U+017F")], ids=["dotless-i", "long-s"])
def test_a_pixel_less_label_the_file_cannot_hold_is_a_file_again(
        tmp_path, declared, written, held):
    """(c) through the worker: the bytes written before #612, two WARNINGs.

    Review finding 1: at d2ac7f1b these instances were not exported
    (`TypeError: function takes exactly 5 arguments (1 given)`), where
    7325ff3a wrote `MONOCHROME2I` and `RGBS`.
    """
    outcome = _export(tmp_path, _pixel_less(declared))

    assert outcome.ok, outcome.error
    assert _wire_label(outcome.output_path).rstrip(b" ") == written
    assert outcome.corrections == [], outcome.corrections
    assert len(outcome.warnings) == 2, outcome.warnings
    assert outcome.warnings[0].startswith(
        f"PhotometricInterpretation {ascii(written.decode('ascii'))} is not "
        f"a label the transfer syntax this file was written under admits ")
    assert outcome.warnings[1] == _FELL_BACK.format(
        ascii(declared), held, ascii(written.decode("ascii")))


@pytest.mark.parametrize("samples, in_file", [(1, b"MONOCHROME2 "),
                                              (3, b"RGBS")])
def test_the_fallback_warning_speaks_only_for_a_label_in_the_file(
        tmp_path, samples, in_file):
    """Over pixels the resolver may write another label than the fallback.

    One sample is `MONOCHROME2` whatever was declared, as before #612 and
    measured on 7325ff3a for `'rgbſ'`: the file holds no `RGBS`, so a
    sentence saying it "was written as 'RGBS'" would be false, and none
    is raised. Over three samples the declared label is kept, the file
    holds `RGBS`, and the sentence is raised. Killing mutation (u13): the
    sentence raised without reading the dataset about to be written.
    """
    arr = np.full((8, 8) if samples == 1 else (8, 8, 3), 100, np.uint8)
    outcome = _export(tmp_path, _image("rgb" + chr(0x17F), samples=samples,
                                       arr=arr))

    assert outcome.ok, outcome.error
    assert _wire_label(outcome.output_path) == in_file
    said = [w for w in outcome.warnings if "cannot hold" in w]
    if samples == 1:
        assert outcome.warnings == [], outcome.warnings
    else:
        assert said == [_FELL_BACK.format("'rgb\\u017f'", "U+017F",
                                          "'RGBS'")], outcome.warnings


def _wire_label(path):
    """The bytes `(0028,0004)` holds in the file at `path`."""
    return pydicom.dcmread(path).get_item(
        0x00280004, keep_deferred=True).value


@pytest.mark.parametrize("declared, changes", [
    ("RGB\t", f"{_WS}its end (U+0009)"),
    ("RGB\n", f"{_WS}its end (U+000A)"),
    (" rgb\r\n", f"{_UPPER} and {_LEAD} and {_WS}its end (U+000A, U+000D)"),
    ("\tRGB", f"{_WS}its start (U+0009)"),
    ("RGB\x1c", f"{_WS}its end (U+001C)"),
    ("RGB\x85", f"{_WS}its end (U+0085)"),
    ("RGB\xa0", f"{_WS}its end (U+00A0)"),
    ("RGB\u2003", f"{_WS}its end (U+2003)"),
    ("\u3000RGB", f"{_WS}its start (U+3000)"),
], ids=["tab", "line-feed", "padded-lower-crlf", "leading-tab", "fs",
        "next-line", "nbsp", "em-space", "ideographic-space-lead"])
def test_whitespace_at_an_end_of_a_label_is_removed_and_said(
        tmp_path, declared, changes):
    """(a): the file is the one written before #612; the note is new.

    Measured on 7325ff3a: each of these over three samples was written
    `RGB`, and only `'\\tRGB'`, `' rgb\\r\\n'` and `'\\u3000RGB'` drew a
    note, which said "its leading spaces removed" of a tab. The bytes are
    the same now -- a file pydicom decodes and `verify_readback=True`
    accepts -- and one INFO note names what was removed, by code point.
    No WARNING: the label the file holds is one the syntax admits. At
    d2ac7f1b the last four were kept, and the last two had no file.
    """
    outcome = _export(tmp_path, _image(declared))

    assert outcome.ok, outcome.error
    assert _wire_label(outcome.output_path) == b"RGB "
    exported = pydicom.dcmread(outcome.output_path)
    assert exported.PhotometricInterpretation == "RGB"
    assert exported.pixel_array.shape == (8, 8, 3)
    assert outcome.corrections == [
        _NOTE.format(ascii(declared), "'RGB'", changes)], outcome.corrections
    assert _is_printable_ascii(outcome.corrections[0])
    assert outcome.warnings == [], outcome.warnings

    verified = _export(tmp_path / "verified", _image(declared),
                       verify_readback=True)
    assert verified.ok, verified.error


@pytest.mark.parametrize("declared", [
    "MONOCHROME2\t", "MONOCHROME2\xa0", " monochrome2\u2003"],
    ids=["tab", "nbsp", "em-space"])
def test_whitespace_at_an_end_of_a_one_sample_label_is_removed(
        tmp_path, declared):
    """One sample: the resolver and the writer read one label (finding 3).

    At d2ac7f1b the resolver read `'MONOCHROME2\\t'` as monochrome and
    left it alone, and the writer wrote it with its tab: pydicom refused
    to decode the file and this library's ingest refused it. Measured on
    7325ff3a: `MONOCHROME2`, as now.
    """
    arr = np.full((8, 8), 100, np.uint8)
    outcome = _export(tmp_path, _image(declared, samples=1, arr=arr))

    assert outcome.ok, outcome.error
    assert _wire_label(outcome.output_path) == b"MONOCHROME2 "
    assert pydicom.dcmread(outcome.output_path).pixel_array.shape == (8, 8)
    assert outcome.warnings == [], outcome.warnings
    verified = _export(tmp_path / "verified",
                       _image(declared, samples=1, arr=arr),
                       verify_readback=True)
    assert verified.ok, verified.error


def test_a_pixel_less_label_ending_in_whitespace_is_written_without_it(
        tmp_path):
    """The pixel-less door, the one a source file reaches with a tab.

    Over pixels `ingest()` refuses a file whose label ends in a tab
    (pydicom cannot decode under it), so whitespace from a source file
    arrives here, or on an icon item. Written `MONOCHROME2` as before
    #612, with the note that was not there, and no WARNING.
    """
    outcome = _export(tmp_path, _pixel_less(" monochrome2\xa0\r\n"))

    assert outcome.ok, outcome.error
    assert _wire_label(outcome.output_path) == b"MONOCHROME2 "
    assert outcome.corrections == [_NOTE.format(
        "' monochrome2\\xa0\\r\\n'", "'MONOCHROME2'",
        f"{_UPPER} and {_LEAD} and {_WS}its end (U+000A, U+000D, U+00A0)")]
    assert outcome.warnings == [], outcome.warnings


def _rgb_source(folder):
    """A Secondary Capture file with an 8x8 RGB frame."""
    import os
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SC_STORAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID, ds.SOPClassUID = meta.MediaStorageSOPInstanceUID, \
        SC_STORAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    ds.ConversionType = "WSD"
    ds.Rows = ds.Columns = 8
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
    ds.PhotometricInterpretation = "RGB"
    ds.PixelRepresentation = 0
    ds.PixelData = np.full((8, 8, 3), YBR, np.uint8).tobytes()
    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)


@pytest.mark.parametrize("label, quoted", [
    ("RG\xdf", "'RG\\xdf'"), ("R\tGB", "'R\\tGB'"),
    ("RGB\xa0X", "'RGB\\xa0X'")], ids=["sharp-s", "tab-inside",
                                      "nbsp-inside"])
def test_the_readback_row_quotes_the_label_in_ascii(tmp_path, label, quoted):
    """The row as stored, not the exception's text (review finding 2).

    `_readback_label_mismatch` quoted the label raw, and the report
    flattens whitespace: at d2ac7f1b the rows for `'RGB\\t'`, `'RGB\\n'`
    and `'RGB\\xa0'` all read `reads back as 'RGB '`, a conformant
    spelling. Those three are stripped before the readback now; a label
    that keeps such a character inside it is quoted as the write-side
    WARNING quotes it.
    """
    from isocenter.io_handlers import ExportError

    src = tmp_path / "src"
    src.mkdir()
    _rgb_source(str(src))
    with DicomSession(str(tmp_path / "rgb.db")) as session:
        session.ingest(str(src))
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        inst.set_attr("0028,0004", label)
        with pytest.raises(ExportError) as raised:
            session.export(str(tmp_path / "out"), verify_readback=True,
                           show_progress=False)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]

    errors = [r[2] for r in rows if r[1] == "ERROR"]
    assert len(errors) == 1, rows
    assert (f"RuntimeError: Readback verification failed: "
            f"PhotometricInterpretation reads back as {quoted}, which the "
            f"transfer syntax the file was written under does not admit "
            ) in errors[0], errors
    assert _is_printable_ascii(errors[0]), errors
    assert f"reads back as {quoted}, which" in str(raised.value)
    assert [reason for _uid, reason in raised.value.failures
            if f"reads back as {quoted}, which" in reason], \
        raised.value.failures


def test_an_icon_label_ending_in_a_tab_is_written_without_it_and_said(
        tmp_path, monkeypatch, caplog):
    """One depth down: the item's note names the item and the code point.

    Threads, so the worker's INFO note reaches `caplog`.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.INFO, logger="isocenter"):
        rows, exported, grade, _summary = _icon_image(tmp_path, "rgb\xa0\t")

    assert exported.IconImageSequence[0].PhotometricInterpretation == "RGB"
    assert rows == [], rows
    assert "PASS" in grade, grade
    notes = [r.getMessage() for r in caplog.records
             if r.levelno == logging.INFO and "(0088,0200) item 0: "
             in r.getMessage()]
    assert len(notes) == 1, [r.getMessage() for r in caplog.records]
    assert notes[0].endswith("(0088,0200) item 0: " + _NOTE.format(
        "'rgb\\xa0\\t'", "'RGB'",
        f"{_UPPER} and {_WS}its end (U+0009, U+00A0)")), notes


def _binary_label_source(folder, door):
    """A file that states the VR `OB` for (0028,0004), holding `rgb `.

    `nopix`: an Encapsulated PDF with no pixel element. `icon`: a
    Secondary Capture whose Icon Image Sequence item states it. Nothing
    is set by hand afterwards: the label reaches the graph as `bytes`.
    """
    import os
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    sop_class = "1.2.840.10008.5.1.4.1.1.104.1" if door == "nopix" \
        else SC_STORAGE
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID, ds.SOPClassUID = meta.MediaStorageSOPInstanceUID, \
        sop_class
    ds.SeriesNumber, ds.InstanceNumber = 1, 1
    ds.StudyDate, ds.StudyTime = "20230101", "120000"
    if door == "nopix":
        ds.Modality = "DOC"
        ds.MIMETypeOfEncapsulatedDocument = "application/pdf"
        ds.EncapsulatedDocument = b"%PDF-1.4\n" + b"x" * 191
        ds.add_new(0x00280004, "OB", b"rgb ")
    else:
        ds.Modality, ds.ConversionType = "OT", "WSD"
        ds.Rows = ds.Columns = 8
        ds.BitsAllocated = ds.BitsStored = 8
        ds.HighBit, ds.PixelRepresentation = 7, 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = np.zeros((8, 8), np.uint8).tobytes()
        icon = Dataset()
        icon.Rows = icon.Columns = 4
        icon.SamplesPerPixel, icon.PlanarConfiguration = 3, 0
        icon.BitsAllocated = icon.BitsStored = 8
        icon.HighBit, icon.PixelRepresentation = 7, 0
        icon.add_new(0x00280004, "OB", b"rgb ")
        icon.add_new(0x7FE00010, "OB", bytes(range(48)))
        ds.IconImageSequence = Sequence([icon])
    ds.save_as(os.path.join(folder, "one.dcm"), enforce_file_format=True)


@pytest.mark.parametrize("door", ["nopix", "icon"])
def test_a_files_own_bytes_label_is_exported_as_held(tmp_path, monkeypatch,
                                                     caplog, door):
    """A `bytes` label is not respelled, from a file or from a hand
    (owner ruling, 2026-10-11; review of ec12e5cf, the blocking finding).

    A file can state a binary VR for (0028,0004), and then ingest holds
    the label as `bytes`, with nothing set by hand. Measured on eed7099d
    for `OB` holding `rgb ` on a pixel-less Encapsulated PDF and in an
    icon item: exported `rgb `, with no note. At ec12e5cf the writer read
    ASCII `bytes` as its text and exported `RGB ` with the respelling
    note: a source's own label changed. It is `rgb ` again, the four
    bytes the source held, and nothing is said.

    Threads, so the worker's notes would reach `caplog`. Killing
    mutation (b1): the writer respelling a `bytes` label as its text.
    """
    for name in _LEVERS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    src = tmp_path / "src"
    src.mkdir()
    _binary_label_source(str(src), door)

    with caplog.at_level(logging.INFO, logger="isocenter"):
        with DicomSession(str(tmp_path / "ob.db")) as session:
            session.ingest(str(src))
            inst = session.store.patients[0].studies[0].series[0].instances[0]
            holder = inst if door == "nopix" else \
                inst.sequences["0088,0200"].items[0]
            # The premise: no hand in it, and the graph holds `bytes`.
            assert holder.attributes["0028,0004"] == b"rgb "
            assert type(holder.attributes["0028,0004"]) is bytes
            session.export(str(tmp_path / "out"), show_progress=False)

    written = list((tmp_path / "out").rglob("*.dcm"))
    assert len(written) == 1, written
    exported = pydicom.dcmread(str(written[0]))
    item = exported if door == "nopix" else exported.IconImageSequence[0]
    assert item.get_item(0x00280004).value in ("rgb", "rgb ", b"rgb ")
    raw = written[0].read_bytes()
    assert raw.count(b"rgb ") == 1 and b"RGB " not in raw
    assert [r.getMessage() for r in caplog.records
            if "is not a defined Code String spelling" in r.getMessage()] \
        == []


def test_a_bytes_label_that_is_not_ascii_does_not_reach_the_file(tmp_path):
    """A characterisation, unchanged by #612: `_merge` does not write it.

    `bytes` that are not ASCII are left as held by the respelling, and
    `_merge` then drops the element with its `DATA_LOSS` row, as it did
    before; so the file holds no label and there is nothing to judge.
    The judgement's own `bytes` arm is pinned at unit level
    (`test_a_character_outside_the_repertoire_is_left_where_it_is`).
    """
    outcome = _export(tmp_path, _pixel_less(b"rg\xdf"))

    assert outcome.ok, outcome.error
    assert "PhotometricInterpretation" not in pydicom.dcmread(
        outcome.output_path)
    assert outcome.warnings == [], outcome.warnings
    assert outcome.corrections == [], outcome.corrections
    assert [(scope, detail.split(":")[0]) for scope, detail in outcome.losses] \
        == [("STANDARD", "Tag 0028,0004 not exported (data loss)")]


def test_a_note_quotes_at_most_a_code_strings_length_of_a_label(tmp_path):
    """The cut is at 16 characters, and it is the note's, not the file's.

    P4 of #612: the cut had no test. Sixteen characters are quoted whole
    and seventeen are cut, in the respelling note and in the WARNING for
    an inadmissible label; the file holds every character either way.
    Killing mutations (s5): the cut at 15, or at 17.
    """
    from isocenter.io_handlers import _cs_quoted, _label_as_written

    sixteen, seventeen = "ABCDEFGHIJKLMNOP", "ABCDEFGHIJKLMNOPQ"
    assert _cs_quoted(sixteen) == f"'{sixteen}'"
    assert _cs_quoted(seventeen) == f"'{sixteen}' (cut at 16 characters)"
    assert _cs_quoted(b"abcdefghijklmnop\xdf") == \
        "b'abcdefghijklmnop' (cut at 16 characters)"
    assert _cs_quoted(b"abcdefghijklmno\xdf") == "b'abcdefghijklmno\\xdf'"

    copy, note = _label_as_written({"0028,0004": seventeen.lower()})
    assert copy["0028,0004"] == seventeen
    assert note == _NOTE.format(
        f"'{sixteen.lower()}' (cut at 16 characters)",
        f"'{sixteen}' (cut at 16 characters)", _UPPER)
    _copy, note = _label_as_written({"0028,0004": sixteen.lower()})
    assert note == _NOTE.format(f"'{sixteen.lower()}'", f"'{sixteen}'",
                                _UPPER)

    outcome = _export(tmp_path, _pixel_less(seventeen))
    assert outcome.ok, outcome.error
    assert pydicom.dcmread(
        outcome.output_path).PhotometricInterpretation == seventeen
    assert len(outcome.warnings) == 1, outcome.warnings
    assert outcome.warnings[0].startswith(
        f"PhotometricInterpretation '{sixteen}' (cut at 16 characters) is "
        f"not a label "), outcome.warnings
