"""A private-element rule can name its creator: `0009,"GEMS_IDEN_01",27`
(#1132).

A private element's tag is not its identity. PS3.5 7.8.1 gives each
private creator a block (`gggg,00BB` names it, its elements are
`gggg,BBee`), and the block is assigned per file, so before this a rule
written for one vendor's element at `0009,1027` also hit another vendor's
element that sat at that tag in another file, and missed its own element
where its creator was given block `11`.

Now a `phi_tags` key `gggg,"CREATOR",ee` matches the element at offset
`ee` of whichever block of the **same item** holds `CREATOR` (surrounding
spaces are LO padding; case and runs of spaces are not compared, #1147).
It beats a concrete key
on the same element (owner ruling Q4 (a), 2026-10-16), and the row names
the creator. The `remove_private_tags` sweep is unchanged.

Creator names here are the vendor block names #1132 quotes, `HOLOGIC` and
`Hologic` from pydicom's private dictionary, the UCSF spelling #1147
quotes, and invented `ACME IMAGING` ones; every value is invented.
"""
import logging
import os
import sqlite3

import pydicom
import pytest
import yaml

from isocenter import Session, config_manager
from isocenter.entities import DicomItem, Instance
from isocenter.privacy import PhiInspector

from support.ct_small_files import write_ct

GE = '0009,"GEMS_IDEN_01",27'


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


def _instance(attrs, uid="1.2.3.4"):
    inst = Instance(sop_instance_uid=uid)
    for tag, value in attrs.items():
        inst.set_attr(tag, value)
    return inst


def _scan(rules, inst, remove_private_tags=False):
    inspector = PhiInspector(config_tags=rules,
                             remove_private_tags=remove_private_tags)
    return inspector._scan_instance(inst, "P")


def _hits(rules, inst):
    return sorted((f.tag, f.remediation_proposal.action_type)
                  for f in _scan(rules, inst))


# --- matching ---------------------------------------------------------------

def test_a_creator_key_removes_its_element_in_either_block():
    """The issue's block-drift case: one rule, the creator at block 10 in
    one file and at 11 in another. Kills the creator arm deleted (neither
    matches) and the block taken from the key instead of the item."""
    rules = {GE: {"action": "REMOVE", "name": "GE Image actual date"}}
    first = _instance({"0009,0010": "GEMS_IDEN_01", "0009,1027": "20000101"})
    second = _instance({"0009,0010": "OTHER_VENDOR", "0009,0011": "GEMS_IDEN_01",
                        "0009,1027": "kept", "0009,1127": "20000101"})
    assert _hits(rules, first) == [("0009,1027", "REMOVE_TAG")]
    assert _hits(rules, second) == [("0009,1127", "REMOVE_TAG")]


def test_a_creator_key_leaves_another_creators_element_at_the_same_tag():
    """The collision case: another creator's element at the tag the GE
    element uses elsewhere. Kills a match on the offset alone."""
    rules = {GE: {"action": "REMOVE"}}
    inst = _instance({"0009,0010": "GEMS_PETD_01", "0009,1027": "1.5"})
    assert _hits(rules, inst) == []


def test_a_creator_key_beats_the_concrete_key_on_the_same_element():
    """Owner ruling Q4 (a): the creator key names the element's identity,
    the concrete key only its slot. For another creator at that slot the
    concrete key still applies."""
    rules = {"0009,1027": {"action": "REMOVE"}, GE: {"action": "KEEP"}}
    ge = _instance({"0009,0010": "GEMS_IDEN_01", "0009,1027": "20000101"})
    other = _instance({"0009,0010": "GEMS_PETD_01", "0009,1027": "1.5"})
    assert _hits(rules, ge) == []
    assert _hits(rules, other) == [("0009,1027", "REMOVE_TAG")]


