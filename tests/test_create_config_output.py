"""Byte-for-byte approval test over `session.create_config()`.

`create_config` was a single 320-line function assembling a scaffolded
YAML config from five separate concerns. Splitting it up is only safe if
"the output did not change" can be checked rather than assumed, so this
pins the exact bytes it produces for a fixed session.

The fixture covers the paths that interact:

* a machine matching the shipped knowledge base by serial number, so the
  KB template and its zones are copied in;
* burned-in annotations on that machine, so the safety warning is
  appended to the KB's own comment rather than replacing it;
* a machine matching nothing, so the empty scaffold branch runs;
* no loaded configuration, so the default PHI tags are read and reshaped.

If a change here is deliberate, regenerate the fixture and read the diff
before committing it -- that diff is the whole review. The output is
deterministic: it contains no timestamps, paths, or ordering that varies
between runs.
"""
import pathlib

import pytest

from isocenter import Session
from isocenter.entities import Patient, Study, Series, Instance, Equipment

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "scaffolded_config.golden.yaml"


@pytest.fixture
def scaffolding_session(tmp_path):
    """A session whose inventory exercises every branch of the scaffold."""
    session = Session(str(tmp_path / "scaffold.db"))

    patient = Patient("P1", "Test")
    study = Study("S1", "20230101")

    # Known to the shipped knowledge base, and carrying burned-in text.
    known = Series("SE1", "CT", 1)
    known.equipment = Equipment("GE", "Revolution CT", "SN-SCANNER-01")
    burned_in = Instance("1.2.3", "/tmp/x.dcm")
    burned_in.attributes["0028,0301"] = "YES"
    known.instances.append(burned_in)

    # Known to nothing: takes the empty-scaffold branch.
    unknown = Series("SE2", "MR", 2)
    unknown.equipment = Equipment("Acme", "Scanner 9000", "SN-UNKNOWN-42")
    unknown.instances.append(Instance("1.2.4", "/tmp/y.dcm"))

    study.series.extend([known, unknown])
    patient.studies.append(study)
    session.store.patients.append(patient)

    yield session
    session.close()


def test_scaffolded_config_matches_the_recorded_output(
        scaffolding_session, tmp_path):
    """The generated file is identical to the fixture, byte for byte."""
    output = tmp_path / "out.yaml"
    scaffolding_session.create_config(str(output))

    produced = output.read_text(encoding="utf-8")
    expected = GOLDEN.read_text(encoding="utf-8")

    assert produced == expected, (
        "create_config's output changed. If that was deliberate, "
        f"regenerate {GOLDEN.name} and review the diff; if not, this is "
        "the regression the fixture exists to catch.")


def test_the_knowledge_base_entry_survives_alongside_the_safety_warning(
        scaffolding_session, tmp_path):
    """The two comment sources are concatenated, not overwritten.

    The KB supplies a description of what to redact; the burned-in scan
    supplies a warning that pixels need checking. Losing either one
    silently would still produce a valid config file.
    """
    output = tmp_path / "out.yaml"
    scaffolding_session.create_config(str(output))
    produced = output.read_text(encoding="utf-8")

    assert "Redact burned-in Patient Name top-left" in produced
    assert "WARNING: 1 images have 'Burned In Annotation' flag" in produced


def test_a_machine_the_knowledge_base_does_not_know_gets_an_empty_scaffold(
        scaffolding_session, tmp_path):
    """An unmatched machine still has to appear, with zones to fill in."""
    output = tmp_path / "out.yaml"
    scaffolding_session.create_config(str(output))
    produced = output.read_text(encoding="utf-8")

    assert "SN-UNKNOWN-42" in produced
    assert "Scanner 9000" in produced
    assert "redaction_zones: []" in produced


def test_scaffolding_does_not_edit_the_session_configuration(
        scaffolding_session, tmp_path):
    """Generating a scaffold reads the configuration; it must not write it.

    `create_config` fills in Study Date, Sex and Age so the scaffold shows
    a research-friendly default for each. Those three used to be inserted
    into the live `configuration.phi_tags` dict, so asking for a scaffold
    silently added tags to the policy the session would go on to apply.
    """
    scaffolding_session.configuration.phi_tags = {"0010,0010": "Patient Name"}

    scaffolding_session.create_config(str(tmp_path / "out.yaml"))

    assert scaffolding_session.configuration.phi_tags == {
        "0010,0010": "Patient Name"}
