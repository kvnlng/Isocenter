"""The output fingerprint: what the golden cohort exports, and what moved (#717).

The promise it serves: for the same input, configuration and project
secret, a release exports what the previous release exported, or its
changelog says what changed. "The same" is whatever this tool measures,
and it measures semantics rather than bytes: each written file's relative
path, every element's VR and encoded value, the *decoded* pixel and
waveform samples, and the audit rows that account for what was and was
not delivered. `fingerprint/output.json` is the tracked recording.

Commands (run from the repository root; exit 0 = no difference,
1 = a difference, 2 = nothing was measured -- never read 2 as a pass)::

    python -m scripts.output_fingerprint take --out PATH [--jobs N] [--members GLOB]
    python -m scripts.output_fingerprint check [--report PATH] [--jobs N] [--members GLOB]
    python -m scripts.output_fingerprint compare OLD NEW [--report PATH] [--full]
    python -m scripts.output_fingerprint compare --base vX.Y.Z [NEW] [--report PATH]
    python -m scripts.output_fingerprint merge --out PATH PART [PART...]
    python -m scripts.output_fingerprint previous-tag [--line X.Y]

`--jobs N` runs members in N processes, one member per task; a member's
sessions never share a process with another member's at the same time,
and `tests/test_output_fingerprint_release_step.py` pins that one job and
four produce the same recording. Measured at introduction on a 14-core
machine: ~4.3 s per member at one job, ~10 minutes for the whole cohort
at `--jobs 4` (the sessions' own worker pools saturate the CPUs, so more
jobs buy little).

`--members GLOB` (fnmatch over member keys) narrows `take` and `check` to
part of the cohort -- while working on a change, or to run the whole
thing in parts: every key begins `pydicom:`, `pydicom-data:` or
`synthetic:`, so `check --members 'pydicom:*'`, `'pydicom-data:*'` and
`'synthetic:*'` together check every member (`check` compares only the
members its glob names, on both sides). A narrowed `take` is never the
tracked file; `merge` joins parts into one, and refuses unless they share
a commit, interpreter and toolchain, do not overlap, and are exactly the
whole cohort.

**The cohort.** pydicom's bundled test files, pydicom-data's downloaded
ones (`python -c "import pydicom.data; pydicom.data.fetch_data_files()"`
once per machine -- `take` refuses without them rather than skipping, since
a skip would read as a pass), and the committed synthetic members under
`fingerprint/cohort/` (`scripts/golden_cohort.py` generated them; the
committed bytes are the authority, not the generator). Each member is its
own sessions, so a difference is attributable to one input.

**The run, per member.** Configuration A (`fingerprint/config-a.yaml`):
secret, ingest, load_config, audit, anonymize, redact, then the arms
`A.dicom` (uncompressed, `check_burned_in=True`), `A.dicom-j2k`
(compressed: the explicit-VR arm), `A.wfdb`, and after closing and
reopening the store, `A.reopened.dicom`. Configuration B
(`fingerprint/config-b.yaml`, private tags kept): the same without redact,
arms `B.dicom` and `B.dicom-j2k`. A step that raises is recorded as its
outcome and the member continues; that is output, not a tool failure.

**What is deliberately not recorded**, the complete list; each
substitution replaces only the exact literal the running toolchain
produces, so a change in where or whether the value appears still shows:

- N1: the temporary paths this tool created (run root, member work
  directories, output folders, the source copy), in step outcomes, export
  results and audit rows. Both the given and the resolved spelling
  (`/var` vs `/private/var` on macOS).
- N2: `isocenter/<running __version__>`, the WFDB annotations' `source`.
  Never the bare version string, which is ordinary data in an LO or DS.
- N3: pydicom's own implementation UID and version name in `0002,0012` /
  `0002,0013`. If Isocenter ever writes its own, that is a difference.
- The per-element remediation trail rows (`REMEDIATION_REMOVE`,
  `_REPLACE`, `_SHIFT_DATE`): their effect is recorded element by
  element. An exclusion list, so a new row type is recorded by default.
- Audit `id` and `timestamp`; rows are a multiset (sorted), not a log.
- A member marked varying (`fingerprint/cohort/<member>/VARIES`, holding
  the reason) is run twice and every value that differed between the two
  runs becomes `<varies:N>`. Only one instance per marked member. The mark
  retires itself: a marked member that runs identically twice is refused,
  and the refusal says to delete the file.

Adding to this list is a reviewed change to the tool, with a test.

Why this lives in `scripts/` and `fingerprint/`: it is release tooling,
not API, and must not ship in the wheel; `MANIFEST.in` grafts `tests/`
into the sdist, which is why the recording and the cohort bytes are not
under it. Never name a directory `data/` here: `.gitignore` has an
unanchored `data/`.
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fnmatch
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
from collections import Counter
from importlib import metadata
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pydicom
import pydicom.data
from pydicom.uid import PYDICOM_IMPLEMENTATION_UID

REPO = Path(__file__).resolve().parents[1]
FINGERPRINT_DIR = REPO / "fingerprint"
GOLDEN = FINGERPRINT_DIR / "output.json"
COHORT = FINGERPRINT_DIR / "cohort"
CONFIGS = {"A": FINGERPRINT_DIR / "config-a.yaml",
           "B": FINGERPRINT_DIR / "config-b.yaml"}

SCHEMA = 1

#: A public test secret, not a secret: `bytes(range(32))`. The suite's
#: `tests/support/project_secret.py` FIXED_A is the same constant, and a
#: test pins the two equal. Not imported from there: scripts do not
#: depend on tests.
SECRET = bytes(range(32))

#: `docs/environment.md` variables that change how the pipeline runs.
#: The recording describes the default configuration, so `take` refuses
#: to run with any of them set.
REFUSED_ENV = (
    "ISOCENTER_MAX_WORKERS",
    "ISOCENTER_CHUNKSIZE",
    "ISOCENTER_MAX_TASKS_PER_CHILD",
    "ISOCENTER_DISABLE_GC",
    "ISOCENTER_FORCE_THREADS",
    "ISOCENTER_FORCE_PROCESSES",
)

#: The registry's other variables, and why each cannot change what is
#: exported. A test holds REFUSED_ENV + this equal to the registry, so a
#: new variable is sorted into one or the other before the suite passes.
NOT_OUTPUT_ENV = {
    "ISOCENTER_LOG_LEVEL": "logging verbosity; logs are not output",
    "ISOCENTER_LOG_FILE": "where the log goes; logs are not output",
    "ISOCENTER_DB_PATH": "read only when Session() is given no store; "
                         "this tool always gives one",
    "ISOCENTER_SHOW_PROGRESS": "progress bars; this tool sets it to 0",
    "ISOCENTER_WORKER_FAULTHANDLER": "a diagnostic traceback dump",
}

#: Audit rows left out of the accounting, and only these: each records
#: one element's remediation, which the recorded elements already show.
TRAIL_ACTIONS = frozenset({"REMEDIATION_REMOVE", "REMEDIATION_REPLACE",
                           "REMEDIATION_SHIFT_DATE"})

TEXT_VRS = frozenset("AE AS CS DA DS DT IS LO LT PN SH ST TM UC UI UR UT".split())
NUMBER_VRS = frozenset("US SS UL SL FL FD UV SV".split())
PIXEL_TAGS = frozenset({0x7FE00010, 0x7FE00008, 0x7FE00009})
WAVEFORM_DATA = 0x54001010
TEXT_LIMIT = 64

TOOLCHAIN_DISTS = ("pydicom", "numpy", "imagecodecs", "pillow", "pylibjpeg",
                   "pylibjpeg-libjpeg", "pylibjpeg-openjpeg", "python-gdcm")

#: pydicom's bundled files that are not DICOM inputs.
BUNDLED_SKIP = (".py", ".json", ".txt", ".gz", ".bz2", ".zip", ".dump", ".icc")

VARIES_FILE = "VARIES"

EXIT_SAME, EXIT_DIFFERENT, EXIT_TOOL = 0, 1, 2


class ToolError(Exception):
    """The tool could not produce or read a fingerprint: exit 2, never 1."""


def _h(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()[:16]


def running_version() -> str:
    """The running package's version, which N2 substitutes."""
    import isocenter  # noqa: PLC0415 -- the tool is importable without a run
    return isocenter.__version__


