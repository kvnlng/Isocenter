import pytest

from gantry import exporters


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


def test_registration_is_idempotent_for_the_same_class():
    class Dummy:
        def export(self, session, folder, **options):
            return []

    exporters.register("dummy", Dummy)
    exporters.register("dummy", Dummy)
    assert "dummy" in exporters.available_formats()


def test_registering_a_class_without_export_is_rejected():
    class NotAnExporter:
        pass

    with pytest.raises(TypeError):
        exporters.register("bogus", NotAnExporter)
