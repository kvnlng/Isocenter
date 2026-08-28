# Configuration Guide

Isocenter uses a **Unified YAML Configuration** (v2.0) to control all aspects of de-identification, including PHI tag rules, date shifting, and pixel redaction.

This file allows you to define a reproducible privacy policy that can be shared across your team or version controlled.

## Quick Reference

| Section | Description |
| :--- | :--- |
| **[privacy_profile](#privacy-profile)** | Base set of rules (e.g., "basic", "comprehensive"). |
| **[date_jitter](#date-jitter)** | Randomly shifts dates to preserve intervals while hiding exact dates. |
| **[remove_private_tags](#private-tags)** | Removes vendor-specific private tags (odd groups). |
| **[phi_tags](#phi-tags)** | Overrides or adds specific tag rules (e.g., `PatientName`). |
| **[machines](#pixel-redaction-machines)** | Defines burn-in redaction zones for specific equipment. |

---

## Complete Example

Save this as `isocenter_config.yaml`:

```yaml
# 1. Privacy Profile (Base Rules)
# Options: "basic", "comprehensive", or path to external YAML
privacy_profile: "basic"

# 2. Date Jitter
# Shifts all dates by a random amount within this range.
# The shift is deterministic per-patient (consistent across studies).
date_jitter:
  min_days: -30
  max_days: -10

# 3. Private Tags
# Remove all odd-group tags (vendor specific) unless whitelisted?
remove_private_tags: true

# 4. Custom PHI Tags (Overrides Profile)
phi_tags:
  "0010,0010": 
    action: "REMOVE"
    name: "PatientName"
    
  "0010,0020": 
    action: "REPLACE"
    name: "PatientID"
    value: "ANONYMIZED" # Matches default if omitted
    
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

Sets the baseline behavior for thousands of DICOM tags.

```yaml
privacy_profile: "comprehensive"
```

* **`basic`**: Implements the *DICOM PS3.15 Annex E Basic Profile*. Retains some descriptors but removes direct identifiers.
* **`comprehensive`**: Aggressive de-identification. Removes almost all non-structural text fields.
* **External File**: You can provide a path to another YAML file (e.g., `./profiles/my_hospital_standard.yaml`) to inherit its rules.

### Date Jitter

Shifts all date attributes (`DA`, `DT`) by a random number of days.

* **Logic**: Isocenter generates a secret random offset for each `PatientID`. This offset is consistent for that patient across all their studies and series, preserving temporal relationships (intervals) while hiding the absolute dates.
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

The flag governs the private tags Isocenter *holds*, which is not every
private tag in your source files. Where the line falls:

| Private tag, by the VR it is read with | `remove_private_tags: true` | `remove_private_tags: false` |
| :--- | :--- | :--- |
| Text, numeric, and `UN` -- `LO`, `SH`, `DS`, `UN` | Removed | **Kept**, and written to the exported file |
| Binary VRs -- `OB`, `OW`, `OF`, `OD`, `OL` | Gone | **Gone** -- dropped at ingest, before the flag is read |

**Both rows assume an explicit-VR source.** Under implicit VR there is no VR
in the file, so pydicom reads *every* private tag as `UN` -- the first row
becomes the whole table, the second applies to nothing, and a vendor `OB` is
kept and exported with no `DATA_LOSS` entry. The first row is therefore not
the small-strings case it looks like; under implicit VR it is all of your
private data, blobs included. See below
([#151](https://github.com/kvnlng/Isocenter/issues/151)).

!!! warning "`false` cannot retain private tags with a binary VR"

    Elements with a binary VR (`OB`, `OW`, `OF`, `OD`, `OL`) are skipped
    by `populate_attrs` at ingest and never enter the object graph, so
    there is nothing left for this flag to keep by the time it is read. A
    vendor block routinely carries one. Setting `false` does not fail: it
    succeeds on a tag that has been gone since ingest, and the exported
    file simply does not have it.

    **Only that VR family is affected.** Private tags with a text or
    numeric VR are ingested normally and are governed by this flag in
    both directions -- swept when it is `true`, kept and written to the
    exported file when it is `false`. That covers the `LO` private
    creator and the `SH`/`LO` strings a vendor block is mostly made of.
    "Private tags are not retained" is the wrong reading; one VR family
    of them is not.

    `UN` -- Unknown -- is raw bytes and neither text nor numeric, and it
    is deliberately kept out of the binary set anyway, on the assumption
    that a private `UN` is a small value rather than a blob. The next
    paragraph is where that assumption stops holding
    ([#151](https://github.com/kvnlng/Isocenter/issues/151)).

    **The VR that decides is the one pydicom reads, not the one the
    vendor wrote**, and for a private tag those differ by transfer
    syntax. An implicit-VR file carries no VR field at all: pydicom
    resolves it from the standard dictionary, which has no entry for a
    private tag, and hands back `UN` -- which is not in the binary set.
    So the same vendor `OB` element that is dropped out of an
    explicit-VR source is ingested, retained, exported, and files no
    `DATA_LOSS` entry when it arrives in an implicit-VR one. Do not plan
    around that: one study written in the two syntaxes gives two
    different answers, and only the explicit-VR one is the answer this
    section describes.

    That tension is real and is tracked: under implicit VR the blob *is*
    resident, which is exactly what the rule below exists to prevent
    ([#151](https://github.com/kvnlng/Isocenter/issues/151)).

    **The loss is announced, not silent.** Each dropped element is logged
    as a warning and written to the audit log as a `DATA_LOSS` entry
    naming the tag *and its VR* -- the VR is the part that says whether
    you lost a four-byte serial number or a megabyte of vendor
    telemetry. It reaches you in three places: the session log, section
    3 (*Data Loss*) of the compliance report written by
    `session.generate_report(path)`, and
    `session.store_backend.get_audit_losses()` if you want the rows
    directly. Read that section before concluding a vendor block came
    through a run intact.

    **This is settled rather than pending**
    ([#125](https://github.com/kvnlng/Isocenter/issues/125)). Warning
    plus an audit entry is the answer; storing the bytes is not planned.
    If you need them, keep your source files -- Isocenter never modifies
    them, so the vendor block is still there to go back to.

**Why the bytes are not stored.** Both ways of keeping them were considered
and rejected. Holding vendor binary in `attributes` makes an arbitrary blob
permanently resident, which is what the binary-VR rule exists to prevent:
memory scaling on 100GB+ datasets depends on heavy arrays never being
resident by default, and that guarantee is worth more than an unread vendor
block. The implicit-VR case above is a hole in that rule rather than an
argument against it -- the rule is worth having *and* it does not currently
cover every input, which is why
[#151](https://github.com/kvnlng/Isocenter/issues/151) is open rather than
closed as intended behaviour.

Routing them to the sidecar instead means giving private tags an
offset/length representation the EAV table does not have, plus a lazy
loader and an export re-merge path. `session.compact()` rewrites the
sidecar and rewires every offset it knows about, so a class of offset it
does not know about is silent corruption after the first compaction. That
is design work, not a flag.

!!! warning "Standard binary elements are dropped too"

    The same rule takes Overlay Data `(60xx,3000)` and the palette color
    LUTs `(0028,120x)`, which are `OW`. `PixelData` and `WaveformData`
    are the only binary elements routed to the sidecar; everything else
    with a binary VR is dropped whatever its group.

    An overlay's *descriptors* (`OverlayRows`, `OverlayColumns`,
    `OverlayBitPosition` and friends) are `US`, so they do survive. An
    exported file therefore declares an overlay plane it does not carry.
    The descriptors are deliberately left in place rather than stripped:
    an overlay may legitimately live in the unused high bits of
    `PixelData` (addressed by `OverlayBitPosition`), and since Isocenter
    preserves `PixelData` intact, those overlays survive and their
    descriptors are the only pointer to them. Stripping the descriptors
    would turn a correct passthrough into silent destruction.

    These drops are reported as `DATA_LOSS` entries the same way
    ([#137](https://github.com/kvnlng/Isocenter/issues/137)).

### PHI Tags

Define specific rules for individual DICOM tags. Keys are `"gggg,eeee"` hex strings (e.g. `"0010,0010"`); case is normalised on load, so either case works.

**Supported Actions:**

| Action | Logic | Example Config |
| :--- | :--- | :--- |
| **`REPLACE`** | Replaces value with "ANONYMIZED" (or custom string). | `action: "REPLACE", value: "Project-X"` |
| **`REMOVE`** | Completely deletes the tag from the dataset. | `action: "REMOVE"` |
| **`EMPTY`** | Sets the tag value to an empty string. | `action: "EMPTY"` |
| **`SHIFT`** | Applies the per-patient Date Jitter offset (Dates only). | `action: "SHIFT"` |
| **`KEEP`** | Explicitly retains the original value (Exception to profile). | `action: "KEEP"` |

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

session = isocenter.Session(data_directory="./dicom_data")

# improved: Access the IsocenterConfiguration object directly
config = session.configuration

print(config.rules)    # List active redaction rules
print(config.phi_tags) # List active PHI tag overrides
```

### Methods

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

Update the policy for a specific DICOM tag.

```python
# Force removal of PatientWeight
session.configuration.set_phi_tag("0010,1030", "REMOVE")

# Replace StudyDescription with a constant
session.configuration.set_phi_tag("0008,1030", "REPLACE", replacement="RESEARCH STUDY")
```

## Auto-Discovery of Redaction Zones

To help identify pixel redaction zones (e.g., for burned-in PHI), Isocenter provides a discovery tool that analyzes a sample of images from a specific machine to find common text "hotspots".

```python
# Discover potential redaction zones for a machine
suggested_zones = session.discover_redaction_zones(
    serial_number="US-12345", 
    sample_size=50
)

print(f"Discovered {len(suggested_zones)} zones: {suggested_zones}")

# Apply these zones to your configuration
if suggested_zones:
    session.configuration.add_rule(
        serial_number="US-12345",
        zones=suggested_zones
    )
```