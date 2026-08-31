# No warning filter is installed here, deliberately (#144).
#
# This module used to run `warnings.filterwarnings("ignore",
# module="pydicom.*")` before anything else. `filterwarnings` prepends
# to the process-wide filter list, so it won even over the host
# application's own `-W` flag: importing Isocenter silenced pydicom
# warnings in the host's own pydicom code, unasked and invisibly.
#
# It was there for non-conformant UID noise -- which came entirely from
# this project's own test fixtures (`SERIES_UID_1` and friends), not
# from user data. `pytest.ini` silences that for the suite independently,
# and removing this line leaves the suite just as quiet, so the filter
# was redundant for the reason it existed and harmful for the reason it
# did not.
#
# It also hid pydicom's own deprecation announcements, which are the
# only signal that would tell us how to lift the `pydicom<4.0` cap in
# `setup.py`. See `tests/test_pydicom_deprecations.py`.
#
# If Isocenter ever needs to suppress a warning its own operations
# provoke, scope it to those calls with a context manager. Do not set a
# global filter at import.

try:
    from .session import DicomSession as Session

    # Expose the Builder for power users
    from .builders import DicomBuilder as Builder

    # Expose Equipment for type hinting
    from .entities import Equipment

    # An exception a caller is expected to catch needs a stable import
    # path, and `isocenter.services` is not one this package advertises.
    from .services import RedactionError

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
# Isocenter used to assign a four-entry priority list to
# `pydicom.config.pixel_data_handlers`. On pydicom 3.x nothing reads it:
# decoding picks its backend from `Dataset._pixel_array_opts`, which
# defaults to `{"use_pdh": False}`, and the handler list is consulted
# only on the `use_pdh` branch. The assignment succeeded and the
# attribute held the list, so it looked configured to anyone reading
# this file -- silent precisely because the attribute is writable.
#
# It was also net-negative if it had ever been read: the list *replaced*
# pydicom's defaults, dropping the jpeg_ls, pylibjpeg and rle handlers
# that ship with it. setup.py requires pydicom>=3.0.0, so there is no
# supported version on which this did anything but narrow support.
#
# pydicom 3.x has no notion of a priority list to migrate it to --
# `pixel_array(..., decoding_plugin=...)` names a single plugin, and the
# `pydicom.pixels` backend orders its own fallbacks per transfer syntax.
# Expressing a preference is therefore a feature, not a repair, and is
# left to #33.
#
# `imagecodecs` support is unaffected, because it never came from this
# list: `Instance.get_pixel_data` calls `isocenter.imagecodecs_handler`
# directly when pydicom fails to decode (see entities.py).

# Declared in _version.py, which setup.py also reads. Deriving it from
# importlib.metadata instead asks "what is installed under this name",
# which is a different question -- and in an editable checkout that has
# drifted from setup.py, a different answer. The version is stamped into
# WFDB annotations.json as producer provenance, so a wrong one becomes a
# wrong claim inside a delivered dataset.
from ._version import __version__
__all__ = ["Session", "Builder", "Equipment", "RedactionError"]
