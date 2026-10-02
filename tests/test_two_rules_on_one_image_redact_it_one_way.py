"""An image two redaction rules cover is redacted once, with every zone, whatever the task order (#908).

**The leak.** `redact()` built one task per rule, so an image covered by
two rules got two tasks. That is an exact rule plus `"*"`, or two rules on
one serial (`load_config` de-duplicates nothing). Each task zeroed only
its own rule's zones on a frame read before the other task's write landed,
and the frame the store kept was one task's. The other rule's zones stayed
in the store's pixels.
- Under processes (the GIL default) that happened every time: each worker
  redacts a pickled copy loaded from the pre-pass frame.
- Under threads (3.14t's default, or forced) it was a race. Review of
  #909 measured a 2048x2048 image: 38 of 40 runs on 3.12 lost a zone. On
  3.14t's default strategy, 3 of 20 lost one, and 16 of 20 left the loader
  and the pixel hash from different workers, which raised `Pixel data hash
  mismatch` at the next read. The 32x32 image here is too small to lose
  that race, which is why the `shared` arm below runs the tasks one after
  the other and models the sharing, not the race.

**When the leak reached a file.** Up to 0.9.7 an ordinary export in the
same session leaked, because export applied only the first exact rule
(#580). From 0.9.8 on, an export zeroes every matching rule's zones from
the rules loaded when it runs. It leaked only without those rules: a
session reopened without `load_config()`, rules changed or cleared before
the export, or `DicomExporter.write_tree()`.

**The UID and the attestation** depended on the same order: each task
derived a redacted SOP Instance UID from its own rule's zones
(`privacy._redaction_uid_for`, #544). dev-b4 found it when the behaviour
digest's `"*"` scenario (#899) hashed differently on 3.12 and 3.14t. A
second `redact()` then found one rule's hash, redacted again and moved the
UID.

The task order is forced here, never left to timing. `run_parallel` is
replaced by a map that runs each task in this process, in task order or
reversed, either on the live instance (the threads path's sharing) or on a
pickled copy of the task (what a process worker is handed, and what a
thread that lost the race had read).
"""
import glob
import hashlib
import pickle
import sqlite3

import numpy as np
import pydicom
import pytest

from isocenter.session import DicomSession
from support.project_secret import load_fixed_secret

#: Two zones inside the fixture's 32x32 image, apart, so each rule's zones
#: can be seen landing or not.
STAR_ZONE = [0, 8, 0, 8]
EXACT_ZONE = [16, 24, 16, 24]
UID = "1.2.3.908"


def _rules():
    return [{"serial_number": "*", "redaction_zones": [list(STAR_ZONE)]},
            {"serial_number": "SN_RELOAD", "redaction_zones": [list(EXACT_ZONE)]}]


def _serial_map(reverse, isolate):
    """A `run_parallel` stand-in: every task in this process, in task order
    or reversed; with `isolate`, on a pickled copy, as a process worker is
    handed it."""
    def run(fn, items, **_kw):
        items = list(items)
        if reverse:
            items.reverse()
        return [fn(pickle.loads(pickle.dumps(item)) if isolate else item)
                for item in items]
    return run


def _the_instance(session):
    return next(inst
                for patient in session.store.patients
                for study in patient.studies
                for series in study.series
                for inst in series.instances)


def _redact(make, monkeypatch, name, reverse, isolate):
    session, _inst = make([list(EXACT_ZONE)], uid=UID, name=name)
    load_fixed_secret(session)
    session.configuration.rules = _rules()
    monkeypatch.setattr("isocenter.session.run_parallel",
                        _serial_map(reverse, isolate))
    applied = session.redact(show_progress=False)
    return session, applied


def _both_orders(make, monkeypatch, isolate):
    """`(applied, uid, frame)` per order, tasks in order then reversed, in
    two stores under one secret."""
    runs = []
    for reverse in (False, True):
        session, applied = _redact(make, monkeypatch,
                                   f"order_{isolate}_{reverse}", reverse, isolate)
        inst = _the_instance(session)
        arr = np.array(inst.get_pixel_data())
        runs.append((applied, inst.sop_instance_uid, arr))
        session.close()
    return runs


