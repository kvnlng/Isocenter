"""Text for the `SCRUB` tests (#1130): descriptions with no identifier,
and the shapes #1130 and #1131 quote.

In `tests/support/` for the reason `project_secret.py` gives. No
`isocenter` import. The clinical corpus is Appendix A of the batch spec,
with every string that could have come from a site's own data replaced
by an invented one of the same shape. The issue rows have the shapes
the issues describe, with invented values, and no value comes from any
benchmark corpus.
"""

#: Descriptions a study selects series by: none holds an identifier.
CLINICAL = [
    "CT PELVIS W CONTRAST", "MR BREAST BILATERAL", "PET CT WHOLE BODY",
    "LUNG CA ORVIN 4471", "T1 AX POST GAD", "Axial FLAIR", "AX T2 FSE",
    "SAG T1", "CHEST PA AND LAT", "ABC-COHORT", "Nodule 1", "pylidc", "0.2.0",
    "Thorax 1.0 B45f", "ABDOMEN PELVIS", "Pelvis^02_PelvisStandard",
    "Knee^Routine", "MR^Spine", "BRAIN WO CONTRAST", "MRI BRAIN W/WO",
    "Chest^1_CHESTROUTINE", "CT HEAD WITHOUT CONTRAST",
    "XR KNEE 3 VIEWS RIGHT", "US ABDOMEN COMPLETE",
    "MAMMO SCREENING BILATERAL", "MR PROSTATE MULTIPARAMETRIC", "DWI b1000",
    "Perfusion 3D ASL", "Ax DWI ADC", "Morphology", "Philips Ingenia",
    "Siemens Somatom Definition", "KM", "Lesion 4 - Reader 2",
    "Lung 1.0 B70f", "RECIST target lesion 1", "Brain Tumor Protocol",
    "PROSTATE MRI TRIAL 6612", "Mayo Protocol", "Boston Scientific",
    "Hounsfield calibration", "Paget disease follow-up", "Wilms tumor",
    "Crohn disease enterography", "Hodgkin lymphoma staging",
    "Parkinson DaTscan", "Alzheimer amyloid PET",
]

#: The shapes #1130 and #1131 quote: source, the answer expected, and the
#: names the instance holds.
ISSUE_ROWS = [
    ("MR HEPA UNIT for 912-48-3071", "MR HEPA UNIT", ()),
    ("MG BIOPSY SPECIMEN for 3920584716", "MG BIOPSY SPECIMEN", ()),
    ("MG BIOPSY SPECIMEN 20190314", "MG BIOPSY SPECIMEN", ()),
    ("4.2 KNEE TIBIA for 6038172945", "4.2 KNEE TIBIA", ()),
    ("Routine Breast Screening Report for Elena Marsh",
     "Routine Breast Screening Report", ("ELENA", "MARSH")),
]

#: Every row #1130's table and #1131's list quote, for the real-model
#: property tests: the source only.
ISSUE_SOURCES = [row[0] for row in ISSUE_ROWS] + [
    "MR HEPA, UNIT at TWMC",
    "LUNG CA ORVIN 4471 at Fernwood Community Clinic",
]
