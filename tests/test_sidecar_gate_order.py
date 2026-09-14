"""The sidecar gate's lock order, and the six sites that must hold it (#368).

The gate (`SqliteStore._hold_sidecar_gate`: a `threading.Lock`, then
`fcntl.flock(LOCK_EX)` on `<sidecar>.lock`) is the one lock in this
codebase deliberately held across a sqlite write, and it sits *above*
`_pixel_swap_lock`. The order is an internal invariant -- every name is
private and lives in implementation files (2026-09-08 spec §3) -- so it
is pinned here by recording wrappers rather than frozen in the API:

    pass-lock EX|NB (holding nothing)  ->  _sidecar_gate   (compact, first)
    _sidecar_gate  ->  _pixel_swap_lock          (compact's rewire; sites 5, 6)
    _sidecar_gate  ->  sqlite                    (every site's row commit)
    _pixel_swap_lock  ->  PIXEL_STATE_LOCK       (publish sections; a leaf)

`entities.PIXEL_STATE_LOCK` (#434, Q6) is taken by `set_pixel_data`,
`discard_pixel_data`, `unload_pixel_data` and the read publish
`_publish_loaded_frame` (#465) holding nothing, and by the four publish
sections under `_pixel_swap_lock`; nothing is taken while it is held. It
is recorded by the same proxy, swapped in on the module.

Reversing the second arm is the cycle the 2026-09-07 spec measured:
`_persist_pixels` calls `write_frame` under `_pixel_swap_lock`, and
`_rewire_sidecar_loaders` takes `_pixel_swap_lock` under the gate -- a
gate taken inside `write_frame` deadlocks against the rewire. Taking the
gate before `compact()`'s leading `save(sync=True)` deadlocks site 6
against itself. The pass-lock is taken exclusive *before* that save,
holding nothing (the review of PR #385 found that an attempt made after
the save admits a pass that opened and closed inside it), and shared
holding nothing by `redact()`/`ingest()`; nothing takes it under the
gate. The EX attempt is `LOCK_NB` because the refusal is an answer and
not a wait.

**What is recorded.** `_sidecar_gate` and `_pixel_swap_lock` on the
store are replaced with proxies that log every acquire and release with
the set of these locks the acquiring thread already holds; `fcntl.flock`
is wrapped to log the inode and flags of every lock call with the same
held-set; `SqliteStore.save_all` and `SidecarManager.write_frame` are
wrapped to log entry and exit. One session then runs every writer once:
`save(sync=True)` (site 6), `persist_pixel_data` (site 5),
`persist_blob` (site 4), an `ingest()` of a pixel file carrying an icon
and a waveform file (sites 1-3), and `compact()`.

**The seventh-site detector** is the last test: an AST walk of
`isocenter/` collects every `write_frame` call by enclosing function and
must equal the six the gate covers. A seventh `write_frame` anywhere is
red before anyone asks whether it is gated -- the gate is never inside
`write_frame` itself (`SidecarManager` is stateless by #366's design and
is called directly by fixture generators), so a new site is gated only
if its author gates it, and this is what makes forgetting loud.
"""
import ast
import collections
import fcntl
import logging
import os
import pathlib
import sys
import threading

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.pixels import convert_color_space
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from isocenter import entities as entities_module
from isocenter import persistence as persistence_module
from isocenter.entities import Equipment, Instance, Patient, Series, Study
from isocenter.logger import get_logger
from isocenter.persistence import SqliteStore
from isocenter.services import RedactionOutcome
from isocenter.session import DicomSession
from isocenter.sidecar import SidecarManager
from scripts.generate_waveform_test_data import write_fixture

REPO = pathlib.Path(__file__).resolve().parent.parent
CT_IMAGE = "1.2.840.10008.5.1.4.1.1.2"

#: The six `write_frame` sites, by (module, enclosing function) and how
#: many calls each holds. The gate-order test asserts every one of these
#: functions wrote a frame with the gate held; the seventh-site detector
#: asserts the AST holds exactly this multiset. Keyed on function names
#: rather than line numbers so it is green through unrelated edits and
#: red on a new site wherever it lands.
_WRITE_FRAME_SITES = {
    ("isocenter/io_handlers.py", "import_files"): 3,   # pixel, nested, waveform
    ("isocenter/persistence.py", "persist_blob"): 1,
    # `persist_pixel_data`'s body: the public method takes the gate and
    # this helper does the swap under it.
    ("isocenter/persistence.py", "_swap_pixels_under_gate"): 1,
    ("isocenter/persistence.py", "_persist_pixels"): 1,
}
_SITE_FUNCTIONS = {name for _file, name in _WRITE_FRAME_SITES}


class _Recorder:
    """One log shared by every wrapper, plus per-thread held-lock sets."""

    def __init__(self):
        self.log = []
        self._local = threading.local()
        self._guard = threading.Lock()

    def held(self):
        return getattr(self._local, "held", frozenset())

    def _set_held(self, held):
        self._local.held = frozenset(held)

    def record(self, event, **fields):
        with self._guard:
            self.log.append(dict(event=event, thread=threading.get_ident(),
                                 held=self.held(), **fields))

    def took(self, name):
        self._set_held(self.held() | {name})

    def dropped(self, name):
        self._set_held(self.held() - {name})


