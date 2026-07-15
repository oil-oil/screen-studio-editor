#!/usr/bin/env python3
"""Run the cached, Gemini-only Screen Studio smart-edit workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = "google/gemini-3.5-flash"
DEFAULT_PREFERENCES = Path(
    "~/Screen Studio Projects/AutoEdit Benchmarks/creator-edit-preferences.json"
).expanduser()


def fail(message: str) -> None:
    raise SystemExit(f"Error: {message}")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def project_sha256(project: Path) -> str:
    return hashlib.sha256((project / "project.json").read_bytes()).hexdigest()


def run(command: list[str], description: str) -> None:
    print(f"[smart-edit] {description}...")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        fail(f"{description} failed.\n{details}")


def baseline_is_current(project: Path, report: Path, transcript: Path) -> bool:
    if not report.exists() or not transcript.exists():
        return False
    try:
        return load_json(report).get("project_sha256") == project_sha256(project)
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def cuts_document(
    project: Path, baseline: dict[str, Any], arbiter: dict[str, Any]
) -> dict[str, Any]:
    cuts = []
    for candidate in arbiter.get("candidates") or []:
        cuts.append({
            "start_ms": candidate["start_ms"],
            "end_ms": candidate["end_ms"],
            "removed_text": candidate.get("removed_text") or "",
            "reason": (
                "gemini_personalized_"
                + str(candidate.get("planner_category") or "candidate")
            ),
            "confidence": "high",
            "kept_text": candidate.get("kept_text") or "",
            "preference_decision": candidate.get("preference_decision"),
        })
    return {
        "schema_version": 2,
        "coordinate_space": "source",
        "project_sha256": baseline.get("project_sha256") or project_sha256(project),
        "cuts": cuts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--preferences", type=Path, default=DEFAULT_PREFERENCES)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--force-analysis", action="store_true")
    args = parser.parse_args()

    project = args.project.expanduser().resolve()
    preferences = args.preferences.expanduser().resolve()
    if not (project / "project.json").exists() or not (project / "recording").is_dir():
        fail(f"Not a Screen Studio project: {project}")
    if not preferences.exists():
        fail(
            f"Creator preferences do not exist: {preferences}. Build them with "
            "preference_edit_arbiter.py build first."
        )

    baseline_report = project / "baseline-report.json"
    transcript = project / "baseline-report.transcript.edit.json"
    planner_report = project / "global-video-planner-gemini35flash-v4.json"
    planner_work = project / "global-video-work-gemini35flash-v4"
    arbiter_report = project / "smart-edit-report.json"
    cuts_path = project / "smart-edit-cuts.json"
    final_report = project / "smart-edit-final-report.json"

    if args.force_analysis or not baseline_is_current(
        project, baseline_report, transcript
    ):
        run(
            [
                sys.executable,
                str(SCRIPT_DIR / "process.py"),
                "--project", str(project),
                "--pause-threshold", "800",
                "--min-pause", "300",
                "--pause-source", "silence",
                "--asr-backend", "bailian",
                "--language", "zh",
                "--dry-run",
                "--report-output", str(baseline_report),
            ],
            "local audio, transcript, and activity analysis",
        )
    else:
        print("[smart-edit] Reusing current baseline analysis.")

    run(
        [sys.executable, str(SCRIPT_DIR / "build_review_proxy.py"), str(project)],
        "aligned review proxy",
    )
    combined_video = project / "review-proxy" / "combined-timeline.mp4"
    run(
        [
            sys.executable,
            str(SCRIPT_DIR / "global_edit_planner.py"),
            "--transcript", str(transcript),
            "--video", str(combined_video),
            "--output", str(planner_report),
            "--work-dir", str(planner_work),
            "--model", args.model,
            "--resume",
        ],
        "Gemini whole-timeline paper edit",
    )
    run(
        [
            sys.executable,
            str(SCRIPT_DIR / "preference_edit_arbiter.py"),
            "decide",
            "--project", str(project),
            "--preferences", str(preferences),
            "--output", str(arbiter_report),
            "--model", args.model,
            "--resume",
        ],
        "Gemini creator-style arbitration",
    )

    baseline = load_json(baseline_report)
    arbiter = load_json(arbiter_report)
    write_json(cuts_path, cuts_document(project, baseline, arbiter))
    final_command = [
        sys.executable,
        str(SCRIPT_DIR / "process.py"),
        "--project", str(project),
        "--skip-transcribe", str(transcript),
        "--cuts-file", str(cuts_path),
        "--pause-threshold", "800",
        "--min-pause", "300",
        "--pause-source", "silence",
        "--asr-backend", "bailian",
        "--language", "zh",
    ]
    if not args.apply:
        final_command.extend(["--dry-run", "--report-output", str(final_report)])
    run(
        final_command,
        "final timeline audit" if not args.apply else "applying verified timeline",
    )

    report = load_json(final_report) if final_report.exists() and not args.apply else {}
    summary = {
        "project": str(project),
        "model": args.model,
        "applied": args.apply,
        "planner_candidates": load_json(planner_report).get("candidate_count"),
        "style_candidates": arbiter.get("candidate_count"),
        "accepted_smart_cuts": arbiter.get("accepted_count"),
        "safety_blocked": arbiter.get("safety_blocked_count"),
        "cuts": str(cuts_path),
        "audit": str(final_report) if not args.apply else str(project / "autoedit-report.json"),
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
