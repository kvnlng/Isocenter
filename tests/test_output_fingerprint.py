"""The output fingerprint's recorder and comparer (#717).

`scripts/output_fingerprint.py` records what the golden cohort exports and
says what moved between two recordings. These tests pin what it records
and what it reports, one decision each; the release-step wiring and the
only session runs are in `test_output_fingerprint_release_step.py`.

No test here names a package module by its dotted name: the mutation
probe's target scan would then demand this file in that module's row and
run it for every mutant there, for tests that are not about that module.
Everything the tests need from the package they reach through the tool.
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import (ExplicitVRLittleEndian, ImplicitVRLittleEndian,
                         RLELossless)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import output_fingerprint as fp  # noqa: E402
from scripts.generate_waveform_test_data import build_ecg_dataset  # noqa: E402
from support.project_secret import FIXED_A  # noqa: E402

CT = "1.2.840.10008.5.1.4.1.1.2"
UID_ROOT = "2.25.717"


def _dataset(n=1, syntax=ExplicitVRLittleEndian):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT
    meta.MediaStorageSOPInstanceUID = f"{UID_ROOT}.{n}"
    meta.TransferSyntaxUID = syntax
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = CT, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = f"{UID_ROOT}.100", f"{UID_ROOT}.200"
    ds.PatientName, ds.PatientID, ds.Modality = "ANONYMIZED", "P1", "CT"
    ds.SliceThickness = "5.000000"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, 16, 15
    ds.SamplesPerPixel, ds.PixelRepresentation = 1, 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = np.arange(16, dtype="<u2").tobytes()
    return ds


def _write(ds, path, implicit=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    pydicom.dcmwrite(str(path), ds, implicit_vr=implicit, little_endian=True,
                     enforce_file_format=True)
    return path


def _record(tmp_path, ds, name="x.dcm", implicit=False):
    return fp.record_dicom(_write(ds, tmp_path / name, implicit=implicit))


def _fingerprint(members, toolchain=None):
    return {"schema": fp.SCHEMA, "provenance": {}, "toolchain": toolchain or {},
            "members": members}


def _member(files, inputs=None, arm="A.dicom", rows=None, steps=None):
    return {"inputs": inputs or {"x.dcm": "sha256:" + "0" * 64},
            "configs": {"A": {
                "steps": steps or {"ingest": {"ingested": 1}},
                "accounting": {"columns": ["action_type", "details"],
                               "rows": rows or []},
                "arms": {arm: {"result": {"written": len(files), "failures": []},
                               "files": files}}}}}


def _file(**elements):
    return {"meta": {}, "elements": {k.replace("_", ","): v
                                     for k, v in elements.items()}}


# -- the recorder ---------------------------------------------------------

def test_an_element_records_its_vr_and_its_encoded_value(tmp_path):
    rec = _record(tmp_path, _dataset())
    assert rec["elements"]["0010,0010"] == "PN 'ANONYMIZED'"

    as_ob, as_ow = _dataset(), _dataset()
    as_ob.add_new(0x00091010, "OB", bytes(range(8)))
    as_ow.add_new(0x00091010, "OW", bytes(range(8)))
    as_ob.add_new(0x00090010, "LO", "GOLD")
    as_ow.add_new(0x00090010, "LO", "GOLD")
    ob = _record(tmp_path, as_ob, "ob.dcm")["elements"]["0009,1010"]
    ow = _record(tmp_path, as_ow, "ow.dcm")["elements"]["0009,1010"]
    assert ob != ow
    assert ob.startswith("OB ") and ow.startswith("OW ")

    # A known tag written as UN: pydicom's reader hands back the
    # dictionary VR, so only the VR as written shows the relabel (#676).
    # pydicom's writer relabels UN on a known tag, so the UN is spliced in.
    as_lo = _dataset()
    as_lo.StudyDescription = "CHEST"
    path = _write(as_lo, tmp_path / "un.dcm")
    lo = b"\x08\x00\x30\x10LO\x06\x00CHEST "
    data = path.read_bytes()
    assert data.count(lo) == 1
    path.write_bytes(data.replace(lo, b"\x08\x00\x30\x10UN\x00\x00\x06\x00\x00\x00CHEST "))
    un = fp.record_dicom(path)["elements"]["0008,1030"]
    assert un.startswith("UN sha256:"), un

    report = fp.compare(_fingerprint({"m": _member({"f.dcm": _file(**{"0009_1010": ob})})}),
                        _fingerprint({"m": _member({"f.dcm": _file(**{"0009_1010": ow})})}))
    kinds = [g.kind for g in report.groups]
    assert kinds == ["VR changed"], report.text()


def test_a_ds_spelled_differently_is_a_different_value(tmp_path):
    long_form, short_form = _dataset(), _dataset()
    short_form.SliceThickness = "5.0"
    a = _record(tmp_path, long_form, "a.dcm")["elements"]["0018,0050"]
    b = _record(tmp_path, short_form, "b.dcm")["elements"]["0018,0050"]
    assert a == "DS '5.000000'"
    assert b == "DS '5.0'"


def test_an_implicit_file_says_its_vr_is_the_readers(tmp_path):
    ds = _dataset(syntax=ImplicitVRLittleEndian)
    rec = _record(tmp_path, ds, implicit=True)
    assert rec["elements"]["0018,0050"] == "DS/implicit '5.000000'"
    # The file meta is always explicit, whatever the data set's syntax.
    assert rec["meta"]["0002,0010"].startswith("UI ")


def test_a_change_inside_a_sequence_is_seen_at_its_path(tmp_path):
    def nested(name):
        ds = _dataset()
        item = Dataset()
        item.ReferencedSOPClassUID, item.PatientName = CT, name
        ds.ReferencedImageSequence = Sequence([item])
        return ds

    old = _record(tmp_path, nested("ANONYMIZED"), "a.dcm")
    new = _record(tmp_path, nested("NESTED^PHI"), "b.dcm")
    assert old["elements"]["0008,1140"] == "SQ items=1"
    assert old["elements"]["0008,1140[0]>0010,0010"] == "PN 'ANONYMIZED'"

    report = fp.compare(_fingerprint({"m": _member({"f.dcm": old})}),
                        _fingerprint({"m": _member({"f.dcm": new})}))
    assert [(g.key, g.kind) for g in report.groups] == [
        ("0008,1140[0]>0010,0010", "changed")], report.text()


def test_pixels_are_compared_as_decoded_samples(tmp_path):
    native = _dataset()
    encoded = _dataset()
    encoded.compress(RLELossless)
    a = _record(tmp_path, native, "native.dcm")
    b = _record(tmp_path, encoded, "rle.dcm")
    assert a["elements"]["7fe0,0010"].endswith("<pixels>")
    assert a["pixels"] == b["pixels"]

    changed = _dataset()
    arr = np.arange(16, dtype="<u2")
    arr[5] += 1
    changed.PixelData = arr.tobytes()
    assert _record(tmp_path, changed, "changed.dcm")["pixels"] != a["pixels"]


def test_a_16_bit_rgb_j2k_file_is_compared_by_its_samples(tmp_path):
    """The #670 shape: pydicom's Pillow route refuses it; its samples still count."""
    import imagecodecs
    from pydicom.encaps import encapsulate
    from pydicom.uid import JPEG2000Lossless

    arr = (np.arange(4 * 4 * 3, dtype="<u2").reshape(4, 4, 3) * 1000)
    ds = _dataset(syntax=JPEG2000Lossless)
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 3, "RGB"
    ds.PlanarConfiguration = 0
    ds.PixelData = encapsulate([imagecodecs.jpeg2k_encode(arr, level=0)])
    ds["PixelData"].VR = "OB"
    rec = _record(tmp_path, ds, "rgb16.dcm")
    expected = fp._h(np.ascontiguousarray(arr).tobytes())  # pylint: disable=protected-access
    assert rec["pixels"].startswith(f"<u2 [4, 4, 3] {expected}"), rec["pixels"]
    if rec["pixels"] != f"<u2 [4, 4, 3] {expected}":
        # pydicom refused; why it refused stays in the recording.
        assert rec["pixels"].startswith(f"<u2 [4, 4, 3] {expected} (imagecodecs; pydicom: ")
        assert "Pillow" in rec["pixels"], rec["pixels"]


