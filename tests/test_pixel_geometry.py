"""Unit tests for `isocenter.pixel_geometry`.

Every row of the design spec's worked-edge-case table (§5 of
`docs/superpowers/specs/2026-08-29-pixel-geometry-authority.md`) is a test
here. The resolver is pure -- a shape tuple and an attributes dict in, a
`PixelGeometry` out -- so these need no session, no store and no numpy.
"""
import pytest

from isocenter.pixel_geometry import (
    GeometryEvidence,
    PixelGeometry,
    declared_int,
    planar_configuration_default,
    resolve_photometric_interpretation,
    resolve_pixel_geometry,
)


# ---------------------------------------------------------------------------
# _declared / declared_int (§3.3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (3, 3),
    ("3", 3),
    (" 3 ", 3),
    (None, None),
    ("", None),
    ("abc", None),
    ([1, 2], None),
    # `int(str(1.0))` raises, so a float reads as "not declared" rather than
    # as 1. None of these tags has a VR that decodes to a float; a value that
    # is one did not come from a conformant source, and guessing at it is the
    # habit this whole module exists to remove.
    (1.0, None),
])
def test_declared_int_coercion(raw, expected):
    """Absent, empty, unparseable and non-scalar all read as 'not declared'."""
    assert declared_int({"0028,0008": raw}, "0028,0008") == expected


def test_declared_int_missing_key_is_none():
    assert declared_int({}, "0028,0008") is None


def test_declared_int_survives_a_hostile_mapping():
    """A mapping whose .get raises must read as 'not declared', not explode.

    `tests/test_pixel_analysis.py` drives `analyze_pixels` with a
    `MagicMock` instance; the resolver has to tolerate whatever
    `attributes.get` hands back.
    """
    class Exploding:
        def get(self, _key):
            raise RuntimeError("no attributes here")

    assert declared_int(Exploding(), "0028,0008") is None


# ---------------------------------------------------------------------------
# Rank 3, grayscale multi-frame -- the filed defect (§5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape,attrs,expected", [
    ((3, 4, 4), {"0028,0002": 1, "0028,0008": 3, "0028,0010": 4, "0028,0011": 4},
     PixelGeometry(3, 4, 4, 1, GeometryEvidence.DECLARED)),
    ((3, 8, 3), {"0028,0002": 1, "0028,0008": 3, "0028,0010": 8, "0028,0011": 3},
     PixelGeometry(3, 8, 3, 1, GeometryEvidence.DECLARED)),
    ((2, 8, 4), {"0028,0002": 1, "0028,0008": 2, "0028,0010": 8, "0028,0011": 4},
     PixelGeometry(2, 8, 4, 1, GeometryEvidence.DECLARED)),
    ((3, 8, 8), {"0028,0002": 1},
     PixelGeometry(3, 8, 8, 1, GeometryEvidence.DECLARED)),
    # 8 is not in {3, 4}, so arm B is inadmissible on structure alone.
    ((3, 8, 8), {},
     PixelGeometry(3, 8, 8, 1, GeometryEvidence.STRUCTURAL)),
])
def test_rank3_grayscale_multiframe(shape, attrs, expected):
    assert resolve_pixel_geometry(shape, attrs) == expected


# ---------------------------------------------------------------------------
# Rank 3, colour (§5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape,attrs,expected", [
    ((4, 4, 3), {"0028,0002": 3, "0028,0010": 4, "0028,0011": 4},
     PixelGeometry(1, 4, 4, 3, GeometryEvidence.DECLARED)),
    ((8, 8, 3), {"0028,0002": 3, "0028,0004": "YBR_FULL"},
     PixelGeometry(1, 8, 8, 3, GeometryEvidence.DECLARED)),
    # Non-conformant but declared: SamplesPerPixel=4 is honoured.
    ((8, 8, 4), {"0028,0002": 4},
     PixelGeometry(1, 8, 8, 4, GeometryEvidence.DECLARED)),
    ((100, 200, 3), {},
     PixelGeometry(1, 100, 200, 3, GeometryEvidence.GUESSED)),
    ((10, 10, 3), {"0028,0004": "MONOCHROME2"},
     PixelGeometry(1, 10, 10, 3, GeometryEvidence.GUESSED)),
])
def test_rank3_colour(shape, attrs, expected):
    assert resolve_pixel_geometry(shape, attrs) == expected


