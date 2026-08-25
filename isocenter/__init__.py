import warnings
# Suppress all pydicom warnings (e.g. strict UID validation)
warnings.filterwarnings("ignore", module="pydicom.*")

try:
    from .session import DicomSession as Session

    # Expose the Builder for power users
    from .builders import DicomBuilder as Builder

    # Expose Equipment for type hinting
    from .entities import Equipment

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

# Configure pydicom handlers
# We prioritize pylibjpeg (if installed) and pillow.
# GDCM is often problematic to install via pip, so pylibjpeg is preferred for JPEG/JPEG-LS/RLE.
try:
    from pydicom import config as pydicom_config
    import pydicom.pixel_data_handlers.gdcm_handler as gdcm_handler
    import pydicom.pixel_data_handlers.pillow_handler as pillow_handler
    import pydicom.pixel_data_handlers.numpy_handler as numpy_handler
    from . import imagecodecs_handler

    # We explicitly define the priority list using module objects
    pydicom_config.pixel_data_handlers = [
        gdcm_handler,
        imagecodecs_handler,
        pillow_handler,
        numpy_handler
    ]
except ImportError:
    pass

try:
    from importlib.metadata import version, PackageNotFoundError
except ImportError:
    # Backport for older Pythons if needed, though Isocenter requires 3.9+ where this is standard
    from importlib_metadata import version, PackageNotFoundError

try:
    __version__ = version("isocenter")
except PackageNotFoundError:
    # Package is not installed
    __version__ = "0.0.0"
__all__ = ["Session", "Builder", "Equipment"]
