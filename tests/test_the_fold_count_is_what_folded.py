"""An owner's row counts the instance findings that folded into it, and
no others (#576).

A Patient or Study write reaches each instance's copy of its tag, and an
instance finding on that copy later in the pass folds into the write
instead of running (#496). The owner's audit row says how many did. It
has to say so when it is appended -- Pin A refuses a row rewritten later
-- so the count is predicted before the pass, by
`_foldable_instance_findings`.

The prediction deduplicated on `(uid, path, attr, removed)` while the
apply loop deduplicates on `_remediation_key`, `(uid, path, attr)`, with
no action in it. So a hand-built list carrying a REMOVE and a REPLACE on
one instance tag was counted twice over, once per action, and the loop
reached one: owner REPLACE on `patient_name`, then instance REMOVE and
REPLACE on `0010,0010`, ran the REMOVE, skipped the REPLACE as a
duplicate, and the owner's row said "1 instance-level finding on this tag
folded into it" over a pass that folded none. Measured on ac33641 and
57400d1: 4 of 28 patient cases and 27 of 117 study-date cases wrong.

**The rule these tests hold.** The count is per loop key. A copy the
owner wrote a value to counts a key when that key's *first* finding is
not a REMOVE; a copy the owner removed counts a key when *any* of its
findings is a REMOVE. Both halves follow from what the loop does with
the copy. A value copy is present, so the first finding ends the key: it
folds, or it is a REMOVE and applies. A removed copy is absent, so every
non-REMOVE on it declines (#547 for REPLACE, #569 for SHIFT) and a
decline claims no key, so the key reaches its first REMOVE, which folds.
The second half is why this ships with #569: before it, a SHIFT
re-created the absent copy and claimed the key, and 7 study cases (an
owner REMOVE with a SHIFT before the REMOVE) stay wrong under this rule
alone; with #569 alone and the old count, 20 do.

**Asserted per case:** the count the row claims equals the folds the pass
made (a spy on `_folds_into_owner`), and the instance's remediation
record vouches for what its copy ends holding -- the count fix changes
text only, and this is the check that it did not move a write.

**Why this file imports what it does.** It reaches the pass through
`isocenter.remediation`, builds the graph from `isocenter.entities` and
the findings from `isocenter.privacy`, so it charges all three modules'
probe rows; see `test_mutation_probe_targets.py`.
"""
import itertools
import re

import pytest

from isocenter.entities import Instance, Patient, Series, Study
from isocenter.privacy import PhiFinding, PhiRemediation
from isocenter.remediation import RemediationService

SECRET = b"\x01" * 32
NAME, STUDY_DATE = "0010,0010", "0008,0020"
INSTANCE_UID = "1.2.3.4.5"
ACTIONS = {"REMOVE_TAG": "REMOVE", "REPLACE_TAG": "REPLACE", "SHIFT_DATE": "SHIFT"}


class _Rows:
    def __init__(self):
        self.rows = []

    def log_audit_batch(self, rows):
        self.rows.extend(rows)

    def log_audit(self, *row):
        self.rows.append(row)


def _cases(level, owner_actions, chain_actions):
    for owner in owner_actions:
        for length in (1, 2, 3):
            for chain in itertools.product(chain_actions, repeat=length):
                label = f"{level}-{ACTIONS[owner]}-[{','.join(ACTIONS[a] for a in chain)}]"
                yield pytest.param(level, owner, chain, id=label)


CASES = [
    *_cases("patient", ("REPLACE_TAG", "REMOVE_TAG"), ("REMOVE_TAG", "REPLACE_TAG")),
    *_cases("study", ("SHIFT_DATE", "REPLACE_TAG", "REMOVE_TAG"),
            ("REMOVE_TAG", "REPLACE_TAG", "SHIFT_DATE")),
]


def _finding(entity, uid, entity_type, target, tag, action, original):
    new = None
    if action == "REPLACE_TAG":
        new = "19000101" if tag == STUDY_DATE else "ANONYMIZED"
    return PhiFinding(
        entity_uid=uid, entity_type=entity_type, field_name=target,
        value=original, reason="r", tag=tag, entity=entity,
        remediation_proposal=PhiRemediation(
            action, target, new_value=new, original_value=original,
            metadata={"patient_id": "P1"}))


