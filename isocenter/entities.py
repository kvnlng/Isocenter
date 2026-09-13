import os
import threading
from datetime import date, datetime
from typing import List, Dict, Any, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
import numpy as np
import pydicom
from pydicom.pixels import as_pixel_options, get_decoder
from pydicom.uid import generate_uid
import isocenter.imagecodecs_handler as h
from .logger import get_logger
from .pixel_geometry import (
    SIDECAR_DTYPE_NAMES,
    GeometryEvidence,
    PIXEL_DTYPE_ATTR,
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


def normalize_study_date(value):
    """The one spelling of "text that names a day becomes a `date`".

    `date.fromisoformat` accepts both the extended form `2024-01-15`
    -- which is what `_as_stored_date` writes into SQLite -- and, on the
    3.12 floor, the DICOM basic form `20240115` that a hand-built graph
    or a `DicomBuilder.add_study` call supplies. Anything it cannot read
    comes back exactly as it was given: a date we cannot read is a date
    we do not have, not one we invent, and not one we discard (#60).

    Lives here, not in `persistence`, because both callers need it and
    `entities` is the one of the two that the other imports.
    `persistence._as_loaded_date` is this function under the name that
    says it is `_as_stored_date`'s inverse; `Study.__setattr__` is the
    same rule applied at assignment, which is what makes the
    constructor and hydration agree by construction rather than by two
    parallel parses (#189).
    """
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return value


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

    **One exception, and it is an edit whose content is known.** Pixel
    redaction re-records an instance's REMEDIATED or CLEARED after its own
    writes. The pixels, their descriptors, the new SOP Instance UID and its
    bookkeeping are values no caller authors, and are left out of the
    comparison; the flags it writes (ImageType, BurnedInAnnotation,
    DerivationDescription, the Derivation Code Sequence) are accepted only
    as they were or at exactly what redaction writes. Every other attribute
    and nested item must be exactly as before the pass; anything else
    changed, by anyone, and the status is left UNSCANNED as the rule above
    requires. Without it, the documented anonymize -> redact -> export path
    left every redacted instance UNSCANNED (#486; confirmed by the owner on
    2026-09-11). See `services.capture_phi_status_for_redaction`.
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
        attribute_vrs (Dict[str, str]): The source Value Representation of
            private tags, where one was known. See `record_attr_vr`.
    """
    # init=False to avoid constructor conflicts during inheritance
    attributes: Dict[str, Any] = field(init=False)
    sequences: Dict[str, DicomSequence] = field(init=False)
    attribute_vrs: Dict[str, str] = field(init=False)

    # What the `SHIFT_DATE` arm wrote, per tag: `{tag: the date string it
    # produced}` (#510, #513). Read through `date_shift_vouches_for` and
    # written through `record_date_shift`; never as a raw dict.
    #
    # **Why it is here and not on `Instance`.** A shifted date and an
    # original are the same bytes, so "has this value already been
    # shifted" is not answerable from the graph -- it has to be recorded
    # when the shift happens. It used to be recorded as
    # `Instance.date_shifted`, one boolean speaking for every date on the
    # instance *and* for every date nested in its sequences, which both
    # over-suppressed (a valid date under a rule added in a later pass
    # was never raised, #510) and under-suppressed (a date inside a
    # sequence was re-shifted every pass, because no flag exists on a
    # `DicomItem`, #513). The scan's decision point holds the item and
    # the tag, and the arm's write point holds the item, so a record on
    # `DicomItem` answers both depths with one mechanism and needs
    # nothing plumbed.
    #
    # The `_nested_pixel_refs` comment below argues a nested concern
    # belongs on the `Instance`, and **its own stated reason inverts
    # here**: it says "the PHI-scan copies must not carry sidecar
    # references at all", whereas the scan copies *must* carry this
    # record or the scan cannot make its decision. `clone_sequences` and
    # `_make_lightweight_copy` therefore both copy it on purpose.
    #
    # **Keyed on the value, which is what makes it self-invalidating.**
    # The record vouches for a tag only while that tag still holds the
    # string the shift produced, so overwriting the tag stops it being
    # vouched for structurally -- no invalidation pass, no convention to
    # remember. Same species as `phi_status` keyed on
    # `_phi_status_revision`; disagreement is the signal, not a bug.
    #
    # `default=None` and **not** `default_factory=dict`: an empty dict is
    # 64 bytes and a fourth slot is 8, so a dict default would cost 64 B
    # and an allocation on every one of the 99% of sequence items that
    # hold no date (measured: a slots dataclass goes 56 B -> 64 B for a
    # fourth field; `DicomItem()` is 88 B -> 96 B).
    #
    # Not initialised in `__post_init__`, unlike the three fields above:
    # those are `init=False` with *no* default, so only `__post_init__`
    # can set them, while a defaulted `init=False` field is assigned by
    # the generated `__init__` before `__post_init__` runs. Measured on
    # 3.12.14: a subclass whose `__post_init__` does not mention such a
    # field still reads `None` from it, so there is nothing here for the
    # inlined copy in `Instance.__post_init__` to fall out of step with.
    _shifted_dates: Optional[Dict[str, str]] = field(
        default=None, init=False, repr=False)

    def __post_init__(self):
        self.attributes = {}
        self.sequences = {}
        self.attribute_vrs = {}

    def record_date_shift(self, tag: str, value) -> None:
        """Records that the `SHIFT_DATE` arm wrote `value` at `tag`.

        There is deliberately **no setter and no "mark this shifted"
        without a value**: "this tag was shifted" with no value is
        exactly the entity-level claim #510 and #513 exist to delete.

        The tag is canonicalised here because `set_attr` canonicalises
        too -- a hand-authored `0008,103E` is stored lowercase, and a
        record kept under the other spelling would vouch for nothing and
        read as absent rather than raising.

        This deliberately does **not** `mark_modified()`. The arm calls
        it immediately before the `set_attr` that writes the value, and
        that call advances the revision for both halves; a second bump
        here would be one change the store is told about twice.
        """
        if self._shifted_dates is None:
            self._shifted_dates = {}
        self._shifted_dates[_canonical_tag(tag)] = value

    def date_shift_vouches_for(self, tag: str, value) -> bool:
        """Whether `value` at `tag` is the value a shift on this item
        produced.

        False for a tag with no record, and False the moment the tag
        stops holding what the shift wrote -- a value replaced after a
        shift is raised again, which is the whole point of recording the
        value rather than a boolean.
        """
        if not self._shifted_dates:
            return False
        return self._shifted_dates.get(_canonical_tag(tag)) == value

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

    def record_attr_vr(self, tag: str, vr: str):
        """Remembers the Value Representation a private tag arrived with.

        The standard data dictionary has no entry for an odd-group tag,
        so `DicomExporter._merge` could only guess a VR from the Python
        type of the value, and every number guessed `LO`. A source `US`,
        `UL`, `FL` or `AT` exported as a decimal string wearing a text
        VR: byte-faithful, type-destroyed, and reported nowhere (#154).
        This is where the answer the source file already gave is kept.

        **Odd group only, and never `UN`.** An even-group tag resolves
        its VR from the dictionary and needs nothing here; a second
        answer beside the dictionary is one that can disagree with it.
        `UN` is not recorded because it is the absence of an answer --
        which is also what makes an Implicit VR ingest, where *every*
        private element arrives `UN`, record nothing at all and behave
        exactly as it did before.

        **This is not an edit.** It records what an existing value
        already was, so it deliberately does not `mark_modified()`:
        `set_attr` has already advanced the revision for the value
        itself, and a second bump here would be a change the store is
        told about twice.

        Args:
            tag (str): The DICOM tag string. Case-insensitive.
            vr (str): The two-letter VR from the source element.
        """
        self.attribute_vrs[_canonical_tag(tag)] = vr

    def add_sequence(self, tag: str) -> 'DicomSequence':
        """
        The sequence at `tag`, created empty if it is not there yet.

        A sequence with no items is a thing a source can assert, and until
        #392 this graph had no way to hold one: the only route in was
        `add_sequence_item`, so a zero-item `SQ` made zero calls and
        vanished at ingest with `losses == []`. Both hops that dropped it
        -- `process_sequence` on the way in, `_deserialize_into` on the way
        back out of the store -- now call this once, before their item
        loop, so the empty and the non-empty case are the same statement
        and the empty one cannot go stale.

        `mark_modified()` **only when it creates**. A sequence that newly
        exists is a change the store must hold; a second call on a tag that
        already has one is not, and dirtying there would have every
        hydration and every re-ingest rewrite rows that did not change
        (#186's rule, applied to sequences).

        Args:
            tag (str): The DICOM tag for the sequence. Case-insensitive.

        Returns:
            DicomSequence: the sequence now at `tag`, existing or new.
        """
        tag = _canonical_tag(tag)
        sequence = self.sequences.get(tag)
        if sequence is None:
            sequence = self.sequences[tag] = DicomSequence(tag=tag)
            self.mark_modified()
        return sequence

    def add_sequence_item(self, tag: str, item: 'DicomItem'):
        """
        Appends a new item to a sequence, creating the sequence if needed.

        Delegates the creating half to `add_sequence()` rather than
        repeating it, so there is one spelling of "a sequence comes into
        existence". The first item on a brand-new sequence therefore
        advances `_revision` twice where it advanced once -- harmless,
        because `_revision` is a monotonic counter and
        `has_unsaved_changes` is a comparison rather than arithmetic, but
        real, and `tests/test_empty_sequence_roundtrip.py::
        test_adding_the_first_item_to_a_new_sequence_still_leaves_one_dirty_entity`
        is what says so.

        Args:
            tag (str): The DICOM tag for the sequence.
            item (DicomItem): The item to append.
        """
        self.add_sequence(tag).items.append(item)
        self.mark_modified()

    def clear_sequence_items(self, tag: str) -> bool:
        """
        Empties the sequence at `tag` to zero items, keeping it present.

        What an `EMPTY` rule on a sequence does (#547): a zero-item
        sequence is how a Type 2 sequence carries no value. A method here
        rather than a `del` plus `mark_modified()` in `remediation.py`, for
        the reason `set_attr` and `add_sequence_item` are methods: the
        revision moves with the change, in one place.

        `add_sequence`'s rule the other way round: `mark_modified()` only
        when items were actually removed, so clearing an already-empty or
        absent sequence changes nothing the store must hold.

        Args:
            tag (str): The DICOM tag for the sequence. Case-insensitive.

        Returns:
            bool: True when items were removed.
        """
        sequence = self.sequences.get(_canonical_tag(tag))
        if sequence is None or not sequence.items:
            return False
        sequence.items.clear()
        self.mark_modified()
        return True

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

    @classmethod
    def from_parts(cls, manufacturer, model_name,
                   device_serial_number) -> Optional["Equipment"]:
        """Builds an `Equipment` from a file's or a row's three fields, or nothing.

        A series has equipment iff it has a manufacturer or a model name.
        That is a statement about what an `Equipment` *is* -- identity
        is manufacturer and model; the serial is the optional field, as
        the default on `device_serial_number` already says -- so the
        rule lives here, beside the fields that define it, rather than
        at each of the places that read those fields off a source. Until
        #290 it was spelled three times (`DicomImporter.import_files`,
        `SqliteStore.load_all`, `SqliteStore.load_patient`) and omitted
        once (`SeriesBuilder.set_equipment`), and the whole suite stayed
        green with manufacturer and model swapped at both hydration
        sites. A classmethod rather than the constructor because a
        frozen dataclass cannot express "maybe none" there --
        `__post_init__` cannot return a value, and a `__new__` that
        returns `None` breaks `dataclasses.replace` and pickling -- and
        rather than a module-level function because `Equipment` is the
        public name and this is discoverable from it.

        Positional, in field order, all three required: the constructor
        is positional and every call site holds a serial value, so a
        default here would be a second spelling of the field's own.

        **No normalisation.** `None` stays `None`; `from_parts("ACME",
        None, None)` equals `Equipment("ACME", None, None)`, which is
        what `load_patient` returns for such a row today.

        **A serial alone is not equipment**, and that is the rule as it
        stood, kept deliberately by #290 rather than widened inside a
        behaviour-preserving refactor. The consequence is real: the
        store keeps `device_serial_number` for such a series and every
        reload discards it, while `_match_machine_rule` in `session.py` and the
        redaction walk key on the serial. Widening the predicate is
        filed separately; it is now one line in one place.

        Returns:
            Optional[Equipment]: the equipment, or `None` when neither
            identifying field is present.
        """
        if not (manufacturer or model_name):
            return None
        return cls(manufacturer, model_name, device_serial_number)


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
            # The per-value date record travels with the item (#513).
            # Without it a worker sees a nested date with no record
            # against it, raises it, and the arm shifts it a second time
            # -- the defect, with the fix in place.
            if nested._shifted_dates:
                nested_clone._shifted_dates = dict(nested._shifted_dates)
            nested_clone.sequences = clone_sequences(nested)
            clone.items.append(nested_clone)
        clones[tag] = clone
    return clones


_ABSENT = object()


# The pixel-state leaf lock (#434, Q6). It makes one step of each of these
# atomic against the others: `set_pixel_data()` (the record, the array,
# the unwritten flag, the descriptors and the revision bump),
# `discard_pixel_data()` and `unload_pixel_data()` (the check, the null,
# the restore and the bump), and the four places that publish a resident
# array as the stored frame -- `SqliteStore._swap_pixels_under_gate`,
# both arms of `SqliteStore._persist_pixels`, and the redaction rebind in
# `Session._apply_redaction_outcomes` -- each of which checks what it read
# still stands, rebinds the loader and clears the flag and the record.
# `get_pixel_data()`'s three read arms (the loader, the file and the
# imagecodecs fallback) fill the array and clear the flag through
# `Instance._publish_loaded_frame`, which takes it after the load,
# holding nothing, and publishes only while the slot is still empty, so a
# set landing during a load keeps its pixels (#465).
#
# Without it a publish could interleave with a mutator. A discard landing
# after `_persist_pixels`' revision guard and before its clears restored
# the pre-set descriptors over the frame just published; a set landing
# there had its unwritten flag cleared by a save that wrote the previous
# array, so `unload_pixel_data()` would then drop the only copy (#293's
# shape).
#
# **A leaf, innermost:** pass-lock -> `_sidecar_gate` -> `_pixel_swap_lock`
# -> this. It is never held while taking any other lock -- no frame write,
# no sqlite, and no logging either (a handler takes its own lock), which
# is why `set_pixel_data` and the refusals log after releasing it.
# `tests/test_sidecar_gate_order.py` records it with the other two, and
# its site detector fails on any write of the flag or the record that is
# neither under this lock nor listed with the issue that says why.
#
# **Module-level, not per instance.** `Instance` is a slots dataclass
# whose generated `__getstate__` pickles every field to a worker; a
# `threading.Lock` field would need a hand-written `__getstate__` on a
# frozen-surface class. A module lock is created by each process's
# import, so no pickle carries or inherits it. It is taken for
# microseconds (no array copy happens under it), so serialising those
# steps across instances costs nothing measurable; the redaction
# benchmark in the #434 PR says how much.
PIXEL_STATE_LOCK = threading.Lock()


def _defer(notes, level, message, *args):
    """Queue a log call until `PIXEL_STATE_LOCK` is released."""
    notes.append((level, message, args))


def _decode_with_pydicom(ds):
    """`ds.pixel_array`, and the colour space pydicom says it is in (#482).

    `Dataset.pixel_array` is the `as_array` call on `get_decoder(ts)`,
    made with `as_pixel_options(ds)`, and it keeps `[0]` of the pair
    (pydicom 3.0.2 `pixels/utils.py:1430`). That throws away the one
    statement pydicom makes about colour: the meta's
    `photometric_interpretation`. With the default
    `as_rgb=True` every 8-bit YBR family comes back RGB -- native
    YBR_FULL, JPEG Baseline YBR_FULL_422, J2K YBR_RCT/ICT through Pillow
    -- while the dataset keeps its YBR label (measured). So the call is
    made here in full, and the meta kept. Ingest reads the same meta
    (`io_handlers._decode_pixels`, #372).

    **Where pydicom would refuse before decoding -- no Transfer Syntax
    UID (#281's header-less population), or one no decoder implements --
    this asks `ds.pixel_array` instead**, which refuses in pydicom's own
    words. Those words reach the caller through `get_pixel_data()`'s
    `Lazy load failed for <path>: ...`, and they are not to be reworded
    by accident here.

    Returns:
        ``(array, photometric)``, `photometric` being the decoder meta's
        `photometric_interpretation`. The `ds.pixel_array` branch has no
        meta and puts None beside its array in form only: every case that
        reaches it raises (measured: no Transfer Syntax UID raises
        `AttributeError`, and one no decoder implements raises
        `NotImplementedError`).
    """
    tsyntax = (getattr(ds, "file_meta", None) or {}).get("TransferSyntaxUID")
    try:
        decoder = get_decoder(tsyntax) if tsyntax else None
    except NotImplementedError:
        decoder = None
    if decoder is None:
        return ds.pixel_array, None
    arr, meta = decoder.as_array(ds, **as_pixel_options(ds))
    return arr, meta.get("photometric_interpretation")


def _log_memory_only_refusal(uid):
    # Refusing is the guard working: pixel data held only in memory
    # (edited but not yet saved) cannot be re-loaded, so clearing it would
    # be a silent discard rather than a free. This announced itself on
    # stdout prefixed "DEBUG:", once per instance, so a correct refusal
    # read as a fault and `release_memory()` over a store with unsaved
    # edits printed a wall of them with no way to quiet it.
    # `unload_waveform_data` declines silently; match it.
    get_logger().debug(
        "Not unloading pixels for %s: held in memory only, with no "
        "file path or loader to restore them.", uid)


# Every attribute `Instance.set_pixel_data()` can write, and so every one
# `discard_pixel_data()` puts back (#434). **A new descriptor write in
# `set_pixel_data` must join this tuple**, or a discard leaves that
# descriptor describing pixels that no longer exist -- which is #434
# exactly: BitsAllocated 8 over a stored 16-bit frame, and a read that
# raises until a save makes it permanent. The carrier is uppercase and
# `set_attr` lowercases, which is why the restore writes `attributes`
# directly.
_SET_PIXEL_DATA_TAGS = (
    "0028,0010",        # Rows
    "0028,0011",        # Columns
    "0028,0002",        # SamplesPerPixel
    "0028,0008",        # NumberOfFrames
    "0028,0004",        # PhotometricInterpretation
    "0028,0006",        # PlanarConfiguration
    "0028,0100",        # BitsAllocated
    "0028,0103",        # PixelRepresentation
    PIXEL_DTYPE_ATTR,   # the float/bool dtype carrier
)


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

    # Transient: True when `pixel_array` was replaced through
    # `set_pixel_data()` and has not since been written anywhere. It is
    # what `unload_pixel_data()` consults to tell "this frame can be
    # brought back" from "there is *a* frame that can be brought back",
    # which is all `file_path or _pixel_loader` ever answered (#293).
    #
    # NOT called `_pixel_dirty`. "Dirty" already means two different
    # things in this codebase -- persistence-dirty (`_revision` vs
    # `_persisted_revision`) and PHI-dirty (`phi_status`) -- and both
    # were called "dirty" once, which made them indistinguishable in the
    # code and in the output users read. A third would be worse than
    # either. This is narrower than any of them: it tracks one specific
    # divergence, not a general state of unsavedness.
    _pixel_array_unwritten: bool = field(default=False, repr=False)

    # Transient: what `set_pixel_data()` found in each descriptor it can
    # write (`_SET_PIXEL_DATA_TAGS`), from before the first unwritten
    # replacement -- so `discard_pixel_data()` can undo the whole set, the
    # descriptors as well as the pixels (#434). Without it a discard left
    # the replacement's Rows, BitsAllocated or PixelRepresentation
    # describing the stored frame it reloads, and the next save wrote
    # them to the store.
    #
    # **Invariant: set => `pixel_array` resident and
    # `_pixel_array_unwritten` True.** Every site that nulls the array or
    # publishes it keeps that: discard restores and clears; unload
    # refuses while unwritten; the redaction rebind in
    # `Session._apply_redaction_outcomes` clears (its loader reads the
    # worker's frame, which the *current* descriptors describe); and the
    # three persistence sites that make the resident array the stored
    # frame clear it beside the flag. `get_pixel_data()`'s three read
    # arms publish only into an empty slot, under the leaf, so a set that
    # landed during the load keeps its array, its flag and this record
    # together (#465). Before that they published without it, and the
    # stale frame went over the set with this record left beside a flag
    # that said written (measured in the review of #466).
    #
    # On the instance, not the loader: after #417 the loader's capture is
    # not authoritative, and the loader is rebuilt per read, replaced by
    # every save and shared with worker results. Present tags only -- an
    # absent tag is simply not a key -- so no sentinel has to survive a
    # pickle to a worker. `init=False`: it is state, not an argument, and
    # an `init=True` field would add a positional.
    _pixel_descriptors_replaced: Optional[Dict[str, Any]] = field(
        default=None, init=False, repr=False)

    # Transient: Decoded waveform samples, shape (num_samples, num_channels)
    waveform_array: Optional[np.ndarray] = field(default=None, repr=False)

    # Transient: Lazy loader for waveform samples (sidecar-backed)
    _waveform_loader: Optional[Callable[[], np.ndarray]] = field(default=None, repr=False)

    # Transient: Integrity hash for the raw waveform bytes
    _waveform_hash: Optional[str] = field(default=None, repr=False)

    # Transient: sidecar references for pixel payloads that live inside a
    # sequence item -- an Icon Image Sequence item's own (7fe0,0010) and the
    # like -- keyed by `(path, terminal_tag)`, where `path` is the
    # `iter_item_tree` route to the enclosing item (#183).
    #
    # **On the instance, not on `DicomItem`, and that is deliberate.**
    # `Instance` already holds `_pixel_loader`, `_pixel_hash` and
    # `_waveform_loader`, so one place answers "what binary does this
    # instance carry" and the save walk, the compaction rewire and the
    # export transport all iterate one dict. A field on `DicomItem` would
    # be a class-shape change that `clone_sequences` and
    # `_make_lightweight_copy` would both have to learn about -- and the
    # PHI-scan copies must not carry sidecar references at all.
    #
    # References rather than loaders: see `io_handlers.NestedPixelRef` for
    # why storing the geometry here would disarm the shift guard.
    _nested_pixel_refs: Dict[tuple, Any] = field(
        default_factory=dict, repr=False)

    # There is deliberately **no `date_shifted` here.** It was a
    # transient boolean that went True when any one date on the instance
    # shifted and said nothing about which, so it over-suppressed a
    # valid date the pipeline never touched (#510) and could not speak
    # for a date inside a sequence at all (#513). It was never persisted
    # either -- the `instances` table has no column for it, unlike
    # `studies.date_shifted` (#182) -- so a loaded instance reported
    # False however many of its dates had been shifted. `_shifted_dates`
    # on `DicomItem` answers "was *this value* shifted"; `date_shifted`
    # survives on `Study`, where it answers the entity-level question
    # honestly ("did a de-identifying shift run on this study") and has
    # a reader that asks exactly that (`exporters/wfdb.py`). Removed
    # rather than left unread, per the pre-1.0 convention (#510).

    # Whether this instance came out of a store written before per-value
    # date records existed (#510). `False` for a freshly constructed or
    # ingested instance -- it has no history to be ignorant of -- and set
    # only by hydration, which reads it off the row.
    #
    # It exists because reading "no record" as "not shifted" would make
    # the first `audit()` after upgrading raise every already-shifted
    # date in every pre-0.9.6 store, and `anonymize()` shift each one a
    # second time with a `REMEDIATION_SHIFT_DATE` row that looks
    # legitimate: #513 applied to a whole archive, caused by the fix.
    # Such an instance keeps the old entity-level rule for the values
    # nobody can name, permanently; its records are still written and
    # still vouch, so a new shift under 0.9.6 is exact.
    #
    # `init=False`: it is state, not an argument, and an `init=True`
    # field would add a positional to a frozen constructor order.
    _legacy_shift_provenance: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        # Inlined from DicomItem to avoid super() mismatch issues with slots/reloads
        self.attributes = {}
        self.sequences = {}
        # Must stay in step with `DicomItem.__post_init__`: an inlined
        # copy is exactly the kind of duplicate that goes one field out
        # of date and fails as an AttributeError deep inside ingest.
        self.attribute_vrs = {}

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
        Frees the cached pixel_array, but only when it can be brought back.

        Two things have to be true, and the second was missing until #293.
        There must be somewhere to reload from (`file_path` or
        `_pixel_loader`), **and** the resident array must not have been
        replaced through `set_pixel_data()` since it was last written.
        `set_pixel_data()` deliberately leaves `_pixel_loader` alone, so
        after a save the loader is still there and still points at the
        frame the replacement superseded: the old check passed while the
        array and the stored frame had diverged, and the clear discarded
        the only copy of the new pixels. The next `save_all` then
        re-recorded the loader's own offset, length and hash and marked
        the instance persisted, so store, sidecar, memory and
        `_pixel_hash` all agreed on the old frame and every integrity
        check passed.

        The precondition is stated exactly, because promising more than
        the flag tracks would be the same defect in the fix: this refuses
        when the array was **replaced through `set_pixel_data()`** and
        not since written. An array mutated **in place** diverges too
        and is not detected. Mutating in place needs a writeable array,
        and a frame that came from a file or the sidecar is not one --
        it is `np.frombuffer`-backed, so `arr[...] = 0` on it raises
        rather than diverging (#323). The reachable shape is a
        replacement a save has since written: that array *is* writeable
        and the flag is back to False, so `arr =
        inst.get_pixel_data(); arr[...] = 0` on it diverges silently.
        Any caller holding such an array can do it; nothing here is
        changed by #293.

        **Not `RedactionService._redact_instance_pixels`' writeable
        arm**, which two versions of this paragraph have now claimed it
        was -- #293's ("zeroes a file-backed array in place", which
        cannot happen: a file-backed array is read-only) and #323's
        first attempt ("the second redaction pass"). Measured: that arm
        does zero in place and never calls `set_pixel_data()`, but on a
        reloaded instance it is not entered at all, on any pass, because
        `get_pixel_data()` hands back a read-only frame and the copying
        arm takes it every time. When it *is* entered -- a resident
        writeable array a save has already written -- both callers
        (`redact_machine_instances` and `execute_redaction_task`)
        persist the pixels in their `try` (the serial arm's moved there
        from its `finally` in #474) and then call `discard_pixel_data()`
        unconditionally in their `finally`, so nothing survives the pass
        for an unload to drop. With no `store_backend` the persist is
        skipped and that same discard loses the mutation immediately,
        which is a different defect with a different fix.

        Use `discard_pixel_data()` where dropping unsaved pixels is the
        intent rather than the accident.

        Returns:
            bool: True if unloaded (or already absent), False if it was
                unsafe to unload -- either the data is in memory only and
                nothing could bring it back, or it has diverged from what
                is stored.
        """
        # The check and the drop under one hold of `PIXEL_STATE_LOCK`:
        # checked outside it, a `set_pixel_data()` landing between the
        # check and the drop would be dropped, unwritten.
        with PIXEL_STATE_LOCK:
            if self.pixel_array is None:
                return True
            unwritten = self._pixel_array_unwritten
            if not unwritten and self._drop_resident_array():
                return True

        if unwritten:
            # Same refusal path as the no-loader case, and silent for the
            # same reason: this is the guard working. A loader may well be
            # present -- it just points at the wrong frame.
            get_logger().debug(
                "Not unloading pixels for %s: the array was replaced and "
                "has not been written, so clearing it would discard the "
                "only copy.", self.sop_instance_uid)
        else:
            _log_memory_only_refusal(self.sop_instance_uid)
        return False

    def discard_pixel_data(self) -> bool:
        """
        Frees the cached pixel_array even if it has unwritten changes.

        This is `unload_pixel_data()`'s behaviour before #293, kept under
        a name that says what it does. It is for the caller who means to
        throw the resident array away -- the redaction `finally` blocks,
        where a partially-zeroed array must be dropped so the next
        `get_pixel_data()` reloads the stored bytes through the loader,
        read under the instance's current descriptors (#417).

        **It undoes the whole `set_pixel_data()` it discards (#434)**: the
        pixels, and every descriptor that call wrote -- Rows, Columns,
        SamplesPerPixel, NumberOfFrames, PhotometricInterpretation,
        PlanarConfiguration, BitsAllocated, PixelRepresentation and the
        float/bool dtype carrier -- go back to what they were before the
        first unwritten replacement, absent ones included. A pixel
        descriptor edited between the set and the discard goes back with
        them: while the replacement is resident, that edit describes the
        replacement. So the next read is the stored frame as it was
        stored. Once the replacement is written -- by a save or the
        redaction swap -- it *is* the stored frame, and there is nothing
        to undo: the array is dropped and the descriptors, which describe
        it, stay. A refusal (below) keeps both the array and the
        descriptors that describe it. Dropping an unwritten replacement
        leaves the instance dirty, as the set did.

        Two behaviours, two names. This is not an alias for
        `unload_pixel_data()` and must not become one: "one spelling per
        behaviour" is about a single behaviour with two names, and these
        answer different questions -- "free this if it is safe" and
        "throw this away".

        Returns:
            bool: True if discarded (or already absent), False if there
                is nowhere to reload from at all.
        """
        with PIXEL_STATE_LOCK:
            if self.pixel_array is None:
                return True
            if self._drop_resident_array():
                return True
        _log_memory_only_refusal(self.sop_instance_uid)
        return False

    def _drop_resident_array(self) -> bool:
        """Discard's drop, for a caller holding `PIXEL_STATE_LOCK`.

        False, touching nothing, when there is nowhere to reload from: a
        refusal keeps the array and the descriptors that describe it.
        """
        if self.file_path or self._pixel_loader:
            dropped_unwritten = self._pixel_array_unwritten
            self.pixel_array = None
            self._restore_replaced_descriptors()
            if dropped_unwritten:
                # A change, and one a save already under way must see.
                # `_persist_pixels` reads the array, writes it, and then
                # publishes only if the revision it captured still
                # stands (#274); a discard landing in between is caught
                # there by this bump and nothing else. Taken whenever an
                # unwritten array is dropped, not only when the restore
                # changed a descriptor: a replacement with the same
                # geometry and dtype changes none, and without the bump
                # the save published the discarded pixels and marked the
                # instance persisted. Single-threaded it changes nothing
                # visible -- the set already dirtied the instance.
                self.mark_modified()
            return True
        return False

    def _restore_replaced_descriptors(self) -> None:
        """Put back what `set_pixel_data()` recorded, and forget the record.

        Direct writes and deletes into `attributes`, not `set_attr`:
        `set_attr` lowercases its key, and the dtype carrier is
        `_ISOCENTER_PIXEL_DTYPE` -- a `set_attr` restore writes a
        lowercase ghost nothing reads and leaves the float frame decoding
        as integers. There is no remove-attribute method, hence the
        `del`: a tag the set introduced (NumberOfFrames for a multi-frame
        array, the carrier for a float one) was absent before, and absent
        is what it goes back to. The caller moves the revision.
        """
        record = self._pixel_descriptors_replaced
        if record is None:
            return
        self._pixel_descriptors_replaced = None
        for tag in _SET_PIXEL_DATA_TAGS:
            if tag in record:
                if self.attributes.get(tag, _ABSENT) != record[tag]:
                    self.attributes[tag] = record[tag]
            elif tag in self.attributes:
                del self.attributes[tag]

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

        A read whose decoder returns RGB from a YBR-labelled file says so.
        An 8-bit `YBR_FULL` JPEG-LS file read through the imagecodecs
        fallback comes back converted to RGB, as `ingest()` stores it
        (#464). So does a JPEG 2000 `YBR_RCT`/`YBR_ICT` file, whose codec
        undoes the colour transform, and any 8-bit YBR source pydicom
        decodes, which it returns as RGB by default (#482). An instance
        that carries a PhotometricInterpretation is relabelled `RGB` to
        match, which advances its revision. This is the one write a read
        makes, and it is made only when the decode converted.

        Raises:
            RuntimeError: If loading fails due to transfer syntax issues,
                missing codecs, or a pixel element the reader could not
                decode. Also, from a file, when an encapsulated pixel
                element's offset table names a different number of frames
                from NumberOfFrames -- "Lazy load failed for <path>:
                <table> names N frames; NumberOfFrames declares M" (#418).
                From the sidecar, when a descriptor written since the
                loader was built asks for a reading the stored bytes
                cannot satisfy (BitsAllocated 16 -> 8, or Rows x Columns
                smaller than the stored samples) -- "Pixel Loader failed
                for <uid>: Integrity Error: ..." (#417). A reopened session
                gives the same refusal.
            FileNotFoundError: If the file path does not exist.
        """
        if self.pixel_array is not None:
            return self.pixel_array

        # One read of the slot. `_apply_redaction_outcomes` rebinds it
        # under the store's `_pixel_swap_lock`; this arm never writes it.
        loader = self._pixel_loader
        if loader:
            try:
                # A loader captures the pixel descriptors once, when it is
                # built, and reads every frame from that capture. A
                # descriptor written since -- by `set_attr` or by any of
                # the writers that go straight to `attributes` -- left the
                # live session reading the old way while the same store,
                # reopened, read the new way or refused: PixelRepresentation
                # 0 -> 1 still read uint16, Rows/Columns 4x4 -> 2x8 still
                # read (4, 4), and export wrote the old Rows back out (#417).
                # So compare on every read and, when the capture is stale,
                # read the same stored, hash-checked bytes through a loader
                # built from the instance as it is now.
                #
                # Here rather than in `_persist_pixels`: a read before any
                # save was measured identical to one after it, so a fix in
                # the save path leaves the first reads stale. And on the
                # read rather than refusing the write: `set_attr` is
                # generic, a two-step edit (Rows, then Columns) passes
                # through a state with no valid reading, and eight writers
                # bypass `set_attr` altogether. A comparison where the
                # bytes are read catches every one of them.
                #
                # **Not stored back.** The rebuilt loader serves this read
                # and is dropped; the next read compares again (one tuple
                # compare, and one small object when stale). Writing it to
                # `_pixel_loader` here, outside `_pixel_swap_lock`, could
                # publish a stale loader over the one
                # `_apply_redaction_outcomes` just bound to the redacted
                # frame -- #274's shape, unredacted pixels under a full
                # redaction attestation.
                #
                # **Not gated on `_pixel_array_unwritten`.** The flag
                # stays set after a `discard_pixel_data()`, so a descriptor
                # edited after the discard -- PixelRepresentation 0 -> 1,
                # say -- is read under a set flag. A gate would read it
                # the old way here and on the next read, and the new way
                # after a save and a reopen: two answers to one question.
                # Ungated, every read agrees with the reopened store.
                # (Before #434 the discard itself left `set_pixel_data`'s
                # descriptors behind and was the example here; it now
                # puts them back.) The publish below is not gated on the
                # flag either, for the same reason: it asks whether the
                # slot is still empty (`_publish_loaded_frame`, #465).
                #
                # Duck-typed: tests install a bare lambda as the loader,
                # and a loader with no `describes` has no capture to go
                # stale.
                describes = getattr(loader, "describes", None)
                if describes is not None and not describes(self):
                    loader = loader.for_instance(self)
                # Invoke callback (e.g. sidecar read)
                arr = loader()
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
                # Published only into an empty slot: a set that landed
                # during the load keeps its pixels (#465).
                return self._publish_loaded_frame(arr)
            except Exception as e:
                raise RuntimeError(f"Pixel Loader failed for {self.sop_instance_uid}: {e}") from e

        if self.file_path and os.path.exists(self.file_path):
            try:
                # Read pixel data on demand
                ds = None
                try:
                    # `force=True`, and it must stay: the eager read and
                    # this lazy re-read are two reads of the *same*
                    # source file, so they have to accept the same
                    # files. `ingest_worker` forces (#281), so a
                    # header-less file -- no preamble, no `DICM` prefix,
                    # the ordinary shape of a raw vendor dump -- indexed
                    # cleanly and then could not produce its own pixels:
                    # `RuntimeError: Lazy load failed ... 'DICM' prefix
                    # is missing`. A file accepted at one boundary and
                    # refused at the next is the defect, whichever
                    # boundary would be right on its own (#289).
                    #
                    # The cost is stated rather than hidden: if
                    # `file_path` has since been replaced by a file that
                    # is not DICOM at all, forcing parses it to a
                    # dataset with no pixel element, so the
                    # `any(t in ds ...)` guard below returns None where
                    # this used to raise. Measured, not reasoned -- the
                    # imagecodecs fallback is never reached. That
                    # narrows #226's "could not decode is not None" for
                    # a population that a forcing ingest had already
                    # accepted.
                    ds = pydicom.dcmread(self.file_path, force=True)

                    # Before `ds.pixel_array`, which returns every frame
                    # the offset table names -- (2, 4, 4) under a
                    # one-frame header -- and before the imagecodecs
                    # fallback below, which would decode frame 0 alone.
                    # Here rather than in the handler only: the fallback
                    # swallows the handler's RuntimeError and re-raises
                    # the *original* error, so a handler-only refusal
                    # would be hidden behind whatever pydicom said (#418).
                    #
                    # The wording trap. This rides the outer `except`
                    # into "Lazy load failed for <path>: ...", and on the
                    # way it passes two message matches: "no pixel data"
                    # (just below) turns into `return None`, a silent
                    # nothing, and "decompress" / "missing dependencies"
                    # turn into the codecs-missing message. The helper's
                    # "<table> names N frames; NumberOfFrames declares M"
                    # contains none of them. Do not reword it into one.
                    mismatch = h.frame_count_mismatch(ds)
                    if mismatch is not None:
                        raise RuntimeError(mismatch)

                    # pydicom returns an 8-bit YBR source as RGB and says
                    # so only in its decoder's meta, leaving `ds` labelled
                    # YBR. Follow the meta, as the imagecodecs arm below
                    # follows the handler's relabel (#482, #464's rule).
                    # Compared with the file's label, not the instance's:
                    # only a conversion is a statement this read makes
                    # about colour.
                    declared = str(getattr(
                        ds, "PhotometricInterpretation", "") or "")
                    arr, decoded = _decode_with_pydicom(ds)
                    relabel = (str(decoded) if decoded is not None
                               and str(decoded) != declared else None)
                    # Cache it in memory. Assigned, not set through
                    # set_pixel_data: pydicom shaped this array from the
                    # file's own descriptors, which are the descriptors
                    # `attributes` holds, so a re-derivation could only
                    # disagree with them (#186). Published, relabel and
                    # all, only into an empty slot (#465).
                    return self._publish_loaded_frame(arr, relabel)
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
                #
                # No `h.is_available()` in this condition, deliberately
                # (#444). With it, a missing imagecodecs was never asked,
                # so the handler's refusal -- which names the import
                # failure -- never reached the caller, who got pydicom's
                # error and advice to install the codec that was installed
                # and broken. The handler raises its own refusal when it
                # is unavailable; let it.
                fallback_words = ""
                try:
                    if ds is not None and h.supports_transfer_syntax(
                            ds.file_meta.TransferSyntaxUID):
                        declared = str(getattr(
                            ds, "PhotometricInterpretation", "") or "")
                        arr = h.get_pixel_data(ds)
                        # The handler converts 8-bit YBR_FULL JPEG-LS to
                        # RGB and says so by relabelling `ds` (#464). Asked
                        # of `ds`, before and after, rather than of the
                        # file's label against the instance's: only a
                        # conversion is a statement this door makes about
                        # colour. A hand-built label that disagrees with
                        # the file for any other reason is not this read's
                        # to correct.
                        decoded = str(getattr(
                            ds, "PhotometricInterpretation", "") or "")
                        # Same reasoning as the two branches above: a read
                        # must not write (#186). The relabel is the one
                        # exception, and it is a label, not a geometry: it
                        # states a conversion this read made, and it is
                        # made only if this read publishes (#465).
                        return self._publish_loaded_frame(
                            arr, decoded if decoded != declared else None)
                except (ImportError, AttributeError, RuntimeError) as exc:
                    # Fallback failed: raise the original error below, and
                    # say what the fallback said beside it (#444). This was
                    # `pass`, which dropped the handler's reason -- a
                    # frame-count, sign or colour refusal, or an import
                    # failure -- for pydicom's words alone, so a refusal
                    # made in the handler never reached this method's
                    # caller. Formatted here because Python unbinds an
                    # `except ... as` name when the block ends. Only when
                    # the handler was actually asked: a syntax it does not
                    # list leaves the message exactly as it was.
                    fallback_words = f"\nimagecodecs fallback: {exc}"

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
                        f"{fallback_words}"
                    ) from e

                # If we just caught the re-raised "no pixel data" exception, it would be handled above,
                # but if dcmread fails completely or something else happens:
                raise RuntimeError(
                    f"Lazy load failed for {self.file_path}: {e}{fallback_words}"
                ) from e

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

    def _relabel_to_decoded_colour(self, label: str) -> None:
        """`get_pixel_data()`'s decode converted: say so (#464, #482).

        The decoder converted the frame this read is about to publish and
        said so: the handler by relabelling its dataset (8-bit YBR_FULL
        JPEG-LS, J2K YBR_RCT/ICT), pydicom in its decoder's meta (8-bit
        YBR sources). The instance's PhotometricInterpretation follows, as
        ingest's does. Otherwise the door returns RGB bytes under a YBR
        label, which is #372's defect, and export writes the two together.
        It bumps the revision, because a new label is a change the store
        should hold. **Every read arm relabels through here, and only
        `_publish_loaded_frame` calls it**, so every relabel gets the
        discipline below. The one other write of this label beside a new
        frame is not a read: `Session._apply_redaction_outcomes` copies a
        process worker's redaction result across, the worker's label with
        its loader, under the same lock and without this helper, which
        writes only on a read's publishing branch (#482).

        **Only when the instance already carries a label.** A bare
        `Instance(file_path=...)` holds no descriptors, so nothing on it
        is false. Neither arm adds one: a lone PhotometricInterpretation
        beside no Rows would be a write no read has made before. After
        `ingest()` the label is RGB already, so on that path this writes
        nothing. It is for hand-built graphs.

        **Called under `PIXEL_STATE_LOCK`, on the publishing branch
        only.** `_publish_loaded_frame` holds the leaf, finds `pixel_array`
        still None, and relabels and publishes the frame in that one hold
        (#465). A `set_pixel_data()` that landed during the load has
        filled the slot, so neither happens and the set keeps its label and
        its pixels; a set that lands after the hold writes its own label
        over this one. It takes no lock of its own: the caller holds a
        plain `threading.Lock`, which a second acquire would deadlock.
        `set_attr` takes no lock and logs nothing, and `set_pixel_data`
        already calls it under this leaf, so the section stays a leaf.
        """
        if "0028,0004" in self.attributes:
            self._write_str_if_changed("0028,0004", label)

    def _publish_loaded_frame(self, arr: np.ndarray,
                              relabel: Optional[str] = None) -> np.ndarray:
        """Cache the frame a read arm loaded, unless a set got there first (#465).

        The three read arms -- the sidecar loader, the file, the
        imagecodecs fallback -- load with no lock held, since a decode
        can take seconds and `PIXEL_STATE_LOCK` is a leaf held for
        microseconds, and then publish here. They used to assign the
        frame and clear the unwritten flag unconditionally: a
        `set_pixel_data()` that landed during the load was overwritten by
        the stale stored frame, the clear marked the lost pixels written,
        `unload_pixel_data()` then dropped the only copy, and the next
        save dedup'd against the stored frame. A set of another geometry
        left the stored frame resident under the set's descriptors, with
        the #434 record set beside a flag that said written.

        So: under the leaf, publish only while the slot is still empty,
        and otherwise return what is resident, the set's array. **The
        predicate is the slot**, not the flag and not the revision. The
        flag stays set after a `discard_pixel_data()`, and the read that
        follows must publish (A10 and S6 in
        `tests/test_descriptor_edit_with_pixels_unloaded.py`). The
        revision moves on every `set_attr`, every PHI status and the
        relabel below, none of which makes the loaded frame wrong, so a
        revision guard would refuse to cache under an audit running on
        another thread.

        `relabel` is the colour space the decode converted to (#464,
        #482), written only when this read publishes.

        The imagecodecs arm is a read like the other two, and its clear
        through here is pinned by
        `tests/test_single_frame_encapsulated_decode.py`'s
        `test_the_imagecodecs_fallback_reads_a_frame_pydicom_cannot`
        (set, discard, read, and `unload_pixel_data()` must come back
        True). Until #407 no transfer syntax reached that arm, and its
        clear was the one line in this method no test could see.
        """
        with PIXEL_STATE_LOCK:
            if self.pixel_array is None:
                if relabel is not None:
                    self._relabel_to_decoded_colour(relabel)
                self.pixel_array = arr
                # A fresh read from the store or the file: the resident
                # array now IS what is stored, so it is freeable again (#293).
                self._pixel_array_unwritten = False
                return arr
            return self.pixel_array

    @staticmethod
    def _accepted_pixel_array(array: np.ndarray) -> np.ndarray:
        """The array as the sidecar will hold it, or a `ValueError`.

        **The accepted set is stated positively, never as a blacklist.**
        A deny-list has to be complete to be safe and is wrong the moment
        numpy adds a kind -- silently, in the direction that stores a
        frame nothing can decode. Stated this way, a new kind is refused
        by default rather than admitted by omission, which is why
        `float128` needs no clause of its own: it is kind `'f'` and
        simply absent from `SIDECAR_DTYPE_NAMES`.

        The three clauses are the three channels the sidecar decodes by,
        and each is exactly as wide as its channel:

        - `'u'`/`'i'` at 1, 2, 4 or 8 bytes -- the domain of
          `_INTEGER_DTYPE_BY_BITS`, so the accept rule and the reload
          table are one statement rather than two that can drift apart.
        - `'b'` -- carried by name in the dtype carrier.
        - `'f'` whose name is in `SIDECAR_DTYPE_NAMES` -- likewise.

        Everything else round-tripped wrongly and nothing refused it:
        `complex64` recorded no carrier, declared `BitsAllocated 128` and
        reloaded through the loader's fallback as `uint16`, silently
        (#386).

        **Byte order is normalised rather than refused.** A big-endian
        `>i2` is kind `'i'` at 2 bytes and passes every clause -- but the
        sidecar stores raw bytes and the loader reads them with a
        native-order dtype, so it reloaded byte-swapped. Refusing an
        array that is exactly representable and merely spelled unusually
        would be the wrong answer; `>i2` and `<i2` now produce
        byte-identical frames, which is what a caller means by handing
        over either. Note that the normalised array is a **copy**, so a
        caller who mutates a big-endian array in place after this call no
        longer reaches the frame the instance holds -- the one place
        where "callers mutate arrays in place" stops being true.
        """
        dtype = array.dtype
        accepted = (
            (dtype.kind in ('u', 'i') and dtype.itemsize in (1, 2, 4, 8))
            or dtype.kind == 'b'
            or (dtype.kind == 'f' and dtype.name in SIDECAR_DTYPE_NAMES))
        if not accepted:
            raise ValueError(
                f"set_pixel_data() cannot take a {dtype.name} array: the "
                f"sidecar stores raw bytes and decodes them from "
                f"BitsAllocated, PixelRepresentation and the dtype carrier, "
                f"which between them name unsigned and signed integers of 1, "
                f"2, 4 or 8 bytes, bool, and float16/float32/float64 -- and "
                f"nothing else. Accepting this array would have stored bytes "
                f"no reload could name, and handed back a different array "
                f"than the one given. Convert to one of those dtypes first, "
                f"deliberately, so the conversion is yours rather than this "
                f"library's.")

        # Byte order last, and only for what passed: `astype` copies the
        # whole frame, so normalising first would copy a large refused
        # array before rejecting it.
        if dtype.byteorder not in ('=', '|'):
            array = array.astype(dtype.newbyteorder('='))
        return array

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
            - PixelRepresentation (0028,0103), from the array's dtype kind:
              1 for signed integers, 0 for unsigned and bool. Left alone
              for a float array, because PS3.5 Section 8.2 forbids it
              beside a float pixel element and the export deletes it
              there.

        A genuinely ambiguous shape is **accepted** with a WARNING rather
        than refused, because a hand-built graph has to be able to take
        pixels before its attributes -- that is what
        `DicomExporter.write_tree()` exists to serve. The export worker
        refuses the same geometry, because that is where a guess would
        become a file on disk. The asymmetry is deliberate.

        Args:
            array (np.ndarray): The pixel data to set. Can be 1D, 2D, 3D, or 4D.

        Raises:
            ValueError: If `array.dtype` is one the sidecar cannot
                round-trip. The accepted set is unsigned and signed
                integers of 1, 2, 4 or 8 bytes, `bool`, and
                `float16`/`float32`/`float64`; `complex64`, `object`,
                strings, structured and void dtypes, datetimes and
                `float128` are refused. Byte order is **normalised, not
                refused**, so a big-endian array is accepted and stored
                native-order. This check runs before any mutation, so a
                caught `ValueError` leaves the instance exactly as it was.
            ValueError: If the instance declares a SamplesPerPixel that no
                axis of `array` can carry, or if the rank is unsupported.
                The two statements cannot both be right and neither
                trusting the attributes (descriptors that do not describe
                the bytes) nor trusting the array (this is how #186
                happened) is honest. **This one raises after
                `self.pixel_array` has been assigned** -- pre-existing,
                and not what the dtype guard above is about.

        It records the prior value, or absence, of each descriptor it can
        write (`_SET_PIXEL_DATA_TAGS`), once, at the first replacement
        since the array was last written; a later set keeps that first
        record. It is kept until the array is written -- by a save or the
        redaction swap -- or discarded, when `discard_pixel_data()` puts
        it back (#434).

        Note that this does **not** clear `_pixel_loader`. #293 weighed
        clearing it as a cheaper fix and rejected it: the loader is what
        lets a partially-redacted array be dropped and the original
        reloaded, which is the design
        `tests/test_redaction_failure_is_reported.py` states outright.
        Instead the divergence is recorded, and `unload_pixel_data()`
        refuses until it is written.
        """
        # **Before any mutation, and that is the whole of it.** The
        # assignment below is this method's first side effect and the
        # descriptor writes follow it, so a refusal raised part-way would
        # leave an instance describing an array it does not hold -- a new
        # silence inside the fix that closes one.
        # `tests/test_pixel_dtype_roundtrip.py::
        # test_a_refused_dtype_leaves_the_instance_exactly_as_it_was`
        # asserts it on the frame as well as on the attributes, because
        # only the frame assertion catches a guard moved below this line.
        #
        # The dtype check runs before the byte-order normalisation
        # deliberately, even though the normalisation reads as the
        # earlier step: the check is O(1) on `dtype`, and `astype` copies
        # the whole frame -- normalising first would fully copy a large
        # `complex64` array immediately before rejecting it.
        array = self._accepted_pixel_array(array)

        # From the record to the revision bump under `PIXEL_STATE_LOCK`,
        # so a save publishing the previous array sees this set whole or
        # not at all (#434, Q6). The dtype guard and its copy stay
        # outside. Logged after release: the lock is a leaf, and a
        # logging handler takes its own.
        notes = []
        try:
            with PIXEL_STATE_LOCK:
                self._replace_pixel_array(array, notes)
        finally:
            for level, message, args in notes:
                getattr(get_logger(), level)(message, *args)

    def _replace_pixel_array(self, array: np.ndarray, notes: list) -> None:
        """`set_pixel_data()` from the record on; the caller holds the lock."""
        # What the descriptors held before this replacement, for
        # `discard_pixel_data()` to put back (#434). After the dtype
        # guard, so a refused dtype records nothing and still leaves the
        # instance exactly as it was; before the assignment and every
        # descriptor write, so the SamplesPerPixel `ValueError` below --
        # raised after the assignment -- finds the record already in
        # place, and a write moved above it cannot slip in unrecorded.
        # Only when there is none: a second set before the array is
        # written keeps the first record, since the second set's "prior"
        # values describe pixels that were never stored.
        if self._pixel_descriptors_replaced is None:
            self._pixel_descriptors_replaced = {
                tag: self.attributes[tag] for tag in _SET_PIXEL_DATA_TAGS
                if tag in self.attributes}

        self.pixel_array = array
        # The resident array no longer matches anything on disk or in the
        # sidecar, and `_pixel_loader` is deliberately left pointing at
        # the frame it replaced -- so from here until something writes
        # these bytes, dropping the array would lose them (#293).
        self._pixel_array_unwritten = True
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
            _defer(
                notes, "warning",
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

        # The dtype of the frame now held, kept true here because the
        # sidecar decodes by it and no DICOM descriptor can tell a
        # 32-bit float frame from a 32-bit integer one (#183). Written
        # for every floating-point array, float16 included -- the
        # sidecar is ours and holds what DICOM has no element for, and a
        # float16 array that reloads as `uint16` takes the export's
        # integer path and never files the DATA_LOSS row that says its
        # pixels could not be written.
        #
        # Kind `'b'` joins them in #386, and is the only integer-kind
        # dtype that ever will. Once the block below records
        # PixelRepresentation, BitsAllocated and PixelRepresentation
        # together name every integer dtype the sidecar can hold exactly,
        # so a carrier for one of those would be a second answer to a
        # question the descriptors already answer -- and the
        # authoritative one, so a graph whose descriptors were later
        # corrected would decode against a stale carrier. `bool` is the
        # exception because no descriptor pair can name it: numpy
        # `bool_` and `uint8` both declare 8 and 0, so a mask set in
        # memory came back as `uint8` and only its values survived.
        #
        # It DELETES as well as writes. Replacing a float instance's
        # pixels with an integer array and leaving the carrier behind
        # would have the loader read those integers back as floats --
        # the same silent corruption arriving from the other direction.
        name = array.dtype.name if array.dtype.kind in ('f', 'b') else None
        if name in SIDECAR_DTYPE_NAMES:
            self.attributes[PIXEL_DTYPE_ATTR] = name
        else:
            self.attributes.pop(PIXEL_DTYPE_ATTR, None)

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
            _defer(
                notes, "debug",
                "BitsAllocated for %s corrected from %s to %d by a %s pixel "
                "array.", self.sop_instance_uid, previous, bits, array.dtype)

        # PixelRepresentation, on exactly the argument the BitsAllocated
        # block above already makes. The width was derived from the array
        # and the signedness was not, so `set_pixel_data(int16_array)`
        # recorded a 16 and nothing at all about the sign: the sidecar
        # reloaded the frame as `uint16` and `-8` came back as `65528`,
        # and `_export_instance_worker`'s
        # `ds.PixelRepresentation = inst.attributes.get("0028,0103", 0)`
        # wrote a file declaring an unsigned frame beside signed bytes --
        # with `wrote 1 of 1 planned instances` in the audit log beside
        # it (#386). The export writes `arr.tobytes()`, so a
        # PixelRepresentation disagreeing with the array cannot be
        # honoured; "the attributes win" is not one of the options here
        # either. **That exporter line is gone since #499**: the writer
        # now derives the element from `arr.dtype.kind` exactly as this
        # does, and hands a disagreeing declaration back on
        # `ExportOutcome.corrections`. This block is still not
        # redundant -- it is what keeps the *graph* and the sidecar
        # coherent, which is what decides the dtype a reload returns --
        # but the two answers can no longer differ.
        #
        # **Floats are deliberately excluded**, and left alone rather
        # than popped. PS3.5 Section 8.2 says Bits Stored, High Bit and
        # Pixel Representation *shall not be present* beside a float
        # pixel element, and the export's float arm already deletes all
        # three -- so writing a 0 here would be a write the export
        # immediately undoes, and the carrier (not this descriptor) is
        # what decodes a float frame on the way back in.
        if array.dtype.kind in ('i', 'u', 'b'):
            representation = 1 if array.dtype.kind == 'i' else 0
            previous = declared_int(self.attributes, "0028,0103")
            if (self._write_int_if_changed("0028,0103", representation)
                    and previous is not None):
                _defer(
                    notes, "debug",
                    "PixelRepresentation for %s corrected from %s to %d by a "
                    "%s pixel array.", self.sop_instance_uid, previous,
                    representation, array.dtype)

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
            Whether *this* study date is one the shift produced is
            `date_shift_vouches_for` (#518).
        study_time (Optional[str]): The time of the study.
    """
    study_instance_uid: str
    study_date: Any
    series: List[Series] = field(default_factory=list)
    date_shifted: bool = False
    study_time: Optional[str] = None

    # What a `SHIFT_DATE` on this study's own date produced, as the DA
    # string (#518). The same self-invalidating shape as
    # `DicomItem._shifted_dates`, sized for the one value a `Study`
    # owns, and reached through the same two method names -- two
    # entities, one question, one vocabulary.
    #
    # `date_shifted` beside it is **not** redundant, and neither is
    # enough alone. The flag says a de-identifying shift ran on this
    # study, which is the honest entity-level question and the one
    # `exporters/wfdb.py` asks before letting the header say
    # "de-identified start date"; the record says *what the shift
    # produced*, which is what tells a fresh original written into
    # `study_date` from the shift's own output. Together they are
    # unambiguous, and that is why the study half needs no
    # `shift_provenance` column of its own the way the instance half
    # does: `True` with no record means "shifted before 0.9.6, value
    # unknowable", and the instance row carried no such witness at all
    # because `Instance.date_shifted` was never persisted.
    #
    # Private, and `init=False`, so no frozen-surface pin moves and the
    # positional constructor order is untouched.
    _shifted_study_date: Optional[str] = field(
        default=None, init=False, repr=False)

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
        # ...and the boundary for #189, which is the same boundary for
        # the same reason. A DA-spelled string was left as a string here
        # while hydration turned the identical value into a `date`, so
        # `export_folder_names` -- which builds the directory with
        # `str(study.study_date or "NoDate")`, not `format_study_date`
        # -- filed one study under `Study_20240115_` fresh and
        # `Study_2024-01-15_` reloaded. The *element* never diverged,
        # because both export paths render a `date` as `YYYYMMDD`; only
        # the folder did, which is why the element's indifference must
        # not be read as the folder's.
        #
        # Normalised at assignment rather than at the folder: routing
        # `export_folder_names` through `format_study_date` would rename
        # every *ingested* study's directory, a far larger break than
        # the one it closes. Refusing the string instead would break
        # `DicomBuilder.add_study`'s own documented example. This is the
        # one option that leaves `study_date` a single type everywhere,
        # which is `_as_loaded_date`'s stated goal for the other half of
        # the same round trip.
        if name == "study_date":
            value = normalize_study_date(value)
        # `object.__setattr__`, not zero-argument `super()`:
        # `@dataclass(slots=True)` builds a *new* class, so the closure
        # cell zero-arg super() reads still names the discarded one and
        # every assignment raises "obj must be an instance or subtype".
        object.__setattr__(self, name, value)

    def record_date_shift(self, value) -> None:
        """Records that a `SHIFT_DATE` on this study produced `value`.

        Stored as the DA string, through the one spelling of "a Study's
        date as a DA string" (#189) -- the spelling
        `_write_to_instances` and `_holds_owners_replacement` already
        compare against -- so the record and the graph are held in one
        representation and a `date` cannot disagree with its own string.

        One value and no tag, unlike `DicomItem.record_date_shift`,
        because a `Study` owns exactly one date. The method names match
        on purpose: it is the same question at another level.

        The import is local because `io_handlers` imports this module.
        """
        from .io_handlers import format_study_date  # pylint: disable=import-outside-toplevel
        self._shifted_study_date = format_study_date(value) or None

    def date_shift_vouches_for(self, value) -> bool:
        """Whether `value` is the date a shift on this study produced.

        False with no record, and False the moment `study_date` stops
        holding what the shift wrote -- which is #518: the flag recorded
        *that* a shift happened and never *what it produced*, so it
        could not tell its own output from a new input, and a fresh
        original assigned to `study_date` was never raised again.
        """
        if not self._shifted_study_date:
            return False
        from .io_handlers import format_study_date  # pylint: disable=import-outside-toplevel
        return format_study_date(value) == self._shifted_study_date

    def mark_subtree_persisted(self):
        """Marks this study and every series beneath it as stored."""
        self._persisted_revision = self._revision
        for series in self.series:
            series.mark_subtree_persisted()


#: The two ways a patient's pseudonym and date offset can be derived,
#: stored verbatim in `patients.jitter_scheme`. Spelled here rather than
#: in `privacy.py`, which derives them, because `Patient` carries one as
#: its default and `privacy` imports this module. The strings are a store
#: format: renaming one reclassifies every row that holds it.
JITTER_SCHEME_KEYED = "keyed-hmac-v1"
JITTER_SCHEME_UNKEYED = "unkeyed-sha256"


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
    # How this patient's pseudonym and date offset are derived. Keyed
    # (under the store's project secret) unless the store classed the
    # patient as de-identified before 0.9.7 when it was opened, in which
    # case the patient keeps the unkeyed scheme so its dates never carry
    # two offsets. Fixed at open by `SqliteStore`, never re-derived from
    # the entity: once a keyed shift is saved, "has a shifted date" is
    # true of keyed patients too, so only a class fixed before any keyed
    # work can tell the two apart.
    #
    # Every clone of a Patient has to copy it -- `_make_lightweight_copy`
    # above all, or every scan worker sees a legacy patient as keyed and
    # gives it a second offset. Private and `init=False`, so no
    # frozen-surface pin moves and the positional constructor is intact.
    _jitter_scheme: str = field(
        default=JITTER_SCHEME_KEYED, init=False, repr=False)

    def mark_subtree_persisted(self):
        """Marks this patient and every study beneath it as stored."""
        self._persisted_revision = self._revision
        for study in self.studies:
            study.mark_subtree_persisted()
