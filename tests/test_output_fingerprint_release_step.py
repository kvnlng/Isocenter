"""The output fingerprint's wiring: the run, the committed cohort, the release step (#717).

The recorder and comparer are pinned in `test_output_fingerprint.py`.
Here: that a real run is deterministic under the fixed secret (and would
not be without it), that the tracked recording describes the committed
cohort, and that RELEASING.md runs the check. Only the first three tests
open sessions (the two-member mini cohort, or one member of it; a few
seconds per take).

As in the sibling file, no package module is named by its dotted name:
the pipeline is reached through `scripts.output_fingerprint` alone, so
the mutation probe's target scan does not pull these slow tests into a
module's row.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pydicom
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import output_fingerprint as fp  # noqa: E402

COHORT = ROOT / "fingerprint" / "cohort"
RELEASING = ROOT / "RELEASING.md"
PYDICOM_GENERATED_ROOT = "1.2.826.0.1.3680043.8.498."


@pytest.fixture(scope="module")
def mini_cohort(tmp_path_factory):
    """Two committed members: a CT carrying '1.000000' DS values, and the ECG."""
    root = tmp_path_factory.mktemp("cohort")
    for name in ("private_nested", "ecg"):
        shutil.copytree(COHORT / name, root / name)
    return root


@pytest.fixture(scope="module")
def two_takes(mini_cohort, tmp_path_factory):
    for name in fp.REFUSED_ENV:
        assert name not in os.environ, name
    out = tmp_path_factory.mktemp("takes")
    takes = {}
    for label, jobs in (("first", 1), ("second", 4)):
        path = out / f"{label}.json"
        fp.take(path, cohort_root=mini_cohort, pydicom_sets=False, jobs=jobs,
                log=lambda line: None)
        takes[label] = json.loads(path.read_text(encoding="utf-8"))
    return takes


def test_two_takes_of_a_small_cohort_are_identical(two_takes, mini_cohort, tmp_path):
    first, second = two_takes["first"], two_takes["second"]
    assert sorted(first["members"]) == ["synthetic:ecg", "synthetic:private_nested"]
    # Two runs, in one process and in four: the same recording. A store
    # that minted its own secret would still be self-consistent within a
    # run, so only the second run can tell the fixed secret was loaded.
    assert first["members"] == second["members"]
    assert first["measure"] == second["measure"] == fp.measure()
    assert fp.compare(first, second).exit_code == 0

    other = tmp_path / "other.json"
    fp.take(other, cohort_root=mini_cohort, pydicom_sets=False,
            secret=bytes(range(1, 33)), log=lambda line: None)
    moved = json.loads(other.read_text(encoding="utf-8"))
    paths = lambda fpr: sorted(  # noqa: E731
        p for m in fpr["members"].values() for c in m["configs"].values()
        for a in c["arms"].values() for p in a["files"])
    assert paths(first) and not set(paths(first)) & set(paths(moved))


def test_the_reopened_arm_exports_what_the_live_arm_exports(two_takes):
    """#662, fixed: a reopened store keeps a DS value's text.

    Until L6 this test pinned the defect (`'1.000000'` live, `'1.0'`
    reopened), and that difference was also what proved the reopened arm
    exported from a reopened store. With the two now equal it proves
    nothing of the kind, so that half moved to
    `test_the_reopened_arm_runs_on_a_second_session_over_the_same_store`.
    """
    arms = two_takes["first"]["members"]["synthetic:private_nested"]["configs"]["A"]["arms"]
    (live,) = arms["A.dicom"]["files"].values()
    (reopened,) = arms["A.reopened.dicom"]["files"].values()
    assert live["elements"]["0018,0050"] == "DS/implicit '1.000000'"
    assert reopened["elements"]["0018,0050"] == "DS/implicit '1.000000'"


def test_the_reopened_arm_runs_on_a_second_session_over_the_same_store(
        mini_cohort, tmp_path, monkeypatch):
    """The reopened arm's export runs on a session opened after configuration
    A's session was closed, over the same store file. Killing mutation: the
    arm quietly re-using the live session -- which, since #662's fix, no
    exported byte of this member tells apart.

    In-process (`jobs=1`), so the spy reaches `_run_config`, which imports
    `Session` from `isocenter` at call time.
    """
    import itertools

    import isocenter

    events = []
    # A counter, not `id(self)`: CPython can hand the reopened session the
    # freed live session's id, and the two would read as one (review of #739).
    serial = itertools.count(1)

    class Spy(isocenter.Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._spy_serial = next(serial)
            events.append(("open", self._spy_serial, kwargs.get("persistence_file")))

        def __exit__(self, *exc):
            events.append(("close", self._spy_serial, None))
            return super().__exit__(*exc)

        def export(self, folder, *args, **kwargs):
            events.append(("export", self._spy_serial, Path(folder).name))
            return super().export(folder, *args, **kwargs)

    monkeypatch.setattr(isocenter, "Session", Spy)
    fp.take(tmp_path / "one.json", members="synthetic:private_nested",
            cohort_root=mini_cohort, pydicom_sets=False, jobs=1,
            log=lambda line: None)

    def session_of(arm):
        (sid,) = [s for kind, s, name in events if kind == "export" and name == arm]
        return sid

    live, reopened = session_of("A.dicom"), session_of("A.reopened.dicom")
    assert live != reopened
    opened = {s: (i, path) for i, (kind, s, path) in enumerate(events) if kind == "open"}
    closed = {s: i for i, (kind, s, _) in enumerate(events) if kind == "close"}
    assert opened[reopened][0] > closed[live]
    assert opened[reopened][1] == opened[live][1]
    assert Path(opened[live][1]).name == "store.db"


#: The committed cohort, file by file. A member is never removed and its
#: bytes never rebuilt to make a difference go away; adding one updates
#: this list, the cohort and the fingerprint in one change.
COMMITTED = {
    "big_endian_words": ["big_endian_words-1.dcm"],
    "curve_overlay": ["curve_overlay-1.dcm"],
    "ecg": ["ecg-1.dcm"],
    "float_pixels": ["float_pixels-1.dcm"],
    "implicit": ["implicit-1.dcm"],
    "longitudinal": ["longitudinal-s1-1.dcm", "longitudinal-s1-2.dcm",
                     "longitudinal-s1-3.dcm", "longitudinal-s2-1.dcm",
                     "longitudinal-s2-2.dcm", "longitudinal-s2-3.dcm"],
    "lut_ambiguous": ["lut_ambiguous-1.dcm"],
    "no_patient_id": ["no_patient_id-s1-1.dcm", "no_patient_id-s2-1.dcm",
                      "no_patient_id-s3-1.dcm"],
    "no_study_date": ["no_study_date-1.dcm"],
    "private_nested": ["private_nested-1.dcm"],
    "redacted": ["redacted-1.dcm", "VARIES"],
    "withheld": ["withheld-1.dcm"],
}


def test_the_committed_cohort_is_the_listed_one():
    found = {m.name: sorted(p.name for p in m.iterdir())
             for m in COHORT.iterdir() if m.is_dir()}
    assert found == {k: sorted(v) for k, v in COMMITTED.items()}


def test_the_generator_builds_what_is_listed_and_never_overwrites_a_member(tmp_path):
    from scripts import golden_cohort
    assert sorted(golden_cohort.MEMBERS) == sorted(COMMITTED)
    assert golden_cohort.uid("x", 1) == golden_cohort.uid("x", 1)
    assert golden_cohort.uid("x", 1).startswith("2.25.")

    written = golden_cohort.build(tmp_path, only={"implicit", "redacted"})
    assert sorted(written) == ["implicit", "redacted"]
    assert sorted(p.name for p in (tmp_path / "redacted").iterdir()) == \
        sorted(COMMITTED["redacted"])
    marker = tmp_path / "implicit" / "implicit-1.dcm"
    marker.write_bytes(b"committed bytes are the authority")
    assert golden_cohort.build(tmp_path, only={"implicit"}) == []
    assert marker.read_bytes() == b"committed bytes are the authority"


def test_configuration_a_redacts_only_the_redacted_member_and_b_keeps_private_tags():
    import yaml
    assert fp.CONFIGS == {"A": ROOT / "fingerprint" / "config-a.yaml",
                          "B": ROOT / "fingerprint" / "config-b.yaml"}
    a = yaml.safe_load(fp.CONFIGS["A"].read_text(encoding="utf-8"))
    b = yaml.safe_load(fp.CONFIGS["B"].read_text(encoding="utf-8"))
    serials = {str(pydicom.dcmread(str(p)).get("DeviceSerialNumber", ""))
               for p in COHORT.rglob("*.dcm")}
    ruled = {rule["serial_number"] for rule in a["machines"]}
    assert ruled == {"GOLD-SN-REDACT"}
    assert ruled & serials == ruled
    assert str(pydicom.dcmread(str(COHORT / "redacted" / "redacted-1.dcm"))
               .DeviceSerialNumber) == "GOLD-SN-REDACT"
    assert a["remove_private_tags"] is True and b["remove_private_tags"] is False
    assert "machines" not in b
    assert {k: v for k, v in a.items() if k not in ("machines", "remove_private_tags")} \
        == {k: v for k, v in b.items() if k != "remove_private_tags"}


def _uids(ds):
    for elem in ds.iterall():
        if elem.VR == "UI" and elem.value:
            yield from (elem.value if isinstance(elem.value, list) else [elem.value])


def test_the_committed_cohort_uses_no_random_uids():
    files = sorted(p for p in COHORT.rglob("*.dcm"))
    assert files
    offenders = []
    for path in files:
        ds = pydicom.dcmread(str(path))
        for uid in list(_uids(ds.file_meta)) + list(_uids(ds)):
            if str(uid).startswith(PYDICOM_GENERATED_ROOT):
                offenders.append(f"{path.relative_to(ROOT)}: {uid}")
    assert not offenders, offenders


def test_the_tracked_fingerprint_covers_the_committed_cohort():
    tracked = json.loads((ROOT / "fingerprint" / "output.json").read_text(encoding="utf-8"))
    assert tracked["schema"] == fp.SCHEMA
    # Taken with these configurations and this recorder.
    assert tracked["measure"] == fp.measure()
    # A whole-cohort take, not a narrowed one.
    assert "members" not in tracked["provenance"]
    keys = set(tracked["members"])
    assert any(k.startswith("pydicom:") for k in keys)
    assert any(k.startswith("pydicom-data:") for k in keys)

    for member in fp.synthetic_members(COHORT):
        entry = tracked["members"].get(member.key)
        assert entry is not None, f"{member.key} is committed but not in the fingerprint"
        assert entry["inputs"] == member.inputs, f"{member.key}: inputs changed, retake"
        assert bool(entry.get("varies")) == (member.varies is not None), member.key
    committed = {m.key for m in fp.synthetic_members(COHORT)}
    recorded = {k for k in keys if k.startswith("synthetic:")}
    assert recorded == committed


def test_an_l10_shaped_move_of_the_tracked_longitudinal_member_invents_nothing():
    """Every UID re-derived, nothing else touched: only UIDs and paths differ.

    The six `longitudinal` files are named by their SOP UIDs, and the new
    names sort in another order; paired by sorted path, instance 1 met
    instance 3 and the report listed pixel and position changes in 20
    files that never happened (#717 review, finding 1).
    """
    import hashlib
    tracked = json.loads((ROOT / "fingerprint" / "output.json").read_text(encoding="utf-8"))
    member = tracked["members"]["synthetic:longitudinal"]

    def remap(match):
        return "2.25." + str(int(hashlib.sha256(match.group(0).encode()).hexdigest()[:30], 16))

    moved = json.loads(re.sub(r"2\.25\.\d+", remap, json.dumps(member)))
    old = {"schema": fp.SCHEMA, "provenance": {}, "toolchain": {},
           "members": {"synthetic:longitudinal": member}}
    new = dict(old, members={"synthetic:longitudinal": moved})
    report = fp.compare(old, new)
    assert {g.kind for g in report.groups if g.section == "Paths"} == {"moved"}
    invented = [f"{g.key} {g.vr} {g.kind} x{g.count}" for g in report.groups
                if g.section in ("Elements", "Outcomes") and g.vr != "UI"]
    assert not invented, report.text()


def _section(text, heading):
    start = text.index(heading)
    following = text.find(" ## ", start + len(heading))
    return text[start:following if following != -1 else len(text)]


def test_the_release_procedure_runs_the_fingerprint_check():
    # Whitespace collapsed: a reflowed paragraph is the same procedure.
    text = re.sub(r"[ \t]*\n[ \t]*", " ", RELEASING.read_text(encoding="utf-8"))
    cutting = _section(text, "## Cutting a release")
    step1 = cutting[cutting.index("1. **Choose the commit**"):cutting.index("2. **Cut the branch:**")]
    assert "python -m scripts.output_fingerprint check" in step1
    assert "3.12 and 3.14t" in step1
    assert "compare --base" in step1
    assert "previous-tag" in step1
    assert "git fetch --tags origin" in step1
    assert "does not apply only when vP.Q.R is below `v1.0.0rc1`" in step1
    assert "pre-release" in step1
    assert "fetch_data_files" in step1
    step3 = cutting[cutting.index("3. **Make the release commit"):cutting.index("4. **Rehearse")]
    assert "output_fingerprint" in step3 and "--line" in step3

    landing = _section(text, "## Changes land on `main`")
    assert "take --out fingerprint/output.json" in landing
    assert "**Output:**" in landing
    assert "Never resolve a conflict in it by hand" in landing


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"],
                        "HOME": str(repo)})


def test_the_previous_release_is_found_by_version_not_reachability(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "tag", "v0.9.7")
    # A release branch, as RELEASING.md cuts one: its tags are not
    # reachable from main, and main's newest reachable tag is older.
    _git(repo, "switch", "-q", "-c", "release/1.0")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "rc")
    _git(repo, "tag", "v1.0.0rc1")
    assert fp.newest_release_tag(repo) == "v1.0.0rc1"
    _git(repo, "commit", "-q", "--allow-empty", "-m", "final")
    _git(repo, "tag", "v1.0.0")
    _git(repo, "tag", "not-a-release")
    _git(repo, "switch", "-q", "main")
    assert fp.newest_release_tag(repo) == "v1.0.0"
    _git(repo, "tag", "v0.9.10")
    assert fp.newest_release_tag(repo, line="0.9") == "v0.9.10"
    assert fp.newest_release_tag(repo, line="1.0") == "v1.0.0"
    assert fp.newest_release_tag(repo, line="2.0") is None


def test_a_missing_base_is_never_a_comparison_that_does_not_apply(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "tag", "v0.9.8")
    _git(repo, "tag", "v1.0.0")  # a fingerprinted-era tag without the file
    with pytest.raises(fp.ToolError, match="does not apply"):
        fp.fingerprint_at("v0.9.8", repo)
    with pytest.raises(fp.ToolError, match="release record is broken"):
        fp.fingerprint_at("v1.0.0", repo)
    with pytest.raises(fp.ToolError, match="no tag v9.9.9 in this clone"):
        fp.fingerprint_at("v9.9.9", repo)

    # A clone that has not fetched origin's newest tag is refused, not
    # answered with an older one.
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(repo), str(clone))
    _git(repo, "tag", "v1.0.1")
    assert fp.newest_release_tag(clone) == "v1.0.0"
    with pytest.raises(fp.ToolError, match="origin has v1.0.1"):
        fp.remote_tag_check("v1.0.0", clone)
    _git(clone, "fetch", "-q", "--tags", "origin")
    fp.remote_tag_check(fp.newest_release_tag(clone), clone)
