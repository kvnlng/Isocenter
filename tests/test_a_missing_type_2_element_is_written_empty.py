"""An absent Type 2 element is written zero-length, not refused (#600).

Type 2 means present, and empty when the value is unknown (PS3.5 7.4.5).
`IODValidator` refuses a Type 2 element that is **absent** and accepts one
that is empty, so a CT with no KVP `(0018,0060)` or no Slice Thickness
`(0018,0050)` failed `session.export()` outright. Measured on 76385a5, 3.12
and 3.14t, CT_small with KVP deleted: `ExportError`, and an `ERROR` row
reading `ValueError: Validation Errors: ['[Type 2 Error] Missing 0018,0060
in CTImage']`.

#570 fixed this for Study Time alone, in the shared export worker, after
every merge. The same fill now covers every Type 2 element the validator
would refuse, read from the validator's own table
(`IODValidator.absent_type2`), so nothing outside that table is invented
and a value the graph supplied is never overwritten. Type 1 still refuses:
an empty Type 1 element is still non-conformant, and a fabricated value is
a lie. No row and no note, as for Study Time (#570, owner ruling Q10): the
written file is conformant, and absent and empty mean the same thing for
Type 2.
"""
from datetime import date

import numpy as np
import pydicom
import pytest
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.io_handlers import ExportError
from isocenter.session import DicomSession
from isocenter.validation import IODValidator
# By module name, as `support.` is imported: pytest puts `tests/` on
# `sys.path` for every module it collects. `tests.` resolves only once
# some earlier module has put the repository root there, which a CI
# checkout does not do before this file is collected.
from test_both_write_doors_stamp_one_answer import (
    _hand_built, _image, _read_one, _session_export, _tree_export)

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
OT_STORAGE = "1.2.840.10008.5.1.4.1.1.7"


def _ct_source(directory, *drop):
    """CT_small with the named keywords deleted."""
    directory.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    for keyword in drop:
        del ds[keyword]
    ds.save_as(str(directory / "ct.dcm"))
    return directory


def _ingest_and_export(tmp_path, source, **options):
    """The summary, the output root, the ERROR rows and the report text."""
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        summary = session.export(str(out), show_progress=False,
                                 verify_readback=True, **options)
        session.store_backend.flush_audit_queue()
        errors = session.store_backend.get_audit_errors()
        session.generate_report(str(report))
    return summary, out, errors, report.read_text()


def _assert_present_empty(written, keyword, vr):
    assert keyword in written, f"{keyword} is absent from the written file"
    assert written[keyword].VR == vr
    assert written[keyword].is_empty, (
        f"{keyword} was written with a value: {written[keyword].value!r}")


@pytest.mark.parametrize("compress", [False, True], ids=["native", "j2k"])
def test_a_ct_without_kvp_or_slice_thickness_exports_with_both_empty(
        tmp_path, compress):
    """Both refused on main; both written present and zero-length now.

    `verify_readback=True`, so the file is read back through the same
    validator. No `ERROR` or `WARNING` row, and the report grades PASS:
    the file is conformant, and the fill is #570's Study Time rule, which
    writes no row either.
    """
    source = _ct_source(tmp_path / "src", "KVP", "SliceThickness")
    summary, out, errors, report = _ingest_and_export(
        tmp_path, source, use_compression=compress)

    assert summary.written == 1, summary.failures
    written = _read_one(out)
    _assert_present_empty(written, "KVP", "DS")
    _assert_present_empty(written, "SliceThickness", "DS")
    assert errors == []
    assert "**PASS**" in report, report


def test_a_present_kvp_is_kept_when_slice_thickness_is_filled(tmp_path):
    """The fill writes only what is absent. CT_small's KVP is `120`."""
    source = _ct_source(tmp_path / "src", "SliceThickness")
    summary, out, _, _ = _ingest_and_export(tmp_path, source,
                                            use_compression=False)

    assert summary.written == 1, summary.failures
    written = _read_one(out)
    assert written.KVP == "120"
    _assert_present_empty(written, "SliceThickness", "DS")


