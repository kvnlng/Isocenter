"""Type 1 image geometry is checked on MR and PET images, not only CT (#879).

PS3.3 makes the Image Plane module (C.7.6.2) Mandatory for MR Image Storage
(A.4) and PET Image Storage (A.21), and its Image Position (Patient)
`(0020,0032)`, Image Orientation (Patient) `(0020,0037)` and Pixel Spacing
`(0028,0030)` are Type 1. `IODValidator` knew CT Image Storage alone, so a
rule removing or emptying one of them on an MR image was applied and the file
written without it. Measured on `main` at 7579d4df over `MR_small` under
`basic@2026c` plus the one rule, for each tag under `REMOVE` and `EMPTY`: one
file written, the element absent or empty, the run graded `PASS`, and the file
carried `(0012,0062) YES`. The same rule on `CT_small` withheld the instance.

The owner's ruling (Q4 A on #879): MR and PET Image Storage get the CT check.
Enhanced and Legacy Converted Enhanced IODs keep their geometry in the
functional groups, not at the top level, so they are not checked; nor are SOP
classes with no Image Plane module.

The check is `IODValidator`'s, which both write paths run: `session.export()`
withholds the instance with one `ERROR` row, and `DicomExporter.write_tree()`
raises `RuntimeError`.

No test pins that `'Common'` stays off MR and PET: measured with it added,
the mutant is equivalent today. Its Type 1 rows are stamped on every file,
and Study Date and Study Time are already written empty when the source
lacks them (the study stamp, and #570's fill), so `absent_type2` would add
nothing. Only Slice Thickness, which `ImagePlane` must not list, is guarded.
"""
import re

import pydicom
import pytest
from pydicom.data import get_testdata_file

from isocenter.io_handlers import DicomExporter, ExportError
from isocenter.session import DicomSession

MR_STORAGE = "1.2.840.10008.5.1.4.1.1.4"
PET_STORAGE = "1.2.840.10008.5.1.4.1.1.128"
ENHANCED_MR_STORAGE = "1.2.840.10008.5.1.4.1.1.4.1"
SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"

GEOMETRY = {
    "0020,0032": "ImagePositionPatient",
    "0020,0037": "ImageOrientationPatient",
    "0028,0030": "PixelSpacing",
}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _source(directory, sop_class=MR_STORAGE, modality=None, drop=()):
    """MR_small, its SOP class rewritten in the dataset and the file meta
    (`IODValidator` reads the file meta's when there is one), and the named
    keywords deleted."""
    directory.mkdir(parents=True, exist_ok=True)
    ds = pydicom.dcmread(get_testdata_file("MR_small.dcm"))
    ds.SOPClassUID = sop_class
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    if modality:
        ds.Modality = modality
    for keyword in drop:
        del ds[keyword]
    ds.save_as(str(directory / "image.dcm"))
    return directory


def _config(tmp_path, rules):
    path = tmp_path / "cfg.yaml"
    lines = ["privacy_profile: basic", "phi_tags:"]
    lines += [f"  '{tag}': {{action: {action}}}" for tag, action in rules.items()]
    if not rules:
        lines = lines[:1]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _run(tmp_path, source, rules=None):
    """ingest -> load_config -> audit -> anonymize -> export -> report.

    Returns the files written, the export error (or None), the ERROR rows'
    details, and the grade token."""
    out = tmp_path / "out"
    report = tmp_path / "report.md"
    raised = None
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        session.load_config(_config(tmp_path, rules or {}))
        session.audit()
        session.anonymize()
        try:
            session.export(str(out), use_compression=False, show_progress=False)
        except ExportError as error:
            raised = error
        session.store_backend.flush_audit_queue()
        errors = [details for _, kind, details
                  in session.store_backend.get_audit_errors() if kind == "ERROR"]
        session.generate_report(str(report))
    files = [pydicom.dcmread(str(p)) for p in out.rglob("*.dcm")] if out.exists() else []
    grades = re.findall(r"\*\*Grade Basis:\*\* ([A-Z_]+)",
                        report.read_text(encoding="utf-8"))
    return files, raised, errors, grades


