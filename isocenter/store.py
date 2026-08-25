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
        Returns a list of all unique Equipment (Manufacturer/Model/Serial) found in the store.

        Returns:
            List[Equipment]: A list of unique Equipment objects.
        """
        unique = set()
        for p in self.patients:
            for st in p.studies:
                for se in st.series:
                    if se.equipment:
                        unique.add(se.equipment)
        return list(unique)

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
