"""Plan/build/verify orchestration and file output."""

import json
import os
import shutil
import tempfile
import wave
from pathlib import Path

from .annotations import load_annotations
from .audio import (
    AudioError,
    load_config,
    probe_recording,
    read_frames,
    write_pcm_wav,
)
from .planner import (
    PLAN_SCHEMA,
    PlanError,
    assert_plan_invariants,
    build_plan,
    plan_to_json,
)


class BuildError(RuntimeError):
    """A build could not be completed; outputs must not be trusted."""


def _annotation_dict(annotation):
    return {
        "recording": annotation.recording,
        "annotation_id": annotation.annotation_id,
        "speaker": annotation.speaker,
        "start_text": annotation.start_text,
        "end_text": annotation.end_text,
        "start_sample": annotation.start_sample,
        "end_sample": annotation.end_sample,
    }


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        # ``mkstemp`` creates 0600 files; make outputs readable by the team.
        os.chmod(path, 0o644)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _rejected_jsonl(rejected):
    lines = [json.dumps(item.as_json(), ensure_ascii=False, sort_keys=True) for item in rejected]
    return "\n".join(lines) + ("\n" if lines else "")


def create_plan(annotations_path, config_path, recordings_dir):
    """Validate inputs and build a plan.  Raises on any fatal input problem."""
    config = load_config(config_path)
    valid, rejected, recordings = load_annotations(annotations_path, recordings_dir)
    annotations = [_annotation_dict(annotation) for annotation in valid]
    plan = build_plan(config, annotations, recordings)

    plan["inputs"] = {
        "annotations": str(Path(annotations_path)),
        "config": str(Path(config_path)),
        "recordings_dir": str(Path(recordings_dir)),
    }
    # Rejection diagnostics live beside the plan, not inside it: the plan is
    # the order-independent product, while line numbers describe one input file.
    plan["rejected_diagnosis"] = _diagnosis_summary(rejected)
    plan["annotation_parts"] = _annotation_parts(plan)
    plan["continuations"] = _continuation_notes(plan)
    return plan, rejected


def _diagnosis_summary(rejected):
    counts = {}
    for item in rejected:
        counts[item.code] = counts.get(item.code, 0) + 1
    return {
        "count": len(rejected),
        "by_code": dict(sorted(counts.items())),
        "note": (
            "Rejected source lines are kept verbatim in the rejected JSONL. "
            "Accepted annotations are never silently trimmed; out-of-range or "
            "zero-length intervals are rejected instead."
        ),
    }


def _annotation_parts(plan):
    """Explain how each annotation was distributed over clips."""
    source = {
        (a["recording"], a["annotation_id"]): a for a in plan["annotations"]
    }
    parts = {}
    for clip in plan["clips"]:
        for record in clip["local_annotations"]:
            source_annotation = source[(clip["recording"], record["annotation_id"])]
            entry = parts.setdefault(
                record["annotation_id"],
                {
                    "annotation_id": record["annotation_id"],
                    "speaker": record["speaker"],
                    "recording": clip["recording"],
                    # Full source interval, not just the first part's share.
                    "source_start_sample": source_annotation["start_sample"],
                    "source_end_sample": source_annotation["end_sample"],
                    "parts": [],
                },
            )
            entry["parts"].append(
                {
                    "clip_id": clip["clip_id"],
                    "source_start_sample": record["source_start_sample"],
                    "source_end_sample": record["source_end_sample"],
                    "clip_start_sample": clip["start_sample"],
                    "clip_end_sample": clip["end_sample"],
                }
            )
    return dict(sorted(parts.items()))


def _continuation_notes(plan):
    notes = []
    for annotation_id, entry in plan["annotation_parts"].items():
        if len(entry["parts"]) <= 1:
            continue
        notes.append(
            {
                "annotation_id": annotation_id,
                "recording": entry["recording"],
                "speaker": entry["speaker"],
                "source_samples": [entry["source_start_sample"], entry["source_end_sample"]],
                "part_count": len(entry["parts"]),
                "reason": (
                    "annotation is longer than the maximum clip length and has no "
                    "natural boundary inside it, so it is cut at exact sample limits"
                ),
                "clip_ids": [part["clip_id"] for part in entry["parts"]],
            }
        )
    return notes


def write_plan_outputs(plan, rejected, plan_path, rejected_path):
    plan_text = plan_to_json(plan)
    rejected_text = _rejected_jsonl(rejected)
    # Both files are written atomically; the rejected file is harmless if the
    # plan write fails.  The plan is the authoritative success artefact.
    _write_atomic(rejected_path, rejected_text)
    _write_atomic(plan_path, plan_text)


