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
from ..logger import get_logger
from ..waveform import Waveform

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
    """Reduce a string to characters safe in a WFDB record name."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(name or ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "record"


def record_name_for(patient, study, series, instance) -> str:
    """Build a record name from already-anonymized identifiers.

    Called after anonymization, so `patient.patient_id` is the pseudonym,
    not the source MRN. The instance number disambiguates multiple
    acquisitions within one series.
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


def format_header(record_name: str,
                  waveform: Waveform,
                  samples: np.ndarray,
                  dat_filename: str,
                  start_datetime=None) -> str:
    """Render a WFDB `.hea` file.

    Emits no `#` comment lines. MIT-BIH convention puts age, sex, and
    diagnosis there, and readers render comments verbatim, so a comment
    line is a PHI escape route.

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
        channel = (waveform.channels[idx]
                   if idx < len(waveform.channels)
                   else waveform.channels[-1])
        column = samples[:, idx]

        gain = channel.gain()
        baseline = channel.wfdb_baseline()
        units = channel.units or "mV"

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
            channel.wfdb_description(),
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

        Returns:
            List[str]: Paths of the `.hea` files written.
        """
        logger = get_logger()
        patient_ids = options.get("patient_ids")
        written = []

        for patient in session.store.patients:
            if patient_ids and patient.patient_id not in patient_ids:
                continue

            for study in patient.studies:
                for series in study.series:
                    for instance in series.instances:
                        path = self._write_instance(
                            folder, patient, study, series, instance, logger)
                        if path:
                            written.append(path)

        logger.info(f"WFDB export complete. {len(written)} records written.")
        return written

    def _write_instance(self, folder, patient, study, series, instance, logger):
        """Write one record. Returns the .hea path, or None if not a waveform."""
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

        out_dir = os.path.join(
            folder,
            _sanitize(patient.patient_id),
            _sanitize(study.study_instance_uid),
            _sanitize(series.series_instance_uid),
        )
        os.makedirs(out_dir, exist_ok=True)

        record_name = record_name_for(patient, study, series, instance)
        dat_filename = f"{record_name}.dat"

        # Format 16 is little-endian, channel-interleaved -- identical to the
        # DICOM layout -- so this is a direct write with no transcoding.
        dat_path = os.path.join(out_dir, dat_filename)
        with open(dat_path, "wb") as f:
            f.write(np.ascontiguousarray(samples, dtype="<i2").tobytes())

        header = format_header(
            record_name, waveform, samples, dat_filename,
            start_datetime=self._start_datetime(instance))

        hea_path = os.path.join(out_dir, f"{record_name}.hea")
        with open(hea_path, "w", encoding="utf-8") as f:
            f.write(header)

        return hea_path

    @staticmethod
    def _start_datetime(instance):
        """Record start time. Overridden with shifted timing in Task 9."""
        return None


register("wfdb", WfdbExporter)