def test_waveform_samples_are_compared(tmp_path):
    def ecg(bump):
        ds = build_ecg_dataset(num_samples=50, patient_id="GOLD")
        ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID = f"{UID_ROOT}.9"
        ds.StudyInstanceUID, ds.SeriesInstanceUID = f"{UID_ROOT}.91", f"{UID_ROOT}.92"
        if bump:
            samples = np.frombuffer(ds.WaveformSequence[0].WaveformData, "<i2").copy()
            samples[3] += 1
            ds.WaveformSequence[0].WaveformData = samples.tobytes()
        path = tmp_path / f"ecg{bump}.dcm"
        pydicom.dcmwrite(str(path), ds, enforce_file_format=True)
        return fp.record_dicom(path)

    same, bumped = ecg(0), ecg(1)
    assert same["elements"]["5400,0100[0]>5400,1010"].endswith("<waveform>")
    assert len(same["waveforms"]) == 1
    assert same["waveforms"] != bumped["waveforms"]
    # The encoded words are not what is compared, the samples are.
    assert same["elements"] == bumped["elements"]


def test_the_running_version_is_not_a_difference_and_a_stale_one_is(tmp_path):
    running = fp.running_version()
    note = tmp_path / "r.annotations.json"
    note.write_text(json.dumps({"source": f"isocenter/{running} (IsocenterTest)",
                                "stale": "isocenter/0.0.1"}))
    rec = fp.normalize(fp.record_file(note), fp.output_substitutions())
    assert rec["json"]["source"] == "isocenter/<isocenter-version> (IsocenterTest)"
    assert rec["json"]["stale"] == "isocenter/0.0.1"

    ds = _dataset()
    ds.SoftwareVersions = running
    rec = fp.normalize(_record(tmp_path, ds), fp.output_substitutions())
    # The bare version string is ordinary data and stays exactly as written.
    assert rec["elements"]["0018,1020"] == f"LO {running!r}"


