"""A codec module that did not load gets the install advice, naming it (#1092).

imagecodecs imports its codec modules lazily, at the first call of a
codec function. A module that cannot load (a shared library missing from
a broken wheel) raises `DelayedImportError`, an `ImportError`, from that
call. Measured on 3e4ee91a (3.12.14 and 3.14.7t), with
`imagecodecs._ljpeg` refused by an import hook, pydicom's `JPEG-LL.dcm`
read through `Instance.get_pixel_data()` gave

    Lazy load failed for instance ...: RuntimeError: ...; imagecodecs
    could not decode it either: DelayedImportError: could not import
    name 'ljpeg_decode' from 'imagecodecs' (caused by DelayedImportError:
    ...)

and no advice: the one state where the installation is at fault got the
message for a file at fault. Now the fallback raises the installation's
refusal, `ImagecodecsUnavailable`, naming the module from this library's
own table by syntax (`imagecodecs_handler.CODEC_MODULES`), and the read
door's advice names it and the command that shows the import's error.

The hook must be in place before the module's first import in that
process, so every case runs in a fresh interpreter, with `COVERAGE_*`
stripped from its environment (CLAUDE.md). An ingest's spawned workers
get the hook through a `sitecustomize.py` on the child's `PYTHONPATH`.

Names `isocenter.imagecodecs_handler` and `isocenter.entities`.
"""
import importlib
import json
import os
import pickle
import subprocess
import sys
import textwrap

import imagecodecs
import pytest
from pydicom.data import get_testdata_file

from isocenter import imagecodecs_handler
from isocenter.imagecodecs_handler import (CODEC_MODULES,
                                           SUPPORTED_TRANSFER_SYNTAXES,
                                           ImagecodecsUnavailable,
                                           JPEGLossless)

#: A loader's message for a module that would not load, with an install
#: path made up for this test.
FAKE_PATH = "/Users/someone/venv/lib/site-packages/imagecodecs/_x.so"

#: The hook: refuse one `imagecodecs.<module>`, named by the environment.
SITECUSTOMIZE = textwrap.dedent('''
    import importlib.abc, os, sys

    class _Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "imagecodecs." + os.environ["X7_BLOCK"]:
                raise ImportError(
                    "dlopen(" + os.environ["X7_FAKE_PATH"] + "): Library "
                    "not loaded", name=name)
            return None

    sys.meta_path.insert(0, _Block())
''')

#: Read one file through the read door, pydicom first refused for every
#: fallback syntax (as `support.decode_doors.pydicom_cannot` does), and
#: print what was raised.
READ_DOOR = textwrap.dedent('''
    import json, sys
    import pydicom
    from pydicom.pixels.decoders.base import Decoder
    from isocenter.entities import Instance
    from isocenter.io_handlers import _IMAGECODECS_FALLBACK_SYNTAXES
    real = Decoder.as_array
    def cannot(self, src, **kwargs):
        if str(self.UID) in _IMAGECODECS_FALLBACK_SYNTAXES:
            raise RuntimeError("all plugins are missing dependencies")
        return real(self, src, **kwargs)
    Decoder.as_array = cannot
    path = sys.argv[1]
    ds = pydicom.dcmread(path)
    inst = Instance(str(ds.SOPInstanceUID), str(ds.SOPClassUID), 1,
                    file_path=path)
    try:
        inst.get_pixel_data()
        print(json.dumps({"read": True}))
    except Exception as exc:
        print(json.dumps({"read": False, "message": str(exc)}))
''')

#: Ingest one folder through a real `Session.ingest()`, on its spawned
#: pool, and print the failures and the stored rows.
INGEST = textwrap.dedent('''
    import json, os, sqlite3, sys
    from isocenter.session import DicomSession
    folder, db = sys.argv[1], sys.argv[2]
    with DicomSession(persistence_file=db) as session:
        summary = session.ingest(folder)
    with sqlite3.connect(db) as conn:
        rows = [d for (d,) in conn.execute(
            "SELECT details FROM audit_log WHERE action_type = 'ERROR'")]
    print(json.dumps({"failures": [f[1] for f in summary.failures],
                      "rows": rows}))
''')


def _child(tmp_path, block, script, *args):
    """Run `script` in a fresh interpreter with `imagecodecs.<block>` refused."""
    hook = tmp_path / "hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("COVERAGE_")}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(hook)] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep)
                       if p])
    env["X7_BLOCK"] = block
    env["X7_FAKE_PATH"] = FAKE_PATH
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    done = subprocess.run([sys.executable, "-c", script, *args], env=env,
                          capture_output=True, text=True, timeout=300,
                          check=False)
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _advice(module):
    return (f"Missing image codecs: imagecodecs imported, and its codec "
            f"module imagecodecs.{module} did not load. This library reads "
            f"this transfer syntax with it where pydicom cannot; reinstall "
            f"imagecodecs. Running `python -c \"import imagecodecs.{module}\"`"
            f" shows the import's own error.")


