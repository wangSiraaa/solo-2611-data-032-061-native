#!/usr/bin/env python3
"""End-to-end demonstration/verification on the bundled fixtures.

Run from the repository root:

    python3 scripts/run_fixtures.py [--out DIR]

It performs plan -> build -> verify, then independently re-checks:

* every output WAV's bytes against the source WAV sample range, sliced by raw
  byte offset (no reliance on the producer's seeking code);
* every annotation/clip intersection against the local annotations;
* that no two output clips overlap and no source sample is assigned twice;
* that continuation parts exactly tile the split annotation;
* that rejected lines are retained with diagnostics.
"""

import argparse
import json
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clipgen.cli import main as cli_main


def _fail(message):
    print(f"CHECK FAILED: {message}")
    raise SystemExit(1)


def _source_slice(recording_path, start, end, frame_size):
    data = recording_path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        _fail(f"{recording_path} is not RIFF/WAVE")
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"data":
            return data[body + start * frame_size:body + end * frame_size]
        pos = body + size + (size & 1)
    _fail(f"no data chunk in {recording_path}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / ".runtime" / "fixture-run")
    parser.add_argument("--records", type=Path, default=ROOT / "fixtures")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--annotations", type=Path, default=None)
    args = parser.parse_args(argv)

    records = args.records
    annotations = args.annotations or records / "annotations.jsonl"
    config = args.config or records / "config.json"
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    plan_path = out / "plan.json"
    rejected_path = out / "rejected.jsonl"
    build_dir = out / "build"

    print("== plan ==")
    code = cli_main(
        [
            "plan",
            "--annotations", str(annotations),
            "--config", str(config),
            "--recordings", str(records),
            "--plan", str(plan_path),
            "--rejected", str(rejected_path),
        ]
    )
    if code != 0:
        _fail("plan command failed")

    print("== build ==")
    code = cli_main(
        ["build", "--plan", str(plan_path), "--recordings", str(records), "--out", str(build_dir)]
    )
    if code != 0:
        _fail("build command failed")

    print("== verify ==")
    code = cli_main(
        ["verify", "--plan", str(plan_path), "--recordings", str(records), "--out", str(build_dir)]
    )
    if code != 0:
        _fail("verify command failed")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    manifest = json.loads((build_dir / "manifest.json").read_text(encoding="utf-8"))
    local = [
        json.loads(line)
        for line in (build_dir / "local_annotations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    print("\n== independent checks ==")

    # 1. Byte-for-byte source correspondence, sliced by raw file offset.
    total_bytes = 0
    for entry in manifest["clips"]:
        recording_path = records / entry["recording"]
        with wave.open(str(recording_path), "rb") as source:
            frame_size = source.getsampwidth() * source.getnchannels()
        expected = _source_slice(
            recording_path, entry["start_sample"], entry["end_sample"], frame_size
        )
        with open(build_dir / entry["wav_file"], "rb") as handle:
            payload = handle.read()
        # Skip the 44-byte canonical header the producer writes.
        audio = payload[44:]
        if audio != expected:
            _fail(f"{entry['clip_id']} audio does not match its source range")
        if len(audio) != entry["frame_count"] * frame_size:
            _fail(f"{entry['clip_id']} byte length is wrong")
        total_bytes += len(audio)

        with wave.open(str(build_dir / entry["wav_file"]), "rb") as clip:
            if (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) != (
                entry["nchannels"], entry["sample_width"], entry["sample_rate"]
            ):
                _fail(f"{entry['clip_id']} format was not preserved")

    # 2. No output overlap and no duplicated source samples.
    by_recording = {}
    for entry in manifest["clips"]:
        by_recording.setdefault(entry["recording"], []).append(entry)
    for name, entries in by_recording.items():
        entries.sort(key=lambda e: e["start_sample"])
        cursor = -1
        for entry in entries:
            if entry["start_sample"] < cursor:
                _fail(f"clips overlap in {name}: {entry['clip_id']}")
            cursor = entry["end_sample"]

    # 3. Every annotation/clip intersection appears exactly once, with the
    #    expected local coordinates, computed independently from plan data.
    expected = {}
    for annotation in plan["annotations"]:
        covered = []
        for entry in sorted(manifest["clips"], key=lambda e: e["start_sample"]):
            if entry["recording"] != annotation["recording"]:
                continue
            start = max(annotation["start_sample"], entry["start_sample"])
            end = min(annotation["end_sample"], entry["end_sample"])
            if start < end:
                covered.append((entry, start, end))
        if not covered:
            _fail(f"annotation {annotation['annotation_id']} has no coverage")
        if covered[0][1] != annotation["start_sample"]:
            _fail(f"annotation {annotation['annotation_id']} start is uncovered")
        if covered[-1][2] != annotation["end_sample"]:
            _fail(f"annotation {annotation['annotation_id']} end is uncovered")
        for index, (entry, start, end) in enumerate(covered, start=1):
            key = (entry["clip_id"], annotation["annotation_id"])
            expected[key] = {
                "start_sample": start - entry["start_sample"],
                "end_sample": end - entry["start_sample"],
                "source_start_sample": start,
                "source_end_sample": end,
                "part_index": index,
                "part_count": len(covered),
                "continuation": index > 1,
                "continues_in_next": index < len(covered),
                "speaker": annotation["speaker"],
            }

    actual = {(row["clip_id"], row["annotation_id"]): row for row in local}
    if set(actual) != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        _fail(f"local annotation set differs; missing={missing} extra={extra}")
    for key, want in expected.items():
        have = actual[key]
        for field, value in want.items():
            if have[field] != value:
                _fail(f"local annotation {key} field {field}: {have[field]} != {value}")

    # 4. Continuations tile a split annotation contiguously.
    split = [
        item for item in plan["continuations"] if item["part_count"] > 1
    ]
    for item in split:
        rows = sorted(
            (row for row in local if row["annotation_id"] == item["annotation_id"]),
            key=lambda row: row["part_index"],
        )
        if [row["part_index"] for row in rows] != list(range(1, len(rows) + 1)):
            _fail(f"continuation parts are not ordered for {item['annotation_id']}")
        if rows[0]["source_start_sample"] != item["source_samples"][0]:
            _fail(f"continuation start mismatch for {item['annotation_id']}")
        if rows[-1]["source_end_sample"] != item["source_samples"][1]:
            _fail(f"continuation end mismatch for {item['annotation_id']}")
        for previous, current in zip(rows, rows[1:]):
            if previous["source_end_sample"] != current["source_start_sample"]:
                _fail(f"continuation gap in {item['annotation_id']}")
            if not previous["continues_in_next"] or not current["continuation"]:
                _fail(f"continuation flags are wrong in {item['annotation_id']}")

    # 5. Rejected lines are retained and explained.
    rejected = [
        json.loads(line)
        for line in rejected_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    raw_lines = annotations.read_text(encoding="utf-8").splitlines()
    for item in rejected:
        if item["raw"] not in raw_lines:
            _fail(f"rejected line {item['line']} raw text is not preserved")
        if not item.get("code") or not item.get("reason"):
            _fail(f"rejected line {item['line']} lacks a diagnosis")

    print(
        json.dumps(
            {
                "clips": len(manifest["clips"]),
                "annotations": len(plan["annotations"]),
                "local_annotations": len(local),
                "bytes_checked": total_bytes,
                "split_annotations": [item["annotation_id"] for item in split],
                "rejected": [
                    {"line": item["line"], "code": item["code"], "annotation_id": item.get("annotation_id")}
                    for item in rejected
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("\nALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
