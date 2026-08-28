"""Importing isocenter must not change the host's warning filters (#144).

`isocenter/__init__.py` used to run, before anything else:

    warnings.filterwarnings("ignore", module="pydicom.*")

`filterwarnings` prepends to the process-wide filter list, so it won
over filters the *user* set -- including `-W` on the command line. An
application that imported Isocenter lost pydicom warnings in its own
pydicom code, without asking and without a way to see why.

Silencing a dependency's diagnostics is a reasonable thing for an
application to choose. It is not a library's call to make on its host's
behalf.

These run in a subprocess on purpose. `warnings.catch_warnings()` saves
and restores the filter list, so a filter installed at import time is
invisible from inside a `catch_warnings` block -- an in-process test
would pass whether or not the filter existed. The question is what a
fresh interpreter does, so the test asks a fresh interpreter.
"""
import subprocess
import sys
import textwrap

import pytest


def _run(body):
    """Run `body` in a fresh interpreter with warnings made visible."""
    return subprocess.run(
        [sys.executable, "-W", "always::DeprecationWarning", "-c",
         textwrap.dedent(body)],
        capture_output=True, text=True, timeout=180)


#: Provokes a pydicom DeprecationWarning without touching Isocenter.
_PROVOKE = '''
    import io
    from pydicom.dataset import FileDataset, FileMetaDataset
    from pydicom.uid import ImplicitVRLittleEndian
    m = FileMetaDataset()
    m.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    m.MediaStorageSOPInstanceUID = "1.2.3"
    m.TransferSyntaxUID = ImplicitVRLittleEndian
    d = FileDataset(None, {}, file_meta=m, preamble=b"\\0" * 128)
    d.PatientID = "X"
    d.save_as(io.BytesIO(), write_like_original=False)
'''


def test_the_baseline_actually_warns():
    """Guards the test below. If pydicom stopped emitting this warning,
    the real test would pass by vacuum rather than by behaviour."""
    result = _run(_PROVOKE)

    assert "DeprecationWarning" in result.stderr, (
        "pydicom no longer warns here; this module's premise is stale\n"
        + result.stderr)


def test_importing_isocenter_does_not_silence_pydicom_for_the_host():
    """The defect, stated as the user experiences it."""
    result = _run("    import isocenter\n" + _PROVOKE)

    assert "DeprecationWarning" in result.stderr, (
        "importing isocenter suppressed a pydicom warning the user "
        "explicitly asked for with -W\n" + result.stderr)


def test_importing_isocenter_installs_no_pydicom_warning_filter():
    """The structural half.

    Pinned separately from the behavioural test because a future filter
    might suppress something other than the one warning above, and the
    rule is "Isocenter does not filter pydicom for its host", not "this
    particular warning survives".

    Scoped to filters naming pydicom rather than asserting *no* new
    filters: importing Isocenter pulls in numpy, urllib3, and requests,
    each of which installs its own -- so a blanket assertion would fail
    for things this project neither did nor can fix.
    """
    result = _run('''
        import warnings
        before = list(warnings.filters)
        import isocenter
        added = [f for f in warnings.filters if f not in before]
        # f = (action, message, category, module, lineno); `module` is a
        # compiled pattern when the filter was set with `module=`.
        pydicom_filters = [
            f for f in added
            if (getattr(f[3], "pattern", "") or "").find("pydicom") != -1
            or (getattr(f[1], "pattern", "") or "").find("pydicom") != -1
        ]
        print("PYDICOM_FILTERS:", pydicom_filters)
    ''')

    assert "PYDICOM_FILTERS: []" in result.stdout, (
        "importing isocenter installed a process-wide filter targeting "
        "pydicom:\n" + result.stdout + result.stderr)
