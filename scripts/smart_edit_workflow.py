#!/usr/bin/env python3
"""Prepare and validate the Screen Studio quality-edit workflow.

The scripts measure audio, transcript timing, and screen activity. The current
Agent reads ``smart-edit-context.json`` and writes ``smart-edit-plan.json``.
This entrypoint validates that plan and asks ``process.py`` to preview or apply
the cuts; it never calls a remote semantic model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from process import analysis_cache_signature


SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW_VERSION = 21
USER_CONFIG_FILE = Path(
    os.environ.get(
        "SCREEN_STUDIO_EDITOR_CONFIG",
        str(Path.home() / ".config" / "screen-studio-editor" / "config.json"),
    )
).expanduser()
TRANSITION_CONNECTIVES = (
    "但是", "不过", "然而", "可是", "也就是说", "所以", "因此", "另外", "同时", "虽然"
)


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_user_config() -> dict[str, Any]:
    if not USER_CONFIG_FILE.exists():
        return {}
    try:
        payload = load_json(USER_CONFIG_FILE)
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Invalid user config {USER_CONFIG_FILE}: {exc}")
    if not isinstance(payload, dict):
        fail(f"User config must be a JSON object: {USER_CONFIG_FILE}")
    return payload


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == serialized:
        return
    path.write_text(serialized, encoding="utf-8")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_sha256(project: Path) -> str:
    return file_sha256(project / "project.json")


def final_audit_signature(
    project: Path,
    cuts_path: Path,
    transcript: Path,
    baseline_report: Path,
    plan_path: Path,
    pause_threshold_ms: float = 300,
    min_pause_ms: float = 180,
) -> str:
    payload = {
        "workflow_version": WORKFLOW_VERSION,
        "project_sha256": project_sha256(project),
        "cuts_sha256": file_sha256(cuts_path),
        "transcript_sha256": file_sha256(transcript),
        "baseline_report_sha256": file_sha256(baseline_report),
        "plan_sha256": file_sha256(plan_path),
        "process_sha256": file_sha256(SCRIPT_DIR / "process.py"),
        "pause_threshold_ms": pause_threshold_ms,
        "min_pause_ms": min_pause_ms,
        "pause_source": "silence",
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def final_audit_is_current(report: Path, signature: str) -> bool:
    if not report.exists():
        return False
    try:
        return load_json(report).get("smart_edit_audit_signature") == signature
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return False


def run(command: list[str], description: str) -> None:
    print(f"[smart-edit] {description}...")
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        fail(f"{description} failed.\n{details}")


def baseline_is_current(
    project: Path,
    report: Path,
    transcript: Path,
    pause_threshold_ms: float = 300,
    min_pause_ms: float = 180,
    asr_backend: str = "bailian",
) -> bool:
    if not report.exists() or not transcript.exists():
        return False
    try:
        payload = load_json(report)
        expected_analysis_signature = analysis_cache_signature(
            project / "project.json",
            transcript,
            argparse.Namespace(
                pause_threshold=pause_threshold_ms,
                min_pause=min_pause_ms,
                pause_source="silence",
                silence_db="auto",
                silence_min_dur=0.25,
                no_vad=False,
                no_screen_activity_protection=False,
                no_visual_scan=False,
                visual_scan_fps=2.5,
                visual_change_threshold=0.012,
                asr_backend=asr_backend,
            ),
        )
        return (
            payload.get("project_sha256") == project_sha256(project)
            and payload.get("analysis_cache_signature") == expected_analysis_signature
        )
    except (OSError, json.JSONDecodeError, TypeError, AttributeError):
        return False


def transcript_atoms(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create stable IDs for the Agent without changing ASR wording or clocks."""
    atoms: list[dict[str, Any]] = []
    for segment in segments:
        words = [
            word for word in (segment.get("words") or [])
            if word.get("start") is not None and word.get("end") is not None
            and str(word.get("word") or word.get("text") or "").strip()
        ]
        if words:
            atoms.append({
                "start": float(words[0]["start"]),
                "end": float(words[-1]["end"]),
                "text": "".join(str(item.get("word") or item.get("text") or "").strip() for item in words),
            })
        else:
            text = " ".join(str(segment.get("text") or "").split()).strip()
            start = float(segment.get("start", 0.0))
            end = float(segment.get("end", start))
            if text and end > start:
                atoms.append({"start": start, "end": end, "text": text})
    atoms.sort(key=lambda item: (item["start"], item["end"]))
    for index, atom in enumerate(atoms, start=1):
        atom["id"] = f"U{index:04d}"
    return atoms


