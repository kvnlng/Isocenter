import hashlib
from typing import List, Optional
from datetime import datetime, timedelta
from tqdm import tqdm
from .entities import PhiStatus
from .privacy import PhiFinding, PhiRemediation
from .logger import get_logger

#: Every action type `_apply_single_remediation` emits, spelled once for
#: the report's evidence check: a session that anonymized must find at
#: least one of these in its audit summary to grade PASS (#254). A new
#: emitter spelling must be added here too -- omitting it fails safe,
#: since a run whose only rows carry the unlisted spelling grades
#: REVIEW_REQUIRED rather than PASS.
REMEDIATION_ACTION_TYPES = frozenset({
    "REMEDIATION_REPLACE",
    "REMEDIATION_SHIFT_DATE",
    "REMEDIATION_REMOVE",
})

#: A remediation that was proposed and did not run, so the value it
#: targeted is still in the graph and will reach the exported file
#: (#301). One spelling for every declining path, not one per reason:
#: `audit_summary` counts action types, and the `REMEDIATION_REMOVE`
#: comment below already rejects splitting one behaviour across two rows
#: of the report's section 2. The reason lives in the row's `details`.
#:
#: **Deliberately not in `REMEDIATION_ACTION_TYPES`.** That frozenset is
#: the ANONYMIZE evidence set `generate_report` checks (#254); if a
#: decline counted as evidence, a run in which every remediation
#: declined would satisfy the check that exists to catch a run whose
#: remediation rows went missing. Pinned by
#: `tests/test_declined_remediation_is_recorded.py`.
REMEDIATION_DECLINED = "REMEDIATION_DECLINED"


