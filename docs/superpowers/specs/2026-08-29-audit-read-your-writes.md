# Audit Read-Your-Writes

**Date:** 2026-08-29
**Status:** Design approved, awaiting implementation
**Tracking:** #218. Undermines the guarantees fixed by #181 (an export
failure must reach the report) and #146 (a `PRIVATE` loss must grade
`REVIEW_REQUIRED`).
**Base:** `main` at `258331c`
**Measured on:** `/Users/kevin/Developer/Isocenter/.venv/bin/python`
(3.14.6), file-backed `SqliteStore`, macOS.

---

## Context

`SqliteStore` writes its audit log asynchronously. `log_audit()`
(`persistence.py:660`) puts a tuple on `self.audit_queue`; `_audit_worker`
(`persistence.py:603`) `get()`s tuples into a **local `batch` list** and
only afterwards calls `log_audit_batch(batch)` (`:625`). Between the
`get()` and the `INSERT` a row is in **neither the queue nor the
database**, and it is owned by a local variable no other thread can see.

Three methods read that table. Two of them "settle" the log by calling
`flush_audit_queue()` (`:648`), which is a **queue drain and nothing
else** — it cannot see the worker's local batch. Those two readers are
racy. The third settles it by calling `stop()`, which joins the worker
and so runs the worker's post-loop flush; that one usually works, and §1.3
shows the case where it does not.

The consequence is not a missing log line. It is a compliance report that
grades `PASS` a run that dropped a private tag and failed a write (§1.4,
measured).

This spec replaces the pretence of a barrier with a real one: a lock that
the worker holds across *dequeue-and-write*, and that the readers take
before they `SELECT`. It has no timeout, no thread join, no thread
restart, and no dependence on the GIL.

---

## 1. Reproduction — what actually happens today

All figures below were taken on `258331c` with the venv named above.
The amplification is a **test-side wrap of `log_audit_batch`**; no shipped
code is modified to produce any of these numbers.

```python
orig = store.log_audit_batch
entered = threading.Event()

def slow(entries):
    entered.set()
    time.sleep(D)          # the row is provably in flight for D seconds
    return orig(entries)

store.log_audit_batch = slow
store.log_audit("ERROR", "uid", "boom")
entered.wait(3.0)          # <- causal, not a sleep: the batch write has begun
rows = store.get_audit_errors()
```

### 1.1 The two racy readers

| reader | settling call | rows returned with one row in flight | wall time |
| --- | --- | --- | --- |
| `get_audit_errors()` (`:706`) | `flush_audit_queue()` | **0 of 1**, 20 of 20 runs | 0.00 s |
| `get_audit_losses()` (`:726`) | `flush_audit_queue()` | **0 of 1**, 5 of 5 runs | 0.00 s |
| `get_audit_summary()` (`:678`) | `stop()` | 1 of 1, 5 of 5 runs | 0.51 s (D = 0.5) |

The 0.00 s is the whole defect in one column: the racy readers do not wait
for anything. `flush_audit_queue()` finds an empty queue, writes nothing,
and returns, and the `SELECT` that follows sees a table the row has not
reached yet.

`get_audit_losses()` is the more serious of the two. It is what
`generate_report` grades on: `row[3] == LOSS_SCOPE_PRIVATE` takes
`validation_status` to `REVIEW_REQUIRED` (`session.py:1565-1586`). A
missed row does not merely omit a line — it awards a passing grade.

### 1.2 There is no `get_audit_log()`

The issue asks that "`get_audit_log()` and anything else reading the audit
table" be surveyed in the same change. **No such method exists.** Grepping
`audit_log` across `isocenter/` returns the three readers above,
`log_audit_batch`'s `INSERT` (`:805`), the schema (`:365`), the index
(`:395`) and the `loss_scope` migration (`:540-543`). The survey is
complete at three readers; nothing else in the package selects from
`audit_log`.

### 1.3 `get_audit_summary()` is safe *only* while the join succeeds

`stop()` (`:641`) joins with `join(timeout=2.0)`. With the batch write
amplified to D = 3.0 s — longer than the join — the measured behaviour is:

```
summary: {}                                  # the recorded ERROR row is simply not there
elapsed: 2.01 s                              # the join gave up
live AuditWorker threads: 2                  # t+1s … t+6s: still 2, permanently
```

