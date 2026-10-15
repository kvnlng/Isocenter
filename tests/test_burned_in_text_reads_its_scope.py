"""The `burned_in_text` policy reads the images its `scope` names (#1121).

Measured on MIDI-B Validation with 1.0.0rc19: the policy read every image
holding pixels, about 24,000, nearly all CT, MR and PET slices; `redact()`
went from 0.1 s to 24 s on a 66-file sample, and two of the four text-free
images it blanked were a CT scout and a PET slice. The owner's rulings of
2026-10-15, which these tests hold:

- `scope: flagged | modalities | all` is **required**; a block without it
  is refused at load, naming the values. `modalities: [CR, DX, MG]` is
  required under `scope: modalities` and refused under any other scope;
- `flagged` reads an image whose Burned In Annotation says `YES` (strip,
  any case), **or** that this store has redacted, **or** whose
  `burned_in_text` record holds a box (Q-A1 (a)). The last two close a
  fail-open: a zone redaction writes `(0028,0301) NO`, and read by the flag
  alone the next `export()` would write the text `withhold` promised to
  keep out;
- `modalities` reads what `flagged` reads, and every image of a series
  whose Modality is listed;
- one predicate decides for the three readers: the `redact()` pass, the
  plain-export gate, and `export(check_burned_in=True)`'s read;
- a pass that selects nothing costs no grade; the pass row and section 5
  say how many were out of scope (Q-A2 (a));
- `scope` and `modalities` are not in the policy hash (they choose images,
  not boxes); the box rule's version is, so every rc19 record is read again.

**Why this file imports what it does.** The loader through
`isocenter.config_manager`, the readers through `isocenter.session`, the
section 5 line through `isocenter.reporting`, so each module's probe row
is charged. The OCR stand-in and the image writer are the redact tests';
threads only.
"""
import hashlib
import json

import numpy as np
import pytest
import yaml

from isocenter import config_manager, entities, pixel_analysis, services
from isocenter import session as session_module
from isocenter.config_manager import ConfigLoader
from isocenter.entities import Instance
from isocenter.reporting import BurnedInTextSummary, ComplianceReport
from isocenter.session import DicomSession as Session

from test_redact_blanks_burned_in_text import (
    INK, INK_BOX, FakeTesseract, audit_rows, exported_files, pixels_of,
    write_image, write_sr)


@pytest.fixture(autouse=True)
def _threads(monkeypatch):
    monkeypatch.setenv("ISOCENTER_MAX_WORKERS", "2")
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    monkeypatch.delenv("ISOCENTER_FORCE_PROCESSES", raising=False)
    monkeypatch.delenv("ISOCENTER_MAX_TASKS_PER_CHILD", raising=False)


@pytest.fixture
def ocr(monkeypatch):
    fake = FakeTesseract()
    monkeypatch.setattr(pixel_analysis, "HAS_OCR", True)
    monkeypatch.setattr(pixel_analysis, "pytesseract", fake)
    return fake


@pytest.fixture
def reads(monkeypatch, ocr):
    """The source SOP UID of every instance handed to OCR, in order."""
    seen = []
    real = pixel_analysis._text_boxes

    def spy(instance, *args, **kwargs):
        seen.append(_source_uid(instance))
        return real(instance, *args, **kwargs)

    monkeypatch.setattr(pixel_analysis, "_text_boxes", spy)
    return seen


def _source_uid(inst):
    return inst.attributes.get(entities.SOURCE_SOP_UID_ATTR, inst.sop_instance_uid)


