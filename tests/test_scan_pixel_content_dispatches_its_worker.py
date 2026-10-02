"""`Session.scan_pixel_content()` and the worker it dispatches (#394).

`scan_pixel_content` is a frozen tier-1 method on `docs/api/stability.md`
and no test had ever run it. `session._verify_worker` sat at 14% line
coverage -- its `def` line -- so `results = run_parallel(_verify_worker,
worker_items, ...)` replaced by `results = []` was a green mutation:
"the pool is never dispatched" was indistinguishable from working code.

**Why this file imports what it does.** It reaches findings through the
returned `PhiReport` rather than importing the module that defines
`PhiFinding`, because that module is a mutation-probe target and
`tests/test_mutation_probe_targets._importers` matches the *text* of a
dotted module name anywhere in a test file. Naming one here would charge
this file to every one of that module's mutants for no kill signal.

**What this file deliberately does not test.** The child's pixel
hydration. Neither local gate interpreter has `pytesseract`, and CI
installs the `ocr` extra and the tesseract binary, so a test whose
result depends on real OCR passes for different reasons in the two
places, which is the definition of an unreliable pin. The facade tests
therefore patch `pixel_analysis._ocr_instance` out entirely, and the
process boundary is covered instead by an explicit pickle round-trip,
which is deterministic everywhere. (They patched
`verification.analyze_pixels` until #423, when the worker stopped calling
it: a patch left there is inert, and the pass runs the real OCR, which
neither local interpreter has.)

**Why the facade tests take `ocr_present`.** Since #422 the facade
refuses before reading the graph when OCR cannot run, which it cannot on
either local interpreter. `ocr_present` (in `conftest.py`) patches both
`HAS_OCR` and `pytesseract` itself: the refusal probes the binary through
the latter, so patching only the flag would be red here and green in CI.
The refusal itself is pinned in `tests/test_ocr_unavailable_refuses.py`.
"""
import pickle
from datetime import date
from unittest.mock import patch

import numpy as np
import pytest

from isocenter import session as session_module
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.pixel_analysis import TextRegion, _InstanceOcr
from isocenter.session import DicomSession

CT_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"

#: Zone space is `(y1, y2, x1, x2)`. OCR box space is `(x, y, w, h)`.
#: `RedactionVerifier._coverage` is the only place that converts, and
#: reading one as the other was #258/#264 -- so these two constants are
#: written in the space they belong to and never reused across it.
ZONE_TOP_LEFT = [0, 100, 0, 100]
TEXT_OUTSIDE_THE_ZONE = (200, 200, 50, 50)   # x, y, w, h
TEXT_INSIDE_THE_ZONE = (10, 10, 50, 50)      # x, y, w, h


@pytest.fixture(autouse=True)
def _threads_not_processes(monkeypatch):
    """Force the thread arm, and say why it is not decoration.

    3.12's default is **processes**, and `unittest.mock.patch` does not
    cross a process boundary: the child imports the real `analyze_pixels`,
    which returns `[]` before touching pixels when no `pytesseract` is
    installed. Measured on 3.12.14: one finding in threads, zero in
    processes, for exactly that reason.

    What that costs without this line is an **interpreter divergence**,
    not a silent pass. Measured, deleting the `setenv` below: 3.12.14
    goes `2 failed, 2 passed` (processes, no findings) while 3.14.7t
    stays `4 passed`, because the free-threaded build already picks
    threads. The assertions here compare against non-empty expected
    values, so an empty report reddens them rather than degrading to
    `0 == 0` -- the failure mode is a file that passes on one gate and
    fails on the other, which is worse than either, and reads as a flake
    to whoever meets it first.

    So do not "simplify" this to a `3.14t`-only assumption, and do not
    reach for `ISOCENTER_FORCE_PROCESSES` here: process mode cannot carry
    these assertions at all while `patch` stops at the boundary. The
    pickle round-trip in `test_the_worker_scans_an_instance_that_crossed_a_pickle`
    is what covers that boundary instead. (`PhiFinding.entity` meant a
    live object in threads and a dead copy in processes until #412. The
    processes arm of that fix is pinned in
    `tests/test_scan_pixel_findings_name_the_live_graph.py`, which can
    run it because it patches inside the worker rather than around it;
    the worker's half, dropping the instance, is asserted in T-394a.)
    """
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "3")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _region(box, text="LEAKTEXT"):
    """One OCR hit. The text must exceed two characters: `verify_instance`
    drops anything shorter as noise before it ever raises a finding."""
    return TextRegion(text, box, 90.0)


