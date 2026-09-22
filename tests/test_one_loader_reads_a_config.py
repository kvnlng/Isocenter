"""One loader reads a configuration (#729).

`ConfigLoader` had three doors into a file: `load_unified_config`, which
`load_config()` and `audit(config_path=)` use and which checks everything
(#711-#714); `load_redaction_rules`, which read `machines` and checked
nothing at the top level; and `load_phi_config`, which returned a file's
own `phi_tags` with no profile merged and no `validate_phi_policy`. Nothing
in the package called the second, and the third only through
`PhiInspector(config_path=)`, which nothing called: `audit()` hands the
inspector the policy itself. A file one door accepts and another refuses
is two readings of one configuration, so both are deleted before the
freeze (owner ruling, #729), and so is the inspector's path arm. The
deleted spellings are pinned in `test_api_coherence.py`, the house list.

**Why this file imports what it does.** `PhiInspector` through
`isocenter.privacy` and `scan_worker` through `isocenter.session`.
"""
import inspect

import pytest

from isocenter.entities import Patient
from isocenter.privacy import PhiInspector
from isocenter.profiles import FLOOR_POLICY
from isocenter.session import scan_worker


def test_the_inspector_takes_a_policy_not_a_path():
    """Kills the `config_path` arm kept, or kept under another reading."""
    assert "config_path" not in inspect.signature(PhiInspector.__init__).parameters
    with pytest.raises(TypeError):
        PhiInspector(config_path="c.yaml")  # pylint: disable=unexpected-keyword-arg


def test_an_inspector_with_no_policy_applies_a_copy_of_the_floor():
    """`PhiInspector()` keeps the floor, as a bare session does (#495), and
    a copy of it: the inspector normalizes what it holds, and a caller may
    edit it. Kills the default emptied, and the module table handed out."""
    inspector = PhiInspector()
    assert inspector.phi_tags == FLOOR_POLICY
    assert inspector.phi_tags is not FLOOR_POLICY
    inspector.phi_tags["0010,0010"]["action"] = "KEEP"
    assert FLOOR_POLICY["0010,0010"]["action"] != "KEEP"


def test_the_scan_worker_refuses_a_policy_that_is_not_a_mapping():
    """`audit()` always sends the policy itself. The worker's other arm
    read anything else as a file path, and a `None` there scanned against
    the floor while the caller's policy said nothing of the kind. Kills
    that arm kept."""
    with pytest.raises(TypeError, match="policy"):
        scan_worker((Patient("P1", "N"), "c.yaml", False, b"\0" * 32))