def _write(tmp_path, text, name="c.yaml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _loaded(tmp_path, text):
    return ConfigLoader.load_unified_config(str(_write(tmp_path, text)))


def _refusal(tmp_path, text):
    path = _write(tmp_path, text)
    with pytest.raises(ValueError) as caught:
        ConfigLoader.load_unified_config(str(path))
    return str(path), str(caught.value)


# --- the loader ---------------------------------------------------------------

def test_an_rc19_policy_without_a_scope_is_refused_naming_the_values(tmp_path):
    """The Breaking change: `{action: redact}` loaded in rc19 and reads
    every image holding pixels; 1.0 makes the author choose."""
    path, message = _refusal(tmp_path, "burned_in_text: {action: redact}\n")
    assert message == (f"{path}: 'burned_in_text' needs a 'scope': one of "
                       f"flagged, modalities, all")


@pytest.mark.parametrize("body, reason", [
    ("{action: redact, scope: null}",
     "'burned_in_text' scope None is not one of flagged, modalities, all "
     "(exact spelling)"),
    ("{action: redact, scope: Flagged}",
     "'burned_in_text' scope 'Flagged' is not one of flagged, modalities, all "
     "(exact spelling)"),
    ("{action: redact, scope: modalities}",
     "'burned_in_text' scope modalities needs 'modalities': a list of "
     "Modality values such as [CR, DX, MG]"),
    ("{action: redact, scope: all, modalities: [CR]}",
     "'burned_in_text' modalities applies only under scope: modalities; "
     "scope is 'all'"),
    ("{action: redact, scope: flagged, modalities: [CR]}",
     "'burned_in_text' modalities applies only under scope: modalities; "
     "scope is 'flagged'"),
    ("{action: redact, scope: modalities, modalities: CR}",
     "'burned_in_text' modalities must be a list such as [CR, DX, MG], "
     "got 'CR' (str)"),
    ("{action: redact, scope: modalities, modalities: []}",
     "'burned_in_text' modalities is empty; name at least one Modality, or "
     "use scope: flagged"),
    ("{action: redact, scope: modalities, modalities: [cr]}",
     "'burned_in_text' modalities item 'cr' is not a Modality as DICOM "
     "writes it (upper case: CR, DX, MG)"),
    ("{action: redact, scope: modalities, modalities: [CR, '']}",
     "'burned_in_text' modalities item '' is not a Modality as DICOM "
     "writes it (upper case: CR, DX, MG)"),
    ("{action: redact, scope: modalities, modalities: [' ']}",
     "'burned_in_text' modalities item ' ' is not a Modality as DICOM "
     "writes it (upper case: CR, DX, MG)"),
    ("{action: redact, scope: modalities, modalities: [CR, 7]}",
     "'burned_in_text' modalities item 7 is not a Modality as DICOM "
     "writes it (upper case: CR, DX, MG)"),
    ("{action: redact, scope: modalities, modalities: [ABCDEFGHIJKLMNOPQ]}",
     "'burned_in_text' modalities item 'ABCDEFGHIJKLMNOPQ' is not a Modality "
     "as DICOM writes it (upper case: CR, DX, MG)"),
    ("{action: redact, scope: modalities, modalities: ['C-R']}",
     "'burned_in_text' modalities item 'C-R' is not a Modality as DICOM "
     "writes it (upper case: CR, DX, MG)"),
], ids=["null", "case", "modalities-missing", "modalities-under-all",
        "modalities-under-flagged", "bare-string", "empty", "lower-case",
        "blank-item", "space-item", "number-item", "too-long", "hyphen"])
def test_each_scope_refusal_says_one_sentence(tmp_path, body, reason):
    """Each refusal in full, so a check deleted or reworded is red. A
    `null` scope is refused as a value, never read as absent: only the
    whole key's `null` is absent. Modality is not case-folded, as no
    `action` is (#754)."""
    path, message = _refusal(tmp_path, f"burned_in_text: {body}\n")
    assert message == f"{path}: {reason}"


def test_the_action_is_judged_before_the_scope(tmp_path):
    """The refusal order: one sentence per file, and the action's first."""
    path, message = _refusal(tmp_path, "burned_in_text: {action: blank}\n")
    assert message == (f"{path}: 'burned_in_text' action 'blank' is not one of "
                       f"report, redact, withhold (exact spelling)")


@pytest.mark.parametrize("scope", ["flagged", "all"])
def test_a_scope_with_no_list_loads_with_no_modalities_key(tmp_path, scope):
    parts = _loaded(tmp_path, f"burned_in_text: {{action: report, scope: {scope}}}\n")
    assert parts[5] == {"action": "report", "scope": scope, "min_confidence": 80}


def test_the_modality_list_is_normalised_to_sorted_distinct_values(tmp_path):
    """A repeat changes nothing a reader sees, and a Code String's
    padding is not part of its value."""
    parts = _loaded(tmp_path, "burned_in_text: {action: redact, scope: modalities, "
                              "modalities: [DX, CR, CR, ' MG ']}\n")
    assert parts[5] == {"action": "redact", "scope": "modalities",
                        "modalities": ["CR", "DX", "MG"], "min_confidence": 80}


@pytest.mark.parametrize("policy", [
    {"action": "withhold", "scope": "flagged", "min_confidence": 70},
    {"action": "report", "scope": "all", "min_confidence": 80},
    {"action": "redact", "scope": "modalities", "modalities": ["CR", "US"],
     "min_confidence": 90},
], ids=["flagged", "all", "modalities"])
def test_save_and_create_config_round_trip_each_scope(tmp_path, policy):
    path = _write(tmp_path, "burned_in_text: "
                  + json.dumps(policy) + "\n")
    with Session(str(tmp_path / "s.db")) as session:
        session.load_config(str(path))
        assert session.configuration.burned_in_text == policy
        session.configuration.save()
        assert yaml.safe_load(path.read_text())["burned_in_text"] == policy
        out = tmp_path / "scaffold.yaml"
        session.create_config(str(out))
        session.load_config(str(out))
        assert session.configuration.burned_in_text == policy


def test_a_policy_assigned_in_code_without_a_scope_is_refused_at_redact(tmp_path, ocr):
    with Session(str(tmp_path / "s.db")) as session:
        session.configuration.burned_in_text = {"action": "redact"}
        with pytest.raises(ValueError) as caught:
            session.redact(show_progress=False)
    assert str(caught.value) == ("session.configuration: 'burned_in_text' needs "
                                 "a 'scope': one of flagged, modalities, all")


# --- the predicate ------------------------------------------------------------

def _image(**attributes):
    inst = Instance(sop_instance_uid="1.2.3.4",
                    pixel_array=np.zeros((4, 4), dtype=np.uint8))
    inst.attributes.update(attributes)
    return inst


FLAGGED = {"action": "withhold", "scope": "flagged", "min_confidence": 80}


@pytest.mark.parametrize("value, inside", [
    ("YES", True), (" yes ", True), (["NO", "YES"], True),
    ("NO", False), (None, False), (b"YES", False), ("YESNO", False),
], ids=["YES", "padded-lower", "multi", "NO", "absent", "bytes", "not-YES"])
def test_flagged_reads_burned_in_annotation_yes_in_any_case(value, inside):
    """`bytes` is not read as YES: not seen, and said in the docstring."""
    inst = _image() if value is None else _image(**{"0028,0301": value})
    assert session_module._in_burned_in_text_scope(inst, "CT", FLAGGED) is inside


def test_flagged_reads_an_image_this_store_redacted():
    inst = _image(**{"0028,0301": "NO", "_ISOCENTER_REDACTION_HASH": "h"})
    assert session_module._in_burned_in_text_scope(inst, "CT", FLAGGED)


def test_flagged_reads_an_image_whose_record_holds_a_box_and_no_other():
    """A record with a box keeps the image gated; a record with none
    (a text-free image read under `all`) does not, or narrowing the scope
    would read every image forever."""
    boxed = _image(**{"0028,0301": "NO"})
    services.write_burned_in_text_record(
        boxed, {"policy": "p", "boxes": [[0, 1, 2, 3, 4]], "pixels": "d"})
    empty = _image(**{"0028,0301": "NO"})
    services.write_burned_in_text_record(
        empty, {"policy": "p", "boxes": [], "pixels": "d"})
    assert session_module._in_burned_in_text_scope(boxed, "CT", FLAGGED)
    assert not session_module._in_burned_in_text_scope(empty, "CT", FLAGGED)


@pytest.mark.parametrize("modality, inside", [
    ("CR", True), ("DX", True), ("CR\\DX", True), (" DX ", True),
    ("dx", True), ("ct\\dx", True),
    ("CT", False), ("", False), (None, False), ("DXA", False),
])
def test_modalities_reads_each_listed_value_of_a_multi_valued_modality(modality, inside):
    """A multi-valued Modality is held backslash-joined (#985). A source
    writing its Modality in lower case is read upper-cased, so it falls in
    the list rather than silently leaving the scope (review of #1122)."""
    policy = {"action": "withhold", "scope": "modalities",
              "modalities": ["DX"], "min_confidence": 80}
    if modality == "CR":
        policy = {**policy, "modalities": ["CR", "DX"]}
    assert session_module._in_burned_in_text_scope(_image(), modality, policy) is inside


def test_modalities_also_reads_what_flagged_reads():
    policy = {"action": "withhold", "scope": "modalities",
              "modalities": ["DX"], "min_confidence": 80}
    assert session_module._in_burned_in_text_scope(
        _image(**{"0028,0301": "YES"}), "CT", policy)


def test_all_reads_every_image_and_no_scope_reads_one_without_pixels():
    policy = {"action": "withhold", "scope": "all", "min_confidence": 80}
    assert session_module._in_burned_in_text_scope(_image(), "CT", policy)
    no_pixels = Instance(sop_instance_uid="1.2.3.5")
    no_pixels.attributes["0028,0301"] = "YES"
    for p in (policy, FLAGGED):
        assert not session_module._in_burned_in_text_scope(no_pixels, "CT", p)


# --- one predicate, three readers ---------------------------------------------

CT, CR, MR, SR = (f"1.2.826.0.1.3680043.8.498.1121.{k}" for k in range(1, 5))
SCOPED = ("burned_in_text: {action: withhold, scope: modalities, "
          "modalities: [CR]}\n")


def _mixed(tmp_path, config=SCOPED):
    """A CT (no flag), a CR, an MR flagged `yes` and an SR, every image
    inked, each in its own series; the tags anonymized so the pre-export
    tag scan withholds nothing."""
    src = tmp_path / "in"
    src.mkdir()
    for i, (uid, modality, flag) in enumerate(
            [(CT, "CT", None), (CR, "CR", None), (MR, "MR", "yes")]):
        write_image(src / f"i{i}.dcm", sop_uid=uid, ink=[(0, *INK_BOX)],
                    modality=modality, burned_in=flag,
                    series_uid=f"{uid}.0")
    write_sr(src / "sr.dcm", sop_uid=SR)
    session = Session(str(tmp_path / "s.db"))
    session.ingest(str(src))
    session.load_config(str(_write(tmp_path, config)))
    session.anonymize()
    return session


def _by_source(session):
    return {_source_uid(inst): inst for p in session.store.patients
            for st in p.studies for se in st.series for inst in se.instances}


def test_redact_reads_only_the_images_in_scope(tmp_path, reads):
    """Kills the pass reading every image holding pixels (rc19)."""
    with _mixed(tmp_path) as session:
        session.redact(show_progress=False)
        live = _by_source(session)
        db = session.store_backend.db_path
    assert sorted(reads) == sorted([CR, MR])
    assert services.BURNED_IN_TEXT_ATTR not in live[CT].attributes
    [row] = [r for r in audit_rows(db) if r[0] == "REDACTION"]
    assert row[2].startswith(
        "burned_in_text (action withhold, scope modalities [CR], min_confidence "
        "80): read 2 of 2 in-scope instance(s) holding pixels (1 out of scope), "
        "0 could not be read, 0 already checked under this policy; text found "
        "on 2 (2 region(s)); 2 to be withheld at export.")


def test_plain_export_gates_only_the_images_in_scope(tmp_path, reads):
    """The CT is written with no row, as a pixel-less instance is; the CR
    and MR are withheld for their text. Kills the gate asking
    `_holds_pixels` alone (the CT would be withheld as never checked)."""
    out = tmp_path / "out"
    with _mixed(tmp_path) as session:
        session.redact(show_progress=False)
        live = _by_source(session)
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db = session.store_backend.db_path
    written = set(summary.written_uids)
    assert live[CT].sop_instance_uid in written
    assert live[SR].sop_instance_uid in written
    assert live[CR].sop_instance_uid not in written
    assert live[MR].sop_instance_uid not in written
    gated = {r[1] for r in audit_rows(db) if r[0] == "WARNING"}
    assert gated == {live[CR].sop_instance_uid, live[MR].sop_instance_uid}


def test_plain_export_reads_the_modality_of_an_image_never_read(tmp_path, reads):
    """Pins the plain export's Modality reading (review of #1122): no
    `redact()`, so no record and no attestation; the CR is in scope by its
    Modality alone and, never checked, is withheld with its row under
    `withhold`. The test above runs `redact()` first, where the record
    arm would gate it whatever Modality the gate was handed."""
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "cr.dcm", sop_uid=CR, ink=[(0, *INK_BOX)], modality="CR")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(str(_write(tmp_path, SCOPED)))
        session.anonymize()
        uid = _by_source(session)[CR].sop_instance_uid
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db = session.store_backend.db_path
    assert reads == []
    assert summary.written == 0
    assert exported_files(out) == []
    assert [r for r in audit_rows(db) if r[0] == "WARNING"] == [(
        "WARNING", uid,
        f"DICOM export to {out} withheld instance {uid}: its pixels were not "
        f"checked for burned-in text under this configuration's "
        f"burned_in_text policy; run redact().")]


