"""
Persistence manager for handling background save operations.
"""
import threading
import queue
import atexit
import weakref
from typing import List
from .entities import Patient
from .persistence import SqliteStore
from .logger import describe_exception, get_logger

#: How long `flush()` waits before saying what it is waiting for.
#:
#: `flush()` must not return until the queue is drained (see its
#: docstring), so the only thing left to tune is how long it stays
#: silent about it. This sits below pytest.ini's
#: `faulthandler_timeout = 300`, so a wedged flush explains itself
#: before any traceback dump arrives and the two land in the same log.
_FLUSH_REPORT_INTERVAL_S = 30.0

#: How long `shutdown()` waits for its worker to take the sentinel.
#:
#: Named rather than inline so tests can shorten it; there is no env
#: var, because one spelling per behaviour and nothing outside a test
#: has a reason to move it. A join that times out is not a failure --
#: it means a save is genuinely still running, which `shutdown()`
#: reports and leaves alone (#314).
_SHUTDOWN_JOIN_TIMEOUT_S = 30.0


def _flush_at_exit(manager_ref):
    """Flush a manager at interpreter exit, if it is still alive.

    Module-level and taking a weak reference, so registering it does not
    keep the manager (and its store, threads and file descriptors) alive
    for the life of the process. See the registration site.
    """
    manager = manager_ref()
    if manager is not None:
        manager.shutdown()


def _report_abandoned_saves(work_queue, held=None):
    """Say that a collected manager took unwritten saves with it.

    The worker holds the queue strongly so that this exit path can count
    what it drops once the manager is gone.

    **A log line and no audit row**, as in `_report_abandoned_audit_rows`:
    writing the row would need `manager.store_backend`, which is gone
    with the manager.

    **It drains rather than reading `qsize()`**: the queue also holds
    shutdown sentinels, and counting a `None` as a lost save would
    overstate the data loss. Draining is safe here and nowhere else: the
    manager is unreachable, so no `flush()` can be waiting on the count
    and no other consumer can be started.

    `held` is the item this worker had already taken off the deque when
    it found the manager gone, passed rather than pushed back so that
    the queue's unfinished count is not disturbed for a caller that
    cannot exist.
    """
    pending = 1 if held is not None else 0
    while True:
        try:
            item = work_queue.get_nowait()
        except queue.Empty:
            break
        if item is not None:
            pending += 1
    if pending:
        get_logger().warning(
            f"A PersistenceManager was collected with {pending} queued "
            f"save{'' if pending == 1 else 's'} that never reached the "
            f"store; they are lost. Call close() -- or use the session "
            f"as a context manager -- rather than dropping a session "
            f"mid-flight (#318).")


