# OCR API

What `discover_redaction_zones()` and `scan_pixel_content()` build on.
Everything on this page is *documented but internal* (see
[API stability](stability.md)), except `DiscoveryResult.filter()`,
`to_zones()` and `to_dataframe()`, which are frozen at 1.0.

## Zone discovery

`session.discover_redaction_zones()` returns a `DiscoveryResult`, which
holds one `DiscoveryCandidate` per text region read at or above `min_confidence`.

```{eval-rst}
.. autoclass:: isocenter.discovery.DiscoveryResult

   .. automethod:: filter
   .. automethod:: to_zones
   .. automethod:: to_dataframe
```

```{eval-rst}
.. autoclass:: isocenter.discovery.DiscoveryCandidate
   :members:
```

```{eval-rst}
.. autoclass:: isocenter.discovery.ZoneDiscoverer
   :members: group_boxes
```

## Verification

```{eval-rst}
.. autoclass:: isocenter.verification.RedactionVerifier
   :members:
```

## Automation

```{eval-rst}
.. autoclass:: isocenter.automation.ConfigAutomator
   :members: suggest_config_updates
```

## Pixel analysis

```{eval-rst}
.. automodule:: isocenter.pixel_analysis

   .. autoclass:: TextRegion
   .. autofunction:: analyze_pixels
   .. autofunction:: detect_text_regions
```
