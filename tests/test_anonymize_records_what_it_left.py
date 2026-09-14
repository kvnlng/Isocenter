"""`anonymize()` records on each instance what a remediation left at a
top-level tag, and the record is never a tag (#537).

`lock_identities()` has to tell a value the source held from one a pass
wrote. Asking which policy the session holds cannot answer that: the
policy is gone after a reopen and replaced by a re-audit or a
`load_config()` (review of #574, M-3). So each `REPLACE`, `EMPTY` and
`REMOVE` on an instance's own tag, and each copy a patient or study write
reaches, is recorded on the instance immediately before the write, and
stored beside `__shifted__` in the instance's JSON.

The record must never become a tag, a scan input, an export column or a
byte in any written file (owner ruling on the C9 redesign); the second
half of this file holds each of those to it.

**Why this file imports what it does.** `isocenter.entities`,
`isocenter.persistence`, `isocenter.remediation`, `isocenter.privacy`,
`isocenter.io_handlers` and `isocenter.session` are named, so their probe
rows are charged.
"""
import json
import sqlite3

import pydicom
import pytest
import yaml

import isocenter.persistence  # noqa: F401  (probe row)
import isocenter.remediation  # noqa: F401  (probe row)
from isocenter.entities import Instance
from isocenter.privacy import PhiFinding, PhiInspector, PhiRemediation
from isocenter.session import DicomSession

from support.ct_small_files import write_ct

NAME = "Orig^Name"
PID = "P537R"
KEY = "__remediated__"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


def _session(tmp_path, rules=None, db="s.db"):
    """A session over one CT_small carrying a birth date and an accession,
    with `rules` loaded (the floor alone when None)."""
    path = write_ct(tmp_path / "in" / "a.dcm", PID, "5379", name=NAME)
    ds = pydicom.dcmread(path)
    ds.PatientBirthDate = "19700101"
    ds.AccessionNumber = "ACC123"
    ds.save_as(path)
    session = DicomSession(str(tmp_path / db))
    session.enable_reversible_anonymization(str(tmp_path / "k.key"))
    session.ingest(str(tmp_path / "in"))
    if rules is not None:
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(yaml.safe_dump({"phi_tags": rules}), encoding="utf-8")
        session.load_config(str(cfg))
    return session


def _instance(session):
    return session.store.patients[0].studies[0].series[0].instances[0]


def _reopen(tmp_path, session, db="s.db"):
    session.save(sync=True)
    session.close()
    return DicomSession(str(tmp_path / db))


REPLACING = {"0010,0010": {"action": "REPLACE", "value": "Project-X"},
             "0010,0020": {"action": "KEEP"},
             "0010,0030": {"action": "REPLACE", "value": "19000101"}}


def test_a_replacement_is_recorded_before_it_is_written(tmp_path, monkeypatch):
    """At the moment `set_attr` writes a replacement, the instance already
    vouches for it: the birth date through the instance arm, the name
    through the patient write's copy. Recorded after the write, a
    background `save()` could store the value without its record, and the
    next lock would stash it as an original. Kills either writer deleted
    or moved after its write."""
    seen = {}
    real = Instance.set_attr

    def spy(self, tag, value):
        if tag in ("0010,0010", "0010,0030"):
            seen.setdefault(tag, []).append(
                (value, self.remediation_vouches_for(tag, value)))
        return real(self, tag, value)

    with _session(tmp_path, REPLACING) as session:
        report = session.audit()
        monkeypatch.setattr(Instance, "set_attr", spy)
        session.anonymize(report)
        monkeypatch.undo()
        instance = _instance(session)
        assert instance.attributes["0010,0010"] == "Project-X"
        assert instance.attributes["0010,0030"] == "19000101"
    assert seen["0010,0010"] == [("Project-X", True)], seen
    assert seen["0010,0030"] == [("19000101", True)], seen


def test_a_removal_is_recorded_before_the_revision_moves(tmp_path, monkeypatch):
    """A `del` bumps no revision; the `mark_modified()` after it does, and
    by then the record must already say the tag was removed. Accession
    through the instance arm, the name through the patient write's copy.
    Kills either writer deleted or moved after `mark_modified()`."""
    rules = {"0010,0010": {"action": "REMOVE"}, "0010,0020": {"action": "KEEP"},
             "0008,0050": {"action": "REMOVE"}}
    removed = {"0010,0010": [], "0008,0050": []}
    real = Instance.mark_modified

    def spy(self):
        for tag, calls in removed.items():
            if tag not in self.attributes:
                calls.append(self.remediation_vouches_for(tag, None))
        return real(self)

    with _session(tmp_path, rules) as session:
        report = session.audit()
        instance = _instance(session)
        assert {"0010,0010", "0008,0050"} <= set(instance.attributes)
        monkeypatch.setattr(Instance, "mark_modified", spy)
        session.anonymize(report)
        monkeypatch.undo()
        assert "0010,0010" not in instance.attributes
        assert "0008,0050" not in instance.attributes
    for tag, calls in removed.items():
        assert calls and all(calls), (tag, calls)


