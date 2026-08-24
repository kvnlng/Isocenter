"""
IO Handlers for Gantry.

This module provides classes for:
- DicomStore: The central catalog of DICOM objects.
- DicomImporter: Parallel file ingestion.
- DicomExporter: Writing DICOM files to disk.
- SidecarPixelLoader: Lazy loading of pixel data.
- SidecarWaveformLoader: Lazy loading of waveform samples.
"""

import os
import pickle
import sys
import shutil
import hashlib
import io
import base64
from typing import List, Set, Dict, Any, Optional, Tuple, NamedTuple, Iterable
from datetime import datetime, date
import json
from dataclasses import dataclass, field

import pydicom
import numpy as np
try:
    from PIL import Image
except ImportError:
    Image = None
from tqdm import tqdm
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, UncompressedTransferSyntaxes, JPEG2000Lossless
from pydicom.tag import Tag
from pydicom.datadict import dictionary_VR
try:
    from pydicom.encapsulate import encapsulate
except ImportError:
    from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence
from pydicom.dataset import Dataset

from .entities import Patient, Study, Series, Instance, Equipment, DicomItem, DicomSequence
from .logger import get_logger
from .parallel import run_parallel
from .validation import IODValidator
from .sidecar import SidecarManager


from .store import DicomStore


def populate_attrs(ds: Any, item: "DicomItem", text_index: list = None):
    """
    Standalone function to populate attributes for pickle-compatibility in workers.

    Extracts standard DICOM elements from a pydicom Dataset and populates the
    Gantry DicomItem. Handles Sequences recursively. Skips large binary blobs
    (PixelData, Overlays) to keep the object graph lightweight.

    Args:
        ds: The pydicom Dataset or Sequence Item.
        item (DicomItem): The Gantry item to populate.
        text_index (list, optional): A list to append (item, tag) tuples for text indexing.
    """

    # Text-like VRs that might contain PHI
    TEXT_VRS = {'PN', 'LO', 'SH', 'ST', 'LT', 'UT', 'DA', 'DT', 'TM'}
    # Binary VRs to explicitly skip (Metadata Refactor)
    # UN left out for safety, usually small private tags
    BINARY_VRS = {'OB', 'OW', 'OF', 'OD', 'OL'}

    for elem in ds:
        if elem.tag.group == 0x7fe0:
            continue  # Skip pixels
        if elem.VR in BINARY_VRS:
            continue  # Skip binary blobs

        tag = f"{elem.tag.group:04x},{elem.tag.element:04x}"

        if elem.VR == 'SQ':
            process_sequence(tag, elem, item, text_index)
        elif elem.VR == 'PN':
            # Sanitize PersonName for pickle safety
            val = str(elem.value)
            item.set_attr(tag, val)
            if text_index is not None:
                text_index.append((item, tag))
        else:
            item.set_attr(tag, elem.value)
            # Index if text
            if text_index is not None and elem.VR in TEXT_VRS:
                text_index.append((item, tag))


def process_sequence(tag, elem, parent_item, text_index: list = None):
    """Recursively parses Sequence (SQ) items."""
    for ds_item in elem:
        seq_item = DicomItem()
        populate_attrs(ds_item, seq_item, text_index)
        parent_item.add_sequence_item(tag, seq_item)


def ingest_worker(fp: str) -> Tuple:
    """
    Worker function to read DICOM and construct Instance object.

    Designed for parallel execution. Reads a file, extracts metadata, constructs
    an Instance object, and optionally extracts raw pixel data and raw waveform
    data for eager sidecar loading.

    Args:
        fp (str): File path to read.

    Returns:
        tuple: (metadata_dict, instance_object, pixel_bytes, pixel_hash,
        pixel_alg, waveform_bytes, waveform_hash, error_string)
    """
    try:
        # Eager load (read pixels)
        ds = pydicom.dcmread(fp, stop_before_pixels=False, force=True)

        # Determine SOP Class UID with fallback to File Meta
        sop_class = str(ds.get("SOPClassUID", ""))
        if not sop_class and "MediaStorageSOPClassUID" in ds.file_meta:
            sop_class = str(ds.file_meta.MediaStorageSOPClassUID)

        # Extract Linking Metadata
        meta = {
            'pid': ds.get("PatientID", "UnknownPatient"),
            'pname': str(ds.get("PatientName", "Unknown")),
            'sid': ds.get("StudyInstanceUID", "UnknownStudy"),
            'sdate': str(ds.get("StudyDate", "19000101")),
            'ser_id': ds.get("SeriesInstanceUID", "UnknownSeries"),
            'modality': ds.get("Modality", "OT"),
            'sop': ds.get("SOPInstanceUID", None),
            'sop_class': sop_class,
            'man': ds.get("Manufacturer", ""),
            'model': ds.get("ManufacturerModelName", ""),
            'dev_sn': ds.get("DeviceSerialNumber", ""),
            'series_num': ds.get("SeriesNumber", 0)
        }

        if not meta['sop']:
            raise ValueError("Missing SOPInstanceUID. Likely not a valid DICOM file.")

        # Construct Instance (Metadata Only)
        inst = Instance(meta['sop'], meta['sop_class'], 0, file_path=fp)
        populate_attrs(ds, inst, inst.text_index)

        # Gantry internally manages pixels as standard contiguous arrays (Interleaved)
        # So we MUST ensure PlanarConfiguration=0 in metadata to match our converted data
        if inst.attributes.get("0028,0006") == 1:
            inst.set_attr("0028,0006", 0)

        # Extract & Process Pixel Data
        p_bytes = None
        p_hash = None
        p_alg = None

        if "PixelData" in ds:
            try:
                # Always decompress to raw bytes to ensure sidecar has consistent format (SidecarPixelLoader expects raw)
                # This handles RLE/JPEG/J2K by decoding them now.
                arr = np.ascontiguousarray(ds.pixel_array)
                p_bytes = arr.tobytes()
                p_alg = 'zlib'  # Always compress the raw bytes
            except Exception as e:
                # If decompression fails (missing codec), we cannot ingest safely for sidecar usage.
                # Could log warning, but for now raise or return error.
                return (None, None, None, None, None, None, None,
                        f"Decompression Failed: {e}")

            if p_bytes:
                # Hash the RAW bytes (stable hash)
                p_hash = hashlib.sha256(p_bytes).hexdigest()

        # Extract Waveform Data
        # populate_attrs skips all OB/OW VRs, so (5400,1010) never reaches the
        # object graph on its own. Pull it out explicitly, exactly as PixelData
        # is handled above, and offload the bytes to the sidecar.
        # Only the first Waveform Sequence item is handled; multi-item
        # sequences (e.g. multiplexed rhythm + median) keep item 0 only.
        w_bytes = None
        w_hash = None

        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            wf_item = ds.WaveformSequence[0]
            raw = getattr(wf_item, "WaveformData", None)
            if raw:
                w_bytes = bytes(raw)
                w_hash = hashlib.sha256(w_bytes).hexdigest()

        return (meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, None)
    except Exception as e:
        return (None, None, None, None, None, None, None, str(e))


