"""`verify_readback=True` holds the delivered label against its syntax (#507).

`export(verify_readback=True)` promised the stronger claim -- "a file
exists that decodes to what we meant" -- and did not check the one
element that names what the samples *are*. Measured on a50632d, 3.12.14
and 3.14.7t: an 8x8x3 `uint8` instance declaring `YBR_ICT`,
`YBR_RCT`, `YBR_PARTIAL_422` or `YBR_PARTIAL_420` was written under
Implicit VR Little Endian, the descriptor pass compared
`_READBACK_DESCRIPTORS` (`Rows`, `Columns`, `SamplesPerPixel`,
`NumberOfFrames`, `BitsAllocated` -- `PhotometricInterpretation` was
never in that tuple at all), the pixel decode compared bit patterns and
agreed, and the readback returned `ok=True` on all four. A file carrying
`['YBR_ICT', 'RGB']` passed too.

**This is the first descriptor on which `verify_readback=True` refuses
something the export worker wrote on purpose**, and it is a contract,
not an implementation detail. Since #502 the default write path writes
an inadmissible label as declared and hands back a `WARNING` -- best
output plus a row saying what could not be honoured. `verify_readback`
is the caller who asked for the stronger claim, and for them the same
file is a failure: `ok=False`, an `ERROR` row, `REVIEW_REQUIRED`, and
nothing delivered, because the raise fires against the temporary file
before the rename (#199). What a caller gets that they did not before is
a **demand for conformant output** -- with no new flag, because #26
freezes the public surface and this is not the moment to widen it.

The documented limit, which closes #507 with it: the check is a
*structural* one, driven by `_ADMISSIBLE_PHOTOMETRICS`, and it answers
"could a conformant reader take this label under this syntax" and not
"are these samples really in that colour space". The second question has
no answer from bytes -- three samples are equally RGB and YBR_FULL --
and #372/#448/#482 is the standing ruling that a claim the bytes cannot
prove is not made. So `RGB` over YBR samples still passes here, and a
syntax with no row in the table passes rather than guessing.
"""
import itertools
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from isocenter import io_handlers
from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import (ExportContext, _READBACK_DESCRIPTORS,
                                  _export_instance_worker)
from isocenter.session import DicomSession

SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"
IMPLICIT_VR_LE = "1.2.840.10008.1.2"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
#: JPEG-LS Lossless: decodable by this library, never written by it, and
#: deliberately absent from `_ADMISSIBLE_PHOTOMETRICS`.
JPEG_LS = "1.2.840.10008.1.2.4.80"
#: The four #502 writes with a WARNING and this refuses under a native
#: syntax.
INADMISSIBLE = ("YBR_ICT", "YBR_RCT", "YBR_PARTIAL_422", "YBR_PARTIAL_420")
YBR = (100, 123, 214)

_serial = itertools.count(1)


def _image(label, *, samples=3, arr=None):
    """A hand-built colour instance declaring `label`."""
    if arr is None:
        arr = np.full((8, 8, samples), YBR[:samples], np.uint8)
    inst = Instance(f"1.2.826.0.1.507.{next(_serial)}", SC_STORAGE, 1)
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


def _hand_built(tmp_path, label, syntax=IMPLICIT_VR_LE, *, name="hand.dcm",
                pixels=True):
    """A file the exporter would not write, for the cases it refuses.

    The multi-valued label and the no-row syntax cannot be reached
    through the worker any more -- #502 refuses the first and this
    exporter writes only two syntaxes -- so the readback is called
    directly on a file built by hand. `_verify_readback` is given this
    same dataset as the "what was meant" side, so the descriptor pass
    agrees by construction and the label check is the only thing under
    test.
    """
    ds = pydicom.Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = syntax
    ds.file_meta.MediaStorageSOPClassUID = SC_STORAGE
    ds.file_meta.MediaStorageSOPInstanceUID = "1.2.826.0.1.507.900"
    ds.SOPClassUID = SC_STORAGE
    ds.SOPInstanceUID = "1.2.826.0.1.507.900"
    if pixels:
        ds.Rows, ds.Columns, ds.SamplesPerPixel = 8, 8, 3
        ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
        ds.PixelRepresentation = 0
        ds.PlanarConfiguration = 0
        # Only the uncompressed syntaxes get bytes: pydicom refuses to
        # write a native value under an encapsulated syntax, and the
        # label check is reached with `written_pixels=None` either way,
        # so the descriptors are what the readback needs here.
        if syntax in (IMPLICIT_VR_LE, str(ExplicitVRLittleEndian)):
            ds.PixelData = np.full((8, 8, 3), YBR, np.uint8).tobytes()
    if label is not None:
        ds.PhotometricInterpretation = label
    path = str(tmp_path / name)
    # `enforce_file_format=True`: without the preamble and the 'DICM'
    # prefix, `dcmread` refuses the file and the readback fails at its
    # first line for a reason that is not the one under test.
    ds.save_as(path, enforce_file_format=True)
    return path, ds


