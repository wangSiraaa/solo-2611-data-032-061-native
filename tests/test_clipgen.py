"""Tests for clipgen.  Run with ``python3 -m unittest discover -s tests``."""

import json
import shutil
import struct
import tempfile
import unittest
import wave
from decimal import Decimal
from pathlib import Path

from clipgen.audio import Config, probe_recording, read_frames
from clipgen.planner import PlanError, assert_plan_invariants, build_plan
from clipgen.timeutil import (
    format_samples,
    parse_seconds,
    samples_to_decimal,
    seconds_to_samples,
)
from clipgen.writer import build_outputs, create_plan, verify_outputs, write_plan_outputs


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def _write_wav(path, nchannels, nframes, framerate=16000, seed=0, sampwidth=2):
    """Deterministic non-silent PCM so byte comparisons are meaningful."""
    frames = []
    for index in range(nframes):
        for channel in range(nchannels):
            value = ((index * 7 + channel * 101 + seed) % 60000) - 30000
            frames.append(value)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(nchannels)
        wav.setsampwidth(sampwidth)
        wav.setframerate(framerate)
        wav.writeframes(struct.pack("<" + "h" * len(frames), *frames))


def _recording_info(nframes, nchannels=2):
    return type(
        "Info",
        (),
        {
            "nchannels": nchannels,
            "sample_width": 2,
            "rate": 16000,
            "nframes": nframes,
        },
    )()


def _ann(recording, annotation_id, start, end, speaker="s", start_text=None, end_text=None):
    return {
        "recording": recording,
        "annotation_id": annotation_id,
        "speaker": speaker,
        "start_sample": start,
        "end_sample": end,
        "start_text": start_text,
        "end_text": end_text,
    }