def test_the_creator_is_read_from_the_item_that_holds_the_element():
    """PS3.5 7.8.1: a sequence item has its own creators. The top level's
    creator for block 10 is another vendor's, the item's is GE's."""
    rules = {GE: {"action": "REMOVE"}}
    inst = _instance({"0009,0010": "OTHER_VENDOR", "0009,1027": "top"})
    item = DicomItem()
    item.set_attr("0009,0010", "GEMS_IDEN_01")
    item.set_attr("0009,1027", "20000101")
    inst.add_sequence_item("0008,1115", item)
    found = _scan(rules, inst)
    assert [(f.tag, f.entity is item) for f in found] == [("0009,1027", True)]


@pytest.mark.parametrize("creator, matches", [
    ("GEMS_IDEN_01 ", True), (" GEMS_IDEN_01", True),
    ("gems_iden_01", True), ("GEMS_IDEN_1", False)])
def test_creator_padding_and_case_are_ignored(creator, matches):
    inst = _instance({"0009,0010": creator, "0009,1027": "20000101"})
    assert bool(_hits({GE: {"action": "REMOVE"}}, inst)) is matches


@pytest.mark.parametrize("key_creator, file_creator, matches", [
    ("ACME IMAGING 01", "Acme Imaging 01", True),
    ("acme imaging 01", "ACME IMAGING 01", True),
    ("ACME IMAGING 01", "ACME  IMAGING 01", True),
    ("ACME  IMAGING 01", "acme imaging 01", True),
    ("ACME IMAGING 01", "ACME IMAGING 02", False),
    ("ACME IMAGING 01", "ACMEIMAGING 01", False),
    ("ÉCLAIR IMAGING 01", "Éclair Imaging 01", True),
    ("ÉCLAIR IMAGING 01", "éclair imaging 01", False),
    ("STRASSE IMAGING 01", "Straße Imaging 01", False)])
def test_a_creator_key_matches_its_creator_in_any_case_and_spacing(
        key_creator, file_creator, matches):
    """#1147: PS3.5 7.8.1 has no rule for comparing two creators, and
    vendors write one creator in more than one case. Kills the key folded
    and not the file's creator, and the reverse (rows 1 and 2); case
    folded and not spaces (rows 3 and 4); spaces removed rather than
    collapsed (a space taken out is another creator); and the fold
    widened past ASCII `a`-`z` (the `É`/`é` and `ß` rows: a non-ASCII
    letter's case is compared exactly, as the docs say)."""
    inst = _instance({"0009,0010": file_creator, "0009,1027": "20000101"})
    key = f'0009,"{key_creator}",27'
    assert bool(_hits({key: {"action": "REMOVE"}}, inst)) is matches


def test_the_row_names_the_creator_as_the_file_spells_it():
    """The key says `HOLOGIC`, the file `Hologic` (both spellings are in
    pydicom's private dictionary). The finding carries the file's
    spelling. Kills `_item_creators` or the metadata folded."""
    inst = _instance({"0019,0010": "Hologic", "0019,1027": "20000101"})
    [finding] = _scan({'0019,"HOLOGIC",27': {"action": "REMOVE"}}, inst)
    assert finding.remediation_proposal.metadata == {"private_creator": "Hologic"}


def test_a_creator_keep_matches_a_case_variant_over_a_concrete_remove():
    """Owner ruling Q1 (a) on #1147: the fold is for every action, so a
    creator KEEP still beats the concrete key on the variant's element.
    Kills a KEEP left matching exactly (#1147's option 2)."""
    rules = {"0009,1027": {"action": "REMOVE"},
             '0009,"ACME IMAGING 01",27': {"action": "KEEP"}}
    inst = _instance({"0009,0010": "Acme Imaging 01", "0009,1027": "20000101"})
    assert _scan(rules, inst, remove_private_tags=False) == []