# ---------------------------------------------------------------------------
# The refusal: the contract this issue adds.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", INADMISSIBLE)
def test_a_native_inadmissible_label_now_fails_the_readback(tmp_path, label):
    """The four labels #502 writes with a WARNING (#507).

    The default path still writes them -- that is #502's ruling -- and
    the caller who passed `verify_readback=True` asked for the stronger
    claim and gets a refusal instead of a file. Nothing is delivered,
    because the check runs against the temporary file before the rename
    (#199).

    Killing mutations: the label check dropped (all four pass, as they
    did before this change); the reason not naming the syntax (asserted
    below, and the syntax is the whole reason the label is wrong -- the
    same label is fine under `...1.2.4.90`).

    **Not** killed here: the check moved after the pixel decode. All
    four of these still fail with the same reason under that mutant,
    measured -- `_decode_pixels` falls back to imagecodecs, which
    decodes a full-sample array beneath a `YBR_PARTIAL_422` label
    without complaint, so the decode does not raise first.
    `test_an_undefined_label_is_named_before_the_decoder_sees_it` is the
    test that observes the ordering.
    """
    outcome = _export(tmp_path, _image(label), verify_readback=True)

    assert not outcome.ok
    message = str(outcome.error)
    assert "Readback verification failed" in message, message
    assert f"'{label}'" in message, message
    assert IMPLICIT_VR_LE in message, message
    assert not (tmp_path / "out").exists() or not list(
        (tmp_path / "out").glob("*.dcm"))


def test_an_undefined_label_is_named_before_the_decoder_sees_it(tmp_path):
    """The ordering of the label check against the pixel decode (#507).

    `NONSENSE` is a label no syntax defines, and it is what makes the
    ordering observable: with the check before the decode the reason is
    `PhotometricInterpretation reads back as 'NONSENSE', which the
    transfer syntax ... does not admit`, and with it after, pydicom gets
    there first and the reason is `the written pixel data could not be
    decoded (ValueError: Unknown (0028,0004) 'Photometric
    Interpretation' value 'NONSENSE')` -- both measured on this branch.
    The second sends the reader of a compliance report to the pixels for
    a fault that is in one element of the header.

    Killing mutation: the label check moved below the `written_pixels`
    block, measured as **4 failures** -- this test plus all three
    parametrizations of
    `test_an_oddly_spelled_inadmissible_label_gets_its_own_remedy`,
    because pydicom refuses a non-upper-case CS at the decode for the
    same reason it refuses `NONSENSE`. What that mutant does *not*
    reach is the four labels this issue is about: measured,
    `pixel_array` returns `uint8 (8, 8, 3)` for `YBR_ICT`, `YBR_RCT`
    and both `YBR_PARTIAL_*` at this shape -- pydicom special-cases the
    byte count only for `YBR_FULL_422` -- so for them the order changes
    nothing at all.
    """
    outcome = _export(tmp_path, _image("NONSENSE"), verify_readback=True)

    assert not outcome.ok
    message = str(outcome.error)
    assert "reads back as 'NONSENSE'" in message, message
    assert "could not be decoded" not in message, message


@pytest.mark.parametrize("label", ["RGB", "YBR_FULL", "MONOCHROME2",
                                   "PALETTE COLOR", "YBR_FULL_422"])
