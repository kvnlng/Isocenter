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

    The original version of this test registered the SAME class twice
    and asserted only that the name was still present. That passes
    identically whether `register()` does `_REGISTRY[name] = cls`
    (real update) or `_REGISTRY.setdefault(name, cls)` (silently keeps
    whatever was registered FIRST and ignores every later call) --
    because with the same class object either way, the end state is
    bit-identical. Registering two DISTINCT classes under one name is
    the only way to tell them apart: `setdefault` would keep `First`
    and silently ignore `Second`.
    """
    class First:
        def export(self, session, folder, **options):
            return []

    class Second:
        def export(self, session, folder, **options):
            return []

    exporters.register("dummy", First)
    assert exporters.get_exporter("dummy").__class__ is First

    exporters.register("dummy", Second)
    assert exporters.get_exporter("dummy").__class__ is Second, (
        "re-registering under an existing name did not replace the "
        "stored class -- looks like setdefault semantics, which "
        "silently ignore updates after the first registration")

    # Re-registering the SAME (Second) class again: genuinely idempotent.
    exporters.register("dummy", Second)
    assert exporters.get_exporter("dummy").__class__ is Second
    assert "dummy" in exporters.available_formats()


def test_registering_a_class_without_export_is_rejected():
    class NotAnExporter:
        pass

    with pytest.raises(TypeError):
        exporters.register("bogus", NotAnExporter)


# ------------------------------------------------ #527: provisional at 1.0
#
# Owner ruling Q1 (2026-09-23): the registry is marked provisional in the
# documentation only -- a named tier-2 subsection on the stability page,
# the two docstrings, and its own page in the API reference. No runtime
# warning: both built-ins register at import, so one would fire on every
# `import isocenter` (PEP 411 marks provisional packages the same way).
# These pin that the marking is there; the prose is the reviewer's.

import pathlib  # noqa: E402
import re  # noqa: E402

_REPO = pathlib.Path(__file__).resolve().parent.parent
_REGISTRY_NAMES = ("Exporter", "register", "get_exporter", "available_formats")


def _section(text, heading):
    """The body under `heading` up to the next heading of its level or
    higher."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading + "\n")
    stop = re.compile(rf"^#{{1,{level}}} ", re.MULTILINE)
    end = stop.search(text, start + len(heading))
    return text[start:end.start() if end else len(text)]


def test_the_stability_page_names_the_registry_provisional_in_tier_two():
    """In "Documented but internal", under its own heading: tier 2 is
    what "may change in a 1.x with a CHANGELOG entry" already means, and
    the subsection is what says the change is expected."""
    page = (_REPO / "docs/api/stability.md").read_text(encoding="utf-8")
    tier_two = _section(page, "## Documented but internal")
    sub = _section(tier_two, "### The exporter registry: provisional until 1.1")
    for name in _REGISTRY_NAMES:
        assert f"`{name}" in sub, name
    assert "provisional" in sub
    assert "exporters.md" in sub
    assert "#783" in sub
    assert "isocenter>=1.0,<1.1" in sub


def test_the_exporters_page_is_in_the_nav_and_renders_the_registry():
    """A page outside the nav is not built into the site, and
    `test_doc_anchors` does not read it."""
    nav = (_REPO / "mkdocs.yml").read_text(encoding="utf-8")
    assert re.search(r"^\s+- '[^']+': api/exporters\.md$", nav, re.MULTILINE), nav
    page = (_REPO / "docs/api/exporters.md").read_text(encoding="utf-8")
    assert '!!! warning "Provisional"' in page
    assert re.search(r"^::: isocenter\.exporters$", page, re.MULTILINE)
    for name in _REGISTRY_NAMES:
        assert f"- {name}\n" in page, name
    assert "#783" in page
    assert "REVIEW_REQUIRED" in page


def test_the_two_docstrings_say_provisional():
    for obj in (exporters.Exporter, exporters.register):
        assert "provisional until 1.1" in obj.__doc__.lower(), obj