def test_the_policy_form_keeps_the_creator_as_written():
    """Matching folds; the key does not, so the policy hash of every
    creator-keyed store stays as it was. Kills the fold moved into
    `_canonical_rule_key`."""
    from isocenter import configuration
    assert (config_manager._canonical_rule_key('0009,"Acme Imaging 01",2A')
            == '0009,"Acme Imaging 01",2a')
    policies = [configuration._canonical_policy_v1(
        {f'0009,"{creator}",27': {"action": "REMOVE"}}, False)
        for creator in ("Acme Imaging 01", "ACME IMAGING 01")]
    assert policies[0] != policies[1]


# --- two keys for one creator (owner ruling Q2 (a) on #1147) -------------------

_TWINS = ('0009,"ACME IMAGING 01",27', '0009,"Acme Imaging 01",27')


def _twin_file(tmp_path, first, second):
    path = tmp_path / "twins.yaml"
    path.write_text("privacy_profile: none\nphi_tags:\n"
                    f"  {_yaml_key(_TWINS[0])}: {first}\n"
                    f"  {_yaml_key(_TWINS[1])}: {second}\n")
    return str(path)


def test_two_keys_for_one_creator_with_different_rules_are_refused(tmp_path):
    """A silent precedence between a KEEP and a REMOVE on one element is
    #1147's shape again, so it is refused at the loader and at every
    other door, naming both keys. Kills the check deleted."""
    path = _twin_file(tmp_path, "{action: REMOVE}", "{action: KEEP}")
    with pytest.raises(ValueError) as caught:
        config_manager.ConfigLoader.load_unified_config(path)
    for key in _TWINS:
        assert repr(key) in str(caught.value), str(caught.value)
    assert "name the same private element" in str(caught.value)
    with pytest.raises(ValueError) as caught:
        PhiInspector(config_tags={_TWINS[0]: {"action": "REMOVE"},
                                  _TWINS[1]: {"action": "KEEP"}})
    assert repr(_TWINS[1]) in str(caught.value)


@pytest.mark.parametrize("first, second", [
    ("{action: REMOVE}", "{action: REMOVE}"),
    ("{action: REMOVE, name: Visit date}", "{action: REMOVE, name: Other name}"),
    ("{action: REMOVE}", "{action: remove}"),
    ("{action: REPLACE, value: null}", "{action: REPLACE}"),
    ("{action: SCRUB, target: [dates, ids]}", "{action: SCRUB, target: [ids, dates]}")])
def test_two_keys_for_one_creator_with_one_rule_load_and_the_first_names_it(
        tmp_path, first, second):
    """Rules that act the same load (MIDI-B's two UCSF keys are this
    case): apart from `name`, `value: null` beside no `value`, and a
    `SCRUB` target in another order. Kills the comparison taking `name`
    in, reading `value: null` as a value, or comparing a target's order."""
    loaded = config_manager.ConfigLoader.load_unified_config(
        _twin_file(tmp_path, first, second))[0]
    assert set(_TWINS) <= set(loaded)
    inst = _instance({"0009,0010": "acme imaging 01", "0009,1027": "20000101 1234567"})
    [finding] = _scan(loaded, inst)
    assert _TWINS[0] in finding.reason, finding.reason


@pytest.mark.parametrize("first, second", [
    ("{action: REPLACE, value: X}", "{action: REPLACE}"),
    ("{action: SCRUB, target: [dates, ids]}", "{action: SCRUB, target: [ids]}"),
    ("{action: REMOVE}", "REMOVE")])
def test_two_keys_for_one_creator_that_act_differently_are_still_refused(
        tmp_path, first, second):
    """The normalising reads only what acts the same as the same: a value
    against none, another set of kinds, a bare string (a name, so a
    REPLACE) against a REMOVE. Kills the normalising widened to drop
    `value` or `target`."""
    with pytest.raises(ValueError, match="name the same private element"):
        config_manager.ConfigLoader.load_unified_config(
            _twin_file(tmp_path, first, second))