def _pass(monkeypatch, level, owner_action, chain):
    """Run one case; return (claimed, folded, instance, tag, rows)."""
    patient = Patient("P1", "Doe^Jane")
    study = Study("1.2.3", "20040119")
    series = Series("1.2.3.4", "CT", 1)
    instance = Instance(INSTANCE_UID, "1.2.840.10008.5.1.4.1.1.2", 1)
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)
    if level == "patient":
        tag, original, owner_uid = NAME, "Doe^Jane", "P1"
        owner = _finding(patient, owner_uid, "Patient", "patient_name", tag,
                         owner_action, original)
    else:
        tag, original, owner_uid = STUDY_DATE, "20040119", "1.2.3"
        owner = _finding(study, owner_uid, "Study", "study_date", tag,
                         owner_action, original)
    instance.set_attr(tag, original)

    folded = []
    real = RemediationService._folds_into_owner

    def spy(self, finding):
        result = real(self, finding)
        if result:
            folded.append(finding)
        return result

    monkeypatch.setattr(RemediationService, "_folds_into_owner", spy)
    rows = _Rows()
    RemediationService(store_backend=rows, project_secret=SECRET).apply_remediation(
        [owner] + [_finding(instance, INSTANCE_UID, "Instance", tag, tag, action, original)
                   for action in chain])

    owner_rows = [r[2] for r in rows.rows
                  if r[1] == owner_uid and r[0] != "REMEDIATION_DECLINED"]
    assert len(owner_rows) == 1, rows.rows
    match = re.search(r"(\d+) instance-level findings? on this tag folded",
                      owner_rows[0])
    return (int(match.group(1)) if match else 0), len(folded), instance, tag, rows


@pytest.mark.parametrize("level,owner_action,chain", CASES)
def test_the_owner_row_counts_exactly_the_folds(monkeypatch, level, owner_action, chain):
    claimed, folded, instance, tag, rows = _pass(monkeypatch, level, owner_action, chain)

    assert claimed == folded, rows.rows

    # The record speaks for the copy, whatever the count said.
    held = instance.attributes.get(tag)
    if held is None:
        assert tag not in instance.attributes
        assert instance.remediation_vouches_for(tag, None), rows.rows
    else:
        assert (instance.remediation_vouches_for(tag, held)
                or instance.date_shift_vouches_for(tag, held)), (held, rows.rows)


def test_the_issue_as_filed_claims_no_fold(monkeypatch):
    """Owner REPLACE, instance REMOVE then REPLACE: the REMOVE runs, the
    REPLACE is a duplicate, nothing folds. Red before: "1 ... folded"."""
    claimed, folded, instance, tag, rows = _pass(
        monkeypatch, "patient", "REPLACE_TAG", ("REMOVE_TAG", "REPLACE_TAG"))

    assert (claimed, folded) == (0, 0), rows.rows
    assert tag not in instance.attributes
    assert instance.remediation_vouches_for(tag, None)
    assert [r[0] for r in rows.rows] == ["REMEDIATION_REPLACE", "REMEDIATION_REMOVE"]


def test_a_shift_on_a_removed_copy_declines_and_the_removal_folds(monkeypatch):
    """Owner REMOVE on the study date, instance SHIFT then REMOVE. The
    SHIFT meets an absent copy and declines (#569), so the key reaches the
    REMOVE, which folds. Without #569 the SHIFT re-created the copy and
    the row claimed a fold that did not happen."""
    claimed, folded, instance, tag, rows = _pass(
        monkeypatch, "study", "REMOVE_TAG", ("SHIFT_DATE", "REMOVE_TAG"))

    assert (claimed, folded) == (1, 1), rows.rows
    assert tag not in instance.attributes
    assert [r[0] for r in rows.rows] == ["REMEDIATION_REMOVE", "REMEDIATION_DECLINED"]


def test_a_removal_after_a_declined_replace_is_counted(monkeypatch):
    """Owner REMOVE, instance REPLACE then REMOVE: the first finding on the
    key is not a REMOVE and still the removal folds, because the REPLACE
    declines on the absent copy (#547). A rule of "the first finding
    decides" for a removed copy counts none."""
    claimed, folded, _, _, rows = _pass(
        monkeypatch, "patient", "REMOVE_TAG", ("REPLACE_TAG", "REMOVE_TAG"))

    assert (claimed, folded) == (1, 1), rows.rows