def test_an_admitted_native_label_still_passes(tmp_path, label):
    """The control, without which "refuses everything" would pass (#507).

    `PALETTE COLOR` and `YBR_FULL_422` are here because the *writer*
    changes them -- the geometry resolver answers `RGB` for a palette
    label at three samples and #470 relabels 422 to `YBR_FULL` -- so
    these two assert the readback judges the label the file carries and
    not the one the instance declared. Killing mutation: the readback
    comparing the file's label against `inst.attributes["0028,0004"]`,
    which is the design this issue rejected; it fails both of these and
    every #516 case below.
    """
    outcome = _export(tmp_path, _image(label), verify_readback=True)

    assert outcome.ok, outcome.error
    assert outcome.warnings == [], outcome.warnings


@pytest.mark.parametrize("declared", [" ybr_ict", "ybr_ict", " YBR_ICT"],
                         ids=["padded-lower", "lower", "padded"])
def test_an_oddly_spelled_inadmissible_label_gets_its_own_remedy(
        tmp_path, declared):
    """Normalization is what makes the reason actionable (#507).

    What the readback sees in a file is right-stripped only, so a
    hand-built `' ybr_ict'` keeps its leading space and its case.
    Normalized, it keys `_PHOTOMETRIC_INADMISSIBLE` and the reason names
    the JPEG 2000 codestream and tells the caller to export with
    `use_compression=True`. Unnormalized it falls to the `None` row, and
    the caller is told only that "no value of that name is defined".

    **A hand-built file, since #532.** The export worker now writes the
    label upper-cased and stripped, so its own output can no longer
    carry this spelling; the readback still reads files it did not
    write, and this is one.

    Killing mutation: `_written_photometric` replaced by `str(label)` in
    `_readback_label_mismatch`.
    """
    path, ds = _hand_built(tmp_path, declared)

    with pytest.raises(RuntimeError) as raised:
        io_handlers._verify_readback(path, ds)

    message = str(raised.value)
    assert "reads back as 'YBR_ICT'" in message, message
    assert "use_compression=True" in message, message
    assert "no value of that name is defined" not in message, message


def test_an_oddly_spelled_admitted_label_is_not_the_label_checks_refusal(
        tmp_path):
    """Where a `' rgb'` file fails, and it is not here (#507, #532).

    The label check normalizes and admits it -- and then the **pixel
    decode** refuses a hand-built file carrying it: `ValueError: Unknown
    (0028,0004) 'Photometric Interpretation' value ' rgb'`, measured.
    That is pydicom reading a CS value the standard says is upper case,
    and the readback's contract for such a file is unchanged.

    **The export of the same declaration now passes**, because since #532
    the worker writes `RGB` and the delivered file decodes. Before, the
    export's own file failed here at the decode.

    The control for the mutation above: under `str(label)` the hand-built
    file is refused by the label check instead.
    """
    path, ds = _hand_built(tmp_path, " rgb")
    written = np.full((8, 8, 3), YBR, np.uint8)

    with pytest.raises(RuntimeError) as raised:
        io_handlers._verify_readback(path, ds, written_pixels=written)

    message = str(raised.value)
    assert "could not be decoded" in message, message
    assert "does not admit" not in message, message

    outcome = _export(tmp_path, _image(" rgb "), verify_readback=True)
    assert outcome.ok, outcome.error


@pytest.mark.parametrize("declared, expected", [
    ("RGB", "YBR_RCT"), ("YBR_ICT", "YBR_ICT"), ("YBR_RCT", "YBR_RCT"),
    ("YBR_FULL", "YBR_FULL")])
def test_a_compressed_colour_export_passes_whatever_516_labelled_it(
        tmp_path, declared, expected):
    """Every label #516's encoder can produce is on the J2K row (#507, #516).

    `_compress_j2k` has three cases and all of them end here: an `RGB`
    source is transformed and **relabelled** `YBR_RCT` (case 1), a
    source already `YBR_RCT`/`YBR_ICT` is transformed and keeps its
    label (case 2, the owner's #490 ruling), and every other 3-sample
    source is encoded with `mct=False` and keeps its label (case 3). The
    readback is the only reader that sees the label after that relabel:
    `_write_pixel_geometry` runs before the encoder, so the writer
    judged `RGB`.

    This is the boundary for the rejected design. Comparing the file's
    label against the declaration fails case 1, which is this library's
    own conformant output; comparing it against the decoder's idea of
    the colour space fails cases 2 and 3, where the label is the
    source's and the samples were never transformed on the way in.
    Killing mutations: either of those two comparisons in place of the
    structural one; the J2K row narrowed to `YBR_RCT` alone (case 2's
    `YBR_ICT` fails).
    """
    outcome = _export(tmp_path, _image(declared), compression="j2k",
                      verify_readback=True)

    assert outcome.ok, outcome.error
    written = pydicom.dcmread(outcome.output_path)
    assert written.file_meta.TransferSyntaxUID == J2K_LOSSLESS
    assert written.PhotometricInterpretation == expected
    assert outcome.warnings == [], outcome.warnings


