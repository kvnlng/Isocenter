"""`session.audit()` has to scan inside sequences, and fix what it finds there.

A DICOM sequence is where the free text lives: structured-report content,
waveform annotations, operator notes. `PhiInspector` has always been able
to scan nested items -- `tests/test_sr_anonymization.py` exercises that
directly -- but nothing reached it from the documented
`create_config()` / `audit()` / `anonymize()` path, because the
per-worker instance copy carried `attributes` and dropped `sequences`
and `text_index` with them.

The failure mode is the dangerous one: the scan reported clean on data
it never opened. These tests come in through `session.audit()` for that
reason. A test that calls `PhiInspector` directly cannot see this bug.
"""
import pydicom
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence as DicomSequence

from isocenter import Session
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.io_handlers import populate_attrs

NESTED_TEXT = "0040,a160"      # Text Value, inside Content Sequence
NESTED_NAME = "0010,0010"      # Patient Name, two levels down
SECRET = "SECRET NOTE"


def _instance_with_nested_text():
    """An instance whose PHI is two levels inside Content Sequence."""
    ds = Dataset()
    ds.PatientName = "Test^Patient"

    deep = Dataset()
    deep.PatientName = "Dr^Referrer"

    item = Dataset()
    item.ValueType = "TEXT"
    item.TextValue = SECRET
    item.ContentSequence = DicomSequence([deep])

    ds.ContentSequence = DicomSequence([item])

    instance = Instance("1.2.3", "1.2.840.10008.5.1.4.1.1.88.33", 1)
    populate_attrs(ds, instance)
    return instance


@pytest.fixture
def session(tmp_path):
    """A session holding one instance with PHI nested in a sequence."""
    sess = Session(str(tmp_path / "nested.db"))

    patient = Patient("P1", "Test^Patient")
    study = Study("S1", "20230101")
    series = Series("SE1", "SR", 1)
    series.instances.append(_instance_with_nested_text())
    study.series.append(series)
    patient.studies.append(study)
    sess.store.patients.append(patient)

    sess.configuration.phi_tags = {
        NESTED_TEXT: {"name": "Text Value", "action": "EMPTY"},
        NESTED_NAME: {"name": "Patient Name", "action": "REPLACE"},
    }

    yield sess
    sess.close()


def _nested_item(session):
    """The Content Sequence item holding the secret, live in the store."""
    instance = session.store.patients[0].studies[0].series[0].instances[0]
    return instance.sequences["0040,a730"].items[0]


def test_audit_finds_phi_nested_in_a_sequence(session):
    """The scan must open sequences, not just top-level attributes."""
    report = session.audit()

    tags = {f.tag for f in report.findings}
    assert NESTED_TEXT in tags, (
        "audit() reported clean on a Text Value it never opened")


def test_audit_finds_phi_nested_two_levels_deep(session):
    """Nesting is recursive; so is the text index. The scan must be too."""
    report = session.audit()

    deep = [f for f in report.findings
            if f.tag == NESTED_NAME and "Deep" in f.field_name]
    assert deep, "a Patient Name two levels down was not reported"


def test_a_nested_finding_points_at_the_live_nested_item(session):
    """Findings come back from worker copies and must be rebound.

    Rehydration matched on SOP Instance UID and rebound every Instance
    finding to the Instance itself -- so a finding raised against a
    sequence item was handed the wrong object to remediate.
    """
    report = session.audit()
    finding = next(f for f in report.findings if f.tag == NESTED_TEXT)

    assert finding.entity is _nested_item(session), (
        "the finding does not point at the item it was raised against")


def test_anonymize_clears_the_nested_value_in_place(session):
    """The value that was found is the value that must be cleared."""
    session.audit()
    session.anonymize()

    assert _nested_item(session).attributes[NESTED_TEXT] == ""


def test_anonymize_does_not_invent_a_top_level_copy_of_a_nested_tag(session):
    """Writing to the Instance instead of the item is the worse failure.

    It leaves the real value untouched inside the sequence *and* adds a
    top-level tag that was never in the file -- so the export carries the
    PHI plus a fabricated element reading "".
    """
    session.audit()
    session.anonymize()

    instance = session.store.patients[0].studies[0].series[0].instances[0]
    assert NESTED_TEXT not in instance.attributes, (
        "anonymize() wrote a nested tag to the top level of the instance")


def test_an_unresolvable_nested_finding_is_dropped_not_redirected(session):
    """If the item is gone, the finding must not fall back to the Instance.

    Rebinding to the nearest available object is what produced the
    fabricated top-level tag. Remediation already skips a finding with no
    entity, which is the correct outcome: nothing written, and a log
    entry saying so.
    """
    report = session.audit()
    instance = session.store.patients[0].studies[0].series[0].instances[0]

    # The sequence is edited between the scan and the rehydrate/remediate.
    instance.sequences["0040,a730"].items.clear()
    session._rehydrate_findings(report.findings)

    nested = [f for f in report.findings if f.tag == NESTED_TEXT]
    assert nested, "test setup: expected a nested finding to exist"
    assert all(f.entity is None for f in nested), (
        "an unresolvable nested finding was redirected onto the instance")


def test_the_basic_profile_reaches_unformatted_text_value(tmp_path):
    """`(0070,0006)` is the profile tag #57 made inert.

    It is the one Basic-profile entry that lives inside a sequence --
    Waveform Annotation Sequence -- so it was documented in
    `docs/waveforms.md` as present but not remediated, and
    `tests/test_profiles.py` pinned the profile as "34 tags, 33
    effective". This proves the 34th now fires, so that sentence and that
    test can say 34.
    """
    ds = Dataset()
    ds.PatientName = "Test^Patient"

    annotation = Dataset()
    annotation.UnformattedTextValue = "Pacemaker patient, Dr Adeyemi"

    ds.WaveformAnnotationSequence = DicomSequence([annotation])

    instance = Instance("1.2.9", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    populate_attrs(ds, instance)

    sess = Session(str(tmp_path / "wf.db"))
    try:
        patient = Patient("P1", "Test^Patient")
        study = Study("S1", "20230101")
        series = Series("SE1", "ECG", 1)
        series.instances.append(instance)
        study.series.append(series)
        patient.studies.append(study)
        sess.store.patients.append(patient)

        from isocenter.profiles import BASIC_PROFILE
        sess.configuration.phi_tags = dict(BASIC_PROFILE)

        sess.audit()
        sess.anonymize()

        item = instance.sequences["0040,b020"].items[0]
        assert item.attributes["0070,0006"] == "", (
            "the Basic profile's EMPTY action still does not reach "
            "Unformatted Text Value")
    finally:
        sess.close()
