"""What `run_parallel` promises, across its three execution paths.

It dispatches to a shared executor, a recycling `multiprocessing.Pool`,
or a fresh executor, and each path had its own copy of the progress-bar
setup and its own reading of the environment. These tests state the
behaviour those copies were supposed to share, so the copies can be
removed.
"""
import logging
import os

import pytest

from isocenter import parallel


def identity(value):
    return value


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No test should inherit another's tuning variables."""
    for name in ("ISOCENTER_MAX_WORKERS", "ISOCENTER_CHUNKSIZE",
                 "ISOCENTER_MAX_TASKS_PER_CHILD", "ISOCENTER_DISABLE_GC",
                 "ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES",
                 "ISOCENTER_SHOW_PROGRESS"):
        monkeypatch.delenv(name, raising=False)


def test_a_malformed_tuning_variable_is_reported(monkeypatch, caplog):
    """A mistyped setting must not look like an applied one.

    Every environment read was wrapped in `except ValueError: pass`, so
    `ISOCENTER_MAX_WORKERS=banana` reverted to the default in silence.
    The symptom is a cohort that runs at the wrong width with nothing
    anywhere saying why.
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "banana")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    with caplog.at_level(logging.WARNING):
        assert parallel.run_parallel(
            identity, [1, 2, 3], show_progress=False) == [1, 2, 3]

    assert any("ISOCENTER_MAX_WORKERS" in record.message
               for record in caplog.records), (
        "the malformed value was ignored without a word")


def test_results_come_back_in_order_on_the_standard_path(monkeypatch):
    """`map` preserves input order; callers rely on it for zip-style joins."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    assert parallel.run_parallel(
        identity, list(range(20)), show_progress=False) == list(range(20))


def test_return_generator_defers_the_work(monkeypatch):
    """Streaming mode must not have run anything before it is consumed."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    seen = []

    def record(value):
        seen.append(value)
        return value

    result = parallel.run_parallel(
        record, [1, 2, 3], show_progress=False, return_generator=True)

    assert seen == [], "the generator ran before anything asked it to"
    assert list(result) == [1, 2, 3]
    # Sorted: workers finish in whatever order they finish. The contract is
    # that every item ran and results come back in input order, not that
    # the pool scheduled them in it.
    assert sorted(seen) == [1, 2, 3]


def test_an_empty_workload_is_not_an_error(monkeypatch):
    """Zero items is an ordinary outcome of a filter, not a failure."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    assert parallel.run_parallel(identity, [], show_progress=False) == []


def test_progress_can_be_switched_off_globally(monkeypatch):
    """ISOCENTER_SHOW_PROGRESS=0 silences a caller that asked for a bar."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_SHOW_PROGRESS", "0")

    drawn = []
    real_tqdm = parallel.tqdm
    monkeypatch.setattr(parallel, "tqdm",
                        lambda *a, **k: drawn.append(k) or real_tqdm(*a, **k))

    parallel.run_parallel(identity, [1, 2], show_progress=True)

    assert not drawn


def test_the_progress_bar_is_told_how_many_items_to_expect(monkeypatch):
    """A sized iterable needs no explicit total; a bar without one is useless."""
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")

    seen = {}
    real_tqdm = parallel.tqdm

    def capture(iterable, **kwargs):
        seen.update(kwargs)
        return real_tqdm(iterable, **kwargs)

    monkeypatch.setattr(parallel, "tqdm", capture)

    parallel.run_parallel(identity, [1, 2, 3, 4], show_progress=True)

    assert seen.get("total") == 4
