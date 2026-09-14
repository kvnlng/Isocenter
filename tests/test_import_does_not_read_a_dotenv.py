"""`import isocenter` leaves the process environment alone (#543).

Until 0.9.8 `config_manager.py` ran a bare `load_dotenv()` at import.
python-dotenv searches upward from the *calling module's* directory, so
whether a project's `.env` applied depended on where the virtual
environment happened to live -- and for `python -c` or a REPL, which
dotenv treats as interactive, it searched from the working directory
instead. A library changing a process's environment because it was
imported is a side effect no caller asked for, so the call and the
dependency are gone rather than documented.

The subprocess is `-c` on purpose. It is the one shape in which a bare
`load_dotenv()` reads a `.env` in a temporary directory on any machine,
CI included: a script file beside `.env` makes dotenv search from the
script's package, which is nowhere near the temporary directory, and the
test would pass whether or not the call was there.
"""
import os
import pathlib
import subprocess
import sys

from tests.test_packaging_contract import _declared_dependencies

REPO = pathlib.Path(__file__).resolve().parent.parent
PROBE = "ISOCENTER_PROBE_543"


def test_importing_isocenter_does_not_load_a_dotenv_in_the_working_directory(
        tmp_path):
    (tmp_path / ".env").write_text(f"{PROBE}=from_env\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != PROBE}
    # The tree under test, not whichever copy the editable install points
    # at: the child prints the file it imported and the assertion reads it.
    env["PYTHONPATH"] = str(REPO)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = ("import os, sys, isocenter; print(isocenter.__file__); "
            f"print(os.environ.get({PROBE!r})); print('dotenv' in sys.modules)")

    child = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=120)

    assert child.returncode == 0, child.stderr
    imported, seen, dotenv_loaded = child.stdout.strip().splitlines()[-3:]
    assert pathlib.Path(imported).resolve().is_relative_to(REPO), (
        f"the child imported {imported}, not the tree under test {REPO}")
    assert seen == "None", (
        f"importing isocenter put {PROBE}={seen} from the working "
        f"directory's .env into os.environ")
    assert dotenv_loaded == "False", (
        "importing isocenter imported dotenv; nothing in the package uses it")


def test_python_dotenv_is_not_a_dependency():
    """Nothing imports it, and `pip install isocenter` must not pull in a
    package whose only use was the side effect this removes."""
    assert "python-dotenv" not in _declared_dependencies()