def test_a_release_bump_moves_no_recorded_text_long_or_short(tmp_path, monkeypatch):
    """N2 reaches a text value before its length test and its hash
    (RECORDER 2). A De-identification Method holding a source's values and
    then `isocenter/<version>; ...` is longer than `TEXT_LIMIT` and is
    recorded as a hash, which `normalize()` cannot reach: under RECORDER 1
    it hashed the running version, and every such file moved at every
    release. And a value just under the limit crossed it, or not, by the
    version's length. Recorded under two faked versions of different
    lengths, both read the same."""
    def recorded(version):
        monkeypatch.setattr(fp, "running_version", lambda: version)
        ds = _dataset()
        ds.DeidentificationMethod = [
            "OtherTool 3.2", "site profile 7",
            f"isocenter/{version}; basic@2026c; v1:0ee566b4"]
        ds.ClinicalTrialProtocolName = f"isocenter/{version}; {'x' * 45}"
        return fp.normalize(_record(tmp_path, ds, f"{len(version)}.dcm"),
                            fp.output_substitutions())["elements"]

    short, long_ = recorded("1.0.0"), recorded("10.10.10rc10.dev1")
    assert short["0012,0063"] == long_["0012,0063"]
    assert "sha256:" in short["0012,0063"], "setup: recorded as a hash"
    assert short["0012,0021"] == long_["0012,0021"]


