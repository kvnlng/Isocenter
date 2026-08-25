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
    def start_patient(id, name):
        """Begin building a Patient."""
        return PatientBuilder(id, name)


class PatientBuilder:
    """Fluent Builder for Patient entities."""

    def __init__(self, id, name): self.patient = Patient(id, name)

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

    def __init__(self, parent, study): self.parent, self.study = parent, study

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

    def __init__(self, parent, series): self.parent, self.series = parent, series

    def set_equipment(self, man, mod, sn=""):
        """Sets the Equipment metadata for this Series."""
        self.series.equipment = Equipment(man, mod, sn)
        return self

    def add_instance(self, uid, cls, num):
        """Adds a child Instance to this Series."""
        inst = Instance(uid, cls, num)
        self.series.instances.append(inst)
        return InstanceContextBuilder(self, inst)

    def end_series(self):
        """Finishes the Series configuration and returns the parent StudyBuilder."""
        return self.parent


class InstanceContextBuilder:
    """Fluent context for configuring a single Instance."""

    def __init__(self, parent, instance): self.parent, self.instance = parent, instance

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