def _persistence_worker_loop(manager_ref, work_queue):
    """Background save worker that does not keep its manager alive.

    Module-level, taking a **weak** reference: a running `Thread` holds
    its target, and a bound-method target would hold `self`, keeping the
    manager alive for as long as the worker runs -- and with it its
    `SqliteStore`, that store's sqlite handles, its audit-writer thread
    and its sidecar descriptors. The `finally` in `threading.Thread.run`
    drops the target, so only a running worker could pin its manager.

    On start, before consuming anything, the worker re-queues the saves
    held by dead workers (`_reap_orphans`, then `_requeue_orphans`).

    Four things not to "simplify":

    - **`del manager` before returning to the blocking wait.**
      `work_queue.get(timeout=1.0)` blocks for a second, and a strong
      reference held across it keeps the manager alive a second at a
      time, forever.
    - **The weakref is a thread argument and is never stored on the
      manager.** Stored, it would be reachable from the object it is
      supposed not to keep alive, one step from a strong reference.
    - **The `queue.Empty` arm resolves the weakref too.** An idle
      abandoned manager never reaches a successful `get()`, so a loop
      that only checked after one would spin forever: a leaked thread in
      place of a leaked manager.
    - **The queue is held strongly, and that is safe *and*
      load-bearing.** It holds `(list(patients), bool)` tuples -- entity
      graphs, never the manager -- so there is no cycle back, and it is
      what lets the exit path count what it abandons.

    Each item a worker takes is recorded in the manager's `_inflight`
    under this thread until its save finishes, and `task_done()` is
    reached whatever the save does; a failed save is logged at ERROR.

    A dead weakref is a second stop signal alongside the sentinel:
    **manager alive at the sentinel -> the worker writes what is queued
    behind it (`_drain_queued_saves`) and stops; manager collected -> the
    worker exits and logs the count of abandoned saves
    (`_report_abandoned_saves`), because there is no store left to write
    to.** A sentinel taken while `running` is True is stale and skipped.
    """
    # **Reap before consuming anything, on the newly started thread.**
    # A worker restart is the event that always accompanies a recovery
    # being needed -- `save_async` starts one precisely because it found
    # the previous worker dead -- so this is where recovery always runs,
    # rather than only where `flush()` happens to look. Before #315
    # `_recover_orphaned_item` was reachable from `flush()` and nowhere
    # else, and `Session` flushed from exactly two places at the time
    # (#294 has since added a third); a session that saved, lost a
    # worker and then closed never reached it.
    #
    # **`_reap_orphans()`, never `_recover_orphaned_item()`.** The latter
    # takes `_recover_lock` and then calls `_start_worker`, which takes
    # `_worker_lock`; putting *that* in `_start_worker` instead would
    # give `_recover_lock` -> `_worker_lock` -> `_recover_lock` on a
    # non-reentrant lock. Calling the reap -- which takes
    # `_inflight_lock` alone and restarts nothing, since this thread *is*
    # the restart -- makes that impossible by construction rather than by
    # a liveness argument about `self.thread`.
    manager = manager_ref()
    if manager is None:
        _report_abandoned_saves(work_queue)
        return
    try:
        manager._requeue_orphans(manager._reap_orphans())
    finally:
        del manager

    while True:
        manager = None
        try:
            try:
                item = work_queue.get(timeout=1.0)
            except queue.Empty:
                # Resolved here as well as after a successful `get()`.
                # An idle abandoned manager reaches this arm and nothing
                # else, forever.
                if manager_ref() is None:
                    _report_abandoned_saves(work_queue)
                    return
                continue

            manager = manager_ref()
            if manager is None:
                # `item` is already off the deque; it is passed to the
                # report rather than pushed back, so the queue's
                # unfinished count is not disturbed for a `flush()` that
                # can no longer exist.
                _report_abandoned_saves(work_queue, held=item)
                return

            # Record the item before anything can fail, under this
            # thread's own key. From here to the `finally` below, this
            # entry is the only reference to the payload that a `flush()`
            # can reach: the deque no longer holds it (#309). Keying on
            # the thread rather than writing a single slot is what stops
            # a *restarted* worker erasing a dead one's orphan on its way
            # past.
            if item is not None:
                with manager._inflight_lock:
                    manager._inflight[threading.current_thread()] = item

            # If we get a sentinel (None), we exit
            if item is None:
                if manager.running:
                    # Stale sentinel from previous shutdown - ignore it
                    work_queue.task_done()
                    continue
                work_queue.task_done()
                # **Write what is behind this sentinel before stopping
                # (#319).** At this instant this thread stands exactly at
                # the sentinel's position and is the queue's sole
                # consumer, so everything still in the deque is precisely
                # "behind the sentinel", by construction rather than by a
                # timing argument. `shutdown()` posts the sentinel and
                # joins; items queued after that -- by a direct `put`, or
                # by `_requeue_orphans` from a `flush()` racing the
                # shutdown -- used to be written by nobody. That was
                # #314's accepted hole, and this closes it.
                #
                # **It runs here and not in `shutdown()`**, which is the
                # obvious place and the wrong one: `shutdown()` would be
                # calling `save_all` on its own thread while this worker
                # is inside `save_all` over the same graph and the same
                # sidecar -- the concurrency #294 removed, and what
                # `_drain_recoverable_saves` refuses to do for the same
                # reason. It also does not extend `close()`: `shutdown()`
                # still returns at its join timeout and this catch-up
                # write happens afterwards, on the daemon worker.
                #
                # **The manager is held strongly across the drain, and
                # that is a different pin from the one #318 removed.**
                # The drain needs `manager.store_backend` to write at
                # all, and this pin is bounded by the drain rather than
                # renewed every second around a blocking wait. The
                # `finally` at the bottom of the loop drops it, and
                # `break` runs that `finally`.
                #
                # **The `try` is not there because the drain can raise.**
                # `_drain_queued_saves` is total by construction -- every
                # item is written under its own `try`, and `task_done()`
                # is guarded. It is there because the `break` must be
                # reached *whatever* a future edit to that method does:
                # inside the outer `try` alone, anything escaping would
                # be swallowed by `except Exception` below and `while
                # True` would resume with `running is False` and an empty
                # queue -- a worker spinning on `queue.Empty` with
                # nothing left to stop it, which is #250's leak
                # reintroduced by #319's own fix.
                try:
                    manager._drain_queued_saves()
                except Exception as exc:  # pylint: disable=broad-except
                    get_logger().error(
                        "PersistenceManager worker could not write the "
                        f"saves queued behind its sentinel: {describe_exception(exc)} (#319).")
                break

            # Perform the save
            # Everything from unpacking onwards runs under the same
            # try/finally, so `task_done()` is reached no matter what
            # fails. A malformed item that escaped this block would
            # leave the queue's unfinished count permanently above
            # zero, and `flush()` waits on it: one bad item and every
            # later save hangs forever.
            try:
                patients, prune_absent_patients = item
                manager.store_backend.save_all(
                    patients, prune_absent_patients=prune_absent_patients)
            except Exception as e:  # pylint: disable=broad-except
                get_logger().error(f"Background save failed: {describe_exception(e)}")
            finally:
                # Cleared *before* `task_done()`, and the ordering is
                # load-bearing in two ways. Reversed, a flush woken by
                # the count reaching zero can return with this entry
                # still present, and a later recovery -- once this
                # thread is dead -- re-queues a payload that was
                # already saved *and* leaves the count one above
                # zero for good. In this order, a `queue.join` that
                # has returned is proof the clear already happened,
                # which is what makes the second assertion in
                # `test_the_worker_records_and_releases_the_item_it_is_holding`
                # race-free rather than lucky.
                #
                # The cost of this order is the second of #309's two
                # residual windows: a thread killed between the clear
                # and `task_done()` returning leaves no entry with
                # the count still at 1, which recovery cannot tell
                # from a healthy queue. Nothing is *lost* there --
                # the save already committed -- but the flush hangs
                # as permanently as in the first window. Closing
                # either needs atomicity inside `Queue`.
                with manager._inflight_lock:
                    manager._inflight.pop(threading.current_thread(), None)
                work_queue.task_done()

        except Exception as e:  # pylint: disable=broad-except
            get_logger().error(f"Worker crashed: {describe_exception(e)}")
        finally:
            # The one-second wait above is reached with no strong
            # reference held. See the docstring.
            del manager


