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
