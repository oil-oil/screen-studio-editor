"""Regression tests for the local-evidence + calling-Agent workflow."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import smart_edit_workflow as workflow


class WorkflowSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "样例.screenstudio"
        (self.project / "recording").mkdir(parents=True)
        self.write(self.project / "recording" / "metadata.json", {"recorders": []})
        self.write(self.project / "project.json", {
            "json": {"scenes": [{"slices": [{
                "id": "slice", "sourceStartMs": 0, "sourceEndMs": 10000,
                "durationMs": 10000,
            }]}], "config": {}}
        })

    @staticmethod
    def write(path: Path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def _segments(self):
        return [
            {"start": 0.0, "end": 1.0, "text": "点击保存按钮", "words": [
                {"word": "点击保存按钮", "start": 0.0, "end": 1.0}
            ]},
            {"start": 2.0, "end": 3.0, "text": "不对重新说", "words": [
                {"word": "不对重新说", "start": 2.0, "end": 3.0}
            ]},
            {"start": 4.0, "end": 5.0, "text": "点击右边的保存按钮", "words": [
                {"word": "点击右边的保存按钮", "start": 4.0, "end": 5.0}
            ]},
        ]

    def run_workflow(self, *extra):
        calls = []

        def fake_run(command, description):
            calls.append(command)
            script = Path(command[1]).name
            if script == "build_review_proxy.py":
                proxy = self.project / "review-proxy"
                proxy.mkdir(exist_ok=True)
                (proxy / "combined-timeline.mp4").write_bytes(b"proxy")
                return
            if script != "process.py":
                self.fail(f"default workflow called retired semantic script: {script}")
            if "--report-output" in command:
                report = Path(command[command.index("--report-output") + 1])
                transcript = report.with_name(f"{report.stem}.transcript.edit.json")
                self.write(transcript, self._segments())
                self.write(report, {
                    "project_sha256": workflow.project_sha256(self.project),
                    "analysis_cache_signature": "not-current",
                    "edit_transcript_cache": str(transcript),
                    "original_duration_ms": 10000,
                    "new_duration_ms": 9000,
                    "input_activity_intervals_ms": [],
                    "visual_activity_intervals_ms": [],
                    "activity_intervals_ms": [],
                    "pauses_applied": [],
                    "pauses_protected_by_activity": [],
                    "silence_regions_ms": [],
                })
            else:
                self.write(self.project / "autoedit-report.json", {
                    "original_duration_ms": 10000, "new_duration_ms": 9000,
                })

        with mock.patch.object(workflow, "load_user_config", return_value={}), \
             mock.patch.object(workflow, "run", side_effect=fake_run), \
             mock.patch.object(sys, "argv", ["workflow", "--project", str(self.project), *extra]), \
             contextlib.redirect_stdout(io.StringIO()):
            workflow.main()
        return calls

    def prepare_plan(self):
        self.run_workflow("--asr-backend", "local")
        context = json.loads((self.project / "smart-edit-context.json").read_text())
        self.write(self.project / "smart-edit-plan.json", {
            "schema_version": 1,
            "project_sha256": context["project_sha256"],
            "context_sha256": context["context_sha256"],
            "decisions": [{
                "decision": "cut", "confidence": "high",
                "start_ms": 2000, "end_ms": 3000,
                "category": "abandoned_take",
                "removed_text": "不对重新说",
                "kept_text": "点击右边的保存按钮",
                "reason": "明确重说，后一遍覆盖前一遍",
                "replacement_evidence": "U0003",
                "screen_action": "redundant",
            }],
        })
        return context

    def test_first_run_only_prepares_context_and_calls_no_remote_semantic_script(self):
        calls = self.run_workflow()
        self.assertTrue((self.project / "smart-edit-context.json").exists())
        self.assertFalse((self.project / "smart-edit-cuts.json").exists())
        self.assertEqual({Path(call[1]).name for call in calls}, {"process.py", "build_review_proxy.py"})

    def test_plan_is_bound_to_context_and_final_cuts_are_source_time(self):
        context = self.prepare_plan()
        calls = self.run_workflow()
        self.assertEqual({Path(call[1]).name for call in calls}, {"process.py", "build_review_proxy.py"})
        cuts = json.loads((self.project / "smart-edit-cuts.json").read_text())
        self.assertEqual(cuts["coordinate_space"], "source")
        self.assertEqual(cuts["project_sha256"], context["project_sha256"])
        self.assertEqual(cuts["decision_source"], "calling-agent")
        self.assertEqual(cuts["cuts"][0]["start_ms"], 2000)
        report = json.loads((self.project / "smart-edit-final-report.json").read_text())
        self.assertEqual(report["semantic_decision_source"], "calling-agent")

    def test_apply_reuses_reviewed_cuts_without_remote_call(self):
        self.prepare_plan()
        self.run_workflow()
        calls = self.run_workflow("--apply")
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0][1]).name, "process.py")

    def test_stale_context_plan_is_rejected(self):
        self.prepare_plan()
        plan_path = self.project / "smart-edit-plan.json"
        plan = json.loads(plan_path.read_text())
        plan["context_sha256"] = "stale"
        self.write(plan_path, plan)
        with self.assertRaisesRegex(SystemExit, "context"):
            self.run_workflow()

    def test_low_confidence_and_keep_rows_are_recorded_but_not_cut(self):
        self.run_workflow()
        context = json.loads((self.project / "smart-edit-context.json").read_text())
        self.write(self.project / "smart-edit-plan.json", {
            "project_sha256": context["project_sha256"],
            "context_sha256": context["context_sha256"],
            "decisions": [
                {"decision": "keep", "confidence": "high", "start_ms": 0, "end_ms": 500},
                {"decision": "cut", "confidence": "low", "start_ms": 2000, "end_ms": 3000},
            ],
        })
        self.run_workflow()
        cuts = json.loads((self.project / "smart-edit-cuts.json").read_text())
        self.assertEqual(cuts["cuts"], [])
        self.assertEqual(len(cuts["rejected_plan_rows"]), 2)

    def test_transition_connective_is_flagged_for_review(self):
        self.run_workflow()
        context = json.loads((self.project / "smart-edit-context.json").read_text())
        self.write(self.project / "smart-edit-plan.json", {
            "project_sha256": context["project_sha256"],
            "context_sha256": context["context_sha256"],
            "decisions": [{
                "decision": "cut", "confidence": "high", "start_ms": 2000, "end_ms": 3000,
                "removed_text": "但是这个方案", "reason": "待人工确认",
            }],
        })
        self.run_workflow()
        cuts = json.loads((self.project / "smart-edit-cuts.json").read_text())["cuts"]
        self.assertIn("starts_with_transition_connective", cuts[0]["risk_flags"])


if __name__ == "__main__":
    unittest.main()