class PersistenceManager:
    """
    Offloads persistence operations to a background thread to unblock the main thread.

    This manager:
    - Maintains a queue of patient snapshots to save.
    - Runs a background worker thread (`_persistence_worker_loop`, module-level
      over a weakref) to process the queue.
    - Registers an `atexit` handler to ensure pending data is flushed before process termination.
    """

    def __init__(self, store_backend: SqliteStore):
        self.store_backend = store_backend
        self.queue = queue.Queue()
        self.running = False
        self.thread = None

        # `{worker Thread: item}` -- the item each worker took off the
        # queue and has not finished. `queue.get()` removes the payload
        # from the deque while leaving `unfinished_tasks` at 1, so a
        # worker that dies in that window takes the only remaining copy
        # of the save with it and leaves a count nothing can decrement.
        # This mapping holds that copy (#309).
        #
        # **Keyed by the owning thread, and that is the whole point.** A
        # single unowned slot loses to the ordinary sequence "worker dies
        # holding a save, another save is queued, then something
        # flushes": `save_async` restarts the worker, the fresh worker's
        # own `get()` overwrites the slot, and the orphan payload is
        # destroyed while `unfinished_tasks` stays at 1 forever. Measured
        # on that shape: the new save reached the store, the orphaned one
        # did not, and `flush()` never returned. Recovery therefore asks
        # whether *the thread that took this item* is dead, not whether
        # the manager currently has a live worker.
        self._inflight = {}
        self._inflight_lock = threading.Lock()
        # Serialises `_recover_orphaned_item` so two concurrent flushes
        # cannot both reach `_start_worker()` and leave two consumers on
        # one queue -- the #250 leak, one level up. Exactly-once re-queue
        # is *not* what this lock buys: that comes from popping each
        # entry out of `_inflight` under `_inflight_lock` before it is
        # put back, so only one caller can ever hold a given payload.
        self._recover_lock = threading.Lock()
        # Makes `_start_worker` idempotent under concurrency. Both call
        # sites used to do their own check-then-act on thread liveness,
        # so two callers could observe one dead worker and both start a
        # replacement -- two consumers on one queue, one sentinel
        # between them, and `shutdown()`'s join burning its full timeout
        # on a thread that never got the sentinel (#313, #250's shape).
        #
        # **A leaf lock.** Nothing is acquired while it is held and
        # nothing under it calls into `SqliteStore`; the body is a
        # liveness check and a thread construction. The order with the
        # other manager lock is `_recover_lock` -> `_worker_lock`
        # (`_recover_orphaned_item` already holds the former when it
        # restarts). Never take `_recover_lock` while holding this one.
        self._worker_lock = threading.Lock()

        self._start_worker()

        # **A weakref, not `self.shutdown`.** `atexit.register` holds its
        # arguments for the life of the process, so a bound method made
        # every manager ever constructed immortal -- and with it its
        # `SqliteStore`, that store's sqlite handles, its audit-writer
        # thread and its sidecar file descriptors. Measured over one full
        # suite run: 647 live managers and 149 threads at interpreter
        # exit, 147 of them audit writers.
        #
        # **That argument used to run differently, and #318 falsified
        # it.** It said nothing was lost structurally, because a manager
        # with a live worker could not be collected at all -- the worker
        # held `self` through `target=self._worker` -- so only a manager
        # whose worker had already exited became collectable, and that
        # one had nothing left to flush. Since #318 the worker holds a
        # weakref, so a manager *can* be collected with saves still
        # queued, and those saves are lost: the worker exits on the dead
        # weakref and reports the count on the log
        # (`_report_abandoned_saves`).
        #
        # **The loss is inherent, not a choice made here.** The worker
        # cannot write without `manager.store_backend`, and holding the
        # store strongly is the exact pin #318 removes.
        #
        # **What is still not lost is what this registration is for.**
        # `close()`, `with`, and a `Session` still referenced at
        # interpreter exit all reach `shutdown()`: on the last of those
        # the weakref below resolves and the flush runs. The newly-lossy
        # population is a `Session` dropped mid-flight without
        # `close()`, which CLAUDE.md already documents as unsupported
        # ("leaks worker subprocesses if skipped"). #316 made the same
        # trade one level down.
        #
        # The ordering this depends on was checked rather than assumed --
        # `atexit` callbacks run while daemon threads are still alive;
        # interpreter finalization, which stops them, comes afterwards.
        #
        # The registration itself is not undone: the `atexit` list still
        # grows by one small closure per manager. What is reclaimed is the
        # manager graph behind it, which is where the handles and threads
        # are. Unregistering at `shutdown()` was rejected because a
        # shut-down manager is restartable -- `save_async` starts a fresh
        # worker -- and it would lose its exit-time flush.
        #
        # Since #314 this path can perform sqlite WRITES it never
        # performed before: `shutdown()` reconciles an orphaned or
        # still-queued save on its way out. Intended, and new. The
        # ordering it depends on is the one already checked above --
        # `atexit` runs while daemon threads are still alive.
        atexit.register(_flush_at_exit, weakref.ref(self))
        get_logger().info("PersistenceManager initialized.")

    def _start_worker(self):
        # **The guard is thread liveness, not `self.running`.** The worker
        # loop is a `while True`; setting `running = False` does not end it, and
        # nothing else does either -- only the sentinel `shutdown()` posts
        # does. So *a live worker with `running is False`* is a normal
        # state (it is the whole window between `shutdown()` setting the
        # flag and the worker draining down to the sentinel), and guarding
        # on the flag started a SECOND consumer on the same queue every
        # time. Measured before this changed: `test_persistence_chaos`
        # toggles it nine times and ended with nine live workers, after
        # which `shutdown()` posts one sentinel, any of the nine may eat
        # it, and the `join(timeout=30)` on `self.thread` burned the full
        # 30s -- twice, the second time from `atexit` after pytest's
        # summary line, where nothing is watching (#250).
        #
        # Restoring the flag under a live worker is what keeps
        # `test_stale_sentinel` composing: a pending sentinel left by an
        # earlier shutdown is read as stale exactly because `running` is
        # True again, so it is counted off and the worker continues.
        # The guard and the create-and-start are ONE critical section,
        # which is the whole content of #313: split, the check is advice
        # rather than an answer, and `save_async`'s unlocked pre-check
        # (which stays, as a fast path) is not the authority.
        with self._worker_lock:
            if self.thread is not None and self.thread.is_alive():
                self.running = True
                return

            self.running = True
            # Named so the stall census in `tests/conftest.py` -- which
            # counts `threading.enumerate()` by name -- says "nine
            # PersistenceWorkers" rather than "nine Thread-N", which is
            # the diagnostic #250 needed and did not have.
            # **A module-level target over a weakref, not
            # `target=self._worker`.** A running `Thread` holds its
            # target and a bound method holds `self`, so the bound
            # spelling made every manager with a live worker immortal --
            # and with it its store, that store's sqlite handles, its
            # audit-writer thread and its sidecar descriptors (#318).
            # The queue is passed alongside because the worker needs it
            # after the weakref goes dead, to count what it abandons.
            self.thread = threading.Thread(
                target=_persistence_worker_loop,
                args=(weakref.ref(self), self.queue),
                daemon=True, name="PersistenceWorker")
            self.thread.start()
            get_logger().info("PersistenceManager worker thread started.")

    def flush(self):
        """
        Blocks until all tasks in the queue have been processed.

        This method ensures that any currently queued save operations are completed before returning.
        If the worker thread has unexpectedly died, it recovers whatever it
        was carrying and restarts it to drain the queue.

        **It never returns early.** A bounded wait that gave up and
        returned would turn a visible hang into a silently dropped save:
        callers flush precisely so they can read or shut down afterwards.
        What is bounded is the *silence* -- every
        `_FLUSH_REPORT_INTERVAL_S` a wait that has not finished logs a
        WARNING saying what it is waiting for and re-attempts recovery,
        so a worker that dies after this flush began is recovered too.
        """
        self._recover_orphaned_item()

        # `queue.join()` on a short-lived daemon so the wait can be
        # interrupted periodically to report and re-check. Cost is one
        # extra thread per `flush()` CALL on a healthy flush, and one more
        # per report interval while it is still waiting -- earlier waiters
        # stay blocked until the queue drains, so a wedged flush
        # accumulates one thread every 30s. Never per queued item, and
        # that is the bound that matters: `Session` calls `flush()` from
        # three places -- `audit()`, `redact()` and, since #294,
        # `save(sync=True)` (which `compact()` leads with). #294 put
        # `flush()` on the save path, so "not on the save path" is no
        # longer the reason the cost is bounded; the reason is that all
        # three are per *caller invocation* and none is per queued item,
        # and the waiter is created and reaped inside the call.
        # (`close()` still does NOT flush -- it calls `shutdown()`, which
        # returns without waiting when the worker is already dead. Since
        # #314 that is no longer a silent drop: `shutdown()` reconciles
        # what it finds on its way out, bounded at one `save_all`.)
        while True:
            waiter = threading.Thread(target=self.queue.join, daemon=True)
            waiter.start()
            waiter.join(timeout=_FLUSH_REPORT_INTERVAL_S)
            if not waiter.is_alive():
                return

            alive = bool(self.thread and self.thread.is_alive())
            with self._inflight_lock:
                inflight = len(self._inflight)
            # `unfinished_tasks` is a CPython implementation detail and is
            # read here for the message only. Nothing in the recovery path
            # depends on it, and nothing anywhere touches `all_tasks_done`.
            get_logger().warning(
                "PersistenceManager.flush() has waited "
                f"{_FLUSH_REPORT_INTERVAL_S:g}s: "
                f"unfinished_tasks={self.queue.unfinished_tasks}, "
                f"worker_alive={alive}, in_flight_items={inflight} (#309)")
            self._recover_orphaned_item()

    def _reap_orphans(self):
        """Pop and return the payloads whose owning worker is dead.

        Popping under `_inflight_lock` is what makes re-queueing
        exactly-once a property of the data rather than of the ordering
        between two callers: two of them cannot both come away holding
        the same payload. A live owner's entry is never touched -- that
        worker is simply mid-save, and re-queueing under it would both
        duplicate the write and unbalance the queue's count.

        Takes `_inflight_lock` and nothing else, and calls nothing, so
        the worker loop can call it on start. Calling
        `_recover_orphaned_item` there instead would take `_recover_lock`
        and then `_worker_lock` from inside a worker start.
        """
        with self._inflight_lock:
            return [self._inflight.pop(owner)
                    for owner in list(self._inflight)
                    if not owner.is_alive()]

    def _requeue_orphans(self, orphaned):
        """Put reaped payloads back, `put()` **then** `task_done()`.

        One spelling for that ordering, which has two callers. Reversed,
        `unfinished_tasks` reaches zero between the two calls, a waiting
        `queue.join` wakes, and `flush()` can return before the payload
        is back in the deque -- a dropped save behind a clean return.
        In this order the net count is unchanged and the payload is
        queued before the orphan is counted off. Logs one WARNING per
        payload.
        """
        for payload in orphaned:
            get_logger().warning(
                "PersistenceManager worker stopped holding a save it "
                "never finished; re-queueing it (#309).")
            self.queue.put(payload)
            self.queue.task_done()

    def _recover_orphaned_item(self):
        """Put back a save whose worker took it and never finished it.

        **The liveness question is asked of the item's own owner, not of
        `self.thread`.** A dead `Thread` is a stable observation -- it
        cannot resume -- whereas a worker still holding its entry is
        simply mid-save, and re-queueing under it would both duplicate
        the write and unbalance the queue's count. Both halves of that
        argument are about *the thread that took the item*. Gating on
        `self.thread` would make recovery a no-op as soon as `save_async`
        restarted the worker, and the flush would hang with the orphan
        unreachable.

        The reap and the re-queue live in `_reap_orphans` and
        `_requeue_orphans`, which `_persistence_worker_loop` also calls on
        start. If the current worker is dead and the queue still has
        unfinished tasks, a new worker is started.

        Re-queueing exactly once does not depend on `_recover_lock`:
        each entry is *popped* out of `_inflight` under `_inflight_lock`
        before it is put back, so two concurrent callers cannot both
        hold the same payload. What `_recover_lock` buys is that they
        cannot both reach `_start_worker()`.
        """
        with self._recover_lock:
            self._requeue_orphans(self._reap_orphans())

            if self.thread is not None and self.thread.is_alive():
                return

            if self.queue.unfinished_tasks:
                get_logger().warning(
                    "PersistenceManager worker was found dead/stopped with "
                    "pending items during flush. Restarting to process "
                    "backlog.")
                print("Restarting stopped Persistence Manager to process pending items...")
                self._start_worker()

    def save_async(self, patients: List[Patient],
                   prune_absent_patients: bool = False):
        """
        Queues an asynchronous save operation for a list of patients.

        Creates a shallow copy (snapshot) of the list to mitigate race conditions
        where the UI/Session might add/remove patients during the save process.

        Args:
            patients (List[Patient]): The list of patients to persist.
            prune_absent_patients (bool): Whether this list is the entire
                contents of the session, so patient rows it does not contain
                may be deleted. Travels with the queued item rather than
                being assumed here: callers queue single patients as well as
                whole stores, and pruning on a single-patient save would
                delete every other patient in the database.
        """
        # Auto-restart if we were shut down
        if not self.running or not self.thread or not self.thread.is_alive():
            get_logger().info("PersistenceManager was stopped. Restarting worker for new save operation.")
            self._start_worker()

        # Shallow copy the list itself so if the session adds/removes patients, we have the old list.
        # But if attributes of patients change, we see the change. This is usually
        # acceptable "eventual consistency" for this UX.
        self.queue.put((list(patients), prune_absent_patients))

    def has_pending_saves(self):
        """Is any save queued or in flight right now?

        A point-in-time reading, and callers must treat it as one: a
        save queued the instant after it returns is not covered.
        `Session.compact()` reads it to refuse a compaction started
        against a manager with work outstanding.
        """
        with self._inflight_lock:
            inflight = bool(self._inflight)
        return inflight or not self.queue.empty()

    def shutdown(self):
        """
        Stops the worker thread gracefully, then reconciles what is left.

        Waits for any pending operations to complete (with a timeout)
        before killing the thread (via sentinel and join), and then --
        always, including on the early return below --
        `_drain_recoverable_saves()` writes whatever a dead worker left
        behind, so an orphaned or still-queued save reaches the store
        rather than being dropped at `close()`.

        **It is not `flush()` and must never become one.** `close()` is
        what `__exit__` calls, `flush()` documents that it never returns
        early, and a context manager that does not exit is worse than
        the bug. The reconciliation is bounded at one `save_all`, and
        never a wait on the queue's unfinished count.
        """
        try:
            self._shutdown_worker()
        finally:
            self._drain_recoverable_saves()

    def _shutdown_worker(self):
        """Post the sentinel and join, waiting at most `_SHUTDOWN_JOIN_TIMEOUT_S`.

        Returns at once if the worker is not alive. Sets `running` to
        False before posting the sentinel.
        """
        # Avoid double shutdown or shutdown if never started
        if not self.thread.is_alive():
            return

        get_logger().info("Shutting down PersistenceManager...")
        print("\nShutting down Isocenter Persistence Manager...")

        # Determine if we have pending work
        pending = self.queue.qsize()
        if pending > 0:
            print(f"Waiting for {pending} pending save operations to complete...")
            get_logger().info(f"Waiting for {pending} pending save operations...")

        # Stop worker
        self.running = False
        # Wake up if sleeping on queue
        self.queue.put(None)

        self.thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_S)
        get_logger().info("PersistenceManager stopped.")
        print("Persistence Manager Stopped.")

    def _drain_recoverable_saves(self):
        """Write what a stopped worker left behind, or say why it cannot.

        Runs from the `finally` of `shutdown()`, so it also covers the early
        return taken when the worker was already dead. With the worker
        dead, it reaps the dead workers' in-flight saves and writes them
        and everything left in the queue (`_drain_queued_saves`).

        **Nothing is touched while a worker is still alive.** A join that
        timed out means a save is genuinely running: *that worker's*
        `_inflight` entry is the item it is inside `save_all` with, and
        saving it here would double-write and race. Its queue must not be
        drained either -- `shutdown()` has already posted the sentinel
        that stops it, and a drain that swallowed that sentinel would
        leave a `while True` worker with nothing left to end it. So it
        reports (`_report_unreconciled`) and returns, when anything is
        outstanding.

        **Nothing is left unwritten by that.** The items *ahead* of the
        sentinel are still the live worker's and it writes them before it
        stops; the items *behind* it the worker writes at the instant it
        takes its own sentinel (`_drain_queued_saves`). Both are deferred
        rather than lost, which is why the reported number is a queue
        *depth* (it counts the sentinel as well) rather than a count of
        lost saves.

        **It never raises.** `Session.close()` re-raises the first
        exception any of its steps produced and `__exit__` returns None,
        so an exception escaping here would replace whatever the user's
        `with` body was unwinding with a persistence one.

        The `in_flight_items` that branch reports is `len(self._inflight)`,
        which counts entries under **every** owner and so can include a
        prior worker's orphan alongside the running save. It is a
        diagnostic for a human reading a teardown log, not a count of
        running saves, and over-reporting is the right direction to err:
        the number exists to say that something was left unreconciled.

        No manager lock is held across the `save_all`: `_reap_orphans()`
        releases `_inflight_lock` before returning, and neither
        `_recover_lock` nor `_worker_lock` is taken here at all.
        """
        if self.thread is not None and self.thread.is_alive():
            with self._inflight_lock:
                outstanding = len(self._inflight)
            if outstanding or not self.queue.empty():
                # "Still running" now covers one state it did not before
                # (#319): the thread may be inside its own post-sentinel
                # drain rather than inside a user's save. Either way it is
                # the queue's sole consumer and must not be raced, which
                # is the only thing this branch decides -- so the message
                # says what is true of both, and no longer claims that
                # what is behind the sentinel goes unwritten.
                self._report_unreconciled(
                    "PersistenceManager.shutdown() timed out with its "
                    "worker still running; whatever it holds is left with "
                    "it rather than written here, which would double-write "
                    f"and race it. in_flight_items={outstanding} (recorded "
                    f"under any owner), queue_depth={self.queue.qsize()} "
                    "(includes the shutdown sentinel; that worker writes "
                    "what is behind its own sentinel before it stops, "
                    "#319) (#314).")
            return

        self._drain_queued_saves(self._reap_orphans())

    def _drain_queued_saves(self, already_held=()):
        """Write every item left in the deque, and count each one off.

        Two callers, one spelling. `_drain_recoverable_saves` calls it
        for a manager whose worker is dead, passing the orphans it
        reaped; `_persistence_worker_loop` calls it with nothing held,
        at the instant it takes its own shutdown sentinel, to write what
        was queued behind that sentinel. Each item written logs a
        WARNING; a failed write is reported through
        `_report_unreconciled`.

        **This method never reaps.** `_reap_orphans` is about items a
        *dead* thread was holding, which the worker calling it would be
        asking about itself; the caller that reaps passes them in as
        `already_held`.

        **`task_done()` exactly once per item consumed**, and the
        arithmetic is the part to get right. A reaped orphan was
        `get()`-ed by its dead owner and never counted off, so it owes
        one. An item still in the deque owes one for the `get_nowait()`
        below. Miss one and `unfinished_tasks` stays above zero on a
        manager `save_async` can restart, and the next `flush()` never
        returns. Count one too many and the queue raises `ValueError:
        task_done() called too many times`, which is caught and logged.

        **One pass to `Empty`, never a re-post loop.** Two sentinels with
        `running` still False would ping-pong forever under a re-post;
        `None` is skipped and counted off instead.

        **An item queued after this sees `Empty` is not stranded.** It
        arrives through `save_async`, which restarts a worker once this
        thread is dead.

        **`_inflight` is not written during this drain.** A thread killed
        mid-drain loses the item it holds; closing that window would need
        atomicity inside `Queue`.

        **It never raises**, and both callers depend on that: one runs
        from `Session.close()`, which would otherwise replace whatever a
        user's `with` body was unwinding; the other must reach its
        `break`, or a `while True` worker is left with nothing to stop it.
        """
        items = list(already_held)
        while True:
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                break

        for item in items:
            try:
                # A `None` here is a stale sentinel from an earlier
                # `shutdown()` whose worker died without taking it.
                # The worker loop special-cases these; a drain that does not
                # runs `patients, prune = None` and raises `TypeError`
                # out of `close()`.
                if item is None:
                    continue
                patients, prune_absent_patients = item
                # Not "shutdown() is writing ...": since #319 the
                # other caller is the worker itself, taking its own
                # sentinel, and a log line naming the wrong thread is
                # the kind of small lie that costs an hour.
                get_logger().warning(
                    "PersistenceManager is writing a save its worker "
                    "never finished (#314, #319).")
                self.store_backend.save_all(
                    patients, prune_absent_patients=prune_absent_patients)
            except Exception as exc:  # pylint: disable=broad-except
                self._report_unreconciled(
                    "PersistenceManager could not write a save its worker "
                    f"left behind: {exc} (#314, #319).")
            finally:
                # `task_done()` gets its own guard, and that is not
                # belt-and-braces. It is the one statement here whose
                # safety rests on an *arithmetic* argument -- exactly one
                # call per item consumed, as argued above -- rather than
                # on construction, and the queue's answer to a wrong
                # argument is `ValueError: task_done() called too many
                # times`. In a bare `finally` that escapes this method,
                # and `Session.close()` re-raises the first exception any
                # of its steps produced, so a miscount would replace
                # whatever the user's `with` body was unwinding with a
                # persistence one -- the precise outcome this method's
                # docstring promises cannot happen. Guarded, "it never
                # raises" is a property of the code rather than of the
                # argument (#314).
                try:
                    self.queue.task_done()
                except ValueError as exc:
                    get_logger().error(
                        "PersistenceManager counted off more tasks than the "
                        f"queue is holding: {describe_exception(exc)} (#314).")

    def _report_unreconciled(self, message):
        """Say -- on both channels -- that a save did not reach the store.

        The log line is for whoever is watching the process; the audit
        row is the durable half. Both are best-effort by construction:
        this runs on a teardown path and must not raise.

        **The row is flushed here rather than by a later `stop()`.**
        `Session.close()` runs `store_backend.stop()` after `shutdown()`,
        but `_flush_at_exit` calls `shutdown()` with no `stop()` behind it,
        and there the row would sit in a queue whose daemon writer the
        interpreter is about to stop at finalization.
        `flush_audit_queue()` drains on this thread under
        `_audit_write_lock`, takes no manager lock, and is bounded by
        one `log_audit_batch`, so it neither touches the invariant nor
        turns the teardown into a wait.
        """
        get_logger().error(message)
        try:
            self.store_backend.log_audit("ERROR", "SESSION", message)
            self.store_backend.flush_audit_queue()
        except Exception as exc:  # pylint: disable=broad-except
            get_logger().error(
                f"PersistenceManager could not record the unreconciled "
                f"save in the audit log: {describe_exception(exc)}")
