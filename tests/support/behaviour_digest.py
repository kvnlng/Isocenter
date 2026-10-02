"""One sha256 over what configurations *do* to fixed input (#782).

`CONFIG_VERSION`'s minor is owed whenever the same file is applied
differently to the same input: findings, values a rule writes, tags a rule
reaches, pixel zones or date jitter (owner ruling on #762). The schema test
(`test_config_schema_version.py`) sees keys only, so a behaviour-only change
left `CONFIG_VERSION` where it was and nothing went red. This digest is
the behaviour half: `test_config_behaviour_is_versioned.py` pins it per
version, and a change that moves it must either bump the minor or, before
the version ships, say in the CHANGELOG why it is not a behaviour change.

**What it runs.** Five policies, each over the same seven files:

- `basic@2026c`, the floor, and `basic@2026c` with `remove_private_tags:
  false`;
- `none` with a kitchen-sink policy: each action on a tag of each VR
  family, a valued `REPLACE`, a value-less `REPLACE` on a UI and on every
  `VR_DUMMY` string VR, and a repeating-group mask key;
- `basic@2026c` with a dict zone on a `"*"` rule beside an exact rule for
  another machine (`STAR_CONFIG`). The store is audited, anonymized and
  redacted under the file's rules. Then fixed text regions on the synthetic
  image are classified against the rules covering it, and the suggestions
  `ConfigAutomator` makes are applied (#808, #814, #899). Redaction under
  the rules the suggestions leave is not seen until #908.

The files are CT_small and MR_small (bundled with pydicom) and five
synthetic images built from CT_small. One holds an element of every
`VR_DUMMY` string VR, a nested sequence holding a UID copy and a date, a
private element, private copies of its own SOP Instance and Study UIDs
(#765), a 60xx overlay element, and a Device Serial Number one pixel zone
matches. The other four are patients of their own: one named `Unknown`
and one with no Patient's Name (#746); one with no Patient ID, keyed by its
study (#584); and one the store classes legacy, so its pseudonym and offset
are the unkeyed pre-0.9.7 derivations (`patients.jitter_scheme`, #903).

**How it runs** (#903): each scenario ingests in one session, classes the
legacy patient in the closed store, then reopens and runs `audit()` and
`anonymize()` twice. The second pass must find nothing and change nothing:
every UID the first pass minted verifies under this store's secret.

**What it cannot see** (a change to any of these moves no digest, so its
PR must say whether it is a behaviour change): pixel-zone matching beyond
an exact serial and one `"*"` rule (no manufacturer or model match, no zone
overlapping the frame edge, no multi-frame image); external
profile files; `SHIFT` and `JITTER` on TM; date ranges other than the one
`JITTER` fixes; `remove_private_tags: false` under the floor or `none`;
nested sequences deeper than one item; private sequences; UIDs only another
instance carries; a re-key of an ID-less subject by a later file
carrying its Patient ID (#584's `_its_key_is_in_use`); a patient merge
(#548); a pass after a reopen with statuses the store holds; the
reversible lock; WFDB and waveform scenarios; reading burned-in text (OCR
itself, and which instances `scan_pixel_content()` selects); and every
behaviour at export (markers, the Type 1
gates, owner stamps), which `fingerprint/output.json` measures instead.

**What it records,** per policy: each finding as `(entity_type, path, tag,
action, new value)`; after `anonymize()`, every patient, study, series and
instance as the graph holds it (attributes and sequences, the tags reached
and the values written, the shifted dates); the second pass's findings and
graph; for the floor and `"*"`, each redacted frame's sha256; and for
`"*"`, each leak (its text, reason and metadata), each suggestion, and the
rules once applied.

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
import sqlite3
from datetime import date, datetime

import numpy as np
import pydicom
from pydicom.data import get_testdata_file
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter.automation import ConfigAutomator
from isocenter.entities import JITTER_SCHEME_UNKEYED
from isocenter.pixel_analysis import TextRegion
from isocenter.privacy import PhiReport
from isocenter.session import DicomSession
from isocenter.verification import RedactionVerifier
from support.project_secret import load_fixed_secret

SERIAL = "B3-DIGEST-SN"
JITTER = {"min_days": -40, "max_days": -3}
ZONE = [10, 30, 12, 40]
#: The patient the store classes legacy (#903): its pseudonym and date
#: offset are the unkeyed pre-0.9.7 derivations (`patients.jitter_scheme`).
LEGACY_ID = "DIGEST-782-7"

#: The `"*"` scenario's file (#808, #814, #899): `ZONE` on the wildcard
#: rule, written as a dict, beside an exact rule for a machine none of the
#: files is. No file names `SERIAL`, so only `"*"` covers the synthetic
#: image.
STAR_CONFIG = (
    "privacy_profile: basic\n"
    "machines:\n"
    "  - serial_number: \"*\"\n"
    "    redaction_zones:\n"
    f"      - roi: {ZONE}\n"
    "        note: wildcard\n"
    "  - serial_number: B4-OTHER-SN\n"
    "    redaction_zones:\n"
    "      - [0, 5, 0, 5]\n")

#: Text regions, `(x, y, w, h)`, as OCR would hand them for the synthetic
#: image: one inside `ZONE`, one across its edge, one outside every zone,
#: and one short enough to be noise.
STAR_REGIONS = (("COVERED", (14, 12, 20, 10)), ("PARTIAL", (30, 20, 20, 8)),
                ("NEWLEAK", (60, 50, 20, 8)), ("ab", (60, 5, 4, 4)))

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


def _named(path, suffix, name, patient_id=True):
    """CT_small as a patient of its own (`suffix` keys its IDs and UIDs)
    whose Patient's Name is `name`, or absent when `name` is None (#746):
    a name literally `Unknown` is replaced like any other, and an absent
    one is held as `''`, never a placeholder. With `patient_id=False`
    the file has no Patient ID, and ingest keys its subject by its study
    (#584, #903)."""
    ds = pydicom.dcmread(get_testdata_file("CT_small.dcm"))
    ds.SOPInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.SeriesInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}2"
    ds.StudyInstanceUID = f"1.2.826.0.1.3680043.8.498.782.{suffix}3"
    if patient_id:
        ds.PatientID = f"DIGEST-782-{suffix}"
    else:
        del ds.PatientID
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
    _named(os.path.join(directory, "legacy.dcm"), "7", "Legacy^Pat")
    _named(os.path.join(directory, "no-id.dcm"), "8", "NoId^Pat", patient_id=False)


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


