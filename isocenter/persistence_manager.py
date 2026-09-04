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
from .logger import get_logger

#: How long `flush()` waits before saying what it is waiting for.
#:
#: `flush()` must not return until the queue is drained (see its
#: docstring), so the only thing left to tune is how long it stays
#: silent about it. This sits below pytest.ini's
#: `faulthandler_timeout = 300`, so a wedged flush explains itself
#: before any traceback dump arrives and the two land in the same log.
_FLUSH_REPORT_INTERVAL_S = 30.0


def _flush_at_exit(manager_ref):
    """Flush a manager at interpreter exit, if it is still alive.

    Module-level and taking a weak reference, so registering it does not
    keep the manager (and its store, threads and file descriptors) alive
    for the life of the process. See the registration site.
    """
    manager = manager_ref()
    if manager is not None:
        manager.shutdown()


class PersistenceManager:
    """
    Offloads persistence operations to a background thread to unblock the main thread.

    This manager:
    - Maintains a queue of patient snapshots to save.
    - Runs a background worker thread (`_worker`) to process the queue.
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
        # Nothing is lost by weakening it, and the reason is structural
        # rather than a judgement call: a manager with a live worker
        # cannot be collected anyway, because the worker thread holds
        # `self` through `target=self._worker`. Only a manager whose
        # worker has already exited becomes collectable, and that manager
        # has nothing left to flush. The ordering this depends on was
        # checked rather than assumed -- `atexit` callbacks run while
        # daemon threads are still alive; interpreter finalization, which
        # stops them, comes afterwards.
        #
        # The registration itself is not undone: the `atexit` list still
        # grows by one small closure per manager. What is reclaimed is the
        # manager graph behind it, which is where the handles and threads
        # are. Unregistering at `shutdown()` was rejected because a
        # shut-down manager is restartable -- `save_async` starts a fresh
        # worker -- and it would lose its exit-time flush.
        atexit.register(_flush_at_exit, weakref.ref(self))
        get_logger().info("PersistenceManager initialized.")

    def _start_worker(self):
        # **The guard is thread liveness, not `self.running`.** `_worker`
        # is a `while True`; setting `running = False` does not end it, and
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
            self.thread = threading.Thread(
                target=self._worker, daemon=True, name="PersistenceWorker")
            self.thread.start()
            get_logger().info("PersistenceManager worker thread started.")

    def flush(self):
        """
        Blocks until all tasks in the queue have been processed.

        This method ensures that any currently queued save operations are completed before returning.
        If the worker thread has unexpectedly died, it recovers whatever it
        was carrying and restarts it to drain the queue.

        **It never returns early.** A bounded wait that gave up and
        returned would turn a visible hang into a silently dropped save,
        which is strictly worse: callers flush precisely so they can read
        or shut down afterwards. What is bounded is the *silence* -- every
        `_FLUSH_REPORT_INTERVAL_S` a wait that has not finished says what
        it is waiting for and re-attempts recovery, so a worker that dies
        after this flush began is recovered too.
        """
        self._recover_orphaned_item()

        # `queue.join()` on a short-lived daemon so the wait can be
        # interrupted periodically to report and re-check. Cost is one
        # extra thread per `flush()` CALL on a healthy flush, and one more
        # per report interval while it is still waiting -- earlier waiters
        # stay blocked until the queue drains, so a wedged flush
        # accumulates one thread every 30s. Never per queued item:
        # `Session` calls `flush()` from exactly two places, `audit()`
        # and `redact()`, and not on the save path. (`close()` does NOT
        # flush -- it calls `shutdown()`, which returns immediately when
        # the worker is already dead. So an orphan reaching `close()`
        # is dropped silently rather than hanging it.)
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

        Takes `_inflight_lock` and nothing else, and calls nothing.
        That is deliberate and is what lets `_worker` call it (see the
        comment at its head) without the reentrancy that calling
        `_recover_orphaned_item` there would need.
        """
        with self._inflight_lock:
            return [self._inflight.pop(owner)
                    for owner in list(self._inflight)
                    if not owner.is_alive()]

    def _requeue_orphans(self, orphaned):
        """Put reaped payloads back, `put()` **then** `task_done()`.

        One spelling for that ordering, because it has two callers and
        the ordering is the load-bearing part. Reversed,
        `unfinished_tasks` reaches zero between the two calls, a waiting
        `queue.join` wakes, and `flush()` can return before the payload
        is back in the deque -- a dropped save wearing a clean return.
        In this order the net count is unchanged and the payload is
        queued before the orphan is counted off.
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
        `self.thread` instead reads as the same thing only while the
        manager has had exactly one worker ever: as soon as a save
        arrives after the orphaning -- `save_async` restarts the worker
        -- `self.thread` is alive, recovery becomes a permanent no-op,
        and the flush hangs with the orphan unreachable. Per-owner, a
        live worker's own entry is still never touched, so nothing is
        traded away for that.

        The reap and the re-queue live in `_reap_orphans` and
        `_requeue_orphans`, which `_worker` also calls on startup
        (#315); the `put()`-then-`task_done()` ordering is argued at the
        latter, in one place because it has two callers.

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

    def _worker(self):
        # **Reap before consuming anything, on the newly started thread.**
        # A worker restart is the event that always accompanies a
        # recovery being needed -- `save_async` starts one precisely
        # because it found the previous worker dead -- so this is where
        # recovery always runs, rather than only where `flush()` happens
        # to look. Before #315 `_recover_orphaned_item` was reachable
        # from `flush()` and nowhere else, and `Session` flushes from
        # exactly two places; a session that saved, lost a worker and
        # then closed never reached it.
        #
        # **`_reap_orphans()`, never `_recover_orphaned_item()`.** The
        # latter takes `_recover_lock` and then calls `_start_worker`,
        # which takes `_worker_lock`; putting *that* in `_start_worker`
        # instead would give `_recover_lock` -> `_worker_lock` ->
        # `_recover_lock` on a non-reentrant lock. Calling the reap --
        # which takes `_inflight_lock` alone and restarts nothing, since
        # this thread *is* the restart -- makes that impossible by
        # construction rather than by a liveness argument about
        # `self.thread`.
        self._requeue_orphans(self._reap_orphans())

        while True:
            try:
                # Wait for work
                item = self.queue.get(timeout=1.0)

                # Record the item before anything can fail, under this
                # thread's own key. From here to the `finally` below,
                # this entry is the only reference to the payload that a
                # `flush()` can reach: the deque no longer holds it
                # (#309). Keying on the thread rather than writing a
                # single slot is what stops a *restarted* worker erasing
                # a dead one's orphan on its way past.
                if item is not None:
                    with self._inflight_lock:
                        self._inflight[threading.current_thread()] = item

                # If we get a sentinel (None), we exit
                if item is None:
                    if self.running:
                        # Stale sentinel from previous shutdown - ignore it
                        self.queue.task_done()
                        continue
                    self.queue.task_done()
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
                    self.store_backend.save_all(
                        patients, prune_absent_patients=prune_absent_patients)
                except Exception as e:
                    get_logger().error(f"Background save failed: {e}")
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
                    with self._inflight_lock:
                        self._inflight.pop(threading.current_thread(), None)
                    self.queue.task_done()

            except queue.Empty:
                # Check exit condition periodically if using timeout,
                # but we rely on sentinel for clean shutdown.
                # However, if running becomes False (force kill?) and no sentinel?
                # shutdown() sends sentinel.
                if not self.running and self.queue.empty():
                    # Fallback exit? No, stick to sentinel.
                    pass
                continue
            except Exception as e:
                get_logger().error(f"Worker crashed: {e}")

    def shutdown(self):
        """
        Stops the worker thread gracefully.

        Waits for any pending operations to complete (with a timeout) before
        killing the thread (via sentinel and join).
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

        self.thread.join(timeout=30)
        get_logger().info("PersistenceManager stopped.")
        print("Persistence Manager Stopped.")
