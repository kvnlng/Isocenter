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
from ..io_handlers import export_folder_names, format_study_date
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
                  start_datetime=None) -> str:
    """Render a WFDB `.hea` file.

    Emits no `#` comment lines. MIT-BIH convention puts age, sex, and
    diagnosis there, and readers render comments verbatim, so a comment
    line is a PHI escape route. This holds even when the channel
    description (Channel Label/Channel Source, raw DICOM SH text) itself
    contains an embedded CR/LF: `_sanitize_description` collapses any
    line-breaking character to a space before it reaches the signal
    line, so it cannot manufacture a second physical line that a
    conformant reader would surface as a comment.

    Args:
        record_name (str): Record name (must match the .hea basename).
        waveform (Waveform): Geometry and per-channel calibration.
        samples (np.ndarray): int16, shape (num_samples, num_channels).
        dat_filename (str): Signal file basename referenced by each line.
        start_datetime (datetime, optional): Already date-shifted start
            time. Omitted from the record line when None.

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


class WfdbExporter(Exporter):
    """Writes WFDB records for every waveform-bearing instance."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Write WFDB records into `folder`.

        Args:
            session (DicomSession): Active session.
            folder (str): Output root.
            **options: `patient_ids` (list, optional) limits the export.
                `include_annotation_text` (bool, default False) writes
                Unformatted Text Value (0070,0006) into annotations.json
                `note` fields; off by default because it is free-text
                clinical commentary.

        Returns:
            List[str]: Paths of the `.hea` files written.
        """
        logger = get_logger()
        patient_ids = options.get("patient_ids")
        # Off by default: (0070,0006) is free-text clinical commentary.
        include_annotation_text = bool(options.get("include_annotation_text", False))
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
                                used_names, include_annotation_text)
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
                        used_names, include_annotation_text=False):
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

        header = format_header(
            record_name, waveform, samples, dat_filename,
            start_datetime=self._start_datetime(instance, study))

        hea_path = os.path.join(out_dir, f"{record_name}.hea")
        with open(hea_path, "w", encoding="utf-8") as f:
            f.write(header)

        from ..murmur import build_annotations, write_annotations

        try:
            from .. import __version__ as gantry_version
        except ImportError:
            gantry_version = "0.0.0"

        manufacturer = str(instance.attributes.get("0008,0070", "") or "").strip()
        source = f"gantry/{gantry_version}"
        if manufacturer:
            source = f"{source} ({manufacturer})"

        write_annotations(
            os.path.join(out_dir, f"{record_name}.annotations.json"),
            build_annotations(instance, waveform, source, include_annotation_text))

        return hea_path

    @staticmethod
    def _instance_time_of_day(instance):
        """Best-effort time-of-day from the instance's own timestamp tags.

        SHIFT_DATE (see `_start_datetime` below) only ever moves a date,
        never a time-of-day, so the time component always comes from here
        regardless of whether the session has been anonymized.

        Returns a `datetime.time`, or None if no usable value exists.
        """
        from datetime import datetime

        raw = str(instance.attributes.get("0008,002a", "") or "").strip()
        if raw:
            stamp = raw.split("+")[0].split("-")[0].strip()
            for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
                try:
                    return datetime.strptime(stamp, fmt).time()
                except ValueError:
                    continue

        time_part = str(instance.attributes.get("0008,0030", "") or "").strip()
        if time_part:
            stamp = time_part.split(".")[0]
            for fmt in ("%H%M%S", "%H%M"):
                try:
                    return datetime.strptime(stamp, fmt).time()
                except ValueError:
                    continue

        return None

    @staticmethod
    def _instance_only_datetime(instance):
        """Fallback datetime built purely from instance tags.

        Used when no `Study` is available, or the Study has no usable
        date. This is real (possibly un-shifted) timing, and that is
        correct here, not a leak: it mirrors how every other field this
        tool does not touch behaves before `session.anonymize()` runs.
        Suppressing timing on an un-anonymized session would be a design
        change, not a bug fix.
        """
        from datetime import datetime

        raw = str(instance.attributes.get("0008,002a", "") or "").strip()
        if raw:
            stamp = raw.split("+")[0].split("-")[0].strip()
            for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S", "%Y%m%d%H%M"):
                try:
                    return datetime.strptime(stamp, fmt)
                except ValueError:
                    continue

        date_part = str(instance.attributes.get("0008,0020", "") or "").strip()
        time_part = str(instance.attributes.get("0008,0030", "") or "").strip()
        if date_part:
            combined = date_part + (time_part.split(".")[0] or "000000")
            try:
                return datetime.strptime(combined, "%Y%m%d%H%M%S")
            except ValueError:
                return None

        return None

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
        coordinator override): the shipped `gantry/resources/phi_tags.json`
        contains no date tags, so instance-level Acquisition DateTime
        (0008,002A), Study Date (0008,0020), and Study Time (0008,0030)
        are NEVER covered by the default remediation config and are
        NEVER shifted by `session.anonymize()`. The date shift that
        actually runs is a Study-level scan (`gantry/privacy.py`,
        `PhiScanner._scan_study`) whose SHIFT_DATE remediation
        (`gantry/remediation.py`) writes the new date onto
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

        Returns None when no usable value exists anywhere.
        """
        from datetime import datetime

        time_of_day = WfdbExporter._instance_time_of_day(instance)

        if study is not None:
            normalized = format_study_date(getattr(study, "study_date", None))
            if normalized:
                try:
                    shifted_date = datetime.strptime(normalized, "%Y%m%d").date()
                    return datetime.combine(
                        shifted_date, time_of_day or datetime.min.time())
                except ValueError:
                    pass

        return WfdbExporter._instance_only_datetime(instance)


register("wfdb", WfdbExporter)