def test_the_export_read_reads_only_the_images_in_scope(tmp_path, reads):
    """Kills `check_burned_in=True` reading every planned image."""
    out = tmp_path / "out"
    with _mixed(tmp_path) as session:
        live = _by_source(session)
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    assert sorted(reads) == sorted([CR, MR])
    assert live[CT].sop_instance_uid in summary.written_uids
    assert live[CR].sop_instance_uid not in summary.written_uids
    report = (tmp_path / "r.md").read_text()
    assert ("export(check_burned_in=True) under action withhold and scope "
            "modalities [CR] read 2 of 2 planned in-scope instance(s) holding "
            "pixels (0 could not be read; 1 out of scope, not read) and found "
            "text on 2 (2 region(s)); 2 withheld, with a row.") in report, report


# --- the fail-open the two extra `flagged` arms close (Q-A1 (a)) --------------

ZONE_RULE = "machines:\n  - serial_number: \"\"\n    redaction_zones:\n      - [0, 4, 0, 4]\n"
FLAGGED_WITHHOLD = "burned_in_text: {action: withhold, scope: flagged}\n"


def _flagged_image(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "i.dcm", sop_uid=CR, ink=[(0, *INK_BOX)],
                modality="CR", burned_in="YES")
    return str(src)


def _assert_withheld(out, summary, db, uid):
    """Read after the session closed, so the audit writer has drained."""
    assert uid not in summary.written_uids
    assert exported_files(out) == []
    assert any(r[0] == "WARNING" and r[1] == uid
               and "withheld instance" in r[2] for r in audit_rows(db))


