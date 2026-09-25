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
#: var, because nothing outside a test has a reason to move it. A join
#: that times out is not a failure -- it means a save is genuinely still
#: running, which `shutdown()` reports and leaves alone.
_SHUTDOWN_JOIN_TIMEOUT_S = 30.0


# Module-level and taking a weak reference, so registering it does not keep
# the manager (and its store, threads and file descriptors) alive for the life
# of the process.
def _flush_at_exit(manager_ref):
    """Shut down a manager at interpreter exit, if it is still alive.

    Args:
        manager_ref (weakref.ref): A weak reference to the manager.
    """
    manager = manager_ref()
    if manager is not None:
        manager.shutdown()


def _report_abandoned_saves(work_queue, held=None):
    """Log that a collected manager took unwritten saves with it.

    Drains `work_queue` and logs one WARNING with the number of saves lost,
    if any; shutdown sentinels are not counted. Writes no audit row.

    Args:
        work_queue (queue.Queue): The dead manager's queue.
        held: The item the worker had already taken off the queue when it
            found the manager gone, counted with the rest; None if none.
    """
    # A log line and no audit row: writing the row would need
    # `manager.store_backend`, which is gone with the manager.
    #
    # Drains rather than reading `qsize()`: the queue also holds shutdown
    # sentinels, and counting a `None` as a lost save would overstate the
    # loss. Draining is safe here and nowhere else: the manager is
    # unreachable, so no `flush()` can be waiting on the count and no other
    # consumer can be started. `held` is passed rather than pushed back for
    # the same reason.
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


# Module-level, taking a **weak** reference: a running `Thread` holds its
# target, and a bound-method target would hold `self`, keeping the manager --
# its store, sqlite handles, audit-writer thread and sidecar descriptors --
# alive for as long as the worker runs.
def _persistence_worker_loop(manager_ref, work_queue):
    """Background save worker that does not keep its manager alive.

    Runs on the manager's worker thread. On start, before consuming
    anything, it re-queues the saves held by dead workers. It then takes
    items off `work_queue` and writes each with `store_backend.save_all`;
    a failed save is logged at ERROR and the loop continues.

    It stops on either of two signals. A sentinel (None) taken while the
    manager is alive and `running` is False: the worker writes everything
    queued behind it (`_drain_queued_saves`) and exits. A sentinel taken
    while `running` is True is stale and skipped. A dead weakref: the
    manager was collected, so the worker logs the count of abandoned saves
    (`_report_abandoned_saves`) and exits, since there is no store left.

    Args:
        manager_ref (weakref.ref): A weak reference to the manager.
        work_queue (queue.Queue): The manager's queue of
            `(list(patients), prune_absent_patients)` items and sentinels.
    """
    # **Reap before consuming anything, on the newly started thread.**
    # A worker restart always accompanies a recovery being needed --
    # `save_async` starts one precisely because it found the previous worker
    # dead -- so this is where recovery always runs, rather than only where
    # `flush()` happens to look. Without it, a session that saved, lost a
    # worker and then closed would never re-queue the orphan.
    #
    # **`_reap_orphans()`, never `_recover_orphaned_item()`.** The latter
    # takes `_recover_lock` and then calls `_start_worker`, which takes
    # `_worker_lock`; calling it from here, inside a worker start, would
    # give `_recover_lock` -> `_worker_lock` -> `_recover_lock` on a
    # non-reentrant lock. The reap takes `_inflight_lock` alone and
    # restarts nothing, since this thread *is* the restart.
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
                # else, forever, so a loop that only checked after a `get()`
                # would spin here for good: a leaked thread in place of a
                # leaked manager.
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
            # can reach: the deque no longer holds it. Keying on the thread
            # rather than writing a single slot is what stops a *restarted*
            # worker erasing a dead one's orphan on its way past.
            if item is not None:
                with manager._inflight_lock:
                    manager._inflight[threading.current_thread()] = item

            if item is None:
                if manager.running:
                    # Stale sentinel from previous shutdown - ignore it
                    work_queue.task_done()
                    continue
                work_queue.task_done()
                # **Write what is behind this sentinel before stopping.** At
                # this instant this thread stands exactly at the sentinel's
                # position and is the queue's sole consumer, so everything
                # still in the deque is precisely "behind the sentinel", by
                # construction rather than by a timing argument. Items queued
                # after `shutdown()` posts the sentinel -- by a direct `put`,
                # or by `_requeue_orphans` from a `flush()` racing the
                # shutdown -- would otherwise be written by nobody.
                #
                # **It runs here and not in `shutdown()`**: `shutdown()` would
                # be calling `save_all` on its own thread while this worker is
                # inside `save_all` over the same graph and the same sidecar,
                # which is what `_drain_recoverable_saves` refuses to do for
                # the same reason. It also does not extend `close()`:
                # `shutdown()` still returns at its join timeout and this
                # catch-up write happens afterwards, on the daemon worker.
                #
                # **The manager is held strongly across the drain.** The drain
                # needs `manager.store_backend` to write at all, and this pin
                # is bounded by the drain rather than renewed every second
                # around a blocking wait. The `finally` at the bottom of the
                # loop drops it, and `break` runs that `finally`.
                #
                # **The `try` is not there because the drain can raise.**
                # `_drain_queued_saves` is total by construction -- every item
                # is written under its own `try`, and `task_done()` is
                # guarded. It is there because the `break` must be reached
                # *whatever* a future edit to that method does: inside the
                # outer `try` alone, anything escaping would be swallowed by
                # `except Exception` below and `while True` would resume with
                # `running is False` and an empty queue -- a worker spinning
                # on `queue.Empty` with nothing left to stop it.
                try:
                    manager._drain_queued_saves()
                except Exception as exc:  # pylint: disable=broad-except
                    get_logger().error(
                        "PersistenceManager worker could not write the "
                        f"saves queued behind its sentinel: {describe_exception(exc)} (#319).")
                break

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
                # load-bearing. Reversed, a flush woken by the count
                # reaching zero can return with this entry still present,
                # and a later recovery -- once this thread is dead --
                # re-queues a payload that was already saved *and* leaves
                # the count one above zero for good. In this order, a
                # `queue.join` that has returned is proof the clear
                # already happened.
                #
                # The cost of this order is a residual window: a thread
                # killed between the clear and `task_done()` returning
                # leaves no entry with the count still at 1, which
                # recovery cannot tell from a healthy queue. Nothing is
                # *lost* there -- the save already committed -- but the
                # flush hangs. Closing it needs atomicity inside `Queue`.
                with manager._inflight_lock:
                    manager._inflight.pop(threading.current_thread(), None)
                work_queue.task_done()

        except Exception as e:  # pylint: disable=broad-except
            get_logger().error(f"Worker crashed: {describe_exception(e)}")
        finally:
            # The one-second wait above is reached with no strong
            # reference held: a reference kept across it would keep the
            # manager alive a second at a time, forever.
            del manager