def context_payload(
    project: Path,
    baseline: dict[str, Any],
    transcript: Path,
    review_proxy: Path,
    preferences: Path | None,
) -> dict[str, Any]:
    segments = load_json(transcript)
    atoms = transcript_atoms(segments)
    evidence_files: dict[str, Any] = {}
    for name in (
        "structured-edit-candidates-v1.json",
        "activity.json",
        "screen-activity.json",
    ):
        path = project / name
        if path.exists():
            try:
                evidence_files[name] = load_json(path)
            except (OSError, json.JSONDecodeError):
                evidence_files[name] = {"unreadable": True}
    payload: dict[str, Any] = {
        "schema_version": 1,
        "workflow": "screen-studio-editor-agent-plan",
        "workflow_version": WORKFLOW_VERSION,
        "project": str(project),
        "project_sha256": project_sha256(project),
        "coordinate_space": "source",
        "transcript": str(transcript),
        "transcript_sha256": file_sha256(transcript),
        "transcript_segments": segments,
        "transcript_atoms": atoms,
        "baseline_report": str(project / "baseline-report.json"),
        "baseline": {
            key: baseline.get(key)
            for key in (
                "original_duration_ms",
                "pauses_applied",
                "pauses_protected_by_activity",
                "input_activity_intervals_ms",
                "visual_activity_intervals_ms",
                "activity_intervals_ms",
                "silence_regions_ms",
            )
            if key in baseline
        },
        "review_proxy": str(review_proxy / "combined-timeline.mp4") if review_proxy.exists() else None,
        "review_proxy_sha256": (
            file_sha256(review_proxy / "combined-timeline.mp4")
            if (review_proxy / "combined-timeline.mp4").exists()
            else None
        ),
        "creator_preferences": str(preferences) if preferences else None,
        "evidence_files": evidence_files,
        "agent_instructions": {
            "purpose": "只删掉有明确证据的废片段，保留独有信息、操作演示和阅读时间。",
            "use_source_time": "所有时间都是 source 时间轴；不要使用导出视频时间。",
            "required_decision": "每条候选都写 decision=keep、review 或 cut；只有 cut 才会进入剪辑。",
            "screen_rule": "确认没有人声的空白由程序按统一音频规则直接剪；屏幕活动只作为报告证据，不会把无声区整段拦住。",
            "speech_rule": "‘嗯/啊/呃’只有在它是孤立口头语且接缝自然时才可删；停顿本身不能证明重复。",
            "plan_binding": "计划必须原样填写本文件的 project_sha256 和 context_sha256。",
        },
    }
    unsigned = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    payload["context_sha256"] = hashlib.sha256(unsigned).hexdigest()
    return payload


def _text_from_atoms(atoms: list[dict[str, Any]], start_id: str, end_id: str) -> str:
    by_id = {str(atom["id"]): atom for atom in atoms}
    positions = {str(atom["id"]): index for index, atom in enumerate(atoms)}
    if start_id not in by_id or end_id not in by_id or positions[start_id] > positions[end_id]:
        return ""
    return "".join(atom["text"] for atom in atoms[positions[start_id] : positions[end_id] + 1])


