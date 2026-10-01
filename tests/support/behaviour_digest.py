"""One sha256 over what configurations *do* to fixed input (#782).

`CONFIG_VERSION`'s minor is owed whenever the same file is applied
differently to the same input: findings, values a rule writes, tags a rule
reaches, pixel zones or date jitter (owner ruling on #762). The schema test
(`test_config_schema_version.py`) sees keys only, so a behaviour-only change
left `CONFIG_VERSION` where it was and nothing went red. This digest is
the behaviour half: `test_config_behaviour_is_versioned.py` pins it per
version, and a change that moves it must either bump the minor or, before
the version ships, say in the CHANGELOG why it is not a behaviour change.

**What it runs.** Four policies, each over the same three files:

- `basic@2026c`, the floor, and `basic@2026c` with `remove_private_tags:
  false`;
- `none` with a kitchen-sink policy: each action on a tag of each VR
  family, a valued `REPLACE`, a value-less `REPLACE` on a UI and on every
  `VR_DUMMY` string VR, and a repeating-group mask key.

The files are CT_small and MR_small (bundled with pydicom) and three
synthetic images built from CT_small. One holds an element of every
`VR_DUMMY` string VR, a nested sequence holding a UID copy and a date, a
private element, private copies of its own SOP Instance and Study UIDs
(#765), a 60xx overlay element, and a Device Serial Number one pixel zone
matches. The other two are patients of their own: one named `Unknown`, one
with no Patient's Name (#746).

**What it cannot see** (a change to any of these moves no digest, so its
PR must say whether it is a behaviour change): pixel-zone matching beyond
one zone matched by exact serial (no `"*"` serial, no manufacturer or model
match, no zone overlapping the frame edge, no multi-frame image); external
profile files; `SHIFT` and `JITTER` on TM; date ranges other than the one
`JITTER` fixes; `remove_private_tags: false` under the floor or `none`;
nested sequences deeper than one item; private sequences; UIDs only another
instance carries; the reversible lock; WFDB and waveform scenarios; burned-in
text detection (OCR); and every behaviour at export (markers, the Type 1
gates, owner stamps), which `fingerprint/output.json` measures instead.

**What it records,** per policy: each finding as `(entity_type, path, tag,
action, new value)`; after `anonymize()`, every patient, study, series and
instance as the graph holds it (attributes and sequences, the tags reached
and the values written, the shifted dates); and, for the floor, the
redacted frame's sha256.

**Fixed state:** the secret is `FIXED_A` (`load_fixed_secret`), the range
is a fixed `date_jitter`, and the caller sets `ISOCENTER_FORCE_THREADS=1`.
No path, time or random value is recorded, so the digest is the same on
every machine and interpreter; a pydicom upgrade that moves it is a
behaviour change to name.

In `tests/support/` for the reason `project_secret.py` gives.
"""
import hashlib
import json
import os
import shutil
from datetime import date, datetime

import numpy as np
import pydicom
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.session import DicomSession
from support.project_secret import load_fixed_secret

SERIAL = "B3-DIGEST-SN"
JITTER = {"min_days": -40, "max_days": -3}
ZONE = [10, 30, 12, 40]