def test_withhold_beside_a_zone_does_not_export_the_text(tmp_path, ocr):
    """The owner's case: the pass reads the `YES` image and records text
    the zone misses; the zone then writes `NO`. Read by the flag alone,
    the export would write the text. Arms 2 and 3 both hold it here; the
    two tests below hold each alone."""
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_flagged_image(tmp_path))
        session.load_config(str(_write(tmp_path, ZONE_RULE + FLAGGED_WITHHOLD)))
        session.anonymize()
        assert session.redact(show_progress=False) == 1
        inst = _by_source(session)[CR]
        assert inst.attributes["0028,0301"] == "NO"
        assert services.burned_in_text_record(inst)["boxes"]
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db, uid = session.store_backend.db_path, inst.sop_instance_uid
    _assert_withheld(out, summary, db, uid)


def test_an_image_redacted_before_the_policy_was_loaded_stays_in_scope(tmp_path, ocr):
    """Arm 2 alone: the zone redaction wrote `NO` and no record exists.
    Kills the predicate without the attestation arm (the export would
    write the text as out of scope)."""
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_flagged_image(tmp_path))
        session.load_config(str(_write(tmp_path, ZONE_RULE, "zones.yaml")))
        session.anonymize()
        assert session.redact(show_progress=False) == 1
        inst = _by_source(session)[CR]
        assert inst.attributes["0028,0301"] == "NO"
        session.load_config(str(_write(tmp_path, ZONE_RULE + FLAGGED_WITHHOLD)))
        assert services.BURNED_IN_TEXT_ATTR not in inst.attributes
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db, uid = session.store_backend.db_path, inst.sop_instance_uid
    _assert_withheld(out, summary, db, uid)


