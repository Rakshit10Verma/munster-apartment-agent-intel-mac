from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent

SECRET_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|password|passwd|secret|token|cookie|authorization|credential|"
    r"storage[_-]?state|session[_-]?id|bewerbermappe[_-]?path|browser[_-]?profile)",
    re.I,
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|password|passwd|client[_-]?secret|access[_-]?token|"
        r"refresh[_-]?token|authorization|cookie)\b\s*[:=]\s*[^\s,;]+"
    ),
)
URL_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:access[_-]?token|refresh[_-]?token|token|session(?:id)?|auth|"
    r"api[_-]?key|key|signature|sig|code)=)[^&#\s]+"
)

RUNTIME_ALLOWLIST = (
    "AUTO_SEND",
    "DRY_RUN",
    "AI_PROVIDER_ORDER",
    "AI_TIMEOUT_SECONDS",
    "ANTHROPIC_TIMEOUT_SECONDS",
    "OPENAI_TIMEOUT_SECONDS",
    "GEMINI_TIMEOUT_SECONDS",
    "AI_MAX_RETRIES",
    "AI_PROVIDER_COOLDOWN_SECONDS",
    "WG_USE_PREMIUM_BOOST",
    "WG_PREMIUM_STRICT",
    "ENABLE_GMAIL",
    "POLL_INTERVAL_SECONDS",
    "LOG_LEVEL",
)
RUNTIME_BOOLEAN_KEYS = {
    "AUTO_SEND",
    "DRY_RUN",
    "WG_USE_PREMIUM_BOOST",
    "WG_PREMIUM_STRICT",
    "ENABLE_GMAIL",
}
RUNTIME_INTEGER_KEYS = {
    "AI_TIMEOUT_SECONDS",
    "ANTHROPIC_TIMEOUT_SECONDS",
    "OPENAI_TIMEOUT_SECONDS",
    "GEMINI_TIMEOUT_SECONDS",
    "AI_MAX_RETRIES",
    "AI_PROVIDER_COOLDOWN_SECONDS",
    "POLL_INTERVAL_SECONDS",
}

CSV_FIELDS = (
    "db_id",
    "platform",
    "wg_gesucht_listing_id",
    "url",
    "discovered_at",
    "updated_at",
    "title",
    "listing_type",
    "advertiser_type",
    "rent_cold_eur",
    "rent_warm_eur",
    "deposit_eur",
    "one_time_fee_eur",
    "furniture_takeover_eur",
    "room_size_m2",
    "flat_size_m2",
    "location",
    "available_from",
    "available_to",
    "minimum_duration_months",
    "wg_size_or_realistic_residents",
    "total_rooms",
    "age_min",
    "age_max",
    "age_requirement_strength",
    "age_mismatch",
    "women_only",
    "wbs_required",
    "religion_or_membership_required",
    "furnishing",
    "raw_listing_text",
    "current_processing_status",
    "final_decision",
    "decision_reason",
    "filter_reasons",
    "warnings",
    "review_reason",
    "hidden_questions_detected",
    "hidden_commands_detected",
    "answered_hidden_question_ids",
    "applied_hidden_command_ids",
    "ai_attempted",
    "ai_result",
    "ai_provider",
    "ai_model",
    "ai_latency_ms",
    "ai_input_tokens",
    "ai_output_tokens",
    "ai_failure_reason",
    "fallback_used",
    "fallback_scenario",
    "fallback_template",
    "fallback_optional_clauses",
    "fallback_hidden_answers",
    "message_source",
    "final_message_subject",
    "final_generated_message",
    "attachment_policy",
    "bewerbermappe_status",
    "current_auto_send_gate",
    "current_dry_run_gate",
    "already_contacted_check",
    "dedup_key",
    "deduplication_result",
    "premium_boost_result",
    "contact_mode",
    "contact_channel",
    "send_attempt_created_at",
    "send_clicked_at",
    "send_confirmed_at",
    "send_result",
    "send_state",
    "reconciliation_state",
    "contact_detail",
    "error_information",
)


@dataclass(frozen=True)
class AuditExportResult:
    directory: Path
    zip_path: Path | None
    listing_count: int


class AuditExportError(RuntimeError):
    pass