def _functions_on_stack():
    """Every function name on the calling thread's stack."""
    names = set()
    frame = sys._getframe(1)
    while frame is not None:
        names.add(frame.f_code.co_name)
        frame = frame.f_back
    return frozenset(names)


def _sites_on_stack():
    """Which of the six site functions are on the calling thread's stack."""
    names = set()
    frame = sys._getframe(1)
    while frame is not None:
        if frame.f_code.co_name in _SITE_FUNCTIONS:
            names.add(frame.f_code.co_name)
        frame = frame.f_back
    return frozenset(names)


class _RecordingLock:
    """A `threading.Lock` stand-in that logs acquire/release with context.

    Exposes exactly what the store uses: `acquire(timeout=)`, `release()`
    and the context-manager protocol. Replaces the store's lock *object*
    because a `threading.Lock`'s methods cannot be wrapped in place.
    """

    def __init__(self, name, recorder):
        self.name, self.recorder = name, recorder
        self._lock = threading.Lock()

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok:
            self.recorder.record("acquire", lock=self.name,
                                 sites=_sites_on_stack(),
                                 callers=_functions_on_stack())
            self.recorder.took(self.name)
        return ok

    def release(self):
        self.recorder.dropped(self.name)
        self.recorder.record("release", lock=self.name)
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def _instrument(session, monkeypatch):
    """Install every wrapper on one session's store and return the recorder."""
    recorder = _Recorder()
    store = session.store_backend
    store._sidecar_gate = _RecordingLock("gate", recorder)
    store._pixel_swap_lock = _RecordingLock("swap", recorder)
    # The pixel-state leaf (#434, Q6). `raising=False` so that, without
    # the lock, the leaf tests fail on what they measure rather than on
    # this line.
    monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK",
                        _RecordingLock("leaf", recorder), raising=False)

    real_flock = fcntl.flock

    def recording_flock(fd, flags):
        fileno = fd if isinstance(fd, int) else fd.fileno()
        real_flock(fd, flags)
        recorder.record("flock", inode=os.fstat(fileno).st_ino, flags=flags)

    monkeypatch.setattr(fcntl, "flock", recording_flock)

    real_save_all = SqliteStore.save_all

    def recording_save_all(self, *args, **kwargs):
        recorder.record("save_all_enter")
        try:
            return real_save_all(self, *args, **kwargs)
        finally:
            recorder.record("save_all_exit")

    monkeypatch.setattr(SqliteStore, "save_all", recording_save_all)

    real_write = SidecarManager.write_frame

    def recording_write(self, data, compression='zlib'):
        recorder.record("write_frame", sites=_sites_on_stack())
        return real_write(self, data, compression)

    monkeypatch.setattr(SidecarManager, "write_frame", recording_write)
    return recorder


