"""The redaction attestation is earned by a zone landing, or not written.

Five signals say "this instance was redacted": the `redact()` count,
`_ISOCENTER_REDACTION_HASH`, `(0028,0301)` BurnedInAnnotation,
`(0008,0008)`/`(0008,2111)`/`(0008,9215)`, and the regenerated SOP
Instance UID. On `84113ab` a rule whose zones all start past the edge of
the image earned four of the five anyway, because
`execute_redaction_task` built its mutation dict **outside**
`if modified:` -- so `redact()` reported `applied: 1`, and the parent
copied a mutation whose attribute values were all `None` onto an
instance nothing had touched. `(0028,0301)` reached the exported file as
a **zero-length CS element**, and BurnedInAnnotation's enumerated values
are `YES` and `NO` (PS3.3 C.7.6.1.1.6); zero length is neither.

`redact_machine_instances`, the serial path, never had that divergence:
it builds no mutation, and when `modified` is false it does nothing. The
fix is therefore not a second gate but the deletion of a divergence --
the mutation dict moves inside `if modified:` and the parallel path
starts answering the question the way the serial one always did (#235).

`force=True` is the other half. `_ISOCENTER_REDACTION_HASH` is computed
over the *configuration*, not over the pixels, so a store damaged by
#229 carries an attestation byte-identical to the one the fixed code
would write, and the fixed code declines to look at it. `force=` bypasses
that skip and nothing else, which makes such a store repairable from the
API instead of by hand-editing a private tag (#237).

**Executor coverage.** `redact()` takes threads on a free-threaded build
and processes elsewhere, so every test whose subject is the parallel path
is parametrised over both levers exactly as `test_redaction_identity.py`
is, and for the same reason: the property is about the executor, not
about the interpreter.

**Fixture geometry, stated rather than left to be discovered.**
`reloaded_redaction_session` is a monoculture -- 32x32, `uint8`,
`MONOCHROME2`, `SamplesPerPixel 1`, single-frame, SC Image Storage -- and
every test here inherits it. That is adequate for #235 and #237, both of
which are control flow *around* `apply_redaction_to_array` rather than
axis selection, which is #186/#205/#217's territory.
"""
import os
import sqlite3

import numpy as np
import pydicom
import pytest

from isocenter.services import RedactionService

#: Both spellings, always: `redact()` picks its executor from these, and
#: the defect being fixed lived in the parallel path only.
LEVERS = ["ISOCENTER_FORCE_THREADS", "ISOCENTER_FORCE_PROCESSES"]

#: Entirely past the edge of a 32x32 image. `apply_redaction_to_array`
#: skips such a zone without raising, so `modified` stays False and
#: nothing about the instance's pixels changed.
OFF_EDGE_ZONE = [100, 108, 100, 108]

#: Inside the image, so a zone really lands.
IN_IMAGE_ZONE = [0, 8, 0, 8]

#: In bounds and selects **zero pixels** -- `arr[0:0, 20:20]`. See
#: `test_a_zone_that_selects_no_pixels_still_earns_the_attestation`.
ZERO_AREA_ZONE = [0, 0, 20, 20]

#: 32*32*200, the fixture's untouched total.
PRISTINE_TOTAL = 32 * 32 * 200

#: The four attribute keys the mutation dict carried, plus the one
#: sequence key. All five are written only by `_apply_redaction_flags`
#: and the hash assignment beside it, both inside `if modified:`.
ATTESTATION_KEYS = ["0028,0301", "0008,0008", "0008,2111",
                    "_ISOCENTER_REDACTION_HASH"]


def _audit(db_path, action_type):
    """Rows straight out of sqlite, after `close()`.

    Not `get_audit_errors()`: #218 is open on that reader missing a row
    still in flight on the writer thread.
    """
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT entity_uid, details FROM audit_log WHERE action_type=?",
            (action_type,)).fetchall()


def _tasks_for(session):
    """How many instances the loaded rule actually matched.

    Every "nothing was applied" test below needs this: with no matching
    instance the rule never reaches a worker and the assertions pass
    while asking nothing. Asked through `prepare_redaction_tasks` rather
    than through the `redact()` return, because the return value is
    itself under test.
    """
    service = RedactionService(session.store, session.store_backend)
    return service.prepare_redaction_tasks(session.configuration.rules[0])