# ---------------------------------------------------------------------------
# Rank 3, tiebreaks (§5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape,attrs,expected", [
    # shape[0] == 1 makes step 2's A_ok and B_ok both true, so NumberOfFrames
    # cannot discriminate and this must fall through to the guess. Writing
    # step 2 as `if f_d > 1: A else: B` returns B/DECLARED here and is wrong:
    # the arm happens to match, the evidence does not, and it is the evidence
    # that decides whether the export refuses the write.
    ((1, 4, 3), {"0028,0008": 1},
     PixelGeometry(1, 1, 4, 3, GeometryEvidence.GUESSED)),
    ((5, 4, 3), {"0028,0008": 5},
     PixelGeometry(5, 4, 3, 1, GeometryEvidence.DECLARED)),
    ((5, 4, 3), {"0028,0010": 4, "0028,0011": 3},
     PixelGeometry(5, 4, 3, 1, GeometryEvidence.MATCHED)),
    ((5, 4, 3), {"0028,0010": 5, "0028,0011": 4},
     PixelGeometry(1, 5, 4, 3, GeometryEvidence.MATCHED)),
    # Both arms match Rows/Columns, so step 3 cannot discriminate either.
    ((4, 4, 4), {"0028,0010": 4, "0028,0011": 4},
     PixelGeometry(1, 4, 4, 4, GeometryEvidence.GUESSED)),
    # ---- Step 3 with only ONE of Rows/Columns declared. -------------------
    # Every row above declares both, which leaves the four `x_d is None`
    # disjuncts and the guard's `or` unexercised: a mutation run killed
    # 42/50 in this module and five of the eight survivors were here. With
    # both declared, an `is None` that becomes `is not None` is masked by
    # the `==` beside it, and a guard narrowed from `or` to `and` still
    # passes. Declaring one at a time is what separates them, and it is
    # also the ordinary shape of a real graph -- Rows without Columns is
    # what a partially populated instance looks like.
    ((5, 4, 3), {"0028,0010": 5},
     PixelGeometry(1, 5, 4, 3, GeometryEvidence.MATCHED)),
    ((5, 4, 3), {"0028,0010": 4},
     PixelGeometry(5, 4, 3, 1, GeometryEvidence.MATCHED)),
    ((5, 4, 3), {"0028,0011": 4},
     PixelGeometry(1, 5, 4, 3, GeometryEvidence.MATCHED)),
    ((5, 4, 3), {"0028,0011": 3},
     PixelGeometry(5, 4, 3, 1, GeometryEvidence.MATCHED)),
    # Step 4's arm-A fallback: both arms admissible (s_d == 1 == shape[2]),
    # nothing to discriminate, and a last axis outside {3, 4} so the guess
    # goes to A rather than B. Nothing reached this `return` before.
    ((4, 4, 1), {"0028,0002": 1},
     PixelGeometry(4, 4, 1, 1, GeometryEvidence.GUESSED)),
])
def test_rank3_tiebreaks(shape, attrs, expected):
    assert resolve_pixel_geometry(shape, attrs) == expected


def test_rank3_declared_frames_matching_neither_arm_falls_through():
    """A NumberOfFrames that matches neither arm is not a contradiction."""
    geom = resolve_pixel_geometry((5, 4, 3), {"0028,0008": 2})
    assert geom.evidence is GeometryEvidence.GUESSED


def test_rank3_string_number_of_frames_is_parsed():
    """A graph that went through the old buggy path holds '3', not 3.

    `set_pixel_data` used to write `str(frames)` for (0028,0008) while
    `ingest_worker` stores an int. A resolver comparing raw values would
    fail the frames check on exactly the instances the bug already touched.
    """
    geom = resolve_pixel_geometry((5, 4, 3), {"0028,0008": "5"})
    assert geom == PixelGeometry(5, 4, 3, 1, GeometryEvidence.DECLARED)


# ---------------------------------------------------------------------------
# The frames bound (§3.5)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape,declared_frames", [
    ((3, 8, 8), 5),
    ((7, 8, 8), 2),
])
def test_frames_always_comes_from_the_array_never_from_the_attribute(
        shape, declared_frames):
    """`PixelGeometry.frames` is never the declared NumberOfFrames.

    `analyze_pixels` iterates `range(geom.frames)` over the array's first
    axis; that is in bounds because of this, not by coincidence.
    """
    geom = resolve_pixel_geometry(
        shape, {"0028,0002": 1, "0028,0008": declared_frames})
    assert geom.frames == shape[0]


