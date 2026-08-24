
"""
Standard Privacy Profiles for Gantry.

This module defines built-in privacy profiles that can be referenced in the
Gantry configuration file using the "privacy_profile" key. These profiles
provide a baseline set of PHI actions (e.g. REMOVE, EMPTY) which can be
overridden by the user's specific "phi_tags" configuration.
"""

# Based on DICOM PS3.15 Annex E (Basic Profile) - Reduced for common usage
#
# NOTE: keys must be lowercase 'gggg,eeee'. Ingested attribute keys on the
# object graph are always lowercased (gantry/io_handlers.py's populate_attrs),
# and PhiInspector.__init__ (gantry/privacy.py) now normalizes any phi_tags
# dict to lowercase keys as a defensive backstop -- but do not rely on that
# backstop when adding new entries here; write them lowercase directly so
# a mismatch never has a chance to silently disable a tag. (0008,103E,
# Series Description, shipped uppercase for a while and was never
# remediated on any documented path as a result.)
BASIC_PROFILE = {
    # Patient Identity
    "0010,0010": {"action": "REMOVE", "name": "Patient's Name"},
    "0010,0020": {"action": "REMOVE", "name": "Patient ID"},
    "0010,0030": {"action": "REMOVE", "name": "Patient's Birth Date"},
    "0010,0032": {"action": "REMOVE", "name": "Patient's Birth Time"},
    "0010,0040": {"action": "REMOVE", "name": "Patient's Sex"},
    "0010,1000": {"action": "REMOVE", "name": "Other Patient IDs"},
    "0010,1001": {"action": "REMOVE", "name": "Other Patient Names"},
    "0010,1040": {"action": "REMOVE", "name": "Patient's Address"},
    "0010,2160": {"action": "REMOVE", "name": "Ethnic Group"},
    "0010,4000": {"action": "REMOVE", "name": "Patient Comments"},

    # Study / Series Information
    "0008,0020": {"action": "REMOVE", "name": "Study Date"},
    "0008,0021": {"action": "REMOVE", "name": "Series Date"},
    "0008,0022": {"action": "REMOVE", "name": "Acquisition Date"},
    "0008,0023": {"action": "REMOVE", "name": "Content Date"},
    "0008,0030": {"action": "REMOVE", "name": "Study Time"},
    "0008,0031": {"action": "REMOVE", "name": "Series Time"},
    "0008,0032": {"action": "REMOVE", "name": "Acquisition Time"},
    "0008,0033": {"action": "REMOVE", "name": "Content Time"},
    "0008,0050": {"action": "REMOVE", "name": "Accession Number"},
    "0008,0090": {"action": "REMOVE", "name": "Referring Physician's Name"},
    # Often contains PHI, but structural
    "0008,1030": {"action": "EMPTY", "name": "Study Description"},
    "0008,103e": {"action": "EMPTY", "name": "Series Description"},
    "0020,0010": {"action": "REMOVE", "name": "Study ID"},
    "0008,0080": {"action": "REMOVE", "name": "Institution Name"},
    "0008,0081": {"action": "REMOVE", "name": "Institution Address"},
    "0008,1040": {"action": "REMOVE", "name": "Institutional Department Name"},
    "0008,1050": {"action": "REMOVE", "name": "Performing Physician's Name"},
    "0008,1070": {"action": "REMOVE", "name": "Operators' Name"},
}

PRIVACY_PROFILES = {
    "basic": BASIC_PROFILE
}
