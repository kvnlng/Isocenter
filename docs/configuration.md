# Configuration Guide

Isocenter uses a **Unified YAML Configuration** (schema version 2) to control all aspects of de-identification, including PHI tag rules, date shifting, and pixel redaction.

This file allows you to define a reproducible privacy policy that can be shared across your team or version controlled.

## Quick Reference

| Section | Description |
| :--- | :--- |
| **[version](#schema-version-2)** | The schema version, `"2.0"`. Optional; a file without it is version 2.0. |
| **[privacy_profile](#privacy-profile)** | Base set of rules: "basic@2026c" (or its short form "basic"), "none", or a path to a YAML profile. |
| **[date_jitter](#date-jitter)** | Randomly shifts dates to preserve intervals while hiding exact dates. |
| **[remove_private_tags](#private-tags)** | Removes vendor-specific private tags (odd groups). |
| **[phi_tags](#phi-tags)** | Overrides or adds specific tag rules, keyed by quoted `"gggg,eeee"` hex (e.g., `"0010,0010"` for Patient's Name). |
| **[machines](#pixel-redaction-machines)** | Defines burn-in redaction zones for specific equipment. |

---

## Complete Example

Save this as `isocenter_config.yaml`:

```yaml
# 0. Schema version (optional; quoted)
version: "2.0"

# 1. Privacy Profile (Base Rules)
# Options: "basic@2026c" ("basic" is its short form), "none", or path to
# external YAML
privacy_profile: "basic@2026c"

# 2. Date Jitter
# Range for the per-patient date shift, applied to every tag whose rule
# is SHIFT or JITTER (consistent across studies), and to Study Date under
# no rule or REPLACE with no value.
date_jitter:
  min_days: -30
  max_days: -10

# 3. Private Tags
# Remove all odd-group tags (vendor specific) unless whitelisted?
remove_private_tags: true

# 4. Custom PHI Tags (Overrides Profile)
phi_tags:
  "0008,1030":
    action: "EMPTY"
    name: "StudyDescription"

  "0008,103e":
    action: "REPLACE" # Writes "ANONYMIZED"
    name: "SeriesDescription"

  "0008,0080":
    action: "KEEP" # Exception: Keep InstitutionName

# 5. Pixel Redaction Rules (Machine Specific)
machines:
  - serial_number: "US-12345"
    model_name: "Voluson E10"
    redaction_zones:
      # [row_start, row_end, col_start, col_end]
      - [0, 50, 0, 800]   # Top Banner
      - [900, 1024, 0, 400] # Bottom Left Details
```

---

## Schema (version 2)

A configuration is read as exactly what it says. Every key and every value
type is listed below; **an unknown key, a value of the wrong type, or a
`version` this library does not read makes `load_config()` and
`audit(config_path=...)` raise `ValueError`**, naming the key, before
anything is assigned
([#711](https://github.com/kvnlng/Isocenter/issues/711),
[#712](https://github.com/kvnlng/Isocenter/issues/712),
[#713](https://github.com/kvnlng/Isocenter/issues/713)). Until 1.0 an
unknown key was ignored, so a misspelling loaded and meant something the
file did not say.

| Level | Key | Type |
| :--- | :--- | :--- |
| top level | `version` | a quoted `"MAJOR.MINOR"` string with no leading zero: `"2.0"` |
| top level | `privacy_profile` | `"basic@2026c"` or `"basic"`, `"none"`, or a path to a profile file; `null` (a bare `privacy_profile:`) is absent, and means the floor |
| top level | `phi_tags` | a mapping of quoted `"gggg,eeee"` tag, or [repeating-group key](#repeating-groups) (`"60xx,xxxx"`), to rule |
| top level | `date_jitter` | `{min_days: int, max_days: int}`, with `min_days` not greater than `max_days` |
| top level | `remove_private_tags` | `true` or `false` (unquoted) |
| top level | `machines` | a list of machine rules |
| machine rule | `serial_number` | a non-blank string, **quoted** if it is all digits (required) |
| machine rule | `manufacturer`, `model_name`, `comment` | strings (metadata; nothing reads them; `null` is read as absent) |
| machine rule | `redaction_zones` | a list of zones |
| zone | a list `[y1, y2, x1, x2]`, or a mapping of `roi` and `note` | `roi`: four non-negative integers; `note`: a string, or `null` (absent) |
| `phi_tags` rule | a string (the tag's name), or a mapping of `action`, `name`, `value` | `action`: one of the actions below; `name`: a string, or `null` (absent); `value`: a string, or `null` (absent: `REPLACE` writes its default) |

Three of these are traps YAML sets, and are refused rather than read:

* **An unquoted serial number is a number.** `serial_number: 12345` is the
  integer 12345, and `serial_number: 0123` is the octal integer 83; neither
  ever equals a Device Serial Number, so the rule matched nothing. Quote it:
  `serial_number: "0123"`.
* **A quoted boolean is a string.** `remove_private_tags: "false"` is a
  non-empty string, which read as true. A bare `remove_private_tags:` is
  null, which read as false. Write `true` or `false` unquoted
  (`yes`/`no` also work).
* **An unquoted version is a number.** `version: 2.10` is the number 2.1.
  Quote it.

**`version`.** A file with no `version` line is version 2.0, and always
will be. A present `version` must be a quoted string whose major is `2`;
any `2.x` loads. It is written one way: `"2.00"` and `"02.0"` are refused
rather than read as 2.0 ([#730](https://github.com/kvnlng/Isocenter/issues/730)). A 1.x release that adds a key or a value does so under a
new `2.x` minor, and never changes what an existing key means, so a file
written for a newer minor either means the same thing here or is refused
by the key or value this release does not have -- and then the refusal says
the file's version is newer than this isocenter's. `"1.0"` is refused: it
labelled the machines-only rules file before version 2 (December 2025),
and such a file loads unchanged as `"2.0"`.

---

## Detailed Options

### Privacy Profile

Sets the baseline rules that `phi_tags` then extends or overrides.

```yaml
privacy_profile: "basic@2026c"
```

**A built-in profile's name is pinned to the PS3.15 edition its table was taken from** ([#714](https://github.com/kvnlng/Isocenter/issues/714)). `basic@2026c` is the name, and a bare `basic` means `basic@2026c` in every 1.x: both load the same rules, and `session.configuration.privacy_profile` holds `"basic@2026c"` after either, so `save()` writes the pinned name back. `create_config()` writes it. A later PS3.15 edition arrives in a minor release as a new name, under a new configuration schema minor, never as a new meaning for this one. A name is looked up exactly: `basic@2026C` is refused, and so is `Basic` unless a file of that name exists, in which case it loads as an external profile, and a value containing `@` that this version does not ship raises `ValueError` saying which names it ships; such a value is never read as a file path.

* **`basic@2026c`** (short form **`basic`**): The Basic Profile column of *DICOM PS3.15 Annex E, Table E.1-1*, **edition 2026c** (`BASIC_PROFILE` in `isocenter/profiles.py`, 646 tag rules). Each row maps to a rule: `X` removes the attribute; `Z` and `X/Z` empty it, so a Type 2 attribute stays present; `D`, and every code with a D arm (`X/D`, `Z/D`, `X/Z/D`), is `REPLACE` with no value, which writes a dummy value consistent with the attribute's VR ([#557](https://github.com/kvnlng/Isocenter/issues/557)). PS3.15 Table E.1-1a defines D as "replace with a non-zero length value that may be a dummy value and consistent with the VR", and Z as "a zero length value, or a non-zero length value that may be a dummy value and consistent with the VR", so the dummy is what the code asks for where it resolves to D (an attribute Type 1 in its IOD, such as Verifying Observer Name in a Structured Report) and a value it permits where it resolves to Z. Until 1.0 `D` emptied the attribute and `X/D` removed it, and a Type 1 attribute was written zero-length or dropped. The dummies are the same constant for every instance and carry nothing of the original:

    | VR | Dummy written |
    | :--- | :--- |
    | AE, CS, LO, LT, PN, SH, ST, UC, UR, UT | `ANONYMIZED` |
    | DA, DT | `19000101` (a DT at date precision: no time of day) |
    | TM | `000000` |
    | AS | `000D` |
    | OB, OW, UN | two zero bytes |
    | OF, OL | four zero bytes |
    | OD, OV | eight zero bytes |

    The table's `50xx,xxxx` row is a rule, and the whole overlay group is one: see [Repeating groups](#repeating-groups) ([#556](https://github.com/kvnlng/Isocenter/issues/556)). A rule on a sequence removes the sequence, or empties it to zero items; identifiers nested inside any sequence are handled wherever they sit. It is **not** the whole of Annex E:
    * **UIDs are replaced by UIDs derived from the project secret** ([#544](https://github.com/kvnlng/Isocenter/issues/544)). Each `U` row, and Annotation Group UID's `D`, is `REPLACE` with no value, which on a UI attribute is UID replacement: the value becomes `2.25.` followed by a UUID derived from the value and the store's [project secret](#what-to-keep). One source UID gets one replacement wherever it appears in the store -- the Study Instance UID on every file of the study, a Referenced SOP Instance UID nested in a sequence and the SOP Instance UID it names -- so references between exported files still resolve, and the next pass recognises a replacement and leaves it alone. The replacement names the entity from then on: `export(subset=...)` accepts the UID a study, series or instance had before the pass as well as its replacement, and a later `ingest()` of another file of a replaced study or series joins it. Three limits: a UID in a private tag is not replaced ([#765](https://github.com/kvnlng/Isocenter/issues/765)); a UID written into free text, such as a description, is not found; and a redacted instance's new SOP Instance UID is derived from its source UID and its zones, so a reference to it from another file names the replacement of the source UID, which no exported file carries. The table's two `X/Z/U*` sequences (Referenced Image Sequence and Source Image Sequence) have no rule: they are kept, and the UIDs inside them are replaced. `REPLACE` with a `value:` on a UI attribute writes that value, as 0.9.8 did, and on Study, Series or SOP Instance UID does not move what the export is organised by: the file keeps the source Study and Series Instance UIDs, and a file whose SOP Instance UID is given a value is still named by its source UID. Use `REPLACE` with no value there.
    * No code from CID 7050 is written to De-identification Method Code Sequence `(0012,0064)`, not even `113100` (Basic Application Confidentiality Profile): the departures listed here are why `basic@2026c` does not claim that profile. What the export writes instead is under [What an exported file says about itself](#what-an-exported-file-says-about-itself) ([#554](https://github.com/kvnlng/Isocenter/issues/554)).
    * Patient's Name is `REPLACE` rather than the table's `Z`: it becomes `ANONYMIZED`, a dummy `Z` permits. Patient ID follows its `Z/D` code: `REPLACE` with no value on Patient ID is the keyed `ANON_` pseudonym, which is its dummy, and a Patient ID rule may not empty or remove it ([#537](https://github.com/kvnlng/Isocenter/issues/537)). Study Date follows the table and is exported zero-length; the floor shifts it instead.
    * **Where an `X/D` or `X/Z/D` attribute is Type 3 in its IOD, the table's code removes it, and Isocenter writes the dummy instead.** This departs from the code's X arm. PS3.15 E.1.1 permits it as protection ("either be removed from the Data Set, or have its value replaced by a different 'replacement value' that does not allow identification of the patient"); Isocenter does not know each attribute's type in each IOD, so it cannot tell which arm applies to an instance ([#558](https://github.com/kvnlng/Isocenter/issues/558)), and writes the value that is valid under every arm. Series Date and Time, Instance Creation Date and Protocol Name are the ones most image files carry; each is present in the export holding its dummy.
    * The four sequences whose code has a D arm keep `EMPTY` or `REMOVE`: Institution Code Sequence and Referenced Performed Procedure Step Sequence (`X/Z/D`) and Person Identification Code Sequence (`D`) are emptied to zero items, and Operator Identification Sequence (`X/D`) is removed. D on a sequence asks for items that are themselves valid, and what makes an item valid depends on the IOD, so no dummy item is written ([#557](https://github.com/kvnlng/Isocenter/issues/557)). Where one of these is Type 1 in its IOD, the export departs from the table there.
    * Deliberate departures from the table: Study and Series Description are emptied rather than removed, because the export directory names read them. Waveform Annotation Sequence (the Murmur annotation bridge reads it) and Icon Image Sequence have no rule; attributes inside them are still scanned, and an icon is dropped when its pixels may show what redaction removed, in two tiers ([#542](https://github.com/kvnlng/Isocenter/issues/542)): an instance's own Icon Image Sequence is dropped when that instance is redacted or has redaction zones applied at export, and every other nested icon -- a thumbnail under Referenced Image Sequence, of a *different* instance -- is dropped when any instance in the store is redacted or a zones rule matches any series in the store, whether or not that instance is in the export. Retired Curve groups `(50xx)` are removed, by the table's own `50xx,xxxx` row. Overlay groups `(60xx)` are removed whole by `60xx,xxxx`, a rule the table does not have: the table removes Overlay Data `(60xx,3000)` and Overlay Comments `(60xx,4000)`, which alone leaves an Overlay Plane module without its Type 1 element (PS3.3 C.9-2) and its free-text Overlay Description and Overlay Label in place, and PS3.15 E.1.1 says "If non-pixel data graphics or overlays contain identification, the de-identifier is required to remove them" ([#556](https://github.com/kvnlng/Isocenter/issues/556)). The two table rows are folded into the group rule, so `"60xx,xxxx": {action: KEEP}` keeps a whole, valid overlay. Isocenter's own redaction note in Derivation Description `(0008,2111)` is kept; any other Derivation Description is removed. Private attributes are the `remove_private_tags` sweep, not a rule.

    **Keeping UIDs.** PS3.15's Retain UIDs Option is `KEEP` on the `U` rows. Keep all of them or none: keeping Study Instance UID while SOP Instance UID is replaced, or a Referenced SOP Instance UID while the SOP Instance UID it names is replaced, leaves references in the export that name nothing in it. A kept UID links the export to its source for anyone who can see the source UIDs, and a UID that embeds a date carries it.

    ```yaml
    phi_tags:
      "0000,1001": {action: KEEP}  # Requested SOP Instance UID
      "0002,0003": {action: KEEP}  # Media Storage SOP Instance UID
      "0004,1511": {action: KEEP}  # Referenced SOP Instance UID in File
      "0008,0014": {action: KEEP}  # Instance Creator UID
      "0008,0017": {action: KEEP}  # Acquisition UID
      "0008,0018": {action: KEEP}  # SOP Instance UID
      "0008,0019": {action: KEEP}  # Pyramid UID
      "0008,0058": {action: KEEP}  # Failed SOP Instance UID List
      "0008,1155": {action: KEEP}  # Referenced SOP Instance UID
      "0008,1195": {action: KEEP}  # Transaction UID
      "0008,3010": {action: KEEP}  # Irradiation Event UID
      "0018,1002": {action: KEEP}  # Device UID
      "0018,100b": {action: KEEP}  # Manufacturer's Device Class UID
      "0018,2042": {action: KEEP}  # Target UID
      "0020,000d": {action: KEEP}  # Study Instance UID
      "0020,000e": {action: KEEP}  # Series Instance UID
      "0020,0052": {action: KEEP}  # Frame of Reference UID
      "0020,0200": {action: KEEP}  # Synchronization Frame of Reference UID
      "0020,9161": {action: KEEP}  # Concatenation UID
      "0020,9164": {action: KEEP}  # Dimension Organization UID
      "0028,1199": {action: KEEP}  # Palette Color Lookup Table UID
      "0028,1214": {action: KEEP}  # Large Palette Color Lookup Table UID
      "003a,0310": {action: KEEP}  # Multiplex Group UID
      "0040,0554": {action: KEEP}  # Specimen UID
      "0040,4023": {action: KEEP}  # Referenced General Purpose Scheduled Procedure Step Transaction UID
      "0040,a124": {action: KEEP}  # UID
      "0040,a171": {action: KEEP}  # Observation UID
      "0040,a172": {action: KEEP}  # Referenced Observation UID (Trial)
      "0040,a402": {action: KEEP}  # Observation Subject UID (Trial)
      "0040,db0c": {action: KEEP}  # Template Extension Organization UID
      "0040,db0d": {action: KEEP}  # Template Extension Creator UID
      "0062,0021": {action: KEEP}  # Tracking UID
      "0064,0003": {action: KEEP}  # Source Frame of Reference UID
      "006a,0003": {action: KEEP}  # Annotation Group UID
      "0070,031a": {action: KEEP}  # Fiducial UID
      "0070,1101": {action: KEEP}  # Presentation Display Collection UID
      "0070,1102": {action: KEEP}  # Presentation Sequence Collection UID
      "0088,0140": {action: KEEP}  # Storage Media File-set UID
      "0400,0100": {action: KEEP}  # Digital Signature UID
      "3006,0024": {action: KEEP}  # Referenced Frame of Reference UID
      "3006,00c2": {action: KEEP}  # Related Frame of Reference UID
      "300a,0013": {action: KEEP}  # Dose Reference UID
      "300a,0054": {action: KEEP}  # Table Top Position Alignment UID
      "300a,0083": {action: KEEP}  # Referenced Dose Reference UID
      "300a,0609": {action: KEEP}  # Treatment Position Group UID
      "300a,0650": {action: KEEP}  # Patient Setup UID
      "300a,0700": {action: KEEP}  # Treatment Session UID
      "300a,0785": {action: KEEP}  # Referenced Treatment Position Group UID
      "3010,0006": {action: KEEP}  # Conceptual Volume UID
      "3010,000b": {action: KEEP}  # Referenced Conceptual Volume UID
      "3010,0013": {action: KEEP}  # Constituent Conceptual Volume UID
      "3010,0015": {action: KEEP}  # Source Conceptual Volume UID
      "3010,0031": {action: KEEP}  # Referenced Fiducials UID
      "3010,003b": {action: KEEP}  # RT Treatment Phase UID
      "3010,006e": {action: KEEP}  # Dosimetric Objective UID
      "3010,006f": {action: KEEP}  # Referenced Dosimetric Objective UID
    ```

    The table removes, empties or replaces attributes research often wants: Patient's Weight and Size (PET SUV), Patient's Age, Protocol Name, Contrast/Bolus Agent, ROI Name and Channel Label. Give any of them `action: "KEEP"` to retain it. What `basic@2026c` contains is frozen for every 1.x: the rules 1.0 ships under it, which are the table, this mapping and these departures. The one exception is a row the published 2026c standard shows was transcribed wrongly, which a 1.x may correct as a **Breaking** changelog entry quoting the standard's row; anything else is a new name. A PHI status records the policy it was recorded under, and an `export()` that writes instances whose statuses were recorded under another policy, or before 1.0, writes one `WARNING` row naming the policies, so its report grades `REVIEW_REQUIRED` ([#555](https://github.com/kvnlng/Isocenter/issues/555)). A store anonymized under 0.9.7's 35-rule profile therefore no longer exports as though the policy in force had been applied: run `audit()` and then `anonymize()` on it before exporting again, and after reopening any store, load the configuration it was anonymized under. That removes what 0.9.8 removes, but cannot bring back the Type 2 attributes 0.9.7 removed (Accession Number, Referring Physician's Name, Study ID, Patient's Birth Date); only re-ingesting the source restores them.
* **`none`**: No base. The file's `phi_tags` are the whole policy. A bare `privacy_profile:` line (YAML null) is **not** `none`: it is absent, and means the floor ([#730](https://github.com/kvnlng/Isocenter/issues/730); until 1.0 it meant `none`, so a template's blank left unfilled switched the floor off).
* **External File**: You can provide a path to another YAML file (e.g., `./profiles/my_hospital_standard.yaml`) to inherit its rules. That file carries its rules under a `phi_tags:` mapping and nothing else, beside an optional `version`; any other key raises `ValueError` naming it ([#712](https://github.com/kvnlng/Isocenter/issues/712)). A profile file contributes only its `phi_tags`, so a `privacy_profile: basic` or `remove_private_tags:` line inside it would be ignored, and is refused instead: a configuration is not a profile. A bare tag map at its root raises `ValueError` too, because the root used to be read as the tags and a profile written like a config then loaded `privacy_profile` itself as a "tag".

Any other value is refused: `load_config()` raises `ValueError` naming it. (These docs once offered a `comprehensive` profile, which never existed; loading it warned and applied no base.)

A session that has loaded no configuration applies the **floor policy**, `FLOOR_POLICY` in `isocenter/profiles.py`: `basic@2026c` with three of its rules changed by the research defaults `create_config()` writes (Study Date jittered, Patient's Sex and Age kept): 646 rules. The floor is built on `basic@2026c` in every 1.x. The compliance report says so (`None (session defaults: the floor policy over basic@2026c)`), and says `None (no base profile)` for a session under `privacy_profile: none`, or under an external profile that contributed no rules.

**Omitting `privacy_profile` means the floor beneath your `phi_tags`.** A file with a few tags and no profile line extends the floor rather than replacing it, so a one-tag config cannot switch the floor off by accident. To opt a single tag out, give it `action: "KEEP"`; to opt out of the floor entirely, write `privacy_profile: "none"`.

!!! note "Patient's Name, Patient ID and Study Date"

    These three belong to the patient and the study, and the exporter writes the patient's and study's value on every file. Their rule governs that value ([#537](https://github.com/kvnlng/Isocenter/issues/537); until 0.9.8 `anonymize()` replaced all three whatever the rule said):

    | Rule | Patient's Name | Patient ID | Study Date |
    | :--- | :--- | :--- | :--- |
    | none, or `REPLACE` with no `value:` | `ANONYMIZED` | the keyed `ANON_` pseudonym | the per-patient shift |
    | `REPLACE` with `value:` | the value | refused | the value (a valid DA) |
    | `KEEP` | kept | kept | kept |
    | `EMPTY` | zero-length | refused | zero-length |
    | `REMOVE` | zero-length in the file; removed from the instance's own copy | refused | zero-length in the file; removed from the instance's own copy |
    | `SHIFT` / `JITTER` | refused | refused | the per-patient shift |

    `REMOVE` still writes the element, at zero length: both are Type 2 in their modules, so a file without them would not conform. A Patient ID rule other than `KEEP` or `REPLACE` with no value raises `ValueError`, because the ID is what keeps two patients apart and `anonymize()` merges patients that share one. On Study Date, the string form (`"0008,0020": "Study Date"`) is `REPLACE` with no value, and means the shift.

### Date Jitter

Sets the range of the per-patient date shift. It is applied to every tag whose rule is `SHIFT` or `JITTER`, and to Study Date when it has no rule or `REPLACE` with no value; other date tags follow their own rule, and the `basic` profile *removes or empties* the dates Table E.1-1 names (Series, Acquisition, Content, Instance Creation and the rest) rather than shifting them. A date tag no rule names is exported as ingested: under `basic` or the floor that is a date the table does not name, and under `privacy_profile: none` it is every date your `phi_tags` leave out, so add a rule for any such date that must not leave the site.

* **Logic**: Isocenter derives each patient's offset from a secret it generates for the project and keeps inside the session store. The offset is the same for every study and series of that patient, so intervals survive, and it cannot be computed from the exported pseudonym, or from any other value the derivation uses, without that secret. That is a statement about the offset, not a guarantee that no exported date can be recovered: a date tag no rule shifts is exported as it was, a UID the configuration keeps carries any date it embeds ([#544](https://github.com/kvnlng/Isocenter/issues/544)), and a whole-day shift keeps the weekday. A patient a store de-identified before 0.9.7 keeps that store's unkeyed, computable offset (see the second Migration Guide link below). The offset is not random per run, and it hides absolute dates only from someone who holds neither the store nor the secret: a store and its exports must not travel together, because the store holds the secret and its audit log records each offset. From 0.9.7 the log file (`isocenter.log`) records neither original Patient IDs nor offsets. Offsets and pseudonyms belong to the store, and a secret cannot be carried to another one; see [What to keep](#what-to-keep). Within one store, a later batch for a patient already in it, ingested under its original Patient ID, joins that patient's studies when `anonymize()` gives it the same pseudonym, and its dates land on the same offset ([#548](https://github.com/kvnlng/Isocenter/issues/548)). Releases before 0.9.7 derived the offset without a secret, so it could be computed from an exported file ([Migration Guide](migration.md#stores-de-identified-before-097-ghsa-phg9-vcvc-j4r7)).
* **Config**:

    ```yaml
    date_jitter:
      min_days: -10
      max_days: 10
    ```

    For a fixed shift, give both bounds the same value (`{min_days: -5, max_days: -5}`). A bare integer (`date_jitter: -5`) is refused, and so is `min_days` greater than `max_days`, which has at least one bound wrong ([#713](https://github.com/kvnlng/Isocenter/issues/713)).

### Private Tags

DICOM Private Tags (Odd Group Numbers, e.g., `0009,xxxx`) often contain hidden PHI strings dumped by the machine.

```yaml
remove_private_tags: true
```

* `true`: Removes **ALL** private tags. (Recommended for safety).
* `false`: Retains them (Use only if you are sure they are safe or strictly needed for analysis).

"All" includes private *sequences*, at every depth, and that is newer
than it sounds: until
[#167](https://github.com/kvnlng/Isocenter/issues/167) the sweep read an
instance's attributes only, so a private `SQ` was never a candidate and
survived into the exported file with its private creator stripped off
it. A private sequence nested inside another sequence is swept on the
same rule.

The flag governs the private tags Isocenter *holds*, which is not every
private tag in your source files. Whether a private value is held is
decided by its **size**, not its VR
([#151](https://github.com/kvnlng/Isocenter/issues/151)):

| Private tag | `remove_private_tags: true` | `remove_private_tags: false` |
| :--- | :--- | :--- |
| Text or numeric VR (`LO`, `SH`, `DS`, ...) | Removed | **Kept**, and written to the exported file |
| Binary value (`OB`, `OW`, `OF`, `OD`, `OL`, or `UN`) of 65534 bytes or less | Removed | **Kept**, and written to the exported file under the VR it was read with (`UN` when the source was Implicit VR, which states none) |
| Binary value over 65534 bytes | Dropped at ingest, `DATA_LOSS` row | Dropped at ingest, `DATA_LOSS` row |

A kept binary value whose bytes are not a whole number of its VR's words
-- an `OL` of six bytes, say -- is written `UN` instead, and the
instance's export draws one `WARNING` row naming the tag, the VR it was
read with and the one it was written under
([#676](https://github.com/kvnlng/Isocenter/issues/676)). The VR is
visible only in an explicit-VR export -- the compressed export of an
instance with pixels; an uncompressed export, and any export of an
instance without pixels, is Implicit VR and carries no VR on the wire.
A binary value read from an Implicit VR source is written `UN` even when
pydicom's private dictionary names a VR for its creator (a Siemens CSA
header reads back as `OB`): the file itself stated none.

The limit is `BINARY_RETENTION_MAX_BYTES` in `isocenter/io_handlers.py`,
the largest value an explicit-VR 16-bit length field can carry. Because it
weighs the value rather than the VR it was read with, explicit-VR and
implicit-VR copies of one study give the same answer: under implicit VR
pydicom reads every private tag as `UN`, and a `UN` blob takes exactly the
same size rule. Before 0.9.1 the rule keyed on VR, so the two syntaxes
disagreed; if you read an older description of a binary-VR private tag
being "always dropped", it is out of date.

**One `UN` value is resolved rather than kept opaque.** If a private
`UN` value begins with the item tag `(FFFE,E000)` and re-encodes byte
for byte as an implicit-VR sequence, it is ingested as a sequence -- the
same graph the explicit-VR reading of the same file produces -- so the
PHI scan walks inside it, remediation reaches the values there, and
`remove_private_tags: true` removes it
([#167](https://github.com/kvnlng/Isocenter/issues/167)). A recovered
sequence is exempt from the size rule, and its items then follow the
ordinary rules, so a large binary child inside it is dropped and reported
like any other. A candidate that does *not* re-encode exactly keeps its
bytes untouched at ingest (if they are within the size limit) and files a
`SCAN_GAP` entry, which appears in section 3.2 of the compliance report
with a disposition resolved at report time: under
`remove_private_tags: true` the sweep removes the bytes like any other
private attribute and the entry reads `removed before export` (grading
`PASS`); under `false` they are retained byte-for-byte, the entry reads
`retained for export`, and the session grades `REVIEW_REQUIRED`.

!!! warning "Private binary values over 64 KiB cannot be retained"

    A binary value larger than 65534 bytes never enters the object graph,
    so `remove_private_tags: false` has nothing left to keep. Setting
    `false` does not fail: the exported file simply does not have it.

    **The loss is announced, not silent.** Each dropped element is logged
    as a warning and written to the audit log as a `DATA_LOSS` entry
    naming the tag *and its VR*. It reaches you in three places: the
    session log, section 3.1 (*Data Loss*, under *Data Loss & Unscanned
    Content*) of the compliance report written by
    `session.generate_report(path)`, and
    `session.store_backend.get_audit_losses()` if you want the rows
    directly. A dropped *private* element grades the run
    `REVIEW_REQUIRED`. Read that section before concluding a vendor block
    came through a run intact.

    **This is settled rather than pending**
    ([#125](https://github.com/kvnlng/Isocenter/issues/125)). If you need
    those bytes, keep your source files -- Isocenter never modifies them,
    so the vendor block is still there to go back to.

**Why large values are not stored.** Holding a megabyte vendor blob in
`attributes` makes it permanently resident, and memory scaling on 100GB+
datasets depends on heavy arrays never being resident by default; the
64 KiB cap bounds what retention can cost per element. Routing large values
to the sidecar instead means giving private tags an offset/length
representation the EAV table does not have, plus a lazy loader and an
export re-merge path. `session.compact()` rewrites the sidecar and rewires
every offset it knows about, so a class of offset it does not know about is
silent corruption after the first compaction. (It also holds the sidecar
gate for the whole rewrite, so any writer of such an offset would have to
take that gate too, and it refuses outright while a `redact()` or
`ingest()` pass is open -- see `compact()`'s API entry.) That is design
work, not a flag.

!!! note "Standard binary elements follow the same size rule"

    Overlay Data `(60xx,3000)` and the palette color LUTs `(0028,120x)`
    are `OW`. At or below 65534 bytes they are carried into the export --
    a 256-entry palette LUT is 512 bytes -- and above it they are dropped
    and reported. `PixelData` and `WaveformData` are the only binary
    elements routed to the sidecar.

    Under `basic@2026c` and the floor the whole overlay group is removed
    ([#556](https://github.com/kvnlng/Isocenter/issues/556)), so the
    size rule matters only where a policy keeps it (`"60xx,xxxx":
    {action: KEEP}`, or `privacy_profile: none`). There, an overlay's
    *descriptors* (`OverlayRows`, `OverlayColumns`, `OverlayBitPosition`
    and friends) are `US`, so they survive, and an export from which a
    large overlay plane was dropped declares a plane it does not carry.
    The descriptors are not stripped by the size rule: an overlay may
    legitimately live in the unused high bits of `PixelData` (addressed
    by `OverlayBitPosition`), and since Isocenter preserves `PixelData`
    intact, those overlays survive and their descriptors are the only
    pointer to them. When the group rule removes the descriptors, such
    high-bit overlay bits stay in `PixelData`, as they always did, with
    nothing left that points to them.

    A dropped *standard* element is listed in the report's Data Loss
    section but does not change the grade
    ([#137](https://github.com/kvnlng/Isocenter/issues/137)).

### PHI Tags

Define specific rules for individual DICOM tags. Keys are `"gggg,eeee"` hex strings (e.g. `"0010,0010"`); case is normalised on load, so either case works.

**Supported Actions:**

| Action | Logic | Example Config |
| :--- | :--- | :--- |
| **`REPLACE`** | Replaces the value with its `value:`. With no `value:` (or `null`), it writes the dummy for the standard tag's VR from the [table above](#privacy-profile): `ANONYMIZED` on a text VR, `19000101` on a DA or DT, `000000` on a TM, `000D` on an AS, zero bytes on a binary VR ([#538](https://github.com/kvnlng/Isocenter/issues/538), [#557](https://github.com/kvnlng/Isocenter/issues/557), [#730](https://github.com/kvnlng/Isocenter/issues/730)); `ANONYMIZED` on a private or unknown tag. Patient ID's is the keyed pseudonym, and Study Date's is the shift. It replaces a value that is there: a zero-length value is left zero-length. A tag's string form (`"0008,0080": "Institution Name"`) is `REPLACE` with no value. | `action: "REPLACE"`, `value: "Project-X"` |
| **`REMOVE`** | Completely deletes the tag from the dataset. Patient's Name and Study Date are the exception: they are written at zero length (see the note above). | `action: "REMOVE"` |
| **`EMPTY`** | Sets the tag value to an empty string (zero-length bytes for a binary VR). | `action: "EMPTY"` |
| **`SHIFT`** | Applies the per-patient Date Jitter offset. DA and DT only; a value that is not a date (a time, a six-digit date, a range, a DateTime at hour or minute precision) is left unchanged and recorded as declined ([#559](https://github.com/kvnlng/Isocenter/issues/559)). | `action: "SHIFT"` |
| **`JITTER`** | Same as `SHIFT`. The generated scaffold and the floor policy use it for Study Date. | `action: "JITTER"` |
| **`KEEP`** | Explicitly retains the original value (Exception to profile). | `action: "KEEP"` |

A rule mapping's keys are `action`, `name` and `value`; any other key raises `ValueError` naming it (a misspelt `actoin: KEEP` used to leave the action at `REPLACE`). Any other action makes `load_config()` raise `ValueError` naming the tag. So does a rule Isocenter cannot honour, checked on the policy the file resolves to (profile and file merged), and again by `set_phi_tag()` and by `audit()` for a `phi_tags` assigned in code:

* a `value:` under any action but `REPLACE`, a `value:` that is not a string, or a `replacement:` key (the name `set_phi_tag` saved in 0.9.7; rename it `value:`);
* a Patient ID `(0010,0020)` rule other than `KEEP` or `REPLACE` with no value;
* `SHIFT` or `JITTER` on a standard tag that is not DA or DT ([#559](https://github.com/kvnlng/Isocenter/issues/559));
* `REPLACE` on a standard tag whose VR cannot hold what it writes ([#560](https://github.com/kvnlng/Isocenter/issues/560)). With no `value:`, that is a VR with no dummy: a numeric VR (US, SS, UL, SL, UV, SV, FL, FD, DS, IS), AT, and a VR the dictionary gives as a choice (`US or SS`); 0.9.8 also refused DA, DT, TM, AS and the binary VRs here, which write a dummy since [#557](https://github.com/kvnlng/Isocenter/issues/557), and UI, where `REPLACE` with no value is UID replacement since [#544](https://github.com/kvnlng/Isocenter/issues/544). With a `value:`, a value the VR cannot hold, such as text in a DA. Use `EMPTY` or `REMOVE`, `JITTER` for a date, or a `value:` the VR can hold. Study Date's `REPLACE` with no value is the shift and is allowed.
* a [repeating-group key](#repeating-groups) with an action other than `REMOVE` or `KEEP`, or in its string form ([#556](https://github.com/kvnlng/Isocenter/issues/556)).
* a `REPLACE` `value:` holding a range: a `-` in a DA or TM, or in a DT anywhere but its UTC offset at the end (`20230515104822-0500` is one DateTime; `20230101-20230201` is a range);
* a `REPLACE` `value:` whose count of `\`-separated values the standard tag's value multiplicity does not allow: a `\` on a tag that holds one value, two values on Image Orientation (Patient), which holds six, or three on Patient Orientation, which holds two. Each value is also checked against the VR on its own.

Private tags are not checked against a VR: the exporter writes a private value its VR cannot hold as `LO`.

#### Repeating groups

PS3.15 Table E.1-1 spells the retired Curve module and the Overlay Plane module as repeating groups, and `phi_tags` accepts those spellings as keys ([#556](https://github.com/kvnlng/Isocenter/issues/556)):

* `"50xx,xxxx"` and `"60xx,xxxx"`: every element of every group;
* `"50xx,eeee"` and `"60xx,eeee"` (for example `"60xx,0022"`, Overlay Description): that element, in every group.

The `x` may be written in either case. A key covers the **even** groups 5000-501E or 6000-601E only (PS3.5 7.6). The odd groups between them are private, and belong to `remove_private_tags`. Such a key takes `REMOVE` or `KEEP` and nothing else, because it names elements of many VRs; any other action, or the string form, raises `ValueError` naming the key. The most specific key wins: `"6002,0022"` over `"60xx,0022"`, and `"60xx,0022"` over `"60xx,xxxx"`. So under `basic@2026c`, which removes both groups whole:

```yaml
phi_tags:
  "60xx,xxxx": { action: "KEEP" }     # keep every overlay, Overlay Data included
  "60xx,3000": { action: "REMOVE" }   # ...or keep the module and remove its data,
  "60xx,4000": { action: "REMOVE" }   #    as the table's own two rows do
```

An element a key covers is matched only where it holds a value in the graph; an Overlay Data over 65534 bytes never reaches it, and has its `DATA_LOSS` row from ingest (see [Private Tags](#private-tags)). Every element removed has its own `REMEDIATION_REMOVE` row.

**Example:**

```yaml
phi_tags:
  "0008,1030": { "action": "EMPTY", "name": "StudyDescription" }
  "0010,0030": { "action": "SHIFT", "name": "PatientBirthDate" }
  "0008,0080": { "action": "REPLACE", "value": "Project-X", "name": "InstitutionName" }
```

### What an exported file says about itself

`export(format="dicom")` writes up to three elements that say how the file was de-identified ([#554](https://github.com/kvnlng/Isocenter/issues/554)). They state which software ran and which policy it applied. They do not say whether that policy is enough: that is your configuration's call.

**When.** Only on an instance whose patient, study and own PHI status all read `REMEDIATED` or `CLEARED`, recorded under one policy, and that policy is the one in force or one this session ran `audit()` under. That is the condition under which the export writes no "recorded under a policy other than the one in force" notice. The markers are not written on:

* an instance never audited;
* an instance with a finding left open, whether declined, not handed to `anonymize()`, or a Series finding;
* an instance whose own attributes were edited after its pass;
* a patient restored with `recover_patient_identity(restore=True)`;
* a store reopened under another policy and not audited again;
* a store written before 1.0.

The markers rest on the same status the report's grade reads. Three kinds of edit after the pass do not yet move that status, so the file still says `YES` ([#767](https://github.com/kvnlng/Isocenter/issues/767)): an owner field assigned directly, such as `patient.patient_name = ...`; a Series field, such as `series.series_instance_uid = ...`; and a value set inside a nested sequence item.

**Attributes, not pixels.** `YES` records that the attribute policy was applied in full and that the file declares no burned-in text. Isocenter does not read the pixels to decide it. PS3.3 defines `YES` as identity removed from the Pixel Data as well, so a file whose Burned In Annotation `(0028,0301)` says `YES` does not get it: the source's own `(0012,0062)`, if any, stays, and the De-identification Method value and the temporal marker are still written. Text drawn into the pixels is `redact()`'s to clear (see [Pixel Redaction](#pixel-redaction-machines)); it writes Burned In Annotation `NO` on the pixels it clears, and that file then says `YES`. A `(0012,0062)` already in the source is kept as the source wrote it, `YES` included, beside a Burned In Annotation `YES`: it is the source's claim, not Isocenter's, and the hold-back governs only the `YES` Isocenter writes. The file is read as it is written, so a rule of yours that removes Burned In Annotation removes the declaration too, and the file then says `YES`: that is your policy's call.

**What.**

* **Patient Identity Removed `(0012,0062)`: `YES`.** A source value of `NO` is replaced, and a source `YES` stays.
* **De-identification Method `(0012,0063)`** gains one value, after any values the source carried, which are kept in order: `isocenter/<version>; <policy>; v1:<8 hex>`.
    * `<policy>` is `basic@2026c`, `floor over basic@2026c` or `none`. An external profile is `external profile`, never its path.
    * The hex is the first 32 bits of the policy's fingerprint (`phi_status_policy`). It tells two policies under one label apart, such as the floor and the floor with overrides.
    * No value is added if the last value is already this one. So re-exporting an ingested Isocenter export under the same policy and release adds nothing.
* **Longitudinal Temporal Information Modified `(0028,0303)`**, read from the file's own dates. Every DA and DT element is read, including nested ones and private ones whose VR is recorded. A private element from an implicit-VR source has no recorded VR, so a date in it is not read and does not stop `MODIFIED`; it is exported as `UN`, and `remove_private_tags` (on by default) removes it.
    * `REMOVED` when every date is empty or the dummy `19000101`.
    * `MODIFIED` when the rest are shifts this store wrote.
    * Nothing when any date is as it was ingested; the source's value, if any, then stays.
    * TM is not read: a time of day beside a shifted date does not place the patient in time.
    * `UNMODIFIED` is never written, because a date kept on purpose cannot be told from one no rule named.
* **De-identification Method Code Sequence `(0012,0064)`**: no code is written (see the departures above). A source's items pass through.

**Your rules decide.** Table E.1-1 has no row for any of the three elements. A rule you write on one, of any action, `KEEP` included, means the export does not stamp that element: `KEEP` over a source `NO` exports `NO`. The other two are still stamped.

**Where not.** `DicomExporter.write_tree()` writes none of them: it is the serializer without the pipeline. A WFDB header has no such field. The markers are never written into the store.

### Pixel Redaction (Machines)

Automatically scrubs burned-in text (pixels) for specific devices. Isocenter identifies the machine using the `DeviceSerialNumber` (0018,1000) tag.

```yaml
machines:
  - serial_number: "SN-9999"
    model_name: "Documentation Only"
    redaction_zones:
      - [0, 100, 0, 500]
```

* **`serial_number`** (Required): Exact match for `0018,1000`, or `"*"` for every series that has one. A series with no Device Serial Number matches no rule.
* **Every matching rule applies**, in `redact()` and at export alike: an exact rule and a `"*"` rule, or two rules for one serial, each zero their zones ([#580](https://github.com/kvnlng/Isocenter/issues/580)). The export applies them whether or not `redact()` has run.
* **`redaction_zones`**: List of regions to zero out.
  * Format: `[y1, y2, x1, x2]` (Row Start, Row End, Col Start, Col End), or `{"roi": [y1, y2, x1, x2]}`, the shape the shipped knowledge base uses and `create_config()` copies for a machine it recognises.
  * Coordinates are 0-indexed.
  * End must be strictly greater than start on both axes. `load_config()` accepts a zone whose start equals its end, but it selects no pixels, and `redact()` fails every instance it applies to with `RedactionError`.


### Generating Configuration Templates

You can generate a starter `isocenter_config.yaml` based on your current session inventory. This is useful for bootstrapping a new configuration file that includes all detected machines.

```python
# Inspects data, finds all unique machine serials, and writes a config file
session.create_config("my_new_policy.yaml")
```

---

## Programmatic Configuration

In addition to YAML files, you can manage the configuration dynamically using Python code via the `session.configuration` property.

### Accessing Configuration

```python
import isocenter

session = isocenter.Session("my_project.db")
session.load_config("isocenter_config.yaml")

config = session.configuration

print(config.rules)    # machine redaction rules
print(config.phi_tags) # the full tag policy in force, floor included
```

### Methods

These methods change the configuration **in memory**. Since 1.0 they do not write the file `load_config()` read ([#715](https://github.com/kvnlng/Isocenter/issues/715)); the first change after a load or a save prints one line saying the file is unchanged. Call `session.configuration.save()` to write it, or turn on auto-save for the session with `session.configuration.auto_save = True`, after which every change is written as it is made, to whichever file the session loaded last. `save()` raises `ValueError` when there is no file to write (a session that loaded none: set `session.configuration.config_path` first), and it raises the error of a write that fails. With auto-save on, a change whose write fails or is refused is undone, and with no file to write each method raises that `ValueError` before changing anything.

`save()` writes a new file. It names the profile rather than copying it: `privacy_profile: basic@2026c` (or the external file's path, `none`, or no line for the floor), and under `phi_tags` only the rules that differ from the profile's. Then it writes `date_jitter`, `remove_private_tags` and every machine rule. It always writes a `version` line, `"2.0"`. **Comments and layout in the loaded file are not kept.** Keep a hand-edited file under version control, and if its comments matter, edit it by hand rather than through these methods. `save()` refuses a policy that has lost a rule its profile supplies (by deleting from `phi_tags` directly), because the file would restore that rule; give the tag `action: KEEP` instead. An external profile is read again when `save()` runs, so the saved file reloads to what the session holds; if the profile file has gained a rule since the load, `save()` refuses and asks you to load it again. `auto_remediate_config()` changes the rules in memory; call `save()` afterwards to keep them.

#### add_rule()

`add_rule(serial_number, manufacturer="Unknown", model="Unknown", zones=None)`

Add a new machine redaction rule dynamically.

```python
# Add a rule for a specific ultrasound machine
session.configuration.add_rule(
    serial_number="US-5555",
    manufacturer="GE",
    model="Voluson",
    zones=[[0, 50, 0, 800]] # [y1, y2, x1, x2]
)
session.configuration.save()  # write it to the loaded file
```

#### delete_rule()

`delete_rule(serial_number)`

Remove a rule by serial number.

```python
session.configuration.delete_rule("US-5555")
```

#### update_rule()
`update_rule(serial_number, updates)`

Update a rule by serial number.

`add_rule()` and `update_rule()` refuse, with `ValueError`, a rule `load_config()` would refuse -- an unknown key such as `redaction_zone`, a serial that is not a string, a malformed zone -- and leave the rules and the file unchanged, so the file `save()` writes always loads again ([#712](https://github.com/kvnlng/Isocenter/issues/712)).

#### set_phi_tag()

`set_phi_tag(tag, action, replacement=None)`

Update the policy for a specific DICOM tag. `replacement` is stored as the rule's `value:`, which `REPLACE` writes ([#538](https://github.com/kvnlng/Isocenter/issues/538)). An unknown action, or a rule Isocenter cannot honour (see [PHI Tags](#phi-tags)), raises `ValueError` and leaves the policy and its file unchanged.

```python
# Force removal of PatientWeight
session.configuration.set_phi_tag("0010,1030", "REMOVE")

# Blank StudyDescription
session.configuration.set_phi_tag("0008,1030", "EMPTY")
```

## What to keep

A de-identification run depends on three things, and the configuration file is only one of them ([#716](https://github.com/kvnlng/Isocenter/issues/716)).

| | What it decides | Where it is | If you lose it |
| :--- | :--- | :--- | :--- |
| **The configuration file** | Which tags are kept, removed, emptied, replaced or date-shifted; the date-shift *range*; whether private tags go; the pixel zones for each machine. | A YAML file you keep, under version control. | Nothing you cannot write again. A 1.0 file loads and means the same thing in every 1.x, and `privacy_profile: basic@2026c` names one fixed table. But a store remembers the policy each PHI status was scanned under, as a fingerprint of the rules: a rewritten file must be the same policy -- the same fingerprint, which covers every rule key but `name` (a `value: null` line and no `value` line differ) and `remove_private_tags` -- or every export from a reopened store warns, and its report grades `REVIEW_REQUIRED`, until the next `audit()` ([#555](https://github.com/kvnlng/Isocenter/issues/555)). |
| **The store** (`Session("my_project.db")`) and its **project secret** | Each patient's `ANON_` pseudonym and date offset, and every replacement UID. All are derived from a secret the store generates the first time `audit()`, `anonymize()` or `redact()` needs one, and keeps inside itself. | Two files that belong together: the session file (`my_project.db`) and the pixel sidecar beside it, named after it (`my_project_pixels.bin`). | The pseudonyms, offsets and UIDs it made. The same configuration over a new store gives every patient a **new** pseudonym and a **new** offset, and every study, series and instance **new** UIDs. Files already exported keep theirs, but data exported later will not link to them, and the intervals between a patient's old and new studies are lost. |
| **`isocenter.key`** (only with [reversible anonymization](quickstart.md#4-backup-identity-optional)) | Whether original identities written into exported files can be recovered. | The file `enable_reversible_anonymization()` names. | Recovery. Identities in files exported under that key cannot be recovered by anyone. |

**The configuration does not reproduce pseudonyms or date shifts.** They belong to the store they were made in. A later batch for the same patients goes into the same store: `ingest()` adds to it, and a patient ingested under the same original Patient ID gets the same pseudonym and the same offset ([#548](https://github.com/kvnlng/Isocenter/issues/548)).

**A project secret cannot be moved to another store.** Isocenter has no way to export one or to load one, and 1.0 offers none. Every session that opens a store uses that store's secret; a session on a different store uses a different one.

**A copy of the store is the same store, secret included.** The store is both files: copy `my_project.db` and `my_project_pixels.bin` together, and keep them under the same basename, because the session finds its sidecar by the `.db` file's name. A copy of the `.db` alone keeps the pseudonyms and offsets; for the instances ingested before the copy, their pixels are not in the copy, and export fails for them. Isocenter does not detect copies or refuse them. Treat every copy as the project itself: back it up as you back up `isocenter.key`, and never send it, or any copy of it, with an export. Whoever holds the store can recover every shifted date, and its audit log records each offset.

**Replacement UIDs belong to the store as well** ([#544](https://github.com/kvnlng/Isocenter/issues/544)). A source UID gets the same replacement in every export from the store and from any copy of it, and a different one from any other store. A redacted instance's new SOP Instance UID is derived from the same secret, its source UID and its zones, so redacting it again with the same zones gives the same UID. A store that holds replaced UIDs but has lost its secret refuses `audit()`, `anonymize()` and `redact()`, because a new secret would not recognise them and would replace them a second time.

## Auto-Discovery of Redaction Zones

To help identify pixel redaction zones (e.g., for burned-in PHI), Isocenter provides a discovery tool that analyzes a sample of images from a specific machine to find common text "hotspots".

`discover_redaction_zones()` returns a `DiscoveryResult` holding the raw text
candidates, not zones. `to_zones()` groups them, and each group's `zone` entry
is the `[y1, y2, x1, x2]` list a rule stores; see
[Zone Discovery](ocr.md#setting-up-new-machines-zone-discovery) for filtering
and tuning the grouping.

```python
# Discover potential redaction zones for a machine
result = session.discover_redaction_zones(
    serial_number="US-12345",
    sample_size=50
)
zones = [z["zone"] for z in result.to_zones()]

print(f"Discovered {len(zones)} zones: {zones}")

# Apply these zones to your configuration
if zones:
    session.configuration.add_rule(
        serial_number="US-12345",
        zones=zones
    )
```