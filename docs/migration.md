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

### Resurrected private tags (#158, #172)

Before #158, private (odd-group) tags written to the store's
`instance_attributes` tier were never read back, and the writer did not
mirror deletions: a session that ran `remove_private_tags: true`,
anonymized, and saved deleted the vendor block from the graph but left
every row of it in the store. Those rows were inert -- nothing read
them -- until #158 wired the tier into hydration, which is the fix that
makes `remove_private_tags: false` survive a reload. The first open of
a pre-#158 store after upgrading therefore puts the stripped rows back
on the graph, and an export taken from that session carries them.

The library cannot decide this for you: a stale row and a legitimate
one are byte-identical, and nothing in the database records which
private tags were deleted from the graph. What the store does record is
what every pre-#158 session actually saw -- the core `attributes_json`,
which was the whole graph before the tier was readable. So the repair
is explicit and opt-in:

```python
with Session("store.db") as s:
    dropped = s.reconcile_private_tags()
```

`reconcile_private_tags()` drops every `instance_attributes` row whose
tag is absent from the instance's core stored attributes, removes the
same tags from the live graph (undoing the resurrection this session's
open performed), and writes one `RECONCILE_PRIVATE` audit row per
affected instance so the repair is in the compliance trail. It returns
the number of rows dropped.

**Call it only if you know your store was de-identified before the
upgrade.** For that store the core attributes are the complete answer,
and everything the call drops is a row a pre-upgrade export never
carried. For a store that legitimately keeps its vendor block
(`remove_private_tags: false`), the tier *is* the private data and this
call deletes it -- the same grain as `redact(force=True)`: the repair
exists in the API, nothing changes silently, and the caller is choosing
its cost. A site that does not know its history should re-run the
privacy pipeline over the store instead, which re-strips the graph and
(since #158) mirrors the deletions into the tier on save.

## Clinical Trial Processor (CTP)

Isocenter includes a utility to convert legacy CTP `DicomPixelAnonymizer.script` files into Isocenter's YAML configuration format.

```bash
# Convert CTP script to Isocenter YAML
python -m isocenter.utils.ctp_parser /path/to/anonymizer.script output_rules.yaml
```

This parser extracts:

- Manufacturer/Model matching criteria.
- Redaction zones (automatically converting `x,y,w,h` to `r1,r2,c1,c2`).