def pydicom_implementation_version() -> str:
    """What pydicom writes into 0002,0013 when a file meta has none."""
    return f"PYDICOM {'.'.join(str(p) for p in pydicom.__version_info__)}"


# -- recording one file --------------------------------------------------

def _decode_text(raw_value, ds, vr: str) -> str:
    if raw_value is None:
        return ""
    if isinstance(raw_value, (bytes, bytearray)):
        from pydicom.charset import decode_bytes
        from pydicom.valuerep import PN_DELIMS, TEXT_VR_DELIMS
        delims = PN_DELIMS | {0x3D} if vr == "PN" else TEXT_VR_DELIMS | {0x5C}
        try:
            text = decode_bytes(bytes(raw_value), ds._character_set, delims)
        except Exception:  # pylint: disable=broad-except
            text = bytes(raw_value).decode("latin-1")
    elif isinstance(raw_value, (list, tuple)) or type(raw_value).__name__ == "MultiValue":
        text = "\\".join(str(v) for v in raw_value)
    else:
        text = str(raw_value)
    # Trailing padding is what pydicom strips; anything else is the value.
    return text.rstrip(" \x00")


def _raw_bytes(raw_value) -> bytes:
    if raw_value is None:
        return b""
    if isinstance(raw_value, (bytes, bytearray)):
        return bytes(raw_value)
    return repr(raw_value).encode()


def _numbers(value) -> list:
    if value is None or value == b"":
        return []
    if isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
        items = list(value)
    else:
        items = [value]
    out = []
    for v in items:
        if isinstance(v, pydicom.tag.BaseTag):
            out.append(f"{int(v):08x}")
        elif isinstance(v, float):
            out.append(repr(v))
        else:
            out.append(int(v) if isinstance(v, (int, np.integer)) else repr(v))
    return out


def _record_elements(ds, implicit: bool, prefix: str, out: dict) -> None:
    for tag in sorted(ds.keys()):
        key = f"{prefix}{tag.group:04x},{tag.element:04x}"
        # get_item() BEFORE ds[tag]: indexing converts the raw element in
        # place, and the VR as written (None in an implicit file) and the
        # value as encoded are then gone. The order is load-bearing.
        raw = ds.get_item(tag)
        written_vr = getattr(raw, "VR", None)
        raw_value = getattr(raw, "value", None)
        try:
            elem = ds[tag]
        except Exception as exc:  # pylint: disable=broad-except
            label = f"{written_vr}/implicit" if implicit else str(written_vr)
            out[key] = f"{label} unreadable: {type(exc).__name__}: {exc}"
            continue
        vr = elem.VR if implicit or not written_vr else written_vr
        label = f"{vr}/implicit" if implicit else vr
        if vr == "SQ":
            items = elem.value or []
            out[key] = f"{label} items={len(items)}"
            for index, item in enumerate(items):
                _record_elements(item, implicit, f"{key}[{index}]>", out)
            continue
        if int(tag) in PIXEL_TAGS and not prefix:
            out[key] = f"{label} <pixels>"
            continue
        if int(tag) == WAVEFORM_DATA:
            out[key] = f"{label} <waveform>"
            continue
        if vr in TEXT_VRS:
            if isinstance(raw_value, (bytes, bytearray)):
                source = raw_value
            else:
                source = elem.value
            text = _decode_text(source, ds, vr)
            if len(text) <= TEXT_LIMIT:
                out[key] = f"{label} {text!r}"
            else:
                data = _raw_bytes(raw_value if raw_value is not None else text)
                out[key] = f"{label} {_h(data)} len={len(data)}"
            continue
        if vr in NUMBER_VRS or vr == "AT":
            value = elem.value
            if not isinstance(value, (bytes, bytearray)):
                out[key] = f"{label} {_numbers(value)}"
                continue
        data = _raw_bytes(raw_value if raw_value is not None else elem.value)
        out[key] = f"{label} {_h(data)} len={len(data)}"


def _pydicom_identity(meta: dict) -> dict:
    """N3: pydicom's own implementation identity, and only pydicom's."""
    if meta.get("0002,0012") == f"UI {str(PYDICOM_IMPLEMENTATION_UID)!r}":
        meta["0002,0012"] = "UI <pydicom-implementation-uid>"
    if meta.get("0002,0013") == f"SH {pydicom_implementation_version()!r}":
        meta["0002,0013"] = "SH <pydicom-implementation-version>"
    return meta


J2K_SYNTAXES = frozenset({"1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91",
                          "1.2.840.10008.1.2.4.201", "1.2.840.10008.1.2.4.202",
                          "1.2.840.10008.1.2.4.203"})


