"""Config and WAV input validation.

Only 16 kHz, 16-bit signed PCM, mono or stereo input is supported.  Neither
resampling nor channel conversion is performed: the source channel count is
preserved in every output clip.
"""

import json
import wave
from dataclasses import dataclass
from pathlib import Path

from .timeutil import TimeValueError, parse_seconds, seconds_to_samples

SUPPORTED_RATE = 16000
SUPPORTED_SAMPLE_WIDTH = 2  # bytes -> 16-bit signed PCM
SUPPORTED_CHANNELS = (1, 2)


class ConfigError(ValueError):
    """Invalid config.json content."""


class AudioError(ValueError):
    """A recording cannot be used as-is with the required format."""


@dataclass(frozen=True)
class Config:
    max_clip_samples: int
    merge_silence_samples: int
    context_samples: int

    @property
    def max_clip_seconds(self):
        return self.max_clip_samples / SUPPORTED_RATE

    @property
    def merge_silence_seconds(self):
        return self.merge_silence_samples / SUPPORTED_RATE

    @property
    def context_seconds(self):
        return self.context_samples / SUPPORTED_RATE


_CONFIG_FIELDS = {
    "max_clip_seconds": "max_clip_samples",
    "merge_silence_seconds": "merge_silence_samples",
    "context_seconds": "context_samples",
}


def load_config(path):
    """Load and validate config.json.

    All durations are decimal strings converted to whole samples with the
    same half-up rule used for annotations, so the planner and the annotation
    mapping share one boundary definition.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")

    missing = [name for name in _CONFIG_FIELDS if name not in raw]
    extra = sorted(set(raw) - set(_CONFIG_FIELDS))
    if missing:
        raise ConfigError(f"config missing field(s): {', '.join(missing)}")
    if extra:
        raise ConfigError(f"config has unknown field(s): {', '.join(extra)}")

    values = {}
    for name, attr in _CONFIG_FIELDS.items():
        try:
            seconds = parse_seconds(raw[name], field=f"config.{name}")
        except TimeValueError as exc:
            raise ConfigError(str(exc)) from exc
        values[attr] = seconds_to_samples(seconds)

    config = Config(**values)
    if config.max_clip_samples <= 0:
        raise ConfigError("config.max_clip_seconds must round to at least one sample")
    if config.merge_silence_samples < 0:
        raise ConfigError("config.merge_silence_seconds must not be negative")
    if config.context_samples < 0:
        raise ConfigError("config.context_seconds must not be negative")
    return config


@dataclass(frozen=True)
class RecordingInfo:
    name: str
    path: Path
    nchannels: int
    sample_width: int
    rate: int
    nframes: int

    @property
    def frame_size(self):
        return self.sample_width * self.nchannels


def probe_recording(path):
    """Read a WAV header and enforce the supported format, without decoding."""
    path = Path(path)
    if not path.exists():
        raise AudioError(f"recording not found: {path}")
    try:
        with wave.open(str(path), "rb") as wav:
            nchannels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            rate = wav.getframerate()
            nframes = wav.getnframes()
            comptype = wav.getcomptype()
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"recording is not a readable PCM WAV: {path}: {exc}") from exc

    if comptype != "NONE":
        raise AudioError(f"recording is compressed, not plain PCM: {path}")
    if rate != SUPPORTED_RATE:
        raise AudioError(f"recording sample rate must be {SUPPORTED_RATE} Hz, got {rate}: {path}")
    if sample_width != SUPPORTED_SAMPLE_WIDTH:
        raise AudioError(f"recording must be 16-bit PCM, got {sample_width * 8}-bit: {path}")
    if nchannels not in SUPPORTED_CHANNELS:
        raise AudioError(f"recording must be mono or stereo, got {nchannels} channels: {path}")

    return RecordingInfo(
        name=path.name,
        path=path,
        nchannels=nchannels,
        sample_width=sample_width,
        rate=rate,
        nframes=nframes,
    )


def read_frames(recording, start_sample, end_sample):
    """Return raw interleaved PCM bytes for the exact sample range [start, end).

    Frames are addressed by index, so the byte range is
    ``start * frame_size`` through ``end * frame_size``.  ``wave`` seeks by
    frame index, which keeps the byte offset from drifting regardless of how
    many clips came before.
    """
    if not (0 <= start_sample <= end_sample <= recording.nframes):
        raise AudioError(
            f"sample range {start_sample}..{end_sample} outside recording of "
            f"{recording.nframes} frames: {recording.name}"
        )
    with wave.open(str(recording.path), "rb") as wav:
        wav.setpos(start_sample)
        raw = wav.readframes(end_sample - start_sample)
    expected = (end_sample - start_sample) * recording.frame_size
    if len(raw) != expected:
        raise AudioError(
            f"short read: expected {expected} bytes for {recording.name} "
            f"frames {start_sample}..{end_sample}, got {len(raw)}"
        )
    return raw


def write_pcm_wav(path, recording, raw):
    """Write raw PCM with the source parameters; no conversion happens.

    The channel count, sample width, and rate come straight from the probed
    source recording.  Because ``raw`` is a contiguous interleaved slice of
    the source, channel layout is preserved without any remixing.
    """
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(recording.nchannels)
        wav.setsampwidth(recording.sample_width)
        wav.setframerate(recording.rate)
        wav.writeframes(raw)