ISOLATION = pytest.mark.parametrize("isolate", [False, True], ids=["shared", "pickled"])


@ISOLATION
def test_either_task_order_gives_one_uid(
        reloaded_redaction_session, monkeypatch, isolate):
    """One image, the same two rules: the same SOP Instance UID whichever
    task runs last."""
    (_, in_order, _), (_, reversed_, _) = _both_orders(
        reloaded_redaction_session, monkeypatch, isolate)
    assert in_order == reversed_, (
        f"the tasks in order gave {in_order}, reversed {reversed_}: the "
        "redacted UID is the last task's, not the image's (#908)")


@ISOLATION
def test_either_task_order_zeroes_both_rules_zones(
        reloaded_redaction_session, monkeypatch, isolate):
    """Every zone of both rules is zeroed, whichever task runs last, and
    the two frames are one frame."""
    runs = _both_orders(reloaded_redaction_session, monkeypatch, isolate)
    for (_, _, arr), order in zip(runs, ("in order", "reversed")):
        for y1, y2, x1, x2 in (STAR_ZONE, EXACT_ZONE):
            assert not arr[y1:y2, x1:x2].any(), (
                f"zone {[y1, y2, x1, x2]} is not zeroed with the tasks "
                f"{order}: the image kept one rule's frame and lost the "
                "other rule's zones")
        assert arr[30, 30] == 200, "a pixel outside every zone was touched"
    assert (hashlib.sha256(runs[0][2].tobytes()).hexdigest()
            == hashlib.sha256(runs[1][2].tobytes()).hexdigest())


@ISOLATION
def test_one_image_redacted_by_two_rules_counts_once(
        reloaded_redaction_session, monkeypatch, isolate):
    """`redact()` returns how many instances had a zone applied."""
    for applied, _, _ in _both_orders(reloaded_redaction_session, monkeypatch, isolate):
        assert applied == 1, (
            f"redact() returned {applied}: it counts instances with a zone "
            "applied, and one image was redacted")


def test_a_store_reopened_without_its_rules_exports_both_rules_zones(
        reloaded_redaction_session, monkeypatch, tmp_path):
    """The leak's way out. An export applies the zones of the rules loaded
    when it runs, so with the same rules still loaded it zeroed the lost
    zone itself. Redact, `save()`, reopen and export with no configuration
    loaded is the documented two-session workflow, and that export writes
    the store's pixels as they are. Measured before #908 with the tasks
    run on pickled copies: the `"*"` rule's zone was exported with its
    pixels, the burned-in text the configuration names."""
    session, _applied = _redact(reloaded_redaction_session, monkeypatch,
                                "reopen", False, True)
    session.save(sync=True)
    db_path = session.persistence_file
    session.close()
    with DicomSession(db_path) as reopened:
        assert not reopened.configuration.rules, (
            "the reopened session holds rules, so its export would zero the "
            "zones itself and this test would ask nothing")
        out = tmp_path / "export"
        reopened.export(str(out), show_progress=False)
    files = glob.glob(str(out / "**" / "*.dcm"), recursive=True)
    assert len(files) == 1, files
    arr = pydicom.dcmread(files[0]).pixel_array
    for y1, y2, x1, x2 in (STAR_ZONE, EXACT_ZONE):
        assert not arr[y1:y2, x1:x2].any(), (
            f"zone {[y1, y2, x1, x2]} reached the export unredacted: the "
            "store kept one rule's frame (#908)")


def test_a_rule_added_after_a_pass_reaches_the_pixels(
        reloaded_redaction_session, monkeypatch):
    """A store redacted under the exact rule, then given `"*"` and
    redacted again: the second pass must see that the image's attestation
    names fewer zones than now cover it, and apply both. A folded task
    attested with its first rule's hash would match the stored one, skip
    the image, and leave `"*"`'s zone in the store (review of #909)."""
    session, _inst = reloaded_redaction_session(
        [list(EXACT_ZONE)], uid=UID, name="added")
    load_fixed_secret(session)
    monkeypatch.setattr("isocenter.session.run_parallel", _serial_map(False, True))
    session.configuration.rules = _rules()[1:]
    assert session.redact(show_progress=False) == 1
    session.configuration.rules = [_rules()[1], _rules()[0]]
    assert session.redact(show_progress=False) == 1, (
        "the second pass skipped the image: its attestation was taken for "
        "the whole set of zones that now cover it")
    arr = np.asarray(_the_instance(session).get_pixel_data())
    for y1, y2, x1, x2 in (STAR_ZONE, EXACT_ZONE):
        assert not arr[y1:y2, x1:x2].any(), f"zone {[y1, y2, x1, x2]} is not zeroed"