class DicomImporter:
    """
    Handles scanning of folders/files and ingesting them into the Object Graph.

    Optimized for parallel processing using `run_parallel` and Eager Ingestion methods.
    """
    @staticmethod
    def import_files(file_paths: List[str], store: DicomStore, executor=None,
                     sidecar_manager=None, store_backend=None):
        """
        Parses a list of files or directories. Recurses into directories to find all files.

        Identifies new files (not already in the store), reads them in parallel,
        and links them into the provided DicomStore's hierarchy (Patient/Study/Series).

        Args:
            file_paths (List[str]): List of file or directory paths to scan.
            store (DicomStore): The active store to populate.
            executor (optional): Shared ProcessPoolExecutor.
            sidecar_manager (optional): Manager for persisting pixel data immediately.
            store_backend (optional): SqliteStore used to register sidecar
                blob references. Waveform blobs are invisible to compaction
                unless recorded here.
        """
        all_files = []
        for path in file_paths:
            if os.path.isfile(path):
                all_files.append(path)
            elif os.path.isdir(path):
                for root, _, filenames in os.walk(path):
                    for filename in filenames:
                        if filename.startswith('.'):
                            continue
                        all_files.append(os.path.join(root, filename))

        known_files = store.get_known_files()
        new_files = [fp for fp in all_files if os.path.abspath(fp) not in known_files]

        logger = get_logger()
        skipped_count = len(all_files) - len(new_files)
        if skipped_count > 0:
            logger.info(f"Skipping {skipped_count} already imported files.")

        if not new_files:
            return

        logger.info(f"Importing {len(new_files)} files (Parallel Eager Ingest)...")

        # 1. Build Fast Lookup Maps (O(1))
        patient_map = {p.patient_id: p for p in store.patients}
        study_map = {}  # Key: study_uid -> Study
        series_map = {}  # Key: series_uid -> Series

        # Populate deep maps
        for p in store.patients:
            for st in p.studies:
                study_map[st.study_instance_uid] = st
                for se in st.series:
                    series_map[se.series_instance_uid] = se

        # 2. Parallel Execution
        # OPTIMIZATION: Use return_generator=True to stream results.
        # This prevents accumulating result tuples (with huge p_bytes) in a list (O(N) memory).
        # We process each result immediately and discard it (O(1) memory).
        # OPTIMIZATION: chunksize=1 to prevent buffering multiple large files in IPC queue
        results = run_parallel(
            ingest_worker,
            new_files,
            desc="Ingesting",
            chunksize=1,
            executor=executor,
            return_generator=True)

        # 3. Aggregation (Streaming)
        count = 0
        for meta, inst, p_bytes, p_hash, p_alg, w_bytes, w_hash, err in results:
            # Clear result components from scope as soon as possible after use to help GC
            # But the loop variable holds them. Next iteration clears them.
            if err:
                logger.error(f"Import Failed: {err}")
                continue
            if inst:
                try:
                    # Persist Pixels to Sidecar (Main Thread Sequential Write)
                    if p_bytes and sidecar_manager:
                        off, leng = sidecar_manager.write_frame(p_bytes, p_alg)
                        inst._pixel_loader = SidecarPixelLoader(
                            sidecar_manager.filepath, off, leng, p_alg, instance=inst)
                        inst._pixel_hash = p_hash

                    # Persist Waveform Samples to Sidecar
                    if w_bytes and sidecar_manager:
                        w_off, w_len = sidecar_manager.write_frame(w_bytes, 'zlib')
                        inst._waveform_hash = w_hash
                        inst._waveform_loader = SidecarWaveformLoader(
                            sidecar_manager.filepath, w_off, w_len, 'zlib',
                            instance=inst, waveform_hash=w_hash)

                        # Unlike pixels, waveform offsets have no column on
                        # `instances`, so the blob table is their only record.
                        # Skipping this makes compaction reclaim them.
                        #
                        # Called without `conn=`: this loop runs outside any
                        # open SqliteStore transaction, so record_blob_ref is
                        # free to open (and commit) its own connection here.
                        if store_backend is not None:
                            store_backend.record_blob_ref(
                                inst.sop_instance_uid, 'waveform',
                                w_off, w_len, w_hash, 'zlib')

                    # Linkage Logic
                    pid = meta['pid']
                    sid = meta['sid']
                    ser_id = meta['ser_id']

                    # Patient
                    pat = patient_map.get(pid)
                    if not pat:
                        pat = Patient(pid, meta['pname'])
                        store.patients.append(pat)
                        patient_map[pid] = pat

                    # Study
                    study = study_map.get(sid)
                    if not study:
                        # Parse date carefully or use fallback
                        try:
                            sdate = datetime.strptime(meta['sdate'], "%Y%m%d").date()
                        except BaseException:
                            sdate = date(1900, 1, 1)

                        study = Study(sid, sdate)
                        pat.studies.append(study)
                        study_map[sid] = study

                    # Series
                    series = series_map.get(ser_id)
                    if not series:
                        series = Series(ser_id, meta['modality'], meta['series_num'])
                        if meta['man'] or meta['model']:
                            series.equipment = Equipment(meta['man'], meta['model'], meta['dev_sn'])
                        study.series.append(series)
                        series_map[ser_id] = series

                    # Instance
                    series.instances.append(inst)
                    count += 1
                except Exception as e:
                    logger.error(f"Linkage Failed: {e}")

        logger.info(f"Successfully ingested {count} instances.")


