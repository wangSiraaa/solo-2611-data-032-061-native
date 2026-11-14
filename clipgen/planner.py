"""Deterministic clip planning.

The planner works on *sample intervals* only, never on a floating-point
running clock.  For each recording it:

1. merges annotations into a speech union that keeps overlapping speakers;
2. joins union pieces when the silence between them is at most the configured
   merge threshold;
3. cuts each joined segment into base spans no longer than ``max_clip`` at
   *natural* boundaries first (annotation starts/ends and silence midpoints),
   falling back to an exact sample cut inside a long annotation only when no
   natural boundary fits;
4. expands each base span by the configured context, bounded by the midpoints
   between neighbouring clips so output ranges never overlap.

Cuts inside a single annotation are allowed and marked as continuations.
The plan is a pure function of the annotation *content*: annotations are
sorted before planning, so input line order cannot change clips or local
boundaries.
"""

import hashlib
import json
import re
from dataclasses import dataclass

from .audio import SUPPORTED_RATE
from .timeutil import format_samples

PLAN_SCHEMA = "clipgen-plan/v1"


class PlanError(ValueError):
    """The plan could not be produced or violates a planning invariant."""


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    def as_json(self):
        return {
            "start_sample": self.start,
            "end_sample": self.end,
            "start_seconds": format_samples(self.start),
            "end_seconds": format_samples(self.end),
        }


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    # Silence gaps that were absorbed into this segment by the merge threshold.
    merged_gaps: tuple = ()


def _union_pieces(intervals):
    """Union overlapping or touching annotation intervals, keeping gaps."""
    pieces = []
    for start, end in sorted(intervals):
        if pieces and start <= pieces[-1].end:
            pieces[-1] = Span(pieces[-1].start, max(pieces[-1].end, end))
        else:
            pieces.append(Span(start, end))
    return pieces


def _joined_segments(pieces, merge_silence_samples):
    """Join pieces separated by at most ``merge_silence_samples`` of silence."""
    segments = []
    for piece in pieces:
        if segments and piece.start - segments[-1].end <= merge_silence_samples:
            previous = segments[-1]
            gaps = previous.merged_gaps
            if piece.start > previous.end:
                gaps = gaps + (Span(previous.end, piece.start),)
            segments[-1] = Segment(
                previous.start, max(previous.end, piece.end), gaps
            )
        else:
            segments.append(Segment(piece.start, piece.end))
    return segments


def _expected_local_parts(annotations, clips):
    """Map every annotation to its non-empty intersections with clip ranges.

    Returns ``{annotation_id: [(clip_id, source_start, source_end), ...]}``
    ordered by clip position.  Only exact interval intersections are used, so
    this doubles as the coverage definition the verifier checks independently.
    """
    by_recording = {}
    for clip in clips:
        by_recording.setdefault(clip["recording"], []).append(clip)
    for recording_clips in by_recording.values():
        recording_clips.sort(key=lambda clip: clip["start_sample"])

    parts = {}
    for annotation in annotations:
        hits = []
        for clip in by_recording.get(annotation["recording"], []):
            start = max(annotation["start_sample"], clip["start_sample"])
            end = min(annotation["end_sample"], clip["end_sample"])
            if start < end:
                hits.append((clip["clip_id"], start, end))
        parts[annotation["annotation_id"]] = hits
    return parts