def test_text_found_stays_gated_when_the_flag_is_rewritten(tmp_path, ocr):
    """Arm 3 alone: no zone, so no attestation; the flag set to `NO` by
    hand after the pass found text. Kills the predicate without the
    record arm."""
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(_flagged_image(tmp_path))
        session.load_config(str(_write(tmp_path, FLAGGED_WITHHOLD)))
        session.anonymize()
        session.redact(show_progress=False)
        inst = _by_source(session)[CR]
        assert "_ISOCENTER_REDACTION_HASH" not in inst.attributes
        inst.set_attr("0028,0301", "NO")
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db, uid = session.store_backend.db_path, inst.sop_instance_uid
    _assert_withheld(out, summary, db, uid)


def test_a_text_free_image_read_under_all_follows_a_narrower_scope(tmp_path, reads):
    """Read under `all` (a record with no box), then `flagged`: neither
    read again nor gated. Kills the record arm accepting any record."""
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "ct.dcm", sop_uid=CT, modality="CT")
    out = tmp_path / "out"
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.anonymize()
        session.load_config(str(_write(
            tmp_path, "burned_in_text: {action: withhold, scope: all}\n", "a.yaml")))
        session.redact(show_progress=False)
        assert reads == [CT]
        session.load_config(str(_write(tmp_path, FLAGGED_WITHHOLD)))
        session.redact(show_progress=False)
        assert reads == [CT]
        summary = session.export(str(out), use_compression=False,
                                 show_progress=False)
        db = session.store_backend.db_path
    assert summary.written == 1
    assert [r for r in audit_rows(db) if r[0] == "WARNING"] == []
    rows = [r[2] for r in audit_rows(db) if r[0] == "REDACTION"]
    assert "read 0 of 0 in-scope instance(s) holding pixels (1 out of scope)" in rows[-1]


