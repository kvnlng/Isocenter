"""Every entity answers the same question the same way.

A session tracks two unrelated things about an item: whether it has
changes not yet written to the store, and whether it still carries
identifiers. Both were called "dirty". These tests pin the first one --
persistence state -- to a single vocabulary that every entity type
shares, so the second can be given its own without either being mistaken
for the other.

`has_unsaved_changes` is deliberately read-only. State moves through
`mark_modified()` and `mark_persisted()`, so an entity can be told what
happened to it but cannot be told what it is.
"""
import pytest

from isocenter.entities import (Patient, Study, Series, Instance, DicomItem,
                                PhiStatus, TrackedEntity)


def make_entities():
    """One of each, so tests can assert the API is genuinely uniform."""
    return {
        "Patient": Patient("P1", "Test^Patient"),
        "Study": Study("S1", "20230101"),
        "Series": Series("SE1", "CT", 1),
        "Instance": Instance("I1", "/tmp/x.dcm"),
        # Sequence items are bare `DicomItem`s, not `Instance`s. They are
        # deep-copied into worker clones by `_make_lightweight_copy` and
        # carry nested findings back, so the identity tests below cover
        # them directly rather than only through `Instance`.
        "DicomItem": DicomItem(),
    }


@pytest.mark.parametrize("name", ["Patient", "Study", "Series", "Instance"])
def test_a_new_entity_has_unsaved_changes(name):
    """Anything built in memory is unwritten until the store says otherwise."""
    entity = make_entities()[name]
    assert entity.has_unsaved_changes is True


@pytest.mark.parametrize("name", ["Patient", "Study", "Series", "Instance"])
def test_persisting_then_modifying_moves_the_state_back(name):
    """The round trip is the whole contract, and it is the same for all four."""
    entity = make_entities()[name]

    entity.mark_persisted()
    assert entity.has_unsaved_changes is False

    entity.mark_modified()
    assert entity.has_unsaved_changes is True


@pytest.mark.parametrize("name", ["Patient", "Study", "Series", "Instance"])
def test_the_state_cannot_be_assigned_directly(name):
    """No caller gets to declare an entity saved without saving it.

    The old flag was a writable attribute, so `entity._dirty = False`
    read as "this is stored" from anywhere in the codebase -- which is
    how a rolled-back save came to leave instances claiming they had
    been written.
    """
    entity = make_entities()[name]

    with pytest.raises(AttributeError):
        entity.has_unsaved_changes = False


def test_marking_a_patient_persisted_does_not_reach_its_children():
    """`mark_persisted` speaks for one entity; the subtree form is explicit.

    Loading a graph from the store marks the whole tree; committing a
    single row must not, or one saved instance would silently vouch for
    every unsaved sibling under the same patient.
    """
    patient = Patient("P1", "Test^Patient")
    study = Study("S1", "20230101")
    series = Series("SE1", "CT", 1)
    instance = Instance("I1", "/tmp/x.dcm")
    series.instances.append(instance)
    study.series.append(series)
    patient.studies.append(study)

    patient.mark_persisted()

    assert patient.has_unsaved_changes is False
    assert instance.has_unsaved_changes is True

    patient.mark_subtree_persisted()

    assert instance.has_unsaved_changes is False
    assert series.has_unsaved_changes is False


def test_an_instance_records_which_revision_was_persisted():
    """Concurrent edits must not be lost to a save that started earlier.

    `mark_persisted(revision)` names the version that reached the store.
    An edit arriving while the save was in flight leaves the instance
    ahead of that revision, so it stays unsaved rather than being
    written off by a commit that never contained it.
    """
    instance = Instance("I1", "/tmp/x.dcm")
    in_flight = instance._revision

    instance.set_attr("0010,0010", "edited during the save")
    instance.mark_persisted(in_flight)

    assert instance.has_unsaved_changes is True


# --- Identity, not field values (#299) ----------------------------------
#
# Two entities holding equal fields are the ORDINARY case in this
# codebase, not a curiosity: the fixture builders in `scripts/` construct
# graphs by hand, and `_make_lightweight_copy` rebuilds entities
# field-by-field for the worker processes. So the question "are these the
# same record?" must be answered by identity, and an entity must be
# usable as a dict key.
#
# The near-miss that makes this worth pinning is PR #296: a review
# prescribed `prepared[inst]` reasoning that entities "hash by identity".
# Under the dataclass default they did not -- the first lookup would have
# raised `TypeError` (unhashable), and had it been hashable by value, two
# distinct instances with matching fields would have been silently
# conflated into one entry.


