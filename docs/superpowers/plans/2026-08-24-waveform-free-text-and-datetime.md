# Waveform Free-Text and Acquisition DateTime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two remaining PHI leaks in the waveform export path — issue #38 (Acquisition DateTime survives `anonymize()`) and issue #39 (operator free text reaches `.hea` and `annotations.json`).

**Architecture:** Three independent leaks, three separate mechanisms. #38 is a missing entry in a data table (`PRIVACY_PROFILES["basic"]`). #39's Channel Label leak is fixed in the *exporter* rather than the profile, so it holds even for a bare `Session()` that loads no PHI configuration at all — a lead-name allowlist replaces free text with a positional token. #39's annotation-note leak is fixed at both layers: the exporter omits it unless the caller opts in, and the profile remediates it when a config is loaded.

**Tech Stack:** Python 3.12+, pytest, pydicom 3.x, numpy. PhysioNet `wfdb` 4.x is available in the `tests` extra and is used as an independent reader oracle.

## Context you need

Gantry ingests DICOM into an object graph, remediates PHI, and exports. The WFDB exporter (`gantry/exporters/wfdb.py`) writes PhysioNet `header(5)` records plus a Murmur-facing `annotations.json` (`gantry/murmur.py`).

**The PHI scan is tag-gated, not content-based.** `gantry/privacy.py` only inspects a tag if the loaded configuration names it (`config_val = self.phi_tags.get(tag); if not config_val: continue`). There are three reachable configurations:

| How the session is set up | What gets scanned |
|---|---|
| bare `Session()` | `phi_tags == {}` — only a hardcoded PatientName/PatientID/StudyDate scan |
| `create_config()` / `load_config()` | `PRIVACY_PROFILES["basic"]` — 28 tags |
| user-supplied `phi_tags.json` | whatever it names |

This is why Task 2 fixes the Channel Label in the exporter and not only in the profile: a profile entry does nothing for a bare `Session()`.

## Global Constraints