def _json_load(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _read_env(root: Path) -> dict[str, str]:
    path = root / ".env"
    if not path.is_file():
        return {}
    return {key: str(value) for key, value in dotenv_values(path).items() if value is not None}


def _secret_values(environment: dict[str, str]) -> list[str]:
    values = []
    for key, value in environment.items():
        if SECRET_KEY_PATTERN.search(key) and len(value.strip()) >= 4:
            values.append(value.strip())
    return sorted(set(values), key=len, reverse=True)


def _redact_text(value: str, secrets: list[str]) -> str:
    result = value
    for secret in secrets:
        result = result.replace(secret, "[REDACTED]")
    result = URL_SECRET_PATTERN.sub(r"\1[REDACTED]", result)
    for pattern in SECRET_TEXT_PATTERNS:
        result = pattern.sub(
            lambda match: (
                f"{match.group(1)}=[REDACTED]" if match.lastindex else "Bearer [REDACTED]"
            ),
            result,
        )
    return result


def _sanitize(value: Any, secrets: list[str], *, key: str = "") -> Any:
    if key and (SECRET_KEY_PATTERN.search(key) or key.casefold().endswith("_path")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize(item, secrets, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, tuple):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: json.dumps(row.get(field), ensure_ascii=False, default=str)
                    if isinstance(row.get(field), (dict, list, tuple))
                    else row.get(field, "")
                    for field in fields
                }
            )
    path.chmod(0o600)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise AuditExportError(f"cannot open SQLite database read-only: {path}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _resolve_database(root: Path, environment: dict[str, str], override: Path | None) -> Path:
    if override is not None:
        path = override.expanduser()
    else:
        configured = environment.get("DATABASE_PATH", "data/apartment_agent.sqlite")
        path = Path(configured).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise AuditExportError(f"SQLite database does not exist: {path}")
    return path.resolve()


def _rows(
    connection: sqlite3.Connection, query: str, parameters: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def _review_reasons(
    row: dict[str, Any], facts: dict[str, Any], decision: dict[str, Any], validation: dict[str, Any]
) -> list[str]:
    reasons = list(validation.get("errors") or [])
    reasons.extend(decision.get("hard_skip_reasons") or [])
    unresolved = facts.get("unresolved_required_facts") or []
    critical = facts.get("critical_ambiguities") or []
    if unresolved:
        reasons.append("unresolved required facts: " + ", ".join(map(str, unresolved)))
    if critical:
        reasons.append("critical ambiguities: " + ", ".join(map(str, critical)))
    if row.get("error"):
        reasons.append(str(row["error"]))
    if not reasons and decision.get("decision") not in (None, "APPLY"):
        reasons.append(f"rule decision is {decision.get('decision')}, not APPLY")
    return list(dict.fromkeys(reasons))


def _latest_attempt(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {}
    return max(attempts, key=lambda row: (str(row.get("created_at") or ""), int(row["id"])))


def _summary_row(
    row: dict[str, Any],
    attempts: list[dict[str, Any]],
    runtime: dict[str, Any],
    secrets: list[str],
) -> dict[str, Any]:
    facts = _json_load(row.get("facts_json"), {})
    decision = _json_load(row.get("decision_json"), {})
    validation = _json_load(row.get("validation_json"), {})
    attachment = _json_load(row.get("attachment_json"), {})
    trace = _json_load(row.get("generation_trace_json"), {})
    message_source = str(row.get("message_source") or "none")
    latest = _latest_attempt(attempts)
    actual = _latest_attempt([attempt for attempt in attempts if attempt.get("mode") == "actual"])
    relevant_attempt = actual or latest
    ai_attempted = bool(trace.get("ai_attempted") or row.get("provider") or row.get("error"))
    ai_failure = trace.get("ai_failure_reason") or row.get("error") or ""
    if row.get("provider"):
        ai_result = "succeeded"
    elif ai_attempted and ai_failure:
        ai_result = "failed"
    elif ai_attempted:
        ai_result = "attempted; result not stored"
    else:
        ai_result = "not attempted"
    fallback_used = bool(trace.get("fallback_used") or message_source.startswith("universal_"))
    review_reasons = _review_reasons(row, facts, decision, validation)
    hard_filters = decision.get("hard_skip_reasons") or []
    warnings = list(
        dict.fromkeys([*(decision.get("warnings") or []), *(validation.get("warnings") or [])])
    )
    attachment_safe = _sanitize(attachment, secrets)
    dedup_count = row.get("dedup_record_count")
    reconciliation_state = (
        str(relevant_attempt.get("status") or "") if relevant_attempt.get("confirmed_at") else ""
    )
    result = {
        "db_id": row.get("id"),
        "platform": row.get("platform"),
        "wg_gesucht_listing_id": row.get("listing_id"),
        "url": row.get("canonical_url"),
        "discovered_at": row.get("discovered_at"),
        "updated_at": row.get("updated_at"),
        "title": row.get("title"),
        "listing_type": facts.get("housing_type"),
        "advertiser_type": facts.get("advertiser_type"),
        "rent_cold_eur": facts.get("cold_rent_eur"),
        "rent_warm_eur": facts.get("warm_rent_eur"),
        "deposit_eur": facts.get("deposit_eur"),
        "one_time_fee_eur": facts.get("one_time_fee_eur"),
        "furniture_takeover_eur": facts.get("furniture_takeover_eur"),
        "room_size_m2": facts.get("room_size_m2"),
        "flat_size_m2": facts.get("flat_size_m2"),
        "location": facts.get("location"),
        "available_from": facts.get("move_in"),
        "available_to": facts.get("end_date"),
        "minimum_duration_months": facts.get("minimum_duration_months"),
        "wg_size_or_realistic_residents": facts.get("realistic_residents"),
        "total_rooms": facts.get("total_rooms"),
        "age_min": facts.get("age_min"),
        "age_max": facts.get("age_max"),
        "age_requirement_strength": facts.get("age_requirement_strength"),
        "age_mismatch": facts.get("age_mismatch"),
        "women_only": facts.get("women_only"),
        "wbs_required": facts.get("wbs_required"),
        "religion_or_membership_required": bool(
            facts.get("religion_or_confession_required")
            or facts.get("religious_fraternity_or_membership_required")
        ),
        "furnishing": facts.get("furnishing"),
        "raw_listing_text": row.get("raw_text"),
        "current_processing_status": row.get("status"),
        "final_decision": decision.get("decision"),
        "decision_reason": [*hard_filters, *warnings],
        "filter_reasons": hard_filters,
        "warnings": warnings,
        "review_reason": review_reasons,
        "hidden_questions_detected": facts.get("hidden_questions") or [],
        "hidden_commands_detected": facts.get("hidden_commands") or [],
        "answered_hidden_question_ids": _json_load(row.get("message_answered_question_ids"), []),
        "applied_hidden_command_ids": _json_load(row.get("message_applied_command_ids"), []),
        "ai_attempted": ai_attempted,
        "ai_result": ai_result,
        "ai_provider": row.get("provider"),
        "ai_model": row.get("model"),
        "ai_latency_ms": row.get("latency_ms"),
        "ai_input_tokens": row.get("input_tokens"),
        "ai_output_tokens": row.get("output_tokens"),
        "ai_failure_reason": ai_failure,
        "fallback_used": fallback_used,
        "fallback_scenario": trace.get("fallback_scenario"),
        "fallback_template": trace.get("fallback_template"),
        "fallback_optional_clauses": trace.get("optional_clauses") or [],
        "fallback_hidden_answers": trace.get("hidden_question_answers") or [],
        "message_source": message_source,
        "final_message_subject": row.get("message_subject"),
        "final_generated_message": row.get("message_body"),
        "attachment_policy": attachment_safe,
        "bewerbermappe_status": relevant_attempt.get("attachment_state"),
        "current_auto_send_gate": runtime.get("AUTO_SEND"),
        "current_dry_run_gate": runtime.get("DRY_RUN"),
        "already_contacted_check": "detected" if row.get("status") == "already_contacted" else "",
        "dedup_key": row.get("dedup_key"),
        "deduplication_result": "unique_db_record" if dedup_count == 1 else "",
        "premium_boost_result": relevant_attempt.get("premium_state"),
        "contact_mode": relevant_attempt.get("mode"),
        "contact_channel": relevant_attempt.get("channel"),
        "send_attempt_created_at": actual.get("created_at"),
        "send_clicked_at": actual.get("send_clicked_at"),
        "send_confirmed_at": actual.get("confirmed_at"),
        "send_result": relevant_attempt.get("status"),
        "send_state": relevant_attempt.get("status") or row.get("status"),
        "reconciliation_state": reconciliation_state,
        "contact_detail": relevant_attempt.get("detail"),
        "error_information": row.get("error"),
    }
    return _sanitize(result, secrets)


def _indented(value: Any) -> str:
    if value in (None, "", [], {}):
        text = "(not stored)"
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return "\n".join(f"    {line}" for line in text.splitlines())


def _listing_markdown(
    summary: dict[str, Any],
    facts: dict[str, Any],
    decision: dict[str, Any],
    validation: dict[str, Any],
    attachment: dict[str, Any],
    trace: dict[str, Any],
    attempts: list[dict[str, Any]],
    events: list[dict[str, Any]],
    logs: list[dict[str, Any]],
) -> str:
    return f"""# Listing {summary["db_id"]}: {summary.get("title") or "(no title)"}

Exported current status: `{summary.get("current_processing_status") or ""}`  
WG-Gesucht ID: `{summary.get("wg_gesucht_listing_id") or ""}`  
URL: {summary.get("url") or "(not stored)"}  
Discovered: `{summary.get("discovered_at") or ""}`  
Last updated: `{summary.get("updated_at") or ""}`

## A. Original listing

{_indented(summary.get("raw_listing_text"))}

## B. Extracted facts

{_indented(facts)}

## C. Filtering decision

{_indented(decision)}

## D. Reasons

Filter reasons:

{_indented(summary.get("filter_reasons"))}

Review reason:

{_indented(summary.get("review_reason"))}

Warnings:

{_indented(summary.get("warnings"))}

Validation:

{_indented(validation)}

## E. Hidden questions and commands

Questions detected:

{_indented(summary.get("hidden_questions_detected"))}

Commands detected:

{_indented(summary.get("hidden_commands_detected"))}

Answered question IDs:

{_indented(summary.get("answered_hidden_question_ids"))}

Applied command IDs:

{_indented(summary.get("applied_hidden_command_ids"))}

## F. AI and fallback generation

AI attempted: `{summary.get("ai_attempted")}`  
AI result: `{summary.get("ai_result")}`  
Provider/model: `{summary.get("ai_provider") or ""}` / `{summary.get("ai_model") or ""}`  
Message source: `{summary.get("message_source") or ""}`  
AI failure reason:

{_indented(summary.get("ai_failure_reason"))}

Generation trace:

{_indented(trace)}

## G. Exact final application message

Subject:

{_indented(summary.get("final_message_subject"))}

Body:

{_indented(summary.get("final_generated_message"))}

## H. Send gates

Current AUTO_SEND: `{summary.get("current_auto_send_gate")}`  
Current DRY_RUN: `{summary.get("current_dry_run_gate")}`  
Already-contacted detection: `{summary.get("already_contacted_check") or "(not stored)"}`  
Deduplication: `{summary.get("deduplication_result") or "(not stored)"}`  
Premium state: `{summary.get("premium_boost_result") or "(not stored)"}`  
Bewerbermappe state: `{summary.get("bewerbermappe_status") or "(not stored)"}`

Attachment policy:

{_indented(attachment)}

## I. Send attempts and result

Current send state: `{summary.get("send_state") or "(not stored)"}`  
Reconciliation state: `{summary.get("reconciliation_state") or "(not separately stored)"}`

{_indented(attempts)}

## J. Errors and related lifecycle entries

Listing error:

{_indented(summary.get("error_information"))}

Database events:

{_indented(events)}

Structured application log entries carrying this DB listing ID:

{_indented(logs)}
"""


def _load_logs(
    root: Path, selected_ids: set[int] | None, secrets: list[str]
) -> list[dict[str, Any]]:
    path = root / "logs" / "agent.jsonl"
    if not path.is_file():
        return []
    recent: deque[dict[str, Any]] = deque(maxlen=5000)
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                item = {"unparsed_log_line": line.rstrip("\n")}
            if selected_ids is not None:
                raw_listing_id = item.get("listing_id")
                if raw_listing_id is None:
                    continue
                try:
                    log_listing_id = int(raw_listing_id)
                except (TypeError, ValueError):
                    continue
                if log_listing_id not in selected_ids:
                    continue
            recent.append(_sanitize(item, secrets))
    return list(recent)


def _write_logs(
    root: Path,
    destination: Path,
    log_rows: list[dict[str, Any]],
    secrets: list[str],
    *,
    include_console: bool,
) -> None:
    destination.mkdir(mode=0o700)
    _write_text(
        destination / "agent.jsonl",
        "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in log_rows),
    )
    console_path = root / "logs" / "daemon-console.log"
    if include_console and console_path.is_file():
        lines: deque[str] = deque(maxlen=500)
        with console_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                lines.append(_redact_text(line, secrets))
        _write_text(destination / "daemon-console.log", "".join(lines))


def _runtime_snapshot(environment: dict[str, str]) -> dict[str, Any]:
    def convert(key: str, value: str) -> Any:
        lowered = value.strip().casefold()
        if key in RUNTIME_BOOLEAN_KEYS:
            if lowered in {"true", "yes", "on", "1"}:
                return True
            if lowered in {"false", "no", "off", "0"}:
                return False
            return value
        if key in RUNTIME_INTEGER_KEYS:
            try:
                return int(value)
            except ValueError:
                return value
        return value

    return {key: convert(key, environment[key]) for key in RUNTIME_ALLOWLIST if key in environment}


def _write_config_snapshot(
    root: Path,
    destination: Path,
    runtime: dict[str, Any],
    secrets: list[str],
) -> None:
    destination.mkdir(mode=0o700)
    config_path = root / "config.yaml"
    config = (
        yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    )
    safe_config = {
        key: config.get(key)
        for key in ("applicant", "housing_rules", "documents", "generation", "wg_watch")
        if isinstance(config, dict) and key in config
    }
    safe_config["supported_fallback_listing_types"] = [
        "wg_room",
        "studio",
        "whole_flat",
        "zwischenmiete",
        "wohnheim",
        "student_room",
        "nachmieter",
    ]
    _write_text(
        destination / "decision_config.yaml",
        yaml.safe_dump(_sanitize(safe_config, secrets), allow_unicode=True, sort_keys=False),
    )
    answers_path = root / "answer_bank.yaml"
    answers = (
        yaml.safe_load(answers_path.read_text(encoding="utf-8")) if answers_path.is_file() else {}
    )
    _write_text(
        destination / "answer_bank.yaml",
        yaml.safe_dump(_sanitize(answers, secrets), allow_unicode=True, sort_keys=False),
    )
    _write_json(destination / "runtime_gates.json", _sanitize(runtime, secrets))


def _daemon_state(root: Path) -> str:
    pid_path = root / "data" / "agent.pid"
    if not pid_path.is_file():
        return "stopped"
    digits = "".join(
        character for character in pid_path.read_text(encoding="utf-8") if character.isdigit()
    )
    if not digits:
        return "stopped (invalid PID file)"
    try:
        os.kill(int(digits), 0)
    except (OSError, ValueError):
        return "stopped (stale PID file)"
    return f"running (PID {digits})"


def _status_text(
    root: Path,
    database_path: Path,
    listings: list[dict[str, Any]],
    watcher_states: list[dict[str, Any]],
    watcher_seen: list[dict[str, Any]],
    scope_id: int | None,
    exported_at: datetime,
) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in listings:
        counts[str(row.get("status") or "unknown")] += 1
    today = exported_at.astimezone(UTC).date().isoformat()
    discovered_today = sum(
        1
        for row in watcher_seen
        if not row.get("is_baseline") and str(row.get("first_seen_at") or "") >= today
    )
    lines = [
        f"Audit snapshot: {exported_at.isoformat()}",
        f"Database: {database_path}",
        f"Scope: {'DB ID ' + str(scope_id) if scope_id is not None else 'all listings'}",
        f"Daemon: {_daemon_state(root)}",
        "",
        "Watcher state:",
    ]
    if watcher_states:
        for row in watcher_states:
            lines.append(
                f"- {row.get('search_name')}: status={row.get('status')}, "
                f"last_checked={row.get('last_checked_at')}, "
                f"last_success={row.get('last_success_at')}, "
                f"next_check={row.get('next_check_at')}, "
                f"errors={row.get('consecutive_errors')}, detail={row.get('detail')}"
            )
    else:
        lines.append("- no watcher state stored")
    lines.extend(
        [
            f"Listings discovered by watcher today: {discovered_today}",
            f"Queue depth (discovered): {counts.get('discovered', 0)}",
            "",
            "Counts by current status:",
        ]
    )
    for status, count in sorted(counts.items()):
        lines.append(f"- {status}: {count}")
    lines.extend(["", "Recent listings:"])
    for row in sorted(listings, key=lambda item: str(item.get("updated_at") or ""), reverse=True)[
        :10
    ]:
        lines.append(
            f"- DB {row.get('id')} | WG {row.get('listing_id') or '-'} | "
            f"{row.get('status')} | {row.get('message_source') or 'none'} | "
            f"{row.get('title') or '(no title)'} | {row.get('updated_at')}"
        )
    human_attention = sum(
        counts.get(status, 0)
        for status in ("review_required", "send_state_unknown", "ai_failed", "premium_boost_failed")
    )
    lines.extend(["", f"Human-attention count: {human_attention}", ""])
    return "\n".join(lines)


def _readme_text(
    database_path: Path,
    scope_id: int | None,
    exported_at: datetime,
    listing_count: int,
) -> str:
    scope = f"only DB ID {scope_id}" if scope_id is not None else "all stored listings"
    return f"""# Münster Apartment Agent — read-only audit export

Created: `{exported_at.isoformat()}`  
Source database: `{database_path}`  
Scope: {scope} ({listing_count} listing record(s))

This bundle was generated with SQLite `mode=ro` plus `PRAGMA query_only=ON`. The exporter does not
instantiate the application's normal `Database` class, analyze listings, reconcile conversations,
open a browser, or import/call any sending path.

## Contents

- `status.txt`: read-only equivalent of the operational status overview, including current counts,
  watcher state, queue depth, daemon PID state, and recent listings.
- `listings.csv`: one flattened row per listing. JSON-valued cells retain arrays/objects where a
  scalar would lose evidence. `raw_listing_text` and the exact final message are included.
- `listings/<DB_ID>.md`: readable A–J lifecycle reconstruction for one listing.
- `events.csv` / `events.jsonl`: chronological rows from SQLite `events`.
- `contact_attempts.csv`: persisted dry-run and actual contact attempts.
- `watcher_search_state.csv` / `watcher_seen_listings.csv`: watcher persistence evidence.
- `database_schema.sql`: table and index definitions only; the SQLite database itself is not copied.
- `logs/agent.jsonl`: up to the latest 5,000 sanitized structured application entries. A one-listing
  export includes only entries carrying that DB listing ID.
- `logs/daemon-console.log`: latest 500 sanitized console lines, when present.
- `config_snapshot/decision_config.yaml`: sanitized decision/profile/generation configuration.
- `config_snapshot/answer_bank.yaml`: sanitized deterministic answer bank.
- `config_snapshot/runtime_gates.json`: allowlisted current non-secret runtime gates only.

## Database tables used

- `listings`: source text, extracted facts, decision, validation, generation trace, exact message,
  provider metadata, status, and top-level error.
- `events`: discovery and analysis-status events with limited detail JSON.
- `contact_attempts`: dry-run/actual mode, contact status, send-click/confirmation timestamps,
  Premium/Bewerbermappe state when stored, browser URL, and attempt detail.
- `watcher_seen_listings`: deduplicated watcher IDs and baseline/new state.
- `watcher_search_state`: last/next watcher checks, health, errors, and summary detail.

## Status relationships

`discovered` is queued input. `filtered_skip` means a deterministic or reconciled hard rule rejected
the listing. `review_required` means a person must resolve uncertainty. `ai_failed` is a legacy or
unresolved AI failure. `drafted` has a stored message that has not necessarily entered browser
preparation. `dry_run_ready` reached the composer and stopped before Send. `already_contacted`
records site-side existing-conversation detection. `premium_boost_failed` and
`bewerbermappe_attachment_failed` are browser gate failures. `sent` is a stored successful result.
`send_state_unknown` means Send may have been clicked but final confirmation is inconclusive; it
must be reconciled manually and never blindly retried. `manually_rejected` and `not_sent` may also
appear after human review/reconciliation.

The `listings.status` value is the current state and can overwrite an earlier state. Use `events`
and `contact_attempts` for the available history, noting the limitations below.

## Information that cannot always be reconstructed

- Historical values of `AUTO_SEND`, `DRY_RUN`, and other environment gates at each old event are
  not stored. CSV gate columns and `runtime_gates.json` show the export-time values only.
- Raw AI prompts/responses and every individual provider attempt are not stored. Only final provider
  metadata, aggregate errors, the generation trace (for newer records), and final message survive.
- Deterministic prefilter signal lists are not persisted separately; extracted facts, rule reasons,
  validation, and status are the surviving evidence.
- Events do not capture every contact-status transition, and their detail JSON is usually limited
  to validation errors. `listings.status` may therefore have transitions absent from `events`.
- Older contact rows may lack `send_clicked_at`, Premium state, attachment state, fingerprints, or
  confirmation timestamps. Blank fields mean unavailable, not a negative result.
- `confirmed_at` exists but the schema does not store a separate “reconciled by” provenance field.
  A reconciliation state is shown only when confirmation evidence is stored and cannot always be
  distinguished from another confirmation path.
- Page DOM snapshots and historical gate screenshots are not copied. They are intentionally
  excluded because authenticated browser artifacts may expose account/session information.
- A blank already-contacted field means no positive persisted detection; it does not prove the
  listing was new at every earlier point in time.

## Security exclusions

The bundle excludes `.env`, API keys, passwords, cookies, OAuth credentials/tokens, browser
profiles, Playwright storage state, Bewerbermappe contents and private local document paths, Gmail
credentials, and browser screenshots/HTML. Known secret values and secret-looking structured log
fields are redacted. The directory and ZIP use owner-only permissions, but the bundle still
contains apartment text, generated messages, and applicant profile information needed for review;
handle it as private material.
"""


def export_audit(
    *,
    root: Path = ROOT,
    database_path: Path | None = None,
    listing_db_id: int | None = None,
    output_root: Path | None = None,
    create_zip: bool = True,
    now: datetime | None = None,
) -> AuditExportResult:
    root = root.resolve()
    environment = _read_env(root)
    secrets = _secret_values(environment)
    database_path = _resolve_database(root, environment, database_path)
    runtime = _runtime_snapshot(environment)
    exported_at = now or datetime.now().astimezone()

    connection = _readonly_connection(database_path)
    try:
        required_tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = {
            "listings",
            "events",
            "contact_attempts",
            "watcher_seen_listings",
            "watcher_search_state",
        } - required_tables
        if missing:
            raise AuditExportError(
                "database is missing required tables: " + ", ".join(sorted(missing))
            )

        condition = " WHERE l.id=?" if listing_db_id is not None else ""
        parameters: tuple[Any, ...] = (listing_db_id,) if listing_db_id is not None else ()
        listings = _rows(
            connection,
            "SELECT l.*, (SELECT COUNT(*) FROM listings d WHERE d.dedup_key=l.dedup_key) "
            f"AS dedup_record_count FROM listings l{condition} ORDER BY discovered_at, id",
            parameters,
        )
        if listing_db_id is not None and not listings:
            raise AuditExportError(f"no listing with DB ID {listing_db_id}")
        selected_ids = {int(row["id"]) for row in listings}
        if selected_ids:
            placeholders = ",".join("?" for _ in selected_ids)
            id_parameters = tuple(sorted(selected_ids))
            events = _rows(
                connection,
                f"SELECT * FROM events WHERE listing_db_id IN ({placeholders}) "
                "ORDER BY created_at, id",
                id_parameters,
            )
            attempts = _rows(
                connection,
                f"SELECT * FROM contact_attempts WHERE listing_db_id IN ({placeholders}) "
                "ORDER BY created_at, id",
                id_parameters,
            )
        else:
            events = []
            attempts = []
        watcher_states = _rows(
            connection, "SELECT * FROM watcher_search_state ORDER BY search_name"
        )
        watcher_seen = _rows(
            connection, "SELECT * FROM watcher_seen_listings ORDER BY first_seen_at, id"
        )
        if listing_db_id is not None:
            wg_listing_id = str(listings[0].get("listing_id") or "")
            watcher_seen = [
                row
                for row in watcher_seen
                if str(row.get("listing_key") or "") == f"wg_gesucht:{wg_listing_id}"
            ]
        schema_rows = _rows(
            connection,
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table','index') AND sql IS NOT NULL ORDER BY type, name",
        )
        contact_fields = tuple(
            str(column["name"])
            for column in connection.execute("PRAGMA table_info(contact_attempts)").fetchall()
        )
    finally:
        connection.close()

    stamp = exported_at.strftime("%Y-%m-%d_%H%M%S")
    suffix = f"_listing_{listing_db_id}" if listing_db_id is not None else ""
    base = output_root.resolve() if output_root else root / "audit_exports"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = base / f"audit_{stamp}{suffix}"
    counter = 1
    while directory.exists() or directory.with_suffix(".zip").exists():
        directory = base / f"audit_{stamp}{suffix}_{counter}"
        counter += 1
    directory.mkdir(mode=0o700)

    attempts_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    events_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for attempt in attempts:
        attempts_by_id[int(attempt["listing_db_id"])].append(_sanitize(attempt, secrets))
    for event in events:
        events_by_id[int(event["listing_db_id"])].append(_sanitize(event, secrets))

    log_rows = _load_logs(root, selected_ids if listing_db_id is not None else None, secrets)
    logs_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for log_row in log_rows:
        raw_listing_id = log_row.get("listing_id")
        if raw_listing_id is None:
            continue
        try:
            logs_by_id[int(raw_listing_id)].append(log_row)
        except (TypeError, ValueError):
            continue

    summaries = [
        _summary_row(row, attempts_by_id[int(row["id"])], runtime, secrets) for row in listings
    ]
    for row, summary in zip(listings, summaries, strict=True):
        listing_id = int(row["id"])
        if any(event.get("event") == "already_contacted" for event in events_by_id[listing_id]):
            summary["already_contacted_check"] = "detected"
    _write_csv(directory / "listings.csv", summaries, CSV_FIELDS)
    _write_csv(
        directory / "events.csv",
        [_sanitize(row, secrets) for row in events],
        ("id", "listing_db_id", "event", "detail_json", "created_at"),
    )
    _write_text(
        directory / "events.jsonl",
        "".join(
            json.dumps(_sanitize(row, secrets), ensure_ascii=False, default=str) + "\n"
            for row in events
        ),
    )
    _write_csv(
        directory / "contact_attempts.csv",
        [_sanitize(row, secrets) for row in attempts],
        contact_fields,
    )
    _write_csv(
        directory / "watcher_search_state.csv",
        [_sanitize(row, secrets) for row in watcher_states],
        (
            "search_name",
            "baseline_done",
            "last_checked_at",
            "last_success_at",
            "last_new_listing_at",
            "next_check_at",
            "status",
            "consecutive_errors",
            "detail",
        ),
    )
    _write_csv(
        directory / "watcher_seen_listings.csv",
        [_sanitize(row, secrets) for row in watcher_seen],
        ("id", "listing_key", "search_name", "first_seen_at", "is_baseline"),
    )
    _write_text(
        directory / "database_schema.sql",
        "\n\n".join(str(row["sql"]) + ";" for row in schema_rows) + "\n",
    )
    _write_text(
        directory / "status.txt",
        _status_text(
            root,
            database_path,
            listings,
            watcher_states,
            watcher_seen,
            listing_db_id,
            exported_at,
        ),
    )

    listing_directory = directory / "listings"
    listing_directory.mkdir(mode=0o700)
    for row, summary in zip(listings, summaries, strict=True):
        listing_id = int(row["id"])
        facts = _sanitize(_json_load(row.get("facts_json"), {}), secrets)
        decision = _sanitize(_json_load(row.get("decision_json"), {}), secrets)
        validation = _sanitize(_json_load(row.get("validation_json"), {}), secrets)
        attachment = _sanitize(_json_load(row.get("attachment_json"), {}), secrets)
        trace = _sanitize(_json_load(row.get("generation_trace_json"), {}), secrets)
        _write_text(
            listing_directory / f"{listing_id}.md",
            _listing_markdown(
                summary,
                facts,
                decision,
                validation,
                attachment,
                trace,
                attempts_by_id[listing_id],
                events_by_id[listing_id],
                logs_by_id[listing_id],
            ),
        )

    _write_logs(
        root,
        directory / "logs",
        log_rows,
        secrets,
        include_console=listing_db_id is None,
    )
    _write_config_snapshot(root, directory / "config_snapshot", runtime, secrets)
    _write_text(
        directory / "README.md",
        _readme_text(database_path, listing_db_id, exported_at, len(listings)),
    )
    manifest: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            manifest[str(path.relative_to(directory))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    _write_json(directory / "manifest_sha256.json", manifest)

    zip_path: Path | None = None
    if create_zip:
        archive = shutil.make_archive(
            str(directory), "zip", root_dir=directory.parent, base_dir=directory.name
        )
        zip_path = Path(archive)
        zip_path.chmod(0o600)
    return AuditExportResult(directory=directory, zip_path=zip_path, listing_count=len(listings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a sanitized, read-only audit export")
    parser.add_argument("db_id", nargs="?", type=int, help="export only this listing DB ID")
    parser.add_argument("--no-zip", action="store_true", help="do not create a ZIP archive")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = export_audit(listing_db_id=args.db_id, create_zip=not args.no_zip)
    except AuditExportError as exc:
        print(f"Audit export failed: {exc}")
        return 2
    print(f"Exported {result.listing_count} listing(s) to: {result.directory}")
    if result.zip_path:
        print(f"ZIP: {result.zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