def _j2k_by_imagecodecs(ds) -> Optional[str]:
    """Samples of a JPEG 2000 file pydicom's own plugins refuse, or None.

    Without pylibjpeg, pydicom decodes JPEG 2000 through Pillow alone,
    which refuses 16-bit multi-sample data -- the #670 shape, which the
    compressed arms write for every 16-bit RGB input (12 files at
    introduction). Recorded as undecodable, those files' pixels would be
    compared by nothing. imagecodecs (a required dependency) decodes them
    directly, frame by frame; the pixels string says it did, so a later
    toolchain that lets pydicom decode them shows as a difference beside
    the toolchain line rather than silently.
    """
    if str(ds.file_meta.get("TransferSyntaxUID", "")) not in J2K_SYNTAXES:
        return None
    try:
        import imagecodecs  # noqa: PLC0415
        from pydicom.encaps import generate_frames
        frames = int(ds.get("NumberOfFrames", 1) or 1)
        decoded = [np.asarray(imagecodecs.jpeg2k_decode(f))
                   for f in generate_frames(ds.PixelData, number_of_frames=frames)]
        arr = np.ascontiguousarray(decoded[0] if len(decoded) == 1 else np.stack(decoded))
    except Exception:  # pylint: disable=broad-except
        return None
    return f"{arr.dtype.str} {list(arr.shape)} {_h(arr.tobytes())} (imagecodecs)"


def record_dicom(path) -> dict:
    """One DICOM file: meta, elements, decoded pixels and waveforms.

    Decoding a compressed arm's pixels relies on the package's codec
    registration, which `import isocenter` performs; `take` checks the
    JPEG 2000 decoder is present before it runs, so a lost registration
    refuses the run instead of turning every compressed file undecodable.
    """
    import isocenter  # noqa: F401,PLC0415 -- registers the decoders
    ds = pydicom.dcmread(str(path), force=True)
    implicit = bool(ds.original_encoding[0])
    rec = {"meta": {}, "elements": {}}
    _record_elements(ds.file_meta, False, "", rec["meta"])
    _pydicom_identity(rec["meta"])
    _record_elements(ds, implicit, "", rec["elements"])
    if any(int(t) in PIXEL_TAGS for t in ds.keys()):
        try:
            arr = np.ascontiguousarray(ds.pixel_array)
            rec["pixels"] = f"{arr.dtype.str} {list(arr.shape)} {_h(arr.tobytes())}"
        except Exception as exc:  # pylint: disable=broad-except
            rec["pixels"] = _j2k_by_imagecodecs(ds) or \
                f"undecodable: {type(exc).__name__}: {exc}"
    if "WaveformSequence" in ds:
        waves = []
        for index in range(len(ds.WaveformSequence)):
            try:
                arr = np.ascontiguousarray(ds.waveform_array(index))
                waves.append(f"{arr.dtype.str} {list(arr.shape)} {_h(arr.tobytes())}")
            except Exception as exc:  # pylint: disable=broad-except
                waves.append(f"undecodable: {type(exc).__name__}: {exc}")
        rec["waveforms"] = waves
    return rec


def record_file(path) -> dict:
    """One written file, by what it is."""
    path = Path(path)
    name = path.name
    if name.endswith(".dcm"):
        try:
            return record_dicom(path)
        except Exception as exc:  # pylint: disable=broad-except
            return {"unreadable": f"{type(exc).__name__}: {exc}"}
    data = path.read_bytes()
    if name.endswith(".hea"):
        return {"lines": data.decode("utf-8", "backslashreplace").splitlines()}
    if name.endswith(".dat"):
        # WFDB format 16: the bytes are the samples.
        return {"samples": f"{_h(data)} len={len(data)}"}
    if name.endswith(".json"):
        try:
            return {"json": json.loads(data)}
        except ValueError as exc:
            return {"unreadable": f"{type(exc).__name__}: {exc}"}
    return {"unrecognised file type": f"{_h(data)} len={len(data)}"}


def record_tree(folder) -> dict:
    """Every file under `folder`, by relative posix path."""
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    return {p.relative_to(folder).as_posix(): record_file(p) for p in files}


# -- normalization -------------------------------------------------------

def normalize(obj, substitutions: List[Tuple[str, str]]):
    """Replace each literal in every string of `obj`, in the order given."""
    if not substitutions:
        return obj
    if isinstance(obj, str):
        for literal, token in substitutions:
            if literal and literal in obj:
                obj = obj.replace(literal, token)
        return obj
    if isinstance(obj, dict):
        return {normalize(k, substitutions): normalize(v, substitutions)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v, substitutions) for v in obj]
    return obj


def path_substitutions(paths: Dict[str, str]) -> List[Tuple[str, str]]:
    """N1: each path the tool created, given and resolved, longest first."""
    pairs = set()
    for token, path in paths.items():
        for spelling in {str(path), os.path.abspath(str(path)),
                         os.path.realpath(str(path))}:
            pairs.add((spelling.rstrip(os.sep), token))
    return sorted(pairs, key=lambda p: (-len(p[0]), p[0]))


def output_substitutions() -> List[Tuple[str, str]]:
    """N2: the running version, in the one form it is stamped."""
    return [(f"isocenter/{running_version()}", "isocenter/<isocenter-version>")]


# -- accounting ------------------------------------------------------------

def read_accounting(db, substitutions) -> dict:
    """Every audit row but the remediation trail, as a sorted multiset."""
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(audit_log)")]
        kept = [c for c in columns if c not in ("id", "timestamp")]
        if not kept:
            return {"columns": [], "rows": []}
        action = kept.index("action_type") if "action_type" in kept else None
        rows = [list(r) for r in conn.execute(
            f"SELECT {', '.join(kept)} FROM audit_log")]
    if action is not None:
        rows = [r for r in rows if r[action] not in TRAIL_ACTIONS]
    rows = normalize(rows, substitutions)
    # Cells mix str, int and None, so the order is by serialization.
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
    return {"columns": kept, "rows": rows}


# -- the cohort ----------------------------------------------------------

@dataclasses.dataclass
class Member:
    key: str
    base: str                 # directory the files are relative to
    files: List[str]          # relative posix paths of the input files
    varies: Optional[str] = None

    @property
    def inputs(self) -> dict:
        return {rel: "sha256:" + hashlib.sha256(
            (Path(self.base) / rel).read_bytes()).hexdigest()
            for rel in self.files}


def _bundled_members() -> List[Member]:
    root = Path(pydicom.data.data_manager.DATA_ROOT)
    out = []
    for path in sorted((root / "test_files").rglob("*")):
        if path.is_file() and not path.name.endswith(BUNDLED_SKIP) \
                and "__pycache__" not in path.parts:
            out.append(Member(f"pydicom:{path.relative_to(root).as_posix()}",
                              str(path.parent), [path.name]))
    return out


