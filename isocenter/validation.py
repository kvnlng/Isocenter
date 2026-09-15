from pydicom.dataset import Dataset
from pydicom.tag import BaseTag, Tag
from typing import List


class IODValidator:
    """
    Minimal IOD (Information Object Definition) Validator for DICOM compliance.

    Checks for the presence of Type 1 and Type 2 attributes based on SOP Class rules.
    Currently implements a subset of "Common" and "CTImage" modules.

    **Validate, and fill.** The export worker asks `absent_type2` before it
    asks `validate`, and writes each tag it names zero-length (#600): Type 2
    means present and empty when unknown, so an absent one is a gap the
    writer can close faithfully rather than a reason to refuse the file.
    Both read `_modules_for`, so the fill covers exactly what the Type 2 arm
    of `validate` would report and nothing this table does not know.
    `validate`'s Type 2 arm is kept: it is the guard that goes red if the
    fill ever stops running. Type 1 is never filled.
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
    def _modules_for(ds: Dataset) -> List[str]:
        """The module names this table holds for `ds`'s SOP class, or `[]`.

        The SOP class is the file meta's when the dataset has one -- the
        export worker's `FileDataset` always does -- and the dataset's own
        `SOPClassUID` otherwise. One spelling for `validate` and
        `absent_type2`, so the fill and the refusal cannot read two SOP
        classes.
        """
        sop = ds.file_meta.MediaStorageSOPClassUID if hasattr(
            ds, 'file_meta') else ds.get("SOPClassUID")
        return IODValidator._SOP_RULES.get(sop, [])

    @staticmethod
    def absent_type2(ds: Dataset) -> List[BaseTag]:
        """Every Type 2 tag of `ds`'s modules that `ds` does not hold (#600).

        Exactly the set `validate` reports as `[Type 2 Error]`: absent, not
        empty, since an empty Type 2 element is conformant. Type 1 tags are
        never named, absent or empty.
        """
        absent = []
        for module in IODValidator._modules_for(ds):
            for tag_str, req in IODValidator._MODULE_DEFINITIONS.get(
                    module, {}).items():
                tag = Tag(*(int(part, 16) for part in tag_str.split(',')))
                if req == '2' and tag not in ds:
                    absent.append(tag)
        return absent

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
        for module in IODValidator._modules_for(ds):
            for tag_str, req in IODValidator._MODULE_DEFINITIONS.get(module, {}).items():
                group, elem = map(lambda x: int(x, 16), tag_str.split(','))
                tag = (group, elem)

                if req == '1' and (tag not in ds or ds[tag].value in [None, ""]):
                    errors.append(f"[Type 1 Error] Missing {tag_str} in {module}")
                elif req == '2' and tag not in ds:
                    errors.append(f"[Type 2 Error] Missing {tag_str} in {module}")
        return errors
