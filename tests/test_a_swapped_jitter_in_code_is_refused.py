"""A `date_jitter` assigned in code is judged as the loader judges a file's
(#731).

The loader refuses `min_days > max_days` (#713): one of the bounds is
wrong, and which cannot be told. A range assigned in code reached no
loader. Measured on `main` at 7579d4df with
`configuration.date_jitter = {"min_days": -1, "max_days": -365}`: `audit()`
and `anonymize()` ran silently, because `RemediationService._get_date_shift`
swapped the bounds; `create_config()` and `configuration.save()` each wrote
a file the loader then refused, which falsified `save()`'s docstring ("A
file this writes loads to the configuration it was written from"). A range
of the wrong shape (`{"min_days": "x", ...}`) passed `audit()` too.

Owner ruling Q5 A on #731: `audit()`, `anonymize()`, `save()` and
`create_config()` refuse it, with the loader's own words
(`config_manager._refused_date_jitter`, one judge for both). `anonymize()`
judges first, since `anonymize(findings)` never enters `audit()`; `save()`
judges in `_rendered`, so auto-save refuses too. The silent swap in
`RemediationService` is gone, and a `RemediationService` handed a range the
loader would refuse raises.
"""
import sqlite3

import pydicom
import pytest
import yaml
from pydicom.data import get_testdata_file

from isocenter.remediation import RemediationService
from isocenter.session import DicomSession

SWAPPED = {"min_days": -1, "max_days": -365}
SWAPPED_PHRASE = "min_days -1 is greater than max_days -365"
SHAPE_PHRASE = "'date_jitter' must be {min_days: int, max_days: int}"
#: Each shape the loader refuses, assigned in code.
BAD_SHAPES = {
    "bool": {"min_days": True, "max_days": -1},
    "str": {"min_days": "x", "max_days": -1},
    "missing-key": {"min_days": -10},
    "extra-key": {"min_days": -10, "max_days": -1, "days": 3},
    "bare-int": -7,
}


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")


@pytest.fixture
def source(tmp_path):
    directory = tmp_path / "src"
    directory.mkdir()
    pydicom.dcmread(get_testdata_file("CT_small.dcm")).save_as(str(directory / "ct.dcm"))
    return str(directory)


def _study_dates(session):
    return [(study.study_instance_uid, study.study_date)
            for patient in session.store.patients for study in patient.studies]


def test_anonymize_refuses_a_swapped_range_and_shifts_nothing(tmp_path, source):
    """Main: the bounds were swapped and every date shifted. Kills the
    swap restored with the judge removed from `anonymize()`."""
    db = str(tmp_path / "s.db")
    with DicomSession(db) as session:
        session.ingest(source)
        before = _study_dates(session)
        session.configuration.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            session.anonymize()
        assert _study_dates(session) == before
        session.save()
    with DicomSession(db) as reopened:
        assert _study_dates(reopened) == before


def test_anonymize_with_a_report_refuses_too(tmp_path, source):
    """`anonymize(findings)` never enters `audit()`. Kills the judge
    placed in `audit()` alone."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(source)
        report = session.audit()
        before = _study_dates(session)
        session.configuration.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            session.anonymize(report)
        assert _study_dates(session) == before


def test_anonymize_judges_before_it_reads_the_secret(tmp_path, source):
    """`anonymize(findings)` on a store with no secret yet: the range is
    refused before `_project_secret_for_use` mints one. Kills the judge
    removed from `anonymize()`, which `RemediationService`'s own judge
    would otherwise hide (it runs after the secret)."""
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(source)
        session.configuration.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match="session.configuration: 'date_jitter' min_days"):
            session.anonymize([])
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_audit_refuses_before_a_secret_is_minted(tmp_path, source):
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(source)
        session.configuration.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            session.audit()
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_save_refuses_and_leaves_the_file_as_it_was(tmp_path):
    """Main: `save()` wrote a file the loader refused. Kills the judge
    removed from `_rendered`."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        config = session.configuration
        config.config_path = str(tmp_path / "saved.yaml")
        config.save()
        saved = (tmp_path / "saved.yaml").read_bytes()
        config.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            config.save()
        assert (tmp_path / "saved.yaml").read_bytes() == saved

        config.config_path = str(tmp_path / "never.yaml")
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            config.save()
        assert not (tmp_path / "never.yaml").exists()