def _local_records(annotations, clips):
    """Attach intersection records with continuation metadata to each clip."""
    parts = _expected_local_parts(annotations, clips)
    metadata = {}
    for annotation in annotations:
        hits = parts.get(annotation["annotation_id"], [])
        for index, (clip_id, start, end) in enumerate(hits, start=1):
            metadata[(annotation["annotation_id"], clip_id)] = {
                "annotation_id": annotation["annotation_id"],
                "speaker": annotation["speaker"],
                "part_index": index,
                "part_count": len(hits),
                "continuation": index > 1,
                "continues_in_next": index < len(hits),
                "source_start_sample": start,
                "source_end_sample": end,
            }

    for clip in clips:
        local = []
        for annotation in annotations:
            if annotation["recording"] != clip["recording"]:
                continue
            key = (annotation["annotation_id"], clip["clip_id"])
            if key not in metadata:
                continue
            record = dict(metadata[key])
            record["clip_start_sample"] = clip["start_sample"]
            record["clip_end_sample"] = clip["end_sample"]
            record["local_start_sample"] = record["source_start_sample"] - clip["start_sample"]
            record["local_end_sample"] = record["source_end_sample"] - clip["start_sample"]
            record["source_start_seconds"] = format_samples(record["source_start_sample"])
            record["source_end_seconds"] = format_samples(record["source_end_sample"])
            record["local_start_seconds"] = format_samples(record["local_start_sample"])
            record["local_end_seconds"] = format_samples(record["local_end_sample"])
            local.append(record)
        local.sort(
            key=lambda r: (
                r["local_start_sample"],
                r["local_end_sample"],
                r["annotation_id"],
                r["speaker"],
            )
        )
        clip["local_annotations"] = local
        clip["is_continuation"] = any(r["continuation"] for r in local)
        clip["split_annotation_ids"] = sorted(
            {r["annotation_id"] for r in local if r["part_count"] > 1}
        )
        clip["continuation_annotation_ids"] = sorted(
            {r["annotation_id"] for r in local if r["continuation"]}
        )


def _sanitize(text):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip(".-_")
    return cleaned or "recording"


def _clip_id_prefixes(recordings):
    """Derive stable, unique clip-id prefixes from recording file names."""
    raw = {}
    for name in recordings:
        stem = name[:-4] if name.lower().endswith(".wav") else name
        raw[name] = _sanitize(stem)
    counts = {}
    for prefix in raw.values():
        counts[prefix] = counts.get(prefix, 0) + 1
    prefixes = {}
    for name, prefix in raw.items():
        if counts[prefix] == 1:
            prefixes[name] = prefix
        else:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
            prefixes[name] = f"{prefix}-{digest}"
    return prefixes


def _natural_candidates(annotations, pieces, seg):
    """Annotation boundaries and silence midpoints strictly inside a segment."""
    candidates = set()
    for annotation in annotations:
        for position in (annotation["start_sample"], annotation["end_sample"]):
            if seg.start < position < seg.end:
                candidates.add(position)
    for left, right in zip(pieces, pieces[1:]):
        if right.start <= left.end:
            continue
        midpoint = (left.end + right.start) // 2
        if seg.start < midpoint < seg.end:
            candidates.add(midpoint)
    return sorted(candidates)


@dataclass(frozen=True)
class BaseSpan:
    start: int
    end: int
    # Why this span ends where it does; ``None`` for the final span of a segment.
    cut_reason: str | None = None
    cut_detail: str | None = None
    # Set when this span ends inside a single annotation's interval.
    cut_inside_annotation_ids: tuple = ()


