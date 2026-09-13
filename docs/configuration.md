# Configuration Guide

Isocenter uses a **Unified YAML Configuration** (v2.0) to control all aspects of de-identification, including PHI tag rules, date shifting, and pixel redaction.

This file allows you to define a reproducible privacy policy that can be shared across your team or version controlled.

## Quick Reference

| Section | Description |
| :--- | :--- |
| **[privacy_profile](#privacy-profile)** | Base set of rules: "basic", "none", or a path to a YAML profile. |
| **[date_jitter](#date-jitter)** | Randomly shifts dates to preserve intervals while hiding exact dates. |
| **[remove_private_tags](#private-tags)** | Removes vendor-specific private tags (odd groups). |
| **[phi_tags](#phi-tags)** | Overrides or adds specific tag rules, keyed by quoted `"gggg,eeee"` hex (e.g., `"0010,0010"` for Patient's Name). |
| **[machines](#pixel-redaction-machines)** | Defines burn-in redaction zones for specific equipment. |

---

## Complete Example

Save this as `isocenter_config.yaml`:

```yaml
# 1. Privacy Profile (Base Rules)
# Options: "basic", "none", or path to external YAML
privacy_profile: "basic"

# 2. Date Jitter
# Range for the per-patient date shift, applied to Study Date and to
# every tag whose rule is SHIFT or JITTER (consistent across studies).
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

## Detailed Options

### Privacy Profile

Sets the baseline rules that `phi_tags` then extends or overrides.

```yaml
privacy_profile: "basic"
```

* **`basic`**: A reduced *DICOM PS3.15 Annex E Basic Profile* (`BASIC_PROFILE` in `isocenter/profiles.py`, 35 tags). Retains some descriptors but removes direct identifiers. It does **not** replace UIDs: Study, Series and SOP Instance UIDs are exported as they were ingested (a redacted instance gets a new SOP Instance UID, and references to it are not updated), so an export can be linked back to its source by anyone who can see the source UIDs ([#544](https://github.com/kvnlng/Isocenter/issues/544)).
* **`none`**: No base. The file's `phi_tags` are the whole policy.
* **External File**: You can provide a path to another YAML file (e.g., `./profiles/my_hospital_standard.yaml`) to inherit its rules. That file must carry them under a `phi_tags:` mapping — a config-shaped file works, a bare tag map at its root raises `ValueError`, because the root used to be read as the tags and a profile written like a config then loaded `privacy_profile` itself as a "tag".

Any other value is refused: `load_config()` raises `ValueError` naming it. (These docs once offered a `comprehensive` profile, which never existed; loading it warned and applied no base.)

A session that has loaded no configuration applies the **floor policy**, `FLOOR_POLICY` in `isocenter/profiles.py`: the basic profile plus the three research defaults `create_config()` writes (Study Date jittered, Patient's Sex and Age kept), 36 rules.

**Omitting `privacy_profile` means the floor beneath your `phi_tags`.** A file with a few tags and no profile line extends the floor rather than replacing it, so a one-tag config cannot switch the floor off by accident. To opt a single tag out, give it `action: "KEEP"`; to opt out of the floor entirely, write `privacy_profile: "none"`.

!!! warning "Three tags are not governed by `phi_tags`"

    Patient's Name `(0010,0010)`, Patient ID `(0010,0020)` and Study Date `(0008,0020)` belong to the patient and study, and `anonymize()` always replaces them -- the name with `ANONYMIZED`, the ID with an `ANON_` pseudonym, the study date with its per-patient shift -- under every profile, including `none`. A `KEEP`, `REMOVE`, `EMPTY` or `REPLACE` rule on one of them does not change the exported value ([#537](https://github.com/kvnlng/Isocenter/issues/537)).

### Date Jitter

Sets the range of the per-patient date shift. It is applied to Study Date and to every tag whose rule is `SHIFT` or `JITTER`; other date tags follow their own rule, and the `basic` profile *removes* Series, Acquisition and Content dates rather than shifting them. A date tag no rule names is exported as ingested: Instance Creation Date `(0008,0012)`, for one, is covered by no shipped profile, so add a rule for it (or for any other date your data carries) if it must not leave the site.

* **Logic**: Isocenter derives each patient's offset from a secret it generates for the project and keeps inside the session store. The offset is the same for every study and series of that patient, so intervals survive, and it cannot be computed from the exported pseudonym, or from any other value the derivation uses, without that secret. That is a statement about the offset, not a guarantee that no exported date can be recovered: a date tag no rule shifts is exported as it was, UIDs that embed a date carry it ([#544](https://github.com/kvnlng/Isocenter/issues/544)), and a whole-day shift keeps the weekday. A patient a store de-identified before 0.9.7 keeps that store's unkeyed, computable offset (see the second Migration Guide link below). The offset is not random per run, and it hides absolute dates only from someone who holds neither the store nor the secret: a store and its exports must not travel together, because the store holds the secret and its audit log records each offset. From 0.9.7 the log file (`isocenter.log`) records neither original Patient IDs nor offsets. To keep offsets consistent across stores (for example, a later batch for the same patients, or re-ingesting an export), write the secret out of one store with `session.store_backend.write_project_secret(path)` and load it into the next with `load_project_secret(path)` before that store's first `audit()` ([Migration Guide](migration.md#carrying-a-project-secret-between-stores)). Within one store, a later batch for a patient already in it, ingested under its original Patient ID, joins that patient's studies when `anonymize()` gives it the same pseudonym, and its dates land on the same offset ([#548](https://github.com/kvnlng/Isocenter/issues/548)). Releases before 0.9.7 derived the offset without a secret, so it could be computed from an exported file ([Migration Guide](migration.md#stores-de-identified-before-097-ghsa-phg9-vcvc-j4r7)).
* **Config**:

    ```yaml
    date_jitter:
      min_days: -10
      max_days: 10
    ```

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
| Binary value (`OB`, `OW`, `OF`, `OD`, `OL`, or `UN`) of 65534 bytes or less | Removed | **Kept**, and written to the exported file |
| Binary value over 65534 bytes | Dropped at ingest, `DATA_LOSS` row | Dropped at ingest, `DATA_LOSS` row |

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

    An overlay's *descriptors* (`OverlayRows`, `OverlayColumns`,
    `OverlayBitPosition` and friends) are `US`, so they always survive,
    and an export from which a large overlay plane was dropped declares a
    plane it does not carry. The descriptors are deliberately left in
    place rather than stripped: an overlay may legitimately live in the
    unused high bits of `PixelData` (addressed by `OverlayBitPosition`),
    and since Isocenter preserves `PixelData` intact, those overlays
    survive and their descriptors are the only pointer to them.

    A dropped *standard* element is listed in the report's Data Loss
    section but does not change the grade
    ([#137](https://github.com/kvnlng/Isocenter/issues/137)).

### PHI Tags

Define specific rules for individual DICOM tags. Keys are `"gggg,eeee"` hex strings (e.g. `"0010,0010"`); case is normalised on load, so either case works.

**Supported Actions:**

| Action | Logic | Example Config |
| :--- | :--- | :--- |
| **`REPLACE`** | Replaces the value with `ANONYMIZED`. A `value:` key is accepted and not used ([#538](https://github.com/kvnlng/Isocenter/issues/538)). | `action: "REPLACE"` |
| **`REMOVE`** | Completely deletes the tag from the dataset. | `action: "REMOVE"` |
| **`EMPTY`** | Sets the tag value to an empty string. | `action: "EMPTY"` |
| **`SHIFT`** | Applies the per-patient Date Jitter offset (Dates only). | `action: "SHIFT"` |
| **`JITTER`** | Same as `SHIFT`. The generated scaffold and the floor policy use it for Study Date. | `action: "JITTER"` |
| **`KEEP`** | Explicitly retains the original value (Exception to profile). | `action: "KEEP"` |

Any other action makes `load_config()` raise `ValueError` naming the tag. The three patient- and study-owned tags above are not governed by these actions.

**Example:**

```yaml
phi_tags:
  "0008,1030": { "action": "EMPTY", "name": "StudyDescription" }
  "0010,0030": { "action": "SHIFT", "name": "PatientBirthDate" }
```

### Pixel Redaction (Machines)

Automatically scrubs burned-in text (pixels) for specific devices. Isocenter identifies the machine using the `DeviceSerialNumber` (0018,1000) tag.

```yaml
machines:
  - serial_number: "SN-9999"
    model_name: "Documentation Only"
    redaction_zones:
      - [0, 100, 0, 500]
```

* **`serial_number`** (Required): Exact match for `0018,1000`.
* **`redaction_zones`**: List of regions to zero out.
  * Format: `[y1, y2, x1, x2]` (Row Start, Row End, Col Start, Col End).
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

These methods change the configuration in memory and, if it came from `load_config(path)`, **write it back to `path` immediately**. The rewritten file holds the whole policy in force, with the profile and floor tags expanded inline and comments dropped, so keep your hand-edited original under version control. A session that loaded no file keeps the changes in memory only. `auto_remediate_config()` is the exception: it edits the rules in memory and does not save.

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

#### set_phi_tag()

`set_phi_tag(tag, action, replacement=None)`

Update the policy for a specific DICOM tag. `REPLACE` always writes `ANONYMIZED`; the `replacement` argument is stored in the policy and not currently applied ([#538](https://github.com/kvnlng/Isocenter/issues/538)).

```python
# Force removal of PatientWeight
session.configuration.set_phi_tag("0010,1030", "REMOVE")

# Blank StudyDescription
session.configuration.set_phi_tag("0008,1030", "EMPTY")
```

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