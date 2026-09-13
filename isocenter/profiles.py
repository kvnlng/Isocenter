"""
Standard Privacy Profiles for Isocenter.

This module defines built-in privacy profiles that can be referenced in the
Isocenter configuration file using the "privacy_profile" key. These profiles
provide a baseline set of PHI actions (e.g. REMOVE, EMPTY) which can be
overridden by the user's specific "phi_tags" configuration.

`BASIC_PROFILE` is the Basic Prof. column of DICOM PS3.15 Annex E, Table
E.1-1, edition 2026c: one rule per row, apart from the departures
`tests/support/annex_e.py` names with their reasons. It is not the whole
of Annex E -- UIDs are not replaced (#544), and the attributes that record
de-identification are not written (#554). See docs/configuration.md.
"""

# DICOM PS3.15 2026c, Table E.1-1, Basic Prof. column (#547). Each entry's
# trailing comment is the table's code for the row.
#
# DO NOT EDIT ENTRIES BY HAND. This is a pasted literal, not a loop over
# the table, because the mutation probe registers this module as data
# with zero sites. `tests/test_basic_profile_annex_e.py` holds it equal to
# `derive()` in `tests/support/annex_e.py`, which reads the vendored table
# (`tests/fixtures/ps3.15-2026c-table-e1-1.json`). To change a rule,
# change the mapping or a named departure there, and regenerate this
# literal from `derive(load_table())`. Until 0.9.8 this was a hand-picked
# 35 rows, and nothing said which of the other 621 were left out.
#
# How a code becomes an action: `X` and `X/D` remove; every code with a
# `Z` arm (`Z`, `Z/D`, `X/Z`, `X/Z/D`) empties, because zero length is
# valid wherever the table's X or Z is and removal drops Type 2
# attributes; `D` empties, because there is no dummy-value action yet
# (#557). `U` rows get no rule (#544).
#
# A rule on a sequence tag removes the sequence or leaves it with zero
# items; identifiers inside any sequence are scanned wherever they sit.
#
# NOTE: keys must be lowercase 'gggg,eeee'. Ingested attribute keys on the
# object graph are always lowercased (isocenter/io_handlers.py's populate_attrs),
# and PhiInspector.__init__ (isocenter/privacy.py) now normalizes any phi_tags
# dict to lowercase keys as a defensive backstop -- but do not rely on that
# backstop when adding new entries here; write them lowercase directly so
# a mismatch never has a chance to silently disable a tag. (0008,103E,
# Series Description, shipped uppercase for a while and was never
# remediated on any documented path as a result.)
BASIC_PROFILE = {
    "0000,1000": {"action": "REMOVE", "name": "Affected SOP Instance UID"},  # X
    "0008,0012": {"action": "REMOVE", "name": "Instance Creation Date"},  # X/D
    "0008,0013": {"action": "EMPTY", "name": "Instance Creation Time"},  # X/Z/D
    "0008,0015": {"action": "REMOVE", "name": "Instance Coercion DateTime"},  # X
    # Z in the table. Entity-owned: anonymize() shifts the study's own
    # date whatever this rule says, and #537 decides what the rule
    # should govern. The floor JITTERs it (RESEARCH_DEFAULTS).
    "0008,0020": {"action": "REMOVE", "name": "Study Date"},  # Z
    "0008,0021": {"action": "REMOVE", "name": "Series Date"},  # X/D
    "0008,0022": {"action": "EMPTY", "name": "Acquisition Date"},  # X/Z
    "0008,0023": {"action": "EMPTY", "name": "Content Date"},  # Z/D
    "0008,0024": {"action": "REMOVE", "name": "Overlay Date"},  # X
    "0008,0025": {"action": "REMOVE", "name": "Curve Date"},  # X
    # DT-valued twin of Acquisition Date: until #38 raw acquisition
    # timing survived a full anonymize() pass while the plain date was
    # stripped.
    "0008,002a": {"action": "EMPTY", "name": "Acquisition DateTime"},  # X/Z/D
    # Z, and Type 2 in General Study (PS3.3 C.7.2.1), so the element
    # stays present and empty. REMOVE here plus a validator that called
    # it Type 1 meant the documented Quick Start exported nothing on any
    # CT file (#495).
    "0008,0030": {"action": "EMPTY", "name": "Study Time"},  # Z
    "0008,0031": {"action": "REMOVE", "name": "Series Time"},  # X/D
    "0008,0032": {"action": "EMPTY", "name": "Acquisition Time"},  # X/Z
    "0008,0033": {"action": "EMPTY", "name": "Content Time"},  # Z/D
    "0008,0034": {"action": "REMOVE", "name": "Overlay Time"},  # X
    "0008,0035": {"action": "REMOVE", "name": "Curve Time"},  # X
    "0008,0050": {"action": "EMPTY", "name": "Accession Number"},  # Z
    "0008,0054": {"action": "REMOVE", "name": "Retrieve AE Title"},  # X
    "0008,0055": {"action": "REMOVE", "name": "Station AE Title"},  # X
    "0008,0080": {"action": "EMPTY", "name": "Institution Name"},  # X/Z/D
    "0008,0081": {"action": "REMOVE", "name": "Institution Address"},  # X
    "0008,0082": {"action": "EMPTY", "name": "Institution Code Sequence"},  # X/Z/D
    "0008,0090": {"action": "EMPTY", "name": "Referring Physician's Name"},  # Z
    "0008,0092": {"action": "REMOVE", "name": "Referring Physician's Address"},  # X
    "0008,0094": {"action": "REMOVE", "name": "Referring Physician's Telephone Numbers"},  # X
    "0008,0096": {"action": "REMOVE", "name": "Referring Physician Identification Sequence"},  # X
    "0008,009c": {"action": "EMPTY", "name": "Consulting Physician's Name"},  # Z
    "0008,009d": {"action": "REMOVE", "name": "Consulting Physician Identification Sequence"},  # X
    "0008,0106": {"action": "EMPTY", "name": "Context Group Version"},  # D
    "0008,0107": {"action": "EMPTY", "name": "Context Group Local Version"},  # D
    "0008,0201": {"action": "REMOVE", "name": "Timezone Offset From UTC"},  # X
    "0008,1000": {"action": "REMOVE", "name": "Network ID"},  # X
    # Absent until #495, so CT_small's `CT01_OC0` survived even the
    # documented path.
    "0008,1010": {"action": "EMPTY", "name": "Station Name"},  # X/Z/D
    # X in the table, EMPTY here: the export directory names read it
    # (`io_handlers.export_folder_names`), and zero length is valid
    # wherever X is. The same for Series Description below.
    "0008,1030": {"action": "EMPTY", "name": "Study Description"},  # X
    "0008,103e": {"action": "EMPTY", "name": "Series Description"},  # X
    "0008,1040": {"action": "REMOVE", "name": "Institutional Department Name"},  # X
    "0008,1041": {"action": "REMOVE", "name": "Institutional Department Type Code Sequence"},  # X
    "0008,1048": {"action": "REMOVE", "name": "Physician(s) of Record"},  # X
    "0008,1049": {"action": "REMOVE", "name": "Physician(s) of Record Identification Sequence"},  # X
    "0008,1050": {"action": "REMOVE", "name": "Performing Physician's Name"},  # X
    "0008,1052": {"action": "REMOVE", "name": "Performing Physician Identification Sequence"},  # X
    "0008,1060": {"action": "REMOVE", "name": "Name of Physician(s) Reading Study"},  # X
    "0008,1062": {"action": "REMOVE", "name": "Physician(s) Reading Study Identification Sequence"},  # X
    "0008,1070": {"action": "EMPTY", "name": "Operators' Name"},  # X/Z/D
    "0008,1072": {"action": "REMOVE", "name": "Operator Identification Sequence"},  # X/D
    "0008,1080": {"action": "REMOVE", "name": "Admitting Diagnoses Description"},  # X
    "0008,1084": {"action": "REMOVE", "name": "Admitting Diagnoses Code Sequence"},  # X
    "0008,1088": {"action": "REMOVE", "name": "Pyramid Description"},  # X
    "0008,1110": {"action": "EMPTY", "name": "Referenced Study Sequence"},  # X/Z
    "0008,1111": {"action": "EMPTY", "name": "Referenced Performed Procedure Step Sequence"},  # X/Z/D
    "0008,1120": {"action": "REMOVE", "name": "Referenced Patient Sequence"},  # X
    "0008,1301": {"action": "REMOVE", "name": "Principal Diagnosis Code Sequence"},  # X
    "0008,1302": {"action": "REMOVE", "name": "Primary Diagnosis Code Sequence"},  # X
    "0008,1303": {"action": "REMOVE", "name": "Secondary Diagnoses Code Sequence"},  # X
    "0008,1304": {"action": "REMOVE", "name": "Histological Diagnoses Code Sequence"},  # X
    # Isocenter's own redaction note here is exempt, by exact value, in
    # `PhiInspector._scan_instance`: safe export would otherwise skip
    # every redacted instance.
    "0008,2111": {"action": "REMOVE", "name": "Derivation Description"},  # X
    "0008,4000": {"action": "REMOVE", "name": "Identifying Comments"},  # X
    # Z in the table. Entity-owned, as Patient ID below (Z/D): #537.
    "0010,0010": {"action": "REMOVE", "name": "Patient's Name"},  # Z
    "0010,0011": {"action": "REMOVE", "name": "Person Names to Use Sequence"},  # X
    "0010,0012": {"action": "REMOVE", "name": "Name to Use"},  # X
    "0010,0013": {"action": "REMOVE", "name": "Name to Use Comment"},  # X
    "0010,0014": {"action": "REMOVE", "name": "Third Person Pronouns Sequence"},  # X
    "0010,0015": {"action": "REMOVE", "name": "Pronoun Code Sequence"},  # X
    "0010,0016": {"action": "REMOVE", "name": "Pronoun Comment"},  # X
    "0010,0020": {"action": "REMOVE", "name": "Patient ID"},  # Z/D
    "0010,0021": {"action": "REMOVE", "name": "Issuer of Patient ID"},  # X
    "0010,0030": {"action": "EMPTY", "name": "Patient's Birth Date"},  # Z
    "0010,0032": {"action": "REMOVE", "name": "Patient's Birth Time"},  # X
    "0010,0040": {"action": "EMPTY", "name": "Patient's Sex"},  # Z
    "0010,0041": {"action": "REMOVE", "name": "Gender Identity Sequence"},  # X
    "0010,0042": {"action": "REMOVE", "name": "Sex Parameters for Clinical Use Category Comment"},  # X
    "0010,0043": {"action": "REMOVE", "name": "Sex Parameters for Clinical Use Category Sequence"},  # X
    "0010,0044": {"action": "REMOVE", "name": "Gender Identity Code Sequence"},  # X
    "0010,0045": {"action": "REMOVE", "name": "Gender Identity Comment"},  # X
    "0010,0046": {"action": "REMOVE", "name": "Sex Parameters for Clinical Use Category Code Sequence"},  # X
    "0010,0047": {"action": "REMOVE", "name": "Sex Parameters for Clinical Use Category Reference"},  # X
    "0010,0050": {"action": "REMOVE", "name": "Patient's Insurance Plan Code Sequence"},  # X
    "0010,0101": {"action": "REMOVE", "name": "Patient's Primary Language Code Sequence"},  # X
    "0010,0102": {"action": "REMOVE", "name": "Patient's Primary Language Modifier Code Sequence"},  # X
    "0010,1000": {"action": "REMOVE", "name": "Other Patient IDs"},  # X
    "0010,1001": {"action": "REMOVE", "name": "Other Patient Names"},  # X
    "0010,1002": {"action": "REMOVE", "name": "Other Patient IDs Sequence"},  # X
    "0010,1005": {"action": "REMOVE", "name": "Patient's Birth Name"},  # X
    "0010,1010": {"action": "REMOVE", "name": "Patient's Age"},  # X
    "0010,1020": {"action": "REMOVE", "name": "Patient's Size"},  # X
    "0010,1030": {"action": "REMOVE", "name": "Patient's Weight"},  # X
    "0010,1040": {"action": "REMOVE", "name": "Patient's Address"},  # X
    "0010,1050": {"action": "REMOVE", "name": "Insurance Plan Identification"},  # X
    "0010,1060": {"action": "REMOVE", "name": "Patient's Mother's Birth Name"},  # X
    "0010,1080": {"action": "REMOVE", "name": "Military Rank"},  # X
    "0010,1081": {"action": "REMOVE", "name": "Branch of Service"},  # X
    "0010,1090": {"action": "REMOVE", "name": "Medical Record Locator"},  # X
    "0010,1100": {"action": "REMOVE", "name": "Referenced Patient Photo Sequence"},  # X
    "0010,2000": {"action": "REMOVE", "name": "Medical Alerts"},  # X
    "0010,2110": {"action": "REMOVE", "name": "Allergies"},  # X
    "0010,2150": {"action": "REMOVE", "name": "Country of Residence"},  # X
    "0010,2152": {"action": "REMOVE", "name": "Region of Residence"},  # X
    "0010,2154": {"action": "REMOVE", "name": "Patient's Telephone Numbers"},  # X
    "0010,2155": {"action": "REMOVE", "name": "Patient's Telecom Information"},  # X
    "0010,2160": {"action": "REMOVE", "name": "Ethnic Group"},  # X
    "0010,2161": {"action": "REMOVE", "name": "Ethnic Group Code Sequence"},  # X
    "0010,2162": {"action": "REMOVE", "name": "Ethnic Groups"},  # X
    "0010,2180": {"action": "REMOVE", "name": "Occupation"},  # X
    "0010,21a0": {"action": "REMOVE", "name": "Smoking Status"},  # X
    "0010,21b0": {"action": "REMOVE", "name": "Additional Patient History"},  # X
    "0010,21c0": {"action": "REMOVE", "name": "Pregnancy Status"},  # X
    "0010,21d0": {"action": "REMOVE", "name": "Last Menstrual Date"},  # X
    "0010,21f0": {"action": "REMOVE", "name": "Patient's Religious Preference"},  # X
    "0010,2203": {"action": "EMPTY", "name": "Patient's Sex Neutered"},  # X/Z
    "0010,2297": {"action": "REMOVE", "name": "Responsible Person"},  # X
    "0010,2299": {"action": "REMOVE", "name": "Responsible Organization"},  # X
    "0010,4000": {"action": "REMOVE", "name": "Patient Comments"},  # X
    "0012,0010": {"action": "EMPTY", "name": "Clinical Trial Sponsor Name"},  # D
    "0012,0020": {"action": "EMPTY", "name": "Clinical Trial Protocol ID"},  # D
    "0012,0021": {"action": "EMPTY", "name": "Clinical Trial Protocol Name"},  # Z
    "0012,0022": {"action": "REMOVE", "name": "Issuer of Clinical Trial Protocol ID"},  # X
    "0012,0023": {"action": "REMOVE", "name": "Other Clinical Trial Protocol IDs Sequence"},  # X
    "0012,0030": {"action": "EMPTY", "name": "Clinical Trial Site ID"},  # Z
    "0012,0031": {"action": "EMPTY", "name": "Clinical Trial Site Name"},  # Z
    "0012,0032": {"action": "REMOVE", "name": "Issuer of Clinical Trial Site ID"},  # X
    "0012,0040": {"action": "EMPTY", "name": "Clinical Trial Subject ID"},  # D
    "0012,0041": {"action": "REMOVE", "name": "Issuer of Clinical Trial Subject ID"},  # X
    "0012,0042": {"action": "EMPTY", "name": "Clinical Trial Subject Reading ID"},  # D
    "0012,0043": {"action": "REMOVE", "name": "Issuer of Clinical Trial Subject Reading ID"},  # X
    "0012,0050": {"action": "EMPTY", "name": "Clinical Trial Time Point ID"},  # Z
    "0012,0051": {"action": "REMOVE", "name": "Clinical Trial Time Point Description"},  # X
    "0012,0055": {"action": "REMOVE", "name": "Issuer of Clinical Trial Time Point ID"},  # X
    "0012,0060": {"action": "EMPTY", "name": "Clinical Trial Coordinating Center Name"},  # Z
    "0012,0071": {"action": "REMOVE", "name": "Clinical Trial Series ID"},  # X
    "0012,0072": {"action": "REMOVE", "name": "Clinical Trial Series Description"},  # X
    "0012,0073": {"action": "REMOVE", "name": "Issuer of Clinical Trial Series ID"},  # X
    "0012,0081": {"action": "EMPTY", "name": "Clinical Trial Protocol Ethics Committee Name"},  # D
    "0012,0082": {"action": "REMOVE", "name": "Clinical Trial Protocol Ethics Committee Approval Number"},  # X
    "0012,0086": {"action": "REMOVE", "name": "Ethics Committee Approval Effectiveness Start Date"},  # X
    "0012,0087": {"action": "REMOVE", "name": "Ethics Committee Approval Effectiveness End Date"},  # X
    "0014,407c": {"action": "REMOVE", "name": "Calibration Time"},  # X
    "0014,407e": {"action": "REMOVE", "name": "Calibration Date"},  # X
    "0016,002b": {"action": "REMOVE", "name": "Maker Note"},  # X
    "0016,004b": {"action": "REMOVE", "name": "Device Setting Description"},  # X
    "0016,004d": {"action": "REMOVE", "name": "Camera Owner Name"},  # X
    "0016,004e": {"action": "REMOVE", "name": "Lens Specification"},  # X
    "0016,004f": {"action": "REMOVE", "name": "Lens Make"},  # X
    "0016,0050": {"action": "REMOVE", "name": "Lens Model"},  # X
    "0016,0051": {"action": "REMOVE", "name": "Lens Serial Number"},  # X
    "0016,0070": {"action": "REMOVE", "name": "GPS Version ID"},  # X
    "0016,0071": {"action": "REMOVE", "name": "GPS Latitude Ref"},  # X
    "0016,0072": {"action": "REMOVE", "name": "GPS Latitude"},  # X
    "0016,0073": {"action": "REMOVE", "name": "GPS Longitude Ref"},  # X
    "0016,0074": {"action": "REMOVE", "name": "GPS Longitude"},  # X
    "0016,0075": {"action": "REMOVE", "name": "GPS Altitude Ref"},  # X
    "0016,0076": {"action": "REMOVE", "name": "GPS Altitude"},  # X
    "0016,0077": {"action": "REMOVE", "name": "GPS Time Stamp"},  # X
    "0016,0078": {"action": "REMOVE", "name": "GPS Satellites"},  # X
    "0016,0079": {"action": "REMOVE", "name": "GPS Status"},  # X
    "0016,007a": {"action": "REMOVE", "name": "GPS Measure Mode"},  # X
    "0016,007b": {"action": "REMOVE", "name": "GPS DOP"},  # X
    "0016,007c": {"action": "REMOVE", "name": "GPS Speed Ref"},  # X
    "0016,007d": {"action": "REMOVE", "name": "GPS Speed"},  # X
    "0016,007e": {"action": "REMOVE", "name": "GPS Track Ref"},  # X
    "0016,007f": {"action": "REMOVE", "name": "GPS Track"},  # X
    "0016,0080": {"action": "REMOVE", "name": "GPS Img Direction Ref"},  # X
    "0016,0081": {"action": "REMOVE", "name": "GPS Img Direction"},  # X
    "0016,0082": {"action": "REMOVE", "name": "GPS Map Datum"},  # X
    "0016,0083": {"action": "REMOVE", "name": "GPS Dest Latitude Ref"},  # X
    "0016,0084": {"action": "REMOVE", "name": "GPS Dest Latitude"},  # X
    "0016,0085": {"action": "REMOVE", "name": "GPS Dest Longitude Ref"},  # X
    "0016,0086": {"action": "REMOVE", "name": "GPS Dest Longitude"},  # X
    "0016,0087": {"action": "REMOVE", "name": "GPS Dest Bearing Ref"},  # X
    "0016,0088": {"action": "REMOVE", "name": "GPS Dest Bearing"},  # X
    "0016,0089": {"action": "REMOVE", "name": "GPS Dest Distance Ref"},  # X
    "0016,008a": {"action": "REMOVE", "name": "GPS Dest Distance"},  # X
    "0016,008b": {"action": "REMOVE", "name": "GPS Processing Method"},  # X
    "0016,008c": {"action": "REMOVE", "name": "GPS Area Information"},  # X
    "0016,008d": {"action": "REMOVE", "name": "GPS Date Stamp"},  # X
    "0016,008e": {"action": "REMOVE", "name": "GPS Differential"},  # X
    "0018,0010": {"action": "EMPTY", "name": "Contrast/Bolus Agent"},  # Z/D
    "0018,0027": {"action": "REMOVE", "name": "Intervention Drug Stop Time"},  # X
    "0018,0035": {"action": "REMOVE", "name": "Intervention Drug Start Time"},  # X
    "0018,1000": {"action": "EMPTY", "name": "Device Serial Number"},  # X/Z/D
    "0018,1004": {"action": "REMOVE", "name": "Plate ID"},  # X
    "0018,1005": {"action": "REMOVE", "name": "Generator ID"},  # X
    "0018,1007": {"action": "REMOVE", "name": "Cassette ID"},  # X
    "0018,1008": {"action": "REMOVE", "name": "Gantry ID"},  # X
    "0018,1009": {"action": "REMOVE", "name": "Unique Device Identifier"},  # X
    "0018,100a": {"action": "REMOVE", "name": "UDI Sequence"},  # X
    "0018,1010": {"action": "REMOVE", "name": "Secondary Capture Device ID"},  # X
    "0018,1011": {"action": "REMOVE", "name": "Hardcopy Creation Device ID"},  # X
    "0018,1012": {"action": "REMOVE", "name": "Date of Secondary Capture"},  # X
    "0018,1014": {"action": "REMOVE", "name": "Time of Secondary Capture"},  # X
    "0018,1030": {"action": "REMOVE", "name": "Protocol Name"},  # X/D
    "0018,1042": {"action": "REMOVE", "name": "Contrast/Bolus Start Time"},  # X
    "0018,1043": {"action": "REMOVE", "name": "Contrast/Bolus Stop Time"},  # X
    "0018,1072": {"action": "REMOVE", "name": "Radiopharmaceutical Start Time"},  # X
    "0018,1073": {"action": "REMOVE", "name": "Radiopharmaceutical Stop Time"},  # X
    "0018,1078": {"action": "REMOVE", "name": "Radiopharmaceutical Start DateTime"},  # X
    "0018,1079": {"action": "REMOVE", "name": "Radiopharmaceutical Stop DateTime"},  # X
    "0018,11bb": {"action": "EMPTY", "name": "Acquisition Field Of View Label"},  # D
    "0018,1200": {"action": "REMOVE", "name": "Date of Last Calibration"},  # X
    "0018,1201": {"action": "REMOVE", "name": "Time of Last Calibration"},  # X
    "0018,1202": {"action": "REMOVE", "name": "DateTime of Last Calibration"},  # X
    "0018,1203": {"action": "EMPTY", "name": "Calibration DateTime"},  # Z
    "0018,1204": {"action": "REMOVE", "name": "Date of Manufacture"},  # X
    "0018,1205": {"action": "REMOVE", "name": "Date of Installation"},  # X
    "0018,1400": {"action": "REMOVE", "name": "Acquisition Device Processing Description"},  # X/D
    "0018,4000": {"action": "REMOVE", "name": "Acquisition Comments"},  # X
    "0018,5011": {"action": "REMOVE", "name": "Transducer Identification Sequence"},  # X
    "0018,700a": {"action": "REMOVE", "name": "Detector ID"},  # X/D
    "0018,700c": {"action": "REMOVE", "name": "Date of Last Detector Calibration"},  # X/D
    "0018,700e": {"action": "REMOVE", "name": "Time of Last Detector Calibration"},  # X/D
    "0018,9074": {"action": "EMPTY", "name": "Frame Acquisition DateTime"},  # D
    "0018,9151": {"action": "EMPTY", "name": "Frame Reference DateTime"},  # D
    "0018,9185": {"action": "REMOVE", "name": "Respiratory Motion Compensation Technique Description"},  # X
    "0018,9367": {"action": "EMPTY", "name": "X-Ray Source ID"},  # D
    "0018,9369": {"action": "EMPTY", "name": "Source Start DateTime"},  # D
    "0018,936a": {"action": "EMPTY", "name": "Source End DateTime"},  # D
    "0018,9371": {"action": "EMPTY", "name": "X-Ray Detector ID"},  # D
    "0018,9373": {"action": "REMOVE", "name": "X-Ray Detector Label"},  # X
    "0018,937b": {"action": "REMOVE", "name": "Multi-energy Acquisition Description"},  # X
    "0018,937f": {"action": "REMOVE", "name": "Decomposition Description"},  # X
    "0018,9424": {"action": "REMOVE", "name": "Acquisition Protocol Description"},  # X
    "0018,9516": {"action": "REMOVE", "name": "Start Acquisition DateTime"},  # X/D
    "0018,9517": {"action": "REMOVE", "name": "End Acquisition DateTime"},  # X/D
    "0018,9623": {"action": "EMPTY", "name": "Functional Sync Pulse"},  # D
    "0018,9701": {"action": "EMPTY", "name": "Decay Correction DateTime"},  # D
    "0018,9804": {"action": "EMPTY", "name": "Exclusion Start DateTime"},  # D
    "0018,9919": {"action": "EMPTY", "name": "Instruction Performed DateTime"},  # Z/D
    "0018,9937": {"action": "REMOVE", "name": "Requested Series Description"},  # X
    "0018,a002": {"action": "REMOVE", "name": "Contribution DateTime"},  # X
    "0018,a003": {"action": "REMOVE", "name": "Contribution Description"},  # X
    "0020,0010": {"action": "EMPTY", "name": "Study ID"},  # Z
    "0020,0027": {"action": "REMOVE", "name": "Pyramid Label"},  # X
    "0020,3401": {"action": "REMOVE", "name": "Modifying Device ID"},  # X
    "0020,3403": {"action": "REMOVE", "name": "Modified Image Date"},  # X
    "0020,3405": {"action": "REMOVE", "name": "Modified Image Time"},  # X
    "0020,3406": {"action": "REMOVE", "name": "Modified Image Description"},  # X
    "0020,4000": {"action": "REMOVE", "name": "Image Comments"},  # X
    "0020,9158": {"action": "REMOVE", "name": "Frame Comments"},  # X
    "0028,4000": {"action": "REMOVE", "name": "Image Presentation Comments"},  # X
    "0032,0012": {"action": "REMOVE", "name": "Study ID Issuer"},  # X
    "0032,0032": {"action": "REMOVE", "name": "Study Verified Date"},  # X
    "0032,0033": {"action": "REMOVE", "name": "Study Verified Time"},  # X
    "0032,0034": {"action": "REMOVE", "name": "Study Read Date"},  # X
    "0032,0035": {"action": "REMOVE", "name": "Study Read Time"},  # X
    "0032,1000": {"action": "REMOVE", "name": "Scheduled Study Start Date"},  # X
    "0032,1001": {"action": "REMOVE", "name": "Scheduled Study Start Time"},  # X
    "0032,1010": {"action": "REMOVE", "name": "Scheduled Study Stop Date"},  # X
    "0032,1011": {"action": "REMOVE", "name": "Scheduled Study Stop Time"},  # X
    "0032,1020": {"action": "REMOVE", "name": "Scheduled Study Location"},  # X
    "0032,1021": {"action": "REMOVE", "name": "Scheduled Study Location AE Title"},  # X
    "0032,1030": {"action": "REMOVE", "name": "Reason for Study"},  # X
    "0032,1032": {"action": "REMOVE", "name": "Requesting Physician"},  # X
    "0032,1033": {"action": "REMOVE", "name": "Requesting Service"},  # X
    "0032,1040": {"action": "REMOVE", "name": "Study Arrival Date"},  # X
    "0032,1041": {"action": "REMOVE", "name": "Study Arrival Time"},  # X
    "0032,1050": {"action": "REMOVE", "name": "Study Completion Date"},  # X
    "0032,1051": {"action": "REMOVE", "name": "Study Completion Time"},  # X
    "0032,1060": {"action": "EMPTY", "name": "Requested Procedure Description"},  # X/Z
    "0032,1066": {"action": "REMOVE", "name": "Reason for Visit"},  # X
    "0032,1067": {"action": "REMOVE", "name": "Reason for Visit Code Sequence"},  # X
    "0032,1070": {"action": "REMOVE", "name": "Requested Contrast Agent"},  # X
    "0032,4000": {"action": "REMOVE", "name": "Study Comments"},  # X
    "0034,0002": {"action": "EMPTY", "name": "Flow Identifier"},  # D
    "0034,0005": {"action": "EMPTY", "name": "Source Identifier"},  # D
    "0034,0007": {"action": "EMPTY", "name": "Frame Origin Timestamp"},  # D
    "0038,0004": {"action": "REMOVE", "name": "Referenced Patient Alias Sequence"},  # X
    "0038,0010": {"action": "REMOVE", "name": "Admission ID"},  # X
    "0038,0011": {"action": "REMOVE", "name": "Issuer of Admission ID"},  # X
    "0038,0014": {"action": "REMOVE", "name": "Issuer of Admission ID Sequence"},  # X
    "0038,001a": {"action": "REMOVE", "name": "Scheduled Admission Date"},  # X
    "0038,001b": {"action": "REMOVE", "name": "Scheduled Admission Time"},  # X
    "0038,001c": {"action": "REMOVE", "name": "Scheduled Discharge Date"},  # X
    "0038,001d": {"action": "REMOVE", "name": "Scheduled Discharge Time"},  # X
    "0038,001e": {"action": "REMOVE", "name": "Scheduled Patient Institution Residence"},  # X
    "0038,0020": {"action": "REMOVE", "name": "Admitting Date"},  # X
    "0038,0021": {"action": "REMOVE", "name": "Admitting Time"},  # X
    "0038,0030": {"action": "REMOVE", "name": "Discharge Date"},  # X
    "0038,0032": {"action": "REMOVE", "name": "Discharge Time"},  # X
    "0038,0040": {"action": "REMOVE", "name": "Discharge Diagnosis Description"},  # X
    "0038,0050": {"action": "REMOVE", "name": "Special Needs"},  # X
    "0038,0060": {"action": "REMOVE", "name": "Service Episode ID"},  # X
    "0038,0061": {"action": "REMOVE", "name": "Issuer of Service Episode ID"},  # X
    "0038,0062": {"action": "REMOVE", "name": "Service Episode Description"},  # X
    "0038,0064": {"action": "REMOVE", "name": "Issuer of Service Episode ID Sequence"},  # X
    "0038,0300": {"action": "REMOVE", "name": "Current Patient Location"},  # X
    "0038,0400": {"action": "REMOVE", "name": "Patient's Institution Residence"},  # X
    "0038,0500": {"action": "REMOVE", "name": "Patient State"},  # X
    "0038,4000": {"action": "REMOVE", "name": "Visit Comments"},  # X
    "003a,0020": {"action": "REMOVE", "name": "Multiplex Group Label"},  # X
    "003a,0203": {"action": "REMOVE", "name": "Channel Label"},  # X
    "003a,020c": {"action": "REMOVE", "name": "Channel Derivation Description"},  # X
    "003a,0314": {"action": "EMPTY", "name": "Impedance Measurement DateTime"},  # D
    "003a,0329": {"action": "REMOVE", "name": "Waveform Filter Description"},  # X
    "003a,032b": {"action": "REMOVE", "name": "Filter Lookup Table Description"},  # X
    "0040,0001": {"action": "REMOVE", "name": "Scheduled Station AE Title"},  # X
    "0040,0002": {"action": "REMOVE", "name": "Scheduled Procedure Step Start Date"},  # X
    "0040,0003": {"action": "REMOVE", "name": "Scheduled Procedure Step Start Time"},  # X
    "0040,0004": {"action": "REMOVE", "name": "Scheduled Procedure Step End Date"},  # X
    "0040,0005": {"action": "REMOVE", "name": "Scheduled Procedure Step End Time"},  # X
    "0040,0006": {"action": "REMOVE", "name": "Scheduled Performing Physician's Name"},  # X
    "0040,0007": {"action": "REMOVE", "name": "Scheduled Procedure Step Description"},  # X
    "0040,0009": {"action": "REMOVE", "name": "Scheduled Procedure Step ID"},  # X
    "0040,000b": {"action": "REMOVE", "name": "Scheduled Performing Physician Identification Sequence"},  # X
    "0040,0010": {"action": "REMOVE", "name": "Scheduled Station Name"},  # X
    "0040,0011": {"action": "REMOVE", "name": "Scheduled Procedure Step Location"},  # X
    "0040,0012": {"action": "REMOVE", "name": "Pre-Medication"},  # X
    "0040,0241": {"action": "REMOVE", "name": "Performed Station AE Title"},  # X
    "0040,0242": {"action": "REMOVE", "name": "Performed Station Name"},  # X
    "0040,0243": {"action": "REMOVE", "name": "Performed Location"},  # X
    "0040,0244": {"action": "REMOVE", "name": "Performed Procedure Step Start Date"},  # X
    "0040,0245": {"action": "REMOVE", "name": "Performed Procedure Step Start Time"},  # X
    "0040,0250": {"action": "REMOVE", "name": "Performed Procedure Step End Date"},  # X
    "0040,0251": {"action": "REMOVE", "name": "Performed Procedure Step End Time"},  # X
    "0040,0253": {"action": "REMOVE", "name": "Performed Procedure Step ID"},  # X
    "0040,0254": {"action": "REMOVE", "name": "Performed Procedure Step Description"},  # X
    "0040,0275": {"action": "REMOVE", "name": "Request Attributes Sequence"},  # X
    "0040,0280": {"action": "REMOVE", "name": "Comments on the Performed Procedure Step"},  # X
    "0040,0310": {"action": "REMOVE", "name": "Comments on Radiation Dose"},  # X
    "0040,050a": {"action": "REMOVE", "name": "Specimen Accession Number"},  # X
    "0040,0512": {"action": "EMPTY", "name": "Container Identifier"},  # D
    "0040,0513": {"action": "EMPTY", "name": "Issuer of the Container Identifier Sequence"},  # Z
    "0040,051a": {"action": "REMOVE", "name": "Container Description"},  # X
    "0040,0551": {"action": "EMPTY", "name": "Specimen Identifier"},  # D
    "0040,0555": {"action": "EMPTY", "name": "Acquisition Context Sequence"},  # X/Z
    "0040,0556": {"action": "REMOVE", "name": "Acquisition Context Description"},  # X
    "0040,0562": {"action": "EMPTY", "name": "Issuer of the Specimen Identifier Sequence"},  # Z
    "0040,0600": {"action": "REMOVE", "name": "Specimen Short Description"},  # X
    "0040,0602": {"action": "REMOVE", "name": "Specimen Detailed Description"},  # X
    "0040,0610": {"action": "EMPTY", "name": "Specimen Preparation Sequence"},  # Z
    "0040,06fa": {"action": "REMOVE", "name": "Slide Identifier"},  # X
    "0040,1001": {"action": "REMOVE", "name": "Requested Procedure ID"},  # X
    "0040,1002": {"action": "REMOVE", "name": "Reason for the Requested Procedure"},  # X
    "0040,1004": {"action": "REMOVE", "name": "Patient Transport Arrangements"},  # X
    "0040,1005": {"action": "REMOVE", "name": "Requested Procedure Location"},  # X
    "0040,100a": {"action": "REMOVE", "name": "Reason for Requested Procedure Code Sequence"},  # X
    "0040,1010": {"action": "REMOVE", "name": "Names of Intended Recipients of Results"},  # X
    "0040,1011": {"action": "REMOVE", "name": "Intended Recipients of Results Identification Sequence"},  # X
    "0040,1101": {"action": "EMPTY", "name": "Person Identification Code Sequence"},  # D
    "0040,1102": {"action": "REMOVE", "name": "Person's Address"},  # X
    "0040,1103": {"action": "REMOVE", "name": "Person's Telephone Numbers"},  # X
    "0040,1104": {"action": "REMOVE", "name": "Person's Telecom Information"},  # X
    "0040,1400": {"action": "REMOVE", "name": "Requested Procedure Comments"},  # X
    "0040,2001": {"action": "REMOVE", "name": "Reason for the Imaging Service Request"},  # X
    "0040,2004": {"action": "REMOVE", "name": "Issue Date of Imaging Service Request"},  # X
    "0040,2005": {"action": "REMOVE", "name": "Issue Time of Imaging Service Request"},  # X
    "0040,2008": {"action": "REMOVE", "name": "Order Entered By"},  # X
    "0040,2009": {"action": "REMOVE", "name": "Order Enterer's Location"},  # X
    "0040,2010": {"action": "REMOVE", "name": "Order Callback Phone Number"},  # X
    "0040,2011": {"action": "REMOVE", "name": "Order Callback Telecom Information"},  # X
    "0040,2016": {"action": "EMPTY", "name": "Placer Order Number / Imaging Service Request"},  # Z
    "0040,2017": {"action": "EMPTY", "name": "Filler Order Number / Imaging Service Request"},  # Z
    "0040,2400": {"action": "REMOVE", "name": "Imaging Service Request Comments"},  # X
    "0040,3001": {"action": "REMOVE", "name": "Confidentiality Constraint on Patient Data Description"},  # X
    "0040,4005": {"action": "REMOVE", "name": "Scheduled Procedure Step Start DateTime"},  # X
    "0040,4008": {"action": "REMOVE", "name": "Scheduled Procedure Step Expiration DateTime"},  # X
    "0040,4010": {"action": "REMOVE", "name": "Scheduled Procedure Step Modification DateTime"},  # X
    "0040,4011": {"action": "REMOVE", "name": "Expected Completion DateTime"},  # X
    "0040,4025": {"action": "REMOVE", "name": "Scheduled Station Name Code Sequence"},  # X
    "0040,4027": {"action": "REMOVE", "name": "Scheduled Station Geographic Location Code Sequence"},  # X
    "0040,4028": {"action": "REMOVE", "name": "Performed Station Name Code Sequence"},  # X
    "0040,4030": {"action": "REMOVE", "name": "Performed Station Geographic Location Code Sequence"},  # X
    "0040,4034": {"action": "REMOVE", "name": "Scheduled Human Performers Sequence"},  # X
    "0040,4035": {"action": "REMOVE", "name": "Actual Human Performers Sequence"},  # X
    "0040,4036": {"action": "REMOVE", "name": "Human Performer's Organization"},  # X
    "0040,4037": {"action": "REMOVE", "name": "Human Performer's Name"},  # X
    "0040,4050": {"action": "REMOVE", "name": "Performed Procedure Step Start DateTime"},  # X
    "0040,4051": {"action": "REMOVE", "name": "Performed Procedure Step End DateTime"},  # X
    "0040,4052": {"action": "REMOVE", "name": "Procedure Step Cancellation DateTime"},  # X
    "0040,a023": {"action": "REMOVE", "name": "Findings Group Recording Date (Trial)"},  # X
    "0040,a024": {"action": "REMOVE", "name": "Findings Group Recording Time (Trial)"},  # X
    "0040,a027": {"action": "EMPTY", "name": "Verifying Organization"},  # D
    "0040,a030": {"action": "EMPTY", "name": "Verification DateTime"},  # D
    "0040,a032": {"action": "REMOVE", "name": "Observation DateTime"},  # X/D
    "0040,a033": {"action": "REMOVE", "name": "Observation Start DateTime"},  # X
    "0040,a034": {"action": "REMOVE", "name": "Effective Start DateTime"},  # X
    "0040,a035": {"action": "REMOVE", "name": "Effective Stop DateTime"},  # X
    "0040,a075": {"action": "EMPTY", "name": "Verifying Observer Name"},  # D
    "0040,a078": {"action": "REMOVE", "name": "Author Observer Sequence"},  # X
    "0040,a07a": {"action": "REMOVE", "name": "Participant Sequence"},  # X
    "0040,a07c": {"action": "REMOVE", "name": "Custodial Organization Sequence"},  # X
    "0040,a082": {"action": "EMPTY", "name": "Participation DateTime"},  # Z
    "0040,a088": {"action": "EMPTY", "name": "Verifying Observer Identification Code Sequence"},  # Z
    "0040,a110": {"action": "REMOVE", "name": "Date of Document or Verbal Transaction (Trial)"},  # X
    "0040,a112": {"action": "REMOVE", "name": "Time of Document Creation or Verbal Transaction (Trial)"},  # X
    "0040,a120": {"action": "EMPTY", "name": "DateTime"},  # D
    "0040,a121": {"action": "EMPTY", "name": "Date"},  # D
    "0040,a122": {"action": "EMPTY", "name": "Time"},  # D
    "0040,a123": {"action": "EMPTY", "name": "Person Name"},  # D
    "0040,a13a": {"action": "EMPTY", "name": "Referenced DateTime"},  # D
    "0040,a192": {"action": "REMOVE", "name": "Observation Date (Trial)"},  # X
    "0040,a193": {"action": "REMOVE", "name": "Observation Time (Trial)"},  # X
    "0040,a307": {"action": "REMOVE", "name": "Current Observer (Trial)"},  # X
    "0040,a352": {"action": "REMOVE", "name": "Verbal Source (Trial)"},  # X
    "0040,a353": {"action": "REMOVE", "name": "Address (Trial)"},  # X
    "0040,a354": {"action": "REMOVE", "name": "Telephone Number (Trial)"},  # X
    "0040,a358": {"action": "REMOVE", "name": "Verbal Source Identifier Code Sequence (Trial)"},  # X
    "0040,b034": {"action": "REMOVE", "name": "Annotation DateTime"},  # X
    "0040,b036": {"action": "REMOVE", "name": "Segment Definition DateTime"},  # X
    "0040,b03b": {"action": "REMOVE", "name": "Montage Name"},  # X
    "0040,b03f": {"action": "REMOVE", "name": "Montage Channel Label"},  # X
    "0040,db06": {"action": "REMOVE", "name": "Template Version"},  # X
    "0040,db07": {"action": "REMOVE", "name": "Template Local Version"},  # X
    "0040,e004": {"action": "REMOVE", "name": "HL7 Document Effective Time"},  # X
    "0040,e012": {"action": "REMOVE", "name": "Display URI"},  # X
    "0042,0011": {"action": "EMPTY", "name": "Encapsulated Document"},  # D
    "0044,0004": {"action": "REMOVE", "name": "Approval Status DateTime"},  # X
    "0044,000b": {"action": "REMOVE", "name": "Product Expiration DateTime"},  # X
    "0044,0010": {"action": "REMOVE", "name": "Substance Administration DateTime"},  # X
    "0044,0104": {"action": "EMPTY", "name": "Assertion DateTime"},  # D
    "0044,0105": {"action": "REMOVE", "name": "Assertion Expiration DateTime"},  # X
    "0050,001b": {"action": "REMOVE", "name": "Container Component ID"},  # X
    "0050,0020": {"action": "REMOVE", "name": "Device Description"},  # X
    "0050,0021": {"action": "REMOVE", "name": "Long Device Description"},  # X
    "0068,6226": {"action": "EMPTY", "name": "Effective DateTime"},  # D
    "0068,6270": {"action": "EMPTY", "name": "Information Issue DateTime"},  # D
    "006a,0005": {"action": "EMPTY", "name": "Annotation Group Label"},  # D
    "006a,0006": {"action": "REMOVE", "name": "Annotation Group Description"},  # X
    # Free-text annotation commentary. Reaches annotations.json `note`
    # when a caller opts in via include_annotation_text; remediated here
    # so that opting in still does not surface raw text.
    "0070,0006": {"action": "EMPTY", "name": "Unformatted Text Value"},  # D
    "0070,0082": {"action": "REMOVE", "name": "Presentation Creation Date"},  # X
    "0070,0083": {"action": "REMOVE", "name": "Presentation Creation Time"},  # X
    "0070,0084": {"action": "EMPTY", "name": "Content Creator's Name"},  # Z/D
    "0070,0086": {"action": "REMOVE", "name": "Content Creator's Identification Code Sequence"},  # X
    "0072,000a": {"action": "EMPTY", "name": "Hanging Protocol Creation DateTime"},  # D
    "0072,005e": {"action": "EMPTY", "name": "Selector AE Value"},  # D
    "0072,005f": {"action": "EMPTY", "name": "Selector AS Value"},  # D
    "0072,0061": {"action": "EMPTY", "name": "Selector DA Value"},  # D
    "0072,0063": {"action": "EMPTY", "name": "Selector DT Value"},  # D
    "0072,0065": {"action": "EMPTY", "name": "Selector OB Value"},  # D
    "0072,0066": {"action": "EMPTY", "name": "Selector LO Value"},  # D
    "0072,0068": {"action": "EMPTY", "name": "Selector LT Value"},  # D
    "0072,006a": {"action": "EMPTY", "name": "Selector PN Value"},  # D
    "0072,006b": {"action": "EMPTY", "name": "Selector TM Value"},  # D
    "0072,006c": {"action": "EMPTY", "name": "Selector SH Value"},  # D
    "0072,006d": {"action": "EMPTY", "name": "Selector UN Value"},  # D
    "0072,006e": {"action": "EMPTY", "name": "Selector ST Value"},  # D
    "0072,0070": {"action": "EMPTY", "name": "Selector UT Value"},  # D
    "0072,0071": {"action": "EMPTY", "name": "Selector UR Value"},  # D
    "0074,1234": {"action": "REMOVE", "name": "Receiving AE"},  # X
    "0074,1236": {"action": "REMOVE", "name": "Requesting AE"},  # X
    "0088,0904": {"action": "REMOVE", "name": "Topic Title"},  # X
    "0088,0906": {"action": "REMOVE", "name": "Topic Subject"},  # X
    "0088,0910": {"action": "REMOVE", "name": "Topic Author"},  # X
    "0088,0912": {"action": "REMOVE", "name": "Topic Keywords"},  # X
    "0100,0420": {"action": "REMOVE", "name": "SOP Authorization DateTime"},  # X
    "0400,0105": {"action": "EMPTY", "name": "Digital Signature DateTime"},  # D
    "0400,0115": {"action": "EMPTY", "name": "Certificate of Signer"},  # D
    "0400,0310": {"action": "REMOVE", "name": "Certified Timestamp"},  # X
    "0400,0402": {"action": "REMOVE", "name": "Referenced Digital Signature Sequence"},  # X
    "0400,0403": {"action": "REMOVE", "name": "Referenced SOP Instance MAC Sequence"},  # X
    "0400,0404": {"action": "REMOVE", "name": "MAC"},  # X
    "0400,0550": {"action": "REMOVE", "name": "Modified Attributes Sequence"},  # X
    "0400,0551": {"action": "REMOVE", "name": "Nonconforming Modified Attributes Sequence"},  # X
    "0400,0552": {"action": "REMOVE", "name": "Nonconforming Data Element Value"},  # X
    "0400,0561": {"action": "REMOVE", "name": "Original Attributes Sequence"},  # X
    "0400,0562": {"action": "EMPTY", "name": "Attribute Modification DateTime"},  # D
    "0400,0563": {"action": "EMPTY", "name": "Modifying System"},  # D
    "0400,0564": {"action": "EMPTY", "name": "Source of Previous Values"},  # Z
    "0400,0565": {"action": "EMPTY", "name": "Reason for the Attribute Modification"},  # D
    "0400,0600": {"action": "REMOVE", "name": "Instance Origin Status"},  # X
    "2030,0020": {"action": "REMOVE", "name": "Text String"},  # X
    "2100,0040": {"action": "REMOVE", "name": "Creation Date"},  # X
    "2100,0050": {"action": "REMOVE", "name": "Creation Time"},  # X
    "2100,0070": {"action": "REMOVE", "name": "Originator"},  # X
    "2100,0140": {"action": "EMPTY", "name": "Destination AE"},  # D
    "2200,0002": {"action": "EMPTY", "name": "Label Text"},  # X/Z
    "2200,0005": {"action": "EMPTY", "name": "Barcode Value"},  # X/Z
    "3002,0121": {"action": "REMOVE", "name": "Position Acquisition Template Name"},  # X
    "3002,0123": {"action": "REMOVE", "name": "Position Acquisition Template Description"},  # X
    "3006,0002": {"action": "EMPTY", "name": "Structure Set Label"},  # D
    "3006,0004": {"action": "REMOVE", "name": "Structure Set Name"},  # X
    "3006,0006": {"action": "REMOVE", "name": "Structure Set Description"},  # X
    "3006,0008": {"action": "EMPTY", "name": "Structure Set Date"},  # Z
    "3006,0009": {"action": "EMPTY", "name": "Structure Set Time"},  # Z
    "3006,0026": {"action": "EMPTY", "name": "ROI Name"},  # Z
    "3006,0028": {"action": "REMOVE", "name": "ROI Description"},  # X
    "3006,002d": {"action": "REMOVE", "name": "ROI DateTime"},  # X
    "3006,002e": {"action": "REMOVE", "name": "ROI Observation DateTime"},  # X
    "3006,0038": {"action": "REMOVE", "name": "ROI Generation Description"},  # X
    "3006,004d": {"action": "REMOVE", "name": "ROI Creator Sequence"},  # X
    "3006,004e": {"action": "REMOVE", "name": "ROI Interpreter Sequence"},  # X
    "3006,0085": {"action": "REMOVE", "name": "ROI Observation Label"},  # X
    "3006,0088": {"action": "REMOVE", "name": "ROI Observation Description"},  # X
    "3006,00a6": {"action": "EMPTY", "name": "ROI Interpreter"},  # Z
    "3008,0024": {"action": "EMPTY", "name": "Treatment Control Point Date"},  # D
    "3008,0025": {"action": "EMPTY", "name": "Treatment Control Point Time"},  # D
    "3008,0054": {"action": "REMOVE", "name": "First Treatment Date"},  # X/D
    "3008,0056": {"action": "REMOVE", "name": "Most Recent Treatment Date"},  # X/D
    "3008,0105": {"action": "EMPTY", "name": "Source Serial Number"},  # X/Z
    "3008,0162": {"action": "EMPTY", "name": "Safe Position Exit Date"},  # D
    "3008,0164": {"action": "EMPTY", "name": "Safe Position Exit Time"},  # D
    "3008,0166": {"action": "EMPTY", "name": "Safe Position Return Date"},  # D
    "3008,0168": {"action": "EMPTY", "name": "Safe Position Return Time"},  # D
    "3008,0250": {"action": "REMOVE", "name": "Treatment Date"},  # X/D
    "3008,0251": {"action": "REMOVE", "name": "Treatment Time"},  # X/D
    "300a,0002": {"action": "EMPTY", "name": "RT Plan Label"},  # D
    "300a,0003": {"action": "REMOVE", "name": "RT Plan Name"},  # X
    "300a,0004": {"action": "REMOVE", "name": "RT Plan Description"},  # X
    "300a,0006": {"action": "REMOVE", "name": "RT Plan Date"},  # X/D
    "300a,0007": {"action": "REMOVE", "name": "RT Plan Time"},  # X/D
    "300a,000b": {"action": "REMOVE", "name": "Treatment Sites"},  # X
    "300a,000e": {"action": "REMOVE", "name": "Prescription Description"},  # X
    "300a,0016": {"action": "REMOVE", "name": "Dose Reference Description"},  # X
    "300a,0072": {"action": "REMOVE", "name": "Fraction Group Description"},  # X
    "300a,00b2": {"action": "EMPTY", "name": "Treatment Machine Name"},  # X/Z
    "300a,00c3": {"action": "REMOVE", "name": "Beam Description"},  # X
    "300a,00dd": {"action": "REMOVE", "name": "Bolus Description"},  # X
    "300a,0196": {"action": "REMOVE", "name": "Fixation Device Description"},  # X
    "300a,01a6": {"action": "REMOVE", "name": "Shielding Device Description"},  # X
    "300a,01b2": {"action": "REMOVE", "name": "Setup Technique Description"},  # X
    "300a,0216": {"action": "REMOVE", "name": "Source Manufacturer"},  # X
    "300a,022c": {"action": "EMPTY", "name": "Source Strength Reference Date"},  # D
    "300a,022e": {"action": "EMPTY", "name": "Source Strength Reference Time"},  # D
    "300a,02eb": {"action": "REMOVE", "name": "Compensator Description"},  # X
    "300a,0608": {"action": "EMPTY", "name": "Treatment Position Group Label"},  # D
    "300a,0611": {"action": "EMPTY", "name": "RT Accessory Holder Slot ID"},  # Z
    "300a,0615": {"action": "EMPTY", "name": "RT Accessory Device Slot ID"},  # Z
    "300a,0619": {"action": "EMPTY", "name": "Radiation Dose Identification Label"},  # D
    "300a,0623": {"action": "EMPTY", "name": "Radiation Dose In-Vivo Measurement Label"},  # D
    "300a,062a": {"action": "EMPTY", "name": "RT Tolerance Set Label"},  # D
    "300a,0676": {"action": "REMOVE", "name": "Equipment Frame of Reference Description"},  # X
    "300a,067c": {"action": "EMPTY", "name": "Radiation Generation Mode Label"},  # D
    "300a,067d": {"action": "EMPTY", "name": "Radiation Generation Mode Description"},  # Z
    "300a,0734": {"action": "EMPTY", "name": "Treatment Tolerance Violation Description"},  # D
    "300a,0736": {"action": "EMPTY", "name": "Treatment Tolerance Violation DateTime"},  # D
    "300a,073a": {"action": "EMPTY", "name": "Recorded RT Control Point DateTime"},  # D
    "300a,0741": {"action": "EMPTY", "name": "Interlock DateTime"},  # D
    "300a,0742": {"action": "EMPTY", "name": "Interlock Description"},  # D
    "300a,0760": {"action": "EMPTY", "name": "Override DateTime"},  # D
    "300a,0783": {"action": "EMPTY", "name": "Interlock Origin Description"},  # D
    "300a,078e": {"action": "REMOVE", "name": "Patient Treatment Preparation Procedure Parameter Description"},  # X
    "300a,0792": {"action": "REMOVE", "name": "Patient Treatment Preparation Method Description"},  # X
    "300a,0794": {"action": "REMOVE", "name": "Patient Setup Photo Description"},  # X
    "300a,079a": {"action": "REMOVE", "name": "Displacement Reference Label"},  # X
    "300c,0113": {"action": "REMOVE", "name": "Reason for Omission Description"},  # X
    "300c,0127": {"action": "EMPTY", "name": "Beam Hold Transition DateTime"},  # D
    "300e,0004": {"action": "EMPTY", "name": "Review Date"},  # Z
    "300e,0005": {"action": "EMPTY", "name": "Review Time"},  # Z
    "300e,0008": {"action": "EMPTY", "name": "Reviewer Name"},  # X/Z
    "3010,000f": {"action": "EMPTY", "name": "Conceptual Volume Combination Description"},  # Z
    "3010,0017": {"action": "EMPTY", "name": "Conceptual Volume Description"},  # Z
    "3010,001b": {"action": "EMPTY", "name": "Device Alternate Identifier"},  # Z
    "3010,002d": {"action": "EMPTY", "name": "Device Label"},  # D
    "3010,0033": {"action": "EMPTY", "name": "User Content Label"},  # D
    "3010,0034": {"action": "EMPTY", "name": "User Content Long Label"},  # D
    "3010,0035": {"action": "EMPTY", "name": "Entity Label"},  # D
    "3010,0036": {"action": "REMOVE", "name": "Entity Name"},  # X
    "3010,0037": {"action": "REMOVE", "name": "Entity Description"},  # X
    "3010,0038": {"action": "EMPTY", "name": "Entity Long Label"},  # D
    "3010,0043": {"action": "EMPTY", "name": "Manufacturer's Device Identifier"},  # Z
    "3010,004c": {"action": "REMOVE", "name": "Intended Phase Start Date"},  # X/D
    "3010,004d": {"action": "REMOVE", "name": "Intended Phase End Date"},  # X/D
    "3010,0054": {"action": "EMPTY", "name": "RT Prescription Label"},  # D
    "3010,0056": {"action": "REMOVE", "name": "RT Treatment Approach Label"},  # X/D
    "3010,005a": {"action": "EMPTY", "name": "RT Physician Intent Narrative"},  # Z
    "3010,005c": {"action": "EMPTY", "name": "Reason for Superseding"},  # Z
    "3010,0061": {"action": "REMOVE", "name": "Prior Treatment Dose Description"},  # X
    "3010,0077": {"action": "REMOVE", "name": "Treatment Site"},  # X/D
    "3010,007a": {"action": "EMPTY", "name": "Treatment Technique Notes"},  # Z
    "3010,007b": {"action": "EMPTY", "name": "Prescription Notes"},  # Z
    "3010,007f": {"action": "EMPTY", "name": "Fractionation Notes"},  # Z
    "3010,0081": {"action": "EMPTY", "name": "Prescription Notes Sequence"},  # Z
    "3010,0085": {"action": "REMOVE", "name": "Intended Fraction Start Time"},  # X
    "4000,0010": {"action": "REMOVE", "name": "Arbitrary"},  # X
    "4000,4000": {"action": "REMOVE", "name": "Text Comments"},  # X
    "4008,0040": {"action": "REMOVE", "name": "Results ID"},  # X
    "4008,0042": {"action": "REMOVE", "name": "Results ID Issuer"},  # X
    "4008,0100": {"action": "REMOVE", "name": "Interpretation Recorded Date"},  # X
    "4008,0101": {"action": "REMOVE", "name": "Interpretation Recorded Time"},  # X
    "4008,0102": {"action": "REMOVE", "name": "Interpretation Recorder"},  # X
    "4008,0108": {"action": "REMOVE", "name": "Interpretation Transcription Date"},  # X
    "4008,0109": {"action": "REMOVE", "name": "Interpretation Transcription Time"},  # X
    "4008,010a": {"action": "REMOVE", "name": "Interpretation Transcriber"},  # X
    "4008,010b": {"action": "REMOVE", "name": "Interpretation Text"},  # X
    "4008,010c": {"action": "REMOVE", "name": "Interpretation Author"},  # X
    "4008,0111": {"action": "REMOVE", "name": "Interpretation Approver Sequence"},  # X
    "4008,0112": {"action": "REMOVE", "name": "Interpretation Approval Date"},  # X
    "4008,0113": {"action": "REMOVE", "name": "Interpretation Approval Time"},  # X
    "4008,0114": {"action": "REMOVE", "name": "Physician Approving Interpretation"},  # X
    "4008,0115": {"action": "REMOVE", "name": "Interpretation Diagnosis Description"},  # X
    "4008,0118": {"action": "REMOVE", "name": "Results Distribution List Sequence"},  # X
    "4008,0119": {"action": "REMOVE", "name": "Distribution Name"},  # X
    "4008,011a": {"action": "REMOVE", "name": "Distribution Address"},  # X
    "4008,0200": {"action": "REMOVE", "name": "Interpretation ID"},  # X
    "4008,0202": {"action": "REMOVE", "name": "Interpretation ID Issuer"},  # X
    "4008,0300": {"action": "REMOVE", "name": "Impressions"},  # X
    "4008,4000": {"action": "REMOVE", "name": "Results Comments"},  # X
    # Repeating group 60xx: one rule per even group 6000-601E, because
    # the loader refuses mask keys. Removing Overlay Data leaves the
    # rest of the Overlay Plane module (#556).
    "6000,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6000,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6002,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6002,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6004,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6004,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6006,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6006,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6008,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6008,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "600a,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "600a,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "600c,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "600c,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "600e,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "600e,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6010,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6010,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6012,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6012,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6014,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6014,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6016,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6016,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "6018,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "6018,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "601a,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "601a,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "601c,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "601c,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "601e,3000": {"action": "REMOVE", "name": "Overlay Data"},  # X
    "601e,4000": {"action": "REMOVE", "name": "Overlay Comments"},  # X
    "fffa,fffa": {"action": "REMOVE", "name": "Digital Signatures Sequence"},  # X
    "fffc,fffc": {"action": "REMOVE", "name": "Data Set Trailing Padding"},  # X
}

