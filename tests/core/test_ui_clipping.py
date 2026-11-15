"""scripts/ui_screenshots.py: the clipped-text check's pure parts, on synthetic masks.

The GUI run checks itself against a calibration panel; these pin the rules
the detector uses to tell Blender's ellipsis from other small marks.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("ui_screenshots", ROOT / "scripts/ui_screenshots.py")
shots = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shots)

EM = 11.0  # the UI font at scale 1


def canvas() -> np.ndarray:
    return np.zeros((40, 80), bool)


def dot(mask: np.ndarray, x: int, y: int, size: int = 2) -> None:
    mask[y : y + size, x : x + size] = True


def ellipsis(mask: np.ndarray, x: int, y: int, step: int = 3) -> None:
    for k in range(3):
        dot(mask, x + k * step, y)


def test_finds_an_ellipsis_between_letters() -> None:
    mask = canvas()
    mask[12:22, 10:12] = True  # a letter before
    ellipsis(mask, 14, 20)
    mask[14:22, 25:27] = True  # and after
    assert shots.find_ellipses(mask, EM) == [(14, 20, 21, 21)]


def test_finds_each_ellipsis_once() -> None:
    mask = canvas()
    ellipsis(mask, 5, 10, step=4)
    ellipsis(mask, 40, 30)
    assert len(shots.find_ellipses(mask, EM)) == 2


@pytest.mark.parametrize(
    "draw",
    [
        pytest.param(lambda m: [dot(m, 10, 20), dot(m, 13, 20)], id="two dots"),
        pytest.param(lambda m: [dot(m, 10, 20), dot(m, 13, 20), dot(m, 18, 20)], id="uneven"),
        pytest.param(lambda m: [dot(m, 10, 20), dot(m, 13, 20), dot(m, 16, 23)], id="not level"),
        pytest.param(lambda m: [dot(m, 10 + 8 * k, 20) for k in range(3)], id="too far apart"),
        pytest.param(lambda m: [dot(m, 10 + 3 * k, 20, size=4) for k in range(3)], id="too big"),
        # Colons: a dot above each.
        pytest.param(lambda m: [ellipsis(m, 10, 20), ellipsis(m, 10, 15)], id="colons"),
        # The dots of "iii": a stem below each.
        pytest.param(
            lambda m: [
                ellipsis(m, 10, 12),
                *(m.__setitem__((slice(16, 22), x), True) for x in (10, 13, 16)),
            ],
            id="iii",
        ),
    ],
)
def test_ignores_other_marks(draw) -> None:
    mask = canvas()
    draw(mask)
    assert shots.find_ellipses(mask, EM) == []


def test_the_rules_scale_with_the_font() -> None:
    mask = np.zeros((80, 160), bool)
    for k in range(3):
        dot(mask, 20 + 6 * k, 40, size=4)
    assert len(shots.find_ellipses(mask, 2 * EM)) == 1
    assert shots.find_ellipses(mask, EM) == []  # too big at scale 1


def test_text_mask_is_the_magenta_text() -> None:
    grey, blue = (0.26, 0.26, 0.26), (0.28, 0.45, 0.70)
    pixels = np.array(
        [
            [(1.0, 0.0, 1.0), grey, blue],
            [
                np.add(grey, 0.6 * np.subtract((1.0, 0.0, 1.0), grey)),
                (0.9, 0.9, 0.9),
                (1, 0.3, 0.3),
            ],
        ]
    )
    assert shots.text_mask(pixels).tolist() == [[True, False, False], [True, False, False]]


# Advances of the UI font at 11 px (Inter, measured with blf in Blender 5.2).
ADVANCE = {".": 3, "'": 3, "!": 3, "…": 10, "m": 10, "M": 10}


def width(text: str) -> float:
    return float(sum(ADVANCE.get(c, 6) for c in text))


@pytest.mark.parametrize(
    ("text", "neutral"),
    [
        ("Save As...", "Save As!!!"),
        ("Clipped at the end…", "Clipped at the endm"),
        ("... and 3 more", "!!! and 3 more"),
        ("No ellipsis. Two dots..", "No ellipsis. Two dots.."),
    ],
)
def test_neutral_text_keeps_the_width(text: str, neutral: str) -> None:
    assert shots.neutral_text(text, width) == neutral
    assert width(neutral) == width(text)


def test_neutral_text_refuses_a_text_it_cannot_match() -> None:
    def odd(text: str) -> float:
        return width(text) + 0.5 * text.count("…")

    with pytest.raises(ValueError, match="stand-in"):
        shots.neutral_text("Too long…", odd)


def test_skips_dots_cut_off_by_the_edge() -> None:
    mask = canvas()
    ellipsis(mask, 10, 38)  # the bottom row: the rows below are not in the image
    ellipsis(mask, 40, 1)  # the top row
    assert shots.find_ellipses(mask, EM) == []