def test_a_multi_valued_label_in_the_file_fails_the_readback(tmp_path):
    """Two values of a VM 1 attribute is not a file that was meant (#507).

    The writer refuses this before it reaches disk on the pixel arms
    since #502, so a file with pixels is built by hand here -- and the
    check stays in the readback rather than being left to the writer,
    because the readback's subject is the *file* and it is reachable
    from an arm the writer's check is not (see the pixel-less test
    below).

    **The reason names the arity, not re-ingestibility, and that is a
    measurement.** "A file this library cannot re-ingest" is true of a
    file with pixel data -- `ingest()` refuses the `MultiValue` while
    decompressing -- and **false** of a pixel-less one, which re-ingests
    as `IngestSummary(ingested=1, failures=[])` with the graph carrying
    both values. VM 1 is the fault on either.

    Killing mutation: the arity clause dropped, after which the
    normalized label is the `str` of a `MultiValue`
    (`"['YBR_ICT', 'RGB']"`), which is in no syntax's row, so the file
    still fails -- but with a reason naming a label no element carries.
    That is why the assertion below is on the reason and not just on the
    raise.
    """
    path, ds = _hand_built(tmp_path, ["YBR_ICT", "RGB"])

    with pytest.raises(RuntimeError) as raised:
        io_handlers._verify_readback(path, ds)

    message = str(raised.value)
    assert "Readback verification failed" in message, message
    assert "is VM 1" in message, message
    assert "reads back as 2 values" in message, message
    assert "cannot re-ingest" not in message, message


# ---------------------------------------------------------------------------
# The documented limit, and the two ways the check declines to judge.
# ---------------------------------------------------------------------------

def test_a_file_with_no_photometric_interpretation_is_not_judged(tmp_path):
    """No element, no claim (#507).

    An SR, a waveform-only instance and a float16 array all reach the
    readback with no pixel element and no label, and "absent" is not
    "inadmissible". Killing mutation: the membership guard removed, so
    the attribute access raises `AttributeError` -- measured as 8
    failures, this one plus every SR and waveform case in
    `tests/test_export_readback.py`.

    **Classified survivor:** rewriting the guard as
    `readback.get("PhotometricInterpretation", "")` is an *equivalent*
    mutant, not a live one. `_written_photometric("")` is `None` and the
    `normalized is None` arm returns None for it, so the absence still
    goes unjudged by a second route. Nothing distinguishes the two
    spellings from the outside, and a test that tried would be pinning
    an implementation detail.
    """
    path, ds = _hand_built(tmp_path, None, pixels=False)

    io_handlers._verify_readback(path, ds)


@pytest.mark.parametrize("label, expected", [
    ("YBR_ICT", "no pixel element at all"),
    (["YBR_ICT", "RGB"], "is VM 1")],
    ids=["inadmissible", "multi-valued"])
def test_a_pixel_less_file_is_judged_and_offered_a_remedy_that_applies(
        tmp_path, label, expected):
    """The third arm, and the reason strings it made false (#507 review).

    `_write_pixel_geometry` runs only on the two pixel-writing worker
    arms, so an instance with **no pixel element** carries whatever
    `0028,0004` the graph declared straight to disk: measured on this
    branch, an SR-shaped instance declaring `YBR_ICT` exports `ok=True`
    with `warnings == []` (#502's defect, one branch over) and one
    declaring `['YBR_ICT', 'RGB']` exports `ok=True` with the file
    reading back `MultiValue` of length 2. #534 has since routed that
    arm through a writer-side judgement too (a `WARNING`, written as
    declared); the readback still refuses both files for a caller who
    asked for `verify_readback=True`.

    What this test pins is what this check says when it meets such a
    file -- it reads the delivered file and does not care which arm
    wrote it, and both reason strings were false of this arm. The inadmissible label was told to `Export with
    use_compression=True`, which cannot help an instance with nothing
    to compress; the multi-valued one was told the file was one "this
    library cannot re-ingest", when in fact
    `IngestSummary(ingested=1, failures=[])`.

    Killing mutations: the `_PIXEL_ELEMENTS` guard dropped from the
    remedy (the compression advice comes back for a file with no
    pixels); "cannot re-ingest" restored to the arity reason.
    """
    path, ds = _hand_built(tmp_path, label, pixels=False)

    with pytest.raises(RuntimeError, match="Readback verification failed") \
            as raised:
        io_handlers._verify_readback(path, ds)

    message = str(raised.value)
    assert expected in message, message
    assert "use_compression=True" not in message, message
    assert "cannot re-ingest" not in message, message