# --- section 5 and the grade (Q-A2 (a)) ---------------------------------------

def test_a_flagged_pass_that_selects_nothing_passes_and_says_so(tmp_path, ocr):
    out = tmp_path / "out"
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "ct.dcm", sop_uid=CT, ink=[(0, *INK_BOX)], modality="CT")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(str(_write(tmp_path, FLAGGED_WITHHOLD)))
        session.audit()
        session.anonymize()
        assert session.redact(show_progress=False) == 0
        session.export(str(out), use_compression=False, show_progress=False)
        session.generate_report(str(tmp_path / "r.md"))
    report = (tmp_path / "r.md").read_text()
    assert "| **Validation Status** | **PASS** |" in report, report
    assert ("redact() under action withhold and scope flagged read 0 of 0 "
            "in-scope instance(s) holding pixels (0 already checked, 0 could "
            "not be read; 1 out of scope, not read)") in report, report
    # The text an out-of-scope image holds is written: the scope's choice.
    [path] = exported_files(out)
    assert (pixels_of(path) == INK).any()


def test_an_export_read_that_selects_nothing_says_so(tmp_path, reads):
    """`check_burned_in=True` whose scope selects no planned image still
    leaves its line in section 5, with the out-of-scope count, and reads
    nothing. Kills the early return that left no trace."""
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "ct.dcm", sop_uid=CT, ink=[(0, *INK_BOX)], modality="CT")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        session.load_config(str(_write(tmp_path, FLAGGED_WITHHOLD)))
        session.anonymize()
        summary = session.export(str(tmp_path / "out"), use_compression=False,
                                 show_progress=False, check_burned_in=True)
        session.generate_report(str(tmp_path / "r.md"))
    assert reads == []
    assert summary.written == 1
    assert ("export(check_burned_in=True) under action withhold and scope "
            "flagged read 0 of 0 planned in-scope instance(s) holding pixels "
            "(0 could not be read; 1 out of scope, not read)"
            ) in (tmp_path / "r.md").read_text()