class RemediationService:
    """
    Applies remediation proposals found by the PhiInspector.

    Handles data anonymization (Replacement/Removal) and semantic modifications like
    date shifting, ensuring data consistency and audit logging.
    """

    def __init__(self, store_backend=None, date_jitter_config: Optional[dict] = None):
        """
        Initialize the remediation service.

        Args:
            store_backend (optional): Persistence layer for logging audit trails.
            date_jitter_config (dict, optional): Configuration for date shifting ({min_days, max_days}).
        """
        self.logger = get_logger()
        self.store_backend = store_backend
        self.jitter_config = date_jitter_config or {"min_days": -365, "max_days": -1}

    def apply_remediation(self, findings: List[PhiFinding]):
        """
        Iterates through the findings and applies valid remediation proposals.

        Dedupes findings (multiple findings might target the same attribute) before applying.
        Flushes audit logs in batch at the end.

        Args:
            findings (List[PhiFinding]): The list of findings with proposals to execute.

        Returns:
            int: How many remediations were applied. Failures are logged and
                excluded, so this is a count of what actually changed.
        """
        processed_entities = set()  # To avoid double-processing if multiple findings point to same entity/attr
        audit_buffer = []
        failures = 0

        for finding in tqdm(findings, desc="Anonymizing Metadata", unit="finding"):
            if not finding.remediation_proposal:
                continue

            # Deduping key: what is being changed, and where it lives.
            #
            # It used to be `(entity_uid, field_name)`, and both halves
            # were wrong. `field_name` is a display string from the
            # config's `name`, falling back to the literal "Unknown Tag"
            # -- so two config entries without names collapsed into one
            # key and the second tag was never remediated. And a finding
            # raised inside a sequence carries the *instance's* UID,
            # nested items having none of their own, so two annotation
            # items holding the same tag collapsed too (reachable once the
            # scan began opening sequences, #57).
            #
            # `target_attr` is the attribute the proposal actually writes,
            # which is what "already handled" should mean.
            key = (finding.entity_uid, finding.entity_path,
                   finding.remediation_proposal.target_attr)
            if key in processed_entities:
                continue

            try:
                # Keyed on the *outcome*, not on "did not raise". A
                # declining path returns False, and adding its key would
                # count a remediation that did not happen as applied --
                # `anonymize()` prints that total -- and would suppress a
                # later finding against the same attribute, which the key
                # cannot tell apart because it carries no action type
                # (#301).
                if self._apply_single_remediation(finding, audit_buffer):
                    processed_entities.add(key)
            except Exception as e:
                failures += 1
                self.logger.error(
                    f"Failed to apply remediation for {
                        finding.entity_uid} ({
                        finding.field_name}): {e}")

        # Flush audit logs
        if self.store_backend and audit_buffer:
            self.logger.info(f"Flushing {len(audit_buffer)} audit logs...")
            self.store_backend.log_audit_batch(audit_buffer)

        if failures:
            self.logger.warning(
                f"{failures} of {failures + len(processed_entities)} "
                "remediations failed and were not applied. The values they "
                "targeted are still present.")

        return len(processed_entities)

    def _apply_single_remediation(self, finding: PhiFinding, audit_buffer: list = None):
        """
        Executes a single remediation proposal on the target entity.

        Handles actions:
        - REPLACE_TAG: Updates attribute values.
        - SHIFT_DATE: Applies deterministic date shifting logic.
        - REMOVE_TAG: Deletes attributes.

        Args:
            finding (PhiFinding): The finding containing the proposal.
            audit_buffer (list, optional): Buffer to append audit log entries (optimization).

        Returns:
            bool: True when the entity was actually changed. False on
                every declining path -- and `apply_remediation` keys its
                dedup set and its returned count on this, so a decline
                neither counts as an applied remediation nor suppresses
                a later finding that could have succeeded against the
                same attribute (#301). The dedup key carries no action
                type, so before this a `REMOVE_TAG` that declined
                blocked a `REPLACE_TAG` on the same tag, and
                `apply_remediation` returned both of them as applied
                while `anonymize()` printed the total.
        """
        proposal = finding.remediation_proposal
        entity = finding.entity

        if not entity:
            self.logger.warning(
                f"Finding for {
                    finding.entity_uid} has no entity reference. Skipping.")
            self._record_decline(
                finding,
                "no entity reference; the finding could not be resolved "
                "against the live graph",
                audit_buffer)
            return False

        action_type = ""
        details = ""

        if proposal.action_type == "REPLACE_TAG":
            # Direct replacement

            # 1. Generic DicomItem support (Instance, Series, etc.)
            if hasattr(entity, "set_attr"):
                # Tag ID is expected in proposal.target_attr (e.g. "0010,0010")
                entity.set_attr(proposal.target_attr, proposal.new_value)
                details = f"Remediated {
                    finding.entity_uid} (Tag {
                    proposal.target_attr}) -> {
                    proposal.new_value}"
                action_type = "REMEDIATION_REPLACE"

            # 2. Python Object Attribute support (Patient.patient_name)
            elif hasattr(entity, proposal.target_attr):
                setattr(entity, proposal.target_attr, proposal.new_value)
                if hasattr(entity, "mark_modified"):
                    entity.mark_modified()
                details = f"Remediated {
                    finding.entity_uid}: {
                    proposal.target_attr} -> {
                    proposal.new_value}"
                action_type = "REMEDIATION_REPLACE"

            else:
                self.logger.warning(
                    f"Entity {
                        finding.entity_uid} (Type: {
                        type(entity).__name__}) has no attribute or setter for {
                        proposal.target_attr}")
                self._record_decline(
                    finding,
                    f"{type(entity).__name__} has no attribute or setter "
                    f"for {proposal.target_attr}",
                    audit_buffer)
                return False

        elif proposal.action_type == "SHIFT_DATE":
            # Deterministic Date Shifting
            patient_id = self._resolve_patient_id(entity, proposal)
            if not patient_id:
                self.logger.warning(
                    f"Could not resolve PatientID for {
                        finding.entity_uid}. Skipping date shift.")
                self._record_decline(
                    finding,
                    f"could not resolve a PatientID to seed the jitter "
                    f"for {proposal.target_attr}, so the date is "
                    f"unshifted",
                    audit_buffer)
                return False

            shift_days = self._get_date_shift(patient_id)
            new_date = self._shift_date_string(proposal.original_value, shift_days)

            if new_date:
                if hasattr(entity, "set_attr"):
                    entity.set_attr(proposal.target_attr, new_date)
                else:
                    setattr(entity, proposal.target_attr, new_date)
                    if hasattr(entity, "mark_modified"):
                        entity.mark_modified()

                # Update tracking flag if it's a Study or Instance (or any entity with the flag)
                if hasattr(entity, "date_shifted"):
                    entity.date_shifted = True

                details = f"Date Shifted {
                    finding.entity_uid}: {
                    proposal.target_attr} ({shift_days} days)"
                action_type = "REMEDIATION_SHIFT_DATE"
            else:
                val_str = str(proposal.original_value).strip(
                ) if proposal.original_value is not None else ""
                if not val_str:
                    # The one non-success path that deliberately writes
                    # no decline row. An empty value is not retained PHI:
                    # there is nothing to shift and nothing left behind,
                    # so a row here would take the run to
                    # REVIEW_REQUIRED over a graph with nothing wrong in
                    # it -- the cry-wolf shape that gets a signal
                    # ignored.
                    self.logger.info(
                        f"Skipping jitter for empty date on {
                            finding.entity_uid} (Tag: {
                            proposal.target_attr})")
                    return False

                self.logger.warning(
                    f"Invalid date format for {
                        finding.entity_uid} (Tag: {
                        proposal.target_attr}): {
                        proposal.original_value}")
                self._record_decline(
                    finding,
                    f"invalid date format for {proposal.target_attr}: "
                    f"{proposal.original_value!r}, so the value is "
                    f"unchanged",
                    audit_buffer)
                return False

        elif proposal.action_type == "REMOVE_TAG":
            # 1. Generic DicomItem support
            if hasattr(entity, "attributes") and isinstance(entity.attributes, dict):
                if proposal.target_attr in entity.attributes:
                    del entity.attributes[proposal.target_attr]
                    # `attributes` is a plain dict, so deleting from it bumps
                    # no revision -- unlike `set_attr`, which does. Without
                    # this an already-saved instance reported no unsaved
                    # changes after its PHI was stripped, the next save
                    # skipped it, and the identifier stayed in the database.
                    entity.mark_modified()
                    details = f"Removed Tag {proposal.target_attr} from {finding.entity_uid}"
                    action_type = "REMEDIATION_REMOVE"
                elif proposal.target_attr in getattr(entity, "sequences", {}):
                    # A private sequence is a private tag, and the sweep
                    # asks for it by name since #167. Without this arm
                    # the finding is filed, the report says the block
                    # was removed, and the exporter writes it anyway.
                    #
                    # Same `mark_modified()` reasoning as the attribute
                    # arm above: `del` on the dict bumps no revision, so
                    # without it the next save skips an instance whose
                    # private sequence was just stripped and the store
                    # keeps it.
                    #
                    # `action_type` stays `REMEDIATION_REMOVE`:
                    # `audit_summary` counts action types, and a second
                    # spelling would split one behaviour across two rows
                    # of the report's section 2. The `details` text
                    # carries the distinction.
                    del entity.sequences[proposal.target_attr]
                    entity.mark_modified()
                    details = (f"Removed Sequence {proposal.target_attr} "
                               f"from {finding.entity_uid}")
                    action_type = "REMEDIATION_REMOVE"
            # 2. Python Object Attribute
            elif hasattr(entity, proposal.target_attr):
                setattr(entity, proposal.target_attr, None)
                if hasattr(entity, "mark_modified"):
                    entity.mark_modified()
                details = f"Cleared Attribute {proposal.target_attr} on {finding.entity_uid}"
                action_type = "REMEDIATION_REMOVE"

        # Logging & Auditing
        if action_type:
            # Recorded after the change, never before: remediation modifies
            # the entity, so a status stamped first would name a revision
            # the entity immediately leaves behind and would read as
            # UNSCANNED the moment anyone asked.
            entity.record_phi_status(PhiStatus.REMEDIATED)
            self.logger.info(details)
            if self.store_backend:
                if audit_buffer is not None:
                    # Five elements, including the `loss_scope` (#146)
                    # and `element_tag` (#167) slots no remediation ever
                    # fills: `log_audit_batch` takes one shape, not one
                    # of several.
                    audit_buffer.append(
                        (action_type, finding.entity_uid, details, None, None))
                else:
                    self.store_backend.log_audit(action_type, finding.entity_uid, details)
            return True
        else:
            # Every other non-success path above `return`s, so reaching
            # here with an empty `action_type` means one of three things,
            # and none of them wrote a word before #301: a `REMOVE_TAG`
            # whose target is in neither `attributes` nor `sequences`; a
            # `REMOVE_TAG` against an entity with no `attributes` dict
            # *and* no matching Python attribute (the arm at the bottom
            # of that block is an `elif` on the outer `hasattr`, so both
            # fall past it); or a proposal carrying an action type this
            # method does not implement.
            #
            # One `else` here rather than an `else` nested in the
            # `attributes` arm: nested, it would cover only the first of
            # the three, and it would stop covering a fourth if one were
            # ever added above.
            self._record_decline(
                finding,
                f"{proposal.action_type} on {proposal.target_attr} "
                f"matched no applicable arm for "
                f"{type(entity).__name__}",
                audit_buffer)
            return False

    def _record_decline(self, finding: PhiFinding, reason: str,
                        audit_buffer: list = None):
        """Write the audit row for a remediation that did not run (#301).

        The log line each caller already emits is kept and this is added
        beside it: the log was never the problem, its being the *only*
        record was. A log line reaches whoever is watching stdout at the
        time; the compliance report, the grade, and any later session
        reading this store all key on the audit table, and a value the
        pipeline was told to remove and did not remove is exactly the
        thing that has to survive into all three.

        The reason is prose in `details`, not a column. **Rejected: a
        `decline_reason` column** -- no caller would branch on it, since
        grading turns on the row existing and the report lists the rows,
        so it would be a column with no reader. **Rejected: re-using
        `element_tag`** -- that slot is documented "for `SCAN_GAP` only"
        in three places (`persistence.py`'s reader, `log_audit`'s
        docstring and `log_audit_batch`'s), and filling it here would
        falsify all three.

        Deliberately does **not** call `record_phi_status`. The success
        block stamps `REMEDIATED`; a declined entity has not been
        remediated and its status has to keep saying so.

        Same two-shape dispatch as the success block, for the same
        reason: `log_audit_batch` takes one tuple shape, so a caller with
        no `loss_scope` and no `element_tag` to describe still writes
        both slots.
        """
        details = f"Remediation declined for {finding.entity_uid}: {reason}"
        if not self.store_backend:
            return
        if audit_buffer is not None:
            audit_buffer.append(
                (REMEDIATION_DECLINED, finding.entity_uid, details,
                 None, None))
        else:
            self.store_backend.log_audit(
                REMEDIATION_DECLINED, finding.entity_uid, details)

    def _resolve_patient_id(self, entity, proposal: PhiRemediation = None) -> Optional[str]:
        """
        Resolves the PatientID for a given entity or proposal.

        Essential for deterministic date shifting which relies on a stable PatientID seed.

        Args:
            entity: The entity being modified.
            proposal (PhiRemediation): The proposal containing metadata.

        Returns:
            Optional[str]: The PatientID string if resolvable.
        """
        # 1. Check metadata in proposal (Best for Date Shifting logic)
        if proposal and proposal.metadata and "patient_id" in proposal.metadata:
            return proposal.metadata["patient_id"]

        # 2. Check entities directly
        if hasattr(entity, "patient_id") and entity.patient_id:
            return entity.patient_id

        # 3. If the entity matches our Patient class structure (it has 'patient_id' field)
        # We already checked hasattr above.

        return None

    def _get_date_shift(self, patient_id: str) -> int:
        """
        Generates a deterministic shift between min_days and max_days based on PatientID.

        Uses SHA-256 hash of PatientID to seed the offset calculation, ensuring
        consistent shifting for the same patient across sessions.

        Args:
            patient_id (str): The seed (PatientID).

        Returns:
            int: The number of days to shift (positive or negative).
        """
        # Create a hash of the PatientID
        hash_obj = hashlib.sha256(patient_id.encode())
        # Convert first 8 bytes to int
        val = int(hash_obj.hexdigest()[:8], 16)

        min_days = self.jitter_config.get("min_days", -365)
        max_days = self.jitter_config.get("max_days", -1)

        # Ensure correct order
        if min_days > max_days:
            min_days, max_days = max_days, min_days

        span = max_days - min_days + 1
        if span < 1:
            span = 1

        # Modulo span to get 0..span-1, then add min_days
        offset = (val % span) + min_days
        return offset

    def _shift_date_string(self, date_val, days: int) -> Optional[str]:
        """
        Shifts a date by `days`.

        Handles messy/varying input formats (DA, DT, ISO).
        Preserves original format where possible.

        Args:
            date_val (Union[str, date, datetime]): The original date value.
            days (int): Delta in days.

        Returns:
            Optional[str]: The shifted date string (or object), same type as input.
        """
        # Handles date and datetime objects
        if hasattr(date_val, 'strftime'):
            return date_val + timedelta(days=days)

        # Try parsing with multiple supported formats
        # We process them in order of specificity
        formats = [
            "%Y%m%d",                # DA: 20230515
            "%Y-%m-%d",              # ISO DA: 2024-05-11
            "%Y%m%d%H%M%S",          # DT: 20230515104822
            "%Y%m%d.%H%M%S",         # DT: 20230515.104822
            "%Y%m%d%H%M%S.%f",       # DT: 20230515104822.123456
            "%Y%m%d.%H%M%S.%f",      # DT: 20230515.104822.123456
            "%Y-%m-%d %H:%M:%S",     # ISO DT: 2024-05-11 10:48:22
            "%Y-%m-%dT%H:%M:%S"      # ISO T DT: 2024-05-11T10:48:22
        ]

        # Handle DICOM's potential for variable millisecond precision if needed
        # But for now let's try standard formats.
        # If the input contains fractional seconds that don't match %f (6 digits),
        # we might need to pad/truncate, but let's assume standard behavior first
        # based on the user provided example.
        # Pro-tip: 20230515.104822.677 is 3 digits. %f expects zero-padded to 6 usually in strict parsing,
        # but let's see. If it fails, we can add a pre-processing step.

        # Actually, for robust DICOM DT handling with generic python strptime,
        # we might need to handle the .FFFFFF part manually if it varies.
        # Let's try to match exactly what we can.

        date_str = str(date_val).strip()
        if not date_str:
            return None

        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                new_dt = dt + timedelta(days=days)
                return new_dt.strftime(fmt)
            except ValueError:
                continue

        # If we are here, we might have odd millisecond precision (e.g. .677)
        # Attempt to handle flexible fractional seconds if a dot is present towards the end
        if '.' in date_str:
            # Try to separate main part and fractional part
            # This is a basic fallback for proper DICOM DT like 20230515.104822.677
            try:
                # Naive check for the "dots" format
                parts = date_str.split('.')
                if len(parts) >= 3:  # YYYYMMDD.HHMMSS.mmmmmm
                    # Re-assemble without fraction to shift, then append fraction?
                    # No, shift might cross day boundary, so 'time' part doesn't change,
                    # but 'date' part changes.
                    # But if we cross DST? DICOM doesn't handle DST explicitly in DT usually, it's just local time.
                    # Actually, simplest is:
                    # 1. Parse just the date part (first 8 chars)
                    # 2. Shift it
                    # 3. Re-attach the rest?
                    # That preserves time exactly, which is what 'SHIFT_DATE' usually intends (days delta).
                    # Let's limit this special handling to when we know it's a date+time string
                    # BOTH halves are load-bearing, and the length check
                    # is the one that looks redundant and is not (#132).
                    # `strptime` with `%Y%m%d` is NOT length-strict:
                    # `"2023051"` parses as 2023-05-01 and `"230515"` as
                    # 2305-01-05, raising nothing. So an all-digit
                    # `parts[0]` of the wrong length reaches `strptime`
                    # happily, and without `len(...) == 8` this branch
                    # would shift a date the caller never wrote and
                    # re-attach `date_str[8:]`, which is misaligned for
                    # any length but 8. Measured with `or` substituted:
                    # `"2023051.104822.1234567"` returns
                    # `"20230511104822.1234567"` -- a fabricated value
                    # that still looks like a DT -- where the real code
                    # returns None and the caller declines to remediate.
                    # Pinned by `test_remediation_dates.py::
                    # test_a_malformed_date_part_is_declined_rather_than
                    # _shifted_into_a_fabricated_one`.
                    if len(parts[0]) == 8 and parts[0].isdigit():
                        base_date = parts[0]
                        rest = date_str[8:]  # everything after YYYYMMDD
                        dt = datetime.strptime(base_date, "%Y%m%d")
                        new_dt = dt + timedelta(days=days)
                        return new_dt.strftime("%Y%m%d") + rest
            except ValueError:
                pass

        return None

    def add_global_deid_tags(self, entity):
        """
        Stamps the entity with standard De-Identification Method tags.

        Adds:
        - (0012,0063) De-identification Method (Isocenter Signature)
        - (0012,0064) De-identification Method Code Sequence (Basic Profile)

        Args:
            entity: The instance/series to stamp.
        """
        if not hasattr(entity, "set_attr"):
            return

        # 1. 0012,0063 De-identification Method
        # We append our method string if one exists, or set it fresh
        # Standard says: "Creator of the De-identification"
        current_method = entity.attributes.get("0012,0063", [])
        if isinstance(current_method, str):
            current_method = [current_method]

        our_method = "Isocenter Privacy Profile"
        if our_method not in current_method:
            current_method.append(our_method)
            # Remove empty/None if any
            current_method = [x for x in current_method if x]

        entity.set_attr("0012,0063", current_method)

        # 2. 0012,0064 De-identification Method Code Sequence
        # We assume "Basic Application Confidentiality Profile" (113100)
        from .entities import DicomSequence, DicomItem

        seq = entity.sequences.get("0012,0064")
        if not seq:
            seq = DicomSequence(tag="0012,0064")

        # Create Item
        # Code: 113100, Scheme: DCM, Meaning: Basic Application Confidentiality Profile
        item = DicomItem()
        item.set_attr("0008,0100", "113100")
        item.set_attr("0008,0102", "DCM")
        item.set_attr("0008,0104", "Basic Application Confidentiality Profile")

        # Avoid duplication if possible?
        # A simple check: do we have an item with 113100?
        exists = False
        for existing_item in seq.items:
            if existing_item.attributes.get("0008,0100") == "113100":
                exists = True
                break

        if not exists:
            seq.items.append(item)

        entity.sequences["0012,0064"] = seq
