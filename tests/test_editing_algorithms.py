from __future__ import annotations

import array
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import editing_core
import gemini_edit_candidates as candidates
import process


def word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


def segment(start: float, end: float, text: str, words: list[dict] | None = None) -> dict:
    return {"start": start, "end": end, "text": text, "words": words or []}


class TimelineContractTests(unittest.TestCase):
    def test_edited_cut_crossing_jump_maps_to_two_source_pieces(self):
        slices = [
            {"sourceStartMs": 0, "sourceEndMs": 1000, "timeScale": 1},
            {"sourceStartMs": 3000, "sourceEndMs": 5000, "timeScale": 2},
        ]
        pieces = editing_core.edited_cut_to_source(
            {"start_ms": 800, "end_ms": 1300, "reason": "duplicate"}, slices
        )
        self.assertEqual([(round(p["start_ms"]), round(p["end_ms"])) for p in pieces], [
            (800, 1000), (3000, 3600),
        ])

    def test_edited_document_requires_exact_project_fingerprint(self):
        slices = [{"sourceStartMs": 0, "sourceEndMs": 2000, "timeScale": 1}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cuts.json"
            path.write_text(json.dumps({
                "schema_version": 2,
                "coordinate_space": "edited",
                "project_sha256": "old",
                "cuts": [{"start_ms": 100, "end_ms": 300}],
            }))
            with self.assertRaises(editing_core.CutsValidationError):
                editing_core.load_cuts_document(
                    path,
                    current_slices=slices,
                    current_project_sha256="new",
                )

    def test_activity_rejects_whole_automatic_cut(self):
        kept, rejected = editing_core.protect_cuts_with_activity(
            [{"start_ms": 1000, "end_ms": 3000}], [(1500, 1600)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(len(rejected), 1)


class PauseSafetyTests(unittest.TestCase):
    def test_input_activity_loader_maps_relative_click_time_to_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recording = root / "recording"
            recording.mkdir()
            (recording / "mouseclicks-0.json").write_text(
                json.dumps({"events": [{"timestampMs": 1200}]})
            )
            metadata = {"recorders": [{
                "type": "input",
                "sessions": [{
                    "processTimeStartMs": 5000,
                    "durationMs": 3000,
                    "mouseClicksFilename": "mouseclicks-0.json",
                }],
            }]}
            intervals = process.load_input_activity_intervals(
                root,
                metadata,
                [{
                    "processTimeStartMs": 5000,
                    "timelineOffsetMs": 10_000,
                    "durationMs": 3000,
                }],
                pad_ms=100,
            )
            self.assertEqual(intervals, [(11_100, 11_300)])

    def test_apply_cuts_does_not_silently_drop_short_remainder(self):
        slices = [{"id": "a", "sourceStartMs": 0, "sourceEndMs": 1000}]
        result, _ = process.apply_cuts(slices, [{"start_ms": 50, "end_ms": 1000}])
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["sourceStartMs"], result[0]["sourceEndMs"]), (0, 50))

    def test_wordless_slice_without_silence_is_kept(self):
        slices = [
            {"id": "a", "sourceStartMs": 0, "sourceEndMs": 1000},
            {"id": "b", "sourceStartMs": 1600, "sourceEndMs": 3600},
            {"id": "c", "sourceStartMs": 4200, "sourceEndMs": 5200},
        ]
        transcript = [
            segment(0, 1, "前", [word("前", 0, 1)]),
            segment(4.2, 5.2, "后", [word("后", 4.2, 5.2)]),
        ]
        kept, removed = process.remove_wordless_pause_slices(slices, transcript, [])
        self.assertEqual([item["id"] for item in kept], ["a", "b", "c"])
        self.assertEqual(removed, [])

    def test_silent_wordless_slice_with_activity_is_kept(self):
        slices = [{"id": "b", "sourceStartMs": 1000, "sourceEndMs": 3000}]
        kept, removed = process.remove_wordless_pause_slices(
            slices, [], [(1.0, 3.0)], [(1500, 1600)]
        )
        self.assertEqual([item["id"] for item in kept], ["b"])
        self.assertEqual(removed, [])

    def test_repeat_boundary_refinement_keeps_complete_removed_word(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            sample_rate = 16000
            samples = array.array("h", [0] * (sample_rate * 2))
            for start_s, end_s in ((0.1, 0.55), (0.7, 1.2), (1.4, 1.8)):
                for index in range(int(start_s * sample_rate), int(end_s * sample_rate)):
                    samples[index] = 9000 if index % 2 else -9000
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(samples.tobytes())
            transcript_words = [
                word("前", 0.1, 0.55), word("嗯", 0.7, 1.2), word("后", 1.4, 1.8),
            ]
            result = process.refine_repeat_cut_boundaries(
                [{"start_ms": 660, "end_ms": 1240, "removed_text": "嗯"}],
                transcript_words,
                wav_path,
                [{
                    "timelineOffsetMs": 0, "audioOffsetMs": 0,
                    "durationMs": 2000, "realDurationMs": 2000,
                }],
            )
            self.assertEqual(len(result), 1)
            self.assertLess(result[0]["start_ms"], 700)
            self.assertGreater(result[0]["end_ms"], 1200)


class CandidateRecallTests(unittest.TestCase):
    def args(self, **overrides):
        values = {
            "context_window": 4.0,
            "max_candidates": 14,
            "repeat_window": 60.0,
            "repeat_span_segments": 3,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_similarity_respects_reversed_instruction_order(self):
        score = candidates.segment_similarity("先打开设置再关闭窗口", "先关闭设置再打开窗口")
        self.assertLess(score, 0.8)

    def test_repeat_search_reaches_beyond_adjacent_segments(self):
        transcript = [
            segment(0, 2, "现在打开项目设置"),
            segment(4, 7, "这里先解释另一个选项"),
            segment(20, 22, "现在打开项目设置"),
        ]
        found = candidates.repeated_segment_candidates(
            transcript, 4.0, repeat_window=60.0, max_span_segments=2
        )
        self.assertTrue(any(item["start_ms"] == 0 and item["kept_start"] == 20 for item in found))

    def test_candidate_cap_does_not_starve_late_repeat(self):
        transcript = []
        for index in range(20):
            start = index * 3.0
            transcript.append(segment(start, start + 0.3, "嗯", [word("嗯", start, start + 0.3)]))
        transcript.extend([
            segment(70, 72, "最后检查项目设置"),
            segment(74, 76, "最后检查项目设置"),
        ])
        found = candidates.build_candidates(self.args(max_candidates=8), transcript)
        self.assertTrue(any(item["type"] == "near_duplicate_segment" and item["start_ms"] >= 70_000 for item in found))

    def test_type_specific_limit_allows_normal_duplicate_take(self):
        candidate = {
            "id": "repeat_1", "type": "near_duplicate_segment",
            "start_ms": 1000, "end_ms": 5500, "removed_text": "重复说明",
        }
        cuts = candidates.cuts_from_decisions(
            [{"id": "repeat_1", "decision": "cut", "confidence": "high"}],
            {"repeat_1": candidate},
            min_cut_ms=160,
        )
        self.assertEqual(len(cuts), 1)

    def test_duplicate_pair_can_remove_later_take(self):
        candidate = {
            "id": "repeat_1", "type": "near_duplicate_segment",
            "start_ms": 1000, "end_ms": 3000, "removed_text": "第一遍",
            "removal_options": [
                {"label": "earlier_take", "start_ms": 1000, "end_ms": 3000, "text": "第一遍"},
                {"label": "later_take", "start_ms": 5000, "end_ms": 7000, "text": "第二遍"},
            ],
        }
        cuts = candidates.cuts_from_decisions(
            [{
                "id": "repeat_1", "decision": "cut", "confidence": "high",
                "start_ms": 5000, "end_ms": 7000,
            }],
            {"repeat_1": candidate},
            min_cut_ms=160,
        )
        self.assertEqual((cuts[0]["start_ms"], cuts[0]["end_ms"]), (5000, 7000))
        self.assertEqual(cuts[0]["selected_take"], "later_take")

    def test_short_filler_is_not_destroyed_by_fixed_padding(self):
        original = [{"start_ms": 460, "end_ms": 880, "removed_text": "嗯"}]
        self.assertEqual(process.pad_repeat_cuts(original), original)


if __name__ == "__main__":
    unittest.main()
