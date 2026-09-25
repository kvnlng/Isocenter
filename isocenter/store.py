"""
Root of the Object Graph + Persistence Logic.
"""
import os
import pickle
from typing import Callable, Dict, List, Optional, Set, Tuple

from .entities import Patient, Equipment, PhiStatus, SOURCE_SOP_UID_ATTR
from .logger import get_logger

#: How much a status assures, least first, for a merge: a patient
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
        """Make every Patient ID name one `Patient` object again.

        Two objects holding one Patient ID are one patient to the store,
        which keeps one row per ID; left in the graph, each object's scoped
        delete would remove the other's studies at the next save. Call it
        after remediation.

        The survivor is the first object in `patients` order, so a patient
        hydrated from the store survives over one ingested since. It keeps
        its identity and `patient_name` and takes the others' studies in
        order; a differing name logs one WARNING with counts only. Each
        other object is removed from `patients` with its `studies` emptied.
        The survivor's status becomes the least assured of the group's
        (IDENTIFIED > UNSCANNED > REMEDIATED > CLEARED) under that member's
        policy, recorded only when it differs. Logs one INFO line with the
        counts.

        Args:
            drain: Called once, only when there is something to merge,
                after the refusal check and before the first mutation.
                The session passes its persistence manager's `flush`, so
                no queued save walks a duplicate after its studies moved.

        Returns:
            Tuple[int, int]: (patients merged away, studies moved).

        Raises:
            RuntimeError: A group's members carry different jitter schemes.
                Raised before `drain` is called or anything is touched.
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
            # The member whose status is kept, not only the status: the
            # status is recorded under that member's policy, which
            # the survivor's own would misstate. `max` returns the first
            # of equals, so a tie on the worst status under two policies
            # keeps the earliest member's, in `members` order.
            kept = max(members,
                       key=lambda m: _MERGE_STATUS_RANK[m.phi_status])
            status, policy = kept.phi_status, kept.phi_status_policy
            # The moved studies are not marked modified: the save
            # re-points their rows (`SqliteStore._reparent_studies`), and
            # marking them would claim an edit to the study that did not
            # happen. A changed status is written because
            # `record_phi_status` advances the survivor's revision.
            for other in others:
                if other.patient_name != survivor.patient_name:
                    renamed += 1
                survivor.studies.extend(other.studies)
                moved += len(other.studies)
                other.studies.clear()
                dropped.add(id(other))
                merged += 1
            if (survivor.phi_status is not status
                    or survivor.phi_status_policy != policy):
                survivor.record_phi_status(status, policy=policy)
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

        Args:
            renamed: `(patient, patient_id)`: group as though that patient
                already held that ID, without assigning it. None groups
                the graph as it stands.

        Returns:
            Dict[str, List[Patient]]: Each shared ID mapped to its members,
            in `patients` order.
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
        """Refuse a merge that would mix jitter schemes.

        `recover_patient_identity` calls it with `renamed` before it writes
        the original identifiers back, so a refusal leaves the graph
        untouched.

        Args:
            renamed: `(patient, patient_id)`: check as though that patient
                already held that ID. None checks the graph as it stands.

        Raises:
            RuntimeError: Patients sharing a Patient ID carry different
                jitter schemes. The message names a count, never an ID.
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

        The order is stable across runs, so `session.create_config()`
        lists the same machines in the same order every time.

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
        # Sorted: set iteration order varies between processes because
        # string hashing is randomised.
        return sorted(
            unique,
            key=lambda e: (e.manufacturer or "", e.model_name or "",
                           e.device_serial_number or ""))

    def get_ingested_paths(self) -> Set[str]:
        """Every file path this store has imported, for ingest de-duplication.

        Read from each instance's `source_path`, so a redacted instance,
        whose `file_path` is cleared, still names the file it came from.

        **A path in this set does not mean the file matches the
        instance.** For a redacted instance it means the opposite: the
        file still holds the burned-in identifier. This set answers
        "have I imported this file before" and nothing else; use
        `file_path` to decide what can be read back off disk.

        Returns:
            Set[str]: Absolute paths, one per instance that came from a file.
        """
        # Keyed on `source_path`, not `file_path`: `regenerate_uid()` clears
        # `file_path`, and keying on it would let the next `ingest()` of the
        # same folder re-add a redacted instance's original. No fallback to
        # `file_path` is needed: `Instance.__post_init__` mirrors it into
        # `source_path`, and nothing assigns it a path afterwards.
        files = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        if inst.source_path:
                            files.add(os.path.abspath(inst.source_path))
        return files

    def get_superseded_uids(self) -> Dict[str, str]:
        """The identities instances were ingested under, mapped to the
        instance that holds them now.

        Holds the SOP Instance UID an instance carried before redaction
        (`regenerate_uid()`) or UID replacement at `anonymize()` first gave
        it a new one. A file offered to `ingest()` under one of these UIDs
        is the source of an image this store already holds, reached by a
        path de-duplication did not recognise; `DicomImporter.import_files`
        declines it. Only superseded UIDs are included, not every UID in
        the store.

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
        """Pickles this store to `filepath`, overwriting any file there.

        Args:
            filepath: The file to write.
        """
        logger = get_logger()
        logger.info(f"Persisting session metadata to {filepath}...")
        with open(filepath, 'wb') as f:
            pickle.dump(self, f)
        logger.info("Saved.")

    @staticmethod
    def load_state(filepath: str) -> 'DicomStore':
        """Unpickles a store from `filepath`.

        Only load a file you trust: unpickling can run arbitrary code.

        Args:
            filepath: The file `save_state` wrote.

        Returns:
            DicomStore: The loaded store, or a new empty one when no file
            exists at `filepath`.
        """
        if not os.path.exists(filepath):
            return DicomStore()
        with open(filepath, 'rb') as f:
            return pickle.load(f)
