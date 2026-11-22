"""`export(format="wfdb")` refuses an option name it does not know (#410).

The wfdb exporter read `options.get("patient_ids")` and
`options.get("include_annotation_text")` and never looked at the rest of
the dict, so `export(folder, format="wfdb", patient_id=["P1"])` -- one
character off the frozen name -- **exported every patient**, with nothing
in the returned list of paths, the log, the audit table or the compliance
report saying the subset filter had been dropped. The same typo on
`format="dicom"` has always raised, because `_export_dicom` has a real
signature.

The two halves of this file are deliberately paired. R1 says the bad
call is refused, R2 says the good call is not: a check that rejects
everything fails R2, one that rejects nothing fails R1, and neither can
pass vacuously while the other holds. R3 is the coherence assertion the
issue is actually about, and R4 pins the allow-list itself -- which is a
thing `tests/test_wfdb_privacy.py`'s AST pin structurally cannot see,
for the reason its own docstring records.
"""
import os
import re

import pytest

from isocenter.exporters import wfdb
from isocenter.session import DicomSession
from scripts.generate_waveform_test_data import write_fixture


def _two_patient_fixture(src):
    """Two patients, one waveform-bearing instance each."""
    src.mkdir(exist_ok=True)
    write_fixture(str(src / "a.dcm"), num_samples=64,
                  patient_id="WFPAT-A", patient_name="Alpha^Ann")
    write_fixture(str(src / "b.dcm"), num_samples=64,
                  patient_id="WFPAT-B", patient_name="Beta^Bob")
    return "WFPAT-A", "WFPAT-B"


def _hea_names(paths):
    return sorted(os.path.basename(p) for p in paths)


def test_a_mistyped_subset_option_raises_instead_of_exporting_everyone(tmp_path):
    """`patient_id=` refuses, and writes nothing while refusing.

    Measured before the fix: this call returned both patients' records,
    which is the whole cohort delivered to a caller who asked for one.

    The message assertion uses a **word boundary**. `"patient_id" in
    msg` is a substring of `patient_ids` and is therefore satisfied by a
    message that names only the two accepted options and never mentions
    what the caller actually passed -- it would pass on a refusal that
    tells the caller nothing about their own typo.

    "It raised" is not enough on its own either: a refusal that fires
    after the walk leaves the cohort on disk, so the output directory is
    asserted empty or absent.
    """
    src = tmp_path / "src"
    first, second = _two_patient_fixture(src)
    out = tmp_path / "typo_out"

    session = DicomSession(persistence_file=str(tmp_path / "strict.db"))
    try:
        session.ingest(str(src))
        assert {p.patient_id for p in session.store.patients} == {first, second}, (
            "precondition: both patients must be in the store, or a refusal "
            "that wrote nothing is indistinguishable from an empty session")

        with pytest.raises(TypeError) as excinfo:
            session.export(str(out), format="wfdb", patient_id=[first])
    finally:
        session.close()

    message = str(excinfo.value)
    assert re.search(r"\bpatient_id\b", message), (
        f"the refusal must name the option the caller passed; got {message!r}")

    assert not out.exists() or not list(out.rglob("*")), (
        f"the refusal wrote files into {out}: "
        f"{sorted(str(p) for p in out.rglob('*'))}")


def test_the_two_frozen_options_are_still_accepted(tmp_path):
    """Both frozen options, together, in one call.

    Paired with the test above so neither can pass vacuously. This one
    also carries the subset assertion, so a check that quietly swallowed
    `patient_ids` along with the unknown names would be red here rather
    than green on "it did not raise".
    """
    src = tmp_path / "src"
    first, second = _two_patient_fixture(src)

    session = DicomSession(persistence_file=str(tmp_path / "accepted.db"))
    try:
        session.ingest(str(src))
        written = session.export(str(tmp_path / "ok"), format="wfdb",
                                 patient_ids=[first],
                                 include_annotation_text=True)
    finally:
        session.close()

    names = _hea_names(written)
    assert names, "the accepted call wrote nothing at all"
    assert all(n.startswith(f"{first}_") for n in names), names
    assert not any(n.startswith(f"{second}_") for n in names), names


