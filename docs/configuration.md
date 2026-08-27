# Configuration Guide

Isocenter uses a **Unified YAML Configuration** (v2.0) to control all aspects of de-identification, including PHI tag rules, date shifting, and pixel redaction.

This file allows you to define a reproducible privacy policy that can be shared across your team or version controlled.

## Quick Reference

| Section | Description |
| :--- | :--- |
| **[privacy_profile](#1-privacy-profile)** | Base set of rules (e.g., "basic", "comprehensive"). |
| **[date_jitter](#2-date-jitter)** | Randomly shifts dates to preserve intervals while hiding exact dates. |
| **[remove_private_tags](#3-private-tags)** | Removes vendor-specific private tags (odd groups). |
| **[phi_tags](#4-phi-tags)** | overrides or adds specific tag rules (e.g., `PatientName`). |
| **[machines](#5-pixel-redaction-machines)** | Defines burn-in redaction zones for specific equipment. |

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

!!! warning "`false` cannot retain private tags with a binary VR"

    Elements with a binary VR (`OB`, `OW`, `OF`, `OD`, `OL`) are skipped
    at ingest and never enter the object graph, so there is nothing left
    for this flag to keep by the time it is read. A vendor block
    routinely carries one. The rule exists to keep pixel and waveform
    blobs out of resident memory -- an arbitrary private `OB` can be
    megabytes -- and private tags are collateral.

    Each one is logged and written to the audit log as a `DATA_LOSS`
    entry naming the tag and its VR, so the loss is visible rather than
    silent. Whether these bytes should be retained at all is still open
    ([#125](https://github.com/kvnlng/Isocenter/issues/125)).

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