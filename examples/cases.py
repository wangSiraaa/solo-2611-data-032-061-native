"""Small worked examples with hand-computable rectangular areas.

Each case is plain data so it can be written to disk by
``examples/build_examples.py`` and asserted against in the test suite.

Areas below are exact decimal numbers: all rectangles use integer or
hundredth-metre coordinates, so a manual calculation is possible.
"""

from __future__ import annotations

from typing import Any

DEFAULT_PLAN: dict[str, Any] = {
    "crs": "EPSG:3857",
    "coordinate_unit": "metre",
    "priority_rule": "higher numeric priority wins, then ascending zone_id",
    "precision_grid": 0.01,
    "area_tolerance_m2": 1e-05,
    "unassigned_id": "UNASSIGNED",
    "wh_remainder_tie_break": "ascending zone_id then canonical fragment geometry",
}


def rect(x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    """A closed CCW rectangular polygon."""
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [x0, y0],
                [x1, y0],
                [x1, y1],
                [x0, y1],
                [x0, y0],
            ]
        ],
    }


def polygon(
    rings: list[list[list[float]]],
) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": rings}


def parcel(
    parcel_id: str, energy_wh: int | float, geometry: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"parcel_id": parcel_id, "energy_wh": energy_wh},
        "geometry": geometry,
    }


def zone(
    zone_id: str, priority: int, geometry: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"zone_id": zone_id, "priority": priority},
        "geometry": geometry,
    }


