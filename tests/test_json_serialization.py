import pytest
import json
import base64
from pydicom.multival import MultiValue
from pydicom.valuerep import DSfloat
from isocenter.persistence import IsocenterJSONEncoder, isocenter_json_object_hook

class TestJsonSerialization:

    def test_multivalue_serialization(self):
        """
        Verifies that pydicom MultiValue objects are serialized as lists.
        """
        # Create a MultiValue similar to ImagePositionPatient
        mv = MultiValue(DSfloat, ['0.5', '1.5', '2.5'])
        data = {"ImagePositionPatient": mv}

        json_str = json.dumps(data, cls=IsocenterJSONEncoder)

        # Verify result is a valid JSON string with a list
        decoded = json.loads(json_str)
        assert decoded["ImagePositionPatient"] == [0.5, 1.5, 2.5]
        assert isinstance(decoded["ImagePositionPatient"], list)

    def test_bytes_serialization(self):
        """
        Verifies that bytes are serialized to base64 dicts and restored.
        """
        data = {"MyBytes": b"HiddenData"}

        # 1. Encode
        json_str = json.dumps(data, cls=IsocenterJSONEncoder)
        decoded_raw = json.loads(json_str)

        # Verify encoding format
        assert decoded_raw["MyBytes"]["__type__"] == "bytes"
        assert decoded_raw["MyBytes"]["data"] == base64.b64encode(b"HiddenData").decode('ascii')

        # 2. Decode via Hook
        restored = json.loads(json_str, object_hook=isocenter_json_object_hook)
        assert restored["MyBytes"] == b"HiddenData"

    def test_mixed_structure(self):
        """
        Verifies serialization of a complex structure with both MultiValue and Bytes.
        """
        mv = MultiValue(DSfloat, ['1.0', '2.0'])
        data = {
            "Complex": [
                {"Pos": mv},
                {"Raw": b"123"}
            ]
        }

        json_str = json.dumps(data, cls=IsocenterJSONEncoder)
        restored = json.loads(json_str, object_hook=isocenter_json_object_hook)

        assert restored["Complex"][0]["Pos"] == [1.0, 2.0]
        assert restored["Complex"][1]["Raw"] == b"123"
