import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import pydicom
from pydicom.uid import generate_uid
import isocenter.imagecodecs_handler as h
from .logger import get_logger
from .pixel_geometry import (
    GeometryEvidence,
    declared_int,
    planar_configuration_default,
    resolve_photometric_interpretation,
    resolve_pixel_geometry,
)


def _canonical_tag(tag: str) -> str:
    """The one spelling of a `"gggg,eeee"` key: lowercase hex.

    Non-strings pass through untouched -- this normalises casing and
    nothing else, so a caller who has wandered off the string-tag
    convention still gets whatever error their own key would have
    caused, rather than an AttributeError from here.
    """
    return tag.lower() if isinstance(tag, str) else tag


#: Attribute key under which an instance records the SOP Instance UID it
#: carried before `regenerate_uid()` first replaced it.
#:
#: Assigned into `attributes` **directly, never through `set_attr`** --
#: `set_attr` runs the key through `_canonical_tag`, which lowercases it,
#: and every reader spells it upper-case. `_ISOCENTER_REDACTION_HASH` is
#: written the same way in `services.py` for the same reason; it stays a
#: bare literal at its five sites there because renaming them is not this
#: fix.
SOURCE_SOP_UID_ATTR = "_ISOCENTER_SOURCE_SOP_UID"


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


class PhiStatus(Enum):
    """What the last scan concluded about an entity, and when.

    This answers a different question from `has_unsaved_changes`, which is
    persistence bookkeeping. Both used to be called "dirty".

    A status is only ever valid for the revision it was computed at. Edit
    the entity and it reverts to UNSCANNED, because a conclusion drawn
    about earlier content says nothing about the current content -- and a
    stale REMEDIATED reads as an assurance, which is worse than admitting
    nothing is known.
    """

    #: Never inspected, or inspected before the entity's current revision.
    UNSCANNED = "unscanned"

    #: The scan found identifiers here, and nothing has acted on them.
    IDENTIFIED = "identified"

    #: Identifiers were found and remediation was applied.
    REMEDIATED = "remediated"

    #: The scan found no identifiers. This means the configured *tag* scan
    #: found nothing -- not that burned-in pixel text was checked, which is
    #: a separate scan, and not that the entity is approved for release.
    CLEARED = "cleared"


