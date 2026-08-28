"""
IO Handlers for Isocenter.

This module provides classes for:
- DicomStore: The central catalog of DICOM objects.
- DicomImporter: Parallel file ingestion.
- DicomExporter: Writing DICOM files to disk.
- SidecarPixelLoader: Lazy loading of pixel data.
- SidecarWaveformLoader: Lazy loading of waveform samples.
"""

import os
import sys
import hashlib
import io
from typing import List, Dict, Any, Optional, Tuple, Iterable
from datetime import datetime, date
from dataclasses import dataclass, field

import pydicom
import numpy as np
try:
    from PIL import Image
except ImportError:
    Image = None
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless
from pydicom.tag import Tag
from pydicom.datadict import dictionary_VR
try:
    from pydicom.encapsulate import encapsulate
except ImportError:
    from pydicom.encaps import encapsulate
from pydicom.sequence import Sequence
from pydicom.dataset import Dataset

from .entities import Patient, Study, Series, Instance, Equipment, DicomItem
from .logger import get_logger
from .parallel import run_parallel
from .validation import IODValidator
from .sidecar import SidecarManager


from .store import DicomStore
from .config_manager import ConfigLoader


#: Binary elements that `populate_attrs` skips but that are *not* lost --
#: each is extracted and written to the sidecar elsewhere. They must stay
#: out of the DATA_LOSS report or every ingest files a loss that did not
#: happen (#137). (7fe0,0010) is listed for the record; it is excluded by
#: the group check before the VR check ever sees it.
_ROUTED_BINARY_TAGS = frozenset({
    Tag(0x7fe0, 0x0010),   # Pixel Data
    Tag(0x5400, 0x1010),   # Waveform Data
})

#: How a `DATA_LOSS` audit entry is graded: PRIVATE takes
#: `validation_status` to REVIEW_REQUIRED, STANDARD does not. Written by
#: the emitter and stored on the audit row rather than re-derived, and
#: why the two differ, are both argued once -- CHANGELOG.md, #146.
LOSS_SCOPE_PRIVATE = "PRIVATE"
LOSS_SCOPE_STANDARD = "STANDARD"


def loss_scope_for_tag(tag: str) -> str:
    """Classify a lost element for grading, by the parity of its group.

    Odd group is private, even is standard -- the same split the store
    already uses to decide where an attribute is written. What each
    scope does to `validation_status`, and why they differ, is in
    CHANGELOG.md under #146.

    Args:
        tag (str): A `"gggg,eeee"` lowercase-hex tag.

    Returns:
        str: `LOSS_SCOPE_PRIVATE` or `LOSS_SCOPE_STANDARD`.

    Raises:
        ValueError: If `tag` is not in `"gggg,eeee"` form. Deliberately
            not caught: every caller holds a tag it has already parsed,
            so an unparseable one is a bug, and defaulting it to
            "standard" would silently downgrade a real loss.
    """
    group = int(tag.split(",")[0], 16)
    return LOSS_SCOPE_PRIVATE if group % 2 else LOSS_SCOPE_STANDARD