@pytest.mark.parametrize(
    "name", ["Patient", "Study", "Series", "Instance", "DicomItem"]
)
def test_two_entities_with_equal_fields_are_not_equal(name):
    """Equal fields do not make two records the same record."""
    a = make_entities()[name]
    b = make_entities()[name]

    # pylint: disable=comparison-with-itself
    # Comparing an entity to itself IS the assertion here: identity
    # semantics mean `a == a` and `a != b` are the two halves of one
    # contract, and dropping the reflexive half would leave a trivially
    # broken `__eq__` (one that returns False for everything) green.
    assert a == a
    assert a != b


@pytest.mark.parametrize(
    "name", ["Patient", "Study", "Series", "Instance", "DicomItem"]
)
def test_entities_are_hashable_and_distinct_in_a_set(name):
    """An entity is usable as a dict key and does not collide with a twin.

    Five parameters defend six classes: `__hash__ = None` propagates down
    the MRO, so dropping `eq=False` from either base makes every leaf
    unhashable and turns all of these red.
    """
    a = make_entities()[name]
    b = make_entities()[name]

    assert len({a, b}) == 2
    assert {a: 1}[a] == 1


# --- The coupling behind remediation's redundant bumps (#132) ------------


@pytest.mark.parametrize(
    "name", ["Patient", "Study", "Series", "Instance", "DicomItem"]
)
def test_recording_a_new_phi_status_leaves_the_entity_needing_a_save(name):
    """A new PHI status is itself a change the store has not got.

    This test kills NONE of the twelve `remediation.py` mutation
    survivors, and saying so is the point of writing it here. It lives
    in a file that never imports the remediation module, so the probe
    does not run it for that module. (The module is named without its
    package prefix on purpose: `test_mutation_probe_targets.py` scans
    whole test files for the text `isocenter.<module>`, not their
    import statements, so writing the dotted name even inside a
    docstring makes that check demand this file be added to `TARGETS`.)

    What it does is convert an ASSUMED coupling into a checked one.
    Four `entity.mark_modified()` calls in `remediation.py` survive
    deletion, and the reason they survive is that `record_phi_status()`
    on the same paths also advances `_revision` -- so the bump is
    redundant, not missing. That reasoning is documented in CLAUDE.md
    and nothing tested it. If the coupling ever silently went away,
    those four calls would become load-bearing and every one of them
    would still survive deletion in isolation, with nothing anywhere
    noticing. Now something does.
    """
    entity = make_entities()[name]
    entity.mark_persisted()
    assert entity.has_unsaved_changes is False

    entity.record_phi_status(PhiStatus.IDENTIFIED)

    assert entity.has_unsaved_changes is True


@pytest.mark.parametrize(
    "name", ["Patient", "Study", "Series", "Instance", "DicomItem"]
)
def test_recording_the_status_an_entity_already_carries_changes_nothing(name):
    """The other half of the contract, so neither half can drift alone.

    The bump is conditional: `record_phi_status` short-circuits when the
    status is unchanged, so repeated scans of unchanged data do not
    force a rewrite of the whole graph. Pinning only the bump would let
    someone make it unconditional and stay green while every re-scan
    dirtied everything it touched.
    """
    entity = make_entities()[name]
    entity.record_phi_status(PhiStatus.IDENTIFIED)
    entity.mark_persisted()
    assert entity.has_unsaved_changes is False

    entity.record_phi_status(PhiStatus.IDENTIFIED)

    assert entity.has_unsaved_changes is False


def test_every_tracked_entity_subclass_hashes_by_identity():
    """A future entity class cannot quietly reintroduce the #299 bug.

    The parametrized tests above enumerate class names by hand, so a new
    `TrackedEntity` subclass added later with the dataclass default
    `eq=True` would be unhashable and value-comparing, and nothing would
    say so -- the very shape of gap this milestone exists to find. This
    walks the module instead of a list, so the check arrives with the
    class rather than having to be remembered.

    `__hash__ is None` is the precise symptom: `eq=True` sets it, and it
    propagates down the MRO, so this catches a missing `eq=False` on a
    new leaf AND on a new intermediate base.

    `Equipment` is deliberately not a `TrackedEntity`, so it is out of
    scope here by construction rather than by exclusion -- it is frozen
    precisely so that value-hashing works.
    """
    import inspect

    from isocenter import entities as entities_module

    subclasses = [
        obj for _, obj in inspect.getmembers(entities_module, inspect.isclass)
        if issubclass(obj, TrackedEntity)
    ]
    assert len(subclasses) >= 5, (
        "the walk found almost nothing, so it would pass vacuously; "
        f"found {[c.__name__ for c in subclasses]}")

    unhashable = [c.__name__ for c in subclasses if c.__hash__ is None]
    assert not unhashable, (
        f"{unhashable} carry the dataclass default `eq=True`, so they are "
        "unhashable and compare by field values; entities must compare by "
        "identity (#299) and `eq=False` is needed on the class AND its bases")
