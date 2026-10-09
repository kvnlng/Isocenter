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

"No door writes a UID-less instance" was measured on a hand-built instance,
and until #936 it held for that one only: an *ingested* instance whose
`sop_instance_uid` a caller then emptied kept its source UID in
`0008,0018`, so the writer had a UID to copy and wrote the dotfile `.dcm`,
with `""` in `written_uids`. Since #936 the element follows the field and
the sentence holds for every instance
(`test_the_sop_uid_element_follows_the_field.py`).

**The deleted arm, on its own, is an equivalent mutant.** It is dead code,
so no test can kill it, and this file does not claim to. What it pins is
the door that keeps it dead: with `enforce_file_format=False` the
UID-less instance is written as the dotfile `.dcm` and reported `ok`, and
the summary gains a second entry (`""` under the fix; the output path,
Patient ID and all, with the arm restored).

`""`, not `None`: a `None` UID fails earlier, in the export's leading
`save(sync=True)`, with the save's own `ValueError` naming how many
instances hold no UID (#721; until then, sqlite's `IntegrityError`), and
never reaches the write door.

**Since GHSA-2rc2-r9r5-x7hm a second door stands in front of that one.**
`io_handlers.export_file_name` refuses an empty UID where the plan is
built, so the write is not reached. The failure keeps its key and its
first words (`UNKNOWN`, "an instance with no SOP Instance UID"); its
reason is the rule's. The test below runs both ways, because with the
rule in front the write's refusal is pinned by nothing else.

**Why this file imports what it does.** `isocenter.session` and
`isocenter.builders` are named, so their probe rows are charged.
`isocenter.io_handlers` is named by the patch that takes the file-name
rule away; the file was on that row already for its measured kill of
M613-1, named in the row's comment (#441).
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


@pytest.mark.parametrize("name_rule", [True, False],
                         ids=["as shipped", "the write's refusal alone"])
def test_an_instance_without_a_uid_is_a_failure_not_a_written_uid(
        tmp_path, monkeypatch, name_rule):
    """One ingested CT and one hand-built instance whose SOP Instance UID is
    `""`. The CT is written and named by its UID; the other is a failure
    keyed `UNKNOWN` (so it was planned and refused, not skipped), nothing
    of the hand-built patient reaches the summary's repr, and exactly one
    file is on disk.

    Two refusals stand between that instance and a file. Since
    GHSA-2rc2-r9r5-x7hm the file-name rule refuses an empty UID before
    the worker writes anything, so as shipped the write's own refusal is
    never reached, and M613-1 (`enforce_file_format=True` turned off at
    the write) survives the first parameter: measured. The second names
    the file as it was named before the rule, in the parent, where the
    plan is built, so the write's refusal is the only one left. That one
    kills M613-1 and M613-2 (M613-1 plus the path arm restored)."""
    if not name_rule:
        monkeypatch.setattr(
            "isocenter.io_handlers.export_file_name",
            lambda instance: f"{instance.sop_instance_uid}.dcm")
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
    assert ("cannot name a file: it is empty" in summary.failures[0][1]) is name_rule
    for secret in ("Subject_", HAND_BUILT_PID):
        assert secret not in repr(summary), repr(summary)
    files = _files(out)
    assert len(files) == 1, files
    assert files[0].endswith(f"{good_uid}.dcm")
    assert not [f for f in files if f.endswith(".tmp")]
