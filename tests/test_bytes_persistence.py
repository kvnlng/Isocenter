import pytest
import os
import json
from isocenter.persistence import SqliteStore, IsocenterJSONEncoder, isocenter_json_object_hook
from isocenter.entities import Patient, Study, Series, Instance

def test_json_encoder_decoder_bytes():
    data = {"key": b"hello world", "nested": {"other": b"\x00\x01\x02"}}
    json_str = json.dumps(data, cls=IsocenterJSONEncoder)

    # Check that it's serializable
    assert isinstance(json_str, str)
    assert "__type__" in json_str
    assert "bytes" in json_str

    # Restore
    loaded = json.loads(json_str, object_hook=isocenter_json_object_hook)
    assert loaded["key"] == b"hello world"
    assert loaded["nested"]["other"] == b"\x00\x01\x02"

def test_persistence_roundtrip_with_bytes(tmp_path):
    db_path = str(tmp_path / "test_bytes.db")
    store = SqliteStore(db_path)

    # Setup Data
    p = Patient("P_BYTES", "Bytes Patient")
    st = Study("ST_BYTES", None)
    p.studies.append(st)
    se = Series("SE_BYTES", "OT", 1)
    st.series.append(se)
    inst = Instance("SOP_BYTES", "1.2.840.10008.5.1.4.1.1.7", 1)

    # Inject problematic bytes attributes
    # Example: Private Creator data (LO) but read as bytes, or OB/OW data
    inst.attributes["0009,0010"] = b"GEMS_PETD_01"
    inst.attributes["0029,1010"] = b"\x00\x01\x02\x03" * 10

    se.instances.append(inst)

    # Save
    store.save_all([p])

    # Load back
    new_store = SqliteStore(db_path)
    patients = new_store.load_all()

    assert len(patients) == 1
    loaded_inst = patients[0].studies[0].series[0].instances[0]

    # Verify attributes
    val1 = loaded_inst.attributes.get("0009,0010")
    val2 = loaded_inst.attributes.get("0029,1010")

    assert isinstance(val1, bytes)
    assert val1 == b"GEMS_PETD_01"

    assert isinstance(val2, bytes)
    assert val2 == b"\x00\x01\x02\x03" * 10