Two separate failures, and they must not be conflated:

1. **The guarantee is lost silently.** The join times out, the `finally`
   block reads the table anyway, and the caller gets `{}` for a store that
   has an `ERROR` row recorded. No exception, no log line.
2. **A worker thread leaks, permanently.** `get_audit_summary()`'s
   `finally` (`:699-704`) does `self._stop_event.clear()` and starts a
   *new* thread. The old worker — still alive, because the join timed out
   — re-checks a stop event that has just been cleared, so it never
   exits. Two `AuditWorker` threads now drain the same queue. Measured
   still 2 at t+6s. Every timed-out read adds one.

So the survey in #218's comment needs a qualifier: `get_audit_summary()`
is safe *when the in-flight batch write completes inside two seconds*, and
that is a property of the machine, not of the code. It is not a barrier
either; it is a race with a two-second head start.

### 1.4 `generate_report()` is correct today by accident of line order

```python
audit_summary = self.store_backend.get_audit_summary()   # session.py:1497
exceptions    = self.store_backend.get_audit_errors()    # session.py:1498
data_losses   = self.store_backend.get_audit_losses()    # session.py:1499
```

Line 1497 joins the worker and restarts it, so by 1498 the queue is
drained and the restarted worker's batch is empty. The two racy reads are
protected by a side effect of the call above them, which nothing states
and nothing enforces.

Measured, on `258331c`, with `get_audit_summary` replaced by
`lambda: {"EXPORT": 1}` — standing in for "someone reordered or deleted
that line" — and one `DATA_LOSS`/`PRIVATE` row plus one `ERROR` row in
flight:

```
| **Validation Status** | **PASS** |
mentions the loss:  False
mentions the error: False
```

A run that dropped a private element and failed a write, graded `PASS`,
with neither fact anywhere in the report. That is exactly the outcome
#181 and #146 were fixed to prevent.

**The stand-in must return a non-empty dict.** The grade is
`"PASS" if audit_summary and not exceptions and not graded_losses else
"REVIEW_REQUIRED"` (`session.py:1583-1586`), so patching the summary to
`{}` produces `REVIEW_REQUIRED` for the wrong reason and proves nothing.
§7 depends on this.

### 1.5 Where the exposure is live today

`generate_report()` is protected by the accident. The exposure that bites
is the direct API call — `store_backend.get_audit_errors()` /
`get_audit_losses()` with no summary read in front of it, which is what
`tests/test_check_reversibility.py:253` does, and why that test, not the
report, is what went red on the 3.12 CI leg.

### 1.6 Two construction paths, both start a worker

`SqliteStore.__init__` (`:400`) starts an `AuditWorker` at `:431-435`.
`__setstate__` (`:448`) — the unpickling path — starts another at
`:462-466`. `__getstate__` (`:437`) drops five keys so the store can
pickle at all. There is one `__init__`; the second block the issue comment
points at is `__setstate__`. **Both are construction, both start a worker,
and §3.6 applies to both.**

---

## 2. The decision: fix the primitive, not the call sites

Three shapes were considered.

**(a) Give each racy reader the `stop()`/restart treatment.** Rejected.
It spreads the §1.3 failure mode to two more methods: every read joins a
thread, spawns a thread, can time out, and leaks a worker when it does. A
caller polling `get_audit_losses()` in a loop would churn one thread per
iteration. It also cannot be made concurrency-safe — two threads reading
at once race on `self._audit_thread`.

**(b) Add a new, separate barrier method the readers call.** Rejected on
convention. `flush_audit_queue()` would keep its name while meaning "drain
only", and the codebase would carry two spellings for one job. CLAUDE.md:
one spelling per behaviour; pre-1.0 we rename or delete, we do not add a
second door.

**(c) Make `flush_audit_queue()` a real barrier.** **Chosen.** The name
already promises the guarantee — "flush the audit queue" is what a reader
believes it does — and the method is what every caller already reaches
for: `stop()` (`:646`), both racy readers, and four test sites
(`test_reporting.py:37,82`, `test_data_loss_reporting.py:72`,
`test_murmur_annotations.py:792`), all of which are today relying on a
guarantee they do not get. Strengthening the primitive fixes all of them
at once, adds no name, and deletes one.