def test_set_phi_tag_on_one_twin_sets_the_rule_on_both(tmp_path):
    """Both keys name one element, so a rule set through one is set on
    the other too: the policy stays loadable, and the element gets the
    new rule whichever key the scan finds first. Kills the twins left
    as they were (the next `audit()` would refuse the policy)."""
    _source(tmp_path)
    rules = (f"  {_yaml_key(_TWINS[0])}: {{action: REMOVE}}\n"
             f"  {_yaml_key(_TWINS[1])}: {{action: REMOVE}}\n")
    with _session(tmp_path, rules) as session:
        session.configuration.set_phi_tag(_TWINS[1], "KEEP")
        tags = session.configuration.phi_tags
        assert tags[_TWINS[0]]["action"] == "KEEP"
        assert tags[_TWINS[1]]["action"] == "KEEP"
        assert tags[_TWINS[0]] is not tags[_TWINS[1]]
        config_manager.validate_phi_policy(tags, "test")
        inst = _instance({"0009,0010": "Acme Imaging 01", "0009,1027": "20000101"})
        twins = {key: tags[key] for key in _TWINS}
        assert _scan(twins, inst, remove_private_tags=False) == []


def test_set_phi_tag_leaves_a_key_for_another_element_alone(tmp_path):
    """Only a key naming the same element is a twin: another offset, or
    another creator, keeps its rule. Kills the identity compared without
    its offset or its creator."""
    _source(tmp_path)
    others = ('0009,"ACME IMAGING 01",28', '0009,"ACME IMAGING 02",27')
    rules = "".join(f"  {_yaml_key(key)}: {{action: REMOVE}}\n" for key in others)
    with _session(tmp_path, rules) as session:
        session.configuration.set_phi_tag(_TWINS[1], "KEEP")
        for key in others:
            assert session.configuration.phi_tags[key]["action"] == "REMOVE"


def test_an_element_with_no_creator_in_its_block_matches_no_creator_key():
    inst = _instance({"0009,1027": "20000101"})
    assert _hits({GE: {"action": "REMOVE"}}, inst) == []


def test_the_finding_names_the_key_and_carries_the_creator():
    inst = _instance({"0009,0011": "GEMS_IDEN_01", "0009,1127": "20000101"})
    [finding] = _scan({GE: {"action": "REMOVE", "name": "GE Image actual date"}},
                      inst)
    assert finding.reason == (
        'Matched PHI Tag 0009,1127 by 0009,"GEMS_IDEN_01",27 '
        "(GE Image actual date)")
    assert finding.remediation_proposal.metadata == {
        "private_creator": "GEMS_IDEN_01"}


def test_a_creator_key_on_a_private_sequence():
    """Scan step 3 resolves creator keys against the item that owns the
    sequence. Kills step 3 reading the concrete key only."""
    inst = _instance({"0009,0010": "ACME_SEQ_01"})
    inst.add_sequence_item("0009,1020", DicomItem())
    rules = {'0009,"ACME_SEQ_01",20': {"action": "REMOVE"}}
    assert _hits(rules, inst) == [("0009,1020", "REMOVE_TAG")]
    other = _instance({"0009,0010": "ACME_SEQ_02"})
    other.add_sequence_item("0009,1020", DicomItem())
    assert _hits(rules, other) == []


# --- through a session --------------------------------------------------------

def _source(tmp_path, block=0x10):
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 1132)
    ds = pydicom.dcmread(path)
    # CT_small carries GE's own blocks; start from none, so the block each
    # creator lands in is the one this test chose.
    ds.remove_private_tags()
    if block == 0x11:
        ds.private_block(0x0009, "OTHER_VENDOR", create=True).add_new(0x27, "LO", "kept")
    ge = ds.private_block(0x0009, "GEMS_IDEN_01", create=True)
    ge.add_new(0x27, "LO", "20000101")
    # A UI copy of the Study UID under the same creator, for the #765 arm.
    ge.add_new(0x1E, "UI", ds.StudyInstanceUID)
    ds.save_as(path, enforce_file_format=True)
    return ds