# --- T1 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_a_rule_whose_zones_all_miss_reports_nothing_applied(
        reloaded_redaction_session, monkeypatch, caplog, lever):
    """`applied` counts pixels changed, not instances a rule matched.

    **Detection.** Measured `applied == 1` on `84113ab`, both levers,
    both gate interpreters: the rule matched the instance, every zone
    started past the edge, nothing was redacted, and the console printed
    "Redaction complete: 1 of 1 images updated".

    The count is public and this narrows it. There is deliberately no
    second count of "instances a rule matched" -- pre-1.0 the project
    deletes rather than doubles a spelling, and "how many images did you
    change" is the question the sentence a user reads is asking.

    **The shortfall warning is asserted here rather than left to the
    reader.** A smaller count is only half the repair: the number drops
    and nothing in the run says why. `_apply_redaction_rules` enumerates
    the reasons a task returned no change, and #235 adds a fourth one to
    that sentence. Measured during review: deleting that fourth clause
    leaves the whole suite green, so without this assertion the sentence
    a user reads is the one part of this change nothing holds still.
    """
    monkeypatch.setenv(lever, "1")
    session, _inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"count_{lever}")

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance, so a count of zero would be the "
        "absence of work rather than the absence of a change")

    with caplog.at_level("WARNING"):
        applied = session.redact(show_progress=False)

    assert applied == 0, (
        "a rule that changed no pixel reported that it updated an image")

    messages = [record.message for record in caplog.records]
    assert any("0 of 1" in message for message in messages), (
        f"the shortfall was not reported anywhere: {messages}")
    assert any("no configured zone that landed inside the image" in message
               for message in messages), (
        "the shortfall warning does not name the reason this run applied "
        f"nothing, so the smaller count is unexplained: {messages}")


