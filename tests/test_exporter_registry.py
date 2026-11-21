import pytest

from isocenter import exporters


def test_dicom_is_registered_by_default():
    assert "dicom" in exporters.available_formats()


def test_get_exporter_returns_a_callable_exporter():
    exp = exporters.get_exporter("dicom")
    assert hasattr(exp, "export")


def test_unknown_format_raises_with_a_helpful_message():
    with pytest.raises(ValueError) as excinfo:
        exporters.get_exporter("nifti")
    message = str(excinfo.value)
    assert "nifti" in message
    assert "dicom" in message


@pytest.fixture
def _clean_registry():
    """`register()` mutates the module-global `_REGISTRY`. Snapshot and
    restore it so a test that registers throwaway names (e.g. "dummy")
    cannot permanently pollute state for the rest of the test session --
    the original version of this test left "dummy" registered forever.
    """
    before = dict(exporters._REGISTRY)
    yield
    exporters._REGISTRY.clear()
    exporters._REGISTRY.update(before)


def test_registration_is_idempotent_for_the_same_class(_clean_registry):
    """Re-registering under an existing name is a REAL update, not a
    no-op -- and re-registering the identical class again afterward is
    genuinely idempotent (no error, same class still wins).

    Registering the SAME class twice and asserting only that the name is
    present passes whether `register()` does `_REGISTRY[name] = cls`
    (real update) or `_REGISTRY.setdefault(name, cls)` (silently keeps
    whatever was registered FIRST). Registering two DISTINCT classes under
    one name is the only way to tell them apart. Since #783 the classes
    live in `tests/support/plugin_exporters.py`, because `register()`
    refuses a class an export worker cannot import.
    """
    from support.plugin_exporters import Declining, Recorder

    exporters.register("dummy", Recorder)
    assert exporters.get_exporter("dummy").__class__ is Recorder

    exporters.register("dummy", Declining)
    assert exporters.get_exporter("dummy").__class__ is Declining, (
        "re-registering under an existing name did not replace the "
        "stored class -- looks like setdefault semantics, which "
        "silently ignore updates after the first registration")

    exporters.register("dummy", Declining)
    assert exporters.get_exporter("dummy").__class__ is Declining
    assert "dummy" in exporters.available_formats()


def test_registering_a_class_without_write_is_rejected():
    """Was `..._without_export_...` until #783: the method a plugin must
    implement is now `write`."""
    class NotAnExporter:
        pass

    with pytest.raises(TypeError, match="write"):
        exporters.register("bogus", NotAnExporter)
    assert "bogus" not in exporters.available_formats()


# ------------------------------------------- #783: the contract since 1.1
#
# #786 marked the registry provisional until 1.1 in the documentation
# only. #783 replaced it (owner rulings 2026-11-20): tier 2, no longer
# provisional (Q13), and no read-side seam is planned (Q14). These pin that
# the pages say so; the prose is the reviewer's.

import pathlib  # noqa: E402
import re  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REGISTRY_NAMES = ("Exporter", "ExportItem", "register", "get_exporter",
                   "available_formats")


def _section(text, heading):
    """The body under `heading` up to the next heading of its level or
    higher."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    stop = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE)
    end = stop.search(text, start + len(heading))
    return text[start:end.start() if end else len(text)]


def test_the_stability_page_names_the_contract_in_tier_two():
    """In "Documented but internal", under its own heading, no longer
    provisional, and with no read-side seam planned."""
    page = (_REPO / "docs/api/stability.md").read_text(encoding="utf-8")
    tier_two = _section(page, "## Documented but internal")
    sub = " ".join(_section(tier_two, "### The exporter contract").split())
    for name in _REGISTRY_NAMES:
        assert f"`{name}" in sub, name
    assert "They are not provisional." in sub
    assert "provisional until 1.1" not in sub
    assert "exporters.md" in sub
    assert "#783" in sub
    assert "`TypeError`" in sub
    assert "can grade `PASS`" in sub
    assert "none is planned" in sub
    assert "1.1 or later" not in sub


def test_the_exporters_page_is_in_the_nav_and_renders_the_registry():
    """A page in no toctree is built but linked from nowhere."""
    landing = (_REPO / "docs/api/index.md").read_text(encoding="utf-8")
    toctree = re.search(r"^```\{toctree\}\n(.*?)^```$", landing, re.S | re.M)
    assert toctree and re.search(r"^(?:.*<)?exporters>?$", toctree.group(1),
                                 re.MULTILINE), landing
    page = (_REPO / "docs/api/exporters.md").read_text(encoding="utf-8")
    assert ":::{admonition} Provisional" not in page
    assert re.search(r"^\.\. automodule:: isocenter\.exporters$", page,
                     re.MULTILINE)
    members = re.search(r"^\.\. automodule:: isocenter\.exporters\n"
                        r"   :members: (.+)$", page, re.MULTILINE)
    assert members, "the automodule has no `:members:` line directly below it"
    rendered = [name.strip() for name in members.group(1).split(",")]
    for name in _REGISTRY_NAMES:
        assert name in rendered, name
    assert "#783" in page
    # What the dataset a plugin gets has been through (#783).
    assert "redaction zones" in page
    assert "(0088,0200)" in page
    assert "_ISOCENTER_SOURCE_SOP_UID" in page
    assert "def write(self, dataset, item, folder, **options)" in page


def test_the_two_docstrings_name_the_contract():
    for obj in (exporters.Exporter, exporters.register):
        doc = obj.__doc__.lower()
        assert "provisional" not in doc, obj
        assert "write" in doc, obj
