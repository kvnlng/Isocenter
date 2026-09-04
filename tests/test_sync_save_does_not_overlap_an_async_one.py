"""`save(sync=True)` must not run alongside the save it is standing in for (#294).

`Session.save(sync=True)` called `store_backend.save_all` directly on the
caller's thread, with no regard for what the persistence manager's worker
was doing. The worker may be inside `save_all` over the same object graph
at that moment -- `save()` (async) followed by `save(sync=True)` is an
ordinary sequence, and `compact()` opens with a synchronous save of its
own -- so two `save_all` calls run concurrently over one graph and one
sidecar. Since #287 the two sidecar prepasses run fully in parallel, which
turns #287's bound on orphaned frames from a per-store property into a
per-concurrent-save one.

The fix is the shape `audit()` and `redact()` already use: drain the
manager first. "Synchronous" then means what it says.

The handshake is an Event pair rather than a sleep. The negative
assertion (`not sync_done.wait(0.5)`) can only fail towards a false
*pass* on a slow machine -- the only way it is set inside that window is
a synchronous save that returned while the async one was provably still
parked inside `save_all`.
"""
import threading

from isocenter.entities import Patient
from isocenter.session import DicomSession


def _on_helper(fn):
    done = threading.Event()

    def run():
        try:
            fn()
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def test_a_sync_save_waits_for_the_async_one_already_running(tmp_path):
    session = DicomSession(str(tmp_path / "overlap"))
    try:
        session.store.patients.append(Patient("P_OVERLAP", "Overlap^Test"))

        events = []
        events_lock = threading.Lock()
        async_inside, release = threading.Event(), threading.Event()
        real_save_all = session.store_backend.save_all

        def recorder(patients, prune_absent_patients=False):
            # Tagged by thread, not by call order: session setup may
            # already have driven a save_all through, and a miscounted
            # first call turns the event list into noise.
            tag = ("async"
                   if threading.current_thread()
                   is session.persistence_manager.thread else "sync")
            with events_lock:
                events.append(f"enter:{tag}")
            if tag == "async" and not async_inside.is_set():
                async_inside.set()
                release.wait(10)
            try:
                return real_save_all(
                    patients, prune_absent_patients=prune_absent_patients)
            finally:
                with events_lock:
                    events.append(f"exit:{tag}")

        session.store_backend.save_all = recorder

        session.save()
        assert async_inside.wait(5), (
            "the background save never entered save_all, so the window "
            "this test measures never opened")

        sync_done = _on_helper(lambda: session.save(sync=True))
        assert not sync_done.wait(0.5), (
            "save(sync=True) returned while a background save was still "
            "parked inside save_all: two save_all calls ran concurrently "
            "over one graph and one sidecar (#294)")

        release.set()
        assert sync_done.wait(15), (
            "save(sync=True) never returned after the background save "
            "was released")

        with events_lock:
            observed = list(events)
        assert observed == ["enter:async", "exit:async",
                            "enter:sync", "exit:sync"], (
            f"the two saves interleaved: {observed} (#294)")
    finally:
        session.close()
