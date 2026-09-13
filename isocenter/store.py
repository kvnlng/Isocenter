"""
Root of the Object Graph + Persistence Logic.
"""
import os
import pickle
from typing import Callable, Dict, List, Optional, Set, Tuple

from .entities import Patient, Equipment, PhiStatus, SOURCE_SOP_UID_ATTR
from .logger import get_logger

#: How much a status assures, least first, for a merge (#548): a patient
#: made of several can claim no more than its least-assured member. A
#: member never scanned (or edited since) is below one whose identifiers
#: are known to be gone, and above one whose identifiers are known to be
#: there.
_MERGE_STATUS_RANK = {
    PhiStatus.CLEARED: 0,
    PhiStatus.REMEDIATED: 1,
    PhiStatus.UNSCANNED: 2,
    PhiStatus.IDENTIFIED: 3,
}


class DicomStore:
    """
    Root of the Object Graph + Persistence Logic.

    This class holds the in-memory representation of the DICOM hierarchy
    (List of Patients) and utilities for querying the graph state.
    """

    def __init__(self):
        self.patients: List[Patient] = []

    def _merge_patients_sharing_an_id(
            self, drain: Optional[Callable[[], None]] = None) -> Tuple[int, int]:
        """Make every Patient ID name one `Patient` object again (#548).

        The store keeps one `patients` row per ID (`UNIQUE(patient_id)`),
        so two objects with one ID are one patient whether or not memory
        agrees. They arise when `anonymize()` gives a re-ingested study's
        patient the pseudonym a stored patient already carries, or when
        `recover_patient_identity(restore=True)` puts back an ID a raw
        patient holds. Left in the graph, each object's scoped delete
        removed the other's studies at the next save.

        **Survivor.** The first object in `patients` order: hydrated
        patients precede ingested ones and an earlier ingest precedes a
        later one, so it is the one that came from the store whenever one
        did. It keeps its identity and its `patient_name` (a differing
        name draws one WARNING, counts only) and takes the others'
        studies in order. Each other object is removed from `patients`
        with its `studies` emptied, so a caller still holding one sees a
        detached patient rather than a second parent of the same studies.

        **Status.** The most conservative member's, ranked IDENTIFIED >
        UNSCANNED > REMEDIATED > CLEARED, read from every member before
        anything moves. Recorded on the survivor only when it differs, and
        `record_phi_status` advancing the revision on a change is what
        makes the save write it. The moved studies are not marked: the
        save re-points their rows (`SqliteStore._reparent_studies`), and
        marking them would claim an edit to the study that did not happen.

        **Refusal.** A group whose members carry different jitter schemes
        raises `RuntimeError` before `drain` is called or anything is
        touched. A row holds one scheme, and either choice would give some
        of that subject's dates a second offset or silently re-class a
        legacy patient. Unreachable from `anonymize()` -- a keyed and an
        unkeyed pseudonym differ in length -- and reachable through a
        restore.

        **Offsets need nothing.** The caller runs this after remediation,
        and a pseudonym and its original seed one offset
        (`privacy.canonical_patient_key`).

        Args:
            drain: Called once, only when there is something to merge,
                after the refusal check and before the first mutation.
                The session passes its persistence manager's `flush`: a
                queued save snapshots the list, not the objects, and one
                still holding a duplicate would walk it after its studies
                moved and write its name and status over the survivor's.

        Returns:
            (patients merged away, studies moved).
        """
        groups = self._patients_sharing_an_id()
        if not groups:
            return 0, 0
        self._refuse_a_merge_across_schemes()

        if drain is not None:
            drain()

        merged = moved = renamed = 0
        dropped = set()
        for members in groups.values():
            survivor, others = members[0], members[1:]
            status = max((m.phi_status for m in members),
                         key=_MERGE_STATUS_RANK.__getitem__)
            for other in others:
                if other.patient_name != survivor.patient_name:
                    renamed += 1
                survivor.studies.extend(other.studies)
                moved += len(other.studies)
                other.studies.clear()
                dropped.add(id(other))
                merged += 1
            if survivor.phi_status is not status:
                survivor.record_phi_status(status)
        self.patients[:] = [p for p in self.patients if id(p) not in dropped]

        logger = get_logger()
        if renamed:
            logger.warning(
                f"{renamed} merged patient(s) carried a Patient Name different "
                "from the patient they were merged into; the surviving "
                "patient's name is kept and stamped on every study (#548)")
        logger.info(
            f"Merged {merged} {'patient' if merged == 1 else 'patients'} into "
            "the patient already holding the same Patient ID; "
            f"{moved} {'study' if moved == 1 else 'studies'} moved (#548)")
        return merged, moved

    def _patients_sharing_an_id(
            self, renamed: Optional[Tuple[Patient, str]] = None
    ) -> Dict[str, List[Patient]]:
        """Patient ID -> members, for every ID more than one object holds.

        `renamed` is `(patient, patient_id)`: group as though that patient
        already held that ID, without assigning it.
        """
        groups: Dict[str, List[Patient]] = {}
        for patient in self.patients:
            pid = patient.patient_id
            if renamed is not None and patient is renamed[0]:
                pid = renamed[1]
            groups.setdefault(pid, []).append(patient)
        return {pid: members for pid, members in groups.items()
                if len(members) > 1}

    def _refuse_a_merge_across_schemes(
            self, renamed: Optional[Tuple[Patient, str]] = None) -> None:
        """Raise the #548 `RuntimeError` if a merge would mix jitter schemes.

        `recover_patient_identity` calls it with `renamed` *before* it
        writes the original identifiers back: the merge's own check runs
        after the restore, when a refusal would leave the graph holding
        two patients with one ID under two schemes. The message names a
        count, never an ID.
        """
        mismatched = sum(
            len(members)
            for members in self._patients_sharing_an_id(renamed).values()
            if len({m._jitter_scheme for m in members}) > 1)
        if mismatched:
            raise RuntimeError(
                f"{mismatched} patients in this session share a Patient ID "
                "but were de-identified under different date-offset schemes; "
                "merging them would give their dates two offsets")

    def get_unique_equipment(self) -> List[Equipment]:
        """
        Returns all unique Equipment (Manufacturer/Model/Serial) in the store.

        The result is sorted. `list(set(...))` iterates in hash order, which
        varies between processes because string hashing is randomised, so
        `session.create_config()` emitted the same machines in a different
        order on each run -- a generated file people keep in version control
        and diff. Sorting costs nothing at these sizes and makes the output
        reproducible.

        Returns:
            List[Equipment]: Unique equipment, ordered by manufacturer,
            then model, then serial number.
        """
        unique = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    if se.equipment:
                        unique.add(se.equipment)
        return sorted(
            unique,
            key=lambda e: (e.manufacturer or "", e.model_name or "",
                           e.device_serial_number or ""))

    def get_ingested_paths(self) -> Set[str]:
        """Every file path this store has imported, for ingest de-duplication.

        Keyed on `Instance.source_path`, not `file_path`. It was
        `file_path` until #238, and `regenerate_uid()` clears that, so a
        redacted instance stopped contributing its source path and the
        next `ingest()` of the same folder re-added the un-redacted
        original as a second instance.

        **A path in this set does not mean the file matches the
        instance.** For a redacted instance it means the opposite: the
        file still holds the burned-in identifier. This set answers
        "have I imported this file before" and nothing else -- do not
        reuse it to decide what can be read back off disk. That is what
        `file_path` is for, and it is absent precisely where it would be
        wrong.

        `file_path` is deliberately not consulted as a fallback: no
        production site assigns it after construction (the only two
        assignments set it to `None`), so `Instance.__post_init__` has
        already mirrored it into `source_path`, and a fallback here
        would be dead code re-asserting the reading this docstring
        denies.

        Returns:
            Set[str]: Absolute paths, one per instance that came from a file.
        """
        files = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        if inst.source_path:
                            files.add(os.path.abspath(inst.source_path))
        return files

    def get_superseded_uids(self) -> Dict[str, str]:
        """Pre-redaction identities, mapped to the instance that holds them now.

        `regenerate_uid()` records the SOP Instance UID an instance
        carried before redaction gave it a new one. A file offered to
        `ingest()` under one of these UIDs is the un-redacted original of
        an image this store already holds, reached by a path
        de-duplication did not recognise -- a copy, a move, or a
        symlinked mount. `DicomImporter.import_files` declines it (#238).

        Deliberately narrow. This is *not* "every UID in the store": a
        map that answered that would make the ingest gate refuse every
        re-offered file, including files this store has never seen, and
        would be a second, worse answer to #197.

        Returns:
            Dict[str, str]: pre-redaction UID -> the current SOP Instance
            UID of the instance that recorded it.
        """
        superseded = {}
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        original = inst.attributes.get(SOURCE_SOP_UID_ATTR)
                        if original and original != inst.sop_instance_uid:
                            superseded[original] = inst.sop_instance_uid
        return superseded

    def save_state(self, filepath: str):
        logger = get_logger()
        logger.info(f"Persisting session metadata to {filepath}...")
        with open(filepath, 'wb') as f:
            pickle.dump(self, f)
        logger.info("Saved.")

    @staticmethod
    def load_state(filepath: str) -> 'DicomStore':
        if not os.path.exists(filepath):
            return DicomStore()
        with open(filepath, 'rb') as f:
            return pickle.load(f)
