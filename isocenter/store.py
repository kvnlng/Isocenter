"""
Root of the Object Graph + Persistence Logic.
"""
import os
import pickle
from typing import List, Set

from .entities import Patient, Equipment
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

    def get_known_files(self) -> Set[str]:
        """
        Returns a set of absolute file paths for all instances currently indexed.

        Returns:
            Set[str]: A set of file path strings.
        """
        files = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    for inst in se.instances:
                        if inst.file_path:
                            files.add(os.path.abspath(inst.file_path))
        return files

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
