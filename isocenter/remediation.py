"""Apply the remediation proposals an audit raised.

`RemediationService` writes each `PhiFinding`'s proposal (replace, shift
a date, remove) onto the graph in memory, records an audit row per
remediation applied or declined, and settles each entity's PHI status
at the end of the pass. Nothing reaches disk here: the store holds the
result after the next save, and the files after `export()`.
"""
from typing import List, Optional, Tuple
from datetime import datetime, timedelta
from .parallel import progress_bar
from .entities import JITTER_SCHEME_KEYED, Instance, Patient, PhiStatus, Series, Study
from .privacy import PhiFinding, PhiRemediation, canonical_patient_key
from .logger import describe_exception, get_logger

#: Every action type `_apply_single_remediation` emits, spelled once for
#: the report's evidence check: a session that anonymized must find at
#: least one of these in its audit summary to grade PASS. A spelling
#: missing here grades a clean run REVIEW_REQUIRED; the frozen-surface
#: tests hold this set equal to the words the arms write, and read it by
#: AST, so keep it a literal `frozenset({...})` of strings.
REMEDIATION_ACTION_TYPES = frozenset({
    "REMEDIATION_REPLACE",
    "REMEDIATION_SHIFT_DATE",
    "REMEDIATION_REMOVE",
})

#: A remediation that was proposed and did not run, so the value it
#: targeted is still in the graph and will reach the exported file.
#: One spelling for every declining path, not one per reason:
#: `audit_summary` counts action types, and a second spelling would split
#: one behaviour across two rows of the report's section 2. The reason
#: lives in the row's `details`.
#:
#: **Deliberately not in `REMEDIATION_ACTION_TYPES`.** That frozenset is
#: the ANONYMIZE evidence set `generate_report` checks; if a decline
#: counted as evidence, a run in which every remediation declined would
#: satisfy the check that exists to catch a run whose remediation rows
#: went missing.
REMEDIATION_DECLINED = "REMEDIATION_DECLINED"


def _count(number: int, singular: str, plural: str = None) -> str:
    """A number and its noun, agreeing: `"1 instance copy"`, `"2 instance
    copies"`.

    Used for every count in a `REMEDIATION_*` row or its log line.

    Args:
        number (int): The count.
        singular (str): The noun for a count of one.
        plural (str, optional): The noun for any other count. Defaults to
            `singular + "s"`; pass it for a noun that does not take `s`.

    Returns:
        str: `"<number> <noun>"`.
    """
    return f"{number} {singular if number == 1 else (plural or singular + 's')}"


