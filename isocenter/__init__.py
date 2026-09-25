"""Isocenter: index, de-identify and export DICOM datasets.

`Session` is the entry point. The package also exports `Builder`,
`Equipment`, `RedactionError` and `ExportError`.
"""
# No warning filter is installed here, deliberately.
#
# `warnings.filterwarnings` prepends to the process-wide filter list, so
# a filter set at import wins even over the host application's own `-W`
# flag, and would silence pydicom warnings in the host's own pydicom
# code. It would also hide pydicom's deprecation announcements, the
# signal for lifting the `pydicom<4.0` cap in `setup.py`.
#
# If Isocenter needs to suppress a warning its own operations provoke,
# scope it to those calls with a context manager. Do not set a global
# filter at import.

try:
    from .session import DicomSession as Session

    from .builders import DicomBuilder as Builder

    from .entities import Equipment

    # An exception a caller is expected to catch needs a stable import
    # path, and `isocenter.services` is not one this package advertises.
    from .services import RedactionError

    # Same reasoning: `isocenter.io_handlers` is no more advertised
    # than `isocenter.services`.
    from .io_handlers import ExportError

    # Expose handler for direct import check
    from . import imagecodecs_handler

except ImportError as e:
    # Catch broken pylibjpeg installations that typically occur on Python 3.14t
    if "_openjpeg" in str(e):
        raise RuntimeError(
            "\n"
            "CRITICAL ERROR: Broken 'pylibjpeg' installation detected.\n"
            "----------------------------------------------------------\n"
            "This environment contains corrupt 'pylibjpeg' packages from a failed build attempt.\n"
            "Isocenter cannot start because 'pydicom' is attempting to load these broken plugins.\n\n"
            "TO FIX: Run this command to clean your environment:\n"
            "    pip uninstall -y pylibjpeg pylibjpeg-openjpeg pylibjpeg-libjpeg pylibjpeg-rle\n"
            "----------------------------------------------------------\n"
        ) from e
    raise

# Codec preference is deliberately NOT expressed here.
#
# Do not assign `pydicom.config.pixel_data_handlers`. On pydicom 3.x
# nothing reads it: decoding picks its backend from
# `Dataset._pixel_array_opts`, which defaults to `{"use_pdh": False}`,
# and the handler list is consulted only on the `use_pdh` branch. The
# assignment succeeds, so it looks configured while doing nothing; and a
# list, if it were read, would *replace* pydicom's defaults and drop the
# jpeg_ls, pylibjpeg and rle handlers that ship with it.
#
# pydicom 3.x has no priority list: `pixel_array(...,
# decoding_plugin=...)` names a single plugin, and the `pydicom.pixels`
# backend orders its own fallbacks per transfer syntax.
#
# `imagecodecs` support does not come from this list: the decode path
# (`io_handlers._decode_pixels`, which `Instance.get_pixel_data` uses)
# calls `isocenter.imagecodecs_handler` itself where pydicom has no
# plugin.

# Declared in _version.py, which setup.py also reads. Deriving it from
# importlib.metadata instead asks "what is installed under this name",
# which is a different question -- and in an editable checkout that has
# drifted from setup.py, a different answer. The version is stamped into
# WFDB annotations.json as producer provenance, so a wrong one becomes a
# wrong claim inside a delivered dataset.
from ._version import __version__
__all__ = ["Session", "Builder", "Equipment", "RedactionError",
           "ExportError"]
