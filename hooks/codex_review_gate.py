#!/usr/bin/env python3
"""
Codex Review Gate — Hook for Claude Code

Detects file changes, tracks pending reviews, and enforces Codex review
before session exit. Works with any project — no hardcoded paths.

Part of the codex-review plugin: https://github.com/hugotomita1201/codex-review
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Configuration ────────────────────────────────────────────────────

PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
STATE_PATH = PROJECT_DIR / ".claude" / "reviews" / "status.json"
CONFIG_PATH = PROJECT_DIR / ".codex-review.json"
PLANS_DIR = PROJECT_DIR / ".claude" / "plans"

SKIP_RE = re.compile(r"\b(skip codex|no codex|no review needed)\b", re.IGNORECASE)

# Default configuration (overridden by .codex-review.json if present)
DEFAULT_CONFIG = {
    "planPaths": [".claude/plans/"],
    "ignorePaths": [".claude/", ".git/", "node_modules/", "dist/", "build/", "__pycache__/"],
    "codeExtensions": [
        ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".yaml", ".yml",
        ".toml", ".py", ".sh", ".bash", ".sql", ".html", ".css", ".scss",
        ".vue", ".svelte", ".graphql", ".gql", ".go", ".rs", ".java", ".kt",
        ".swift", ".rb", ".php", ".c", ".cpp", ".h", ".hpp",
    ],
    "promptPaths": ["/prompts/"],
    "specialFiles": ["Dockerfile", "Makefile", "Procfile", "render.yaml", "docker-compose.yml"],
    "timeout": 120,
    "autoReview": True,
    "circuitBreaker": True,
    "model": "gpt-5.4",
    "reasoningEffort": "xhigh",
}


def load_config() -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user_config = json.loads(CONFIG_PATH.read_text())
            config.update(user_config)
        except (json.JSONDecodeError, OSError):
            pass
    return config


CONFIG = load_config()
IMPL_EXTENSIONS = set(CONFIG["codeExtensions"])
IGNORED_PREFIXES = tuple(CONFIG["ignorePaths"])
SPECIAL_FILES = set(CONFIG["specialFiles"])
REVIEWS_DIR = PROJECT_DIR / ".claude" / "reviews"


def scope_hash(files: list[str]) -> str:
    """Hash sorted file list to identify review scope."""
    return hashlib.md5("|".join(sorted(files)).encode()).hexdigest()[:8]


# ── State management ─────────────────────────────────────────────────

def default_state() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "plan_pending": False, "plan_files": [],
        "impl_pending": False, "impl_files": [],
        "bypass": False, "last_reviewed_at": None,
        # v2: scoped review tracking
        "plan_review": {"scope_hash": None, "round": 0, "previous_findings": None},
        "impl_review": {"scope_hash": None, "round": 0, "previous_findings": None},
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        raw = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return default_state()
    # Migrate v1 → v2
    if raw.get("schema_version", 1) < 2:
        return {
            "schema_version": 2,
            "plan_pending": raw.get("plan_review", {}).get("pending", False),
            "plan_files": raw.get("plan_review", {}).get("files", []),
            "impl_pending": raw.get("implementation_review", {}).get("pending", False),
            "impl_files": raw.get("implementation_review", {}).get("files", []),
            "bypass": raw.get("review_bypass", {}).get("active", False),
            "last_reviewed_at": None,
            "plan_review": {"scope_hash": None, "round": 0, "previous_findings": None},
            "impl_review": {"scope_hash": None, "round": 0, "previous_findings": None},
        }
    state = default_state()
    state.update({k: v for k, v in raw.items() if k in state})
    # Ensure nested review dicts have all expected keys
    for key in ("plan_review", "impl_review"):
        defaults = {"scope_hash": None, "round": 0, "previous_findings": None}
        if isinstance(state.get(key), dict):
            merged = dict(defaults)
            merged.update(state[key])
            state[key] = merged
        else:
            state[key] = defaults
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(STATE_PATH)


# ── File classification ──────────────────────────────────────────────

def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return str(path)


def is_plan(path: Path) -> bool:
    r = rel(path)
    return any(r.startswith(p) for p in CONFIG["planPaths"])


def is_impl(path: Path) -> bool:
    r = rel(path)
    if any(r.startswith(p) for p in IGNORED_PREFIXES):
        return False
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        lo = r.lower()
        return any(p in f"/{lo}" for p in CONFIG["promptPaths"]) or Path(lo).name.startswith("prompt")
    if suffix in IMPL_EXTENSIONS:
        return True
    return path.name in SPECIAL_FILES


def extract_paths(payload: dict[str, Any]) -> list[Path]:
    if payload.get("tool_name") not in {"Write", "Edit", "MultiEdit"}:
        return []
    paths: list[Path] = []
    for src in (payload.get("tool_input") or {}, payload.get("tool_response") or {}):
        for key in ("file_path", "filePath", "path"):
            v = src.get(key)
            if isinstance(v, str) and v:
                p = Path(v) if Path(v).is_absolute() else PROJECT_DIR / v
                paths.append(p.resolve())
    return list(dict.fromkeys(paths))  # dedupe, preserve order


def fmt(files: list[str]) -> str:
    if not files:
        return "current task scope"
    if len(files) <= 3:
        return ", ".join(files)
    return f"{', '.join(files[:3])}, +{len(files) - 3} more"


# ── Hook output helpers ──────────────────────────────────────────────

def context(event: str, msg: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": event, "additionalContext": msg,
    }})


def block(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def add_files(current: list[str], new: list[str]) -> list[str]:
    seen = set(current)
    out = list(current)
    for f in new:
        if f and f not in seen:
            out.append(f)
            seen.add(f)
    return out


# ── Pending summary ──────────────────────────────────────────────────

def pending_lines(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if state["plan_pending"]:
        lines.append(f"Plan review pending for {fmt(state['plan_files'])}")
    if state["impl_pending"]:
        lines.append(f"Implementation review pending for {fmt(state['impl_files'])}")
    return lines


# ── Review archive helpers ────────────────────────────────────────────

def archive_review(output_path: str, review_type: str, state: dict) -> str | None:
    """Copy codex output to durable location and return the path."""
    src = Path(output_path)
    if not src.exists() or src.stat().st_size == 0:
        return None
    review = state.get(f"{review_type}_review", {})
    h = review.get("scope_hash", "unknown")
    r = review.get("round", 1)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    dest = REVIEWS_DIR / f"{review_type}-{h}-r{r}.txt"
    shutil.copy2(str(src), str(dest))
    return str(dest.relative_to(PROJECT_DIR))


def read_summary(path: str, lines: int = 3) -> str:
    """Read first N lines of a file for preview."""
    try:
        with open(path) as f:
            return "\n".join(f.readline().rstrip() for _ in range(lines))
    except (OSError, IOError):
        return ""


# ── Codex CLI check ──────────────────────────────────────────────────

def check_codex_available() -> bool:
    return shutil.which("codex") is not None


# ── Event handlers ───────────────────────────────────────────────────

def handle_post_tool(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    tool = payload.get("tool_name")

    # Track file changes from Write/Edit/MultiEdit
    if tool in {"Write", "Edit", "MultiEdit"}:
        touched = extract_paths(payload)
        plan_files = [rel(p) for p in touched if is_plan(p)]
        impl_files = [rel(p) for p in touched if not is_plan(p) and is_impl(p)]

        notes: list[str] = []
        if plan_files:
            state["plan_pending"] = True
            state["plan_files"] = add_files(state["plan_files"], plan_files)
            # v2: track scope hash and round
            new_hash = scope_hash(state["plan_files"])
            pr = state.setdefault("plan_review", {"scope_hash": None, "round": 0, "previous_findings": None})
            if pr["scope_hash"] == new_hash:
                pr["round"] += 1
            else:
                pr["scope_hash"] = new_hash
                pr["round"] = 1
            round_info = f" (round {pr['round']})" if pr["round"] > 1 else ""
            prev_info = f" Previous findings: {pr['previous_findings']}" if pr["previous_findings"] else ""
            notes.append(
                f"Codex plan review required{round_info}. Mode: plan-review. "
                f"Files: {fmt(plan_files)}.{prev_info} "
                'Run Skill({{ skill: "codex-review" }}) now.'
            )
        if impl_files:
            state["impl_pending"] = True
            state["impl_files"] = add_files(state["impl_files"], impl_files)
            # v2: track scope hash and round
            new_hash = scope_hash(state["impl_files"])
            ir = state.setdefault("impl_review", {"scope_hash": None, "round": 0, "previous_findings": None})
            if ir["scope_hash"] == new_hash:
                ir["round"] += 1
            else:
                ir["scope_hash"] = new_hash
                ir["round"] = 1
            round_info = f" (round {ir['round']})" if ir["round"] > 1 else ""
            prev_info = f" Previous findings: {ir['previous_findings']}" if ir["previous_findings"] else ""
            notes.append(
                f"Codex implementation review required{round_info}. Mode: impl-review. "
                f"Changed files: {fmt(impl_files)}.{prev_info} "
                'Run Skill({{ skill: "codex-review" }}) now. '
                "Fill in Goal + Invariants before running."
            )
        if notes and not state["bypass"]:
            return context("PostToolUse", "\n".join(notes))
        return None

    # Detect Codex completion from Bash
    if tool == "Bash":
        cmd = str((payload.get("tool_input") or {}).get("command", ""))

        if "codex exec" in cmd or "codex-review" in cmd:
            resp = payload.get("tool_response") or {}
            out = str(resp.get("stdout") or resp.get("output") or "")
            if "stopped" in out.lower() or "killed" in out.lower():
                return context("PostToolUse",
                    "Codex was stopped before completion. Output may be stale. Re-run if needed.")

            # v2: parse -o <path> from command string
            output_match = re.search(r"-o\s+(\S+)", cmd)
            output_path = output_match.group(1) if output_match else None

            cleared: list[str] = []
            archive_paths: list[str] = []
            summary_lines: list[str] = []

            # v2: use output filename for review type detection (more reliable than command text)
            output_name = Path(output_path).name.lower() if output_path else ""

            # Detect plan review completion
            if ("plan" in output_name or re.search(r'\bplan\b', cmd.lower())) and state["plan_pending"]:
                state["plan_pending"] = False
                state["plan_files"] = []
                state["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
                cleared.append("plan review")
                if output_path:
                    archived = archive_review(output_path, "plan", state)
                    if archived:
                        archive_paths.append(archived)
                        state["plan_review"]["previous_findings"] = archived
                    preview = read_summary(output_path)
                    if preview:
                        summary_lines.append(preview)
            # Detect implementation review completion (word boundary to avoid "code" matching "codex")
            if (re.search(r'\b(?:code|targeted|impl)\b', output_name) or re.search(r'\b(?:code|targeted|impl)\b', cmd.lower())) and state["impl_pending"]:
                state["impl_pending"] = False
                state["impl_files"] = []
                state["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
                cleared.append("implementation review")
                if output_path:
                    archived = archive_review(output_path, "impl", state)
                    if archived:
                        archive_paths.append(archived)
                        state["impl_review"]["previous_findings"] = archived
                    preview = read_summary(output_path)
                    if preview:
                        summary_lines.append(preview)
            # Fallback: if codex exec completed and something was pending, clear it
            if not cleared and (state["plan_pending"] or state["impl_pending"]):
                if state["plan_pending"]:
                    state["plan_pending"] = False
                    state["plan_files"] = []
                    cleared.append("plan review")
                    if output_path:
                        archived = archive_review(output_path, "plan", state)
                        if archived:
                            archive_paths.append(archived)
                            state["plan_review"]["previous_findings"] = archived
                if state["impl_pending"]:
                    state["impl_pending"] = False
                    state["impl_files"] = []
                    cleared.append("implementation review")
                    if output_path:
                        archived = archive_review(output_path, "impl", state)
                        if archived:
                            archive_paths.append(archived)
                            state["impl_review"]["previous_findings"] = archived
                state["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
                if output_path:
                    preview = read_summary(output_path)
                    if preview:
                        summary_lines.append(preview)

            if cleared and not state["bypass"]:
                msg = f"Codex {' and '.join(cleared)} complete."
                if archive_paths:
                    msg += f" Archived to: {', '.join(archive_paths)}."
                if summary_lines:
                    msg += f"\nPreview:\n{chr(10).join(summary_lines)}"
                msg += "\nEvaluate findings and address issues."
                return context("PostToolUse", msg)

    return None


def handle_user_prompt(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    prompt = str(payload.get("prompt") or "")
    if SKIP_RE.search(prompt):
        state["bypass"] = True
        return context("UserPromptSubmit",
            "User waived Codex review for this task. Stop gate will allow exit.")

    if state["bypass"]:
        return None

    lines = pending_lines(state)
    if not lines:
        return None
    return context("UserPromptSubmit",
        "Pending Codex reviews:\n- " + "\n- ".join(lines)
        + '\nRun Skill({ skill: "codex-review" }) before presenting plan or claiming completion.')


def handle_session_start(state: dict[str, Any]) -> str | None:
    # Check if Codex CLI is installed
    if not check_codex_available():
        return context("SessionStart",
            "Codex CLI not found. Install it with: npm install -g @openai/codex\n"
            "Then set OPENAI_API_KEY in your environment.\n"
            "The codex-review plugin requires Codex CLI to function.")

    if state["bypass"]:
        return None
    lines = pending_lines(state)
    if not lines:
        return None
    return context("SessionStart",
        "Pending from previous session:\n- " + "\n- ".join(lines)
        + '\nRun Skill({ skill: "codex-review" }) before presenting plan or claiming completion.')


def handle_stop(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    if state["bypass"]:
        state["plan_pending"] = state["impl_pending"] = False
        state["plan_files"] = state["impl_files"] = []
        state["bypass"] = False
        return None

    lines = pending_lines(state)
    if not lines:
        return None

    # Circuit breaker: block once, allow on second attempt
    if not CONFIG.get("circuitBreaker", True):
        return None
    if payload.get("stop_hook_active"):
        return None

    return block(
        "Codex review still pending:\n- " + "\n- ".join(lines)
        + '\nRun Skill({ skill: "codex-review" }) and address findings before stopping.\n'
        + 'Say "skip codex" to bypass.'
    )


# ── Entry point ──────────────────────────────────────────────────────

def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.load(sys.stdin) if not sys.stdin.isatty() else {}
    state = load_state()

    handlers = {
        "post-tool": handle_post_tool,
        "user-prompt": handle_user_prompt,
        "session-start": lambda _p, s: handle_session_start(s),
        "stop": handle_stop,
    }
    handler = handlers.get(mode)
    response = handler(payload, state) if handler else None

    save_state(state)
    if response:
        sys.stdout.write(response)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit(0)
