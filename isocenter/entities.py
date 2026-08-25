import os
from typing import List, Dict, Any, Optional, Callable, Tuple
from dataclasses import dataclass, field
import numpy as np
import pydicom
from pydicom.uid import generate_uid
import isocenter.imagecodecs_handler as h
from .logger import get_logger


@dataclass(slots=True)
class DicomSequence:
    """
    Represents a DICOM Sequence (SQ) containing multiple DicomItems.

    Attributes:
        tag (str): The DICOM tag for this sequence (e.g., "0008,1111").
        items (List[DicomItem]): A list of DicomItem objects contained in this sequence.
    """
    tag: str
    items: List['DicomItem'] = field(default_factory=list)


@dataclass(slots=True)
class TrackedEntity:
    """Tracks whether an entity holds changes the session store does not have.

    This is persistence bookkeeping and nothing else. It says whether the
    object in memory has been written to the session store -- not whether
    it still carries identifiers, which is a separate question with its
    own vocabulary. Both were called "dirty" once, which made them
    indistinguishable in the code and in the output users read.

    State is read through `has_unsaved_changes` and moved through
    `mark_modified()` and `mark_persisted()`. There is deliberately no
    setter: an entity can be told what happened to it, but not told what
    it is. "Declare this saved" is precisely the operation that let a
    rolled-back save leave instances claiming they had been written.

    The revision counter is what makes a concurrent edit survive. A save
    records the revision it actually wrote; an edit arriving while that
    save was in flight leaves the entity ahead of it, so the entity stays
    unsaved rather than being written off by a commit that never
    contained it.
    """

    # Starts at 1 against 0 persisted: anything built in memory is
    # unwritten until a store says otherwise.
    _revision: int = field(init=False, default=1)
    _persisted_revision: int = field(init=False, default=0)

    @property
    def has_unsaved_changes(self) -> bool:
        """Whether this entity holds changes the store does not have."""
        return self._revision > self._persisted_revision

    def mark_modified(self):
        """Records that this entity changed and needs writing again."""
        self._revision += 1

    def mark_persisted(self, revision: Optional[int] = None):
        """Records that a revision of this entity reached the store.

        Args:
            revision (int, optional): The revision that was written.
                Defaults to the current one, which is only correct when
                nothing can have changed since the write. A save that
                takes time should capture the revision before it starts
                and pass that here.
        """
        if revision is None:
            revision = self._revision
        # Never move backwards: an out-of-order or retried save must not
        # un-persist a revision that already reached the store.
        self._persisted_revision = max(self._persisted_revision, revision)

    def mark_subtree_persisted(self):
        """Marks this entity and everything beneath it as stored.

        Used when a whole graph is hydrated from the store, where the
        claim is true of every node at once. `mark_persisted()` speaks for
        one entity only -- committing a single row must not vouch for its
        unsaved siblings.
        """
        self._persisted_revision = self._revision


@dataclass(slots=True)
class DicomItem(TrackedEntity):
    """
    Base class for any entity that holds DICOM attributes and sequences.

    This class provides a dictionary-like interface for managing DICOM attributes
    Persistence state comes from TrackedEntity. Items nested in
    sequences are not stored separately, so the subtree form below reaches
    them.

    Attributes:
        attributes (Dict[str, Any]): A dictionary mapping generic DICOM tags to values.
        sequences (Dict[str, DicomSequence]): A dictionary mapping tags to nested DicomSequences.
    """
    # init=False to avoid constructor conflicts during inheritance
    attributes: Dict[str, Any] = field(init=False)
    sequences: Dict[str, DicomSequence] = field(init=False)

    def __post_init__(self):
        self.attributes = {}
        self.sequences = {}

    def set_attr(self, tag: str, value: Any):
        """
        Sets a generic attribute by its hex tag (e.g., '0010,0010').

        Args:
            tag (str): The DICOM tag string.
            value (Any): The value to set.
        """
        self.attributes[tag] = value
        self.mark_modified()

    def add_sequence_item(self, tag: str, item: 'DicomItem'):
        """
        Appends a new item to a sequence, creating the sequence if needed.

        Args:
            tag (str): The DICOM tag for the sequence.
            item (DicomItem): The item to append.
        """
        if tag not in self.sequences:
            self.sequences[tag] = DicomSequence(tag=tag)
        self.sequences[tag].items.append(item)
        self.mark_modified()

    def mark_subtree_persisted(self):
        """Marks this item and every item nested in its sequences as stored."""
        self._persisted_revision = self._revision
        for seq in self.sequences.values():
            for item in seq.items:
                item.mark_subtree_persisted()