# The three entries where a research export deliberately departs from the
# basic profile: the study date is jittered so intervals survive, and sex
# and age are kept because analyses stratify on them. `create_config()`
# writes exactly these beneath `privacy_profile: basic` -- it diffs the
# floor against `BASIC_PROFILE`, so the scaffold cannot drift from this
# table -- and they are half of what a bare session applies. Keys
# lowercase, for the reason the header comment above gives.
RESEARCH_DEFAULTS = {
    "0008,0020": {"action": "JITTER", "name": "Study Date"},
    "0010,0040": {"action": "KEEP", "name": "Patient's Sex"},
    "0010,1010": {"action": "KEEP", "name": "Patient's Age"},
}

# What a session applies when it has loaded no configuration, and what a
# config file with no `privacy_profile` line extends (#495). Until then a
# bare session scanned against `{}`: it remediated patient name, ID and
# study date through the hardcoded entity checks and left Study ID,
# Institution Name, Station Name and every series/acquisition/content
# date in the exported file, graded PASS.
#
# Not a named profile. `PRIVACY_PROFILES` is what a config can spell; the
# floor is what spelling nothing means, so it has no name to collide with
# a user's. Callers that hand it out copy it (`copy.deepcopy`) -- a
# session's `set_phi_tag` writes into its own `phi_tags`, and a shared
# dict would carry that edit into every later session. The dict is built
# from copies of each entry for the same reason: `BASIC_PROFILE` is what
# `privacy_profile: basic` reads.
FLOOR_POLICY = {tag: dict(rule)
                for tag, rule in {**BASIC_PROFILE, **RESEARCH_DEFAULTS}.items()}

PRIVACY_PROFILES = {
    "basic": BASIC_PROFILE
}