The barrier is a **lock the worker holds across dequeue-and-write**, so
that the worker never owns a row outside it. It is not a join, not a
sentinel, and not a timeout. §3 specifies it.

---

## 3. The design

### 3.1 The invariant

> **No audit row is ever owned by anything other than the queue or the
> database, except by a thread holding `_audit_write_lock`.**

Everything else follows. A thread that holds `_audit_write_lock` and finds
the queue empty knows that every row enqueued before it took the lock is
in the database, because there is nowhere else for a row to be.

### 3.2 The lock

A plain `threading.Lock` on the store, created before the worker thread
starts:

```python
self._audit_write_lock = threading.Lock()
```

It is not an `RLock`, deliberately (§3.7 clause 3).

### 3.3 The drain-and-write helper

One private method is the *only* place rows leave the queue:

```python
def _drain_and_write(self):
    """Move every currently queued row into the database.

    The lock is what makes `flush_audit_queue` a barrier rather than a
    hopeful drain (#218): rows leave the queue only under it, and are
    in the database before it is released, so a row is never owned by
    a local variable a reader cannot see.
    """
    while True:
        with self._audit_write_lock:
            batch = []
            while len(batch) < 100:
                try:
                    batch.append(self.audit_queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                self.log_audit_batch(batch)
        if len(batch) < 100:
            return
```

The 100-row cap is the existing batch size, preserved. The outer loop
exists only so a backlog larger than 100 is not left half-written; it
terminates the first time the queue is momentarily empty.

### 3.4 The worker

The worker no longer takes rows off the queue itself. It waits for work
**owning nothing**, then delegates:

```python
def _audit_worker(self):
    while not self._stop_event.is_set():
        self._audit_wakeup.wait(timeout=1.0)
        self._audit_wakeup.clear()
        try:
            self._drain_and_write()
        except Exception as e:              # pylint: disable=broad-except
            self.logger.error(f"Audit Worker Error: {e}")
    self._drain_and_write()
```

`self._audit_wakeup` is a `threading.Event` set by `log_audit()` after the
`put`. The 1.0 s timeout is the existing `get(timeout=1.0)` cadence, kept
as a backstop.

**Why `get(timeout=1.0)` cannot be kept.** It is the defect. `get()`
returns a row into a local name *before* any lock can be taken, so a
barrier can still slip into the gap between the return and the
acquisition. The window shrinks; it does not close. The worker must block
on something that is not the queue.

**Why the wakeup Event cannot lose a row.** Order is `wait()` → `clear()`
→ drain. A `put` + `set` that lands between `wait()` returning and
`clear()` has its `set` erased, but the row is in the queue *before* the
`clear`, so the drain that follows takes it. A `put` + `set` after the
`clear` leaves the Event set, so the next `wait()` returns at once.
Neither ordering loses a row.

**And the stronger fact, which is the design's best review property: the
barrier's correctness does not depend on the Event at all.** Break the
wakeup mechanism entirely and `flush_audit_queue()` still returns only
after every queued row is in the database, because it drains under the
lock itself. A broken Event costs background-write *latency*, never
read-your-writes. Reviewers should check the Event for liveness and the
lock for correctness, and not confuse the two.

### 3.5 The barrier, and the three readers

```python
def flush_audit_queue(self):
    """Settle the audit log: return only when every row enqueued
    before this call is readable from `audit_log`.

    This is a barrier, not a poll. Before #218 it drained the queue and
    returned, which said nothing about rows the worker had already
    taken out of the queue and not yet written -- and a compliance
    report read through it graded PASS a run that dropped a private
    tag.
    """
    self._drain_and_write()
```

All three readers become the same shape, and each is safe on its own:

1. `get_audit_errors()` — unchanged body; its existing
   `flush_audit_queue()` is now a barrier.
2. `get_audit_losses()` — likewise.
3. `get_audit_summary()` — **`self.stop()` is replaced by
   `self.flush_audit_queue()`, and the entire `try/finally` restart block
   (`:699-704`) is deleted, not emptied.** The `except
   sqlite3.OperationalError: return {}` arm is preserved verbatim.