@dataclass(frozen=True, slots=True)
class Equipment:
    """
    Immutable Equipment definition.
    Frozen=True allows hashing, enabling unique set generation.

    Attributes:
        manufacturer (str): The manufacturer of the equipment.
        model_name (str): The model name of the equipment.
        device_serial_number (str): The serial number (optional).
    """
    manufacturer: str
    model_name: str
    device_serial_number: str = ""


# --- Core Hierarchy ---

@dataclass(slots=True)
class Instance(DicomItem):
    """
    Represents a single DICOM image (SOP Instance).
    Manages lazy loading of pixel data.
    """
    sop_instance_uid: str = ""
    sop_class_uid: str = ""
    instance_number: int = 0

    # Persistence: Link to original file for lazy loading
    file_path: Optional[str] = None

    # Transient: Actual pixel data (NOT persisted to pickle)
    pixel_array: Optional[np.ndarray] = field(default=None, repr=False)

    # Transient: Lazy Loader (Callable that returns np.ndarray)
    # Used for Sidecar or deferred logic
    _pixel_loader: Optional[Callable[[], np.ndarray]] = field(default=None, repr=False)

    # Transient: Hash for Integrity Check
    _pixel_hash: Optional[str] = field(default=None, repr=False)

    # Transient: Decoded waveform samples, shape (num_samples, num_channels)
    waveform_array: Optional[np.ndarray] = field(default=None, repr=False)

    # Transient: Lazy loader for waveform samples (sidecar-backed)
    _waveform_loader: Optional[Callable[[], np.ndarray]] = field(default=None, repr=False)

    # Transient: Integrity hash for the raw waveform bytes
    _waveform_hash: Optional[str] = field(default=None, repr=False)

    # Transient: Track if dates have been shifted in memory
    date_shifted: bool = field(default=False, init=False)

    # Transient: Index of all text-based nodes for O(1) PHI scanning
    # List of (DicomItem_Reference, Tag_String)
    text_index: List[Tuple['DicomItem', str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self):
        # Inlined from DicomItem to avoid super() mismatch issues with slots/reloads
        self.attributes = {}
        self.sequences = {}

        self.set_attr("0008,0018", self.sop_instance_uid)
        self.set_attr("0008,0016", self.sop_class_uid)
        self.set_attr("0020,0013", self.instance_number)

    def regenerate_uid(self):
        """
        Generates a new, globally unique SOP Instance UID.

        Call this whenever pixel data is modified to ensure the instance is treated
        as a new distinct entity, preventing collisions with the original data.

        This method:
            1. Generates a new SOP Instance UID.
            2. Updates the internal object property.
            3. Updates the '0008,0018' DICOM attribute.
            4. Detaches the instance from its physical file path (since consistent hash changed).
        """
        # 1. Generate new UID using pydicom's generator (or your org root)
        new_uid = generate_uid()

        # 2. Update the Object Property
        self.sop_instance_uid = new_uid

        # 3. Update the DICOM Attribute Dictionary
        self.set_attr("0008,0018", new_uid)

        # 4. Detach from physical file
        # Since this object is now a "new" instance in memory,
        # it no longer matches the file on disk.
        self.file_path = None

        get_logger().debug(f"  -> Identity regenerated: {new_uid}")

    def unload_pixel_data(self) -> bool:
        """
        Clears the cached pixel_array from memory to free resources.

        Only performs the clear if the data can be re-loaded (i.e., `file_path`
        or `_pixel_loader` is present).

        Returns:
            bool: True if unloaded successfully,
                 False if it was unsafe to unload (data would be lost).
        """
        if self.pixel_array is None:
            return True

        if self.file_path or self._pixel_loader:
            self.pixel_array = None
            # print(f"DEBUG: Unloaded pixels for {self.sop_instance_uid}")
            return True
        else:
            # Data is in memory only (e.g. modified but not saved)
            print(f"DEBUG: FAILED TO UNLOAD {self.sop_instance_uid} - No file path or loader!")
            return False

    def get_pixel_data(self) -> Optional[np.ndarray]:
        """
        Returns pixel_array. Loads from disk if not in memory.

        This method attempts to:
            1. Return already cached `pixel_array`.
            2. Use `_pixel_loader` (Sidecar) if available.
            3. Read from `file_path` using `pydicom`.
            4. Fallback to `isocenter.imagecodecs_handler` if pydicom fails.

        Returns:
            Optional[np.ndarray]: The pixel data as a numpy array, or None if missing/load failed.

        Raises:
            RuntimeError: If loading fails due to transfer syntax issues or missing codecs.
            FileNotFoundError: If the file path does not exist.
        """
        if self.pixel_array is not None:
            return self.pixel_array

        if self._pixel_loader:
            try:
                # Invoke callback (e.g. sidecar read)
                arr = self._pixel_loader()
                # Use set_pixel_data to ensure attributes (rows, cols) are synced
                # This is critical if the loader returns a raw array but
                # attributes were not yet set/restored
                self.set_pixel_data(arr)
                return self.pixel_array
            except Exception as e:
                raise RuntimeError(f"Pixel Loader failed for {self.sop_instance_uid}: {e}") from e

        if self.file_path and os.path.exists(self.file_path):
            try:
                # Read pixel data on demand
                ds = None
                try:
                    ds = pydicom.dcmread(self.file_path)

                    self.set_pixel_data(ds.pixel_array)  # Cache it in memory
                    return self.pixel_array
                except (AttributeError, TypeError):
                    # No pixel data element
                    return None
                except Exception as e:
                    if "no pixel data" in str(e).lower():
                        return None
                    # Re-raise to be handled by outer except
                    raise e

            except Exception as e:
                # Try explicit fallback to isocenter.imagecodecs_handler
                # Pydicom sometimes fails to iterate handlers correctly or swallows errors.
                try:
                    if ds is not None and h.is_available() and h.supports_transfer_syntax(ds.file_meta.TransferSyntaxUID):
                        arr = h.get_pixel_data(ds)
                        self.set_pixel_data(arr)
                        return self.pixel_array
                except (ImportError, AttributeError, RuntimeError):
                    # Fallback failed, proceed to raise original error
                    pass

                # Try to get Transfer Syntax UID for better debugging
                ts_uid = "Unknown"
                if ds is not None and hasattr(ds, "file_meta"):
                    ts_uid = getattr(ds.file_meta, "TransferSyntaxUID", "Unknown")

                if "missing dependencies" in str(e) or "decompress" in str(e):
                    # Enhanced debug output
                    handlers = []
                    try:
                        # pydicom is already imported globally
                        handlers = [str(h) for h in pydicom.config.pixel_data_handlers]
                    except AttributeError:
                        # pydicom 4.0 removes pixel_data_handlers. This is
                        # only decorating an error message, so an empty list
                        # is fine -- but do not let it hide the real one.
                        handlers = []

                    raise RuntimeError(
                        f"Failed to decompress pixel data for {os.path.basename(self.file_path)} "
                        f"(Transfer Syntax: {ts_uid}).\n"
                        f"Underlying Error: {e}\n"
                        f"Active pydicom handlers: {handlers}\n"
                        "Missing image codecs. Please ensure 'pillow', 'pylibjpeg', or 'gdcm' are installed."
                    ) from e

                # If we just caught the re-raised "no pixel data" exception, it would be handled above,
                # but if dcmread fails completely or something else happens:
                raise RuntimeError(f"Lazy load failed for {self.file_path}: {e}") from e

        raise FileNotFoundError(f"Pixels missing and file not found: {self.file_path}")

    def unload_waveform_data(self) -> bool:
        """Clear cached waveform samples to free memory.

        Unloads only when a `_waveform_loader` can restore the samples.
        Deliberately narrower than `unload_pixel_data`, which also accepts
        `file_path` as a recovery route: `get_pixel_data` re-reads the file
        with pydicom as a fallback, but `get_waveform_data` has no such
        fallback -- it returns the cached array, else the loader, else None.
        Accepting `file_path` here would report a safe unload and then hand
        back None forever, which is exactly the silent discard this guard
        exists to prevent.

        Returns:
            bool: True if unloaded (or already absent), False if unsafe --
            i.e. the samples are in memory only and nothing could reload them.
        """
        if self.waveform_array is None:
            return True

        if self._waveform_loader:
            self.waveform_array = None
            return True
        return False

    def get_waveform_data(self) -> Optional[np.ndarray]:
        """Return decoded waveform samples, loading from the sidecar if needed.

        Returns:
            Optional[np.ndarray]: int16 array of shape
            (num_samples, num_channels), or None if this instance has no
            waveform.
        """
        if self.waveform_array is not None:
            return self.waveform_array

        if self._waveform_loader is not None:
            self.waveform_array = self._waveform_loader()
            return self.waveform_array

        return None

    def set_pixel_data(self, array: np.ndarray):
        """
        Sets the pixel array and automatically updates metadata tags.

        Updates tags:
            - Rows (0028,0010)
            - Columns (0028,0011)
            - SamplesPerPixel (0028,0002)
            - NumberOfFrames (0028,0008) (if > 1)
            - PhotometricInterpretation (0028,0004) (RGB if samples >= 3)
            - PlanarConfiguration (0028,0006) (0 if RGB)

        Args:
            array (np.ndarray): The pixel data to set. Can be 1D, 2D, 3D, or 4D.
        """
        self.pixel_array = array
        shape = array.shape
        ndim = len(shape)

        # Defaults
        samples = 1
        frames = 1

        if ndim == 1:
            # Flattened array (e.g. from Sidecar loader)
            # Attempt to reshape using existing metadata if available
            try:
                r = int(self.attributes.get("0028,0010", 0))
                c = int(self.attributes.get("0028,0011", 0))
                s = int(self.attributes.get("0028,0002", 1))
                f = int(self.attributes.get("0028,0008", 1))

                expected_size = r * c * s * f
                if expected_size > 0 and array.size >= expected_size:
                    # Truncate padding if present (DICOM alignment)
                    if array.size > expected_size:
                        array = array[:expected_size]

                    # Reshape logic
                    if f > 1:
                        array = array.reshape((f, r, c, s)) if s > 1 else array.reshape((f, r, c))
                    elif s > 1:
                        array = array.reshape((r, c, s))
                    else:
                        array = array.reshape((r, c))
                    self.pixel_array = array
                    return  # Done, attributes already match
                elif expected_size == 0:
                    # Metadata missing, treat as linear?
                    pass

            except ValueError:
                pass

            # Only raise if we couldn't resolve it
            if len(array.shape) == 1:  # Still 1D
                rows, cols = 1, shape[0]

        elif ndim == 2:
            rows, cols = shape
        elif ndim == 3:
            if shape[-1] in [3, 4]:
                rows, cols, samples = shape
            else:
                frames, rows, cols = shape
        elif ndim == 4:
            frames, rows, cols, samples = shape
        else:
            raise ValueError(f"Unknown shape: {shape}")

        self.set_attr("0028,0010", rows)
        self.set_attr("0028,0011", cols)
        self.set_attr("0028,0002", samples)
        if frames > 1:
            self.set_attr("0028,0008", str(frames))
        if samples >= 3:
            self.set_attr("0028,0004", "RGB")
            self.set_attr("0028,0006", 0)  # Force Interleaved (standard numpy)
        else:
            # Preserve existing PhotometricInterpretation (e.g. MONOCHROME1)
            # Only set default if missing
            if not self.attributes.get("0028,0004"):
                self.set_attr("0028,0004", "MONOCHROME2")

        # Ensure BitsAllocated matches array data type
        # SidecarPixelLoader relies on this to determine uint8 vs uint16
        bits = array.itemsize * 8
        self.set_attr("0028,0100", bits)

        self.mark_modified()


@dataclass(slots=True)
class Series(TrackedEntity):
    """
    Groups Instances by Series Instance UID.
    Typically represents a single scan or reconstruction.

    Attributes:
        series_instance_uid (str): The unique identifier for the series.
        modality (str): The modality type (e.g., 'CT', 'MR').
        series_number (int): The series number.
        equipment (Optional[Equipment]): The equipment used for this series.
        instances (List[Instance]): List of instances belonging to this series.
    """
    series_instance_uid: str
    modality: str
    series_number: int
    equipment: Optional[Equipment] = None
    instances: List[Instance] = field(default_factory=list)

    def mark_subtree_persisted(self):
        """Marks this series and every instance beneath it as stored."""
        self._persisted_revision = self._revision
        for instance in self.instances:
            instance.mark_subtree_persisted()


@dataclass(slots=True)
class Study(TrackedEntity):
    """
    Groups Series by Study Instance UID.
    Represents a single patient visit or examination.

    Attributes:
        study_instance_uid (str): The unique identifier for the study.
        study_date (Any): The date of the study.
        series (List[Series]): List of series belonging to this study.
        date_shifted (bool): Whether dates in this study have been shifted.
        study_time (Optional[str]): The time of the study.
    """
    study_instance_uid: str
    study_date: Any
    series: List[Series] = field(default_factory=list)
    date_shifted: bool = False
    study_time: Optional[str] = None

    def mark_subtree_persisted(self):
        """Marks this study and every series beneath it as stored."""
        self._persisted_revision = self._revision
        for series in self.series:
            series.mark_subtree_persisted()


@dataclass(slots=True)
class Patient(TrackedEntity):
    """
    Root of the object hierarchy. Groups Studies by Patient ID.

    Attributes:
        patient_id (str): The primary patient identifier.
        patient_name (str): The patient's name.
        studies (List[Study]): List of studies belonging to this patient.
    """
    patient_id: str
    patient_name: str
    studies: List[Study] = field(default_factory=list)

    def mark_subtree_persisted(self):
        """Marks this patient and every study beneath it as stored."""
        self._persisted_revision = self._revision
        for study in self.studies:
            study.mark_subtree_persisted()