#: Everything `IODValidator` demands of a CT image except KVP and Slice
#: Thickness, so a hand-built CT differs from an exportable one only there.
CT_REQUIRED_BUT_TYPE_2 = (
    ("0008,0060", "CT"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
    ("0028,0002", 1), ("0028,0004", "MONOCHROME2"),
)


def _hand_built_ct(extra=()):
    inst = Instance("1.2.826.0.1.600.1", CT_STORAGE, 1)
    inst.file_path = None
    for tag, value in (*CT_REQUIRED_BUT_TYPE_2, *extra):
        inst.set_attr(tag, value)
    inst.set_pixel_data(np.arange(64, dtype=np.uint16).reshape(8, 8))
    patient = Patient("PAT600", "Doe^Jane")
    study = Study("1.2.826.0.2.600", date(2023, 1, 2))
    study.study_time = "120000"
    series = Series("1.2.826.0.3.600", "CT", 1)
    series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    return patient


def test_a_kvp_held_only_by_the_instance_is_kept(tmp_path):
    """A hand-built CT whose KVP is the instance's own attribute.

    The fill runs after the instance's merge, so the instance's `120`
    reaches the file and only Slice Thickness is written empty.
    """
    summary, out = _session_export(
        tmp_path, _hand_built_ct([("0018,0060", "120")]))

    assert summary.written == 1, summary.failures
    written = _read_one(out)
    assert written.KVP == "120"
    _assert_present_empty(written, "SliceThickness", "DS")


def test_a_missing_type_1_element_still_refuses(tmp_path):
    """Type 1 is not filled, and the refusal names only Type 1.

    KVP is gone too, so on main the refusal also named the Type 2 error;
    now KVP is filled and only Image Position is left to refuse.
    """
    source = _ct_source(tmp_path / "src", "ImagePositionPatient", "KVP")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        with pytest.raises(ExportError) as raised:
            session.export(str(tmp_path / "out"), show_progress=False,
                           use_compression=False)

    (_, reason), = raised.value.failures
    assert "[Type 1 Error] Missing 0020,0032" in reason
    assert "[Type 2 Error]" not in reason


def test_a_non_ct_instance_gains_no_ct_elements(tmp_path):
    """The table is per SOP class: an OT image gets no KVP, on either door.

    Green on main as well; it is the guard against a fill that ignores
    the SOP class.
    """
    summary, via_session = _session_export(tmp_path, _hand_built(_image()))
    via_tree = _tree_export(tmp_path, _hand_built(_image()))

    assert summary.written == 1, summary.failures
    for root in (via_session, via_tree):
        written = _read_one(root)
        assert "KVP" not in written
        assert "SliceThickness" not in written


def test_both_doors_write_the_same_empty_type_2(tmp_path):
    """`session.export()` and `write_tree` share the worker, so the fill."""
    summary, via_session = _session_export(
        tmp_path, _hand_built_ct([("0018,0050", "1.0")]))
    via_tree = _tree_export(tmp_path, _hand_built_ct([("0018,0050", "1.0")]))

    assert summary.written == 1, summary.failures
    for root in (via_session, via_tree):
        written = _read_one(root)
        _assert_present_empty(written, "KVP", "DS")
        assert written.SliceThickness == "1.0"


def test_absent_type2_reads_the_validator_table():
    """Unit: exactly the Type 2 tags `validate` would report, and no others.

    A plain `Dataset` has no `file_meta`, so the SOP class is read from the
    dataset itself, as `validate` reads it.
    """
    ds = Dataset()
    ds.SOPClassUID = CT_STORAGE
    ds.StudyDate = "20230102"

    assert set(IODValidator.absent_type2(ds)) == {
        0x00080030, 0x00180050, 0x00180060}
    reported = " ".join(IODValidator.validate(ds))
    for tag in IODValidator.absent_type2(ds):
        assert (f"[Type 2 Error] Missing {tag.group:04x},{tag.element:04x}"
                in reported)

    ds.KVP = ""
    assert set(IODValidator.absent_type2(ds)) == {0x00080030, 0x00180050}

    ds.SOPClassUID = OT_STORAGE
    assert IODValidator.absent_type2(ds) == []
