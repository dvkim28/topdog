"""Positional visibility scoring (S_position) for a single observed tile.

    Score = SectionWeight * (1 / row) ** 0.7 * (1 / column) ** 0.5      -- grid form
    Score = SectionWeight * 100 / sqrt(index + 1)                       -- sequence-decay form

The grid form needs real row/column coordinates, which only some sniffed JSON
payloads carry (e.g. a lobby API that groups tiles into rows). Most tiles only
give us a flat position within their section, so the sequence-decay form is
the default: it rewards being early in a section without requiring layout
metadata the source doesn't expose.
"""

from __future__ import annotations

from index.models import Placement

# Section weights per the MVI spec. Existing Placement values line up as:
# HERO = Hero Carousel, GRID = Featured Grid, OTHER = Main Lobby, LIVE_SECTION = Live Casino.
SECTION_WEIGHT = {
    Placement.HERO: 2.5,
    Placement.GRID: 1.8,
    Placement.OTHER: 1.0,
    Placement.LIVE_SECTION: 1.0,
}


def section_weight(placement: str) -> float:
    return SECTION_WEIGHT.get(Placement(placement), 1.0)


def positional_score(
    placement: str,
    position: int,
    row: int | None = None,
    column: int | None = None,
) -> float:
    """Score one tile. `position` is 0-based index within its section.

    When `row`/`column` are known (1-based, as sniffed JSON layouts usually
    give them), the grid form is used; otherwise it falls back to sequence
    decay on `position`.
    """
    weight = section_weight(placement)
    if row and column and row > 0 and column > 0:
        return round(weight * (1 / row) ** 0.7 * (1 / column) ** 0.5, 4)
    return round(weight * 100 / (position + 1) ** 0.5, 4)


def score_tiles(tiles: list) -> list[float]:
    """Score a list of Tile-like objects (`.placement`, `.position`, and
    optional `.row`/`.column`), in the same order they were passed in.
    """
    return [
        positional_score(
            t.placement, t.position, getattr(t, "row", None), getattr(t, "column", None)
        )
        for t in tiles
    ]
