# Import CTP rules

If your site already redacts burned-in text with the RSNA Clinical Trial
Processor (CTP), you have a `DicomPixelAnonymizer.script`. That script holds
the pixel regions you trust for each machine. Isocenter can convert the script
into rules. You then copy the rules into a configuration's `machines:` list.

## Convert the script

```bash
python -m isocenter.utils.ctp_parser /path/to/DicomPixelAnonymizer.script ctp_rules.yaml
```

It prints `Successfully converted N rules to ctp_rules.yaml`. For a script
block such as

```text
GE LOGIQ ultrasound banner
{ Manufacturer.containsIgnoreCase("GE") * ManufacturerModelName.containsIgnoreCase("LOGIQ E9") }
(0,0,640,40)
```

the output is

```text
rules:
- manufacturer: GE
  model_name: LOGIQ E9
  comment: 'Imported from CTP. Condition: Manufacturer.containsIgnoreCase("GE") *
    ManufacturerModelName.containsIgnoreCase("LOGIQ E9")'
  redaction_zones:
  - - 0
    - 40
    - 0
    - 640
```

What the converter reads:

- **One rule per block** that meets two conditions. The block's condition
  names a `Manufacturer` or a `ManufacturerModelName` with
  `containsIgnoreCase`. At least one `(x,y,w,h)` region follows the
  condition. The converter skips any other block, so the count it prints can be lower than
  the number of blocks in the script. The converter does not read other
  conditions in a block (`Rows.equals(...)`, `CommentsOnRadiationDose...`).
  They survive only in the `comment`.
- **Coordinates are converted.** CTP writes `(x, y, width, height)`. Isocenter
  zones are `[row_start, row_end, col_start, col_end]` in pixels. So
  `(x,y,w,h)` becomes `[y, y+h, x, x+w]`.
- **No serial number.** CTP matches a manufacturer and model. The imported
  rule carries no selector. A block with a manufacturer but no model gets
  `model_name: Unknown`. Add a `serial_number`. Or replace the labels with a
  `match:` that names the manufacturer and model exactly as the files spell
  them. CTP's `containsIgnoreCase` is not exact.

## Use the rules in a configuration

The output is a rule list, not a configuration. `load_config()` refuses it
with `ValueError: ...: unknown key 'rules' at the top level`. Copy each rule
you want into the `machines:` list of your configuration. Then add the
`serial_number` of the machine that the rule is for:

```yaml
machines:
  - serial_number: "US12345"
    manufacturer: GE
    model_name: LOGIQ E9
    comment: 'Imported from CTP.'
    redaction_zones:
    - - 0
      - 40
      - 0
      - 640
```

Quote the serial number. Give one entry per machine. Two scanners of the same
model are two entries with the same zones. Before you rely on the zones, check
them against your own images. See
[Pixel Redaction (Machines)](configuration.md#pixel-redaction-machines) and
[Burned-in text (OCR)](ocr.md).

## The built-in CTP knowledge base

`create_config()` already knows the rules from CTP's published script. For
each machine in your data, it does these steps:

1. It looks for an exact serial number in its own knowledge base.
2. Then it looks for a CTP rule whose manufacturer and model are both
   contained in the machine's, ignoring case.
3. It writes the zones it finds into the new configuration, with a comment
   that names the source.

You need the converter only for a script that your site wrote or changed.
