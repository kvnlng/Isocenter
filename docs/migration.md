# Migration Tools

## Clinical Trial Processor (CTP)

Isocenter includes a utility to convert legacy CTP `DicomPixelAnonymizer.script` files into Isocenter's YAML configuration format.

```bash
# Convert CTP script to Isocenter YAML
python -m isocenter.utils.ctp_parser /path/to/anonymizer.script output_rules.yaml
```

This parser extracts:

- Manufacturer/Model matching criteria.
- Redaction zones (automatically converting `x,y,w,h` to `r1,r2,c1,c2`).