@pytest.mark.parametrize("shape,attrs", [
    ((3, 4, 4), {"0028,0002": 1, "0028,0008": 3}),
    ((3, 4, 4), {}),
    ((100, 200, 3), {}),
    ((2, 4, 4, 3), {"0028,0002": 3}),
    ((10, 10), {}),
    ((7,), {}),
])
def test_frames_never_exceeds_the_first_axis(shape, attrs):
    assert resolve_pixel_geometry(shape, attrs).frames <= shape[0]


# ---------------------------------------------------------------------------
# Rank 3 and 4, contradiction (§3.6)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shape,attrs", [
    ((5, 8, 4), {"0028,0002": 3}),
    ((3, 8, 8), {"0028,0002": 4}),
    ((2, 4, 4, 3), {"0028,0002": 1}),
])
def test_contradiction_raises(shape, attrs):
    with pytest.raises(ValueError) as exc:
        resolve_pixel_geometry(shape, attrs)
    msg = str(exc.value)
    assert str(shape) in msg
    assert "SamplesPerPixel" in msg


def test_contradiction_message_names_every_declared_descriptor():
    with pytest.raises(ValueError) as exc:
        resolve_pixel_geometry(
            (5, 8, 4),
            {"0028,0002": 3, "0028,0008": 5, "0028,0010": 8, "0028,0011": 4})
    msg = str(exc.value)
    for name in ("SamplesPerPixel", "NumberOfFrames", "Rows", "Columns"):
        assert name in msg


# ---------------------------------------------------------------------------
# Ranks 0, 1, 2, 4, >=5 (§5)
# ---------------------------------------------------------------------------

def test_rank1_reshapes_from_declared_descriptors():
    geom = resolve_pixel_geometry(
        (16,), {"0028,0010": 4, "0028,0011": 4, "0028,0002": 1, "0028,0008": 1})
    assert geom == PixelGeometry(1, 4, 4, 1, GeometryEvidence.DECLARED)


def test_rank1_tolerates_dicom_padding():
    """A buffer larger than the declared size is padded, not wrong."""
    geom = resolve_pixel_geometry((20,), {"0028,0010": 4, "0028,0011": 4})
    assert geom == PixelGeometry(1, 4, 4, 1, GeometryEvidence.DECLARED)


def test_rank1_falls_through_when_nothing_is_declared():
    geom = resolve_pixel_geometry((7,), {})
    assert geom == PixelGeometry(1, 1, 7, 1, GeometryEvidence.STRUCTURAL)


def test_rank1_falls_through_when_the_buffer_is_too_small():
    geom = resolve_pixel_geometry((7,), {"0028,0010": 4, "0028,0011": 4})
    assert geom == PixelGeometry(1, 1, 7, 1, GeometryEvidence.STRUCTURAL)


def test_rank2_is_unambiguous():
    assert resolve_pixel_geometry((10, 10), {}) == PixelGeometry(
        1, 10, 10, 1, GeometryEvidence.STRUCTURAL)


def test_rank2_overwrites_a_declared_samples_per_pixel():
    """No ambiguity exists at rank 2, so this is magnitude, not layout."""
    assert resolve_pixel_geometry((10, 10), {"0028,0002": 3}) == PixelGeometry(
        1, 10, 10, 1, GeometryEvidence.STRUCTURAL)


@pytest.mark.parametrize("shape,attrs,expected", [
    ((2, 4, 4, 3), {"0028,0002": 3, "0028,0008": 2},
     PixelGeometry(2, 4, 4, 3, GeometryEvidence.DECLARED)),
    ((1, 4, 4, 3), {"0028,0002": 3},
     PixelGeometry(1, 4, 4, 3, GeometryEvidence.DECLARED)),
    ((2, 4, 4, 3), {},
     PixelGeometry(2, 4, 4, 3, GeometryEvidence.STRUCTURAL)),
])
def test_rank4_is_unambiguous(shape, attrs, expected):
    assert resolve_pixel_geometry(shape, attrs) == expected