def _read(*regions):
    """What `_ocr_instance` returns for an instance it read cleanly."""
    return _InstanceOcr(list(regions), True, None)


def _instance(uid, serial, with_pixels=False):
    instance = Instance(uid, CT_SOP_CLASS, 1)
    instance.file_path = None
    instance.set_attr("0018,1000", serial or "")
    if with_pixels:
        instance.set_pixel_data(np.full((16, 16), 1000, dtype=np.uint16))
    return instance


def _series(series_uid, number, serial, uids, with_pixels=False):
    """One series, its equipment, and one instance per uid.

    `serial=None` means no `Equipment` at all; `serial=""` means an
    `Equipment` that carries no device serial number. Those are two
    different arms of the same guard.
    """
    series = Series(series_uid, "CT", number)
    if serial is not None:
        series.equipment = Equipment("Acme", "Model", serial)
    for uid in uids:
        series.instances.append(_instance(uid, serial, with_pixels))
    return series


def _session_with(tmp_path, name, series_list, rules):
    session = DicomSession(persistence_file=str(tmp_path / f"{name}.db"))
    patient = Patient("PAT1", "Scan^Patient")
    study = Study("ST_1", date(2023, 1, 1))
    study.study_time = "120000"
    for series in series_list:
        study.series.append(series)
    patient.studies.append(study)
    session.store.patients.append(patient)
    session.configuration.rules = rules
    return session


