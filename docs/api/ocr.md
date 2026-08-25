# Intelligent OCR API

## Zone Discovery

::: isocenter.discovery.ZoneDiscoverer
    handler: python
    options:
      show_root_heading: true
      show_source: true
      members:
        - group_boxes

::: isocenter.discovery.DiscoveryResult
    handler: python
    options:
        show_root_heading: true
        members:
            - filter
            - to_zones

## Verification

::: isocenter.verification.RedactionVerifier
    handler: python
    options:
      show_root_heading: true

## Automation

::: isocenter.automation.ConfigAutomator
    handler: python
    options:
      show_root_heading: true
      members:
        - suggest_config_updates

## Pixel Analysis

::: isocenter.pixel_analysis
    handler: python
    options:
      show_root_heading: true
      members:
        - analyze_pixels
        - detect_text_regions
