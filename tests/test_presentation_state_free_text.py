"""A presentation state's annotations are not exported with free text no row cleans.

PS3.15 Table E.1-1 codes Graphic Annotation Sequence (0070,0001) `D`. A
text or graphic object in it can carry Tracking ID (0062,0020), and a
compound graphic's major tick a Tick Label (0070,0289): free text the table
has no row for, so the rows nested inside cannot clean it. `basic@2026c`,
and the floor built on it, remove the sequence, as they do Content Sequence
for an SR's TEXT items (#848). Removed, not emptied: the sequence is Type 1
in the Graphic Annotation Module, so a zero-item one breaks PS3.3, while a
presentation state needs the module only if annotations are to be applied.
Until 1.0.0rc4 it had no rule, and both were exported as written under
`(0012,0062) YES` with a PASS grade (found in the review of #840); 1.0.0rc4
emptied it.
"""
import os

import pydicom
import pydicom.data
import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from isocenter import Session

#: Free text an annotation can carry: letters in each, so no replacement
#: UID can contain one by chance.
NOTE = "Annotation for Quillon Vantreese"
TRACKING_ID = "Vantreese left adrenal"
TICK_LABEL = "QV tick"


def _with_graphic_annotation(path):
    """pydicom's `CT_small.dcm` with one Graphic Annotation item: a text
    object holding an Unformatted Text Value (a `D` row), a Tracking ID and
    a Tracking UID (a `U` row), and a ruler whose major tick holds a Tick
    Label."""
    ds = pydicom.dcmread(pydicom.data.get_testdata_file("CT_small.dcm"))
    text = Dataset()
    text.AnchorPointAnnotationUnits = "PIXEL"
    text.AnchorPoint = [10.0, 10.0]
    text.AnchorPointVisibility = "Y"
    text.UnformattedTextValue = NOTE
    text.TrackingID = TRACKING_ID
    text.TrackingUID = "1.2.826.0.1.3680043.2.1125.840.1"
    tick = Dataset()
    tick.TickPosition = 0.5
    tick.TickLabel = TICK_LABEL
    ruler = Dataset()
    ruler.CompoundGraphicInstanceID = 1
    ruler.CompoundGraphicUnits = "PIXEL"
    ruler.GraphicDimensions = 2
    ruler.NumberOfGraphicPoints = 2
    ruler.GraphicData = [0.0, 0.0, 20.0, 20.0]
    ruler.CompoundGraphicType = "RULER"
    ruler.MajorTicksSequence = Sequence([tick])
    ruler.TickAlignment = "CENTER"
    ruler.TickLabelAlignment = "TOP"
    ruler.ShowTickLabel = "Y"
    annotation = Dataset()
    annotation.GraphicLayer = "LAYER1"
    annotation.TextObjectSequence = Sequence([text])
    annotation.CompoundGraphicSequence = Sequence([ruler])
    ds.GraphicAnnotationSequence = Sequence([annotation])
    ds.save_as(path)


def _export(tmp_path, config=None):
    """Ingest, audit, anonymize and export that file under `config`, YAML
    text, or a bare session (the floor) for None. Returns the exported
    file's path."""
    os.makedirs(tmp_path / "in")
    _with_graphic_annotation(str(tmp_path / "in" / "ct.dcm"))
    with Session(str(tmp_path / "s.db")) as session:
        if config is not None:
            (tmp_path / "config.yaml").write_text(config)
            session.load_config(str(tmp_path / "config.yaml"))
        session.ingest(str(tmp_path / "in"))
        session.anonymize(session.audit())
        summary = session.export(str(tmp_path / "out"), use_compression=False)
    assert summary.written == 1, summary.failures
    written = [os.path.join(root, name) for root, _, names in os.walk(tmp_path / "out")
               for name in names if name.endswith(".dcm")]
    assert len(written) == 1, written
    return written[0]


@pytest.mark.parametrize("config", [None, "privacy_profile: basic\n"],
                         ids=["floor", "basic"])
def test_a_presentation_state_s_free_text_is_not_exported(tmp_path, config):
    """Kills: `0070,0001` without a rule again, or given `KEEP` (Tracking ID
    and Tick Label are exported); `EMPTY` in place of `REMOVE` (the
    sequence would be present with zero items); and the pass declining
    the finding, which would withhold the marker."""
    path = _export(tmp_path, config)

    with open(path, "rb") as handle:
        raw = handle.read()
    exported = [part for part in (b"Quillon Vantreese", b"Vantreese left adrenal",
                                  b"QV tick") if part in raw]
    assert exported == []
    out = pydicom.dcmread(path)
    assert "GraphicAnnotationSequence" not in out
    assert out.PatientIdentityRemoved == "YES"


def test_a_graphic_annotation_the_configuration_keeps_exports_its_free_text(tmp_path):
    """Beyond the table, the configuration decides. `KEEP` on Graphic
    Annotation Sequence keeps the annotations, and Tracking ID and Tick
    Label are exported as written: no row reaches them, and Isocenter does
    not implement the Clean Graphics option, which would clean them. The
    rows nested inside still apply: Unformatted Text Value holds its dummy
    and the Tracking UID is replaced.

    Kills: a `KEEP` on a sequence that still empties it, a rule on either
    tag added to the profile unannounced, and the nested rows skipped under
    a kept sequence."""
    config = ("privacy_profile: basic\n"
              "phi_tags:\n"
              "  \"0070,0001\": {action: KEEP}\n")

    path = _export(tmp_path, config)

    annotation = pydicom.dcmread(path).GraphicAnnotationSequence[0]
    text = annotation.TextObjectSequence[0]
    assert text.TrackingID == TRACKING_ID
    assert annotation.CompoundGraphicSequence[0].MajorTicksSequence[0].TickLabel == TICK_LABEL
    assert text.UnformattedTextValue == "ANONYMIZED"
    assert text.TrackingUID.startswith("2.25."), text.TrackingUID
