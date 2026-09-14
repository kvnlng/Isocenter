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
    """One predicate for "is this file compressed", not two (#502 review).

    `_finalize_dataset` runs `_compress_j2k` for `compression == 'j2k'`
    and for nothing else, so any *other* truthy value writes a native
    Implicit VR Little Endian file. The worker's `written_syntax` has to
    key on the same comparison: read as truthiness it judged the label
    against the JPEG 2000 row while the file went out native, and
    `YBR_ICT` was written with no warning at all -- measured with
    `compression="rle"`.

    The assertion is on the *file's own* transfer syntax rather than on
    `"rle"` meaning anything, because it does not: `compression` is not
    a documented open enum and this test promises nothing about that
    value. What it pins is the invariant -- the label is judged against
    the syntax the file ends up carrying. Killing mutation:
    `written_syntax` back to `if ctx.compression`.
    """
    outcome = _export(tmp_path, _image("YBR_ICT"), compression="rle")

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
    notes = [c for c in outcome.corrections if "#461" in c]
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
