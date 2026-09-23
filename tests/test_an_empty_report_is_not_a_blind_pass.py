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


def _secret_rows(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        return list(conn.execute("SELECT COUNT(*) FROM project_secret"))[0][0]


def test_an_empty_call_still_reads_the_stores_project_secret(tmp_path):
    """The read that stays above the guard.

    An empty call applies nothing, but it is still a pass over this
    store, and the store's project secret is what a pass is conducted
    under: a fresh store gets its row, and a store whose row is gone
    refuses rather than quietly returning 0. A call that refuses
    consistently is easier to reason about than one that refuses only
    when the list is non-empty (#660, ruling Q3).
    """
    with _session(tmp_path) as session:
        assert _secret_rows(tmp_path / "m.db") == 0

        assert session.anonymize([]) == 0

        assert _secret_rows(tmp_path / "m.db") == 1


def test_an_empty_call_on_a_store_that_lost_its_secret_refuses(tmp_path):
    """The other half: the refusal is not skipped for an empty call."""
    with _session(tmp_path) as session:
        session.anonymize()
        session.save(sync=True)
    with sqlite3.connect(str(tmp_path / "m.db")) as conn:
        conn.execute("DELETE FROM project_secret")

    with DicomSession(str(tmp_path / "m.db")) as second:
        with pytest.raises(RuntimeError) as raised:
            second.anonymize([])

    assert "no longer has" in str(raised.value)
    # The advice names no carry: there is none since #716.
    assert "re-ingest" in str(raised.value)


def test_a_report_taken_after_an_empty_call_attests_nothing(tmp_path):
    """The grade the empty call now earns: the truth about a graph
    nothing cleaned, where before this it graded `PASS` over a pass the
    caller never asked for (#660)."""
    with _session(tmp_path) as session:
        assert session.anonymize([]) == 0
        out = tmp_path / "report.md"
        session.generate_report(str(out))

    text = out.read_text()
    assert "REVIEW_REQUIRED" in text
    assert ("the audit trail holds no rows, so nothing this run did is "
            "attested (section 2)") in text


def test_a_filtered_report_that_matched_nothing_remediates_nothing(tmp_path):
    """The shape that produced the report: a caller narrows a report and
    the filter matches nothing, so nothing is asked for."""
    with _session(tmp_path) as session:
        report = session.audit()
        # Patient's Address, which the fixture does not carry. It was
        # `entity_type == "Series"` until a Series had findings of its own
        # (#544).
        assert report.findings
        subset = PhiReport([f for f in report.findings
                            if f.tag == "0010,1040"])
        assert list(subset) == []
        before = len(_rows(session))

        assert session.anonymize(subset) == 0

        assert _remediation_rows(session, before) == []
        assert _names(session) == ["CompressedSamples^CT1",
                                   "CompressedSamples^MR1"]
