"""Isocenter must not use pydicom APIs that 4.0 removes (#141).

`setup.py` caps pydicom at `<4.0`. That cap is a precaution against an
unreleased major version, not a parking space for deprecated calls: each
one left in place is work that has to happen under time pressure the day
4.0 lands.

**These tests must run in-process, and they must clear the warning
filters themselves.** Two things independently destroy this signal, and
either one alone makes the test pass for the wrong reason:

1. `session.export()` fans out through `ProcessPoolExecutor`. A warning
   raised in a worker reaches neither the parent's `catch_warnings` nor
   the caller -- the same loss channel as #126. So these call
   `_export_instance_worker` directly, which is safe because the worker
   is module-scope by design (it has to pickle).
2. `isocenter/__init__.py` sets a process-wide `filterwarnings("ignore",
   module="pydicom.*")` at import, which wins even over the user's own
   `-W` flag. `catch_warnings` + `simplefilter("always")` clears it for
   the duration of the block. That filter is #144; this module works
   around it rather than depending on it either way.

Scoped to pydicom's removal announcements by message rather than
promoting every `DeprecationWarning` to an error: a blanket filter would
catch numpy, pandas, and anything else in the stack, and would go red on
an unrelated dependency bump -- a failure this project did not cause.
"""
import warnings

import numpy as np
import pytest
from pydicom.tag import Tag

from isocenter.entities import (Instance, DicomSequence, DicomItem)
from isocenter.io_handlers import (DicomExporter, ExportContext,
                                   _export_instance_worker)


#: Substrings of the pydicom 3.x messages announcing 4.0 removals.
REMOVED_IN_V4 = ("is_little_endian", "is_implicit_VR",
                 "pixel_data_handlers", "write_like_original")


def _removals(recorded):
    return sorted({str(w.message) for w in recorded
                   if issubclass(w.category, DeprecationWarning)
                   and any(m in str(w.message) for m in REMOVED_IN_V4)})


def _un_sequence_fixture():
    """The one-item implicit-VR sequence the gate must accept."""
    import struct

    def elem(group, element, value):
        return struct.pack("<HHI", group, element, len(value)) + value

    payload = elem(0x0010, 0x0010, b"SECRET^PHI")
    return struct.pack("<HHI", 0xFFFE, 0xE000, len(payload)) + payload


#: A payload length whose little-endian first two bytes are `41 5A` --
#: "AZ", both inside the ASCII range pydicom's VR sniffer treats as
#: proof of explicit VR. `filereader.py:376-379` says in its own comment
#: that the sniffer only fails to recover once the first element exceeds
#: about 0x4141 bytes; this is that case, deliberately.
_VR_AMBIGUOUS_LENGTH = 0x5A41


def _vr_sensitive_sequence_fixture():
    """A sequence whose parse DEPENDS on the implicitness argument.

    The control for the read-stream test. On the ordinary fixture above,
    `read_sequence` returns the same dataset whether it is told implicit
    or explicit -- not because the flag is ignored, but because the
    sniffer at `filereader.py:371-379` re-derives the truth from the
    bytes and silently corrects the caller. A test that flipped the
    stream attribute on THAT fixture would stay green even if pydicom
    started honouring the attribute, because nothing about the fixture
    can tell the two answers apart.

    Here the first element's length field is chosen so the sniffer's
    recovery cannot fire, which makes the implicitness argument
    load-bearing and gives the test something to discriminate against.
    """
    import struct

    payload = (struct.pack("<HHI", 0x0009, 0x1001, _VR_AMBIGUOUS_LENGTH)
               + b"V" * _VR_AMBIGUOUS_LENGTH)
    return struct.pack("<HHI", 0xFFFE, 0xE000, len(payload)) + payload