def populate_attrs(ds: Any, item: "DicomItem", dropped: list = None):
    """
    Standalone function to populate attributes for pickle-compatibility in workers.

    Extracts standard DICOM elements from a pydicom Dataset and populates the
    Isocenter DicomItem. Handles Sequences recursively. Skips large binary blobs
    to keep the object graph lightweight.

    Skipping is not the same as routing. `PixelData` and `WaveformData`
    are extracted and written to the sidecar by `ingest_worker`, so
    skipping them here loses nothing. Everything else with a binary VR
    -- private vendor blocks, Overlay Data, the palette LUTs -- is
    skipped and routed nowhere, which means it is dropped. Those are
    collected in `dropped` so the caller can report them (#125, #137).

    Took a third `text_index` argument until #84, which collected the
    text-VR elements it saw into `Instance.text_index`. Nothing read that
    index after the PHI scan became structural, and the VR filter it
    applied was never a scan boundary -- a configured PHI tag is one
    wherever it sits and whatever its VR.

    Args:
        ds: The pydicom Dataset or Sequence Item.
        item (DicomItem): The Isocenter item to populate.
        dropped (list, optional): Collects `(tag, vr)` for every element
            skipped for its VR that is not routed to the sidecar, so the
            caller can report them (#125, #137). See
            `_ROUTED_BINARY_TAGS` for the exclusions and why they exist.
    """

    # Binary VRs to explicitly skip (Metadata Refactor)
    # UN left out for safety, usually small private tags
    BINARY_VRS = {'OB', 'OW', 'OF', 'OD', 'OL'}

    for elem in ds:
        if elem.tag.group == 0x7fe0:
            continue  # Skip pixels
        if elem.VR in BINARY_VRS:
            # These bytes do not reach the object graph at all, so
            # `remove_private_tags=False` cannot keep them -- there is
            # nothing left to keep by the time the flag is consulted.
            #
            # The gate is "binary VR and routed nowhere", not "binary VR"
            # and not "odd group" (#137). Two standard elements hit this
            # rule and are *not* lost: (7fe0,0010) never gets here at all
            # -- the group check above takes it -- and (5400,1010) is
            # pulled out and written to the sidecar by `ingest_worker`
            # before this runs. Reporting either would put a DATA_LOSS
            # entry in the record of every image and every waveform ever
            # ingested, which is how a compliance trail becomes noise.
            #
            # Everything else genuinely vanishes, whatever its group.
            # Overlay Data and the palette LUTs are `OW`, standard, and
            # routed nowhere; the odd-group gate this replaces left them
            # unreported because it read "standard" as "safe". Whether
            # any of these bytes should be *kept* is the open half of
            # #125; that they vanished in silence is what this closes.
            if dropped is not None and elem.tag not in _ROUTED_BINARY_TAGS:
                dropped.append(
                    (f"{elem.tag.group:04x},{elem.tag.element:04x}", elem.VR))
            continue  # Skip binary blobs

        tag = f"{elem.tag.group:04x},{elem.tag.element:04x}"

        if elem.VR == 'SQ':
            process_sequence(tag, elem, item, dropped)
        elif elem.VR == 'PN':
            # Sanitize PersonName for pickle safety
            item.set_attr(tag, str(elem.value))
        else:
            item.set_attr(tag, elem.value)


