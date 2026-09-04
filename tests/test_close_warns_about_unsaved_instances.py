"""`close()` says so when it is dropping unsaved instance changes (#307).

`Session.close()` shuts down the persistence manager, the audit thread
and the executor. What it never did was look at the graph: an instance
carrying edits that no `save()` ever reached was closed over in silence,
and the changes were gone. The three verbs that produce those edits are
the ordinary ones -- `audit()` advances `_revision` through
`record_phi_status()`, and `anonymize()` and `redact()` both mutate --
so "I audited and closed" is the shape this is most often about, which
is why the message has to name *what* is unsaved rather than only how
much.

**Scoped to instances, deliberately.** `SqliteStore.save_all` calls
`mark_persisted()` on instances only; patients, studies and series get no
`mark_persisted()` anywhere in the save walk, so a *built-then-saved*
graph reaches `close()` with every parent level reporting
`has_unsaved_changes` while nothing is actually pending. Warning on those
would fire on every correct session, and a warning that fires when
nothing is wrong is one people learn to skip past. `mark_subtree_persisted()`
is hydration-only, which is why a *reloaded* parent looks clean and a
built one does not -- the same asymmetry `test_private_tag_reload.py`
already pins. Widening this warning means fixing the save walk first, and
that needs per-parent revision capture (the `mark_persisted()` trap in
CLAUDE.md), not a wider list comprehension here.

The first two tests in this file are **measurements**, written before the
warning existed and kept because the warning's condition is only correct
while they hold: they assert directly that a saved session, and an
exported one, reach `close()` with no dirty instances. If either goes
red, the warning would cry wolf on a correct run and its condition -- not
its assertion -- is what needs narrowing.

Fixtures here are hand-built rather than `reloaded_redaction_session`,
and that matters: after #322 a redacted instance is nulled *and*
`mark_modified()`, so a redaction session closed without saving fires
this warning correctly. Building on that fixture would couple the two
fixes' tests together.
"""
from datetime import date

import numpy as np
import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"

#: Everything `IODValidator` demands of a CT image, so the graph is
#: exportable and the export measurement below is not measuring a refusal.
#: Same set as `tests/test_export_failure_audit.py`.
CT_REQUIRED = (
    ("0008,0020", "20230101"), ("0008,0030", "120000"),
    ("0008,0060", "CT"),
    ("0018,0050", "1.0"), ("0018,0060", "120"),
    ("0020,0032", ["0", "0", "0"]),
    ("0020,0037", ["1", "0", "0", "0", "1", "0"]),
    ("0028,0030", ["0.5", "0.5"]),
)

#: The fragment the warning is identified by. Matched rather than the
#: whole sentence so a rewording does not silently retire the test.
WARNING_FRAGMENT = "unsaved change"


def _session(tmp_path, name="close"):
    """Two exportable CT instances, saved synchronously, in a fresh store."""
    session = DicomSession(str(tmp_path / f"{name}.db"))

    patient = Patient("PAT1", "Original Name")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)

    for n in range(2):
        inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        for tag, value in CT_REQUIRED:
            inst.set_attr(tag, value)
        inst.set_pixel_data(np.zeros((8, 8), dtype=np.uint16))
        series.instances.append(inst)

    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    return session


def _instances(session):
    return [i
            for p in session.store.patients
            for st in p.studies
            for se in st.series
            for i in se.instances]


def test_a_synchronously_saved_session_reaches_close_with_clean_instances(
        tmp_path):
    """Measurement, written before the warning: is the premise even true?

    The warning fires on `has_unsaved_changes` over instances, so it is
    correct only if a session that did the right thing has none by the
    time `close()` runs. Asserted directly rather than through the
    warning's absence -- "no warning was printed" passes trivially
    against code that has no warning, and cannot measure anything.
    """
    session = _session(tmp_path)
    try:
        dirty = [i for i in _instances(session) if i.has_unsaved_changes]
        assert dirty == [], (
            "a synchronously saved session already has unsaved instances; "
            "the warning would fire on a correct run")
    finally:
        session.close()

    assert [i for i in _instances(session) if i.has_unsaved_changes] == [], (
        "close() itself left instances dirty, so the warning's condition "
        "cannot be read after shutdown()")


