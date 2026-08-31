"""A rule with N zones applied only its Nth zone to a reloaded instance.

`SidecarPixelLoader` builds its array with `np.frombuffer` over an
immutable `bytes` buffer, so every instance loaded from a saved store
hands back a **read-only** array. The redaction wrapper copied that array
and rebound a *local* name; the caller's `for roi in rois:` loop then
handed it the pristine original again on the next zone, so zone 2 copied
the untouched source afresh and `set_pixel_data` discarded everything
zone 1 had written.

Nothing raised. `modified` was True, the `_ISOCENTER_REDACTION_HASH` was
written, `redact()` reported every image updated, the half-redacted array
reached the sidecar, the burned-in identifier reached the exported file,
and `generate_report()` graded the run `PASS`. That was #229.

Every redaction fixture in the suite was either built in memory or read
through pydicom from a source file, and **both give a writeable array**,
where the code takes its other arm, mutates in place, and is correct.
There was no redaction test anywhere that exercised a reloaded instance,
which is the whole of why this survived. `reloaded_redaction_session`
(`tests/conftest.py`) is that shape, and these tests are what it is for.
"""
import numpy as np
import pydicom
import pytest

from isocenter.entities import Instance
from isocenter.services import RedactionService
from isocenter.session import DicomSession

#: Two disjoint zones on a 32x32 image, applied in config order.
TWO_ZONES = [[0, 8, 0, 8], [16, 24, 16, 24]]

#: 8*8*200 -- what zone 1's region sums to when nothing touched it.
PRISTINE_ZONE_SUM = 8 * 8 * 200


def _zone_sums(arr):
    return (int(arr[0:8, 0:8].sum()), int(arr[16:24, 16:24].sum()))


# --- T1 --------------------------------------------------------------------

def test_every_zone_of_a_rule_is_applied_on_the_parallel_path(
        reloaded_redaction_session):
    """`session.redact()` applies all of a rule's zones, not just the last.

    On `4507d48` this is red with `zone1 == 12800` -- the region is
    untouched and still carries whatever was burned into it.

    **Both gate interpreters, and the threads leg is not redundant.**
    `redact()` takes processes on 3.12 and threads on 3.14t, but the
    defect is name rebinding inside one stack frame, so both executors
    reproduce it identically and both must be seen to be fixed. Measured
    red on 3.12.13 and on 3.14.7t.
    """
    session, inst = reloaded_redaction_session(TWO_ZONES)

    updated = session.redact(show_progress=False)
    assert updated == 1, (
        "no image matched the rule; the assertions below would be vacuous")

    inst.unload_pixel_data()
    zone1, zone2 = _zone_sums(inst.get_pixel_data())
    assert zone2 == 0, "the last zone was not applied at all"
    assert zone1 == 0, (
        f"zone 1 survived the redaction with sum {zone1}: the copy taken "
        "for zone 2 was made from the pristine original (#229)")


# --- T2 --------------------------------------------------------------------

def test_every_zone_of_a_rule_is_applied_on_the_serial_path(
        reloaded_redaction_session):
    """The same, through `redact_machine_instances`.

    The serial path is public API -- `process_machine_rules` calls it and
    #213 tests it directly -- and it carried its **own copy** of the
    per-zone loop. A fix applied only to `execute_redaction_task` passes
    the parallel test above and leaves this one red.

    This path never leaves the calling thread, so both interpreters
    behave identically. On `4507d48`: red, `zone1 == 12800`.
    """
    session, inst = reloaded_redaction_session(TWO_ZONES, name="serial")

    service = RedactionService(session.store, session.store_backend)
    service.redact_machine_instances(
        "SN_RELOAD", [tuple(z) for z in TWO_ZONES],
        targets=[inst], show_progress=False)

    inst.unload_pixel_data()
    zone1, zone2 = _zone_sums(inst.get_pixel_data())
    assert zone2 == 0, "the last zone was not applied at all"
    assert zone1 == 0, (
        f"zone 1 survived the redaction with sum {zone1} on the serial "
        "path (#229)")


# --- T3 --------------------------------------------------------------------

