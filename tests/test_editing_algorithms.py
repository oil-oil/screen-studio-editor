from __future__ import annotations

import array
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark_autoedit
import build_review_proxy
import editing_core
import process
import smart_edit_workflow


def word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


def segment(start: float, end: float, text: str, words: list[dict] | None = None) -> dict:
    return {"start": start, "end": end, "text": text, "words": words or []}


class ReviewProxyTests(unittest.TestCase):
    def test_frame_sampling_happens_before_scaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            recording = project / "recording"
            recording.mkdir()
            (recording / "display.mp4").touch()
            sessions = [{"outputFilename": "display.mp4", "durationMs": 1000}]
            captured: list[str] = []

            def capture(args, *, description):
                captured.extend(args)

            original = build_review_proxy.run_ffmpeg
            build_review_proxy.run_ffmpeg = capture
            try:
                build_review_proxy.build_video_proxy(
                    project, sessions, project / "proxy.mp4", width=960, height=600, fps=6.0
                )
            finally:
                build_review_proxy.run_ffmpeg = original
            filters = captured[captured.index("-filter_complex") + 1]
            self.assertLess(filters.index("fps=6"), filters.index("scale=960:600"))


class TimelineContractTests(unittest.TestCase):
    def test_benchmark_interval_score_matches_expected_overlap(self):
        result = benchmark_autoedit.score_intervals(
            [(1000, 3000), (5000, 6000)], [(2000, 4000), (5000, 7000)]
        )
        self.assertEqual(result["predicted_removed_s"], 3.0)
        self.assertEqual(result["manual_removed_s"], 4.0)
        self.assertEqual(result["overlap_s"], 2.0)
        self.assertAlmostEqual(result["time_precision"], 2 / 3, places=5)
        self.assertAlmostEqual(result["time_recall"], 0.5, places=5)

    def test_benchmark_complement_builds_manual_cut_map(self):
        self.assertEqual(
            benchmark_autoedit.complement_intervals([(1000, 3000), (4000, 6000)], 7000),
            [(0, 1000), (3000, 4000), (6000, 7000)],
        )

    def test_edited_cut_crossing_jump_maps_to_two_source_pieces(self):
        slices = [
            {"sourceStartMs": 0, "sourceEndMs": 1000, "timeScale": 1},
            {"sourceStartMs": 3000, "sourceEndMs": 5000, "timeScale": 2},
        ]
        pieces = editing_core.edited_cut_to_source(
            {"start_ms": 800, "end_ms": 1300, "reason": "agent_review"}, slices
        )
        self.assertEqual([(round(p["start_ms"]), round(p["end_ms"])) for p in pieces], [(800, 1000), (3000, 3600)])

    def test_edited_document_requires_exact_project_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cuts.json"
            path.write_text(json.dumps({
                "schema_version": 2, "coordinate_space": "edited", "project_sha256": "old",
                "cuts": [{"start_ms": 100, "end_ms": 300}],
            }))
            with self.assertRaises(editing_core.CutsValidationError):
                editing_core.load_cuts_document(
                    path, current_slices=[{"sourceStartMs": 0, "sourceEndMs": 2000, "timeScale": 1}],
                    current_project_sha256="new",
                )

    def test_activity_rejects_whole_automatic_cut(self):
        kept, rejected = editing_core.protect_cuts_with_activity(
            [{"start_ms": 1000, "end_ms": 3000}], [(1500, 1600)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 1)

    def test_audio_pause_activity_is_annotated_without_blocking_the_audio_rule(self):
        annotated = editing_core.annotate_activity_overlaps(
            [{"start_ms": 1000, "end_ms": 5000}],
            [(1800, 2200)],
        )
        self.assertEqual(len(annotated), 1)
        self.assertEqual(annotated[0]["activity_overlap_ms"], (1800, 2200))

    def test_reviewed_visual_activity_stays_protected_without_clearance(self):
        cut = {"start_ms": 1000, "end_ms": 3000, "screen_action": "unclear"}
        kept, rejected, overrides = editing_core.protect_reviewed_cuts_with_activity(
            [cut], [], [(1700, 1800)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0]["activity_source"], "visual")
        self.assertEqual(overrides, [])


class PauseSafetyTests(unittest.TestCase):
    @staticmethod
    def analysis_args(**overrides):
        values = {
            "pause_threshold": 300, "min_pause": 180, "pause_source": "silence",
            "silence_db": "auto", "silence_min_dur": 0.25, "no_vad": False,
            "no_screen_activity_protection": False, "no_visual_scan": False,
            "visual_scan_fps": 2.5, "visual_change_threshold": 0.012, "asr_backend": "local",
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_silence_pause_threshold_is_300ms_and_keeps_180ms_air(self):
        cuts = process.detect_pauses_from_silence(
            [(1.0, 1.32), (2.0, 2.29)],
            threshold_ms=300,
            min_pause_ms=180,
            segments=[],
        )
        self.assertEqual(len(cuts), 1)
        self.assertAlmostEqual(cuts[0]["duration_ms"], 320.0)
        self.assertAlmostEqual(cuts[0]["end_ms"] - cuts[0]["start_ms"], 140.0)

    def test_audio_silence_overlap_with_asr_word_does_not_veto_cut(self):
        cuts = process.detect_pauses_from_silence(
            [(1.0, 1.4)],
            threshold_ms=300,
            min_pause_ms=180,
            segments=[segment(1.0, 1.5, "轻声", [word("轻声", 1.1, 1.2)])],
        )
        self.assertEqual(len(cuts), 1)

    def test_reusable_analysis_requires_exact_transcript_and_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project_json = root / "project.json"
            transcript = root / "transcript.json"
            report_path = root / "baseline.json"
            project_json.write_text('{"project": 1}')
            transcript.write_text('[{"text": "第一版"}]')
            signature = process.analysis_cache_signature(project_json, transcript, self.analysis_args())
            report = {
                "dry_run": True, "analysis_cache_signature": signature, "reviewed_cuts_applied": [],
                "silence_regions_ms": [[100, 200]], "pauses_applied": [],
                "pauses_protected_by_activity": [], "input_activity_intervals_ms": [],
                "visual_activity_intervals_ms": [], "activity_intervals_ms": [],
            }
            report_path.write_text(json.dumps(report))
            self.assertEqual(process.load_reusable_analysis(report_path, signature), report)
            transcript.write_text('[{"text": "第二版"}]')
            changed = process.analysis_cache_signature(project_json, transcript, self.analysis_args())
            self.assertIsNone(process.load_reusable_analysis(report_path, changed))

    def test_smart_workflow_reuses_only_current_analysis_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "project.json").write_text('{"project": 1}')
            transcript = project / "transcript.json"
            transcript.write_text('[]')
            report = project / "baseline.json"
            signature = process.analysis_cache_signature(project / "project.json", transcript, self.analysis_args())
            report.write_text(json.dumps({
                "project_sha256": smart_edit_workflow.project_sha256(project),
                "analysis_cache_signature": signature,
            }))
            self.assertTrue(smart_edit_workflow.baseline_is_current(project, report, transcript, asr_backend="local"))
            report.write_text(json.dumps({"project_sha256": smart_edit_workflow.project_sha256(project)}))
            self.assertFalse(smart_edit_workflow.baseline_is_current(project, report, transcript, asr_backend="local"))

    def test_input_activity_loader_maps_relative_click_time_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording = root / "recording"
            recording.mkdir()
            (recording / "mouseclicks-0.json").write_text(json.dumps({"events": [{"timestampMs": 1200}]}))
            metadata = {"recorders": [{"type": "input", "sessions": [{
                "processTimeStartMs": 5000, "durationMs": 3000, "mouseClicksFilename": "mouseclicks-0.json",
            }]}]}
            intervals = process.load_input_activity_intervals(
                root, metadata, [{"processTimeStartMs": 5000, "timelineOffsetMs": 10000, "durationMs": 3000}], pad_ms=100
            )
            self.assertEqual(intervals, [(11100, 11300)])

    def test_apply_cuts_does_not_silently_drop_short_remainder(self):
        result, _ = process.apply_cuts(
            [{"id": "a", "sourceStartMs": 0, "sourceEndMs": 1000}],
            [{"start_ms": 50, "end_ms": 1000}],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["sourceStartMs"], result[0]["sourceEndMs"]), (0, 50))

    def test_wordless_slice_with_activity_is_kept(self):
        slices = [{"id": "b", "sourceStartMs": 1000, "sourceEndMs": 3000}]
        kept, removed = process.remove_wordless_pause_slices(slices, [], [(1.0, 3.0)], [(1500, 1600)])
        self.assertEqual([item["id"] for item in kept], ["b"])
        self.assertEqual(removed, [])

    def test_refine_repeat_cut_boundaries_keeps_complete_removed_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            sample_rate = 16000
            samples = array.array("h", [0] * (sample_rate * 2))
            for start_s, end_s in ((0.1, 0.55), (0.7, 1.2), (1.4, 1.8)):
                for index in range(int(start_s * sample_rate), int(end_s * sample_rate)):
                    samples[index] = 9000 if index % 2 else -9000
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(sample_rate); handle.writeframes(samples.tobytes())
            result = process.refine_repeat_cut_boundaries(
                [{"start_ms": 660, "end_ms": 1240, "removed_text": "嗯"}],
                [word("前", 0.1, 0.55), word("嗯", 0.7, 1.2), word("后", 1.4, 1.8)],
                wav_path, [{"timelineOffsetMs": 0, "audioOffsetMs": 0, "durationMs": 2000, "realDurationMs": 2000}],
            )
            self.assertEqual(len(result), 1)
            self.assertLess(result[0]["start_ms"], 700)
            self.assertGreater(result[0]["end_ms"], 1200)


if __name__ == "__main__":
    unittest.main()