- Python floor is **3.12**. CI runs 3.12, 3.13, 3.14, 3.14t.
- Dependencies are declared **only** in `setup.py`. There is deliberately no `requirements.txt` — do not create one.
- Profile keys **must be lowercase** `gggg,eeee`. Ingested attribute keys are lowercased at ingest (`gantry/io_handlers.py` `populate_attrs`). `0008,103E` shipped uppercase once and was silently never remediated (#41). Write new entries lowercase directly.
- **TDD.** Write the failing test first and show it RED before implementing. Every test must be demonstrated capable of failing against broken code — this project has already found nine false-passing tests plus a plan-authored test that raised `NameError` instead of asserting.
- Run the full suite in the **FOREGROUND** with the Bash tool's `timeout` parameter set to `300000`. The suite takes ~157s; the tool's default cutoff is 120s and will kill it. Never set `run_in_background: true`, never spawn a poller.
- Baseline at branch point (`main` @ `dc61148`): **381 passed, 1 skipped, 0 failed.**
- Branch: `fix/waveform-free-text-phi`, created from `main` @ `dc61148`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `gantry/profiles.py` | The Basic profile tag table | 1, 3 |
| `gantry/waveform.py` | `WaveformChannel.wfdb_description()` and the new lead-name allowlist | 2 |
| `gantry/exporters/wfdb.py` | Passes the channel index; threads the `include_annotation_text` option | 2, 3 |
| `gantry/murmur.py` | `build_annotations()` — the `note`/`lead`/`source` fields | 2, 3, 4 |
| `tests/test_wfdb_privacy.py` | Existing characterization tests that must be **converted**, not deleted | 2 |
| `docs/waveforms.md` | The "What is and isn't de-identified" disclosure section | 4 |

---

### Task 1: Acquisition DateTime in the Basic profile (#38)

`(0008,002A)` Acquisition DateTime is the DT-valued twin of `(0008,0022)` Acquisition Date, which the profile *does* remove. A reader would reasonably assume it is covered. It is not, so raw acquisition timing survives a full `audit()` / `anonymize()` pass.

**Files:**
- Modify: `gantry/profiles.py` (the `BASIC_PROFILE` dict, "Study / Series Information" block, around line 35-42)
- Test: `tests/test_profiles.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `BASIC_PROFILE` gains `"0008,002a"` and the four Performed Procedure Step date/time keys listed below. Task 3 adds one more key to the same dict.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_profiles.py`:

```python
from gantry.profiles import BASIC_PROFILE


def test_basic_profile_covers_datetime_twins_of_the_dates_it_removes():
    """Every date/time tag the profile removes must have its DT-valued twin
    covered too.

    (0008,002A) Acquisition DateTime carries the same information as
    (0008,0022) Acquisition Date, which the profile removes. Leaving the DT
    twin out means raw acquisition timing survives anonymize() while the
    plain date is stripped -- the profile looks complete and is not.
    """
    required = {
        "0008,002a": "Acquisition DateTime",
        "0040,0244": "Performed Procedure Step Start Date",
        "0040,0245": "Performed Procedure Step Start Time",
        "0040,0250": "Performed Procedure Step End Date",
        "0040,0251": "Performed Procedure Step End Time",
    }
    missing = [tag for tag in required if tag not in BASIC_PROFILE]
    assert not missing, (
        f"Basic profile is missing date/time tags: {missing}. These carry "
        "acquisition and procedure timing that survives anonymize().")


def test_basic_profile_keys_are_all_lowercase():
    """Ingested attribute keys are lowercased, so an uppercase profile key
    never matches and silently disables that tag (see #41, where
    0008,103E shipped uppercase and Series Description was never
    remediated on any documented path).
    """
    uppercase = [k for k in BASIC_PROFILE if k != k.lower()]
    assert not uppercase, f"profile keys must be lowercase: {uppercase}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_profiles.py -k "datetime_twins or lowercase" -v`

Expected: `test_basic_profile_covers_datetime_twins_of_the_dates_it_removes` FAILS listing all five missing tags. `test_basic_profile_keys_are_all_lowercase` should PASS already — it is a regression guard, and you must confirm it can fail by temporarily adding `"0008,ABCD": {"action": "REMOVE", "name": "temp"}` to `BASIC_PROFILE`, re-running, seeing it fail, then removing it. Record that evidence in your report.

- [ ] **Step 3: Add the tags**

In `gantry/profiles.py`, inside `BASIC_PROFILE`, immediately after the `"0008,0023"` Content Date line:

```python
    # DT-valued twins of the dates above. (0008,002A) carries the same
    # information as (0008,0022) Acquisition Date; omitting it meant raw
    # acquisition timing survived a full anonymize() pass while the plain
    # date was stripped.
    "0008,002a": {"action": "REMOVE", "name": "Acquisition DateTime"},
```

And after the `"0008,0033"` Content Time line:

```python
    # Procedure step timing: same shape of leak as the acquisition dates.
    "0040,0244": {"action": "REMOVE", "name": "Performed Procedure Step Start Date"},
    "0040,0245": {"action": "REMOVE", "name": "Performed Procedure Step Start Time"},
    "0040,0250": {"action": "REMOVE", "name": "Performed Procedure Step End Date"},
    "0040,0251": {"action": "REMOVE", "name": "Performed Procedure Step End Time"},
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_profiles.py -v`
Expected: PASS.

- [ ] **Step 5: Add an end-to-end test that the tag is actually remediated**

A profile entry that is never read is worthless — #41 was exactly that failure. Add to `tests/test_wfdb_privacy.py`:

```python
def test_acquisition_datetime_is_remediated_end_to_end(tmp_path):
    """The profile entry must actually fire on a real ingest->anonymize pass.

    A tag can sit in BASIC_PROFILE and still never be remediated if the key
    casing does not match the lowercased ingested keys (#41). This asserts
    the value is gone from the object graph, not merely that the profile
    mentions it.
    """
    import pydicom
    from gantry.session import DicomSession

    raw_datetime = "20260101101530.000000"
    path = tmp_path / "wf.dcm"
    ds = build_ecg_dataset(
        channels=[("MDC_ECG_LEAD_I", "Lead I")],
        patient_id="DTTEST001",
        patient_name="Waveform^Test",
    )
    ds.AcquisitionDateTime = raw_datetime
    pydicom.dcmwrite(str(path), ds, write_like_original=False)

    session = DicomSession(":memory:")
    try:
        session.ingest(str(tmp_path))
        session.create_config()
        session.audit()
        session.anonymize()

        remaining = []
        for patient in session.store.patients:
            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        value = instance.attributes.get("0008,002a")
                        if value:
                            remaining.append(str(value))
    finally:
        session.close()

    assert raw_datetime not in remaining, (
        "Acquisition DateTime survived anonymize(); the BASIC_PROFILE entry "
        f"is present but not firing. Remaining values: {remaining!r}")
```

`build_ecg_dataset` already exists in `tests/test_wfdb_privacy.py` — read its signature before use and do not redefine it.

- [ ] **Step 6: Run it, then mutation-test it**

Run: `.venv/bin/python -m pytest tests/test_wfdb_privacy.py::test_acquisition_datetime_is_remediated_end_to_end -v`
Expected: PASS.

Then prove it can fail: change the profile key to `"0008,002A"` (uppercase), re-run, confirm FAIL, restore lowercase, confirm PASS, and confirm `git status --porcelain` is clean. This is the exact #41 failure mode — the test's whole purpose is catching it. Record the evidence.

- [ ] **Step 7: Run the full suite**

Run `.venv/bin/python -m pytest -rs` in the FOREGROUND with `timeout: 300000`.
Expected: 384 passed (381 + 3 new), 1 skipped, 0 failed.

- [ ] **Step 8: Commit**

```bash
git add gantry/profiles.py tests/test_profiles.py tests/test_wfdb_privacy.py
git commit -m "fix: remediate Acquisition DateTime and procedure step timing

(0008,002A) is the DT-valued twin of (0008,0022) Acquisition Date, which
the Basic profile already removes, so raw acquisition timing survived a
full audit()/anonymize() pass while the plain date was stripped. The
same gap covered the four Performed Procedure Step date/time tags.

Adds an end-to-end test rather than only asserting the dict contents: a
profile entry whose key casing does not match the lowercased ingested
keys is present and inert, which is how Series Description went
unremediated for several releases (#41).

Closes #38"
```

---

### Task 2: Lead-name allowlist for Channel Label (#39, part 1)

`(003A,0203)` Channel Label is operator-typed `SH` text. `WaveformChannel.wfdb_description()` falls back to it whenever a channel has no coded Channel Source Sequence, so it reaches the `.hea` signal-line description and the `annotations.json` `lead` field verbatim.

**The decision (already made, do not re-litigate):** emit the label only when it is a recognisable lead name; otherwise emit a positional token `ch<N>`. This keeps headers usable for real leads like `II` and `V5` while making it impossible for operator free text to reach either output — including for a bare `Session()` that loads no PHI configuration.

**Files:**
- Modify: `gantry/waveform.py` (`WaveformChannel.wfdb_description`, currently line 173-179; add `KNOWN_LEAD_NAMES` and `_is_known_lead_name` at module scope)
- Modify: `gantry/exporters/wfdb.py:207` (pass the loop index)
- Modify: `gantry/murmur.py:74` (pass the index)
- Test: `tests/test_waveform_model.py`, `tests/test_wfdb_privacy.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `WaveformChannel.wfdb_description(index: Optional[int] = None) -> str`. Task 3 does not call it; Task 4 documents it.

- [ ] **Step 1: Write the failing unit tests**

Add to `tests/test_waveform_model.py`:

```python
from gantry.waveform import WaveformChannel


def test_coded_source_still_wins():
    """A coded value cannot contain operator text, so it is always preferred."""
    channel = WaveformChannel(label="anything at all", source_code="MDC_ECG_LEAD_II")
    assert channel.wfdb_description(0) == "MDC_ECG_LEAD_II"


def test_recognisable_lead_names_survive():
    """Real lead names stay in the header -- a positional token for every
    uncoded channel would make records much harder to interpret."""
    for label in ["II", "v5", "aVR", "Lead I", " III "]:
        channel = WaveformChannel(label=label)
        assert channel.wfdb_description(3) == label.strip(), (
            f"{label!r} is a valid lead name and should survive verbatim")


def test_free_text_label_is_replaced_with_a_positional_token():
    """Operator free text must never reach the header, coded or not."""
    for label in [
        "OPERATOR NOTE Smith^John DOB19800101",
        "Lead I taken by Jane Doe",
        "II - patient moved",
        "MRN-12345678",
    ]:
        channel = WaveformChannel(label=label)
        assert channel.wfdb_description(3) == "ch3", (
            f"{label!r} is not a lead name and must be replaced")


def test_absent_label_is_positional():
    assert WaveformChannel(label="").wfdb_description(2) == "ch2"


def test_index_is_optional_for_callers_that_lack_one():
    assert WaveformChannel(label="").wfdb_description() == "signal"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_waveform_model.py -k "coded_source or recognisable or free_text or positional or index_is_optional" -v`

Expected: `test_free_text_label_is_replaced_with_a_positional_token` and the positional tests FAIL — currently the raw label is returned. `test_coded_source_still_wins` and `test_recognisable_lead_names_survive` may already pass; that is fine, they are the guard against over-correcting.

- [ ] **Step 3: Implement the allowlist**

In `gantry/waveform.py`, at module scope above `class WaveformChannel`:

```python
# Recognisable physiological signal names. Channel Label (003A,0203) is
# operator-typed SH text, so anything NOT on this list is treated as free
# text and replaced with a positional token rather than written into the
# .hea header or annotations.json. Compared case-insensitively after
# stripping an optional "lead " prefix, so "Lead I", "lead I" and "I" all
# match.
KNOWN_LEAD_NAMES = frozenset({
    # 12-lead ECG
    "i", "ii", "iii", "avr", "avl", "avf",
    "v1", "v2", "v3", "v4", "v5", "v6",
    # Extended / posterior / right-sided
    "v7", "v8", "v9", "v3r", "v4r", "v5r",
    # Monitoring
    "mcl1", "mcl6",
    # Frank orthogonal (vectorcardiography)
    "x", "y", "z",
    # EASI
    "es", "as", "ai",
    # Common non-ECG physiological channels
    "resp", "pleth", "spo2", "co2",
    "abp", "art", "cvp", "pap", "icp",
})


def _is_known_lead_name(label: str) -> bool:
    """True if `label` is a recognisable signal name rather than free text.

    Normalises case, collapses internal whitespace, and drops an optional
    "lead " prefix before comparing, because DICOM sources write the same
    lead as "I", "Lead I" and "LEAD  I" interchangeably.
    """
    normalized = " ".join(str(label or "").split()).lower()
    if normalized.startswith("lead "):
        normalized = normalized[len("lead "):]
    return normalized in KNOWN_LEAD_NAMES
```

Replace `wfdb_description` (currently line 173-179) with:

```python
    def wfdb_description(self, index: Optional[int] = None) -> str:
        """Signal description for the .hea signal line and annotations `lead`.

        Prefers the coded channel source, which cannot contain
        operator-typed text. Falls back to the free-text Channel Label
        ONLY when that label is a recognisable lead name; anything else is
        replaced with a positional token, because (003A,0203) is
        operator-typed SH and has been observed carrying names, MRNs and
        clinical commentary.

        The check lives here rather than in the privacy profile on purpose:
        the PHI scan is tag-gated, so a profile entry protects only sessions
        that loaded a configuration. A bare Session() would still leak.

        Args:
            index (int, optional): Zero-based channel index, used for the
                positional token. Callers without one get "signal".
        """
        if self.source_code:
            return self.source_code
        if self.label and _is_known_lead_name(self.label):
            return self.label.strip()
        return f"ch{index}" if index is not None else "signal"
```

`Optional` is already imported in `gantry/waveform.py`; confirm before adding an import.

- [ ] **Step 4: Update both call sites to pass the index**

`gantry/exporters/wfdb.py:207` — inside the `for idx in range(n_channels):` loop that starts at line 173:

```python
            _sanitize_description(channel.wfdb_description(idx)),
```

`gantry/murmur.py:74` — inside `_lead_for`, where `index` is already computed:

```python
        return waveform.channels[index].wfdb_description(index)
```

- [ ] **Step 5: Run the unit tests**

Run: `.venv/bin/python -m pytest tests/test_waveform_model.py -v`
Expected: PASS.

- [ ] **Step 6: Convert the existing characterization tests**

`tests/test_wfdb_privacy.py` has a characterization block (from line ~233) documenting the *old* unsafe behaviour. Its own comment says: *"If this fallback was fixed, update this test to match the new (safer) behaviour instead of deleting the assertion."* You have sign-off — the allowlist is the agreed fix.

**Convert, do not delete.** Find the assertion near line 318 that reads `assert description_field == FREE_TEXT_MARKER` and change it to assert the marker no longer arrives:

```python
    # SAFETY ASSERTION (converted from characterization once the lead-name
    # allowlist landed): FREE_TEXT_MARKER is not a recognisable lead name,
    # so wfdb_description() replaces it with a positional token and the
    # operator text never reaches the .hea description field.
    assert description_field == "ch0", (
        "expected the free-text ChannelLabel to be replaced with a "
        f"positional token; got {description_field!r}. If this reverted to "
        "the raw label, the allowlist in gantry/waveform.py stopped firing "
        "and operator text is reaching the header again.")
    assert FREE_TEXT_MARKER not in description_field
```

Also update the module-level comment block above `FREE_TEXT_MARKER` (line ~233-247): it currently says the test "does NOT assert the fallback is safe or unsafe" and warns against changing `wfdb_description()` without sign-off. Rewrite it to state that the fallback was fixed under #39 and that the test is now a safety assertion. Leaving that comment in place would tell the next reader the opposite of what the code does.

Read the whole file for other assertions referencing `FREE_TEXT_MARKER` or the raw-label fallback and update each one. Do not assume there are only two.

- [ ] **Step 7: Run the WFDB privacy tests, then mutation-test**

Run: `.venv/bin/python -m pytest tests/test_wfdb_privacy.py -v`
Expected: PASS.

Then prove the new assertions are load-bearing: temporarily revert `wfdb_description` to `return self.source_code or self.label or "signal"`, re-run `tests/test_wfdb_privacy.py` and `tests/test_waveform_model.py`, confirm they FAIL, restore, confirm PASS, confirm `git status --porcelain` is clean. Record the evidence.

- [ ] **Step 8: Run the full suite**

Run `.venv/bin/python -m pytest -rs` FOREGROUND, `timeout: 300000`.
Expected: 389 passed (384 + 5 new), 1 skipped, 0 failed. If any *other* test fails, it is asserting on the old free-text fallback — read it, and convert it the same way rather than weakening it.

- [ ] **Step 9: Commit**

```bash
git add gantry/waveform.py gantry/exporters/wfdb.py gantry/murmur.py tests/test_waveform_model.py tests/test_wfdb_privacy.py
git commit -m "fix: never write operator free text into a WFDB header

Channel Label (003A,0203) is operator-typed SH text and was written
verbatim into the .hea signal description and the annotations.json lead
field whenever a channel had no coded Channel Source Sequence. Observed
carrying names, MRNs and clinical commentary.

wfdb_description() now emits the label only when it is a recognisable
signal name, and a positional token ch<N> otherwise. The check lives in
the exporter rather than the privacy profile because the PHI scan is
tag-gated: a profile entry protects only sessions that loaded a config,
so a bare Session() would still have leaked.

Converts the existing characterization tests, which documented the unsafe
behaviour deliberately and asked to be updated rather than deleted once
it was fixed.

Refs #39"
```

---

### Task 3: Omit annotation note text by default (#39, part 2)

`(0070,0006)` Unformatted Text Value routinely holds free-text clinical commentary and is written into `annotations.json` as `note`.

**The decision (already made):** omit `note` unless the caller explicitly opts in, **and** add `0070,0006` to the Basic profile. Both layers, because the exporter default protects a bare `Session()` while the profile entry means the value is actually remediated on the object graph for configured sessions.

**Files:**
- Modify: `gantry/murmur.py` (`build_annotations` signature and the `note` block at lines 169-171)
- Modify: `gantry/exporters/wfdb.py` (`export()` reads the option; `_write_instance` accepts and forwards it)
- Modify: `gantry/profiles.py` (one more `BASIC_PROFILE` entry)
- Test: `tests/test_murmur_annotations.py`

**Interfaces:**
- Consumes: `BASIC_PROFILE` from Task 1 (same dict, new key — do not disturb Task 1's entries).
- Produces: `build_annotations(instance, waveform, source, include_text: bool = False)`. The public option is `session.export(folder, format="wfdb", include_annotation_text=True)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_murmur_annotations.py`:

```python
def test_note_is_omitted_by_default():
    """(0070,0006) is free-text clinical commentary and must not be written
    unless the caller asks for it.

    The PHI scan is tag-gated, so a bare Session() scans almost nothing.
    Defaulting to omit is what makes this safe regardless of configuration.
    """
    instance, waveform = _annotated_instance(note="Reviewed by Dr Jane Doe, MRN-12345678")
    document = build_annotations(instance, waveform, "gantry/test")
    assert document["findings"], "fixture produced no findings"
    for finding in document["findings"]:
        assert "note" not in finding, (
            "annotation note text was written without the caller opting in")


def test_note_is_written_when_explicitly_requested():
    """Opting in is a deliberate act, and must actually work."""
    instance, waveform = _annotated_instance(note="sinus rhythm")
    document = build_annotations(instance, waveform, "gantry/test", include_text=True)
    notes = [f.get("note") for f in document["findings"]]
    assert "sinus rhythm" in notes
```

`_annotated_instance` is a helper you must add if the file does not already have an equivalent — read `tests/test_murmur_annotations.py` first and reuse whatever fixture builder it already uses to construct an instance with a Waveform Annotation Sequence. Do not invent a second fixture style alongside an existing one.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_murmur_annotations.py -k "note_is_omitted or note_is_written" -v`
Expected: `test_note_is_omitted_by_default` FAILS (the note is currently always written). `test_note_is_written_when_explicitly_requested` FAILS with `TypeError: build_annotations() got an unexpected keyword argument 'include_text'`.

- [ ] **Step 3: Add the parameter**

In `gantry/murmur.py`, change the signature at line 124:

```python
def build_annotations(instance, waveform, source: str, include_text: bool = False) -> Dict[str, Any]:
```

Add to its docstring's `Args:` block:

```
        include_text (bool): If True, write Unformatted Text Value
            (0070,0006) into each finding's `note`. Defaults to False
            because that tag routinely holds free-text clinical
            commentary, and the PHI scan is tag-gated -- so a bare
            Session() would otherwise write it out unremediated.
```

Replace the `note` block at lines 169-171:

```python
        if include_text:
            note = item.attributes.get(TAG_UNFORMATTED_TEXT)
            if note:
                finding["note"] = str(note)
```

- [ ] **Step 4: Thread the option through the exporter**

In `gantry/exporters/wfdb.py`, in `export()` (line 216), after `patient_ids = options.get("patient_ids")`:

```python
        # Off by default: (0070,0006) is free-text clinical commentary.
        include_annotation_text = bool(options.get("include_annotation_text", False))
```

Update the `export()` docstring's `**options` line to document it:

```
            **options: `patient_ids` (list, optional) limits the export.
                `include_annotation_text` (bool, default False) writes
                Unformatted Text Value (0070,0006) into annotations.json
                `note` fields; off by default because it is free-text
                clinical commentary.
```

Change the `_write_instance` call (line ~247) to forward it:

```python
                            path = self._write_instance(
                                folder, patient, study, series, instance, logger,
                                used_names, include_annotation_text)
```

Change the `_write_instance` signature (line ~283) to accept it:

```python
    def _write_instance(self, folder, patient, study, series, instance, logger,
                        used_names, include_annotation_text=False):
```

And the `build_annotations` call (line ~346):

```python
            build_annotations(instance, waveform, source, include_annotation_text))
```

- [ ] **Step 5: Add the profile entry**

In `gantry/profiles.py`, at the end of `BASIC_PROFILE`, after the `"0008,1070"` Operators' Name line:

```python
    # Free-text annotation commentary. Reaches annotations.json `note` when
    # a caller opts in via include_annotation_text; remediated here so that
    # opting in on a configured session still does not surface raw text.
    "0070,0006": {"action": "EMPTY", "name": "Unformatted Text Value"},
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_murmur_annotations.py tests/test_profiles.py -v`
Expected: PASS.

- [ ] **Step 7: Add an end-to-end test through the public API**

The unit tests above call `build_annotations` directly. Prove the option actually reaches it from `session.export()`. Add to `tests/test_wfdb_privacy.py`:

```python
def test_annotation_text_is_absent_from_exported_json_by_default(tmp_path):
    """The default must hold through the real export path, not just the
    helper -- an option that is never threaded through is the same bug as
    a profile entry that never fires (#41).
    """
    import json
    import glob
    from gantry.session import DicomSession

    secret = "Reviewed by Dr Jane Doe, MRN-12345678"
    _write_annotated_fixture(tmp_path / "in", note=secret)

    out = tmp_path / "out"
    session = DicomSession(":memory:")
    try:
        session.ingest(str(tmp_path / "in"))
        session.export(str(out), format="wfdb")
    finally:
        session.close()

    payloads = []
    for path in glob.glob(str(out / "**" / "*.annotations.json"), recursive=True):
        with open(path, encoding="utf-8") as handle:
            payloads.append(handle.read())

    assert payloads, "no annotations.json was written; fixture is not exercising the path"
    for payload in payloads:
        assert secret not in payload, "annotation free text reached annotations.json by default"
        for finding in json.loads(payload)["findings"]:
            assert "note" not in finding
```

You must write `_write_annotated_fixture(directory, note)` as a helper in that file, building on the existing `build_ecg_dataset` and whatever annotation-sequence construction `tests/test_murmur_annotations.py` already uses. Read both files first and reuse, do not duplicate.

- [ ] **Step 8: Run it, then mutation-test**

Run: `.venv/bin/python -m pytest tests/test_wfdb_privacy.py -v`
Expected: PASS.

Then prove it is load-bearing: temporarily change the `include_text` default in `build_annotations` to `True`, re-run, confirm the end-to-end test FAILS, restore, confirm PASS, confirm `git status --porcelain` is clean. This specifically catches the case where the option exists but is not threaded through. Record the evidence.

- [ ] **Step 9: Run the full suite**

Run `.venv/bin/python -m pytest -rs` FOREGROUND, `timeout: 300000`.
Expected: 392 passed (389 + 3 new), 1 skipped, 0 failed.

- [ ] **Step 10: Commit**

```bash
git add gantry/murmur.py gantry/exporters/wfdb.py gantry/profiles.py tests/test_murmur_annotations.py tests/test_wfdb_privacy.py
git commit -m "fix: omit annotation free text from annotations.json by default

Unformatted Text Value (0070,0006) routinely holds free-text clinical
commentary and was written verbatim into every annotations.json note
field. Now omitted unless the caller passes
include_annotation_text=True, and added to the Basic profile so that
opting in on a configured session still does not surface raw text.

Both layers are needed: the PHI scan is tag-gated, so the profile entry
alone would leave a bare Session() exposed, while the exporter default
alone would leave the value un-remediated on the object graph.

BREAKING: annotations.json no longer carries `note` unless
include_annotation_text=True is passed to session.export().

Refs #39"
```

---

### Task 4: Pin the `source` field and update the disclosure docs

Issue #39 claims `annotations.json` `source` embeds a device serial, giving `gantry/0.6.0 (AcmeCart SN-12345)` as the example. **That is wrong.** `gantry/exporters/wfdb.py:339` reads only `(0008,0070)` Manufacturer; the serial would be `(0018,1000)` and nothing in `murmur.py` or `wfdb.py` reads it. There is no serial leak to fix — so pin the current behaviour with a test and correct the record.

**Files:**
- Test: `tests/test_wfdb_privacy.py`
- Modify: `docs/waveforms.md` (the "What is and isn't de-identified" section)

**Interfaces:**
- Consumes: `wfdb_description(index)` from Task 2, `include_annotation_text` from Task 3.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Write the pinning test**

```python
def test_annotations_source_carries_no_device_serial(tmp_path):
    """`source` is producer provenance: gantry version plus Manufacturer.

    Device Serial Number (0018,1000) is a stable identifier that can link
    records across exports back to one machine and site. It is not read
    today; this pins that so it cannot be added casually.
    """
    import json
    import glob
    import pydicom
    from gantry.session import DicomSession

    serial = "SN-DEADBEEF-12345"
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    ds = build_ecg_dataset(
        channels=[("MDC_ECG_LEAD_I", "Lead I")],
        patient_id="SRCTEST001",
        patient_name="Waveform^Test",
    )
    ds.Manufacturer = "AcmeCart"
    ds.DeviceSerialNumber = serial
    pydicom.dcmwrite(str(in_dir / "wf.dcm"), ds, write_like_original=False)

    out = tmp_path / "out"
    session = DicomSession(":memory:")
    try:
        session.ingest(str(in_dir))
        session.export(str(out), format="wfdb")
    finally:
        session.close()

    documents = []
    for path in glob.glob(str(out / "**" / "*.annotations.json"), recursive=True):
        with open(path, encoding="utf-8") as handle:
            documents.append(json.load(handle))

    assert documents, "no annotations.json written; fixture is not exercising the path"
    for document in documents:
        source = document["source"]
        assert serial not in source, f"device serial leaked into source: {source!r}"
        assert "AcmeCart" in source, (
            f"manufacturer provenance was lost from source: {source!r}")
```

If this fixture produces no annotations (because `build_ecg_dataset` has no annotation sequence), reuse `_write_annotated_fixture` from Task 3 instead and set `Manufacturer`/`DeviceSerialNumber` on the dataset it builds. Check before assuming.

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_wfdb_privacy.py::test_annotations_source_carries_no_device_serial -v`
Expected: PASS immediately — it pins existing behaviour.

Then prove it can fail: temporarily change `gantry/exporters/wfdb.py:339-342` to append `instance.attributes.get("0018,1000")` to `source`, re-run, confirm FAIL, restore, confirm `git status --porcelain` clean. A test that has never been seen red is not evidence. Record it.

- [ ] **Step 3: Update the disclosure docs**

`docs/waveforms.md` has a "What is and isn't de-identified" section. It currently discloses the Channel Label and note passthroughs as *known behaviour*. Those are now fixed, so the section is actively misleading.

Rewrite that section to state:

- Channel Label reaches `.hea` and `annotations.json` `lead` **only** when it is a recognisable signal name; anything else becomes `ch<N>`. Name the module-level `KNOWN_LEAD_NAMES` in `gantry/waveform.py` as the list, so a reader can check it.
- `annotations.json` carries `note` **only** when `session.export(..., format="wfdb", include_annotation_text=True)` is passed. State that `(0070,0006)` is also in the Basic profile, so a configured session that opts in still gets the remediated value.
- `source` carries the gantry version and `(0008,0070)` Manufacturer. No device serial.
- Acquisition DateTime `(0008,002A)` is now removed by the Basic profile — remove any text saying it survives.
- **Keep** the existing disclosure that the WFDB header's time-of-day is not shifted. That is still true and still deliberate: `SHIFT_DATE` is a Study-level remediation that writes `study.study_date`, and time-of-day is sourced from the instance. Time-of-day alone is not a Safe Harbor identifier. Do not delete this — it is the one disclosure that remains accurate.

Read the section before rewriting and preserve its structure and voice.

- [ ] **Step 4: Add CHANGELOG entries**

Under `## [Unreleased]` in `CHANGELOG.md`:

Under `### Fixed`:

```markdown
- **Acquisition DateTime survived `anonymize()`.** `(0008,002A)` was absent from `PRIVACY_PROFILES["basic"]` while the plain `(0008,0022)` Acquisition Date it duplicates was removed, so raw acquisition timing reached exported DICOM after a full audit/anonymize pass. The four Performed Procedure Step date/time tags had the same gap. (#38)
- **Operator free text reached the WFDB header.** Channel Label `(003A,0203)` is operator-typed and was written verbatim into the `.hea` signal description and the `annotations.json` `lead` field whenever a channel carried no coded Channel Source Sequence. It is now emitted only when it is a recognisable signal name (see `KNOWN_LEAD_NAMES` in `gantry/waveform.py`); anything else becomes a positional `ch<N>` token. Fixed in the exporter rather than the privacy profile because the PHI scan is tag-gated, so a profile entry would not protect a bare `Session()`. (#39)
```

Under `### Changed`:

```markdown
- **BREAKING: `annotations.json` no longer includes `note` by default.** Unformatted Text Value `(0070,0006)` routinely holds free-text clinical commentary. Pass `session.export(folder, format="wfdb", include_annotation_text=True)` to restore the old behaviour. `(0070,0006)` was also added to the Basic profile, so a configured session that opts in receives the remediated value rather than raw text. (#39)
```

- [ ] **Step 5: Run the full suite**

Run `.venv/bin/python -m pytest -rs` FOREGROUND, `timeout: 300000`.
Expected: 393 passed (392 + 1 new), 1 skipped, 0 failed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_wfdb_privacy.py docs/waveforms.md CHANGELOG.md
git commit -m "docs: correct the waveform de-identification disclosures

The 'What is and isn't de-identified' section documented the Channel
Label and annotation-note passthroughs as known behaviour. Both are now
fixed, so the section described leaks that no longer exist.

Also pins that annotations.json `source` carries no device serial.
Issue #39 claimed it embedded one, giving 'gantry/0.6.0 (AcmeCart
SN-12345)' as an example; the code reads only (0008,0070) Manufacturer
and never (0018,1000). Nothing to remove, so the behaviour is pinned
with a test that was demonstrated red against a deliberate leak.

