"""
Builder pattern implementation for constructing DICOM entity hierarchies.

This module provides a fluent interface for creating Patient, Study, Series,
and Instance objects in a structured way.
"""
from .entities import Patient, Study, Series, Instance, Equipment


class DicomBuilder:
    """
    Factory for creating fluent Dicom hierarchy builders.

    Usage:
       patient = DicomBuilder.start_patient("P123", "Doe^John")
           .add_study("1.2.3", "20240101")
           .add_series("1.2.3.1", "CT", "1")
           .end_study().build()
    """
    @staticmethod
    def start_patient(patient_id, name):
        """Begin building a Patient.

        The first parameter was `id` until the 1.0 freeze (#26), which
        shadowed the builtin; it was renamed, not aliased, so
        `start_patient(id=...)` raises `TypeError`.
        """
        return PatientBuilder(patient_id, name)


class PatientBuilder:
    """Fluent Builder for Patient entities."""

    def __init__(self, patient_id, name):
        self.patient = Patient(patient_id, name)

    def add_study(self, uid, date):
        """Adds a child Study to this Patient."""
        s = Study(uid, date)
        self.patient.studies.append(s)
        return StudyBuilder(self, s)

    def build(self):
        """Returns the fully constructed Patient object."""
        return self.patient


class StudyBuilder:
    """Fluent Builder for Study entities."""

    def __init__(self, parent, study):
        self.parent = parent
        self.study = study

    def add_series(self, uid, mod, num):
        """Adds a child Series to this Study."""
        s = Series(uid, mod, num)
        self.study.series.append(s)
        return SeriesBuilder(self, s)

    def end_study(self):
        """Finishes the Study configuration and returns the parent PatientBuilder."""
        return self.parent


class SeriesBuilder:
    """Fluent Builder for Series entities."""

    def __init__(self, parent, series):
        self.parent = parent
        self.series = series

    def set_equipment(self, man, mod, sn=""):
        """Sets the Equipment metadata for this Series.

        Routed through `Equipment.from_parts`, so a call with neither a
        manufacturer nor a model name leaves `series.equipment` as
        `None` -- the same answer ingest and reload give (#290). Before
        that this was the one construction site with no predicate, and
        `.set_equipment("", "", "SN")` built an `Equipment` that
        `save_all` wrote and no reload could return.

        **Also writes the equipment onto every instance** -- those already
        added, and (through `add_instance`) those added later -- as
        Manufacturer (0008,0070), Manufacturer's Model Name (0008,1090)
        and Device Serial Number (0018,1000), each only when given (#570).
        The instance is where the export reads equipment from; the writer
        re-stamping it from `Series.equipment` put a serial back into a
        file after `anonymize()` had removed it from the instance. The
        latest write wins: a `set_attribute` of one of the three tags
        before this call is overwritten by it, and one after is kept.
        """
        self.series.equipment = Equipment.from_parts(man, mod, sn)
        for inst in self.series.instances:
            self._stamp_equipment(inst)
        return self

    def _stamp_equipment(self, inst):
        """Copy the non-empty parts of `Series.equipment` onto `inst`."""
        equipment = self.series.equipment
        if equipment is None:
            return
        for tag, value in (("0008,0070", equipment.manufacturer),
                           ("0008,1090", equipment.model_name),
                           ("0018,1000", equipment.device_serial_number)):
            if value:
                inst.set_attr(tag, value)

    def add_instance(self, uid, cls, num):
        """Adds a child Instance to this Series, carrying the series'
        equipment tags if `set_equipment` has already run (#570)."""
        inst = Instance(uid, cls, num)
        self._stamp_equipment(inst)
        self.series.instances.append(inst)
        return InstanceContextBuilder(self, inst)

    def end_series(self):
        """Finishes the Series configuration and returns the parent StudyBuilder."""
        return self.parent


class InstanceContextBuilder:
    """Fluent context for configuring a single Instance."""

    def __init__(self, parent, instance):
        self.parent = parent
        self.instance = instance

    def set_attribute(self, tag, val):
        """Sets a generic DICOM attribute."""
        self.instance.set_attr(tag, val)
        return self

    def set_pixel_data(self, arr):
        """Injects pixel data (numpy array)."""
        self.instance.set_pixel_data(arr)
        return self

    def end_instance(self):
        """Finishes the Instance and returns the parent SeriesBuilder."""
        return self.parent