@pytest.mark.parametrize("listing", ["zones", "rules"])
def test_the_uid_does_not_depend_on_how_the_zones_are_listed(
        reloaded_redaction_session, monkeypatch, listing):
    """One set of zones gives one UID however the configuration lists it:
    one rule's zones in either order, or the two rules in either order.
    Zeroing is commutative, so the order cannot change the pixels, and the
    hash sorts the zones (`services._redaction_config_hash`) so it cannot
    change the UID or the attestation either. Without the sort, reordering
    a file's zones would re-redact the store and move every UID."""
    if listing == "zones":
        layouts = ([{"serial_number": "SN_RELOAD",
                     "redaction_zones": [list(EXACT_ZONE), list(STAR_ZONE)]}],
                   [{"serial_number": "SN_RELOAD",
                     "redaction_zones": [list(STAR_ZONE), list(EXACT_ZONE)]}])
    else:
        layouts = (_rules(), _rules()[::-1])
    uids = []
    for n, rules in enumerate(layouts):
        session, _inst = reloaded_redaction_session(
            [list(EXACT_ZONE)], uid=UID, name=f"listed_{listing}_{n}")
        load_fixed_secret(session)
        session.configuration.rules = rules
        monkeypatch.setattr("isocenter.session.run_parallel",
                            _serial_map(False, False))
        session.redact(show_progress=False)
        uids.append(_the_instance(session).sop_instance_uid)
        session.close()
    assert uids[0] == uids[1], (
        f"the same zones listed two ways gave {uids}: the hash reads the "
        "order they are listed in")


def test_the_uid_names_both_rules_zones(reloaded_redaction_session, monkeypatch):
    """The UID is derived over every zone applied, not one rule's: the same
    image under each rule alone takes another UID than under both. A
    folded task that kept its first rule's UID would pass the order tests
    above, since the first rule is the same in either order."""
    uids = {}
    for name, rules in (("both", _rules()), ("star", _rules()[:1]),
                        ("exact", _rules()[1:])):
        session, _inst = reloaded_redaction_session(
            [list(EXACT_ZONE)], uid=UID, name=f"set_{name}")
        load_fixed_secret(session)
        session.configuration.rules = rules
        monkeypatch.setattr("isocenter.session.run_parallel",
                            _serial_map(False, False))
        session.redact(show_progress=False)
        uids[name] = _the_instance(session).sop_instance_uid
        session.close()
    assert len(set(uids.values())) == 3, (
        f"{uids}: the UID under both rules equals one rule's alone, so it "
        "does not name the zones the image was redacted with")


def test_a_second_redact_skips_an_image_two_rules_redacted(
        reloaded_redaction_session, monkeypatch):
    """The attestation names the whole set of zones applied, so a second
    pass finds it and skips, and the UID stays where the first put it."""
    session, _applied = _redact(reloaded_redaction_session, monkeypatch,
                                "again", False, False)
    first = _the_instance(session).sop_instance_uid
    assert session.redact(show_progress=False) == 0, (
        "the second pass redacted again: the attestation is one rule's, "
        "so the other rule's task did not recognise it")
    assert _the_instance(session).sop_instance_uid == first


def test_each_rule_still_writes_its_own_row(
        reloaded_redaction_session, monkeypatch):
    """One task for the image, and still one REDACTION row per rule-pass,
    each counting the image once (#255)."""
    session, _applied = _redact(reloaded_redaction_session, monkeypatch,
                                "rows", False, True)
    db_path = session.persistence_file
    session.close()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT entity_uid, details FROM audit_log "
            "WHERE action_type='REDACTION'").fetchall()
    assert rows == [
        ("*", "Applied 1 of 1 candidate images with 1 zones"),
        ("SN_RELOAD", "Applied 1 of 1 candidate images with 1 zones"),
    ], rows