def test_every_zone_reaches_the_store_and_the_exported_file(
        reloaded_redaction_session, tmp_path):
    """`redact()` -> `save()` -> reopen -> `export()`, the route that leaks.

    **The reopen is what makes this test say anything the in-memory ones
    do not.** `_export_instance_worker` re-applies `ctx.redaction_zones`
    in a single `apply_redaction_to_array` call, so an export running
    under the same configuration silently repairs the damage on the way
    out -- and that repair is what hid #229. A second session that loaded
    the store carries **no rules** (asserted below, so this is structural
    rather than a `configuration.rules = []` line someone can tidy away),
    which is one of the four ordinary routes where the repair is
    unavailable. The others: `DicomExporter.write_tree()` (#78, applies no
    zones at all), a serial that no longer matches at export time, and
    simply *reading* the saved store.

    So the store is asserted before the export is, and it is the stronger
    of the two: `save()` persists the array to the sidecar, so a
    half-redacted array there carries the burned-in identifier whatever a
    later export does.

    The `len(files) == 1` assertion comes before any pixel assertion.
    `export()` does not raise when an instance fails module validation, so
    a non-exportable fixture leaves an empty tree and everything below it
    would be skipped rather than run.

    `export()` uses processes on **every** interpreter (#185), so only the
    `redact()` half differs between 3.12 and 3.14t -- and it does not.
    On `4507d48`: red, `zone1 == 12800` in the store and on disk, under a
    report grading `PASS`.
    """
    session, _inst = reloaded_redaction_session(TWO_ZONES, name="ondisk")
    db_path = session.store_backend.db_path

    assert session.redact(show_progress=False) == 1
    session.save()
    session.close()

    reopened = DicomSession(db_path)
    try:
        assert not reopened.configuration.rules, (
            "the reopened session carries rules, so `export()` below would "
            "re-apply the zones and repair the damage under test")

        stored = next(i
                      for p in reopened.store.patients
                      for st in p.studies
                      for se in st.series
                      for i in se.instances)
        zone1, zone2 = _zone_sums(stored.get_pixel_data())
        assert zone2 == 0
        assert zone1 == 0, (
            f"the saved store holds zone 1 with sum {zone1}: the sidecar "
            "carries the burned-in identifier (#229)")

        out_dir = tmp_path / "export_ondisk"
        reopened.export(str(out_dir), use_compression=False,
                        show_progress=False)
    finally:
        reopened.close()

    files = sorted(out_dir.rglob("*.dcm"))
    assert len(files) == 1, (
        f"the fixture did not export; a pixel assertion over {files} is "
        "not an assertion")

    arr = pydicom.dcmread(str(files[0])).pixel_array
    zone1, zone2 = _zone_sums(arr)
    assert zone2 == 0
    assert zone1 == 0, (
        f"the exported file carries zone 1 with sum {zone1}: a burned-in "
        "identifier reached disk under a run that reported success (#229)")


# --- T4 --------------------------------------------------------------------

def test_a_reloaded_instance_is_copied_exactly_once(
        reloaded_redaction_session, monkeypatch):
    """Three zones, one copy. Detection, with its limits measured.

    A full-array copy per zone is the mechanism of #229 and also a cost
    the lazy-pixel design exists to avoid: on a 100GB dataset, N zones
    meant N copies of every image. This pins the copy count directly
    rather than inferring it from the pixels.

    **What it does not catch, measured rather than assumed.** The spec
    claimed this was the clause that fails when someone fixes the
    aliasing but keeps a per-zone loop. It is not: hoisting the
    writeability copy into the callers and keeping a per-ROI wrapper
    makes the counter read `1` and leaves this green, along with T1-T3 --
    verified by applying exactly that shape. This test goes red on the
    same mutation T1-T3 do (the counter reads `3`), and its value is the
    directness of the signal, not selectivity the others lack.

    The **serial** path is used deliberately: it runs in the calling
    thread on every interpreter, so a parent-side wrap of
    `Instance.set_pixel_data` is visible. A wrap around `session.redact()`
    would be invisible inside a spawned child on 3.12.

    On `4507d48`: red, the counter reads 3.
    """
    session, inst = reloaded_redaction_session(
        [[0, 8, 0, 8], [16, 24, 16, 24], [24, 32, 0, 8]], name="onecopy")

    calls = []
    original = Instance.set_pixel_data

    def counted(self, array):
        calls.append(self.sop_instance_uid)
        return original(self, array)

    monkeypatch.setattr(Instance, "set_pixel_data", counted)

    service = RedactionService(session.store, session.store_backend)
    service.redact_machine_instances(
        "SN_RELOAD", [(0, 8, 0, 8), (16, 24, 16, 24), (24, 32, 0, 8)],
        targets=[inst], show_progress=False)

    assert len(calls) == 1, (
        f"the read-only array was copied {len(calls)} times for three "
        "zones; the per-zone loop is still there (#229)")

    inst.unload_pixel_data()
    arr = inst.get_pixel_data()
    assert int(arr[0:8, 0:8].sum()) == 0
    assert int(arr[16:24, 16:24].sum()) == 0
    assert int(arr[24:32, 0:8].sum()) == 0
    assert int(arr.sum()) == 32 * 32 * 200 - 3 * PRISTINE_ZONE_SUM, (
        "more or less than the three zones was zeroed")