def test_a_hand_built_summary_without_the_new_fields_still_renders():
    summary = BurnedInTextSummary(source="redact()", action="report",
                                  attempted=1, read=1, unread=0, skipped=0,
                                  with_text=0, regions=0, acted=0)
    assert summary.scope == "" and summary.out_of_scope == 0
    report = ComplianceReport.__new__(ComplianceReport)
    report.burned_in_text_passes = [summary]
    from isocenter.reporting import _burned_in_text_line
    line = _burned_in_text_line(report)
    assert ("redact() under action report read 1 of 1 instance(s) holding "
            "pixels (0 already checked, 0 could not be read)") in line
    assert "scope" not in line


# --- records --------------------------------------------------------------------

#: rc19's policy hash for `{min_confidence: 80}` and no zones: md5 of
#: `{"min_confidence": 80.0, "zones": []}`, recomputed by hand.
RC19_POLICY = "524eccc7244b9e8aa0b11413ca2379b4"


def test_an_rc19_record_is_read_again_without_force(tmp_path, reads):
    """The box rule changed, so a record rc19 wrote is stale: the next
    `redact()` reads the image again with no `force`."""
    src = tmp_path / "in"
    src.mkdir()
    write_image(src / "cr.dcm", sop_uid=CR, ink=[(0, *INK_BOX)], modality="CR")
    with Session(str(tmp_path / "s.db")) as session:
        session.ingest(str(src))
        inst = _by_source(session)[CR]
        digest = pixel_analysis._pixels_digest(inst, [])
        services.write_burned_in_text_record(
            inst, {"policy": RC19_POLICY, "boxes": [], "tesseract": "5.5.3",
                   "pixels": digest})
        session.load_config(str(_write(
            tmp_path, "burned_in_text: {action: withhold, scope: all}\n")))
        session.redact(show_progress=False)
        assert reads == [CR]
        assert services.burned_in_text_record(inst)["policy"] != RC19_POLICY


def _md5(key):
    return hashlib.md5(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()


#: Literal hashes of `_burned_in_text_policy_hash`, each md5 over the
#: sorted-key JSON of `{"boxes": 2, "min_confidence": float, "zones":
#: sorted}`, recomputed by hand when pinned. Red when a key is added,
#: renamed or reordered without a decision; PRs B and C re-pin.
_PINNED_POLICY_HASHES = [
    (({"action": "redact", "scope": "all", "min_confidence": 80}, []),
     "8b75880301e8e60408f2b496a42c4b0d"),
    (({"action": "report", "scope": "modalities", "modalities": ["CR"],
       "min_confidence": 62.5}, [(10, 20, 30, 40), (0, 4, 0, 4)]),
     "db82a6638ac0013466165b08793d34bb"),
]


@pytest.mark.parametrize("args, expected", _PINNED_POLICY_HASHES,
                         ids=["default", "zones-and-scope"])
def test_the_policy_hash_is_pinned_as_literals(args, expected):
    policy, zones = args
    assert services._burned_in_text_policy_hash(policy, zones) == expected


def test_the_scope_is_not_in_the_policy_hash():
    """Scope chooses images, not boxes: a record read under `all` is
    current under `flagged`."""
    base = {"action": "redact", "min_confidence": 80}
    hashes = {services._burned_in_text_policy_hash({**base, **extra}, [])
              for extra in ({"scope": "all"}, {"scope": "flagged"},
                            {"scope": "modalities", "modalities": ["CR"]})}
    assert hashes == {_md5({"boxes": 2, "min_confidence": 80.0, "zones": []})}


def test_the_key_set_and_values_are_the_ones_the_schema_row_names():
    assert config_manager._BURNED_IN_TEXT_KEYS == frozenset(
        {"action", "min_confidence", "scope", "modalities"})
    assert config_manager.BURNED_IN_TEXT_SCOPES == ("flagged", "modalities", "all")