def _write_ct_with_icon(folder):
    """A CT file with a top-level frame and one decodable icon (sites 1, 2)."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT_GATE", "DOE^GATE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "CT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 0
    ds.PixelData = np.arange(16, dtype=np.uint8).tobytes()
    icon = Dataset()
    icon.Rows = icon.Columns = 2
    icon.BitsAllocated = icon.BitsStored = 8
    icon.HighBit = 7
    icon.SamplesPerPixel = 1
    icon.PhotometricInterpretation = "MONOCHROME2"
    icon.PixelRepresentation = 0
    icon.add_new(0x7FE00010, 'OB', bytes([11, 22, 33, 44]))
    ds.IconImageSequence = Sequence([icon])
    path = os.path.join(folder, "ct_icon.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _write_native_ybr(folder):
    """A native 8-bit YBR_FULL file, which pydicom's read door converts.

    `ds.pixel_array` returns RGB for an 8-bit YBR source and says so only
    in its decoder's meta, so `get_pixel_data()`'s pydicom arm publishes
    *with* a relabel (#482). That is the one branch of
    `_publish_loaded_frame` that calls `_relabel_to_decoded_colour`
    inside the hold, and no sidecar read reaches it: the loader converts
    nothing, so every other load here publishes with `relabel=None`.
    """
    rgb = (np.arange(48, dtype=np.int64) * 5).astype(np.uint8).reshape(4, 4, 3)
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CT_IMAGE
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PAT_GATE", "DOE^GATE"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = CT_IMAGE
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows = ds.Columns = 4
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.SamplesPerPixel, ds.PlanarConfiguration = 3, 0
    ds.PhotometricInterpretation = "YBR_FULL"
    ds.PixelRepresentation = 0
    ds.PixelData = convert_color_space(rgb, "RGB", "YBR_FULL").tobytes()
    path = os.path.join(folder, "native_ybr.dcm")
    ds.save_as(path, enforce_file_format=True)
    return path


def _relabelling_instance(folder):
    """A hand-built instance over that file, carrying the file's label.

    `_relabel_to_decoded_colour` writes only when the instance already
    carries a PhotometricInterpretation -- a bare instance holds no
    descriptors, so nothing on it is false -- so the label is set here.
    """
    inst = Instance(generate_uid(), CT_IMAGE, 1,
                    file_path=_write_native_ybr(folder))
    inst.attributes["0028,0004"] = "YBR_FULL"
    return inst


def _inode_or_none(path):
    try:
        return os.stat(path).st_ino
    except FileNotFoundError:
        return None


def _make_instance(uid):
    inst = Instance(uid, CT_IMAGE, 1, file_path=None)
    inst.set_attr("0028,0010", 8)
    inst.set_attr("0028,0011", 8)
    inst.set_attr("0028,0002", 1)
    inst.set_attr("0028,0100", 8)
    inst.set_attr("0028,0103", 0)
    inst.set_pixel_data(np.arange(64, dtype=np.uint8).reshape(8, 8))
    return inst


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """Every writer once, under instrumentation; yields the recorder."""
    # Nothing here contends for the gate, so 5 s is generous -- and it
    # is what makes an order violation *fail* instead of stall: a
    # `compact()` that took the gate before its leading `save(sync=True)`
    # would deadlock site 6 against itself for the full deadline.
    monkeypatch.setattr(persistence_module, "_SIDECAR_GATE_TIMEOUT_S", 5.0)
    session = DicomSession(persistence_file=str(tmp_path / "order.db"))
    try:
        patient = Patient("P_ORDER", "Order Test")
        study = Study("S_ORDER", "20230101")
        series = Series("SE_ORDER", "CT", 1,
                        Equipment("ACME", "SCAN", "SN_ORDER"))
        patient.studies.append(study)
        study.series.append(series)
        session.store.patients.append(patient)
        first, second = _make_instance("1.2.3.A"), _make_instance("1.2.3.B")
        series.instances.extend([first, second])

        recorder = _instrument(session, monkeypatch)

        session.save(sync=True)                                   # site 6
        first.set_pixel_data(np.full((8, 8), 9, dtype=np.uint8))
        session.store_backend.persist_pixel_data(first)           # site 5
        first.discard_pixel_data()                                # leaf
        # A load into the emptied slot: the read publish (#465, leaf).
        # Nothing else here reads an unloaded instance, so without this
        # line `_publish_loaded_frame` never takes the lock.
        first.get_pixel_data()
        # ... and one that *relabels* while it publishes, so
        # `_relabel_to_decoded_colour` runs inside the same hold. The
        # line above takes the sidecar loader, which converts nothing and
        # so publishes with `relabel=None`: without this one the relabel
        # branch of the publish is never measured, and a lock or a log
        # added inside it would be invisible here (the review of #514).
        relabelling = _relabelling_instance(str(tmp_path))
        assert relabelling.get_pixel_data().shape == (4, 4, 3)
        assert relabelling.attributes["0028,0004"] == "RGB"
        # A descriptor edit over unsaved resident pixels: the write and
        # the republish in one hold (#531, leaf). Discarded and reloaded
        # after, so everything below sees `first` as it did before.
        first.set_pixel_data(np.full((8, 8), 200, dtype=np.uint8))
        first.set_attr("0028,0103", 1)
        assert first.pixel_array.dtype == np.int8
        assert first.discard_pixel_data() is True
        first.get_pixel_data()
        # The dedup arm: the same bytes under a new dtype (leaf).
        second.set_pixel_data(second.get_pixel_data().view(np.int8))
        session.save(sync=True)
        assert second.unload_pixel_data() is True                 # leaf
        # The redaction rebind, onto the loader the instance already has.
        DicomSession._apply_redaction_outcomes(
            [RedactionOutcome(ok=True, sop_instance_uid=first.sop_instance_uid,
                              mutation={"original_sop_uid": first.sop_instance_uid,
                                        "pixel_loader": first._pixel_loader,
                                        "pixel_hash": first._pixel_hash})],
            {first.sop_instance_uid: first},
            store_backend=session.store_backend)
        session.store_backend.persist_blob(
            second, 'waveform', np.arange(64, dtype=np.int16))    # site 4
        src = tmp_path / "src"
        src.mkdir()
        _write_ct_with_icon(str(src))
        write_fixture(str(src / "ecg.dcm"), num_samples=64)
        summary = session.ingest(str(src))                        # sites 1-3
        assert summary.failed == 0, summary.failures
        session.compact()

        # The lock files are created by their first acquisition. A path
        # nothing has taken has no inode to match, and `None` matches
        # no recorded call -- so a missing pass-lock reads as "compact()
        # never attempted the pass-lock" in the test that asks, rather
        # than as a fixture error that hides the other three.
        recorder.pass_lock_inode = _inode_or_none(
            session.store_backend._pass_lock_path())
        recorder.gate_inode = _inode_or_none(
            session.store_backend._gate_path())
        yield recorder
    finally:
        session.close()


def test_the_gate_is_never_taken_by_a_thread_holding_the_swap_lock(recorded):
    """`_sidecar_gate -> _pixel_swap_lock`, never the reverse (#368)."""
    gate_acquires = [e for e in recorded.log
                     if e["event"] == "acquire" and e["lock"] == "gate"]
    assert gate_acquires, "the gate was never acquired; nothing was measured"
    violations = [e for e in gate_acquires if "swap" in e["held"]]
    assert not violations, (
        "the gate was acquired by a thread already holding _pixel_swap_lock "
        "(sites on the stack: %s). That is the reverse of the order the "
        "rewire needs and deadlocks against it (#368)"
        % ([sorted(e["sites"]) for e in violations],))
    # And the forward arm is exercised, not merely un-violated.
    swap_under_gate = [e for e in recorded.log
                       if e["event"] == "acquire" and e["lock"] == "swap"
                       and "gate" in e["held"]]
    assert swap_under_gate, (
        "_pixel_swap_lock was never taken under the gate; the rewire and "
        "sites 5/6 are expected to do exactly that")


def test_every_write_frame_call_happens_with_the_gate_held(recorded):
    """Six sites, every call gated -- by the *calling* thread (#368).

    The gate is a threading.Lock in front of a flock, so "held" here
    means held by the thread that is writing, which is the only thing
    that stops that thread's frame landing in a file `compact()` is
    replacing.
    """
    writes = [e for e in recorded.log if e["event"] == "write_frame"]
    assert writes, "no frame was written; nothing was measured"
    ungated = [sorted(e["sites"]) for e in writes if "gate" not in e["held"]]
    assert not ungated, (
        "write_frame was called without the gate held, from %s (#368)"
        % (ungated,))
    covered = set().union(*(e["sites"] for e in writes))
    assert covered == _SITE_FUNCTIONS, (
        "these site functions never wrote a frame under this fixture, so "
        "their gating was not measured: %s" % (
            sorted(_SITE_FUNCTIONS - covered),))
    from_ingest = [e for e in writes if "import_files" in e["sites"]]
    assert len(from_ingest) >= 3, (
        "the ingest fixture reached %d of import_files' three sites "
        "(pixel, nested icon, waveform)" % len(from_ingest))


def test_compact_takes_the_pass_lock_before_its_leading_save_holding_nothing(
        recorded):
    """pass-lock `LOCK_EX|LOCK_NB`, holding nothing -> `save_all` -> gate (#368).

    The EX attempt is the first thing `compact()` does, before its
    leading `save(sync=True)`: a pass that opens after that save's rows
    are written and closes before the rewrite would otherwise be
    admitted with its `instances` rows on the old UIDs and its blob rows
    on the new ones, and the rewrite reclaims every worker frame (the
    window the review of PR #385 reproduced). Holding nothing, so the
    order stays acyclic -- `redact()`/`ingest()` take their SH holding
    nothing, and no site takes the pass-lock under the gate any more.
    `LOCK_NB` still: the refusal is an answer, not a wait.
    """
    log = recorded.log
    attempts = [i for i, e in enumerate(log)
                if e["event"] == "flock"
                and e.get("inode") == recorded.pass_lock_inode
                and e["flags"] & fcntl.LOCK_EX]
    assert attempts, "compact() never attempted the pass-lock"
    for i in attempts:
        attempt = log[i]
        assert not attempt["held"], (
            "the pass-lock EX attempt was made holding %s; compact() must "
            "take it before its leading save, holding nothing (#368)"
            % (sorted(attempt["held"]),))
        assert attempt["flags"] & fcntl.LOCK_NB, (
            "the pass-lock EX attempt is blocking; it must be LOCK_NB, the "
            "refusal is an answer and not a wait (#368)")
        after = log[i + 1:]
        save_enter = next((j for j, e in enumerate(after)
                           if e["event"] == "save_all_enter"), None)
        gate_take = next((j for j, e in enumerate(after)
                          if e["event"] == "acquire" and e["lock"] == "gate"),
                         None)
        assert save_enter is not None and gate_take is not None, (
            "after the pass-lock EX attempt compact() must run its leading "
            "save and then take the gate; saw save_all_enter=%r, gate=%r"
            % (save_enter, gate_take))
        assert save_enter < gate_take, (
            "compact() took the gate before its leading save_all; the EX "
            "attempt precedes the save, and the gate follows it (#368)")
    shared = [e for e in recorded.log
              if e["event"] == "flock"
              and e.get("inode") == recorded.pass_lock_inode
              and e["flags"] & fcntl.LOCK_SH]
    assert shared, "ingest() never took the pass-lock shared"
    for hold in shared:
        assert not hold["held"], (
            "the pass-lock was taken shared while holding %s; a pass "
            "opens holding nothing, or the order has a cycle (#368)"
            % (sorted(hold["held"]),))


def test_compact_takes_the_gate_only_after_its_leading_save_returns(recorded):
    """`save(sync=True)` runs site 6 on this thread; the gate must be free.

    A `compact()` that took the gate first would deadlock its own leading
    save on site 6 -- bounded by `_SIDECAR_GATE_TIMEOUT_S`, so a stall
    rather than a hang, but a stall on every compaction.
    """
    entries = [e for e in recorded.log if e["event"] == "save_all_enter"]
    assert entries, "save_all never ran"
    held_at_entry = [sorted(e["held"]) for e in entries if e["held"]]
    assert not held_at_entry, (
        "save_all was entered with %s held; compact() must take the gate "
        "only after its leading save(sync=True) has returned (#368)"
        % (held_at_entry,))


def _write_frame_sites():
    """Every `write_frame` call under isocenter/, by (file, enclosing def)."""
    found = collections.Counter()
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_frame"):
                continue
            scope = parents.get(node)
            while scope is not None and not isinstance(
                    scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents.get(scope)
            found[(path.relative_to(REPO).as_posix(),
                   scope.name if scope is not None else "<module>")] += 1
    return dict(found)


def test_there_are_exactly_six_write_frame_sites_and_they_are_these():
    """The seventh-site detector (#368).

    Red before anyone asks whether a new site is gated. If this fails
    because you added a `write_frame` call: gate it (append *and* row
    commit, outside `_pixel_swap_lock`, never inside `write_frame`), add
    it to the fixture above so the gating is measured, and then add it
    to `_WRITE_FRAME_SITES`.
    """
    assert _write_frame_sites() == _WRITE_FRAME_SITES


#: Everything that takes the pixel-state leaf, by function name (#434, Q6).
_LEAF_TAKERS = {"set_pixel_data", "discard_pixel_data", "unload_pixel_data",
                "_publish_loaded_frame",
                # `Instance.set_attr`, for a tag the loader reads (#531).
                "set_attr",
                "_swap_pixels_under_gate", "_persist_pixels",
                "_apply_redaction_outcomes"}


def test_the_pixel_state_lock_is_a_leaf(recorded):
    """`... -> _pixel_swap_lock -> PIXEL_STATE_LOCK`, and nothing under it.

    The leaf is taken by the pixel mutators holding nothing and by the
    publish sections under the gate and the swap lock. While it is held
    no other lock is taken -- not the gate, not the swap lock, not a
    flock (so no frame write) -- which is what keeps it out of every
    cycle the rest of this file rules out.
    """
    leaf = [e for e in recorded.log
            if e["event"] == "acquire" and e["lock"] == "leaf"]
    assert leaf, "the pixel-state lock was never taken; nothing was measured"
    takers = set().union(*(e["callers"] for e in leaf)) & _LEAF_TAKERS
    assert takers == _LEAF_TAKERS, (
        "these never took the pixel-state lock under this fixture: %s"
        % sorted(_LEAF_TAKERS - takers))
    under_leaf = [e for e in recorded.log
                  if e["event"] in ("acquire", "flock", "write_frame",
                                    "save_all_enter")
                  and "leaf" in e["held"]
                  and not (e["event"] == "acquire" and e["lock"] == "leaf")]
    assert not under_leaf, (
        "something was taken while the pixel-state lock was held: %s"
        % [(e["event"], e.get("lock")) for e in under_leaf])
    publishes = [e for e in leaf if e["callers"] & {
        "_swap_pixels_under_gate", "_persist_pixels"}]
    assert publishes and all("swap" in e["held"] for e in publishes), (
        "a publish section took the pixel-state lock outside "
        "_pixel_swap_lock")


#: The pixel state the leaf guards: the unwritten flag (#293) and the
#: descriptor record (#434).
_PIXEL_STATE_FIELDS = ("_pixel_array_unwritten", "_pixel_descriptors_replaced")

#: Helpers that write that state for a caller already holding the leaf.
#: Checked below, transitively: every call to one is itself under the
#: leaf, or inside another of these.
_CALLER_HOLDS = {"_replace_pixel_array", "_drop_resident_array",
                 "_restore_replaced_descriptors"}

#: Every write of the pixel state under isocenter/, by (file, enclosing
#: def): how many, and how each is serialised against `PIXEL_STATE_LOCK`.
#: "leaf" -- lexically inside `with ... PIXEL_STATE_LOCK`; "caller holds"
#: -- in one of `_CALLER_HOLDS`; anything else is unlocked, and names the
#: issue that says why. Keyed on function names, like
#: `_WRITE_FRAME_SITES`, so it is green through unrelated edits and red on
#: a new site wherever it lands.
_PIXEL_STATE_WRITES = {
    ("isocenter/entities.py", "_replace_pixel_array"): (2, "caller holds"),
    ("isocenter/entities.py", "_restore_replaced_descriptors"): (1, "caller holds"),
    # The three read arms -- loader, file, imagecodecs fallback -- publish
    # through this one helper, under the leaf, only while the slot is
    # still empty (#465).
    ("isocenter/entities.py", "_publish_loaded_frame"): (1, "leaf"),
    ("isocenter/persistence.py", "_swap_pixels_under_gate"): (2, "leaf"),
    ("isocenter/persistence.py", "_persist_pixels"): (4, "leaf"),
    ("isocenter/session.py", "_apply_redaction_outcomes"): (1, "leaf"),
}


def _is_leaf_with(node):
    return isinstance(node, ast.With) and any(
        (isinstance(item.context_expr, ast.Name)
         and item.context_expr.id == "PIXEL_STATE_LOCK")
        or (isinstance(item.context_expr, ast.Attribute)
            and item.context_expr.attr == "PIXEL_STATE_LOCK")
        for item in node.items)


def _scope_of(node, parents):
    """The enclosing def's name, and whether a leaf `with` lies between."""
    in_leaf = False
    scope = parents.get(node)
    while scope is not None and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        in_leaf = in_leaf or _is_leaf_with(scope)
        scope = parents.get(scope)
    return (scope.name if scope is not None else "<module>"), in_leaf


def _attribute_targets(node):
    """Every attribute name a statement binds or deletes (#477).

    Tuple and list targets are unpacked, nested ones too, and a starred
    target is read through: `a.x, (b.y, *c.z) = ...` writes all three.

    Assignment is not the only place Python binds a name, and the other
    three bind an attribute in exactly the same way: a `for`/`async for`
    target, a `with`/`async with` `as` clause, and a comprehension's own
    `for` target. The review of #514 measured all three passing the
    detector, and they are read here rather than excluded because each
    is a *literal* name a reader finds by searching for the field --
    which is the line this detector draws (see `_writes_pixel_state`).
    """
    if isinstance(node, (ast.Assign, ast.Delete)):
        pending = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.For,
                           ast.AsyncFor, ast.comprehension)):
        pending = [node.target]
    elif isinstance(node, ast.withitem):
        pending = [] if node.optional_vars is None else [node.optional_vars]
    else:
        return []
    names = []
    while pending:
        target = pending.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            pending.extend(target.elts)
        elif isinstance(target, ast.Starred):
            pending.append(target.value)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


def _writes_pixel_state(node):
    """Does this node write or delete a field of the pixel state? (#477)

    Plain, tuple/list (nested, starred), augmented and annotated
    assignment; a `for`/`async for` target, a `with`/`async with` `as`
    clause and a comprehension's `for` target; `del`; and
    `setattr`/`delattr` whose name is a string literal.

    **Not covered, deliberately:** a name computed at runtime
    (`setattr(self, name, ...)`), `object.__setattr__`, and
    `vars(self)[...]` -- `Instance` is a slots dataclass with no
    `__dict__` for that to reach. A computed name is not a site a reader
    can find by searching for the field either; matching every
    `setattr` would put every dynamic write in the package on the list.
    That is the whole of the exclusion: every form that names the field
    outright is matched. The list said less than that until the review
    of #514 measured `for self._x in ...`, `with ... as self._x` and a
    comprehension target each reported as no write at all.
    """
    if any(name in _PIXEL_STATE_FIELDS for name in _attribute_targets(node)):
        return True
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("setattr", "delattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _PIXEL_STATE_FIELDS)


def _pixel_state_sites_in(source, rel):
    """Writes of the pixel state, and calls of `_CALLER_HOLDS`, in one module.

    Takes source text rather than a path so the per-form tests below can
    feed it a snippet without editing the package (#477).
    """
    writes = collections.defaultdict(list)
    calls = collections.defaultdict(list)
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree)
               for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if _writes_pixel_state(node):
            scope, in_leaf = _scope_of(node, parents)
            writes[(rel, scope)].append(in_leaf)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CALLER_HOLDS):
            scope, in_leaf = _scope_of(node, parents)
            calls[node.func.attr].append((rel, scope, in_leaf))
    return writes, calls