def test_create_config_refuses_and_writes_nothing(tmp_path):
    """Main: the scaffold carried the swapped range and the loader refused
    it. Kills the judge removed from `create_config`."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.configuration.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            session.create_config(str(tmp_path / "scaffold.yaml"))
    assert not (tmp_path / "scaffold.yaml").exists()


def test_auto_save_refuses_a_phi_tag_change_while_the_range_is_swapped(tmp_path):
    """Under auto-save every mutator writes the file, and the file it
    would write is one the loader refuses; #715's trial copy keeps memory
    and the file as they were. The jitter error from a phi-tag call is
    named in the CHANGELOG."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        config = session.configuration
        config.config_path = str(tmp_path / "saved.yaml")
        config.auto_save = True
        config.set_phi_tag("0008,0080", "KEEP")
        saved = (tmp_path / "saved.yaml").read_bytes()
        config.date_jitter = dict(SWAPPED)
        tags = {t: dict(r) if isinstance(r, dict) else r for t, r in config.phi_tags.items()}
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            config.set_phi_tag("0008,0081", "KEEP")
        assert config.phi_tags == tags
        assert (tmp_path / "saved.yaml").read_bytes() == saved


@pytest.mark.parametrize("shape", sorted(BAD_SHAPES))
@pytest.mark.parametrize("door", ["audit", "anonymize", "save", "create_config"])
def test_every_shape_the_loader_refuses_is_refused_in_code(tmp_path, source, door, shape):
    """The loader's own wording at each door: one judge, so one spelling.
    Main: `audit()` passed every one."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(source)
        session.configuration.date_jitter = BAD_SHAPES[shape]
        with pytest.raises(ValueError, match=r"'date_jitter' must be \{min_days: int, max_days: int\}"):
            if door == "audit":
                session.audit()
            elif door == "anonymize":
                session.anonymize()
            elif door == "save":
                session.configuration.config_path = str(tmp_path / "saved.yaml")
                session.configuration.save()
            else:
                session.create_config(str(tmp_path / "scaffold.yaml"))


@pytest.mark.parametrize("door", ["audit", "anonymize", "save", "create_config"])
def test_equal_bounds_pass_every_door(tmp_path, source, door):
    """A fixed shift is `{min_days: n, max_days: n}`, as the loader's own
    advice for the old single int says. Kills `>` written `>=`."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(source)
        session.configuration.date_jitter = {"min_days": -7, "max_days": -7}
        if door == "audit":
            session.audit()
        elif door == "anonymize":
            session.anonymize()
        elif door == "save":
            session.configuration.config_path = str(tmp_path / "saved.yaml")
            session.configuration.save()
        else:
            session.create_config(str(tmp_path / "scaffold.yaml"))


def test_a_remediation_service_refuses_a_swapped_range():
    """The swap in `_get_date_shift` was the silence; with it gone, the
    service judges its range when it is made."""
    with pytest.raises(ValueError, match=SWAPPED_PHRASE):
        RemediationService(date_jitter_config=dict(SWAPPED))
    RemediationService(date_jitter_config={"min_days": -7, "max_days": -7})
    RemediationService()


def test_auto_save_recovers_once_the_range_is_fixed(tmp_path):
    """The refusal holds only while the refused range is in memory (review
    of #895): once `date_jitter` is fixed, the next mutators save again,
    with the fixed range and each change made since. Kills a refusal that
    latches, and a trial save that keeps the refused range."""
    with DicomSession(str(tmp_path / "s.db")) as session:
        config = session.configuration
        config.config_path = str(tmp_path / "saved.yaml")
        config.auto_save = True
        config.date_jitter = dict(SWAPPED)
        with pytest.raises(ValueError, match=SWAPPED_PHRASE):
            config.set_phi_tag("0008,0081", "KEEP")
        assert not (tmp_path / "saved.yaml").exists()
        config.date_jitter = {"min_days": -365, "max_days": -1}
        config.set_phi_tag("0008,0081", "KEEP")
        config.add_rule("SN-RECOVERED")
    saved = yaml.safe_load((tmp_path / "saved.yaml").read_text(encoding="utf-8"))
    assert saved["date_jitter"] == {"min_days": -365, "max_days": -1}
    assert saved["phi_tags"]["0008,0081"]["action"] == "KEEP"
    assert [m["serial_number"] for m in saved["machines"]] == ["SN-RECOVERED"]
