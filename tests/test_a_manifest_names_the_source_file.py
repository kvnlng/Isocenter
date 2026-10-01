"""A manifest names each instance's source file, and carries no size keys (#794).

Measured on `main` at 7579d4df over `CT_small` (with a Device Serial
Number a redaction rule names), `MR_small` and `waveform_ecg`: after
`redact()`, the CT item's `file_path` was the string `"None"` in both
formats, though `instance.source_path` still held the file `ingest()`
read. `generate_manifest` wrote `str(instance.file_path)`, and `redact()`
detaches `file_path` from a file whose pixels no longer match. The key's
own documentation says it is the file the instance was ingested from,
which is `source_path`.

The same measurement read `total_size_bytes: 0` and every
`file_size_bytes: 0`: nothing measured a file for the manifest. By owner
ruling (Q4-A) both keys are deleted, not filled; the key-set assertions
below go red if either comes back.
"""
import json
import os
import shutil

import pydicom
from pydicom.data import get_testdata_file

from isocenter.manifest import Manifest, ManifestItem, generate_manifest_file
from isocenter.session import DicomSession

SERIAL = "SN-794"

ITEM_KEYS = {
    "patient_id", "study_instance_uid", "series_instance_uid",
    "sop_instance_uid", "file_path", "modality", "manufacturer",
    "model_name", "anonymized",
}
ROOT_KEYS = {"generated_at", "project_name", "total_files", "items"}


def _inputs(tmp_path):
    src = tmp_path / "input"
    src.mkdir()
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.DeviceSerialNumber = SERIAL
    ct = str(src / "CT_small.dcm")
    ds.save_as(ct)
    mr = str(src / "MR_small.dcm")
    shutil.copy(get_testdata_file("MR_small.dcm"), mr)
    return str(src), ct, mr


def _instances(session):
    return [i for p in session.store.patients for st in p.studies
            for se in st.series for i in se.instances]


def _json_paths(session, out):
    session.generate_manifest(out, format="json")
    with open(out, encoding="utf-8") as fh:
        manifest = json.load(fh)
    return manifest, {it["modality"]: it["file_path"] for it in manifest["items"]}


def test_a_redacted_instance_is_listed_under_the_file_it_was_read_from(tmp_path):
    src, ct, mr = _inputs(tmp_path)
    db = str(tmp_path / "manifest.db")
    session = DicomSession(persistence_file=db)
    try:
        session.ingest(src)
        session.configuration.rules = [
            {"serial_number": SERIAL, "redaction_zones": [[0, 10, 0, 10]]}]
        assert session.redact(show_progress=False) == 1
        ct_inst = [i for i in _instances(session) if i.source_path == ct]
        assert len(ct_inst) == 1 and ct_inst[0].file_path is None, (
            "precondition: redact() must have detached the CT's file_path, "
            "or this test does not reach the case #794 is about")

        manifest, paths = _json_paths(session, str(tmp_path / "m.json"))
        assert paths == {"CT": ct, "MR": mr}

        html_out = str(tmp_path / "m.html")
        session.generate_manifest(html_out, format="html")
        with open(html_out, encoding="utf-8") as fh:
            html = fh.read()
        assert f"<code>{ct}</code>" in html
        assert "<code>None</code>" not in html
        session.save(sync=True)
    finally:
        session.close()

    reopened = DicomSession(persistence_file=db)
    try:
        _, paths = _json_paths(reopened, str(tmp_path / "m2.json"))
        assert paths == {"CT": ct, "MR": mr}
    finally:
        reopened.close()


def test_the_json_manifest_carries_no_size_keys(tmp_path):
    src, _ct, _mr = _inputs(tmp_path)
    session = DicomSession(persistence_file=str(tmp_path / "keys.db"))
    try:
        session.ingest(src)
        manifest, _ = _json_paths(session, str(tmp_path / "m.json"))
    finally:
        session.close()
    assert set(manifest) == ROOT_KEYS
    assert len(manifest["items"]) == 2
    assert all(set(item) == ITEM_KEYS for item in manifest["items"])


def test_an_instance_with_no_source_file_is_null_not_the_string_none(tmp_path):
    """An item with no path writes JSON `null` and an empty HTML cell."""
    item = ManifestItem(patient_id="P", study_instance_uid="1.2",
                        series_instance_uid="1.2.3",
                        sop_instance_uid="1.2.3.4", file_path=None)
    manifest = Manifest(generated_at="now", items=[item])
    out = str(tmp_path / "m.json")
    generate_manifest_file(manifest, out, format="json")
    with open(out, encoding="utf-8") as fh:
        assert json.load(fh)["items"][0]["file_path"] is None
    html_out = str(tmp_path / "m.html")
    generate_manifest_file(manifest, html_out, format="html")
    with open(html_out, encoding="utf-8") as fh:
        html = fh.read()
    assert "<code></code>" in html
    assert "None" not in html


def test_the_session_writes_null_for_an_instance_with_neither_path(tmp_path):
    src, _ct, _mr = _inputs(tmp_path)
    session = DicomSession(persistence_file=str(tmp_path / "nopath.db"))
    try:
        session.ingest(src)
        for inst in _instances(session):
            inst.source_path = None
            inst.file_path = None
        manifest, _ = _json_paths(session, str(tmp_path / "m.json"))
    finally:
        session.close()
    assert [it["file_path"] for it in manifest["items"]] == [None, None]
    assert os.path.exists(str(tmp_path / "m.json"))
