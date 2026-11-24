"""Exporter plugins for the tests of the exporter contract (#783).

They live in a module, not in a test file's body, because `write()` runs in
an export worker process: the worker imports the class by its module and
name, and a class defined in a test function or a notebook cannot be
imported there (`exporters.register` refuses it).

Each writer records what it was handed in a JSON file beside its output,
because the test reads it from another process.
"""
import json
import os
import time

from pydicom.multival import MultiValue

from isocenter import exporters


def _icons_in(dataset):
    """Every Icon Image Sequence at any depth of `dataset`."""
    found = 0
    for element in dataset.iterall():
        if element.tag == 0x00880200:
            found += 1
    return found


def _texts_in(dataset):
    """Every text value at any depth of `dataset` and of its file meta:
    each `str`, each value of a multi-valued element, and each `bytes`
    value read as Latin-1, so a UID held in any of them is found."""
    found = []

    def take(value):
        if isinstance(value, (bytes, bytearray)):
            found.append(bytes(value).decode("latin-1"))
        elif isinstance(value, (list, tuple, MultiValue)):
            for one in value:
                take(one)
        elif value is not None:
            found.append(str(value))

    for element in dataset.iterall():
        if element.VR != "SQ":
            take(element.value)
    for element in getattr(dataset, "file_meta", None) or ():
        take(element.value)
    return found


def _record(dataset, item, folder, options):
    pixels = None
    if "PixelData" in dataset:
        pixels = int(dataset.pixel_array.max())
    return {
        "tags": sorted(f"{int(e.tag):08x}" for e in dataset),
        "texts": _texts_in(dataset),
        "pixel_max": pixels,
        "icons": _icons_in(dataset),
        "patient_identity_removed": dataset.get("PatientIdentityRemoved"),
        "deid_method": (str(dataset.DeidentificationMethod)
                        if "DeidentificationMethod" in dataset else None),
        "patient_id": (str(dataset.PatientID)
                       if "PatientID" in dataset else None),
        "sop_instance_uid": str(dataset.SOPInstanceUID),
        "item_uid": item.sop_instance_uid,
        "item_patient_id": item.patient_id,
        "item_folder_names": list(item.folder_names),
        "folder": folder,
        "options": options,
        "pid": os.getpid(),
        "dataset_type": type(dataset).__name__,
    }


class Recorder(exporters.Exporter):
    """Writes `<folder>/<subject>/<uid>.json`: what it was handed."""

    options = frozenset({"note"})

    def write(self, dataset, item, folder, **options):
        out = os.path.join(folder, item.folder_names[0])
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, f"{item.sop_instance_uid}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(_record(dataset, item, folder, options), handle)
        return path


class Raising(exporters.Exporter):
    """Fails every instance it is handed."""

    def write(self, dataset, item, folder, **options):
        raise RuntimeError("the plugin failed")


class Declining(exporters.Exporter):
    """Writes nothing for any instance, and says so with None."""

    def write(self, dataset, item, folder, **options):
        return None


class OneFile(exporters.Exporter):
    """Returns one path for every instance: each write replaces the last."""

    def write(self, dataset, item, folder, **options):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "all.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"uid": item.sop_instance_uid}, handle)
        return path


class OldShape:
    """A plugin written for 1.0: `export(session, folder, **options)`."""

    def export(self, session, folder, **options):
        return []


class InheritsWrite(exporters.Exporter):
    """Defines nothing: `write` is the base class's."""


class OwnsAnOption(exporters.Exporter):
    """Claims an option name the session owns."""

    options = frozenset({"patient_ids"})

    def write(self, dataset, item, folder, **options):
        return None


from isocenter.exporters.dicom import DicomFormatExporter  # noqa: E402


class SubOfDicom(DicomFormatExporter):
    """A subclass of the built-in that implements `write`: a plugin, not
    the built-in, because a subclass may override anything."""

    def write(self, dataset, item, folder, **options):
        return Recorder().write(dataset, item, folder)


class _EqualToEverything(type):
    def __eq__(cls, other):
        return True

    __hash__ = type.__hash__


class Impostor(exporters.Exporter, metaclass=_EqualToEverything):
    """Compares equal to every class, so a built-in test that falls back
    to `==` -- `type(x) in (A, B)` does -- takes it for one. Only `is`
    sees through it (review of #786)."""

    def write(self, dataset, item, folder, **options):
        return Recorder().write(dataset, item, folder)


class Spoofed(exporters.Exporter):
    """Claims the built-in's module, where no worker can find it."""

    def write(self, dataset, item, folder, **options):
        return None


Spoofed.__module__ = DicomFormatExporter.__module__


class Aggregate(exporters.Exporter):
    """Appends every instance to one shared file and returns None: what it
    was handed is in the folder, under no path it named (review of #1197)."""

    def write(self, dataset, item, folder, **options):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "all.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"uid": item.sop_instance_uid,
                 "has_token": 0x04000500 in dataset}) + "\n")
        return None


class WriteThenRaise(exporters.Exporter):
    """Writes what it was handed, then raises."""

    def write(self, dataset, item, folder, **options):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{item.sop_instance_uid}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"has_token": 0x04000500 in dataset}))
        raise RuntimeError("late failure")


class WriteThenHang(exporters.Exporter):
    """Writes what it was handed, then never returns: a stuck upload
    (#673). The time bound stops its worker; it kept the token all the
    same."""

    def write(self, dataset, item, folder, **options):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "kept.jsonl")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"uid": item.sop_instance_uid,
                                     "has_token": 0x04000500 in dataset})
                         + "\n")
        while True:
            time.sleep(3600)


class OptionNamedDataset(exporters.Exporter):
    """Names an option `write` takes as its own argument."""

    options = frozenset({"dataset"})

    def write(self, dataset, item, folder, **options):
        return None


class OptionNamedItem(exporters.Exporter):
    """Names an option `write` takes as its own argument."""

    options = frozenset({"item"})

    def write(self, dataset, item, folder, **options):
        return None