def candidate_cut(candidate: dict[str, Any]) -> dict[str, Any]:
    """Compatibility normalizer for old benchmark fixtures.

    Production quality editing uses :func:`plan_to_cuts`; keeping this small
    adapter lets historical unit tests inspect candidate provenance without
    bringing the retired remote planner back into the default path.
    """
    removed_text = str(candidate.get("removed_text") or "").strip()
    cut: dict[str, Any] = {
        "start_ms": candidate["start_ms"],
        "end_ms": candidate["end_ms"],
        "removed_text": removed_text,
        "reason": "agent_semantic_review_" + str(
            candidate.get("detector_type") or candidate.get("planner_category") or "candidate"
        ),
        "confidence": "high",
        "kept_text": str(candidate.get("kept_text") or "").strip(),
        "candidate_type": candidate.get("detector_type") or candidate.get("type"),
    }
    if any(removed_text.startswith(conn) for conn in TRANSITION_CONNECTIVES):
        cut["risk_flags"] = ["starts_with_transition_connective"]
    for key in ("spoken_start_ms", "spoken_end_ms"):
        if candidate.get(key) is not None:
            cut[key] = candidate[key]
    for key in ("screen_action", "visual_assessment", "source_report"):
        if candidate.get(key):
            cut[key] = candidate[key]
    if (
        candidate.get("planner_category") == "screen_pause"
        or (
            candidate.get("replacementless_local_cleanup")
            and not candidate.get("refine_speech_boundaries")
        )
    ):
        cut["preserve_reviewed_boundaries"] = True
    if candidate.get("preference_decision"):
        cut["preference_decision"] = candidate["preference_decision"]
    return cut