def process_sequence(tag, elem, parent_item, dropped: list = None):
    """Recursively parses Sequence (SQ) items."""
    for ds_item in elem:
        seq_item = DicomItem()
        populate_attrs(ds_item, seq_item, dropped)
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
            # Absent stays absent. This used to default to "19000101",
            # and nothing downstream could tell that from a real date --
            # SHIFT_DATE jittered it and the result was exported as
            # genuine study timing, so a study that never had a date
            # acquired one near 1900 (#60).
            'sdate': str(ds.StudyDate) if "StudyDate" in ds else None,
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
        # Rides `meta` rather than a ninth tuple slot, which is the
        # channel #36's multiplex-group loss already uses. This worker
        # may be in a subprocess with no store handle, so the loss
        # travels and the parent records it (#125, and #126 for the
        # export side of the same constraint).
        dropped = []
        populate_attrs(ds, inst, dropped)
        meta['dropped_private_binary'] = dropped

        # Isocenter internally manages pixels as standard contiguous arrays (Interleaved)
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
        #
        # The count is reported back in `meta` rather than kept here,
        # because this function runs in a worker process and cannot reach
        # the audit log. `import_files` warns and records the loss on the
        # far side. It rides in `meta` rather than as a tenth tuple
        # element so the return arity -- unpacked at every call site --
        # does not change.
        w_bytes = None
        w_hash = None
        meta['waveform_groups'] = len(ds.WaveformSequence) if "WaveformSequence" in ds else 0

        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            wf_item = ds.WaveformSequence[0]
            raw = getattr(wf_item, "WaveformData", None)
            if raw:
                w_bytes = bytes(raw)
                w_hash = hashlib.sha256(w_bytes).hexdigest()

        # The samples of groups 1..n are discarded just above; their
        # sequence items go with them. `populate_attrs` walks the whole
        # sequence, so the graph used to hold one item per group while
        # the sidecar held one group's bytes -- and the export wrote
        # every item, producing a file that declared a multiplex group
        # and carried no Waveform Data for it. (5400,1010) is Type 1
        # (PS3.3 C.10.9): a conformant reader may reject such a file, and
        # a trusting one reads `NumberOfWaveformSamples` with nothing
        # behind it (#160).
        #
        # Dropped at ingest rather than at export because the graph is
        # what every consumer reads -- the DICOM writer, the WFDB record,
        # the annotation bridge, the PHI report. Patching the writer
        # alone would leave the rest describing a group whose samples
        # this pipeline does not have. Nothing is hidden by dropping
        # them: `import_files` warns and files the DATA_LOSS entry from
        # `meta['waveform_groups']`, which still carries the source's
        # original group count.
        #
        # This is not a position on #150. It is correct under every
        # answer there, and if multi-rate support ever lands the block
        # stops firing on its own -- the items are dropped because the
        # samples are, and then they would not be.
        wf_seq = inst.sequences.get("5400,0100")
        if wf_seq is not None and len(wf_seq.items) > 1:
            del wf_seq.items[1:]

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

                    # Silent truncation is the defect here, not the
                    # missing multi-rate support -- that is deferred on
                    # purpose. A record whose groups were dropped without
                    # a word is indistinguishable from one that only ever
                    # had a single group (#36).
                    groups = meta.get('waveform_groups', 0)
                    if groups > 1:
                        dropped = groups - 1
                        detail = (f"WaveformSequence carried {groups} multiplex "
                                  f"groups; kept group 0 and discarded "
                                  f"{dropped}. Multi-rate records are not yet "
                                  f"supported.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        # The log line alone is not a compliance trail: it
                        # goes to a file the user may never open. The audit
                        # entry is what puts this in the record.
                        #
                        # Scoped STANDARD, so it is reported and not
                        # graded: what was discarded lives under Waveform
                        # Sequence (5400,0100), an even group. This is
                        # the one loss where that rule is uncomfortable
                        # -- a discarded multiplex group is not routine
                        # the way an overlay is -- and it is open on
                        # #150, deliberately, rather than special-cased
                        # here. Do not "fix" it to PRIVATE: the scope
                        # states what the element was, not how bad the
                        # loss felt.
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=LOSS_SCOPE_STANDARD)

                    # Private binary elements never reached the graph, so
                    # `remove_private_tags=False` could not have kept
                    # them. Same reasoning as the block above: a loss the
                    # caller cannot see is indistinguishable from a file
                    # that never carried the tag (#125).
                    #
                    # The key still says `private` because #125 found it
                    # there; since #137 the list also carries standard
                    # elements, which is why the message is chosen per
                    # tag. Saying "Private tag 6000,3000" on a row the
                    # report scopes STANDARD invites the reader to
                    # distrust whichever half they check second.
                    for tag, vr in meta.get('dropped_private_binary', ()):
                        scope = loss_scope_for_tag(tag)
                        if scope == LOSS_SCOPE_PRIVATE:
                            detail = (f"Private tag {tag} ({vr}) was not "
                                      f"ingested; binary-VR elements are not "
                                      f"held in the object graph, so it "
                                      f"cannot be exported even with "
                                      f"remove_private_tags=False.")
                        else:
                            detail = (f"Standard tag {tag} ({vr}) was not "
                                      f"ingested; binary-VR elements are not "
                                      f"held in the object graph, so it is "
                                      f"not in the exported file.")
                        logger.warning(f"{inst.sop_instance_uid}: {detail}")
                        if store_backend is not None:
                            store_backend.log_audit(
                                action_type="DATA_LOSS",
                                entity_uid=inst.sop_instance_uid,
                                details=detail,
                                loss_scope=scope)

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
                        # A date we cannot read is a date we do not have.
                        # Substituting one here is indistinguishable
                        # downstream from a date that was recorded (#60).
                        sdate = None
                        if meta['sdate']:
                            try:
                                sdate = datetime.strptime(
                                    meta['sdate'], "%Y%m%d").date()
                            except (ValueError, TypeError):
                                logger.warning(
                                    f"Study {sid} has an unreadable Study "
                                    f"Date ({meta['sdate']!r}); it will be "
                                    "treated as absent rather than guessed.")

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


@dataclass
class ExportOutcome:
    """What one worker has to tell the parent about one instance (#126).

    The worker used to answer `True` or the exception, which is enough to
    count successes and no help at all for a *partial* success: a file
    that was written and is missing something the caller asked for. Data
    loss is neither an error nor nothing, so it needs its own field.

    `error` lives here rather than being returned bare so the worker has
    one return shape. Call sites still have to filter for `Exception`,
    because `run_parallel` can return one of its own when a worker dies
    -- but a site that forgets no longer gets an `AttributeError` on the
    failure path, which is the path that only runs when something has
    already gone wrong.
    """
    ok: bool
    output_path: str
    sop_instance_uid: Optional[str] = None
    #: `(scope, detail)` per lost element, where scope is one of
    #: `LOSS_SCOPE_PRIVATE` / `LOSS_SCOPE_STANDARD`. The scope travels
    #: with the message rather than being worked out by the parent
    #: because only the worker still has the tag; by the time
    #: `_report_export_losses` sees this, the tag is prose (#146).
    losses: List[Tuple[str, str]] = field(default_factory=list)
    error: Optional[BaseException] = None