Clause 3 is how §1.4's accidental correctness is removed **structurally**
rather than documented. After it, `generate_report()` does not depend on
line order because there is no longer a line with a settling side effect
to depend on: the mechanism that made the dependence possible is gone
from the code. No comment is added at `session.py:1497-1499`; a comment
there would be a second home for the invariant, and CLAUDE.md is explicit
about what happens to those.

Deleting the restart also removes the §1.3 thread leak at its source. The
leak is fixed by deleting the restart, **not** by improving the join —
keep those two facts apart when reviewing.

### 3.6 Pickling — the highest-consequence clause

`_audit_write_lock` and `_audit_wakeup` are both unpicklable
(`TypeError: cannot pickle '_thread.lock' object`, measured).
`__getstate__`/`__setstate__` exist precisely so a `SqliteStore` can cross
a process boundary, and the store pickles cleanly on `258331c` today.

**Corrected at review (2026-08-31). The paragraph this replaces claimed
"nothing in the suite pins that, and no production path was found that
sends the store to a worker." Both halves are false**, and the survey
that produced them was scoped to the wrong three test files
(`test_persistence_concurrency.py`, `test_concurrency_stress.py`,
`test_multiprocessing.py` — none of which does mention pickling).

`redact_pixels()` sends the store across a process boundary on every
run. The per-instance callable is a **bound method** of
`RedactionService` (`services.py:_process_single_instance`), so `self`
— and with it `self.store_backend` — is pickled into every worker
submitted to `Session._executor` (`session.py:572`). Measured: deleting
the two `keys_to_remove` entries turns **eight** existing tests red with
`TypeError: cannot pickle '_thread.lock' object`, raised from
`multiprocessing.reduction._ForkingPickler.dumps` on a
`concurrent.futures.process._CallItem`:

```
tests/test_redaction_parallel.py            (2)
tests/test_redact_reports_outcome.py        (4)
tests/test_redaction_wildcard.py            (1)
tests/test_session.py::test_execute_config_integration
```

T4 is therefore the *direct* pin on §3.6, not the only one. It is still
worth having — it fails on the same mutation and names the reason — but
the change was never one `keys_to_remove` entry away from shipping
silently broken. Clauses 1–3 below are unchanged and still correct.

1. Create **both** in `__init__` **before** `self._audit_thread.start()`
   (`:431-435`).
2. Create **both** in `__setstate__` **before** `self._audit_thread.start()`
   (`:462-466`).
3. Add **both** to `__getstate__`'s `keys_to_remove` (`:440-445`).

Miss (3) and every pickle of a `SqliteStore` raises. Miss (1) or (2) and
the freshly started worker touches an attribute that does not exist yet.
Measured: the baseline store pickle-round-trips today and its
round-tripped copy logs and reads correctly; a subclass that adds the two
primitives without extending `keys_to_remove` fails with the `TypeError`
above.

### 3.7 Concurrency correctness

**No deadlock.**

1. `_audit_write_lock` is never held across a blocking wait. It is held
   across `get_nowait()` (non-blocking) and one `log_audit_batch()`
   (bounded — one `executemany` of at most 100 rows plus a commit).
2. Lock order is **`_audit_write_lock` → `_memory_lock`**, always, and
   never the reverse. `log_audit_batch` reaches `_get_connection`, which
   takes `_memory_lock` on a `:memory:` store (`:478`). The reverse
   order would deadlock, so: **`flush_audit_queue()` must be called
   before `_get_connection()` is entered, never inside it.** All three
   readers already flush first; the rewritten `get_audit_summary()` must
   keep the flush *above* the `with self._get_connection()` block. This is
   a positional requirement, not just a prohibition.
3. **`log_audit_batch()` must never acquire `_audit_write_lock`.** The
   worker calls it *while holding* the lock, and `threading.Lock` is not
   reentrant, so a defensive acquire self-deadlocks on the first row. The
   obvious hardening move is the fatal one. `remediation.py:84` calls
   `log_audit_batch` directly, off the queue and off the lock; that stays
   as it is, and it is already read-your-writes because it is synchronous.
4. `log_audit()` takes **neither** lock. Producers `put` and `set`, and
   are never blocked by a database write. That is what the current
   architecture buys and this design must not spend.

**Safe from multiple threads.** Concurrent barriers serialise on
`_audit_write_lock`; whichever arrives second finds an empty queue and
returns. Measured: 8 threads reading concurrently against a 500-row
backlog each returned exactly 500 rows.