@pytest.mark.parametrize("syntax", [JPEG_LS, ExplicitVRLittleEndian])
def test_a_syntax_with_no_row_passes_and_one_with_a_row_is_judged(
        tmp_path, syntax):
    """Measured rows only, which is the same discipline as the fallbacks (#507).

    `_ADMISSIBLE_PHOTOMETRICS` has rows for the three uncompressed
    syntaxes and JPEG 2000 Lossless -- everything this exporter writes,
    plus the two uncompressed spellings a hand-built file can carry. A
    file under any other syntax has no row and **passes**: this library
    can decode eight transfer syntaxes it cannot write (#526), and
    inventing a row for one of them would refuse files on a table nobody
    measured. Explicit VR LE is the other half: it is a syntax this
    exporter never writes either, and it *does* have a row, so a
    hand-built file under it is judged rather than waved through.

    Killing mutation: `.get(syntax, _PHOTOMETRIC_ANY_SYNTAX)` in place
    of the `None` arm, after which the JPEG-LS file is refused for a
    label that syntax may well admit.
    """
    path, ds = _hand_built(tmp_path, "YBR_ICT", syntax)

    if syntax == JPEG_LS:
        io_handlers._verify_readback(path, ds)
    else:
        with pytest.raises(RuntimeError, match="YBR_ICT"):
            io_handlers._verify_readback(path, ds)


def test_rgb_over_ybr_samples_still_passes(tmp_path):
    """The limit, stated as a test (#507, #372, #448, #482).

    The check is structural: it asks whether a conformant reader could
    take this label under this syntax, and no reading of the bytes can
    answer whether three samples are really in the colour space the
    label names. `RGB` over YBR samples is admissible under every
    uncompressed syntax and passes here, and that is deliberate -- the
    alternative is a verifier guessing at a colour space, which is the
    claim-without-bytes #372 ruled against.

    **Characterisation, no mutant.** There is no mutation of the shipped
    code this test kills; it exists so the limit is written down
    somewhere a reader of the suite will find it, and so that a later
    change which starts inferring colour spaces has to come through
    here and argue with the docstring.
    """
    outcome = _export(tmp_path, _image("RGB"), verify_readback=True)

    assert outcome.ok, outcome.error


def test_photometric_interpretation_is_not_a_readback_descriptor():
    """The check is its own pass, not a sixth row in the tuple (#507).

    `_READBACK_DESCRIPTORS` compares the file against **the dataset the
    worker serialized**, and `PhotometricInterpretation` is a tag the
    worker itself just wrote onto that dataset -- so the two sides agree
    by construction and no mutation of the writer could make them
    disagree. Adding it there would have been a comparison that cannot
    fail, which is worse than no check: it reads in a diff as coverage.
    The syntax is what the label has to be held against, and the syntax
    is not on `ds` at all -- it is in `file_meta`.

    **Characterisation, no mutant**, and it is the assertion that keeps
    the tuple from growing a row that looks like this fix.
    """
    assert _READBACK_DESCRIPTORS == ("Rows", "Columns", "SamplesPerPixel",
                                     "NumberOfFrames", "BitsAllocated")
    assert "PhotometricInterpretation" not in _READBACK_DESCRIPTORS


# ---------------------------------------------------------------------------
# The parent's half: one ERROR row, and no WARNING row beside it.
# ---------------------------------------------------------------------------