# `eq=False` on this base and on every graph entity below it is
# load-bearing (#299). The trap is that `eq=True` is the dataclass
# DEFAULT, so "a dataclass that defines no `__eq__`" is not what it
# reads as: the decorator generates a field-by-field `__eq__` and sets
# `__hash__ = None`, leaving graph entities unhashable and comparing two
# distinct records as equal whenever their fields happen to match --
# which is the ordinary case here, since fixture builders and
# `_make_lightweight_copy` produce field-equal siblings by design.
#
# It must be spelled on the BASES, not just the leaves. A subclass
# carrying `eq=False` inherits the base's value `__eq__` *and* its
# `__hash__ = None` through the MRO, so it stays unhashable *and* starts
# comparing on the base's fields ONLY -- the leaf's own identifiers drop
# out of the comparison entirely. Measured on the four-leaf form:
# `Series("S1", "CT", 1) == Series("S2", "MR", 9)` is True, because
# `TrackedEntity` holds nothing but revision counters. (`Instance`
# happens to escape that particular collapse only because its
# `__post_init__` mirrors the UID into `attributes`, which *is* a base
# field -- an accident, not a defence.) Spelling `eq=False` on the four
# leaves alone is therefore strictly worse than leaving the default.
#
# `Equipment` is deliberately excluded: it is `frozen=True` precisely so
# that value-hashing works, which is how unique equipment sets are built
# (`tests/test_verification_logic.py`).
@dataclass(slots=True, eq=False)
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

    # -1 matches no revision, so an entity that has never been scanned
    # reports UNSCANNED without needing a separate "was it scanned" flag.
    _phi_status: 'PhiStatus' = field(init=False, default=None)
    _phi_status_revision: int = field(init=False, default=-1)

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

    @property
    def phi_status(self) -> 'PhiStatus':
        """What the last scan concluded, if it still applies.

        Returns UNSCANNED when the entity has changed since the scan ran.
        The check is structural rather than a convention someone has to
        remember: there is no way to read a status that describes content
        the entity no longer holds.
        """
        if self._phi_status is None or self._phi_status_revision != self._revision:
            return PhiStatus.UNSCANNED
        return self._phi_status

    def record_phi_status(self, status: 'PhiStatus'):
        """Records what a scan concluded about this entity's current state.

        Call this *after* any change the status describes -- remediation
        modifies the entity, so recording REMEDIATED first would stamp a
        revision the entity immediately leaves behind.

        A new status is a change to what the store should hold, so it
        advances the revision and leaves the entity with unsaved changes.
        Without that, a scan of an already-saved session would record
        statuses that the next save had no reason to write. Recording the
        status an entity already carries changes nothing and is ignored,
        so repeated scans of unchanged data do not force a rewrite.

        That rule has a scope worth stating, because it reads as
        harmless and is not. Being ignored means a change that does not
        itself move the revision is invisible to the status. A graph
        loaded from the store carries whatever conclusion was stored for
        each entity, so an entity already remediated once comes back at
        REMEDIATED -- and a second remediation then records the status it
        already has, which is to say records nothing and advances
        nothing.
        The `mark_modified()` calls in `remediation.py` are what keep it
        saveable -- they look redundant because on a first remediation
        this method's bump would have covered them, and after a reload
        they are the only bump there is (#173).
        """
        if self.phi_status is status:
            return
        self._revision += 1
        self._phi_status = status
        self._phi_status_revision = self._revision

    def mark_subtree_persisted(self):
        """Marks this entity and everything beneath it as stored.

        Used when a whole graph is hydrated from the store, where the
        claim is true of every node at once. `mark_persisted()` speaks for
        one entity only -- committing a single row must not vouch for its
        unsaved siblings.
        """
        self._persisted_revision = self._revision


