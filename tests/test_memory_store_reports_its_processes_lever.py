"""What `redact()` says about a processes lever it cannot honour (#400).

An operator sets a processes lever -- in a shell profile, a CI job, a
deployment manifest -- and runs a pipeline against `Session(":memory:")`.
Every other parallel pass in that process obeys it: `audit()`,
`scan_pixel_content()` and `export()` run in processes, and `ingest()`
ignores it either way (#390). `redact()` alone did neither of the two
honest things, and which of them it owes depends on the lever:

| lever | `use_threads` | what happened | response |
| --- | --- | --- | --- |
| none | either | nobody asked; the free-threaded default is a default | silence |
| `ISOCENTER_FORCE_PROCESSES` | True | the request was discarded, the pass was correct | **warn** |
| `ISOCENTER_MAX_TASKS_PER_CHILD` | False | the request was obeyed and cannot work | **refuse** |

`redact()` passes `force_threads=True` for a `:memory:` store, which sits
at rank 2 of `_resolve_execution_choice`'s order, so it beats
`ISOCENTER_FORCE_PROCESSES` (rank 3) and loses to
`ISOCENTER_MAX_TASKS_PER_CHILD` (rank 1). Measured on 3.12.14 and
3.14.7t: under `ISOCENTER_FORCE_PROCESSES=1` the pass runs in threads,
returns 3 and zeroes every zone -- **nothing about the run is wrong**, so
there is a correct run to annotate and a warning is what it is owed
(#185's case). Under `ISOCENTER_MAX_TASKS_PER_CHILD` the pass runs in
processes and all three of three instances fail with `no such table:
instance_blobs`, every time, because `execute_redaction_task` ends in
`persist_pixel_data` on every task -- there is no run in which obeying it
is correct, so it is refused.

**The separating field is `strategy.use_threads`, not the lever's name.**
There is no `if lever == "ISOCENTER_FORCE_PROCESSES"` anywhere; a fifth
lever added at some future rank would be classified correctly without
touching the helper.

**Selecting a record before asserting on it.** A `caplog` assertion on a
strategy message is the same correct-by-accident shape as a `capsys`
assertion on the banner, and for the same measured reason: one real
warning in this tree names `threads`, `processes` and two of the three
variables at once. Every warning assertion here selects the WARNING
records whose message names the lever, asserts there is **exactly one**,
and only then asserts on that record's `getMessage()`. Asserting the
count first is what stops an unrelated warning satisfying the test, and
it is what makes "fires once" testable at all.
"""
import logging
from datetime import date

import numpy as np
import pytest

from isocenter import session as session_module
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.parallel import _Strategy
from isocenter.services import RedactionError
from isocenter.session import DicomSession, _NO_IMAGE_MATCHED

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
SERIAL = "SN_PROBE"
FILL = 1000
ZONE = [0, 10, 0, 10]  # [y1, y2, x1, x2]

FORCE_PROCESSES = "ISOCENTER_FORCE_PROCESSES"
MAX_TASKS = "ISOCENTER_MAX_TASKS_PER_CHILD"

THREADS_LINE = "Executing using 3 workers (threads)..."
PROCESSES_LINE = "Executing using 3 workers (processes)..."


@pytest.fixture(autouse=True)
def three_workers_and_no_levers(monkeypatch):
    """A pinned worker count, and no lever this file did not set itself."""
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "3")
    for name in ("ISOCENTER_FORCE_THREADS", FORCE_PROCESSES, MAX_TASKS):
        monkeypatch.delenv(name, raising=False)


def _populate(session, serial=SERIAL):
    """Three CT instances on one machine, 16x16, every pixel `FILL`."""
    patient = Patient("PAT1", "Probe^Patient")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    series.equipment = Equipment("Probe", "Model", SERIAL)
    for n in range(3):
        inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        inst.set_attr("0018,1000", SERIAL)
        inst.set_attr("0008,0060", "CT")
        inst.set_attr("0010,0010", "Probe^Patient")
        inst.set_pixel_data(np.full((16, 16), FILL, dtype=np.uint16))
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    session.configuration.rules = [
        {"serial_number": serial, "redaction_zones": [ZONE]}]
    return session


