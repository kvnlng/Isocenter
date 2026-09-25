# OCR API

What `discover_redaction_zones()` and `scan_pixel_content()` build on.
Everything on this page is *documented but internal* (see
[API stability](stability.md)), except `DiscoveryResult.filter()`,
`to_zones()` and `to_dataframe()`, which are frozen at 1.0.

## Zone discovery

`session.discover_redaction_zones()` returns a `DiscoveryResult`, which
holds one `DiscoveryCandidate` per text region read at or above `min_confidence`.

::: isocenter.discovery.DiscoveryResult
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false
      members:
        - filter
        - to_zones
        - to_dataframe

::: isocenter.discovery.DiscoveryCandidate
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false

::: isocenter.discovery.ZoneDiscoverer
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false
      members:
        - group_boxes

## Verification

::: isocenter.verification.RedactionVerifier
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false

## Automation

::: isocenter.automation.ConfigAutomator
    handler: python
    options:
      show_root_heading: true
      show_root_full_path: false
      members:
        - suggest_config_updates

## Pixel analysis

::: isocenter.pixel_analysis
    handler: python
    options:
      show_root_heading: true
      members:
        - analyze_pixels
        - detect_text_regions
        - TextRegion
