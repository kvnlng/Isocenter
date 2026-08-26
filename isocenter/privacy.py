from dataclasses import dataclass, field
from typing import List, Any, Optional, Dict, Tuple
import hashlib
from .entities import Patient, Study, Instance, iter_item_tree
from .logger import get_logger


@dataclass(slots=True)
class PhiRemediation:
    """
    Proposed action to fix a PHI finding.

    Attributes:
        action_type (str): The remediation logic code (e.g., "REPLACE_TAG", "SHIFT_DATE").
        target_attr (str): The attribute or tag to modify.
        new_value (Any): The proposed new value (if known).
        original_value (Any): The original value for audit/reversion.
        metadata (Dict[str, Any]): Context metadata (e.g. patient linkage for date shifting).
    """
    action_type: str  # e.g., "REPLACE_TAG", "REDACT_REGION"
    target_attr: str  # e.g., "patient_name", "study_date"
    new_value: Any = None
    original_value: Any = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PhiFinding:  # pylint: disable=too-many-instance-attributes
    """
    Represents a potential PHI breach discovered during a scan.

    The attribute count is the point of the record -- it is a report line,
    and every field below is something a reader of that report needs. The
    same reasoning that disables `too-few-public-methods` for DTOs in
    `pylintrc.toml` applies here; splitting it would only move fields
    behind another name.

    Attributes:
        entity_uid (str): Unique identifier of the entity (PatientID, SOPInstanceUID).
        entity_type (str): Type of entity ("Patient", "Instance", etc).
        field_name (str): The specific field or tag description.
        value (Any): The PHI value found.
        reason (str): Why this was flagged (e.g. "Safe Harbor Rules").
        tag (Optional[str]): The DICOM tag (e.g., "0010,0010").
        patient_id (Optional[str]): Linkage for context.
        entity (Any): Reference to the Python object for direct remediation.
        remediation_proposal (Optional[PhiRemediation]): The suggested fix.
        entity_path (Tuple): Route from the Instance to the item this was
            raised against, as `(sequence_tag, index)` steps. Empty means
            the Instance itself. Findings cross a process boundary, and a
            sequence item has no UID to be found again by, so this is the
            only way to rebind one to the item it actually came from.
    """
    entity_uid: str
    entity_type: str
    field_name: str
    value: Any
    reason: str
    tag: Optional[str] = None  # specific DICOM tag if applicable
    patient_id: Optional[str] = None  # Added for linkage
    entity: Any = None  # Reference to the actual object (Patient, Study, etc.)
    remediation_proposal: Optional[PhiRemediation] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    entity_path: Tuple = ()  # Route from the Instance down to a sequence item