@dataclass
class ExportContext:
    instance: Instance
    output_path: str
    patient_attributes: Dict[str, Any]
    study_attributes: Dict[str, Any]
    series_attributes: Dict[str, Any]
    pixel_array: Optional[Any] = None  # Numpy array or None
    compression: Optional[str] = None  # 'j2k' or None
    # Zero-Copy Sidecar Support
    sidecar_path: Optional[str] = None
    pixel_offset: Optional[int] = None
    pixel_length: Optional[int] = None
    pixel_alg: Optional[str] = None
    redaction_zones: List[Tuple] = field(default_factory=list)


def _export_instance_worker(ctx: ExportContext) -> Optional[bool]:
    """
    Worker function to export a single instance.

    Reconstructs a pydicom Dataset from the ExportContext (Instance + Attributes)
    and saves it to disk. Handles optional compression (JPEG2000).

    Args:
        ctx (ExportContext): The context/request for export.

    Returns:
        Optional[bool]: True on success, None (and prints error) on failure.
    """

    try:
        inst = ctx.instance
        ds = DicomExporter._create_ds(inst)

        # 0. Base Attributes
        DicomExporter._merge(ds, inst.attributes)
        DicomExporter._merge_sequences(ds, inst.sequences)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes)
        # 0. Base Attributes
        DicomExporter._merge(ds, inst.attributes)
        DicomExporter._merge_sequences(ds, inst.sequences)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes)

        # 2. Study Level
        DicomExporter._merge(ds, ctx.study_attributes)

        # 3. Series Level
        DicomExporter._merge(ds, ctx.series_attributes)

        # 4. Instance defaults helper
        populate_attrs(ds, inst)

        # Handle Pixel Data
        # If we have modified pixels in memory (redaction), we MUST use them.
        # If they were unloaded, we load them.
        arr = inst.pixel_array

        if arr is None:
            try:
                arr = inst.get_pixel_data()
            except FileNotFoundError:
                # Check Modality to decide if we should fail or proceed
                # Image implementations MUST have pixels.
                # Non-image (SR, PR, KO, DOC) can proceed without.
                mod = inst.attributes.get("0008,0060", "OT")
                IMAGE_MODALITIES = {"CT", "MR", "US", "DX", "CR",
                                    "MG", "NM", "PT", "XA", "RF", "SC", "OT"}

                # If it claims to be an image but has no pixels, fail hard (Safety)
                if mod in IMAGE_MODALITIES:
                    raise RuntimeError(f"Pixels missing for Image Modality {mod}")

                # Otherwise (SR, etc.), proceed
                arr = None

        if arr is not None:
            # APPLY REDACTION (Fix for Export Compression Bug)
            if ctx.redaction_zones:
                # Local import to avoid circular dependency
                from .services import RedactionService

                # Check writeability
                if not arr.flags.writeable:
                    arr = arr.copy()

                # Apply zones
                RedactionService.apply_redaction_to_array(arr, ctx.redaction_zones)

            # MEMORY OPTIMIZATION:
            # If compression is requested, DO NOT convert to bytes here.
            # Pass the numpy array to _finalize_dataset -> _compress_j2k directly.
            # Only set PixelData if NOT compressing.

            if not ctx.compression:
                ds.PixelData = arr.tobytes()

            # Recalculate dimensions based on array shape
            # Logic mirrored from Instance.set_pixel_data
            shape = arr.shape
            ndim = len(shape)

            rows, cols = 0, 0
            # defaults
            if ndim == 2:
                rows, cols = shape
            elif ndim == 3:
                if shape[-1] in [3, 4]:
                    rows, cols, _ = shape
                else:
                    _, rows, cols = shape
            elif ndim == 4:
                _, rows, cols, _ = shape  # frames, rows, cols, samples

            if rows > 0 and cols > 0:
                ds.Rows = rows
                ds.Columns = cols

            ds.SamplesPerPixel = inst.attributes.get("0028,0002", 1)
            ds.PhotometricInterpretation = inst.attributes.get("0028,0004", "MONOCHROME2")

            if arr.itemsize == 1:
                default_bits = 8
            else:
                default_bits = 16

            ds.BitsAllocated = inst.attributes.get("0028,0100", default_bits)
            ds.BitsStored = inst.attributes.get("0028,0101", default_bits)
            ds.HighBit = inst.attributes.get("0028,0102", default_bits - 1)
            ds.PixelRepresentation = inst.attributes.get("0028,0103", 0)

        if "_GANTRY_REDACTION_HASH" in ds:
            del ds["_GANTRY_REDACTION_HASH"]

        # Validate & Save
        ds = DicomExporter._finalize_dataset(ds, ctx.compression, pixel_array=arr)

        # Ensure dir exists (race safe)
        os.makedirs(os.path.dirname(ctx.output_path), exist_ok=True)

        ds.save_as(ctx.output_path, write_like_original=False)
        return True
    except Exception as e:
        # Do not raise, as it aborts the entire parallel batch.
        # Log error and return None (Failure)
        print(f"ERROR: Export failed for {ctx.output_path}: {e}", file=sys.stderr)
        return e


