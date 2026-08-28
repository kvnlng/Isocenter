"""PhysioNet WFDB export format.

Writes format-16 WFDB records (`.hea` + `.dat`), one record per
waveform-bearing Instance. Output is `header(5)`-conformant so it stays
readable by PhysioNet's own tooling as well as by Murmur Studio.
"""
import os
import re
from typing import List, Optional

import numpy as np

from . import Exporter, register
from ..io_handlers import (export_folder_names, format_study_date,
                           LOSS_SCOPE_STANDARD)
from ..logger import get_logger
from ..waveform import Waveform, WaveformChannel

WAVEFORM_SEQUENCE_TAG = "5400,0100"

# WFDB format 16: 16-bit two's complement, little-endian, channel-interleaved.
WFDB_FORMAT = 16
WFDB_ADC_ZERO = 0


def signal_checksum(channel_samples) -> int:
    """16-bit signed sum of a signal's samples, as `header(5)` defines it.

    Args:
        channel_samples: 1-D array of int16 samples for one channel.

    Returns:
        int: Checksum in the range [-32768, 32767].
    """
    arr = np.asarray(channel_samples)
    if arr.size == 0:
        return 0
    total = int(np.sum(arr, dtype=np.int64)) & 0xFFFF
    if total >= 0x8000:
        total -= 0x10000
    return total