def test_the_worker_scans_an_instance_that_crossed_a_pickle():
    """T-394a: `_verify_worker` over the round-trip the pool puts it through.

    Not setup noise: what a real pass sends to a worker is the
    `SidecarPixelLoader`, not a resident array. A resident array pickles
    as bytes and proves only that numpy pickles. Measured on 3.12.14:
    1773 bytes with the 16x16 array resident against 1002 with it
    unloaded, and `get_pixel_data()` on the far side returning the right
    frame. This asserts that far-side read, so a loader that stopped
    surviving the pickle is red here rather than silently scanning
    nothing in CI.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        import pathlib
        tmp_path = pathlib.Path(tmp)
        series = _series("SE_1", 1, "SN-A", ["1.2.826.0.1.0"], with_pixels=True)
        session = _session_with(
            tmp_path, "worker", [series],
            [{"serial_number": "SN-A", "redaction_zones": [ZONE_TOP_LEFT]}])
        try:
            session.save(sync=True)
            instance = series.instances[0]
            resident = instance.get_pixel_data().copy()
            # The pool's real case: the loader crosses, not the array.
            instance.unload_pixel_data()

            args = (instance, series.equipment, session.configuration.rules, None)
            revived = pickle.loads(pickle.dumps(args))

            assert np.array_equal(revived[0].get_pixel_data(), resident), (
                "the pixel loader did not survive the pickle; a worker would "
                "scan nothing and every finding assertion would read 0 == 0")

            with patch("isocenter.pixel_analysis._ocr_instance",
                       return_value=_read(_region(TEXT_OUTSIDE_THE_ZONE))):
                uncovered = session_module._verify_worker(revived).findings

            with patch("isocenter.pixel_analysis._ocr_instance",
                       return_value=_read(_region(TEXT_INSIDE_THE_ZONE))):
                covered = session_module._verify_worker(revived).findings
        finally:
            session.close()

    assert len(uncovered) == 1, uncovered
    finding = uncovered[0]
    # 412-b: the worker drops the instance before the finding crosses
    # back, as `scan_worker` does for `audit()`. The facade puts the live
    # one back (`tests/test_scan_pixel_findings_name_the_live_graph.py`),
    # and that test cannot see this line: rehydration overwrites the
    # copy either way, so a missing strip leaves the identity intact
    # while the worker's result still carries its decoded frame back to
    # the parent -- once per scanned instance with a finding, since
    # pickle memoises the instance the findings share. Only this
    # assertion is red for it.
    assert finding.entity is None, (
        "the worker returned a finding still carrying its instance")
    assert finding.entity_uid == "1.2.826.0.1.0"
    assert finding.field_name == "PixelData[Frame=0]"
    assert finding.value == "LEAKTEXT"

    # The covered case is what makes `equipment` load-bearing. With only
    # the uncovered case, passing `None` for equipment changes nothing --
    # coverage is 0.0 either way -- and dropping it would be a green
    # mutation.
    assert covered == [], covered

    # The only line of the worker the two cases above miss.
    assert session_module._verify_worker((None, None, [], None)) == (
        session_module._ScanOutcome(None, [], False, None))


def test_the_facade_dispatches_its_worker_and_applies_its_four_filters(
        tmp_path, ocr_present):
    """T-394b: exactly the configured series reach the pool.

    Four arms, each with two instances so a count can tell them apart:

    | series | equipment            | rule                  | expected |
    | ------ | -------------------- | --------------------- | -------- |
    | S1     | serial `SN-A`        | zones                 | scanned  |
    | S2     | serial `""`          | zones, blank serial   | skipped  |
    | S3     | serial `SN-C`        | none                  | skipped  |
    | S4     | serial `SN-D`        | rule, `zones: []`     | skipped  |

    S2 is the arm that makes the second half of `if not equip or not
    equip.device_serial_number` load-bearing, and it needs the blank-serial
    *rule* to do it: without a rule that a blank serial matches, dropping
    that clause changes nothing, because S2 would then be skipped one
    filter later for having no matching rule. With the rule present,
    dropping the clause scans a device whose serial the config never
    named -- which is the thing the clause prevents.

    Set equality in both directions: a superset assertion is green when a
    filter stops filtering.
    """
    scanned = ["1.1", "1.2"]
    session = _session_with(tmp_path, "filters", [
        _series("SE_1", 1, "SN-A", scanned),
        _series("SE_2", 2, "", ["2.1", "2.2"]),
        _series("SE_3", 3, "SN-C", ["3.1", "3.2"]),
        _series("SE_4", 4, "SN-D", ["4.1", "4.2"]),
    ], [
        {"serial_number": "SN-A", "redaction_zones": [ZONE_TOP_LEFT]},
        {"serial_number": "", "redaction_zones": [ZONE_TOP_LEFT]},
        {"serial_number": "SN-D", "redaction_zones": []},
    ])
    try:
        with patch("isocenter.pixel_analysis._ocr_instance",
                   return_value=_read(_region(TEXT_OUTSIDE_THE_ZONE))):
            report = session.scan_pixel_content()
    finally:
        session.close()

    assert {f.entity_uid for f in report} == set(scanned)


def test_the_serial_number_argument_narrows_the_scan(tmp_path, ocr_present):
    """T-394c: `scan_pixel_content(serial_number=)` filters, and filters *down*.

    Both calls are asserted. Asserting only the narrowed one is green on
    a filter that rejects everything.
    """
    first, second = ["1.1", "1.2"], ["2.1", "2.2"]
    session = _session_with(tmp_path, "narrow", [
        _series("SE_1", 1, "SN-A", first),
        _series("SE_2", 2, "SN-B", second),
    ], [
        {"serial_number": "SN-A", "redaction_zones": [ZONE_TOP_LEFT]},
        {"serial_number": "SN-B", "redaction_zones": [ZONE_TOP_LEFT]},
    ])
    try:
        with patch("isocenter.pixel_analysis._ocr_instance",
                   return_value=_read(_region(TEXT_OUTSIDE_THE_ZONE))):
            narrowed = session.scan_pixel_content(serial_number="SN-A")
            everything = session.scan_pixel_content()
    finally:
        session.close()

    assert {f.entity_uid for f in narrowed} == set(first)
    assert {f.entity_uid for f in everything} == set(first) | set(second)


def test_a_session_with_no_configured_equipment_scans_nothing_and_says_so(
        tmp_path, capsys, ocr_present):
    """T-394d: the empty path returns an empty report and counts the skips."""
    session = _session_with(
        tmp_path, "empty", [_series("SE_1", 1, None, ["1.1", "1.2", "1.3"])], [])
    try:
        with patch("isocenter.pixel_analysis._ocr_instance",
                   return_value=_read(_region(TEXT_OUTSIDE_THE_ZONE))):
            report = session.scan_pixel_content()
    finally:
        session.close()

    assert len(report) == 0

    # The one line naming the count, and the count in it -- not a
    # substring match on the whole capture, which would pass on any
    # output that happened to contain the word.
    skipped = [line for line in capsys.readouterr().out.splitlines()
               if "Skipped" in line]
    assert len(skipped) == 1, skipped
    # Since #808 the line counts machines no rule names apart from
    # instances whose rules hold no zone; these three are the first kind.
    assert ("(Skipped 3 instance(s) of machines no rule names)"
            in skipped[0]), skipped[0]