def _compress_j2k(ds, pixel_array=None):
    """
    Compresses the pixel data of the dataset using JPEG 2000 Lossless (Pillow).
    Updates TransferSyntaxUID and PixelData.
    """
    try:
        arr = pixel_array
        if arr is None:
            # Fallback to reconstructing from PixelData bytes if array not passed
            if not hasattr(ds, 'PixelData'):
                return

            # 1. Get metadata
            rows = ds.Rows
            cols = ds.Columns
            samples = ds.SamplesPerPixel
            bits = ds.BitsAllocated

            # 2. Reconstruct Numpy Array from bytes (since we just set it in worker)
            # Assuming Little Endian input for now (as set in _create_ds)
            dt = np.uint16 if bits > 8 else np.uint8
            arr = np.frombuffer(ds.PixelData, dtype=dt)

            # Reshape
            # Correctly handle frames
            frames = getattr(ds, "NumberOfFrames", 1)

            # Shape logic matching export worker
            if frames > 1:
                if samples > 1:
                    arr = arr.reshape((frames, rows, cols, samples))
                else:
                    arr = arr.reshape((frames, rows, cols))
            else:
                if samples > 1:
                    arr = arr.reshape((rows, cols, samples))
                else:
                    arr = arr.reshape((rows, cols))
        else:
            # Array passed explicitly.
            # Handle Flattened (1D)
            if len(arr.shape) == 1:
                frames = getattr(ds, "NumberOfFrames", 1)
                rows = getattr(ds, "Rows", 0)
                cols = getattr(ds, "Columns", 0)
                samples = getattr(ds, "SamplesPerPixel", 1)

                try:
                    target_shape = None
                    if frames > 1:
                        target_shape = (
                            frames, rows, cols, samples) if samples > 1 else (
                            frames, rows, cols)
                    else:
                        target_shape = (rows, cols, samples) if samples > 1 else (rows, cols)

                    if target_shape:
                        arr = arr.reshape(target_shape)
                except Exception as e:
                    # If reshape fails, we MUST fail export. Continuing with 1D array is dangerous.
                    # This explains the "tuple index out of range" crash when iterating 1D
                    # array as frames.
                    raise RuntimeError(
                        f"Array shape mismatch. Expected {target_shape} for {
                            arr.size} elements. Error: {e}")

            frames = getattr(ds, "NumberOfFrames", 1)
            samples = getattr(ds, "SamplesPerPixel", 1)

            # Robust Squeeze Logic for Single Sample/Single Frame Edge Cases
            # Pillow prefers (H, W) over (H, W, 1) or (1, H, W) for grayscale.
            if samples == 1:
                if frames == 1:
                    # Expect (H, W) or (1, H, W) or (H, W, 1)
                    if len(arr.shape) == 3:
                        if arr.shape[0] == 1:
                            arr = arr.squeeze(0)  # (1, H, W) -> (H, W)
                        elif arr.shape[-1] == 1:
                            arr = arr.squeeze(-1)  # (H, W, 1) -> (H, W)
                elif frames > 1:
                    # Expect (Frames, H, W) or (Frames, H, W, 1)
                    if len(arr.shape) == 4 and arr.shape[-1] == 1:
                        arr = arr.squeeze(-1)  # (F, H, W, 1) -> (F, H, W)

        # 3. Compress
        frames_data = []

        # Helper to compress single frame
        def encode_frame(frame_arr):
            # Pillow expects [H, W] or [H, W, C]
            if Image is None:
                raise ImportError("Pillow not installed.")
            img = Image.fromarray(frame_arr)
            bio = io.BytesIO()
            img.save(bio, format='JPEG2000', compression='lossless')
            return bio.getvalue()

        if frames > 1:
            for i in range(frames):
                frames_data.append(encode_frame(arr[i]))
        else:
            frames_data.append(encode_frame(arr))

        ds.PixelData = encapsulate(frames_data)
        # ds.TransferSyntaxUID = JPEG2000Lossless # REMOVE: Group 2 tags must be in file_meta only
        ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
        ds.is_implicit_VR = False  # Compressed transfer syntaxes are always Explicit VR
        # JPEG 2000 is always Little Endian (in DICOM encapsulation typically)
        ds.is_little_endian = True

    except ImportError:
        # Fallback or Log?
        raise RuntimeError("Pillow or pydicom not installed/configured for JPEG 2000.")
    except Exception as e:
        raise RuntimeError(f"Compression failed: {e}")


def gantry_json_object_hook(d):
    if "__type__" in d and d["__type__"] == "bytes":
        return base64.b64decode(d["data"])
    return d