#: `none`'s whole policy. One rule per line of the action x VR grid the
#: scan and the remediation distinguish.
KITCHEN_SINK = {
    "0010,0010": {"action": "REPLACE"},                       # PN dummy
    "0010,0020": {"action": "REPLACE"},                       # the pseudonym
    "0008,0050": {"action": "REPLACE", "value": "ACC-1"},     # SH, valued
    "0008,0080": {"action": "EMPTY"},                         # LO
    "0008,0081": {"action": "REPLACE"},                       # ST dummy
    "0008,0054": {"action": "REPLACE"},                       # AE dummy
    "0010,4000": {"action": "REPLACE"},                       # LT dummy
    "0040,a160": {"action": "REPLACE"},                       # UT dummy
    "0008,0119": {"action": "REPLACE"},                       # UC dummy
    "0008,010e": {"action": "REPLACE"},                       # UR dummy
    "0010,1010": {"action": "REPLACE"},                       # AS dummy
    "0008,0021": {"action": "JITTER"},                        # DA, shifted
    "0008,002a": {"action": "SHIFT"},                         # DT, shifted
    "0008,0022": {"action": "REPLACE"},                       # DA dummy
    "0008,0031": {"action": "REPLACE"},                       # TM dummy
    "0008,0020": {"action": "REPLACE"},                       # Study Date: the shift
    "0010,0030": {"action": "REMOVE"},                        # DA removed
    "0008,0018": {"action": "REPLACE"},                       # SOP UID, keyed
    "0020,000d": {"action": "REPLACE"},                       # owned UID, keyed
    "0020,000e": {"action": "REPLACE"},                       # owned UID, keyed
    "0020,0052": {"action": "REPLACE"},                       # Frame of Reference
    "0008,1155": {"action": "REPLACE"},                       # nested UID copy
    "0008,1140": {"action": "KEEP"},                          # the sequence kept
    "0008,0060": {"action": "KEEP"},                          # CS kept
    "0018,1000": {"action": "REPLACE", "value": "SERIAL-X"},  # Device Serial
    "60xx,xxxx": {"action": "REMOVE"},                        # overlay mask
}


def _synthetic(path):
    """CT_small with one element per `VR_DUMMY` string VR, a nested
    sequence, a private element, an overlay element and the zone's serial."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.SOPInstanceUID = "1.2.826.0.1.3680043.8.498.782.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.782.2"
    ds.StudyInstanceUID = "1.2.826.0.1.3680043.8.498.782.3"
    ds.PatientID = "DIGEST-782"
    ds.PatientName = "Digest^Pat"
    ds.AccessionNumber = "A782"
    ds.InstitutionName = "Hospital"
    ds.InstitutionAddress = "1 Road"
    ds.RetrieveAETitle = "AE782"
    ds.PatientComments = "comment"
    ds.TextValue = "free text"
    ds.LongCodeValue = "code"
    ds.CodingSchemeURL = "http://example.org"
    ds.PatientAge = "042Y"
    ds.SeriesDate = "20040119"
    ds.AcquisitionDateTime = "20040119072731"
    ds.AcquisitionDate = "20040119"
    ds.SeriesTime = "072731"
    ds.PatientBirthDate = "19700101"
    ds.DeviceSerialNumber = SERIAL
    item = Dataset()
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.ReferencedSOPInstanceUID = "1.2.826.0.1.3680043.8.498.782.9"
    item.ReferencedSOPClassUID = ds.SOPClassUID
    item.add_new(0x00080023, "DA", "20040118")
    ds.ReferencedImageSequence = Sequence([item])
    ds.add_new(0x00090010, "LO", "B3 DIGEST")
    ds.add_new(0x00091001, "LO", "private value")
    # Private copies of this instance's own UIDs (#765): while private tags
    # are kept (`basic-keep-private`) each takes its UID's replacement.
    ds.add_new(0x00091002, "LO", ds.SOPInstanceUID)
    ds.add_new(0x00091003, "LO", ds.StudyInstanceUID)
    ds.add_new(0x60000022, "LO", "overlay description")
    ds.save_as(str(path))


def _named(path, suffix, name):
    """CT_small as a patient of its own (`suffix` keys its IDs and UIDs)
    whose Patient's Name is `name`, or absent when `name` is None (#746):
    a name literally `Unknown` is replaced like any other, and an absent
    one is held as `''`, never a placeholder."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.SOPInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}2"
    ds.StudyInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}3"
    ds.PatientID = f"DIGEST-782-{suffix}"
    if name is None:
        del ds.PatientName
    else:
        ds.PatientName = name
    ds.save_as(str(path))


def _inputs(directory):
    os.makedirs(directory, exist_ok=True)
    shutil.copy(get_testdata_file("CT_small.dcm"), os.path.join(directory, "ct.dcm"))
    shutil.copy(get_testdata_file("MR_small.dcm"), os.path.join(directory, "mr.dcm"))
    _synthetic(os.path.join(directory, "synthetic.dcm"))
    _named(os.path.join(directory, "named-unknown.dcm"), "5", "Unknown")
    _named(os.path.join(directory, "no-name.dcm"), "6", None)


