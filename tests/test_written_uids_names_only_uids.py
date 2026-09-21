"""`ExportSummary.written_uids` names written instances by SOP Instance UID,
and an instance with no UID is a failure, not a written path (#613).

The summary was built with `r.sop_instance_uid or r.output_path`, and the
field's comment said a UID-less instance written through `write_tree()`
reached it as its path, `None.dcm`. Measured on 63a64158, neither was
true: `write_tree()` builds no summary, and no door writes a UID-less
instance. The write puts the UID into the file's Media Storage SOP
Instance UID, and `save_as(..., enforce_file_format=True)` refuses an
empty one, so the instance fails, keyed `UNKNOWN`, and no file or temp
file is left. The path arm was dead; had it run, it would have put
`Subject_<Patient ID>/...` into a repr that is printed and logged (D10).
It is deleted.

**The deleted arm, on its own, is an equivalent mutant.** It is dead code,
so no test can kill it, and this file does not claim to. What it pins is
the door that keeps it dead: with `enforce_file_format=False` the
UID-less instance is written as the dotfile `.dcm` and reported `ok`, and
the summary gains a second entry (`""` under the fix; the output path,
Patient ID and all, with the arm restored).

`""`, not `None`: a `None` UID fails earlier, in the export's leading
`save(sync=True)`, with `sqlite3.IntegrityError` (#721), and never reaches
the write door.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.builders` are named, so their probe rows are charged. It does
not name `isocenter.io_handlers`; it is on that row for its measured kill
of M613-1, named in the row's comment (#441).
"""
import datetime
import os

import numpy as np
import pytest

from isocenter.builders import DicomBuilder
from isocenter.session import DicomSession

from support.ct_small_files import study_uid, write_ct

HAND_BUILT_PID = "REAL-MRN-613"


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _files(folder):
    """Every file under `folder`, dotfiles included: under the mutant the
    stray file is `.dcm`, which `glob` skips by default, so a glob count
    would be right by accident."""
    return sorted(os.path.join(root, name)
                  for root, _, names in os.walk(folder) for name in names)


def test_an_instance_without_a_uid_is_a_failure_not_a_written_uid(tmp_path):
    """One ingested CT and one hand-built instance whose SOP Instance UID is
    `""`. The CT is written and named by its UID; the other is a failure
    keyed `UNKNOWN` (so it was planned and refused, not skipped), nothing
    of the hand-built patient reaches the summary's repr, and exactly one
    file is on disk. Kills M613-1 (`enforce_file_format=True` turned off
    at the write) and M613-2 (that plus the path arm restored)."""
    write_ct(tmp_path / "in" / "a.dcm", "PAT-613", "6131")
    good_uid = f"{study_uid('6131')}.1.1"
    with DicomSession(str(tmp_path / "s.db")) as session:
        session.ingest(str(tmp_path / "in"))
        patient = (DicomBuilder.start_patient(HAND_BUILT_PID, "Hand^Built")
                   .add_study("2.3.9", datetime.date(2020, 1, 1))
                   .add_series("3.9.1", "CT", 1)
                   .add_instance("", "1.2.840.10008.5.1.4.1.1.7", 1)
                   .set_pixel_data(np.zeros((4, 4), dtype=np.uint8))
                   .end_instance().end_series().end_study().build())
        session.store.patients.append(patient)

        out = tmp_path / "out"
        summary = session.export(str(out), show_progress=False)

    assert summary.written_uids == [good_uid]
    assert summary.written == 1
    assert [uid for uid, _ in summary.failures] == ["UNKNOWN"]
    assert "no SOP Instance UID" in summary.failures[0][1]
    for secret in ("Subject_", HAND_BUILT_PID):
        assert secret not in repr(summary), repr(summary)
    files = _files(out)
    assert len(files) == 1, files
    assert files[0].endswith(f"{good_uid}.dcm")
    assert not [f for f in files if f.endswith(".tmp")]