def _export_instance_worker(ctx: ExportContext) -> "ExportOutcome":
    """
    Worker function to export a single instance.

    Reconstructs a pydicom Dataset from the ExportContext (Instance + Attributes)
    and saves it to disk. Handles optional compression (JPEG2000).

    Args:
        ctx (ExportContext): The context/request for export.

    Returns:
        ExportOutcome: the write's result, plus any elements lost on the
            way out for the parent to log and audit (#126).
    """
    losses: List[Tuple[str, str]] = []
    uid = getattr(ctx.instance, "sop_instance_uid", None)

    try:
        inst = ctx.instance
        ds = DicomExporter._create_ds(inst)

        # 0. Base Attributes
        DicomExporter._merge(ds, inst.attributes, losses)
        DicomExporter._merge_sequences(ds, inst.sequences, losses)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes, losses)
        # 0. Base Attributes
        DicomExporter._merge(ds, inst.attributes, losses)
        DicomExporter._merge_sequences(ds, inst.sequences, losses)

        # 1. Patient Level
        DicomExporter._merge(ds, ctx.patient_attributes, losses)

        # 2. Study Level
        DicomExporter._merge(ds, ctx.study_attributes, losses)

        # 3. Series Level
        DicomExporter._merge(ds, ctx.series_attributes, losses)

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

        # Waveform samples never reach `attributes` -- populate_attrs skips
        # OB/OW -- so the rebuilt dataset carries a complete Waveform
        # Sequence (channel definitions, sampling frequency, sample count)
        # with no signal in it unless they are put back here (#34).
        #
        # The sidecar's bytes are written back verbatim rather than
        # re-encoded from the decoded array, because nothing in this
        # pipeline mutates waveform samples -- unlike pixels, which
        # redaction burns into a few lines above. A re-encode could
        # therefore only lose: it would have to undo the int16 rebasing
        # `decode_samples` applies to US, and any slip there shifts every
        # value by 32768 while (5400,1006) still says "US". Copying the
        # original bytes makes that mismatch structurally impossible
        # rather than merely tested against.
        #
        # Endianness is inherited, not assumed here: ingest never records
        # the source transfer syntax and `decode_samples` hardcodes
        # little-endian, so the whole pipeline already requires a
        # little-endian source. This adds no new assumption.
        if "WaveformSequence" in ds and len(ds.WaveformSequence) > 0:
            w_raw = inst.get_waveform_bytes()
            if w_raw:
                # Only group 0 is ingested (#36), so only group 0 can
                # be written -- and by the time the graph gets here it
                # is the only item there is, because `ingest_worker`
                # drops the items whose samples it discarded (#160).
                # Indexing [0] is therefore exhaustive, not a choice
                # among items: writing samples onto one item of several
                # is what left the rest declaring a Type 1 element they
                # did not carry.
                ds.WaveformSequence[0].WaveformData = w_raw
            else:
                # Structurally plausible and empty is the failure mode this
                # whole fix exists to end; if it is still reachable -- a
                # source that never carried samples -- say so rather than
                # writing the file in silence.
                #
                # This is the export side of the same loss #36 records at
                # ingest, so it rides the same channel (#126). Only the
                # empty-samples case: the multiplex-group loss above is
                # reported at ingest and is not re-reported here.
                #
                # Scoped STANDARD: what is missing is Waveform Data
                # (5400,1010), an even group. It is reported and not
                # graded, on the same rule as the ingest-side multiplex
                # loss and with the same reservation filed as #150.
                losses.append((
                    LOSS_SCOPE_STANDARD,
                    "Waveform Sequence present but no samples are available "
                    "to export; the written file will describe a waveform it "
                    "does not contain."))

        if "_ISOCENTER_REDACTION_HASH" in ds:
            del ds["_ISOCENTER_REDACTION_HASH"]

        # Validate & Save
        ds = DicomExporter._finalize_dataset(ds, ctx.compression, pixel_array=arr)

        # Ensure dir exists (race safe)
        os.makedirs(os.path.dirname(ctx.output_path), exist_ok=True)

        ds.save_as(ctx.output_path, enforce_file_format=True)
        return ExportOutcome(ok=True, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses)
    except Exception as e:
        # Do not raise, as it aborts the entire parallel batch.
        # Report the failure back for the parent to count and raise on.
        print(f"ERROR: Export failed for {ctx.output_path}: {e}", file=sys.stderr)
        return ExportOutcome(ok=False, output_path=ctx.output_path,
                             sop_instance_uid=uid, losses=losses, error=e)


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
        # The transfer syntax is the encoding. `is_implicit_VR` and
        # `is_little_endian` are not set alongside it: pydicom derives
        # both from the UID and removes the attributes in 4.0 (#141).
        ds.file_meta.TransferSyntaxUID = JPEG2000Lossless

    except ImportError:
        # Fallback or Log?
        raise RuntimeError("Pillow or pydicom not installed/configured for JPEG 2000.")
    except Exception as e:
        raise RuntimeError(f"Compression failed: {e}")


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

    def read_raw(self) -> bytes:
        """Return the original Waveform Data bytes, integrity-checked.

        Split out from `__call__` so DICOM export can write the source
        bytes back without a decode/re-encode round trip (#34). Callers
        get the sha256 verification for free, which is the reason to come
        through here rather than reading the frame directly.
        """
        mgr = SidecarManager(self.sidecar_path)
        raw = mgr.read_frame(self.offset, self.length, self.alg)

        if self.waveform_hash:
            actual = hashlib.sha256(raw).hexdigest()
            if actual != self.waveform_hash:
                raise ValueError(
                    f"Waveform integrity check failed: expected "
                    f"{self.waveform_hash}, got {actual}")

        return raw

    def __call__(self):
        from .waveform import decode_samples

        return decode_samples(self.read_raw(), self.interpretation,
                              self.num_samples, self.num_channels)