def test_pydicoms_implementation_identity_is_not_a_difference_but_ours_would_be(tmp_path):
    rec = _record(tmp_path, _dataset(), "pydicom.dcm")
    assert rec["meta"]["0002,0012"] == "UI <pydicom-implementation-uid>"
    assert rec["meta"]["0002,0013"] == "SH <pydicom-implementation-version>"

    ours = _dataset()
    ours.file_meta.ImplementationClassUID = f"{UID_ROOT}.1.1"
    ours.file_meta.ImplementationVersionName = "ISOCENTER 1"
    rec = _record(tmp_path, ours, "ours.dcm")
    assert rec["meta"]["0002,0012"] == f"UI '{UID_ROOT}.1.1'"
    assert rec["meta"]["0002,0013"] == "SH 'ISOCENTER 1'"


def _audit_db(path, rows):
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp TEXT,"
                     " action_type TEXT, entity_uid TEXT, details TEXT,"
                     " loss_scope TEXT, element_tag TEXT)")
        conn.executemany("INSERT INTO audit_log (timestamp, action_type, entity_uid,"
                         " details, loss_scope, element_tag) VALUES (?,?,?,?,?,?)",
                         [("2026-09-21T00:00:0%d" % i,) + tuple(r)
                          for i, r in enumerate(rows)])
    return path


def test_the_output_folder_is_not_part_of_an_accounting_row(tmp_path):
    recorded = []
    for name in ("one", "two"):
        root = tmp_path / name
        (root / "out").mkdir(parents=True)
        # The rows carry the resolved spelling; the tool knows the one it
        # was handed. On macOS those differ (/var vs /private/var), so the
        # link stands in for that here on any platform.
        link = tmp_path / f"{name}-link"
        link.symlink_to(root, target_is_directory=True)
        real = os.path.realpath(root)
        db = _audit_db(tmp_path / f"{name}.db", [
            ("EXPORT", real, f"Exported 1 instances to {real}/out", None, None)])
        subs = fp.path_substitutions({"<out:A.dicom>": str(link / "out"),
                                      "<root>": str(link)})
        recorded.append(fp.read_accounting(db, subs))
    assert recorded[0] == recorded[1]
    assert recorded[0]["rows"] == [
        ["EXPORT", "<root>", "Exported 1 instances to <out:A.dicom>", None, None]]


def test_accounting_is_a_multiset():
    rows = [["EXPORT", "u", "a"], ["WARNING", "u", "b"], ["WARNING", "u", "b"]]
    old = _fingerprint({"m": _member({}, rows=rows)})
    shuffled = _fingerprint({"m": _member({}, rows=[rows[1], rows[0], rows[2]])})
    assert fp.compare(old, shuffled).exit_code == 0

    one_fewer = _fingerprint({"m": _member({}, rows=rows[:2])})
    assert fp.compare(old, one_fewer).exit_code == 1

    changed = _fingerprint({"m": _member({}, rows=[rows[0], rows[1],
                                                    ["WARNING", "u", "c"]])})
    report = fp.compare(old, changed)
    assert report.exit_code == 1
    assert {g.kind for g in report.groups} == {"row only in OLD", "row only in NEW"}


def test_the_remediation_trail_is_left_out_and_a_new_row_kind_is_kept(tmp_path):
    db = _audit_db(tmp_path / "s.db", [
        ("REMEDIATION_REMOVE", "u", "removed 0010,0010", None, "0010,0010"),
        ("REMEDIATION_REPLACE", "u", "replaced", None, None),
        ("REMEDIATION_SHIFT_DATE", "u", "shifted", None, None),
        ("FOO", "u", "a kind nobody has seen", None, None),
    ])
    acc = fp.read_accounting(db, [])
    assert acc["columns"] == ["action_type", "entity_uid", "details",
                              "loss_scope", "element_tag"]
    assert [r[0] for r in acc["rows"]] == ["FOO"]


# -- the varying member ----------------------------------------------------

