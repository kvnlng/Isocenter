"""Ingest after UID replacement, and addressing by a UID an entity no
longer holds (#544).

`anonymize()` replaces a Study's, a Series' and an Instance's UID in the
graph, and the store re-keys on the next save. Everything that looks an
entity up by UID -- ingest's study and series maps, the #238 gate, a report
taken before the pass, an export subset -- has to still find it: "a UID
names an entity if it is the entity's UID, or the entity's UID is its
replacement" (spec section 3.7), and for an instance, also the UID it was
ingested under (`SOURCE_SOP_UID_ATTR`).
"""
import shutil
import sqlite3

import pydicom
import pytest

from isocenter.entities import NO_PATIENT_ID_PREFIX, SOURCE_SOP_UID_ATTR
from isocenter.privacy import _replacement_uid_for
from isocenter.session import DicomSession

from support.project_secret import FIXED_A, load_fixed_secret
from test_uids_are_replaced_by_the_project_secret import U_ROWS, OWNER_FIELDS, _ct

STUDY, SERIES = "1.2.3.99.1", "1.2.3.99.2"


def M(uid):
    return _replacement_uid_for(uid, FIXED_A)


def _cfg(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("privacy_profile: basic\n", encoding="utf-8")
    return str(path)


def _uid_findings(report):
    return [f for f in report
            if f.tag in U_ROWS or (f.remediation_proposal is not None
                                   and f.remediation_proposal.target_attr in OWNER_FIELDS)]


def _only(folder, *files):
    folder.mkdir()
    for f in files:
        shutil.copy(f, folder / f.name)
    return folder


def test_a_later_file_of_a_replaced_study_joins_it(tmp_path):
    """Kills the alias lookup removed: the second file would make a second
    Study under its source UID, and the export two study folders."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    _ct(tmp_path / "s2.dcm", "1.2.3.99.41")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        session.anonymize()
        session.save(sync=True)
        session.ingest(str(_only(tmp_path / "two", tmp_path / "s2.dcm")))
        session.anonymize()
        session.save(sync=True)
        (patient,) = session.store.patients
        (study,) = patient.studies
        (series,) = study.series
        assert study.study_instance_uid == M(STUDY)
        assert series.series_instance_uid == M(SERIES)
        assert sorted(i.sop_instance_uid for i in series.instances) == sorted(
            [M("1.2.3.99.40"), M("1.2.3.99.41")])
        session.export(str(tmp_path / "out"), use_compression=False)
    studies = {p.parent.parent for p in (tmp_path / "out").rglob("*.dcm")}
    assert len(studies) == 1
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT study_instance_uid FROM studies").fetchall() == [(M(STUDY),)]


def test_a_copy_of_a_replaced_instances_source_is_declined(tmp_path):
    """A copy of the source file at a new path carries the source SOP UID,
    which the graph no longer holds; the UID the instance was ingested
    under does. Kills: the anonymize-time `SOURCE_SOP_UID_ATTR` not
    recorded (the copy is admitted as a second instance of one image);
    the redaction-only wording."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        session.anonymize()
        inst = session.store.patients[0].studies[0].series[0].instances[0]
        assert inst.attributes[SOURCE_SOP_UID_ATTR] == "1.2.3.99.40"
        copy = tmp_path / "copy"
        copy.mkdir()
        shutil.copy(tmp_path / "s1.dcm", copy / "renamed.dcm")
        summary = session.ingest(str(copy))
        assert summary.ingested == 0 and summary.declined == 1
        session.save(sync=True)
    with sqlite3.connect(str(db)) as conn:
        rows = conn.execute("SELECT entity_uid, details FROM audit_log WHERE "
                            "action_type='WARNING' AND details LIKE "
                            "'Not importing%'").fetchall()
    assert [uid for uid, _ in rows] == ["1.2.3.99.40"]
    assert "before this store replaced it" in rows[0][1], rows[0][1]
    assert M("1.2.3.99.40") in rows[0][1]


def test_a_re_ingested_export_is_not_replaced_again(tmp_path):
    """A second store under the same secret ingests the first store's
    export: nothing it carries is a UID finding, and its own export is the
    same. (Into the *same* store, #431's duplicate gate declines every file
    first, so the second store is what reaches the scan.) Kills the minted
    guard removed from the scan."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    with DicomSession(str(tmp_path / "a.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        session.anonymize()
        session.export(str(tmp_path / "first"), use_compression=False)
    with DicomSession(str(tmp_path / "b.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(tmp_path / "first"))
        assert _uid_findings(session.audit()) == []
        session.anonymize()
        session.export(str(tmp_path / "second"), use_compression=False)
    first = [pydicom.dcmread(p) for p in (tmp_path / "first").rglob("*.dcm")]
    second = [pydicom.dcmread(p) for p in (tmp_path / "second").rglob("*.dcm")]
    for keyword in ("SOPInstanceUID", "StudyInstanceUID", "SeriesInstanceUID",
                    "FrameOfReferenceUID"):
        assert [getattr(d, keyword) for d in first] == [getattr(d, keyword) for d in second]


def test_a_file_joins_the_study_held_under_its_own_uid_first(tmp_path):
    """A store can hold one study twice: under its source UID (ingested and
    not yet anonymized) and under its replacement (another store's export,
    same secret). A later source file joins the study under the UID it
    carries; the replacement is only the fallback. Kills the held-UID
    short-circuit dropped from `_held_under`."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    _ct(tmp_path / "s2.dcm", "1.2.3.99.41")
    with DicomSession(str(tmp_path / "a.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        session.anonymize()
        session.export(str(tmp_path / "a-out"), use_compression=False)
    with DicomSession(str(tmp_path / "b.db")) as session:
        load_fixed_secret(session)
        session.ingest(str(_only(tmp_path / "src1", tmp_path / "s1.dcm")))
        session.ingest(str(tmp_path / "a-out"))
        session.ingest(str(_only(tmp_path / "src2", tmp_path / "s2.dcm")))
        held = {study.study_instance_uid: sorted(i.sop_instance_uid for s in study.series
                                                for i in s.instances)
                for p in session.store.patients for study in p.studies}
    assert held == {STUDY: ["1.2.3.99.40", "1.2.3.99.41"],
                    M(STUDY): [M("1.2.3.99.40")]}


def test_ingest_never_mints_a_secret(tmp_path):
    """Ingest reads the secret only where the store already holds one.
    Kills `_project_secret_for_use` at ingest."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
    with sqlite3.connect(str(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM project_secret").fetchone()[0] == 0


def test_an_id_less_file_of_a_replaced_study_joins_its_subject(tmp_path):
    """L8's ID-less subject is keyed on its **source** Study UID
    (`NO_PATIENT_ID_PREFIX + study`), which is never exported and never
    rewritten. A later ID-less file of the same study, after the study's UID
    was replaced, joins that subject through the alias. Kills: the
    synthetic key rewritten to the minted UID; the alias missing from the
    study-first linkage."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40", patient_id="")
    _ct(tmp_path / "s2.dcm", "1.2.3.99.41", patient_id="")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        session.anonymize()
        session.ingest(str(_only(tmp_path / "two", tmp_path / "s2.dcm")))
        (patient,) = session.store.patients
        assert patient.patient_id == NO_PATIENT_ID_PREFIX + STUDY
        (study,) = patient.studies
        assert len(study.series[0].instances) == 2


# --------------------------------------------------------------------
# A1-A2: a UID from before the pass still names its entity
# --------------------------------------------------------------------

def test_a_report_kept_across_a_reopen_resolves_after_replacement(tmp_path):
    """The report names source UIDs; the reopened graph holds minted ones.
    Study and Series findings resolve through the alias, and the pass
    declines nothing. Kills the alias not tried in `_live_findings`."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    db = tmp_path / "s.db"
    with DicomSession(str(db)) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "one", tmp_path / "s1.dcm")))
        report = session.audit()
        session.anonymize(report)
        session.save(sync=True)
    with DicomSession(str(db)) as session:
        session.load_config(_cfg(tmp_path))
        session.anonymize(report)
        session.save(sync=True)
    with sqlite3.connect(str(db)) as conn:
        declined = conn.execute("SELECT entity_uid, details FROM audit_log WHERE "
                                "action_type='REMEDIATION_DECLINED'").fetchall()
    assert declined == []
    owners = [f for f in report if f.entity_type in ("Study", "Series")
              and f.remediation_proposal.target_attr in OWNER_FIELDS]
    assert {f.entity_type for f in owners} == {"Study", "Series"}


def test_a_subset_taken_before_anonymize_still_selects(tmp_path):
    """Kills the #678-shaped empty export: a subset of source UIDs after
    the pass matched nothing."""
    _ct(tmp_path / "s1.dcm", "1.2.3.99.40")
    _ct(tmp_path / "s2.dcm", "1.2.3.99.50", study="1.2.3.99.51", series="1.2.3.99.52")
    with DicomSession(str(tmp_path / "s.db")) as session:
        load_fixed_secret(session)
        session.load_config(_cfg(tmp_path))
        session.ingest(str(_only(tmp_path / "src", tmp_path / "s1.dcm", tmp_path / "s2.dcm")))
        session.anonymize()
        session.export(str(tmp_path / "study"), subset=[STUDY], use_compression=False)
        session.export(str(tmp_path / "sop"), subset=["1.2.3.99.50"], use_compression=False)
    assert [pydicom.dcmread(p).StudyInstanceUID
            for p in (tmp_path / "study").rglob("*.dcm")] == [M(STUDY)]
    assert [pydicom.dcmread(p).SOPInstanceUID
            for p in (tmp_path / "sop").rglob("*.dcm")] == [M("1.2.3.99.50")]