**Safe with no worker running.** The barrier waits on a lock, never on a
thread's liveness, so it behaves identically whether the worker is
running, stopped, or was never started. Called after `stop()` it takes an
uncontended lock and writes the rows synchronously on the caller's
thread — measured, both before and after the change. This is what makes
§3.5 clause 3 safe: `get_audit_summary()` no longer restarts anything, so
there is no longer an ordering question about when it does.

**No GIL dependence.** Every clause above rests on `threading.Lock`,
`threading.Event` and `queue.Queue`, all of which are internally locked.
Nothing here reads or writes a shared list, counter, or flag outside a
lock, so it is correct on 3.14t. **The implementation must not introduce
one** — an "is the worker idle" boolean or an in-flight counter read
without the lock would be exactly the clause whose correctness depends on
GIL atomicity, and it is not needed: the lock already answers the
question.

### 3.8 No timeout on the barrier, and what that costs

The barrier has **no timeout**, deliberately. The only timeout in this
subsystem today is `stop()`'s `join(timeout=2.0)`, and §1.3 measured what
it does when it fires: it returns `{}` for a store with a recorded `ERROR`
row, silently, and leaks a thread. A bounded barrier would reintroduce
precisely that failure mode — a compliance read that quietly gives up is
worse than one that takes an extra second, because the caller cannot tell
the difference between "no errors" and "I stopped waiting".

The cost is real and a reviewer will find it: **the barrier's worst case
is one `log_audit_batch`,** and `_get_connection` opens file-backed
databases with `sqlite3.connect(..., timeout=900.0)` (`:491`), so a
pathologically contended database could hold a reader for a long time.
Two things about that. It is **inherited, not introduced** —
`get_audit_errors()` already calls `log_audit_batch` inline on the
caller's thread whenever the queue is non-empty, so the same 900 s is
reachable on `258331c`. And it is bounded by *one* batch: producers never
hold the lock, so no amount of logging traffic can extend a single
acquisition.

