# #167 — PHI inside a private sequence, invisible under Implicit VR

**Status:** design, ready to implement
**Issue:** #167 (milestone v0.9.1 — Reports Success While Wrong)
**Touches:** `io_handlers.py`, `privacy.py`, `remediation.py`, `persistence.py`, `reporting.py`, `session.py`, docs, CHANGELOG

Every behavioural claim below was produced by running a command against
this worktree and reading its output. The scripts live in the session
scratchpad (`.../scratchpad/167/`); each claim names the one that
produced it. Runs used `/Users/kevin/Developer/Isocenter/.venv/bin/python`
(3.14.6, pydicom 3.0.2) with
`PYTHONPATH=<worktree>` and `assert "agent-aafcb938714b39e2c" in
isocenter.__file__` at the top of every script.

---

## 1. The problem, measured

### 1.1 The reported bug

`fixture.py` writes one instance twice — Explicit VR LE and Implicit VR
LE — with private creator `(0009,0010) = ACME_HEADER` and a private `SQ`
at `(0009,1003)` whose single item carries `PatientName = "SECRET^PHI"`
and `PatientID = "MRN-999"`.

```
$ python fixture.py
=== EXPLICIT ===
  VR=SQ type=Sequence
=== IMPLICIT ===
  VR=UN type=bytes
  len=42 first16=feff00e022000000100010000a000000
```

`pipeline.py 0` runs the documented order (`ingest → audit → anonymize →
export → save(sync=True) → generate_report`) with `phi_tags` on
`0010,0010` / `0010,0020` / `0008,0020` and `remove_private_tags=False`:

```
=== EXPLICIT (remove_private_tags=False) ===
  graph sequences keys: ['0009,1003']
  attributes[0009,1003]: NoneType None
  audit findings (8):  ... ('0010,0010','SECRET^PHI',(('0009,1003',0),))
                           ('0010,0020','MRN-999',  (('0009,1003',0),))
  report: | **Validation Status** | **PASS** |
  export contains SECRET^PHI: False
=== IMPLICIT (remove_private_tags=False) ===
  graph sequences keys: []
  attributes[0009,1003]: bytes feff00e022000000
  audit findings (6):  (the two nested findings are absent)
  report: | **Validation Status** | **PASS** |
  export contains SECRET^PHI: True
  export contains MRN-999   : True
```

Eight findings against six: the delta is exactly the two nested ones.
The issue quotes five against three because it configured two tags and
scanned one entity level; the mechanism and the delta are the same. The
report grades `PASS` in both, and the implicit export carries the PHI.

`populate_attrs` (io_handlers.py:210) has no arm for this: `UN` is not in
`BINARY_VRS`, so the bytes fall through to `item.set_attr(tag,
elem.value)` at the bottom of the loop. Nothing creates a `sequences`
entry, so `PhiInspector._scan_instance`'s structural walk
(`iter_item_tree`) has nothing to walk into.

### 1.2 The affected population is narrower than the issue says

`undef_probe.py` hand-builds the four framings (element length
defined/undefined × item length defined/undefined) as raw bytes and asks
pydicom what it hands back:

```
$ python undef_probe.py
seq_undefined=False item_undefined=False -> VR=UN type=bytes len=42 head=feff00e022000000
seq_undefined=False item_undefined=True  -> VR=UN type=bytes len=50 head=feff00e0ffffffff
seq_undefined=True  item_undefined=False -> VR=SQ type=Sequence items=1 first=[(0010,0010), (0010,0020)]
seq_undefined=True  item_undefined=True  -> VR=SQ type=Sequence items=1 first=[(0010,0010), (0010,0020)]
```

**Only a private sequence written with a *defined* element length is
affected.** An undefined-length private `SQ` is already recovered as a
`Sequence` by pydicom, whatever the item framing. Both defined-length
rows must be handled by the fix — the item framing inside is free to be
either.

### 1.3 A second defect the fix would otherwise inherit

The issue certifies the default configuration as safe. Measured on
unmodified main, `pipeline.py 1` (`remove_private_tags=True`) plus
`dump.py` on the exported file:

```
=== EXPLICIT (remove_private_tags=True) ===
  audit findings (9): ... ('0009,0010','<PRIVATE>')   <- creator swept
                          (no '0009,1003' finding)
  exported 0009,1003 VR=UN type=bytes

$ python dump.py run/EXPLICIT/out
  TS: 1.2.840.10008.1.2
  (0009,1003) VR=UN feff00e024000000...414e4f4e594d495a4544...
```

`remove_private_tags=True` **does not remove a private sequence.**
`_scan_instance` builds its `scan_targets` from `item.attributes.keys()`
only (privacy.py:339), so a sequence tag is never a sweep target; and
`RemediationService`'s `REMOVE_TAG` arm only deletes from
`entity.attributes` (remediation.py:196). The exported file keeps an
orphaned private sequence whose creator was stripped.

On the implicit path this is currently masked: the blob is an
*attribute*, so the sweep sees it (`('0009,1003','<PRIVATE>')`) and the
export is clean. **Restoring the structure removes that mask.** Measured
with the prototype parse and *without* the sweep fix
(`roundtrip.py 1`):

```
=== IMPLICIT, prototype parse, remove_private=True ===
  exported (0009,1003) VR=UN feff00e024000000...
```

That is a regression in the default configuration. The privacy sweep and
the `REMOVE_TAG` arm are therefore **in scope for this fix**, not
adjacent to it: the parse cannot ship without them.

---

## 2. Decision

**Parse what provably parses; report what does not.** Both halves, in one
change.

At ingest, a private (odd-group) element whose VR is `UN`, whose value is
`bytes`, and whose first four bytes are the item tag `FE FF 00 E0` is a
sequence candidate. It is re-parsed as an implicit-VR sequence and, if
and only if the parse is *verified*, its items are added to the real
`sequences` graph and the raw bytes are dropped from `attributes`. A
candidate that fails verification keeps its bytes exactly as today and
files a `SCAN_GAP` audit row, which takes the compliance grade to
`REVIEW_REQUIRED`.

