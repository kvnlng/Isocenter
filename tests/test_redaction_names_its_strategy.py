"""`redact()` says which strategy it resolved, and says the one it used (#384).

#384 was filed against `Executing using {max_workers} workers (Process
Isolation)...`, a parenthetical that was false on the threads path. 0.9.4
removed it (#381), which converted a lie into a silence: measured across
seven store-and-lever combinations on 3.12.14 and 3.14.7t, **three
dispatch paths produced one identical sentence**, including the case the
issue was filed from -- a file-backed store with no levers set, which
runs in processes on 3.12 and in threads on 3.14t. The number of workers
was the only fact the line carried, and it is the fact a reader least
needs: under threads the worker mutates the live `Instance`, under
processes it mutates a pickled copy whose result the parent applies back.

The fix is not a second reading of the environment at the print site --
that would be a second implementation of the four-rank order in
`_resolve_execution_choice`, which is the defect class this milestone is
about. `redact()` resolves a `_Strategy` once, prints from it, logs from
it, and hands it to `run_parallel` as `strategy=`. The printed object
*is* the dispatched object, so there is no arrangement of the environment
under which the line and the pool can disagree.

**Whole lines, never substrings.** Measured: one real line of `redact()`
output -- #185's recycling warning -- contains the words `threads`,
`processes`, `ISOCENTER_FORCE_THREADS` and `ISOCENTER_MAX_TASKS_PER_CHILD`
together, so `assert "threads" in captured.out` passes on a run that
printed `(processes)` and the mirror passes on a run that printed
`(threads)`. Every banner assertion here is whole-line membership in
`out.splitlines()`, plus the negative on the opposite parenthetical.

**The expected line is a literal.** The autouse fixture pins
`ISOCENTER_MAX_WORKERS=3`, so the expectation is the string
`Executing using 3 workers (threads)...` rather than an f-string built
from `_redaction_worker_count()`. Building the expectation out of the
production helper would keep these tests green if that helper broke, and
would make them depend on the CPU count of whoever runs them.

The fixture's pixels are **non-zero** and the image is larger than the
zone, for `tests/test_memory_store_redaction_strategy.py`'s reasons: a
zero image redacted to zero is green with the fix reverted.
"""
import logging
import sys
import sysconfig
from datetime import date

import numpy as np
import pytest

from isocenter import parallel
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.session import DicomSession

CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
SERIAL = "SN_PROBE"
FILL = 1000
ZONE = [0, 10, 0, 10]  # [y1, y2, x1, x2]

THREADS_LINE = "Executing using 3 workers (threads)..."
PROCESSES_LINE = "Executing using 3 workers (processes)..."


@pytest.fixture(autouse=True)
def three_workers_and_no_levers(monkeypatch):
    """A pinned worker count, and no lever this file did not set itself."""
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "3")
    for name in ("ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_MAX_TASKS_PER_CHILD"):
        monkeypatch.delenv(name, raising=False)


def _populate(session):
    """Three CT instances on one machine, 16x16, every pixel `FILL`."""
    patient = Patient("PAT1", "Probe^Patient")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    series = Series("SE_1", "CT", 1)
    # The redaction index keys on `series.equipment.device_serial_number`
    # (`RedactionIndex.index_store`), not on the (0018,1000) attribute.
    series.equipment = Equipment("Probe", "Model", SERIAL)
    for n in range(3):
        inst = Instance(f"1.2.826.0.1.{n}", CT_STORAGE, n + 1)
        inst.file_path = None
        inst.set_attr("0018,1000", SERIAL)
        inst.set_attr("0008,0060", "CT")
        inst.set_pixel_data(np.full((16, 16), FILL, dtype=np.uint16))
        series.instances.append(inst)
    study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.save(sync=True)
    session.configuration.rules = [
        {"serial_number": SERIAL, "redaction_zones": [ZONE]}]
    return session


def _assert_banner(captured, expected, forbidden):
    """The whole line is present, and the opposite parenthetical is not."""
    lines = captured.out.splitlines()
    assert expected in lines, (
        f"the strategy banner {expected!r} is not among the lines "
        f"redact() printed: {lines}")
    assert not [line for line in lines if forbidden in line], (
        f"a line naming {forbidden!r} was printed by a pass that ran the "
        f"other way: {lines}")


def test_a_memory_store_says_it_is_running_in_threads(capsys):
    """The `:memory:` arm, on every interpreter.

    `redact()` passes `force_threads=True` for a `:memory:` store, which
    sits at rank 2 and beats everything below it, so this arm is threads
    on 3.12.14 and 3.14.7t alike and the banner must say so on both.

    Killing edit: the strategy conditional replaced by the bare
    `Executing using {max_workers} workers...` of 0.9.4.
    """
    with DicomSession(":memory:") as session:
        _populate(session)
        assert session.redact() == 3

    _assert_banner(capsys.readouterr(), THREADS_LINE, "(processes)")


