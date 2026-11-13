"""Layout of the three independent SQLite files and small shared helpers."""
from __future__ import annotations

import os


PARTICIPANTS = ("p1", "p2")


def layout(base: str) -> dict[str, str]:
    """Return the canonical file paths for a demo workspace.

    ``base`` is normally ``.runtime/demo``.  There is deliberately no single
    combined database: the coordinator and each participant have their own
    file, and no ATTACH is ever used.
    """
    base = os.path.abspath(base)
    return {
        "base": base,
        "coordinator": os.path.join(base, "coordinator.sqlite3"),
        "p1": os.path.join(base, "participant-p1.sqlite3"),
        "p2": os.path.join(base, "participant-p2.sqlite3"),
        "request": os.path.join(base, "request.json"),
        "traces": os.path.join(base, "traces"),
    }


def participant_paths(lay: dict[str, str]) -> dict[str, str]:
    return {"p1": lay["p1"], "p2": lay["p2"]}
