"""A PHI status and the policy it was recorded under are read as one pair
(#753).

A status is two fields, `_phi_status` and `_phi_status_policy`, valid
while `_phi_status_revision` equals `_revision`. The save thread reads
them for a row while a scan on another thread records a new status, and
nothing locks the four fields: what keeps a reader from pairing one
status with another status's policy is the **order** of three sequences.

- `TrackedEntity.record_phi_status` writes `_revision` first and
  `_phi_status_revision` last.
- `TrackedEntity._phi_status_record` reads `_phi_status_revision` first
  and `_revision` last.
- `persistence._status_columns` restates those four reads for the save.

A reader that overlaps a writer then sees the revisions disagree and
reports `(UNSCANNED, None)`. Swapping the reads in either reader, or
writing the status before the revision, left every test green (the review
of #750, and C5's spec, §3.1: three mutants, 105 tests each).

**How this tests an order with no thread.** `Watched` reports each read
and write of the four fields. The writes the live `record_phi_status`
makes are captured, in the order it makes them, and then replayed between
the live reader's own field reads, at every point: every way one read can
overlap one write (70 schedules) and two (495). Nothing here restates
either order, so a change to any of the three sequences changes what is
replayed or where.

**What it does not show.** That each field read is atomic. Each is one
attribute load, and nothing here runs on two threads; on the free-threaded
build that rests on the interpreter, not on this file.

**Why this file imports what it does.** `isocenter.entities` and
`isocenter.persistence`, so both modules' probe rows are charged.
"""
import itertools

import pytest

from isocenter import persistence
from isocenter.entities import PhiStatus, ScanPolicy, TrackedEntity

FIELDS = ("_revision", "_phi_status", "_phi_status_policy",
          "_phi_status_revision")
PA = ScanPolicy("v1:" + "a" * 64, "base-a")
PB = ScanPolicy("v1:" + "b" * 64, "base-b")
PC = ScanPolicy("v1:" + "c" * 64, "base-c")
A, B, C = PhiStatus.IDENTIFIED, PhiStatus.REMEDIATED, PhiStatus.CLEARED
NOT_NOW = (PhiStatus.UNSCANNED, None)


class Watched(TrackedEntity):
    """A tracked entity whose reads and writes of the four fields are seen.

    `TrackedEntity` is a slots dataclass; this subclass declares no
    `__slots__`, which leaves the four fields the parent's slots.
    """
    on_read = None
    on_write = None

    def __getattribute__(self, name):
        if name in FIELDS:
            hook = type(self).on_read
            if hook is not None:
                hook(self, name)
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        hook = type(self).on_write
        if hook is not None and name in FIELDS:
            hook(self, name, value)
        object.__setattr__(self, name, value)


def _at_a():
    entity = Watched()
    entity.record_phi_status(A, PA)
    assert entity._phi_status_record() == (A, PA)
    return entity


def captured(*records):
    """The field writes the live `record_phi_status` makes for each record,
    in the order it makes them, from an entity at `(A, PA)`."""
    entity = _at_a()
    writes = []
    Watched.on_write = lambda _self, name, value: writes.append((name, value))
    try:
        for status, policy in records:
            entity.record_phi_status(status, policy)
    finally:
        Watched.on_write = None
    return writes


def read_with(reader, writes, schedule):
    """`reader` over an entity at `(A, PA)`, with the first `schedule[k]`
    of `writes` applied before the reader's k-th field read.

    Returns:
        tuple: `(what the reader returned, the fields it read in order)`.
    """
    entity = _at_a()
    seen = []
    done = [0]

    def before_a_read(self, name):
        k = len(seen)
        seen.append(name)
        upto = schedule[k] if k < len(schedule) else len(writes)
        while done[0] < upto:
            field, value = writes[done[0]]
            object.__setattr__(self, field, value)
            done[0] += 1

    Watched.on_read = before_a_read
    try:
        result = reader(entity)
    finally:
        Watched.on_read = None
    return result, seen


def schedules(writes):
    """Every way four reads can fall among `writes` writes: for each read,
    how many writes came before it, never decreasing."""
    return list(itertools.combinations_with_replacement(range(writes + 1), 4))


def _the_pair_in(columns):
    """`_status_columns`' row as the pair it speaks for. The fourth column
    (the stale marker) is not part of the pair."""
    status, fingerprint, base, _edited = columns
    if status == PhiStatus.UNSCANNED.value:
        assert fingerprint is None and base is None
        return NOT_NOW
    assert (fingerprint is None) == (base is None)
    return (PhiStatus(status),
            ScanPolicy(fingerprint, base) if fingerprint else None)


READERS = {
    "_phi_status_record": lambda entity: entity._phi_status_record(),
    "_status_columns": lambda entity: _the_pair_in(
        persistence._status_columns(entity)),
}


def _results(reader, writes):
    out = {}
    for schedule in schedules(len(writes)):
        result, _seen = read_with(READERS[reader], writes, schedule)
        out[schedule] = result
    return out


def _torn(results, whole):
    return {schedule: (status.name, policy.base if policy else None)
            for schedule, (status, policy) in results.items()
            if (status, policy) not in whole}


@pytest.mark.parametrize("reader", sorted(READERS))
def test_no_interleaving_of_a_read_and_a_write_tears_the_pair(reader):
    """Kills: `_revision` read first in either reader; the status or the
    policy written before the revision is advanced."""
    writes = captured((B, PB))
    results = _results(reader, writes)
    assert len(results) == 70
    assert not _torn(results, {(A, PA), (B, PB), NOT_NOW}), (
        "writes applied before each of the reader's four field reads -> "
        "the pair it returned. A status beside another status's policy "
        "is what a save would write to the row.")


@pytest.mark.parametrize("reader", sorted(READERS))
def test_no_interleaving_with_two_writes_tears_the_pair(reader):
    """The same over two writes in a row, where a read can begin in one
    record and end in the next but one. Kills: the same three mutants, at
    more schedules each (51, 51 and 10 torn pairs in the spec's
    measurement against 8, 8 and 5)."""
    writes = captured((B, PB), (C, PC))
    results = _results(reader, writes)
    assert len(results) == 495
    assert not _torn(results, {(A, PA), (B, PB), (C, PC), NOT_NOW})


@pytest.mark.parametrize("reader", sorted(READERS))
def test_the_probe_sees_every_read_and_every_write(reader):
    """The two tests above pass over nothing if the harness goes blind: a
    reader that stops loading attributes (a cached tuple) is never
    interleaved, and one that always answers UNSCANNED is never torn."""
    writes = captured((B, PB))
    assert len(writes) == 4
    assert sorted(name for name, _value in writes) == sorted(FIELDS)
    # The values, so a replay is the write it stands for.
    assert dict(writes)["_phi_status"] is B
    assert dict(writes)["_phi_status_policy"] == PB
    assert dict(writes)["_revision"] == dict(writes)["_phi_status_revision"]

    for schedule in schedules(4):
        _result, seen = read_with(READERS[reader], writes, schedule)
        assert len(seen) == 4 and sorted(seen) == sorted(FIELDS), seen

    results = _results(reader, writes)
    assert set(results.values()) == {(A, PA), (B, PB), NOT_NOW}
    # Before any write the reader sees the old pair, after all the new one.
    assert results[(0, 0, 0, 0)] == (A, PA)
    assert results[(4, 4, 4, 4)] == (B, PB)

    two = captured((B, PB), (C, PC))
    assert len(two) == 8
    assert set(_results(reader, two).values()) == {
        (A, PA), (B, PB), (C, PC), NOT_NOW}