class TimeMappingTests(unittest.TestCase):
    def test_exact_values(self):
        self.assertEqual(seconds_to_samples(Decimal("1.2")), 19200)
        self.assertEqual(seconds_to_samples(Decimal("0")), 0)
        self.assertEqual(format_samples(19200), "1.2")

    def test_round_half_up_at_exact_half(self):
        # 0.00003125 s = 0.5 samples exactly -> rounds up to 1.
        self.assertEqual(seconds_to_samples(Decimal("0.00003125")), 1)
        # 0.00009375 s = 1.5 samples exactly -> rounds up to 2.
        self.assertEqual(seconds_to_samples(Decimal("0.00009375")), 2)

    def test_no_float_drift_across_late_timestamps(self):
        # A float implementation would accumulate error over many additions;
        # each timestamp here is converted independently from its own decimal.
        samples = [seconds_to_samples(Decimal("0.1") * i) for i in range(1, 1001)]
        for i, sample in enumerate(samples, start=1):
            self.assertEqual(sample, (16000 * i + 5) // 10)
        self.assertEqual(samples[-1], 1600000)

    def test_rejects_malformed_times(self):
        for bad in ["-1", "1e3", "0.1.2", "", "abc", "1_000"]:
            with self.assertRaises(ValueError):
                parse_seconds(bad)


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.recordings = self.tmp / "recordings"
        self.recordings.mkdir()
        _write_wav(self.recordings / "session-a.wav", 2, 144000)
        _write_wav(self.recordings / "session-b.wav", 1, 80000)
        self.config = self.tmp / "config.json"
        self.config.write_text(
            json.dumps(
                {
                    "max_clip_seconds": "3.0",
                    "merge_silence_seconds": "0.25",
                    "context_seconds": "0.10",
                }
            ),
            encoding="utf-8",
        )

    def test_fixture_annotations_plan(self):
        plan, rejected = create_plan(
            FIXTURES / "annotations.jsonl", FIXTURES / "config.json", FIXTURES
        )
        self.assertEqual(len(plan["annotations"]), 6)
        self.assertEqual([item.record["annotation_id"] for item in rejected], ["bad-range"])
        self.assertEqual(plan["totals"]["clips"], 6)
        self.assertEqual(plan["continuations"][0]["annotation_id"], "a3")
        assert_plan_invariants(plan)

    def test_rounding_rule_is_recorded(self):
        plan, _ = create_plan(
            FIXTURES / "annotations.jsonl", FIXTURES / "config.json", FIXTURES
        )
        self.assertEqual(plan["rounding"]["rule"], "ROUND_HALF_UP")
        by_id = {a["annotation_id"]: a for a in plan["annotations"]}
        # 0.10003 * 16000 = 1600.48 -> 1600; 7.70003 * 16000 = 123200.48 -> 123200
        self.assertEqual(by_id["a1"]["start_sample"], 1600)
        self.assertEqual(by_id["a3"]["end_sample"], 123200)
        # 1.20000 * 16000 = 19200 exactly.
        self.assertEqual(by_id["a2"]["start_sample"], 19200)

    def test_duplicate_id_rejects_all_occurrences(self):
        ann = self.tmp / "dup.jsonl"
        ann.write_text(
            "\n".join(
                json.dumps(item)
                for item in [
                    {"recording": "session-a.wav", "annotation_id": "dup", "speaker": "s1", "start": "0.1", "end": "0.5"},
                    {"recording": "session-a.wav", "annotation_id": "dup", "speaker": "s2", "start": "0.6", "end": "0.9"},
                    {"recording": "session-a.wav", "annotation_id": "ok", "speaker": "s3", "start": "1", "end": "2"},
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        plan, rejected = create_plan(ann, self.config, self.recordings)
        dup_rejections = [r for r in rejected if r.code == "duplicate_annotation_id"]
        self.assertEqual(len(dup_rejections), 2)
        self.assertEqual([a["annotation_id"] for a in plan["annotations"]], ["ok"])

    def test_zero_duration_and_reversed_and_out_of_range(self):
        ann = self.tmp / "bad.jsonl"
        rows = [
            {"recording": "session-b.wav", "annotation_id": "zero", "speaker": "s", "start": "1.0", "end": "1.00002"},
            {"recording": "session-b.wav", "annotation_id": "rev", "speaker": "s", "start": "2.0", "end": "1.0"},
            {"recording": "session-b.wav", "annotation_id": "oor", "speaker": "s", "start": "4.9", "end": "5.1"},
            {"recording": "missing.wav", "annotation_id": "gone", "speaker": "s", "start": "0", "end": "1"},
            {"recording": "session-b.wav", "annotation_id": "ok", "speaker": "s", "start": "0", "end": "1"},
        ]
        ann.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        plan, rejected = create_plan(ann, self.config, self.recordings)
        codes = {r.code for r in rejected}
        self.assertIn("zero_duration", codes)
        self.assertIn("invalid_range", codes)
        self.assertIn("out_of_range", codes)
        self.assertIn("missing_recording", codes)
        self.assertEqual([a["annotation_id"] for a in plan["annotations"]], ["ok"])
        # Rejected records keep the original raw line verbatim.
        for item in rejected:
            self.assertIn(item.raw, ann.read_text(encoding="utf-8"))

    def test_order_independence(self):
        lines = (FIXTURES / "annotations.jsonl").read_text(encoding="utf-8").splitlines()
        shuffled = self.tmp / "shuffled.jsonl"
        # Reverse order (including the bad line) and blank lines interspersed.
        contents = "\n\n".join(reversed(lines)) + "\n"
        shuffled.write_text(contents, encoding="utf-8")
        plan_a, rej_a = create_plan(
            FIXTURES / "annotations.jsonl", FIXTURES / "config.json", FIXTURES
        )
        plan_b, rej_b = create_plan(shuffled, FIXTURES / "config.json", FIXTURES)
        for key in ("annotations", "clips", "segmentation", "continuations", "annotation_parts"):
            self.assertEqual(plan_a[key], plan_b[key], key)
        self.assertEqual(
            {r.record["annotation_id"] for r in rej_a},
            {r.record["annotation_id"] for r in rej_b},
        )

    def test_bad_format_rejected(self):
        bad = self.tmp / "recordings2"
        bad.mkdir()
        _write_wav(bad / "r8.wav", 1, 16000, framerate=8000, sampwidth=2)
        _write_wav(bad / "stereo32.wav", 2, 16000, framerate=16000, sampwidth=4)
        ann = self.tmp / "fmt.jsonl"
        ann.write_text(
            "\n".join(
                json.dumps({"recording": name, "annotation_id": name, "speaker": "s", "start": "0", "end": "1"})
                for name in ("r8.wav", "stereo32.wav")
            )
            + "\n",
            encoding="utf-8",
        )
        plan, rejected = create_plan(ann, self.config, bad)
        self.assertEqual(plan["annotations"], [])
        self.assertEqual({r.code for r in rejected}, {"invalid_recording"})


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.info = _recording_info(160000)
        self.config = Config(
            max_clip_samples=48000,
            merge_silence_samples=4000,
            context_samples=1600,
        )

    def test_overlapping_speakers_stay_in_one_clip_with_two_local_annotations(self):
        annotations = [
            _ann("r.wav", "x", 1600, 20000, "s1"),
            _ann("r.wav", "y", 18000, 36000, "s2"),
        ]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        self.assertEqual(len(plan["clips"]), 1)
        local_ids = [r["annotation_id"] for r in plan["clips"][0]["local_annotations"]]
        self.assertEqual(sorted(local_ids), ["x", "y"])

    def test_merge_threshold_boundary(self):
        annotations = [_ann("r.wav", "a", 0, 16000), _ann("r.wav", "b", 20000, 30000)]
        # Gap is exactly 4000 samples == threshold, so pieces merge.
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        self.assertEqual(len(plan["clips"]), 1)
        far = [_ann("r.wav", "a", 0, 16000), _ann("r.wav", "b", 20001, 30000)]
        plan_far = build_plan(self.config, far, {"r.wav": self.info})
        self.assertEqual(len(plan_far["clips"]), 2)

    def test_cuts_at_natural_boundaries_not_fixed_length(self):
        annotations = [
            _ann("r.wav", "a", 0, 16000),
            _ann("r.wav", "b", 17000, 20000),
            _ann("r.wav", "c", 21000, 40000),
        ]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        # The 40000-sample speech union exceeds 48000?  No: it is 40000, so one clip.
        self.assertEqual(len(plan["clips"]), 1)
        annotations = [
            _ann("r.wav", "a", 0, 16000),
            _ann("r.wav", "b", 20000, 30000),
            _ann("r.wav", "c", 34000, 70000),
        ]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        self.assertEqual(len(plan["clips"]), 2)
        cut_reasons = {clip["cut_reason"] for clip in plan["clips"]}
        self.assertIn("annotation_boundary", cut_reasons)
        # First clip should end at annotation c's start (a natural boundary).
        self.assertEqual(plan["clips"][0]["base_end_sample"], 34000)

    def test_long_annotation_continuation(self):
        annotations = [_ann("r.wav", "long", 10000, 110000)]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        self.assertEqual(len(plan["clips"]), 3)
        parts = [r for c in plan["clips"] for r in c["local_annotations"]]
        self.assertEqual([r["part_index"] for r in parts], [1, 2, 3])
        self.assertEqual([r["continuation"] for r in parts], [False, True, True])
        self.assertTrue(plan["clips"][0]["is_continuation"] is False)
        # Base spans avoid a degenerate trailing sliver: the final remainder is
        # balanced rather than leaving a few thousand samples.
        bases = [(c["base_start_sample"], c["base_end_sample"]) for c in plan["clips"]]
        self.assertEqual(bases, [(10000, 58000), (58000, 84000), (84000, 110000)])
        for clip in plan["clips"]:
            length = clip["base_end_sample"] - clip["base_start_sample"]
            self.assertLessEqual(length, 48000)
            self.assertGreaterEqual(length, 24000)

    def test_context_never_overlaps_output(self):
        annotations = [_ann("r.wav", "a", 0, 10000), _ann("r.wav", "b", 20000, 30000)]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        clips = plan["clips"]
        self.assertEqual(len(clips), 2)
        self.assertLessEqual(clips[0]["end_sample"], clips[1]["start_sample"])
        # Context is the full 1600 samples here; the midpoint bound (15000)
        # is looser, so the configured context wins.
        self.assertEqual(clips[0]["end_sample"], 11600)
        self.assertEqual(clips[1]["start_sample"], 18400)
        self.assertEqual(clips[0]["context_end_samples"], 1600)
        self.assertEqual(clips[1]["context_start_samples"], 1600)

    def test_large_context_is_clamped_at_midpoint(self):
        config = Config(
            max_clip_samples=48000, merge_silence_samples=4000, context_samples=20000
        )
        annotations = [_ann("r.wav", "a", 0, 10000), _ann("r.wav", "b", 20000, 30000)]
        plan = build_plan(config, annotations, {"r.wav": self.info})
        clips = plan["clips"]
        self.assertEqual(clips[0]["end_sample"], 15000)
        self.assertEqual(clips[1]["start_sample"], 15000)
        self.assertEqual(clips[0]["context_end_samples"], 5000)
        self.assertEqual(clips[1]["context_start_samples"], 5000)

    def test_balanced_tail_without_natural_boundary(self):
        annotations = [_ann("r.wav", "long", 0, 100000)]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        bases = [(c["base_start_sample"], c["base_end_sample"]) for c in plan["clips"]]
        self.assertEqual(bases, [(0, 48000), (48000, 74000), (74000, 100000)])
        self.assertEqual(plan["clips"][1]["cut_reason"], "balanced_tail")
        for clip in plan["clips"]:
            self.assertLessEqual(
                clip["base_end_sample"] - clip["base_start_sample"], 48000
            )

    def test_sliver_guard_pulls_back_a_tight_natural_boundary(self):
        # Boundary at 45000 would leave only 5000 samples (sliver < 6000 =
        # max_clip // 8); the planner pulls the cut back to 50000 - 6000.
        annotations = [
            _ann("r.wav", "a", 0, 45000),
            _ann("r.wav", "b", 45000, 50000),
        ]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        bases = [(c["base_start_sample"], c["base_end_sample"]) for c in plan["clips"]]
        self.assertEqual(bases, [(0, 44000), (44000, 50000)])
        self.assertEqual(plan["clips"][0]["cut_reason"], "sliver_guard")

    def test_clip_ids_unique_and_prefixes_disambiguated(self):
        info = _recording_info(160000)
        plan = build_plan(
            self.config,
            [_ann("a/session.wav", "x", 0, 10000), _ann("b/session.wav", "y", 0, 10000)],
            {"a/session.wav": info, "b/session.wav": info},
        )
        ids = [clip["clip_id"] for clip in plan["clips"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_invariant_detects_overlap(self):
        annotations = [_ann("r.wav", "a", 0, 50000), _ann("r.wav", "b", 10000, 60000)]
        plan = build_plan(self.config, annotations, {"r.wav": self.info})
        broken = json.loads(json.dumps(plan))
        broken["clips"][0]["end_sample"] = broken["clips"][1]["start_sample"] + 5
        with self.assertRaises(PlanError):
            assert_plan_invariants(broken)


class BuildAndVerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.recordings = FIXTURES

    def _plan(self):
        plan, rejected = create_plan(
            FIXTURES / "annotations.jsonl", FIXTURES / "config.json", self.recordings
        )
        plan_path = self.tmp / "plan.json"
        rejected_path = self.tmp / "rejected.jsonl"
        write_plan_outputs(plan, rejected, plan_path, rejected_path)
        return plan, plan_path

    def test_bytes_match_source_ranges(self):
        plan, plan_path = self._plan()
        out = self.tmp / "out"
        build_outputs(plan, self.recordings, out)
        report = verify_outputs(plan_path, self.recordings, out)
        self.assertTrue(report["clips"] >= 1)

        # Independently re-slice the source with raw byte offsets and compare.
        for entry in json.loads((out / "manifest.json").read_text(encoding="utf-8"))["clips"]:
            info = probe_recording(self.recordings / entry["recording"])
            expected = read_frames(info, entry["start_sample"], entry["end_sample"])
            with wave.open(str(out / entry["wav_file"]), "rb") as wav:
                actual = wav.readframes(wav.getnframes())
                self.assertEqual(wav.getnchannels(), info.nchannels)
                self.assertEqual(wav.getframerate(), 16000)
                self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(actual, expected, entry["clip_id"])
            self.assertEqual(
                len(actual),
                (entry["end_sample"] - entry["start_sample"]) * info.frame_size,
            )

    def test_local_annotations_cover_every_intersection(self):
        plan, plan_path = self._plan()
        out = self.tmp / "out"
        build_outputs(plan, self.recordings, out)
        records = [
            json.loads(line)
            for line in (out / "local_annotations.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        expected = set()
        for annotation in plan["annotations"]:
            for clip in plan["clips"]:
                if clip["recording"] != annotation["recording"]:
                    continue
                start = max(annotation["start_sample"], clip["start_sample"])
                end = min(annotation["end_sample"], clip["end_sample"])
                if start < end:
                    expected.add(
                        (
                            annotation["annotation_id"],
                            clip["clip_id"],
                            start - clip["start_sample"],
                            end - clip["start_sample"],
                        )
                    )
        actual = {
            (r["annotation_id"], r["clip_id"], r["start_sample"], r["end_sample"])
            for r in records
        }
        self.assertEqual(actual, expected)

    def test_no_output_overlap_and_audio_does_not_repeat(self):
        plan, _ = self._plan()
        out = self.tmp / "out"
        build_outputs(plan, self.recordings, out)
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        by_recording = {}
        for clip in manifest["clips"]:
            by_recording.setdefault(clip["recording"], []).append(clip)
        for clips in by_recording.values():
            clips.sort(key=lambda c: c["start_sample"])
            for previous, current in zip(clips, clips[1:]):
                self.assertLessEqual(previous["end_sample"], current["start_sample"])

    def test_failed_build_does_not_leave_stale_manifest(self):
        plan, _ = self._plan()
        out = self.tmp / "out"
        build_outputs(plan, self.recordings, out)
        self.assertTrue((out / "manifest.json").exists())
        # Corrupt the source recording so the next build must fail.
        broken_dir = self.tmp / "broken"
        broken_dir.mkdir()
        shutil.copy(self.recordings / "session-a.wav", broken_dir / "session-a.wav")
        shutil.copy(self.recordings / "session-b.wav", broken_dir / "session-b.wav")
        (broken_dir / "session-a.wav").write_bytes(b"not a wav")
        with self.assertRaises(Exception):
            build_outputs(plan, broken_dir, out)
        self.assertFalse((out / "manifest.json").exists())
        status = json.loads((out / "build-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "failed")

    def test_tampered_clip_fails_verification(self):
        plan, plan_path = self._plan()
        out = self.tmp / "out"
        build_outputs(plan, self.recordings, out)
        clip_path = out / "clips" / "session-a-0001.wav"
        with wave.open(str(clip_path), "rb") as wav:
            nchannels = wav.getnchannels()
            rate = wav.getframerate()
            nframes = wav.getnframes()
        with wave.open(str(clip_path), "wb") as wav:
            wav.setnchannels(nchannels)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(b"\x00\x00" * nchannels * nframes)
        with self.assertRaises(Exception):
            verify_outputs(plan_path, self.recordings, out)

    def test_build_rejects_plan_with_wrong_recording_length(self):
        plan, _ = self._plan()
        plan["recordings"]["session-b.wav"]["nframes"] += 1
        out = self.tmp / "out2"
        with self.assertRaises(Exception):
            build_outputs(plan, self.recordings, out)
        self.assertFalse((out / "manifest.json").exists())


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_end_to_end_cli(self):
        from clipgen.cli import main

        plan_path = self.tmp / "plan.json"
        out = self.tmp / "out"
        self.assertEqual(
            main(
                [
                    "plan",
                    "--annotations",
                    str(FIXTURES / "annotations.jsonl"),
                    "--config",
                    str(FIXTURES / "config.json"),
                    "--recordings",
                    str(FIXTURES),
                    "--plan",
                    str(plan_path),
                ]
            ),
            0,
        )
        self.assertTrue((self.tmp / "plan.json.rejected.jsonl").exists())
        self.assertEqual(
            main(
                [
                    "build",
                    "--plan",
                    str(plan_path),
                    "--recordings",
                    str(FIXTURES),
                    "--out",
                    str(out),
                ]
            ),
            0,
        )
        self.assertEqual(
            main(
                [
                    "verify",
                    "--plan",
                    str(plan_path),
                    "--recordings",
                    str(FIXTURES),
                    "--out",
                    str(out),
                ]
            ),
            0,
        )

    def test_plan_missing_input_exits_nonzero(self):
        from clipgen.cli import main

        self.assertNotEqual(
            main(
                [
                    "plan",
                    "--annotations",
                    str(self.tmp / "nope.jsonl"),
                    "--config",
                    str(FIXTURES / "config.json"),
                    "--recordings",
                    str(FIXTURES),
                    "--plan",
                    str(self.tmp / "p.json"),
                ]
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