def test_an_exported_session_reaches_close_with_clean_instances(tmp_path):
    """Measurement: `_export_dicom`'s save is asynchronous.

    `export()` calls `self.save()` -- not `save(sync=True)` -- and
    nothing after it mutates the graph, so on the documented order the
    instances *should* be clean at `close()`. That is a timing property
    of the persistence manager's drain and of `shutdown()`'s
    reconciliation, not something the source states, so it is measured
    here. The warning is emitted after `shutdown()` precisely so this
    holds.
    """
    session = _session(tmp_path, name="exported")
    try:
        session.anonymize()
        # No `save(sync=True)` after this: the export's own save is the
        # thing under measurement. Adding one would answer a question
        # nobody asked.
        session.export(str(tmp_path / "out"), show_progress=False)
    finally:
        session.close()

    assert [i for i in _instances(session) if i.has_unsaved_changes] == [], (
        "an anonymize-then-export session reaches close() with unsaved "
        "instances, so the warning fires on the documented pipeline")


def test_a_clean_close_says_nothing(tmp_path, capsys):
    """The cry-wolf guard, and it passes on both sides of the fix.

    It cannot go red against code with no warning at all -- that is what
    the two measurements above are for. It is here because it is the
    thing that goes red if the warning is ever widened past instances:
    every parent level in this hand-built graph reports
    `has_unsaved_changes` after a successful save, so an unscoped version
    of this warning fires here.
    """
    session = _session(tmp_path, name="clean")
    capsys.readouterr()
    session.close()

    assert WARNING_FRAGMENT not in capsys.readouterr().out


def test_an_unsaved_instance_edit_is_named_at_close(tmp_path, capsys):
    """The positive case: one instance, its count and its UID."""
    session = _session(tmp_path, name="dirty")
    inst = _instances(session)[0]
    inst.set_attr("0008,103E", "changed after the save")

    capsys.readouterr()
    session.close()
    out = capsys.readouterr().out

    assert WARNING_FRAGMENT in out
    # `"1" in out` would be satisfied by the UID's own digits, and by
    # "instance(s)" in a message about eleven of them.
    assert "1 instance(s)" in out
    assert inst.sop_instance_uid in out, (
        "the warning names a count but not which instance, so a caller "
        "cannot tell what they are about to lose")


def test_a_dirty_parent_alone_does_not_warn(tmp_path, capsys):
    """The scope guard: patients, studies and series are out of scope.

    `save_all` never marks them persisted, so they are *always* dirty
    after a build-and-save. Warning on them would fire on every session
    this suite creates.
    """
    session = _session(tmp_path, name="parent")
    session.store.patients[0].mark_modified()

    capsys.readouterr()
    session.close()

    assert WARNING_FRAGMENT not in capsys.readouterr().out


def test_a_broken_walk_does_not_break_close(tmp_path, monkeypatch):
    """A bug in a diagnostic must not become a close failure.

    `close()` re-raises the first exception any of its steps raised, and
    a raise here would abort before the executor shut down -- leaking
    worker subprocesses for the life of the interpreter, which is the
    exact failure `close()`'s ordering exists to prevent. So the warning
    is not routed through `_run_step` and swallows its own errors.
    """
    session = _session(tmp_path, name="broken")

    class _BrokenStore:
        @property
        def patients(self):
            raise RuntimeError("the walk is broken")

    # Replaced after the save, so nothing else in `close()` needs it:
    # `persistence_manager.shutdown()` drains jobs that carry their own
    # patient lists, and `store_backend.stop()` never reads the graph.
    session.store = _BrokenStore()

    session.close()

    # close() returned rather than raising, and got far enough to shut
    # the executor down -- which is what a raise from the diagnostic
    # would have cost.
    with pytest.raises(RuntimeError):
        session._executor.submit(int, "1")


def test_a_second_close_warns_again(tmp_path, capsys):
    """Stated rather than suppressed: `close()` is idempotent, this is not.

    A second `close()` over a still-dirty graph says the same thing
    again. That is accepted -- the graph really is still unsaved, and
    tracking "already warned" would be a second piece of state answering
    a question the graph answers. Zero dirty instances is silent, so the
    ordinary double-close prints nothing extra.
    """
    session = _session(tmp_path, name="twice")
    _instances(session)[0].set_attr("0008,103E", "changed after the save")

    session.close()
    capsys.readouterr()
    session.close()

    assert WARNING_FRAGMENT in capsys.readouterr().out