def _cohort(tmp_path, instances=1, varies=True):
    member = tmp_path / "cohort" / "redacted"
    member.mkdir(parents=True)
    for n in range(instances):
        _write(_dataset(n + 1), member / f"redacted-{n + 1}.dcm")
    if varies:
        (member / "VARIES").write_text("regenerate_uid() draws a random UID\n")
    return tmp_path / "cohort"


def _runner(uids):
    """A stand-in for one member's pipeline run, drawing its UID from `uids`."""
    def run(member, work, secret):
        uid = next(uids)
        files = {f"ANON_1/{uid}.dcm": {
            "meta": {"0002,0003": f"UI '{uid}'"},
            "elements": {"0008,0018": f"UI '{uid}'", "0010,0010": "PN 'X'"}}}
        return {"configs": {"A": {
            "steps": {}, "accounting": {"columns": [], "rows": []},
            "arms": {"A.dicom": {"result": {"written": 1, "failures": []},
                                 "files": files}}}}}
    return run


def test_a_varying_member_with_two_instances_is_refused(tmp_path):
    cohort = _cohort(tmp_path, instances=2)
    with pytest.raises(fp.ToolError, match="exactly one instance"):
        fp.take(tmp_path / "out.json", cohort_root=cohort, pydicom_sets=False,
                runner=_runner(iter(["2.25.1", "2.25.2"])))


def test_a_varying_member_is_measured_twice(tmp_path):
    cohort = _cohort(tmp_path)
    out = tmp_path / "out.json"
    fp.take(out, cohort_root=cohort, pydicom_sets=False,
            runner=_runner(iter(["2.25.111", "2.25.222"])))
    member = json.loads(out.read_text())["members"]["synthetic:redacted"]
    assert member["varies"] is True
    files = member["configs"]["A"]["arms"]["A.dicom"]["files"]
    assert list(files) == ["ANON_1/<varies:1>.dcm"]
    (rec,) = files.values()
    # One value in three places is still one value: the equality survives.
    assert rec["elements"]["0008,0018"] == "UI '<varies:1>'"
    assert rec["meta"]["0002,0003"] == "UI '<varies:1>'"
    assert rec["elements"]["0010,0010"] == "PN 'X'"


def test_a_member_marked_varying_that_runs_identically_is_refused(tmp_path):
    cohort = _cohort(tmp_path)
    with pytest.raises(fp.ToolError, match="remove .*VARIES"):
        fp.take(tmp_path / "out.json", cohort_root=cohort, pydicom_sets=False,
                runner=_runner(iter(["2.25.5", "2.25.5"])))


def _steady(member, work, secret):
    """A deterministic stand-in run: one file named after the member."""
    name = member.key.split(":", 1)[1]
    return {"configs": {"A": {
        "steps": {}, "accounting": {"columns": [], "rows": []},
        "arms": {"A.dicom": {"result": {"written": 1, "failures": []},
                             "files": {f"{name}.dcm": _file(**{"0010_0010": "PN 'X'"})}}}}}}


def test_parts_merge_into_the_whole_and_only_the_whole(tmp_path):
    cohort = tmp_path / "cohort"
    for name in ("alpha", "beta", "gamma"):
        _write(_dataset(), cohort / name / f"{name}-1.dcm")
    take = lambda out, members=None: fp.take(  # noqa: E731
        tmp_path / out, members=members, cohort_root=cohort, pydicom_sets=False,
        runner=_steady, log=lambda line: None)
    whole = take("whole.json")
    take("a.json", "synthetic:alpha")
    take("bg.json", "synthetic:[bg]*")
    merged = fp.merge(tmp_path / "merged.json", [tmp_path / "a.json", tmp_path / "bg.json"],
                      cohort_root=cohort, pydicom_sets=False)
    assert merged["members"] == whole["members"]
    assert "members" not in merged["provenance"]
    assert json.loads((tmp_path / "merged.json").read_text()) == merged

    with pytest.raises(fp.ToolError, match="not the whole cohort: missing synthetic:alpha"):
        fp.merge(tmp_path / "m2.json", [tmp_path / "bg.json"],
                 cohort_root=cohort, pydicom_sets=False)
    with pytest.raises(fp.ToolError, match="already in an earlier part"):
        fp.merge(tmp_path / "m3.json", [tmp_path / "whole.json", tmp_path / "a.json"],
                 cohort_root=cohort, pydicom_sets=False)
    other = json.loads((tmp_path / "a.json").read_text())
    other["provenance"]["python"] = "0.0.0"
    (tmp_path / "a-other.json").write_text(json.dumps(other))
    with pytest.raises(fp.ToolError, match="python"):
        fp.merge(tmp_path / "m4.json", [tmp_path / "a-other.json", tmp_path / "bg.json"],
                 cohort_root=cohort, pydicom_sets=False)