def test_a_verified_run_records_the_error_and_not_the_warning(tmp_path):
    """One row per failed instance, not two (#507, #502).

    The instance takes both paths: `_write_pixel_geometry` appends
    #502's warning sentence, and then the readback refuses the file. Only
    the `ERROR` belongs in the report -- `_report_export_warnings`
    describes a file the caller now has, and there is no file -- so the
    warning is dropped with the outcome. Without this assertion a later
    reorder double-counts the instance in section 4 of the compliance
    report, which is the section the grade is read from.

    Killing mutations: the `ok` filter dropped from
    `_report_export_warnings` (two rows for one instance); the readback
    failure not audited (no row at all, and the run reads PASS).
    """
    bad, good = _image("YBR_RCT"), _image("RGB")
    report = tmp_path / "report.md"
    out = tmp_path / "out"

    with DicomSession(str(tmp_path / "v.db")) as session:
        session.store.patients.append(_graph([bad, good]))
        session.save()
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, verify_readback=True)
        rows = [tuple(r) for r in session.store_backend.get_audit_errors()]
        session.generate_report(str(report))

    assert [p.stem for p in out.rglob("*.dcm")] == [good.sop_instance_uid]
    assert len(summary.failures) == 1, summary
    # `get_audit_errors()` returns (timestamp, action_type, details).
    labelled = [r for r in rows if "PhotometricInterpretation" in r[2]]
    assert [r[1] for r in labelled] == ["ERROR"], labelled
    assert "Readback verification failed" in labelled[0][2], labelled
    assert "REVIEW_REQUIRED" in _grade(report), _grade(report)


def test_the_default_export_still_delivers_the_same_instance(tmp_path):
    """The two contracts, side by side on one instance (#507, #502).

    Without `verify_readback` the same `YBR_RCT` instance is delivered
    with its label intact and a `WARNING` row; with it, nothing is
    delivered and the row is an `ERROR`. That divergence is the whole
    ruling, and this is the test that would go red if someone
    "unified" the two paths in either direction.
    """
    lenient = _export(tmp_path, _image("YBR_RCT"))
    strict = _export(tmp_path, _image("YBR_RCT"), verify_readback=True)

    assert lenient.ok, lenient.error
    assert pydicom.dcmread(
        lenient.output_path).PhotometricInterpretation == "YBR_RCT"
    assert len(lenient.warnings) == 1, lenient.warnings
    assert not strict.ok
    assert "Readback verification failed" in str(strict.error)


def test_the_written_syntax_is_read_from_the_file_not_assumed(tmp_path):
    """`ImplicitVRLittleEndian` is not hard-coded (#507).

    The exporter writes Implicit VR LE natively and JPEG 2000 Lossless
    compressed, and the readback reads whichever the file carries -- so
    a `YBR_ICT` file passes compressed and fails native with no change
    to the check. Killing mutation: the syntax taken from
    `ImplicitVRLittleEndian` or from `ctx.compression` rather than from
    `readback.file_meta.TransferSyntaxUID`, after which the compressed
    file is refused for a label its syntax admits.
    """
    native = _export(tmp_path, _image("YBR_ICT"), verify_readback=True)
    compressed = _export(tmp_path, _image("YBR_ICT"), compression="j2k",
                         verify_readback=True)

    assert not native.ok
    assert compressed.ok, compressed.error
    assert str(ImplicitVRLittleEndian) == IMPLICIT_VR_LE


# ---------------------------------------------------------------------------
# #525: YBR_PARTIAL_* under JPEG 2000 fails the strict contract too.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label", ("YBR_PARTIAL_422", "YBR_PARTIAL_420"))
def test_ybr_partial_under_j2k_fails_the_readback(tmp_path, label):
    """One answer under both syntaxes the exporter writes (#525).

    Natively this file already failed `verify_readback=True` (#507); under
    JPEG 2000 it passed, because the J2K row admitted both subsampled
    labels pending this issue. Breaking by ruling (Q5): a verified
    compressed export of such an instance is now not delivered.

    Killing mutation (M1, the readback half): `YBR_PARTIAL_*` restored to
    the J2K row.
    """
    outcome = _export(tmp_path, _image(label), compression="j2k",
                      verify_readback=True)

    assert not outcome.ok
    message = str(outcome.error)
    assert "Readback verification failed" in message, message
    assert f"reads back as '{label}'" in message, message
    assert f"does not admit ({J2K_LOSSLESS})" in message, message


# ---------------------------------------------------------------------------
# #596: the readback reads what ingest reads, colour space included.
# ---------------------------------------------------------------------------