def _sanitize(name: str) -> str:
    """Reduce a string to characters safe in a WFDB *record name*.

    This is deliberately stricter than `ConfigLoader.clean_filename`
    (which permits spaces): a WFDB record name is a bare token, not a
    filename component, so it must not contain whitespace. It is used
    only for `record_name_for` below -- the *folder* names this module
    writes into come from `export_folder_names` in `io_handlers.py`,
    which uses `ConfigLoader.clean_filename` (the same sanitizer
    `DicomSession._export_dicom` uses) so the WFDB and DICOM exporters
    land in character-for-character identical directories. Do not use
    this function for folder names, or the two trees will diverge again.
    """
    # NOTE: `name or ""` would discard a legitimate falsy-but-meaningful
    # value like the int 0 (InstanceNumber 0 is valid DICOM), collapsing
    # it to the "record" fallback below. Test for None explicitly instead.
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(name if name is not None else ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "record"


def record_name_for(patient, study, series, instance) -> str:
    """Build a record name from already-anonymized identifiers.

    Called after anonymization, so `patient.patient_id` is the pseudonym,
    not the source MRN. The instance number disambiguates multiple
    acquisitions within one series -- but InstanceNumber is frequently
    absent (`io_handlers.py` defaults it to 0), and multiple instances
    missing it all produce the SAME base name here. This function does
    not resolve that collision by itself; callers that write more than
    one instance into the same directory must disambiguate the result
    (see `WfdbExporter._unique_record_name`).
    """
    return "_".join([
        _sanitize(patient.patient_id),
        _sanitize(series.series_number if series.series_number is not None else 0),
        _sanitize(instance.instance_number if instance.instance_number is not None else 0),
    ])


def _format_number(value) -> str:
    """Render a float without a trailing '.0', which WFDB readers dislike."""
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    return repr(round(as_float, 6))


# Matches CR, LF, and other control characters a conformant WFDB reader
# could interpret as ending the current line (vertical tab, form feed,
# NEL, LINE/PARAGRAPH SEPARATOR). Deliberately narrower than "all
# whitespace" -- ordinary spaces are legal and preserved.
_LINE_BREAK_CHARS = re.compile(r"[\r\n\x0b\x0c\x1c-\x1f\x85  ]")


def _sanitize_description(value: str) -> str:
    """Remove characters that could inject a `.hea` comment line.

    The signal-line description is the LAST field on a `header(5)`
    signal line and legally runs to end of line, including embedded
    spaces (PhysioNet's own reader parses `... 0 0 Lead I taken by Jane
    Doe` as one `sig_name`) -- so this must NOT strip spaces. What it
    must remove is any character a reader could treat as ending the
    line: raw DICOM text (e.g. Channel Label, SH) is not guaranteed
    free of control characters, and an embedded newline followed by a
    line starting with `#` is read by `wfdb.rdheader` as a real header
    comment (`comments=[...]`) -- a PHI escape route on a
    de-identification product. Replacing line-breaking characters with
    a space guarantees the description can never manufacture a second
    physical line, so a leading `#` anywhere in the collapsed text stays
    inert (interior text on a signal line, not a comment line).
    """
    return _LINE_BREAK_CHARS.sub(" ", str(value)).strip()


def _sanitize_units(value: str) -> str:
    """Remove ALL whitespace/control characters from the `units` field.

    Unlike the description, `units` is field 3 of 9 inside
    `gain(baseline)/units` -- it is NOT the last field on the line, so
    any whitespace here (not just newlines) shifts every field after it.
    Reproduced: CodeValue "mV per s" makes `wfdb.rdheader` parse
    `units=['mV']` and `sig_name=['per s 16 0 0 1225 0 ...']` -- the
    signal name and every numeric field after gain are silently wrong.
    """
    return re.sub(r"\s+", "", str(value))


def format_header(record_name: str,
                  waveform: Waveform,
                  samples: np.ndarray,
                  dat_filename: str,
                  start_datetime=None,
                  start_date_note: Optional[str] = None) -> str:
    """Render a WFDB `.hea` file.

    Emits no `#` comment lines, with exactly one deliberate exception:
    `start_date_note`. MIT-BIH convention puts age, sex, and diagnosis
    in comments, and readers render comments verbatim, so a comment line
    is normally a PHI escape route -- this holds even when the channel
    description (Channel Label/Channel Source, raw DICOM SH text) itself
    contains an embedded CR/LF: `_sanitize_description` collapses any
    line-breaking character to a space before it reaches the signal
    line, so it cannot manufacture a second physical line that a
    conformant reader would surface as a comment. `start_date_note` is
    different in kind: it is not operator-typed or attacker-controlled
    text, it is a string the caller has already computed around a
    `DD/MM/YYYY` date (the same format used for the record line's own
    date field), and it is written through this function's own
    comment-writing path -- not a second, separately-sanitized one --
    specifically so this narrow exception cannot become a
    general-purpose injection route later.

    Args:
        record_name (str): Record name (must match the .hea basename).
        waveform (Waveform): Geometry and per-channel calibration.
        samples (np.ndarray): int16, shape (num_samples, num_channels).
        dat_filename (str): Signal file basename referenced by each line.
        start_datetime (datetime, optional): Already date-shifted start
            time. Omitted from the record line when None.
        start_date_note (str, optional): The complete comment text
            preserving a start date when `start_datetime` is None because
            no real time-of-day is available -- e.g. Acquisition DateTime
            and Study Time both removed by the Basic profile. Writing a
            fabricated `00:00:00` into the record line to keep the date
            would be worse than omitting it (see
            `WfdbExporter._start_datetime`); losing the date outright
            would be a research-utility regression, since `SHIFT_DATE`
            produces a genuine de-identified date. This is the
            compromise: the date survives, but only somewhere a consumer
            would not mistake it for a precise timestamp.

            The caller supplies the whole string, not just the date,
            because the wording is a claim about provenance:
            `de-identified start date: ...` only when the date really was
            shifted, `start date: ...` when it is the real one and
            nothing has de-identified it. This function used to prepend
            "de-identified" unconditionally, which labelled real study
            dates as de-identified on any export where `anonymize()` had
            not run. Omitted (no comment line at all) when None or empty
            -- never an empty/placeholder comment.

    Returns:
        str: Complete header text, newline-terminated.
    """
    n_samples = int(samples.shape[0]) if samples.ndim == 2 else 0
    n_channels = int(samples.shape[1]) if samples.ndim == 2 else 0

    record_fields = [
        record_name,
        str(n_channels),
        _format_number(waveform.sampling_frequency),
        str(n_samples),
    ]
    if start_datetime is not None:
        record_fields.append(start_datetime.strftime("%H:%M:%S"))
        record_fields.append(start_datetime.strftime("%d/%m/%Y"))

    lines = [" ".join(record_fields)]

    if start_date_note:
        # Same sanitizer the signal-line description gets -- not a new,
        # separately-maintained comment-writing path. `start_date_note`
        # is a caller-computed string around a DD/MM/YYYY date, not
        # attacker input, but a second unsanitized comment path is
        # exactly how injection bugs reappear.
        comment_text = _sanitize_description(start_date_note)
        lines.append(f"# {comment_text}")

    for idx in range(n_channels):
        if waveform.channels:
            channel = (waveform.channels[idx]
                       if idx < len(waveform.channels)
                       else waveform.channels[-1])
        else:
            # Non-conformant source: NumberOfWaveformChannels > 0 but the
            # Channel Definition Sequence is absent or empty, so there is
            # no calibration to read. `waveform.channels[-1]` would raise
            # IndexError here and, with no per-instance guard around the
            # caller, abort an entire batch export over one bad instance.
            # Fall back to an uncalibrated placeholder channel (gain 1.0,
            # baseline 0, "mV") so this instance's other channels/other
            # instances still export.
            channel = WaveformChannel(label=f"unknown_channel_{idx}")

        column = samples[:, idx]

        gain = channel.gain()
        baseline = channel.wfdb_baseline()
        units = _sanitize_units(channel.units or "mV")

        # header(5): <gain>(<baseline>)/<units>
        gain_field = f"{_format_number(gain)}({baseline})/{units}"

        lines.append(" ".join([
            dat_filename,
            str(WFDB_FORMAT),
            gain_field,
            str(int(waveform.bits_allocated or 16)),
            str(WFDB_ADC_ZERO),
            str(int(column[0]) if column.size else 0),
            str(signal_checksum(column)),
            "0",
            _sanitize_description(channel.wfdb_description(idx)),
        ]))

    return "\n".join(lines) + "\n"


def _parse_dicom_tm(value: str):
    """Parse a DICOM TM (`HH`, `HHMM`, `HHMMSS[.FFFFFF]`) to a `time`.

    Keyed by length rather than tried longest-format-first, because
    `strptime` accepts one- or two-digit fields and therefore *succeeds*
    on the wrong format instead of raising and falling through:
    `"1430"` matches `%H%M%S` as 14:03:00, and `"14"` matches `%H%M` as
    01:04:00. Nothing raised, so nothing corrected it -- an ECG recorded
    at half past two exported claiming three minutes past.

    Returns None for anything not a legal TM length.
    """
    from datetime import datetime

    stamp = (value or "").split(".")[0].strip()
    fmt = {2: "%H", 4: "%H%M", 6: "%H%M%S"}.get(len(stamp))
    if not fmt:
        return None
    try:
        return datetime.strptime(stamp, fmt).time()
    except ValueError:
        return None


def _parse_dicom_dt(value: str):
    """Parse a DICOM DT to a `datetime`, at the precision it carries.

    Same length-keyed reasoning as `_parse_dicom_tm`. The offset suffix
    (`&ZZXX`) is dropped: this tool reports the timestamp as recorded and
    does not convert time zones.

    Returns None for anything not a legal DT length.
    """
    from datetime import datetime

    stamp = (value or "").split("+")[0].split("-")[0].split(".")[0].strip()
    fmt = {4: "%Y", 6: "%Y%m", 8: "%Y%m%d", 10: "%Y%m%d%H",
           12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(stamp))
    if not fmt:
        return None
    try:
        return datetime.strptime(stamp, fmt)
    except ValueError:
        return None


class WfdbExporter(Exporter):
    """Writes WFDB records for every waveform-bearing instance."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Write WFDB records into `folder`.

        Args:
            session (DicomSession): Active session.
            folder (str): Output root.
            **options: `patient_ids` (list, optional) limits the export.
                `include_annotation_text` (bool, default False) releases
                the operator-typed text in annotations.json: Unformatted
                Text Value (0070,0006) into `note`, and a site-defined
                Concept Name's Code Meaning into `label` with its Code
                Value as `category`. Off by default because both are free
                text; pass it when the study protocol permits their
                release. Concepts from a published coding scheme are
                unaffected either way.

        Returns:
            List[str]: Paths of the `.hea` files written.
        """
        logger = get_logger()
        patient_ids = options.get("patient_ids")
        # Off by default: (0070,0006) is free-text clinical commentary, and
        # a site-defined Concept Name's Code Meaning is operator-typed too.
        # This is the auditor's override, not a debug switch -- it says the
        # protocol permits releasing that text.
        include_annotation_text = bool(options.get("include_annotation_text", False))
        # Passed down explicitly rather than read off `session` inside
        # `_write_instance`, so the one place that writes an audit entry
        # names its dependency instead of reaching back through the
        # facade. `None` is a legitimate value: `_write_instance` is
        # called directly by tests with no session behind it.
        store_backend = getattr(session, "store_backend", None)
        written = []
        used_names = {}  # out_dir -> set of record names already claimed

        for patient in session.store.patients:
            if patient_ids and patient.patient_id not in patient_ids:
                continue

            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        # Contain per-instance failures the way
                        # `DicomExporter._export_instance_worker` does
                        # (io_handlers.py): catch, log, continue. Without
                        # this, one malformed instance out of hundreds
                        # raises out of `session.export()` and aborts the
                        # whole run, leaving every later patient silently
                        # unexported with no indication on disk that the
                        # run was partial.
                        try:
                            path = self._write_instance(
                                folder, patient, study, series, instance, logger,
                                used_names, include_annotation_text,
                                store_backend)
                        except Exception as e:
                            logger.error(
                                f"WFDB export failed for instance "
                                f"{instance.sop_instance_uid}: {e}")
                            continue
                        if path:
                            written.append(path)

        logger.info(f"WFDB export complete. {len(written)} records written.")
        return written

    @staticmethod
    def _unique_record_name(base_name, out_dir, used_names):
        """Disambiguate record names that collide within one output directory.

        `record_name_for` derives its instance component from
        InstanceNumber, which is frequently absent -- `io_handlers.py`
        defaults it to 0, and `_sanitize` used to collapse that (and now
        renders it as the literal digit "0", still identical across every
        instance missing InstanceNumber). Two instances proposing the
        same record name in the same directory would otherwise silently
        overwrite each other's `.hea`/`.dat` files. This guarantees a
        unique name per directory deterministically, in write order.
        """
        seen = used_names.setdefault(out_dir, set())
        candidate = base_name
        suffix = 2
        while candidate in seen:
            candidate = f"{base_name}_{suffix}"
            suffix += 1
        seen.add(candidate)
        return candidate

    def _write_instance(self, folder, patient, study, series, instance, logger,
                        used_names, include_annotation_text=False,
                        store_backend=None):
        """Write one record. Returns the .hea path, or None if not a waveform.

        `used_names` is REQUIRED (not `=None`): its absence used to
        silently disable `_unique_record_name` deduplication, which is a
        real data-loss bug (two instances missing InstanceNumber would
        propose the same record name and the second write would silently
        overwrite the first's `.hea`/`.dat`). Making it required removes
        that latent reintroduction path -- a caller that forgets it now
        gets a loud `TypeError` instead of quiet overwrite corruption.
        """
        seq = instance.sequences.get(WAVEFORM_SEQUENCE_TAG)
        if seq is None or not seq.items:
            return None

        samples = instance.get_waveform_data()
        if samples is None or samples.size == 0:
            logger.warning(
                f"Instance {instance.sop_instance_uid} declares a waveform "
                "but has no sample data; skipping.")
            return None

        waveform = Waveform.from_dicom_item(seq.items[0])

        # Co-locate with the DICOM exporter's tree: same Subject_/Study_/
        # Series_ folder names, built by the one shared helper both
        # exporters call, so the two trees cannot drift apart.
        subj_name, study_folder, series_folder = export_folder_names(patient, study, series)
        out_dir = os.path.join(folder, subj_name, study_folder, series_folder)
        os.makedirs(out_dir, exist_ok=True)

        base_name = record_name_for(patient, study, series, instance)
        record_name = self._unique_record_name(base_name, out_dir, used_names)
        dat_filename = f"{record_name}.dat"

        # Format 16 is little-endian, channel-interleaved -- identical to the
        # DICOM layout -- so this is a direct write with no transcoding.
        dat_path = os.path.join(out_dir, dat_filename)
        with open(dat_path, "wb") as f:
            f.write(np.ascontiguousarray(samples, dtype="<i2").tobytes())

        start_datetime, start_date_note = self._start_datetime(instance, study)
        header = format_header(
            record_name, waveform, samples, dat_filename,
            start_datetime=start_datetime,
            start_date_note=start_date_note)

        hea_path = os.path.join(out_dir, f"{record_name}.hea")
        with open(hea_path, "w", encoding="utf-8") as f:
            f.write(header)

        from ..murmur import build_annotations, write_annotations

        try:
            from .. import __version__ as isocenter_version
        except ImportError:
            isocenter_version = "0.0.0"

        manufacturer = str(instance.attributes.get("0008,0070", "") or "").strip()
        source = f"isocenter/{isocenter_version}"
        if manufacturer:
            source = f"{source} ({manufacturer})"

        dropped_groups = []
        write_annotations(
            os.path.join(out_dir, f"{record_name}.annotations.json"),
            build_annotations(instance, waveform, source, include_annotation_text,
                              dropped_groups=dropped_groups))

        # Warn-plus-audit, the shape #36 established for the multiplex
        # discard itself. A dropped annotation that says nothing is a
        # different bug from a mislabelled one, not a fix for it (#159).
        #
        # ONE row per instance, naming the count and the groups -- not
        # one per annotation. #36's emitter, which this descends from,
        # reports "carried N groups; kept group 0 and discarded N-1"
        # once; a cart that marks forty beats on a discarded group would
        # otherwise put forty near-identical rows into section 3 of the
        # compliance report, and a section nobody can read reports
        # nothing. Group ordinals are deduplicated but the annotation
        # count is not, because they answer different questions: which
        # signal was referenced, and how much was dropped.
        #
        # Scoped STANDARD because Waveform Annotation Sequence
        # (0040,B020) is an even group, so under the #146 parity rule the
        # session still grades PASS. That is deliberate and is the one
        # choice here that does not pre-empt #150: the scope states what
        # the element *was*, not how bad the loss felt, exactly as the
        # ingest-side multiplex emitter in `io_handlers.py` insists.
        # Grading this one harder than the group discard it follows from
        # would decide #150 on the wrong ticket, and in the wrong place.
        if dropped_groups:
            # `dropped_groups` is one list per dropped annotation, so the
            # count and the group set come from different axes of it: a
            # single annotation may name several groups, and reporting
            # only its first would under-report the loss while the word
            # "groups" promised otherwise.
            count = len(dropped_groups)
            ordinals = sorted({g for groups in dropped_groups for g in groups})
            detail = (
                f"Dropped {count} waveform "
                f"{'annotation' if count == 1 else 'annotations'} from "
                f"annotations.json: referenced multiplex "
                f"{'group' if len(ordinals) == 1 else 'groups'} "
                f"{', '.join(str(g) for g in ordinals)}, not ingested. Only "
                f"Waveform Sequence item 0 is kept (#36); resolving these "
                f"against the surviving group would have placed each mark at "
                f"a position and lead belonging to a different signal.")
            logger.warning(f"{instance.sop_instance_uid}: {detail}")
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="DATA_LOSS",
                    entity_uid=instance.sop_instance_uid,
                    details=detail,
                    loss_scope=LOSS_SCOPE_STANDARD)

        return hea_path

    @staticmethod
    def _instance_time_of_day(instance):
        """Best-effort time-of-day from the instance's own timestamp tags.

        SHIFT_DATE (see `_start_datetime` below) only ever moves a date,
        never a time-of-day, so the time component always comes from here
        regardless of whether the session has been anonymized.

        Returns a `datetime.time`, or None if no usable value exists.
        """
        acquired = _parse_dicom_dt(
            str(instance.attributes.get("0008,002a", "") or ""))
        if acquired is not None:
            return acquired.time()

        return _parse_dicom_tm(
            str(instance.attributes.get("0008,0030", "") or ""))

    @staticmethod
    def _instance_only_datetime(instance):
        """Fallback timing built purely from instance tags.

        Used when no `Study` is available, or the Study has no usable
        date. This is real (possibly un-shifted) timing, and that is
        correct here, not a leak: it mirrors how every other field this
        tool does not touch behaves before `session.anonymize()` runs.
        Suppressing timing on an un-anonymized session would be a design
        change, not a bug fix.

        Returns:
            tuple[Optional[datetime], Optional[str]]: the record-line
                value, or None with a note for the caller to write as a
                comment when a date is known but no time of day is.
        """
        from datetime import datetime

        acquired = _parse_dicom_dt(
            str(instance.attributes.get("0008,002a", "") or ""))
        if acquired is not None:
            return acquired, None

        date_part = str(instance.attributes.get("0008,0020", "") or "").strip()
        if not date_part:
            return None, None

        try:
            only_date = datetime.strptime(date_part, "%Y%m%d").date()
        except ValueError:
            return None, None

        # Parsed on its own, not concatenated onto the date. The combined
        # stamp was only ever tried as `%Y%m%d%H%M%S`, so a four-digit
        # Study Time failed and took the date down with it -- real
        # recorded timing discarded, while an instance with no time at
        # all kept its date.
        time_of_day = _parse_dicom_tm(
            str(instance.attributes.get("0008,0030", "") or ""))
        if time_of_day is not None:
            return datetime.combine(only_date, time_of_day), None

        # A date, and no time of day we can read. `000000` used to be
        # appended here, so the record line carried `00:00:00` -- timing
        # this tool invented, indistinguishable to a reader from an
        # acquisition that really happened at midnight (#59).
        return None, f"start date: {only_date.strftime('%d/%m/%Y')}"

    @staticmethod
    def _start_datetime(instance, study):
        """Record start time, read after de-identification.

        `study` is REQUIRED (not `=None`): its absence used to silently
        revert to reading the instance's own (never-shifted) date tags,
        reinstating a Safe Harbor date leak past a genuine
        `session.anonymize()` pass (see below). Pass `study=None`
        explicitly for the documented "no study available" fallback --
        that is a supported value, just no longer an accidental default.

        APPROVED DEVIATION from the brief (Task 9 review round 1,
        coordinator override): the shipped `isocenter/resources/phi_tags.json`
        contains no date tags, so instance-level Acquisition DateTime
        (0008,002A), Study Date (0008,0020), and Study Time (0008,0030)
        are NEVER covered by the default remediation config and are
        NEVER shifted by `session.anonymize()`. The date shift that
        actually runs is a Study-level scan (`isocenter/privacy.py`,
        `PhiScanner._scan_study`) whose SHIFT_DATE remediation
        (`isocenter/remediation.py`) writes the new date onto
        `study.study_date` and sets `study.date_shifted = True`. Reading
        the instance tags as "post-remediation" (the original Task 9
        Step 3 approach) was therefore reading a field that is never
        remediated -- a real Safe Harbor date leak past a genuine
        anonymize() pass.

        This reads the DATE from `study.study_date` -- the field SHIFT_DATE
        actually writes -- and combines it with the instance's own
        time-of-day (SHIFT_DATE never touches time-of-day, so that part is
        always sourced from the instance, shifted session or not).

        Falls back to instance-only date+time when `study` is not
        supplied, or has no usable `study_date`: on an un-anonymized
        session (or a session with no date at all), the header carries
        real timing, exactly like every other un-remediated field. This
        function does not suppress timing on an un-anonymized session --
        that would be a design change, not a bug fix.

        Never fabricates a time-of-day. When `study.study_date` is a real
        (shifted or unshifted) date but no real time-of-day is available --
        which is now the normal case on a fully configured, anonymized
        session, since the Basic profile (#38) removes both Acquisition
        DateTime (0008,002A) and Study Time (0008,0030) -- the record's
        start time/date fields are omitted entirely rather than
        substituting a fake "00:00:00". header(5) does not support a
        date-only start time: PhysioNet's own spec documents base_date as
        depending on base_time being present, `wfdb-python`'s RECORD_SPECS
        encodes that same dependency (`Record.wrheader()` raises "Missing
        field required: base_time" if only a date is set), and empirically
        `wfdb.rdheader`'s own field regex cannot disambiguate a bare date
        from a bare time -- both are just digit runs, so a hand-written
        date-only record line gets its day silently swallowed into
        base_time and its base_date left dangling (verified: it either
        raises inside `datetime.strptime` or misparses). So "write the
        date without a time" is not achievable against the reference
        reader; the only options that neither fabricate a fake time nor
        misparse are (a) omit both fields, or (b) leak the unshifted
        instance date via the fallback below. This picks (a).

        But (a) alone would silently drop a genuine de-identified date:
        `study.study_date` after SHIFT_DATE is real, useful information
        (e.g. for ordering records within a cohort), not PHI-adjacent
        noise -- unlike the fabricated time it would otherwise have been
        paired with. So this case is not "return nothing"; it is "return
        no record-line datetime, but hand the caller the shifted date
        separately" so `format_header` can preserve it as a `#` comment
        instead of a precise (and precisely wrong) timestamp.

        Returns:
            tuple[Optional[datetime], Optional[str]]:
            `(start_datetime, deidentified_date_comment)`.
            `start_datetime` is exactly what this function used to
            return alone: the record-line value, or None. When it is
            None specifically because a real study date exists but no
            real time-of-day does, `deidentified_date_comment` carries
            that date as a `DD/MM/YYYY` string (the record line's own
            date format) for the caller to write as a comment.
            `deidentified_date_comment` is None in every other case --
            including when `start_datetime` already carries the date (no
            need to duplicate it), and when there is no shifted date to
            report at all (the instance-only fallback below never
            populates it: that path is real, un-shifted, pre-anonymize
            timing, not a SHIFT_DATE result worth preserving specially).
        """
        from datetime import datetime

        time_of_day = WfdbExporter._instance_time_of_day(instance)

        if study is not None:
            normalized = format_study_date(getattr(study, "study_date", None))
            if normalized:
                try:
                    shifted_date = datetime.strptime(normalized, "%Y%m%d").date()
                except ValueError:
                    shifted_date = None
                if shifted_date is not None:
                    if time_of_day is not None:
                        return datetime.combine(shifted_date, time_of_day), None
                    # Real date, no real time: do not fabricate one, and
                    # do not fall through to the instance-only fallback
                    # below -- that would read the instance's real,
                    # un-shifted date and reopen the Safe Harbor leak
                    # this function's docstring above exists to close.
                    # The date is real information, so hand it back for
                    # the caller to preserve as a comment rather than
                    # dropping it outright.
                    #
                    # Labelled by whether a shift actually happened. This
                    # said "de-identified" unconditionally, so exporting
                    # without ever calling anonymize() wrote the
                    # patient's real study date under a comment asserting
                    # it had been de-identified -- a false provenance
                    # claim in the one place a consumer would check.
                    token = shifted_date.strftime("%d/%m/%Y")
                    if getattr(study, "date_shifted", False):
                        return None, f"de-identified start date: {token}"
                    return None, f"start date: {token}"

        return WfdbExporter._instance_only_datetime(instance)


register("wfdb", WfdbExporter)
