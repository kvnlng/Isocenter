from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from tqdm import tqdm
from .entities import JITTER_SCHEME_KEYED, PhiStatus
from .privacy import PhiFinding, PhiRemediation, canonical_patient_key
from .logger import describe_exception, get_logger

#: Every action type `_apply_single_remediation` emits, spelled once for
#: the report's evidence check: a session that anonymized must find at
#: least one of these in its audit summary to grade PASS (#254). A spelling
#: missing here grades a clean run REVIEW_REQUIRED, so it is pinned (#429):
#: tests/test_frozen_surface.py::
#: test_the_anonymize_evidence_set_is_exactly_what_remediation_writes
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


def _count(number: int, singular: str, plural: str = None) -> str:
    """A number and its noun, agreeing: `"1 instance copy"`, `"2 instance
    copies"`.

    One spelling for the three places #492 and #496 added a count to a
    `REMEDIATION_*` row or to the log. Written inline, each of the three
    said `"1 instance copies"` and `"1 instance-level findings"` -- a row
    an operator reads, and the one place the fold is explained at all, so
    the plural is not a cosmetic slip but a row that misdescribes itself.
    `plural` is optional because most of these nouns take `s`; `copy`
    does not, which is why the parameter exists rather than a bare
    `+ "s"` at the call sites.
    """
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