def test_only_a_uid_or_a_path_may_vary(tmp_path):
    names = iter(["PN 'A'", "PN 'B'"])

    def run(member, work, secret):
        record = _runner(iter(["2.25.7", "2.25.8"]))(member, work, secret)
        (rec,) = record["configs"]["A"]["arms"]["A.dicom"]["files"].values()
        rec["elements"]["0010,0010"] = next(names)
        return record

    with pytest.raises(fp.ToolError, match="only a UI value or a file path may"):
        fp.take(tmp_path / "out.json", cohort_root=_cohort(tmp_path), pydicom_sets=False,
                runner=run)


# -- the comparer ----------------------------------------------------------

def test_a_changed_input_is_cohort_drift_not_an_output_change():
    old = _fingerprint({"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'A'"})},
                                     inputs={"x.dcm": "sha256:1"})})
    new = _fingerprint({"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'B'"})},
                                     inputs={"x.dcm": "sha256:2"})})
    report = fp.compare(old, new)
    assert report.exit_code == 1
    assert [g.section for g in report.groups] == ["Cohort"], report.text()


def test_differences_are_grouped_by_element_across_files():
    def member(value):
        return _member({f"{n}.dcm": _file(**{"0020_000d": f"UI '{value}.{n}'",
                                              "0010_0010": "PN 'X'"})
                        for n in range(3)})
    report = fp.compare(_fingerprint({"m": member("1.2")}),
                        _fingerprint({"m": member("2.25")}))
    (group,) = report.groups
    assert (group.key, group.vr, group.kind, group.count) == (
        "0020,000d", "UI", "changed", 3)
    assert "3 files" in report.text()


def _instances(uid_of, changed=None):
    """Three distinct instances, each named by its SOP UID as exports are."""
    files = {}
    for n in (1, 2, 3):
        uid = uid_of(n)
        files[f"S/{uid}.dcm"] = {
            "meta": {"0002,0003": f"UI '{uid}'"},
            "elements": {"0008,0018": f"UI '{uid}'", "0020,0013": f"IS '{n}'",
                         "0010,0010": "PN 'Y'" if n == changed else "PN 'X'"},
            "pixels": f"<u2 [2, 2] sha256:{n:016d}"}
    return files


def test_paths_that_all_move_are_still_compared_element_by_element():
    # The new UIDs sort in the reverse order of the old ones, as L10's
    # re-derived UIDs will: pairing by sorted path would compare instance
    # 1 with instance 3 and invent differences in both.
    old = _member(_instances(lambda n: f"1.{n}"))
    new = _member(_instances(lambda n: f"2.{4 - n}"))
    report = fp.compare(_fingerprint({"m": old}), _fingerprint({"m": new}))
    moved = [g for g in report.groups if g.section == "Paths"]
    assert [(g.kind, g.count) for g in moved] == [("moved", 3)], report.text()
    others = [(g.key, g.kind) for g in report.groups if g.section == "Elements"]
    assert sorted(others) == [("0002,0003", "changed"), ("0008,0018", "changed")]
    assert not [g for g in report.groups
                if g.section == "Elements" and g.vr != "UI"], report.text()

    # One real change in one moved file is one group, counted once.
    new = _member(_instances(lambda n: f"2.{4 - n}", changed=2))
    report = fp.compare(_fingerprint({"m": old}), _fingerprint({"m": new}))
    real = [g for g in report.groups if g.section == "Elements" and g.vr != "UI"]
    assert [(g.key, g.kind, g.count) for g in real] == [("0010,0010", "changed", 1)]
    assert "S/2.2.dcm" in real[0].examples[0], real[0].examples


