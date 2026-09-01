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

    Both are required: without them `read_sequence` raises
    `AttributeError: 'DicomBytesIO' object has no attribute
    '_tag_packer'`. They are also two of the four names `REMOVED_IN_V4`
    watches, which makes this the one call shape in the codebase where a
    pydicom bump could turn a working parse into a silent refusal --
    the gate returns None for every failure, so #167 would come back
    reported as "this vendor block is unparseable" when the truth is
    that our parser broke.

    Runs the gate on a sequence it must accept, so a refusal fails here
    rather than only in the audit (#167).
    """
    import struct

    from isocenter.io_handlers import _sequence_from_un_bytes

    def elem(group, element, value):
        return struct.pack("<HHI", group, element, len(value)) + value

    payload = elem(0x0010, 0x0010, b"SECRET^PHI")
    raw = struct.pack("<HHI", 0xFFFE, 0xE000, len(payload)) + payload

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