def _downloaded_members() -> List[Member]:
    try:
        pydicom.data.fetch_data_files()
    except Exception as exc:  # pylint: disable=broad-except
        raise ToolError(
            "pydicom-data's files are not all available, and a skipped member "
            "would read as a pass. Fetch them once per machine: python -c "
            "\"import pydicom.data; pydicom.data.fetch_data_files()\" "
            f"({type(exc).__name__}: {exc})") from exc
    from pydicom.data.download import get_data_dir, get_url_map
    cache = Path(get_data_dir())
    out = []
    for name in sorted(get_url_map()):
        path = cache / name
        if not path.is_file():
            raise ToolError(f"pydicom-data file missing after fetch: {path}")
        out.append(Member(f"pydicom-data:{name}", str(cache), [name]))
    return out


def synthetic_members(cohort_root) -> List[Member]:
    """Each directory under `cohort_root` is one member."""
    cohort_root = Path(cohort_root)
    out = []
    if not cohort_root.is_dir():
        return out
    for member in sorted(p for p in cohort_root.iterdir() if p.is_dir()):
        files = sorted(p.relative_to(member).as_posix()
                       for p in member.rglob("*")
                       if p.is_file() and p.name != VARIES_FILE)
        mark = member / VARIES_FILE
        varies = mark.read_text(encoding="utf-8").strip() if mark.exists() else None
        if varies is not None and len(files) != 1:
            raise ToolError(
                f"synthetic:{member.name} is marked varying ({mark}) but holds "
                f"{len(files)} files; a varying member must hold exactly one "
                "instance, because its runs are paired file by file and files "
                "whose names are random cannot be paired by name")
        out.append(Member(f"synthetic:{member.name}", str(member), files, varies))
    return out


def assemble_cohort(cohort_root=COHORT, pydicom_sets=True,
                    members: Optional[str] = None) -> List[Member]:
    cohort = synthetic_members(cohort_root)
    if pydicom_sets:
        cohort = _bundled_members() + _downloaded_members() + cohort
    if members:
        cohort = [m for m in cohort if fnmatch.fnmatchcase(m.key, members)]
        if not cohort:
            raise ToolError(f"--members {members!r} matches no member")
    keys = [m.key for m in cohort]
    if len(keys) != len(set(keys)):
        raise ToolError("two members share a key: "
                        + ", ".join(k for k, n in Counter(keys).items() if n > 1))
    return sorted(cohort, key=lambda m: m.key)


# -- running one member ----------------------------------------------------

def _raised(exc: BaseException) -> str:
    return f"raised {type(exc).__name__}: {exc}"


def _step(fn) -> object:
    try:
        return fn()
    except Exception as exc:  # pylint: disable=broad-except
        return _raised(exc)


def _ingest_outcome(summary) -> dict:
    return {"ingested": summary.ingested,
            "failures": sorted([os.path.basename(str(p)), str(r)]
                               for p, r in summary.failures),
            "declined": summary.declined, "skipped": summary.skipped}


def _export_outcome(result):
    if hasattr(result, "written") and hasattr(result, "failures"):
        return {"written": result.written,
                "failures": sorted([str(u), str(d)] for u, d in result.failures)}
    if isinstance(result, (list, tuple)):
        return {"returned": sorted(str(r) for r in result)}
    return {"returned": type(result).__name__}


def _secret_file(folder: Path, secret: bytes) -> str:
    path = folder / "secret.txt"
    path.write_text(f"isocenter-project-secret-v1:{secret.hex()}\n", encoding="ascii")
    return str(path)


def _arm(session, out: Path, fmt: str, options: dict) -> dict:
    try:
        result = _export_outcome(session.export(str(out), format=fmt, **options))
    except Exception as exc:  # pylint: disable=broad-except
        result = _raised(exc)
    return {"result": result, "files": record_tree(out)}


A_ARMS = (("A.dicom", "dicom", {"use_compression": False, "check_burned_in": True}),
          ("A.dicom-j2k", "dicom", {"use_compression": True}),
          ("A.wfdb", "wfdb", {}))
A_REOPENED = ("A.reopened.dicom", "dicom", {"use_compression": False})
B_ARMS = (("B.dicom", "dicom", {"use_compression": False}),
          ("B.dicom-j2k", "dicom", {"use_compression": True}))


def _run_config(name: str, src: Path, work: Path, secret: bytes,
                config: Path, arms, redact: bool, reopened) -> Tuple[dict, dict]:
    from isocenter import Session  # noqa: PLC0415
    work.mkdir(parents=True)
    db = work / "store.db"
    secret_path = _secret_file(work, secret)
    steps, results, paths = {}, {}, {}
    with Session(persistence_file=str(db)) as session:
        # Before the first audit(): a store mints its own random secret
        # there, which is self-consistent within one run and so invisible
        # to anything but a second run.
        session.store_backend.load_project_secret(secret_path)
        steps["ingest"] = _step(lambda: _ingest_outcome(session.ingest(str(src))))
        steps["load_config"] = _step(lambda: session.load_config(str(config)) and "ok"
                                     or "ok")
        steps["audit"] = _step(lambda: session.audit() and "ok" or "ok")
        steps["anonymize"] = _step(lambda: session.anonymize() and "ok" or "ok")
        if redact:
            steps["redact"] = _step(
                lambda: f"redacted {session.redact(show_progress=False)}")
        for arm, fmt, options in arms:
            out = work / arm
            paths[f"<out:{arm}>"] = str(out)
            results[arm] = _arm(session, out, fmt, options)
    if reopened:
        arm, fmt, options = reopened
        with Session(persistence_file=str(db)) as session:
            out = work / arm
            paths[f"<out:{arm}>"] = str(out)
            results[arm] = _arm(session, out, fmt, options)
    paths[f"<work:{name}>"] = str(work)
    return {"steps": steps, "arms": results}, {"db": str(db), "paths": paths}


def run_member(member: Member, work, secret: bytes) -> dict:
    """The pipeline for one member: configuration A, then B."""
    work = Path(work)
    src = work / "src"
    for rel in member.files:
        dest = src / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(member.base) / rel, dest)
    configs, paths = {}, {"<src>": str(src), "<member>": str(work)}
    runs = (("A", CONFIGS["A"], A_ARMS, True, A_REOPENED),
            ("B", CONFIGS["B"], B_ARMS, False, None))
    for name, config, arms, redact, reopened in runs:
        record, extra = _run_config(name, src, work / name, secret, config,
                                    arms, redact, reopened)
        paths.update(extra["paths"])
        subs = path_substitutions(dict(paths, **{"<root>": str(work.parent)}))
        record["steps"] = normalize(record["steps"], subs)
        for arm in record["arms"].values():
            arm["result"] = normalize(arm["result"], subs)
        record["accounting"] = read_accounting(extra["db"], subs)
        configs[name] = record
    return {"configs": configs}