def test_a_kept_uid_says_which_file_moved_where():
    """The folder moved (a date in it), the UIDs did not, two instances swapped numbers.

    Paired by everything but the UIDs, each renumbered instance would meet
    the instance whose number it took and the renumbering would vanish;
    the UIDs say which file is which.
    """
    def files(folder, numbers):
        return {f"{folder}/{uid}.dcm": {
            "meta": {"0002,0003": f"UI '{uid}'"},
            "elements": {"0008,0018": f"UI '{uid}'", "0020,0013": f"IS '{n}'"}}
            for uid, n in zip(("1.1", "1.2"), numbers)}

    report = fp.compare(_fingerprint({"m": _member(files("D1", (1, 2)))}),
                        _fingerprint({"m": _member(files("D2", (2, 1)))}))
    elements = [(g.key, g.kind, g.count) for g in report.groups if g.section == "Elements"]
    assert elements == [("0020,0013", "changed", 2)], report.text()


def test_any_difference_exits_one_and_none_exits_zero():
    one = _fingerprint({"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'X'"})})})
    same = fp.compare(one, json.loads(json.dumps(one)))
    assert same.exit_code == 0
    assert same.text().rstrip().endswith("No difference.")

    other = _fingerprint({"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'Y'"})})})
    moved = fp.compare(one, other)
    assert moved.exit_code == 1
    assert moved.text().rstrip().splitlines()[-1].startswith("1 difference")

    # A narrowed comparison says so on the line a run is recorded by.
    narrowed = fp.compare(one, json.loads(json.dumps(one)), members="m*")
    assert narrowed.text().rstrip().splitlines()[-1] == (
        "No difference in the 1 members matching 'm*' (not the whole cohort).")
    assert "(not the whole cohort)" in fp.compare(one, other, members="m*").text()


def test_a_crash_measured_nothing_and_exits_two(tmp_path):
    part = tmp_path / "part.json"
    part.write_text(json.dumps({"schema": fp.SCHEMA, "members": {}}))
    assert fp.main(["merge", "--out", str(tmp_path / "m.json"), str(part)]) == 2


def test_a_changed_measuring_stick_is_cohort_drift_and_output_is_still_compared():
    member = {"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'X'"})})}
    changed = {"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'Y'"})})}
    old = dict(_fingerprint(member), measure={"recorder": 1, "configs": {"A": "sha256:1"}})
    new = dict(_fingerprint(changed), measure={"recorder": 1, "configs": {"A": "sha256:2"}})
    report = fp.compare(old, new)
    assert sorted((g.section, g.key) for g in report.groups) == [
        ("Cohort", "measuring stick"), ("Elements", "0010,0010")], report.text()
    assert "NOTE: the configurations or the recorder differ" in report.text()
    assert fp.measure()["recorder"] == fp.RECORDER
    assert set(fp.measure()["configs"]) == {"A", "B"}