class PhiReport:
    """
    A container for PHI findings that supports analysis and export.

    Acts as a list wrapper for backward compatibility but enables
    DataFrame export features.
    """

    def __init__(self, findings: List[PhiFinding]):
        self.findings = findings

    def to_dataframe(self):
        """
        Converts findings to a Pandas DataFrame for analysis.

        Returns:
            pd.DataFrame: A dataframe containing flattened finding details.

        Raises:
            ImportError: If pandas is not installed.
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "Pandas is required for this feature. Install it with `pip install pandas`.")

        data = []
        for f in self.findings:
            row = {
                "patient_id": f.patient_id,
                "entity_type": f.entity_type,
                "entity_uid": f.entity_uid,
                "tag": f.tag,
                "field": f.field_name,
                "value": str(f.value),
                "reason": f.reason,
                "action": f.remediation_proposal.action_type if f.remediation_proposal else None
            }
            data.append(row)
        return pd.DataFrame(data)

    def __iter__(self):
        return iter(self.findings)

    def __len__(self):
        return len(self.findings)

    def __getitem__(self, index):
        return self.findings[index]

    def __repr__(self):
        return f"<PhiReport: {len(self.findings)} findings>"


class PhiInspector:
    """
    Scans the Object Graph for attributes that are known to contain Protected Health Information (PHI).

    Implements rules based on HIPAA Safe Harbor identifiers and Configurable PHI Tags.
    Also handles Private Tag scanning/flagging.
    """

    def __init__(self,
                 config_path: str = None,
                 config_tags: Dict[str,
                                   str] = None,
                 remove_private_tags: bool = False):
        """
        Initializes the inspector.

        Args:
            config_path (str, optional): Path to a JSON/YAML config file.
            config_tags (Dict[str, Union[str, Dict]], optional): Direct
                config dictionary, taking precedence over `config_path`.
                A tag's value is either a **rule** --
                `{"name": ..., "action": "REMOVE"|"EMPTY"|"SHIFT"|"JITTER"}`
                -- or a plain string, which is the tag's **display name**
                and leaves the action as `REPLACE`. The string form names
                the tag; it does not choose what happens to it. Passing a
                bare action name (`{"0008,0020": "SHIFT"}`) therefore
                replaces the value instead of shifting it, and is warned
                about at construction (#111).
            remove_private_tags (bool): If True, scans all attributes for non-whitelisted private tags.
        """
        from .config_manager import ConfigLoader

        self.remove_private_tags = remove_private_tags

        if config_tags is not None:
            self.phi_tags = config_tags
        elif config_path:
            self.phi_tags = ConfigLoader.load_phi_config(config_path)
        else:
            self.phi_tags = ConfigLoader.load_phi_config()

        # Normalize tag-key casing HERE, once, at the boundary between
        # "however phi_tags got built" (a built-in PRIVACY_PROFILES entry,
        # a user's own YAML, an external custom-profile file, or the
        # shipped default resource) and "how it's looked up"
        # (`_scan_instance`'s `self.phi_tags.get(tag)` below). Every
        # ingested attribute key on the object graph is lowercased
        # (`io_handlers.py`'s `populate_attrs`,
        # `f"{elem.tag.group:04x},{elem.tag.element:04x}"`), so a config
        # source that spells a tag with an uppercase hex letter (A-F) --
        # as `isocenter/profiles.py`'s Basic profile did for Series
        # Description, `0008,103E` -- would otherwise never match and
        # silently disable a declared policy with no error anywhere.
        # Normalizing at this single choke point, rather than fixing the
        # one known offending key, means this class of bug cannot recur
        # regardless of which future profile or config file introduces it.
        self.phi_tags = self._normalize_tag_keys(self.phi_tags)
        self._warn_on_bare_action_values()

    # Action names a caller is most likely to write where a description
    # belongs, having read `Dict[str, str]` and reasonably concluded the
    # string chooses the behaviour.
    _ACTION_WORDS = frozenset({"REMOVE", "EMPTY", "SHIFT", "JITTER", "REPLACE"})

    def _warn_on_bare_action_values(self) -> None:
        """Report tags whose value names an action rather than the tag.

        A string value is the tag's display name and always leaves the
        action as `REPLACE`, so `{"0008,0020": "SHIFT"}` replaces the date
        with `ANONYMIZED` instead of shifting it -- destroying the
        interval information shifting exists to preserve, with nothing
        raised and nothing logged.

        Warned rather than raised: the string form works as designed, and
        rejecting a call that succeeds today would be a breaking change.
        A caller may also legitimately have a tag *described* as "Shift".
        """
        offenders = sorted(
            tag for tag, val in self.phi_tags.items()
            if isinstance(val, str) and val.strip().upper() in self._ACTION_WORDS)
        if not offenders:
            return

        for tag in offenders:
            get_logger().warning(
                "config_tags[%r] is %r, which is read as the tag's display "
                "name, not its action -- the action stays REPLACE. To %s "
                "this tag, write {'action': %r, 'name': ...}.",
                tag, self.phi_tags[tag], self.phi_tags[tag].strip().lower(),
                self.phi_tags[tag].strip().upper())

    @staticmethod
    def _normalize_tag_keys(phi_tags: Dict[str, Any]) -> Dict[str, Any]:
        """Lowercase every string PHI-tag key so it matches the
        lowercased 'gggg,eeee' keys the object graph actually uses.

        Non-string keys (defensive only -- config_tags is caller-supplied
        and its shape isn't strictly validated elsewhere) pass through
        unchanged.
        """
        if not phi_tags:
            return phi_tags

        normalized: Dict[str, Any] = {}
        for key, value in phi_tags.items():
            norm_key = key.lower() if isinstance(key, str) else key
            if norm_key in normalized:
                # Two source keys collided after lowercasing (e.g. both
                # "0008,103E" and "0008,103e" were present). Last one
                # wins, matching ordinary dict.update()/dict-literal
                # overwrite semantics -- but log it, since a silent
                # overwrite here is exactly the kind of thing this
                # normalization exists to make loud.
                get_logger().warning(
                    "PHI tag key collision after case normalization: "
                    "%r overwrites the earlier entry for %r.", key, norm_key)
            normalized[norm_key] = value
        return normalized

    def scan_patient(self, patient: Patient) -> List[PhiFinding]:
        """
        Recursively scans a Patient and their child studies for PHI.

        Args:
            patient (Patient): The patient object to scan.

        Returns:
            List[PhiFinding]: A list of all identified PHI findings.
        """
        findings = []

        # 1. Direct Attributes
        if patient.patient_name and patient.patient_name != "Unknown" and patient.patient_name != "ANONYMIZED":
            proposal = PhiRemediation(
                action_type="REPLACE_TAG",
                target_attr="patient_name",
                new_value="ANONYMIZED",
                original_value=patient.patient_name
            )
            findings.append(PhiFinding(
                entity_uid=patient.patient_id,
                entity_type="Patient",
                field_name="patient_name",
                value=patient.patient_name,
                reason="Names are PHI",
                tag="0010,0010",
                patient_id=patient.patient_id,
                entity=patient,
                remediation_proposal=proposal
            ))

        if patient.patient_id and patient.patient_id != "UNKNOWN" and not patient.patient_id.startswith(
                "ANON_"):
            # Simple deterministic anonymization proposal for now (can be refined in Service)
            # The Service will handle the hash calculation if 'new_value' is a
            # placeholder or if logic dictates
            hashed_id = f"ANON_{hashlib.sha256(patient.patient_id.encode()).hexdigest()[:12]}"
            proposal = PhiRemediation(
                action_type="REPLACE_TAG",
                target_attr="patient_id",
                new_value=hashed_id,
                original_value=patient.patient_id
            )
            findings.append(PhiFinding(
                entity_uid=patient.patient_id,
                entity_type="Patient",
                field_name="patient_id",
                value=patient.patient_id,
                reason="Medical Record Numbers are PHI",
                tag="0010,0020",
                patient_id=patient.patient_id,
                entity=patient,
                remediation_proposal=proposal
            ))

        # 2. Traverse Children & Scan Instances (Generic Unified Config)
        for study in patient.studies:
            findings.extend(self._scan_study(study, patient.patient_id))

            for series in study.series:
                for instance in series.instances:
                    findings.extend(self._scan_instance(instance, patient.patient_id, study=study))

        return findings

    def _scan_instance(self, instance: Instance, patient_id: str,
                       study: Study = None) -> List[PhiFinding]:
        """
        Scans a single instance for PHI based on configured tags and private tag rules.

        Uses cached `text_index` (if available) for O(1) access to all text nodes,
        including nested sequence items.
        """
        findings = []

        # 0. Determine Scan Targets
        # Walked from the instance itself rather than read off its
        # `text_index`. That index is built once at ingest and is not
        # rebuilt when a session is loaded from the store, nor carried
        # into the worker copies `session.audit()` scans -- so every route
        # into this method except a direct call arrived with it empty and
        # took a top-level-only scan, reporting clean on sequence content
        # it never opened (#57).
        #
        # Walking also drops the index's text-VR filter, which the
        # top-level path never applied either. A configured PHI tag is a
        # configured PHI tag wherever it sits and whatever its VR.
        scan_targets = [
            (item, tag, path)
            for item, path in iter_item_tree(instance)
            for tag in list(item.attributes.keys())
        ]

        # 1. Private Tag Removal Logic
        if self.remove_private_tags:
            # Private tags live in odd groups. Remove all of them except
            # the two the reversibility service owns -- (0099,0010) is its
            # Private Creator and (0099,1001) holds the encrypted
            # identities, so stripping them would destroy the ability to
            # de-anonymize with the key.
            WHITELIST_TAGS = {"0099,0010", "0099,1001"}

            for item, tag, path in scan_targets:
                try:
                    group_str, _ = tag.split(',')
                    group = int(group_str, 16)
                    if group % 2 != 0:  # Odd group = Private
                        if tag not in WHITELIST_TAGS:
                            findings.append(PhiFinding(
                                entity_uid=instance.sop_instance_uid,
                                entity_type="Instance",
                                field_name=f"Private Tag {tag}",
                                value="<PRIVATE>",
                                reason="Private Tag Removal Requested",
                                tag=tag,
                                patient_id=patient_id,
                                entity=item,
                                entity_path=path,
                                remediation_proposal=PhiRemediation(
                                    action_type="REMOVE_TAG",
                                    target_attr=tag
                                )
                            ))
                except ValueError:
                    pass  # Malformed tag?

        # 2. Configured PHI Tags
        if not self.phi_tags:
            return findings

        for item, tag, path in scan_targets:
            # Parse config
            config_val = self.phi_tags.get(tag)
            if not config_val:
                continue

            if isinstance(config_val, dict):
                description = config_val.get("name", "Unknown Tag")
                action_code = config_val.get("action", "REPLACE").upper()
            else:
                description = str(config_val)
                action_code = "REPLACE"

            # Check if tag exists in item items
            val = item.attributes.get(tag)

            if val is None:
                continue

            # Determine if remediation is needed
            needs_remediation = False
            remediation_action = "REPLACE_TAG"
            new_val = None

            if action_code == "REMOVE":
                # If user wants it gone, and it exists (val is not None), finding!
                needs_remediation = True
                remediation_action = "REMOVE_TAG"
            elif action_code == "EMPTY":
                if val != "":
                    needs_remediation = True
                    remediation_action = "REPLACE_TAG"
                    new_val = ""
            elif action_code in ["SHIFT", "JITTER"]:
                # Date Shifting
                # If instance or its parent study is already shifted, this is not a finding
                is_shifted = False
                if hasattr(instance, "date_shifted") and instance.date_shifted:
                    is_shifted = True
                elif study and hasattr(study, "date_shifted") and study.date_shifted:
                    is_shifted = True

                if is_shifted:
                    needs_remediation = False
                else:
                    needs_remediation = True
                    remediation_action = "SHIFT_DATE"
            elif action_code == "KEEP":
                needs_remediation = False
            else:  # REPLACE (Default)
                if val != "ANONYMIZED" and val != "":
                    needs_remediation = True
                    remediation_action = "REPLACE_TAG"
                    new_val = "ANONYMIZED"

            if needs_remediation:
                proposal = PhiRemediation(
                    action_type=remediation_action,
                    target_attr=tag,
                    new_value=new_val,
                    original_value=val,
                    metadata={
                        "patient_id": patient_id} if remediation_action == "SHIFT_DATE" else {})

                findings.append(PhiFinding(
                    entity_uid=instance.sop_instance_uid,
                    entity_type="Instance",
                    field_name=f"{description} (Deep)" if item != instance else description,
                    value=val,
                    reason=f"Matched PHI Tag {tag} ({description})",
                    tag=tag,
                    patient_id=patient_id,
                    entity=item,  # Point to the specific deep item!
                    entity_path=path,
                    remediation_proposal=proposal
                ))
        return findings

    def _scan_study(self, study: Study, patient_id: str = None) -> List[PhiFinding]:
        """
        Scans a Study entity for study-level PHI (e.g. StudyDate).
        """
        findings = []
        uid = study.study_instance_uid

        # If successfully remediated (shifted), do not flag as PHI again
        if hasattr(study, "date_shifted") and study.date_shifted:
            return findings

        if study.study_date:
            proposal = PhiRemediation(
                action_type="SHIFT_DATE",  # Special action for the Service to handle
                target_attr="study_date",
                original_value=study.study_date,
                metadata={"patient_id": patient_id}
            )
            findings.append(PhiFinding(
                entity_uid=uid,
                entity_type="Study",
                field_name="study_date",
                value=study.study_date,
                reason="Dates are Safe Harbor restricted",
                tag="0008,0020",
                patient_id=patient_id,
                entity=study,
                remediation_proposal=proposal
            ))

        return findings