def _instance():
    inst = Instance("1.2.3.3", "1.2.840.10008.5.1.4.1.1.2", 1)
    inst.attributes = {
        "0010,0010": "Dep^Check", "0008,0060": "CT",
        "0028,0010": 8, "0028,0011": 8, "0028,0100": 16,
        "0028,0101": 16, "0028,0102": 15, "0028,0103": 0,
        "0028,0002": 1, "0028,0004": "MONOCHROME2",
        "0008,0016": "1.2.840.10008.5.1.4.1.1.2", "0008,0018": "1.2.3.3",
        "0008,0030": "120000", "0018,0050": "1.0", "0018,0060": "120",
        "0020,0032": "0\\0\\0", "0020,0037": "1\\0\\0\\0\\1\\0",
        "0028,0030": "1.0\\1.0",
    }
    inner = DicomItem()
    inner.attributes = {"0008,0100": "INNER", "0008,0104": "Inner Meaning"}
    outer = DicomItem()
    outer.attributes = {"0008,0100": "OUTER", "0008,0104": "Outer Meaning"}
    outer.sequences = {"0040,a168": DicomSequence("0040,a168", items=[inner])}
    inst.sequences = {"0008,1032": DicomSequence("0008,1032", items=[outer])}
    # On the instance, not on the ExportContext: the worker reads
    # `inst.pixel_array` and only falls back to loading from the sidecar.
    inst.pixel_array = np.arange(64, dtype=np.uint16).reshape(8, 8)
    return inst


@pytest.mark.parametrize("compression", [None, "j2k"])
def test_writing_an_instance_uses_no_pydicom_api_removed_in_v4(
        tmp_path, compression):
    """Covers all four sites at once, which is why it goes through the
    worker rather than the helpers individually: `_create_ds` runs for
    every instance, but the JPEG 2000 branch needs compression, the
    sequence-item builder needs a nested sequence, and `save_as` is only
    reached at the end of a real write.
    """
    inst = _instance()
    ctx = ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / "1.2.3.3.dcm"),
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3.1", "0008,0020": "20230101"},
        series_attributes={"0020,000e": "1.2.3.2"},
        compression=compression,
    )

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        outcome = _export_instance_worker(ctx)

    assert outcome.ok, f"fixture must export or this pins nothing: {outcome.error}"

    offenders = _removals(recorded)
    assert not offenders, (
        "writing an instance used a pydicom API that 4.0 removes:\n  "
        + "\n  ".join(offenders))


def test_the_worker_actually_reaches_the_sequence_and_compression_paths(tmp_path):
    """Guards the test above rather than the code.

    If the fixture ever stopped producing a nested sequence or compressed
    output, the deprecation test would keep passing while silently
    covering one site instead of four.
    """
    import pydicom

    inst = _instance()
    ctx = ExportContext(
        instance=inst,
        output_path=str(tmp_path / "out" / "1.2.3.3.dcm"),
        patient_attributes={"0010,0020": "P1"},
        study_attributes={"0020,000d": "1.2.3.1", "0008,0020": "20230101"},
        series_attributes={"0020,000e": "1.2.3.2"},
        compression="j2k",
    )
    assert _export_instance_worker(ctx).ok

    ds = pydicom.dcmread(ctx.output_path)
    assert ds.file_meta.TransferSyntaxUID.is_compressed, \
        "compression path not exercised"
    outer = ds[(0x0008, 0x1032)]
    assert outer.value[0][(0x0040, 0xa168)] is not None, \
        "nested sequence path not exercised"


def test_importing_isocenter_uses_no_pydicom_api_removed_in_v4():
    """Module-scope imports are what make the `<4.0` cap load-bearing: an
    unguarded import of a package 4.0 removes fails at import time,
    before any caller can degrade gracefully."""
    import importlib

    import isocenter.pixel_analysis

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        importlib.reload(isocenter.pixel_analysis)

    offenders = _removals(recorded)
    assert not offenders, (
        "importing isocenter announced a pydicom removal:\n  "
        + "\n  ".join(offenders))


