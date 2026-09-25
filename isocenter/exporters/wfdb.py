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
from ..config_manager import _vr_dummy
from ..io_handlers import (ExportError, export_folder_names,
                           format_study_date, LOSS_SCOPE_STANDARD,
                           select_patient_ids, unmatched_patient_ids_sentence)
from ..entities import exported_patient_id
from ..logger import describe_exception_without_paths, get_logger
from ..waveform import Waveform, WaveformChannel

WAVEFORM_SEQUENCE_TAG = "5400,0100"

# WFDB format 16: 16-bit two's complement, little-endian, channel-interleaved.
WFDB_FORMAT = 16
WFDB_ADC_ZERO = 0

# The options `WfdbExporter.export` honours, and the set every other name
# handed to it is measured against. `docs/api/stability.md` freezes both
# names with the method.
#
# This constant is the **only** place in this module the two names are
# spelled as a pair: spelling the allow-list inline in the check would
# leave two lists to keep in step.
#
# The AST pin on the keys the body reads cannot see a name added to this
# frozenset, so `tests/test_wfdb_option_strictness.py` pins the set
# against that page: one pin on what is read, one on what is admitted.
_WFDB_OPTIONS = frozenset({"patient_ids", "include_annotation_text"})


def signal_checksum(channel_samples) -> int:
    """16-bit signed sum of a signal's samples, as `header(5)` defines it.

    Args:
        channel_samples (np.ndarray): 1-D array of int16 samples for one channel.

    Returns:
        int: Checksum in the range [-32768, 32767]; 0 for no samples.
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

    Keeps `[A-Za-z0-9_-]`, collapses every other run to one `_`, and strips
    leading and trailing underscores. Stricter than
    `ConfigLoader.clean_filename`: a record name is a bare token and holds no
    whitespace. For record names only, never folder names.

    Args:
        name: The value to sanitize; None reads as empty, and `0` as "0".

    Returns:
        str: The sanitized token, or `"record"` when nothing is left.
    """
    # The folder names this module writes into come from
    # `export_folder_names` in `io_handlers.py`, which uses
    # `ConfigLoader.clean_filename`, the sanitizer `DicomSession._export_dicom`
    # uses, so the WFDB and DICOM trees land in identical directories. Using
    # this function for folder names would make the two trees diverge.
    # NOTE: `name or ""` would discard a legitimate falsy-but-meaningful
    # value like the int 0 (InstanceNumber 0 is valid DICOM), collapsing
    # it to the "record" fallback below. Test for None explicitly instead.
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(name if name is not None else ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "record"


def record_name_for(patient, study, series, instance) -> str:
    """Build a record name from already-anonymized identifiers.

    Call after anonymization, so `patient.patient_id` is the pseudonym, not
    the source MRN. InstanceNumber is often absent (read as 0), so several
    instances of one series can produce the same name; a caller writing more
    than one instance into a directory must disambiguate the result (see
    `WfdbExporter._unique_record_name`).

    Args:
        patient (Patient): The instance's patient; its exported Patient ID is
            used, never a synthetic no-Patient-ID key.
        study (Study): The instance's study (not used in the name).
        series (Series): The instance's series.
        instance (Instance): The instance.

    Returns:
        str: `<patient>_<series number>_<instance number>`, each part
            sanitized for a record name.
    """
    return "_".join([
        # Never `patient_id` itself: a subject with no Patient ID is keyed
        # on its source Study UID, which must not name a record.
        _sanitize(exported_patient_id(patient)),
        _sanitize(series.series_number if series.series_number is not None else 0),
        _sanitize(instance.instance_number if instance.instance_number is not None else 0),
    ])


def _format_number(value) -> str:
    """Render a float without a trailing '.0', which WFDB readers dislike.

    Args:
        value: A number.

    Returns:
        str: An integer spelling for a whole number, otherwise the repr
            rounded to 6 places.
    """
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

    Every line-breaking character (CR, LF, VT, FF, the file/group/record/unit
    separators, NEL, LINE and PARAGRAPH SEPARATOR) becomes a space; ordinary
    spaces are kept, and the result is stripped.

    Args:
        value (str): The description; any value is converted with `str()`.

    Returns:
        str: Text that cannot form a second physical line.
    """
    # The signal-line description is the LAST field on a `header(5)` signal
    # line and legally runs to end of line, including embedded spaces
    # (PhysioNet's reader parses `... 0 0 Lead I taken by Jane Doe` as one
    # `sig_name`), so spaces must not be stripped. An embedded newline
    # followed by a line starting with `#` is read by `wfdb.rdheader` as a
    # header comment, a PHI escape route; with line breaks collapsed, a `#` in
    # the text stays interior to the signal line.
    return _LINE_BREAK_CHARS.sub(" ", str(value)).strip()


def _sanitize_units(value: str) -> str:
    """Remove ALL whitespace/control characters from the `units` field.

    Args:
        value (str): The units; any value is converted with `str()`.

    Returns:
        str: The units with every whitespace character removed.
    """
    # Unlike the description, `units` is field 3 of 9 inside
    # `gain(baseline)/units`, not the last field on the line, so any whitespace
    # here shifts every field after it: CodeValue "mV per s" would make
    # `wfdb.rdheader` parse `units=['mV']` and `sig_name=['per s 16 0 0 ...']`.
    return re.sub(r"\s+", "", str(value))


def format_header(record_name: str,
                  waveform: Waveform,
                  samples: np.ndarray,
                  dat_filename: str,
                  start_datetime=None,
                  start_date_note: Optional[str] = None) -> str:
    """Render a WFDB `.hea` file.

    Emits no `#` comment lines except `start_date_note`. A channel
    description is sanitized so it cannot manufacture a comment line.

    Args:
        record_name (str): Record name (must match the .hea basename).
        waveform (Waveform): Geometry and per-channel calibration. A
            channel past the defined ones takes the last defined channel's
            calibration. When the Channel Definition Sequence is absent or
            empty, every channel is written uncalibrated (gain 1,
            baseline 0, "mV").
        samples (np.ndarray): int16, shape (num_samples, num_channels).
        dat_filename (str): Signal file basename referenced by each line.
        start_datetime (datetime, optional): Already date-shifted start
            time. Omitted from the record line when None.
        start_date_note (str, optional): The complete comment text
            preserving a start date when `start_datetime` is None because
            no real time-of-day is available -- e.g. Acquisition DateTime
            and Study Time both emptied by the Basic profile. The caller
            supplies the whole string, because the wording is a claim about
            provenance: `de-identified start date: ...` only when the date
            really was shifted, `start date: ...` otherwise. Sanitized like a
            description, and written as one `#` line. Omitted (no comment
            line at all) when None or empty.

    Returns:
        str: Complete header text, newline-terminated.
    """
    # MIT-BIH convention puts age, sex and diagnosis in comments, and readers
    # render comments verbatim, so a comment line is a PHI escape route.
    # `start_date_note` is the one exception: a string the caller computed
    # around a `DD/MM/YYYY` date, not operator-typed text, and it goes through
    # the same `_sanitize_description` as the signal lines. Do not add a
    # second, separately sanitized comment path. The record line never
    # carries a fabricated `00:00:00` (see `WfdbExporter._start_datetime`).
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
        # attacker input, but a second unsanitized comment path would be
        # an injection route.
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

    Args:
        value (str): The TM value; fractional seconds are dropped.

    Returns:
        datetime.time: The time, or None for anything not a legal TM length
            or not parseable.
    """
    # Keyed by length rather than tried longest-format-first: `strptime`
    # accepts one- or two-digit fields and so *succeeds* on the wrong format
    # (`"1430"` matches `%H%M%S` as 14:03:00, and `"14"` matches `%H%M` as
    # 01:04:00).
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

    The offset suffix (`&ZZXX`) and fractional seconds are dropped: the
    timestamp is reported as recorded, with no time-zone conversion.

    Args:
        value (str): The DT value.

    Returns:
        datetime.datetime: The timestamp, or None for anything not a legal DT
            length or not parseable.
    """
    # Keyed by length, for the reason `_parse_dicom_tm` gives.
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


def _real_timing(instance, tag: str) -> str:
    """`tag`'s value on `instance` as timing, or `""` when it is the dummy
    a value-less REPLACE writes on that tag.

    A source that really holds the dummy (`19000101` for DT) reads as no
    timing.

    Args:
        instance (Instance): The instance to read.
        tag (str): The timing tag, e.g. `"0008,002a"`.

    Returns:
        str: The value, or `""`.
    """
    # Acquisition DateTime is X/Z/D in PS3.15 Table E.1-1, so `basic` writes
    # the DT dummy `19000101` there, which `_parse_dicom_dt` reads as
    # 1900-01-01 00:00: without this the record line would carry an invented
    # `00:00:00`. Compared with `config_manager._vr_dummy`, the one table,
    # rather than relying on the parser rejecting a dummy.
    value = str(instance.attributes.get(tag, "") or "")
    return "" if value == _vr_dummy(tag) else value


class WfdbExporter(Exporter):
    """Writes WFDB records for every waveform-bearing instance."""

    def export(self, session, folder: str, **options) -> List[str]:
        """Write WFDB records into `folder`.

        Args:
            session (DicomSession): Active session.
            folder (str): Output root.
            **options: `patient_ids` (iterable, optional) limits the
                export to those patients, read exactly as the `dicom`
                format reads it (`io_handlers.select_patient_ids`). Only
                `None`, or the option omitted, means every patient: an
                empty list, tuple or set is a filter that selected
                nobody and the export writes nothing. An iterator
                is materialised, so a generator is not consumed by the
                first patient walked. An ID no patient holds is counted,
                never named, in one `WARNING` log line and one `WARNING`
                audit row, and the matching patients are written.
                `include_annotation_text` (bool, default False) releases
                the operator-typed text in annotations.json: Unformatted
                Text Value (0070,0006) into `note`, and a site-defined
                Concept Name's Code Meaning into `label` with its Code
                Value as `category`. Off by default because both are free
                text; pass it when the study protocol permits their
                release. Concepts from a published coding scheme are
                unaffected either way.

                Those two are the whole set. Any other name raises
                `TypeError` before anything is written -- see `Raises:`
                below.

        Returns:
            List[str]: Paths of the `.hea` files written. Empty when the
            export attempted nothing -- no waveform instances in scope,
            only waveforms with no samples, each of which files its
            own `STANDARD` `DATA_LOSS` row, or a `patient_ids`
            that selected no patient in the store. A partial export
            returns the records that did reach disk.

        Raises:
            io_handlers.ExportError: When at least one record was
                attempted and none was written, raised last, after every
                `ERROR` row and the `EXPORT` row, with or without a store
                behind the session. `.failures` names each record as
                `(uid, detail)` and `.attempted` counts the records that
                failed.
            TypeError: If `options` carries any name outside
                `_WFDB_OPTIONS`. Nothing is written when this raises.
                Also when `patient_ids` is a bare `str` (wrap one ID in a
                list), bytes-like, not iterable, or holds an element that
                is not a `str`.
        """
        logger = get_logger()
        # First thing, before `patient_ids` is read and before any file
        # is written: a refusal raised after the walk arrives with the
        # cohort already on disk.
        #
        # `TypeError`, not `ValueError`: it is what Python raises for an
        # unexpected keyword, and it is what the `dicom` path raises for
        # this exact mistake, because `_export_dicom` has a real
        # signature. The two formats must agree on a mistyped option.
        #
        # `sorted`, so the message is deterministic and a test can assert
        # on it.
        unknown = sorted(set(options) - _WFDB_OPTIONS)
        if unknown:
            raise TypeError(
                f"export(format='wfdb') got unexpected keyword argument(s) "
                f"{', '.join(repr(name) for name in unknown)}; the wfdb "
                f"options are "
                f"{', '.join(repr(name) for name in sorted(_WFDB_OPTIONS))}.")
        # Straight after the unknown-option refusal above and before any
        # file is written, for the same reason: a refusal raised later
        # arrives with records already on disk. `select_patient_ids` is the
        # one reading of this option, shared with
        # `DicomSession._export_dicom` and `get_cohort_report` -- see
        # `io_handlers.normalize_id_filter` for what it refuses.
        #
        # `store_backend` is read first so the count below can write its
        # row, and is passed down to `_write_instance` explicitly rather
        # than read off `session` there, so the one place that writes an
        # audit entry names its dependency instead of reaching back
        # through the facade. `None` is a legitimate value:
        # `_write_instance` is called directly by tests with no session
        # behind it.
        store_backend = getattr(session, "store_backend", None)
        selection = select_patient_ids(options.get("patient_ids"),
                                       session.store.patients)
        patient_ids = selection.ids
        # Straight after the selection: nothing can refuse between here and
        # the walk, so the export this row describes is certain to run.
        # Counted and never named, one row per export; `WARNING`, so a
        # short export grades `REVIEW_REQUIRED` rather than PASS.
        if selection.unmatched:
            sentence = unmatched_patient_ids_sentence(selection)
            logger.warning(sentence)
            if store_backend is not None:
                store_backend.log_audit(
                    action_type="WARNING", entity_uid=folder,
                    details=f"WFDB export to {folder}: {sentence}")
        # Off by default: (0070,0006) is free-text clinical commentary, and
        # a site-defined Concept Name's Code Meaning is operator-typed too.
        # This is the auditor's override, not a debug switch -- it says the
        # protocol permits releasing that text.
        include_annotation_text = bool(options.get("include_annotation_text", False))

        # The notice for statuses recorded under another policy, over the
        # instances this export will attempt: the ones in the selected
        # patients that hold a waveform. A CT slice
        # sharing a series with an ECG is written nowhere, so its status is
        # not this export's to report (`_write_instance`'s first arm).
        # After the option checks above, so a refused call says nothing;
        # through `getattr` because a caller may hand in a session-like
        # object that is not a `Session`.
        report_policies = getattr(
            session, "_report_statuses_under_another_policy", None)
        if report_policies is not None:
            report_policies(
                [(patient, study, instance)
                 for patient in session.store.patients
                 if patient_ids is None or patient.patient_id in patient_ids
                 for study in patient.studies
                 for series in study.series
                 for instance in series.instances
                 if getattr(instance.sequences.get(WAVEFORM_SEQUENCE_TAG),
                            "items", None)],
                folder, "WFDB")
        written = []
        failed = 0
        # `(uid, detail)` per failed record, the shape `ExportError`
        # carries. `failed` alone could say that nothing was
        # delivered but not which records, which is all a caller who
        # catches the exception can act on.
        failures = []
        used_names = {}  # out_dir -> set of record names already claimed

        for patient in session.store.patients:
            # `is not None`, never a truthiness test: an empty container
            # is a filter that selected nobody, and a truthiness test
            # would read it as no filter and write the whole cohort. The
            # siblings that answer the same question are spelled the same
            # way: `_export_dicom`, `Session.get_cohort_report`, and
            # `SqliteStore._iter_flattened_instances`.
            if patient_ids is not None and patient.patient_id not in patient_ids:
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
                        #
                        # Containment is only half the pattern: like the
                        # DICOM path (`DicomExporter._report_export_failures`),
                        # each failure also records an `ERROR` audit row,
                        # so `get_audit_errors()` and the report see it and
                        # the run does not grade PASS. A returned list one
                        # entry short is not a channel: nothing compares
                        # its length against the graph.
                        try:
                            path = self._write_instance(
                                folder, patient, study, series, instance, logger,
                                used_names, include_annotation_text,
                                store_backend)
                        except Exception as e:
                            failed += 1
                            # Fall back the way `_report_export_failures`
                            # does. `sop_instance_uid` may be `None`, and
                            # sqlite stores that without complaint -- a
                            # row nobody can look up is barely better
                            # than no row.
                            uid = instance.sop_instance_uid or "UNKNOWN"
                            # Never `{e}`: an OSError's text ends in the
                            # path it failed on, and a record path carries
                            # the Patient ID into a persisted row and the
                            # report. The type leads, as every other
                            # recorded reason does.
                            detail = (f"WFDB export failed for instance "
                                      f"{uid}: "
                                      f"{describe_exception_without_paths(e)}")
                            # Flattened and pipe-escaped before it is
                            # recorded, as `io_handlers.py` does for the
                            # same reason: an arbitrary exception's
                            # `str()` can carry newlines and pipes, and
                            # this renders straight into a markdown table
                            # row in the compliance report. The
                            # neighbouring `DATA_LOSS` site does not
                            # flatten because it composes its own
                            # single-line detail; this one cannot know
                            # what it is holding.
                            detail = " ".join(detail.split()).replace(
                                "|", "\\|")
                            failures.append((uid, detail))
                            logger.error(detail)
                            if store_backend is not None:
                                # `log_audit`, never `log_audit_batch` --
                                # the reason is at
                                # `DicomExporter._report_export_losses`:
                                # the batch method writes straight to the
                                # database while the audit writer thread
                                # is live and swallows `sqlite3.Error`
                                # into a log line, so contention would
                                # lose the very entry that exists because
                                # a log line was not enough.
                                store_backend.log_audit(
                                    action_type="ERROR", entity_uid=uid,
                                    details=detail)
                            continue
                        if path:
                            written.append(path)

        logger.info(f"WFDB export complete. {len(written)} records written.")
        # Every export run writes one EXPORT row, whatever the
        # format: `generate_report` keys its export boundary on the
        # row's absence, so a format that skipped it would tell
        # its users their finished export never happened.
        #
        # It names the failure count as well as the written one.
        # "wrote 8 records" cannot be read as "8 of 10" or as "8 of 8",
        # and the whole-run question is the one a reader asks first --
        # the per-instance rows above answer *which*, this answers
        # *whether*. It is stated on every run, including a clean one,
        # so `0 instances failed` is a fact rather than an absence.
        if store_backend is not None:
            store_backend.log_audit(
                action_type="EXPORT",
                entity_uid=folder,
                details=(f"WFDB export to {folder}: wrote {len(written)} "
                         f"{'record' if len(written) == 1 else 'records'}, "
                         f"{failed} "
                         f"{'instance' if failed == 1 else 'instances'} "
                         f"failed."))
        # Last, after every ERROR row and the EXPORT row, for the reason
        # `_export_dicom` raises there: a caller who catches this still
        # holds a complete audit trail and a report grading
        # REVIEW_REQUIRED.
        #
        # `failed and not written`, never `not written` alone: a store
        # with no waveform instances, or waveforms with no samples (a
        # skip with its own STANDARD row), attempted nothing and
        # `[]` is the truth about it. Never `failed` alone either: a
        # partial export is a real result, and raising would discard the
        # list naming what did reach disk. And not guarded on
        # `store_backend`: the exception is the caller's channel, not
        # the store's, so a session-less caller is owed it too.
        if failed and not written:
            raise ExportError(failures, failed, folder)
        return written

    @staticmethod
    def _unique_record_name(base_name, out_dir, used_names):
        """Disambiguate record names that collide within one output directory.

        Appends `_2`, `_3`, ... until the name is unused in `out_dir`,
        deterministically in write order, and records the name it returns.

        Args:
            base_name (str): The name `record_name_for` built.
            out_dir (str): The directory the record is written into.
            used_names (dict): `{out_dir: set of names}`, updated in place.

        Returns:
            str: A name unique within `out_dir`.
        """
        # `record_name_for` derives its instance component from InstanceNumber,
        # which is often absent and read as 0, so two instances can propose the
        # same name in one directory and would overwrite each other's `.hea`/`.dat`
        # files without this.
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
        """Write one record: `.hea`, `.dat`, and `annotations.json` when there are
        findings.

        A waveform instance with no samples writes nothing, logs one
        WARNING and files one `STANDARD` `DATA_LOSS` row; dropped
        annotations log one WARNING and file one `DATA_LOSS` row. Each row
        is written only when `store_backend` is given.

        Args:
            folder (str): The export root.
            patient (Patient): The instance's patient.
            study (Study): The instance's study, the source of the start date.
            series (Series): The instance's series.
            instance (Instance): The instance to write.
            logger (logging.Logger): Where warnings go.
            used_names (dict): Record names already claimed, per directory.
                Required, never defaulted.
            include_annotation_text (bool): Passed to `build_annotations`.
            store_backend: The store audit rows go to, or None to write none.

        Returns:
            Optional[str]: The `.hea` path, or None if the instance holds no
                waveform or no samples.
        """
        # `used_names` is required, not `=None`: it is what `_unique_record_name`
        # deduplicates against, and without it two instances missing
        # InstanceNumber would write the same record name and the second would
        # overwrite the first. A caller that forgets it gets a `TypeError`.
        seq = instance.sequences.get(WAVEFORM_SEQUENCE_TAG)
        if seq is None or not seq.items:
            # An ordinary non-waveform instance -- a CT slice sharing a
            # series with an ECG. It was never asked to become a record,
            # so it is not loss and files nothing. Deliberately NOT
            # merged with the arm below: both return `None` and the loop
            # cannot tell them apart, so an emitter placed here would
            # write one `DATA_LOSS` row per slice and bury the real ones.
            return None

        samples = instance.get_waveform_data()
        if samples is None or samples.size == 0:
            # This one *is* loss. The instance declared a waveform and
            # produced no record: `written` does not hold it, `failed`
            # does not count it (nothing raised), and the `EXPORT` row's
            # "0 instances failed" is therefore true while a record the
            # run was asked for is missing. This row is how it is found.
            #
            # **`STANDARD` and not `SIGNAL`.** `LOSS_SCOPE_SIGNAL` is in
            # `GRADED_LOSS_SCOPES` and takes `validation_status` to
            # `REVIEW_REQUIRED`; this skip is deliberate. Reported, not
            # graded.
            #
            # **The detail is about this export, not about the source.**
            # `get_waveform_data()` returns `None` when nothing was ever
            # ingested, which from here is indistinguishable from a
            # source that carried a Waveform Sequence with no samples --
            # so claiming the source *had* samples would assert what this
            # frame cannot establish (the shape `persistence.py` refuses
            # when it declines to back-fill `loss_scope` from `details`).
            # The two arms do differ in what they know, and say only
            # that.
            #
            # This row makes the loss findable; it does not make the
            # `EXPORT` line sum, which would need a third counter.
            # `entity_uid` is the locating column of the compliance
            # report's section 3.1 table, so it chains the way
            # `_report_export_losses` chains its own `DATA_LOSS` rows
            # (`r.sop_instance_uid or r.output_path`) rather than
            # collapsing straight to `"UNKNOWN"` like the `except` arm
            # above -- that arm falls back on a worker result that may
            # carry nothing at all, and this one has an instance in hand.
            # A row keyed `UNKNOWN` is a row nobody can look up, and a
            # second UID-less instance in the same run would file a
            # second one indistinguishable from the first.
            uid = (instance.sop_instance_uid or instance.source_path
                   or "UNKNOWN")
            cause = ("nothing is held for this instance" if samples is None
                     else "a loader produced an empty array")
            # Single line and no `|`: this renders straight into a
            # markdown table cell in the compliance report. Composed here
            # rather than flattened after the fact, like the neighbouring
            # `DATA_LOSS` emitter and unlike the `except` arm, which
            # cannot know what an arbitrary exception's `str()` holds.
            detail = (
                "Declared a Waveform Sequence but no sample data reached "
                f"this export ({cause}), so no record was written.")
            logger.warning(f"{uid}: {detail}")
            if store_backend is not None:
                # `log_audit`, never `log_audit_batch` -- the reason is
                # at the loop's `except` arm above.
                store_backend.log_audit(
                    action_type="DATA_LOSS",
                    entity_uid=uid,
                    details=detail,
                    loss_scope=LOSS_SCOPE_STANDARD)
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

        # Warn-plus-audit, the shape of the multiplex discard itself: an
        # annotation dropped without a word is as wrong as a mislabelled
        # one.
        #
        # ONE row per instance, naming the count and the groups -- not
        # one per annotation, as the multiplex discard reports "carried N
        # groups; kept group 0 and discarded N-1" once. A cart that marks
        # forty beats on a discarded group would
        # otherwise put forty near-identical rows into section 3 of the
        # compliance report, and a section nobody can read reports
        # nothing. Group ordinals are deduplicated but the annotation
        # count is not, because they answer different questions: which
        # signal was referenced, and how much was dropped.
        #
        # Scoped STANDARD, though the group discard itself is graded
        # SIGNAL. An annotation is a mark *about* the signal,
        # not the signal: the acquired-samples loss these annotations
        # described already costs the run its PASS via the ingest-side
        # multiplex row, and grading the bookkeeping that follows from
        # it would double-charge one loss under two rows.
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

        Acquisition DateTime (0008,002A) unless it is the dummy, else Study Time
        (0008,0030).

        Args:
            instance (Instance): The instance to read.

        Returns:
            datetime.time: The time of day, or None if no usable value exists.
        """
        # SHIFT_DATE (see `_start_datetime`) only ever moves a date, never a
        # time-of-day, so the time component always comes from here, anonymized or
        # not.
        acquired = _parse_dicom_dt(
            _real_timing(instance, "0008,002a"))
        if acquired is not None:
            return acquired.time()

        return _parse_dicom_tm(
            str(instance.attributes.get("0008,0030", "") or ""))

    @staticmethod
    def _instance_only_datetime(instance):
        """Fallback timing built purely from instance tags.

        Used when no `Study` is available, or the Study has no usable date. This
        is real (possibly un-shifted) timing. Reads Acquisition DateTime
        (0008,002A) unless it is the dummy, else Study Date (0008,0020) plus
        Study Time (0008,0030), each parsed on its own. A date with no readable
        time of day is never given a `00:00:00`.

        Args:
            instance (Instance): The instance to read.

        Returns:
            tuple[Optional[datetime], Optional[str]]: the record-line
                value, or None with a note (`start date: DD/MM/YYYY`) for
                the caller to write as a comment when a date is known but
                no time of day is.
        """
        # Real, possibly un-shifted timing on purpose, not a leak: it
        # behaves like every other field this tool has not remediated.
        # Suppressing timing on an un-anonymized session would be a design
        # change, not a fix.
        from datetime import datetime

        acquired = _parse_dicom_dt(
            _real_timing(instance, "0008,002a"))
        if acquired is not None:
            return acquired, None

        date_part = str(instance.attributes.get("0008,0020", "") or "").strip()
        if not date_part:
            return None, None

        try:
            only_date = datetime.strptime(date_part, "%Y%m%d").date()
        except ValueError:
            return None, None

        # Parsed on its own, not concatenated onto the date: a combined
        # stamp tried only as `%Y%m%d%H%M%S` would fail on a four-digit
        # Study Time and take the date down with it.
        time_of_day = _parse_dicom_tm(
            str(instance.attributes.get("0008,0030", "") or ""))
        if time_of_day is not None:
            return datetime.combine(only_date, time_of_day), None

        # A date, and no time of day we can read. Never append `000000`:
        # a record line carrying `00:00:00` is timing this tool invented,
        # indistinguishable to a reader from an acquisition that really
        # happened at midnight. The date is kept as a note rather than
        # dropped because it is useful for research.
        return None, f"start date: {only_date.strftime('%d/%m/%Y')}"

    @staticmethod
    def _start_datetime(instance, study):
        """Record start time, read after de-identification.

        The date comes from `study.study_date`, the field SHIFT_DATE writes; the
        time of day from the instance (`_instance_time_of_day`). Falls back to
        `_instance_only_datetime` when `study` is None or has no usable
        `study_date`. Never fabricates a time of day: with a usable study date
        and no real time of day, the record line's time and date fields are both
        omitted and the date is returned as a note instead.

        Args:
            instance (Instance): The instance to read.
            study (Study): The instance's study, or None explicitly for the "no
                study available" fallback. Required, never defaulted.

        Returns:
            tuple[Optional[datetime], Optional[str]]: `(start_datetime,
                start_date_note)`. `start_datetime` is the record-line value, or
                None. `start_date_note` is None whenever `start_datetime` is set
                or no date is known; otherwise it is the complete comment text
                around a `DD/MM/YYYY` date: `de-identified start date: ...` when
                `study.date_shifted` is true, `start date: ...` when the study
                date was not shifted or came from the instance-only fallback.
        """
        # `study` is required: without it this would read the instance's own date
        # tags, which SHIFT_DATE never writes, and leak the unshifted date past
        # `session.anonymize()`. header(5) cannot carry a date without a time
        # (`wfdb` requires base_time for base_date, and `wfdb.rdheader` misparses a
        # date-only record line), so a date with no time goes in the note. That
        # case never falls through to the instance-only fallback, which would read
        # the unshifted instance date. The fallback's real timing on an
        # un-anonymized session is deliberate (see the comment at the top of
        # `_instance_only_datetime`).
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
                    # the comment at the top of this function describes.
                    # The date is kept as a note rather than dropped
                    # because it is useful for research.
                    #
                    # Labelled by whether a shift actually happened: a real
                    # study date exported without `anonymize()` must not be
                    # labelled de-identified.
                    token = shifted_date.strftime("%d/%m/%Y")
                    if getattr(study, "date_shifted", False):
                        return None, f"de-identified start date: {token}"
                    return None, f"start date: {token}"

        return WfdbExporter._instance_only_datetime(instance)


register("wfdb", WfdbExporter)
