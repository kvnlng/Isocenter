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
    populate_attrs(ds, instance)
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


# ---------------------------------------------------------------------------
# A REPLACE already there is satisfied, not written again (#952)
# ---------------------------------------------------------------------------
#
# Measured on main at fd359eb3: the same instance findings handed to
# `anonymize()` twice under the floor changed nothing in the graph the second
# time, yet the second call returned 17, wrote 17 `REMEDIATION_REPLACE` rows
# again, and the report then counted 36 replacements for 19. Every `REPLACE`
# wrote again: `_replace_on_item` never asked whether the element already held
# the value. Owner ruling Q-D1-4 A (2026-10-08): a `REPLACE` on an instance's
# own element that already holds the value, vouched by the instance's
# remediation record, is satisfied -- no row, not counted. The record is the
# gate, so a source value that happens to equal the rule's value is still
# written once, and recorded. The Patient, Study and Series arms are unchanged.

@pytest.fixture
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _ct_session(tmp_path):
    """CT_small ingested under the floor, audited: the open session, the
    report and the one instance."""
    from support.ct_small_files import write_ct

    write_ct(tmp_path / "in" / "a.dcm", "PID-952", "9520", name="Alpha^One")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    report = session.audit()
    [instance] = [i for p in session.store.patients for st in p.studies
                  for se in st.series for i in se.instances]
    return session, report, instance


def _remediation_rows(session):
    import sqlite3

    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log "
            "WHERE action_type LIKE 'REMEDIATION%' ORDER BY rowid").fetchall()


def _graph(session):
    """Every value the graph holds, as reprs."""
    from isocenter.entities import iter_item_tree

    out = {}
    for patient in session.store.patients:
        out["patient"] = (repr(patient.patient_id), repr(patient.patient_name))
        for study in patient.studies:
            out["study", study.study_instance_uid] = (
                repr(study.study_date), study.date_shifted)
            for series in study.series:
                for instance in series.instances:
                    for item, path in iter_item_tree(instance):
                        for tag, value in item.attributes.items():
                            out[path, tag] = repr(value)
                        for tag, sequence in item.sequences.items():
                            out[path, tag, "items"] = len(sequence.items)
    return out


def _instance_findings(report):
    return [f for f in report.findings if f.entity_type == "Instance"]


def _section_two_count(session, tmp_path, action):
    """The count the report's section 2 gives `action`."""
    import re

    path = tmp_path / "report.md"
    session.generate_report(str(path))
    [count] = re.findall(rf"\|\s*{action}\s*\|\s*(\d+)\s*\|",
                         path.read_text(encoding="utf-8"))
    return int(count)


def test_the_same_instance_findings_handed_twice_apply_nothing_the_second_time(
        tmp_path, _threads):
    """Red on main: the second call returned 17 and wrote 17 rows. Now it
    returns 0, the audit trail gains no `REMEDIATION_*` row, the graph is
    value for value what the first pass left, and the report's section 2
    counts each replacement once. The SOP Instance UID's keyed replacement
    is one of the repeats: the instance stays on the UID the first pass
    gave it, with one row. Kills: the repeat written again; the SOP arm
    left repeating."""
    session, report, instance = _ct_session(tmp_path)
    with session:
        mine = _instance_findings(report)
        replaces = [f for f in mine
                    if f.remediation_proposal.action_type == "REPLACE_TAG"
                    and not f.entity_path]
        assert len(replaces) > 10, "setup: the floor raises instance REPLACEs"
        first = session.anonymize(mine)
        assert first > len(replaces) // 2
        rows = _remediation_rows(session)
        graph = _graph(session)
        sop = instance.sop_instance_uid
        replaced = _section_two_count(session, tmp_path, "REMEDIATION_REPLACE")
        assert replaced == [a for a, _ in rows].count("REMEDIATION_REPLACE")

        assert session.anonymize(mine) == 0

        assert _remediation_rows(session) == rows
        assert _graph(session) == graph
        assert instance.sop_instance_uid == sop
        assert len([d for a, d in rows if "(Tag 0008,0018)" in d]) == 1
        assert _section_two_count(
            session, tmp_path, "REMEDIATION_REPLACE") == replaced