def test_the_un_sequence_gate_uses_no_pydicom_api_removed_in_v4():
    """`_sequence_from_un_bytes` sets two `DicomIO` flags 4.0 may remove.

    They are two of the four names `REMOVED_IN_V4` watches, which makes
    this the one call shape in the codebase where a pydicom bump could
    turn a working parse into a silent refusal -- the gate returns None
    for every failure, so #167 would come back reported as "this vendor
    block is unparseable" when the truth is that our parser broke.

    **What this docstring said until #285 was half false**, and the
    correction is worth carrying: `read_sequence` does NOT raise
    `AttributeError: 'DicomBytesIO' object has no attribute
    '_tag_packer'` when the READ stream's flags are unset. It consults
    neither attribute -- it takes both as positional arguments. That
    error belongs to the WRITE stream and to `is_little_endian`, whose
    setter is what builds the packers. The two tests below measure both
    halves; keeping both names in `REMOVED_IN_V4` is still right,
    because the write stream needs them.

    Runs the gate on a sequence it must accept, so a refusal fails here
    rather than only in the audit (#167).
    """
    from isocenter.io_handlers import _sequence_from_un_bytes

    raw = _un_sequence_fixture()

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        parsed = _sequence_from_un_bytes(raw, Tag(0x0009, 0x1003), "iso8859")

    assert parsed is not None and len(parsed) == 1, (
        "the gate refused a sequence it re-encodes byte for byte; "
        "a pydicom change, not a malformed vendor block")

    offenders = _removals(recorded)
    assert not offenders, (
        "the UN-sequence gate used a pydicom API that 4.0 removes:\n  "
        + "\n  ".join(offenders))


def test_the_gate_can_still_read_a_datasets_character_set():
    """`populate_attrs` reads `Dataset._character_set` to decode text.

    A private name, so pydicom may rename it without announcing a
    removal -- and the `getattr` default would then hide the rename by
    silently falling back to `iso8859` for every dataset, including one
    that declared a Specific Character Set. Both return shapes are valid
    `encoding` arguments; what this pins is that the attribute exists
    and is non-empty.
    """
    from pydicom.dataset import Dataset

    bare = getattr(Dataset(), "_character_set", None)
    assert bare, "Dataset._character_set is gone or empty"

    declared = Dataset()
    declared.SpecificCharacterSet = "ISO_IR 100"
    assert declared._character_set, "a declared character set reads as empty"


def test_the_gates_write_stream_is_the_one_whose_vr_mode_changes_the_bytes():
    """The gate's WRITE stream, not its read stream, is what #167 needs.

    **Green on HEAD by construction, and that is the point.** This is a
    characterization test of pydicom, not a red-first test of ours: its
    value is going red on a pydicom bump that would otherwise break the
    UN-sequence gate silently.

    The gate accepts a vendor block only when it re-encodes byte for
    byte. That comparison is made against the WRITE stream's VR mode. A
    wrong value there is not an error -- it is a different, valid
    encoding, so `write_sequence` succeeds, the bytes differ, the gate
    returns `None`, and #167 comes back reported to users as "this
    vendor block is unparseable" with no exception anywhere.

    Note what is deliberately NOT asserted: that leaving the write
    stream's VR mode unset raises. It does not, for the gate's own
    datasets -- `write_dataset` falls back to a dataset's
    `original_encoding`, and datasets that came out of `read_sequence`
    carry `(True, True)`. The third assertion pins that fallback,
    because it is the reason "unset happens to work" is true today.
    """
    from pydicom.dataelem import DataElement
    from pydicom.filebase import DicomBytesIO
    from pydicom.filereader import read_sequence
    from pydicom.filewriter import write_sequence

    raw = _un_sequence_fixture()
    tag = Tag(0x0009, 0x1003)

    src = DicomBytesIO(raw)
    src.is_little_endian = True
    src.is_implicit_VR = True
    parsed = read_sequence(src, True, True, len(raw), "iso8859")

    assert parsed[0].original_encoding == (True, True), (
        "a dataset from `read_sequence` no longer records its own "
        "encoding; the write stream's flags stop having a fallback")

    def encoded(implicit):
        out = DicomBytesIO()
        out.is_little_endian = True
        out.is_implicit_VR = implicit
        write_sequence(out, DataElement(tag, "SQ", parsed), "iso8859")
        return out.getvalue()

    assert encoded(True) == raw, (
        "the write stream in implicit VR no longer round-trips an "
        "implicit-VR sequence; the gate would refuse every one of them")
    assert encoded(False) != raw, (
        "explicit VR on the write stream now produces the same bytes as "
        "implicit -- the byte-equality gate has stopped distinguishing "
        "the two, so a wrong flag would no longer be caught here")