class SidecarPixelLoader:
    """
    Functor for lazy loading of pixel data from sidecar.

    Must be a top-level class to be picklable.
    Breaks reference cycles by storing primitive metadata (snapshot) instead of the Instance object.
    Designed to be lightweight and serializable for IPC.
    """

    def __init__(self, sidecar_path, offset, length, alg, instance=None, metadata=None, pixel_hash=None):
        self.sidecar_path = sidecar_path
        self.offset = offset
        self.length = length
        self.alg = alg

        # We need metadata to reshape safely.
        # Prefer direct metadata check, fallback to instance extraction.
        if metadata:
            self.sop_instance_uid = metadata.get("sop_instance_uid", "Unknown")
            self.rows = metadata.get("rows", 0) or 0
            self.cols = metadata.get("cols", 0) or 0
            self.samples = metadata.get("samples", 1) or 1
            self.frames = metadata.get("frames", 0) or 0
            self.bits = metadata.get("bits", 8) or 8
            self.pixel_representation = metadata.get("pixel_representation", 0) or 0
            self.planar_conf = metadata.get("planar_configuration", 0) or 0
            self.pixel_hash = metadata.get("pixel_hash", None)
        elif instance:
            self.sop_instance_uid = instance.sop_instance_uid
            # Extract attributes safely
            self.rows = int(instance.attributes.get("0028,0010", 0) or 0)
            self.cols = int(instance.attributes.get("0028,0011", 0) or 0)
            self.samples = int(instance.attributes.get("0028,0002", 1) or 1)
            self.frames = int(instance.attributes.get("0028,0008", 0) or 0)
            self.bits = int(instance.attributes.get("0028,0100", 8) or 8)
            self.pixel_representation = int(instance.attributes.get("0028,0103", 0) or 0)
            self.planar_conf = int(instance.attributes.get("0028,0006", 0) or 0)
            self.pixel_hash = pixel_hash or getattr(instance, "_pixel_hash", None)
        else:
            raise ValueError("SidecarPixelLoader requires either 'instance' or 'metadata'")

    def __call__(self):
        mgr = SidecarManager(self.sidecar_path)

        try:
            raw = mgr.read_frame(self.offset, self.length, self.alg)
        except Exception as e:
            raise RuntimeError(
                f"Integrity Error: Failed to read/decompress frame for {self.sop_instance_uid}: {e}")

        # Integrity Check
        if self.pixel_hash:
            curr_hash = hashlib.sha256(raw).hexdigest()
            if curr_hash != self.pixel_hash:
                raise RuntimeError(
                    f"Integrity Error: Pixel data hash mismatch for {self.sop_instance_uid}. "
                    f"Expected {self.pixel_hash}, got {curr_hash}. "
                    f"Loader(offset={self.offset}, length={self.length}, alg={self.alg})"
                )

        # Reconstruct based on attributes
        dt = np.uint16 if self.bits > 8 else np.uint8
        # Handle signed?
        if self.pixel_representation == 1:
            dt = np.int16 if self.bits > 8 else np.int8

        arr = np.frombuffer(raw, dtype=dt)

        rows = self.rows
        cols = self.cols
        samples = self.samples
        frames = self.frames
        planar_conf = self.planar_conf

        target_shape = None
        if frames > 1:
            target_shape = (frames, rows, cols, samples)
            if samples == 1:
                target_shape = (frames, rows, cols)
        elif samples > 1:
            if planar_conf == 0:
                target_shape = (rows, cols, samples)
            else:
                # Planar Configuration 1: (Samples, Rows, Cols)
                target_shape = (samples, rows, cols)
        else:
            target_shape = (rows, cols)

        try:
            arr_reshaped = arr.reshape(target_shape)
        except ValueError:
            # Handle padding
            target_size = 1
            for d in target_shape:
                target_size *= d
            if arr.size >= target_size:
                arr = arr[:target_size]
                arr_reshaped = arr.reshape(target_shape)
            else:
                return arr  # Fallback to 1D

        # If Planar=1, transpose to (Rows, Cols, Samples) for consistency
        if samples > 1 and frames <= 1 and planar_conf == 1:
            arr_reshaped = arr_reshaped.transpose(1, 2, 0)
        return arr_reshaped


class SidecarWaveformLoader:
    """Functor for lazy loading of waveform samples from the sidecar.

    Top-level class so it stays picklable across process boundaries.
    Stores primitive geometry rather than an Instance reference, which
    avoids a reference cycle and keeps IPC payloads small.
    """

    def __init__(self, sidecar_path, offset, length, alg,
                 instance=None, metadata=None, waveform_hash=None):
        self.sidecar_path = sidecar_path
        self.offset = offset
        self.length = length
        self.alg = alg

        if metadata:
            self.num_samples = metadata.get("num_samples", 0)
            self.num_channels = metadata.get("num_channels", 0)
            self.interpretation = metadata.get("interpretation", "SS")
            self.waveform_hash = metadata.get("waveform_hash")
        elif instance is not None:
            from .waveform import Waveform
            seq = instance.sequences.get("5400,0100")
            if seq is None or not seq.items:
                raise ValueError(
                    "SidecarWaveformLoader requires a Waveform Sequence on the instance")
            wf = Waveform.from_dicom_item(seq.items[0])
            self.num_samples = wf.num_samples
            self.num_channels = wf.num_channels
            self.interpretation = wf.sample_interpretation
            self.waveform_hash = waveform_hash or getattr(instance, "_waveform_hash", None)
        else:
            raise ValueError(
                "SidecarWaveformLoader requires either 'instance' or 'metadata'")

    def __call__(self):
        from .waveform import decode_samples

        mgr = SidecarManager(self.sidecar_path)
        raw = mgr.read_frame(self.offset, self.length, self.alg)

        if self.waveform_hash:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != self.waveform_hash:
                raise ValueError(
                    f"Waveform integrity check failed: expected "
                    f"{self.waveform_hash}, got {actual}")

        return decode_samples(raw, self.interpretation,
                              self.num_samples, self.num_channels)


