import itertools

import pytest
import numpy as np
import warnings

# Suppress all pydicom warnings during tests
warnings.filterwarnings("ignore", module="pydicom.*")

import os
import json
from datetime import date
from isocenter.entities import Patient, Study, Series, Instance, Equipment
from isocenter.builders import DicomBuilder

@pytest.fixture(autouse=True)
def redirect_logging(tmp_path):
    """Redirects isocenter.log to a temp file for all tests."""
    log_file = tmp_path / "isocenter.log"
    os.environ["ISOCENTER_LOG_FILE"] = str(log_file)
    yield
    if "ISOCENTER_LOG_FILE" in os.environ:
        del os.environ["ISOCENTER_LOG_FILE"]

@pytest.fixture
def dummy_pixel_array_2d():
    return np.zeros((512, 512), dtype=np.uint16)


@pytest.fixture
def dummy_patient(dummy_pixel_array_2d):
    """Creates a full object graph using the Builder."""
    return (
        DicomBuilder.start_patient("P123", "Test^Patient")
        .add_study("1.2.840.111.1", date(2023, 1, 1))
        .add_series("1.2.840.111.1.1", "CT", 1)
        .set_equipment("TestManu", "TestModel", "SN-999")
        .add_instance("1.2.840.111.1.1.1", "1.2.840.10008.5.1.4.1.1.2", 1)
        .set_pixel_data(dummy_pixel_array_2d)

        # Type 1 (Pos/Orient/Spacing)
        .set_attribute("0020,0032", ["0", "0", "0"])
        .set_attribute("0020,0037", ["1", "0", "0", "0", "1", "0"])
        .set_attribute("0028,0030", ["0.5", "0.5"])

        # --- FIX: ADD TYPE 2 MANDATORY TAGS ---
        .set_attribute("0018,0050", "2.5")  # SliceThickness
        .set_attribute("0018,0060", "120")  # KVP

        .end_instance()
        .end_series()
        .end_study()
        .build()
    )

@pytest.fixture
def config_file(tmp_path):
    """Creates a temporary YAML config file."""
    data = {
        "version": "1.0",
        "machines": [
            {
                "serial_number": "SN-999",
                "model_name": "TestModel",
                "redaction_zones": [{"roi": [10, 50, 10, 50]}]
            }
        ]
    }
    p = tmp_path / "rules.yaml"
    import yaml
    with open(p, "w") as f:
        yaml.dump(data, f)
    return str(p)


#: Secondary Capture Image Storage. `reloaded_redaction_session` uses it
#: deliberately -- see that fixture's docstring.
SC_STORAGE = "1.2.840.10008.5.1.4.1.1.7"


@pytest.fixture
def reloaded_redaction_session(tmp_path):
    """A saved-and-reopened session whose pixels arrive read-only.

    Build -> save() -> close() -> reopen, so `get_pixel_data()` comes back
    through `SidecarPixelLoader`, which builds its array with
    `np.frombuffer` over an immutable `bytes` buffer and is therefore
    **not writeable**. That is the ordinary shape for any instance loaded
    from a saved store -- the documented ingest -> save -> reopen ->
    redact workflow -- and until #229 no redaction test in the suite used
    it. Every other fixture builds the graph in memory or reads a source
    file, and both give a *writeable* array, where the redaction path
    mutates in place and is correct. That is the only reason a rule with
    N zones applying only its Nth zone survived the whole suite.

    The `flags.writeable is False` assertion is the fixture's guard on
    itself: give this instance a `file_path` and pydicom hands back a
    writeable array, at which point every test built on it goes vacuous
    rather than red.

    **The instance is exportable, and that is not decoration.**
    `session.export()` does not raise when an instance fails module
    validation -- it logs and writes nothing -- so a fixture missing a
    Type 1 element leaves an empty output tree, and any test that walks
    that tree iterates an empty list and passes. Measured on `4507d48`: a
    CT-class instance without these fails with `['[Type 1 Error] Missing
    0008,0030 in Common', '[Type 2 Error] Missing 0018,0050 in CTImage',
    ...]`. Under SC Image Storage the CTImage module does not apply, which
    is why only the Common-module elements have to be supplied here.

    Yields:
        A callable `make(zones, ...)` returning `(session, instance)`. The
        returned session already carries a rule matching the instance's
        serial. Every session handed out is closed at teardown -- an
        unclosed one leaks worker subprocesses.
    """
    from isocenter.session import DicomSession

    opened = []
    made = itertools.count()

    def make(zones, *, serial="SN_RELOAD", uid=None,
             shape=(32, 32), fill=200, sop_class=SC_STORAGE, name=None):
        # **`name` and `uid` both default per call, and they have to be
        # the same counter.** `name` names the database file and `uid`
        # identifies the instance inside it; two `make()` calls in one
        # test used to share `tmp_path/reload.db` *and* `1.2.3.reload`,
        # so the second reopened the first's store and the `next(...)`
        # below could hand back the first call's instance -- the
        # vacuous-fixture failure this fixture exists to prevent,
        # reintroduced by its own defaults. Deriving only `name` closes
        # it for the default call and leaves it open for the documented
        # one: `make(z, name="x")` twice lands two instances carrying
        # one UID in one database, which is the same collision through
        # the parameter instead of the default.
        n = next(made)
        name = f"reload{n}" if name is None else name
        uid = f"1.2.3.reload{n}" if uid is None else uid
        db_file = tmp_path / f"{name}.db"

        session = DicomSession(str(db_file))
        opened.append(session)
        patient = Patient("P_RELOAD", "Test^Patient")
        study = Study(f"ST_{name}", "20230101")
        series = Series(f"SE_{name}", "OT", 1)
        series.equipment = Equipment("Acme", "Scanner", serial)
        inst = Instance(uid, sop_class, 1)
        inst.file_path = None
        inst.set_pixel_data(np.full(shape, fill, dtype=np.uint8))
        inst.set_attr("0008,0016", sop_class)
        inst.set_attr("0008,0030", "120000")
        inst.set_attr("0008,0060", "OT")
        inst.set_attr("0028,0004", "MONOCHROME2")
        series.instances.append(inst)
        study.series.append(series)
        patient.studies.append(study)
        session.store.patients.append(patient)

        session.save()
        session.close()

        session = DicomSession(str(db_file))
        opened.append(session)
        inst = next(i
                    for p in session.store.patients
                    for st in p.studies
                    for se in st.series
                    for i in se.instances
                    if i.sop_instance_uid == uid)

        arr = inst.get_pixel_data()
        assert arr is not None, "the reopened instance lost its pixels"
        assert arr.flags.writeable is False, (
            "this fixture exists to supply a read-only array; a writeable "
            "one makes every test built on it vacuous rather than red")
        # Drop the resident array so the redaction re-reads through the
        # loader rather than the copy this assertion just materialised.
        inst.unload_pixel_data()

        session.configuration.rules = [
            {"serial_number": serial, "redaction_zones": zones}]
        return session, inst

    yield make

    for session in opened:
        try:
            session.close()
        except Exception:  # pragma: no cover - teardown must not mask failures
            pass
