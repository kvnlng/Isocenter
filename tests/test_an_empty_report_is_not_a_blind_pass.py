"""Only `None` asks `anonymize()` to work out what to remediate (#660).

`anonymize()`'s blind-execution check was `if not findings:`, and
`PhiReport` has `__len__`, so an empty report was falsy and indexed as
"no argument": `anonymize([])`, `anonymize(())`, `anonymize(PhiReport([]))`
and any filtered report that matched nothing ran a full `audit()` and a
full remediation pass. An empty *iterator* did not, because an iterator
is truthy -- so the same emptiness meant two different things depending
on the container it arrived in.

"Remediate these findings" and "work out what to remediate" are
different requests, and the only spelling for the second is `None`.
These tests pin both halves: an empty argument of any shape applies
nothing and scans nothing, and no argument -- or an explicit `None` --
still runs the blind pass.
"""
import json
import shutil
import sqlite3

import pytest
from pydicom.data import get_testdata_file

from isocenter.privacy import PhiReport
from isocenter.session import DicomSession


def _sources(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("CT_small.dcm", "MR_small.dcm"):
        shutil.copy(get_testdata_file(name), src / name)
    return str(src)


def _session(tmp_path, rules=None):
    session = DicomSession(str(tmp_path / "m.db"))
    if rules is not None:
        cfg = tmp_path / "cfg.yaml"
        cfg.write_text(json.dumps({"phi_tags": rules}))
        session.load_config(str(cfg))
    session.ingest(_sources(tmp_path))
    session.save(sync=True)
    return session


def _rows(session):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.store_backend.db_path) as conn:
        return list(conn.execute(
            "SELECT action_type, details FROM audit_log ORDER BY rowid"))


def _remediation_rows(session, before=0):
    return [(a, d) for a, d in _rows(session)[before:]
            if a.startswith("REMEDIATION_")]


def _names(session):
    return sorted(str(p.patient_name) for p in session.store.patients)


@pytest.mark.parametrize("empty", ["list", "tuple", "report", "iterator"])
def test_an_empty_report_applies_nothing_and_runs_no_audit(tmp_path, empty):
    """An empty argument is not a request to remediate everything.

    The iterator parameter passed before this change too: it is here so
    the four containers are read by one assertion, and so a fix that
    reached only the sized ones would be visible.
    """
    with _session(tmp_path) as session:
        arg = {"list": [], "tuple": (), "report": PhiReport([]),
               "iterator": iter([])}[empty]
        audits = []
        real = session.audit
        session.audit = lambda *a, **k: (audits.append(1), real(*a, **k))[1]
        before = len(_rows(session))

        assert session.anonymize(arg) == 0

        assert audits == []
        assert _remediation_rows(session, before) == []
        assert _names(session) == ["CompressedSamples^CT1",
                                   "CompressedSamples^MR1"]
        assert "ANONYMIZE" not in session._actions_performed


@pytest.mark.parametrize("form", ["omitted", "none"])
def test_no_argument_and_none_still_run_the_blind_pass(tmp_path, form):
    """The half that must not move: `None` is still the blind pass."""
    with _session(tmp_path) as session:
        applied = (session.anonymize() if form == "omitted"
                   else session.anonymize(None))

        assert applied > 0
        assert _names(session) == ["ANONYMIZED", "ANONYMIZED"]


def test_a_filtered_report_that_matched_nothing_remediates_nothing(tmp_path):
    """The shape that produced the report: a caller narrows a report and
    the filter matches nothing, so nothing is asked for."""
    with _session(tmp_path) as session:
        report = session.audit()
        subset = PhiReport([f for f in report.findings
                            if f.entity_type == "Series"])
        assert list(subset) == []
        before = len(_rows(session))

        assert session.anonymize(subset) == 0

        assert _remediation_rows(session, before) == []
        assert _names(session) == ["CompressedSamples^CT1",
                                   "CompressedSamples^MR1"]
