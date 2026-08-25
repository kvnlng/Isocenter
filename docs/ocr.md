# Intelligent Pixel Analysis (OCR)

Isocenter includes a powerful Optical Character Recognition (OCR) engine designed to detect and verify burned-in text within DICOM pixel data. This feature moves beyond simple text detection, offering **Intelligent Redaction Verification** to validate your anonymization rules.

## Overview

The OCR module uses **Tesseract** to scan pixel data for text. It allows you to:

1. **Audit** your dataset for missed PHI (burned-in names, dates, etc.).
2. **Verify** that your configured redaction zones actully cover the text present in the image.
3. **Auto-Remediate** your configuration by suggesting new or expanded zones based on findings.

## Prerequisites

To use OCR features, you must have the Tesseract binary installed on your system:

=== "macOS"
    ```bash
    brew install tesseract
    ```

=== "Linux (Ubuntu/Debian)"
    ```bash
    sudo apt-get install tesseract-ocr
    ```

## Intelligent Verification

The core workflow is **Verification**. Instead of just finding all text (which includes safe text like anatomical labels), Isocenter filters findings based on your **Redaction Rules**.

### How it Works

1. **Match**: Isocenter identifies the Redaction Rule for each instance (matched by Serial Number).
2. **Scan**: It detects all text regions in the image.
3. **Filter**: It checks if each text region is covered by a configured `redaction_zone`.
    * **Safe**: Text > 80% covered by a zone. (Ignored)
    * **Partial Leak**: Text partially covered (0-80%). (Flagged)
    * **New Leak**: Text completely uncovered. (Flagged)

### Running a Scan

To avoid scanning the entire cohort, Isocenter scans **only** machines that are present in your configuration (`priv_config.yaml`). Unconfigured machines are skipped.

You can also focus the scan on a single machine:

```python
from isocenter.session import DicomSession

session = DicomSession("my_project.db")
session.ingest("dicom_data/")

# Run the intelligent scan (only configured machines)
report = session.scan_pixel_content()

# OR: Focus on a specific serial number
report = session.scan_pixel_content(serial_number="SN-12345")

print(f"Found {len(report)} leaks.")
for finding in report:
    print(f"{finding.metadata['leak_type']}: {finding.value} in {finding.entity_uid}")
```

## Setting Up New Machines (Zone Discovery)

When you add a new machine to your configuration (Scaffolding), it typically has no redaction zones defined. Isocenter uses **Zone Discovery** to analyze a sample of images and find repeating "hotspots" of burned-in text.

### Smart Discovery

Isocenter 0.6+ introduces smart text classification to help you identify **Proper Nouns** (like Patient Names) versus static text (like "Hospital" or "Slice ID").

```python
# 1. Run Discovery
# Returns a DiscoveryResult object containing all detected text
result = session.discover_redaction_zones(
    serial_number="SN-NEW", 
    sample_size=50, 
    min_confidence=60.0
)

# 2. Convert to Zones (Grouping)
# You can adjust padding to merge text on the same line
zones = result.to_zones(pad_x=100, pad_y=10)

# 3. Inspect Results (Rich Metadata)
for z in zones:
    print(f"Type: {z['type']}")         # PROPER_NOUN, LIKELY_NAME, or TEXT
    print(f"Zone: {z['zone']}")         # [y1, y2, x1, x2]
    print(f"Examples: {z['examples']}") # ['Smith^John', 'Hospital A', ...]
    print("-" * 20)
```

### Advanced Usage: Power Users & Data Scientists

Isocenter's `DiscoveryResult` provides powerful tools for deep inspection, ideal for data science workflows.

#### 1. Data Science Integration (Pandas/Matplotlib)

Export results directly to a Pandas DataFrame or a density matrix for custom analysis.

```python
import pandas as pd
import matplotlib.pyplot as plt

# Export to Pandas
df = result.to_dataframe()
high_conf = df[df['confidence'] > 90.0]
print(high_conf['text'].value_counts().head())

# Export Density Matrix (for Heatmaps)
matrix = result.get_density_matrix(bins=(100, 100))
plt.imshow(matrix, cmap='hot', interpolation='nearest')
plt.title("Burned-in Text Heatmap")
plt.show()
```

#### 2. Lambda Filtering

Apply arbitrary logic to filter candidates using Python lambdas.

```python
# Keep candidates that look like dates OR appear in the first 10 frames
filtered = result.filter(lambda c: 
    re.match(r"\d{4}", c.text) or c.source_index < 10
)
zones = filtered.to_zones()
```

#### 3. Temporal Stability Analysis

Identify whether a zone is static (overlay) or transient (noise/motion).

```python
report = result.analyze_temporal_stability()
for item in report:
    print(f"Zone: {item['zone']} | Status: {item['status']} ({item['occurrence']*100:.1f}%)")
# Output:
# Zone: [10, 10, 100, 20] | Status: STATIC_ALWAYS (100.0%)
```

#### 4. Quick Visualization

For a quick terminal-based check without external libraries:

```python
print(result.visualize_heatmap(bins=(20, 20)))
```

### Entity Detection Modes

Discovery uses a tiered approach to classify text:

1. **Regex Heuristics (Default)**: Extremely fast. Detects DICOM name patterns (e.g., `Smith^John`) and capitalized phrases.
2. **NLP (Optional)**: If you install the optional NLP extras, Isocenter uses **spaCy** for high-precision Named Entity Recognition (NER). This improves detection of names in natural formats (e.g., "John Smith" without carets).

    ```bash
    pip install isocenter[nlp]
    ```

### Applying Zones

Once identified, add the `zone` coordinates to your `priv_config.yaml`.

```yaml
machines:
  - serial_number: "SN-NEW"
    redaction_zones:
      # Found: PROPER_NOUN ['Smith^John']
      - [20, 50, 200, 30]
```

### Validation

After updating the config, run a scan to confirm coverage:

```python
report = session.scan_pixel_content("SN-NEW")
# Should be 0 findings if zones are correct
```

## Automated Remediation

Isocenter can analyze the "Partial" and "New" leaks to suggest updates to your configuration file.

### Auto-Remediation Workflow

```python
# 1. Scan
report = session.scan_pixel_content()

# 2. Apply Suggestions
# This analyzes the report and updates the in-memory configuration
count = session.auto_remediate_config(report)

if count > 0:
    print(f"Applied {count} fixes.")
    
    # 3. Validation Scan (Optional)
    # Re-run to confirm leaks are gone
    report_v2 = session.scan_pixel_content()
    assert len(report_v2) == 0
    
    # 4. Save Config
    session.configuration.save_config("updated_priv_config.yaml")
```

## Configuration Reference

Your `priv_config.yaml` defines the zones used for verification.

```yaml
machines:
  - serial_number: "SN-12345"
    model_name: "CT-Scanner-X"
    redaction_zones:
      # [x, y, width, height]
      - [0, 0, 200, 100]       # Top-Left Info Box
      - [400, 400, 100, 50]    # Bottom-Right Label
```

## API Reference

::: isocenter.session.DicomSession.scan_pixel_content
    options:
      show_source: true

::: isocenter.session.DicomSession.auto_remediate_config
    options:
      show_source: true
