from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from .parallel import progress_bar
from .entities import JITTER_SCHEME_KEYED, Instance, Patient, PhiStatus, Study
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
        # The copies an entity-level write reached this pass, `(id(instance),
        # tag) -> removed`, and the foldable instance findings waiting on
        # each (#496, #537: a REMOVE folds only where the owner removed).
        # Reset by `apply_remediation`; read by `_folds_into_owner`.
        self._owner_copies: dict = {}
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
        self._declined_entities, self._satisfied_keys = [], set()
        self._owner_copies = {}
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

        for finding in progress_bar(findings, desc="Anonymizing Metadata", unit="finding"):
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
            # which is what "already handled" should mean. One spelling,
            # shared with the scan tally that settles the pass (#553).
            key = _remediation_key(finding)
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
                self._record_decline(finding, self._raised(finding, e), audit_buffer)

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
        #
        # The same holds for what the pass was never handed (#553): a
        # raise is a decline, and an entity the last audit raised more
        # against than the passes on it handled is demoted too, settled
        # against the scan tally. `_settle_statuses` says how.
        self._settle_statuses(findings, processed_entities | folded_keys)

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

            # 1. A DicomItem: an Instance, or an item inside a sequence.
            # `_replace_on_item` decides what is written and whether the
            # target is still there to write it to: zero items for EMPTY
            # on a sequence, zero-length bytes for EMPTY on a binary VR,
            # and a decline -- the one REMOVE_TAG already records -- for
            # a target the item no longer holds, rather than an element
            # the graph did not have written into it (#547). `details`
            # None is a decline, or the end state already there (#567):
            # `_satisfied` stamps that. One call rather than the cases
            # inline, so the pinned `mark_modified()` lines do not move.
            if hasattr(entity, "set_attr"):
                details, declined = self._replace_on_item(entity, finding)
                if declined:
                    self._record_decline(finding, declined, audit_buffer)
                if details is None:
                    return self._satisfied(finding, declined)
                action_type = "REMEDIATION_REPLACE"

            # 2. A Python attribute (`Patient.patient_name`, `Study.study_date`).
            elif self._replace_attr_refused(entity, proposal) is None:
                setattr(entity, proposal.target_attr, proposal.new_value)
                if hasattr(entity, "mark_modified"):
                    entity.mark_modified()
                details = f"Remediated {
                    finding.entity_uid}: {
                    proposal.target_attr} -> {
                    proposal.new_value}"
                action_type = "REMEDIATION_REPLACE"

            else:
                # `hasattr` was the test above until #625, and it is True
                # of a slot holding None: a Study Date the caller cleared
                # came back as the rule's date, filed as a success. The
                # helper answers "no such attribute" and "cleared" alike,
                # and is asked again here rather than bound above the
                # arm, because a line there moves the pin at 291 (#310).
                reason = self._replace_attr_refused(entity, proposal)
                self.logger.warning(
                    f"Remediation declined for {self._log_subject(finding)}: {reason}")
                self._record_decline(finding, reason, audit_buffer)
                return False

        elif proposal.action_type == "SHIFT_DATE":
            # Deterministic Date Shifting
            patient_id = self._resolve_patient_id(entity, proposal)
            if not patient_id:
                self.logger.warning(f"Could not resolve PatientID for "
                                    f"{self._log_subject(finding)}. Skipping date shift.")
                self._record_decline(
                    finding, f"could not resolve a PatientID to seed the jitter for "
                    f"{proposal.target_attr}, so the date is unshifted", audit_buffer)
                return False

            # The scheme the scan recorded for this patient; a finding
            # built by hand carries none and is keyed, as every patient
            # this release creates is.
            shift_days = self._get_date_shift(
                patient_id,
                (proposal.metadata or {}).get("jitter_scheme",
                                              JITTER_SCHEME_KEYED))
            new_date = self._shift_date_string(proposal.original_value, shift_days)
            moved = self._shift_target_moved(entity, finding, new_date)
            if moved:
                self._record_decline(finding, moved, audit_buffer)
                return False

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
                    self._record_what_is_left(entity, proposal.target_attr, None)
                    del entity.attributes[proposal.target_attr]
                    # `attributes` is a plain dict, so `del` bumps no revision,
                    # unlike `set_attr`. Without this an already-saved instance
                    # reported no unsaved changes after its PHI was stripped,
                    # the next save skipped it, and the identifier stayed.
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
            # The first of the three is the end state REMOVE asks for,
            # already there: satisfied, as an EMPTY on a sequence at
            # zero items is (#567), not a decline. As a decline,
            # `anonymize(report)` handed one report twice wrote a
            # `matched no applicable arm` row for every removal the
            # first call made -- 196 of 196 on CT_small and MR_small
            # under the floor, measured on a67eb30 -- demoted every
            # instance to IDENTIFIED and graded a clean graph
            # REVIEW_REQUIRED (#626). The other two still decline.
            # `_remove_is_satisfied` says what "gone" means: a
            # well-formed `gggg,eeee` tag absent under its canonical key.
            # Any other spelling -- `00080080`, `InstitutionName` -- still
            # declines, because its absence says nothing about the value.
            #
            # One `else` here rather than an `else` nested in the
            # `attributes` arm: nested, it would cover only the first of
            # the three, and it would stop covering a fourth if one were
            # ever added above.
            #
            # Absence is read on the object the session holds at the
            # finding's address, not on `entity` (review of #639): a
            # report kept across a reopen points at objects its first
            # pass cleaned, and read there every removal of an unsaved
            # pass was satisfied while the live graph still held it.
            # Since #644 the session hands over findings already bound to
            # that object, so on the session path the two agree, and they
            # differ only for an entity filed at another address.
            subject = self._removal_subject(finding, entity)
            if self._remove_is_satisfied(subject, proposal):
                self.logger.info(
                    f"{proposal.target_attr} is not on "
                    f"{self._log_subject(finding)}; nothing to remove")
                return self._satisfied(finding, None)
            # Named apart only where the address changed the answer: the
            # entity reads the tag gone, and the object at its address
            # does not, or is not there. Every other decline keeps its text.
            if subject is not entity and self._remove_is_satisfied(entity, proposal):
                reason = (f"{proposal.action_type} on {proposal.target_attr}: "
                          f"the finding's {type(entity).__name__} is "
                          f"{self._STALE_REMOVAL} its address, so the tag's "
                          f"absence from it is no evidence the element is gone")
            else:
                reason = (f"{proposal.action_type} on {proposal.target_attr} "
                          f"matched no applicable arm for "
                          f"{type(entity).__name__}")
            self._record_decline(finding, reason, audit_buffer)
            return False

    #: The VRs whose value is bytes, so whose empty value is `b""`.
    #: `UN` included: pydicom accepts a str there without a word, but the
    #: element reads back as `b""` all the same, and the scan has to meet
    #: one spelling of empty. "OB or OW" is how the dictionary names the
    #: overlay and pixel-data tags that may be either.
    _BINARY_VRS = frozenset({"OB", "OD", "OF", "OL", "OV", "OW", "UN",
                             "OB or OW"})

    def _replace_on_item(self, entity, finding: PhiFinding
                         ) -> Tuple[Optional[str], Optional[str]]:
        """`REPLACE_TAG` on a `DicomItem`: `(details, decline_reason)`.

        `details` is the audit row's text when the item was changed and
        None when nothing was written; `decline_reason` is set when that
        was a decline. The caller writes both rows, so the action type
        and `audit_buffer` stay where `tests/test_frozen_surface.py`'s Pin
        A reads them.

        **The contract on `(None, None)`: it means the item already holds
        what the rule asks**, and the caller stamps the entity REMEDIATED
        on the strength of it (#567). Any other path that writes nothing
        must return a reason, or it reads as remediated with the value
        still there.

        Three things the arm used to get wrong, all reached by default
        once #547 made the basic profile the whole of Table E.1-1:

        - **A target the item no longer holds declines.** The audit saw
          the tag and something removed it before the remediation ran.
          `REMOVE_TAG` has always declined there (its arms match nothing
          and the bottom `else` records it); this arm called `set_attr`
          regardless, which put back an element the graph no longer had
          -- #57's decoy, at the top level -- and filed
          `REMEDIATION_REPLACE` for it. Four basic rules were `EMPTY`
          while the profile was 35; since #547, 154 of 620 are.
          The same for a sequence that vanished: the old sequence branch
          was gated on the tag being in `sequences`, so it fell through
          to `set_attr` and wrote `""` under the SQ key, which the
          exporter wrote as a zero-item SQ. A value other than `""` aimed
          at a sequence declines too: there is no value to write into
          one, and the scan never proposes it (it warns instead).
        - **EMPTY on a sequence clears its items, and only says so when
          it did.** `clear_sequence_items` returns False for a sequence
          already at zero items; that is what the rule asks for, so no
          decline row and no success row either -- `(None, None)`, which
          the caller stamps REMEDIATED with no row (#567).
        - **EMPTY on a binary VR writes `b""`.** The str `""` in an OB
          element made pydicom warn in the export worker, and the file
          read back as `b""`, which the scan's `val != ""` then raised
          again: the floor did not converge on its own output. The VR
          decides -- the item's recorded one for a private tag, the
          dictionary's otherwise -- and the value's type only when
          neither knows. The VR first because the value's type is not
          reliable: a binary slot can already hold a str (a `""` written
          before this fix reloads as one).
        """
        # Local: the dictionary is wanted only on this arm, and `entities`
        # is imported at module scope for other names already.
        from pydicom.datadict import dictionary_VR  # pylint: disable=import-outside-toplevel
        from .entities import _canonical_tag  # pylint: disable=import-outside-toplevel

        proposal = finding.remediation_proposal
        tag = _canonical_tag(proposal.target_attr)
        attributes = getattr(entity, "attributes", None)
        sequences = getattr(entity, "sequences", None) or {}

        if tag in sequences and proposal.new_value == "":
            if not entity.clear_sequence_items(tag):
                self.logger.info(
                    f"Sequence {tag} on {self._log_subject(finding)} is "
                    "already empty; nothing to do")
                return None, None
            return (f"Emptied Sequence {proposal.target_attr} on "
                    f"{finding.entity_uid}"), None

        if isinstance(attributes, dict) and tag not in attributes:
            if tag in sequences:
                reason = (f"{tag} is a sequence on the "
                          f"{type(entity).__name__}, and "
                          f"{proposal.new_value!r} cannot be written to one")
            else:
                reason = (f"{tag} is no longer on the "
                          f"{type(entity).__name__}; writing "
                          f"{proposal.new_value!r} would create an element "
                          "the graph did not hold")
            self.logger.warning(
                f"Remediation declined for {self._log_subject(finding)}: "
                f"{tag} is not there to replace")
            return None, reason

        value = proposal.new_value
        if value != "":
            # A value the tag's VR cannot hold declines (#560). The loader
            # refuses such a rule, so this is reached by a finding built by
            # hand: an OB given `ANONYMIZED` failed the export with
            # `TypeError`, and a DA exported the literal. The dictionary VR
            # of a standard tag only, never the recorded one: a private
            # value its VR cannot hold is written as LO, and declining
            # there would keep the identifier. A decline, `(None, reason)`,
            # never `(None, None)`, which says the rule is already met.
            from .config_manager import _dictionary_vr_refuses  # pylint: disable=import-outside-toplevel
            refused_vr = _dictionary_vr_refuses(tag, value)
            if refused_vr is not None:
                reason = (f"{tag} is {refused_vr}, which cannot hold "
                          f"{value!r}; the value is left unchanged (#560)")
                self.logger.warning(
                    f"Remediation declined for {self._log_subject(finding)}: "
                    f"{reason}")
                return None, reason
        if value == "":
            vr = (getattr(entity, "attribute_vrs", None) or {}).get(tag)
            if vr is None:
                try:
                    vr = dictionary_VR(int(tag.replace(",", ""), 16))
                except (KeyError, ValueError, AttributeError):
                    vr = None
            if vr in self._BINARY_VRS or (
                    vr is None and isinstance(
                        (attributes or {}).get(tag), (bytes, bytearray))):
                value = b""
        self._record_what_is_left(entity, proposal.target_attr, value)
        entity.set_attr(proposal.target_attr, value)
        return (f"Remediated {finding.entity_uid} (Tag {proposal.target_attr}) "
                f"-> {proposal.new_value}"), None

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

    @staticmethod
    def _record_what_is_left(item, tag: str, value) -> None:
        """Record on `item` what the write about to run leaves at `tag`
        (`value` None: a removal), for `lock_identities()` (#537).

        Called as the statement **immediately before** each write, never
        after: a background `save()` between the two would otherwise
        store the value without its record, and the next lock would stash
        a replacement as the original. `Instance` alone records; a nested
        item has no slot, and the lock reads top-level values only, so a
        write inside a sequence records nothing on the instance holding
        it. Not a wrapper around `_apply_single_remediation`: that would
        hand `audit_buffer` to a callee Pin A does not list
        (`tests/test_frozen_surface.py`).
        """
        if hasattr(item, "record_remediation"):
            item.record_remediation(tag, value)

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

    def _raised(self, finding: PhiFinding, error: Exception) -> str:
        """Log a remediation that raised, and return its decline reason (#553).

        The `except` arm in `apply_remediation` used to log this line and
        nothing else: no row and no demotion. So a proposal that raised
        left its entity REMEDIATED -- stamped by a sibling's success --
        with the value still in the graph, and the run graded PASS. A
        raise is a decline: the value was not removed, and may be partly
        written, which the reason says.

        Flattened and pipe-escaped: the reason lands in `details`, which
        the report's section 3.3 renders into a markdown table cell, and
        an exception's text is under nobody's control. The log line keeps
        `_log_subject`, so a patient finding's Patient ID stays out of it.
        """
        self.logger.error(
            f"Failed to apply remediation for {self._log_subject(finding)} "
            f"({finding.field_name}): {describe_exception(error)}")
        proposal = finding.remediation_proposal
        reason = (f"{proposal.action_type} on {proposal.target_attr} raised "
                  f"{describe_exception(error)}; the value may be unchanged "
                  "or partly written")
        return " ".join(reason.split()).replace("|", "\\|")

    def _satisfied(self, finding: PhiFinding, declined) -> bool:
        """A proposal whose end state the item already holds (#567, #626).

        Two callers, one case each: `_replace_on_item` returning `(None,
        None)`, an `EMPTY` on a sequence already at zero items; and the
        bottom `else` of `_apply_single_remediation`, a `REMOVE_TAG`
        whose tag is already gone from the item (`_remove_is_satisfied`).
        Nothing was written,
        so there is no row and nothing is counted as applied -- and
        nothing is left to remove either, so the entity (and the instance
        holding a nested item) is stamped REMEDIATED, and the key is
        counted as handled for the scan tally. It used to be left at
        whatever status the audit gave it: IDENTIFIED, over an instance
        with nothing in it, beside a PASS grade.

        No `mark_modified()`: nothing changed. The stamp itself advances
        the revision when the status changes, which is right -- a status
        change is a change the store should hold.

        `_satisfied_keys` is rebound, never mutated: its class default is
        a `frozenset` shared by every service, and a direct
        `_apply_single_remediation` call never passes the reset in
        `apply_remediation`.

        Always returns False, the caller's value for "nothing written".
        """
        if not declined:
            finding.entity.record_phi_status(PhiStatus.REMEDIATED)
            owner = self._instance_owners.get(id(finding.entity))
            if owner is not None:
                owner.record_phi_status(PhiStatus.REMEDIATED)
            self._satisfied_keys = (self._satisfied_keys
                                    | {_remediation_key(finding)})
        return False

    def _shift_target_moved(self, entity, finding: PhiFinding,
                            new_date) -> Optional[str]:
        """Why a `SHIFT_DATE` must not write, or None when it may (#569).

        The arm's output is a function of `proposal.original_value`, the
        value the scan read, and not of what the target holds now. Written
        without this check, a date deleted between `audit()` and
        `anonymize()` came back shifted, a blanked or edited one was
        overwritten with a shift of the value it no longer held, and each
        was filed `REMEDIATION_SHIFT_DATE` and stamped REMEDIATED.

        It may write when the target still holds the audited value, **or
        already holds `new_date`**: `anonymize(report)` handed the same
        report twice re-applies every shift onto its own output, and that
        call is idempotent and must stay so. Admitting only the audited
        value would decline every date on the second call and grade
        REVIEW_REQUIRED over a clean graph. Shifting the live value
        instead would move it twice.

        Anything else declines, absent included, rather than counting as
        satisfied (#567): nothing was shifted, and `_replace_on_item`
        declines a vanished target for the same reason (#547).

        A `DicomItem` is read at the canonical key, because `set_attr`
        writes there and a hand-built `target_attr` may be upper-case; a
        key holding None is gone, as the scan and the exporter read it. A
        `Study` is compared through `normalize_study_date`, the rule its
        `__setattr__` stores a date by, so `"2004-01-19"`, `"20040119"`
        and `date(2004, 1, 19)` are one date; the audited side is stripped
        first, as the arm's parser strips it, so a padded DA is that date
        too. The reasons name the tag and never a value: they are
        persisted in the decline row.

        An unparseable original (`new_date` None) on a target still
        holding it passes, so the arm's own invalid-format decline is the
        one row; on a target that is gone it declines here, as gone, since
        "the value is unchanged" would describe a value no longer there.

        **A blank original passes whatever the target holds**, to the
        arm's empty-date branch, which writes no row: there was no date to
        shift, so nothing can have been re-created or overwritten, and a
        decline would be `REVIEW_REQUIRED` over nothing -- the cry-wolf
        shape `test_an_empty_date_is_not_a_decline` pins, whose instance
        does not hold the tag at all. The scan never raises a blank.

        The warning is logged here so the arm stays within its line
        budget: every line above the success block counts toward the five
        `mark_modified()` pins (#310).

        **A seed minted under another secret declines (#644).** The
        offset is derived under this service's secret from the Patient ID
        the finding carries (`_resolve_patient_id`), and a report a store
        raised after its own pass carries that store's keyed pseudonym.
        Resolved against another store's graph, such a finding shifted
        the date by an offset of this secret over the other store's
        pseudonym: measured -184 days on CT_small where this store's own
        offset for the patient is -359, a second offset for one patient,
        derived from a value minted elsewhere. A keyed-shaped seed that
        does not verify under this secret is refused, as
        `_replace_attr_refused` refuses the ID itself; an original ID, a
        legacy unkeyed pseudonym and a service with no secret pass.
        """
        from .entities import _canonical_tag, normalize_study_date  # pylint: disable=import-outside-toplevel
        from .privacy import (  # pylint: disable=import-outside-toplevel
            _is_keyed_pseudonym_shape, _pseudonym_verifies)

        proposal = finding.remediation_proposal
        if proposal.original_value is None or not str(proposal.original_value).strip():
            return None
        seed = self._resolve_patient_id(entity, proposal)
        if (self.project_secret and _is_keyed_pseudonym_shape(seed)
                and not _pseudonym_verifies(seed, self.project_secret)):
            reason = (f"{proposal.target_attr}: the pseudonym its offset is seeded "
                      "on was not minted under this store's project secret, so "
                      "the date is not shifted")
            self.logger.warning(
                f"Date shift declined for {self._log_subject(finding)}: {reason}")
            return reason
        attr, admitted = proposal.target_attr, [proposal.original_value]
        if new_date is not None:
            admitted.append(new_date)
        if hasattr(entity, "set_attr"):
            attr = _canonical_tag(attr)
            present = entity.attributes.get(attr) is not None
            held = entity.attributes.get(attr)
        else:
            held = getattr(entity, attr, None)
            present = held is not None
            held = normalize_study_date(held)
            admitted = [normalize_study_date(str(value).strip()) for value in admitted]
        if not present:
            reason = (f"{attr} is no longer on the {type(entity).__name__}, "
                      "so there is no date to shift")
        elif held in admitted:
            return None
        else:
            reason = (f"{attr} changed after the finding was raised, so a "
                      "shift of the value it held then is not written over it")
        self.logger.warning(
            f"Date shift declined for {self._log_subject(finding)}: {reason}")
        return reason

    def _replace_attr_refused(self, entity, proposal) -> Optional[str]:
        """Why a `REPLACE_TAG` must not write a Python attribute, or None
        when it may (#625).

        The arm's test was `hasattr(entity, target_attr)`, which is True
        of a slots field holding None: `Study(uid, study_date=None)` has
        the attribute. So a `0008,0020: REPLACE` rule with a value, over
        a Study whose date the caller cleared between `audit()` and
        `anonymize()`, wrote the rule's date into it, filed
        `REMEDIATION_REPLACE ... written to 1 instance copy` and stamped
        REMEDIATED -- #569's re-creation on the attribute arm, where
        `_replace_on_item` already declines a target the item no longer
        holds (#547). Measured on a67eb30 for `Study.study_date`,
        `Study.study_time`, `Patient.patient_name`, `Patient.patient_id`
        and `Series.modality`: the arm is generic, so this is.

        A slot holding None refuses a value and takes `""`: the exporter
        writes None and `""` as the same zero-length element, so an
        EMPTY fabricates nothing, and the entity then holds `""` with a
        row that says so, as it did before this check. A present but
        empty value is not a cleared one and is written. A name the
        entity lacks refuses as it always did. The reasons name the
        attribute and the type and never a value: they are persisted in
        the row and rendered into the report.

        **A Patient ID pseudonym minted under another secret refuses
        (#644).** `Session.anonymize(findings)` resolves a report against
        the live graph, so a report raised in one store can act on another
        that holds the same files, and its `patient_id` proposals carry
        the first store's keyed pseudonyms. Measured before this check:
        store B exported store A's `ANON_...` IDs and graded PASS, and
        B's next `audit()` does not re-propose an ID already shaped
        `ANON_`, so the cross-project link the project secret exists to
        prevent (GHSA-phg9) was written and kept. A value shaped as a
        keyed pseudonym (`_is_keyed_pseudonym_shape`, never
        `_is_replacement_id`, which also accepts the shorter legacy shape
        that `_pseudonym_verifies` can never verify) that does not verify
        under this service's secret is refused, whatever the entity. A
        legacy unkeyed pseudonym is not keyed-shaped and passes; so does
        anything with no secret to check against. Refused in the safe
        direction too: a replacement value that merely has the keyed
        shape and does not verify is not written, whatever produced it.

        Pure, with no `audit_buffer`: Pin A in
        `tests/test_frozen_surface.py` refuses a new callee that takes
        one. Called twice from the arm, once as the condition and once
        for the reason, because binding the answer above the arm is a
        line above the pinned `mark_modified()` at 291 (#310). A method
        rather than static since #644, for the secret; both call sites
        already spelt `self._replace_attr_refused(...)`, so no line above
        the pins moved.
        """
        from .privacy import (  # pylint: disable=import-outside-toplevel
            _is_keyed_pseudonym_shape, _pseudonym_verifies)

        attr = proposal.target_attr
        if not hasattr(entity, attr):
            return f"{type(entity).__name__} has no attribute or setter for {attr}"
        if (attr == "patient_id" and self.project_secret
                and _is_keyed_pseudonym_shape(proposal.new_value)
                and not _pseudonym_verifies(proposal.new_value, self.project_secret)):
            return (f"{attr}: the pseudonym was not minted under this store's "
                    "project secret, so it is not written")
        if getattr(entity, attr) is None and proposal.new_value not in (None, ""):
            return (f"{attr} is no longer set on the {type(entity).__name__}, "
                    "so the rule's value is not written where the caller "
                    "cleared one")
        return None

    @staticmethod
    def _remove_is_satisfied(entity, proposal) -> bool:
        """Whether a `REMOVE_TAG` that matched no arm is one whose target
        is already gone from a `DicomItem` (#626).

        True only when all three hold: the entity has an `attributes`
        dict; `target_attr` lower-cased is a well-formed `gggg,eeee` tag
        (`config_manager._is_tag_key`, the check a config's tag keys
        already pass); and neither `attributes` nor `sequences` holds
        that canonical key. Anything else declines, as it did.

        Read canonically because the REMOVE arms above test the raw
        `target_attr`: a hand-built upper-case tag the item holds
        lower-case fell past them, and read raw here it would count as
        satisfied over a value still there.

        Well-formed because absence under a key is evidence only for the
        key the graph would store the element under. `00080080`,
        `(0008,0080)`, `0008, 0080`, ` 0008,0080`, `InstitutionName`, and
        `patient_id` against an item holding `0010,0020` all lower-case
        onto keys no item has, so they read as absent over a value still
        there: in review of #626 (c6d0112) each was stamped REMEDIATED
        with no row, and through a session the run graded PASS with the
        value in the exported file. Only a well-formed tag is satisfied;
        a malformed or non-tag key reaches the decline, whether or not
        the element it seems to name is held -- the arm cannot tell.

        And only on the object the finding addresses. Under a session the
        caller passes `_removal_subject`'s answer, the live object at the
        finding's `entity_uid` and `entity_path`, not `finding.entity`, and
        None when the address cannot be read as done, which has no
        `attributes` and so declines: a report kept across a reopen, or a hand-built finding
        filed under another instance's UID, read absence on an object
        export never writes (review of #639 r2).

        An entity with no `attributes` dict, and an action this method
        does not implement, are the other two ways to the bottom `else`,
        and both stay declines.
        """
        # pylint: disable=import-outside-toplevel
        from .config_manager import _is_tag_key
        from .entities import _canonical_tag

        if proposal.action_type != "REMOVE_TAG":
            return False
        attributes = getattr(entity, "attributes", None)
        if not isinstance(attributes, dict):
            return False
        tag = _canonical_tag(proposal.target_attr)
        if not (isinstance(tag, str) and _is_tag_key(tag)):
            return False
        sequences = getattr(entity, "sequences", None) or {}
        return tag not in attributes and tag not in sequences

    def _settle_statuses(self, findings: list, handled: set) -> None:
        """Demote every entity a pass left REMEDIATED over something it did
        not remove (#486, #553).

        Two sources, one demotion:

        - **A decline** -- including a proposal that raised -- names its
          entity in `_declined_entities`. The success block stamps
          REMEDIATED per proposal and cannot know what a later proposal on
          the same entity will do; the pass can.
        - **The scan tally**, when `audit()` built one: every uid this
          pass's findings name is settled against the keys the pass
          handled (applied, folded, already satisfied, or inside a
          sequence a pass removed, #644). An incomplete
          uid demotes every entity the pass's findings under it resolve
          to, and the instance holding a nested one. Keyed on the
          scan-time `entity_uid` strings, not live entities: a patient's
          `patient_id` changes during the pass.

        **The entity's own UID is settled too.** A finding names its uid,
        and a hand-built one can name the wrong one or none: asked only
        about that name, the tally had no opinion, and the success stamped
        the instance REMEDIATED over everything the audit raised under
        its real UID that the pass was never handed. So each resolved
        finding's live UID (`_live_uid`) is settled beside the name, with
        the keys handled under it. For a scan finding the two are the
        same string and it is settled once. A UID the audit did not raise
        under -- one `redact()` regenerated since -- gets no opinion, as
        before. A patient is settled under the `patient_id` it held when
        the pass began, snapshotted at the tally handover: the pass may
        replace it, so reading it at the settle would give the pseudonym
        for a pass that handled the ID, and the original only for one
        that did not.

        Only an entity that ends the pass REMEDIATED is touched: one that
        only declined keeps whatever status it had, which
        `test_a_pass_that_only_declined_leaves_the_status_alone` pins.
        `getattr`, because a hand-built entity need carry no status.
        """
        by_uid = {}
        for key in handled | self._satisfied_keys | self._gone_keys:
            by_uid.setdefault(key[0], set()).add(key)
        demote = list(self._declined_entities)
        if self._scan_tally is not None:
            live = {id(f): self._live_uid(f.entity) for f in findings
                    if f.remediation_proposal and f.entity is not None}
            incomplete = {
                uid for uid in ({f.entity_uid for f in findings
                                 if f.remediation_proposal}
                                | set(live.values()))
                if self._scan_tally.settle(uid, by_uid.get(uid, ())) is False}
            for finding in findings:
                if finding.entity is not None and (
                        finding.entity_uid in incomplete
                        or live.get(id(finding)) in incomplete):
                    demote.append(finding.entity)
                    owner = self._instance_owners.get(id(finding.entity))
                    if owner is not None:
                        demote.append(owner)
        for entity in demote:
            if getattr(entity, "phi_status", None) is PhiStatus.REMEDIATED:
                entity.record_phi_status(PhiStatus.IDENTIFIED)

    def _live_uid(self, entity) -> Optional[str]:
        """The UID the scan files `entity`'s findings under.

        An instance's SOP Instance UID or a study's Study Instance UID,
        read now -- the uids `PhiInspector` writes into `entity_uid`. A
        patient's is its `patient_id` as the pass began
        (`_pass_start_ids`), because the pass may already have replaced
        it with the pseudonym; None for a patient the snapshot does not
        hold. None for a nested item. The item's owner is not consulted
        because it could add nothing: `Session._nested_finding_owners`
        finds an owner *through* the finding's `entity_uid`, so an item
        has one only when that name is already its instance's UID, and a
        mis-named nested finding stamps the item alone, never the
        instance.
        """
        if isinstance(entity, Instance):
            return entity.sop_instance_uid
        if isinstance(entity, Study):
            return entity.study_instance_uid
        if isinstance(entity, Patient):
            return self._pass_start_ids.get(id(entity))
        return None

    #: The `Patient`/`Study` fields the exporter stamps onto every exported
    #: instance from the entity, with the tag each is the value of
    #: (`io_handlers.export_stamp_attributes`, #570). Exactly these
    #: four: that helper also reads `birth_date`, `sex` and
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
        # entity (`export_stamp_attributes`) and the rule of this table is
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
    #: instances sharing a UID. Read-only: it is a class attribute, so an
    #: in-place write would reach every service in the process; the
    #: setter replaces it. Here rather than in `__init__` for
    #: `ENTITY_FIELD_TAGS`' reason above: nothing is added above the five
    #: pinned lines (#310) -- which is also why `MappingProxyType` is
    #: imported here and not with the module's imports.
    #: No reset in `apply_remediation` for the same reason, and none is
    #: needed: `Session.anonymize()` builds a fresh service per call.
    from types import MappingProxyType as _MappingProxyType
    _instance_owners = _MappingProxyType({})

    def _use_instance_owners(self, owners) -> None:
        """Name the instance that holds each nested finding's item (#494).

        A nested success then marks that instance modified and stamps it
        REMEDIATED, and a nested decline names it for the pass-end
        demotion, exactly as a top-level finding on the instance would.

        What this does not do: decide that the pass is the whole story. A
        partial list, and a proposal that raised, are settled at the pass
        end against the scan tally and the declines (#553), for nested
        and top-level findings alike; this only says which instance a
        nested entity belongs to.
        """
        self._instance_owners = self._MappingProxyType(dict(owners))

    #: `id(finding) -> the object the session holds at its address` for
    #: the `REMOVE_TAG` findings of this pass, None where the address
    #: cannot be read as done
    #: (`Session._removal_targets`, review of #639). **None as the whole
    #: map means no session**, not "resolved nothing": a service used
    #: without one -- the direct tests, hand-built findings -- has no graph
    #: to resolve an address in and reads `finding.entity`, as it always
    #: did. A class attribute for `_instance_owners`' reason.
    _removal_objects = None
    #: The decline text for a removal read on an object other than the
    #: finding's entity. Carries the uid only, never a value.
    _STALE_REMOVAL = "not the object this session holds at"

    def _use_removal_targets(self, targets) -> None:
        """Name the live object each `REMOVE_TAG` finding addresses.

        A removal is then satisfied only when that object lacks the tag,
        and one whose address resolved to nothing is never satisfied: a
        report kept across `close()` and a reopen, or a hand-built finding
        whose `entity` is not the object at its `entity_uid` and
        `entity_path`, reads as a decline instead of as done.
        """
        self._removal_objects = self._MappingProxyType(dict(targets))

    #: Keys of the findings `Session._live_findings` did not hand over
    #: because a pass already removed or emptied the sequence their REPLACE
    #: or SHIFT lived in (#644): nothing is at the address to write, and
    #: no value reaches the export. Counted as handled by the scan tally,
    #: as a satisfied proposal is -- without it, an instance whose
    #: container an earlier partial pass emptied was demoted over nothing.
    #: Rebound, never mutated, so the class default is safe to share.
    _gone_keys = frozenset()

    def _use_gone_keys(self, keys) -> None:
        """Count `keys` as handled when this pass settles its statuses."""
        self._gone_keys = frozenset(keys)

    def _removal_subject(self, finding: PhiFinding, entity):
        """What a removal's absence is read on: `entity` with no session,
        and the session's live object at the address otherwise."""
        if self._removal_objects is None:
            return entity
        return self._removal_objects.get(id(finding))

    #: What the session's last `audit()` raised, per scan-time entity uid
    #: (#553): a `_ScanTally`, or None when there is no audit behind the
    #: pass -- hand-built findings, a reopened session -- which keeps the
    #: pass's own accounting. Set by `_use_scan_tally`; a class attribute
    #: for `_instance_owners`' reason.
    _scan_tally = None
    #: Keys of the proposals whose end state the graph already held this
    #: pass (#567), counted as handled by the tally. Rebound, never
    #: mutated, so the class default is safe to share.
    _satisfied_keys = frozenset()
    #: `id(Patient) -> patient_id` as the pass began, for `_live_uid`.
    #: Set with the tally; read-only for `_instance_owners`' reason.
    _pass_start_ids = _MappingProxyType({})

    def _use_scan_tally(self, tally, findings=()) -> None:
        """Settle this service's passes against `tally` (#553).

        The session's own tally, not a copy: a partial pass leaves the
        keys it handled in it, so the next pass over the same audit
        completes what this one did not.

        `findings` are the pass's, and each patient they resolve to has
        its `patient_id` snapshotted here, before the pass can replace
        it, so a patient finding filed under a wrong uid or none is still
        settled under the patient's real one. Here and not at the top of
        `apply_remediation`, which would move the five pinned
        `mark_modified()` lines (#310). A patient whose ID an earlier pass
        already replaced is snapshotted as its pseudonym, which the tally
        does not hold, so a mis-named finding on it has no opinion.
        """
        self._scan_tally = tally
        self._pass_start_ids = self._MappingProxyType({
            id(f.entity): f.entity.patient_id for f in findings
            if isinstance(f.entity, Patient)})

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
        exporter reads too (`export_stamp_attributes`, both doors, #570),
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
                    self._record_what_is_left(instance, tag, value)
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
                    # Whether the write removed it: a REMOVE folds only
                    # into a removal, anything else only into a value.
                    removed = value is None
                    self._owner_copies[(id(instance), tag)] = removed
                    folds += self._pending_folds.get((id(instance), tag, removed), 0)
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

        A REMOVE folds only into a write that *removed* the copy, and a
        REPLACE only into one that wrote a value (`_owner_copies` records
        which). REMOVE was exempt until #537 made an owner's REMOVE
        reachable: the owner's removal took the copy away, the instance's
        own REMOVE then matched nothing and filed `REMEDIATION_DECLINED`,
        and a correct outcome graded REVIEW_REQUIRED. An owner that wrote
        a value does not absorb an instance REMOVE, which still runs and
        removes the copy; one rule feeds both levels on every scanned path,
        so that pairing only arises from a hand-built list.

        Three things never fold, each measured before this was written:

        - **A mismatch of the two** above.
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
        # No "is this an instance?" check either, for the same reason as
        # the nested case: a Patient, a Study or a None entity is never in
        # `_owner_copies`, which holds only the instances the owner wrote
        # -- so the lookup alone decides. `.get` is None for a copy no
        # owner reached, and `None is True/False` is False.
        removed = self._owner_copies.get((id(finding.entity), proposal.target_attr))
        return removed is (proposal.action_type == "REMOVE_TAG")

    @staticmethod
    def _foldable_instance_findings(findings: list) -> dict:
        """The instance findings that fold if an owner's write reaches
        their copy, counted per `(id(instance), tag, removed)` (#496).

        Counted before the pass so an owner's audit row can name its folds
        when it is appended: the row is complete from the start, and no
        row is rewritten after the fact, which Pin A in
        `tests/test_frozen_surface.py` refuses. `removed` is whether the
        owner's write removed the copy; a REMOVE folds only into a removal
        and anything else only into a value (see `_folds_into_owner`,
        #537). Only a copy an owner's write reaches is ever asked for its
        count, so a finding counted here whose copy no owner reaches costs
        nothing.

        **Counted per loop key (`_remediation_key`), because that is what
        the loop runs.** The loop takes each key at most once, and the key
        carries no action, so of several findings on one key only one can
        fold. Which one depends on the copy the owner left:

        - **A value.** The copy is present, so the key's *first* finding
          ends it: a non-REMOVE folds, and a REMOVE applies and removes the
          copy, after which the rest are duplicates. The value copy counts
          the key iff that first finding is not a REMOVE.
        - **A removal.** The copy is absent, so every non-REMOVE on it
          declines -- `_replace_on_item` for REPLACE (#547),
          `_shift_target_moved` for SHIFT (#569), the arm's bottom `else`
          for anything else -- and a decline claims no key. The key
          therefore reaches its first REMOVE, which folds. The removal
          copy counts the key iff any finding on it is a REMOVE.

        Counted per `(key, removed)` instead, until 0.9.8, a REMOVE and a
        REPLACE on one tag were two folds waiting, and an owner that wrote
        a value claimed the REPLACE the loop skipped as a duplicate of the
        REMOVE it ran (#576). The second bullet needs #569: a SHIFT that
        re-created the absent copy claimed the key, and its REMOVE never
        folded.

        A finding that raises claims no key. On a removed copy that is a
        decline, and is counted as one; on a value copy a REMOVE that
        raises leaves the key to a later non-REMOVE, which folds and is not
        counted, so the row under-claims by one (57400d1 counted it). That
        is not restored: before the pass a REMOVE that will raise cannot be
        told from one that will apply, and counting it is #576's
        over-claim. The copy is the first finding's, which is exact while
        the findings on a key name one entity; two entities sharing a UID
        can under-claim the same way, unchanged from 57400d1.
        """
        chains = {}
        for finding in findings:
            proposal = finding.remediation_proposal
            if not proposal or not hasattr(finding.entity, "set_attr"):
                continue
            remove = proposal.action_type == "REMOVE_TAG"
            # `[copy, first is a REMOVE, any is a REMOVE]`; the copy is the
            # first finding's (see the docstring's last paragraph).
            chain = chains.setdefault(_remediation_key(finding), [
                (id(finding.entity), proposal.target_attr), remove, False])
            chain[2] = chain[2] or remove
        pending = {}
        for (instance, tag), first_remove, any_remove in chains.values():
            for removed, counts in ((False, not first_remove), (True, any_remove)):
                if counts:
                    pending[(instance, tag, removed)] = pending.get(
                        (instance, tag, removed), 0) + 1
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

    #: The shapes `_shift_date_string` shifts (#559), matched whole and
    #: ASCII-only. A DA, or a DT at second precision (optionally with a
    #: fraction) whose first eight digits are its date; the non-standard
    #: dotted DT this parser always accepted; and the ISO date and
    #: date-time. Hour- and minute-precision DT are deliberately absent:
    #: see the docstring's "The accept set only narrows".
    _DA_OR_DT = (r"([0-9]{4})([0-9]{2})([0-9]{2})"
                 r"(?:([0-9]{2})([0-9]{2})([0-9]{2})(?:\.[0-9]{1,6})?)?")
    _DOTTED_DT = (r"([0-9]{4})([0-9]{2})([0-9]{2})"
                  r"\.([0-9]{2})([0-9]{2})([0-9]{2})(?:\.[0-9]+)?")
    _ISO = (r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})"
            r"(?:([ T])([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2}))?")

    @staticmethod
    def _shift_date_string(date_val, days: int) -> Optional[str]:
        """
        Shifts a date by `days`, or returns None when `date_val` is not a
        date this can shift -- which the `SHIFT_DATE` arm records as a
        decline, and `_date_shift_declines` reads as "would decline".

        A `date` or `datetime` is shifted as itself. A string is read by
        shape, and only these shapes shift:

        - `YYYYMMDD` (DA), and a DT that begins with one at second
          precision -- `...HHMMSS`, `...HHMMSS.F` to six fraction digits.
          The date moves; everything after it is re-attached exactly as
          written.
        - The dotted DT `YYYYMMDD.HHMMSS[.F...]` this parser has always
          accepted, the same way.
        - ISO `YYYY-MM-DD`, optionally with ` HH:MM:SS` or `THH:MM:SS`,
          rendered zero-padded as before.

        The time part must be a clock time (hour < 24, minute and second
        < 60), and a shift that leaves years 1-9999 declines rather than
        raising `OverflowError` into the pass.

        **Why not `strptime` (#559).** This was a loop over strptime
        formats, and `%Y%m%d` is not length-strict: it reads `072731`, a
        Study Time, as the year 0727, so a JITTER rule on a TM wrote
        `07270219`, a DA-shaped value, into the TM. A six-digit date
        `230515` became `23041226`; an hour-precision DateTime
        `2023051510` matched `%Y%m%d%H%M%S` as `2023 05 1 5 10` and came
        back `20230421050100`, date and time both wrong. Each branch also
        re-rendered with `strftime`, which turned a fraction of `.1` into
        `.100000`, and a year below 1000 into three digits on Linux.

        **The accept set only narrows.** Every value this shifted before
        and shifted correctly is still shifted, to the same result but for
        the two fraction spellings above; only the fabricating shapes
        (TM-shaped, six- and seven-digit dates, a dotted time that is not
        six digits, and hour- and minute-precision DT) now decline.
        Widening it is not safe: `_date_shift_declines` answers "would
        this shift" for the legacy scan branch, which skips what would
        shift as already shifted, so a wider parser silently skips PHI on
        a pre-0.9.6 store. That is why a 10- or 12-digit DT declines
        rather than shifting correctly: the old loop declined
        `2023060510`, a legacy instance can still hold it unshifted, and
        shifting it here graded that instance CLEARED with the value
        retained (review of #574; the misread ones, `2023051510`, it
        accepted, and they now decline visibly instead).

        Args:
            date_val (Union[str, date, datetime]): The original date value.
            days (int): Delta in days.

        Returns:
            Optional[str]: The shifted value (a `date`/`datetime` for one),
                or None when the value is declined.
        """
        # Local: the patterns are wanted on this path only, and `re`
        # caches their compiled forms.
        import re  # pylint: disable=import-outside-toplevel

        if hasattr(date_val, 'strftime'):
            return date_val + timedelta(days=days)
        if date_val is None:
            return None
        text = str(date_val).strip()

        def moved(year, month, day):
            try:
                return datetime(int(year), int(month), int(day)) + timedelta(days=days)
            except (ValueError, OverflowError):
                return None

        def is_clock_time(hour, minute, second):
            return all(part is None or int(part) < limit
                       for part, limit in ((hour, 24), (minute, 60), (second, 60)))

        for pattern in (RemediationService._DA_OR_DT, RemediationService._DOTTED_DT):
            match = re.fullmatch(pattern, text)
            if match is None:
                continue
            shifted = moved(*match.group(1, 2, 3))
            if shifted is None or not is_clock_time(*match.group(4, 5, 6)):
                return None
            return (f"{shifted.year:04d}{shifted.month:02d}{shifted.day:02d}"
                    f"{text[8:]}")

        match = re.fullmatch(RemediationService._ISO, text)
        if match is None:
            return None
        shifted = moved(*match.group(1, 2, 3))
        if shifted is None or not is_clock_time(*match.group(5, 6, 7)):
            return None
        rendered = f"{shifted.year:04d}-{shifted.month:02d}-{shifted.day:02d}"
        if match.group(4):
            hour, minute, second = (int(part) for part in match.group(5, 6, 7))
            rendered += f"{match.group(4)}{hour:02d}:{minute:02d}:{second:02d}"
        return rendered

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
    as `'notadate'`. Since #559 the parser is length-strict, so a
    TM-shaped value (`072731`) or a six-digit date, which it used to
    misread as a date, now answers True here too: the legacy branch
    re-raises it where it used to skip it, which is right, because the
    arm never could shift it. Blank is False because the arm skips a blank value
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