# --- the fixture's own guard ------------------------------------------------

def test_the_reloaded_fixture_really_is_read_only(reloaded_redaction_session):
    """Selectivity guard on the fixture, not on #229.

    Green on `4507d48` and green after; it cannot reproduce the defect.
    Its job is to fail loudly if `reloaded_redaction_session` ever stops
    handing back a `SidecarPixelLoader` array -- at which point T1-T4
    would go vacuous rather than red, which is how #229 lasted this long.
    """
    _session, inst = reloaded_redaction_session(TWO_ZONES, name="guard")
    arr = inst.get_pixel_data()
    assert isinstance(arr, np.ndarray)
    assert arr.flags.writeable is False
    assert int(arr.sum()) == 32 * 32 * 200


def test_a_rule_that_applied_nothing_returns_False_and_attests_nothing(
        reloaded_redaction_session):
    """Guard on the one bit the redaction attestation hangs on.

    Selectivity guard, not evidence for #229: it needs
    `_redact_instance_pixels` to exist, so it cannot be run against
    `4507d48` at all, and it is green under the multi-zone defect.

    It is here because that method's **return value** was the one thing
    the whole suite did not check. Measured: replacing its body's
    `return self.apply_redaction_to_array(...)` with a call plus an
    unconditional `return True` left **964 passed, 1 skipped** -- the
    entire suite green -- while a rule whose zones are all off-image
    gained a real `_ISOCENTER_REDACTION_HASH` and `BurnedInAnnotation`
    `NO` on an image whose pixels were never touched. That bool gates
    `_apply_redaction_flags`, `regenerate_uid` and the hash in both
    callers, so a wrong `True` there is an attestation that a redaction
    happened when none did.

    A zone starting past the edge describes nothing to redact and
    `apply_redaction_to_array` skips it without raising, so this is the
    reachable shape of "nothing applied". The *reporting* of that run --
    `applied: 1` and `(0028,0301)` written as `None` -- is #235 and is
    deliberately not asserted here; only the return value and the absence
    of the attestation are, both of which #235's fix leaves alone.
    """
    zones = [[100, 200, 100, 200], [200, 300, 200, 300]]
    rois = [tuple(z) for z in zones]

    # Two instances, one per half. Calling the method directly leaves the
    # instance holding a *writeable* copy on the not-writeable arm, so
    # reusing it below would run the second half against the other arm.
    session, direct = reloaded_redaction_session(zones)
    arr = direct.get_pixel_data()
    assert arr.flags.writeable is False, "vacuous on a writeable array"

    service = RedactionService(session.store, session.store_backend)
    assert service._redact_instance_pixels(direct, arr, rois) is False, (
        "every zone was off-image and nothing was redacted, but the method "
        "reported a modification -- its callers write a redaction hash on "
        "that bool")

    session2, inst = reloaded_redaction_session(zones)
    assert inst.get_pixel_data().flags.writeable is False
    inst.unload_pixel_data()
    service2 = RedactionService(session2.store, session2.store_backend)
    service2.redact_machine_instances(
        "SN_RELOAD", rois, targets=[inst], show_progress=False)

    assert not inst.attributes.get("_ISOCENTER_REDACTION_HASH"), (
        "an image nothing was applied to carries a redaction hash")
    inst.unload_pixel_data()
    assert int(inst.get_pixel_data().sum()) == 32 * 32 * 200, (
        "something was zeroed after all; this test is not asking its "
        "question")