def _leaks_on_the_synthetic_image(session):
    """What the OCR check makes of `STAR_REGIONS` on the synthetic image
    under the session's rules, the suggestions it leads to, and the rules
    once they are applied (#808, #814, #899). No OCR runs: the regions are
    fixed, so this is the classification against the covering rules' zones
    and `ConfigAutomator`, the config-driven half of `scan_pixel_content()`.

    Run after `redact()`, so the file's rules redact. Applied first, the
    suggestions put an exact rule for `SERIAL` beside `"*"`, and two rules on
    one instance each mint a redacted SOP UID; which one the instance keeps
    depends on which task finishes last, which threads do not fix. That is
    `redact()`'s behaviour on main, not this scenario's to pin. The image
    is found by the serial ingest indexed, since its UIDs are replaced by
    now."""
    found = [(inst, series.equipment)
             for patient in session.store.patients
             for study in patient.studies
             for series in study.series
             if series.equipment is not None
             and series.equipment.device_serial_number == SERIAL
             for inst in series.instances]
    assert len(found) == 1, found
    instance, equipment = found[0]
    regions = [TextRegion(text, box, 90.0) for text, box in STAR_REGIONS]
    leaks = RedactionVerifier(session.configuration.rules)._findings_for(
        instance, regions, equipment)
    suggestions = ConfigAutomator.suggest_config_updates(PhiReport(leaks))
    applied = ConfigAutomator.apply_suggestions(session, suggestions)
    return {
        "leaks": [[f.entity_uid, f.field_name, f.value, f.reason, _canon(f.metadata)]
                  for f in leaks],
        "suggestions": _canon(suggestions),
        "applied": applied,
        "rules": _canon(session.configuration.rules),
    }


def _scenario(root, name, configure, redact=False, inspect=None):
    """Ingest the files into a fresh store and reopen it, apply
    `configure`, audit and anonymize twice (and redact), run `inspect` (its
    record kept under `"inspected"`), and return what was found and
    written."""
    directory = os.path.join(root, name)
    _inputs(os.path.join(directory, "input"))
    db = os.path.join(directory, "s.db")
    with DicomSession(db) as session:
        load_fixed_secret(session)
        session.ingest(os.path.join(directory, "input"))
    _class_legacy(db, LEGACY_ID)
    with DicomSession(db) as session:
        configure(session, directory)
        session.configuration.date_jitter = dict(JITTER)
        report = session.audit()
        found = _findings(report)
        session.anonymize(report)
        record = {"findings": found, "graph": _graph(session)}
        # A second pass (#903): every UID the first minted verifies under
        # this store's secret and is left alone, so what it finds and
        # writes is pinned too.
        again = session.audit()
        record["second_findings"] = _findings(again)
        session.anonymize(again)
        record["graph_after_second"] = _graph(session)
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
        if inspect:
            record["inspected"] = inspect(session)
    return record


def _class_legacy(db, patient_id):
    """Class `patient_id` legacy in the closed store at `db`, as a store
    opened from before 0.9.7 classes its patients (#903): the next open
    reads the scheme from `patients.jitter_scheme`."""
    with sqlite3.connect(db) as conn:
        changed = conn.execute(
            "UPDATE patients SET jitter_scheme = ? WHERE patient_id = ?",
            (JITTER_SCHEME_UNKEYED, patient_id)).rowcount
    assert changed == 1, f"{patient_id} not in the store"


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
        # Redacts under the file's rules, before the suggestions are
        # applied; `_leaks_on_the_synthetic_image` says why (#908).
        "star-zone": _scenario(root, "star", _load(STAR_CONFIG), redact=True,
                               inspect=_leaks_on_the_synthetic_image),
    }


def behaviour_digest(root):
    """The sha256 of `behaviour(root)` as canonical JSON.

    Args:
        root (str): An empty directory the scenarios run in.

    Returns:
        str: 64 hex digits.
    """
    return digest_of(behaviour(root))


def digest_of(record):
    """The sha256 of a `behaviour()` record as canonical JSON."""
    text = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
