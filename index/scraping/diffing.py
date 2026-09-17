"""Snapshot hashing and diffing for the cost-saving scrape short-circuit.

Comparison is always against `Brand.latest_snapshot` / `Brand.content_hash` -
the last successfully resolved state for that specific brand - never against
"yesterday". Manual checks can run several times in one day, or be skipped
for several days, so the only comparison point that's always meaningful is
"whatever we last saw for this brand".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


def hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_id(row: dict) -> str:
    """A resolved placement's identity for diffing: which game, in which
    section. Position is deliberately excluded from the identity - it's the
    thing being diffed (a "moved" row), not what makes two rows "the same
    game" across runs.
    """
    return f"{row['game_id']}:{row['placement']}"


@dataclass
class SnapshotDiff:
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    moved: list[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.moved)

    def as_dict(self) -> dict:
        return {"added": self.added, "removed": self.removed, "moved": self.moved}


def diff_snapshots(previous: list[dict], current: list[dict]) -> SnapshotDiff:
    """previous/current: Brand.latest_snapshot-shaped lists, i.e. the same
    row dicts tasks.py builds from resolve_tiles() output
    ({"game_id", "placement", "position", "raw_label", "position_score"}).

    A game moving from one placement to another (e.g. live_section -> grid)
    is reported as removed from the old section and added to the new one,
    not as a "move" - that's a real merchandising change, not a reposition.
    A "move" is only ever a position change within the same (game, placement).
    """
    prev_by_id = {_row_id(row): row for row in previous}
    curr_by_id = {_row_id(row): row for row in current}

    added = [row for key, row in curr_by_id.items() if key not in prev_by_id]
    removed = [row for key, row in prev_by_id.items() if key not in curr_by_id]
    moved = [
        {
            "game_id": curr["game_id"],
            "placement": curr["placement"],
            "old_position": prev_by_id[key]["position"],
            "new_position": curr["position"],
        }
        for key, curr in curr_by_id.items()
        if key in prev_by_id and prev_by_id[key]["position"] != curr["position"]
    ]
    return SnapshotDiff(added=added, removed=removed, moved=moved)