@pytest.mark.parametrize("action", ["REMOVE", "EMPTY"])
@pytest.mark.parametrize("tag", sorted(GEOMETRY))
@pytest.mark.parametrize("sop_class, modality", [(MR_STORAGE, None), (PET_STORAGE, "PT")],
                         ids=["MR", "PET"])
def test_a_rule_removing_geometry_withholds_an_mr_or_pet_image(
        tmp_path, sop_class, modality, tag, action):
    """Main: one file, the element absent or empty, PASS. Kills the MR or
    PET row deleted, each tag dropped from the module and, on the EMPTY
    rows, a tag's `'1'` read as `'2'`."""
    source = _source(tmp_path / "src", sop_class, modality)
    files, raised, errors, grades = _run(tmp_path, source, {tag: action})

    assert files == [], [str(f.SOPClassUID) for f in files]
    assert isinstance(raised, ExportError)
    expected = f"[Type 1 Error] Missing {tag} in ImagePlane"
    assert [row for row in errors if expected in row] and len(errors) == 1, errors
    assert grades and set(grades) == {"REVIEW_REQUIRED"}, grades


@pytest.mark.parametrize("keyword", sorted(GEOMETRY.values()))
def test_a_source_mr_lacking_geometry_is_withheld(tmp_path, keyword):
    """No rule involved: a source MR without one of the three is withheld
    too. Main: written. The CHANGELOG says so."""
    source = _source(tmp_path / "src", drop=(keyword,))
    files, raised, errors, _ = _run(tmp_path, source)

    assert files == []
    assert isinstance(raised, ExportError)
    tag = next(t for t, k in GEOMETRY.items() if k == keyword)
    assert any(f"Missing {tag} in ImagePlane" in row for row in errors), errors


def test_write_tree_refuses_an_mr_lacking_geometry(tmp_path):
    """The serializer runs the same check. Main: it writes."""
    source = _source(tmp_path / "src", drop=("ImagePositionPatient",))
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(source))
        with pytest.raises(RuntimeError, match=r"Missing 0020,0032 in ImagePlane"):
            DicomExporter.write_tree(session.store.patients[0],
                                     str(tmp_path / "tree"), show_progress=False)


# --------------------------------------------------------------------------
# Guards: green on main, and must stay green.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sop_class, modality", [(MR_STORAGE, None), (PET_STORAGE, "PT")],
                         ids=["MR", "PET"])
def test_an_image_keeping_its_geometry_still_passes(tmp_path, sop_class, modality):
    files, raised, errors, grades = _run(
        tmp_path, _source(tmp_path / "src", sop_class, modality))

    assert raised is None and errors == []
    assert len(files) == 1
    assert files[0].PatientIdentityRemoved == "YES"
    assert set(grades) == {"PASS"}, grades


def test_an_enhanced_mr_is_not_checked(tmp_path):
    """Enhanced MR keeps its geometry in the functional groups; every
    `emri_small*` in pydicom-data lacks the top-level elements. Kills a
    mutant that maps every SOP class, or Enhanced MR."""
    source = _source(tmp_path / "src", ENHANCED_MR_STORAGE,
                     drop=tuple(GEOMETRY.values()))
    files, raised, errors, _ = _run(tmp_path, source)

    assert raised is None and errors == []
    assert len(files) == 1


def test_a_secondary_capture_without_geometry_is_written(tmp_path):
    source = _source(tmp_path / "src", SC_STORAGE, "OT", drop=tuple(GEOMETRY.values()))
    files, raised, errors, _ = _run(tmp_path, source)

    assert raised is None and errors == []
    assert len(files) == 1


def test_an_mr_lacking_slice_thickness_is_written_without_it(tmp_path):
    """Slice Thickness `(0018,0050)` is Type 2 in Image Plane. Listed in
    the module, `absent_type2` would write it zero-length into every MR
    that lacks it: an output change beyond the refusal. Kills the mutant
    that adds it."""
    source = _source(tmp_path / "src", drop=("SliceThickness",))
    files, raised, errors, _ = _run(tmp_path, source)

    assert raised is None and errors == []
    (written,) = files
    assert "SliceThickness" not in written