> **Corrected in review (PR #249).** "Keeps its bytes exactly as today"
> is true of the object graph and false of the export under the shipped
> default, and the unconditional `REVIEW_REQUIRED` is wrong for the same
> reason. See the block under §3.6.

### 2.1 Verification: byte-exact re-encode

The parse is accepted only when all four hold:

1. the value starts with `FE FF 00 E0`;
2. `pydicom.filereader.read_sequence` returns without raising;
3. it consumed exactly `len(value)` bytes;
4. re-encoding the parsed sequence with
   `pydicom.filewriter.write_sequence` (implicit VR LE) reproduces the
   original bytes **exactly**.

Rule 4 is the load-bearing one. It converts "this probably decodes" into
"these bytes *are* this sequence" — the substitution is provably
lossless, so replacing the attribute with the structure cannot lose
anything. `gate_probe.py` runs the whole gate over the adversarial set:

```
$ python gate_probe.py
real sequence              -> PARSED 1 item(s) [['SECRET^PHI', 'MRN-999']]
truncated item header      -> FALLBACK (read failed: OSError: No tag to read at file position 5)
item then garbage          -> FALLBACK (read failed: OSError: No tag to read at file position 17)
huge length                -> FALLBACK (re-encode differs (16 vs 16 bytes))
undefined item, no delim   -> FALLBACK (re-encode differs (24 vs 16 bytes))
coincidental OB            -> FALLBACK (re-encode differs (8 vs 9 bytes))
empty item                 -> PARSED 1 item(s) [[]]
plain vendor blob          -> FALLBACK (not a sequence)
```

Rules 2 and 3 alone are **not** enough, and this is why rule 4 is not
optional: `parse_probe.py` shows that `read_sequence` on its own accepts
three of those adversarial inputs without raising and without leaving
bytes unread —

```
$ python parse_probe.py
huge length                  -> 1 item(s) consumed=16/16 items=[['(0000,0000)']]
undefined item len no delim  -> 1 item(s) consumed=16/16 items=[['(0000,0000)']]
coincidental OB              -> 1 item(s) consumed=9/9 items=[[]]
```

The `coincidental OB` case is the false-positive the brief asks about: a
9-byte vendor blob that happens to start `FF FE 00 E0` decodes to one
empty item and, without rule 4, its five payload bytes would be silently
destroyed. With rule 4 it falls back untouched.

> **Corrected in review.** Two of the rule attributions above do not
> reproduce on pydicom 3.0.2, and the shipped tests use a different set
> because of it. Re-measured element by element
> (`gate_reprobe.py`, 3.12.13, pydicom 3.0.2):
>
> ```
> real sequence                      ACCEPTED (1 item(s))
> spec 9-byte coincidental blob      rule 2 (OSError: No tag to read at file position 9)
> 16-byte coincidental blob          rule 4 (re-encode 16 vs 16 bytes)
> truncated item header              rule 2 (OSError: No tag to read at file position 6)
> item then garbage                  rule 4 (re-encode 16 vs 16 bytes)
> huge item length                   rule 4 (re-encode 16 vs 16 bytes)
> plain vendor blob                  rule 1 (not item-tag prefixed)
> ```
>
> The 9-byte blob never reaches rule 4 -- `read_sequence` raises on the
> odd trailing byte -- and it cannot be written into a DICOM file
> anyway, since element values are even-length. `item then garbage` is
> refused at rule 4, not at rule 2. Neither correction weakens the
> argument: rule 4 is still the only thing that refuses three of the
> four adversarial values, measured by deleting the
> `out.getvalue() == raw` comparison and running the suite -- five
> tests go red, three of them `test_an_unverifiable_candidate_keeps_its_bytes_exactly`
> parameters. The shipped fixture is a **16-byte** empty-item-plus-payload
> blob, which is writable and does reach rule 4.

`reencode_probe.py` confirms rule 4 does not reject well-formed input —
seven shapes, all byte-identical on re-encode, no warnings:

```
$ python reencode_probe.py
simple                   consumed=42/42 items=1 reencode_equal=True  warns=set()
simple undefined-item    consumed=42/42 items=1 reencode_equal=True  warns=set()
odd-length values        consumed=34/34 items=1 reencode_equal=True  warns=set()
nested sequence          consumed=58/58 items=1 reencode_equal=True  warns=set()
two items                consumed=44/44 items=2 reencode_equal=True  warns=set()
private child elems      consumed=66/66 items=1 reencode_equal=True  warns=set()
numeric values           consumed=42/42 items=1 reencode_equal=True  warns=set()
```

and `gate_undef.py` confirms the defined-element/undefined-item framing
from §1.2 passes the gate too:

```
$ python gate_undef.py
item_undefined=False -> PARSED 1 item(s) ['SECRET^PHI', 'MRN-999']
item_undefined=True  -> PARSED 1 item(s) ['SECRET^PHI', 'MRN-999']
```

The gate is deliberately conservative in one known way: a sequence whose
item elements are **not in ascending tag order** re-encodes to different
bytes (pydicom's writer sorts) and falls back. Discovered by
accident — `binary_child.py`'s first fixture had its elements out of
order and produced `sequences: []`; reordering them produced
`sequences: ['0009,1003']`. Non-conformant input therefore gets a
`SCAN_GAP` row rather than a guess, which is the right side to err on.

### 2.2 Alternatives rejected

**(a) Report only, no parse.** Leaves the PHI in the export under
`remove_private_tags=False` and the finding set short. It answers the
"reports clean" half of #167 and none of the "exported unremediated"
half. Rejected as a partial fix where a whole one is available and
verifiable.

**(b) Parse only, no report.** Narrows the silent population instead of
closing it, which is the failure pattern #137/#169/#194 exist to
prevent: an element the scan could not open, retained and exported, with
nothing anywhere saying so, still grades `PASS`.

**(c) Drop the unverifiable bytes and file a `DATA_LOSS` row.** Cheapest
(no new machinery — the row's sentence, "present in the source and not
in the exported data", would even be true). Rejected because it *creates
a second transfer-syntax divergence*: under Explicit VR the same
defined-length private `SQ` resolves to `SQ` and never reaches the gate,
so the identical file would keep its vendor block explicit and lose it
implicit. #167 is a bug about the two syntaxes disagreeing about one
file; a fix must not add a fresh disagreement. It also breaks the #125
promise on a heuristic match — see the `coincidental OB` case above.

**(d) Reuse `DATA_LOSS` for the unparseable case, keeping the bytes.**
Rejected: section 3 of the compliance report is headed "Elements below
were present in the source and are **not** in the exported data"
(reporting.py:179). These bytes *are* in the exported data — measured,
§6. A row that makes its own section header false is worse than no row.

**(e) Re-parse on load from the store as well as at ingest.** Not
needed: the structure is persisted as `__sequences__` and rebuilt on
load (measured, §5), so this version's stores round-trip. Stores written
by an earlier version keep the bytes; that is §9.

**(f) A pydicom `raw_element_vr` hook.** pydicom 3 exposes one
(`hooks.py:180`), and it would resolve the VR before the value is ever
built. Rejected: the hook is process-global, and #144 is this project's
standing decision not to change pydicom's behaviour for the host
application.

---

## 3. Change list

### 3.1 `isocenter/io_handlers.py`

**(1) Module-scope imports** (beside the existing pydicom imports):

```python
from pydicom.charset import default_encoding
from pydicom.dataelem import DataElement
from pydicom.filebase import DicomBytesIO
from pydicom.filereader import read_sequence
from pydicom.filewriter import write_sequence
```

No `setup.py` change: pydicom is already in `install_requires`.

**(2) A module constant**, next to `_ROUTED_BINARY_TAGS`:

```python
#: The Item tag (FFFE,E000) as it appears on the wire, little endian.
#: A private element that pydicom resolved to `UN` and whose value
#: starts with these four bytes is a sequence whose VR the transfer
#: syntax did not carry (#167). Four bytes is a weak signal on its own
#: -- any vendor blob may begin with them by chance -- so it only
#: selects candidates for `_sequence_from_un_bytes`, which proves or
#: refuses each one.
_ITEM_TAG_LE = b"\xfe\xff\x00\xe0"
```

**(3) A new module-scope function**, placed directly above
`populate_attrs`:

```python
def _sequence_from_un_bytes(raw: bytes, tag, encoding) -> Optional[Sequence]:
    """Re-parse `UN` bytes as an implicit-VR sequence, or return None.

    Under Implicit VR Little Endian a private sequence has no VR on the
    wire and no dictionary entry, so pydicom resolves it to `UN` and
    hands back bytes. The structure is still in those bytes; nothing
    downstream can see it, because the PHI scan walks `sequences` and
    there is no entry there to walk (#167).

    Returns the parsed `Sequence` only when re-encoding it reproduces
    `raw` byte for byte. That is the whole safety argument: the caller
    replaces an attribute with a structure, and the equality proves the
    two are the same bytes, so nothing is lost by the substitution and
    nothing is guessed. Three adversarial inputs get past
    `read_sequence` without raising and without leaving bytes unread --
    a garbage length, an undefined-length item with no delimiter, and a
    nine-byte vendor blob that happens to start with the item tag --
    and all three re-encode to something else. The last one is the
    reason this is not "parse and hope": it decodes to one empty item,
    and accepting it would delete five bytes of vendor data.

    Returns None for every failure, and the caller keeps the bytes. An
    ingest must not raise on a malformed private element: the file is
    still readable, and the value is still exportable.
    """
```

Body, exactly:

```python
    if not raw.startswith(_ITEM_TAG_LE):
        return None

    fp = DicomBytesIO(raw)
    fp.is_little_endian = True
    fp.is_implicit_VR = True
    try:
        parsed = read_sequence(fp, True, True, len(raw), encoding)
    except Exception:      # pylint: disable=broad-except
        return None
    if fp.tell() != len(raw):
        return None

    out = DicomBytesIO()
    out.is_little_endian = True
    out.is_implicit_VR = True
    try:
        write_sequence(out, DataElement(tag, "SQ", parsed), encoding)
    except Exception:      # pylint: disable=broad-except
        return None
    return parsed if out.getvalue() == raw else None
```

Constraints the coder must not "clean up":

- The two `is_little_endian` / `is_implicit_VR` assignments are
  **required**, not decoration: without them `read_sequence` raises
  `AttributeError: 'DicomBytesIO' object has no attribute '_tag_packer'`
  (measured, `warn_probe.py`). They are the public setters and emit no
  deprecation warning under pydicom 3.0.2 (same run).
- The re-encode must happen **before** anything iterates the parsed
  datasets. `read_sequence` returns raw elements and `write_sequence`
  writes their bytes back; converting first would compare a re-encoding
  of converted values.
- `except Exception` is deliberate and broad. A malformed private
  element must not fail an ingest.

**(4) `populate_attrs` gains a fifth parameter** and one branch.
Signature becomes:

```python
def populate_attrs(ds, item, dropped=None, is_root=True, unscanned=None):
```

Docstring addition for `unscanned`: *collects `(tag, byte_length)` for
every private `UN` value that begins with the item tag and did not
verify as a sequence, so the caller can report a value the PHI scan
could not open (#167). Distinct from `dropped`: nothing was lost — the
bytes stay in `attributes` and are exported.*

The new branch goes immediately after `tag = f"{elem.tag.group:04x},…"`
and before `if elem.VR == 'SQ':`:

```python
        # A private sequence under Implicit VR arrives here as `UN`
        # bytes, because the transfer syntax carries no VR and the
        # standard dictionary has no entry (#167). Restore the
        # structure so the PHI scan can walk it -- and only when the
        # parse is proven byte-exact; see `_sequence_from_un_bytes`.
        #
        # The tag then lives in `sequences` and *not* in `attributes`,
        # which is what the Explicit VR ingest of the same file
        # produces. Keeping both would put the same tag through
        # `_merge` and `_merge_sequences` (io_handlers.py:920-921),
        # whose order would decide whether the export carried the
        # remediated sequence or the original bytes.
        if (elem.VR == 'UN' and elem.tag.group % 2 == 1
                and isinstance(elem.value, (bytes, bytearray, memoryview))):
            raw = bytes(elem.value)
            if raw.startswith(_ITEM_TAG_LE):
                parsed = _sequence_from_un_bytes(raw, elem.tag, encoding)
                if parsed is not None:
                    process_sequence(tag, parsed, item, dropped, unscanned)
                    continue
                if unscanned is not None:
                    unscanned.append((tag, len(raw)))
                # Falls through: the bytes stay in `attributes` and are
                # exported exactly as before.
```

`encoding` is read once above the element loop, beside `has_pixel_data`:

```python
    # The enclosing dataset's character set, so text inside a re-parsed
    # sequence decodes the way it would have if pydicom had parsed the
    # sequence itself. The gate compares raw bytes, so this cannot
    # change whether a value parses -- only how its text reads.
    encoding = getattr(ds, "_character_set", default_encoding)
```

Keep the `getattr` default. `populate_attrs` is called with bare
sequence items by the waveform and Murmur tests
(`tests/test_murmur_annotations.py:28` passes
`ds.WaveformSequence[0]`) — those are `Dataset`s and do carry
`_character_set`, but the default is what makes the line true for any
`ds` this function is ever handed, and `ds._character_set` would not be.
Both shapes the attribute returns are valid `encoding` arguments: a bare
`Dataset` gives `'iso8859'` and one with a Specific Character Set gives
`['latin_1']` (measured). No normalization is needed; do not add any.

Odd group only. A standard tag resolves its VR from the dictionary with
no dataset present, so an even-group element does not reach `UN` by this
route — the issue measured `dictionary_VR(0x52009230) == 'SQ'`, and
`fixture.py`'s standard elements come back typed under both syntaxes.
Even-group `UN` is §9.

**(5) `process_sequence` gains the same parameter** and forwards it:

```python
def process_sequence(tag, elem, parent_item, dropped=None, unscanned=None):
    ...
        populate_attrs(ds_item, seq_item, dropped, is_root=False,
                       unscanned=unscanned)
```

`dropped` must keep being passed for a re-parsed sequence: its items go
through the ordinary rules, so a binary-VR child is reported like any
other (measured, §7 T11).

**(6) `ingest_worker`** (io_handlers.py:400) collects and ships it:

```python
        dropped = []
        unscanned = []
        populate_attrs(ds, inst, dropped, unscanned=unscanned)
        meta['dropped_private_binary'] = dropped
        # Rides `meta` for the same reason as `dropped_private_binary`
        # above: this worker may be in a subprocess with no store
        # handle, and the return arity is unpacked at every call site.
        meta['unscanned_private_sequences'] = unscanned
```

**(7) `import_files`** emits the row, directly after the
`dropped_private_binary` loop (io_handlers.py:623-643):

```python
                    # Retained, not lost -- and that is exactly why this
                    # is not a DATA_LOSS row. The bytes are in the
                    # exported file; what is missing is any assurance
                    # about what is inside them. Section 3 of the
                    # compliance report is headed "present in the source
                    # and not in the exported data", so filing this
                    # there would make that header false (#167).
                    #
                    # No `loss_scope`: the column grades losses, and
                    # this is not one. `generate_report` grades the
                    # presence of the row itself.
                    for tag, nbytes in meta.get(
                            'unscanned_private_sequences', ()):
                        detail = (f"Private tag {tag} holds {nbytes} bytes "
                                  f"that begin with the item tag "
                                  f"(FFFE,E000) but do not parse as an "
                                  f"implicit-VR sequence. It was retained "
                                  f"verbatim and exported; the PHI scan "
                                  f"could not open it.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="SCAN_GAP",
                                entity_uid=inst.sop_instance_uid,
                                details=detail)
```

**(8) No change to `_export_instance_worker`'s `populate_attrs(ds, inst)`
call** (io_handlers.py:968). It passes neither list, so nothing is
double-reported; and by then `_merge_sequences` has written the tag with
VR `SQ`, so the new branch cannot fire on it.

### 3.2 `isocenter/privacy.py` — the sweep must see sequences

In `_scan_instance`, add `seq_removals = []` beside `findings = []`, and
inside the existing `if self.remove_private_tags:` block, after the
`scan_targets` loop:

```python
            # A private *sequence* is a private tag. `scan_targets` is
            # built from `attributes` alone, so before #167 a private SQ
            # was swept only when it arrived as an opaque `UN` blob --
            # i.e. only under Implicit VR, and only because the scan
            # could not see it was a sequence. Restoring the structure
            # (#167) removes that accident, so the sweep has to ask the
            # question directly, at every depth, or the default
            # configuration starts exporting private sequences.
            for owner, path in iter_item_tree(instance):
                for seq_tag in list(owner.sequences.keys()):
                    try:
                        if int(seq_tag.split(',')[0], 16) % 2 == 0:
                            continue
                    except ValueError:
                        continue
                    if seq_tag in WHITELIST_TAGS:
                        continue
                    seq_removals.append(PhiFinding(
                        entity_uid=instance.sop_instance_uid,
                        entity_type="Instance",
                        field_name=f"Private Sequence {seq_tag}",
                        value="<PRIVATE>",
                        reason="Private Tag Removal Requested",
                        tag=seq_tag,
                        patient_id=patient_id,
                        entity=owner,
                        entity_path=path,
                        remediation_proposal=PhiRemediation(
                            action_type="REMOVE_TAG",
                            target_attr=seq_tag)))
```

`_scan_instance` has exactly two `return findings` statements and both
become `return findings + seq_removals`: **privacy.py:394**, the early
return under `if not self.phi_tags:`, and **privacy.py:472**, the last
line of the method. Leave the other three alone —
`grep -n "return findings" isocenter/privacy.py` also lists 308
(`scan_patient`) and 483/504 (`_scan_study`), none of which sees an
instance's sequences.

```python
        # Appended last, and that is the point: these delete a whole
        # sequence, and a configured-tag finding raised *inside* one
        # holds a live reference to an item within it. Remediating the
        # contents before removing the container means every audit row
        # describes an item that was still in the graph when it was
        # written.
```

The `WHITELIST_TAGS` set stays where it is; the new loop reads it, so it
must sit after the existing assignment.

### 3.3 `isocenter/remediation.py` — `REMOVE_TAG` must reach sequences

In `_apply_single_remediation`'s `REMOVE_TAG` arm (remediation.py:196), add an
`elif` to the *inner* `if`, leaving the outer branch structure alone:

```python
            if hasattr(entity, "attributes") and isinstance(entity.attributes, dict):
                if proposal.target_attr in entity.attributes:
                    ...unchanged...
                elif proposal.target_attr in getattr(entity, "sequences", {}):
                    # Same `mark_modified()` reasoning as the attribute
                    # arm above: `del` on the dict bumps no revision, so
                    # without it the next save skips an instance whose
                    # private sequence was just stripped and the store
                    # keeps it.
                    del entity.sequences[proposal.target_attr]
                    entity.mark_modified()
                    details = (f"Removed Sequence {proposal.target_attr} "
                               f"from {finding.entity_uid}")
                    action_type = "REMEDIATION_REMOVE"
```

`action_type` stays `REMEDIATION_REMOVE` — `audit_summary` counts action
types, and a second spelling would split one behaviour across two rows
of the report's section 2. The `details` text carries the distinction.

### 3.4 `isocenter/persistence.py` — read the rows back

New method on `SqliteStore`, modelled on `get_audit_losses` and placed
beside it:

```python
    def get_audit_scan_gaps(self) -> List[tuple]:
        """Every `SCAN_GAP` entry: content retained but not scanned.

        Separate from `get_audit_losses` because it is a different
        claim. A loss says an element is not in the output; this says an
        element *is* in the output and the PHI scan could not read it
        (#167). Both take the grade to REVIEW_REQUIRED, and folding them
        together would file one under a section header that denies it.

        No `loss_scope` column: these are private by construction --
        only an odd-group tag reaches the parse gate -- so the column
        would hold one value and grade nothing.

        Returns:
            List[tuple]: (timestamp, entity_uid, details)
        """
```

Body mirrors `get_audit_losses`: `flush_audit_queue()`, then
`SELECT timestamp, entity_uid, details FROM audit_log WHERE action_type
= 'SCAN_GAP' ORDER BY timestamp ASC`, wrapped in the same
`except sqlite3.OperationalError: return []`.

### 3.5 `isocenter/reporting.py`

New field on `ComplianceReport`, after `data_losses`:

```python
    # Content that reached the exported file and that the PHI scan
    # could not open: (timestamp, entity_uid, details). Its own field
    # rather than more `data_losses` -- see get_audit_scan_gaps (#167).
    scan_gaps: list = field(default_factory=list)
```

Document it in the class docstring's `Attributes:` block alongside
`data_losses`.

In `MarkdownRenderer.render`, section 3's heading becomes:

```
## 3. Data Loss & Unscanned Content
```

and its two tables get subheadings — `### 3.1 Data Loss` above the
existing warning block and table (its empty-case sentence stays exactly
`*No data loss was recorded.*`), then:

```
### 3.2 Unscanned Content
```

with, when `report.scan_gaps` is non-empty, a
`> [!WARNING]` block reading *"Elements below were retained in the
exported data and the PHI scan could not open them:"* and a
`| Timestamp | Instance | Element |` table, one row per gap; and when it
is empty, the prose `*No unscanned content was recorded.*`.

> **Corrected in review (PR #249).** That header, that row text and the
> unconditional grade are all wrong under the shipped default, and the
> PR-artifact review graded them a required fix. Reproduced on
> `e67ea77` with `remove_private_tags=True` and one 16-byte adversarial
> blob (`repro249.py`): section 2 carries `REMEDIATION_REMOVE`, the
> exported file has no `(0009,1003)` and none of the payload bytes, and
> section 3.2 renders *"Elements below were retained in the exported
> data…"* over a row saying *"It was retained verbatim and exported"*,
> under `REVIEW_REQUIRED`. Two sections of one report contradicting each
> other about the same element.
>
> The row is written by `DicomImporter` at ingest, before
> `remove_private_tags` has been applied and before any export, so it
> cannot make an export claim. Three shapes were on the table: reword
> to ingest knowledge only; resolve at render time against what shipped;
> annotate at export. The second was chosen and the other two measured
> against it:
>
> - Rewording alone leaves the reader to work out from section 2 whether
>   the bytes shipped, and leaves the grade asymmetric.
> - Annotating at export needs the gap set attached to the instance,
>   which is the stored second index #84 removed.
> - Resolving at render is a *presence* test against the object graph,
>   never a second run of the gate. It is sound because
>   `remove_private_tags` has exactly one consumer, `PhiInspector`, and
>   the exporter applies no private filtering of its own (measured: the
>   only production consumers of the flag are `privacy.py:353` and the
>   two `session.py` call sites that build the inspector).
>
> So: the header claims only *"The PHI scan could not open the elements
> below"*, the row states ingest knowledge, and a fourth **Disposition**
> column carries `removed before export` / `retained for export` /
> `unresolved`. The wording is tenseless because the documented call
> order puts `generate_report` before `export()`; what the graph settles
> is whether the element is still there to be written.
>
> The grade follows the disposition. Measured on `e67ea77`, same
> `remove_private_tags=True`, both elements absent from the exported
> file: a *parseable* private sequence graded `PASS` and the unparseable
> blob `REVIEW_REQUIRED` (`asym249.py`). That asymmetry had no argument
> behind it -- both are attestations about the export -- and is gone.
> `retained for export` and `unresolved` still grade `REVIEW_REQUIRED`.
>
> `audit_log` gains an `element_tag` column, on `loss_scope`'s precedent
> (#146): only the emitter still holds the tag, and reading it back out
> of `details` is the coupling that column exists to avoid. A row from a
> store written before it reads `unresolved`.

Two constraints, both load-bearing:

- **The empty 3.2 case must render as prose, never as an empty table.**
  `tests/test_data_loss_reporting.py::_loss_table` slices
  `content.split("Data Loss", 1)[1].split("Exceptions", 1)[0]` and reads
  every `|`-leading line in it, taking the first as the header row. An
  always-rendered 3.2 header row would land in that slice and shift
  every existing assertion.

  > **Corrected in review.** `_loss_table` is now bounded by `### 3.1`
  > to `### 3.2`, so an unconditional 3.2 header lands *outside* the
  > slice and this hazard no longer exists. The prose empty case stays,
  > for symmetry with 3.1 and nothing else -- the comment in
  > `reporting.py` says so rather than keeping a rationale whose
  > mechanism has been removed.
- **The literal string `Data Loss` must still appear ahead of any table
  row in the section.** `_loss_table` splits on its *first* occurrence,
  so the retitled `## 3. Data Loss & Unscanned Content` satisfies it —
  but a subheading shortened to `### Losses` would leave the split
  landing somewhere else. Use the subheading text as written.
- **Sections 4 and 5 keep their numbers.** `## 4. Exceptions & Errors`
  is asserted verbatim in `tests/test_reporting.py:90`,
  `tests/test_export_failure_audit.py:158` and
  `tests/test_float_pixel_data_export.py:1398`. Renumbering buys nothing
  the retitle does not.

### 3.6 `isocenter/session.py`

In `generate_report`, beside `data_losses = self.store_backend.get_audit_losses()`
(session.py:1500):

```python
        scan_gaps = self.store_backend.get_audit_scan_gaps()
```

Pass `scan_gaps=scan_gaps` into `ComplianceReport`, and extend the grade
(session.py:1584):

```python
            validation_status=("PASS"
                               if audit_summary and not exceptions
                               and not graded_losses and not scan_gaps
                               else "REVIEW_REQUIRED")
```

with a comment beside the `graded_losses` note:

```python
        # Every scan gap is graded, with no scope test: the row only
        # exists for a private element, and it says the de-identification
        # scan could not read content that is in the output. A run that
        # cannot vouch for what it exported does not get to call itself
        # PASS (#167).
```

### 3.7 Docs and changelog

- `docs/analytics.md`: item 3 of the report-section list gains the
  unscanned-content half and says it grades `REVIEW_REQUIRED`.
- `docs/configuration.md:163`: the cross-reference to "section 3 (*Data
  Loss*)" gets the section's new title.
- `docs/configuration.md`, where `remove_private_tags` is documented:
  state that the sweep removes private *sequences* too, at every depth.
- `CHANGELOG.md`: one entry under Fixed, at the depth this repo uses —
  the transfer-syntax asymmetry, the byte-exact gate and why the parse
  is not a guess, the sweep extension as a regression that the parse
  would otherwise have caused (with the explicit-VR bug it also closes),
  and the two grade changes callers will see (§7 T8, T11).

---

## 4. What the graph looks like afterwards

Measured with the prototype (`roundtrip.py 0`):

```
=== IMPLICIT, prototype parse, remove_private=False ===
  graph sequences: ['0009,1003']
  attributes has 0009,1003: False
  item attrs: {'0010,0010': 'SECRET^PHI', '0010,0020': 'MRN-999'}
  ingest findings: ... ('0010,0010','SECRET^PHI',(('0009,1003',0),))
                       ('0010,0020','MRN-999',  (('0009,1003',0),))
```

Identical in shape to the Explicit VR ingest of the same file
(`pipeline.py 0`, §1.1): tag in `sequences`, absent from `attributes`,
nested findings carrying an `entity_path` to their item. The two
transfer syntaxes converge, which is the whole point.

`entity_path` resolution works end to end — the findings above came back
from `session.audit()`, i.e. through `_make_lightweight_copy`, the
worker pool and `_rehydrate_findings`, and remediation reached the
nested item (the export below carries `ANONYMIZED`). Nothing in #57's
machinery needed changing: a nested value is remediated where it lives,
and a path that cannot resolve still returns None.

---

## 5. Persistence round-trip

The re-parsed sequence is serialized into `__sequences__` by
`SqliteStore._serialize_item` and rebuilt by `_deserialize_into`
(persistence.py:1136-1181). `_split_core_and_private` keeps
`__sequences__` inline unconditionally (persistence.py:248), so nothing
about tiering changes and nothing needs to: the bytes were already in
`attributes_json` (base64, as #151 notes) and the structure lands in the
same column. **This is a consequence of restoring the structure, not a
tiering decision** — the private *scalars* inside the items ride in
`__sequences__` too, exactly as they already do for a standard
sequence's private children.

Measured (`roundtrip.py 0`, ingest → `save(sync=True)` → `close()` →
reopen → `audit()`):

```
  -- reopen --
  reloaded sequences: ['0009,1003']
  reloaded item attrs: {'0010,0010': 'SECRET^PHI', '0010,0020': 'MRN-999'}
  reload findings: ... ('0010,0010','SECRET^PHI',(('0009,1003',0),))
                       ('0010,0020','MRN-999',  (('0009,1003',0),))
```

So this is *not* the pre-#84 shape: the structure is not derived at
ingest and lost on load, it is stored. No load-path code is required,
and none should be added — a second place that decides what an element
is, is the thing #84 removed.

---

## 6. Export

**A re-parsed, remediated sequence exports as a well-formed sequence,
under both output encodings.** `_merge_sequences` writes every sequence
with `ds.add_new(tag, 'SQ', pydicom_seq)` (io_handlers.py:2473),
including private ones, and there are two output transfer syntaxes:

- No pixel data → `_create_ds` sets `ImplicitVRLittleEndian`
  (io_handlers.py:2208), so no VR reaches the wire at all. Measured
  (`roundtrip.py 0`): the exported `(0009,1003)` re-reads as `UN` bytes
  containing `ANONYMIZED` twice; the PHI is gone and the block is kept.
- Pixel data → `use_compression` defaults True (session.py:2254) and
  `_compress_j2k` sets `JPEG2000Lossless`, which is an **explicit VR**
  syntax. Measured (`j2k_probe.py`):

```
  export TS: 1.2.840.10008.1.2.4.90 (implicit=False)
  (0009,1003) VR=SQ type=Sequence
  item: (0010,0010) Patient's Name    PN: 'ANONYMIZED'
```

  The answer to "what VR does a UN-turned-SQ get on write" is therefore
  `SQ`, written explicitly, read back as a `Sequence`. It is correct
  because the gate proved the bytes *are* that sequence.

**An unverifiable candidate does not vanish.** Its bytes stay in
`attributes` and `_merge`'s fallback encodes `bytes` as `UN`
(`_fallback_encoding`, io_handlers.py:2318). Measured in the same
explicit-VR run, with a 9-byte blob that fails the gate:

```
  (0009,1004) VR=UN value=feff00e00102030405
```

Byte-identical, under the explicit-VR export. And
`remove_private_tags=True` still removes it — measured in the same
script (`j2k_probe.py 1`), where it is the existing attribute sweep
doing the work:

```
  (0009,1003) VR=SQ type=Sequence      <- the §1.3 defect, again
  (0009,1004) : ABSENT from the export
```

---

## 7. Test plan

New file `tests/test_private_sequence_implicit_vr.py`, plus one addition
to `tests/test_pydicom_deprecations.py`.

Fixture rule: the implicit-VR files are written **by hand** with
`struct.pack("<HHI", …)` — element header, value, item framing — so the
test controls the exact bytes under test and no helper can supply the
answer. Route every assertion through `session.audit()` / `session.export()`,
never through a direct `PhiInspector` call: #57's lesson is that a test
that calls the inspector directly cannot see this class of bug.

| # | Test | The one-line change it turns red |
| --- | --- | --- |
| T1 | `audit()` on an implicit-VR file finds the two nested identifiers, with `entity_path == (("0009,1003", 0),)` | deleting the `_sequence_from_un_bytes` call from `populate_attrs`. **Red on current main** — measured 6 findings with neither nested one (§1.1) |
| T2 | with `remove_private_tags=False`, the exported bytes contain neither `SECRET^PHI` nor `MRN-999`, and `(0009,1003)` is still present | remediation not reaching the nested item, or the parse being dropped. **Red on current main** — measured `export contains SECRET^PHI: True` |
| T3 | after ingest, `"0009,1003" in inst.sequences` and `"0009,1003" not in inst.attributes` | keeping `item.set_attr(tag, elem.value)` alongside the parse. Without this the same tag goes through both `_merge` and `_merge_sequences` and their call order (io_handlers.py:920-921) decides whether the file carries the remediated sequence or the original PHI-bearing bytes |
| T4 | ingest → `save(sync=True)` → `close()` → reopen → `audit()` still yields the nested findings | building the sequence anywhere that `_serialize_item` does not read — e.g. onto a side attribute of `Instance` — or `_split_core_and_private` routing `__sequences__` to the EAV table |
| T5 | a private SQ nested inside a *standard* sequence is parsed and its PHI found | gating the new branch on `is_root` |
| T6 | the same private sequence written Explicit VR and Implicit VR produces the same graph shape (same `sequences` key, both absent from `attributes`, same item attributes) | a parse that lands the items under a different key or shape than `process_sequence` produces |
| T7 | parametrized over the four adversarial values from `gate_probe.py` (truncated header; item then garbage; huge length; 9-byte coincidental blob): ingest does not raise, `inst.attributes[tag]` equals the input bytes **exactly**, and `inst.sequences` is empty | relaxing the gate to "parsed without raising" (drops the re-encode check). Three of the four get past `read_sequence` cleanly, and the coincidental blob would silently lose five bytes |
| T8 | the coincidental blob files a `SCAN_GAP` audit row; `generate_report` grades `REVIEW_REQUIRED` and the report body **names the tag** | emitting only a `logger.warning`, or grading without rendering — the "count with no rows" defect #146 argues against. Assert the tag string in the file, not just the status |
| T9 | parametrized over both transfer syntaxes: with `remove_private_tags=True` (the default), the exported file contains no odd-group element at all | landing the parse without §3.2/§3.3. **Red on current main for the explicit parameter** — measured `(0009,1003) VR=UN feff00e0…` in the export (§1.3) |
| T10 | with both `remove_private_tags=True` and `phi_tags` set, filter the report to findings whose `entity_uid` is this instance's SOP UID; the `0009,1003` `<PRIVATE>` finding's index is greater than both nested findings' | returning `seq_removals + findings`; the nested remediation would then write into an item already detached from the graph and file audit rows for it. Filter by `entity_uid` rather than asserting `findings[-1]`: patient-, study- and instance-level findings interleave in the report (§1.1's measured order), and that interleaving is not what this test is about |
| T11 | a private sequence whose item carries a binary-VR child (`(0009,1002)` under creator `BrainLAB_Conversion`, `OB` in pydicom's private dictionary) files a PRIVATE `DATA_LOSS` row and grades `REVIEW_REQUIRED` | calling `process_sequence` without `dropped` for the re-parsed sequence — the child would vanish with no row, which is #137 all over again |
| T12 (in `test_pydicom_deprecations.py`) | the gate's exact call shape works and emits no message matching `REMOVED_IN_V4`; `getattr(Dataset(), "_character_set")` is non-empty (it is `'iso8859'`; a dataset with a Specific Character Set gives a list, and both are valid `encoding` arguments) | pydicom deprecating `DicomIO.is_little_endian` / `is_implicit_VR` or renaming `_character_set`. Without it, a pydicom bump makes `_sequence_from_un_bytes` return None for *every* sequence and #167 silently returns, reported as "unparseable" when the truth is that our parser broke |

T11 is a **behaviour change to announce, not just to test**: before the
parse, such an instance had one opaque `UN` attribute and graded `PASS`;
after it, the binary child is reported and the run grades
`REVIEW_REQUIRED`. Measured with the prototype (`binary_child.py`):

```
  item attrs: {'0009,0010': 'BrainLAB_Conversion', '0010,0010': 'SECRET^PHI'}
  report: | **Validation Status** | **REVIEW_REQUIRED** |
  report: | ... | Private tag 0009,1002 (OB) was not ingested; ... | PRIVATE |
  report: | ... | Standard tag 0028,1201 (OW) was not ingested; ... | STANDARD |
```

The loss is real and was previously unreported; both rows are correct.

### Verification the coder owes

- `find . -name .DS_Store -delete` first, and run from a clean tree
  (#234, #245).
- Full suite on 3.12.13 **and** 3.14.7t. The prototype (parse + sweep +
  `REMOVE_TAG`) already ran green here:
  `/Users/kevin/Developer/Isocenter/.venv/bin/python -m pytest -q` →
  `988 passed in 196.52s`, no existing test touched.
- Confirm T1, T2 and T9-explicit are red before the fix and green after.
  A test plan whose "red on main" claim was never run is the thing this
  spec exists to avoid.

---

## 8. Order of work

Two commits, one PR. They are not independent — commit 1 alone is the
default-config regression measured in §1.3, so it must not be pushed on
its own.

1. `io_handlers.py` (§3.1) + T1-T8, T11, T12.
2. `privacy.py`, `remediation.py`, report plumbing (§3.2-§3.6) + T9, T10.

T9 and T10 land with commit 2 by construction — commit 1 alone fails
them, because it *is* the regression. Do not add them in commit 1 and
mark them xfail.

> **Corrected in review.** This is not the split that shipped. §3.4-§3.6
> moved into commit 1 so that T8 — which asserts on the rendered report
> — could pass there, leaving:
>
> 1. `io_handlers.py` (§3.1) + report plumbing (§3.4-§3.6) + T1-T8,
>    T11, T12 (`172331c`).
> 2. `privacy.py`, `remediation.py` (§3.2-§3.3) + T9, T10 (`afb1ee8`).
>
> The prose above says commit 1 alone fails T9 and T10, which is two
> tests. Measured by checking out `172331c` and running the file: **four
> red** — T9[implicit], T9[explicit], T10, and
> `test_remove_private_tags_reaches_a_nested_private_sequence`. **Five**
> once `3d862a0`'s
> `test_a_nested_private_sequence_is_removed_before_its_container` is
> applied on top — measured by running the test file as of `3d862a0`
> against `isocenter/` as of `172331c` (`split_probe.sh`): T9[implicit],
> T9[explicit], T10, the nested-sweep test and the ordering test, `5
> failed, 16 passed`. The four is what the coder's tree produced at the
> time it was measured. Either way the
> conclusion holds and hardens: commit 1 is the default-config
> regression and must not ship alone.

---

## 9. Non-goals and deferrals

- **#154 (restoring the original VR of retained private tags) is not
  built here.** This fix resolves exactly one VR, `UN → SQ`, and only
  when it can prove it byte for byte. That is the natural extension
  point for #154: `_sequence_from_un_bytes` is already "decide what
  these bytes are, and refuse if you cannot", and a VR carried through
  ingest would let it answer without the re-encode. Nothing here
  forecloses it — no VR is stored, and no caller is taught that `UN`
  means "unknown blob".
- **#151's tiering is untouched.** The bytes were in
  `attributes_json`; the structure is in `attributes_json`. Where a
  private *scalar* lives is still `_split_core_and_private`'s question.
- **Even-group `UN` values are not parsed.** A standard tag resolves
  from the dictionary, so it does not reach `UN` by the implicit-VR
  route; an even-group `UN` means a writer chose it explicitly, which is
  a different question with a different population.
- **Undefined-length private sequences need no work** — pydicom already
  recovers them (§1.2, measured).
- **Stores written before this fix keep their blobs.** Hydration
  reproduces what was saved, and re-deriving structure on load would put
  a second answer to "what is this element" into the codebase, which is
  what #84 removed. Callers with an old store re-ingest. Say so in the
  CHANGELOG entry.
- **The PHI report from `audit()` stays silent about an unverifiable
  candidate.** The compliance report carries the row and the grade; a
  `PhiFinding` with no remediation proposal would be a new shape for
  that record, and is not worth it for this population. §D.
- **Non-conformant item content (elements out of ascending tag order)
  falls back rather than parsing** (§2.1). Widening the gate to accept
  it means giving up the byte-exactness argument; the row is the
  answer instead.

## §D — Findings not filed

1. **Private `UN` blobs that are not sequence-shaped are never scanned
   and get no row.** They are retained under `remove_private_tags=False`
   and could hold identifiers in any vendor encoding. This fix narrows
   the silent population to "not sequence-shaped"; it does not eliminate
   it. That residual is #151/#154's ground and the deliberate boundary
   of this change.
2. **The PHI report cannot say "I could not read this".** Every
   `PhiFinding` carries a remediation proposal, so a scan gap has
   nowhere to go in the artifact a user reads to decide what to fix.
   Adding a proposal-less finding shape would close §9's fourth bullet
   and would also give the `remove_private_tags=False` blobs of item 1 a
   voice. Not filed; it is an API-shape call and #164-#218 is heading
   for v1.0.
3. **Section 3 of the compliance report now carries two claims under one
   number.** The retitle keeps the section numbering stable, which is
   worth more today than a clean taxonomy; if a third kind of "the
   output is not what the source was" row ever appears, the section
   should be split and 4/5 renumbered in one deliberate change rather
   than a third subheading.
4. **`_export_instance_worker` calls `populate_attrs(ds, inst)` on the
   dataset it has just built** (io_handlers.py:1121), which appends
   duplicate items to `inst.sequences` whenever `inst` is the live
   object.

   Corrected in review. The original probe set
   `ISOCENTER_FORCE_THREADS=1` and saw the item count stay at 1, and
   concluded the worker always operates on a copy. It does not: the env
   var was inert for that probe because `DicomSession` passes its own
   `self._executor` -- a `ProcessPoolExecutor`, constructed
   unconditionally at `session.py:573` -- into `run_parallel`, so
   `session.export()` pickles the instance on *every* interpreter and
   the mutation lands on a copy. `DicomExporter.write_tree()` passes no
   executor, so `run_parallel` chooses for itself, and on a
   free-threaded build it chooses threads. Measured (`writetree_probe.py`)
   with a hand-built graph carrying one standard `(0008,1140)` item:
   `before: 1` / `after: 1` on 3.12.13, `before: 1` / `after: 2` on
   3.14.7t. The caller's graph is mutated by the serializer.

   Pre-existing and independent of #167 -- the probe uses a *standard*
   sequence and the call is untouched by this change -- but #167 puts
   private sequences into `inst.sequences` for the first time, so it
   widens the population. Not fixed here: `write_tree` is the serializer
   the `scripts/` fixture generators use, the written file is unaffected
   (the mutation happens after `ds` is assembled), and removing the call
   is an export-semantics change that wants its own issue.
