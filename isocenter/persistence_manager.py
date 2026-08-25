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
        If the worker thread has unexpectedly died, it restarts it to drain the queue.
        """
        # Check for silent crash: Worker is dead, but queue has items.
        if (not self.thread or not self.thread.is_alive()) and not self.queue.empty():
            get_logger().warning("PersistenceManager worker was found dead/stopped with pending items during flush. Restarting to process backlog.")
            print("Restarting stopped Persistence Manager to process pending items...")
            self._start_worker()

        # Wait for queue to be empty, regardless of thread state
        # (If thread dies during join, we might hang? No, task_done() is called in finally block)
        # But if thread dies BEFORE get(), item remains.
        # Our modified start_worker handles restart.

        self.queue.join()

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