class RemediationService:
    """
    Applies remediation proposals found by the PhiInspector.

    Handles data anonymization (Replacement/Removal) and semantic modifications like
    date shifting, ensuring data consistency and audit logging.
    """

    def __init__(self, store_backend=None, date_jitter_config: Optional[dict] = None,
                 project_secret: Optional[bytes] = None):
        """
        Initialize the remediation service.

        Args:
            store_backend (optional): Persistence layer for logging audit trails.
            date_jitter_config (dict, optional): Configuration for date shifting ({min_days, max_days}).
            project_secret (bytes, optional): The store's project secret,
                which keys the date offset. Required at use: a keyed
                date shift without one raises `RuntimeError`.
                `Session.anonymize()` always passes it.
        """
        self.logger = get_logger()
        self.store_backend = store_backend
        self.project_secret = project_secret
        # Entities `_record_decline` named during the current pass;
        # reset by `apply_remediation` and read at its end.
        self._declined_entities: list = []
        # The instance copies an entity-level write reached during the
        # current pass, as `(id(instance), tag)`, and the foldable instance
        # findings waiting on each copy. Reset by `apply_remediation`; read
        # by `_folds_into_owner` and `_write_to_instances` (#496).
        self._owner_copies: set = set()
        self._pending_folds: dict = {}
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
        self._declined_entities = []
        self._owner_copies = set()
        # Entity-level findings first, everything else after in its own
        # relative order (#496). The owner's write has to land before the
        # instance findings on the same copies are judged, or which value
        # an instance keeps depends on the order the caller handed the
        # findings in. Stable, so #167's deepest-first order for
        # private-sequence removals survives.
        findings = self._entity_findings_first(findings)
        self._pending_folds = self._foldable_instance_findings(findings)
        # Keys of the instance findings folded into an owner's write, so
        # a duplicate folds once (#496).
        folded_keys = set()
        failures = 0
        # How many proposals actually reached
        # `_apply_single_remediation`, which is what the failure warning
        # below means by "of N". Its own counter and not
        # `failures + len(processed_entities)`: since #301 a decline is
        # excluded from `processed_entities`, so that sum stopped being
        # the number attempted the moment the dedup key moved to the
        # success arm -- it would report "1 of 2 failed" over a run that
        # tried five. The applied count is `processed_entities`; the
        # attempted count has to be counted.
        attempted = 0

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
            if key in processed_entities or key in folded_keys:
                continue

            # Folded, not applied: an owner's write in this pass already
            # put its value on this copy, and running the instance's own
            # proposal would put a second one there (#496). Stamped
            # REMEDIATED as its own success would have been, and inside
            # the loop -- so #491's pass-end demotion below still takes an
            # instance that declined something else back to IDENTIFIED.
            # Not in `processed_entities`: that set is the applied count
            # `anonymize()` returns, and this finding applied nothing of
            # its own. The owner's audit row says it was folded.
            if self._folds_into_owner(finding):
                folded_keys.add(key)
                finding.entity.record_phi_status(PhiStatus.REMEDIATED)
                continue

            try:
                # Incremented before the call, not after it: a proposal
                # that raises was still attempted, and the warning below
                # divides failures by this.
                attempted += 1
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
                        self._log_subject(finding)} ({
                        finding.field_name}): {describe_exception(e)}")

        if folded_keys:
            self.logger.info(
                f"{_count(len(folded_keys), 'instance-level finding')} folded "
                "into a patient's or study's remediation: the owner's value "
                "is already on the copies it reached (#496).")

        # An entity that declined during this pass does not leave it
        # REMEDIATED. The success block stamps REMEDIATED per proposal
        # and the decline path stamps nothing, so an instance with one
        # remediated finding and one declined ended the pass REMEDIATED
        # with the declined value still in it: the manifest read
        # `"anonymized": true` off that status while the same session's
        # report graded REVIEW_REQUIRED and named the decline in section
        # 3.3 (#486). Demoted here rather than by withholding the stamp,
        # because the success arm cannot know what a later proposal on
        # the same entity will do; the pass is the unit that can. Only
        # an entity the pass left REMEDIATED is touched: one that only
        # declined keeps whatever status it had, which
        # `test_a_pass_that_only_declined_leaves_the_status_alone` pins, and the
        # manifest keeps reading the status rather than the audit
        # trail's declines, so there is one source for the answer.
        for entity in self._declined_entities:
            if entity.phi_status is PhiStatus.REMEDIATED:
                entity.record_phi_status(PhiStatus.IDENTIFIED)

        # Flush audit logs
        if self.store_backend and audit_buffer:
            self.logger.info(f"Flushing {len(audit_buffer)} audit logs...")
            self.store_backend.log_audit_batch(audit_buffer)

        if failures:
            self.logger.warning(
                f"{failures} of {attempted} "
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
                    self._log_subject(finding)} has no entity reference. Skipping.")
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
                        self._log_subject(finding)} (Type: {
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
                        self._log_subject(finding)}. Skipping date shift.")
                self._record_decline(
                    finding,
                    f"could not resolve a PatientID to seed the jitter "
                    f"for {proposal.target_attr}, so the date is "
                    f"unshifted",
                    audit_buffer)
                return False

            # The scheme the scan recorded for this patient; a finding
            # built by hand carries none and is keyed, as every patient
            # this release creates is.
            shift_days = self._get_date_shift(
                patient_id,
                (proposal.metadata or {}).get("jitter_scheme",
                                              JITTER_SCHEME_KEYED))
            new_date = self._shift_date_string(proposal.original_value, shift_days)

            if new_date:
                if hasattr(entity, "set_attr"):
                    # Recorded **before** the write, not after (#510,
                    # #513). `anonymize()` does not drain the
                    # persistence manager -- `audit()` and `redact()` do
                    # -- so a background `save()` can serialize this
                    # entity between the two statements. A stored value
                    # whose record did not reach the store is raised
                    # again on the next load and shifted twice, which is
                    # #513 reintroduced as a race; a stored record whose
                    # value did not reach the store is harmless, because
                    # the equality check fails and the old value is
                    # correctly raised. One ordering is recoverable and
                    # the other is not.
                    #
                    # Called bare, where the `else` below guards the same
                    # name with `hasattr`, and the asymmetry is
                    # deliberate in **that** direction: `set_attr` and
                    # `record_date_shift` are both `DicomItem`'s, so
                    # anything reaching this branch has the second
                    # method by having the first, and a guard here would
                    # turn a future writer that somehow lacked it into a
                    # silently unrecorded shift -- #513 again, as a
                    # skip rather than an error. The `else` branch
                    # cannot make that argument: it is reached by
                    # anything *without* `set_attr`, which since #518 is
                    # `Study` on every shipped path but, by this arm's
                    # own duck-typing convention, may be any object
                    # carrying the names it writes (test doubles
                    # included). A third entity type is therefore
                    # required to record by construction on this side
                    # and merely invited to on the other; if one ever
                    # arrives without a record, this branch must raise
                    # and that one must skip.
                    entity.record_date_shift(proposal.target_attr, new_date)
                    entity.set_attr(proposal.target_attr, new_date)
                else:
                    # `Study`'s own one-value record, before the write
                    # for the same reason (#518). `new_date` is already
                    # the value the entity will hold -- `Study` only
                    # normalises `study_date`, and `format_study_date`
                    # renders both spellings identically -- so recording
                    # it first is recording what is about to be written.
                    if hasattr(entity, "record_date_shift"):
                        entity.record_date_shift(new_date)
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
                            self._log_subject(finding)} (Tag: {
                            proposal.target_attr})")
                    return False

                self.logger.warning(
                    f"Invalid date format for {
                        self._log_subject(finding)} (Tag: {
                        proposal.target_attr}); the value is left "
                    "unchanged")
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
            # A Patient or Study field was written, so the same value
            # goes onto each instance's own copy of the tag (#492).
            # Here and not in the arms above: the arms end in the five
            # `mark_modified()` lines `tests/test_remediation_invariants.py`
            # pins by number, and this block sits below all of them.
            # Before the REMEDIATED stamp below, which is only about
            # `entity`; the instances keep their own status.
            wrote = self._write_to_instances(entity, proposal.target_attr)
            if wrote is not None:
                written, folds = wrote
                verb = ("removed from" if action_type == "REMEDIATION_REMOVE"
                        else "written to")
                details += f"; {verb} {_count(written, 'instance copy', 'instance copies')}"
                # Said on this row because the folded findings get no rows
                # of their own (#496). Counted from the pass's pending set
                # before they run, so the row is complete when it is
                # appended: Pin A in `tests/test_frozen_surface.py`
                # refuses any rewrite of a row already in `audit_buffer`.
                if folds:
                    details += (f"; {_count(folds, 'instance-level finding')} on this "
                                f"tag folded into it")
            # The instance holding a nested item this wrote (#494). An
            # item has no link to its instance and `DicomItem.set_attr`
            # moves only the item, so without this the instance neither
            # read REMEDIATED nor had anything to save. The owner's
            # `mark_modified()` looks redundant beside the stamp below and
            # is not: an owner already reading REMEDIATED -- loaded that
            # way, or stamped by an earlier call and saved since --
            # short-circuits the stamp, and then this is the only thing
            # that makes the save write the replacement inside it (#173,
            # one level down). Before the stamp, for the reason the next
            # comment gives.
            owner = self._instance_owners.get(id(entity))
            if owner is not None:
                owner.mark_modified()
            # Recorded after the change, never before: remediation modifies
            # the entity, so a status stamped first would name a revision
            # the entity immediately leaves behind and would read as
            # UNSCANNED the moment anyone asked.
            entity.record_phi_status(PhiStatus.REMEDIATED)
            if owner is not None:
                owner.record_phi_status(PhiStatus.REMEDIATED)
            self.logger.info(self._log_line(action_type, finding, wrote))
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

    @staticmethod
    def _log_subject(finding: PhiFinding) -> str:
        """What a log line calls the finding's entity.

        A UID for an instance or a study, and never the value of a
        patient finding's `entity_uid`, which is the original Patient ID.
        The audit row keeps it: the store is documented as holding the
        ID-to-pseudonym map and every offset, and is guarded as the
        project secret is. The log file is not, and until 0.9.7 it held
        the same map line by line, so a log shipped with an export undid
        the de-identification.
        """
        if finding.entity_type == "Patient":
            return "a patient"
        return str(finding.entity_uid)

    def _log_line(self, action_type: str, finding: PhiFinding, wrote) -> str:
        """The log file's line for an applied remediation.

        Deliberately not the audit row's `details`: that row names the
        original Patient ID, the replacement written and a shift's days,
        and a log line pairing an identity with an offset is the offset
        handed to whoever reads the log. UIDs, the field and counts only.
        """
        proposal = finding.remediation_proposal
        verb = {"REMEDIATION_REPLACE": "Remediated",
                "REMEDIATION_SHIFT_DATE": "Date Shifted",
                "REMEDIATION_REMOVE": "Removed"}.get(action_type, action_type)
        line = f"{verb} {proposal.target_attr} on {self._log_subject(finding)}"
        if wrote is not None:
            written, folds = wrote
            line += f"; {_count(written, 'instance copy', 'instance copies')}"
            if folds:
                line += f"; {_count(folds, 'instance-level finding')} folded into it"
        return line

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

        Deliberately does **not** call `record_phi_status` here. The
        success block stamps `REMEDIATED` per proposal, and a declined
        entity has not been remediated, so its status has to keep
        saying so -- but per proposal is the wrong unit for that: a
        later proposal on the same entity can succeed and stamp
        REMEDIATED over the decline. So the entity is named in
        `_declined_entities` instead, and `apply_remediation` demotes
        every named entity that ends the pass REMEDIATED back to
        IDENTIFIED (#486). Named before the no-backend return below:
        the demotion is about the graph, not the audit table.

        Same two-shape dispatch as the success block, for the same
        reason: `log_audit_batch` takes one tuple shape, so a caller with
        no `loss_scope` and no `element_tag` to describe still writes
        both slots.
        """
        if finding.entity is not None:
            self._declined_entities.append(finding.entity)
            # A decline inside a sequence leaves the value in the
            # instance, so the instance is demoted with the item (#494).
            owner = self._instance_owners.get(id(finding.entity))
            if owner is not None:
                self._declined_entities.append(owner)
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

    #: The `Patient`/`Study` fields the exporter stamps onto every exported
    #: instance from the entity, with the tag each is the value of
    #: (`session._patient_attributes`, `_study_attributes`). Exactly these
    #: four: those helpers also read `birth_date`, `sex` and
    #: `accession_number` through `getattr`, and neither slots dataclass
    #: has such a field, so those arms never fire.
    #:
    #: Keyed on the field, not on `PhiFinding.tag`: a hand-built finding
    #: can carry a tag that disagrees with the field its proposal writes,
    #: and it is the field that was written.
    #:
    #: A class attribute this far down the class rather than a module
    #: constant at the top, on purpose: every line above the success
    #: block of `_apply_single_remediation` is counted by the five
    #: line-number pins in `tests/test_remediation_invariants.py`, and a
    #: constant at the top of the module would move all five (#310).
    ENTITY_FIELD_TAGS = {
        "patient_name": "0010,0010",
        "patient_id": "0010,0020",
        "study_date": "0008,0020",
        # Unreachable by any shipped scan: `Study.study_time` is never
        # populated by ingest, and no inspector raises a finding on it.
        # Kept deliberately, because the exporter stamps it from the
        # entity (`_study_attributes`) and the rule of this table is
        # "the fields the exporter stamps", not "the fields a scan
        # reaches today" -- a hand-built finding on it gets the same
        # one-truth treatment (#497 review, R7).
        "study_time": "0008,0030",
    }

    #: `id(nested item) -> Instance` holding it, for the findings of this
    #: pass raised inside a sequence (#494). Empty unless
    #: `_use_instance_owners` is called, so a service used without a
    #: session -- the direct tests, hand-built findings -- stamps the item
    #: alone, as it always did: it has no graph to find an owner in, and
    #: a guess from `entity_uid` could name the wrong one of two
    #: instances sharing a UID. Never mutated in place; the setter
    #: replaces it. Here rather than in `__init__` for `ENTITY_FIELD_TAGS`'
    #: reason above: nothing is added above the five pinned lines (#310).
    #: No reset in `apply_remediation` for the same reason, and none is
    #: needed: `Session.anonymize()` builds a fresh service per call.
    _instance_owners: dict = {}

    def _use_instance_owners(self, owners: dict) -> None:
        """Name the instance that holds each nested finding's item (#494).

        A nested success then marks that instance modified and stamps it
        REMEDIATED, and a nested decline names it for the pass-end
        demotion, exactly as a top-level finding on the instance would.

        What this does not do, deliberately (#553): the pass still speaks
        only for the findings handed to it, so a partial list stamps an
        owner REMEDIATED over findings it was not given, and a proposal
        that raised demotes nothing -- both true of top-level findings
        too, and decided there rather than here.
        """
        self._instance_owners = dict(owners)

    def _write_to_instances(self, entity, field: str) -> Optional[Tuple[int, int]]:
        """Write the value a Patient/Study field now holds onto each
        instance beneath it that carries the field's tag (#492).

        Each copy written is recorded in `_owner_copies`, so an instance
        finding on it later in the pass folds into this write (#496; see
        `_folds_into_owner`).

        Returns `(written, folds)` -- how many instance copies were
        written, and how many instance findings in this pass are waiting
        to fold into them -- or None when the write does not apply:
        `entity` has `set_attr` (it is an item, and the arm wrote its tag
        directly), or `field` is not one the exporter stamps.

        The value is read back off the entity, after the arm wrote it,
        rather than passed in from the arm: that is the one source the
        exporter reads too (`_patient_attributes`, `_study_attributes`),
        and it makes the three actions one case. REPLACE_TAG left the
        replacement; SHIFT_DATE left the shifted date, rendered here as
        the exporter renders it (`format_study_date`, "YYYYMMDD"), so an
        instance's DA string stays a DA string; REMOVE_TAG left None,
        which removes the tag from the instance as the exporter would
        write nothing for it. Written as the entity's value and not as a
        relative shift of the instance's own copy: a shift applied to a
        copy already shifted by the instance's own SHIFT_DATE finding --
        `anonymize(findings=[...])` takes them in any order -- would
        shift it twice, and an instance whose own StudyDate differed
        from the study's would stay different from what the file says.

        Only a copy that exists is replaced. An instance without the tag
        is not given one: the exporter stamps the patient module on
        every file because the module is mandatory there, but a tag
        fabricated onto the graph is #57's decoy in a new place.
        Top-level `attributes` only, because the exporter's stamp
        (`_merge(ds, ctx.patient_attributes)`) reaches the dataset root
        only; a nested copy is the instance scan's to find.

        Each instance keeps the PHI status it had, re-recorded at the
        revision the write produced -- the "edit whose content is known"
        exception, and the values written here are the patient module's
        own replacements, which identify nobody. Not stamped REMEDIATED:
        `anonymize(findings=[...])` with the patient's findings alone
        would then vouch for an instance whose own IDENTIFIED findings
        were never applied. Not left alone either: the write moves the
        revision, and a status at the old revision reads UNSCANNED,
        which the manifest reads as not anonymized (#486). An instance
        already UNSCANNED stays so; a status it has left is not revived.
        """
        if hasattr(entity, "set_attr"):
            return None
        tag = self.ENTITY_FIELD_TAGS.get(field)
        if tag is None:
            return None

        value = getattr(entity, field, None)
        if hasattr(value, "strftime"):
            # Lazy: io_handlers is the heavy module, and this is the
            # one spelling of "a Study's date as a DA string" (#189).
            from .io_handlers import format_study_date
            value = format_study_date(value)

        # A Patient walks its studies; a Study walks its own series and
        # no sibling's. `getattr` with defaults because the arm fires
        # for any object carrying the field, test doubles included.
        studies = getattr(entity, "studies", None)
        if studies is None:
            studies = [entity]
        written = folds = 0
        for study in studies:
            for series in getattr(study, "series", []):
                for instance in getattr(series, "instances", []):
                    if tag not in instance.attributes:
                        continue
                    status = instance.phi_status
                    if value is None:
                        del instance.attributes[tag]
                        instance.mark_modified()
                    else:
                        instance.set_attr(tag, value)
                    if status is not PhiStatus.UNSCANNED:
                        instance.record_phi_status(status)
                    # This copy now holds the owner's value; an
                    # instance finding on it later in the pass folds
                    # into this write rather than running (#496).
                    self._owner_copies.add((id(instance), tag))
                    folds += self._pending_folds.get((id(instance), tag), 0)
                    written += 1
        return written, folds

    @staticmethod
    def _entity_findings_first(findings) -> list:
        """The findings with every entity-level one first (#496).

        Entity-level means the finding's entity has no `set_attr`: a
        `Patient` or a `Study`, whose write reaches the instances' copies
        through `_write_to_instances`. Stable, so each half keeps the
        caller's relative order -- #167's deepest-first private-sequence
        removals included. A finding with no entity sorts with the
        entity-level half; it only declines, so where it sits changes
        nothing.
        """
        return sorted(findings, key=lambda f: hasattr(f.entity, "set_attr"))

    def _folds_into_owner(self, finding: PhiFinding) -> bool:
        """Whether an instance finding folds into an owner's write (#496).

        True when an entity-level write earlier in this pass reached
        exactly this copy: the finding's own instance, at the top level,
        on the tag the owner wrote. A folded finding does not run.

        Three things never fold, each measured before this was written:

        - **REMOVE.** It writes no second value, it is the policy's
          explicit request, and it was already order-independent: it runs
          after the owner's write, and the copy ends absent either way.
        - **A nested copy.** The owner's write and the exporter's stamp
          reach the dataset root only; a copy inside a sequence is the
          instance scan's to judge. There is no `entity_path` check for
          it, and none is needed: a nested finding's entity is its
          sequence item, and `_owner_copies` holds only the instances the
          owner wrote, so the lookup cannot find one. A check was written
          first and measured dead (#496 mutant N4).
        - **A copy the owner's write did not reach** -- the owner's own
          finding declined, or was not handed in. Folding it anyway would
          leave the original value in the instance dict, which is what
          `export_dataframe(expand_metadata=True)` and
          `get_flattened_instances()` read (#492). The owner's DECLINED
          row already grades such a run REVIEW_REQUIRED.
        """
        proposal = finding.remediation_proposal
        if proposal.action_type == "REMOVE_TAG":
            return False
        # No "is this an instance?" check either, for the same reason as
        # the nested case: a Patient, a Study or a None entity is never in
        # `_owner_copies`, which holds only the instances the owner wrote
        # -- so the lookup alone decides.
        return (id(finding.entity), proposal.target_attr) in self._owner_copies

    @staticmethod
    def _foldable_instance_findings(findings: list) -> dict:
        """The instance findings that fold if an owner's write reaches
        their copy, counted per `(id(instance), tag)` (#496).

        Counted before the pass so an owner's audit row can name its folds
        when it is appended: the row is complete from the start, and no
        row is rewritten after the fact, which Pin A in
        `tests/test_frozen_surface.py` refuses. Distinct dedup keys only,
        because a duplicate is skipped, not folded twice. REMOVE never
        folds (see `_folds_into_owner`), so it is never counted. Only a
        copy an owner's write reaches is ever asked for its count, so a
        finding counted here whose copy no owner reaches costs nothing.
        """
        pending = {}
        seen = set()
        for finding in findings:
            proposal = finding.remediation_proposal
            if (not proposal or proposal.action_type == "REMOVE_TAG"
                    or not hasattr(finding.entity, "set_attr")):
                continue
            key = (finding.entity_uid, finding.entity_path, proposal.target_attr)
            if key in seen:
                continue
            seen.add(key)
            copy = (id(finding.entity), proposal.target_attr)
            pending[copy] = pending.get(copy, 0) + 1
        return pending

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

    def _get_date_shift(self, patient_id: str, scheme: str) -> int:
        """
        Generates a deterministic shift between min_days and max_days for a patient.

        Seeded on the patient's **canonical key**
        (`privacy.canonical_patient_key`) rather than on the PatientID
        text, so the offset survives `anonymize()` replacing that id
        (#517): the original and the pseudonym the first pass wrote over
        it give one offset, so a date first shifted in a later pass
        lands where its siblings did.

        For a keyed patient the key is derived under the project secret,
        so the offset is deterministic per patient *within a project*
        (stores holding the same secret, under the same jitter range) and
        cannot be computed from anything an export carries. With no
        secret this raises `RuntimeError`; there is no unkeyed fallback.
        `scheme` is required for the same reason: a legacy patient
        (`JITTER_SCHEME_UNKEYED`) keeps the offset its store already
        gave its dates, and guessing the scheme would give it a second.

        Args:
            patient_id (str): The seed (PatientID), in either spelling.
            scheme (str): The patient's jitter scheme.

        Returns:
            int: The number of days to shift (positive or negative).
        """
        val = canonical_patient_key(patient_id, self.project_secret, scheme)

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

    @staticmethod
    def _shift_date_string(date_val, days: int) -> Optional[str]:
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


def _date_shift_declines(value) -> bool:
    """True when the `SHIFT_DATE` arm's parser would leave `value` unshifted (#498).

    **Who asks this, since 0.9.6.** The entity-level rule this was written
    under is gone: the scan no longer reads `Instance.date_shifted` (the
    field does not exist) nor `Study.date_shifted` in its instance arm.
    It asks a per-value record instead -- `DicomItem._shifted_dates`,
    the value the `SHIFT_DATE` arm wrote at that tag -- so a value the
    pipeline never shifted is raised because no record vouches for it,
    not because a predicate rescued it from a flag (#510, #513).

    The one place the old rule survives is the **legacy branch** of
    `PhiInspector._scan_instance`: an instance hydrated from a pre-0.9.6
    store carries no records for the dates it already holds, so reading
    "no record" as "not shifted" there would shift every already-shifted
    date in an archive a second time. Such an instance therefore keeps
    the entity-level rule, permanently, and that branch is the only
    caller inside the scan -- which is exactly why it asks *this* rather
    than a constant: #498's defect is that a value the arm could not
    parse must be raised again even while a sibling date on the same
    entity was shifted. So the rule the branch keeps is #498's version of
    the entity-level rule, not the older one, and the branch (with this
    caller) dies when no pre-0.9.6 store remains.

    The predicate itself is unchanged and is still read directly --
    `tests/test_declined_date_recurs.py` pairs it against the arm's own
    blank-value guard so the two spellings cannot silently disagree --
    which is why its blank guard below stays even though the scan's own
    blank arm now sits *above* the legacy branch and makes it unreachable
    from there.

    The answer is the arm's own parser rather than a second one, so the
    scan re-raises exactly what the arm declines: a DA range, a
    multi-valued DA, and a DT with a UTC offset are declined here the same
    as `'notadate'`. Blank is False because the arm skips a blank value
    without a decline -- nothing is left behind -- and answering True
    would make this predicate disagree with the arm it models.

    The parser is the *one* decline this models, and the arm has a second:
    an unresolvable PatientID. The sentence above says "the parser" rather
    than "the arm" on purpose, because widening it would invite a caller
    to trust this for a decline it does not see. Within one pass that
    other decline cannot be reached from here at all: one PatientID, off
    the patient being walked, seeds every date proposal in a pass
    (`_scan_study` and `_scan_instance` are both handed
    `patient.patient_id`, and a proposal's `metadata` carries it from the
    scan), so an unresolvable one declines the study's own date and every
    sibling date beside it and nothing on that patient is shifted at all.

    Across passes it is reachable on the legacy path -- `Study.date_shifted`
    persists, so a pass whose PatientID the pipeline has since **emptied**
    can ask this about a value shifted under the old one. The answer is
    still right: a True says re-raise, the arm declines on the PatientID
    instead of the parser, and the value still ends raised, declined and
    IDENTIFIED -- the same outcome by the other arm, which is why
    modelling that arm buys nothing and would mean threading an entity
    through a predicate that takes a value. A PatientID the pipeline
    *replaced* rather than emptied resolves and seeds the same offset it
    seeded in pass 1 (#517), so that case reaches the parser, which was
    measured across both spellings of the identity rather than reasoned
    about.
    """
    if value is None or not str(value).strip():
        return False
    # The class's own module; the parser is private to it, not to the class.
    return RemediationService._shift_date_string(  # pylint: disable=protected-access
        value, 0) is None