Keeps the time-of-day disclosure: SHIFT_DATE is a Study-level
remediation writing study.study_date, so instance time-of-day is
genuinely not shifted, and that remains deliberate and accurate.

Closes #39"
```

---

## After all tasks

- Post a correction on issue #39 recording that the device-serial claim was wrong, so the record does not outlive the fix.
- Both #38 and #39 are sub-issues of epic #42. Closing them closes the epic — verify it does, and close it manually if the sub-issue automation does not.

## Self-Review

**Spec coverage.** #38 → Task 1 (profile entry plus the end-to-end test that catches the #41 casing trap). #39 Channel Label → Task 2 (allowlist, both call sites, characterization tests converted). #39 note → Task 3 (exporter default, opt-in option, profile entry, end-to-end threading test). #39 `source` → Task 4 (pinned, issue corrected). #39's "also worth doing" doc disclosure → Task 4.

**Placeholder scan.** No TBDs. Every code step carries real code. Three steps deliberately instruct the implementer to *read an existing fixture and reuse it* rather than giving fixture code — `_annotated_instance`, `_write_annotated_fixture`, and the `build_ecg_dataset` reuse. That is not a placeholder: inventing a second fixture style beside an existing one is a defect, and I cannot paste the existing helper without having read the whole file. Each of those steps names the file to read and what to look for.

**Type consistency.** `wfdb_description(index: Optional[int] = None) -> str` is defined once in Task 2 and used with that exact signature at both call sites. `build_annotations(instance, waveform, source, include_text=False)` is defined in Task 3 and called with the positional forwarding shown. The public option name is `include_annotation_text` at the `session.export()` boundary and `include_text` inside `build_annotations` — deliberate, because the public name needs the `annotation` qualifier while the internal one already has annotation context. Task 3 Step 4 shows the exact translation point so this cannot drift.

**Expected test counts** chain: 381 → 384 → 389 → 392 → 393. Each task states its own expected total.
