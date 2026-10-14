"""The #1108 acceptance image: a CR with no Device Serial Number and a
name burned into its pixels at a known box.

pydicom's bundled `RG3_UNCR.dcm` is a Fuji CR (Manufacturer `FUJI PHOTO
FILM CO. LTD.`, Model `IRS`, 1760 x 1760, MONOCHROME1, 10 bits stored,
Explicit VR Little Endian) and carries no Device Serial Number, which is
the case #1108 measured: rc18 left every ink pixel of a burned-in name in
the export under a `"*"` zone, and graded PASS. The ink here is a solid
block at the stored maximum inside `BOX`, the region a burned-in name
block occupies on the unexposed border; the tests that read OCR stub it
with a region over the same box, so no font and no tesseract are needed.

`write_inputs` also writes, on request, the same image as a series with a
serial, and as a series with no manufacturer and no model (an
equipment-less series, `Equipment.from_parts` returns None for it), each
with its own UIDs so they are three series of one study.

In `tests/support/` for the reason `project_secret.py` gives.
"""
import os

import pydicom
from pydicom.data import get_testdata_file

#: `[y1, y2, x1, x2]` in zone space: where the name is burned.
BOX = [60, 90, 60, 380]
#: A zone exactly over the name, and one elsewhere on the frame.
OVER = list(BOX)
ELSEWHERE = [1500, 1520, 1500, 1520]
#: The ink's stored value: the frame's maximum for 10 bits stored.
INK = 1023
SERIAL = "SN-1108"
MANUFACTURER = "FUJI PHOTO FILM CO. LTD."
MODEL = "IRS"
_UID_ROOT = "1.2.826.0.1.3680043.8.498.1108"


def _burned(kind):
    """The acceptance image as `kind`: `"none"` (no serial), `"serial"`
    (Device Serial Number `SERIAL`) or `"bare"` (no serial, no
    manufacturer, no model)."""
    ds = pydicom.dcmread(get_testdata_file("RG3_UNCR.dcm"))
    arr = ds.pixel_array.copy()
    y1, y2, x1, x2 = BOX
    arr[y1:y2, x1:x2] = INK
    ds.PixelData = arr.tobytes()
    n = {"none": 1, "serial": 2, "bare": 3}[kind]
    ds.SeriesInstanceUID = f"{_UID_ROOT}.{n}"
    ds.SOPInstanceUID = f"{_UID_ROOT}.{n}.1"
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if kind == "serial":
        ds.DeviceSerialNumber = SERIAL
    if kind == "bare":
        del ds.Manufacturer
        del ds.ManufacturerModelName
    assert "DeviceSerialNumber" in ds or kind != "serial"
    return ds


def source_uid(kind):
    """The SOP Instance UID `kind`'s file is written with."""
    n = {"none": 1, "serial": 2, "bare": 3}[kind]
    return f"{_UID_ROOT}.{n}.1"


def write_inputs(directory, kinds=("none",)):
    """Write one file per kind into `directory` (created), named `<kind>.dcm`.

    Returns:
        str: `directory`.
    """
    os.makedirs(directory, exist_ok=True)
    for kind in kinds:
        _burned(kind).save_as(os.path.join(directory, f"{kind}.dcm"))
    return str(directory)


def ink_left(pixels):
    """How many pixels inside `BOX` still hold the ink value."""
    y1, y2, x1, x2 = BOX
    return int((pixels[y1:y2, x1:x2] == INK).sum())


#: Every pixel of the box is ink.
INK_PIXELS = (BOX[1] - BOX[0]) * (BOX[3] - BOX[2])