class RemediationService:
    """Applies the remediation proposals the `PhiInspector` raised.

    Replaces, removes and date-shifts values on the graph in memory, and
    writes an audit row for each remediation applied or declined.
    `Session.anonymize()` builds one per pass and configures it through
    the private `_use_*` methods; a service used without them (hand-built
    findings) skips the checks that need the live graph.
    """

    def __init__(self, store_backend=None, date_jitter_config: Optional[dict] = None,
                 project_secret: Optional[bytes] = None):
        """Initialize the remediation service.

        Args:
            store_backend (optional): The store the audit rows are written
                to (`log_audit`, `log_audit_batch`). None writes no rows.
            date_jitter_config (dict, optional): The date shift range,
                `{"min_days": ..., "max_days": ...}`. Defaults to -365 to -1.
            project_secret (bytes, optional): The store's project secret,
                which keys the date offset and the UID and Patient ID
                checks. Required at use: a keyed date shift without one
                raises `RuntimeError`. `Session.anonymize()` always passes
                it.
        """
        self.logger = get_logger()
        self.store_backend = store_backend
        self.project_secret = project_secret
        # Entities `_record_decline` named during the current pass;
        # reset by `apply_remediation` and read at its end.
        self._declined_entities: list = []
        # The copies an entity-level write reached this pass, `(id(instance),
        # tag) -> removed`, and the foldable instance findings waiting on
        # each (a REMOVE folds only where the owner removed).
        # Reset by `apply_remediation`; read by `_folds_into_owner`.
        self._owner_copies: dict = {}
        self._pending_folds: dict = {}
        self.jitter_config = date_jitter_config or {"min_days": -365, "max_days": -1}

    def apply_remediation(self, findings: List[PhiFinding]):
        """Apply the remediation proposal of each finding, as one pass.

        Findings without a proposal are skipped, and findings on the same
        attribute (`_remediation_key`) are applied once. Entity-level
        findings (Patient, Study, Series) run first; an instance finding on
        a copy an owner's write already reached is folded into that write
        rather than run. A proposal that raises is logged and recorded as
        a decline, and the pass continues.

        Side effects: writes each value onto the graph in memory and marks
        the entities it changed modified; stamps each remediated entity
        REMEDIATED, then at the pass end demotes to IDENTIFIED any entity
        left REMEDIATED over something it declined or was never handed
        (`_settle_statuses`); writes one audit row per remediation applied
        or declined, in one batch at the end, when a store backend is set.

        Args:
            findings (List[PhiFinding]): The findings whose proposals to
                apply.

        Returns:
            int: How many remediations were applied. Failures are logged and
                excluded, so this is a count of what actually changed.
        """
        processed_entities = set()  # To avoid double-processing if multiple findings point to same entity/attr
        audit_buffer = []
        self._declined_entities, self._satisfied_keys = [], set()
        self._owner_copies = {}
        # Entity-level findings first, everything else after in its own
        # relative order. The owner's write has to land before the
        # instance findings on the same copies are judged, or which value
        # an instance keeps depends on the order the caller handed the
        # findings in. Stable, so the deepest-first order of
        # private-sequence removals survives.
        findings = self._entity_findings_first(findings)
        self._pending_folds = self._foldable_instance_findings(findings)
        # Keys of the instance findings folded into an owner's write, so
        # a duplicate folds once.
        folded_keys = set()
        failures = 0
        # How many proposals actually reached
        # `_apply_single_remediation`, which is what the failure warning
        # below means by "of N". Its own counter, not
        # `failures + len(processed_entities)`: a decline is excluded
        # from `processed_entities`, so that sum is not the number
        # attempted.
        attempted = 0

        for finding in progress_bar(findings, desc="Anonymizing Metadata", unit="finding"):
            if not finding.remediation_proposal:
                continue

            # Deduping key: what is being changed, and where it lives.
            # `target_attr` is the attribute the proposal actually writes,
            # which is what "already handled" should mean; not
            # `field_name`, a display string that is "Unknown Tag" for
            # every unnamed config entry. `entity_path` is in the key
            # because a finding inside a sequence carries its instance's
            # UID, so two items holding the same tag would otherwise
            # collide. One spelling, shared with the scan tally that
            # settles the pass.
            key = _remediation_key(finding)
            if key in processed_entities or key in folded_keys:
                continue

            # Folded, not applied: an owner's write in this pass already
            # put its value on this copy, and running the instance's own
            # proposal would put a second one there. Stamped
            # REMEDIATED as its own success would have been, and inside
            # the loop, so the pass-end demotion below still takes an
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
                # cannot tell apart because it carries no action type.
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
        # REMEDIATED, or the manifest would read `"anonymized": true` over
        # the declined value. Demoted here rather than by withholding the
        # stamp, because the success arm cannot know what a later proposal
        # on the same entity will do; the pass is the unit that can. Only
        # an entity the pass left REMEDIATED is touched: one that only
        # declined keeps whatever status it had. The manifest reads the
        # status rather than the audit trail's declines, so there is one
        # source for the answer.
        #
        # The same holds for what the pass was never handed: a raise is a
        # decline, and an entity the last audit raised more against than
        # the passes on it handled is demoted too, settled against the
        # scan tally. `_settle_statuses` says how.
        self._settle_statuses(findings, processed_entities | folded_keys)

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
        """Apply one finding's remediation proposal to its entity.

        Handles `REPLACE_TAG`, `SHIFT_DATE` (deterministic per patient) and
        `REMOVE_TAG`. On success the entity is marked modified and stamped
        REMEDIATED, a Patient, Study or Series field's new value is written
        to each instance's own copy of the tag, and the audit row is
        written. A proposal that cannot run writes a `REMEDIATION_DECLINED`
        row instead; one whose end state is already there writes no row
        and stamps the entity REMEDIATED.

        Args:
            finding (PhiFinding): The finding containing the proposal.
            audit_buffer (list, optional): Rows are appended here for one
                batched write; None writes each row at once.

        Returns:
            bool: True when the entity was actually changed. False on
                every declining path, and when the end state was already
                there. `apply_remediation` keys its dedup set and its
                returned count on this, so a decline neither counts as
                applied nor suppresses a later finding against the same
                attribute (the dedup key carries no action type).
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
            # 1. A DicomItem: an Instance, or an item inside a sequence.
            # `_replace_on_item` decides what is written and whether the
            # target is still there to write it to. `details` None is a
            # decline, or the end state already there, which `_satisfied`
            # stamps. One call rather than the cases inline, so the
            # line-cited `mark_modified()` calls below do not move.
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
                # Asked again here for the reason rather than bound above
                # the arm: a line there would move the line-cited
                # `mark_modified()` calls.
                reason = self._replace_attr_refused(entity, proposal)
                self.logger.warning(
                    f"Remediation declined for {self._log_subject(finding)}: {reason}")
                self._record_decline(finding, reason, audit_buffer)
                return False

        elif proposal.action_type == "SHIFT_DATE":
            patient_id = self._resolve_patient_id(entity, proposal)
            if not patient_id:
                self.logger.warning(f"Could not resolve PatientID for "
                                    f"{self._log_subject(finding)}. Skipping date shift.")
                self._record_decline(
                    finding, f"could not resolve a PatientID to seed the jitter for "
                    f"{proposal.target_attr}, so the date is unshifted", audit_buffer)
                return False

            # The scheme the scan recorded for this patient; a finding
            # built by hand carries none and is keyed, as every new
            # patient is.
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
                    # Recorded **before** the write, not after.
                    # `anonymize()` does not drain the persistence manager,
                    # so a background `save()` can serialize this entity
                    # between the two statements. A stored value whose
                    # record did not reach the store is raised again on
                    # the next load and shifted twice; a stored record
                    # whose value did not reach the store is harmless,
                    # because the equality check fails and the old value
                    # is correctly raised. One ordering is recoverable and
                    # the other is not.
                    #
                    # Called bare, where the `else` below guards the same
                    # name with `hasattr`, and the asymmetry is
                    # deliberate: `set_attr` and `record_date_shift` are
                    # both `DicomItem`'s, so anything reaching this branch
                    # has the second method by having the first, and a
                    # guard here would turn a writer that lacked it into a
                    # silently unrecorded shift rather than an error. The
                    # `else` branch is reached by anything *without*
                    # `set_attr` -- `Study` on every shipped path, but any
                    # object carrying the names it writes by this arm's
                    # duck-typing -- so there a missing record skips.
                    entity.record_date_shift(proposal.target_attr, new_date)
                    entity.set_attr(proposal.target_attr, new_date)
                else:
                    # `Study`'s own one-value record, before the write
                    # for the same reason. `new_date` is already
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
                    # it.
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
            # 1. A DicomItem.
            if hasattr(entity, "attributes") and isinstance(entity.attributes, dict):
                if proposal.target_attr in entity.attributes:
                    self._record_what_is_left(entity, proposal.target_attr, None)
                    del entity.attributes[proposal.target_attr]
                    # `attributes` is a plain dict, so `del` bumps no revision,
                    # unlike `set_attr`, and after a reload the REMEDIATED
                    # stamp below short-circuits on an unchanged status.
                    # Without this an already-saved instance reports no
                    # unsaved changes after its PHI is stripped, the next
                    # save skips it, and the identifier stays in the store.
                    entity.mark_modified()
                    details = f"Removed Tag {proposal.target_attr} from {finding.entity_uid}"
                    action_type = "REMEDIATION_REMOVE"
                elif proposal.target_attr in getattr(entity, "sequences", {}):
                    # A private sequence is a private tag, and the sweep
                    # asks for it by name. Without this arm the finding
                    # is filed, the report says the block was removed,
                    # and the exporter writes it anyway.
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
            # 2. A Python attribute (`Patient`/`Study`/`Series` field).
            elif self._holds_attr_to_remove(entity, proposal.target_attr):
                setattr(entity, proposal.target_attr, None)
                if hasattr(entity, "mark_modified"):
                    entity.mark_modified()
                details = f"Cleared Attribute {proposal.target_attr} on {finding.entity_uid}"
                action_type = "REMEDIATION_REMOVE"

        if action_type:
            # A Patient, Study or Series field was written, so the same
            # value goes onto each instance's own copy of the tag. Here
            # and not in the arms above: the arms end in the five
            # line-cited `mark_modified()` calls, and this block sits
            # below all of them. Before the REMEDIATED stamp below, which
            # is only about `entity`; the instances keep their own status.
            wrote = self._write_to_instances(entity, proposal.target_attr)
            if wrote is not None:
                written, folds = wrote
                verb = ("removed from" if action_type == "REMEDIATION_REMOVE"
                        else "written to")
                details += f"; {verb} {_count(written, 'instance copy', 'instance copies')}"
                # Said on this row because the folded findings get no rows
                # of their own. Counted from the pass's pending set before
                # they run, so the row is complete when it is appended: no
                # row already in `audit_buffer` may be rewritten (the
                # frozen-surface Pin A).
                if folds:
                    details += (f"; {_count(folds, 'instance-level finding')} on this "
                                f"tag folded into it")
            # The instance holding a nested item this wrote. An item has
            # no link to its instance, so the instance is marked and
            # stamped here, or it would neither read REMEDIATED nor have
            # anything to save. The owner's `mark_modified()` looks
            # redundant beside the stamp below and is not: an owner
            # already reading REMEDIATED -- loaded that way, or stamped by
            # an earlier call and saved since -- short-circuits the stamp,
            # and then this is the only thing that makes the save write
            # the replacement inside it. Before the stamp, for the reason
            # the next comment gives.
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
                    # Five elements, including the `loss_scope` and
                    # `element_tag` slots no remediation ever fills:
                    # `log_audit_batch` takes one shape, not one of
                    # several.
                    audit_buffer.append(
                        (action_type, finding.entity_uid, details, None, None))
                else:
                    self.store_backend.log_audit(action_type, finding.entity_uid, details)
            return True
        else:
            # Every other non-success path above `return`s, so reaching
            # here with an empty `action_type` means one of four things:
            # a `REMOVE_TAG` whose target is in neither `attributes` nor
            # `sequences`; a `REMOVE_TAG` against an entity with no
            # `attributes` dict *and* no matching Python attribute (the
            # arm at the bottom of that block is an `elif` on the outer
            # `hasattr`, so both fall past it); a `REMOVE_TAG` on a
            # `Patient` or `Study` field the exporter stamps that is
            # already None and on no instance beneath it, which the arm
            # above declines to write (`_holds_attr_to_remove`); or a
            # proposal carrying an action type this method does not
            # implement.
            #
            # The first and the third are the end state REMOVE asks for,
            # already there: satisfied, not a decline, or
            # `anonymize(report)` handed one report twice would write a
            # decline row for every removal the first call made and grade
            # a clean graph REVIEW_REQUIRED. The other two decline.
            # `_remove_is_satisfied` says what "gone" means; any other
            # spelling -- `00080080`, `InstitutionName`, a Python
            # attribute the export never writes -- declines, because its
            # absence says nothing about the value.
            #
            # One `else` here rather than an `else` nested in the
            # `attributes` arm: nested, it would cover only the first of
            # the four, and neither the third, which arrives from the arm
            # below it, nor a fifth if one were ever added above.
            #
            # Absence is read on the object the session holds at the
            # finding's address, not on `entity`: a report kept across a
            # reopen points at objects its first pass cleaned, and read
            # there every removal of an unsaved pass would be satisfied
            # while the live graph still held it. On the session path the
            # findings are already bound to that object, so the two
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
        """Apply a `REPLACE_TAG` proposal to a `DicomItem`.

        Checked in order:

        - **An owner-stamped copy** is decided by `_owner_stamps_copy`,
          whose reason wins; then a UID replacement that is not this
          store's declines (`_foreign_uid_refused`).
        - **EMPTY on a sequence clears its items.** A sequence already at
          zero items is satisfied.
        - **A target the item no longer holds declines**, rather than
          writing an element the graph no longer had. So does a value
          other than `""` aimed at a sequence.
        - **A value the tag's dictionary VR cannot hold declines** (a
          standard tag only; a private value is written as LO).
        - **EMPTY on a binary VR writes `b""`.** The VR is the item's
          recorded one for a private tag, the dictionary's otherwise, and
          the value's type only when neither knows.
        - **The SOP Instance UID under a UID replacement** moves the
          instance (`_take_sop_uid`, pixels unchanged) only while it
          still holds the scanned UID or already holds the replacement.
          At a UID this project minted (replaced or redacted) it is
          satisfied; at any other it declines, so a kept report cannot
          move a redacted instance back onto the unredacted export's UID.
          A `REPLACE` with a value writes the element alone.

        A value write (not a sequence clear) is preceded by
        `_record_what_is_left`. Writes no audit row itself.

        Args:
            entity: The `DicomItem` (an Instance or a nested item).
            finding (PhiFinding): The finding holding the proposal.

        Returns:
            Tuple[Optional[str], Optional[str]]: `(details, decline_reason)`.
                `details` is the audit row's text when the item was changed
                and None when nothing was written; `decline_reason` is set
                when that was a decline. `(None, None)` means the item
                already holds what the rule asks, and the caller stamps the
                entity REMEDIATED on the strength of it.
        """
        # The caller writes both rows, so the action type and
        # `audit_buffer` stay in `_apply_single_remediation`, where the
        # frozen-surface pin (Pin A) reads them. Any path here that writes
        # nothing must return a reason unless the rule is already met:
        # `(None, None)` is stamped REMEDIATED.
        # Local: the dictionary is wanted only on this arm, and `entities`
        # is imported at module scope for other names already.
        from pydicom.datadict import dictionary_VR  # pylint: disable=import-outside-toplevel
        from .entities import _canonical_tag  # pylint: disable=import-outside-toplevel
        from .privacy import UID_REPLACEMENT  # pylint: disable=import-outside-toplevel

        proposal = finding.remediation_proposal
        tag = _canonical_tag(proposal.target_attr)
        attributes = getattr(entity, "attributes", None)
        sequences = getattr(entity, "sequences", None) or {}

        # An instance's top-level copy of a tag the export stamps from its
        # owner is written only through the owner: reached here, the
        # owner's write did not reach it (a reach folds, in the loop).
        stamped = self._owner_stamps_copy(entity, finding)
        if stamped is not None:
            return None, stamped or None
        # After the owner's: for a stamped copy the owner's reason wins, as
        # it does over the date shift's seed check.
        foreign = self._foreign_uid_refused(proposal)
        if foreign:
            self.logger.warning(
                f"Remediation declined for {self._log_subject(finding)}: {foreign}")
            return None, foreign

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
            # A value the tag's VR cannot hold declines: written, an OB
            # given `ANONYMIZED` fails the export with `TypeError` and a DA
            # exports the literal. The loader refuses such a rule, so this
            # is reached by a finding built by hand. The dictionary VR of
            # a standard tag only, never the recorded one: a private value
            # its VR cannot hold is written as LO, and declining there
            # would keep the identifier. A decline, `(None, reason)`, never
            # `(None, None)`, which says the rule is already met. A dummy
            # the scan built for a value-less REPLACE never reaches this:
            # it is valid for the tag's VR by construction.
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
            # EMPTY on a binary VR is `b""`, so the export writes a binary
            # empty and the scan meets one spelling of empty. The VR
            # decides before the value's type: a binary slot can already
            # hold a str.
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
        sop_move = (tag == "0008,0018" and not finding.entity_path
                    and hasattr(entity, "_take_sop_uid")
                    and (proposal.metadata or {}).get(UID_REPLACEMENT))
        if sop_move and entity.sop_instance_uid not in (proposal.original_value,
                                                        proposal.new_value):
            # The instance left the UID the scan saw for one other than
            # this finding's replacement (at that replacement already, the
            # write repeats itself as any REPLACE does). A kept report
            # still reaches a redacted instance -- `_instances_by_uid`
            # files it under its source on purpose -- and moving it to the
            # source's replacement would give the redacted pixels the UID
            # of the unredacted export. A UID this project minted,
            # redacted or replaced, is what the rule asks for: met.
            # Anything else is not ours to overwrite.
            from .privacy import _uid_is_minted  # pylint: disable=import-outside-toplevel
            if _uid_is_minted(entity.sop_instance_uid, self.project_secret):
                return None, None
            reason = (f"the SOP Instance UID is {entity.sop_instance_uid!r}, "
                      f"not {proposal.original_value!r} as scanned; the "
                      "identity is left as it is (#544)")
            self.logger.warning(
                f"Remediation declined for {self._log_subject(finding)}: "
                f"{reason}")
            return None, reason
        self._record_what_is_left(entity, proposal.target_attr, value)
        if sop_move:
            # An Instance's own SOP Instance UID under the keyed UID
            # replacement: the property moves with the element, or the
            # export would name the file, write the file meta and key the
            # store by the source UID while the element said another. The
            # pixels are unchanged, so the source file still serves them.
            # A `REPLACE value:` writes the element alone.
            entity._take_sop_uid(value, pixels_changed=False)
        else:
            entity.set_attr(proposal.target_attr, value)
        return (f"Remediated {finding.entity_uid} (Tag {proposal.target_attr}) "
                f"-> {proposal.new_value}"), None

    @staticmethod
    def _log_subject(finding: PhiFinding) -> str:
        """What a log line calls the finding's entity.

        Args:
            finding (PhiFinding): The finding.

        Returns:
            str: `"a patient"` for a patient finding, never its
                `entity_uid` (the original Patient ID); the `entity_uid`
                otherwise.
        """
        # The audit row keeps the Patient ID, because the store holds the
        # ID-to-pseudonym map and is guarded as the project secret is; the
        # log file is not guarded, and a Patient ID in it beside a
        # pseudonym or an offset would undo the de-identification.
        if finding.entity_type == "Patient":
            return "a patient"
        return str(finding.entity_uid)

    @staticmethod
    def _record_what_is_left(item, tag: str, value) -> None:
        """Record on `item` what the write about to run leaves at `tag`,
        for `lock_identities()`.

        Call it as the statement **immediately before** each write, never
        after: a background `save()` between the two would otherwise
        store the value without its record, and the next lock would stash
        a replacement as the original. Only an `Instance` records; a
        nested item has no slot, and the lock reads top-level values only.

        Args:
            item: The entity about to be written.
            tag (str): The tag being written.
            value: The value the write leaves; None for a removal.
        """
        # Not a wrapper around `_apply_single_remediation`: that would hand
        # `audit_buffer` to a callee the frozen-surface pin (Pin A) does not
        # list.
        if hasattr(item, "record_remediation"):
            item.record_remediation(tag, value)

    def _log_line(self, action_type: str, finding: PhiFinding, wrote) -> str:
        """The log file's line for an applied remediation.

        Names UIDs, the field and counts only: never the original Patient
        ID, the replacement written or a shift's days.

        Args:
            action_type (str): The `REMEDIATION_*` action type written.
            finding (PhiFinding): The finding applied.
            wrote: `(written, folds)` from `_write_to_instances`, or None.

        Returns:
            str: The log line.
        """
        # Deliberately not the audit row's `details`: a log line pairing an
        # identity with an offset hands the offset to whoever reads the log.
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
        """Write the `REMEDIATION_DECLINED` audit row for a remediation that
        did not run.

        The row goes beside the caller's own log line; the reason is prose
        in `details`, and `element_tag` stays empty. The entity (and, for a
        nested item, the instance holding it) is named in
        `_declined_entities`, and the pass end demotes every named entity
        that ends the pass REMEDIATED to IDENTIFIED; its status is not
        changed here. It is named even with no store backend, when no row
        is written.

        Two reasons from `_owner_stamps_copy` are not declines and write
        no row: a `_CopyLeftEmpty` is routed to `_satisfied`, and an
        `_OwnerNotHandedIn` does nothing, leaving the finding unhandled
        for the scan tally.

        Args:
            finding (PhiFinding): The finding that declined.
            reason (str): Why, as persisted in the row; names no value.
            audit_buffer (list, optional): The row is appended here as a
                five-element tuple (`loss_scope` and `element_tag` None)
                for `log_audit_batch`; None writes it at once through
                `log_audit`.
        """
        # No `record_phi_status` here: a later proposal on the same entity
        # can succeed and stamp REMEDIATED over the decline, so the pass
        # end demotes instead. The entity is named before the no-backend
        # return because the demotion is about the graph, not the audit
        # table. `element_tag` is for `SCAN_GAP` rows only.
        if isinstance(reason, _CopyLeftEmpty):
            # Not a decline either: an owner-stamped copy left empty, as
            # its owner holds no value, with nothing handed in to write
            # one -- nothing remains in the graph or the file, so the
            # finding is satisfied. Routed through here so the arms'
            # line-cited `mark_modified()` calls do not move.
            self._satisfied(finding, None)
            return
        if isinstance(reason, _OwnerNotHandedIn):
            # Not a decline: an owner-stamped copy whose owner this pass
            # was not handed. No row and no demotion; the
            # finding stays unhandled for the scan tally.
            return
        if finding.entity is not None:
            self._declined_entities.append(finding.entity)
            # A decline inside a sequence leaves the value in the
            # instance, so the instance is demoted with the item.
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
        """Log a remediation that raised, and return its decline reason.

        `apply_remediation` records the reason with `_record_decline`, so
        the entity is demoted at the pass end even when a sibling's success
        stamped it REMEDIATED. The log line names the entity through
        `_log_subject`, never a Patient ID.

        Args:
            finding (PhiFinding): The finding whose proposal raised.
            error (Exception): What it raised.

        Returns:
            str: The reason, on one line with `|` escaped, saying the value
                may be unchanged or partly written.
        """
        # Flattened and pipe-escaped: the reason lands in `details`, which
        # the report renders into a markdown table cell, and an exception's
        # text is under nobody's control.
        self.logger.error(
            f"Failed to apply remediation for {self._log_subject(finding)} "
            f"({finding.field_name}): {describe_exception(error)}")
        proposal = finding.remediation_proposal
        reason = (f"{proposal.action_type} on {proposal.target_attr} raised "
                  f"{describe_exception(error)}; the value may be unchanged "
                  "or partly written")
        return " ".join(reason.split()).replace("|", "\\|")

    def _satisfied(self, finding: PhiFinding, declined) -> bool:
        """Settle a proposal whose end state the item already holds.

        Called for `_replace_on_item`'s `(None, None)` (an `EMPTY` on a
        sequence already at zero items, a SOP UID this project already
        minted, an owner-stamped copy already holding the value the export
        writes), for a `_CopyLeftEmpty`, and for a `REMOVE_TAG` whose
        target is already gone (`_remove_is_satisfied`).

        When `declined` is falsy the entity (and the instance holding a
        nested item) is stamped REMEDIATED and the key is counted as
        handled for the scan tally. No row is written, nothing is counted
        as applied, and nothing is marked modified. When `declined` is
        set, nothing is stamped.

        Args:
            finding (PhiFinding): The finding whose end state is there.
            declined: The decline reason the caller recorded, if any.

        Returns:
            bool: Always False, the caller's value for "nothing written".
        """
        # No `mark_modified()`: nothing changed. The stamp itself advances
        # the revision when the status changes, as a status change should.
        # `_satisfied_keys` is rebound, never mutated: its class default is
        # a `frozenset` shared by every service, and a direct
        # `_apply_single_remediation` call never passes the reset in
        # `apply_remediation`.
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
        """Why a `SHIFT_DATE` must not write, or None when it may.

        Checked in order:

        - **A blank original passes** whatever the target holds, to the
          arm's empty-date branch, which writes no row.
        - **An owner-stamped copy**: a non-empty reason from
          `_owner_stamps_copy` is returned as this one.
        - **A seed that is not the holder's declines.** The offset is
          derived from the Patient ID and scheme the finding carries
          (`_resolve_patient_id`, `metadata["jitter_scheme"]`), so in a
          session that seed must key to the patient holding the date as
          the pass began (`_use_holders`, `_belongs_to_holder`): the
          original ID or its pseudonym, keyed or unkeyed, or the real
          Patient ID of another project's export ingested here. A nested
          date's holder is its instance. A finding whose entity has no
          holder declines. A service used without a session checks
          nothing here.
        - **The target** may be written when it still holds the audited
          value **or already holds `new_date`**, so `anonymize(report)`
          handed the same report twice is idempotent. Anything else
          declines, absent included. An unparseable original (`new_date`
          None) on a target still holding it passes, so the arm's own
          invalid-format decline is the one row.

        A `DicomItem` is read at the canonical key; a key holding None is
        gone. A `Study` is compared through `normalize_study_date`, so
        `"2004-01-19"`, `"20040119"` and `date(2004, 1, 19)` are one date,
        and the audited side is stripped first. Every decline logs a
        warning. The reasons name the tag and never a value: they are
        persisted in the decline row.

        Args:
            entity: The entity holding the date (a `DicomItem` or a
                `Study`).
            finding (PhiFinding): The finding holding the proposal.
            new_date: The shifted value the arm would write, or None when
                the original does not parse.

        Returns:
            Optional[str]: The decline reason, or None when the shift may
                be written.
        """
        # The arm's output is a function of `proposal.original_value`, not
        # of what the target holds now, so without this check a date
        # deleted, blanked or edited between `audit()` and `anonymize()`
        # would be re-created or overwritten. The warning is logged here
        # rather than in the arm, so the arm adds no line above the five
        # line-cited `mark_modified()` calls.
        from .entities import _canonical_tag, normalize_study_date  # pylint: disable=import-outside-toplevel

        proposal = finding.remediation_proposal
        # A blank: there was no date to shift, so nothing can have been
        # re-created or overwritten, and a decline would grade
        # REVIEW_REQUIRED over nothing.
        if proposal.original_value is None or not str(proposal.original_value).strip():
            return None
        # Before the seed check: for a copy the export stamps from its
        # owner, "the owner did not write it" is the truer reason.
        stamped = self._owner_stamps_copy(entity, finding)
        if stamped:
            return stamped
        # `Session.anonymize(findings)` resolves a report against the live
        # graph, so a report can reach a patient it was not raised for
        # (another store's report, a legacy unkeyed one, another site's
        # files under the same UIDs) and would write an offset that is not
        # the patient's own.
        if self._holders is not None and not self._belongs_to_holder(
                self._resolve_patient_id(entity, proposal),
                (proposal.metadata or {}).get("jitter_scheme", JITTER_SCHEME_KEYED),
                self._holders.get(id(self._instance_owners.get(id(entity), entity)))):
            reason = (f"{proposal.target_attr}: the Patient ID its offset is seeded "
                      "on is not that of the patient holding the date in this "
                      "store, so the date is not shifted")
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
        when it may.

        A name the entity lacks refuses. A slots field holding None (a
        value the caller cleared after `audit()`) refuses a value, so the
        rule's value is not re-created where the caller cleared one; it
        takes `""`, because the exporter writes None and `""` as the same
        zero-length element, and the entity then holds `""` with a row
        that says so. A present but empty value is not a cleared one and
        is written. Generic over every entity field (`Study.study_date`,
        `Patient.patient_name`, `Series.modality`, ...). The reasons name
        the attribute and the type and never a value: they are persisted
        in the row and rendered into the report.

        **A Patient ID that is not this store's pseudonym for the patient
        refuses.** In a session the value written must be the one this
        store mints for `original_value` under the patient's own scheme,
        as the pass began (`_use_holders`), and `original_value` must key
        to that patient (`_is_holders_pseudonym`). A report from this store
        writes the value its patient already holds or would be given;
        anything else refuses, whatever produced it. A service used without
        a session checks nothing. Finally a UID replacement is checked by
        `_foreign_uid_refused`.

        Pure: changes nothing and writes no row.

        Args:
            entity: The Patient, Study or Series to be written.
            proposal (PhiRemediation): The `REPLACE_TAG` proposal.

        Returns:
            Optional[str]: The refusal reason, or None when the write may
                go ahead.
        """
        # Takes no `audit_buffer`: the frozen-surface pin (Pin A) refuses a
        # new callee that takes one. Called twice from the arm, once as the
        # condition and once for the reason, because binding the answer
        # above the arm adds a line above the line-cited `mark_modified()`
        # calls.
        attr = proposal.target_attr
        if not hasattr(entity, attr):
            return f"{type(entity).__name__} has no attribute or setter for {attr}"
        if getattr(entity, attr) is None and proposal.new_value not in (None, ""):
            return (f"{attr} is no longer set on the {type(entity).__name__}, "
                    "so the rule's value is not written where the caller "
                    "cleared one")
        # `Session.anonymize(findings)` resolves a report against the live
        # graph, so a report raised in another store over the same files
        # carries that store's pseudonyms: written, they would link the two
        # exports, and this store's next `audit()` does not re-propose an ID
        # already shaped `ANON_`.
        if (attr == "patient_id" and self._holders is not None
                and not self._is_holders_pseudonym(proposal, self._holders.get(id(entity)))):
            return (f"{attr}: the value is not this store's pseudonym for the "
                    "patient it would be written to, so it is not written")
        return self._foreign_uid_refused(proposal)

    def _foreign_uid_refused(self, proposal) -> Optional[str]:
        """Why a UID replacement must not be written, or None when it may.

        A proposal carrying `UID_REPLACEMENT` is written only when its
        `new_value` is the one this store mints for `original_value`
        (`_replaced_uids`), so another store's replacements never link this
        store's export to that project's by UID. Without a project secret
        nothing is checked.

        Args:
            proposal (PhiRemediation): The proposal to check.

        Returns:
            Optional[str]: The refusal reason, naming no value, or None.
        """
        # A report resolves against the live graph by the UIDs it names, so
        # one raised in another store over the same files reaches this one;
        # this store's next `audit()` would not recognise those UIDs as its
        # own and would replace them a second time.
        from .privacy import UID_REPLACEMENT, _replaced_uids  # pylint: disable=import-outside-toplevel

        if not (self.project_secret and (proposal.metadata or {}).get(UID_REPLACEMENT)):
            return None

        # Both sides as the scan builds them: `_replaced_uids` gives a str,
        # or a list for a multi-valued element, and a pickle keeps either.
        if _replaced_uids(proposal.original_value, self.project_secret) == proposal.new_value:
            return None
        return (f"{proposal.target_attr}: the value is not this store's "
                "replacement for the UID the scan saw, so it is not written")

    @classmethod
    def _holds_attr_to_remove(cls, entity, attr) -> bool:
        """Whether the `REMOVE_TAG` Python-attribute arm has something to
        remove on `entity`.

        False where the entity lacks the attribute or the field is already
        gone (`_owner_field_gone`): the removal then falls past every arm
        to the bottom `else`, where it is satisfied when the object at its
        address reads gone too, and declines otherwise.

        Args:
            entity: The entity the removal targets.
            attr (str): The attribute name.

        Returns:
            bool: True when the arm should clear the attribute.
        """
        # `hasattr` alone is True of a slots field holding None, so a
        # removal of a field already cleared would write a row and count as
        # applied for work nothing did. Pure, with no `audit_buffer`, as
        # `_replace_attr_refused` is, and defined below the five line-cited
        # `mark_modified()` calls so the arm's condition stays a same-line
        # call.
        return hasattr(entity, attr) and not cls._owner_field_gone(entity, attr)

    @classmethod
    def _owner_field_gone(cls, entity, attr) -> bool:
        """Whether the end state a `REMOVE_TAG` asks for on a `Patient` or
        `Study` field is already there.

        True only when all four hold:

        - `attr` is a field the exporter stamps from the entity
          (`ENTITY_FIELD_TAGS`); a removal on any other name is left to
          the arm.
        - The entity is not a `DicomItem` (no `set_attr`).
        - It has the attribute, and the attribute holds None. A `""` is a
          present, empty value, not gone.
        - No instance beneath the entity still holds the field's tag at
          the top level: a field cleared by hand while the copies survive
          is **not** gone, and the removal takes those copies away with
          its row, folding the instance-level findings on the tag into it.

        Args:
            entity: The entity the removal targets.
            attr (str): The attribute name.

        Returns:
            bool: True when nothing is left to remove.
        """
        # The field gate mirrors `_remove_is_satisfied`'s well-formed-tag
        # gate: absence under a name the export never writes is no evidence
        # about an element. `Study.__setattr__`'s `normalize_study_date("")`
        # keeps `""`, so None and `""` never convert behind this reader.
        # The instance walk is `_write_to_instances`' own, reading
        # `attributes` the same raw way, so the writer and this reader
        # cannot disagree about one instance. `getattr` with defaults
        # throughout, because the arm fires for any object carrying the
        # field, test doubles included.
        tag = cls.ENTITY_FIELD_TAGS.get(attr)
        if tag is None or hasattr(entity, "set_attr") or not hasattr(entity, attr):
            return False
        if getattr(entity, attr) is not None:
            return False
        # `_write_to_instances`' walk, the same helper.
        for instance in cls._instances_beneath(entity):
            if tag in getattr(instance, "attributes", {}):
                return False
        return True

    def _belongs_to_holder(self, patient_id, scheme, holder) -> bool:
        """Whether `patient_id`, read under `scheme`, names the patient
        `holder`.

        One patient has one canonical key per scheme: the original ID and
        the pseudonym this store gives it key alike, keyed or unkeyed, and
        another patient's ID does not. The schemes must match as well as
        the keys, so a legacy spelling never admits a keyed patient.

        Args:
            patient_id (str): The Patient ID to test; None is False.
            scheme (str): The jitter scheme it is read under.
            holder: A `(patient_id, jitter_scheme)` pair from
                `_use_holders`, or None for an entity with no holder in the
                graph (always False).

        Returns:
            bool: True when both name the same patient.
        """
        if holder is None or patient_id is None:
            return False
        holder_id, holder_scheme = holder
        return (scheme == holder_scheme
                and canonical_patient_key(patient_id, self.project_secret, scheme)
                == canonical_patient_key(holder_id, self.project_secret, holder_scheme))

    def _is_holders_pseudonym(self, proposal, holder) -> bool:
        """Whether a `patient_id` REPLACE writes the pseudonym this store
        mints for its `original_value` under the holder's scheme, and that
        original names the holder.

        Args:
            proposal (PhiRemediation): The `patient_id` REPLACE proposal.
            holder: The `(patient_id, jitter_scheme)` pair of the patient
                it would be written to, or None.

        Returns:
            bool: True when both hold; False with no holder or no
                original.
        """
        from .entities import JITTER_SCHEME_UNKEYED  # pylint: disable=import-outside-toplevel
        from .privacy import (  # pylint: disable=import-outside-toplevel
            _replacement_id_for, _unkeyed_replacement_id_for)

        if holder is None or proposal.original_value is None:
            return False
        scheme = holder[1]
        minted = (_unkeyed_replacement_id_for(proposal.original_value)
                  if scheme == JITTER_SCHEME_UNKEYED
                  else _replacement_id_for(proposal.original_value, self.project_secret))
        return (proposal.new_value == minted
                and self._belongs_to_holder(proposal.original_value, scheme, holder))

    @staticmethod
    def _remove_is_satisfied(entity, proposal) -> bool:
        """Whether a `REMOVE_TAG` that matched no arm is one whose target
        is already gone -- from a `DicomItem`, or from a `Patient` or
        `Study` whose own field it names. False for any action other than
        `REMOVE_TAG`.

        **An item.** True only when all three hold: the entity has an
        `attributes` dict; `target_attr` lower-cased is a well-formed
        `gggg,eeee` tag (`config_manager._is_tag_key`, the check a
        config's tag keys already pass); and neither `attributes` nor
        `sequences` holds that canonical key. Anything else declines.

        **A Patient or a Study** (no `attributes` dict):
        `_owner_field_gone` answers, and says what "gone" means for a
        field the exporter stamps from the entity. A name outside
        `ENTITY_FIELD_TAGS`, a field still holding a value, an instance
        copy still holding the tag, and None (an address that named no
        object) are all False, so those decline with `matched no
        applicable arm`.

        Under a session, pass `_removal_subject`'s answer as `entity`: the
        live object at the finding's `entity_uid` and `entity_path`, not
        `finding.entity`, and None when the address cannot be read as done
        (which is False), so a report kept across a reopen, or a
        hand-built finding filed under another instance's UID, does not
        read absence on an object the export never writes.

        Args:
            entity: The object the removal's absence is read on, or None.
            proposal (PhiRemediation): The proposal.

        Returns:
            bool: True when the target is already gone.
        """
        # Read canonically because the REMOVE arms above test the raw
        # `target_attr`: a hand-built upper-case tag the item holds
        # lower-case falls past them, and read raw here it would count as
        # satisfied over a value still there. Well-formed because absence
        # under a key is evidence only for the key the graph would store
        # the element under: `00080080`, `(0008,0080)`, `InstitutionName`
        # or `patient_id` lower-case onto keys no item has, and would read
        # as absent over a value still there.
        # pylint: disable=import-outside-toplevel
        from .config_manager import _is_tag_key
        from .entities import _canonical_tag

        if proposal.action_type != "REMOVE_TAG":
            return False
        attributes = getattr(entity, "attributes", None)
        if not isinstance(attributes, dict):
            # A Patient or a Study: its own field, not an element of an
            # item, and read by the fields the exporter stamps.
            return RemediationService._owner_field_gone(
                entity, proposal.target_attr)
        tag = _canonical_tag(proposal.target_attr)
        if not (isinstance(tag, str) and _is_tag_key(tag)):
            return False
        sequences = getattr(entity, "sequences", None) or {}
        return tag not in attributes and tag not in sequences

    def _settle_statuses(self, findings: list, handled: set) -> None:
        """Demote to IDENTIFIED every entity a pass left REMEDIATED over
        something it did not remove.

        Called once at the end of `apply_remediation`. Four sources, one
        demotion:

        - **A decline** -- including a proposal that raised -- names its
          entity in `_declined_entities`.
        - **The scan tally**, when `audit()` built one: every uid this
          pass's findings name is settled against the keys the pass
          handled (applied, folded, already satisfied, or inside a
          sequence a pass removed). An incomplete uid demotes every
          entity the pass's findings under it resolve to, and the
          instance holding a nested one.
        - **A Series held open by the tally** (the tally arm): each
          Series of the session (`_series_at_start`) whose pass-start UID
          the tally settles incomplete -- raised and not handed in, or
          handed and declined -- is demoted with its instances, whether
          or not this pass named it. A uid already settled this pass is
          not asked twice.
        - **A Series still open as it stands** (the live arm), tally or
          none. Each Series is asked the scan's own condition
          (`privacy._owned_uid_is_open`) under `_series_policy`, reading
          its UID at the pass end (one this pass replaced is minted and
          reads closed). An open Series demotes only the instances beneath
          it whose status revision this pass moved
          (`_status_revisions_at_start`): a status recorded before the
          pass is not this pass's to change. Needs a secret and a policy.

        Neither Series arm lifts a demotion: a Series completed later
        completes its instances only when their own findings are handed in
        again or a re-audit runs.

        **The entity's own UID is settled too.** Each resolved finding's
        live UID (`_live_uid`) is settled beside the uid it names, with the
        keys handled under it, so a hand-built finding naming the wrong uid
        or none cannot leave its entity REMEDIATED over what the audit
        raised under its real UID. A UID the audit did not raise under --
        one `redact()` regenerated since -- gets no opinion. A patient is
        settled under the `patient_id` it held when the pass began.

        Only an entity that ends the pass REMEDIATED is touched: one that
        only declined keeps whatever status it had. A Series in the list
        demotes the instances that bear its status.

        Args:
            findings (list): The pass's findings.
            handled (set): The keys the pass applied or folded.
        """
        # A decline is settled here, not by withholding the success stamp:
        # the success block stamps REMEDIATED per proposal and cannot know
        # what a later proposal on the same entity will do; the pass can.
        # The tally is keyed on scan-time `entity_uid` strings, not live
        # entities, because a patient's `patient_id` changes during the
        # pass. `getattr` on the status, because a hand-built entity need
        # carry no status.
        by_uid = {}
        for key in handled | self._satisfied_keys | self._gone_keys:
            by_uid.setdefault(key[0], set()).add(key)
        demote = list(self._declined_entities)
        if self._scan_tally is not None:
            live = {id(f): self._live_uid(f.entity) for f in findings
                    if f.remediation_proposal and f.entity is not None}
            settled = {
                uid: self._scan_tally.settle(uid, by_uid.get(uid, ()))
                for uid in ({f.entity_uid for f in findings
                             if f.remediation_proposal}
                            | set(live.values()))}
            incomplete = {uid for uid, done in settled.items() if done is False}
            for finding in findings:
                if finding.entity is not None and (
                        finding.entity_uid in incomplete
                        or live.get(id(finding)) in incomplete):
                    demote.append(finding.entity)
                    owner = self._instance_owners.get(id(finding.entity))
                    if owner is not None:
                        demote.append(owner)
            # A Series' instances bear its findings: a series whose
            # scan-time UID the tally holds open -- raised and not handed
            # in, or handed and declined -- keeps them from reading
            # REMEDIATED, whether or not this pass named it. Nothing here
            # lifts them when a later pass completes the Series: as for an
            # owner handed in late, the instance findings handed again, or
            # a re-audit, do. A uid this pass already settled is not asked
            # twice.
            for series, uid in self._series_at_start:
                if uid is None:
                    continue
                done = (settled[uid] if uid in settled
                        else self._scan_tally.settle(uid, by_uid.get(uid, ())))
                if done is False:
                    demote.append(series)
        # The same question asked of the Series as it stands, tally or
        # none. A Series has no stored status, so after a reopen its
        # finding survives only in a report, and a pass handed a plain list
        # without it has no tally to hold it open: without this the
        # instances would read REMEDIATED over a file carrying the source
        # Series UID. So at the pass end each Series is asked the scan's
        # own condition (`_owned_uid_is_open`) under the policy the session
        # judges by: its UID *now* -- one this pass replaced is minted and
        # asks nothing -- non-blank, not minted here, under a value-less
        # REPLACE on `0020,000e`. An open Series demotes only the instances
        # beneath it whose status this pass recorded: a status recorded
        # before the pass is not this pass's to change, and the check has
        # no audit behind it, so it speaks only for what the pass itself
        # stamped. Only a demotion: a Series found closed lifts nothing.
        if self._series_at_start and self._series_policy is not None and self.project_secret:
            from .privacy import _owned_uid_is_open  # pylint: disable=import-outside-toplevel
            before = self._status_revisions_at_start
            for series, _ in self._series_at_start:
                stamped = [instance for instance in series.instances
                           if getattr(instance, "_phi_status_revision", None)
                           != before.get(id(instance))]
                if stamped and _owned_uid_is_open(
                        self._series_policy, "0020,000e",
                        getattr(series, "series_instance_uid", None),
                        self.project_secret):
                    demote.extend(stamped)
        # A Series in the list demotes itself and the instances that bear
        # its status.
        demote = [bearer for entity in demote
                  for bearer in ([entity, *entity.instances] if isinstance(entity, Series)
                                 else [entity])]
        for entity in demote:
            if getattr(entity, "phi_status", None) is PhiStatus.REMEDIATED:
                entity.record_phi_status(PhiStatus.IDENTIFIED)

    def _live_uid(self, entity) -> Optional[str]:
        """The UID the scan files `entity`'s findings under.

        An instance's SOP Instance UID, a study's Study Instance UID or a
        series' Series Instance UID -- the uids `PhiInspector` writes into
        `entity_uid` -- as the pass began (`_pass_start_uids`); read now
        (`_uid_of`) only for an entity the snapshot does not hold. A
        patient's is its `patient_id` as the pass began
        (`_pass_start_ids`).

        Args:
            entity: A Patient, Study, Series, Instance or nested item.

        Returns:
            Optional[str]: The UID; None for a patient the snapshot does
                not hold, and for a nested item.
        """
        # A nested item's owner is not consulted because it could add
        # nothing: `Session._nested_finding_owners` finds an owner
        # *through* the finding's `entity_uid`, so an item has one only
        # when that name is already its instance's UID. A patient's ID is
        # read from the snapshot because the pass may already have replaced
        # it with the pseudonym.
        if isinstance(entity, Patient):
            return self._pass_start_ids.get(id(entity))
        # As the pass began, like a patient's: the pass itself moves an
        # instance's, a study's and a series' UID, and read now
        # it would be the replacement, under which the audit raised
        # nothing. Read now only for an entity the snapshot does not hold.
        if id(entity) in self._pass_start_uids:
            return self._pass_start_uids[id(entity)]
        return self._uid_of(entity)

    @staticmethod
    def _uid_of(entity) -> Optional[str]:
        """The entity's own UID as it stands now.

        Args:
            entity: An Instance, Study, Series or nested item.

        Returns:
            Optional[str]: The SOP, Study or Series Instance UID; None for a
                nested item, which has none.
        """
        # The Series arm feeds only `_live_uid`, and through it
        # `_pass_start_uids`; it is redundant with the Series settle at the
        # pass end only because `_use_series` is always called beside
        # `_use_scan_tally` -- once, in `Session.anonymize()`. A new caller
        # of `_use_scan_tally` that skips `_use_series` makes this arm
        # load-bearing.
        if isinstance(entity, Instance):
            return entity.sop_instance_uid
        if isinstance(entity, Study):
            return entity.study_instance_uid
        return getattr(entity, "series_instance_uid", None)

    #: The `Patient`/`Study`/`Series` fields the exporter stamps onto every
    #: exported instance from the entity, with the tag each is the value
    #: of (`io_handlers.export_stamp_attributes`). That helper also reads
    #: `birth_date`, `sex` and `accession_number` through `getattr`, and
    #: no slots dataclass has such a field, so those arms never fire and
    #: are not listed.
    #:
    #: Keyed on the field, not on `PhiFinding.tag`: a hand-built finding
    #: can carry a tag that disagrees with the field its proposal writes,
    #: and it is the field that was written.
    #:
    #: A class attribute this far down the class rather than a module
    #: constant at the top, on purpose: a line added above the success
    #: block of `_apply_single_remediation` moves the five line-cited
    #: `mark_modified()` calls.
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
        # treatment.
        "study_time": "0008,0030",
        # The owners' own UIDs: the exporter stamps both from the
        # entity, and the keyed UID replacement moves the entity, so its
        # instances' top-level copies take the same value in the same
        # write. A Series walks its own instances.
        "study_instance_uid": "0020,000d",
        "series_instance_uid": "0020,000e",
    }

    @staticmethod
    def _instances_beneath(entity):
        """Every instance under a Patient, a Study or a Series.

        Args:
            entity: A Patient, Study or Series, or any object carrying
                `instances`, `studies` or `series`.

        Yields:
            Instance: Each instance beneath `entity`.
        """
        # The walk `_write_to_instances` writes and `_owner_field_gone`
        # reads, spelled once so the writer and the reader cannot disagree
        # about one instance. `getattr` with defaults because the arms fire
        # for any object carrying the field, test doubles included.
        if hasattr(entity, "instances"):
            yield from getattr(entity, "instances", [])
            return
        studies = getattr(entity, "studies", None)
        if studies is None:
            studies = [entity]
        for study in studies:
            for series in getattr(study, "series", []):
                yield from getattr(series, "instances", [])

    #: `id(nested item) -> Instance` holding it, for the findings of this
    #: pass raised inside a sequence. Empty unless
    #: `_use_instance_owners` is called, so a service used without a
    #: session -- the direct tests, hand-built findings -- stamps the item
    #: alone: it has no graph to find an owner in, and a guess from
    #: `entity_uid` could name the wrong one of two instances sharing a
    #: UID. Read-only: it is a class attribute, so an in-place write would
    #: reach every service in the process; the setter replaces it. Here
    #: rather than in `__init__` for `ENTITY_FIELD_TAGS`' reason above:
    #: nothing is added above the five line-cited `mark_modified()` calls
    #: -- which is also why `MappingProxyType` is imported here and not
    #: with the module's imports.
    #: No reset in `apply_remediation` for the same reason, and none is
    #: needed: `Session.anonymize()` builds a fresh service per call.
    from types import MappingProxyType as _MappingProxyType
    _instance_owners = _MappingProxyType({})

    def _use_instance_owners(self, owners) -> None:
        """Name the instance that holds each nested finding's item.

        A nested success then marks that instance modified and stamps it
        REMEDIATED, and a nested decline names it for the pass-end
        demotion, exactly as a top-level finding on the instance would.
        Without it a nested finding stamps the item alone.

        Args:
            owners: A mapping `id(nested item) -> Instance`; copied, so a
                later change to it does not reach this service.
        """
        self._instance_owners = self._MappingProxyType(dict(owners))

    #: `id(finding) -> the object the session holds at its address` for
    #: the `REMOVE_TAG` findings of this pass, None where the address
    #: cannot be read as done
    #: (`Session._removal_targets`). **None as the whole map means no
    #: session**, not "resolved nothing": a service used without one --
    #: the direct tests, hand-built findings -- has no graph to resolve an
    #: address in and reads `finding.entity`. A class attribute for
    #: `_instance_owners`' reason.
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

        Args:
            targets: A mapping `id(finding) -> live object`, None where the
                address cannot be read as done.
        """
        self._removal_objects = self._MappingProxyType(dict(targets))

    #: Keys of the findings `Session._live_findings` did not hand over
    #: because a pass already removed or emptied the sequence their REPLACE
    #: or SHIFT lived in: nothing is at the address to write, and no value
    #: reaches the export. Counted as handled by the scan tally, as a
    #: satisfied proposal is -- without it, an instance whose container an
    #: earlier partial pass emptied would be demoted over nothing.
    #: Rebound, never mutated, so the class default is safe to share.
    _gone_keys = frozenset()

    def _use_gone_keys(self, keys) -> None:
        """Count `keys` as handled when this pass settles its statuses.

        Args:
            keys: The remediation keys of findings not handed over because
                a pass already removed or emptied their sequence.
        """
        self._gone_keys = frozenset(keys)

    #: `id(entity) -> (patient_id, jitter_scheme)` of the live patient
    #: holding it, read before the pass can replace an ID
    #: (`Session._finding_holders`): what a Patient ID REPLACE and a
    #: SHIFT's seed must belong to. **None as the whole map means no
    #: session**, and nothing is checked, as `_removal_objects` reads it.
    #: A class attribute for `_instance_owners`' reason.
    _holders = None

    def _use_holders(self, holders) -> None:
        """Name the patient holding each entity of this pass, as it begins.

        Enables the Patient ID and date-shift seed checks.

        Args:
            holders: A mapping `id(entity) -> (patient_id, jitter_scheme)`.
        """
        self._holders = self._MappingProxyType(dict(holders))

    #: `id(Instance) -> (Patient, Study, Series)` holding it, for every
    #: instance of the graph (`Session._copy_owners`): the owners the
    #: export stamps Patient's Name, Patient ID, Study Date and the Study
    #: and Series Instance UIDs from. Instances only, so a nested item is
    #: never found in it -- the stamp reaches the dataset root only.
    #: **None as the whole map means no
    #: session**, and nothing is checked, as `_holders` reads it. A class
    #: attribute for `_instance_owners`' reason.
    _copy_owners = None
    #: `(id(owner), field)` for each Patient, Study or Series finding this pass was
    #: handed, by the field it writes. A copy whose owner field is not in
    #: it was not declined by its owner: the owner was simply not handed
    #: in. An owner finding that could not be resolved against the live
    #: graph (a reopen after an unsaved audit, say) is keyed
    #: `(None, field)` and counts as handed for every owner of that field:
    #: it was handed in and declined ("could not be resolved"), and which
    #: live owner it meant is exactly what is unknown, so the copy's row
    #: is kept (fail-closed).
    _owners_handed = frozenset()

    def _use_copy_owners(self, owners, handed=()) -> None:
        """Name the Patient, Study and Series the export stamps each
        instance from, and the owner fields this pass was handed.

        Enables the owner-stamped copy rule (`_owner_stamps_copy`).

        Args:
            owners: A mapping `id(Instance) -> (Patient, Study, Series)`.
            handed: `(id(owner), field)` pairs for the owner findings this
                pass was handed; `(None, field)` for one that could not be
                resolved.
        """
        self._copy_owners = self._MappingProxyType(dict(owners))
        self._owners_handed = frozenset(handed)

    def _owner_stamps_copy(self, entity, finding: PhiFinding) -> Optional[str]:
        """Why an instance finding on an owner-stamped copy must not run, or
        None when the copy is not one.

        The export writes `0010,0010` and `0010,0020` from the `Patient`,
        `0008,0020` and `0020,000d` from the `Study` and `0020,000e` from
        the `Series` over whatever the instance holds
        (`io_handlers.export_stamp_attributes`), so an instance's
        top-level copy of one of them is never what the file carries, and
        an instance finding on it that reaches an arm (its owner's write
        did not reach it) does not run. None also when there is no session
        (`_copy_owners` unset) or the instance has no entry.

        **Already there**: when the copy holds the owner's value and a
        remediation record vouches for it, returns `""`, which the
        callers read as satisfied (REPLACE) or run their own checks on
        (SHIFT).

        Otherwise the copy is first set to what the export writes: the
        owner's current value, rendered as the export renders it
        (`exported_patient_id`, `format_study_date`), `''` for an owner
        holding None -- even when that is the source identifier, since
        the graph copy equals the file. A copy no longer there is not
        re-created. The write is recorded with `_record_what_is_left`
        only when another instance under the same owner vouches for the
        value (it is an owner's write, not a source value). The
        instance's status is re-recorded at the new revision (unless it
        read UNSCANNED): kept when vouched or empty, and otherwise set to
        IDENTIFIED and the instance named in `_declined_entities`, with no
        row, so later successes in the pass cannot leave it REMEDIATED.

        Then, by whether the owner's finding was handed to this pass
        (`_owners_handed`, keyed `(id(owner), field)` or `(None, field)`):

        - **Handed in (and so declined):** returns a decline reason; the
          decline demotes the instance to IDENTIFIED at the pass end, as
          its owner is.
        - **Not handed in, owner and copy both empty:** returns a
          `_CopyLeftEmpty`, which `_record_decline` routes to `_satisfied`.
        - **Not handed in otherwise:** returns an `_OwnerNotHandedIn`,
          which `_record_decline` writes no row for; the finding is left
          unhandled for the scan tally, so the instance stays IDENTIFIED
          until the owner is acted on and a re-audit runs.

        Writes no audit row itself: the callers return the reason and the
        arm records the row. The reason names the tag and the owner's
        type, never a value.

        Args:
            entity: The entity the finding targets.
            finding (PhiFinding): The instance finding.

        Returns:
            Optional[str]: None when the copy is not owner-stamped; `""`
                when the end state is already there; otherwise a reason (a
                plain str, a `_CopyLeftEmpty` or an `_OwnerNotHandedIn`).
        """
        # An owner's write in this pass reaches the copy and the instance
        # finding folds into it before any arm runs, so a finding here is
        # one whose owner did not write; run anyway, it would write a value
        # no file carries and stamp REMEDIATED over it. No `audit_buffer`:
        # the frozen-surface pin (Pin A) refuses a callee that takes one.
        from .entities import _canonical_tag, exported_patient_id  # pylint: disable=import-outside-toplevel

        owners = (self._copy_owners or {}).get(id(entity))
        if owners is None:
            return None
        patient, study, series = owners
        tag = _canonical_tag(finding.remediation_proposal.target_attr)
        if tag == "0010,0010":
            owner, value = patient, patient.patient_name
        elif tag == "0010,0020":
            owner, value = patient, exported_patient_id(patient)
        elif tag == "0008,0020":
            from .io_handlers import format_study_date  # pylint: disable=import-outside-toplevel
            owner, value = study, format_study_date(study.study_date)
        # The owned UIDs: the export stamps them from the Study and the
        # Series as they stand, so a copy written as the replacement while
        # its owner kept the source would be a file carrying the source
        # beside a graph and a REMEDIATED stamp that said otherwise.
        elif tag == "0020,000d":
            owner, value = study, study.study_instance_uid
        elif tag == "0020,000e":
            owner, value = series, series.series_instance_uid
        else:
            return None
        value = "" if value is None else value
        # The copy already holds what an owner's write left on it -- an
        # earlier pass handed the owner and this one the instance: the
        # end state is there, and a decline would be REVIEW_REQUIRED over
        # nothing. `""` tells the callers so: REPLACE reads it as
        # satisfied, SHIFT runs its own checks.
        if (entity.attributes.get(tag) == value
                and entity.remediation_vouches_for(tag, value)):
            return ""
        if tag in entity.attributes and entity.attributes[tag] != value:
            # The status it had, re-recorded at the revision the write
            # produced, as `_write_to_instances` keeps it: a status at the
            # old revision reads UNSCANNED, which the pass-end demotion
            # would not take to IDENTIFIED.
            status = entity.phi_status
            # Recorded as a remediation's output only when it is one: when
            # the owner's value is what an owner's write left on another
            # of its copies. Usually it is the source value -- the owner
            # declined or was not handed in -- and a record would make the
            # next lock read an original as a replacement, and a later
            # pass read it as already remediated.
            # Every instance under the owner: the walk `_write_to_instances`
            # writes, so "another copy under the same owner" means one thing.
            vouched = any(other.remediation_vouches_for(tag, value)
                          for other in self._instances_beneath(owner) if other is not entity)
            if vouched:
                self._record_what_is_left(entity, tag, value)
            entity.set_attr(tag, value)
            # Not re-recorded when it read UNSCANNED: that would write
            # UNSCANNED over the raw `_phi_status` a stale status leaves,
            # which the pass reads the same, but which the ID-less
            # patient re-key gate (`io_handlers._its_key_is_in_use`) reads
            # as scan evidence, stale included. Equivalent for the pass;
            # not for the gate.
            if status is not PhiStatus.UNSCANNED:
                # Fail-closed: a value no record vouches for, and not
                # empty, is an identifier the policy names -- the source
                # name or date the file carries -- so the instance reads
                # IDENTIFIED, not the REMEDIATED an earlier pass left it,
                # which would grade PASS over a name set back on its
                # Patient after the pass.
                entity.record_phi_status(
                    status if vouched or value == "" else PhiStatus.IDENTIFIED)
            if not (vouched or value == ""):
                # ...and named for the pass-end demotion, as a decline
                # names its entity: the instance's other findings in this
                # pass stamp it REMEDIATED after this, and the scan tally
                # does not demote it -- an earlier pass already acted on
                # this key. Not a decline: no row.
                self._declined_entities.append(entity)
        field = next(f for f, t in self.ENTITY_FIELD_TAGS.items() if t == tag)
        if not {(id(owner), field), (None, field)} & self._owners_handed:
            if value == "" and entity.attributes.get(tag, "") == "":
                # The owner holds no value -- a Study Date the source
                # spelled `1994.11.05`, which no Study holds -- so the
                # export writes the element empty and the copy is empty
                # too: nothing is left to act on, and the finding is
                # satisfied, not left for an owner finding that will
                # never exist; left unhandled, the instance would read
                # IDENTIFIED beside a PASS. A declined owner keeps its
                # row (this is only the not-handed case).
                return _CopyLeftEmpty(tag)
            # The owner was not handed in: no decline row, since nothing
            # declined, and the finding is left unhandled, so the scan
            # tally keeps the instance IDENTIFIED and condition 7 grades
            # the run REVIEW_REQUIRED until the owner is acted on; a
            # later pass handing the owner, then a re-audit, clears it. A
            # row here would be permanent: every decline row ever written
            # grades.
            self.logger.info(
                f"{tag} on {self._log_subject(finding)} follows its "
                f"{type(owner).__name__}, which this pass was not handed")
            return _OwnerNotHandedIn(tag)
        reason = (f"{tag} is written by the export from the "
                  f"{type(owner).__name__}, whose value this pass did not write; "
                  "the instance's copy holds the value the export writes")
        self.logger.warning(
            f"Remediation declined for {self._log_subject(finding)}: {reason}")
        return reason

    def _removal_subject(self, finding: PhiFinding, entity):
        """What a removal's absence is read on.

        Args:
            finding (PhiFinding): The `REMOVE_TAG` finding.
            entity: The finding's own entity.

        Returns:
            `entity` with no session; otherwise the session's live object
            at the finding's address, or None when it has none.
        """
        if self._removal_objects is None:
            return entity
        return self._removal_objects.get(id(finding))

    #: What the session's last `audit()` raised, per scan-time entity uid:
    #: a `_ScanTally`, or None when there is no audit behind the
    #: pass -- hand-built findings, a reopened session -- which keeps the
    #: pass's own accounting. Set by `_use_scan_tally`; a class attribute
    #: for `_instance_owners`' reason.
    _scan_tally = None
    #: Keys of the proposals whose end state the graph already held this
    #: pass, counted as handled by the tally. Rebound, never
    #: mutated, so the class default is safe to share.
    _satisfied_keys = frozenset()
    #: `id(Patient) -> patient_id` as the pass began, for `_live_uid`.
    #: Set with the tally; read-only for `_instance_owners`' reason.
    _pass_start_ids = _MappingProxyType({})
    #: `id(entity) -> its SOP, Study or Series Instance UID` as the pass
    #: began, for `_live_uid`. Set with the tally; read-only.
    _pass_start_uids = _MappingProxyType({})

    def _use_scan_tally(self, tally, findings=()) -> None:
        """Settle this service's passes against `tally`.

        Pass the session's own tally, not a copy: a partial pass leaves the
        keys it handled in it, so the next pass over the same audit
        completes what this one did not. Call it before
        `apply_remediation`, and beside `_use_series`.

        Each patient the findings resolve to has its `patient_id`
        snapshotted here, before the pass can replace it, so a patient
        finding filed under a wrong uid or none is still settled under the
        patient's real one; each other entity's UID is snapshotted likewise
        (`_pass_start_uids`). A patient whose ID an earlier pass already
        replaced is snapshotted as its pseudonym, which the tally does not
        hold, so a mis-named finding on it has no opinion.

        Args:
            tally (_ScanTally): The last audit's tally, or None.
            findings: The pass's findings.
        """
        # Here and not at the top of `apply_remediation`, which would move
        # the five line-cited `mark_modified()` calls.
        self._scan_tally = tally
        self._pass_start_ids = self._MappingProxyType({
            id(f.entity): f.entity.patient_id for f in findings
            if isinstance(f.entity, Patient)})
        # The same for the UIDs the pass may replace.
        self._pass_start_uids = self._MappingProxyType({
            id(f.entity): self._uid_of(f.entity) for f in findings
            if f.entity is not None and not isinstance(f.entity, Patient)})

    #: `(series, its Series Instance UID as the pass began)` for every
    #: series of the session's graph. A Series has
    #: no status; its instances bear its findings, so an incomplete Series
    #: uid demotes them at the pass end. Empty without a session, where a
    #: declined Series still demotes its own instances. Read-only for
    #: `_instance_owners`' reason.
    _series_at_start = ()
    #: The policy the pass end asks each Series under: the one the
    #: session last audited under, else its configuration's.
    _series_policy = None
    #: Each instance's status revision as the pass began, so the pass end
    #: can tell the statuses this pass recorded from the rest.
    _status_revisions_at_start = {}

    def _use_series(self, series, phi_tags) -> None:
        """Snapshot each series and its UID before the pass can replace it,
        each instance's status revision, and the policy a Series' UID is
        judged under at the pass end.

        Call it beside `_use_scan_tally`, before `apply_remediation`.

        Args:
            series: Every Series of the session's graph.
            phi_tags: The policy the session last audited under, else its
                configuration's.
        """
        self._series_at_start = tuple(
            (s, getattr(s, "series_instance_uid", None)) for s in series)
        self._status_revisions_at_start = self._MappingProxyType({
            id(instance): getattr(instance, "_phi_status_revision", None)
            for s, _ in self._series_at_start for instance in s.instances})
        self._series_policy = phi_tags

    def _write_to_instances(self, entity, field: str) -> Optional[Tuple[int, int]]:
        """Write the value a Patient, Study or Series field now holds onto
        each instance beneath it that carries the field's tag.

        The value is the entity's own, read after the arm wrote it: a
        replacement, a shifted date rendered as the exporter renders it
        (`format_study_date`, "YYYYMMDD"), or None, which removes the tag
        from the instance. Only a copy that exists is replaced, and only
        at the top level of `attributes`. Each write is preceded by
        `_record_what_is_left`; a removal deletes the key and marks the
        instance modified. Each copy written is recorded in
        `_owner_copies`, so an instance finding on it later in the pass
        folds into this write (see `_folds_into_owner`).

        Each instance keeps the PHI status it had, re-recorded at the
        revision the write produced; it is not stamped REMEDIATED. An
        instance already UNSCANNED stays so.

        Args:
            entity: The entity the arm wrote.
            field (str): The attribute it wrote.

        Returns:
            Optional[Tuple[int, int]]: `(written, folds)` -- how many
                instance copies were written, and how many instance
                findings in this pass are waiting to fold into them -- or
                None when the write does not apply: `entity` has `set_attr`
                (an item, whose tag the arm wrote directly), or `field` is
                not one the exporter stamps.
        """
        # The value is read back off the entity rather than passed in from
        # the arm: that is the one source the exporter reads too
        # (`export_stamp_attributes`), and it makes the three actions one
        # case. Written as the entity's value, not as a relative shift of
        # the instance's copy: `anonymize(findings=[...])` takes findings
        # in any order, so a copy the instance's own SHIFT_DATE already
        # shifted would move twice. An instance without the tag is not
        # given one: a tag fabricated onto the graph would be a decoy
        # element the file never held. Top-level only, because the
        # exporter's stamp (`_merge(ds, ctx.patient_attributes)`) reaches
        # the dataset root only.
        #
        # The status is re-recorded, not stamped REMEDIATED: the values
        # written here are the module's own replacements, which identify
        # nobody, but `anonymize(findings=[...])` with the patient's
        # findings alone would then vouch for an instance whose own
        # IDENTIFIED findings were never applied. Not left alone either:
        # the write moves the revision, and a status at the old revision
        # reads UNSCANNED, which the manifest reads as not anonymized.
        if hasattr(entity, "set_attr"):
            return None
        tag = self.ENTITY_FIELD_TAGS.get(field)
        if tag is None:
            return None

        value = getattr(entity, field, None)
        if hasattr(value, "strftime"):
            # Lazy: io_handlers is the heavy module, and this is the
            # one spelling of "a Study's date as a DA string".
            from .io_handlers import format_study_date
            value = format_study_date(value)

        # A Patient walks its studies; a Study walks its own series and
        # no sibling's; a Series its own instances.
        written = folds = 0
        for instance in self._instances_beneath(entity):
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
            # This copy now holds the owner's value; an instance finding
            # on it later in the pass folds into this write rather than
            # running. Whether the write removed it: a REMOVE folds
            # only into a removal, anything else only into a value.
            removed = value is None
            self._owner_copies[(id(instance), tag)] = removed
            folds += self._pending_folds.get((id(instance), tag, removed), 0)
            written += 1
        return written, folds

    @staticmethod
    def _entity_findings_first(findings) -> list:
        """The findings with every entity-level one first.

        Entity-level means the finding's entity has no `set_attr`: a
        `Patient`, `Study` or `Series`, whose write reaches the instances'
        copies through `_write_to_instances`. Stable, so each half keeps
        the caller's relative order -- the deepest-first order of
        private-sequence removals included. A finding with no entity sorts
        with the entity-level half; it only declines, so where it sits
        changes nothing.

        Args:
            findings: The pass's findings.

        Returns:
            list: The findings, reordered.
        """
        return sorted(findings, key=lambda f: hasattr(f.entity, "set_attr"))

    def _folds_into_owner(self, finding: PhiFinding) -> bool:
        """Whether an instance finding folds into an owner's write.

        True when an entity-level write earlier in this pass reached
        exactly this copy: the finding's own instance, at the top level,
        on the tag the owner wrote. A folded finding does not run.

        A REMOVE folds only into a write that *removed* the copy, and
        anything else only into one that wrote a value (`_owner_copies`
        records which). An owner that wrote a value does not absorb an
        instance REMOVE, which still runs and removes the copy; one rule
        feeds both levels on every scanned path, so that pairing only
        arises from a hand-built list.

        Three things never fold:

        - **A mismatch of the two** above.
        - **A nested copy.** The owner's write and the exporter's stamp
          reach the dataset root only; a copy inside a sequence is the
          instance scan's to judge.
        - **A copy the owner's write did not reach** -- the owner's own
          finding declined, or was not handed in. `_owner_stamps_copy`
          then decides the finding.

        Args:
            finding (PhiFinding): The instance finding.

        Returns:
            bool: True when the finding folds and must not run.
        """
        proposal = finding.remediation_proposal
        # No `entity_path` check is needed: a nested finding's entity is
        # its sequence item, and `_owner_copies` holds only the instances
        # the owner wrote, so the lookup cannot find one.
        # No "is this an instance?" check either, for the same reason as
        # the nested case: a Patient, a Study or a None entity is never in
        # `_owner_copies`, which holds only the instances the owner wrote
        # -- so the lookup alone decides. `.get` is None for a copy no
        # owner reached, and `None is True/False` is False.
        removed = self._owner_copies.get((id(finding.entity), proposal.target_attr))
        return removed is (proposal.action_type == "REMOVE_TAG")

    @staticmethod
    def _foldable_instance_findings(findings: list) -> dict:
        """Count the instance findings that fold if an owner's write
        reaches their copy.

        Called before the pass, so an owner's audit row can name its folds
        when it is appended. One count per loop key (`_remediation_key`):
        the copy that holds a value counts the key when its first finding
        is not a REMOVE, and the removed copy counts it when any finding
        on it is a REMOVE.

        Args:
            findings (list): The pass's findings.

        Returns:
            dict: `(id(instance), tag, removed) -> count`, where `removed`
                is whether the owner's write removed the copy.
        """
        # Counted up front so no row is rewritten after it is appended
        # (the frozen-surface Pin A refuses that). Only a copy an owner's
        # write reaches is ever asked for its count, so a finding counted
        # here whose copy no owner reaches costs nothing.
        #
        # Per loop key, because that is what the loop runs: it takes each
        # key at most once, and the key carries no action, so of several
        # findings on one key only one can fold. On a copy holding a value,
        # the key's *first* finding ends it: a non-REMOVE folds, and a
        # REMOVE applies and removes the copy, after which the rest are
        # duplicates. On a removed copy every non-REMOVE declines
        # (`_replace_on_item`, `_shift_target_moved`, the arm's bottom
        # `else`) and a decline claims no key, so the key reaches its first
        # REMOVE, which folds. That relies on `_shift_target_moved`
        # declining an absent target: a SHIFT that re-created the copy
        # would claim the key, and its REMOVE would never fold. Counting
        # per `(key, removed)` instead would over-claim: a REMOVE and a
        # REPLACE on one tag would be two folds waiting.
        #
        # A finding that raises claims no key. On a value copy a REMOVE
        # that raises leaves the key to a later non-REMOVE, which folds and
        # is not counted, so the row under-claims by one; before the pass a
        # REMOVE that will raise cannot be told from one that will apply.
        # The copy is the first finding's, which is exact while the
        # findings on a key name one entity; two entities sharing a UID can
        # under-claim the same way.
        chains = {}
        for finding in findings:
            proposal = finding.remediation_proposal
            if not proposal or not hasattr(finding.entity, "set_attr"):
                continue
            remove = proposal.action_type == "REMOVE_TAG"
            # `[copy, first is a REMOVE, any is a REMOVE]`; the copy is the
            # first finding's (see the comment above).
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
        """Resolve the Patient ID a date shift is seeded on.

        The proposal's `metadata["patient_id"]` first, then the entity's
        own non-empty `patient_id`.

        Args:
            entity: The entity being modified.
            proposal (PhiRemediation, optional): The proposal containing
                metadata.

        Returns:
            Optional[str]: The Patient ID, or None when neither holds one.
        """
        # The proposal's metadata first: the scan records the Patient ID
        # the date shift is seeded on there.
        if proposal and proposal.metadata and "patient_id" in proposal.metadata:
            return proposal.metadata["patient_id"]

        if hasattr(entity, "patient_id") and entity.patient_id:
            return entity.patient_id

        return None

    def _get_date_shift(self, patient_id: str, scheme: str) -> int:
        """The deterministic day offset for a patient, within the jitter
        range.

        Seeded on the patient's canonical key
        (`privacy.canonical_patient_key`), so the original Patient ID and
        the pseudonym a pass wrote over it give one offset. For a keyed
        patient the key is derived under the project secret, so the offset
        is the same per patient across stores holding the same secret and
        jitter range, and cannot be computed from anything an export
        carries. With no secret, `canonical_patient_key` raises
        `RuntimeError` for a keyed patient; there is no unkeyed fallback.
        The offset is `key % span + min_days`, with the range's bounds
        swapped if given reversed.

        Args:
            patient_id (str): The seed Patient ID, original or pseudonym.
            scheme (str): The patient's jitter scheme; a legacy patient
                (`JITTER_SCHEME_UNKEYED`) keeps the offset its store
                already gave its dates.

        Returns:
            int: The number of days to shift (positive or negative).
        """
        # `scheme` is required rather than guessed: guessing would give a
        # legacy patient a second offset beside the one its dates already
        # carry.
        val = canonical_patient_key(patient_id, self.project_secret, scheme)

        min_days = self.jitter_config.get("min_days", -365)
        max_days = self.jitter_config.get("max_days", -1)

        if min_days > max_days:
            min_days, max_days = max_days, min_days

        span = max_days - min_days + 1
        if span < 1:
            span = 1

        offset = (val % span) + min_days
        return offset

    #: The shapes `_shift_date_string` shifts, matched whole and
    #: ASCII-only. A DA, or a DT at second precision (optionally with a
    #: fraction) whose first eight digits are its date; the non-standard
    #: dotted DT; and the ISO date and date-time. Hour- and
    #: minute-precision DT are deliberately absent: see "Do not widen the
    #: accept set" in `_shift_date_string`'s body.
    _DA_OR_DT = (r"([0-9]{4})([0-9]{2})([0-9]{2})"
                 r"(?:([0-9]{2})([0-9]{2})([0-9]{2})(?:\.[0-9]{1,6})?)?")
    _DOTTED_DT = (r"([0-9]{4})([0-9]{2})([0-9]{2})"
                  r"\.([0-9]{2})([0-9]{2})([0-9]{2})(?:\.[0-9]+)?")
    _ISO = (r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})"
            r"(?:([ T])([0-9]{1,2}):([0-9]{1,2}):([0-9]{1,2}))?")

    @staticmethod
    def _shift_date_string(date_val, days: int) -> Optional[str]:
        """Shift a date by `days`.

        A None return is what the `SHIFT_DATE` arm records as a decline,
        and `_date_shift_declines` reads as "would decline".

        A `date` or `datetime` is shifted as itself. A string is read by
        shape, and only these shapes shift:

        - `YYYYMMDD` (DA), and a DT that begins with one at second
          precision -- `...HHMMSS`, `...HHMMSS.F` to six fraction digits.
          The date moves; everything after it is re-attached exactly as
          written.
        - The dotted DT `YYYYMMDD.HHMMSS[.F...]`, the same way.
        - ISO `YYYY-MM-DD`, optionally with ` HH:MM:SS` or `THH:MM:SS`,
          rendered zero-padded.

        The time part must be a clock time (hour < 24, minute and second
        < 60), and a shift that leaves years 1-9999 declines. TM-shaped
        values, six- and seven-digit dates, a dotted time that is not six
        digits, and hour- and minute-precision DT (10 or 12 digits) all
        decline.

        Args:
            date_val (Union[str, date, datetime]): The original date value.
            days (int): Delta in days.

        Returns:
            Optional[str]: The shifted value (a `date`/`datetime` for one),
                or None when the value is declined.
        """
        # Matching is by whole-string, length-strict pattern, not
        # `strptime`, whose `%Y%m%d` is not length-strict and would read a
        # TM (`072731`), a six-digit date or an hour-precision DT as a date
        # and fabricate a shifted value.
        #
        # Do not widen the accept set. `_date_shift_declines` answers
        # "would this shift" for the legacy scan branch, which skips what
        # would shift as already shifted, so a wider parser silently skips
        # PHI on a legacy store: a 10- or 12-digit DT such a store can still
        # hold unshifted must keep declining, even though it could be
        # shifted correctly.
        # Local: the patterns are wanted on this path only, and `re`
        # caches their compiled forms.
        import re  # pylint: disable=import-outside-toplevel

        if hasattr(date_val, 'strftime'):
            return date_val + timedelta(days=days)
        if date_val is None:
            return None
        text = str(date_val).strip()

        def moved(year, month, day):
            """Move a calendar date by `days`.

            Args:
                year (str): The year digits.
                month (str): The month digits.
                day (str): The day digits.

            Returns:
                Optional[datetime]: The moved date, or None when the date
                    is invalid or the result leaves years 1-9999.
            """
            try:
                return datetime(int(year), int(month), int(day)) + timedelta(days=days)
            except (ValueError, OverflowError):
                return None

        def is_clock_time(hour, minute, second):
            """Whether the parts form a clock time; a missing part passes.

            Args:
                hour (Optional[str]): The hour digits, or None.
                minute (Optional[str]): The minute digits, or None.
                second (Optional[str]): The second digits, or None.

            Returns:
                bool: True when hour < 24 and minute and second < 60.
            """
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


def _date_shift_declines(value) -> bool:
    """True when the `SHIFT_DATE` arm's parser would leave `value` unshifted.

    Asked by the legacy branch of `PhiInspector._scan_instance`, for an
    instance hydrated from a store with no per-value shift records, which
    keeps the entity-level rule (`Study.date_shifted`): a value the arm
    cannot parse is raised again even while a sibling date on the same
    entity was shifted.

    The answer is the arm's own parser (`_shift_date_string`), so the scan
    re-raises exactly what the arm declines: a DA range, a multi-valued
    DA, a DT with a UTC offset, a TM-shaped value (`072731`) and a
    six-digit date answer True, as `'notadate'` does. Blank (None or
    whitespace) is False, because the arm skips a blank value without a
    decline. The arm's other decline, an unresolvable Patient ID, is not
    modelled: do not rely on this for it.

    Args:
        value: The date value as the scan read it.

    Returns:
        bool: True when the arm would decline to shift `value`.
    """
    # The blank guard stays although the scan's own blank arm sits above
    # the legacy branch: the predicate is also read directly, paired
    # against the arm's blank-value guard, and must agree with it.
    #
    # Not modelling the Patient ID decline is safe. Within one pass one
    # Patient ID seeds every date proposal on a patient, so an
    # unresolvable one declines them all. Across passes, on the legacy
    # path, a Patient ID since emptied makes the arm decline on the ID
    # instead, and the value still ends raised, declined and IDENTIFIED;
    # one replaced rather than emptied seeds the same offset.
    if value is None or not str(value).strip():
        return False
    # The class's own module; the parser is private to it, not to the class.
    return RemediationService._shift_date_string(  # pylint: disable=protected-access
        value, 0) is None


def _remediation_key(finding: PhiFinding) -> tuple:
    """What `apply_remediation` dedupes on, and what the scan tally counts.

    Args:
        finding (PhiFinding): A finding with a remediation proposal.

    Returns:
        tuple: `(entity_uid, entity_path, target_attr)`: the attribute the
            proposal writes, and where it lives.
    """
    # One spelling for both readers, so "already handled" and "what the
    # audit raised" cannot drift apart. See the comment at the dedup in
    # `apply_remediation` for why each part is there.
    return (finding.entity_uid, finding.entity_path,
            finding.remediation_proposal.target_attr)


#: The width of the scan tally's hash-sum.
_TALLY_MASK = (1 << 64) - 1


# Imported here, not at the top: a line added above this module's five
# `mark_modified()` calls moves them, and each is cited by line number.
import hashlib  # pylint: disable=wrong-import-position,wrong-import-order
import json  # pylint: disable=wrong-import-position,wrong-import-order
import uuid  # pylint: disable=wrong-import-position,wrong-import-order


#: One encoder for every key: `json.dumps` with non-default arguments
#: builds a new one per call, which is measurably slower per key.
_KEY_ENCODER = json.JSONEncoder(ensure_ascii=True, separators=(",", ":"))


def _canonical_key(value) -> str:
    """A remediation key as text that is the same in every process.

    JSON for a key it can encode, which keeps `1`, `True`, `"1"` and
    `None` four keys; otherwise `"~"` plus `_typed_key`'s spelling (no
    JSON text begins with `~`).

    Args:
        value: The key.

    Returns:
        str: The canonical text.
    """
    # A scan's key is `(str, tuple of (str, int) pairs, str)`, which JSON
    # spells one way, in C (faster than `_typed_key`). A key JSON refuses
    # can only come from a hand-built finding.
    try:
        return _KEY_ENCODER.encode(value)
    except (TypeError, ValueError):
        return "~" + _typed_key(value)


def _typed_key(value) -> str:
    """The fallback spelling of a key: tagged by type, `repr` for anything
    else.

    For an object with a default `repr` the spelling differs between
    processes, and such a key then fails to match, which the tally reads
    as incomplete (fail-closed), never as complete.

    Args:
        value: The key or a part of it.

    Returns:
        str: The type-tagged text.
    """
    if value is None:
        return "n"
    if isinstance(value, bool):
        return "b1" if value else "b0"
    if isinstance(value, int):
        return f"i{value}"
    if isinstance(value, str):
        return "s" + json.dumps(value, ensure_ascii=True)
    if isinstance(value, tuple):
        return "t(" + ",".join(_typed_key(v) for v in value) + ")"
    return f"o{type(value).__qualname__}:{value!r}"


def _key_hash(key) -> int:
    """64 bits of blake2b over `_canonical_key(key)`.

    Args:
        key: A remediation key.

    Returns:
        int: The hash, little-endian.
    """
    return int.from_bytes(hashlib.blake2b(
        _canonical_key(key).encode("utf-8"), digest_size=8).digest(), "little")


def _key_digest(keys) -> int:
    """The 64-bit sum of `_key_hash(key)` over a set of remediation keys.

    The same in every process.

    Args:
        keys: The remediation keys.

    Returns:
        int: The sum, modulo 2**64.
    """
    # Must not be `hash(key)`: that is salted per process, and a report
    # carries its scan's tally into whatever process reopens the store,
    # where a salted hash would settle every uid False.
    return sum(_key_hash(key) for key in keys) & _TALLY_MASK


class _ScanTally:
    """What the most recent `audit()` raised, per scan-time `entity_uid`.

    `anonymize(findings=...)` applies what it is handed, and each success
    stamps its entity REMEDIATED; the tally is how a pass knows what else
    the audit raised against an entity it touched, so one finding handed
    alone does not leave the entity REMEDIATED over the rest. It holds,
    per uid, the count of distinct remediation keys the audit raised and
    their 64-bit hash-sum, not the keys.

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

    A merged set holding a key the audit did not raise under that uid is
    incomplete (fail-closed): such a key can demote an entity and never
    complete one. That includes a finding from an earlier audit, since the
    tally is the most recent `audit()`'s only: an instance with nothing
    left on it then reads IDENTIFIED until the next `audit()`.

    `audit()` puts a copy on its report (`report._scan_tally`), and a
    report kept across `close()` -- pickled, even -- settles in whatever
    session is handed it as it would have in the session that scanned.
    The tally is not persisted with the store: a reopened session without
    the report has no tally and keeps pass accounting.

    A deliberately partial workflow -- a patient-level pass now, the
    instances later -- keeps the handled key set of every uid it left
    incomplete until the uid completes or the next `audit()` replaces
    the tally. A full pass keeps nothing.

    Each tally carries an audit token (`_audit`, a uuid4 hex) naming the
    audit it came from; pickling, `copy.deepcopy` and `copy()` keep it.

    Args:
        findings: The audit's findings; those without a proposal or an
            `entity_uid` are not counted.
    """

    def __init__(self, findings):
        # Two ints per uid, not the keys: a CT instance raises about two
        # hundred, and holding them for every instance of a large cohort
        # for the life of the session is memory the ordinary path, which
        # handles every key, never reads. Count and hash-sum cannot test
        # containment without the keys, so a superset never completes; the
        # hash-sum only stops a stray key from making up the count in place
        # of a raised key that was not handled (a collision between two
        # distinct sets of equal size is about 2^-64). It is not a secret
        # and not an integrity check. blake2b over a canonical spelling
        # (`_key_hash`), not `hash()`, because the tally crosses processes.
        raised = {}
        for finding in findings:
            if finding.remediation_proposal is None or finding.entity_uid is None:
                continue
            raised.setdefault(finding.entity_uid, set()).add(
                _remediation_key(finding))
        self._raised = {uid: (len(keys), _key_digest(keys))
                        for uid, keys in raised.items()}
        self._partial = {}
        # Which audit this is: what a session
        # keys its working copy of a kept report's tally by, so every
        # report from one audit -- `copy.copy`, `copy.deepcopy`, pickled,
        # or loaded from the same bytes once per step -- drains one copy,
        # as its passes drain one tally in the session that scanned. An
        # attribute, so pickle and deepcopy carry it; `copy()` carries it
        # by hand. uuid4, not a counter: reports from two processes can
        # reach a third, where two counters' tokens would collide.
        self._audit = uuid.uuid4().hex

    def copy(self) -> "_ScanTally":
        """This tally as the audit left it, with no pass's progress, and
        the same audit token (`_audit`).

        What `audit()` puts on its report (`report._scan_tally`), and how
        a session makes its working copy of a kept report's tally on first
        use (`Session._working_tally`), which that session's passes over
        the audit's reports then drain. A pass handed the whole report
        over a pristine copy still completes what an earlier *saved* pass
        applied, whose end state the graph holds. A pass handed only the
        rest of an entity's findings does not: the keys an earlier
        session's pass handled are not in its list, so the entity stays
        IDENTIFIED.

        Returns:
            _ScanTally: A fresh tally with the same raised counts and
                token and no partial progress.
        """
        # Never hand out the session's own tally: it is drained as its
        # passes complete uids, and a report sharing it would name nothing
        # after a reopen of a pass that was never saved.
        fresh = _ScanTally(())
        fresh._raised = dict(self._raised)
        fresh._audit = self._audit
        return fresh

    def raised_under(self, uid) -> bool:
        """Whether the audit raised anything under `uid` that no pass has
        yet completed.

        Read by `Session.anonymize` before a pass, to know which entities
        a kept report's scan speaks for.

        Args:
            uid (str): A scan-time `entity_uid`.

        Returns:
            bool: True while `uid` has raised keys not yet completed.
        """
        return uid in self._raised

    def settle(self, uid, handled) -> Optional[bool]:
        """Whether the keys handled under `uid` complete what was raised.

        The handled keys are merged with those earlier passes over the
        same audit handled. On True the uid is dropped, so a later pass
        over it has no opinion; on False the merged set is kept for a
        later pass.

        Args:
            uid (str): A scan-time `entity_uid`.
            handled (Iterable[tuple]): The remediation keys this pass
                handled under `uid`.

        Returns:
            Optional[bool]: None when nothing is raised under `uid`; True
                when the merged set is exactly the raised set; False
                otherwise.
        """
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


class _CopyLeftEmpty(str):
    """The reason `RemediationService._owner_stamps_copy` gives for an
    owner-stamped copy left empty under an owner holding no value, with
    no owner finding handed in.

    Truthy, so both arms stop, and routed by type in `_record_decline` to
    `_satisfied`. Carries the tag only.
    """


class _OwnerNotHandedIn(str):
    """The reason `RemediationService._owner_stamps_copy` gives for an
    owner-stamped copy whose owner the pass was not handed.

    Truthy, so both arms stop as at a decline, and told apart by type in
    `_record_decline`, which then writes no row. Carries the tag only.
    """
