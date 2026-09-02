# Migration Tools

## Upgrading an existing store

The code in a new release is fixed; the data an old release wrote is
not. Three shapes of legacy damage are known, and each is handled the
way its information allows -- healed where the store itself proves what
is wrong, detected where it cannot be repaired, and left to an explicit
opt-in where only the site knows the answer.

### Corrupted pixel geometry (#186, #214)

Releases before the #186 fix could persist a guessed geometry --
`SamplesPerPixel=3`, `PhotometricInterpretation=RGB`, swapped axes --
for a multi-frame grayscale instance whose Columns was 3 or 4. A store
carrying those descriptors exports garbage while grading `PASS`: every
step downstream behaves correctly on descriptors that are already
wrong.

Opening a session over such a store now runs an arithmetic check --
does Rows x Columns x SamplesPerPixel x NumberOfFrames x
bytes-per-sample equal the stored sidecar frame length? -- and logs a
warning naming each instance it flags. The same result reaches the
compliance report as a `COMPLIANCE_CHECK` exception, so a session
holding detected damage grades `REVIEW_REQUIRED` rather than `PASS`.
The check is exact for frames stored uncompressed; a zlib-stored
frame's length is post-compression, so damage behind one is caught
where the bytes are decoded instead: `export(verify_readback=True)`
(#209) re-reads every written file and fails the mismatch at delivery.

There is deliberately no automatic repair. The sidecar's bytes are
shape-free, so a migration would be a best-effort guess, and a
best-effort repair that silently half-works is worse than a detector.
The remedy is to re-ingest the affected instances from their source
files.

### Hollow waveform multiplex items (#160, #168)

A store that ingested a multi-group waveform between 0.8.2 and the
#160 fix holds one Waveform Sequence item per multiplex group with
samples behind item 0 only, and exported a file declaring Waveform
Data it did not carry. Hydration now heals that shape on load -- see
[Waveforms](waveforms.md) for the full story.

## Clinical Trial Processor (CTP)

Isocenter includes a utility to convert legacy CTP `DicomPixelAnonymizer.script` files into Isocenter's YAML configuration format.

```bash
# Convert CTP script to Isocenter YAML
python -m isocenter.utils.ctp_parser /path/to/anonymizer.script output_rules.yaml
```

This parser extracts:

- Manufacturer/Model matching criteria.
- Redaction zones (automatically converting `x,y,w,h` to `r1,r2,c1,c2`).
