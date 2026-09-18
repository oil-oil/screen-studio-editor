#!/usr/bin/env python3
"""Run the cached, Gemini-only Screen Studio smart-edit workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from process import analysis_cache_signature


SCRIPT_DIR = Path(__file__).resolve().parent
WORKFLOW_VERSION = 17
USER_CONFIG_FILE = Path(
    os.environ.get(
        "SCREEN_STUDIO_EDITOR_CONFIG",
        str(Path.home() / ".config" / "screen-studio-editor" / "config.json"),
    )
).expanduser()


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def load_json(path: Path) -> dict[str, Any]:
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
    pause_threshold_ms: float = 700,
    min_pause_ms: float = 180,
) -> str:
    payload = {
        "workflow_version": WORKFLOW_VERSION,
        "project_sha256": project_sha256(project),
        "cuts_sha256": file_sha256(cuts_path),
        "transcript_sha256": file_sha256(transcript),
        "baseline_report_sha256": file_sha256(baseline_report),
        "process_sha256": file_sha256(SCRIPT_DIR / "process.py"),
        "pause_threshold_ms": pause_threshold_ms,
        "min_pause_ms": min_pause_ms,
        "pause_source": "silence",
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def final_audit_is_current(report: Path, signature: str) -> bool:
    if not report.exists():
        return False
    try:
        return load_json(report).get("smart_edit_audit_signature") == signature
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def run(command: list[str], description: str) -> None:
    print(f"[smart-edit] {description}...")
    result = subprocess.run(command, stdout=subprocess.PIPE, text=True)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        fail(f"{description} failed.\n{details}")


def baseline_is_current(
    project: Path,
    report: Path,
    transcript: Path,
    pause_threshold_ms: float = 700,
    min_pause_ms: float = 180,
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
                silence_min_dur=0.3,
                no_vad=False,
                no_screen_activity_protection=False,
                no_visual_scan=False,
                visual_scan_fps=2.5,
                visual_change_threshold=0.012,
            ),
        )
        return (
            payload.get("project_sha256") == project_sha256(project)
            and payload.get("analysis_cache_signature")
            == expected_analysis_signature
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return False


TRANSITION_CONNECTIVES = (
    "但是", "不过", "然而", "可是", "也就是说", "所以", "因此", "另外", "同时", "虽然"
)


def candidate_cut(candidate: dict[str, Any]) -> dict[str, Any]:
    detector = str(
        candidate.get("detector_type")
        or candidate.get("planner_category")
        or "candidate"
    )
    removed_text = str(candidate.get("removed_text") or "").strip()
    kept_text = str(candidate.get("kept_text") or "").strip()
    risk_flags = []
    if any(removed_text.startswith(conn) for conn in TRANSITION_CONNECTIVES):
        risk_flags.append("starts_with_transition_connective")

    cut = {
        "start_ms": candidate["start_ms"],
        "end_ms": candidate["end_ms"],
        "removed_text": removed_text,
        "reason": "gemini_personalized_" + detector,
        "confidence": "high",
        "kept_text": kept_text,
        "candidate_type": candidate.get("detector_type") or candidate.get("type"),
    }
    if risk_flags:
        cut["risk_flags"] = risk_flags
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
    cut["preference_decision"] = candidate.get("preference_decision")
    return cut


def cuts_document(
    project: Path,
    baseline: dict[str, Any],
    arbiter: dict[str, Any],
) -> dict[str, Any]:
    cuts = []
    for candidate in arbiter.get("candidates") or []:
        cuts.append(candidate_cut(candidate))
    return {
        "schema_version": 2,
        "coordinate_space": "source",
        "project_sha256": baseline.get("project_sha256") or project_sha256(project),
        "cuts": cuts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--preferences", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force-analysis", action="store_true")
    parser.add_argument(
        "--discard-external-edits",
        action="store_true",
        help="Re-apply from project.json.bak, discarding prior auto-edits or external edits.",
    )
    args = parser.parse_args()

    config = load_user_config()
    project = args.project.expanduser().resolve()
    configured_preferences = (
        args.preferences
        or os.environ.get("SCREEN_STUDIO_EDITOR_PREFERENCES")
        or config.get("creator_preferences")
    )
    preferences = (
        Path(configured_preferences).expanduser().resolve()
        if configured_preferences else None
    )
    if preferences and not preferences.exists():
        if args.preferences:
            fail(f"指定的偏好文件不存在：{preferences}")
        print(f"[smart-edit] 偏好文件不存在，本次按音画和上下文判断：{preferences}")
        preferences = None
    model = (
        args.model
        or os.environ.get("SCREEN_STUDIO_EDITOR_MODEL")
        or config.get("model")
    )
    if not isinstance(model, str) or not model.strip():
        fail("未配置剪辑模型；请设置 config.json 的 model、SCREEN_STUDIO_EDITOR_MODEL 或 --model。")
    model = model.strip()
    smart_edit_config = config.get("smart_edit") or {}
    if not isinstance(smart_edit_config, dict):
        fail(f"smart_edit config must be a JSON object: {USER_CONFIG_FILE}")
    pause_threshold_ms = float(smart_edit_config.get("pause_threshold_ms", 700))
    min_pause_ms = float(smart_edit_config.get("min_pause_ms", 180))
    if not (project / "project.json").exists() or not (project / "recording").is_dir():
        fail(f"Not a Screen Studio project: {project}")
    baseline_report = project / "baseline-report.json"
    transcript = project / "baseline-report.transcript.edit.json"
    planner_report = project / "global-video-planner-v11.json"
    planner_work = project / "global-video-work-v11"
    arbiter_report = project / "smart-edit-report.json"
    cuts_path = project / "smart-edit-cuts.json"
    final_report = project / "smart-edit-final-report.json"

    final_command = [
        sys.executable,
        str(SCRIPT_DIR / "process.py"),
        "--project", str(project),
        "--skip-transcribe", str(transcript),
        "--reuse-analysis-report", str(baseline_report),
        "--cuts-file", str(cuts_path),
        "--pause-threshold", str(pause_threshold_ms),
        "--min-pause", str(min_pause_ms),
        "--pause-source", "silence",
        "--asr-backend", "bailian",
        "--language", "zh",
    ]
    if args.discard_external_edits:
        final_command.append("--discard-external-edits")
    if args.apply:
        if args.force_analysis:
            fail("重新分析后需要先审查结果，不能同时使用 --force-analysis 和 --apply。")
        if not all(path.exists() for path in (transcript, baseline_report, cuts_path, final_report)):
            fail("请先运行 dry-run 并审查剪辑结果。")
        # 应用时只执行已有 cuts，不再请求模型或覆盖已审查的候选。
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
        sys.executable,
        str(SCRIPT_DIR / "process.py"),
        "--project", str(project),
        "--pause-threshold", str(pause_threshold_ms),
        "--min-pause", str(min_pause_ms),
        "--pause-source", "silence",
        "--asr-backend", "bailian",
        "--language", "zh",
        "--dry-run",
        "--report-output", str(baseline_report),
    ]
    if args.discard_external_edits:
        baseline_command.append("--discard-external-edits")
    proxy_command = [
        sys.executable,
        str(SCRIPT_DIR / "build_review_proxy.py"),
        str(project),
    ]
    baseline_current = (
        not args.force_analysis
        and baseline_is_current(
            project,
            baseline_report,
            transcript,
            pause_threshold_ms,
            min_pause_ms,
        )
    )
    if baseline_current:
        print("[smart-edit] Reusing current baseline analysis.")
        run(proxy_command, "aligned review proxy")
    else:
        run(baseline_command, "local audio, transcript, and activity analysis")
        run(proxy_command + ["--force"], "aligned review proxy")
    if not transcript.exists() or not load_json(baseline_report).get("edit_transcript_cache"):
        fail("本次转录失败，无法继续语义剪辑；请重试分析。")

    api_base = (
        os.environ.get("SCREEN_STUDIO_EDITOR_API_BASE")
        or config.get("api_base")
    )
    api_key = (
        os.environ.get("SCREEN_STUDIO_EDITOR_API_KEY")
        or config.get("api_key")
    )
    timeout = (
        config.get("timeout")
        or (int(os.environ["SCREEN_STUDIO_EDITOR_TIMEOUT"]) if "SCREEN_STUDIO_EDITOR_TIMEOUT" in os.environ else None)
    )

    combined_video = project / "review-proxy" / "combined-timeline.mp4"
    planner_command = [
        sys.executable,
        str(SCRIPT_DIR / "global_edit_planner.py"),
        "--transcript", str(transcript),
        "--output", str(planner_report),
        "--work-dir", str(planner_work),
        "--model", model,
        "--video", str(combined_video),
    ]
    if not args.force_analysis:
        planner_command.append("--resume")
    if api_base:
        planner_command.extend(["--api-base", str(api_base)])
    if api_key:
        planner_command.extend(["--api-key", str(api_key)])
    if timeout:
        planner_command.extend(["--timeout", str(timeout)])
    run(planner_command, "Gemini whole-timeline paper edit")

    arbiter_command = [
        sys.executable,
        str(SCRIPT_DIR / "preference_edit_arbiter.py"),
        "decide",
        "--project", str(project),
        "--output", str(arbiter_report),
        "--model", model,
        "--candidate-source", "global",
        "--protected-pause-min-ms", "0",
        "--video", str(combined_video),
    ]
    if preferences:
        arbiter_command.extend(["--preferences", str(preferences)])
    if not args.force_analysis:
        arbiter_command.append("--resume")
    if api_base:
        arbiter_command.extend(["--api-base", str(api_base)])
    if api_key:
        arbiter_command.extend(["--api-key", str(api_key)])
    if timeout:
        arbiter_command.extend(["--timeout", str(timeout)])
    run(
        arbiter_command,
        "Gemini creator-style arbitration",
    )

    baseline = load_json(baseline_report)
    arbiter = load_json(arbiter_report)
    write_json(
        cuts_path,
        cuts_document(project, baseline, arbiter),
    )
    final_command.extend(["--dry-run", "--report-output", str(final_report)])
    audit_signature = final_audit_signature(
        project,
        cuts_path,
        transcript,
        baseline_report,
        pause_threshold_ms,
        min_pause_ms,
    )
    if not args.force_analysis and final_audit_is_current(final_report, audit_signature):
        print("[smart-edit] Reusing current final timeline audit.")
    else:
        run(
            final_command,
            "final timeline audit",
        )
        audited = load_json(final_report)
        audited["smart_edit_audit_signature"] = audit_signature
        audited["smart_edit_workflow_version"] = WORKFLOW_VERSION
        write_json(final_report, audited)

    report = load_json(final_report)
    summary = {
        "project": str(project),
        "model": model,
        "mode": "quality",
        "applied": False,
        "planner_candidates": load_json(planner_report).get("candidate_count"),
        "style_candidates": arbiter.get("candidate_count"),
        "accepted_smart_cuts": arbiter.get("accepted_count"),
        "safety_blocked": arbiter.get("safety_blocked_count"),
        "flagged_risk_cuts": sum(
            bool(item.get("risk_flags"))
            for item in load_json(cuts_path).get("cuts") or []
        ),
        "cuts": str(cuts_path),
        "audit": str(final_report),
        "original_duration_s": (
            round(float(report.get("original_duration_ms")) / 1000.0, 3)
            if report.get("original_duration_ms") is not None
            else None
        ),
        "projected_duration_s": (
            round(float(report.get("new_duration_ms")) / 1000.0, 3)
            if report.get("new_duration_ms") is not None
            else None
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