def _instances(session):
    return [inst
            for p in session.store.patients
            for st in p.studies
            for se in st.series
            for inst in se.instances]


def _naming(caplog, lever):
    """The WARNING records that name `lever`, as formatted messages."""
    return [record.getMessage() for record in caplog.records
            if record.levelname == "WARNING" and lever in record.getMessage()]


def _assert_zone_redacted(session):
    for inst in _instances(session):
        pixels = inst.get_pixel_data()
        assert not pixels[0:10, 0:10].any(), (
            f"{inst.sop_instance_uid}: the zone was not zeroed")
        assert pixels[12, 12] == FILL, (
            f"{inst.sop_instance_uid}: a pixel outside the zone changed")


# --------------------------------------------------------------------------
# The lever that is ignored while the run stays correct: a warning.
# --------------------------------------------------------------------------


def test_force_processes_on_a_memory_store_warns_and_still_redacts(
        monkeypatch, caplog):
    """The whole ruling in one test: it runs, it is right, and it says so.

    Refusing here was considered and rejected. The result was never
    wrong -- measured, `redact()` returns 3 and zeroes every zone on both
    gate interpreters -- so there is a correct run to annotate, which is
    #185's case rather than its inverse.

    Killing edit: the warning deleted.
    """
    monkeypatch.setenv(FORCE_PROCESSES, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            assert session.redact() == 3
            _assert_zone_redacted(session)

    selected = _naming(caplog, FORCE_PROCESSES)
    assert len(selected) == 1, (
        f"expected exactly one warning naming {FORCE_PROCESSES}, got "
        f"{len(selected)}: {selected}")
    message = selected[0]
    assert '":memory:"' in message, (
        "the warning does not name the store type, so a reader with "
        "several sessions cannot tell which one it is about")
    assert FORCE_PROCESSES in message
    assert "had no effect" in message, (
        "the warning does not say the variable was ignored")
    assert "instance_blobs" in message, (
        "the warning does not say why processes cannot work here")
    assert 'Session("session.db")' in message, (
        "the warning offers no remedy the reader can type")


def test_the_warning_does_not_name_a_knob_the_operator_did_not_set(
        monkeypatch, caplog):
    """The one property a reviewer cannot see by watching the rest pass.

    #185's warning opens `force_threads=True was set`, and on the
    redaction path `force_threads=True` was set by `redact()`, not by the
    reader -- so it points at a knob they cannot find and did not touch.
    That sentence is the model of what this message must not become.

    Killing edit: the warning written as a copy of #185's.
    """
    monkeypatch.setenv(FORCE_PROCESSES, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            assert session.redact() == 3

    selected = _naming(caplog, FORCE_PROCESSES)
    assert len(selected) == 1, selected
    assert "force_threads" not in selected[0], (
        "the warning names `force_threads`, which redact() set and the "
        f"reader did not: {selected[0]!r}")


def test_the_warning_fires_once_for_a_whole_session_of_passes(
        monkeypatch, caplog, ocr_present):
    """Once per `redact()` call, not once per `run_parallel`.

    #185's warning fires on every parallel pass in the process when both
    its levers are set, which its own comment concedes is "a lot of
    output on a long run"; this one is emitted from `session.py`, once.

    Killing edit: the warning emitted from `parallel.py` instead, keyed
    on `strategy.processes_requested_by and strategy.use_threads` -- the
    shape an implementer reaches for, since `parallel.py` cannot see the
    store's path. It passes every other test in this file.

    **`discover_redaction_zones()` is the pass that discriminates, and
    `audit()` is not.** Measured: with the warning moved to
    `parallel.py`, a session that runs `audit()` then `redact()` still
    warns exactly once, so this test was a full survivor of the very
    mutation it names. `audit()` under `ISOCENTER_FORCE_PROCESSES=1`
    *honours* the request -- `use_threads` is `False` there -- so the
    parallel-side condition is false on it, and it is the one pass in
    the session that cannot tell the two placements apart.
    `discover_redaction_zones()` passes `force_threads=True`
    unconditionally, so its strategy carries `use_threads=True` beside a
    set `processes_requested_by`: it is a second pass the relocated
    warning fires on, and this test then reads `got 2`.

    That placement has a second wrong output which **nothing in this
    file observes**: on a *file* store the relocation prints a sentence
    about a `":memory:"` store for a pass that has nothing to do with
    one. Killing the placement forecloses it; this test does not assert
    on it, and no count here would see it.
    """
    monkeypatch.setenv(FORCE_PROCESSES, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            # A configured tag list, so the audit pass is unambiguous
            # rather than merely reporting that it had nothing to look
            # for.
            session.configuration.phi_tags = {
                "0010,0010": {"name": "Patient Name", "action": "EMPTY"}}
            report = session.audit()
            assert report.findings, (
                "the audit found nothing, so it is not the parallel pass "
                "this test needs to have run")
            assert session.redact() == 3
            # The pass that tells the two placements apart: it asks for
            # threads unconditionally, so its strategy carries
            # `use_threads=True` beside a set `processes_requested_by`,
            # which is the exact condition a parallel-side warning would
            # be keyed on.
            session.discover_redaction_zones(SERIAL)

    selected = _naming(caplog, FORCE_PROCESSES)
    assert len(selected) == 1, (
        f"a session that ran audit(), redact() and "
        f"discover_redaction_zones() under {FORCE_PROCESSES} must warn "
        f"once, from redact(); got {len(selected)}: {selected}")


def test_a_file_store_with_force_processes_says_nothing(tmp_path, monkeypatch,
                                                        caplog, capsys):
    """The variable is honoured here, so there is nothing to report.

    Killing edit: the `db_path == ":memory:"` half of the condition
    dropped.
    """
    monkeypatch.setenv(FORCE_PROCESSES, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(str(tmp_path / "file_store.db")) as session:
            _populate(session)
            assert session.redact() == 3

    assert _naming(caplog, FORCE_PROCESSES) == [], (
        "a file-backed store obeys the variable; nothing was ignored")
    assert PROCESSES_LINE in capsys.readouterr().out.splitlines(), (
        "and the pass must actually have run in processes, or this test "
        "asserts silence about a run that had something to say")


def test_a_memory_store_with_no_lever_says_nothing_and_says_threads(capsys,
                                                                    caplog):
    """The out-of-the-box path stays quiet and stays working.

    On 3.12, the `python_requires` floor, processes are the *default* for
    a store with no lever set -- and a default is not a request. An
    attribution that treated rank 4 as one would warn on every
    `Session(":memory:")` in existence.

    Killing edit: attribution that treats the free-threaded default rank
    as a request.
    """
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            assert session.redact() == 3
            _assert_zone_redacted(session)

    assert _naming(caplog, FORCE_PROCESSES) == [], (
        "nobody asked for processes; a default is not a request")
    assert THREADS_LINE in capsys.readouterr().out.splitlines()


def test_force_threads_beside_force_processes_says_nothing(monkeypatch,
                                                           capsys, caplog):
    """Both force levers: the operator's own threads lever supersedes.

    `ISOCENTER_FORCE_THREADS` wins over `ISOCENTER_FORCE_PROCESSES` by
    the documented order, so the operator's effective request *is*
    threads and nothing has been denied. A session that read
    `ISOCENTER_FORCE_PROCESSES` directly -- the cheapest patch, and the
    one this design rejects -- would warn here.

    Killing edit: attribution that reports `ISOCENTER_FORCE_PROCESSES`
    without the `ISOCENTER_FORCE_THREADS` supersession.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv(FORCE_PROCESSES, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            assert session.redact() == 3

    assert _naming(caplog, FORCE_PROCESSES) == [], (
        "nothing was ignored here, so there is nothing to say")
    assert THREADS_LINE in capsys.readouterr().out.splitlines()


# --------------------------------------------------------------------------
# The lever that is obeyed and cannot work: a refusal.
# --------------------------------------------------------------------------


def test_max_tasks_per_child_on_a_memory_store_refuses(monkeypatch):
    """Only `multiprocessing.Pool` recycles, so this one wins and breaks.

    Measured on both gate interpreters: three of three instances fail
    with `no such table: instance_blobs`, every time, because
    `execute_redaction_task` ends in `persist_pixel_data` on every task.
    There is no correct-run-with-a-caveat to annotate.

    Killing edit: the refusal deleted, or keyed on the lever's name
    rather than on `strategy.use_threads`.
    """
    monkeypatch.setenv(MAX_TASKS, "1")
    with DicomSession(":memory:") as session:
        _populate(session)
        with pytest.raises(RuntimeError) as excinfo:
            session.redact()

    message = str(excinfo.value)
    assert '":memory:"' in message
    assert MAX_TASKS in message, (
        "the refusal does not name the variable the caller set")
    assert "would have run in processes" in message, (
        "the refusal does not say what continuing would have done")
    assert "no such table: instance_blobs" in message, (
        "the refusal does not say how it would have failed")
    assert f"Unset {MAX_TASKS}" in message, (
        "the refusal offers no remedy the reader can type")


def test_the_refusal_is_not_a_redaction_error(monkeypatch):
    """`except RedactionError` must not swallow a configuration refusal.

    `RedactionError` means *the pass ran and these instances failed*, and
    is raised at the end of a pass so a caller that catches it still
    holds a correct graph and a `REVIEW_REQUIRED` report. Nothing was
    attempted here. Its own test, so it cannot sit behind an assertion
    that fails first, and both halves are needed: `RedactionError` **is**
    a `RuntimeError`, so `isinstance(exc, RuntimeError)` alone passes for
    the wrong type.

    Killing edit: the refusal raised as `RedactionError`.
    """
    monkeypatch.setenv(MAX_TASKS, "1")
    with DicomSession(":memory:") as session:
        _populate(session)
        with pytest.raises(RuntimeError) as excinfo:
            session.redact()

    assert type(excinfo.value) is RuntimeError, (  # pylint: disable=C0123
        f"the refusal is a {type(excinfo.value).__name__}; a subclass "
        "would be machinery with nothing to do, and RedactionError would "
        "be the wrong report")
    assert not isinstance(excinfo.value, RedactionError)


def test_the_refusal_has_done_nothing(monkeypatch, caplog):
    """No task prepared, no UID regenerated, no audit row, no ERROR line.

    The positive control comes first and in the same session: a
    successful pass whose `REDACTION` count is asserted **non-zero**, so
    the "unchanged" assertion after the refusal is not a `0 == 0` that
    grades nothing. `get_audit_summary()` calls `flush_audit_queue()`
    before its `SELECT`, so it is a barrier rather than a race.

    The dispatch spy carries a positive control for the same reason the
    audit count does. `assert dispatched == []` is trivially true of a
    spy that was never wired to anything, so the spy is installed before
    the successful pass, asserted to have seen it, and only then
    cleared -- an empty list after the refusal then means the refusal
    dispatched nothing, rather than that nothing was ever being watched.

    The `caplog` clause is the one that catches a refusal placed inside
    `redact()`'s `try`: that handler logs `Redaction failed. ...` for any
    exception raised there, which would be a sentence about work that
    never started -- in a milestone about false sentences. The spy and
    the audit count stay green for such a placement.

    Killing edit: the refusal moved below task preparation, inside the
    `try`, or above the persistence drain.
    """
    dispatched = []
    real = session_module.run_parallel

    def spy(*args, **kwargs):
        strategy = kwargs.get("strategy")
        if strategy is not None:
            dispatched.append(strategy.desc)
        return real(*args, **kwargs)

    monkeypatch.setattr(session_module, "run_parallel", spy)

    with DicomSession(":memory:") as session:
        _populate(session)
        assert session.redact() == 3
        # The spy's own positive control. Installed *before* the
        # successful pass, so `dispatched` is asserted non-empty once
        # while work really is being dispatched: without this, a spy
        # patched onto the wrong target records nothing and the
        # `== []` below holds for a reason that has nothing to do with
        # the refusal.
        assert dispatched == ["Redacting Pixels"], (
            f"the spy saw no redaction dispatch on a pass that redacted "
            f"3 images, so it is not wired to the call under test: "
            f"{dispatched}")
        dispatched.clear()
        before = session.store_backend.get_audit_summary().get("REDACTION", 0)
        assert before, (
            "the positive control recorded no REDACTION row, so the "
            "assertion below would hold at 0 == 0 and grade nothing")
        pixels_before = {inst.sop_instance_uid: inst.get_pixel_data().copy()
                         for inst in _instances(session)}

        # The other side of the same placement: *after* the drain.
        # `docs/api/stability.md`'s frozen "audit() and redact() drain
        # the persistence manager on entry" is stated of every call, and
        # the CHANGELOG and `redact()`'s `Raises:` both say the refusal
        # is raised after it so that stays true of a refused one.
        # Measured: resolution and refusal moved *above*
        # `persistence_manager.flush()` passes the entire suite --
        # 1795/2 on 3.12.14, identical to unmutated. Three committed
        # claims and nothing behind them.
        drained = []
        real_flush = session.persistence_manager.flush

        def flush_spy(*args, **kwargs):
            drained.append(True)
            return real_flush(*args, **kwargs)

        monkeypatch.setattr(session.persistence_manager, "flush", flush_spy)
        monkeypatch.setenv(MAX_TASKS, "1")
        with caplog.at_level(logging.INFO):
            with pytest.raises(RuntimeError):
                session.redact()

        assert drained, (
            "the refused call never drained the persistence manager, so "
            'the frozen "audit() and redact() drain on entry" clause is '
            "not true of it -- the refusal is above the drain")
        assert dispatched == [], (
            f"the refusal dispatched work before raising: {dispatched}")
        after = session.store_backend.get_audit_summary().get("REDACTION", 0)
        assert after == before, (
            f"the refused call wrote audit accounting: {before} -> {after}")
        assert {inst.sop_instance_uid: inst.get_pixel_data()
                for inst in _instances(session)}.keys() == pixels_before.keys(), (
            "the refused call regenerated a SOP Instance UID")
        for inst in _instances(session):
            assert np.array_equal(inst.get_pixel_data(),
                                  pixels_before[inst.sop_instance_uid]), (
                f"{inst.sop_instance_uid}: the refused call touched pixels")

    assert not [record for record in caplog.records
                if record.getMessage().startswith("Redaction failed.")], (
        "the refusal was raised inside redact()'s try, so the session "
        "logged a sentence about work that never started")


def test_the_refusal_is_not_preceded_by_the_recycling_warning(monkeypatch,
                                                              caplog):
    """A strategy that is resolved and then refused says nothing.

    `redact()` resolves the strategy before it decides whether to run at
    all. While #185's recycling-override warning lived in the resolver,
    this configuration printed `... so this run uses processes.` one line
    above a refusal of a run that never starts -- a false sentence, in a
    milestone about false sentences. The warning now fires at dispatch.

    Killing edit: the `get_logger().warning(...)` left in
    `_resolve_execution_choice`. Its mandatory partner is
    `test_the_recycling_warning_still_fires_when_the_pass_actually_runs`:
    without it, moving the warning and simply *deleting* it are
    indistinguishable here.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv(MAX_TASKS, "1")
    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            with pytest.raises(RuntimeError):
                session.redact()

    assert not [record for record in caplog.records
                if "so this run uses processes" in record.getMessage()], (
        "the recycling override was announced in front of a pass that was "
        "then refused")


def test_the_recycling_warning_still_fires_when_the_pass_actually_runs(
        tmp_path, monkeypatch, caplog):
    """#185's speech survives the move (#400).

    **The mandatory partner of the test above**, and the only thing
    standing between "move the warning to dispatch" and "delete the
    warning" -- which would be this milestone undoing itself inside its
    own fix. Every other test in this file is green for a change that
    silently removed #185's speech.

    A file-backed store, so no refusal and no `:memory:` warning is in
    play; `ISOCENTER_FORCE_THREADS=1` beside `ISOCENTER_MAX_TASKS_PER_CHILD=25`,
    which is the contradiction #185 announces. The message text and the
    quoted number are both asserted, not merely a record count.

    Killing edit: the warning deleted rather than relocated.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv(MAX_TASKS, "25")
    with caplog.at_level(logging.WARNING):
        with DicomSession(str(tmp_path / "file_store.db")) as session:
            _populate(session)
            assert session.redact() == 3

    selected = [record.getMessage() for record in caplog.records
                if "so this run uses processes" in record.getMessage()]
    assert len(selected) == 1, (
        f"expected exactly one recycling-override warning from a pass "
        f"that ran; got {len(selected)}: {selected}")
    assert "ISOCENTER_FORCE_THREADS was set" in selected[0], (
        "the warning does not name the lever that was overridden")
    assert "maxtasksperchild=25" in selected[0], (
        "the warning does not quote the recycling value that won")


def test_the_refusal_reaches_redact_by_machine(monkeypatch):
    """`redact_by_machine()` inherits it, and still restores the rules.

    It swaps in a one-rule configuration and calls `self.redact()` inside
    a `try/finally`. The refusal travels out through that `finally`, so
    the caller's configuration is intact when it arrives (#213).

    Killing edit: the refusal placed inside `_apply_redaction_rules` and
    reached only from `redact()`.
    """
    monkeypatch.setenv(MAX_TASKS, "1")
    with DicomSession(":memory:") as session:
        _populate(session)
        original = list(session.configuration.rules)
        with pytest.raises(RuntimeError) as excinfo:
            session.redact_by_machine(SERIAL, ZONE)

        assert MAX_TASKS in str(excinfo.value)
        assert session.configuration.rules == original, (
            "redact_by_machine's finally did not restore the caller's "
            "configuration before the refusal reached them")


def test_the_refusal_fires_even_when_no_image_matches(monkeypatch, capsys):
    """A configuration that cannot run is refused whether or not it had work.

    The rule names a serial nothing carries, so without the refusal this
    call returns `0` after printing the no-match sentence,
    `session._NO_IMAGE_MATCHED`. One behaviour rather than two.

    The sentence is read from the module, not spelled here: until #807 it
    was spelled here, and #807's reword would have left this assertion
    true of every output, the refusal's placement included.

    Killing edit: the refusal placed after task preparation.
    """
    monkeypatch.setenv(MAX_TASKS, "1")
    with DicomSession(":memory:") as session:
        _populate(session, serial="SN_NOTHING_HAS_THIS")
        with pytest.raises(RuntimeError) as excinfo:
            session.redact()

    assert MAX_TASKS in str(excinfo.value)
    assert _NO_IMAGE_MATCHED not in capsys.readouterr().out, (
        "the refusal ran after task preparation; it must precede it")


def test_a_file_store_with_max_tasks_per_child_still_redacts(tmp_path,
                                                             monkeypatch):
    """Recycling is a legitimate setting; only the store makes it wrong.

    Killing edit: the `:memory:` half of the refusal condition dropped,
    which would break `session.export()`'s own recycling path by the same
    reasoning.
    """
    monkeypatch.setenv(MAX_TASKS, "2")
    with DicomSession(str(tmp_path / "file_store.db")) as session:
        _populate(session)
        assert session.redact() == 3
        _assert_zone_redacted(session)


def _synthetic_strategy(use_threads, lever):
    """A `_Strategy` shaped like `redact()`'s, asked for by `lever`.

    Built by hand rather than resolved, because the row it describes is
    the one the ranking cannot currently produce: a lever at a rank
    *below* `force_threads=True` that asks for processes and loses. Every
    other test in this file goes through `redact()` and therefore can
    only exercise the two rows the four ranks reach today.
    """
    return _Strategy(
        max_workers=3,
        chunksize=1,
        maxtasksperchild=None,
        disable_gc=False,
        use_threads=use_threads,
        show_progress=False,
        desc="Redacting Pixels",
        total=None,
        processes_requested_by=lever,
        threads_request_overridden_by=None)


def test_a_lever_that_asked_and_lost_is_warned_about_whatever_it_is_called(
        caplog):
    """The discriminator is `use_threads`, not the lever's name.

    §14.1's stated reason for keying on `strategy.use_threads` is a fifth
    lever added at some future rank: written that way it is classified
    correctly without touching the helper, where `if lever == "..."`
    would work today and be wrong the moment such a lever exists.

    Nothing held that. Measured: `session.py`'s `if strategy.use_threads:`
    replaced by `if lever == "ISOCENTER_FORCE_PROCESSES":` is **zero red**
    across every test in this file, `test_redaction_names_its_strategy.py`,
    `test_parallel_contract.py` and `test_memory_store_redaction_strategy.py`
    on both gate interpreters -- because the two rows `redact()` can
    actually reach today are exactly the two rows the hardcoded form gets
    right by coincidence. The reason for the design was written down in
    three docstrings and pinned by nothing.

    This calls the helper directly with a strategy the ranking cannot
    build yet, which is the only way to reach the row that tells the two
    spellings apart. Killing edit: `if lever == "ISOCENTER_FORCE_PROCESSES"`
    -- the hardcoded form then falls through to the refusal and this
    raises instead of warning.
    """
    lever = "ISOCENTER_SOME_FUTURE_LEVER"
    strategy = _synthetic_strategy(use_threads=True, lever=lever)

    with caplog.at_level(logging.WARNING):
        session_module._report_processes_lever_on_a_memory_store(
            ":memory:", strategy)

    selected = _naming(caplog, lever)
    assert len(selected) == 1, (
        f"a lever that asked for processes and lost is owed the same "
        f"warning whatever it is called; got {len(selected)}: {selected}")
    assert "had no effect on this run" in selected[0]


def test_a_lever_that_asked_and_won_is_refused_whatever_it_is_called():
    """The other half of the same discriminator.

    A lever that *obtained* processes on a `:memory:` store is refused
    because processes cannot redact one, and that is true of any lever
    that manages it -- the name is not what makes it fatal. Killing edit:
    a name test on the refusal side, which would let a future lever
    reach the pool and fail three of three with
    `no such table: instance_blobs`, the exact 0.9.4 behaviour #400
    replaced.
    """
    lever = "ISOCENTER_SOME_FUTURE_LEVER"
    strategy = _synthetic_strategy(use_threads=False, lever=lever)

    with pytest.raises(RuntimeError) as excinfo:
        session_module._report_processes_lever_on_a_memory_store(
            ":memory:", strategy)

    assert lever in str(excinfo.value), (
        "the refusal must name the lever it read, not a hardcoded one")
    assert type(excinfo.value) is RuntimeError
