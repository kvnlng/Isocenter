"""One file through every pixel decode door, for the bunch F1 tests.

Every door that turns an encapsulated file into an array now calls
`io_handlers._decode_pixels` (#453): ingest, the export readback, an
icon, and `Instance.get_pixel_data()` from a file. A door-parity test
writes one file and asks three of them -- `ingest()` (the stored array
and label, or the failure words), `_decode_pixels` itself, and a bare
`Instance(file_path=...)` -- whether they agree.

**`pydicom_cannot`** (a pytest fixture) makes pydicom's decoder raise
the `RuntimeError` it raises when it has no plugin, for the syntaxes the
imagecodecs fallback takes, so a shape Pillow decodes is also answered by
the fallback. A differential test runs with and without it: the fallback
must return Pillow's array on every shape Pillow decodes.

It is a monkeypatch, and a monkeypatch does not reach a spawned ingest
worker. So the fixture also sets `ISOCENTER_FORCE_THREADS=1`, and it
counts its own calls: `calls["n"]` stays 0 when nothing in this process
asked pydicom, which is how a test proves its ingest half measured the
fallback and not Pillow a second time (brief F §5.1, attack A20b).

In `tests/support/` for the reason `project_secret.py` gives. Every
`isocenter` import is inside a function, so the mutation probe charges
coverage to the tests that call these, not to this helper.
"""
import os

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.encaps import encapsulate
from pydicom.pixels.decoders.base import Decoder
from pydicom.uid import generate_uid

LJPEG = "1.2.840.10008.1.2.4.57"
LJPEG_SV1 = "1.2.840.10008.1.2.4.70"
JPEGLS = "1.2.840.10008.1.2.4.80"
JPEGLS_NEAR = "1.2.840.10008.1.2.4.81"
J2K_LOSSLESS = "1.2.840.10008.1.2.4.90"
J2K = "1.2.840.10008.1.2.4.91"
HTJ2K_LOSSLESS = "1.2.840.10008.1.2.4.201"
HTJ2K_RPCL = "1.2.840.10008.1.2.4.202"
HTJ2K = "1.2.840.10008.1.2.4.203"
EXPLICIT_LE = "1.2.840.10008.1.2.1"
SOP_CLASS = "1.2.840.10008.5.1.4.1.1.7"

#: The words the fixture's refusal carries, so a test can tell it from a
#: real plugin failure.
FORCED = ("Unable to decompress pixel data because all plugins are "
          "missing dependencies (pydicom_cannot)")


def dataset(ts, codestreams=None, *, rows, cols, samples=1, bits_allocated,
            bits_stored=None, high_bit=None, pixel_representation=0,
            photometric=None, planar=0, frames=None, native=None, drop=()):
    """A one-instance file under `ts`: encapsulated `codestreams`, or `native`.

    `high_bit` defaults to BitsStored - 1; `drop` deletes the named
    keywords last, so a Type 1 element can be made absent.
    """
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = SOP_CLASS
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ts
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.PatientID, ds.PatientName = "PATF1", "DOE^JOHN"
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID = SOP_CLASS
    ds.Modality, ds.SeriesNumber, ds.InstanceNumber = "OT", 1, 1
    ds.StudyDate = "20230101"
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = samples
    if samples > 1 and planar is not None:
        ds.PlanarConfiguration = planar
    ds.PhotometricInterpretation = photometric or (
        "RGB" if samples > 1 else "MONOCHROME2")
    ds.BitsAllocated = bits_allocated
    ds.BitsStored = bits_stored or bits_allocated
    ds.HighBit = (ds.BitsStored - 1) if high_bit is None else high_bit
    ds.PixelRepresentation = pixel_representation
    if frames is not None:
        ds.NumberOfFrames = frames
    if native is not None:
        ds.PixelData = native
    else:
        ds.PixelData = encapsulate(list(codestreams), has_bot=True)
        ds["PixelData"].is_undefined_length = True
    for keyword in drop:
        if keyword in ds:
            delattr(ds, keyword)
    return ds


def write(folder, ds, name="one"):
    """Save `ds` alone in `folder/name/` and return the file's path."""
    src = os.path.join(str(folder), name)
    os.makedirs(src)
    path = os.path.join(src, "one.dcm")
    ds.save_as(path, enforce_file_format=False)
    return path