# -- the varying member ----------------------------------------------------

_NUMBER_RUN = re.compile(r"\d+(?:\.\d+)*")


class _Varies:
    def __init__(self):
        self.tokens: Dict[Tuple[str, str], str] = {}

    def token(self, a: str, b: str) -> str:
        if (a, b) not in self.tokens:
            self.tokens[(a, b)] = f"<varies:{len(self.tokens) + 1}>"
        return self.tokens[(a, b)]

    def strings(self, a: str, b: str, where: str) -> Tuple[str, str]:
        if a == b:
            return a, b
        # A value differing inside a longer string (a UID in a path) is
        # replaced where it differs, so its other occurrences share one N.
        pa, pb = _NUMBER_RUN.split(a), _NUMBER_RUN.split(b)
        na, nb = _NUMBER_RUN.findall(a), _NUMBER_RUN.findall(b)
        if pa != pb or len(na) != len(nb):
            token = self.token(a, b)
            return token, token
        out = [pa[0]]
        for index, (x, y) in enumerate(zip(na, nb)):
            out.append(x if x == y else self.token(x, y))
            out.append(pa[index + 1])
        joined = "".join(out)
        return joined, joined

    def walk(self, a, b, where: str):
        if isinstance(a, str) and isinstance(b, str):
            return self.strings(a, b, where)
        if isinstance(a, dict) and isinstance(b, dict):
            ka, kb = sorted(a), sorted(b)
            if len(ka) != len(kb):
                raise ToolError(f"varying member: the two runs differ in shape at {where}")
            oa, ob = {}, {}
            # Positional pairing: keys in sorted order. Safe only because a
            # varying member holds one instance per arm (checked above).
            for x, y in zip(ka, kb):
                nx, ny = self.walk(x, y, f"{where}/{x}")
                vx, vy = self.walk(a[x], b[y], f"{where}/{x}")
                oa[nx], ob[ny] = vx, vy
            return oa, ob
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                raise ToolError(f"varying member: the two runs differ in length at {where}")
            pairs = [self.walk(x, y, f"{where}[{i}]") for i, (x, y) in enumerate(zip(a, b))]
            return [p[0] for p in pairs], [p[1] for p in pairs]
        if a != b:
            raise ToolError(f"varying member: {where} differs and is not text: {a!r} / {b!r}")
        return a, b


def fold_varies(key: str, first: dict, second: dict, mark: str) -> dict:
    """Two runs of a marked member -> one record, `<varies:N>` where they differed."""
    varies = _Varies()
    a, b = varies.walk(first, second, key)
    if not varies.tokens:
        raise ToolError(
            f"{key} is marked varying but ran identically twice: remove {mark} "
            "(the member no longer varies, and the mark would hide a change)")
    if a != b:
        raise ToolError(f"{key}: the two runs still differ after folding")
    return a


# -- take ------------------------------------------------------------------

def refuse_environment() -> None:
    present = [name for name in REFUSED_ENV if os.environ.get(name) is not None]
    if present:
        raise ToolError(
            "the fingerprint describes the default configuration; unset "
            + ", ".join(present) + " (docs/environment.md)")


def _require_decoders() -> None:
    import isocenter  # noqa: F401,PLC0415
    from pydicom.pixels import get_decoder
    from pydicom.uid import JPEG2000Lossless
    if not get_decoder(JPEG2000Lossless).is_available:
        raise ToolError("no JPEG 2000 decoder is available: every compressed "
                        "arm would record as undecodable")


def _git(*args) -> Optional[str]:
    try:
        return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def provenance(jobs: int) -> dict:
    status = _git("status", "--porcelain")
    return {"git_sha": _git("rev-parse", "HEAD"),
            "dirty": None if status is None else bool(status),
            "isocenter_version": running_version(),
            "python": platform.python_version(),
            "gil": bool(getattr(sys, "_is_gil_enabled", lambda: True)()),
            "platform": platform.platform(),
            "jobs": jobs}