def format_study_date(study_date) -> str:
    """Render a Study's date as "YYYYMMDD" for use in exported DICOM
    attributes.

    Args:
        study_date: `Study.study_date` -- a `date`/`datetime`-like object,
            a preformatted string, or falsy/None.

    Returns:
        str: "YYYYMMDD" when `study_date` supports `strftime`, else
        `str(study_date)`, else "".
    """
    if not study_date:
        return ""
    if hasattr(study_date, 'strftime'):
        return study_date.strftime("%Y%m%d")
    return str(study_date)


def _get_attr_case_insensitive(attributes: dict, tag: str, default):
    """Look up a DICOM attribute tag tolerating either hex-letter casing.

    Real ingested attribute keys are always lowercased
    (`populate_attrs`'s `f"{elem.tag.group:04x},{elem.tag.element:04x}"`),
    but object graphs built directly by a caller -- test fixtures,
    `scripts/generate_test_dataset.py`'s `inst_builder.set_attribute(
    "0008,103E", ...)` -- are free to spell a tag with uppercase hex
    letters. Checking only one casing silently drops values set under the
    other; this is the same trap `privacy.py`'s
    `PHIRedactor._normalize_tag_keys` normalizes away for PHI-tag config
    keys (see its comment naming this exact tag, "0008,103E"). Callers of
    this function should look up a tag through it rather than re-adding a
    `.lower()`/`.upper()` at their own call site.

    Args:
        attributes (dict): A `DicomItem.attributes`-shaped dict.
        tag (str): The tag to look up, e.g. `"0008,103e"`.
        default: Returned if `tag` is absent under every casing.

    Returns:
        The attribute value, or `default`.
    """
    if tag in attributes:
        return attributes[tag]
    tag_lower = tag.lower()
    for key, value in attributes.items():
        if isinstance(key, str) and key.lower() == tag_lower:
            return value
    return default