# --- T2 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_a_rule_whose_zones_all_miss_creates_no_attributes(
        reloaded_redaction_session, tmp_path, monkeypatch, lever):
    """Absent stays absent; nothing is created with the value `None`.

    **Detection**, and the assertions have to be `not in` rather than
    `not ...get(...)`: on `84113ab` all four keys are **present with the
    value `None`**, which a truthiness assertion cannot tell from
    absence. `test_redaction_multizone.py:347` is written the loose way,
    which is why it stayed green through this defect.

    #235 reported "no `_ISOCENTER_REDACTION_HASH` is written". Measured,
    the key *is* created, with `None`. That is not a second seal -- the
    skip check is `current_hash == config_hash` and `None` never matches
    a hex digest, so the instance is still retried -- but it is noise in
    the store's `attributes_json`, and the other three nulls are not
    noise at all.

    The UID and `file_path` assertions at the end are **selectivity
    guards** with respect to #235: both are green on `84113ab`, where
    `test_redaction_identity.py::test_an_instance_nothing_was_applied_to_keeps_its_identity`
    already pins them. They are restated here because this change deletes
    #228's `new_uid != sop` gate, and the property has to keep being
    asserted. Measured: re-widening the mutation construction back
    outside `if modified:` takes the `file_path` assertion red -- the
    two UID assignments become no-ops but `instance.file_path = None`,
    the third statement in the same block, does not.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"attrs_{lever}")
    source_uid = inst.sop_instance_uid

    stale = tmp_path / f"stale_attrs_{lever}.dcm"
    stale.write_bytes(b"not read; only the path is under test")
    inst.file_path = str(stale)

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance and no worker ever ran")

    session.redact(show_progress=False)

    for key in ATTESTATION_KEYS:
        assert key not in inst.attributes, (
            f"{key} was created on an instance nothing was applied to, "
            f"with the value {inst.attributes.get(key)!r}")
    assert "0008,9215" not in inst.sequences, (
        "a Derivation Code Sequence was added to an instance nothing was "
        "derived from")

    assert inst.sop_instance_uid == source_uid
    assert inst.attributes.get("0008,0018", source_uid) == source_uid
    assert inst.file_path == str(stale), (
        "an instance whose zones all missed was detached from its source "
        "file, which is #238's mechanism reaching a run that changed "
        "nothing")


# --- T3 ---------------------------------------------------------------------

def test_a_rule_whose_zones_all_miss_writes_no_element_to_the_exported_file(
        reloaded_redaction_session, tmp_path):
    """The nulls reach a DICOM file, so a graph-only test is not enough.

    **Detection.** Measured on `84113ab`: the exported dataset carries
    `BurnedInAnnotation` (`CS`), `ImageType` (`CS`) and
    `DerivationDescription` (`ST`) as **zero-length elements**. A
    compliance reader asking "are identifiers burned into these pixels"
    gets an element that answers neither `YES` nor `NO`, which is not the
    same claim as the source's silence.

    **No executor lever here, deliberately.** `session.export()` pins
    `maxtasksperchild`, and worker recycling gives processes the last
    word, so the export runs in processes on every interpreter (#185).
    Adding a lever would suggest the export followed it. What this test
    asks is what the export worker sees in a graph the redaction left
    alone, and that is lever-independent.
    """
    session, inst = reloaded_redaction_session([OFF_EDGE_ZONE], name="export")

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance and no worker ever ran")

    session.redact(show_progress=False)
    session.save()

    out = tmp_path / "out_offedge"
    session.export(str(out), format="dicom")

    files = sorted(out.rglob("*.dcm"))
    assert len(files) == 1, (
        f"expected exactly one exported instance, got {files}; an empty "
        "tree means the instance failed module validation and every "
        "assertion below would be vacuous")

    ds = pydicom.dcmread(str(files[0]))
    for keyword in ("BurnedInAnnotation", "ImageType",
                    "DerivationDescription"):
        assert keyword not in ds, (
            f"{keyword} was written to the exported file with the value "
            f"{getattr(ds, keyword, None)!r} by a redaction that changed "
            "no pixel")

    arr = ds.pixel_array
    assert int(arr.sum()) == PRISTINE_TOTAL, (
        "pixels changed after all; this test is not asking its question")


# --- T4 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_one_zone_landing_is_enough(
        reloaded_redaction_session, monkeypatch, lever):
    """The gate is "any zone landed", not "every zone landed".

    **Selectivity guard** -- green on `84113ab` and green after. It is
    the only guard against over-correcting the new gate's polarity: a
    fix written as "no zone may have been skipped" would leave a
    perfectly ordinary rule, one of whose zones happens to fall outside
    a smaller image in the series, silently attesting nothing while
    having redacted a real burned-in identifier.

    `test_redaction_multizone.py::test_a_rule_applies_every_in_image_zone_whatever_the_order`
    covers the same mixed rule but asserts only the pixels and the count;
    nothing else in the suite asserts the *attestation* for one.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE, OFF_EDGE_ZONE], name=f"mixed_{lever}")
    source_uid = inst.sop_instance_uid

    assert session.redact(show_progress=False) == 1, (
        "a rule with one landing zone reported no change")

    assert inst.attributes.get("0028,0301") == "NO"
    assert inst.attributes.get("_ISOCENTER_REDACTION_HASH"), (
        "a redacted instance carries no attestation, so a later run will "
        "redact it again")
    assert inst.sop_instance_uid != source_uid, (
        "the redacted instance kept the identity of the data it was "
        "derived from")

    inst.unload_pixel_data()
    arr = inst.get_pixel_data()
    assert int(arr[0:8, 0:8].sum()) == 0, "the landing zone was not applied"