def outcome(read):
    """`read()`, or the exception it raised."""
    try:
        return read()
    except Exception as exc:  # pylint: disable=broad-except
        return exc


def at_decode_pixels(path, **kwargs):
    """`(array, label)` from `_decode_pixels`, or its exception."""
    from isocenter.io_handlers import _decode_pixels  # pylint: disable=import-outside-toplevel
    return outcome(lambda: _decode_pixels(pydicom.dcmread(path, force=True),
                                          **kwargs))


def at_instance(path):
    """`(array, label)` from a bare file-backed Instance, or its exception."""
    from isocenter.entities import Instance  # pylint: disable=import-outside-toplevel
    inst = Instance(generate_uid(), SOP_CLASS, 1, file_path=path)

    def _read():
        return inst.get_pixel_data(), inst.attributes.get("0028,0004")
    return outcome(_read)


def at_ingest(tmp_path, path, name="s"):
    """What `ingest()` makes of the folder holding `path`.

    `{"array", "label", "attributes", "failure", "rows"}`: the stored
    array after `unload_pixel_data()` (the sidecar's answer), its label
    and pixel descriptors, the failure reason when the file was refused,
    and every `WARNING` and `DATA_LOSS` row as `(action, entity, details)`.
    """
    import sqlite3  # pylint: disable=import-outside-toplevel
    from isocenter.session import DicomSession  # pylint: disable=import-outside-toplevel
    db = os.path.join(str(tmp_path), f"{name}.db")
    got = {"array": None, "label": None, "attributes": None,
           "failure": None, "rows": []}
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(os.path.dirname(path))
        if summary.failures:
            got["failure"] = summary.failures[0][1]
        instances = [i for p in session.store.patients for st in p.studies
                     for se in st.series for i in se.instances]
        if instances:
            inst = instances[0]
            assert inst.unload_pixel_data() is True
            got["array"] = inst.get_pixel_data()
            got["label"] = inst.attributes.get("0028,0004")
            got["attributes"] = {t: inst.attributes.get(t) for t in (
                "0028,0100", "0028,0101", "0028,0102", "0028,0103")}
            got["uid"] = inst.sop_instance_uid
    with sqlite3.connect(db) as conn:
        got["rows"] = conn.execute(
            "SELECT action_type, entity_uid, details FROM audit_log "
            "WHERE action_type IN ('WARNING', 'DATA_LOSS')").fetchall()
    return got


def same(arr, want):
    """Dtype, shape and every value -- the whole of "the same array"."""
    return (isinstance(arr, np.ndarray) and arr.dtype == want.dtype
            and arr.shape == want.shape and arr.tolist() == want.tolist())


@pytest.fixture
def pydicom_cannot(monkeypatch):
    """pydicom has no plugin for any imagecodecs fallback syntax, here.

    Yields `{"n": calls}`. Syntaxes outside the fallback set decode as
    they always do. `ISOCENTER_FORCE_THREADS=1` so an ingest in this
    test runs its worker in this process, where the patch is.
    """
    from isocenter.io_handlers import _IMAGECODECS_FALLBACK_SYNTAXES  # pylint: disable=import-outside-toplevel
    real = Decoder.as_array
    calls = {"n": 0}

    def cannot(self, src, **kwargs):
        if str(self.UID) not in _IMAGECODECS_FALLBACK_SYNTAXES:
            return real(self, src, **kwargs)
        calls["n"] += 1
        raise RuntimeError(FORCED)

    monkeypatch.setattr(Decoder, "as_array", cannot)
    monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
    yield calls


@pytest.fixture(params=["pydicom-first", "pydicom-cannot"])
def route(request, monkeypatch):
    """Both routes a fallback syntax can take; yields the call counter.

    `None` on the pydicom-first route. On the other, the counter a test
    asserts moved, so it knows the fallback was what answered.
    """
    if request.param == "pydicom-first":
        monkeypatch.setenv("ISOCENTER_FORCE_THREADS", "1")
        yield None
        return
    yield request.getfixturevalue("pydicom_cannot")