def export_folder_names(patient, study, series):
    """Build the Subject/Study/Series folder names for the exported file
    tree, reproducing `DicomSession._export_dicom`'s "Hybrid Naming"
    scheme -- the naming every user actually gets from
    `session.export(folder)` / `session.export(folder, format="dicom")`
    via the registered `"dicom"` exporter.

    This is the single source of truth for that naming so every export
    format lands in the same `Patient/Study/Series` tree -- callers must
    not reimplement this logic locally, or the trees will drift apart on
    the next edit to either one.

    Uses `ConfigLoader.clean_filename`, the single sanitizer for folder
    names -- NOT the even-stricter per-format record-*name* sanitizers
    such as `isocenter.exporters.wfdb._sanitize` (which forbids spaces,
    appropriate for a bare record-name token but not for a folder name
    that must match `_export_dicom`'s output character-for-character).

    Args:
        patient (Patient): Patient root.
        study (Study): Study whose folder name is being built.
        series (Series): Series whose folder name is being built.

    Returns:
        tuple[str, str, str]: (subject_folder, study_folder, series_folder)
    """
    subj_name = "Subject_" + ConfigLoader.clean_filename(patient.patient_id or "UnknownPatient")

    # Study/Series descriptions are read from the FIRST series' FIRST
    # instance -- not from whichever instance a caller happens to be
    # iterating -- matching `_export_dicom`'s "peek" exactly, so every
    # instance in a series lands under the same folder name.
    st_desc = "Study"
    try:
        if study.series and study.series[0].instances:
            st_desc = _get_attr_case_insensitive(
                study.series[0].instances[0].attributes, "0008,1030", "Study")
    except (AttributeError, IndexError, KeyError):
        # No instances, or no description tag: the "Study" default above
        # stands. Narrow on purpose -- BaseException here also swallowed
        # Ctrl-C during a long export.
        pass
    st_date = str(study.study_date or "NoDate")
    # The suffix disambiguates two studies sharing a date and description.
    # With no UID there is nothing to disambiguate *with*, so say so --
    # slicing a placeholder produced `"Unknown"[-5:]` == "nknow", a word
    # from nowhere that looks like real data and sorts among real
    # suffixes. Take the last 5 only when there is a UID to take them
    # from. (#53, #78)
    st_uid_suffix = (study.study_instance_uid[-5:]
                     if study.study_instance_uid else "NoUID")
    study_folder = ConfigLoader.clean_filename(f"Study_{st_date}_{st_desc}_{st_uid_suffix}")

    se_desc = "Series"
    try:
        if series.instances:
            se_desc = _get_attr_case_insensitive(
                series.instances[0].attributes, "0008,103e", "Series")
    except (AttributeError, IndexError, KeyError):
        # As above: fall back to the "Series" default.
        pass
    # `str(None)` is "None", which reads as a series *numbered* None
    # rather than one whose number was never recorded -- the same defect
    # as the sliced placeholder, one line up.
    se_num = ("NoNumber" if series.series_number is None
              else str(series.series_number))
    se_mod = series.modality or "OT"
    se_uid_suffix = (series.series_instance_uid[-5:]
                     if series.series_instance_uid else "NoUID")
    series_folder = ConfigLoader.clean_filename(
        f"Series_{se_num}_{se_mod}_{se_desc}_{se_uid_suffix}")

    return subj_name, study_folder, series_folder