def test_the_record_survives_a_reopen_and_is_never_a_tag(tmp_path):
    """Stored as `__remediated__` beside `__shifted__`, popped before
    hydration. Kills the key not written, not popped (it lands in
    `attributes` and becomes a dataframe column), or popped and not
    assigned."""
    with _session(tmp_path, {**REPLACING, "0008,0050": {"action": "REMOVE"}}) as session:
        session.anonymize(session.audit())
        session = _reopen(tmp_path, session)
    with session:
        instance = _instance(session)
        assert instance.remediation_vouches_for("0010,0010", "Project-X")
        assert instance.remediation_vouches_for("0010,0030", "19000101")
        assert instance.remediation_vouches_for("0008,0050", None)
        assert not instance.remediation_vouches_for("0010,0040", instance.attributes["0010,0040"])
        assert KEY not in instance.attributes
        frame = session.export_dataframe(str(tmp_path / "meta.csv"), expand_metadata=True)
        assert not [column for column in frame.columns if "remediated" in str(column)]
    with sqlite3.connect(str(tmp_path / "s.db")) as conn:
        (stored,) = conn.execute("SELECT attributes_json FROM instances").fetchone()
    record = json.loads(stored)[KEY]
    assert record["values"]["0010,0010"] == "Project-X"
    assert "0008,0050" in record["blank"].split()


def test_a_value_written_over_the_record_is_not_vouched_for(tmp_path):
    """The record is keyed on the value, as `__shifted__` is: a tag that no
    longer holds what the pass wrote stops vouching, with no invalidation
    pass. By hand, and by `recover_patient_identity(restore=True)`. Kills
    a vouch that reads the tag's presence in the record only."""
    with _session(tmp_path, REPLACING) as session:
        session.lock_identities(PID, tags_to_lock=["0010,0010", "0010,0020", "0010,0030"])
        session.anonymize(session.audit())
        instance = _instance(session)
        assert instance.remediation_vouches_for("0010,0030", "19000101")
        instance.set_attr("0010,0030", "19000202")
        assert not instance.remediation_vouches_for("0010,0030", "19000202")
        assert instance.remediation_vouches_for("0010,0010", "Project-X")
        session.recover_patient_identity(PID, restore=True)
        assert instance.attributes["0010,0010"] == NAME
        assert not instance.remediation_vouches_for("0010,0010", NAME)
        assert not instance.remediation_vouches_for("0010,0030", "19700101")


def test_private_tags_share_one_word(tmp_path):
    """The floor removes about 180 private tags per CT; one record entry
    each cost more memory than the instance's own attributes (measured in
    the C9 brief). So every private removal is the one word `private`.
    Kills per-tag private entries, and a vouch that ignores the word."""
    source = pydicom.dcmread(write_ct(tmp_path / "probe" / "a.dcm", PID, "5379"))
    private = sorted(f"{element.tag.group:04x},{element.tag.element:04x}"
                     for element in source if element.tag.is_private)
    assert private, "CT_small carries no private tag to remove"
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        instance = _instance(session)
        gone = [tag for tag in private if tag not in instance.attributes]
        assert gone, "the floor removed no private tag"
        words = instance._remediated_blank.split()
        assert "private" in words
        recorded = list(instance._remediated_values or {}) + words
        assert not [tag for tag in recorded if tag != "private" and int(tag[:4], 16) % 2], recorded
        assert instance.remediation_vouches_for(gone[0], None)


def _replace(instance, tag, value):
    return PhiFinding(
        entity_uid=instance.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=instance.attributes.get(tag), reason="hand-built",
        tag=tag, entity=instance,
        remediation_proposal=PhiRemediation(
            action_type="REPLACE_TAG", target_attr=tag, new_value=value,
            original_value=instance.attributes.get(tag)))


