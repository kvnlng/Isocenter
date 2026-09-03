"""
Persistence manager for handling background save operations.
"""
import threading
import queue
import atexit
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

        # The item the worker took off the queue and has not finished.
        # `queue.get()` removes the payload from the deque while leaving
        # `unfinished_tasks` at 1, so a worker that dies in that window
        # takes the only remaining copy of the save with it and leaves a
        # count nothing can decrement. This field is that copy (#309).
        self._inflight = None
        self._inflight_lock = threading.Lock()
        # Serialises recovery so two flushes cannot re-queue one payload
        # twice, which would both duplicate the save and unbalance the
        # queue's count.
        self._recover_lock = threading.Lock()

        self._start_worker()

        atexit.register(self.shutdown)
        get_logger().info("PersistenceManager initialized.")

    def _start_worker(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._worker, daemon=True)
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
        or close afterwards. What is bounded is the *silence* -- every
        `_FLUSH_REPORT_INTERVAL_S` a wait that has not finished says what
        it is waiting for and re-attempts recovery, so a worker that dies
        after this flush began is recovered too.
        """
        self._recover_orphaned_item()

        # `queue.join()` on a short-lived daemon so the wait can be
        # interrupted periodically to report and re-check. One extra
        # thread per `flush()` call, not per queued item: `flush()` is
        # called by audit(), redact() and close(), not on the save path.
        while True:
            waiter = threading.Thread(target=self.queue.join, daemon=True)
            waiter.start()
            waiter.join(timeout=_FLUSH_REPORT_INTERVAL_S)
            if not waiter.is_alive():
                return

            alive = bool(self.thread and self.thread.is_alive())
            with self._inflight_lock:
                inflight = self._inflight is not None
            # `unfinished_tasks` is a CPython implementation detail and is
            # read here for the message only. Nothing in the recovery path
            # depends on it, and nothing anywhere touches `all_tasks_done`.
            get_logger().warning(
                "PersistenceManager.flush() has waited "
                f"{_FLUSH_REPORT_INTERVAL_S:g}s: "
                f"unfinished_tasks={self.queue.unfinished_tasks}, "
                f"worker_alive={alive}, in_flight_item={inflight} (#309)")
            self._recover_orphaned_item()

    def _recover_orphaned_item(self):
        """Put back a save whose worker took it and never finished it.

        Only ever runs against a **dead** worker. A dead `Thread` is a
        stable observation -- it cannot resume -- whereas a live worker
        holding `_inflight` is simply mid-save, and re-queueing under it
        would both duplicate the write and unbalance the queue's count.
        This gate is also why a second `flush()` after a recovery is a
        no-op: the first one restarted the worker.

        The re-queue is deliberately `put()` **then** `task_done()`.
        Reversed, `unfinished_tasks` reaches zero between the two calls,
        a waiting `queue.join` wakes, and `flush()` can return before
        the payload is back in the deque -- a dropped save wearing a
        clean return. In this order the net count is unchanged and the
        payload is queued before the orphan is counted off.
        """
        with self._recover_lock:
            if self.thread is not None and self.thread.is_alive():
                return

            with self._inflight_lock:
                payload, self._inflight = self._inflight, None

            if payload is not None:
                get_logger().warning(
                    "PersistenceManager worker stopped holding a save it "
                    "never finished; re-queueing it (#309).")
                self.queue.put(payload)
                self.queue.task_done()

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
        while True:
            try:
                # Wait for work
                item = self.queue.get(timeout=1.0)

                # Record the item before anything can fail. From here to
                # the `finally` below, this field is the only reference to
                # the payload that a `flush()` can reach: the deque no
                # longer holds it (#309).
                if item is not None:
                    with self._inflight_lock:
                        self._inflight = item

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
                    # Cleared *before* `task_done()`: once the count drops
                    # a waiting flush can return, and it must not find a
                    # stale item to re-queue behind it.
                    with self._inflight_lock:
                        self._inflight = None
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
