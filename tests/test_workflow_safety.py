"""使用临时工程验证剪辑结果，不访问模型服务或用户录屏。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import preference_edit_arbiter as arbiter
import process
import global_edit_planner as planner
import smart_edit_workflow as workflow
import structured_edit_candidates as structured


def segment(start, tokens):
    words = [
        {"word": text, "start": start + i * 0.25, "end": start + (i + 1) * 0.25}
        for i, text in enumerate(tokens)
    ]
    return {"start": start, "end": words[-1]["end"], "text": "".join(tokens), "words": words}


class WorkflowSafetyTests(unittest.TestCase):
    def test_no_preferences_prompt_uses_direct_evidence(self):
        prompt = arbiter.prompt_for_arbitration([], [], video_supplied=True)
        self.assertIn("No creator examples are available", prompt)
        self.assertNotIn("only when the creator's examples", prompt)
        self.assertNotIn("The hand-edited removals show", prompt)
        self.assertIn("audio/video", prompt)

    def test_grounded_failed_take_longer_than_90_seconds_reaches_ai_review(self):
        atoms = [
            {"id": "U0001", "start": 10, "end": 119, "text": "失败的完整演示"},
            {"id": "U0002", "start": 120, "end": 140, "text": "重新完整演示"},
        ]
        edits = {"edits": [{
            "remove_start_id": "U0001", "remove_end_id": "U0001",
            "cut_until_id": "U0002", "replacement_ids": ["U0002"],
            "removed_quote": "失败的完整演示", "replacement_quote": "重新完整演示",
            "category": "abandoned_take", "confidence": "high",
        }]}
        rows, rejected = planner.candidates_from_plan(edits, atoms, model="test", max_candidate_ms=None)
        self.assertEqual(rejected, [])
        self.assertEqual((rows[0]["start"], rows[0]["end"]), (10, 120))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "样例.screenstudio"
        self.project.mkdir()
        recording = self.project / "recording"
        recording.mkdir()
        self.write(recording / "metadata.json", {"recorders": [{
            "type": "microphone", "sessions": [{
                "outputFilename": "microphone.mp4", "durationMs": 20000,
                "processTimeStartMs": 0,
            }],
        }]})
        self.write(self.project / "project.json", self.document(10000))
        self.write(self.project / "project.json.bak", self.document(20000))
        self.transcript = self.project / "input-transcript.json"
        self.write(self.transcript, [segment(1, ["当前", "保留", "内容"]),
                                     segment(15, ["之前", "删除", "内容"])])

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def document(end):
        return {"json": {"scenes": [{"slices": [{
            "id": "slice", "sourceStartMs": 0, "sourceEndMs": end,
        }]}], "config": {"userSetting": "保留"}}}

    def run_process(self, *extra):
        def audio(_project, _sessions, output, _temporary):
            with wave.open(str(output), "wb") as handle:
                handle.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                handle.writeframes(b"\x00\x00" * 16000 * 20)
            return []

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", ["process", "--project", str(self.project), *extra]))
            stack.enter_context(mock.patch.object(process, "load_user_config", return_value={}))
            stack.enter_context(mock.patch.object(process.shutil, "which", return_value="ffmpeg"))
            stack.enter_context(mock.patch.object(process, "merge_audio", side_effect=audio))
            stack.enter_context(mock.patch.object(process, "detect_silence_regions_by_session", return_value=([], [])))
            stack.enter_context(mock.patch.object(process, "collect_screen_activity_evidence", return_value=([], [])))
            stack.enter_context(mock.patch.object(process, "transcribe", return_value=json.loads(self.transcript.read_text())))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            process.main()

    def run_workflow(self, *extra, edits=None, decision="keep", config=None, with_preferences=True):
        preferences = self.project / "preferences.json"
        self.write(preferences, {})
        calls = []

        def execute(command, _description):
            calls.append(command)
            script = Path(command[1]).name
            if script == "process.py":
                self.run_process(*command[4:])
            elif script == "build_review_proxy.py":
                output = self.project / "review-proxy"
                output.mkdir(exist_ok=True)
                (output / "combined-timeline.mp4").write_bytes(b"proxy")
            elif script == "global_edit_planner.py":
                atoms = planner.transcript_atoms(planner.load_transcript(
                    Path(command[command.index("--transcript") + 1])
                ))
                candidates, rejected = planner.candidates_from_plan(
                    {"edits": edits or []}, atoms, model="test", max_candidate_ms=90000
                )
                self.assertEqual(rejected, [])
                self.write(Path(command[command.index("--output") + 1]), {
                    "candidates": candidates, "candidate_count": len(candidates),
                })
            elif script == "preference_edit_arbiter.py":
                # 只替换模型响应；候选读取、裁决处理、切点和工程写入实际执行。
                decisions = [{
                    "id": row["id"], "decision": decision, "confidence": "high",
                    "reason": "按上下文判断是否重复", "sequence_role": "other",
                    "screen_action": "none", "visual_assessment": "无独有画面操作",
                } for row in arbiter.target_candidates(self.project)]
                with mock.patch.object(sys, "argv", command[1:]), \
                     mock.patch.object(arbiter, "api_key", return_value="test"), \
                     mock.patch.object(arbiter, "request_arbitration", return_value=({}, {"decisions": decisions})):
                    arbiter.main()
            else:
                self.fail(f"默认流程调用了非 AI 语义阶段：{script}")

        argv = ["workflow", "--project", str(self.project), *extra]
        if with_preferences:
            argv.extend(["--preferences", str(preferences)])
        with mock.patch.object(sys, "argv", argv), mock.patch.object(workflow, "load_user_config", return_value={"model": "test-model"} if config is None else config), mock.patch.object(workflow, "run", side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
            workflow.main()
        return calls

    def repeated_warning(self, warning):
        return [segment(0, ["点击", "保存", "按钮"]), segment(2, warning),
                segment(4, ["点击", "保存", "按钮", "然后", "确认"])]

    def test_repeat_range_preserves_intervening_warning_including_short_asr_segment(self):
        activity = self.project / "activity.json"
        self.write(activity, {"input_activity_intervals_ms": []})
        for warning in (["原文件", "不能", "覆盖"], ["不能覆盖"]):
            with self.subTest(warning=warning):
                self.write(self.transcript, self.repeated_warning(warning))
                candidates = structured.build_structured_candidates(self.transcript, activity)
                self.assertTrue(candidates)
                for candidate in candidates:
                    self.assertLessEqual(candidate["end_ms"], 750)
                    self.assertNotIn("不能", candidate["removed_text"])

    @staticmethod
    def ai_repeat_edit(removed, kept):
        return {
            "remove_start_id": "U0001", "remove_end_id": "U0001",
            "cut_until_id": "U0003", "replacement_ids": ["U0003"],
            "removed_quote": removed, "replacement_quote": kept,
            "category": "duplicate_take", "confidence": "high",
            "reason": "后面有完整替代，保留中间独有提醒",
        }

    def test_identical_wording_is_kept_when_ai_rejects_redundancy(self):
        self.write(self.transcript, self.repeated_warning(["原文件", "不能", "覆盖"]))
        edit = self.ai_repeat_edit("点击保存按钮", "点击保存按钮")
        self.run_workflow(edits=[edit], decision="keep")
        targets = arbiter.candidate_rows(self.project)
        self.assertEqual(len(targets), 1)
        self.assertEqual(json.loads((self.project / "smart-edit-cuts.json").read_text())["cuts"], [])
        self.run_workflow("--apply")
        self.assertEqual(json.loads((self.project / "project.json").read_text()), self.document(10000))

    def test_different_wording_can_be_cut_only_after_ai_approval(self):
        self.write(self.transcript, [
            segment(0, ["点右上角", "的叉", "就能退出"]),
            segment(2, ["文件", "需要先", "保存"]),
            segment(4, ["按关闭按钮", "离开", "这个窗口"]),
        ])
        edit = self.ai_repeat_edit("点右上角的叉就能退出", "按关闭按钮离开这个窗口")
        self.run_workflow(edits=[edit], decision="cut")
        cuts = json.loads((self.project / "smart-edit-cuts.json").read_text())["cuts"]
        self.assertEqual(len(cuts), 1)
        self.assertEqual(cuts[0]["removed_text"], "点右上角的叉就能退出")
        # cut_until_id 不能跨过未被 AI 选中的中间句。
        self.assertEqual(cuts[0]["end_ms"], 750)
        self.run_workflow("--apply")
        slices = json.loads((self.project / "project.json").read_text())["json"]["scenes"][0]["slices"]
        self.assertFalse(any(row["sourceStartMs"] <= 0 and row["sourceEndMs"] >= 750 for row in slices))
        self.assertTrue(any(row["sourceStartMs"] <= 2000 and row["sourceEndMs"] >= 2750 for row in slices))

    def test_no_ai_candidate_means_no_semantic_cut_even_with_legacy_local_report(self):
        self.write(self.transcript, [
            segment(0, ["点击", "保存", "按钮"]),
            {"start": 2, "end": 2.6, "text": "嗯", "words": [{"start": 2, "end": 2.6, "word": "嗯"}]},
            segment(4, ["点击", "保存", "按钮"]),
        ])
        self.write(self.project / "structured-edit-candidates-v1.json", {"candidates": [
            {"start": 2, "end": 2.6, "start_ms": 2000, "end_ms": 2600, "detector_type": "hard_filler"},
            {"start": 0, "end": .75, "start_ms": 0, "end_ms": 750, "detector_type": "possible_tail_restart"},
        ]})
        self.run_workflow()
        self.assertEqual(arbiter.candidate_rows(self.project), [])
        self.assertEqual(json.loads((self.project / "smart-edit-cuts.json").read_text())["cuts"], [])

    def test_repeated_analysis_and_write_use_current_timeline(self):
        report = self.project / "dry.json"
        self.run_process("--dry-run", "--skip-transcribe", str(self.transcript), "--report-output", str(report))
        self.run_process("--skip-transcribe", str(self.transcript))
        dry = json.loads(report.read_text())
        applied = json.loads((self.project / "autoedit-report.json").read_text())
        self.assertEqual(dry["new_duration_ms"], 10000)
        self.assertEqual(applied["new_duration_ms"], dry["new_duration_ms"])
        self.assertEqual(json.loads((self.project / "project.json").read_text()), self.document(10000))

    def test_explicit_backup_rebuild_is_also_previewed(self):
        report = self.project / "dry.json"
        options = ["--discard-external-edits", "--skip-transcribe", str(self.transcript)]
        self.run_process(*options, "--dry-run", "--report-output", str(report))
        self.run_process(*options)
        self.assertEqual(json.loads(report.read_text())["new_duration_ms"], 20000)
        self.assertEqual(json.loads((self.project / "project.json").read_text()), self.document(20000))

    def test_quality_apply_reuses_reviewed_cuts_without_model_calls(self):
        self.run_workflow()
        report = json.loads((self.project / "smart-edit-final-report.json").read_text())
        calls = self.run_workflow("--apply")
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0][1]).name, "process.py")
        self.assertIn("--cuts-file", calls[0])
        applied = json.loads((self.project / "autoedit-report.json").read_text())
        self.assertEqual(applied["new_duration_ms"], report["new_duration_ms"])

    def test_quality_edit_runs_without_optional_preferences(self):
        self.write(self.transcript, self.repeated_warning(["独有", "提醒", "保留"]))
        calls = self.run_workflow(
            with_preferences=False,
            edits=[self.ai_repeat_edit("点击保存按钮", "点击保存按钮")],
            decision="keep",
        )
        arbitration = next(c for c in calls if Path(c[1]).name == "preference_edit_arbiter.py")
        self.assertNotIn("--preferences", arbitration)
        self.assertEqual(json.loads((self.project / "smart-edit-report.json").read_text())["training_examples"], 0)

    def test_stale_config_preference_path_does_not_disable_semantic_editing(self):
        calls = self.run_workflow(
            with_preferences=False,
            config={"model": "test-model", "creator_preferences": str(self.project / "missing.json")},
        )
        arbitration = next(c for c in calls if Path(c[1]).name == "preference_edit_arbiter.py")
        self.assertNotIn("--preferences", arbitration)

    def test_missing_model_stops_without_falling_back(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "未配置剪辑模型"):
                self.run_workflow(config={})
        self.assertFalse((self.project / "smart-edit-cuts.json").exists())

    def test_configured_model_reaches_both_ai_stages(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            calls = self.run_workflow(config={"model": "configured-model"})
        model_calls = [c for c in calls if "--model" in c]
        self.assertEqual(len(model_calls), 2)
        self.assertTrue(all(c[c.index("--model") + 1] == "configured-model" for c in model_calls))

    def test_apply_requires_existing_analysis(self):
        with self.assertRaises(SystemExit):
            self.run_workflow("--apply")
        self.assertEqual(json.loads((self.project / "project.json").read_text()), self.document(10000))

    def test_force_refresh_recomputes_transcript_proxy_and_models(self):
        self.run_workflow()
        calls = self.run_workflow("--force-analysis")
        self.assertNotIn("--skip-transcribe", calls[0])
        self.assertIn("--force", calls[1])
        model_calls = [c for c in calls if Path(c[1]).name in {"global_edit_planner.py", "preference_edit_arbiter.py"}]
        self.assertEqual(len(model_calls), 2)
        self.assertTrue(all("--resume" not in c for c in model_calls))

    def test_failed_asr_cannot_use_leftover_transcript(self):
        self.run_workflow()
        report = self.project / "baseline-report.json"
        baseline = json.loads(report.read_text())
        baseline["edit_transcript_cache"] = None
        self.write(report, baseline)
        with mock.patch.object(workflow, "baseline_is_current", return_value=True):
            with self.assertRaisesRegex(SystemExit, "转录失败"):
                self.run_workflow()

    def test_unchanged_input_reuses_existing_analysis(self):
        self.run_workflow()
        calls = self.run_workflow()
        self.assertFalse(any(Path(c[1]).name == "process.py" for c in calls))

    def test_user_timeline_and_layout_are_preserved(self):
        changed = self.document(7000)
        changed["json"]["config"]["userSetting"] = "手工修改"
        self.write(self.project / "project.json", changed)
        self.run_workflow()
        self.run_workflow("--apply")
        self.assertEqual(json.loads((self.project / "project.json").read_text()), changed)

    def test_discard_external_edits_forwarded_to_process(self):
        self.run_workflow()
        calls = self.run_workflow("--apply", "--discard-external-edits")
        apply_call = calls[-1]
        self.assertIn("--discard-external-edits", apply_call)

    def test_api_config_forwarded_to_models(self):
        calls = self.run_workflow(config={
            "model": "test-model",
            "api_base": "https://api.example.com/v1",
            "api_key": "custom-key",
            "timeout": 120,
        })
        model_calls = [c for c in calls if Path(c[1]).name in {"global_edit_planner.py", "preference_edit_arbiter.py"}]
        self.assertEqual(len(model_calls), 2)
        for c in model_calls:
            self.assertIn("--api-base", c)
            self.assertEqual(c[c.index("--api-base") + 1], "https://api.example.com/v1")
            self.assertIn("--api-key", c)
            self.assertEqual(c[c.index("--api-key") + 1], "custom-key")
            self.assertIn("--timeout", c)
            self.assertEqual(c[c.index("--timeout") + 1], "120")

    def test_transition_connective_flags_cut_risk(self):
        self.write(self.transcript, [
            segment(0, ["但是", "这个框架", "很好用"]),
            segment(2, ["中间", "演示", "操作"]),
            segment(4, ["这个", "框架", "很好用"]),
        ])
        edit = self.ai_repeat_edit("但是这个框架很好用", "这个框架很好用")
        self.run_workflow(edits=[edit], decision="cut")
        cuts = json.loads((self.project / "smart-edit-cuts.json").read_text())["cuts"]
        self.assertEqual(len(cuts), 1)
        self.assertIn("starts_with_transition_connective", cuts[0].get("risk_flags", []))


if __name__ == "__main__":
    unittest.main()
