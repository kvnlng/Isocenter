
import os
import pytest
from isocenter.session import DicomSession
from isocenter.services import RedactionOutcome, RedactionService
from isocenter.entities import Instance, Patient, Study, Series

def test_redaction_result_application_logic():
    """
    Regression Test for UID Mismatch Bug.
    Verifies that RedactionService results can be correctly applied to instances
    even if the Worker thread/process changes the SOP Instance UID (regenerate_uid).
    """
    # Setup
    db_path = "debug_redact_test.db"
    if os.path.exists(db_path): os.remove(db_path)
    session = DicomSession(db_path)

    try:
        pat = Patient("P1", "Test"); st = Study("S1", "D1"); se = Series("SE1", "M1", 1)
        inst = Instance("1.2.3.OLD_UID", "1.2.840.10008...", 1)
        se.instances.append(inst); st.series.append(se); pat.studies.append(st)
        session.store.patients.append(pat)

        # Manually configure the map/task as 'redact' would
        all_tasks = [{"instance": inst}]
        # Map is keyed by OLD_UID because it's built before processing
        instance_map = {t['instance'].sop_instance_uid: t['instance'] for t in all_tasks}

        # Simulate Worker Result (Mutation)
        # Worker changes UID to NEW_UID
        # Worker MUST return 'original_sop_uid' for matching
        mutation = {
            "original_sop_uid": "1.2.3.OLD_UID", # The fix
            "sop_uid": "1.2.3.NEW_UID",
            "attributes": {"0010,0010": "REDACTED"},
            "pixel_hash": "NEW_HASH",
            "pixel_loader": None
        }

        # Call the production applier, do not re-implement it here. This
        # test used to carry its own copy of session.py's matching line --
        # `mutation.get('original_sop_uid') or mutation.get('sop_uid')` --
        # and assert against the copy. #239 deleted that `or` fallback from
        # `_apply_redaction_outcomes` (the map is keyed on **pre**-redaction
        # UIDs, so the post-redaction `sop_uid` could never match anything,
        # and once #228 made `sop_uid` the instance's new identity, one name
        # meaning both was the bug). The replica survived, so the test went
        # on green while pinning logic the package no longer runs -- the
        # #234 drift class. Whatever the applier does is now what is under
        # test.
        applied, failures = DicomSession._apply_redaction_outcomes(
            [RedactionOutcome(ok=True, sop_instance_uid="1.2.3.OLD_UID",
                              mutation=mutation)],
            instance_map)

        assert failures == [], failures
        assert applied == 1, "Main process failed to match worker result to instance using original_sop_uid"
        assert inst.attributes["0010,0010"] == "REDACTED", "Attributes were not applied to the instance"

    finally:
        session.close()
        if os.path.exists(db_path): os.remove(db_path)

if __name__ == "__main__":
    test_redaction_result_application_logic()