# --- T5 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_an_existing_burned_in_flag_survives_a_rule_that_applied_nothing(
        reloaded_redaction_session, monkeypatch, lever):
    """A source's `(0028,0301) = YES` must keep reaching the risk scan.

    **Selectivity guard**, and labelled one because it was *measured*
    green on `84113ab`. The obvious reading of #235 is that the null
    write downgrades a positive flag, after which
    `scan_burned_in_annotations` -- whose read is
    `.get("0028,0301", "NO")` behind an `isinstance(val, str)` guard --
    silently stops counting it. It does not: the mutation dict carries
    `inst.attributes.get("0028,0301")` read from the worker's own
    instance, which still holds whatever the source had, so the `None`
    appears only where the element was **absent**. Writing this as
    detection would be claiming a fix for a defect that was never there.

    It is worth having anyway, because it is the safety-relevant half of
    the rule this change makes explicit: `"YES"` is the scanner's own
    claim that identifiers are drawn into the pixels, and a redaction
    that removed none of them has no basis to touch it.

    `(0008,0008)` is supplied here on purpose, and it is what keeps this
    test a guard rather than detection: without it the null write lands
    on `ImageType` too and `scan_burned_in_annotations` raises. That is a
    real defect and it is pinned separately, immediately below.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"yes_{lever}")
    db_path = session.store_backend.db_path
    inst.set_attr("0028,0301", "YES")
    inst.set_attr("0008,0008", ["ORIGINAL", "PRIMARY"])

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance and no worker ever ran")

    session.redact(show_progress=False)

    assert inst.attributes.get("0028,0301") == "YES", (
        "a scanner's own burned-in-annotation flag was overwritten by a "
        "redaction that removed nothing")
    session.close()

    rows = _audit(db_path, "RISK")
    assert rows, (
        "the untreated burned-in annotation was not filed as a risk, so a "
        "report over this session would grade it clean")


@pytest.mark.parametrize("lever", LEVERS)
def test_a_yes_flag_without_an_image_type_does_not_break_the_risk_scan(
        reloaded_redaction_session, monkeypatch, lever):
    """The null `ImageType` write crashes the scan it was supposed to feed.

    **Detection, and it is not the failure mode #235 predicted.**
    Measured on `84113ab`, both levers: `redact()` raises
    `TypeError: 'NoneType' object is not iterable` from
    `scan_burned_in_annotations` (`services.py:211`).

    The route is the null write itself. `_apply_redaction_outcomes`
    copies `{"0008,0008": None, ...}` onto an instance whose source
    carried no `ImageType`; `scan_burned_in_annotations` then reads
    `inst.attributes.get("0008,0008", [])`, which returns the stored
    `None` rather than the default, and iterates it. The `isinstance`
    guard one line above covers `(0028,0301)` and there is no equivalent
    here.

    So the null element is not only an unanswerable value in an exported
    file: on the instances that matter most -- the ones a scanner already
    flagged as carrying a burned-in identifier -- it takes down the
    post-redaction safety scan and turns a run that changed nothing into
    a raising one. `scan_burned_in_annotations` runs **before** the
    console summary and before the `RedactionError` check, so the whole
    call fails.

    Fixed by not writing the null, not by hardening the reader. A guard
    at `services.py:207` would make the scan survive a value the graph
    should never have held; #235 is that the value is written at all.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"noimagetype_{lever}")
    inst.set_attr("0028,0301", "YES")
    assert "0008,0008" not in inst.attributes, (
        "the fixture supplied an ImageType, which is the value under "
        "test; this test would pass without the null write ever being "
        "read")

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance and no worker ever ran")

    session.redact(show_progress=False)

    assert inst.attributes.get("0028,0301") == "YES"


# --- T6 ---------------------------------------------------------------------

def test_the_serial_path_attests_nothing_when_no_zone_lands(
        reloaded_redaction_session):
    """`redact_machine_instances` is the shape the parallel path adopts.

    **Selectivity guard** -- green on `84113ab`, because the serial path
    never had the divergence: it builds no mutation, so when `modified`
    is false there is nothing for a parent to copy. It is pinned here so
    that the property the parallel path is being moved *to* is asserted
    for both paths in one file, with the `not in` spelling.

    `test_redaction_multizone.py::test_a_rule_that_applied_nothing_returns_False_and_attests_nothing`
    covers the serial path too, but asserts the hash alone and with
    `assert not ... .get(...)`, which passes on `None` as readily as on
    absent.
    """
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name="serial_attest")

    service = RedactionService(session.store, session.store_backend)
    service.redact_machine_instances(
        "SN_RELOAD", [tuple(OFF_EDGE_ZONE)], targets=[inst],
        show_progress=False)

    for key in ATTESTATION_KEYS:
        assert key not in inst.attributes, (
            f"{key} was created on the serial path with the value "
            f"{inst.attributes.get(key)!r}")
    assert "0008,9215" not in inst.sequences

    inst.unload_pixel_data()
    assert int(inst.get_pixel_data().sum()) == PRISTINE_TOTAL, (
        "something was zeroed after all; this test is not asking its "
        "question")