def _ramp(dtype):
    """Eight by eight by three samples no 8-bit reader could hold."""
    return np.arange(8 * 8 * 3, dtype=dtype).reshape(8, 8, 3) * 100


@pytest.mark.parametrize("declared", ["YBR_FULL", "YBR_FULL_422"])
@pytest.mark.parametrize("dtype", [np.uint16, np.int16])
def test_native_16_bit_ybr_full_fails_the_readback(tmp_path, dtype,
                                                   declared):
    """A file `ingest()` refuses is not one the readback may pass (#596).

    The readback decodes the stored samples (`as_rgb=False`), because
    those are what it compares; `ingest()` decodes with pydicom's
    default, which converts `YBR_FULL` to RGB and refuses anything but
    8-bit samples (`ValueError: Invalid ndarray.dtype 'uint16' for color
    space conversion`). Measured before: the native 16-bit `YBR_FULL`
    export with `verify_readback=True` passed, and neither `ingest()` nor
    `pixel_array` could read the file; the JPEG 2000 one already failed.
    `YBR_FULL_422` is declared here too, and the writer turns it into
    `YBR_FULL` (#470) before the readback sees it.

    Killing mutation (M14): the second decode removed.
    """
    outcome = _export(tmp_path, _image(declared, arr=_ramp(dtype)),
                      verify_readback=True)

    assert not outcome.ok
    message = str(outcome.error)
    assert message.startswith(
        "Readback verification failed: the written file cannot be "
        "ingested by this library"), message
    assert str(tmp_path) not in message, message
    assert "Subject_" not in message, message
    assert not list((tmp_path / "out").glob("*.dcm"))


def test_a_hand_built_16_bit_ybr_full_422_file_fails_the_readback(tmp_path):
    """The `_422` spelling is gated by name, not only by the writer (#596).

    The export writer rewrites `YBR_FULL_422` to `YBR_FULL`, so the
    export-driven test above cannot tell a gate on `YBR_FULL` alone from
    the right one. A hand-built native file carries the `_422` label and
    the 4:2:2 byte count pydicom expects, and the "what was meant" array
    is the stored-sample decode itself, so the first decode and the exact
    compare pass and only the second decode is under test.

    Killing mutation (M15): the gate spelled `== "YBR_FULL"`.
    """
    from isocenter.io_handlers import _decode_pixels

    path, ds = _hand_built(tmp_path, "YBR_FULL_422", name="h422.dcm")
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.PixelData = np.arange(8 * 8 * 2, dtype=np.uint16).tobytes()
    ds.save_as(path, enforce_file_format=True)
    stored, _ = _decode_pixels(pydicom.dcmread(path), as_rgb=False)

    with pytest.raises(RuntimeError) as raised:
        io_handlers._verify_readback(path, ds, written_pixels=stored)

    assert "cannot be ingested by this library" in str(raised.value), \
        str(raised.value)


@pytest.mark.parametrize("compression", [None, "j2k"])
@pytest.mark.parametrize("declared, dtype", [
    ("RGB", np.uint16), ("YBR_FULL", np.uint8)],
    ids=["16-bit-rgb", "8-bit-ybr-full"])
def test_16_bit_rgb_and_8_bit_ybr_full_pass(tmp_path, compression, declared,
                                            dtype):
    """The controls: what `ingest()` reads, the readback still passes (#596).

    16-bit `RGB` needs no colour conversion, and an 8-bit `YBR_FULL`
    converts. Killing mutation (M16'): the second decode replaced by a
    refusal keyed on `BitsAllocated > 8`, which refuses the 16-bit `RGB`
    file `ingest()` reads.

    **Classified survivor (M16):** the decode gated on `BitsAllocated > 8`
    instead of the label is equivalent from the outside. A default decode
    of a file the stored-sample decode already read differs only by the
    colour conversion, which succeeds for every label on 8-bit samples
    and is a no-op for `RGB`, so the gate decides what the check costs,
    not what it answers.
    """
    arr = _ramp(np.uint16) if dtype == np.uint16 else \
        np.full((8, 8, 3), YBR, np.uint8)
    outcome = _export(tmp_path, _image(declared, arr=arr),
                      compression=compression, verify_readback=True)

    assert outcome.ok, outcome.error