def test_a_declined_replacement_records_nothing(tmp_path):
    """A replacement that declines writes nothing, so it records nothing:
    a tag removed before the pass (#547's arm) and a value the dictionary
    VR cannot hold (#560's backstop). Kills the writer placed above the
    decline returns."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        write_ct(tmp_path / "in" / "a.dcm", PID, "5379", name=NAME)
        session.ingest(str(tmp_path / "in"))
        instance = _instance(session)
        gone = _replace(instance, "0008,0050", "")
        del instance.attributes["0008,0050"]
        unfit = _replace(instance, "0008,0012", "ANONYMIZED")
        assert session.anonymize([gone, unfit]) == 0
        assert instance.attributes["0008,0012"] == "20040119"
        assert instance._remediated_values is None
        assert instance._remediated_blank is None


def test_a_nested_write_records_nothing_on_the_instance(tmp_path):
    """The lock reads top-level values, so a write inside a sequence item
    records nothing on the instance that holds it: CT_small's Other
    Patient IDs items take `ANONYMIZED` at 0010,0020 under the floor, and
    the instance's record for 0010,0020 stays whatever its own top-level
    copy was given. Kills the writer recording against the owning
    instance."""
    with _session(tmp_path) as session:
        report = session.audit()
        instance = _instance(session)
        nested = [f for f in report.findings
                  if f.tag == "0010,0020" and f.entity_type == "Instance"
                  and f.entity is not instance
                  and getattr(f.remediation_proposal, "action_type", None) == "REPLACE_TAG"]
        assert nested, "CT_small raised no nested 0010,0020 replacement"
        session.anonymize(report)
        assert [f.entity.attributes.get("0010,0020") for f in nested] == ["ANONYMIZED"] * len(nested)
        assert instance.attributes.get("0010,0020") != "ANONYMIZED"
        assert not instance.remediation_vouches_for("0010,0020", "ANONYMIZED")
        assert (instance._remediated_values or {}).get("0010,0020") == \
            instance.attributes.get("0010,0020")


# --- never a tag, a scan input, an export column or a written byte ---------


def _nest_a_record(db):
    """Hand-edit the stored JSON so a sequence item carries `__remediated__`:
    hydration reaches every depth, and the key must be popped there too.
    The floor removes CT_small's one sequence, so the item is added as a
    Referenced Image Sequence holding two UIDs."""
    with sqlite3.connect(str(db)) as conn:
        uid, stored = conn.execute(
            "SELECT sop_instance_uid, attributes_json FROM instances").fetchone()
        data = json.loads(stored)
        assert KEY in data, "the pass left no record to find"
        data.setdefault("__sequences__", {})["0008,1140"] = [{
            "0008,1150": "1.2.840.10008.5.1.4.1.1.2", "0008,1155": "1.2.3.537",
            KEY: {"values": {"0010,0020": "X"}, "blank": "private"}}]
        conn.execute("UPDATE instances SET attributes_json=? WHERE sop_instance_uid=?",
                     (json.dumps(data), uid))


def _items(item):
    yield item
    for sequence in item.sequences.values():
        for child in sequence.items:
            yield from _items(child)


def test_a_reopened_store_scans_and_examines_no_record(tmp_path, monkeypatch, capsys):
    """After a reopen, with a record hand-nested in a sequence item as
    well: no item at any depth holds the key, the scan is handed no
    instance carrying it and raises nothing on it, and `examine()` prints
    nothing of it. Kills the pop placed after `attributes.update`, or on
    the root only."""
    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    _nest_a_record(tmp_path / "s.db")
    handed = []
    real = PhiInspector._scan_instance

    def spy(self, instance, *args, **kwargs):
        handed.append([KEY in item.attributes for item in _items(instance)])
        return real(self, instance, *args, **kwargs)

    monkeypatch.setattr(PhiInspector, "_scan_instance", spy)
    with DicomSession(str(tmp_path / "s.db")) as session:
        instance = _instance(session)
        assert "private" in instance._remediated_blank.split()
        assert not any(KEY in item.attributes for item in _items(instance))
        assert "0008,1140" in instance.sequences
        report = session.audit()
        session.examine()
    assert handed and not any(any(flags) for flags in handed), handed
    assert not [f for f in report.findings if "remediated" in str(f.tag) + str(f.field_name)]
    assert "remediated" not in capsys.readouterr().out


def _bytes_under(root):
    files = [path for path in root.rglob("*") if path.is_file()]
    assert files, f"nothing was written under {root}"
    return [(path, path.read_bytes()) for path in files]


def test_no_written_file_carries_the_record(tmp_path):
    """`session.export()` and `DicomExporter.write_tree()` over a reopened
    CT, and the WFDB export over a reopened ECG, each with a record
    hand-nested in a sequence item too: not one byte of `__remediated__`
    reaches a file."""
    from isocenter.io_handlers import DicomExporter
    from scripts.generate_waveform_test_data import write_fixture

    with _session(tmp_path) as session:
        session.anonymize(session.audit())
        session.save(sync=True)
    _nest_a_record(tmp_path / "s.db")
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.export(str(tmp_path / "out"), format="dicom")
        DicomExporter.write_tree(session.store.patients[0], str(tmp_path / "tree"))
    for root in ("out", "tree"):
        for path, data in _bytes_under(tmp_path / root):
            assert KEY.encode() not in data, path

    ecg = tmp_path / "ecg"
    write_fixture(str(ecg / "in" / "ecg.dcm"), num_samples=200,
                  patient_id="MRN-537", patient_name="Doe^Jane")
    with DicomSession(str(ecg / "s.db")) as session:
        session.ingest(str(ecg / "in"))
        session.anonymize(session.audit())
        assert _instance(session)._remediated_values or _instance(session)._remediated_blank
        session.save(sync=True)
    with DicomSession(str(ecg / "s.db")) as session:
        session.export(str(ecg / "wfdb"), format="wfdb")
    for path, data in _bytes_under(ecg / "wfdb"):
        assert KEY.encode() not in data, path