def test_both_formats_refuse_the_same_typo(tmp_path):
    """One keyword, both formats, one `TypeError` each.

    This is the coherence assertion #410 is about: the `dicom` path
    raised for this typo and the `wfdb` path shipped the cohort, and a
    de-identification library that is loud about a typo on one format
    and silent on the other has the strictness exactly backwards. Both
    outcomes are asserted here, in one test, so the two cannot drift
    apart again without something going red.

    The `dicom` half's exception text is **not** asserted beyond its
    type. It currently reads `DicomSession._export_dicom() got an
    unexpected keyword argument 'patient_id'`, naming a tier-3 private
    method through a tier-1 public call; that is filed separately and
    deliberately not fixed here, and pinning the wording would make this
    test an obstacle to fixing it.
    """
    src = tmp_path / "src"
    first, _ = _two_patient_fixture(src)

    session = DicomSession(persistence_file=str(tmp_path / "coherent.db"))
    try:
        session.ingest(str(src))

        with pytest.raises(TypeError):
            session.export(str(tmp_path / "dcm"), format="dicom",
                           patient_id=[first], show_progress=False)

        with pytest.raises(TypeError):
            session.export(str(tmp_path / "wf"), format="wfdb",
                           patient_id=[first])
    finally:
        session.close()


@pytest.mark.parametrize("dicom_only", ["use_compression",
                                        "check_reversibility", "show_progress",
                                        "verify_readback"])
def test_every_dicom_only_option_is_refused_by_the_wfdb_path(tmp_path, dicom_only):
    """The `dicom`-only names are refused, one per case.

    Six until #783, whose owner rulings Q10 and Q11 (2026-11-20) gave
    `wfdb` `subset` and `check_burned_in`; the four left are still
    refused.

    Two holes this closes, and they are the same hole seen from two
    sides.

    **The CHANGELOG's "what breaks" paragraph** names exactly these six
    `dicom`-only options and promises the wfdb path now raises for each.
    Nothing pinned that promise. After #410 deleted the one line in this
    repository that passed a `dicom` name to the wfdb path
    (`show_progress=False` in `tests/test_wfdb_writer.py`), **no test
    anywhere passed one** -- so the documented breaking change was
    unmeasured by the suite that shipped it.

    **The allow-list's own reason for existing was unpinned.**
    `_WFDB_OPTIONS` exists, by its own comment, so the check and the
    message cannot drift apart. A reviewer left the constant alone and
    inlined a three-name set literal in the check --

        unknown = sorted(set(options) - {"patient_ids",
                                         "include_annotation_text",
                                         "show_progress"})

    -- and the entire suite stayed green at `1832 passed, 2 skipped`,
    R4 included: R4 reads the constant, which was untouched. On that
    tree `show_progress=False` exported both patients while the refusal
    message still recited `_WFDB_OPTIONS`, so the exception text was the
    thing lying about which options were really accepted -- and that
    text is what the CHANGELOG quotes as the exact exception. This test
    is red on `show_progress` there.

    The message assertion is not decoration: "it raised" would be
    satisfied by a refusal that names something other than what the
    caller passed, which is precisely the drift shape above.
    """
    src = tmp_path / "src"
    first, _ = _two_patient_fixture(src)

    session = DicomSession(persistence_file=str(tmp_path / "dicom_only.db"))
    try:
        session.ingest(str(src))
        assert session.store.patients, (
            "precondition: an empty store would let a refusal that never "
            "ran look the same as one that did")

        with pytest.raises(TypeError) as excinfo:
            session.export(str(tmp_path / "out"), format="wfdb",
                           **{dicom_only: False})
    finally:
        session.close()

    message = str(excinfo.value)
    assert re.search(rf"\b{re.escape(dicom_only)}\b", message), (
        f"the refusal must name {dicom_only!r}, the option the caller "
        f"passed; got {message!r}")


def test_the_admitted_options_are_the_two_the_page_freezes():
    """`_WFDB_OPTIONS` is exactly what `docs/api/stability.md` freezes.

    This is the second freeze pin, and it exists because the first one
    cannot see what it needs to. `tests/test_wfdb_privacy.py`'s
    `test_the_wfdb_export_options_are_the_two_the_page_freezes` collects
    the literal keys the body *touches* by five syntactic forms --
    `.get`, `.pop`, `.setdefault`, subscript and `in` -- and a name
    listed in an allow-list constant is none of them. **Measured:** with
    `"zzz_third"` added to `_WFDB_OPTIONS`, that test stays green.

    So one pin covers what the exporter *reads* and this one covers what
    it *admits*, and they go red against each other if the two ever
    disagree: a name added here and never read fails nothing there, and
    a name read there and never listed here is refused at the door.

    Asserted against the constant itself and not against the docstring,
    which is what a test written from the prose would do and which would
    pass on a docstring that had stopped describing the code.
    """
    assert wfdb._WFDB_OPTIONS == frozenset({
        "patient_ids", "include_annotation_text", "subset",
        "check_burned_in"}), (
        f"the wfdb exporter admits {sorted(wfdb._WFDB_OPTIONS)}; "
        f"docs/api/stability.md names patient_ids, subset, check_burned_in "
        f"and include_annotation_text")