class PersistenceManager:
    """Offloads persistence operations to a background thread to unblock the main thread.

    This manager:
    - Maintains a queue of patient snapshots to save.
    - Runs a background worker thread (`_persistence_worker_loop`) to
      process the queue.
    - Registers an `atexit` handler that shuts it down, flushing pending
      saves, before process termination, for as long as the manager is
      alive.

    Args:
        store_backend (SqliteStore): The store every queued save is written to.
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
        # This mapping holds that copy.
        #
        # **Keyed by the owning thread.** A single unowned slot loses to
        # the ordinary sequence "worker dies holding a save, another save
        # is queued, then something flushes": `save_async` restarts the
        # worker, the fresh worker's own `get()` overwrites the slot, and
        # the orphan payload is destroyed while `unfinished_tasks` stays at
        # 1 forever. Recovery therefore asks whether *the thread that took
        # this item* is dead, not whether the manager currently has a live
        # worker.
        self._inflight = {}
        self._inflight_lock = threading.Lock()
        # Serialises `_recover_orphaned_item` so two concurrent flushes
        # cannot both reach `_start_worker()` and leave two consumers on
        # one queue. Exactly-once re-queue is *not* what this lock buys:
        # that comes from popping each entry out of `_inflight` under
        # `_inflight_lock` before it is put back, so only one caller can
        # ever hold a given payload.
        self._recover_lock = threading.Lock()
        # Makes `_start_worker` idempotent under concurrency. Without it
        # two callers could observe one dead worker and both start a
        # replacement -- two consumers on one queue, one sentinel between
        # them, and `shutdown()`'s join burning its full timeout on a
        # thread that never got the sentinel.
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
        # arguments for the life of the process, so a bound method would
        # make every manager ever constructed immortal -- and with it its
        # `SqliteStore`, that store's sqlite handles, its audit-writer
        # thread and its sidecar file descriptors.
        #
        # **A manager *can* be collected with saves still queued, and those
        # saves are lost:** the worker holds only a weakref, exits on the
        # dead weakref and reports the count on the log
        # (`_report_abandoned_saves`). The loss is inherent: the worker
        # cannot write without `manager.store_backend`, and holding the
        # store strongly is the pin the weakref exists to avoid.
        #
        # **What is not lost is what this registration is for.** `close()`,
        # `with`, and a `Session` still referenced at interpreter exit all
        # reach `shutdown()`: on the last of those the weakref below
        # resolves and the flush runs. The lossy case is a `Session`
        # dropped mid-flight without `close()`, which is unsupported.
        #
        # This depends on `atexit` callbacks running while daemon threads
        # are still alive; interpreter finalization, which stops them,
        # comes afterwards. `shutdown()` can perform sqlite writes on this
        # path, because it reconciles an orphaned or still-queued save on
        # its way out.
        #
        # The registration itself is not undone: the `atexit` list still
        # grows by one small closure per manager. What is reclaimed is the
        # manager graph behind it, which is where the handles and threads
        # are. Do not unregister at `shutdown()`: a shut-down manager is
        # restartable -- `save_async` starts a fresh worker -- and it would
        # lose its exit-time flush.
        atexit.register(_flush_at_exit, weakref.ref(self))
        get_logger().info("PersistenceManager initialized.")

    def _start_worker(self):
        # **The guard is thread liveness, not `self.running`.** The worker
        # loop is a `while True`; setting `running = False` does not end it,
        # and nothing else does either -- only the sentinel `shutdown()`
        # posts does. So *a live worker with `running is False`* is a normal
        # state (the whole window between `shutdown()` setting the flag and
        # the worker draining down to the sentinel), and guarding on the
        # flag would start a SECOND consumer on the same queue each time;
        # `shutdown()` then posts one sentinel, any consumer may eat it, and
        # the join on `self.thread` burns its full timeout.
        #
        # Restoring the flag under a live worker is what makes a pending
        # sentinel left by an earlier shutdown read as stale: `running` is
        # True again, so it is counted off and the worker continues.
        # The guard and the create-and-start are ONE critical section:
        # split, the check is advice rather than an answer, and
        # `save_async`'s unlocked pre-check (which stays, as a fast path)
        # is not the authority.
        with self._worker_lock:
            if self.thread is not None and self.thread.is_alive():
                self.running = True
                return

            self.running = True
            # Named so a thread census by name says "PersistenceWorker"
            # rather than "Thread-N".
            # **A module-level target over a weakref, not
            # `target=self._worker`.** A running `Thread` holds its
            # target and a bound method holds `self`, so the bound
            # spelling would make every manager with a live worker
            # immortal -- and with it its store, that store's sqlite
            # handles, its audit-writer thread and its sidecar descriptors.
            # The queue is passed alongside because the worker needs it
            # after the weakref goes dead, to count what it abandons; it
            # holds entity graphs, never the manager, so there is no cycle
            # back. The weakref is a thread argument and is never stored on
            # the manager, one step from a strong reference.
            self.thread = threading.Thread(
                target=_persistence_worker_loop,
                args=(weakref.ref(self), self.queue),
                daemon=True, name="PersistenceWorker")
            self.thread.start()
            get_logger().info("PersistenceManager worker thread started.")

    def flush(self):
        """Blocks until all tasks in the queue have been processed.

        If a worker has died holding a save, the save is re-queued and a
        worker restarted to drain the queue.

        It never returns early: a wedged save means a wedged flush. Every
        `_FLUSH_REPORT_INTERVAL_S` a wait that has not finished logs a WARNING
        saying what it is waiting for and re-attempts recovery, so a worker
        that dies after this flush began is recovered too.
        """
        self._recover_orphaned_item()

        # `queue.join()` on a short-lived daemon so the wait can be
        # interrupted periodically to report and re-check. Cost is one
        # extra thread per `flush()` CALL on a healthy flush, and one more
        # per report interval while it is still waiting -- earlier waiters
        # stay blocked until the queue drains, so a wedged flush
        # accumulates one thread every interval. Never per queued item:
        # `Session` calls `flush()` from `audit()`, `redact()` and
        # `save(sync=True)` (which `compact()` leads with), each once per
        # caller invocation, and the waiter is created and reaped inside
        # the call. (`close()` does NOT flush -- it calls `shutdown()`,
        # which returns without waiting when the worker is already dead
        # and reconciles what it finds, bounded at one `save_all`.)
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

        A live owner's entry is never touched. Takes `_inflight_lock` and no
        other lock.

        Returns:
            list: The reaped payloads, each removed from `_inflight`.
        """
        # Popping under `_inflight_lock` makes re-queueing exactly-once a
        # property of the data: two callers cannot both come away holding
        # the same payload. A live owner is simply mid-save; re-queueing
        # under it would duplicate the write and unbalance the queue's count.
        # Taking no other lock and calling nothing is what lets the worker
        # loop call this on start without reentering `_recover_lock`.
        with self._inflight_lock:
            return [self._inflight.pop(owner)
                    for owner in list(self._inflight)
                    if not owner.is_alive()]

    def _requeue_orphans(self, orphaned):
        """Put reaped payloads back on the queue, `put()` then `task_done()`.

        Leaves the queue's unfinished count unchanged. Logs one WARNING per
        payload.

        Args:
            orphaned (list): Payloads from `_reap_orphans`.
        """
        for payload in orphaned:
            get_logger().warning(
                "PersistenceManager worker stopped holding a save it "
                "never finished; re-queueing it (#309).")
            # `put()` then `task_done()`. Reversed, `unfinished_tasks` reaches
            # zero between the two calls, a waiting `queue.join` wakes, and
            # `flush()` can return before the payload is back in the queue.
            self.queue.put(payload)
            self.queue.task_done()

    def _recover_orphaned_item(self):
        """Put back the saves whose worker took them and never finished them.

        Re-queues each payload whose owning thread is dead, then, if the
        current worker is dead and the queue still has unfinished tasks,
        starts a new worker. Safe to call concurrently: each payload is
        re-queued exactly once and at most one worker is started.
        """
        # Liveness is asked of each item's own owner, not of `self.thread`:
        # gating on `self.thread` would make recovery a no-op as soon as
        # `save_async` restarted the worker, and the flush would hang with the
        # orphan unreachable. `_recover_lock` is what stops two callers both
        # reaching `_start_worker()`; exactly-once re-queue comes from the pop.
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
        """Queues an asynchronous save operation for a list of patients.

        Queues a shallow copy of the list, so patients added or removed later
        do not change this save; changes to the patients themselves before
        the save runs are written. Restarts the worker if it was shut down or
        has died.

        Args:
            patients (List[Patient]): The list of patients to persist.
            prune_absent_patients (bool): Whether this list is the entire
                contents of the session, so patient rows it does not contain
                may be deleted. Pass True only for the whole session: pruning
                on a single-patient save deletes every other patient in the
                database.
        """
        if not self.running or not self.thread or not self.thread.is_alive():
            get_logger().info("PersistenceManager was stopped. Restarting worker for new save operation.")
            self._start_worker()

        # Shallow copy the list itself so if the session adds/removes patients, we have the old list.
        # But if attributes of patients change, we see the change. This is usually
        # acceptable "eventual consistency" for this UX.
        self.queue.put((list(patients), prune_absent_patients))

    def has_pending_saves(self):
        """Is any save queued or in flight right now?

        A point-in-time reading: a save queued the instant after it returns
        is not covered.

        Returns:
            bool: True when a save is queued or a worker is holding one.
        """
        with self._inflight_lock:
            inflight = bool(self._inflight)
        return inflight or not self.queue.empty()

    def shutdown(self):
        """Stops the worker thread, then writes what a dead worker left behind.

        Posts the shutdown sentinel and waits at most
        `_SHUTDOWN_JOIN_TIMEOUT_S` for the worker. Then, always, writes any
        orphaned or still-queued save to the store, unless the worker is
        still running, in which case it is left to that worker and reported
        at ERROR and as an audit row. Bounded: it never waits on the queue's
        unfinished count. The manager can be restarted by `save_async`.
        """
        # Not `flush()`, and must never become one: `close()` and `__exit__`
        # call this, and a context manager that does not exit is worse than a
        # save left to a still-running worker.
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

        pending = self.queue.qsize()
        if pending > 0:
            print(f"Waiting for {pending} pending save operations to complete...")
            get_logger().info(f"Waiting for {pending} pending save operations...")

        self.running = False
        # Wake up if sleeping on queue
        self.queue.put(None)

        self.thread.join(timeout=_SHUTDOWN_JOIN_TIMEOUT_S)
        get_logger().info("PersistenceManager stopped.")
        print("Persistence Manager Stopped.")

    def _drain_recoverable_saves(self):
        """Write what a stopped worker left behind, or report why it cannot.

        With the worker dead, writes its orphaned saves and everything left
        in the queue (`_drain_queued_saves`). With the worker still alive,
        touches nothing and, when anything is outstanding, reports through
        `_report_unreconciled`; that worker still writes what is ahead of and
        behind its sentinel. Never raises.
        """
        # A live worker after the join means a save is genuinely running: its
        # `_inflight` entry is the item it is inside `save_all` with, and
        # saving it here would double-write and race. Its queue is not
        # drained either: a drain that swallowed the sentinel would leave a
        # `while True` worker with nothing to end it. The reported queue depth
        # counts the sentinel too, and `in_flight_items` counts entries under
        # every owner: both are diagnostics for a teardown log, not counts of
        # lost saves.
        #
        # Must never raise: `Session.close()` re-raises the first exception
        # its steps produce, which would replace whatever the user's `with`
        # body was unwinding. No manager lock is held across the `save_all`.
        if self.thread is not None and self.thread.is_alive():
            with self._inflight_lock:
                outstanding = len(self._inflight)
            if outstanding or not self.queue.empty():
                # "Still running" covers two states: the thread may be inside
                # its own post-sentinel drain or inside a user's save. Either
                # way it is the queue's sole consumer and must not be raced,
                # which is the only thing this branch decides, so the message
                # says what is true of both.
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
        """Write every item left in the queue, and count each one off.

        Each item written logs a WARNING; a failed write is reported through
        `_report_unreconciled`. Stale sentinels are skipped and counted off.
        Calls `task_done()` exactly once per item consumed, including each
        item in `already_held`. Never raises.

        Args:
            already_held: Payloads a dead worker took off the queue and never
                counted off, from `_reap_orphans`. Empty when the worker itself
                drains what is behind its sentinel.
        """
        # Two callers: `_drain_recoverable_saves` for a dead worker, passing
        # the orphans it reaped, and the worker itself at its sentinel. The
        # worker never reaps: `_reap_orphans` asks about threads that are
        # dead, which the worker would be asking about itself.
        #
        # `task_done()` exactly once per item consumed: a reaped orphan was
        # `get()`-ed by its dead owner and never counted off, and each item
        # taken below owes one for its `get_nowait()`. Miss one and the next
        # `flush()` never returns; count one too many and `Queue` raises.
        #
        # One pass to `Empty`, never a re-post loop: two sentinels with
        # `running` False would ping-pong forever. An item queued after the
        # pass sees `Empty` arrives through `save_async`, which restarts a
        # worker. `_inflight` is not written during the drain, so a thread
        # killed mid-drain loses the item it holds; closing that needs
        # atomicity inside `Queue`.
        #
        # Must never raise: one caller runs from `Session.close()`, the other
        # must reach its `break` or a `while True` worker is left running.
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
                # Not "shutdown() is writing ...": the other caller is the
                # worker itself, taking its own sentinel, and the log line
                # must not name the wrong thread.
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
                # persistence one. Guarded, "it never raises" is a
                # property of the code rather than of the argument.
                try:
                    self.queue.task_done()
                except ValueError as exc:
                    get_logger().error(
                        "PersistenceManager counted off more tasks than the "
                        f"queue is holding: {describe_exception(exc)} (#314).")

    def _report_unreconciled(self, message):
        """Report, as an ERROR log line and an audit row, that a save did not reach the store.

        The audit row is flushed before returning. Best effort: never raises.

        Args:
            message (str): The text for both the log line and the row.
        """
        # The row is flushed here rather than by a later `stop()`:
        # `_flush_at_exit` calls `shutdown()` with no `stop()` behind it, and
        # there the row would sit in a queue whose daemon writer is about to
        # be stopped at finalization. `flush_audit_queue()` drains on this
        # thread under `_audit_write_lock`, takes no manager lock, and is
        # bounded by one `log_audit_batch`.
        get_logger().error(message)
        try:
            self.store_backend.log_audit("ERROR", "SESSION", message)
            self.store_backend.flush_audit_queue()
        except Exception as exc:  # pylint: disable=broad-except
            get_logger().error(
                f"PersistenceManager could not record the unreconciled "
                f"save in the audit log: {describe_exception(exc)}")