def test_one_change_in_explicit_and_implicit_arms_is_one_group():
    old = _member({"f.dcm": _file()}, arm="B.dicom")
    old["configs"]["A"]["arms"]["B.dicom-j2k"] = {"result": {}, "files": {"f.dcm": _file()}}
    new = _member({"f.dcm": _file(**{"6000_3000": "OW/implicit sha256:1 len=2"})},
                  arm="B.dicom")
    new["configs"]["A"]["arms"]["B.dicom-j2k"] = {
        "result": {}, "files": {"f.dcm": _file(**{"6000_3000": "OW sha256:1 len=2"})}}
    report = fp.compare(_fingerprint({"m": old}), _fingerprint({"m": new}))
    (group,) = report.groups
    assert (group.key, group.vr, group.kind, group.count) == ("6000,3000", "OW", "added", 2)
    assert group.arms == {"B.dicom", "B.dicom-j2k"}

    # A relabel between the two spellings is still its own kind.
    x = _fingerprint({"m": _member({"f.dcm": _file(**{"6000_3000": "OW sha256:1 len=2"})})})
    y = _fingerprint({"m": _member({"f.dcm": _file(
        **{"6000_3000": "OW/implicit sha256:1 len=2"})})})
    assert [g.kind for g in fp.compare(x, y).groups] == ["VR changed"]


def test_a_toolchain_change_alone_is_not_a_difference():
    member = {"m": _member({"f.dcm": _file(**{"0010_0010": "PN 'X'"})})}
    report = fp.compare(_fingerprint(member, {"pydicom": "3.0.2"}),
                        _fingerprint(member, {"pydicom": "3.1.0"}))
    assert report.exit_code == 0
    assert "pydicom: 3.0.2 -> 3.1.0" in report.text()


def test_a_step_outcome_that_changes_is_a_difference():
    old = _member({}, steps={"ingest": {"ingested": 1}, "audit": "ok"})
    new = _member({}, steps={"ingest": {"ingested": 1},
                             "audit": "raised ValueError: no"})
    report = fp.compare(_fingerprint({"m": old}), _fingerprint({"m": new}))
    assert [(g.section, g.key) for g in report.groups] == [("Outcomes", "A audit")]


# -- what the tool refuses ------------------------------------------------

def _documented_variables():
    text = (ROOT / "docs" / "environment.md").read_text(encoding="utf-8")
    import re
    return set(re.findall(r"^\| \*\*`(ISOCENTER_[A-Z_]+)`\*\*", text, re.M))


def test_a_parallelism_variable_refuses_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    started = []

    def ran(*args, **kwargs):
        # Recorded, not raised: main() turns any exception into exit 2,
        # which is the refusal's exit too. An empty cohort ends the run
        # at once, with exit 0.
        started.append(args)
        return []

    monkeypatch.setattr(fp, "assemble_cohort", ran)
    assert fp.main(["take", "--out", str(tmp_path / "x.json")]) == 2
    assert not started
    assert not (tmp_path / "x.json").exists()

    # Every documented variable is either refused or named as not
    # changing output; a new registry row fails here until it is sorted.
    refused, allowed = set(fp.REFUSED_ENV), set(fp.NOT_OUTPUT_ENV)
    assert not refused & allowed
    assert refused | allowed == _documented_variables()


def test_missing_pydicom_data_refuses_rather_than_skips(tmp_path, monkeypatch):
    for name in fp.REFUSED_ENV:
        monkeypatch.delenv(name, raising=False)

    def offline():
        raise RuntimeError("An error occurred downloading the following files: x")

    monkeypatch.setattr(pydicom.data, "fetch_data_files", offline)
    # Nothing else to run, so a skip would finish at once with exit 0.
    monkeypatch.setattr(fp, "_bundled_members", lambda: [])
    monkeypatch.setattr(fp, "synthetic_members", lambda root: [])
    assert fp.main(["take", "--out", str(tmp_path / "x.json")]) == 2
    assert not (tmp_path / "x.json").exists()


def test_an_unreadable_fingerprint_is_not_a_difference(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_fingerprint({})))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert fp.main(["compare", str(good), str(bad)]) == 2
    other_schema = tmp_path / "schema.json"
    other_schema.write_text(json.dumps(dict(_fingerprint({}), schema=999)))
    assert fp.main(["compare", str(good), str(other_schema)]) == 2
    assert fp.main(["compare", str(good), str(good)]) == 0


def test_the_tools_secret_is_the_suites_fixed_secret():
    assert fp.SECRET == FIXED_A
