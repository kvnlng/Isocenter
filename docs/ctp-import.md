# Import CTP rules

If your site already redacts burned-in text with the RSNA Clinical Trial
Processor (CTP), its `DicomPixelAnonymizer.script` holds the pixel regions
you trust for each machine. Isocenter can convert that script into rules you
then copy into a configuration's `machines:` list.

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

- **One rule per block** whose condition names a `Manufacturer` or a
  `ManufacturerModelName` with `containsIgnoreCase`, and which is followed by
  at least one `(x,y,w,h)` region. Any other block is skipped, so the count
  it prints can be lower than the number of blocks in the script. Other
  conditions in a block (`Rows.equals(...)`, `CommentsOnRadiationDose...`)
  are not read; they survive only in the `comment`.
- **Coordinates are converted.** CTP writes `(x, y, width, height)`;
  Isocenter zones are `[row_start, row_end, col_start, col_end]` in pixels,
  so `(x,y,w,h)` becomes `[y, y+h, x, x+w]`.
- **No serial number.** CTP matches a manufacturer and model; Isocenter
  matches a machine by its Device Serial Number. A block with a manufacturer
  but no model gets `model_name: Unknown`.

## Use the rules in a configuration

The output is a rule list, not a configuration: `load_config()` refuses it
with `ValueError: ...: unknown key 'rules' at the top level`. Copy each rule
you want into the `machines:` list of your configuration and add the
`serial_number` of the machine it is for:

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

Quote the serial number. Give one entry per machine: two scanners of the
same model are two entries with the same zones. Then check the zones against
your own images before you rely on them: see
[Pixel Redaction (Machines)](configuration.md#pixel-redaction-machines) and
[Burned-in text (OCR)](ocr.md).

## The built-in CTP knowledge base

`create_config()` already knows the rules from CTP's published script.
For each machine in your data it looks for an exact serial number in its
own knowledge base first, then for a CTP rule whose manufacturer and model
are both contained in the machine's (ignoring case), and writes the zones it
finds into the new configuration with a comment naming the source. You need
the converter only for a script your site has written or changed.