def _replace(report, instance, tag, value):
    """A hand-built top-level `REPLACE_TAG` finding on `instance`, made
    from one of the audit's own (this file imports no finding class)."""
    import dataclasses

    template = next(f for f in report.findings
                    if f.entity_type == "Instance" and not f.entity_path)
    return dataclasses.replace(
        template, entity_uid=instance.sop_instance_uid,
        field_name="hand-built", value=instance.attributes[tag],
        reason="hand-built", tag=tag, entity=instance,
        remediation_proposal=dataclasses.replace(
            template.remediation_proposal, action_type="REPLACE_TAG",
            target_attr=tag, new_value=value,
            original_value=instance.attributes[tag], metadata={}))


def test_a_source_value_equal_to_the_rules_value_is_still_written_once(
        tmp_path, _threads):
    """Control: the instance's remediation record is the gate, not
    equality. A REPLACE whose value the element already holds *as the
    source wrote it* has no record behind it, so it is written, counted
    and logged once -- the row and the record are what say the value is a
    replacement, which `lock_identities()` reads. Handed again, it is
    satisfied. Kills: the write skipped on equality alone."""
    session, _report, instance = _ct_session(tmp_path)
    with session:
        held = instance.attributes["0008,0070"]
        assert held == "GE MEDICAL SYSTEMS"
        assert not instance.remediation_vouches_for("0008,0070", held)
        finding = _replace(_report, instance,"0008,0070", held)

        assert session.anonymize([finding]) == 1
        rows = _remediation_rows(session)
        assert [a for a, _ in rows] == ["REMEDIATION_REPLACE"]
        assert instance.remediation_vouches_for("0008,0070", held)

        assert session.anonymize([finding]) == 0
        assert _remediation_rows(session) == rows
        assert instance.attributes["0008,0070"] == held


def test_an_element_edited_between_the_passes_is_written_again(tmp_path, _threads):
    """Control: the element must hold the value now. Edited to another
    value after the first pass, the same finding writes again, with its
    row. (The record vouches for what the element holds, so the hand
    edit alone un-vouches it; the control below is the one that turns on
    the equality with the proposal's value.) Kills: a pass remembered as
    done whatever the element holds since."""
    session, _report, instance = _ct_session(tmp_path)
    with session:
        finding = _replace(_report, instance,"0008,0070", "ANONYMIZED")
        assert session.anonymize([finding]) == 1
        instance.set_attr("0008,0070", "SET BACK BY HAND")

        assert session.anonymize([finding]) == 1

        assert instance.attributes["0008,0070"] == "ANONYMIZED"
        assert [a for a, _ in _remediation_rows(session)] == [
            "REMEDIATION_REPLACE"] * 2


def test_a_replace_with_another_value_than_the_one_recorded_is_written(
        tmp_path, _threads):
    """Control: the element must hold *the value this proposal writes*.
    After a first REPLACE the element holds `ANONYMIZED` and the record
    vouches for it; a second finding asking for `REDACTED` is not met by
    that, and is written, with its row. Kills: any vouched value read as
    this proposal's end state (the equality dropped: the record vouches
    for what the element holds, whatever the proposal asks)."""
    session, _report, instance = _ct_session(tmp_path)
    with session:
        assert session.anonymize(
            [_replace(_report, instance,"0008,0070", "ANONYMIZED")]) == 1
        assert instance.remediation_vouches_for("0008,0070", "ANONYMIZED")

        assert session.anonymize(
            [_replace(_report, instance,"0008,0070", "REDACTED")]) == 1

        assert instance.attributes["0008,0070"] == "REDACTED"
        assert [a for a, _ in _remediation_rows(session)] == [
            "REMEDIATION_REPLACE"] * 2