def _pixel_state_sites():
    """Writes of the pixel state, and calls of `_CALLER_HOLDS`, by site."""
    writes = collections.defaultdict(list)
    calls = collections.defaultdict(list)
    for path in sorted((REPO / "isocenter").rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        module_writes, module_calls = _pixel_state_sites_in(
            path.read_text(encoding="utf-8"), rel)
        for site, in_leaf in module_writes.items():
            writes[site].extend(in_leaf)
        for helper, sites in module_calls.items():
            calls[helper].extend(sites)
    return writes, calls


def test_every_write_of_the_pixel_state_is_under_the_leaf_or_listed():
    """The pixel-state site detector (#434, Q6; review of #466).

    `_LEAF_TAKERS` above shows each taker took the lock under one fixture;
    it cannot see a write moved out from under it. This reads the source:
    every write of the unwritten flag or the descriptor record is inside
    `with ... PIXEL_STATE_LOCK`, or in a helper every call to which is, or
    is listed as unlocked with the issue that says why. If this fails
    because you added a write: put it under the leaf and add it to
    `_PIXEL_STATE_WRITES`. An unlocked one needs an issue.
    """
    writes, calls = _pixel_state_sites()
    assert {k: len(v) for k, v in writes.items()} == {
        k: n for k, (n, _how) in _PIXEL_STATE_WRITES.items()}
    for (rel, scope), (_n, how) in _PIXEL_STATE_WRITES.items():
        if how == "leaf":
            assert all(writes[(rel, scope)]), (
                f"{rel}:{scope} writes the pixel state outside PIXEL_STATE_LOCK")
        elif how == "caller holds":
            assert scope in _CALLER_HOLDS, f"{scope} is not in _CALLER_HOLDS"
        else:
            assert how.startswith("unlocked: #"), (
                f"{rel}:{scope} is listed as unlocked with no issue: {how!r}")
    for helper in sorted(_CALLER_HOLDS):
        assert calls[helper], f"{helper} is never called; drop it from _CALLER_HOLDS"
        outside = [(rel, scope) for rel, scope, in_leaf in calls[helper]
                   if not in_leaf and scope not in _CALLER_HOLDS]
        assert not outside, (
            f"{helper} writes the pixel state for a caller holding "
            f"PIXEL_STATE_LOCK, and is called without it from {outside}")


def _snippet_sites(snippet):
    writes, _calls = _pixel_state_sites_in(snippet, "<snippet>")
    return {site: len(in_leaf) for site, in_leaf in writes.items()}


@pytest.mark.parametrize("snippet", [
    "self._pixel_array_unwritten = False",
    "self._pixel_array_unwritten, other.x = False, None",
    "[other.x, (self._pixel_descriptors_replaced, other.y)] = 1, (2, 3)",
    "(other.x, (*self._pixel_descriptors_replaced, other.y)) = 1, (2, 3)",
    "*self._pixel_descriptors_replaced, x = 1, 2",
    "self._pixel_array_unwritten |= True",
    "self._pixel_array_unwritten: bool = False",
    "del self._pixel_descriptors_replaced",
    "del other.x, self._pixel_array_unwritten",
    "setattr(self, '_pixel_array_unwritten', False)",
    "delattr(self, '_pixel_descriptors_replaced')",
    "for self._pixel_array_unwritten in (True, False):\n        pass",
    "with other as self._pixel_descriptors_replaced:\n        pass",
    "x = [other for self._pixel_array_unwritten in (True,)]",
], ids=["plain", "tuple", "nested list", "starred in a nested tuple",
        "starred", "augassign", "annassign", "del", "del of two",
        "setattr", "delattr", "for target", "with as", "comprehension"])
def test_the_detector_sees_every_form_of_write(snippet):
    """One write of the pixel state, in each form Python spells one (#477).

    The detector matched `self.<field> = ...` and augmented and annotated
    assignment only, so a tuple target, `del`, and `setattr`/`delattr`
    with a literal name wrote the state past it (the #466 re-review's
    D2-D4). Each form is fed to the detector as a one-statement function,
    and must be reported as exactly one write in it. `augassign` and
    `annassign` were already seen; they are here so a simplification of
    the detector cannot drop them unnoticed. The last three are the
    review of #514's: a `for` target, a `with ... as` clause and a
    comprehension's target each reported no write at all.
    """
    source = "def f(self, other):\n    %s\n" % snippet
    assert _snippet_sites(source) == {("<snippet>", "f"): 1}


@pytest.mark.parametrize("snippet", [
    "async for self._pixel_array_unwritten in other:\n        pass",
    "async with other as self._pixel_descriptors_replaced:\n        pass",
    "x = [y async for self._pixel_array_unwritten in other]",
], ids=["async for", "async with as", "async comprehension"])
def test_the_detector_sees_the_async_binding_forms(snippet):
    """The `async` spellings of the three forms above (#477).

    Separate only because they need an `async def` around them. `ast`
    gives `AsyncFor` its own node type, while `async with` and an
    `async for` inside a comprehension reuse `withitem` and
    `comprehension`, so this is one new arm and two already covered --
    which is exactly the kind of asymmetry a test should hold rather
    than a reader infer.
    """
    source = "async def f(self, other):\n    %s\n" % snippet
    assert _snippet_sites(source) == {("<snippet>", "f"): 1}


def test_the_detector_ignores_other_attributes_and_computed_names():
    """The negative half: not every `setattr` is a site (#477).

    A computed name cannot be read from the source by the detector or by
    a reader, and is deliberately out of scope; matching every `setattr`
    would put every dynamic write in the package on the allow-list.
    """
    assert _snippet_sites(
        "def f(self, name):\n"
        "    self.other = 1\n"
        "    setattr(self, name, 2)\n"
        "    setattr(self, 'other', 3)\n"
        "    del self.other\n"
        "    other_unwritten = self._pixel_array_unwritten\n"
        "    for self.other in (1, 2):\n"
        "        pass\n"
        "    with self as self.other:\n"
        "        pass\n"
        "    x = [y for self.other in (1,)]\n"
        "    with self:\n"
        "        pass\n") == {}


class _PausingLock:
    """A lock that parks the first `_persist_pixels` holder until told."""

    def __init__(self):
        self._lock = threading.Lock()
        self.inside = threading.Event()
        self.go = threading.Event()
        self._paused = False

    def acquire(self, blocking=True, timeout=-1):
        ok = self._lock.acquire(blocking, timeout)
        if ok and not self._paused and "_persist_pixels" in _functions_on_stack():
            self._paused = True
            self.inside.set()
            self.go.wait(30)
        return ok

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


@pytest.mark.parametrize("kind", ["new bytes", "same bytes, new dtype"])
def test_a_discard_waits_for_a_publish_holding_the_pixel_state_lock(
        tmp_path, monkeypatch, kind):
    """The window Q6 closes, with two real threads (#434).

    Both publishing arms of `_persist_pixels`: new bytes append a frame
    and publish after the revision guard; the same bytes under a new
    dtype take the dedup arm, which rebuilds the loader in place. Each
    takes the lock itself, so each is parked in turn.

    The save is parked inside `_persist_pixels`' publish section, holding
    the leaf; a discard on another thread must wait for it rather than
    restore the pre-set descriptors over the frame being published. Once
    the publish finishes the replacement is the stored frame, so the
    discard has nothing to undo and the instance reads the replacement.
    """
    session = DicomSession(persistence_file=str(tmp_path / "leaf.db"))
    pausing = _PausingLock()
    try:
        patient = Patient("P_LEAF", "Leaf Test")
        study = Study("S_LEAF", "20230101")
        series = Series("SE_LEAF", "CT", 1, Equipment("ACME", "SCAN", "SN_LEAF"))
        patient.studies.append(study)
        study.series.append(series)
        session.store.patients.append(patient)
        inst = _make_instance("1.2.3.LEAF")
        series.instances.append(inst)
        session.save(sync=True)
        replacement = (np.full((4, 4), 9, dtype=np.uint16) if kind == "new bytes"
                       else inst.get_pixel_data().view(np.int8))
        inst.set_pixel_data(replacement)
        after_set = dict(inst.attributes)

        monkeypatch.setattr(entities_module, "PIXEL_STATE_LOCK", pausing,
                            raising=False)
        saver = threading.Thread(target=session.save, kwargs={"sync": True})
        saver.start()
        assert pausing.inside.wait(10), (
            "the save never took the pixel-state lock in _persist_pixels")

        discarded = []
        discarder = threading.Thread(
            target=lambda: discarded.append(inst.discard_pixel_data()))
        discarder.start()
        discarder.join(0.5)
        assert discarder.is_alive(), (
            "discard_pixel_data() ran while a publish held the "
            "pixel-state lock")

        pausing.go.set()
        saver.join(30)
        discarder.join(30)
        assert not saver.is_alive() and not discarder.is_alive()
        assert discarded == [True]
        assert {t: inst.attributes[t] for t in ("0028,0010", "0028,0011", "0028,0100")} == {
            t: after_set[t] for t in ("0028,0010", "0028,0011", "0028,0100")}
        assert np.array_equal(inst.get_pixel_data(), replacement)
    finally:
        pausing.go.set()
        session.close()


class _LockProbe(logging.Handler):
    """Records, per log line, whether the pixel-state lock was held."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.seen = []

    def emit(self, record):
        self.seen.append((record.getMessage(),
                          entities_module.PIXEL_STATE_LOCK.locked()))


def test_nothing_logs_while_the_pixel_state_lock_is_held(tmp_path):
    """A logging handler takes its own lock, so the leaf defers its lines.

    `set_pixel_data`'s correction notes and the discard refusal are the
    lines emitted around the lock; each must be emitted after release.
    Single-threaded, so `locked()` is this thread's own hold.

    **Every taker goes through this, the read publish included.** This
    probe drove `set_pixel_data` and `discard_pixel_data` on a
    memory-only instance and nothing else, so it performed no *load* and
    `_publish_loaded_frame` -- the fifth taker (#465) -- was invisible to
    it: the review of #514 put a `get_logger().info(...)` inside that
    hold and all 22 tests in this file stayed green, with the line
    observed executing under `PIXEL_STATE_LOCK.locked()`. So both
    publishing branches are driven here, the sidecar-shaped one with
    `relabel=None` and the pydicom one that relabels, parallel to the
    two loads the `recorded` fixture adds for the lock half of the same
    invariant. A log call added inside the publish is now red here.
    """
    logger = get_logger()
    probe = _LockProbe()
    level = logger.level
    logger.addHandler(probe)
    logger.setLevel(logging.DEBUG)
    try:
        inst = Instance("1.2.3.LOG", CT_IMAGE, 1, file_path=None)
        inst.set_attr("0028,0100", 16)
        inst.set_pixel_data(np.zeros((4, 4), dtype=np.uint8))   # BitsAllocated 16 -> 8
        assert inst.discard_pixel_data() is False              # memory only
        # A descriptor edit over those unsaved pixels (#531): republished
        # in the write's hold, and refused before it. Then one over a
        # memory-only array a save never set, whose release is refused
        # with a line -- emitted after the hold, not inside it.
        inst.set_attr("0028,0103", 1)
        assert inst.pixel_array.dtype == np.int8
        with pytest.raises(ValueError, match="would read the unsaved"):
            inst.set_attr("0028,0010", 99)
        assigned = Instance("1.2.3.LOG.ASSIGNED", CT_IMAGE, 1, file_path=None)
        assigned.pixel_array = np.zeros((4, 4), dtype=np.uint8)
        assigned.set_attr("0028,0103", 1)
        assert assigned.pixel_array is not None
        # The read publish, both branches. Asserted to have published --
        # the loader's own array back, and the relabel written -- so a
        # refactor that stops loading cannot leave this probe passing
        # vacuously.
        loaded = np.arange(16, dtype=np.uint8).reshape(4, 4)
        lazy = Instance("1.2.3.LOG.LOAD", CT_IMAGE, 1, file_path=None)
        lazy._pixel_loader = lambda: loaded                    # relabel None
        assert lazy.get_pixel_data() is loaded
        relabelling = _relabelling_instance(str(tmp_path))     # relabel RGB
        assert relabelling.get_pixel_data().shape == (4, 4, 3)
        assert relabelling.attributes["0028,0004"] == "RGB"
    finally:
        logger.removeHandler(probe)
        logger.setLevel(level)
    corrected = [held for msg, held in probe.seen if "BitsAllocated" in msg]
    refused = [held for msg, held in probe.seen if "held in memory only" in msg]
    assert corrected and len(refused) == 2, probe.seen
    assert not any(held for _msg, held in probe.seen), probe.seen