class DicomExporter:
    """
    Handles writing the Object Graph back to standard DICOM files.

    Provides static methods for saving Patients, Studies, or creating export batches from Validated/Curated data.
    """
    @staticmethod
    def save_patient(patient: Patient, out_dir: str):
        """
        Iterates over a Patient's hierarchy and saves valid .dcm files to `out_dir`.

        Args:
            patient (Patient): The patient root object.
            out_dir (str): The destination directory.
        """
        DicomExporter.save_studies(patient, patient.studies, out_dir)

    @staticmethod
    def generate_export_from_db(
            store_backend,
            out_dir: str,
            patient_ids: List[str] = None,
            compression: str = None,
            instance_uids: List[str] = None):
        """
        Generator that yields ExportContext objects directly from the DB.

        Designed for O(1) Memory usage during massive exports. Streamingly reconstructs
        lightweight Instance objects from the database rows without loading the full Graph.

        Args:
            store_backend: The persistence backend (SqliteStore).
            out_dir (str): Destination directory.
            patient_ids (List[str], optional): Filter by Patient IDs.
            compression (str, optional): Compression format (e.g., 'j2k').
            instance_uids (List[str], optional): Filter by SOP Instance UIDs.

        Yields:
            ExportContext: A prepared context for exporting a single file.
        """
        for row in store_backend.get_flattened_instances(patient_ids, instance_uids):
            # 1. Rehydrate Attributes
            attrs = {}
            if row['attributes_json']:
                try:
                    attrs = json.loads(row['attributes_json'], object_hook=gantry_json_object_hook)
                except BaseException:
                    pass

            # 2. Construct Lightweight Instance
            inst = Instance(
                sop_instance_uid=row['sop_instance_uid'],
                sop_class_uid=row['sop_class_uid'],
                instance_number=row['instance_number'] or 0,
                file_path=row['file_path']
            )

            # 3. Handle Sequences
            if '__sequences__' in attrs:
                seq_data = attrs.pop('__sequences__')

                # Recursive rehydration helper
                # Recursive rehydration helper

                def rehydrate_seq(seq_dict):
                    rehydrated = {}
                    for tag, items in seq_dict.items():
                        ds = DicomSequence(tag=tag)
                        for item_data in items:
                            di = DicomItem()
                            # Item data is a dict (serialized item)
                            # recursion
                            if '__sequences__' in item_data:
                                sub_seqs = item_data.pop('__sequences__')
                                di.sequences = rehydrate_seq(sub_seqs)
                            di.attributes = item_data
                            ds.items.append(di)
                        rehydrated[tag] = ds
                    return rehydrated

                inst.sequences = rehydrate_seq(seq_data)

            inst.attributes = attrs

            # 4. Pixel Loader
            if row['pixel_offset'] is not None:
                # Use the store's sidecar path, not the instance's original file path
                # Note: This assumes single sidecar file or current one.
                # If using multiple sidecars, we'd need pixel_file_id lookup.
                sc_path = getattr(store_backend, 'sidecar_path', None)
                if not sc_path and row.get('file_path'):
                    # Fallback if pixel data is in original file but we have offset (e.g. partial read?)
                    # But usually offset implies SidecarManager format.
                    # If store_backend doesn't expose sidecar_path, we might be in trouble.
                    # But SqliteStore does.
                    sc_path = row['file_path']

                inst._pixel_loader = SidecarPixelLoader(
                    sidecar_path=sc_path,
                    offset=row['pixel_offset'],
                    length=row['pixel_length'],
                    alg=row['compress_alg'],
                    instance=inst
                )

            # 5. Metadata Overrides (from DB columns)
            pat_attrs = {"PatientName": row['patient_name'], "PatientID": row['patient_id']}

            s_date = row['study_date'] or ""
            study_attrs = {
                "StudyInstanceUID": row['study_instance_uid'],
                "StudyDate": s_date,
                "StudyTime": "120000"
            }

            series_attrs = {
                "SeriesInstanceUID": row['series_instance_uid'],
                "Modality": row['modality'],
                "SeriesNumber": row['series_number'],
                "Manufacturer": row['manufacturer'],
                "ManufacturerModelName": row['model_name'],
                "DeviceSerialNumber": row['device_serial_number']
            }

            # 6. Output Path Logic
            subj_name = f"Subject_{DicomExporter._sanitize(row['patient_id'])}"
            st_desc = attrs.get("0008,1030", "Study")
            study_folder = f"Study_{s_date}_{DicomExporter._sanitize(st_desc)}"
            se_desc = attrs.get("0008,103E", "Series")
            se_num = row['series_number'] or "0"
            series_folder = f"Series_{se_num}_{DicomExporter._sanitize(se_desc)}"
            fname = f"{row['sop_instance_uid']}.dcm"

            full_out_path = os.path.join(out_dir, subj_name, study_folder, series_folder, fname)

            yield ExportContext(
                instance=inst,
                output_path=full_out_path,
                patient_attributes=pat_attrs,
                study_attributes=study_attrs,
                series_attributes=series_attrs,
                pixel_array=None,
                compression=compression
            )

    @staticmethod
    def _generate_export_contexts(
            patient: Patient,
            studies: List[Study],
            out_dir: str,
            compression: str = None) -> List[ExportContext]:
        """
        Generates ExportContext objects for the given studies.

        Calculates output paths and metadata overrides for each instance in the
        provided studies.

        Args:
            patient (Patient): The patient object.
            studies (List[Study]): List of studies to export.
            out_dir (str): Output directory.
            compression (str, optional): Compression format (e.g. 'j2k').

        Returns:
            List[ExportContext]: List of prepared export contexts.
        """
        contexts = []
        for st in studies:
            for se in st.series:
                for inst in se.instances:
                    # Prepare Metadata used for directory structure AND overrides

                    # Patient Attributes
                    pat_attrs = {
                        "0010,0010": patient.patient_name,
                        "0010,0020": patient.patient_id
                    }

                    # Study Attributes
                    s_date_str = ""
                    if st.study_date:
                        if hasattr(st.study_date, 'strftime'):
                            s_date_str = st.study_date.strftime("%Y%m%d")
                        else:
                            s_date_str = str(st.study_date)

                    study_attrs = {
                        "0020,000D": st.study_instance_uid,
                        "0008,0020": s_date_str,
                        "0008,0030": "120000"
                    }

                    # Series Attributes
                    series_attrs = {
                        "0020,000E": se.series_instance_uid,
                        "0008,0060": se.modality,
                        "0020,0011": se.series_number
                    }
                    if se.equipment:
                        series_attrs["0008,0070"] = se.equipment.manufacturer
                        series_attrs["0008,1090"] = se.equipment.model_name
                        series_attrs["0018,1000"] = se.equipment.device_serial_number

                    # Calculate Output Path
                    # 1. Subject Folder
                    subj_name = f"Subject_{DicomExporter._sanitize(patient.patient_id)}"

                    # 2. Study Folder
                    s_date_clean = s_date_str.replace("-", "") or "UnknownDate"

                    s_desc = "Study"
                    if "0008,1030" in inst.attributes:
                        s_desc = inst.attributes["0008,1030"]
                    study_folder = f"Study_{s_date_clean}_{DicomExporter._sanitize(s_desc)}"

                    # 3. Series Folder
                    ser_num = se.series_number if se.series_number is not None else "0"
                    ser_desc = "Series"
                    if "0008,103E" in inst.attributes:
                        ser_desc = inst.attributes["0008,103E"]
                    series_folder = f"Series_{ser_num}_{DicomExporter._sanitize(ser_desc)}"

                    # 4. Filename
                    fname = f"{inst.sop_instance_uid}.dcm"
                    if "0020,0013" in inst.attributes:
                        try:
                            inum = int(inst.attributes["0020,0013"])
                            fname = f"{inum:04d}.dcm"
                        except BaseException:
                            pass

                    full_out_path = os.path.join(
                        out_dir, subj_name, study_folder, series_folder, fname)

                    # Handle In-Memory Pixels (e.g. Remediated/Detached instances)
                    # If file_path is None, worker cannot load pixels. send them.
                    p_array = None
                    if inst.pixel_array is not None:
                        p_array = inst.pixel_array

                    # Extract Sidecar Info if available (Zero-Copy)
                    sc_path, sc_offset, sc_length, sc_alg = None, None, None, None
                    if hasattr(inst, '_pixel_loader') and inst._pixel_loader:
                        # Check if it's a SidecarPixelLoader
                        # We duck-type check for attributes
                        pl = inst._pixel_loader
                        if hasattr(pl, 'sidecar_path') and hasattr(pl, 'offset'):
                            sc_path = pl.sidecar_path
                            sc_offset = pl.offset
                            sc_length = pl.length
                            sc_alg = pl.alg

                    # Add to queue
                    ctx = ExportContext(
                        instance=inst,
                        output_path=full_out_path,
                        patient_attributes=pat_attrs,
                        study_attributes=study_attrs,
                        series_attributes=series_attrs,
                        pixel_array=p_array,
                        compression=compression,
                        sidecar_path=sc_path,
                        pixel_offset=sc_offset,
                        pixel_length=sc_length,
                        pixel_alg=sc_alg
                    )
                    contexts.append(ctx)
        return contexts

    @staticmethod
    def save_studies(
            patient: Patient,
            studies: List[Study],
            out_dir: str,
            compression: str = None,
            show_progress: bool = True,
            executor=None):
        """
        Exports a specific list of studies for a patient using parallel workers.

        Args:
            patient (Patient): The patient root object.
            studies (List[Study]): The list of studies to export.
            out_dir (str): Destination directory.
            compression (str, optional): Compression format ('j2k' or None).
            show_progress (bool): If True, shows a progress bar.
            executor (ProcessPoolExecutor, optional): Shared executor for parallelism.
        """
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        logger = get_logger()

        # Planning Phase: Generate Contexts
        export_tasks = DicomExporter._generate_export_contexts(
            patient, studies, out_dir, compression)

        # Execution Phase
        if not export_tasks:
            logger.warning("No instances found to export.")
            return

        # Log only if progress is shown, or at least one summary line if hidden?
        # If hidden, the caller (batch export) is logging.
        if show_progress:
            logger.info(f"Starting parallel export of {len(export_tasks)} instances...")

        results = run_parallel(
            _export_instance_worker,
            export_tasks,
            desc="Exporting",
            chunksize=10,
            show_progress=show_progress,
            executor=executor)

        # results contains True (success) or Exception (failure)
        success_count = sum(1 for r in results if r is True)
        failures = [r for r in results if isinstance(r, Exception)]

        logger.info(f"Export Complete. Success: {success_count}/{len(export_tasks)}")

        if failures:
            # Raise the first failure to satisfy strict tests
            raise RuntimeError(
                f"Export incomplete. {
                    len(failures)} failed. First error: {
                    failures[0]}")

    @staticmethod
    def export_batch(
            export_tasks: Iterable[ExportContext],
            show_progress: bool = True,
            total: int = None,
            executor=None,
            maxtasksperchild: int = None,
            disable_gc: bool = False):
        """
        Exports a flat list of ExportContexts using parallel workers.

        Args:
            export_tasks (Iterable[ExportContext]): Iterator/List of tasks.
            show_progress (bool): If True, shows progress bar.
            total (int, optional): Total count for progress bar.
            executor (optional): Shared executor.
            maxtasksperchild (int, optional): Worker recycle rate (for memory management).
            disable_gc (bool): If True, disables GC in workers for throughput.

        Returns:
            int: Number of successfully exported instances.
        """
        logger = get_logger()
        # if not export_tasks: return # Cannot easily check empty iterator without consuming

        if show_progress:
            count_str = str(total) if total else "?"
            logger.info(f"Starting global parallel export of {count_str} instances...")

        # Run parallel
        results = run_parallel(
            _export_instance_worker,
            export_tasks,
            desc="Exporting",
            chunksize=1,
            show_progress=show_progress,
            total=total,
            executor=executor,
            maxtasksperchild=maxtasksperchild,
            disable_gc=disable_gc)

        success_count = sum(1 for r in results if r is True)
        # failures = [r for r in results if isinstance(r, Exception)]
        # We don't raise here by default (batch mode), but success_count reflects only True results.

        logger.info(f"Export Complete. Success: {success_count}/{total or '?'}")
        return success_count

    @staticmethod
    def _finalize_dataset(ds, compression=None, pixel_array=None):
        """
        Finalizes the dataset before saving.

        Applies compression if requested and validates the IOD against DICOM standards.

        Args:
            ds (pydicom.Dataset): The dataset to process.
            compression (str, optional): 'j2k' or None.
            pixel_array (np.ndarray, optional): Pixel data to compress.

        Returns:
            pydicom.Dataset: The finalized dataset.

        Raises:
            ValueError: If validation fails.
        """
        if compression == 'j2k':
            _compress_j2k(ds, pixel_array)

        errs = IODValidator.validate(ds)
        if errs:
            # We log but might want to raise? logic in worker returns None on error.
            # But worker expects exception to be raised for error?
            # In previous logic: "if not errs: save else return None"
            # So here we should probably return None or raise.
            # Let's raise to be clearer in worker catch
            raise ValueError(f"Validation Errors: {errs}")

        return ds

    @staticmethod
    def _create_ds(inst):
        """Helper to create a fresh FileDataset from an Instance."""
        meta = FileMetaDataset()
        # Fallback to attributes if sop_class_uid property is missing/empty
        sop_class = inst.sop_class_uid
        if not sop_class and "0008,0016" in inst.attributes:
            sop_class = inst.attributes["0008,0016"]

        meta.MediaStorageSOPClassUID = sop_class
        meta.MediaStorageSOPInstanceUID = inst.sop_instance_uid
        meta.TransferSyntaxUID = ImplicitVRLittleEndian
        ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
        ds.is_little_endian = True
        ds.is_implicit_VR = True
        return ds

    @staticmethod
    def _merge(ds, attrs):
        """Merges a dictionary of attributes into a pydicom Dataset."""
        for t, v in attrs.items():
            # Explicit handling for Gantry Private Tags to ensure correct VR
            if t == "0099,0010":
                ds.add_new(0x00990010, 'LO', v)
                continue
            if t == "0099,1001":
                ds.add_new(0x00991001, 'OB', v)
                continue

            # Explicit handling for Encrypted Attributes to fix potential dictionary mismatches
            if t == "0400,0510":  # Encrypted Content
                ds.add_new(0x04000510, 'OB', v)
                continue
            if t == "0400,0520":  # Encrypted Content Transfer Syntax UID
                ds.add_new(0x04000520, 'UI', v)
                continue

            if t.startswith("_") or "," not in t:
                continue

            g, e = map(lambda x: int(x, 16), t.split(','))

            # Skip Command Set elements (Group 0000) which are illegal for file persistence
            if g == 0x0000:
                continue

            try:
                vr = dictionary_VR(Tag(g, e))
                ds.add_new(Tag(g, e), vr, v)
            except Exception as e:
                get_logger().warning(f"Failed to merge tag {t} ({v}): {e}")

    @staticmethod
    def _sanitize(filename: str) -> str:
        """
        Removes illegal characters from filenames.

        Args:
            filename (str): Input filename.

        Returns:
            str: Sanitized filename string.
        """
        if not filename:
            return "Unknown"
        # Keep alphanumeric, dashes, underscores, spaces (maybe replace spaces with underscores?)
        # For strictness:
        safe = "".join([c for c in str(filename) if c.isalnum() or c in (' ', '.', '-', '_')])
        return safe.strip().replace(" ", "_")

    @staticmethod
    def _merge_sequences(ds, sequences: Dict[str, Any]):
        """
        Recursively populates sequences into the dataset.

        Args:
            ds (pydicom.Dataset): The dataset to modify.
            sequences (Dict[str, DicomSequence]): Dictionary mapping tags to Sequence objects.
        """
        for tag_str, dicom_seq in sequences.items():
            g, e = map(lambda x: int(x, 16), tag_str.split(','))
            tag = Tag(g, e)

            pydicom_seq = Sequence()
            for item in dicom_seq.items:
                ds_item = Dataset()
                ds_item.is_little_endian = True
                ds_item.is_implicit_VR = True

                # Recursively merge item attributes and sub-sequences
                DicomExporter._merge(ds_item, item.attributes)
                DicomExporter._merge_sequences(ds_item, item.sequences)

                pydicom_seq.append(ds_item)

            ds.add_new(tag, 'SQ', pydicom_seq)