def _canon(value):
    """A JSON-safe, deterministic spelling of a graph value."""
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (date, datetime)):
        return {"date": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _canon(v) for k, v in value.items()}
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return repr(value)
    return {type(value).__name__: str(value)}


def _item(item):
    return {
        "attributes": {tag: _canon(value) for tag, value in sorted(item.attributes.items())},
        "sequences": {tag: [_item(child) for child in seq.items]
                      for tag, seq in sorted(item.sequences.items())},
    }


def _graph(session):
    patients = []
    for patient in sorted(session.store.patients, key=lambda p: str(p.patient_id)):
        studies = []
        for study in sorted(patient.studies, key=lambda s: s.study_instance_uid):
            series = []
            for one in sorted(study.series, key=lambda s: s.series_instance_uid):
                series.append({
                    "uid": one.series_instance_uid,
                    "instances": [
                        {"sop": inst.sop_instance_uid, **_item(inst)}
                        for inst in sorted(one.instances, key=lambda i: i.sop_instance_uid)],
                })
            studies.append({"uid": study.study_instance_uid,
                            "date": _canon(study.study_date),
                            "time": study.study_time,
                            "shifted": study.date_shifted,
                            "series": series})
        patients.append({"id": patient.patient_id, "name": _canon(patient.patient_name),
                         "studies": studies})
    return patients


def _findings(report):
    rows = []
    for finding in report:
        proposal = finding.remediation_proposal
        rows.append([
            finding.entity_type,
            _canon(list(finding.entity_path)),
            finding.tag,
            finding.field_name,
            proposal.action_type if proposal else None,
            _canon(proposal.new_value) if proposal else None,
        ])
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _scenario(root, name, configure, redact=False):
    """Ingest the three files into a fresh store, apply `configure`, audit,
    anonymize (and redact), and return what was found and written."""
    directory = os.path.join(root, name)
    _inputs(os.path.join(directory, "input"))
    with DicomSession(os.path.join(directory, "s.db")) as session:
        load_fixed_secret(session)
        session.ingest(os.path.join(directory, "input"))
        configure(session, directory)
        session.configuration.date_jitter = dict(JITTER)
        report = session.audit()
        found = _findings(report)
        session.anonymize(report)
        record = {"findings": found, "graph": _graph(session)}
        if redact:
            session.redact(show_progress=False)
            # Every frame, not only the zone's: the zone matches by the
            # serial ingest indexed, which `anonymize()` may have replaced
            # in the attributes since.
            frames = {}
            for patient in session.store.patients:
                for study in patient.studies:
                    for series in study.series:
                        for inst in series.instances:
                            pixels = np.ascontiguousarray(inst.get_pixel_data())
                            frames[inst.sop_instance_uid] = hashlib.sha256(
                                pixels.tobytes()).hexdigest()
            record["frames_after_redact"] = frames
            record["sop_after_redact"] = _graph(session)
    return record


def _load(text):
    def configure(session, directory):
        path = os.path.join(directory, "config.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        session.load_config(path)
    return configure


def _floor(session, directory):
    del directory
    session.configuration.add_rule(SERIAL, redaction_zones=[list(ZONE)])


def _none(session, directory):
    _load("privacy_profile: none\n")(session, directory)
    session.configuration.phi_tags = {tag: dict(rule) for tag, rule in KITCHEN_SINK.items()}


def behaviour(root):
    """Every scenario's record, keyed by scenario."""
    return {
        "basic": _scenario(root, "basic", _load("privacy_profile: basic\n")),
        "basic-keep-private": _scenario(
            root, "basic-keep-private",
            _load("privacy_profile: basic\nremove_private_tags: false\n")),
        "floor": _scenario(root, "floor", _floor, redact=True),
        "none-kitchen-sink": _scenario(root, "none", _none),
    }


def behaviour_digest(root):
    """The sha256 of `behaviour(root)` as canonical JSON.

    Args:
        root (str): An empty directory the scenarios run in.

    Returns:
        str: 64 hex digits.
    """
    text = json.dumps(behaviour(root), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
