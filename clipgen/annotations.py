"""Annotation loading, validation, and per-line diagnostics.

Every rejected input line is retained verbatim in a rejected-records JSONL
file.  Validation never guesses: a line is either a valid annotation or it
carries a machine-readable code and a human-readable reason tied to its
original line number.

Input order does not affect the result.  In particular, an annotation ID
that appears more than once rejects *all* of its occurrences rather than
letting the first line win, which would make the plan order-dependent.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .audio import AudioError, probe_recording
from .timeutil import TimeValueError, format_samples, parse_seconds, seconds_to_samples

REQUIRED_FIELDS = ("recording", "annotation_id", "speaker", "start", "end")


@dataclass(frozen=True)
class Annotation:
    recording: str
    annotation_id: str
    speaker: str
    start_text: str
    end_text: str
    start_sample: int
    end_sample: int
    line: int
    raw: str

    def as_source_json(self):
        return {
            "recording": self.recording,
            "annotation_id": self.annotation_id,
            "speaker": self.speaker,
            "start_text": self.start_text,
            "end_text": self.end_text,
            "start_sample": self.start_sample,
            "end_sample": self.end_sample,
            "start_seconds": format_samples(self.start_sample),
            "end_seconds": format_samples(self.end_sample),
            "source_line": self.line,
        }


@dataclass(frozen=True)
class Rejected:
    line: int
    code: str
    reason: str
    raw: str
    record: dict | None

    def as_json(self):
        payload = {
            "line": self.line,
            "code": self.code,
            "reason": self.reason,
            "raw": self.raw,
        }
        if isinstance(self.record, dict):
            for key in ("recording", "annotation_id", "speaker"):
                if key in self.record:
                    payload[key] = self.record[key]
        return payload


def _reject(line, code, reason, raw, record):
    return Rejected(line=line, code=code, reason=reason, raw=raw, record=record)


def _safe_recording_name(name):
    """Recordings are plain file names inside the recordings directory."""
    if not name:
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if name in (".", ".."):
        return False
    if Path(name).name != name:
        return False
    if Path(name).is_absolute():
        return False
    return True


def _read_lines(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"annotations not found: {path}") from exc
    # ``splitlines`` keeps the line content without terminators; blank lines
    # are ignored but still occupy a line number.
    return text.splitlines()


def load_annotations(annotations_path, recordings_dir):
    """Load annotations and return ``(valid, rejected, recordings)``.

    ``recordings`` maps referenced recording names to probed ``RecordingInfo``
    for every recording that has at least one valid annotation.
    """
    recordings_dir = Path(recordings_dir)
    lines = _read_lines(annotations_path)

    candidates = []  # (line_no, raw, record)
    rejected = []

    for line_no, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            rejected.append(_reject(line_no, "invalid_json", f"not valid JSON: {exc}", raw, None))
            continue
        if not isinstance(record, dict):
            rejected.append(_reject(line_no, "invalid_record", "annotation must be a JSON object", raw, None))
            continue
        missing = [name for name in REQUIRED_FIELDS if name not in record]
        if missing:
            rejected.append(
                _reject(line_no, "missing_field", f"missing field(s): {', '.join(missing)}", raw, record)
            )
            continue
        type_bad = []
        for name in ("recording", "annotation_id", "speaker"):
            if not isinstance(record[name], str) or not record[name].strip():
                type_bad.append(name)
        for name in ("start", "end"):
            if not isinstance(record[name], str):
                type_bad.append(name)
        if type_bad:
            rejected.append(
                _reject(
                    line_no,
                    "invalid_field_type",
                    "field(s) must be non-empty strings: " + ", ".join(type_bad),
                    raw,
                    record,
                )
            )
            continue
        candidates.append((line_no, raw, record))

    # Duplicate-ID detection across all structurally usable lines.  Reject
    # every occurrence so that neither ordering nor "first wins" decides.
    by_id = {}
    for line_no, raw, record in candidates:
        by_id.setdefault(record["annotation_id"], []).append(line_no)
    conflicting_ids = {key: lines for key, lines in by_id.items() if len(lines) > 1}

    valid = []
    probe_cache = {}
    probe_errors = {}

    for line_no, raw, record in candidates:
        annotation_id = record["annotation_id"]
        if annotation_id in conflicting_ids:
            occurrences = ", ".join(str(n) for n in conflicting_ids[annotation_id])
            rejected.append(
                _reject(
                    line_no,
                    "duplicate_annotation_id",
                    f"annotation_id {annotation_id!r} occurs on line(s) {occurrences}; "
                    "all occurrences rejected",
                    raw,
                    record,
                )
            )
            continue

        recording_name = record["recording"]
        if not _safe_recording_name(recording_name):
            rejected.append(
                _reject(
                    line_no,
                    "invalid_recording_name",
                    f"recording must be a plain file name inside the recordings directory: "
                    f"{recording_name!r}",
                    raw,
                    record,
                )
            )
            continue
        if recording_name not in probe_cache and recording_name not in probe_errors:
            try:
                probe_cache[recording_name] = probe_recording(recordings_dir / recording_name)
            except AudioError as exc:
                probe_errors[recording_name] = str(exc)
        if recording_name in probe_errors:
            reason = probe_errors[recording_name]
            code = "missing_recording" if not (recordings_dir / recording_name).exists() else "invalid_recording"
            rejected.append(_reject(line_no, code, reason, raw, record))
            continue
        info = probe_cache[recording_name]

        try:
            start_seconds = parse_seconds(record["start"], field="start")
            end_seconds = parse_seconds(record["end"], field="end")
        except TimeValueError as exc:
            rejected.append(_reject(line_no, "invalid_time", str(exc), raw, record))
            continue

        start_sample = seconds_to_samples(start_seconds, rate=info.rate)
        end_sample = seconds_to_samples(end_seconds, rate=info.rate)

        if start_sample > end_sample:
            rejected.append(
                _reject(
                    line_no,
                    "invalid_range",
                    f"start maps to sample {start_sample}, after end sample {end_sample}",
                    raw,
                    record,
                )
            )
            continue
        if start_sample == end_sample:
            rejected.append(
                _reject(
                    line_no,
                    "zero_duration",
                    f"annotation maps to an empty sample interval [{start_sample}, {end_sample})",
                    raw,
                    record,
                )
            )
            continue
        if start_sample > info.nframes or end_sample > info.nframes:
            rejected.append(
                _reject(
                    line_no,
                    "out_of_range",
                    f"sample interval [{start_sample}, {end_sample}) exceeds recording "
                    f"length {info.nframes} samples ({format_samples(info.nframes)} s)",
                    raw,
                    record,
                )
            )
            continue

        valid.append(
            Annotation(
                recording=recording_name,
                annotation_id=annotation_id,
                speaker=record["speaker"],
                start_text=record["start"],
                end_text=record["end"],
                start_sample=start_sample,
                end_sample=end_sample,
                line=line_no,
                raw=raw,
            )
        )

    used_recordings = {}
    for annotation in valid:
        used_recordings.setdefault(annotation.recording, probe_cache[annotation.recording])

    # Diagnostics are reported in input order but the *plan* never depends on
    # that order; sort valid annotations deterministically by content.
    valid.sort(key=lambda a: (a.recording, a.start_sample, a.end_sample, a.annotation_id, a.speaker))
    rejected.sort(key=lambda r: r.line)
    return valid, rejected, used_recordings
