"""Write every small example to ``examples/data/<name>/`` for manual inspection.

    .venv/bin/python examples/build_examples.py

Each case gets plan.json, parcels.geojson and zones.geojson only; run the
tool on them with::

    python -m energyalloc allocate \
        --plan examples/data/<name>/plan.json \
        --parcels examples/data/<name>/parcels.geojson \
        --zones examples/data/<name>/zones.geojson \
        --out examples/out/<name>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from cases import CASES, DEFAULT_PLAN  # noqa: E402


def feature_collection(features):
    return {"type": "FeatureCollection", "features": features}


def main() -> int:
    root = HERE / "data"
    for case in CASES:
        name = case["name"]
        out = root / name
        out.mkdir(parents=True, exist_ok=True)
        (out / "plan.json").write_text(
            json.dumps(case.get("plan", DEFAULT_PLAN), indent=2), encoding="utf-8"
        )
        (out / "parcels.geojson").write_text(
            json.dumps(feature_collection(case["parcels"]), indent=2),
            encoding="utf-8",
        )
        (out / "zones.geojson").write_text(
            json.dumps(feature_collection(case["zones"]), indent=2),
            encoding="utf-8",
        )
        docs = [f"# {name}", "", case["description"], ""]
        docs.append("Expected fragments (hand-computed):")
        docs.append("")
        docs.append("| owner | area_m2 | wh | tiny |")
        docs.append("| --- | --- | --- | --- |")
        for frag in case["expected"]["fragments"]:
            docs.append(
                "| {owner} | {area} | {wh} | {tiny} |".format(
                    owner=frag["owner_id"],
                    area=frag["area_m2"],
                    wh=frag["wh"],
                    tiny=frag.get("tiny", False),
                )
            )
        docs.append("")
        (out / "README.md").write_text("\n".join(docs), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