@pytest.mark.parametrize("shape", [(), (2, 3, 4, 5, 6)])
def test_unsupported_rank_raises(shape):
    with pytest.raises(ValueError):
        resolve_pixel_geometry(shape, {})


# ---------------------------------------------------------------------------
# NumberOfFrames = 1 (§5)
# ---------------------------------------------------------------------------

def test_declared_single_frame_is_not_the_same_as_an_absent_one():
    """A declared 0028,0008 = 1 participates in step 2 and survives."""
    assert declared_int({"0028,0008": 1}, "0028,0008") == 1
    assert declared_int({}, "0028,0008") is None
    # (5,4,3) with NoF=1: only arm B can be a single frame.
    geom = resolve_pixel_geometry((5, 4, 3), {"0028,0008": 1})
    assert geom == PixelGeometry(1, 5, 4, 3, GeometryEvidence.DECLARED)


# ---------------------------------------------------------------------------
# PhotometricInterpretation (§3.8)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attrs,samples,expected", [
    # Absent -> the neutral default for the sample count.
    ({}, 3, "RGB"),
    ({}, 1, "MONOCHROME2"),
    ({"0028,0004": ""}, 1, "MONOCHROME2"),
    # An outright contradiction is corrected, and only to the neutral value.
    ({"0028,0004": "MONOCHROME2"}, 3, "RGB"),
    ({"0028,0004": "MONOCHROME1"}, 3, "RGB"),
    ({"0028,0004": "PALETTE COLOR"}, 3, "RGB"),
    ({"0028,0004": "RGB"}, 1, "MONOCHROME2"),
    # Everything coherent is left alone. This is the YBR relabelling fix.
    ({"0028,0004": "YBR_FULL"}, 3, None),
    ({"0028,0004": "YBR_FULL_422"}, 3, None),
    ({"0028,0004": "YBR_ICT"}, 3, None),
    ({"0028,0004": "RGB"}, 3, None),
    ({"0028,0004": "MONOCHROME1"}, 1, None),
    ({"0028,0004": "MONOCHROME2"}, 1, None),
    # PALETTE COLOR is SamplesPerPixel = 1, so it belongs in the mono set.
    ({"0028,0004": "PALETTE COLOR"}, 1, None),
    # Whitespace and casing are not a disagreement.
    ({"0028,0004": " ybr_full "}, 3, None),
])
def test_resolve_photometric_interpretation(attrs, samples, expected):
    assert resolve_photometric_interpretation(attrs, samples) == expected


# ---------------------------------------------------------------------------
# PlanarConfiguration (§3.9)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("attrs,samples,expected", [
    ({}, 3, True),
    ({}, 1, False),
    ({"0028,0006": 1}, 3, False),   # never overwrite a declared value
    ({"0028,0006": 0}, 3, False),
])
def test_planar_configuration_default(attrs, samples, expected):
    assert planar_configuration_default(attrs, samples) is expected


# ---------------------------------------------------------------------------
# Import hygiene (§3.2)
# ---------------------------------------------------------------------------

def test_module_imports_nothing_heavy():
    """The resolver must stay importable in a bare child interpreter.

    `_export_instance_worker` runs in a separate process, and the module
    must not drag in numpy, entities or io_handlers -- the last two would
    be an import cycle, and none of them belong in a pure function over a
    shape tuple and a dict.
    """
    import ast
    import pathlib

    import isocenter.pixel_geometry as pg

    tree = ast.parse(pathlib.Path(pg.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])

    assert "numpy" not in imported
    assert "pydicom" not in imported
    assert "entities" not in imported
    assert "io_handlers" not in imported


def test_planar_configuration_default_survives_attributes_that_cannot_be_searched():
    """The `except` arm of `planar_configuration_default` had no caller.

    A mutation run left `return True` here alive: nothing passed an
    `attributes` that raises on `in`. The branch exists so a odd mapping
    proxy cannot make an export crash on a descriptor question, and the
    answer is deliberately True -- write the neutral interleaved default
    rather than leave a colour instance with no PlanarConfiguration at
    all, which is the value pydicom then demands and cannot find.
    """
    class Hostile:
        def __contains__(self, key):
            raise RuntimeError("not searchable")

    assert planar_configuration_default(Hostile(), 3) is True
    # Still short-circuits on sample count before it ever looks.
    assert planar_configuration_default(Hostile(), 1) is False