def _session(tmp_path, rules_yaml, remove="false"):
    (tmp_path / "cfg.yaml").write_text(
        "privacy_profile: basic\n"
        f"remove_private_tags: {remove}\nphi_tags:\n{rules_yaml}")
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(tmp_path / "in"))
    session.load_config(str(tmp_path / "cfg.yaml"))
    return session


def _rows(session, action_type):
    session.store_backend.flush_audit_queue()
    with sqlite3.connect(session.persistence_file) as conn:
        return [row[0] for row in conn.execute(
            "SELECT details FROM audit_log WHERE action_type=?", (action_type,))]


def _instance_of(session):
    [patient] = session.store.patients
    return patient.studies[0].series[0].instances[0]


def test_the_row_names_the_creator(tmp_path):
    _source(tmp_path, block=0x11)
    with _session(tmp_path, f"  '{GE}': {{action: REMOVE}}\n") as session:
        session.anonymize(session.audit())
        inst = _instance_of(session)
        assert "0009,1127" not in inst.attributes
        assert inst.attributes["0009,1027"] == "kept"
        rows = [r for r in _rows(session, "REMEDIATION_REMOVE") if "0009,1127" in r]
    assert len(rows) == 1
    assert "(creator GEMS_IDEN_01)" in rows[0]
    assert "20000101" not in rows[0]


def test_a_replace_row_names_the_creator(tmp_path):
    _source(tmp_path)
    with _session(tmp_path,
                  f"  '{GE}': {{action: REPLACE, value: X}}\n") as session:
        session.anonymize(session.audit())
        assert _instance_of(session).attributes["0009,1027"] == "X"
        rows = [r for r in _rows(session, "REMEDIATION_REPLACE") if "0009,1027" in r]
    assert len(rows) == 1 and "(creator GEMS_IDEN_01)" in rows[0], rows


def test_a_creator_keep_is_not_given_a_private_uid_copy(tmp_path):
    """The #765 arm skips a private element with a rule of its own; a
    creator-scoped rule is one. Kills that arm's lookup ignoring creator
    keys: the KEEP'd copy would be replaced."""
    src = _source(tmp_path)
    with _session(tmp_path,
                  "  '0009,\"GEMS_IDEN_01\",1e': {action: KEEP}\n") as session:
        report = session.audit()
        assert "0009,101e" not in {f.tag for f in report.findings}
        session.anonymize(report)
        assert (_instance_of(session).attributes["0009,101e"]
                == src.StudyInstanceUID)


def test_the_uid_copy_arm_still_replaces_without_the_creator_rule(tmp_path):
    """The control for the test above: without the rule the copy is raised."""
    _source(tmp_path)
    with _session(tmp_path, "  '0010,1000': {action: REMOVE}\n") as session:
        assert "0009,101e" in {f.tag for f in session.audit().findings}


UCSF_KEY = '0117,"UCSF BIRP PRIVATE CREATOR 011710XX",c4'


def _ucsf_source(tmp_path):
    """#1147's other spelling of the creator, holding an invented note."""
    path = write_ct(tmp_path / "in" / "a.dcm", "PA", 1147)
    ds = pydicom.dcmread(path)
    ds.remove_private_tags()
    block = ds.private_block(0x0117, "UCSF BIRP PRIVATE CREATOR 011710xx", create=True)
    block.add_new(0xC4, "LO", "visit note 4826153907")
    ds.save_as(path, enforce_file_format=True)


def _exported(session, tmp_path):
    out = tmp_path / "out"
    session.export(str(out), use_compression=False, show_progress=False)
    [path] = list(out.rglob("*.dcm"))
    return pydicom.dcmread(path)