def test_the_gates_read_stream_does_not_consult_the_streams_vr_mode():
    """The gate's READ stream sets a VR mode nothing reads.

    `read_sequence` takes implicitness as a positional argument and
    threads it down to the element generator; the attribute never
    appears on a stream anywhere in `filereader.py`. So the READ stream's
    VR mode is not read, and setting it wrong changes nothing.

    **The control is the whole test, and the first version of it had
    none.** On the ordinary gate fixture, `read_sequence` returns the
    same dataset whether it is *told* implicit or explicit -- so
    "flipping the stream attribute changed nothing" was uninformative,
    and this test would have stayed green even if pydicom had started
    honouring the attribute. Worse, the reason was not the
    `filereader.py:368-369` short-circuit named in the earlier
    docstring: that needs `implicit_vr_is_assumed=True` to fire, and
    with `False` control falls through to the byte sniffer below it,
    which re-derives implicitness and corrects the caller.

    So this asks two questions in order. First, on a fixture built to
    defeat that sniffer, does the implicitness *argument* change the
    parse? It must -- otherwise the second question is unanswerable.
    Then, holding that argument fixed, does the stream *attribute*
    change it? It must not.

    That is what goes red if a pydicom release starts consulting
    `fp.is_implicit_VR` on the read path -- the only event that would
    make deleting the read stream's assignment in
    `_sequence_from_un_bytes` unsafe.
    """
    from pydicom.filebase import DicomBytesIO
    from pydicom.filereader import read_sequence

    from isocenter.io_handlers import _sequence_from_un_bytes

    def parse(raw, told_implicit, stream_vr_mode):
        fp = DicomBytesIO(raw)
        fp.is_little_endian = True
        if stream_vr_mode is not None:
            fp.is_implicit_VR = stream_vr_mode
        parsed = read_sequence(fp, told_implicit, True, len(raw), "iso8859")
        return [sorted(str(tag) for tag in ds.keys()) for ds in parsed]

    # -- the control: on this fixture, implicitness is load-bearing ----
    # Argument and attribute AGREE in both control runs, so the control
    # stays valid whichever of the two pydicom reads. If they disagreed
    # here, the simulated "pydicom now honours the attribute" event
    # would break the control instead of the assertion below it, and
    # this test would go red for the wrong reason.
    sensitive = _vr_sensitive_sequence_fixture()
    as_implicit = parse(sensitive, True, True)
    as_explicit = parse(sensitive, False, False)

    assert as_implicit == [["(0009,1001)"]], as_implicit
    assert as_explicit != as_implicit, (
        "the control has stopped controlling: pydicom now parses this "
        "fixture identically whether told implicit or explicit, so the "
        "assertions below can no longer tell an ignored stream attribute "
        "from a consulted one. Rebuild the fixture -- see "
        "`_vr_sensitive_sequence_fixture` -- before trusting this test")

    # -- the question: the stream attribute is not the channel ---------
    for stream_vr_mode in (True, False, None):
        assert parse(sensitive, True, stream_vr_mode) == as_implicit, (
            f"with the stream's VR mode set to {stream_vr_mode!r}, "
            "`read_sequence` parsed differently from the run that only "
            "passed the argument -- the read stream's attribute has "
            "become load-bearing and the gate's assignment to it can no "
            "longer be called symmetry")

    # -- and the gate's own fixture still goes through the gate --------
    raw = _un_sequence_fixture()
    through_the_gate = _sequence_from_un_bytes(raw, Tag(0x0009, 0x1003),
                                               "iso8859")
    assert through_the_gate is not None and len(through_the_gate) == 1
    assert through_the_gate[0].PatientName == "SECRET^PHI"