# --- T7 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_an_off_image_redaction_appends_nothing_to_the_sidecar(
        reloaded_redaction_session, monkeypatch, lever):
    """A swap that did not happen is not persisted.

    **Detection.** Measured on `84113ab`, both levers: the sidecar grows
    `17 -> 34` bytes and `instance_blobs` still holds exactly one row, so
    17 bytes are appended that nothing references.
    `execute_redaction_task`'s `finally` calls `persist_pixel_data`
    whenever the task did not *fail*, and `persist_pixel_data` has no
    deduplication -- it hashes, writes a frame and re-points the loader
    every time it is called with a resident array.

    It is the same gate as the rest of this file rather than a separate
    tidy-up: persisting a redaction that did not happen is "attested
    without being earned", in the sidecar instead of in the attributes.

    Read **after `close()`**: the persistence manager drains on a
    background thread, so a size taken before the close is a size of a
    half-written file.

    The in-bounds path appends an orphan too -- `execute_redaction_task`
    persists once inside `if modified:`, which is the call that supplies
    `mutation["pixel_loader"]` and so cannot move, and once again in the
    `finally`. That half is filed separately and is **not** asserted
    here.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name=f"sidecar_{lever}")
    db_path = session.store_backend.db_path
    sidecar = os.path.splitext(db_path)[0] + "_pixels.bin"

    before = os.path.getsize(sidecar)
    assert before > 0, (
        "the fixture wrote no pixel frame, so an unchanged size below "
        "would be the absence of a sidecar rather than the absence of a "
        "write")

    assert len(_tasks_for(session)) == 1, (
        "the rule matched no instance and no worker ever ran")

    session.redact(show_progress=False)
    session.save()
    session.close()

    assert os.path.getsize(sidecar) == before, (
        "an instance nothing was applied to had its pixels appended to "
        "the sidecar; only `compact()` ever notices those bytes")

    with sqlite3.connect(db_path) as conn:
        blobs = conn.execute(
            "SELECT instance_uid, kind FROM instance_blobs").fetchall()
    assert blobs == [(inst.sop_instance_uid, "pixels")], blobs


def test_the_serial_path_appends_nothing_to_the_sidecar_either(
        reloaded_redaction_session):
    """The same gate on `redact_machine_instances`' `finally`.

    **Detection**, and it is the reason this test exists as its own case:
    the serial path's `finally` carries the identical unconditional
    `persist_pixel_data`, and measured on `84113ab` it appends the same
    17 unreferenced bytes -- `17 -> 34`, with `instance_blobs` re-pointed
    at the *second* copy, so it is the first that is orphaned. Gating
    only the parallel path would have fixed one half of a divergence in
    the change whose whole argument is that the two paths must answer one
    question the same way.
    """
    session, inst = reloaded_redaction_session(
        [OFF_EDGE_ZONE], name="serial_sidecar")
    db_path = session.store_backend.db_path
    sidecar = os.path.splitext(db_path)[0] + "_pixels.bin"

    before = os.path.getsize(sidecar)
    assert before > 0, "the fixture wrote no pixel frame"

    service = RedactionService(session.store, session.store_backend)
    service.redact_machine_instances(
        "SN_RELOAD", [tuple(OFF_EDGE_ZONE)], targets=[inst],
        show_progress=False)
    session.save()
    session.close()

    assert os.path.getsize(sidecar) == before, (
        "the serial path appended an unreferenced pixel frame for an "
        "instance nothing was applied to")

    with sqlite3.connect(db_path) as conn:
        blobs = conn.execute(
            "SELECT instance_uid, kind FROM instance_blobs").fetchall()
    assert blobs == [(inst.sop_instance_uid, "pixels")], blobs


# --- T8 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_a_damaged_store_is_repaired_by_force(
        reloaded_redaction_session, monkeypatch, lever):
    """`force=True` re-redacts an instance whose attestation already matches.

    **Detection for `force=`**, which does not exist on `84113ab`.

    **It simulates the damage rather than replaying `0.9.0`, and that is
    deliberate: a test that shells out to an old version is not a test.**
    The evidence that the simulated state is the state `0.9.0` leaves is
    a real two-version replay, recorded in
    `docs/superpowers/specs/2026-08-31-redaction-attestation.md` §2 --
    `git archive v0.9.0` into a scratch tree, the same database carried
    across, `zone1 = 12800` under a valid attestation on both gate
    interpreters. The per-zone loop that produced it shipped in every
    release up to and including `0.9.0` (`git show
    v0.9.0:isocenter/services.py | grep -c "for roi in rois"` -> 3), so
    the damaged population exists outside this repository.

    The simulation is corroborated rather than assumed: the attestation
    this test's damaged instance carries is
    `b3889c5ba55e757f17a7f2fba0d112c0`, which is byte-for-byte the hash
    #237 measured on its own `0.9.0`-damaged store for the same rule.
    The attestation is what the skip reads, so an identical attestation
    over an identically-damaged array is the same input to the code under
    test.

    That the repair lands is measured too -- §B5.4's table, run on
    unmodified `84113ab` by deleting the attestation by hand, which is
    exactly and only what `force=True` does: `zone1 12800 -> 0` on both
    interpreters. It works because the burned-in identifier is still *in*
    the store's own pixels, so no source file is needed.
    """
    monkeypatch.setenv(lever, "1")
    zones = [IN_IMAGE_ZONE, [16, 24, 16, 24]]
    session, inst = reloaded_redaction_session(zones, name=f"damaged_{lever}")
    db_path = session.store_backend.db_path

    assert session.redact(show_progress=False) == 1
    attestation = inst.attributes.get("_ISOCENTER_REDACTION_HASH")
    assert attestation, "nothing was redacted, so there is no damage to do"

    # The damage: put zone 1 back the way #229 left it, keeping the
    # attestation the damaged run wrote.
    inst.unload_pixel_data()
    arr = np.array(inst.get_pixel_data(), copy=True)
    arr[0:8, 0:8] = 200
    inst.set_pixel_data(arr)
    inst.mark_modified()
    session.save()
    session.close()

    from isocenter.session import DicomSession
    repaired = DicomSession(db_path)
    try:
        damaged = next(i
                       for p in repaired.store.patients
                       for st in p.studies
                       for se in st.series
                       for i in se.instances)
        repaired.configuration.rules = [
            {"serial_number": "SN_RELOAD", "redaction_zones": zones}]

        # Non-vacuity, and the whole premise: the damaged instance is
        # carrying the attestation, so the skip really is what stops the
        # plain call below.
        assert damaged.attributes.get(
            "_ISOCENTER_REDACTION_HASH") == attestation, (
            "the damage step cleared the attestation, so the plain "
            "`redact()` below would redact and this test would measure "
            "nothing")
        damaged.unload_pixel_data()
        assert int(damaged.get_pixel_data()[0:8, 0:8].sum()) == 8 * 8 * 200, (
            "the damage did not reach the store")

        assert repaired.redact(show_progress=False) == 0, (
            "the plain call did not skip, so `force=` is not the thing "
            "under test here")
        damaged.unload_pixel_data()
        assert int(damaged.get_pixel_data()[0:8, 0:8].sum()) == 8 * 8 * 200, (
            "the plain call changed the pixels after all")

        assert repaired.redact(show_progress=False, force=True) == 1, (
            "`force=True` did not re-redact an instance whose attestation "
            "matched the configuration")
        damaged.unload_pixel_data()
        assert int(damaged.get_pixel_data()[0:8, 0:8].sum()) == 0, (
            "`force=True` reported a redaction it did not perform")
    finally:
        repaired.close()


def test_the_serial_path_takes_force_too(reloaded_redaction_session):
    """`redact_machine_instances(..., force=True)` bypasses the same skip.

    **Detection for `force=` on the serial path**, which does not exist
    on `84113ab`. The parameter is threaded to both call sites so the two
    public spellings of one behaviour keep answering the same way; a
    `force` that reached only the parallel path would be a new
    divergence in the change that exists to delete one.

    Simpler damage than T8's: a second call under an attestation the
    first call wrote, against pixels put back by hand. What is under test
    is the skip, not the repair arithmetic.

    The damage is **persisted**, not left resident. Both paths'
    `finally` ends in `unload_pixel_data()`, so an array assigned with
    `set_pixel_data` alone is dropped on the next pass and the loader
    hands back the redacted frame -- the damage would silently undo
    itself and the force leg would pass over pixels that were never
    damaged.
    """
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name="serial_force")
    service = RedactionService(session.store, session.store_backend)
    rois = [tuple(IN_IMAGE_ZONE)]

    service.redact_machine_instances(
        "SN_RELOAD", rois, targets=[inst], show_progress=False)
    assert inst.attributes.get("_ISOCENTER_REDACTION_HASH"), (
        "nothing was redacted, so there is no attestation to bypass")

    inst.unload_pixel_data()
    arr = np.array(inst.get_pixel_data(), copy=True)
    arr[0:8, 0:8] = 200
    inst.set_pixel_data(arr)
    inst._pixel_hash = None
    session.store_backend.persist_pixel_data(inst)
    inst.unload_pixel_data()
    assert int(inst.get_pixel_data()[0:8, 0:8].sum()) == 8 * 8 * 200, (
        "the damage did not survive the loader, so nothing below is "
        "measuring a skip over damaged pixels")

    service.redact_machine_instances(
        "SN_RELOAD", rois, targets=[inst], show_progress=False)
    inst.unload_pixel_data()
    assert int(inst.get_pixel_data()[0:8, 0:8].sum()) == 8 * 8 * 200, (
        "the plain second call did not skip, so the force leg below is "
        "not measuring the skip")

    service.redact_machine_instances(
        "SN_RELOAD", rois, targets=[inst], show_progress=False, force=True)
    inst.unload_pixel_data()
    assert int(inst.get_pixel_data()[0:8, 0:8].sum()) == 0, (
        "`force=True` did not bypass the attestation skip on the serial "
        "path")


# --- T9 ---------------------------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_force_is_off_by_default(
        reloaded_redaction_session, monkeypatch, lever):
    """A second `redact()` with the same rules stays a no-op.

    **Selectivity guard** -- green on `84113ab`, where
    `test_redaction_failure_is_reported.py:339` already asserts the count
    half. It is here because `force=` is the parameter that would break
    this if its default ever moved, and what it would break is a promise
    the project shipped three commits ago, in #228's CHANGELOG entry:

        re-running `redact()` with the same rules is a no-op and renames
        nothing -- already-redacted instances keep the identity the old
        run gave them.

    That sentence is why `force=` is opt-in rather than an epoch stamp on
    the attestation: an epoch bump repairs a damaged store automatically
    and falsifies this for every store in existence, handing a new SOP
    Instance UID, a new exported filename and a cleared `file_path`
    (#238) to a population that had no defect.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [IN_IMAGE_ZONE], name=f"default_{lever}")

    assert session.redact(show_progress=False) == 1
    redacted_uid = inst.sop_instance_uid

    assert session.redact(show_progress=False) == 0, (
        "the second run redacted an already-redacted instance without "
        "being asked to")
    assert inst.sop_instance_uid == redacted_uid, (
        "an already-redacted instance was renamed by a no-op run, which "
        "changes its exported filename (#78)")


# --- the documented boundary ------------------------------------------------

@pytest.mark.parametrize("lever", LEVERS)
def test_a_zone_that_selects_no_pixels_still_earns_the_attestation(
        reloaded_redaction_session, monkeypatch, lever):
    """The gate is "a zone was applied", not "the bytes changed".

    **Selectivity guard** -- green on `84113ab` and green after. It pins
    a boundary this change deliberately does **not** move, so that the
    next reader tidying #235 into "only count instances whose pixels
    differ" finds a test standing in the way with the reason attached.

    `[0, 0, 20, 20]` is in bounds and selects `arr[0:0, 20:20]`, which is
    empty. `apply_redaction_to_array` sets `modified = True` after the
    assignment without comparing, so this instance is counted, renamed
    and fully attested with no pixel touched.

    Redefining `modified` as "the bytes changed" is the fix that would
    close it, and it is rejected: a correct re-run of an already-redacted
    instance changes nothing either, so it would never re-earn the
    attestation, and the `current_hash == config_hash` skip -- the thing
    that keeps a second `redact()` off every pixel in a 100GB store --
    would stop working. `tests/test_redact_reports_outcome.py`'s own
    fixture uses exactly this zone, which is how the shape stayed
    invisible.
    """
    monkeypatch.setenv(lever, "1")
    session, inst = reloaded_redaction_session(
        [ZERO_AREA_ZONE], name=f"zeroarea_{lever}")
    source_uid = inst.sop_instance_uid

    assert session.redact(show_progress=False) == 1, (
        "an in-bounds zone selecting no pixels stopped being counted; "
        "that is a behaviour change this test exists to make deliberate")

    assert inst.attributes.get("0028,0301") == "NO"
    assert inst.attributes.get("0008,0008") == ["DERIVED", "SECONDARY"]
    assert inst.attributes.get("_ISOCENTER_REDACTION_HASH")
    assert inst.sop_instance_uid != source_uid

    inst.unload_pixel_data()
    assert int(inst.get_pixel_data().sum()) == PRISTINE_TOTAL, (
        "a zone selecting no pixels changed some; the boundary this test "
        "documents is not the one being exercised")
