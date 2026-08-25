"""Remediation has to reach every finding, and say how many it applied.

Two defects sit together here. The dedupe key that decides whether a
finding has already been handled is `(entity_uid, field_name)`, and a
finding raised inside a sequence carries the *instance's* UID -- items
nested in sequences have no UID of their own. So two annotation items on
one instance, each holding the same tag, produce one key between them and
the second is dropped silently.

That was unreachable until the scan started opening sequences (#57), and
it is the same failure mode #57 was: PHI left in place by a step that
reported success. The count is the other half -- `apply_remediation`
returned nothing at all, so `anonymize()` has always printed
"Anonymized/Remediated None tags according to policy."
"""
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence as DicomSequence

from isocenter import Session
from isocenter.entities import Patient, Study, Series, Instance
from isocenter.io_handlers import populate_attrs

TEXT = "0070,0006"
ANNOTATION_SEQ = "0040,b020"


def _instance_with(*notes):
    """One instance carrying an annotation item per note."""
    ds = Dataset()
    ds.PatientName = "Test^Patient"
    items = []
    for note in notes:
        item = Dataset()
        item.UnformattedTextValue = note
        items.append(item)
    ds.WaveformAnnotationSequence = DicomSequence(items)

    instance = Instance("1.2.9", "1.2.840.10008.5.1.4.1.1.9.1.1", 1)
    populate_attrs(ds, instance, instance.text_index)
    return instance


@pytest.fixture
def make_session(tmp_path):
    """Builds a session around a prepared instance."""
    sessions = []

    def build(instance):
        sess = Session(str(tmp_path / f"rem{len(sessions)}.db"))
        sessions.append(sess)
        patient = Patient("P1", "Test^Patient")
        study = Study("S1", "20230101")
        series = Series("SE1", "ECG", 1)
        series.instances.append(instance)
        study.series.append(series)
        patient.studies.append(study)
        sess.store.patients.append(patient)
        sess.configuration.phi_tags = {
            TEXT: {"name": "Unformatted Text Value", "action": "EMPTY"}}
        return sess

    yield build
    for sess in sessions:
        sess.close()


def test_every_sequence_item_is_remediated_not_just_the_first(make_session):
    """Two items, one tag, one instance UID between them.

    The second finding deduped against the first and was dropped, so its
    text survived a run that reported success.
    """
    instance = _instance_with("First note, Dr Adeyemi", "Second note, Dr Ito")
    session = make_session(instance)

    session.audit()
    session.anonymize()

    values = [item.attributes[TEXT]
              for item in instance.sequences[ANNOTATION_SEQ].items]
    assert values == ["", ""], (
        f"a sequence item was skipped by deduplication: {values}")


def test_anonymize_reports_how_many_remediations_it_applied(make_session):
    """`apply_remediation` returned None, so the console said "None tags"."""
    session = make_session(_instance_with("A note"))

    session.audit()
    applied = session.anonymize()

    assert isinstance(applied, int), (
        "anonymize() gives the caller no way to tell what it did")
    assert applied >= 1


def test_the_console_does_not_report_a_count_of_none(make_session, capsys):
    """The line a user reads after de-identifying must carry a number."""
    session = make_session(_instance_with("A note"))
    session.audit()
    session.anonymize()

    out = capsys.readouterr().out
    assert "None tags" not in out, (
        "anonymize() tells the operator it remediated 'None' tags")


def test_a_remediation_that_fails_is_not_counted_as_applied(
        make_session, monkeypatch, caplog):
    """The per-finding handler logs and continues; the count must not lie.

    A run where every remediation failed would otherwise report the same
    number as a run where every one succeeded.
    """
    import isocenter.remediation as remediation

    def explode(*_args, **_kwargs):
        raise RuntimeError("entity is read-only")

    monkeypatch.setattr(
        remediation.RemediationService, "_apply_single_remediation", explode)

    session = make_session(_instance_with("A note"))
    session.audit()

    with caplog.at_level("WARNING"):
        applied = session.anonymize()

    assert applied == 0
    assert any("failed" in record.message.lower() for record in caplog.records)


def test_two_tags_sharing_a_display_name_are_both_remediated(make_session):
    """The dedupe key must identify the attribute, not its label.

    `field_name` is a display string taken from the config's `name`, and
    it falls back to the literal "Unknown Tag" when a config entry omits
    one. Two such entries on the same instance produced the same key, so
    the second tag was skipped and its value survived -- reachable in
    every released version with a hand-written config, since
    `create_config()` and the shipped profiles always write names.
    """
    instance = _instance_with("A note")
    instance.attributes["0008,0080"] = "St Elsewhere"
    instance.attributes["0008,1010"] = "SCANNER-1"

    session = make_session(instance)
    session.configuration.phi_tags = {
        "0008,0080": {"action": "REMOVE"},   # no "name" -> "Unknown Tag"
        "0008,1010": {"action": "REMOVE"},   # no "name" -> "Unknown Tag"
    }

    session.audit()
    session.anonymize()

    assert "0008,0080" not in instance.attributes
    assert "0008,1010" not in instance.attributes, (
        "the second tag deduped against the first because they share a "
        "display name")