@pytest.mark.parametrize("name, module", [
    ("JPEG-LL.dcm", "_ljpeg"),                    # .70
    ("JPEG-lossy.dcm", "_jpeg8"),                 # .51, 12-bit
    ("MR_small_jpeg_ls_lossless.dcm", "_jpegls"),  # .80
    ("JPEG2000.dcm", "_jpeg2k"),                  # .91
])
def test_a_blocked_codec_module_gets_advice_naming_it(tmp_path, name, module):
    """Red if the `DelayedImportError` catch or the arm-1 branch is removed."""
    path = get_testdata_file(name)
    got = _child(tmp_path, module, READ_DOOR, path)

    assert got["read"] is False
    message = got["message"]
    assert message.startswith("Failed to decompress pixel data for instance ")
    assert message.splitlines()[-1] == _advice(module)
    assert (f"ImagecodecsUnavailable: imagecodecs is not available: its "
            f"codec module imagecodecs.{module} did not load") in message
    assert "DelayedImportError" not in message
    assert "caused by" not in message


def test_no_path_reaches_the_advice(tmp_path):
    """The hook's `ImportError` names an install path; nothing quotes it.

    Red if `from None` is dropped: the loader's message would ride the
    chain into `describe_exception`'s tail.
    """
    got = _child(tmp_path, "_ljpeg", READ_DOOR,
                 get_testdata_file("JPEG-LL.dcm"))
    assert FAKE_PATH not in got["message"]
    assert "someone" not in got["message"]
    assert "dlopen" not in got["message"]


def test_the_ingest_row_names_the_module_once(tmp_path):
    """Through `Session.ingest()`, whose workers are spawned interpreters.

    Ingest gives no advice; its row ends in the refusal's own words, once.
    Red if the row holds `DelayedImportError`, `caused by` or the path.
    """
    folder = tmp_path / "in"
    folder.mkdir()
    with open(get_testdata_file("JPEG-LL.dcm"), "rb") as source:
        (folder / "one.dcm").write_bytes(source.read())
    got = _child(tmp_path, "_ljpeg", INGEST, str(folder),
                 str(tmp_path / "s.db"))

    said = ("ImagecodecsUnavailable: imagecodecs is not available: its "
            "codec module imagecodecs._ljpeg did not load")
    assert len(got["failures"]) == 1 and len(got["rows"]) == 1, got
    for text in (got["failures"][0], got["rows"][0]):
        assert text.endswith(f"; imagecodecs could not decode it either: "
                             f"{said}"), text
        assert text.count(said) == 1
        assert "DelayedImportError" not in text
        assert "caused by" not in text
        assert FAKE_PATH not in text


def test_the_codec_refusal_pickles_with_its_module():
    refusal = imagecodecs_handler._codec_module_unavailable(JPEGLossless)  # pylint: disable=protected-access
    back = pickle.loads(pickle.dumps(refusal))
    assert type(back) is ImagecodecsUnavailable  # pylint: disable=unidiomatic-typecheck
    assert back.codec_module == "_ljpeg"
    assert str(back) == ("imagecodecs is not available: its codec module "
                         "imagecodecs._ljpeg did not load")


#: The inner codec function each module's arm of `_decode_frame` reaches.
#: `jpeg_decode` is a Python dispatcher, not an extension attribute; it
#: calls `jpeg8_decode`.
_FUNCTION = {"_ljpeg": "ljpeg_decode", "_jpeg8": "jpeg8_decode",
             "_jpegls": "jpegls_decode", "_jpeg2k": "jpeg2k_decode"}


def test_each_supported_syntax_names_a_codec_module_imagecodecs_has():
    """An imagecodecs rename goes red here, not at a user's door."""
    assert set(CODEC_MODULES) == set(SUPPORTED_TRANSFER_SYNTAXES)
    attributes = imagecodecs.imagecodecs._ATTRIBUTES  # pylint: disable=protected-access
    for syntax, module in CODEC_MODULES.items():
        importlib.import_module(f"imagecodecs.{module}")
        assert attributes[_FUNCTION[module]] == module, (syntax, module)


def test_a_whole_import_failure_keeps_its_own_advice(monkeypatch):
    """Arm 1 without a module: imagecodecs itself did not import."""
    from isocenter.entities import _missing_decoder_words  # pylint: disable=import-outside-toplevel
    monkeypatch.setattr(imagecodecs_handler, "imagecodecs", None)
    monkeypatch.setattr(imagecodecs_handler, "IMPORT_ERROR",
                        ImportError("gone"))
    refusal = imagecodecs_handler._unavailable()  # pylint: disable=protected-access
    assert refusal.codec_module is None
    assert _missing_decoder_words(refusal, None) == (
        "imagecodecs did not import. This library installs it and reads "
        "this transfer syntax with it where pydicom cannot; reinstall "
        "imagecodecs. Running `python -c \"import imagecodecs\"` shows the "
        "import's own error.")


@pytest.mark.parametrize("module", ["", "_x/../y", "a b", None, 5])
def test_a_module_name_that_is_no_identifier_gets_the_plain_advice(module):
    """Only a dotted identifier reaches the sentence and the command."""
    from isocenter.entities import _missing_decoder_words  # pylint: disable=import-outside-toplevel
    refusal = ImagecodecsUnavailable("imagecodecs is not available")
    refusal.codec_module = module
    assert _missing_decoder_words(refusal, None).startswith(
        "imagecodecs did not import.")