@pytest.mark.parametrize("key, removed", [
    (UCSF_KEY, True),
    # The control: another creator's key leaves the element as it was.
    ('0117,"ACME IMAGING 01",c4', False)])
def test_the_issues_two_spellings_are_one_creator(tmp_path, key, removed):
    """#1147: the key spells the creator `...XX`, the file `...xx`. A
    REMOVE that reaches the exported file is what the issue is about.
    Under the sweep every private element goes whatever the key says, so
    this runs with it off."""
    _ucsf_source(tmp_path)
    with _session(tmp_path, f"  {_yaml_key(key)}: {{action: REMOVE}}\n") as session:
        assert session.configuration.remove_private_tags is False
        session.anonymize(session.audit())
        ds = _exported(session, tmp_path)
        rows = [r for r in _rows(session, "REMEDIATION_REMOVE") if "0117,10c4" in r]
    assert (0x011710C4 in ds) is not removed
    if removed:
        assert len(rows) == 1
        assert "(creator UCSF BIRP PRIVATE CREATOR 011710xx)" in rows[0], rows
        assert "4826153907" not in rows[0]


# --- every door keeps the creator's case ------------------------------------

def _raised(session):
    return "0009,1027" in {f.tag for f in session.audit().findings}


def test_the_creator_keeps_its_case_through_the_loader(tmp_path):
    _source(tmp_path)
    with _session(tmp_path, f"  '{GE}': {{action: REMOVE}}\n") as session:
        assert GE in session.configuration.phi_tags
        assert _raised(session)


def test_the_creator_keeps_its_case_through_set_phi_tag(tmp_path):
    _source(tmp_path)
    with _session(tmp_path, "  '0010,1000': {action: REMOVE}\n") as session:
        session.configuration.set_phi_tag('0009,"GEMS_IDEN_01",27', "REMOVE")
        assert GE in session.configuration.phi_tags
        assert _raised(session)


def test_the_creator_keeps_its_case_through_an_assignment(tmp_path):
    """Assigned in code, the key reaches `audit()` as written."""
    _source(tmp_path)
    with _session(tmp_path, "  '0010,1000': {action: REMOVE}\n") as session:
        session.configuration.phi_tags[GE] = {"action": "REMOVE"}
        assert _raised(session)


def test_the_creator_keeps_its_case_through_the_inspector():
    inst = _instance({"0009,0010": "GEMS_IDEN_01", "0009,10AB": "20000101"})
    rules = {'0009,"GEMS_IDEN_01",AB': {"action": "REMOVE"}}
    inspector = PhiInspector(config_tags=rules)
    assert list(inspector.phi_tags) == ['0009,"GEMS_IDEN_01",ab']
    assert [f.tag for f in inspector._scan_instance(inst, "P")] == ["0009,10ab"]


def test_a_creator_key_round_trips_through_save_and_create_config(tmp_path):
    _source(tmp_path)
    with _session(tmp_path, f"  '{GE}': {{action: REMOVE, name: GE date}}\n") as session:
        session.configuration.save()
        session.create_config(str(tmp_path / "scaffold.yaml"))
        policy = dict(session.configuration.phi_tags)
    for name in ("cfg.yaml", "scaffold.yaml"):
        loaded = config_manager.ConfigLoader.load_unified_config(str(tmp_path / name))[0]
        assert loaded[GE] == {"action": "REMOVE", "name": "GE date"}, name
        if name == "cfg.yaml":
            assert loaded == policy


# --- the loader --------------------------------------------------------------

def _load(tmp_path, key, rule="{action: REMOVE}"):
    path = tmp_path / "k.yaml"
    path.write_text("privacy_profile: none\nphi_tags:\n"
                    f"  {_yaml_key(key)}: {rule}\n")
    return config_manager.ConfigLoader.load_unified_config(str(path))[0]


def _yaml_key(key):
    return "'" + key.replace("'", "''") + "'"


