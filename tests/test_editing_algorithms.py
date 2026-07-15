from __future__ import annotations

import array
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import editing_core
import benchmark_autoedit
import gemini_edit_candidates as candidates
import global_edit_planner
import session_edit_planner
import consensus_edit_candidates
import preference_edit_arbiter
import structured_edit_candidates
import smart_edit_workflow
import process


def word(text: str, start: float, end: float) -> dict:
    return {"word": text, "start": start, "end": end}


def segment(start: float, end: float, text: str, words: list[dict] | None = None) -> dict:
    return {"start": start, "end": end, "text": text, "words": words or []}


class TimelineContractTests(unittest.TestCase):
    def test_benchmark_interval_score_matches_expected_overlap(self):
        result = benchmark_autoedit.score_intervals(
            [(1000, 3000), (5000, 6000)],
            [(2000, 4000), (5000, 7000)],
        )
        self.assertEqual(result["predicted_removed_s"], 3.0)
        self.assertEqual(result["manual_removed_s"], 4.0)
        self.assertEqual(result["overlap_s"], 2.0)
        self.assertAlmostEqual(result["time_precision"], 2 / 3, places=5)
        self.assertAlmostEqual(result["time_recall"], 0.5, places=5)

    def test_benchmark_complement_builds_manual_cut_map(self):
        removed = benchmark_autoedit.complement_intervals(
            [(1000, 3000), (4000, 6000)], 7000
        )
        self.assertEqual(removed, [(0, 1000), (3000, 4000), (6000, 7000)])

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

    def test_visual_none_never_overrides_click_activity(self):
        cut = {
            "start_ms": 1000, "end_ms": 3000,
            "screen_action": "none",
        }
        kept, rejected, overrides = editing_core.protect_reviewed_cuts_with_activity(
            [cut], [(1500, 1600)], [(1700, 1800)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0]["activity_source"], "input")
        self.assertEqual(overrides, [])

    def test_explicit_redundant_assessment_can_override_click_activity(self):
        cut = {
            "start_ms": 1000, "end_ms": 3000,
            "screen_action": "redundant",
            "visual_assessment": "Setup clicks return to the same state.",
        }
        kept, rejected, overrides = editing_core.protect_reviewed_cuts_with_activity(
            [cut], [(1500, 1600)], [(1700, 1800)]
        )
        self.assertEqual(kept, [cut])
        self.assertEqual(rejected, [])
        self.assertTrue(any(item["activity_source"] == "input" for item in overrides))

    def test_explicit_model_clearance_can_override_visual_only_activity(self):
        cut = {
            "start_ms": 1000, "end_ms": 3000,
            "screen_action": "redundant",
        }
        kept, rejected, overrides = editing_core.protect_reviewed_cuts_with_activity(
            [cut], [], [(1700, 1800)]
        )
        self.assertEqual(kept, [cut])
        self.assertEqual(rejected, [])
        self.assertEqual(overrides[0]["activity_source"], "visual")

    def test_unclear_model_visual_assessment_stays_protected(self):
        cut = {
            "start_ms": 1000, "end_ms": 3000,
            "screen_action": "unclear",
        }
        kept, rejected, overrides = editing_core.protect_reviewed_cuts_with_activity(
            [cut], [], [(1700, 1800)]
        )
        self.assertEqual(kept, [])
        self.assertEqual(rejected[0]["activity_source"], "visual")
        self.assertEqual(overrides, [])


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
            self.assertGreaterEqual(result[0]["start_ms"], 660)
            self.assertLessEqual(result[0]["end_ms"], 1240)

    def test_abandoned_sentence_refinement_keeps_gap_to_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            sample_rate = 16000
            samples = array.array("h", [0] * (sample_rate * 4))
            for start_s, end_s in ((0.9, 1.5), (3.0, 3.7)):
                for index in range(int(start_s * sample_rate), int(end_s * sample_rate)):
                    samples[index] = 9000 if index % 2 else -9000
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(samples.tobytes())
            result = process.refine_repeat_cut_boundaries(
                [{
                    "start_ms": 900, "end_ms": 3000,
                    "candidate_type": "possible_abandoned_sentence",
                    "removed_text": "说错的一遍",
                }],
                [word("说错的一遍", 0.9, 1.5), word("重新开始", 3.0, 3.7)],
                wav_path,
                [{
                    "timelineOffsetMs": 0, "audioOffsetMs": 0,
                    "durationMs": 4000, "realDurationMs": 4000,
                }],
            )
            self.assertEqual(len(result), 1)
            self.assertGreaterEqual(result[0]["end_ms"], 2760)
            self.assertLessEqual(result[0]["end_ms"], 3000)

    def test_structured_take_refinement_keeps_leading_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            sample_rate = 16000
            samples = array.array("h", [0] * (sample_rate * 4))
            for index in range(sample_rate, sample_rate * 2):
                samples[index] = 9000 if index % 2 else -9000
            with wave.open(str(wav_path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(samples.tobytes())
            result = process.refine_repeat_cut_boundaries(
                [{
                    "start_ms": 500, "end_ms": 3000,
                    "candidate_type": "possible_isolated_take",
                    "spoken_start_ms": 1000, "spoken_end_ms": 2000,
                }],
                [word("失败的一遍", 1.0, 2.0), word("重新开始", 3.0, 3.7)],
                wav_path,
                [{
                    "timelineOffsetMs": 0, "audioOffsetMs": 0,
                    "durationMs": 4000, "realDurationMs": 4000,
                }],
            )
            self.assertEqual(len(result), 1)
            self.assertLessEqual(result[0]["start_ms"], 740)
            self.assertGreaterEqual(result[0]["start_ms"], 500)


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

    def test_sparse_retake_spans_long_failed_demo_before_paraphrased_restart(self):
        transcript = [
            segment(0, 5, "快速模式的价格降低到五十美金"),
            segment(30, 33, "这里操作的时候出了问题"),
            segment(80, 86, "快速模式现在降价到五十美金"),
        ]
        found = candidates.sparse_retake_candidates(
            transcript, 4.0, retake_window=120.0
        )
        self.assertTrue(any(
            item["start_ms"] == 0 and item["end_ms"] == 80_000
            for item in found
        ))

    def test_sparse_retake_rejects_dense_unique_explanation(self):
        transcript = [
            segment(0, 8, "没有人会把这个模型当做编程agent的基座模型"),
            segment(8.1, 18, "因为它的指令遵循性很差还会直接修改代码"),
            segment(18.1, 28, "对话轮数较高之后输出的内容也会变得很奇怪"),
            segment(30, 36, "很少有人会把这个模型当做编程agent的基座模型"),
        ]
        found = candidates.sparse_retake_candidates(
            transcript, 4.0, retake_window=120.0
        )
        self.assertEqual(found, [])

    def test_tail_restart_removes_only_repeated_sentence_tail(self):
        transcript = [
            segment(0, 8, "完整说明之后我把我的这个目录给它拉进来", [
                word("完整说明", 0, 2), word("之后", 2, 3),
                word("我把", 3, 3.5), word("我的", 3.5, 4), word("这个", 4, 4.5),
                word("目录", 4.5, 5), word("给", 5, 5.2), word("它", 5.2, 5.4),
                word("拉", 5.4, 5.6), word("进来", 5.6, 6),
            ]),
            segment(9, 10, "拖拽进来", [
                word("拖拽", 9, 9.5), word("进来", 9.5, 10), word("啊", 10, 10.1),
            ]),
            segment(13, 15, "我把我的这个目录拉进来", [
                word("我把", 13, 13.3), word("我的", 13.3, 13.6),
                word("这个", 13.6, 13.9), word("目录", 13.9, 14.2),
                word("拉", 14.2, 14.5), word("进来", 14.5, 15),
            ]),
        ]
        found = candidates.tail_restart_candidates(transcript, 4.0)
        self.assertTrue(any(
            item["start_ms"] == 3000 and item["end_ms"] == 13_000
            for item in found
        ))

    def test_paused_immediate_reformulation_becomes_narrow_candidate(self):
        transcript = [
            segment(0, 6, "就这个cursor自己的cursor就是cursor自己的这个composer", [
                word("就", 0, .2), word("这个", .2, .4), word("cursor", .4, .8),
                word("自己的", .8, 1.2), word("cursor", 1.2, 1.6),
                word("就是", 2.2, 2.5), word("cursor", 2.5, 2.9),
                word("自己的", 2.9, 3.3), word("这个", 3.3, 3.6),
                word("composer", 3.6, 4.2),
            ]),
        ]
        found = candidates.immediate_repair_candidates(
            candidates.flatten_words(transcript), transcript, 4.0
        )
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["start_ms"], found[0]["end_ms"]), (0, 2200))

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

    def test_abandoned_sentence_includes_gap_before_clean_restart(self):
        transcript = [
            segment(10, 16, "核心原理就是我们先使用子 agent 去啊。", [
                word("核心", 10, 11), word("去", 15.2, 15.6), word("啊。", 15.6, 16),
            ]),
            segment(19.2, 25, "然后我重新完整解释核心原理"),
        ]
        found = candidates.abandoned_sentence_candidates(transcript, 4.0)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["start_ms"], found[0]["end_ms"]), (10_000, 19_200))

    def test_asr_split_continuation_is_not_abandoned_sentence(self):
        transcript = [
            segment(10, 14, "我的这个线程可能它的呃。", [
                word("我的", 10, 10.5), word("呃。", 13.5, 14),
            ]),
            segment(14.8, 18, "大小已经达到三十几M了。"),
        ]
        found = candidates.abandoned_sentence_candidates(transcript, 4.0)
        self.assertEqual(found, [])

    def test_abandoned_stub_expands_to_the_failed_question(self):
        transcript = [
            segment(10, 15, "第一次没有问好的问题"),
            segment(15.1, 15.8, "就是。", [word("就是。", 15.1, 15.8)]),
            segment(30, 35, "那我重新问一次完整的问题"),
        ]
        found = candidates.abandoned_sentence_candidates(transcript, 4.0)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["start_ms"], found[0]["end_ms"]), (10_000, 30_000))

    def test_isolated_take_with_adjacent_repeat_gets_structural_support(self):
        transcript = [
            segment(0, 4, "第一次演示创建幻灯片"),
            segment(7, 10, "演示创建幻灯片时说错了"),
            segment(13, 17, "重新演示创建幻灯片"),
        ]
        supporting = [{
            "id": "repeat_1", "type": "near_duplicate_segment",
            "start_ms": 0, "end_ms": 4000, "duration_ms": 4000,
            "kept_start": 13.0,
        }]
        found = candidates.isolated_take_candidates(
            transcript, 4.0, supporting, gap_s=2.5
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["supporting_repeat_id"], "repeat_1")
        self.assertEqual(found[0]["absorbed_repeat_id"], "repeat_1")
        self.assertEqual(found[0]["start_ms"], 0)

    def test_unsupported_isolated_take_cannot_auto_cut(self):
        candidate = {
            "id": "island_1", "type": "possible_isolated_take",
            "start_ms": 1000, "end_ms": 9000, "duration_ms": 8000,
            "restart_similarity": 0.1, "supporting_repeat_id": None,
        }
        cuts = candidates.cuts_from_decisions(
            [{"id": "island_1", "decision": "cut", "confidence": "high"}],
            {"island_1": candidate}, min_cut_ms=160,
        )
        self.assertEqual(cuts, [])

    def test_explicit_restart_island_can_cut_with_redundant_screen_clearance(self):
        candidate = {
            "id": "island_1", "type": "possible_isolated_take",
            "start_ms": 1000, "end_ms": 9000, "duration_ms": 8000,
            "restart_similarity": 0.0, "supporting_repeat_id": None,
            "removed_text": "不要看，重新来。",
        }
        cuts = candidates.cuts_from_decisions(
            [{
                "id": "island_1", "decision": "cut", "confidence": "high",
                "screen_action": "redundant", "visual_assessment": "Setup only.",
            }],
            {"island_1": candidate}, min_cut_ms=160,
        )
        self.assertEqual(len(cuts), 1)

    def test_low_similarity_island_in_supported_retake_chain_can_cut(self):
        source = {
            "island_1": {
                "id": "island_1", "type": "possible_isolated_take",
                "start_ms": 1000, "end_ms": 9000, "duration_ms": 8000,
                "restart_similarity": 0.4, "supporting_repeat_id": None,
                "removed_text": "失败的第一段",
            },
            "island_2": {
                "id": "island_2", "type": "possible_isolated_take",
                "start_ms": 11000, "end_ms": 19000, "duration_ms": 8000,
                "restart_similarity": 0.0, "supporting_repeat_id": None,
                "removed_text": "同一次失败录制的后续设置",
            },
        }
        decisions = [{
            "id": candidate_id, "decision": "cut", "confidence": "high",
            "screen_action": "redundant", "visual_assessment": "Redundant setup.",
        } for candidate_id in source]
        cuts = candidates.cuts_from_decisions(
            decisions, source, min_cut_ms=160,
        )
        self.assertEqual(
            {item["candidate_id"] for item in cuts}, {"island_1", "island_2"}
        )

    def test_filler_group_does_not_cross_semantic_words(self):
        transcript = [
            segment(10, 11.5, "这个呃当前的这个", [
                word("这个", 10, 10.3),
                word("呃", 10.3, 10.6),
                word("当前", 10.6, 11.0),
                word("的", 11.0, 11.2),
                word("这个", 11.2, 11.5),
            ]),
        ]
        found = candidates.group_nearby_fillers(
            candidates.flatten_words(transcript), transcript, 4.0
        )
        strong = [item for item in found if item.get("contains_strong_hesitation")]
        self.assertEqual(len(strong), 1)
        self.assertEqual(strong[0]["removed_text"], "呃")
        self.assertFalse(any(
            item["start_ms"] <= 10_300 and item["end_ms"] >= 11_200
            for item in found
        ))

    def test_default_candidate_build_skips_weak_discourse_fillers(self):
        transcript = [
            segment(0, 1.2, "这个然后呃", [
                word("这个", 0, 0.3), word("然后", 0.35, 0.75), word("呃", 0.8, 1.2),
            ]),
        ]
        found = candidates.build_candidates(self.args(max_candidates=20), transcript)
        filler_texts = {
            item["removed_text"] for item in found
            if item["type"] in {"hard_filler", "soft_filler", "filler_cluster"}
        }
        self.assertEqual(filler_texts, {"呃"})

    def test_only_isolated_filler_is_cut_by_local_gate(self):
        transcript = [
            segment(0, 1.2, "前面呃后面", [
                word("前面", 0.0, 0.2),
                word("呃", 0.4, 0.7),
                word("后面", 0.9, 1.2),
            ]),
        ]
        found = candidates.group_nearby_fillers(
            candidates.flatten_words(transcript), transcript, 4.0
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "activity.json"
            report.write_text(json.dumps({"input_activity_intervals_ms": []}))
            decisions, remaining = candidates.conservative_local_filler_decisions(
                found, report
            )
        self.assertEqual(remaining, [])
        self.assertEqual(decisions[0]["decision"], "cut")
        self.assertEqual(decisions[0]["reason"], "local_easy_filler")

    def test_connected_filler_is_preserved_without_model_call(self):
        transcript = [
            segment(0, 0.8, "前面呃后面", [
                word("前面", 0.0, 0.2),
                word("呃", 0.2, 0.5),
                word("后面", 0.5, 0.8),
            ]),
        ]
        found = candidates.group_nearby_fillers(
            candidates.flatten_words(transcript), transcript, 4.0
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "activity.json"
            report.write_text(json.dumps({"input_activity_intervals_ms": []}))
            decisions, remaining = candidates.conservative_local_filler_decisions(
                found, report
            )
        self.assertEqual(remaining, [])
        self.assertEqual(decisions[0]["decision"], "keep")
        self.assertIn("no_clean_leading_gap", decisions[0]["local_blockers"])
        self.assertIn("no_clean_trailing_gap", decisions[0]["local_blockers"])

    def test_input_activity_blocks_otherwise_easy_local_filler(self):
        transcript = [
            segment(0, 1.2, "前面呃后面", [
                word("前面", 0.0, 0.2),
                word("呃", 0.4, 0.7),
                word("后面", 0.9, 1.2),
            ]),
        ]
        found = candidates.group_nearby_fillers(
            candidates.flatten_words(transcript), transcript, 4.0
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "activity.json"
            report.write_text(json.dumps({
                "input_activity_intervals_ms": [[450, 550]],
            }))
            decisions, _ = candidates.conservative_local_filler_decisions(found, report)
        self.assertEqual(decisions[0]["decision"], "keep")
        self.assertIn("input_activity_overlap", decisions[0]["local_blockers"])

    def test_weak_filler_cannot_become_automatic_cut(self):
        candidate = {
            "id": "soft_1", "type": "soft_filler",
            "start_ms": 1000, "end_ms": 1400, "removed_text": "这个",
            "contains_strong_hesitation": False,
        }
        cuts = candidates.cuts_from_decisions(
            [{"id": "soft_1", "decision": "cut", "confidence": "high"}],
            {"soft_1": candidate},
            min_cut_ms=160,
        )
        self.assertEqual(cuts, [])

    def test_marker_only_self_correction_cannot_become_automatic_cut(self):
        candidate = {
            "id": "repair_1", "type": "explicit_self_correction",
            "start_ms": 1000, "end_ms": 2400,
            "removed_text": "前面的解释，也就是说",
            "repair_marker": "也就是说", "auto_safe": False,
        }
        cuts = candidates.cuts_from_decisions(
            [{"id": "repair_1", "decision": "cut", "confidence": "high"}],
            {"repair_1": candidate},
            min_cut_ms=160,
        )
        self.assertEqual(cuts, [])

    def test_weak_repair_marker_is_preserved_locally_but_restart_is_reviewed(self):
        weak = {
            "id": "repair_1", "type": "explicit_self_correction",
            "start_ms": 1000, "end_ms": 2000, "removed_text": "也就是说",
        }
        restart = {
            "id": "repair_2", "type": "explicit_self_correction",
            "start_ms": 3000, "end_ms": 4000, "removed_text": "不要看，重新来",
        }
        long_gap = {
            "id": "repair_3", "type": "explicit_self_correction",
            "start_ms": 5000, "end_ms": 9000, "removed_text": "也就是说",
            "max_internal_gap_ms": 1800,
        }
        local, review = candidates.conservative_local_advisory_decisions(
            [weak, restart, long_gap]
        )
        self.assertEqual([item["id"] for item in local], ["repair_1"])
        self.assertEqual([item["id"] for item in review], ["repair_2", "repair_3"])

    def test_explicit_restart_correction_can_cut_after_multimodal_clearance(self):
        candidate = {
            "id": "repair_1", "type": "explicit_self_correction",
            "start_ms": 1000, "end_ms": 2400,
            "removed_text": "不要看，重新来",
            "repair_marker": "重新来", "auto_safe": False,
        }
        cuts = candidates.cuts_from_decisions(
            [{
                "id": "repair_1", "decision": "cut", "confidence": "high",
                "screen_action": "redundant", "visual_assessment": "Failed take only.",
            }],
            {"repair_1": candidate},
            min_cut_ms=160,
        )
        self.assertEqual(len(cuts), 1)

    def test_sparse_retake_requires_redundant_visual_clearance(self):
        candidate = {
            "id": "sparse_1", "type": "possible_sparse_retake",
            "start_ms": 1000, "end_ms": 81000, "duration_ms": 80000,
            "similarity": 0.5, "speech_density": 0.2, "removed_text": "失败演示",
        }
        blocked = candidates.cuts_from_decisions(
            [{
                "id": "sparse_1", "decision": "cut", "confidence": "high",
                "screen_action": "meaningful", "visual_assessment": "Unique result.",
            }],
            {"sparse_1": candidate}, min_cut_ms=160,
        )
        allowed = candidates.cuts_from_decisions(
            [{
                "id": "sparse_1", "decision": "cut", "confidence": "high",
                "screen_action": "redundant", "visual_assessment": "Failed demo only.",
            }],
            {"sparse_1": candidate}, min_cut_ms=160,
        )
        self.assertEqual(blocked, [])
        self.assertEqual(len(allowed), 1)


class PreferenceArbiterTests(unittest.TestCase):
    def test_arbiter_retries_one_empty_structured_response(self):
        responses = [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": '{"decisions": []}'}}]},
        ]
        with mock.patch.object(
            preference_edit_arbiter,
            "post_json",
            side_effect=responses,
        ) as post:
            response, parsed = preference_edit_arbiter.request_arbitration(
                "https://example.invalid", {}, "secret", 30
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual(parsed, {"decisions": []})
        self.assertEqual(response, responses[1])

    def test_arbiter_fallback_separates_speech_and_screen_pauses(self):
        batches = preference_edit_arbiter.fallback_candidate_batches([
            {"id": "speech", "planner_category": "abandoned_take"},
            {"id": "pause", "planner_category": "screen_pause"},
            {"id": "speech2", "planner_category": "duplicate_take"},
        ])

        self.assertEqual(
            [[item["id"] for item in batch] for batch in batches],
            [["speech", "speech2"], ["pause"]],
        )

    def test_session_planner_exposes_real_recording_boundaries(self):
        atoms = [
            {"id": "U0001", "start": 1.0, "end": 2.0, "text": "第一遍"},
            {"id": "U0002", "start": 6.0, "end": 7.0, "text": "第二遍"},
        ]
        sessions = [
            {"id": "S01", "start": 0.0, "end": 5.0},
            {"id": "S02", "start": 5.0, "end": 10.0},
        ]

        rows = session_edit_planner.transcript_rows(atoms, sessions)
        prompt = session_edit_planner.build_prompt(atoms, sessions)

        self.assertIn("=== SESSION S01 0.000-5.000 ===", rows)
        self.assertIn("[U0002 S02 6.000-7.000] 第二遍", rows)
        self.assertIn("whole session", prompt)

    def test_session_intervals_use_cumulative_source_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            recording = project / "recording"
            recording.mkdir()
            (recording / "metadata.json").write_text(json.dumps({
                "recorders": [{
                    "type": "microphone",
                    "sessions": [
                        {"durationMs": 2500, "processTimeStartMs": 100},
                        {"durationMs": 4000, "processTimeStartMs": 5000},
                    ],
                }],
            }))

            sessions = session_edit_planner.session_intervals(project)

            self.assertEqual(sessions, [
                {"id": "S01", "start": 0.0, "end": 2.5},
                {"id": "S02", "start": 2.5, "end": 6.5},
            ])

    def test_preference_arbiter_loads_long_session_hypothesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "baseline-report.transcript.edit.json").write_text(
                json.dumps({"segments": [
                    {"start": 0.0, "end": 200.0, "text": "较长的失败录制"},
                ]})
            )
            (project / "session-text-planner-gemini35flash-v1.json").write_text(
                json.dumps({"candidates": [{
                    "start": 5.0,
                    "end": 185.0,
                    "planner_category": "abandoned_take",
                    "removed_text": "较长的失败录制",
                    "kept_text": "后续干净版本",
                }]})
            )

            rows = preference_edit_arbiter.candidate_rows(project)

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["duration_ms"], 180_000)

    def test_short_speech_needs_structural_replacement_before_auto_cut(self):
        unrelated = {
            "start": 10.0,
            "end": 12.8,
            "duration_ms": 2800,
            "planner_category": "abandoned_take",
            "removed_text": "我们在内置，呃，浏览器调试，呃",
            "kept_text": "做这些自动化工作会更加方便",
        }
        exact_restart = {
            **unrelated,
            "removed_text": "然后除此以外",
            "kept_text": "然后除此以外它还增加了一个档位",
        }
        long_take = {**unrelated, "duration_ms": 5000}

        self.assertEqual(
            preference_edit_arbiter.automatic_safety_blocker(unrelated),
            "short_speech_without_structural_replacement",
        )
        self.assertIsNone(
            preference_edit_arbiter.automatic_safety_blocker(exact_restart)
        )
        self.assertIsNone(preference_edit_arbiter.automatic_safety_blocker(long_take))

    def test_video_must_explicitly_clear_screen_pause_activity(self):
        candidate = {
            "start": 10.0,
            "end": 20.0,
            "planner_category": "screen_pause",
            "video_review_supplied": True,
            "screen_action": "meaningful",
            "visual_assessment": "The result is being demonstrated.",
        }
        self.assertEqual(
            preference_edit_arbiter.automatic_safety_blocker(candidate),
            "video_did_not_clear_screen_activity",
        )
        candidate.update({
            "screen_action": "none",
            "visual_assessment": "The page is static with no unique action or result.",
        })
        self.assertIsNone(
            preference_edit_arbiter.automatic_safety_blocker(candidate)
        )

    def test_structured_micro_gate_keeps_only_long_isolated_filler_and_exact_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript = root / "transcript.json"
            activity = root / "activity.json"
            transcript.write_text(json.dumps({"segments": [
                {"start": 0.0, "end": 2.0, "text": "前呃后", "words": [
                    word("前", 0.0, 0.4), word("呃", 0.7, 1.2), word("后", 1.5, 2.0),
                ]},
                {"start": 3.0, "end": 4.0, "text": "好这个是混元三", "words": [
                    word("好", 3.0, 3.2), word("这个", 3.2, 3.5),
                    word("是", 3.5, 3.7), word("混元三", 3.7, 4.0),
                ]},
                {"start": 5.0, "end": 6.0, "text": "好这个是混元三", "words": [
                    word("好", 5.0, 5.2), word("这个", 5.2, 5.5),
                    word("是", 5.5, 5.7), word("混元三", 5.7, 6.0),
                ]},
                {"start": 7.0, "end": 8.4, "text": "又呃来", "words": [
                    word("又", 7.0, 7.3), word("呃", 7.6, 7.92),
                    word("来", 8.2, 8.4),
                ]},
            ]}))
            activity.write_text(json.dumps({"input_activity_intervals_ms": []}))

            candidates = structured_edit_candidates.build_structured_candidates(
                transcript, activity
            )

            self.assertEqual(
                [item["detector_type"] for item in candidates],
                ["hard_filler", "possible_tail_restart"],
            )
            self.assertEqual(candidates[0]["spoken_duration_ms"], 500)
            self.assertEqual(candidates[1]["similarity"], 1.0)

    def test_speech_labels_do_not_treat_removed_silence_as_removed_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "val2-sample.screenstudio"
            project.mkdir()
            (project / "benchmark-ground-truth.json").write_text(json.dumps({
                "source_project": "/original/sample.screenstudio",
                "manual_cut_intervals_ms": [[2000, 5000]],
            }))
            (project / "baseline-report.transcript.edit.json").write_text(json.dumps({
                "segments": [{
                    "start": 1.0, "end": 2.0, "text": "这句话被保留",
                    "words": [word("这句话被保留", 1.0, 2.0)],
                }]
            }))
            (project / "structured-edit-candidates-v1.json").write_text(json.dumps({
                "candidates": [{
                    "start": 1.0, "end": 5.0,
                    "planner_category": "abandoned_take",
                    "detector_type": "possible_isolated_take",
                    "removed_text": "这句话被保留",
                }]
            }))

            result = preference_edit_arbiter.build_preferences(
                root, candidate_source="structured"
            )

            self.assertEqual(result["examples"][0]["overlap_fraction"], 0.75)
            self.assertEqual(result["examples"][0]["speech_overlap_fraction"], 0.0)
            self.assertEqual(result["examples"][0]["label"], "keep")

    def test_smart_cuts_include_nonoverlapping_local_micro_candidate(self):
        document = smart_edit_workflow.cuts_document(
            Path("/tmp/example.screenstudio"),
            {"project_sha256": "abc"},
            {"candidates": [{
                "start_ms": 1000, "end_ms": 2500,
                "planner_category": "abandoned_take",
            }]},
            [
                {
                    "start_ms": 1200, "end_ms": 2000,
                    "detector_type": "possible_tail_restart",
                },
                {
                    "start_ms": 3000, "end_ms": 3600,
                    "detector_type": "hard_filler",
                    "spoken_start_ms": 3040, "spoken_end_ms": 3520,
                },
            ],
        )
        self.assertEqual(len(document["cuts"]), 2)
        self.assertTrue(document["cuts"][1]["local_micro_decision"])
        self.assertEqual(document["cuts"][1]["candidate_type"], "hard_filler")
        self.assertEqual(document["cuts"][1]["spoken_start_ms"], 3040)

    def test_smart_cut_preserves_multimodal_activity_clearance(self):
        cut = smart_edit_workflow.candidate_cut({
            "start_ms": 1000,
            "end_ms": 5000,
            "planner_category": "screen_pause",
            "screen_action": "redundant",
            "visual_assessment": "Only a loading spinner changes.",
        })
        self.assertEqual(cut["screen_action"], "redundant")
        self.assertEqual(
            cut["visual_assessment"], "Only a loading spinner changes."
        )

    def test_smart_cut_preserves_candidate_provenance(self):
        cut = smart_edit_workflow.candidate_cut({
            "start_ms": 1000,
            "end_ms": 5000,
            "planner_category": "abandoned_take",
            "source_report": "session-text-planner-gemini35flash-v1.json",
        })
        self.assertEqual(
            cut["source_report"],
            "session-text-planner-gemini35flash-v1.json",
        )

    def test_final_audit_cache_requires_exact_signature(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "audit.json"
            report.write_text(json.dumps({
                "smart_edit_audit_signature": "current",
            }))

            self.assertTrue(
                smart_edit_workflow.final_audit_is_current(report, "current")
            )
            self.assertFalse(
                smart_edit_workflow.final_audit_is_current(report, "changed")
            )

    def test_default_reviews_screen_active_pauses_from_two_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "baseline-report.transcript.edit.json").write_text(
                json.dumps({"segments": [
                    {"start": 0.0, "end": 1.0, "text": "前一句"},
                    {"start": 6.0, "end": 7.0, "text": "后一句"},
                ]})
            )
            (project / "baseline-report.json").write_text(json.dumps({
                "pauses_protected_by_activity": [
                    {"start_ms": 1200, "end_ms": 3000, "duration_ms": 1800},
                    {"start_ms": 3200, "end_ms": 5700, "duration_ms": 2500},
                ]
            }))

            rows = preference_edit_arbiter.candidate_rows(project)

            self.assertEqual(preference_edit_arbiter.PROTECTED_PAUSE_MIN_MS, 2_000.0)
            self.assertEqual([(row["start_ms"], row["end_ms"]) for row in rows], [
                (3200, 5700),
            ])

    def test_smart_workflow_writes_source_time_cuts(self):
        document = smart_edit_workflow.cuts_document(
            Path("/tmp/example.screenstudio"),
            {"project_sha256": "abc"},
            {"candidates": [{
                "start_ms": 1000,
                "end_ms": 2500,
                "planner_category": "abandoned_take",
                "removed_text": "错的一遍",
                "kept_text": "正确的一遍",
            }]},
        )
        self.assertEqual(document["coordinate_space"], "source")
        self.assertEqual(document["project_sha256"], "abc")
        self.assertEqual(document["cuts"][0]["start_ms"], 1000)

    def test_continuous_visual_without_input_is_not_auto_cut(self):
        blocker = preference_edit_arbiter.automatic_safety_blocker({
            "planner_category": "screen_pause",
            "visual_activity_fraction": 0.96,
            "input_activity_fraction": 0.0,
        })
        self.assertEqual(blocker, "continuous_visual_without_input")

    def test_preferences_are_labeled_from_creator_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "val2-sample.screenstudio"
            project.mkdir()
            (project / "benchmark-ground-truth.json").write_text(json.dumps({
                "source_project": "/original/sample.screenstudio",
                "manual_cut_intervals_ms": [[1000, 2000]],
            }))
            (project / "baseline-report.transcript.edit.json").write_text(json.dumps({
                "segments": [
                    {"start": 0.0, "end": 2.5, "text": "先说错一遍，再重新说。"},
                    {"start": 2.5, "end": 3.0, "text": "这里"},
                    {"start": 4.0, "end": 5.0, "text": "保留给观众看。"},
                ]
            }))
            (project / "global-video-planner-gemini35flash-v4.json").write_text(json.dumps({
                "candidates": [
                    {"start": 1.0, "end": 2.0, "planner_category": "retake", "removed_text": "说错一遍"},
                    {"start": 3.0, "end": 4.0, "planner_category": "screen_pause", "removed_text": "[screen pause]"},
                ]
            }))

            result = preference_edit_arbiter.build_preferences(root)

            self.assertEqual(result["example_count"], 2)
            self.assertEqual([item["label"] for item in result["examples"]], ["cut", "keep"])
            self.assertTrue(all(item["source_project"] == "/original/sample.screenstudio" for item in result["examples"]))


class BailianReviewerTests(unittest.TestCase):
    def test_json_extractor_ignores_text_after_first_complete_object(self):
        parsed = candidates.extract_json_from_text(
            '{"decisions": []}\nAdditional non-JSON explanation.'
        )
        self.assertEqual(parsed, {"decisions": []})

    def test_visual_pause_is_narrowed_to_transcript_grounded_silence(self):
        atoms = [
            {"id": "U0001", "start": 1.0, "end": 3.0, "text": "前一句"},
            {"id": "U0002", "start": 5.0, "end": 8.0, "text": "后一句"},
        ]
        plan = {"edits": [{
            "remove_start_s": 2.0,
            "remove_end_s": 7.0,
            "category": "screen_pause",
            "confidence": "high",
            "reason": "visual model proposed an over-wide pause",
        }]}

        planned, rejected = global_edit_planner.candidates_from_plan(
            plan, atoms, model="test", max_candidate_ms=90_000
        )

        self.assertEqual(rejected, [])
        self.assertEqual(len(planned), 1)
        self.assertAlmostEqual(planned[0]["start"], 3.08)
        self.assertAlmostEqual(planned[0]["end"], 4.92)

    def test_global_planner_uses_only_valid_external_replacements(self):
        atoms = [
            {"id": "U0001", "start": 1.0, "end": 2.0, "text": "第一遍"},
            {"id": "U0002", "start": 3.0, "end": 4.0, "text": "重来"},
            {"id": "U0003", "start": 6.0, "end": 8.0, "text": "干净版本"},
        ]
        plan = {"edits": [{
            "remove_start_id": "U0001",
            "remove_end_id": "U0002",
            "cut_until_id": "U0003",
            "replacement_ids": ["U0003"],
            "removed_quote": "第一遍",
            "replacement_quote": "干净版本",
            "category": "explicit_restart",
            "confidence": "high",
            "reason": "The speaker explicitly restarts.",
        }]}
        planned, rejected = global_edit_planner.candidates_from_plan(
            plan, atoms, model="test", max_candidate_ms=90_000
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["start_ms"], 1000)
        self.assertEqual(planned[0]["end_ms"], 6000)
        self.assertEqual(planned[0]["kept_text"], "干净版本")

    def test_global_planner_rejects_reason_timestamp_misalignment(self):
        atoms = [
            {"id": "U0001", "start": 1.0, "end": 2.0, "text": "实际第一句"},
            {"id": "U0002", "start": 3.0, "end": 4.0, "text": "实际第二句"},
        ]
        plan = {"edits": [{
            "remove_start_id": "U0001",
            "remove_end_id": "U0001",
            "replacement_ids": ["U0002"],
            "removed_quote": "模型理由里说的另一句话",
            "replacement_quote": "实际第二句",
            "category": "self_correction",
            "confidence": "high",
            "reason": "Reason refers to a different timestamp.",
        }]}
        planned, rejected = global_edit_planner.candidates_from_plan(
            plan, atoms, model="test", max_candidate_ms=90_000
        )
        self.assertEqual(planned, [])
        self.assertEqual(rejected[0]["reason"], "removed_quote_mismatch")

    def test_cross_model_consensus_keeps_only_overlapping_candidate(self):
        primary = {
            "transcript": "/tmp/t.json",
            "candidates": [
                {"id": "p1", "start": 10.0, "end": 20.0, "planner_category": "screen_pause"},
                {"id": "p2", "start": 30.0, "end": 35.0, "planner_category": "abandoned_take"},
            ],
        }
        support = {
            "transcript": "/tmp/t.json",
            "model": "support",
            "candidates": [
                {"id": "s1", "start": 12.0, "end": 19.0, "planner_category": "screen_pause"},
            ],
        }
        result = consensus_edit_candidates.consensus_candidates(
            primary, [support], min_overlap=0.5
        )
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["start"], result[0]["end"]), (12.0, 19.0))

    def test_extra_global_candidates_are_loaded_as_hypotheses(self):
        transcript = [
            segment(0.0, 2.0, "前文"),
            segment(2.0, 5.0, "重读"),
            segment(5.0, 8.0, "后文"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript_path = Path(tmp) / "transcript.json"
            transcript_path.write_text("[]", encoding="utf-8")
            report_path = Path(tmp) / "planner.json"
            report_path.write_text(json.dumps({
                "transcript": str(transcript_path),
                "candidates": [{
                    "id": "global_001",
                    "type": "global_paper_edit",
                    "start": 2.0,
                    "end": 5.0,
                    "removed_text": "重读",
                }],
            }), encoding="utf-8")
            loaded = candidates.load_extra_candidates(
                [report_path], transcript, 4.0, transcript_path
            )
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "global1_001")
        self.assertIn("重读", loaded[0]["context"])

    def test_default_preserves_screen_active_pauses_under_six_seconds(self):
        source = [
            {
                "id": "difficult", "type": "protected_pause",
                "start_ms": 0, "end_ms": 5500, "duration_ms": 5500,
            },
            {
                "id": "long", "type": "protected_pause",
                "start_ms": 7000, "end_ms": 13500, "duration_ms": 6500,
            },
        ]
        local, review = candidates.conservative_local_advisory_decisions(source)
        self.assertEqual([item["id"] for item in local], ["difficult"])
        self.assertEqual([item["id"] for item in review], ["long"])

    def test_short_screen_active_pause_is_preserved_without_model_review(self):
        source = [
            {
                "id": "short", "type": "protected_pause",
                "start_ms": 0, "end_ms": 2000, "duration_ms": 2000,
            },
            {
                "id": "long", "type": "protected_pause",
                "start_ms": 3000, "end_ms": 7000, "duration_ms": 4000,
            },
        ]
        local, review = candidates.conservative_local_advisory_decisions(
            source, protected_pause_min_review_ms=3000
        )
        self.assertEqual([item["id"] for item in local], ["short"])
        self.assertEqual(local[0]["decision"], "keep")
        self.assertEqual([item["id"] for item in review], ["long"])

    def test_cached_bailian_batch_requires_exact_candidate_ids(self):
        source = [
            {"id": "a", "type": "protected_pause", "start_ms": 0, "end_ms": 100},
            {"id": "b", "type": "protected_pause", "start_ms": 200, "end_ms": 300},
        ]
        response = {
            "candidate_signature": candidates.candidate_batch_signature(source),
            "text": json.dumps({"decisions": [
                {"id": "a", "decision": "keep", "confidence": "high"},
            ]}),
        }
        with self.assertRaises(ValueError):
            candidates.parse_cached_bailian_response(
                response, source, require_screen_action=False
            )

    def test_cached_bailian_batch_reuses_complete_response(self):
        source = [
            {"id": "a", "type": "protected_pause", "start_ms": 0, "end_ms": 100},
        ]
        response = {
            "candidate_ids": ["a"],
            "candidate_signature": candidates.candidate_batch_signature(source),
            "usage": {"total_tokens": 12},
            "text": json.dumps({"decisions": [
                {"id": "a", "decision": "keep", "confidence": "high"},
            ]}),
        }
        parsed = candidates.parse_cached_bailian_response(
            response, source, require_screen_action=False
        )
        self.assertEqual(parsed["decisions"][0]["id"], "a")
        self.assertEqual(parsed["usage"]["total_tokens"], 12)

    def test_long_candidate_isolated_from_review_batch(self):
        source = [
            {"id": "short_1", "start_ms": 0, "end_ms": 1000, "duration_ms": 1000},
            {"id": "long_1", "start_ms": 2000, "end_ms": 22000, "duration_ms": 20000},
            {"id": "short_2", "start_ms": 23000, "end_ms": 24000, "duration_ms": 1000},
        ]
        batches = candidates.review_batches(
            source,
            max_count=3,
            isolate_duration_ms=15000,
        )
        self.assertEqual(
            [[item["id"] for item in batch] for batch in batches],
            [["short_1"], ["long_1"], ["short_2"]],
        )

    def test_semantic_audit_selects_only_high_confidence_long_cuts(self):
        source = [
            {"id": "long", "start_ms": 0, "end_ms": 20000, "duration_ms": 20000},
            {"id": "short", "start_ms": 21000, "end_ms": 22000, "duration_ms": 1000},
            {"id": "kept", "start_ms": 23000, "end_ms": 43000, "duration_ms": 20000},
        ]
        decisions = [
            {"id": "long", "decision": "cut", "confidence": "high"},
            {"id": "short", "decision": "cut", "confidence": "high"},
            {"id": "kept", "decision": "keep", "confidence": "high"},
        ]
        selected = candidates.semantic_audit_candidates(
            source, decisions, mode="long-cuts", min_cut_ms=15000
        )
        self.assertEqual([item["id"] for item in selected], ["long"])

    def test_semantic_audit_also_routes_structural_and_long_active_risks(self):
        source = [
            {
                "id": "island", "type": "possible_isolated_take",
                "start_ms": 0, "end_ms": 2000, "duration_ms": 2000,
            },
            {
                "id": "active", "type": "protected_pause",
                "start_ms": 3000, "end_ms": 9000, "duration_ms": 6000,
            },
            {
                "id": "short_active", "type": "protected_pause",
                "start_ms": 10000, "end_ms": 12000, "duration_ms": 2000,
            },
        ]
        decisions = [
            {"id": item["id"], "decision": "cut", "confidence": "high"}
            for item in source
        ]
        selected = candidates.semantic_audit_candidates(
            source,
            decisions,
            mode="long-cuts",
            min_cut_ms=15000,
            protected_pause_min_ms=5000,
        )
        self.assertEqual([item["id"] for item in selected], ["island", "active"])

    def test_semantic_audit_can_veto_but_never_create_cut(self):
        original = [
            {"id": "unsafe", "decision": "cut", "confidence": "high", "note": "Omni cut."},
            {"id": "safe", "decision": "cut", "confidence": "high"},
            {"id": "already_kept", "decision": "keep", "confidence": "high"},
        ]
        audit = [
            {
                "id": "unsafe", "decision": "keep", "confidence": "high",
                "screen_action": "redundant", "note": "Unique explanation.",
            },
            {
                "id": "safe", "decision": "cut", "confidence": "high",
                "screen_action": "redundant", "note": "True retake.",
            },
            {
                "id": "already_kept", "decision": "cut", "confidence": "high",
                "screen_action": "redundant",
            },
        ]
        result = candidates.apply_semantic_audit_veto(original, audit)
        by_id = {item["id"]: item for item in result}
        self.assertEqual(by_id["unsafe"]["decision"], "review")
        self.assertEqual(by_id["safe"]["decision"], "cut")
        self.assertEqual(by_id["already_kept"]["decision"], "keep")

    def test_input_activity_contradiction_is_selected_for_clearance(self):
        source = [{
            "id": "repair", "start_ms": 1000, "end_ms": 4000,
            "duration_ms": 3000,
        }]
        decisions = [{
            "id": "repair", "decision": "cut", "confidence": "high",
            "screen_action": "none",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "activity.json"
            report.write_text(json.dumps({
                "input_activity_intervals_ms": [[2000, 2200]],
            }))
            selected = candidates.activity_clearance_candidates(
                source, decisions, report
            )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["detected_input_activity_overlap_ms"], 200.0)

    def test_cut_with_keep_rationale_is_downgraded(self):
        source = [{
            "id": "a", "type": "protected_pause", "start_ms": 100, "end_ms": 300,
            "removed_text": "[pause]",
        }]
        result = candidates.complete_decisions({"decisions": [{
            "id": "a", "decision": "cut", "confidence": "high",
            "screen_action": "redundant",
            "note": "This should be kept because cutting it would feel unnatural.",
        }]}, source)
        self.assertEqual(result["decisions"][0]["decision"], "review")

    def test_missing_model_decision_becomes_manual_review(self):
        source = [{
            "id": "a", "type": "hard_filler", "start_ms": 100, "end_ms": 300,
            "removed_text": "呃",
        }]
        result = candidates.complete_decisions({"decisions": []}, source)
        self.assertEqual(result["decisions"][0]["decision"], "review")
        self.assertEqual(result["decisions"][0]["confidence"], "low")

    def test_video_cut_without_screen_clearance_is_downgraded(self):
        source = [{
            "id": "a", "type": "hard_filler", "start_ms": 100, "end_ms": 300,
            "removed_text": "呃",
        }]
        result = candidates.complete_decisions({"decisions": [{
            "id": "a", "decision": "cut", "confidence": "high",
        }]}, source, require_screen_action=True)
        self.assertEqual(result["decisions"][0]["decision"], "review")

    def test_bailian_payload_embeds_video_and_requests_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"small-video")
            source = [{
                "id": "a", "type": "hard_filler", "start_ms": 100, "end_ms": 300,
                "start": 0.1, "end": 0.3, "removed_text": "呃",
                "clips": [{"label": "remove", "start": 0.0, "end": 1.0, "path": str(clip)}],
            }]
            payload = candidates.build_bailian_payload(
                SimpleNamespace(bailian_model="qwen3.5-omni-plus"), source
            )
            self.assertEqual(payload["model"], "qwen3.5-omni-plus")
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            video_parts = [
                item for item in payload["messages"][0]["content"]
                if item.get("type") == "video_url"
            ]
            self.assertEqual(len(video_parts), 1)
            self.assertTrue(video_parts[0]["video_url"]["url"].startswith("data:video/mp4;base64,"))

    def test_reasoning_payload_enables_thinking_without_json_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(b"small-video")
            source = [{
                "id": "a", "type": "possible_sparse_retake",
                "start_ms": 100, "end_ms": 20100,
                "start": 0.1, "end": 20.1, "removed_text": "earlier explanation",
                "clips": [{"label": "remove", "start": 0.0, "end": 21.0, "path": str(clip)}],
            }]
            payload = candidates.build_bailian_payload(
                SimpleNamespace(bailian_model="qwen3.5-omni-plus"),
                source,
                model="qwen3.7-plus",
                enable_thinking=True,
                video_fps=0.5,
            )
            self.assertEqual(payload["model"], "qwen3.7-plus")
            self.assertTrue(payload["enable_thinking"])
            self.assertNotIn("response_format", payload)
            self.assertNotIn("modalities", payload)
            video_part = next(
                item for item in payload["messages"][0]["content"]
                if item.get("type") == "video_url"
            )
            self.assertEqual(video_part["fps"], 0.5)


if __name__ == "__main__":
    unittest.main()
