"""The worker keys its mutation on the UID the *parent* captured (#257).

`execute_redaction_task` used to capture `original_sop_uid` from the
live instance at the top of the worker. Under threads (3.14t's default,
or `ISOCENTER_FORCE_THREADS`) every task's worker shares that instance,
so when two rules target one instance the second worker could read
`inst.sop_instance_uid` *after* the first worker's `regenerate_uid()`:
its mutation came home keyed on a post-redaction UID, the parent's
`instances` map -- deliberately keyed on pre-redaction UIDs (#228) --
matched nothing, and the redaction was discarded by a run that reported
no error. `applied` came back 1 instead of 2, and since #255 the second
pass's `REDACTION` audit row intermittently read `Applied 0 of 1` where
every other run of the same input said `1 of 1`. Identical input,
intermittently different compliance report -- at roughly 1 run in 8.

The fix moves the capture to `prepare_redaction_tasks`: parent-side,
pre-dispatch, race-free, on the task dict alongside the other per-task
state the worker must not re-derive (`config_hash`, `force`). Under
processes nothing changes -- tasks are pickled at dispatch, so the
worker's read of its own copy was already equivalent to the parent's.

The race is probabilistic, so these tests pin its *certain* form
instead: a serial in-process map runs the workers on the parent's own
objects -- the threads path's sharing -- and back-to-back, so the
second task's capture always follows the first task's
`regenerate_uid()`. Measured pre-fix on this tree: `applied=1`,
`('SN_RELOAD', 'Applied 0 of 1 ...')`, and the discard error naming a
UID no rule ever targeted -- every run, not one in 8.
"""
import sqlite3

from isocenter.services import RedactionService

#: Inside the fixture's 32x32 image, so the zone really lands.
IN_IMAGE_ZONE = [0, 8, 0, 8]


def _redaction_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='REDACTION'").fetchall()


def test_the_task_carries_the_uid_the_parent_captured(
        reloaded_redaction_session):
    """The capture is task-preparation state, not worker state.

    This is the structural half of the pin: the task dict carries
    `original_sop_uid`, read in the parent before any worker can have
    moved the live attribute. A worker that trusts this key cannot be
    raced by a sibling's `regenerate_uid()`, whichever executor runs it.
    """
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="capture_shape")
    service = RedactionService(session.store, session.store_backend)

    uid_at_prep = inst.sop_instance_uid
    tasks = service.prepare_redaction_tasks(
        {"serial_number": "SN_RELOAD", "redaction_zones": [IN_IMAGE_ZONE]})

    assert len(tasks) == 1
    assert tasks[0]["original_sop_uid"] == uid_at_prep, (
        "the task must carry the pre-dispatch UID; a worker-side capture "
        "is exactly the racy read #257 removes")


def test_the_worker_trusts_the_task_over_the_shared_object(
        reloaded_redaction_session):
    """A sibling's regenerate_uid() between dispatch and capture is inert.

    The race, one interleaving, made deterministic: the task is prepared
    while the instance still holds its pre-redaction UID, the "first
    worker's" `regenerate_uid()` lands, and only then does this task's
    worker run. Pre-fix the worker re-read the live attribute and keyed
    its mutation on the moved UID; the parent's pre-redaction map
    discarded it.
    """
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="capture_trust")
    service = RedactionService(session.store, session.store_backend)

    uid_at_prep = inst.sop_instance_uid
    tasks = service.prepare_redaction_tasks(
        {"serial_number": "SN_RELOAD", "redaction_zones": [IN_IMAGE_ZONE]})

    # The sibling worker's mutation, landed before this worker starts.
    inst.regenerate_uid()
    assert inst.sop_instance_uid != uid_at_prep, (
        "regenerate_uid() must move the live attribute for this test to "
        "exercise anything")

    outcome = service.execute_redaction_task(tasks[0])

    assert outcome.ok and outcome.mutation is not None
    assert outcome.mutation["original_sop_uid"] == uid_at_prep, (
        "the mutation is keyed on the live attribute, not on the task's "
        "parent-side capture -- the parent's pre-redaction map cannot "
        "match this and discards the redaction (#257)")
    assert outcome.sop_instance_uid == uid_at_prep, (
        "the outcome names an identity the parent never issued; failure "
        "rows built from it would not match any targeted instance")


def test_two_rules_interleaved_on_one_instance_both_land(
        reloaded_redaction_session, monkeypatch):
    """The full pipeline, under the race's certain form: both rules apply.

    A serial map is the threads path's object sharing with the racy
    interleaving guaranteed -- worker two starts only after worker one's
    `regenerate_uid()`. The wildcard and the explicit rule both match
    the one instance (`load_config` de-duplicates nothing, so this is an
    ordinary config); both must apply, and each pass's audit row must
    account for its own application. Pre-fix, every run of this test:
    `applied == 1` and the second row read `Applied 0 of 1`.
    """
    session, _inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="capture_interleaved")
    db_path = session.persistence_file
    session.configuration.rules = [
        {"serial_number": "*", "redaction_zones": [IN_IMAGE_ZONE]},
        {"serial_number": "SN_RELOAD",
         "redaction_zones": [[0, 4, 0, 4], [8, 12, 8, 12]]},
    ]
    monkeypatch.setattr("isocenter.session.run_parallel",
                        lambda fn, items, **_kw: [fn(i) for i in items])

    applied = session.redact(show_progress=False)
    assert applied == 2, (
        "two rules matched one instance and only one application came "
        "home; the other was keyed on a post-redaction UID and "
        "discarded (#257)")
    session.close()

    assert _redaction_rows(db_path) == [
        ("*", "Applied 1 of 1 candidate images with 1 zones"),
        ("SN_RELOAD", "Applied 1 of 1 candidate images with 2 zones"),
    ], (
        "identical input must produce this accounting on every run; "
        "'Applied 0 of 1' here is #257's intermittent under-report, "
        "made deterministic")
