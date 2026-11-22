"""`_write_str_if_changed` compares labels by the one Code String spelling (#1084).

It compared `str(raw).strip().upper()` with the same of the new value: a
second spelling of a declared Photometric Interpretation beside
`pixel_geometry._cs_spelling`, which upper-cases `a`-`z` only (#612,
#646). Python's `upper()` folds `ß` to `SS`, so a held `rgß` compared
equal to a written `RGSS`, which `_cs_spelling` reads as two labels. No
exported byte is known to have moved; the comparison now reads the one
spelling. A `bytes` label held compares unequal to any text on purpose:
it is written as held (owner ruling of 2026-10-11), and a relabel after
the decoder converted the samples is a new label, not a respelling.

Names `isocenter.entities`.
"""
import pytest

from isocenter import entities
from isocenter.entities import Instance


def _write(held, value):
    inst = Instance("1.2.3.4", "1.2.840.10008.5.1.4.1.1.7", 1)
    inst.attributes["0028,0004"] = held
    before = inst._revision  # pylint: disable=protected-access
    with entities.PIXEL_STATE_LOCK:
        wrote = inst._write_str_if_changed("0028,0004", value)  # pylint: disable=protected-access
    moved = inst._revision != before  # pylint: disable=protected-access
    return wrote, moved, inst.attributes["0028,0004"]


@pytest.mark.parametrize("held", [" rgb", "RGB ", "rgb", "RGB"])
def test_a_respelling_of_the_same_label_is_not_a_write(held):
    assert _write(held, "RGB") == (False, False, held)


def test_a_bytes_label_held_is_replaced():
    assert _write(b"RGB", "RGB") == (True, True, "RGB")


def test_two_labels_the_spelling_tells_apart_are_two_labels():
    """Red under `str.upper()`, which folds `ß` to `SS`."""
    wrote, moved, now = _write("rgß", "RGSS")
    assert (wrote, moved, now) == (True, True, "RGSS")


def test_an_absent_label_is_written():
    inst = Instance("1.2.3.4", "1.2.840.10008.5.1.4.1.1.7", 1)
    with entities.PIXEL_STATE_LOCK:
        assert inst._write_str_if_changed("0028,0004", "RGB") is True  # pylint: disable=protected-access
    assert inst.attributes["0028,0004"] == "RGB"
