"""A hand-built instance `REMOVE` spelling its tag in upper case removes
it as the lower-case spelling does (#1012; owner ruling Q3, 2026-11-21).

The REMOVE item arm tested the raw `target_attr` against `attributes`,
whose keys are lower case. Under a Study whose UID had been emptied by
hand, `0020,000d` removed the instance's copy with a
`REMEDIATION_REMOVE` row, while `0020,000D` left the copy `''` with no
row and returned 0. On any other present tag (`0008,103E`) the upper-case
spelling declined "matched no applicable arm"; REPLACE and SHIFT already
canonicalise. The REMOVE arm now reads the canonical key, for every tag.

**Why this file imports what it does.** The pipeline through
`isocenter.session`, hand-built findings through `isocenter.privacy`, a
nested item through `isocenter.entities`: those rows of the probe's
`TARGETS`, with `remediation`'s.
"""
import sqlite3

import pytest

from isocenter.entities import DicomItem
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.session import DicomSession
from support.ct_small_files import write_ct


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid").fetchall()


def _remove(instance, tag, original=None):
    return PhiFinding(
        entity_uid=instance.sop_instance_uid, entity_type="Instance",
        field_name=tag, value=original, reason="hand-built", entity=instance,
        tag=tag.lower(),
        remediation_proposal=PhiRemediation(
            action_type="REMOVE_TAG", target_attr=tag, original_value=original))


def _session(tmp_path):
    write_ct(tmp_path / "src" / "a.dcm", "PID-1012", 1012, name="Alpha^One")
    session = DicomSession(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "src"))
    return session


@pytest.mark.parametrize("spelling", ["0020,000d", "0020,000D"])
def test_an_owned_uid_removal_under_an_emptied_owner_removes_the_copy_with_its_row(
        tmp_path, spelling):
    with _session(tmp_path) as session:
        study = session.store.patients[0].studies[0]
        instance = study.series[0].instances[0]
        original = instance.attributes["0020,000d"]
        study.study_instance_uid = ""
        before = len(_rows(session))

        applied = session.anonymize([_remove(instance, spelling, original)])

        assert applied == 1
        assert "0020,000d" not in instance.attributes
        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_REMOVE"], rows


def test_an_upper_case_removal_of_a_present_tag_removes_it(tmp_path):
    with _session(tmp_path) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        instance.set_attr("0008,103e", "a description")
        before = len(_rows(session))

        applied = session.anonymize([_remove(instance, "0008,103E", "a description")])

        assert applied == 1
        assert "0008,103e" not in instance.attributes
        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_REMOVE"], rows


def test_an_upper_case_private_sequence_removal_removes_it(tmp_path):
    with _session(tmp_path) as session:
        instance = session.store.patients[0].studies[0].series[0].instances[0]
        instance.add_sequence_item("0009,10ab", DicomItem())
        assert "0009,10ab" in instance.sequences
        before = len(_rows(session))

        applied = session.anonymize([_remove(instance, "0009,10AB")])

        assert applied == 1
        assert "0009,10ab" not in instance.sequences
        rows = _rows(session)[before:]
        assert [a for a, _ in rows] == ["REMEDIATION_REMOVE"], rows
