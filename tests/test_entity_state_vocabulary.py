"""Every entity answers the same question the same way.

A session tracks two unrelated things about an item: whether it has
changes not yet written to the store, and whether it still carries
identifiers. Both were called "dirty". These tests pin the first one --
persistence state -- to a single vocabulary that all four entity types
share, so the second can be given its own without either being mistaken
for the other.

`has_unsaved_changes` is deliberately read-only. State moves through
`mark_modified()` and `mark_persisted()`, so an entity can be told what
happened to it but cannot be told what it is.
"""
import pytest

from isocenter.entities import Patient, Study, Series, Instance


def make_entities():
    """One of each, so tests can assert the API is genuinely uniform."""
    return {
        "Patient": Patient("P1", "Test^Patient"),
        "Study": Study("S1", "20230101"),
        "Series": Series("SE1", "CT", 1),
        "Instance": Instance("I1", "/tmp/x.dcm"),
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