class DicomExporter:
    """
    Handles writing the Object Graph back to standard DICOM files.

    Provides static methods for saving Patients, Studies, or creating export batches from Validated/Curated data.
    """
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
                    s_date_str = format_study_date(st.study_date)

                    study_attrs = {
                        "0020,000d": st.study_instance_uid,
                        "0008,0020": s_date_str,
                        "0008,0030": "120000"
                    }

                    # Series Attributes
                    series_attrs = {
                        "0020,000e": se.series_instance_uid,
                        "0008,0060": se.modality,
                        "0020,0011": se.series_number
                    }
                    if se.equipment:
                        series_attrs["0008,0070"] = se.equipment.manufacturer
                        series_attrs["0008,1090"] = se.equipment.model_name
                        series_attrs["0018,1000"] = se.equipment.device_serial_number

                    # Calculate Output Path
                    # 1-3. Subject/Study/Series folders, via the shared
                    # hybrid naming used by every other export format --
                    # see `export_folder_names` for the scheme.
                    subj_name, study_folder, series_folder = export_folder_names(
                        patient, st, se)

                    # 4. Filename -- the SOP Instance UID, matching
                    # `DicomSession._export_dicom`. InstanceNumber
                    # (0020,0013) used to win here when it parsed as an
                    # integer, which meant the same instance landed under
                    # two different names depending on which export path
                    # wrote it, and a tree built by one could not be
                    # diffed against a tree built by the other.
                    #
                    # The UID is also the only correct choice on its own
                    # terms: InstanceNumber is not unique and collides
                    # silently within a series, so `0001.dcm` could be
                    # overwritten by a second instance claiming the same
                    # number. Do not reintroduce a "friendlier" name
                    # here without making it unique. (#50, #78)
                    fname = f"{inst.sop_instance_uid}.dcm"

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
    def _report_export_losses(results, store_backend=None) -> int:
        """Log every loss the workers reported, and audit it if we can.

        Warning and auditing are deliberately not the same condition. The
        warning is unconditional because `write_tree` can never supply a
        backend -- it is the serializer path, with no session behind it --
        and gating the report on one would make the fixture generators in
        `scripts/` lose elements in total silence. The audit entry is what
        turns a log line into a compliance record (#36), and needs a store.

        Returns the number of losses reported.
        """
        logger = get_logger()
        count = 0
        for r in results:
            for scope, loss in getattr(r, "losses", ()):  # Exceptions have none
                uid = r.sop_instance_uid or r.output_path
                logger.warning(f"{uid}: {loss}")
                count += 1
                if store_backend is not None:
                    # `log_audit`, not `log_audit_batch`: the batch method
                    # writes straight to the database while the audit
                    # writer thread is live, and swallows `sqlite3.Error`
                    # into a log line -- so contention would lose the very
                    # entry that exists because a log line was not enough.
                    # The queue is the path #36 uses, and `close()` drains
                    # it.
                    store_backend.log_audit(
                        action_type="DATA_LOSS", entity_uid=uid, details=loss,
                        loss_scope=scope)
        return count

    @staticmethod
    def write_tree(
            patient: Patient,
            out_dir: str,
            studies: List[Study] = None,
            compression: str = None,
            show_progress: bool = True,
            executor=None,
            store_backend=None):
        """Write an object graph to disk as DICOM, exactly as it stands.

        **This applies no de-identification.** It is the serializer, not
        the pipeline: it runs no PHI scan, honours no subset filter,
        applies no redaction zones, and reports no partial failure beyond
        raising on the first one. Whatever is in `patient` is what lands
        on disk.

        `DicomSession.export()` is the pipeline, and is what a caller
        de-identifying a cohort wants. It performs the same write, after
        the burned-in identifier scan (`check_burned_in`), the subset
        filter, the recoverable-identity disclosure (`check_reversibility`)
        and the configured redaction rules.

        This exists as a public API because building an object graph by
        hand and writing it out is a real need with no session behind it
        -- it is how `scripts/generate_test_dataset.py` and the other
        fixture generators work, and how the test suite produces DICOM
        without standing up a database. It was previously called
        `save_patient`/`save_studies`, which named it as though it were
        the export path rather than half of one (#54, #78).

        Args:
            patient (Patient): The patient root object.
            out_dir (str): Destination directory.
            studies (List[Study], optional): Write only these studies.
                Defaults to every study under `patient`.
            compression (str, optional): Compression format ('j2k' or None).
            show_progress (bool): If True, shows a progress bar.
            executor (ProcessPoolExecutor, optional): Shared executor for parallelism.
            store_backend (SqliteStore, optional): Where to write a
                `DATA_LOSS` audit entry for each element that could not be
                written. Callers of this path usually have no session and
                so pass nothing; the losses are logged either way (#126).

        Raises:
            RuntimeError: If any instance failed to write.
        """
        if studies is None:
            studies = patient.studies
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

        # An ExportOutcome per task -- or an Exception, if `run_parallel`
        # itself lost a worker. Both shapes have to survive this.
        #
        # Materialized because what follows walks it twice, and
        # `run_parallel` returns a generator when asked to. Neither export
        # site asks today; if one ever does, the loss report would consume
        # the results and every success count would silently read zero.
        results = list(results)
        DicomExporter._report_export_losses(results, store_backend)
        success_count = sum(1 for r in results if getattr(r, "ok", False))
        failures = [r.error if isinstance(r, ExportOutcome) else r
                    for r in results
                    if isinstance(r, Exception) or (
                        isinstance(r, ExportOutcome) and not r.ok)]

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
            disable_gc: bool = False,
            store_backend=None):
        """
        Exports a flat list of ExportContexts using parallel workers.

        Args:
            export_tasks (Iterable[ExportContext]): Iterator/List of tasks.
            show_progress (bool): If True, shows progress bar.
            total (int, optional): Total count for progress bar.
            executor (optional): Shared executor.
            maxtasksperchild (int, optional): Worker recycle rate (for memory management).
            disable_gc (bool): If True, disables GC in workers for throughput.
            store_backend (SqliteStore, optional): Where to write a
                `DATA_LOSS` audit entry for each element the workers could
                not write. Without it the losses are still logged, but
                only logged (#126).

        Returns:
            int: Number of successfully exported instances. An instance
                that was written but lost an element counts as a success;
                the loss is reported separately.
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

        # Two passes -- see the note in `write_tree`.
        results = list(results)
        DicomExporter._report_export_losses(results, store_backend)
        success_count = sum(1 for r in results if getattr(r, "ok", False))
        # We don't raise here by default (batch mode); `success_count`
        # counts only instances that were actually written. An instance
        # that was written *and* lost an element counts as a success and
        # is reported through the loss channel above, not this number.

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
        # Encoding comes from meta.TransferSyntaxUID above; see the
        # note on the JPEG 2000 branch in `_compress_j2k` (#141).
        ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
        return ds

    @staticmethod
    def _merge(ds, attrs, losses=None):
        """Merges a dictionary of attributes into a pydicom Dataset.

        `losses` is an optional list that collects `(scope, detail)` for
        every element that could not be written -- the description, plus
        which side of the private/standard line the tag fell on, which
        is what grades the run (#146). It is an accumulator rather than
        a return value because `_merge` is called six times per instance
        and the loss belongs to the instance, not the call.
        """
        for t, v in attrs.items():
            # Explicit VRs for the `gantry` v0.4.1 encrypted-identity
            # tags. They are private, so `dictionary_VR` below raises for
            # them and the fallback would only log a warning -- the tags
            # would silently not be written. Nothing has *written* these
            # since v0.5.0 (47278f8) migrated to (0400,0500); this is the
            # read-back path for stores from that one release, and pairs
            # with the WHITELIST_TAGS exemption in `privacy.py`. Remove
            # one without the other and the sweep strips what this
            # preserves.
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

            vr, encoded = None, None
            try:
                vr = dictionary_VR(Tag(g, e))
            except Exception:
                # Not a standard tag. Almost always a private (odd-group)
                # one, which is the whole point of `remove_private_tags=
                # False`: the caller asked to keep the vendor block, and
                # until #118 this arm only logged, so the tags reached
                # the object graph and the index and then never reached
                # the file (#118).
                encoded = DicomExporter._fallback_encoding(v)

            try:
                if vr is None:
                    if encoded is None:
                        raise ValueError(
                            f"no VR fits a {type(v).__name__} value")
                    vr, v = encoded
                ds.add_new(Tag(g, e), vr, v)
            except Exception as exc:
                # Say "not exported". "Failed to merge" reads like an
                # internal hiccup; this is an element the caller asked
                # for that will not be in the output.
                #
                # Reported by handing it back rather than logging it
                # here: `_merge` runs inside `_export_instance_worker`,
                # which may be in a subprocess with no store handle and
                # -- as the #126 tests show -- no logger the caller can
                # see either. The parent logs it and writes the audit
                # entry (#126).
                loss = f"Tag {t} not exported (data loss): {exc}"
                if losses is None:
                    get_logger().warning(loss)
                    continue
                # The scope is attached here, where `t` is still a tag,
                # not in the parent where it is only a substring of a
                # sentence (#146).
                entry = (loss_scope_for_tag(t), loss)
                if entry not in losses:
                    losses.append(entry)

    # PS3.5 6.2: `LO` is a Long String, 64 characters maximum.
    _LO_MAX = 64

    @staticmethod
    def _fallback_encoding(value) -> Optional[Tuple[str, Any]]:
        """How to write a tag the standard dictionary does not know.

        Returns `(vr, value)` -- picking the VR and encoding the value are
        one decision, not two, because pydicom will accept almost anything
        at `add_new` and only raise when the dataset is written. A wrong
        pairing here does not fail on the offending element; it fails the
        whole export, thousands of instances later, with a `TypeError`
        from `filewriter`.

        PS3.5 A.1 makes `UN` the VR for an unknown value, and for raw
        bytes that is right. It is wrong for everything else: `UN` is an
        OB-family VR and rejects `str` at write time. Text needs a text
        VR, and `LO` caps at 64 characters, so longer values go to `UT`,
        which is unbounded. Numbers are stringified, which is what the
        EAV table (`instance_attributes.value_text`) would have done to
        them anyway -- without it, whether a private tag exported would
        depend on whether a save had happened yet.

        The `UT` branch narrows one thing: `UT` has a value multiplicity
        of 1, where `LO` is 1-n. A backslash-delimited value past 64
        characters therefore round-trips as one string containing literal
        backslashes rather than a list. Widening `LO` to cover it would
        be worse -- an over-long `LO` is non-conformant -- and nothing
        downstream reads these as lists anyway, because they arrive from
        `value_text` already flattened to a single string.

        Returns None when nothing fits, which the caller reports as data
        loss rather than encoding something it would have to guess at.
        """
        if isinstance(value, (bytes, bytearray)):
            return 'UN', bytes(value)
        if isinstance(value, memoryview):
            return 'UN', value.tobytes()
        if isinstance(value, bool):
            # Before `int`: `bool` is a subclass of it, and "True" is a
            # better round-trip than "1" for a value that was set as one.
            return 'LO', str(value)
        if isinstance(value, (int, float)):
            return 'LO', str(value)
        if isinstance(value, str):
            vr = 'LO' if len(value) <= DicomExporter._LO_MAX else 'UT'
            return vr, value
        return None

    @staticmethod
    def _merge_sequences(ds, sequences: Dict[str, Any], losses=None):
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
                # A sequence item is never encoded on its own: pydicom
                # writes it with the enclosing file's encoding, so these
                # flags were read by nothing even before 4.0 drops them.
                ds_item = Dataset()

                # Recursively merge item attributes and sub-sequences
                DicomExporter._merge(ds_item, item.attributes, losses)
                DicomExporter._merge_sequences(ds_item, item.sequences, losses)

                pydicom_seq.append(ds_item)

            ds.add_new(tag, 'SQ', pydicom_seq)