def test_a_file_store_says_it_is_running_in_processes(tmp_path, capsys,
                                                      monkeypatch):
    """The processes arm, selected structurally rather than by default.

    `ISOCENTER_FORCE_PROCESSES=1` rather than the interpreter's default,
    so this arm is processes on the free-threaded gate too -- where the
    default is threads and a test that relied on it would be green for
    the wrong reason. A file store, so #400's warning and refusal are
    both out of the picture and this test is about the banner only.

    Killing edit: the conditional inverted, or hardcoded to `(threads)`.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    with DicomSession(str(tmp_path / "file_store.db")) as session:
        _populate(session)
        assert session.redact() == 3

    _assert_banner(capsys.readouterr(), PROCESSES_LINE, "(threads)")


def test_the_recycling_pool_is_reported_as_processes(tmp_path, capsys,
                                                     monkeypatch):
    """The third dispatch path, which sets neither force lever.

    `multiprocessing.Pool` with `maxtasksperchild` is processes, and the
    banner must say so -- this is the arm that distinguishes a banner
    read off `strategy.use_threads` from one keyed on the two force
    levers, because the recycling arm sets neither of them.

    Killing edit: a banner keyed on `ISOCENTER_FORCE_THREADS` /
    `ISOCENTER_FORCE_PROCESSES` rather than on `strategy.use_threads`.
    """
    monkeypatch.setenv("ISOCENTER_MAX_TASKS_PER_CHILD", "2")
    with DicomSession(str(tmp_path / "file_store.db")) as session:
        _populate(session)
        assert session.redact() == 3

    _assert_banner(capsys.readouterr(), PROCESSES_LINE, "(threads)")


def test_the_banner_is_the_strategy_the_pool_was_built_from(
        tmp_path, capsys, monkeypatch):
    """The line and the pool are two readings of one object (#384).

    The patched ranking and the real environment are made to **disagree**:
    `ISOCENTER_FORCE_PROCESSES=1` is set, so the shipped ranking says
    processes on both gate interpreters, while the patched
    `_resolve_execution_choice` says threads. A banner that re-derived
    the choice at the print site -- from the environment, or from
    `sys._is_gil_enabled()` -- would print `(processes)` over a pool of
    threads. Only a banner that reads the resolved object survives.

    The disagreement is the whole test, which is why **both** the lever
    and the GIL reading are pinned against the patch. Measured, each is
    load-bearing on a different leg:

    - Without the `setenv`, an environment-derived banner survives on
      *both* interpreters -- there is no lever for it to read, so it
      falls through to the same answer the patch gives.
    - Without the `setattr`, a `sys._is_gil_enabled()`-derived banner is
      red on 3.12.14 and **green on 3.14.7t**, because there it says
      `threads` and agrees with the patched pool. That is the leg this
      test exists for, and this docstring claimed to cover it while
      nothing did.

    `raising=False` because `sys._is_gil_enabled` does not exist on the
    3.12 floor at all; the production ranking guards on `hasattr`, and
    creating the attribute here is harmless because the ranking itself is
    patched out.

    `parallel._resolve_execution_choice` is the patch target because
    `_resolve_strategy` looks it up as a module global at call time.
    `_resolve_strategy` itself is imported *into* `session.py` by name,
    so patching that one on the `parallel` module would not reach the
    session -- a mistake that produces a green test which exercised the
    unpatched code.
    """
    monkeypatch.setenv("ISOCENTER_FORCE_PROCESSES", "1")
    monkeypatch.setattr(sys, "_is_gil_enabled", lambda: True, raising=False)
    monkeypatch.setattr(
        parallel, "_resolve_execution_choice",
        lambda force_threads, maxtasksperchild, recycling_lever:
        parallel._Choice(True, None, None))

    dispatched = []
    real_executor_path = parallel._run_on_new_executor

    def spy(func, items, strategy):
        dispatched.append((strategy.desc, strategy.use_threads))
        return real_executor_path(func, items, strategy)

    monkeypatch.setattr(parallel, "_run_on_new_executor", spy)

    with DicomSession(str(tmp_path / "file_store.db")) as session:
        _populate(session)
        assert session.redact() == 3

    _assert_banner(capsys.readouterr(), THREADS_LINE, "(processes)")
    assert ("Redacting Pixels", True) in dispatched, (
        "the banner said threads but the pool was not built from a "
        f"threads strategy; the dispatch saw {dispatched}")


def test_the_log_line_names_the_strategy_too(caplog):
    """The INFO line beside the print carries the same field.

    Read off the same `_Strategy`, so it cannot drift from the banner.
    Whole formatted record, for the reason the banner assertions are
    whole lines.

    Killing edit: `strategy=` dropped from the log line.
    """
    with caplog.at_level(logging.INFO):
        with DicomSession(":memory:") as session:
            _populate(session)
            assert session.redact() == 3

    expected = ("Starting granular redaction (3 tasks, workers=3, "
                "strategy=threads)...")
    messages = [record.getMessage() for record in caplog.records]
    assert expected in messages, (
        f"the redaction INFO line does not name the strategy; expected "
        f"{expected!r} among {[m for m in messages if 'redaction' in m]}")


@pytest.mark.skipif(
    sysconfig.get_config_var("Py_GIL_DISABLED") != 1,
    reason="the free-threaded default is only reachable on a "
           "free-threading build")
def test_a_free_threaded_build_runs_a_file_store_in_threads_and_says_so(
        tmp_path, capsys):
    """The divergence #384 was filed from, on the leg that diverges.

    A file-backed store with no lever set runs in **processes** on 3.12
    and in **threads** on 3.14t. Before #384 both printed the same
    sentence; the sibling test above pins the 3.12 reading of this same
    configuration through a lever, and this one pins the free-threaded
    default itself.

    The skip is on `Py_GIL_DISABLED`, a **build** property an extension
    cannot change, and `sys._is_gil_enabled() is False` is a hard
    assertion inside -- after `import isocenter` has pulled numpy,
    pydicom and imagecodecs. An extension without free-threaded support
    re-enables the GIL silently, and a skip keyed on
    `sys._is_gil_enabled()` would turn that into a green gate that proved
    nothing. This way it turns red.

    Killing edit: the banner hardcoded to `(processes)`; and, separately,
    losing `parallel.py`'s `hasattr(sys, "_is_gil_enabled") and not
    sys._is_gil_enabled()` rank, which turns this arm into processes and
    reddens the banner honestly.
    """
    assert sys._is_gil_enabled() is False, (
        "this is a free-threading build whose GIL has been re-enabled -- "
        "an extension without free-threaded support did it at import "
        "time, and the gate's 3.14t leg is running GIL semantics")

    with DicomSession(str(tmp_path / "file_store.db")) as session:
        _populate(session)
        assert session.redact() == 3

    _assert_banner(capsys.readouterr(), THREADS_LINE, "(processes)")


# --------------------------------------------------------------------------
# What a `redact()` with nothing to do now says about its tuning values.
# --------------------------------------------------------------------------

#: The five parallel tuning variables `redact()` reads while resolving its
#: strategy, and whether a below-minimum value is reported on a pass that
#: matches no images. The three that speak go through `_env_int`, which
#: warns; the two that do not go through `_env_is`, which never does.
TUNING_VARIABLES = [
    ("ISOCENTER_MAX_WORKERS", True),
    ("ISOCENTER_CHUNKSIZE", True),
    ("ISOCENTER_MAX_TASKS_PER_CHILD", True),
    ("ISOCENTER_DISABLE_GC", False),
    ("ISOCENTER_SHOW_PROGRESS", False),
]


@pytest.mark.parametrize("variable,reported", TUNING_VARIABLES)
def test_a_malformed_tuning_value_is_reported_by_a_pass_with_no_work(
        variable, reported, monkeypatch, caplog):
    """The behaviour change #384 made to a `redact()` that matches nothing.

    Resolving the strategy before task preparation -- which the banner
    requires, since it has to name the strategy the pool was built from
    rather than guess at it -- moved three environment reads ahead of
    the no-match sentence (`No matching images found for any loaded
    rules.` then; `No image matched any loaded rule's serial_number.`
    since #807). At `f544989` none
    of the five variables below said anything on such a call; three of
    them now warn once each.

    The CHANGELOG states that as fact, so it is asserted here rather
    than left to a measurement in a commit message. Both directions are
    parametrized on purpose: the three that speak pin the change, and
    the two that stay silent pin its *boundary* -- `_env_is` has no
    warning to emit, and a future edit that gave it one would be a
    second behaviour change arriving unannounced under cover of this
    one.

    `0` is the malformed value for all five: it is below every minimum
    `_env_int` is called with here, and it is the value #341 and #335
    are both about.
    """
    monkeypatch.setenv(variable, "0")

    with caplog.at_level(logging.WARNING):
        with DicomSession(":memory:") as session:
            _populate(session)
            # The rule that matches nothing, which is the arm under
            # test: `redact()` returns 0 without preparing a task.
            session.configuration.rules = [
                {"serial_number": "NO_SUCH_SERIAL",
                 "redaction_zones": [ZONE]}]
            caplog.clear()
            assert session.redact() == 0, (
                "the rule matched something, so this is not the "
                "no-matching-images arm this test is about")

    naming = [record.getMessage() for record in caplog.records
              if record.levelname == "WARNING"
              and variable in record.getMessage()]

    if reported:
        assert len(naming) == 1, (
            f"{variable} is read through _env_int while the strategy is "
            f"resolved, which now happens before the no-matching-images "
            f"return, so a value of 0 must be reported exactly once on "
            f"this call; got {len(naming)}: {naming}")
        assert "below the minimum" in naming[0]
    else:
        assert naming == [], (
            f"{variable} is read through _env_is, which has no warning to "
            f"emit, so this call must stay silent about it; got {naming}")