CASES: list[dict[str, Any]] = [
    {
        "name": "01_overlap",
        "description": (
            "Two overlapping zones. A (priority 10) covers x=0..6, B "
            "(priority 5) covers x=4..10 in a 10x10 parcel. B's raw 60 m2 "
            "includes the 20 m2 already won by A, so B nets 40 m2. "
            "Manual: A=60 m2, B=40 m2, unassigned=0."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("P", 100, rect(0, 0, 10, 10))],
        "zones": [
            zone("A", 10, rect(0, 0, 6, 10)),
            zone("B", 5, rect(4, 0, 10, 10)),
        ],
        "expected": {
            "accepted": ["P"],
            "fragments": [
                {"owner_id": "A", "area_m2": 60.0, "wh": 60},
                {"owner_id": "B", "area_m2": 40.0, "wh": 40},
            ],
            "unassigned_area_m2": 0.0,
            "energy_wh": 100,
        },
    },
    {
        "name": "02_equal_priority",
        "description": (
            "X and Y share priority 10 and both cover x=4..6. The tie is "
            "resolved by ascending zone_id: X adjudicates before Y. "
            "Z (priority 5) lies entirely inside X and is fully preempted "
            "but kept as evidence. Manual: X=60, Y=40, Z=0."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("P", 100, rect(0, 0, 10, 10))],
        "zones": [
            zone("Y", 10, rect(4, 0, 10, 10)),
            zone("X", 10, rect(0, 0, 6, 10)),
            zone("Z", 5, rect(0, 0, 3, 10)),
        ],
        "expected": {
            "accepted": ["P"],
            "fragments": [
                {"owner_id": "X", "area_m2": 60.0, "wh": 60},
                {"owner_id": "Y", "area_m2": 40.0, "wh": 40},
            ],
            "preempted": {"Z": 30.0},
            "unassigned_area_m2": 0.0,
            "energy_wh": 100,
        },
    },
    {
        "name": "03_hole",
        "description": (
            "Parcel 20x20 with a 10x10 hole has net area 300 m2 and energy "
            "301 Wh. B (priority 20) takes the x=0..4 strip (80 m2). A "
            "(priority 10) covers the rest and keeps the hole: 220 m2. "
            "Manual: B=80, A=220, hole preserved in A. Remainder: exact "
            "shares are 80.27 and 220.73, so A receives the extra Wh."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [
            parcel(
                "H",
                301,
                polygon(
                    [
                        rect(0, 0, 20, 20)["coordinates"][0],
                        rect(5, 5, 15, 15)["coordinates"][0],
                    ]
                ),
            )
        ],
        "zones": [
            zone("A", 10, rect(-1, -1, 21, 21)),
            zone("B", 20, rect(0, 0, 4, 20)),
        ],
        "expected": {
            "accepted": ["H"],
            "fragments": [
                {"owner_id": "A", "area_m2": 220.0, "wh": 221},
                {"owner_id": "B", "area_m2": 80.0, "wh": 80},
            ],
            "interior_rings": {"A": 1},
            "unassigned_area_m2": 0.0,
            "energy_wh": 301,
        },
    },
    {
        "name": "04_unassigned",
        "description": (
            "A 10x10 parcel is covered only on x=0..7 by adjacent zones A "
            "(30 m2) and B (40 m2). The x=7..10 strip (30 m2) is outside "
            "every zone and becomes UNASSIGNED. Manual: A=30, B=40, "
            "UNASSIGNED=30. Energy 77: exact 23.1/30.8/23.1; B has the "
            "largest remainder and receives the extra Wh."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("U", 77, rect(0, 0, 10, 10))],
        "zones": [
            zone("A", 10, rect(0, 0, 3, 10)),
            zone("B", 10, rect(3, 0, 7, 10)),
        ],
        "expected": {
            "accepted": ["U"],
            "fragments": [
                {"owner_id": "A", "area_m2": 30.0, "wh": 23},
                {"owner_id": "B", "area_m2": 40.0, "wh": 31},
                {"owner_id": "UNASSIGNED", "area_m2": 30.0, "wh": 23},
            ],
            "unassigned_area_m2": 30.0,
            "energy_wh": 77,
        },
    },
    {
        "name": "05_remainder_tie",
        "description": (
            "A 3x1 parcel split into three equal 1 m2 thirds with energy "
            "1 Wh. Every exact share is 1/3, so all fractional remainders "
            "tie. The plan orders ties by ascending zone_id: A receives the "
            "single Wh, B and C receive 0. Manual: 1/3 + 1/3 + 1/3 = 1."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("T", 1, rect(0, 0, 3, 1))],
        "zones": [
            zone("C", 10, rect(2, 0, 3, 1)),
            zone("A", 10, rect(0, 0, 1, 1)),
            zone("B", 10, rect(1, 0, 2, 1)),
        ],
        "expected": {
            "accepted": ["T"],
            "fragments": [
                {"owner_id": "A", "area_m2": 1.0, "wh": 1},
                {"owner_id": "B", "area_m2": 1.0, "wh": 0},
                {"owner_id": "C", "area_m2": 1.0, "wh": 0},
            ],
            "unassigned_area_m2": 0.0,
            "energy_wh": 1,
        },
    },
    {
        "name": "06_geometry_tie",
        "description": (
            "One MultiPolygon parcel of two equal 1 m2 squares, both covered "
            "by the same zone. Exact 0.5/0.5 shares tie; the tie is broken "
            "by canonical fragment geometry, so the result is deterministic "
            "regardless of ring/input order. Manual: 0.5+0.5 = 1 Wh."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [
            parcel(
                "M",
                1,
                {
                    "type": "MultiPolygon",
                    "coordinates": [
                        rect(0, 0, 1, 1)["coordinates"],
                        rect(2, 0, 3, 1)["coordinates"],
                    ],
                },
            )
        ],
        "zones": [zone("Z", 10, rect(-1, -1, 4, 2))],
        "expected": {
            "accepted": ["M"],
            "fragments": [
                {"owner_id": "Z", "area_m2": 1.0, "wh": 0},
                {"owner_id": "Z", "area_m2": 1.0, "wh": 1},
            ],
            "unassigned_area_m2": 0.0,
            "energy_wh": 1,
        },
    },
    {
        "name": "07_tiny_fragment",
        "description": (
            "B (priority 20) clips a 0.01 x 0.01 m = 0.0001 m2 square from "
            "a 100 m2 parcel, exactly one precision-grid cell. The sliver is "
            "kept and flagged tiny_fragment instead of being dropped. With "
            "1,000,000 Wh, its exact share is 1 Wh. Manual: B=0.0001 m2 / "
            "1 Wh, A=99.9999 m2 / 999,999 Wh."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("TNY", 1_000_000, rect(0, 0, 10, 10))],
        "zones": [
            zone("A", 10, rect(0, 0, 10, 10)),
            zone("B", 20, rect(1, 1, 1.01, 1.01)),
        ],
        "expected": {
            "accepted": ["TNY"],
            "fragments": [
                {"owner_id": "A", "area_m2": 99.9999, "wh": 999_999},
                {"owner_id": "B", "area_m2": 0.0001, "wh": 1, "tiny": True},
            ],
            "unassigned_area_m2": 0.0,
            "energy_wh": 1_000_000,
        },
    },
    {
        "name": "08_candidate_filter",
        "description": (
            "Only the first of three zones intersects the parcel; the other "
            "two are far away. The spatial filter should evaluate one "
            "candidate pair instead of three. Manual: A=50, UNASSIGNED=50."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [parcel("F", 10, rect(0, 0, 10, 10))],
        "zones": [
            zone("A", 10, rect(0, 0, 5, 10)),
            zone("FAR1", 10, rect(100, 100, 110, 110)),
            zone("FAR2", 10, rect(200, 200, 210, 210)),
        ],
        "expected": {
            "accepted": ["F"],
            "fragments": [
                {"owner_id": "A", "area_m2": 50.0, "wh": 5},
                {"owner_id": "UNASSIGNED", "area_m2": 50.0, "wh": 5},
            ],
            "unassigned_area_m2": 50.0,
            "energy_wh": 10,
            "candidate_pairs": 1,
            "zone_parcel_pairs_total": 3,
        },
    },
    {
        "name": "09_rejections",
        "description": (
            "One good parcel plus four rejected parcels: a self-intersecting "
            "bowtie, a negative energy, a fractional energy, and a "
            "LineString. Rejected parcels are reported separately and their "
            "energy is not moved to the good parcel. Manual: only G (10 Wh) "
            "is allocated."
        ),
        "plan": DEFAULT_PLAN,
        "parcels": [
            parcel("G", 10, rect(0, 0, 10, 10)),
            parcel(
                "BAD_INVALID",
                5,
                {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [20, 0],
                            [30, 10],
                            [30, 0],
                            [20, 10],
                            [20, 0],
                        ]
                    ],
                },
            ),
            parcel("BAD_NEGATIVE", -3, rect(40, 0, 50, 10)),
            parcel("BAD_FRACTION", 2.5, rect(60, 0, 70, 10)),
            {
                "type": "Feature",
                "properties": {"parcel_id": "BAD_LINE", "energy_wh": 1},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0, 20], [10, 20]],
                },
            },
        ],
        "zones": [zone("A", 10, rect(0, 0, 10, 10))],
        "expected": {
            "accepted": ["G"],
            "fragments": [{"owner_id": "A", "area_m2": 100.0, "wh": 10}],
            "unassigned_area_m2": 0.0,
            "energy_wh": 10,
            "rejected": {
                "BAD_INVALID": "invalid_geometry",
                "BAD_NEGATIVE": "negative_energy",
                "BAD_FRACTION": "invalid_energy",
                "BAD_LINE": "unsupported_geometry",
            },
        },
    },
]

CASES_BY_NAME = {case["name"]: case for case in CASES}