def _split_segment(seg, candidates, max_clip_samples, annotations):
    """Greedily cut a joined segment into spans at most ``max_clip_samples``.

    ``annotations`` are all annotations of the same recording.  Prefer the
    latest natural boundary that still fits and fills at least half the clip.
    The half-fill guard prevents pathological one-sample clips when a boundary
    sits immediately after a cut; if no boundary qualifies, cut at the exact
    maximum sample count (which may split a long annotation).
    """
    spans = []
    # A cut should fill at least half the maximum length so that natural
    # boundaries near the start are not chosen over much better later ones.
    minimum_fill = max(1, max_clip_samples // 2)
    # Only guard against truly degenerate trailing slivers (roughly <0.5 s),
    # not against merely short final clips, which are still reviewable.
    minimum_sliver = max(1, min(max_clip_samples // 8, 8000))
    position = seg.start
    while seg.end - position > max_clip_samples:
        limit = position + max_clip_samples
        best = None
        for candidate in candidates:
            if candidate <= position:
                continue
            if candidate > limit:
                break
            if candidate - position >= minimum_fill:
                best = candidate
        if best is not None:
            cut = best
            if _is_annotation_boundary(annotations, cut):
                reason = "annotation_boundary"
                detail = "cut at an annotation start/end boundary"
            else:
                reason = "silence_midpoint"
                detail = "cut at the midpoint of a short silence between speech pieces"
            # If the natural boundary leaves a degenerate remainder, fall back
            # to an exact cut so the following clip is still usable.
            if seg.end - cut < minimum_sliver:
                cut = seg.end - minimum_sliver
                reason = "sliver_guard"
                detail = (
                    "natural boundary would leave less than the minimum usable "
                    "trailing clip, so the cut is pulled back"
                )
        else:
            if seg.end - limit < minimum_sliver:
                # Splitting at the exact maximum would leave a degenerate
                # trailing clip; divide the remainder between the last two
                # clips instead (both still within the maximum).
                cut = position + (seg.end - position) // 2
                reason, detail = "balanced_tail", (
                    "no natural boundary exists and an exact maximum cut would "
                    "leave a sliver, so the remainder is split evenly"
                )
            else:
                cut = limit
                reason, detail = "max_length", (
                    "no annotation boundary or silence midpoint fits within the "
                    "maximum clip length, so the cut is placed at the exact sample limit"
                )
        if not (position < cut < seg.end):
            raise PlanError(f"internal cut error at sample {position} in segment {seg}")
        inside = tuple(
            sorted(
                a["annotation_id"]
                for a in annotations
                if a["start_sample"] < cut < a["end_sample"]
            )
        )
        spans.append(BaseSpan(position, cut, reason, detail, inside))
        position = cut
    spans.append(BaseSpan(position, seg.end))
    return spans


def _is_annotation_boundary(annotations, position):
    return any(
        position in (a["start_sample"], a["end_sample"]) for a in annotations
    )


def _output_ranges(base_spans, nframes, context_samples):
    """Expand base spans by context without letting neighbouring output overlap."""
    ranges = []
    for index, span in enumerate(base_spans):
        previous = base_spans[index - 1] if index > 0 else None
        following = base_spans[index + 1] if index + 1 < len(base_spans) else None

        start = max(0, span.start - context_samples)
        if previous is not None:
            boundary = (previous.end + span.start) // 2
            start = max(start, boundary)
        end = min(nframes, span.end + context_samples)
        if following is not None:
            boundary = (span.end + following.start) // 2
            end = min(end, boundary)
        if start > span.start or end < span.end:
            raise PlanError("context expansion crossed a base span boundary")
        ranges.append(Span(start, end))
    return ranges


def build_plan(config, annotations, recordings):
    """Create the plan dictionary from validated annotations and recordings.

    ``annotations`` are plain dicts with ``recording``, ``annotation_id``,
    ``speaker``, ``start_sample`` and ``end_sample``.  ``recordings`` maps a
    recording name to an object exposing ``nchannels``, ``sample_width``,
    ``rate`` and ``nframes``.
    """
    ordered = sorted(
        annotations,
        key=lambda a: (
            a["recording"],
            a["start_sample"],
            a["end_sample"],
            a["annotation_id"],
            a["speaker"],
        ),
    )

    by_recording = {}
    for annotation in ordered:
        by_recording.setdefault(annotation["recording"], []).append(annotation)

    prefixes = _clip_id_prefixes(sorted(by_recording))
    all_clips = []
    segmentation = {}

    for recording_name in sorted(by_recording):
        recording_annotations = by_recording[recording_name]
        info = recordings[recording_name]
        intervals = [(a["start_sample"], a["end_sample"]) for a in recording_annotations]
        pieces = _union_pieces(intervals)
        segments = _joined_segments(pieces, config.merge_silence_samples)

        base_spans = []
        for segment in segments:
            candidates = _natural_candidates(recording_annotations, pieces, segment)
            base_spans.extend(
                _split_segment(
                    segment, candidates, config.max_clip_samples, recording_annotations
                )
            )

        if not base_spans:
            continue

        # ``_output_ranges`` looks at neighbours within one recording only.
        output_ranges = _output_ranges(base_spans, info.nframes, config.context_samples)

        # Explain, per joined segment, which speech pieces and silence gaps it
        # contains and how it was divided.  This is what makes cuts auditable
        # without replaying the algorithm.
        segment_notes = []
        for segment_index, segment in enumerate(segments, start=1):
            covering_pieces = [
                piece
                for piece in pieces
                if piece.start < segment.end and piece.end > segment.start
            ]
            segment_notes.append(
                {
                    "segment_index": segment_index,
                    "start_sample": segment.start,
                    "end_sample": segment.end,
                    "start_seconds": format_samples(segment.start),
                    "end_seconds": format_samples(segment.end),
                    "speech_pieces": [
                        {
                            "start_sample": piece.start,
                            "end_sample": piece.end,
                            "start_seconds": format_samples(piece.start),
                            "end_seconds": format_samples(piece.end),
                        }
                        for piece in covering_pieces
                    ],
                    "merged_silence_gaps": [
                        {
                            "start_sample": gap.start,
                            "end_sample": gap.end,
                            "duration_samples": gap.end - gap.start,
                        }
                        for gap in segment.merged_gaps
                    ],
                }
            )
        segmentation[recording_name] = segment_notes

        prefix = prefixes[recording_name]
        for index, (span, output) in enumerate(zip(base_spans, output_ranges), start=1):
            clip_id = f"{prefix}-{index:04d}"
            all_clips.append(
                {
                    "clip_id": clip_id,
                    "recording": recording_name,
                    "wav_file": f"clips/{clip_id}.wav",
                    "start_sample": output.start,
                    "end_sample": output.end,
                    "base_start_sample": span.start,
                    "base_end_sample": span.end,
                    "context_start_samples": span.start - output.start,
                    "context_end_samples": output.end - span.end,
                    "cut_reason": span.cut_reason,
                    "cut_detail": span.cut_detail,
                    "cut_inside_annotation_ids": list(span.cut_inside_annotation_ids),
                    "nchannels": info.nchannels,
                    "channel_layout": "mono" if info.nchannels == 1 else "stereo",
                    "sample_width": info.sample_width,
                    "sample_rate": info.rate,
                }
            )

    _local_records(ordered, all_clips)
    all_clips.sort(key=lambda c: (c["recording"], c["start_sample"], c["clip_id"]))

    plan = {
        "schema": PLAN_SCHEMA,
        "sample_rate": SUPPORTED_RATE,
        "rounding": {
            "rule": "ROUND_HALF_UP",
            "unit": "samples",
            "description": (
                "Each decimal-seconds value is multiplied by 16000 exactly and rounded "
                "half up to the nearest sample; no float accumulation is used. "
                "Intervals are left-closed, right-open [start, end)."
            ),
        },
        "config": {
            "max_clip_seconds": format_samples(config.max_clip_samples),
            "max_clip_samples": config.max_clip_samples,
            "merge_silence_seconds": format_samples(config.merge_silence_samples),
            "merge_silence_samples": config.merge_silence_samples,
            "context_seconds": format_samples(config.context_samples),
            "context_samples": config.context_samples,
        },
        "recordings": {
            name: {
                "nchannels": info.nchannels,
                "channel_layout": "mono" if info.nchannels == 1 else "stereo",
                "sample_width": info.sample_width,
                "sample_rate": info.rate,
                "nframes": info.nframes,
            }
            for name, info in sorted(recordings.items())
            if name in by_recording
        },
        "annotations": [
            {
                "recording": a["recording"],
                "annotation_id": a["annotation_id"],
                "speaker": a["speaker"],
                "start_text": a.get("start_text"),
                "end_text": a.get("end_text"),
                "start_sample": a["start_sample"],
                "end_sample": a["end_sample"],
            }
            for a in ordered
        ],
        "clips": all_clips,
        "segmentation": segmentation,
        "totals": {
            "annotations": len(ordered),
            "clips": len(all_clips),
            "recordings": len({a["recording"] for a in ordered}),
            "continuation_clips": sum(1 for clip in all_clips if clip["is_continuation"]),
            "split_annotations": len(
                {r["annotation_id"] for clip in all_clips for r in clip["local_annotations"]
                 if r["part_count"] > 1}
            ),
        },
    }
    assert_plan_invariants(plan)
    return plan


def _validate_plan_shape(plan):
    if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
        raise PlanError(f"plan schema must be {PLAN_SCHEMA!r}")
    for key in ("config", "recordings", "annotations", "clips"):
        if key not in plan:
            raise PlanError(f"plan missing {key!r}")
    if not isinstance(plan["clips"], list) or not isinstance(plan["annotations"], list):
        raise PlanError("plan clips and annotations must be lists")


def assert_plan_invariants(plan):
    """Check properties the build step is allowed to rely on."""
    _validate_plan_shape(plan)
    rate = plan.get("sample_rate")
    if rate != SUPPORTED_RATE:
        raise PlanError(f"plan sample_rate must be {SUPPORTED_RATE}")

    recordings = plan["recordings"]
    annotations = plan["annotations"]
    clips = plan["clips"]

    annotation_ids = set()
    for annotation in annotations:
        annotation_id = annotation.get("annotation_id")
        if not isinstance(annotation_id, str) or not annotation_id:
            raise PlanError("annotation without an id")
        if annotation_id in annotation_ids:
            raise PlanError(f"duplicate annotation id in plan: {annotation_id}")
        annotation_ids.add(annotation_id)
        name = annotation.get("recording")
        if name not in recordings:
            raise PlanError(f"annotation references unknown recording: {name}")
        start = annotation.get("start_sample")
        end = annotation.get("end_sample")
        if not isinstance(start, int) or not isinstance(end, int):
            raise PlanError(f"annotation {annotation_id} has non-integer samples")
        if not (0 <= start < end <= recordings[name]["nframes"]):
            raise PlanError(f"annotation {annotation_id} range is invalid")

    by_recording = {}
    seen_clip_ids = set()
    for clip in clips:
        name = clip.get("recording")
        if name not in recordings:
            raise PlanError(f"clip references unknown recording: {name}")
        clip_id = clip.get("clip_id")
        if not isinstance(clip_id, str) or not clip_id:
            raise PlanError("clip without an id")
        if clip_id in seen_clip_ids:
            raise PlanError(f"duplicate clip id in plan: {clip_id}")
        seen_clip_ids.add(clip_id)
        info = recordings[name]
        if clip.get("nchannels") != info["nchannels"]:
            raise PlanError(f"clip {clip.get('clip_id')} channel count mismatch")
        start = clip.get("start_sample")
        end = clip.get("end_sample")
        base_start = clip.get("base_start_sample")
        base_end = clip.get("base_end_sample")
        for label, value in (
            ("start", start),
            ("end", end),
            ("base_start", base_start),
            ("base_end", base_end),
        ):
            if not isinstance(value, int):
                raise PlanError(f"clip {clip.get('clip_id')} has non-integer {label}")
        if not (0 <= start <= base_start < base_end <= end <= info["nframes"]):
            raise PlanError(f"clip {clip.get('clip_id')} has an invalid sample range")
        if base_end - base_start > plan["config"]["max_clip_samples"]:
            raise PlanError(f"clip {clip.get('clip_id')} exceeds the maximum base span")
        by_recording.setdefault(name, []).append(clip)

    for name, recording_clips in by_recording.items():
        recording_clips.sort(key=lambda c: c["start_sample"])
        for previous, current in zip(recording_clips, recording_clips[1:]):
            if previous["end_sample"] > current["start_sample"]:
                raise PlanError(
                    f"output clips overlap in {name}: "
                    f"{previous['clip_id']} and {current['clip_id']}"
                )

    parts = _expected_local_parts(annotations, clips)
    for annotation in annotations:
        hits = parts.get(annotation["annotation_id"], [])
        if not hits:
            raise PlanError(f"annotation {annotation['annotation_id']} is not covered")
        cursor = annotation["start_sample"]
        for _, start, end in hits:
            if start != cursor:
                raise PlanError(
                    f"annotation {annotation['annotation_id']} coverage is not contiguous"
                )
            cursor = end
        if cursor != annotation["end_sample"]:
            raise PlanError(f"annotation {annotation['annotation_id']} is only partly covered")

    # Every local record must correspond to a real intersection.
    for clip in clips:
        for record in clip.get("local_annotations", []):
            annotation = next(
                (a for a in annotations if a["annotation_id"] == record["annotation_id"]),
                None,
            )
            if annotation is None:
                raise PlanError(f"local annotation references unknown id: {record['annotation_id']}")
            expected_start = max(annotation["start_sample"], clip["start_sample"])
            expected_end = min(annotation["end_sample"], clip["end_sample"])
            if not (record["source_start_sample"] == expected_start
                    and record["source_end_sample"] == expected_end):
                raise PlanError(
                    f"local annotation {record['annotation_id']} in {clip['clip_id']} "
                    "does not match its source intersection"
                )
    return True


def plan_to_json(plan):
    return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