def cuts_document(
    project: Path,
    baseline: dict[str, Any],
    arbiter: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility adapter for archived benchmark reports."""
    return {
        "schema_version": 2,
        "coordinate_space": "source",
        "project_sha256": baseline.get("project_sha256") or project_sha256(project),
        "cuts": [candidate_cut(candidate) for candidate in arbiter.get("candidates") or []],
    }


def plan_to_cuts(
    plan: dict[str, Any], context: dict[str, Any], project: Path, plan_path: Path | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(plan, dict):
        fail("smart-edit-plan.json 必须是 JSON 对象。")
    if plan.get("project_sha256") != context.get("project_sha256"):
        fail("剪辑计划对应的 project.json 已变化，请重新生成 smart-edit-context.json。")
    if plan.get("context_sha256") != context.get("context_sha256"):
        fail("剪辑计划不是根据当前 smart-edit-context.json 生成的，请重新判断。")
    atoms = context.get("transcript_atoms") or []
    rows = plan.get("decisions") or plan.get("cuts") or plan.get("edits")
    if not isinstance(rows, list):
        fail("剪辑计划需要 decisions、cuts 或 edits 数组。")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            rejected.append({"index": index, "reason": "not_an_object"})
            continue
        decision = str(raw.get("decision") or "cut").lower()
        confidence = str(raw.get("confidence") or "low").lower()
        if decision != "cut":
            rejected.append({"index": index, "decision": decision, "reason": "not_cut"})
            continue
        if confidence not in {"high", "medium"}:
            rejected.append({"index": index, "reason": "low_confidence"})
            continue
        start = raw.get("start_ms")
        end = raw.get("end_ms")
        start_id = str(raw.get("remove_start_id") or raw.get("start_id") or "")
        end_id = str(raw.get("remove_end_id") or raw.get("end_id") or "")
        if start is None and start_id and end_id:
            atoms_by_id = {str(atom["id"]): atom for atom in atoms}
            if start_id in atoms_by_id and end_id in atoms_by_id:
                start = float(atoms_by_id[start_id]["start"]) * 1000.0
                end = float(atoms_by_id[end_id]["end"]) * 1000.0
        try:
            start = float(start)
            end = float(end)
        except (TypeError, ValueError):
            rejected.append({"index": index, "reason": "invalid_time_range"})
            continue
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            rejected.append({"index": index, "reason": "invalid_time_range"})
            continue
        removed_text = str(raw.get("removed_text") or raw.get("removed_quote") or "").strip()
        if not removed_text and start_id and end_id:
            removed_text = _text_from_atoms(atoms, start_id, end_id)
        risk_flags = []
        if any(removed_text.startswith(conn) for conn in TRANSITION_CONNECTIVES):
            risk_flags.append("starts_with_transition_connective")
        cut: dict[str, Any] = {
            "start_ms": round(start, 3),
            "end_ms": round(end, 3),
            "removed_text": removed_text,
            "kept_text": str(raw.get("kept_text") or raw.get("replacement_quote") or "").strip(),
            "reason": str(raw.get("reason") or raw.get("decision_reason") or "").strip(),
            "confidence": confidence,
            "category": str(raw.get("category") or raw.get("sequence_role") or "agent_review"),
            "source": "agent_semantic_review",
            "screen_action": str(raw.get("screen_action") or "unknown"),
            "replacement_evidence": str(raw.get("replacement_evidence") or "").strip(),
        }
        if raw.get("visual_assessment"):
            cut["visual_assessment"] = str(raw["visual_assessment"])
        if risk_flags:
            cut["risk_flags"] = risk_flags
        accepted.append(cut)
    accepted.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    document = {
        "schema_version": 2,
        "coordinate_space": "source",
        "project_sha256": context["project_sha256"],
        "project_json": str(project / "project.json"),
        "transcript": context.get("transcript"),
        "plan": str(plan_path or project / "smart-edit-plan.json"),
        "decision_source": "calling-agent",
        "cuts": accepted,
        "rejected_plan_rows": rejected,
    }
    return document, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--preferences", type=Path)
    parser.add_argument("--plan", type=Path, help="Agent 语义判断 JSON；默认是工程内的 smart-edit-plan.json")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force-analysis", action="store_true")
    parser.add_argument("--asr-backend", choices=["bailian", "local"], help="只影响转录；语义判断始终由当前 Agent 完成。")
    parser.add_argument("--discard-external-edits", action="store_true")
    args = parser.parse_args()

    config = load_user_config()
    project = args.project.expanduser().resolve()
    if not (project / "project.json").exists() or not (project / "recording").is_dir():
        fail(f"Not a Screen Studio project: {project}")
    configured_preferences = args.preferences or os.environ.get("SCREEN_STUDIO_EDITOR_PREFERENCES") or config.get("creator_preferences")
    preferences = Path(configured_preferences).expanduser().resolve() if configured_preferences else None
    if preferences and not preferences.exists():
        if args.preferences:
            fail(f"指定的偏好文件不存在：{preferences}")
        print(f"[smart-edit] 偏好文件不存在，本次按音画和上下文判断：{preferences}")
        preferences = None

    smart_edit_config = config.get("smart_edit") or {}
    if not isinstance(smart_edit_config, dict):
        fail(f"smart_edit config must be a JSON object: {USER_CONFIG_FILE}")
    pause_threshold_ms = float(smart_edit_config.get("pause_threshold_ms", 300))
    min_pause_ms = float(smart_edit_config.get("min_pause_ms", 180))
    asr_backend = args.asr_backend or str(smart_edit_config.get("asr_backend") or config.get("asr_backend") or "bailian")
    if asr_backend not in {"bailian", "local"}:
        fail("asr_backend 只能是 bailian 或 local。")

    baseline_report = project / "baseline-report.json"
    transcript = project / "baseline-report.transcript.edit.json"
    review_proxy = project / "review-proxy"
    context_path = project / "smart-edit-context.json"
    plan_path = (args.plan or project / "smart-edit-plan.json").expanduser().resolve()
    cuts_path = project / "smart-edit-cuts.json"
    final_report = project / "smart-edit-final-report.json"

    final_command = [
        sys.executable, str(SCRIPT_DIR / "process.py"),
        "--project", str(project), "--skip-transcribe", str(transcript),
        "--reuse-analysis-report", str(baseline_report), "--cuts-file", str(cuts_path),
        "--pause-threshold", str(pause_threshold_ms), "--min-pause", str(min_pause_ms),
        "--pause-source", "silence", "--asr-backend", asr_backend, "--language", "zh",
    ]
    if args.discard_external_edits:
        final_command.append("--discard-external-edits")
    if args.apply:
        if args.force_analysis:
            fail("重新分析后需要先审查结果，不能同时使用 --force-analysis 和 --apply。")
        if not all(path.exists() for path in (transcript, baseline_report, context_path, plan_path, cuts_path, final_report)):
            fail("请先完成本地分析、Agent 计划和 dry-run 审查。")
        run(final_command, "applying reviewed cuts")
        applied = load_json(project / "autoedit-report.json")
        print(json.dumps({
            "project": str(project), "applied": True,
            "original_duration_s": applied["original_duration_ms"] / 1000.0,
            "projected_duration_s": applied["new_duration_ms"] / 1000.0,
            "audit": str(project / "autoedit-report.json"),
        }, ensure_ascii=False, indent=2))
        return

    baseline_command = [
        sys.executable, str(SCRIPT_DIR / "process.py"), "--project", str(project),
        "--pause-threshold", str(pause_threshold_ms), "--min-pause", str(min_pause_ms),
        "--pause-source", "silence", "--asr-backend", asr_backend, "--language", "zh",
        "--dry-run", "--report-output", str(baseline_report),
    ]
    if args.discard_external_edits:
        baseline_command.append("--discard-external-edits")
    proxy_command = [sys.executable, str(SCRIPT_DIR / "build_review_proxy.py"), str(project)]
    baseline_current = not args.force_analysis and baseline_is_current(
        project, baseline_report, transcript, pause_threshold_ms, min_pause_ms, asr_backend
    )
    if baseline_current:
        print("[smart-edit] Reusing current baseline analysis.")
        run(proxy_command, "aligned review proxy")
    else:
        run(baseline_command, "local audio, transcript, and activity analysis")
        run(proxy_command + ["--force"], "aligned review proxy")
    if not transcript.exists() or not load_json(baseline_report).get("edit_transcript_cache"):
        fail("本次转录失败，无法继续语义剪辑；请重试分析。")

    baseline = load_json(baseline_report)
    context = context_payload(project, baseline, transcript, review_proxy, preferences)
    write_json(context_path, context)
    if not plan_path.exists():
        print(json.dumps({
            "project": str(project), "mode": "prepare-agent-context", "applied": False,
            "context": str(context_path), "plan": str(plan_path),
            "message": "请由当前 Agent 读取 context，写入 smart-edit-plan.json 后再次运行本命令。",
        }, ensure_ascii=False, indent=2))
        return

    plan = load_json(plan_path)
    cuts_document, rejected = plan_to_cuts(plan, context, project, plan_path)
    write_json(cuts_path, cuts_document)
    final_command.extend(["--dry-run", "--report-output", str(final_report)])
    audit_signature = final_audit_signature(
        project, cuts_path, transcript, baseline_report, plan_path,
        pause_threshold_ms, min_pause_ms,
    )
    if not args.force_analysis and final_audit_is_current(final_report, audit_signature):
        print("[smart-edit] Reusing current final timeline audit.")
    else:
        run(final_command, "final timeline audit")
        audited = load_json(final_report)
        audited["smart_edit_audit_signature"] = audit_signature
        audited["smart_edit_workflow_version"] = WORKFLOW_VERSION
        audited["semantic_decision_source"] = "calling-agent"
        audited["semantic_plan"] = str(plan_path)
        audited["plan_rows_rejected"] = len(rejected)
        write_json(final_report, audited)

    report = load_json(final_report)
    cuts = cuts_document.get("cuts") or []
    summary = {
        "project": str(project), "mode": "quality", "applied": False,
        "semantic_decision_source": "calling-agent",
        "accepted_agent_cuts": len(cuts), "rejected_plan_rows": len(rejected),
        "flagged_risk_cuts": sum(bool(item.get("risk_flags")) for item in cuts),
        "cuts": str(cuts_path), "context": str(context_path), "audit": str(final_report),
        "original_duration_s": round(float(report.get("original_duration_ms")) / 1000.0, 3) if report.get("original_duration_ms") is not None else None,
        "projected_duration_s": round(float(report.get("new_duration_ms")) / 1000.0, 3) if report.get("new_duration_ms") is not None else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