@pytest.mark.parametrize("key", [
    '0008,"X",10',            # an even group is not private
    '0001,"X",10', '0003,"X",10', '0005,"X",10', '0007,"X",10',
    'ffff,"X",10',            # PS3.5 7.8.1: not private groups
    '0009,"",10',             # no creator
    '0009,"' + "C" * 65 + '",10',   # longer than an LO
    '0009,"A\\B",10',         # a backslash is LO's delimiter
    '0009,"A\tB",10',         # a control character
    '0009,"X",1',             # one-digit offset
    '0009,"X",100',           # three-digit offset
    '0009,GEMS,27',           # unquoted
])
def test_a_malformed_creator_key_is_refused_by_name(tmp_path, key):
    with pytest.raises(ValueError) as caught:
        _load(tmp_path, key)
    assert repr(key) in str(caught.value), str(caught.value)
    if '"' in key:
        # Every door names a key that tries to be a creator key; an
        # unquoted one is the loader's non-tag refusal, as before.
        with pytest.raises(ValueError) as caught:
            PhiInspector(config_tags={key: {"action": "REMOVE"}})
        assert "is not a private-creator key" in str(caught.value)


def test_a_64_character_creator_loads(tmp_path):
    key = '0009,"' + "C" * 64 + '",10'
    assert key in _load(tmp_path, key)


@pytest.mark.parametrize("rule", [
    "{action: KEEP}", "{action: REMOVE}", "{action: EMPTY}",
    "{action: REPLACE}", "{action: REPLACE, value: X}",
    "{action: SHIFT}", "{action: JITTER}"])
def test_every_action_loads_on_a_creator_key_as_on_a_concrete_private_key(tmp_path, rule):
    assert _load(tmp_path, GE, rule)[GE]
    assert _load(tmp_path, "0009,1027", rule)["0009,1027"]


def test_a_creator_key_and_its_lowercased_twin_collide_with_a_warning(caplog):
    """Two spellings differing only in hex case are one key, with the
    collision WARNING; two keys for one creator differing in its case are
    two keys, here with one rule, so they load (#1147)."""
    with caplog.at_level(logging.WARNING, logger="isocenter"):
        inspector = PhiInspector(config_tags={
            '0009,"GEMS_IDEN_01",2A': {"action": "KEEP"},
            '0009,"GEMS_IDEN_01",2a': {"action": "REMOVE"},
            '0009,"gems_iden_01",2a': {"action": "REMOVE"}})
    assert list(inspector.phi_tags) == ['0009,"GEMS_IDEN_01",2a',
                                       '0009,"gems_iden_01",2a']
    assert inspector.phi_tags['0009,"GEMS_IDEN_01",2a'] == {"action": "REMOVE"}
    assert any("collision" in r.getMessage() for r in caplog.records)


def test_the_canonical_key_strips_creator_padding():
    assert config_manager._canonical_rule_key('0009," GEMS_IDEN_01 ",2A') == GE.replace("27", "2a")
    assert config_manager._canonical_rule_key("0008,103E") == "0008,103e"
    assert config_manager._canonical_rule_key("60XX,XXXX") == "60xx,xxxx"


def test_the_sweep_still_removes_an_element_a_creator_keep_names(tmp_path):
    """Stated limit: under `remove_private_tags: true` a creator KEEP does
    not keep the element, as a concrete KEEP does not today."""
    inst = _instance({"0009,0010": "GEMS_IDEN_01", "0009,1027": "x"})
    found = _scan({GE: {"action": "KEEP"}}, inst, remove_private_tags=True)
    assert "0009,1027" in {f.tag for f in found}


def test_no_value_reaches_the_log(tmp_path, caplog):
    _source(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="isocenter"):
        with _session(tmp_path, f"  '{GE}': {{action: REMOVE}}\n") as session:
            session.anonymize(session.audit())
    assert "20000101" not in caplog.text
    assert os.path.exists(tmp_path / "s.db")