def _remediation_key(finding: PhiFinding) -> tuple:
    """What `apply_remediation` dedupes on, and what the scan tally counts.

    `(entity_uid, entity_path, target_attr)`: the attribute the proposal
    writes, and where it lives -- see the comment at the dedup for why
    each half is there. One spelling for both readers, so "already
    handled" and "what the audit raised" cannot drift apart (#553).
    """
    return (finding.entity_uid, finding.entity_path,
            finding.remediation_proposal.target_attr)


#: The width of the scan tally's hash-sum.
_TALLY_MASK = (1 << 64) - 1


def _key_digest(keys) -> int:
    """The 64-bit sum of `hash(key)` over a set of remediation keys."""
    return sum(hash(key) & _TALLY_MASK for key in keys) & _TALLY_MASK


class _ScanTally:
    """What the most recent `audit()` raised, per scan-time `entity_uid` (#553).

    `anonymize(findings=...)` applies what it is handed, and each success
    stamps its entity REMEDIATED. Without this, one of an instance's 202
    findings handed alone read REMEDIATED over the other 201, and the
    manifest's `anonymized` -- documented as "left no identifier
    unremediated" -- read that status. The tally is how a pass knows what
    else the audit raised against an entity it touched.

    **Two ints per entity**: how many distinct remediation keys the audit
    raised under the uid, and their 64-bit hash-sum. Not the keys: a CT
    instance raises about two hundred, and holding them for every
    instance of a large cohort for the life of the session is memory the
    ordinary path, which handles every key, never reads.

    `settle(uid, handled)` is asked once per pass for each uid the pass's
    findings name, with the keys the pass handled under it (applied,
    folded into an owner's write, or already satisfied):

    - **None**: the audit raised nothing under that uid. No opinion; the
      pass accounts for itself, as a pass with no audit behind it does.
      Asked about a finding's named uid *and* its entity's own UID
      (`RemediationService._settle_statuses`), so a wrong or missing name
      reaches None only when that own UID is not one the audit raised
      under either: an instance `redact()` gave a new UID since, a
      patient whose ID an earlier pass already replaced, or a nested
      item.
    - **True**: the handled keys, merged with those earlier passes since
      the same audit handled, are exactly the raised set. The uid is dropped,
      so a later pass over it has no opinion either.
    - **False**: they are not. The merged set is kept in `_partial` until
      a later pass completes it, so two complementary partial passes end
      complete and the same partial list handed twice does not: the
      comparison is between sets, never a running count.

    **What is compared.** The merged set's size and its hash-sum against
    the raised pair. A merged set *larger* than the raised count holds a
    key the audit did not raise under that uid and is incomplete:
    fail-closed, such a key can demote an entity and never complete one.
    Such a key is a hand-built finding, **or a finding from an earlier
    audit**: the tally is the most recent `audit()`'s only, so a report
    from before it, or from an audit under another config, carries keys
    it does not hold, and an instance with nothing left on it reads
    IDENTIFIED until the next `audit()`. Count and hash-sum cannot test
    containment without holding the keys; whether a superset should
    complete is #582, and
    `test_an_earlier_report_is_settled_against_the_latest_audit` pins
    today's answer. The hash-sum only stops such a key
    from making up the count in place of a raised key that was not
    handled; two distinct sets of equal size colliding is about 2^-64.
    It is not a secret and not an integrity check. `hash()` of a str is
    salted per process, which is safe here because the tally is built
    and settled in the parent process of one session and never crosses a
    boundary; it is not persisted, so a reopened session has no tally and
    keeps pass accounting.

    **`_partial`'s cost.** Nothing on a full pass. A deliberately partial
    workflow -- a patient-level pass now, the instances later -- keeps
    the handled key set of every uid it left incomplete, about 77 B per
    handled key (measured on CT_small when #553 was designed): about two
    keys per patient for a patient-only pass, one per instance for a
    one-finding-per-instance list. Freed when
    the uid completes, or by the next `audit()`, which replaces the tally.
    """

    def __init__(self, findings):
        raised = {}
        for finding in findings:
            if finding.remediation_proposal is None or finding.entity_uid is None:
                continue
            raised.setdefault(finding.entity_uid, set()).add(
                _remediation_key(finding))
        self._raised = {uid: (len(keys), _key_digest(keys))
                        for uid, keys in raised.items()}
        self._partial = {}

    def settle(self, uid, handled) -> Optional[bool]:
        """Whether the keys handled under `uid` complete what was raised."""
        raised = self._raised.get(uid)
        if raised is None:
            return None
        merged = self._partial.get(uid, frozenset()) | frozenset(handled)
        if (len(merged), _key_digest(merged)) == raised:
            del self._raised[uid]
            self._partial.pop(uid, None)
            return True
        self._partial[uid] = merged
        return False