**The sharpest form of the objection, and the honest answer.** On a
`:memory:` store the chain is longer: the worker holds
`_audit_write_lock` while `log_audit_batch` waits on `_memory_lock`
(`:478`), and `PersistenceManager`'s save thread can hold `_memory_lock`
across a large save. A reader's barrier can therefore block behind *the
worker blocked behind a save*, where on `258331c` the same reader found
an empty queue and returned in 0.00 s. The kind of exposure is inherited;
the **probability** is not, because after this change a reader waits
whenever the worker owns rows, not only when the queue happens to be
non-empty. That is the price of the guarantee, and it is the right
trade: a compliance read that takes a second is recoverable, and one
that reports `PASS` on a private-tag loss is not. If it ever becomes a
practical problem, the fix is to shrink what `log_audit_batch` holds —
not to bound the barrier (§3.8's first paragraph).

`stop()` keeps its `join(timeout=2.0)` — a shutdown concern, not a read
concern. It should additionally `self._audit_wakeup.set()` after
`self._stop_event.set()` so the worker wakes immediately instead of
waiting out its 1.0 s tick, which makes the join far less likely to fire
at all. And after this change a timed-out join **no longer loses rows**:
`stop()`'s subsequent `flush_audit_queue()` waits out the in-flight write
and drains the queue, so everything enqueued before `stop()` reaches the
database either way. The surviving daemon worker sees `_stop_event` still
set — nothing clears it any more — and exits after its current iteration.

### 3.9 Deliberate behaviour changes

Both follow from §3.5 clause 3, both are improvements, and both must be
named in the PR body rather than discovered:

1. **`generate_report()` no longer stops and restarts the audit worker.**
   Today it leaves a *different* thread running than the one it found.
   After this, it leaves the same one.
2. **`get_audit_summary()` no longer resurrects a stopped worker.** Today
   its `finally` clears `_stop_event` and starts a thread, so calling it
   after `close()` silently brings the subsystem back up. After this it
   reads through the barrier and starts nothing. No test in the suite
   calls `generate_report()` or `get_audit_summary()` after `close()`
   (verified), so nothing depends on the resurrection — but it is a
   behaviour change, not a refactor.

---

## 4. Files touched

| file | change |
| --- | --- |
| `isocenter/persistence.py` | `_audit_write_lock` + `_audit_wakeup` created in `__init__` and `__setstate__`, both added to `__getstate__` (§3.6); **`log_audit()` sets `_audit_wakeup` after the `put`** (§3.4); new `_drain_and_write` (§3.3); `_audit_worker` rewritten (§3.4); `flush_audit_queue` delegates to it (§3.5); `get_audit_summary` loses `stop()`/restart (§3.5 clause 3); `stop()` sets the wakeup (§3.8) |
| `tests/test_audit_read_barrier.py` | new; §7 |
| `CHANGELOG.md` | entry naming the exact wrong outcome (a `PASS` grade on a run that dropped a private tag), per the project's convention that breaking/behavioural entries carry the reasoning |

The `log_audit()` row is bolded because it is the one a coder working
down this table will skip — it is a one-line addition to a method the
rest of the design never mentions. Skipping it is a **latency** bug, not
a correctness bug: the Event is never set, the worker drains only on its
1.0 s tick, and read-your-writes still holds because the barrier drains
under the lock itself (§3.4's last paragraph). Fix it anyway; a
one-second lag on every audit write is not the shape this subsystem is
supposed to have.

`session.py` is **not** touched. Nothing is added at `1497-1499`; the
dependence is removed by making the readers independent, not by annotating
the order.

---

## 5. Worked cases

| case | before | after |
| --- | --- | --- |
| row in the queue, worker idle | drain writes it; read sees it | same |
| row in the worker's local batch | **read misses it** | barrier waits out the write; read sees it |
| row mid-`executemany` | **read misses it** | barrier blocks on the lock until the commit |
| batch write exceeds 2 s, `get_audit_summary()` | returns `{}`; leaks a worker | blocks until the write commits; returns the row; leaks nothing |
| reader called after `stop()` | drain writes it on the caller's thread | same, under the lock |
| eight readers at once, 500 queued | each drains part of the backlog; all eventually correct | serialised; each returns 500 (measured) |
| store crosses a process boundary | pickles | pickles, iff §3.6 is honoured |
| `remediation.py:84`'s direct `log_audit_batch` | synchronous, already visible | unchanged, and must not take the lock (§3.7 clause 3) |

---

## 6. Explicitly out of scope

- **Stabilising `tests/test_check_reversibility.py`.** The test is
  correct, its assertion (`errors == 1`) is right, and the write it
  exercises genuinely fails as intended — the CI log carries the expected
  `OSError` from the truncation fixture. It goes green as a *consequence*
  of fixing the reader and for no other reason. **Adding a retry, a poll,
  a `sleep`, or a `flush` to that test is forbidden by this spec.** It
  would green the required gate and leave the public API still
  under-reporting to every real caller — the failure #218 exists to fix
  would survive, invisible, with the one thing that was pointing at it
  removed. The implementation must demonstrate that test green with an
  **empty diff on that file**.
- **A run/session identifier on audit rows** (#166, #196, #153). See §8.
- **`ORDER BY timestamp` resolution.** `log_audit_batch` stamps
  `timestamp = datetime.now().isoformat()` once per *batch*, at write
  time, and every row in the batch shares it (`:798-800`). Ordering is
  therefore already coarse and enqueue-time attribution is not
  recoverable. Pre-existing; this design neither helps nor hinders it, and
  nobody should later read the barrier as having settled it.
- **`stop()`'s `join(timeout=2.0)` as a shutdown mechanism.** It stays.
  §3.8 explains why it is no longer load-bearing for correctness.
- **Widening what `get_audit_errors()` selects, or folding `DATA_LOSS`
  into it.** Settled by #146 and unchanged here.
- **Making `log_audit()` synchronous.** It would fix the race by deleting
  the subsystem, and would put a sqlite write on the export loop's
  critical path.

---

## 7. Tests the implementation must satisfy

New file `tests/test_audit_read_barrier.py`. Every test constructs its own
store or session; none may reuse one another's, because a shared store
would recreate §1.4's accidental correctness *inside the test file*.

### T1 — the deterministic one. Lead with it.

**`flush_audit_queue()` blocks while `_audit_write_lock` is held, and
returns the rows once it is released.**

The test acquires `store._audit_write_lock` itself, starts a thread
calling `get_audit_errors()`, and asserts **both** halves:

```python
store.log_audit("ERROR", "u", "boom")
store._audit_write_lock.acquire()
result = []
t = threading.Thread(target=lambda: result.append(store.get_audit_errors()))
t.start()
t.join(timeout=0.5)
assert t.is_alive(), "the reader did not wait on the audit write lock"
store._audit_write_lock.release()
t.join(timeout=10)
assert not t.is_alive()
assert len(result[0]) == 1
```

This has **no timing dependence in the failure direction**. Delete the
barrier and nothing can block the reader, so it finishes and `is_alive()`
is `False` — the assertion fails deterministically. The 0.5 s only bounds
the false-negative side. Asserting both halves is required: "it blocked"
alone would pass against a barrier that blocks and then returns nothing.

Coupling to the private attribute name is intentional. Renaming the lock
without preserving the guarantee breaks this test loudly, which is the
point.

### T2 — the amplified probe, parametrized over all three readers

`@pytest.mark.parametrize` over `get_audit_errors`, `get_audit_losses`,
`get_audit_summary`, **fresh store per case, and no other reader called in
the case**. That parametrization is the mechanical enforcement of "each
read is safe independently"; a shared-store version would rebuild the
accidental correctness it exists to kill.

Body is §1's wrap: `log_audit_batch` replaced by one that sets an Event,
sleeps `D = 1.0`, then calls the original. The test waits on the Event —
causal, not a sleep — so the row is *provably* in flight, then asserts the
reader sees it. Restore the original attribute in a `finally`.

Honest about its nature: T2 is **margin-based**, not deterministic. The
margin is a 1.0 s in-flight window against a drain-and-`SELECT` measured
at 0.00 s — roughly three orders of magnitude, observed 20/20 and 5/5 on
`258331c`. Do not present T1 and T2 as the same kind of evidence in the
PR body.

### T3 — the report grades correctly with the summary call neutralised

The test for §1.4. With `store_backend.get_audit_summary` patched to
`lambda: {"EXPORT": 1}` — a **non-empty** dict, per §1.4's last paragraph
— and one `DATA_LOSS`/`LOSS_SCOPE_PRIVATE` row plus one `ERROR` row in
flight under T2's wrap, `session.generate_report(path)` must produce a
report that:

- grades **`REVIEW_REQUIRED`**;
- contains the loss detail text;
- contains the error detail text.

Measured on `258331c`: `PASS`, and neither text present. This is the test
that fails if someone reorders or removes `session.py:1497` — not by
asserting the order, which the fix makes irrelevant, but by asserting the
two reads below it are correct **without** it. That is the property worth
pinning; the line order is not.

### T4 — the pickle round-trip

`pickle.loads(pickle.dumps(store))` succeeds, and the round-tripped
store's `log_audit()` → `get_audit_errors()` returns the row. Pins §3.6.
Passes on `258331c` (the baseline pickles today) and fails against an
implementation that adds the two primitives without extending
`__getstate__` — verified both ways.

### T5 — no worker restart, no thread leak

With the batch write amplified past `stop()`'s 2.0 s join (`D = 3.0`),
`get_audit_summary()` must return the row, and the count of live threads
named `AuditWorker` must be unchanged before and after the call.
Measured on `258331c`: returns `{}`, and the count goes 1 → 2 and stays.

### T6 — concurrent readers

Eight threads calling `get_audit_errors()` against a 500-row backlog each
return 500 rows. Pins §3.7's multi-thread clause. Deterministic (it
asserts on counts, not on timing) and cheap.

### Must keep passing

- `tests/test_reporting.py`, `tests/test_data_loss_reporting.py`,
  `tests/test_export_failure_audit.py`, `tests/test_export_loss_audit.py`,
  `tests/test_murmur_annotations.py` — every existing `flush_audit_queue()`
  caller, now getting a stronger guarantee than it asked for.
- `tests/test_dataframe_export.py` — its `store.stop()` ahead of the
  connection counter exists so the audit worker's own `_get_connection`
  calls are not blamed on the paged walk. That reasoning is unaffected:
  the barrier still opens a connection only when it has rows to write,
  and after `stop()` there are none.
- `tests/test_api_coherence.py::…close…` — `close()` still runs
  `store_backend.stop()`.
- `tests/test_check_reversibility.py` — green, with an empty diff (§6).
- `tests/test_persistence_concurrency.py`, `tests/test_concurrency_stress.py`
  — they exercise the worker under load. They do **not** cover the pickle
  path: none of them mentions `pickle`, `run_parallel`, `ProcessPool` or
  `multiprocessing` (checked). The pickle path *is* covered, but by the
  redaction tests, via `redact_pixels()`'s process isolation — see the
  correction in §3.6.
- Full suite on **3.12 and 3.14t**. A local 3.14.6 pass exercises one of
  the two gate versions and neither of them exactly; 3.14t is the one that
  matters for §3.7's no-GIL clause and it must be run, not reasoned about.

### Non-vacuity, with polarities

| polarity | tests | fails on `258331c`? |
| --- | --- | --- |
| reproduces the defect this change fixes | T2 (`get_audit_errors`, `get_audit_losses`), T3, T5, T6, the `:memory:` case | **yes** — measured |
| pins the new mechanism; the behaviour did not exist before | T1 | **yes**, with `AttributeError: _audit_write_lock` — there is no lock to hold |
| guards behaviour that is already correct | T2 (`get_audit_summary`), T4 | **no** — and the PR body must say so, or a reviewer scanning for "red before, green after" will read them as broken |

Row 2 held only T1 as written. **T6 belongs in row 1, and its mechanism
is not "no lock to hold"** (corrected at review, 2026-08-31). Pre-fix,
`flush_audit_queue` was an unsynchronised drain, so eight concurrent
readers *split* one 500-row backlog between them and each `SELECT`ed
before the others had committed their share. Measured on `258331c`:
15 runs, 15 red. That is a reproduction of the defect, not a pin on a
mechanism that did not exist — the same reader race the whole spec is
about, seen from the concurrent side rather than the in-flight side.

For every test in the first row, a version that passes on `258331c` is
testing something else. The implementation must run each of T1–T6 against
`258331c` and record the observed failure mode in the PR body.

Additionally, the coder should verify the barrier is not vacuous by
deleting `flush_audit_queue()`'s body (making it `pass`) and confirming
T1, T2 and T3 all go red.

---

## 8. Future work — the run identifier (#166, #196, #153)

Those issues need a notion of *which export run* an audit row belongs to;
the log is cumulative across reopens and carries no run or session id.
**This design is orthogonal to that and does not solve it.**

It also does not make it harder. The barrier changes *when* a row becomes
visible, not what a row contains: no column is added or removed, no
reader's `SELECT` list changes, and `log_audit`'s signature is untouched.
A run id would arrive as a column on `audit_log`, a parameter on
`log_audit`/`log_audit_batch`, and a `WHERE` clause on the readers — every
one of which sits outside anything this spec touches.

One point of contact worth recording so it is not rediscovered as a
regression: a run-scoped reader will want the barrier *more*, not less. If
`get_audit_losses(run_id=…)` filters to the current run, a missed
in-flight row is no longer diluted by history — it is the difference
between "this run dropped nothing" and the truth. Whoever implements #166
should keep the flush ahead of the `SELECT` (§3.7 clause 2).

The batch-shared write-time timestamp (§6) is the thing that would
actually complicate ordering within a run. It is pre-existing and
untouched here.

---

## 9. Found here, to be filed separately

Not folded in; listed for the maintainer to file or discard.

1. **`log_audit_batch` swallows `sqlite3.Error` into a log line**
   (`:808-809`). A compliance row that fails to insert produces a log
   message and no other trace, which is the same class of problem as this
   issue one layer down: the caller cannot tell a written row from a lost
   one. `io_handlers.py:1670-1676` already reasons about this when
   choosing `log_audit` over `log_audit_batch`.
2. **`_audit_worker`'s `except Exception` logs and continues** (`:628-631`).
   With §3.4 the failing call is `_drain_and_write`, whose `with`
   statement releases the lock on the way out, so a raising batch cannot
   wedge the barrier. Worth a test of its own; not required by this spec.
3. **`stop()` is not idempotent and not guarded against concurrent
   callers.** After §3.5 clause 3 nothing calls it on a read path, so the
   exposure drops to `close()`, which is already single-caller. Low
   priority, but the reason it was survivable before was the very restart
   this spec deletes.
