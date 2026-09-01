"""
Root of the Object Graph + Persistence Logic.
"""
import os
import pickle
from typing import Dict, List, Set

from .entities import Patient, Equipment, SOURCE_SOP_UID_ATTR
from .logger import get_logger


class DicomStore:
    """
    Root of the Object Graph + Persistence Logic.

    This class holds the in-memory representation of the DICOM hierarchy
    (List of Patients) and utilities for querying the graph state.
    """

    def __init__(self):
        self.patients: List[Patient] = []

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
