from pydicom.dataset import Dataset
from typing import List


class IODValidator:
    """
    Minimal IOD (Information Object Definition) Validator for DICOM compliance.

    Checks for the presence of Type 1 and Type 2 attributes based on SOP Class rules.
    Currently implements a subset of "Common" and "CTImage" modules.
    """

    _MODULE_DEFINITIONS = {
        'Common': {
            '0008,0016': '1', '0008,0018': '1',
            # Study Date is Type 2 in General Study (PS3.3 C.7.2.1), as
            # Study Time below is. It read '1' until #537, which nothing
            # noticed while `anonymize()` always wrote a shifted date: a
            # CT whose source had no or an empty Study Date failed export
            # ('[Type 1 Error] Missing 0008,0020', 0 files), and once the
            # rule governs the study's date the basic profile's own EMPTY
            # would have failed every CT file the same way.
            '0008,0020': '2',
            # Study Time is Type 2 in General Study (PS3.3 C.7.2.1):
            # present and empty is conformant. It read '1' until #495,
            # which nothing noticed while no policy touched the tag; the
            # basic profile empties it, and under '1' the Type-1 arm
            # below rejected the empty value, so the documented
            # create_config -> load_config -> anonymize -> export path
            # raised on every CT file and wrote nothing.
            '0008,0030': '2', '0008,0060': '1', '0020,000e': '1',
        },
        'CTImage': {
            '0018,0050': '2', '0018,0060': '2',  # SliceThickness, KVP
            '0020,0032': '1', '0020,0037': '1',  # Pos, Orient
            '0028,0030': '1',  # Pixel Spacing
        }
    }

    _SOP_RULES = {
        '1.2.840.10008.5.1.4.1.1.2': ['Common', 'CTImage'],  # CT Image Storage
    }

    @staticmethod
    def validate(ds: Dataset) -> List[str]:
        """
        Validates the dataset against internal IOD rules based on SOP Class.

        Args:
            ds (pydicom.Dataset): The dataset to validate.

        Returns:
            List[str]: A list of error messages describing missing Type 1/2 attributes.
        """
        errors = []
        sop = ds.file_meta.MediaStorageSOPClassUID if hasattr(
            ds, 'file_meta') else ds.get("SOPClassUID")

        if sop not in IODValidator._SOP_RULES:
            return []

        for module in IODValidator._SOP_RULES[sop]:
            for tag_str, req in IODValidator._MODULE_DEFINITIONS.get(module, {}).items():
                group, elem = map(lambda x: int(x, 16), tag_str.split(','))
                tag = (group, elem)

                if req == '1' and (tag not in ds or ds[tag].value in [None, ""]):
                    errors.append(f"[Type 1 Error] Missing {tag_str} in {module}")
                elif req == '2' and tag not in ds:
                    errors.append(f"[Type 2 Error] Missing {tag_str} in {module}")
        return errors