@dataclass(slots=True, eq=False)
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

        The tag is lowercased first. Ingested keys are always lowercase
        (`io_handlers.populate_attrs` builds
        `f"{elem.tag.group:04x},{elem.tag.element:04x}"`), while
        hand-authored ones are freely written `0008,103E`. Storing both
        spellings made a lookup in one casing miss a value written in
        the other, and a missed key reads as *absent* rather than
        raising -- so the failure looked like ordinary missing data.
        Two of the three recorded encounters were silent PHI defects:
        a Basic-profile rule for Series Description that never matched
        and so never remediated (#41), and a folder-naming helper that
        dropped descriptions (#40). This is the choke point where
        hand-authored keys enter the graph. (#51)

        Args:
            tag (str): The DICOM tag string. Case-insensitive.
            value (Any): The value to set.
        """
        self.attributes[_canonical_tag(tag)] = value
        self.mark_modified()

    def add_sequence_item(self, tag: str, item: 'DicomItem'):
        """
        Appends a new item to a sequence, creating the sequence if needed.

        Args:
            tag (str): The DICOM tag for the sequence.
            item (DicomItem): The item to append.
        """
        tag = _canonical_tag(tag)
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

def iter_item_tree(item: 'DicomItem', path: tuple = ()):
    """Yields `(item, path)` for `item` and every item nested below it.

    `path` is the route from the root: a tuple of `(sequence_tag, index)`
    steps, empty for the root itself. It is what lets a finding raised
    against a sequence item be matched back to that same item in another
    copy of the graph -- nested items carry no UID of their own, so
    position is the only identity they have.

    Depth-first, and in declaration order, so two copies of the same graph
    are walked identically.
    """
    yield item, path
    for tag, sequence in item.sequences.items():
        for index, nested in enumerate(sequence.items):
            yield from iter_item_tree(nested, path + ((tag, index),))


def resolve_item_path(root: 'DicomItem', path: tuple) -> Optional['DicomItem']:
    """Follows a path from `iter_item_tree` back to an item, or None.

    None means the graph changed after the path was recorded -- an item
    removed, or a sequence shortened. The caller must treat that as "this
    item is gone", never as "use the root instead": writing a nested tag
    onto the root fabricates a top-level element that was never in the
    file, and leaves the real value in place.
    """
    item = root
    for tag, index in path or ():
        sequence = item.sequences.get(tag)
        if sequence is None or index >= len(sequence.items):
            return None
        item = sequence.items[index]
    return item


def clone_sequences(item: 'DicomItem') -> dict:
    """Deep-copies an item's sequences.

    Workers must not share sequence items with the session, or a finding
    raised in a worker would carry a reference the parent also holds.

    This used to return an `id()`-keyed mapping alongside the clones, for
    rebuilding `Instance.text_index` against them. That index had no
    production consumer and is gone (#84); nothing else ever read the
    mapping. Nested items are matched between copies of a graph by the
    `entity_path` from `iter_item_tree`, not by identity -- position is
    the only identity a sequence item has.
    """
    clones = {}
    for tag, sequence in item.sequences.items():
        clone = DicomSequence(tag=tag)
        for nested in sequence.items:
            nested_clone = DicomItem()
            nested_clone.attributes = dict(nested.attributes)
            nested_clone.sequences = clone_sequences(nested)
            clone.items.append(nested_clone)
        clones[tag] = clone
    return clones


@dataclass(slots=True, eq=False)
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

    # Persistence: the file this instance was *read from*, which stays
    # true after redaction detaches `file_path`.
    #
    # `file_path` answers "where are bytes that match this instance now",
    # and `regenerate_uid()` must clear it -- `get_pixel_data()` falls
    # back to it, and a redacted instance that still pointed at its
    # source would silently reload the un-redacted frame. `source_path`
    # answers "which file did this come from", which redaction does not
    # change. Ingest de-duplication keys on this one (#238).
    #
    # Never read to load pixels. Nothing may assign `file_path` from it.
    source_path: Optional[str] = None

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

    def __post_init__(self):
        # Inlined from DicomItem to avoid super() mismatch issues with slots/reloads
        self.attributes = {}
        self.sequences = {}

        # An instance constructed from a file records that file as its
        # origin, structurally rather than by convention: every
        # construction site that knows a path passes `file_path`, and a
        # site that had to remember a second argument is a site that can
        # forget one. `and not self.source_path` is what lets an
        # explicit value win -- passed here, or assigned straight
        # afterwards, which is how the store's load path restores the
        # origin of an instance whose `file_path` redaction cleared.
        if self.file_path and not self.source_path:
            self.source_path = self.file_path

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
            4. Records the retired SOP Instance UID under
               `SOURCE_SOP_UID_ATTR`, the first time only (#238).
            5. Detaches the instance from its physical file path (since consistent hash changed).
        """
        previous_uid = self.sop_instance_uid

        # 1. Generate new UID using pydicom's generator (or your org root)
        new_uid = generate_uid()

        # 2. Update the Object Property
        self.sop_instance_uid = new_uid

        # 3. Update the DICOM Attribute Dictionary
        self.set_attr("0008,0018", new_uid)

        # 4. Record the identity this instance is leaving behind, once.
        #
        # Only the first one. The UIDs generated here exist in no file,
        # so recording a later one would replace the single value a
        # re-ingested source file could actually carry -- which is what
        # the ingest gate matches on (#238). A second redaction
        # (`force=True`, #237) must therefore leave this alone.
        #
        # Direct assignment, not `set_attr`: `set_attr` lowercases the
        # key. The revision already moved on the `set_attr` above, so
        # the store still sees this instance as unsaved.
        if previous_uid and SOURCE_SOP_UID_ATTR not in self.attributes:
            self.attributes[SOURCE_SOP_UID_ATTR] = previous_uid

        # 5. Detach from physical file
        # Since this object is now a "new" instance in memory,
        # it no longer matches the file on disk.
        #
        # `source_path` is deliberately not touched here: it records
        # where the bytes came from, which redaction does not change.
        self.file_path = None

        get_logger().debug(f"  -> Identity regenerated: {new_uid}")

    def unload_pixel_data(self) -> bool:
        """
        Clears the cached pixel_array from memory to free resources.

        Only performs the clear if the data can be re-loaded (i.e., `file_path`
        or `_pixel_loader` is present).

        Returns:
            bool: True if unloaded (or already absent), False if it was
                unsafe to unload -- the data is in memory only and
                nothing could bring it back.
        """
        if self.pixel_array is None:
            return True

        if self.file_path or self._pixel_loader:
            self.pixel_array = None
            return True

        # Refusing here is the guard working: pixel data held only in
        # memory (edited but not yet saved) cannot be re-loaded, so
        # clearing it would be a silent discard rather than a free. This
        # announced itself on stdout prefixed "DEBUG:", once per instance,
        # so a correct refusal read as a fault and `release_memory()` over
        # a store with unsaved edits printed a wall of them with no way to
        # quiet it. `unload_waveform_data` declines silently; match it.
        get_logger().debug(
            "Not unloading pixels for %s: held in memory only, with no "
            "file path or loader to restore them.", self.sop_instance_uid)
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
            Optional[np.ndarray]: The pixel data as a numpy array, or None
            when the instance genuinely carries no pixel element. "Could
            not decode" is *not* None -- it raises (#226).

        Raises:
            RuntimeError: If loading fails due to transfer syntax issues,
                missing codecs, or a pixel element the reader could not
                decode.
            FileNotFoundError: If the file path does not exist.
        """
        if self.pixel_array is not None:
            return self.pixel_array

        if self._pixel_loader:
            try:
                # Invoke callback (e.g. sidecar read)
                arr = self._pixel_loader()
                # A read must not write. This used to call set_pixel_data
                # "to ensure attributes (rows, cols) are synced", and the
                # sync could only ever disagree: SidecarPixelLoader reshaped
                # this array *from* those same attributes, so re-deriving
                # them from the result overwrote the input with a guess
                # about which axis was which. On a 3-frame 4-column
                # grayscale instance that guess rewrote SamplesPerPixel
                # 1->4, PhotometricInterpretation MONOCHROME2->RGB and
                # Rows 4->3 -- and since set_pixel_data ends in
                # mark_modified(), the next save() wrote it to SQLite (#186).
                self.pixel_array = arr
                return self.pixel_array
            except Exception as e:
                raise RuntimeError(f"Pixel Loader failed for {self.sop_instance_uid}: {e}") from e

        if self.file_path and os.path.exists(self.file_path):
            try:
                # Read pixel data on demand
                ds = None
                try:
                    ds = pydicom.dcmread(self.file_path)

                    # Cache it in memory. Assigned, not set through
                    # set_pixel_data: pydicom shaped this array from the
                    # file's own descriptors, which are the descriptors
                    # `attributes` holds, so a re-derivation could only
                    # disagree with them (#186).
                    self.pixel_array = ds.pixel_array
                    return self.pixel_array
                except (AttributeError, TypeError):
                    # "No pixel data element" was the intent and is still
                    # right -- but `.pixel_array` raises AttributeError for
                    # a whole family of reasons that are not that, and this
                    # arm called every one of them "this instance has no
                    # pixels". Measured: a Parametric Map declaring
                    # SamplesPerPixel 3 with no Planar Configuration raises
                    # `AttributeError: Missing required element: (0028,0006)
                    # 'Planar Configuration'`, and the export wrote a 4x4
                    # 32-bit image with no pixel element of any kind -- a
                    # missing Type 1 -- and graded PASS (#226).
                    #
                    # Ask the dataset, not the message. If the file holds
                    # one of the three pixel elements and the decode still
                    # failed, this is "pixels this library cannot decode",
                    # which is a different outcome from "no pixels" and one
                    # the caller is entitled to hear about (#191, #209).
                    # Matching on the message instead would be a fourth
                    # spelling of "is there pixel data" in this file.
                    #
                    # A bare `raise`, deliberately: the outer `except
                    # Exception` below ends in `RuntimeError(f"Lazy load
                    # failed for {self.file_path}: {e}")`, which
                    # interpolates pydicom's own words into the message.
                    # That is what survives -- `ExportOutcome.error` crosses
                    # a process boundary (`session.export()` is always
                    # processes, #185) and `__cause__` does not survive
                    # pickling, while the message does. A second
                    # RuntimeError raised here would either duplicate that
                    # message or bypass the codec fallback.
                    #
                    # `ds is not None` keeps `dcmread`'s own AttributeError
                    # /TypeError on the old path deliberately: that is a
                    # different failure and narrowing this arm is not the
                    # place to change it.
                    #
                    # The disable is about `ds`'s inferred type, not about
                    # the membership test: pylint sees the `ds = None`
                    # initialiser above the inner `try` and cannot narrow it
                    # past the `is not None` guard. `Dataset` implements
                    # `__contains__`.
                    if ds is not None and any(
                            t in ds  # pylint: disable=unsupported-membership-test
                            for t in (0x7FE00010, 0x7FE00008, 0x7FE00009)):
                        raise
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
                        # Same reasoning as the two branches above: a read
                        # must not write (#186).
                        self.pixel_array = arr
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

    def get_waveform_bytes(self) -> Optional[bytes]:
        """Return the original Waveform Data (5400,1010) bytes, undecoded.

        DICOM export writes these back verbatim, so a DICOM -> DICOM round
        trip is byte-exact rather than re-encoded (#34). Deliberately not
        cached: the decoded array is what callers normally hold, and
        keeping both resident would double the cost of the largest thing
        an instance owns.

        Returns:
            Optional[bytes]: Raw sample bytes, or None when this instance
            has no waveform or its samples are not backed by the sidecar.
        """
        loader = self._waveform_loader
        if loader is None or not hasattr(loader, "read_raw"):
            return None
        return loader.read_raw()

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

    def _write_int_if_changed(self, tag: str, value: int) -> bool:
        """Write an integer descriptor only when it actually differs.

        This is not an optimisation. `set_attr` bumps `_revision`, so an
        idempotent call would otherwise dirty the instance and have the
        next `save()` rewrite a row that did not change (#186).

        The comparison parses the stored value the way the resolver does,
        so an instance holding `"3"` from the old string form of
        NumberOfFrames compares equal to `3` and is left alone. The
        canonicalisation to `int` therefore does not churn existing graphs.
        """
        if declared_int(self.attributes, tag) == value:
            return False
        self.set_attr(tag, value)
        return True

    def _write_str_if_changed(self, tag: str, value: str) -> bool:
        """Write a string descriptor only when it actually differs."""
        raw = self.attributes.get(tag)
        if raw is not None and str(raw).strip().upper() == str(value).strip().upper():
            return False
        self.set_attr(tag, value)
        return True

    def set_pixel_data(self, array: np.ndarray):
        """
        Sets the pixel array and updates the descriptors that describe it.

        Which axis of the array means what is decided by the instance's own
        attributes (`isocenter.pixel_geometry.resolve_pixel_geometry`), not
        by the array's shape: `(frames, rows, cols)` and
        `(rows, cols, samples)` are the same rank, so the old
        `if shape[-1] in [3, 4]` test was a guess that relabelled every
        multi-frame 3- or 4-column image and every non-RGB colour space
        (#186, #205). How *large* each axis is still comes from the array --
        replacing the pixels with a differently-sized array is what a setter
        is for.

        Updates tags, each only when the value actually changes:
            - Rows (0028,0010)
            - Columns (0028,0011)
            - SamplesPerPixel (0028,0002)
            - NumberOfFrames (0028,0008), if > 1 or already declared
            - PhotometricInterpretation (0028,0004), only to correct an
              outright contradiction -- YBR_FULL and MONOCHROME1 survive
            - PlanarConfiguration (0028,0006), only when colour and undeclared
            - BitsAllocated (0028,0100), from the array's itemsize

        A genuinely ambiguous shape is **accepted** with a WARNING rather
        than refused, because a hand-built graph has to be able to take
        pixels before its attributes -- that is what
        `DicomExporter.write_tree()` exists to serve. The export worker
        refuses the same geometry, because that is where a guess would
        become a file on disk. The asymmetry is deliberate.

        Args:
            array (np.ndarray): The pixel data to set. Can be 1D, 2D, 3D, or 4D.

        Raises:
            ValueError: If the instance declares a SamplesPerPixel that no
                axis of `array` can carry, or if the rank is unsupported.
                The two statements cannot both be right and neither
                trusting the attributes (descriptors that do not describe
                the bytes) nor trusting the array (this is how #186
                happened) is honest.
        """
        self.pixel_array = array
        shape = array.shape

        geom = resolve_pixel_geometry(shape, self.attributes)

        if len(shape) == 1 and geom.evidence is GeometryEvidence.DECLARED:
            # A flat buffer the declared descriptors already describe:
            # reshape to them and write nothing back. The attributes are the
            # input to this reshape, so they need no correcting.
            expected = geom.frames * geom.rows * geom.cols * geom.samples
            if array.size > expected:
                # DICOM alignment padding.
                array = array[:expected]
            if geom.frames > 1:
                array = (array.reshape((geom.frames, geom.rows, geom.cols, geom.samples))
                         if geom.samples > 1
                         else array.reshape((geom.frames, geom.rows, geom.cols)))
            elif geom.samples > 1:
                array = array.reshape((geom.rows, geom.cols, geom.samples))
            else:
                array = array.reshape((geom.rows, geom.cols))
            self.pixel_array = array
            # Writes nothing back, but is still a change, and this call is
            # the one behavioural difference from `692218c` in the whole
            # fix. This branch returns before reaching any descriptor
            # write, so leaving it out is the conditional-dirtying bug in
            # its purest form: no descriptor changes because none is
            # written at all, while `self.pixel_array` has been replaced
            # and the store's copy is now stale. An incremental `save_all`
            # then skips the instance. The declared descriptors are the
            # *input* to this reshape, so nothing here can notice.
            # `tests/test_pixel_geometry_pipeline.py::
            # test_a_flat_buffer_reshaped_from_the_descriptors_still_dirties`
            # is the pin, and it fails on `692218c`.
            self.mark_modified()
            return

        if geom.evidence is GeometryEvidence.GUESSED:
            get_logger().warning(
                "Pixel array shape %s for %s is ambiguous: it is equally a "
                "%d-frame %dx%d image and a %dx%d image with %d samples per "
                "pixel, and the instance declares neither SamplesPerPixel "
                "(0028,0002) nor NumberOfFrames (0028,0008) nor Rows/Columns "
                "to settle it. Reading it as %d samples per pixel. Set "
                "SamplesPerPixel before set_pixel_data() to make this "
                "explicit -- this call writes the guess into the instance's "
                "own descriptors, so a later export sees a declared geometry "
                "and writes it rather than refusing it.",
                tuple(shape), self.sop_instance_uid,
                shape[0], shape[1], shape[2],
                shape[0], shape[1], shape[2],
                geom.samples)

        self._write_int_if_changed("0028,0010", geom.rows)
        self._write_int_if_changed("0028,0011", geom.cols)
        self._write_int_if_changed("0028,0002", geom.samples)

        # An int, matching what `ingest_worker` stores. This used to be
        # written as `str(frames)`, so a graph that went through it once
        # held "3" where ingest had 3 -- two spellings of the same
        # descriptor in one store. A declared NumberOfFrames of 1 is not
        # the same as an absent one, so it is written back rather than
        # dropped once it exists.
        if geom.frames > 1 or "0028,0008" in self.attributes:
            self._write_int_if_changed("0028,0008", geom.frames)

        photometric = resolve_photometric_interpretation(
            self.attributes, geom.samples)
        if photometric is not None:
            self._write_str_if_changed("0028,0004", photometric)

        if planar_configuration_default(self.attributes, geom.samples):
            self._write_int_if_changed("0028,0006", 0)

        # BitsAllocated stays derived from the array, deliberately, and is
        # not the same defect as the geometry. The frames-vs-samples
        # question has no attribute-free answer; the storage width does --
        # `array.itemsize * 8` is exact. And the export writes
        # `arr.tobytes()`, so a BitsAllocated disagreeing with the array's
        # itemsize produces a file that cannot be decoded at all: "the
        # attributes win" is not an option here, because the attribute
        # cannot be honoured. SidecarPixelLoader also relies on this to
        # tell uint8 from uint16.
        bits = array.itemsize * 8
        previous = declared_int(self.attributes, "0028,0100")
        if self._write_int_if_changed("0028,0100", bits) and previous is not None:
            get_logger().debug(
                "BitsAllocated for %s corrected from %s to %d by a %s pixel "
                "array.", self.sop_instance_uid, previous, bits, array.dtype)

        # Unconditional, and separate from the conditional descriptor writes
        # above. The array's *contents* are part of what the store holds, and
        # they are invisible to every comparison in this method: a redacted
        # frame has exactly the Rows, Columns, SamplesPerPixel and
        # BitsAllocated of the frame it replaced, so "no descriptor changed"
        # is not "nothing changed". Dirtying only on a descriptor change
        # leaves an incremental `save_all` skipping the instance and the
        # redacted pixels never reaching the sidecar --
        # `tests/test_blob_storage.py::
        # test_compaction_does_not_resurrect_pre_redaction_pixels` and
        # `::test_save_all_keeps_the_blob_table_in_step_with_instances` are
        # the executable proof. Setting pixel data is a mutation of what the
        # store holds, full stop; do not make that conditional on anything,
        # including object identity -- callers mutate arrays in place
        # (`RedactionService._redact_instance_pixels`), so identity does not
        # track content.
        self.mark_modified()


@dataclass(slots=True, eq=False)
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


@dataclass(slots=True, eq=False)
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

    def __setattr__(self, name, value):
        # The boundary for #188, and it is one spelling on purpose: the
        # dataclass __init__ assigns through here too, so the
        # constructor and a later `study.study_date = ...` refuse
        # identically, and nothing needs a second check downstream.
        # `isinstance` alone would not do -- `datetime` *is* a `date` --
        # which is also why every legitimate value still passes.
        #
        # Refused rather than truncated to `.date()`: a silently
        # discarded time-of-day is the same quiet lossy normalisation
        # #60 forbids for unreadable dates, and the half being discarded
        # has a home of its own. A `datetime` that got in round-tripped
        # through the store as the ISO string `isoformat()` writes --
        # `date.fromisoformat` rejects the 'T' -- and exported as a
        # ten-plus-character (0008,0020), which PS3.5 Table 6.2-1 fixes
        # at eight digits.
        if name == "study_date" and isinstance(value, datetime):
            raise TypeError(
                "Study.study_date holds a date, not a datetime: call "
                ".date() on it, and put the time of day in Study Time "
                "(0008,0030) -- Study.study_time -- instead. A datetime "
                "here comes back from the store as an ISO string and "
                "exports as an illegal DA value (#188).")
        # `object.__setattr__`, not zero-argument `super()`:
        # `@dataclass(slots=True)` builds a *new* class, so the closure
        # cell zero-arg super() reads still names the discarded one and
        # every assignment raises "obj must be an instance or subtype".
        object.__setattr__(self, name, value)

    def mark_subtree_persisted(self):
        """Marks this study and every series beneath it as stored."""
        self._persisted_revision = self._revision
        for series in self.series:
            series.mark_subtree_persisted()


@dataclass(slots=True, eq=False)
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