def load_plan(plan_path):
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError(f"plan not found: {plan_path}") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"plan is not valid JSON: {exc}") from exc
    try:
        assert_plan_invariants(plan)
    except PlanError as exc:
        raise BuildError(f"plan is not valid: {exc}") from exc
    if plan.get("schema") != PLAN_SCHEMA:
        raise BuildError(f"plan schema must be {PLAN_SCHEMA!r}")
    return plan


def _recording_probes(plan, recordings_dir):
    probes = {}
    errors = []
    for name, expected in plan["recordings"].items():
        try:
            info = probe_recording(Path(recordings_dir) / name)
        except AudioError as exc:
            errors.append(str(exc))
            continue
        for field, actual in (
            ("nchannels", info.nchannels),
            ("sample_width", info.sample_width),
            ("sample_rate", info.rate),
            ("nframes", info.nframes),
        ):
            if actual != expected[field]:
                errors.append(
                    f"recording {name} differs from plan: {field} "
                    f"is {actual}, plan says {expected[field]}"
                )
        probes[name] = info
    if errors:
        raise BuildError("; ".join(errors))
    return probes


def build_outputs(plan, recordings_dir, out_dir):
    """Write WAVs, the manifest, and local annotations for a validated plan."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = out_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    local_path = out_dir / "local_annotations.jsonl"
    status_path = out_dir / "build-status.json"

    # Any previous successful manifest is invalidated before writing new data,
    # so a failure below cannot leave stale files posing as this build.
    if manifest_path.exists():
        manifest_path.unlink()
    _write_atomic(
        status_path,
        json.dumps({"status": "building", "plan_schema": plan.get("schema")}, indent=2) + "\n",
    )

    staging = Path(tempfile.mkdtemp(dir=out_dir, prefix=".build-"))
    try:
        probes = _recording_probes(plan, recordings_dir)
        written = []
        for clip in plan["clips"]:
            info = probes[clip["recording"]]
            if clip["end_sample"] > info.nframes:
                raise BuildError(
                    f"clip {clip['clip_id']} ends past the end of {clip['recording']}"
                )
            raw = read_frames(info, clip["start_sample"], clip["end_sample"])
            expected_bytes = (clip["end_sample"] - clip["start_sample"]) * info.frame_size
            if len(raw) != expected_bytes:
                raise BuildError(
                    f"clip {clip['clip_id']} byte length {len(raw)} != {expected_bytes}"
                )
            staging_path = staging / f"{clip['clip_id']}.wav"
            write_pcm_wav(
                staging_path,
                info,
                raw,
            )
            written.append((clip, staging_path))

        local_lines = []
        for clip in plan["clips"]:
            for record in clip["local_annotations"]:
                line = {
                    "clip_id": clip["clip_id"],
                    "recording": clip["recording"],
                    "annotation_id": record["annotation_id"],
                    "speaker": record["speaker"],
                    "start_sample": record["local_start_sample"],
                    "end_sample": record["local_end_sample"],
                    "source_start_sample": record["source_start_sample"],
                    "source_end_sample": record["source_end_sample"],
                    "clip_start_sample": clip["start_sample"],
                    "clip_end_sample": clip["end_sample"],
                    "part_index": record["part_index"],
                    "part_count": record["part_count"],
                    "continuation": record["continuation"],
                    "continues_in_next": record["continues_in_next"],
                }
                local_lines.append(json.dumps(line, ensure_ascii=False, sort_keys=True))

        manifest = {
            "schema": "clipgen-manifest/v1",
            "plan_schema": plan.get("schema"),
            "sample_rate": plan["sample_rate"],
            "config": plan["config"],
            "rounding": plan.get("rounding"),
            "clips": [
                {
                    "clip_id": clip["clip_id"],
                    "recording": clip["recording"],
                    "wav_file": clip["wav_file"],
                    "start_sample": clip["start_sample"],
                    "end_sample": clip["end_sample"],
                    "frame_count": clip["end_sample"] - clip["start_sample"],
                    "base_start_sample": clip["base_start_sample"],
                    "base_end_sample": clip["base_end_sample"],
                    "context_start_samples": clip["context_start_samples"],
                    "context_end_samples": clip["context_end_samples"],
                    "cut_reason": clip.get("cut_reason"),
                    "cut_detail": clip.get("cut_detail"),
                    "cut_inside_annotation_ids": clip.get("cut_inside_annotation_ids", []),
                    "nchannels": clip["nchannels"],
                    "channel_layout": clip["channel_layout"],
                    "sample_width": clip["sample_width"],
                    "sample_rate": clip["sample_rate"],
                    "is_continuation": clip["is_continuation"],
                    "continuation_annotation_ids": clip["continuation_annotation_ids"],
                    "split_annotation_ids": clip["split_annotation_ids"],
                    "local_annotation_count": len(clip["local_annotations"]),
                }
                for clip in plan["clips"]
            ],
        }

        # Move staged WAVs into place only after every clip was produced.
        for clip, staging_path in written:
            destination = out_dir / clip["wav_file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_path, destination)

        _write_atomic(
            local_path,
            "\n".join(local_lines) + ("\n" if local_lines else ""),
        )
        _write_atomic(
            manifest_path,
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        _write_atomic(
            status_path,
            json.dumps(
                {
                    "status": "ok",
                    "plan_schema": plan.get("schema"),
                    "clips": len(manifest["clips"]),
                },
                indent=2,
            )
            + "\n",
        )
        return manifest
    except BaseException as exc:
        _write_atomic(
            status_path,
            json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2)
            + "\n",
        )
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# Independent verification


def _read_source_frames_by_offset(recording_path, start_sample, end_sample, frame_size):
    """Slice raw frames by direct byte offset, independent of ``wave.setpos``."""
    offset = start_sample * frame_size
    length = (end_sample - start_sample) * frame_size
    with open(recording_path, "rb") as handle:
        header = handle.read(44)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise BuildError(f"unexpected WAV header: {recording_path}")
        # Locate the data chunk rather than assuming a 44-byte header.
        handle.seek(12)
        data_offset = None
        while True:
            chunk_header = handle.read(8)
            if len(chunk_header) < 8:
                break
            chunk_id = chunk_header[:4]
            chunk_size = int.from_bytes(chunk_header[4:8], "little")
            if chunk_id == b"data":
                data_offset = handle.tell()
                break
            handle.seek(chunk_size + (chunk_size & 1), 1)
        if data_offset is None:
            raise BuildError(f"no data chunk in {recording_path}")
        handle.seek(data_offset + offset)
        data = handle.read(length)
    if len(data) != length:
        raise BuildError(
            f"source slice short: wanted {length} bytes at sample {start_sample}, got {len(data)}"
        )
    return data


def verify_outputs(plan_path, recordings_dir, out_dir):
    """Check built files against the plan and the source WAVs.

    Returns a report; raises ``BuildError`` on the first failed check.
    """
    plan = load_plan(plan_path)
    out_dir = Path(out_dir)
    manifest_path = out_dir / "manifest.json"
    local_path = out_dir / "local_annotations.jsonl"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError(f"manifest not found: {manifest_path}") from exc
    if manifest.get("schema") != "clipgen-manifest/v1":
        raise BuildError("manifest schema is not clipgen-manifest/v1")

    status_path = out_dir / "build-status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("status") != "ok":
            raise BuildError(f"build status is {status.get('status')!r}, not 'ok'")

    clips = {clip["clip_id"]: clip for clip in plan["clips"]}
    manifest_clips = {clip["clip_id"]: clip for clip in manifest["clips"]}
    if len(manifest_clips) != len(manifest["clips"]):
        raise BuildError("manifest contains duplicate clip ids")
    if set(clips) != set(manifest_clips):
        raise BuildError("manifest clip set does not match the plan")

    source_cache = {}
    checked_bytes = 0
    for clip_id, expected in sorted(clips.items()):
        entry = manifest_clips[clip_id]
        for field in ("recording", "start_sample", "end_sample", "nchannels", "sample_width", "sample_rate"):
            if entry[field] != expected[field]:
                raise BuildError(f"manifest field {field} mismatch for {clip_id}")
        if entry["frame_count"] != expected["end_sample"] - expected["start_sample"]:
            raise BuildError(f"frame count mismatch for {clip_id}")

        wav_path = out_dir / entry["wav_file"]
        if not wav_path.exists():
            raise BuildError(f"missing clip file: {wav_path}")
        with wave.open(str(wav_path), "rb") as wav:
            nchannels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            nframes = wav.getnframes()
            payload = wav.readframes(nframes)
            trailing = wav.readframes(1)
        if (nchannels, sample_width, rate) != (
            expected["nchannels"],
            expected["sample_width"],
            expected["sample_rate"],
        ):
            raise BuildError(f"clip {clip_id} WAV format does not match the plan")
        if nframes != entry["frame_count"]:
            raise BuildError(f"clip {clip_id} frame count does not match the manifest")
        if trailing:
            raise BuildError(f"clip {clip_id} has frames beyond its declared length")

        frame_size = sample_width * nchannels
        if len(payload) != nframes * frame_size:
            raise BuildError(f"clip {clip_id} payload length is inconsistent")

        recording_name = entry["recording"]
        info = source_cache.get(recording_name)
        if info is None:
            info = probe_recording(Path(recordings_dir) / recording_name)
            source_cache[recording_name] = info
        source = _read_source_frames_by_offset(
            info.path,
            entry["start_sample"],
            entry["end_sample"],
            info.frame_size,
        )
        if payload != source:
            raise BuildError(
                f"clip {clip_id} bytes do not match source samples "
                f"[{entry['start_sample']}, {entry['end_sample']})"
            )
        checked_bytes += len(payload)

    # Independent coverage: recompute every annotation/clip intersection from
    # the plan's source intervals, without trusting local annotations.
    local_records = []
    if local_path.exists():
        for line_no, raw in enumerate(local_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                local_records.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise BuildError(f"local annotation line {line_no} is not JSON: {exc}") from exc
    else:
        raise BuildError(f"local annotations not found: {local_path}")

    plan_clips = {clip["clip_id"]: clip for clip in plan["clips"]}
    expected_local = []
    local_keys = set()
    for annotation in plan["annotations"]:
        hits = []
        for clip in plan["clips"]:
            if clip["recording"] != annotation["recording"]:
                continue
            start = max(annotation["start_sample"], clip["start_sample"])
            end = min(annotation["end_sample"], clip["end_sample"])
            if start < end:
                hits.append((clip["clip_id"], start, end))
        if not hits:
            raise BuildError(f"annotation {annotation['annotation_id']} has no clip coverage")
        cursor = annotation["start_sample"]
        for index, (clip_id, start, end) in enumerate(hits, start=1):
            key = (clip_id, annotation["annotation_id"])
            if key in local_keys:
                raise BuildError(f"duplicate local annotation for {key}")
            local_keys.add(key)
            if start != cursor:
                raise BuildError(
                    f"annotation {annotation['annotation_id']} coverage has a gap at sample {cursor}"
                )
            cursor = end
            expected_local.append(
                {
                    "clip_id": clip_id,
                    "annotation_id": annotation["annotation_id"],
                    "speaker": annotation["speaker"],
                    "start_sample": start - plan_clips[clip_id]["start_sample"],
                    "end_sample": end - plan_clips[clip_id]["start_sample"],
                    "source_start_sample": start,
                    "source_end_sample": end,
                    "part_index": index,
                    "part_count": len(hits),
                    "continuation": index > 1,
                    "continues_in_next": index < len(hits),
                }
            )
        if cursor != annotation["end_sample"]:
            raise BuildError(
                f"annotation {annotation['annotation_id']} is only partly covered"
            )

    def canonical(records):
        return sorted(
            (
                r["clip_id"],
                r["annotation_id"],
                r["speaker"],
                r["start_sample"],
                r["end_sample"],
                r["source_start_sample"],
                r["source_end_sample"],
                r["part_index"],
                r["part_count"],
                bool(r["continuation"]),
                bool(r["continues_in_next"]),
            )
            for r in records
        )

    if canonical(local_records) != canonical(expected_local):
        raise BuildError("local annotations do not match independently computed intersections")

    # No output may overlap a sibling, and every clip must stay in its source.
    by_recording = {}
    for clip in manifest["clips"]:
        by_recording.setdefault(clip["recording"], []).append(clip)
    for recording_name, recording_clips in by_recording.items():
        recording_clips.sort(key=lambda c: c["start_sample"])
        for previous, current in zip(recording_clips, recording_clips[1:]):
            if previous["end_sample"] > current["start_sample"]:
                raise BuildError(
                    f"output clips overlap in {recording_name}: "
                    f"{previous['clip_id']} / {current['clip_id']}"
                )

    return {
        "clips": len(clips),
        "annotations": len(plan["annotations"]),
        "local_annotations": len(local_records),
        "bytes_checked": checked_bytes,
        "continuation_annotations": sum(
            1 for item in plan.get("continuations", []) if item["part_count"] > 1
        ),
        "rejected": plan.get("rejected_diagnosis", {}).get("count", 0),
    }