def toolchain() -> dict:
    out = {}
    for dist in TOOLCHAIN_DISTS:
        try:
            out[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            out[dist] = None
    return out


Runner = Callable[[Member, Path, bytes], dict]


def _quietly(fn, *args):
    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore")
        return fn(*args)


def _member_record(member: Member, root: str, secret: bytes,
                   runner: Optional[Runner] = None) -> Tuple[str, dict, float]:
    """One member, run once (twice if marked varying); runs in a worker too."""
    runner = runner or run_member
    started = time.time()
    os.chdir(root)
    slug = hashlib.sha256(member.key.encode()).hexdigest()[:12]
    runs = []
    for attempt in range(2 if member.varies is not None else 1):
        work = Path(root) / f"{slug}-{attempt}"
        record = _quietly(runner, member, work, secret)
        runs.append(normalize(record, output_substitutions()))
    if member.varies is not None:
        mark = f"fingerprint/cohort/{member.key.split(':', 1)[1]}/{VARIES_FILE}"
        record = fold_varies(member.key, runs[0], runs[1], mark)
        record["varies"] = True
    else:
        record = runs[0]
    record["inputs"] = member.inputs
    return member.key, record, time.time() - started


def _worker_init(root: str) -> None:
    os.environ["ISOCENTER_SHOW_PROGRESS"] = "0"
    os.chdir(root)


def take(out, *, members: Optional[str] = None, jobs: int = 1,
         cohort_root=COHORT, pydicom_sets: bool = True, secret: bytes = None,
         runner: Optional[Runner] = None, log=None) -> dict:
    """Run the cohort and write its fingerprint to `out`. Raises ToolError."""
    secret = SECRET if secret is None else secret
    log = log or (lambda line: print(line, file=sys.stderr, flush=True))
    refuse_environment()
    if runner is None:
        _require_decoders()
    cohort = assemble_cohort(cohort_root, pydicom_sets, members)
    if jobs < 1:
        raise ToolError("--jobs must be at least 1")
    started = time.time()
    previous_cwd = os.getcwd()
    previous_progress = os.environ.get("ISOCENTER_SHOW_PROGRESS")
    root = os.path.realpath(tempfile.mkdtemp(prefix="isocenter-fingerprint-"))
    records = {}
    try:
        # Every session runs with the cwd in the temporary root, so no
        # isocenter.log, store or lock file lands in the caller's cwd.
        _worker_init(root)
        if jobs == 1 or runner is not None:
            for index, member in enumerate(cohort, 1):
                key, record, took = _member_record(member, root, secret, runner)
                records[key] = record
                log(f"[{index}/{len(cohort)}] {key} {took:.1f}s")
        else:
            import multiprocessing  # noqa: PLC0415
            from concurrent.futures import ProcessPoolExecutor, as_completed
            context = multiprocessing.get_context("spawn")
            with ProcessPoolExecutor(max_workers=jobs, mp_context=context,
                                     initializer=_worker_init,
                                     initargs=(root,)) as pool:
                futures = [pool.submit(_member_record, m, root, secret) for m in cohort]
                for index, future in enumerate(as_completed(futures), 1):
                    key, record, took = future.result()
                    records[key] = record
                    log(f"[{index}/{len(cohort)}] {key} {took:.1f}s")
    finally:
        os.chdir(previous_cwd)
        if previous_progress is None:
            os.environ.pop("ISOCENTER_SHOW_PROGRESS", None)
        else:
            os.environ["ISOCENTER_SHOW_PROGRESS"] = previous_progress
        shutil.rmtree(root, ignore_errors=True)
    fingerprint = {"schema": SCHEMA, "provenance": provenance(jobs),
                   "toolchain": toolchain(),
                   "members": dict(sorted(records.items()))}
    fingerprint["provenance"]["seconds"] = round(time.time() - started)
    if members:
        fingerprint["provenance"]["members"] = members
    write_fingerprint(out, fingerprint)
    log(f"took {len(records)} members in {time.time() - started:.0f}s -> {out}")
    return fingerprint


#: Provenance two parts of one recording must share to be merged.
MERGE_KEYS = ("git_sha", "isocenter_version", "python", "gil", "platform")


def merge(out, parts: List, *, cohort_root=COHORT, pydicom_sets: bool = True) -> dict:
    """Join narrowed takes into one whole-cohort recording, or refuse.

    For a machine where a whole `take` does not fit one sitting: take the
    cohort in `--members` parts, then merge them. The parts must come from
    one commit, interpreter and toolchain, must not overlap, and together
    must be exactly the cohort `take` would run -- so a merged file is the
    whole-cohort recording, never a partial one that reads as whole.
    """
    loaded = [load_fingerprint(Path(p)) for p in parts]
    if not loaded:
        raise ToolError("merge needs at least one part")
    first = loaded[0]
    members: Dict[str, dict] = {}
    for path, part in zip(parts, loaded):
        for key in MERGE_KEYS:
            if part["provenance"].get(key) != first["provenance"].get(key):
                raise ToolError(f"{path}: {key} is {part['provenance'].get(key)!r}, "
                                f"not {first['provenance'].get(key)!r}; one recording "
                                "comes from one commit and one interpreter")
        if part["toolchain"] != first["toolchain"]:
            raise ToolError(f"{path}: its toolchain differs from {parts[0]}'s")
        overlap = set(members) & set(part["members"])
        if overlap:
            raise ToolError(f"{path}: members already in an earlier part: "
                            + ", ".join(sorted(overlap)))
        members.update(part["members"])
    expected = {m.key for m in assemble_cohort(cohort_root, pydicom_sets)}
    missing, extra = sorted(expected - set(members)), sorted(set(members) - expected)
    if missing or extra:
        raise ToolError("the parts are not the whole cohort: "
                        + (f"missing {', '.join(missing)}" if missing else "")
                        + ("; " if missing and extra else "")
                        + (f"not in the cohort {', '.join(extra)}" if extra else ""))
    provenance_ = {k: v for k, v in first["provenance"].items() if k != "members"}
    provenance_["dirty"] = any(bool(p["provenance"].get("dirty")) for p in loaded)
    provenance_["seconds"] = sum(p["provenance"].get("seconds", 0) for p in loaded)
    provenance_["parts"] = [p["provenance"].get("members") or "*" for p in loaded]
    fingerprint = {"schema": SCHEMA, "provenance": provenance_,
                   "toolchain": first["toolchain"],
                   "members": dict(sorted(members.items()))}
    write_fingerprint(out, fingerprint)
    return fingerprint


def write_fingerprint(path, fingerprint: dict) -> None:
    text = json.dumps(fingerprint, sort_keys=True, indent=1, ensure_ascii=False)
    Path(path).write_text(text + "\n", encoding="utf-8")


def load_fingerprint(path_or_text, name: str = "") -> dict:
    try:
        if isinstance(path_or_text, Path) or (isinstance(path_or_text, str)
                                              and not path_or_text.lstrip().startswith("{")):
            text = Path(path_or_text).read_text(encoding="utf-8")
            name = name or str(path_or_text)
        else:
            text = path_or_text
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        raise ToolError(f"cannot read fingerprint {name}: {type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ToolError(f"{name} is not a schema-{SCHEMA} fingerprint "
                        f"(schema {data.get('schema') if isinstance(data, dict) else None!r})")
    return data


# -- compare ---------------------------------------------------------------

SECTIONS = ("Cohort", "Outcomes", "Accounting rows", "Paths", "Elements")


@dataclasses.dataclass
class Group:
    section: str
    key: str
    kind: str
    vr: str = ""
    count: int = 0
    arms: set = dataclasses.field(default_factory=set)
    examples: list = dataclasses.field(default_factory=list)

    def add(self, arm: str, example: str) -> None:
        self.count += 1
        if arm:
            self.arms.add(arm)
        self.examples.append(example)


class Report:
    """What `compare` found: groups by section, and the exit status."""

    def __init__(self, old: dict, new: dict):
        self.old, self.new = old, new
        self._groups: Dict[tuple, Group] = {}
        self.toolchain: List[str] = []
        self.scope = ""

    def group(self, section, key, kind, vr="") -> Group:
        ident = (section, key, kind, vr)
        if ident not in self._groups:
            self._groups[ident] = Group(section, key, kind, vr)
        return self._groups[ident]

    @property
    def groups(self) -> List[Group]:
        order = {s: i for i, s in enumerate(SECTIONS)}
        return sorted(self._groups.values(),
                      key=lambda g: (order[g.section], g.key, g.vr, g.kind))

    @property
    def differences(self) -> int:
        return sum(g.count for g in self._groups.values())

    @property
    def exit_code(self) -> int:
        return EXIT_DIFFERENT if self._groups else EXIT_SAME

    def text(self, full: bool = False) -> str:
        lines = []
        for label, fp in (("OLD", self.old), ("NEW", self.new)):
            p = fp.get("provenance", {})
            lines.append(f"{label}: git_sha={p.get('git_sha')} dirty={p.get('dirty')} "
                         f"isocenter={p.get('isocenter_version')} python={p.get('python')} "
                         f"gil={p.get('gil')} members={len(fp.get('members', {}))}")
        if self.scope:
            lines.append(self.scope)
        if self.toolchain and self._groups:
            lines.append("NOTE: the toolchain differs too (section 1); some "
                         "differences below may be the toolchain's.")
        lines.append("")
        lines.append(f"1. Toolchain ({len(self.toolchain)}; informational, not a difference)")
        lines.extend(f"   {t}" for t in self.toolchain)
        if not self.toolchain:
            lines.append("   none")
        groups = self.groups
        for number, section in enumerate(SECTIONS, 2):
            mine = [g for g in groups if g.section == section]
            lines.append(f"{number}. {section} ({len(mine)} groups, "
                         f"{sum(g.count for g in mine)} differences)")
            if not mine:
                lines.append("   none")
            for g in mine:
                what = " ".join(x for x in (g.key, g.vr, g.kind) if x)
                unit = "files" if section == "Elements" else "times"
                arms = f" ({', '.join(sorted(g.arms))})" if g.arms else ""
                lines.append(f"   {what} in {g.count} {unit}{arms}")
                shown = g.examples if full else g.examples[:3]
                lines.extend(f"     e.g. {e}" for e in shown)
                if len(g.examples) > len(shown):
                    lines.append(f"     ... {len(g.examples) - len(shown)} more (--full)")
        lines.append("")
        if not self._groups:
            lines.append("No difference.")
        else:
            n, k = self.differences, len(self._groups)
            lines.append(f"{n} difference{'s' if n != 1 else ''} in {k} "
                         f"group{'s' if k != 1 else ''} -- each is a defect or is named "
                         "by an **Output:** changelog line; at release an unnamed "
                         "one stops the release (RELEASING.md)")
        return "\n".join(lines) + "\n"


def _short(value, limit=160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    text = text.replace("\n", "\\n")  # one example, one report line
    return text if len(text) <= limit else text[:limit] + "..."


def _vr(entry) -> str:
    return entry.split(" ", 1)[0] if isinstance(entry, str) else ""


def _compare_file(report, where, arm, old: dict, new: dict) -> None:
    for field in sorted(set(old) | set(new)):
        a, b = old.get(field), new.get(field)
        if a == b:
            continue
        if field in ("meta", "elements"):
            a, b = a or {}, b or {}
            for key in sorted(set(a) | set(b)):
                x, y = a.get(key), b.get(key)
                if x == y:
                    continue
                if x is None:
                    g = report.group("Elements", key, "added", _vr(y))
                elif y is None:
                    g = report.group("Elements", key, "removed", _vr(x))
                elif _vr(x) != _vr(y):
                    g = report.group("Elements", key, "VR changed",
                                     f"{_vr(x)}->{_vr(y)}")
                else:
                    g = report.group("Elements", key, "changed", _vr(x))
                g.add(arm, f"{where}: {_short(x)} -> {_short(y)}")
        elif field in ("pixels", "waveforms"):
            now_bad = isinstance(b, str) and b.startswith("undecodable") and not (
                isinstance(a, str) and a.startswith("undecodable"))
            kind = "undecodable" if now_bad else f"{field} changed"
            report.group("Elements", f"<{field}>", kind).add(
                arm, f"{where}: {_short(a)} -> {_short(b)}")
        else:
            report.group("Elements", f"<{field}>", "changed").add(
                arm, f"{where}: {_short(a)} -> {_short(b)}")


def _compare_arm(report, mkey, arm, old: dict, new: dict) -> None:
    if old.get("result") != new.get("result"):
        report.group("Outcomes", f"{arm} result", "changed").add(
            arm, f"{mkey}: {_short(old.get('result'))} -> {_short(new.get('result'))}")
    fa, fb = old.get("files", {}), new.get("files", {})
    same = sorted(set(fa) & set(fb))
    pairs = [(p, p) for p in same]
    only_a = sorted(set(fa) - set(fb))
    only_b = sorted(set(fb) - set(fa))
    if only_a and len(only_a) == len(only_b):
        # Every path moved (a UID or date in the folder name changed):
        # pair in sorted order and still compare element by element.
        for x, y in zip(only_a, only_b):
            report.group("Paths", arm, "moved").add(arm, f"{mkey}: {x} -> {y}")
            pairs.append((x, y))
    else:
        for x in only_a:
            report.group("Paths", arm, "only in OLD").add(arm, f"{mkey}: {x}")
        for y in only_b:
            report.group("Paths", arm, "only in NEW").add(arm, f"{mkey}: {y}")
    for x, y in pairs:
        _compare_file(report, f"{mkey} {arm} {y}", arm, fa[x], fb[y])


def _compare_member(report, mkey, old: dict, new: dict) -> None:
    if bool(old.get("varies")) != bool(new.get("varies")):
        report.group("Cohort", "marked varying", "changed").add(
            "", f"{mkey}: {bool(old.get('varies'))} -> {bool(new.get('varies'))}")
    ca, cb = old.get("configs", {}), new.get("configs", {})
    for config in sorted(set(ca) | set(cb)):
        a, b = ca.get(config, {}), cb.get(config, {})
        sa, sb = a.get("steps", {}), b.get("steps", {})
        for step in sorted(set(sa) | set(sb)):
            if sa.get(step) != sb.get(step):
                report.group("Outcomes", f"{config} {step}", "changed").add(
                    "", f"{mkey}: {_short(sa.get(step))} -> {_short(sb.get(step))}")
        acc_a, acc_b = a.get("accounting", {}), b.get("accounting", {})
        if acc_a.get("columns") != acc_b.get("columns"):
            report.group("Accounting rows", "columns", "changed").add(
                "", f"{mkey} {config}: {acc_a.get('columns')} -> {acc_b.get('columns')}")
        rows_a = Counter(json.dumps(r) for r in acc_a.get("rows", []))
        rows_b = Counter(json.dumps(r) for r in acc_b.get("rows", []))
        for label, extra in (("row only in OLD", rows_a - rows_b),
                             ("row only in NEW", rows_b - rows_a)):
            for row, n in sorted(extra.items()):
                action = json.loads(row)[0] if json.loads(row) else ""
                for _ in range(n):
                    report.group("Accounting rows", str(action), label).add(
                        "", f"{mkey} {config}: {_short(row, 240)}")
        arms_a, arms_b = a.get("arms", {}), b.get("arms", {})
        for arm in sorted(set(arms_a) | set(arms_b)):
            if arm not in arms_a or arm not in arms_b:
                report.group("Outcomes", arm, "arm only in " + (
                    "NEW" if arm not in arms_a else "OLD")).add(arm, mkey)
                continue
            _compare_arm(report, mkey, arm, arms_a[arm], arms_b[arm])


def compare(old: dict, new: dict, members: Optional[str] = None) -> Report:
    report = Report(old, new)
    ta, tb = old.get("toolchain", {}), new.get("toolchain", {})
    report.toolchain = [f"{k}: {ta.get(k)} -> {tb.get(k)}"
                        for k in sorted(set(ta) | set(tb)) if ta.get(k) != tb.get(k)]
    ma, mb = old.get("members", {}), new.get("members", {})
    if members:
        ma = {k: v for k, v in ma.items() if fnmatch.fnmatchcase(k, members)}
        mb = {k: v for k, v in mb.items() if fnmatch.fnmatchcase(k, members)}
        report.scope = (f"compared only members matching {members!r}: "
                        f"{len(ma)} in OLD, {len(mb)} in NEW")
    for key in sorted(set(ma) - set(mb)):
        report.group("Cohort", "member", "only in OLD").add("", key)
    for key in sorted(set(mb) - set(ma)):
        report.group("Cohort", "member", "only in NEW").add("", key)
    for key in sorted(set(ma) & set(mb)):
        if ma[key].get("inputs") != mb[key].get("inputs"):
            # The measuring stick changed, not the output: this member's
            # outputs are not compared, so a new input never reads as a
            # changed output.
            report.group("Cohort", "member", "input changed").add("", key)
            continue
        _compare_member(report, key, ma[key], mb[key])
    return report


# -- previous release ------------------------------------------------------

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?$")
_STAGE = {"a": 0, "b": 1, "rc": 2, None: 3}


def _tag_order(tag: str):
    m = _TAG.match(tag)
    if not m:
        return None
    major, minor, patch, stage, n = m.groups()
    return (int(major), int(minor), int(patch), _STAGE[stage], int(n or 0))


def newest_release_tag(repo=REPO, line: Optional[str] = None) -> Optional[str]:
    """The highest `v*` tag by version order, pre-releases included.

    By version order across the repository, not by reachability: release
    tags sit on `release/X.Y`, and `main` reaches none of them (at
    0.9.8's cut `git tag --merged main` stopped at v0.9.7). `line` ("1.0")
    restricts it to one release line, for a patch release.
    """
    out = subprocess.run(["git", "tag", "--list", "v*"], cwd=str(repo),
                         capture_output=True, text=True, check=True).stdout
    tags = [t for t in out.split() if _tag_order(t)]
    if line:
        tags = [t for t in tags if ".".join(t[1:].split(".")[:2]) == line]
    return max(tags, key=_tag_order) if tags else None


def fingerprint_at(tag: str, repo=REPO) -> dict:
    rel = GOLDEN.relative_to(REPO).as_posix()
    try:
        text = subprocess.run(["git", "show", f"{tag}:{rel}"], cwd=str(repo),
                              capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise ToolError(f"{tag} carries no {rel}: the comparison with it does not "
                        f"apply ({exc.stderr.strip()})") from exc
    return load_fingerprint(text, f"{tag}:{rel}")


# -- CLI -------------------------------------------------------------------

def _emit(report: Report, path: Optional[str], full: bool) -> int:
    text = report.text(full=full)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return report.exit_code


def check(*, golden=GOLDEN, members=None, jobs=1, cohort_root=COHORT,
          pydicom_sets=True, report_path=None, full=False, log=None) -> int:
    old = load_fingerprint(Path(golden))
    with tempfile.TemporaryDirectory(prefix="isocenter-fingerprint-check-") as tmp:
        taken = Path(tmp) / "taken.json"
        take(taken, members=members, jobs=jobs, cohort_root=cohort_root,
             pydicom_sets=pydicom_sets, log=log)
        new = load_fingerprint(taken)
    return _emit(compare(old, new, members), report_path, full)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.output_fingerprint",
                                     description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_take = sub.add_parser("take", help="run the cohort, write a fingerprint")
    p_take.add_argument("--out", required=True)
    p_check = sub.add_parser("check", help="take, then compare with the tracked file")
    p_check.add_argument("--report")
    p_check.add_argument("--full", action="store_true")
    p_check.add_argument("--golden", default=str(GOLDEN))
    for p in (p_take, p_check):
        p.add_argument("--jobs", type=int, default=1)
        p.add_argument("--members", help="fnmatch glob over member keys")
    p_cmp = sub.add_parser("compare", help="compare two fingerprints")
    p_cmp.add_argument("files", nargs="*")
    p_cmp.add_argument("--base", help="a v* tag: OLD is its fingerprint/output.json")
    p_cmp.add_argument("--report")
    p_cmp.add_argument("--full", action="store_true")
    p_cmp.add_argument("--members")
    p_merge = sub.add_parser("merge", help="join --members takes into one whole recording")
    p_merge.add_argument("--out", required=True)
    p_merge.add_argument("parts", nargs="+")
    p_prev = sub.add_parser("previous-tag", help="newest v* tag by version order")
    p_prev.add_argument("--line", help="restrict to one release line, e.g. 1.0")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "take":
            take(args.out, members=args.members, jobs=args.jobs)
            return EXIT_SAME
        if args.command == "check":
            return check(golden=args.golden, members=args.members, jobs=args.jobs,
                         report_path=args.report, full=args.full)
        if args.command == "compare":
            if args.base:
                if len(args.files) > 1:
                    raise ToolError("with --base give at most one file (NEW)")
                old = fingerprint_at(args.base)
                new = load_fingerprint(Path(args.files[0] if args.files else GOLDEN))
            else:
                if len(args.files) != 2:
                    raise ToolError("compare needs OLD and NEW, or --base TAG")
                old, new = (load_fingerprint(Path(f)) for f in args.files)
            return _emit(compare(old, new, args.members), args.report, args.full)
        if args.command == "merge":
            merge(args.out, args.parts)
            return EXIT_SAME
        if args.command == "previous-tag":
            tag = newest_release_tag(line=args.line)
            if tag is None:
                raise ToolError("no v* tag" + (f" on line {args.line}" if args.line else ""))
            print(tag)
            return EXIT_SAME
    except ToolError as exc:
        print(f"output_fingerprint: {exc}", file=sys.stderr)
        return EXIT_TOOL
    return EXIT_TOOL


if __name__ == "__main__":
    sys.exit(main())
