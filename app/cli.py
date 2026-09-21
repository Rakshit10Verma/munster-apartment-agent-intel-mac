from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .analyzer import process_listing
from .browser import fetch_wg_listing_snapshot, login_wg, reconcile_wg_url
from .config_loader import ROOT, Settings, load_all
from .contact import contact_listing
from .daemon import ApartmentDaemon
from .database import Database, has_send_evidence
from .doctor import doctor
from .gmail_client import gmail_service
from .logging_setup import configure_logging
from .review import (
    REVIEW_GROUPS,
    SEND_STATE_UNKNOWN_MESSAGE,
    approval_blockers,
    approve_listing,
    current_status_reason,
    latest_screenshot,
    load_listing_record,
    reconcile_listing,
    reject_listing,
    reprocess_listing,
    rows_needing_review,
)
from .rules import zwischenmiete_duration_months
from .schemas import AnalysisOutcome, SourceListing
from .sources import WGSearchWatcherSource, configured_wg_searches

console = Console()


def _read_listing(args: argparse.Namespace, settings: Any) -> SourceListing:
    source_metadata: dict[str, str] = {}
    inspected_listing_id = ""
    if args.file:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    elif args.url:
        if args.platform == "wg_gesucht":
            text, inspection = fetch_wg_listing_snapshot(args.url, settings)
            inspected_listing_id = inspection.listing_id
            source_metadata = {
                "wg_contact_state": inspection.state,
                "wg_contact_state_detail": inspection.detail,
            }
        else:
            response = httpx.get(args.url, follow_redirects=True, timeout=20)
            response.raise_for_status()
            text = response.text
    else:
        console.print("[bold]Paste the full listing, then press Ctrl-D:[/bold]")
        text = sys.stdin.read()
    listing_id = (
        args.listing_id or inspected_listing_id or (Path(args.file).stem if args.file else "")
    )
    return SourceListing(
        platform=args.platform,
        listing_id=listing_id,
        url=args.url or "",
        title=args.title or "",
        raw_text=text,
        contact_email=args.email,
        source_metadata=source_metadata,
    )


def _show_outcome(outcome: AnalysisOutcome) -> None:
    provider = (
        f"{outcome.provider.provider}/{outcome.provider.model} ({outcome.provider.latency_ms} ms)"
        if outcome.provider
        else "not called"
    )
    console.print(
        Panel(
            f"Status: {outcome.status}\n"
            f"Decision: {outcome.rule_decision.decision}\n"
            f"AI: {provider}\n"
            f"Message source: {outcome.message_source}\n"
            f"Hard skips: {outcome.rule_decision.hard_skip_reasons or 'none'}\n"
            f"Warnings: {outcome.rule_decision.warnings or 'none'}\n"
            f"Deterministic send-safe: {outcome.validation.auto_send_allowed}",
            title="Result",
        )
    )
    if outcome.facts.hidden_questions:
        console.print("[bold]Hidden questions[/bold]")
        for question in outcome.facts.hidden_questions:
            console.print(f"- {question.id}: {question.question}")
    if outcome.facts.hidden_commands:
        console.print("[bold]Hidden commands[/bold]")
        for command in outcome.facts.hidden_commands:
            console.print(f"- {command.id}: {command.instruction}")
    if outcome.message:
        console.print(
            Panel(f"{outcome.message.subject}\n\n{outcome.message.body}", title="Application")
        )
    if outcome.validation.errors:
        console.print("[bold red]Validation blocks[/bold red]")
        for error in outcome.validation.errors:
            console.print(f"- {error}")
    if outcome.validation.warnings:
        console.print("[bold yellow]Validation warnings[/bold yellow]")
        for warning in outcome.validation.warnings:
            console.print(f"- {warning}")
    for note in outcome.router_notes:
        console.print(f"[dim]Router: {note}[/dim]")


def _contact_exit_code(status: str, *, require_sent: bool) -> int:
    if require_sent:
        return 0 if status == "sent" else 2
    return 0 if status in {"dry_run_ready", "sent"} else 2


def _daemon_pid_running() -> bool:
    pid_file = ROOT / "data" / "agent.pid"
    if not pid_file.is_file():
        return False
    digits = "".join(ch for ch in pid_file.read_text(encoding="utf-8") if ch.isdigit())
    if not digits:
        return False
    try:
        os.kill(int(digits), 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _ago(iso_value: str | None) -> str:
    if not iso_value:
        return "never"
    try:
        then = datetime.fromisoformat(iso_value)
    except ValueError:
        return "unknown"
    seconds = max(0, round((datetime.now(UTC) - then).total_seconds()))
    return f"{seconds} sec ago"


def _eta(iso_value: str | None) -> str:
    if not iso_value:
        return "unknown"
    try:
        then = datetime.fromisoformat(iso_value)
    except ValueError:
        return "unknown"
    seconds = round((then - datetime.now(UTC)).total_seconds())
    return f"~{seconds} sec" if seconds > 0 else "due now"


def _watcher_status_lines(database: Database, config: dict[str, Any]) -> list[str]:
    searches = configured_wg_searches(config)
    if not searches:
        return ["WG watcher: not configured"]
    summary = database.watcher_status_summary()
    rows = {row["search_name"]: row for row in summary["searches"]}
    tracked = [rows[name] for name, _url in searches if name in rows]
    last_success = max(
        (r["last_success_at"] for r in tracked if r["last_success_at"]), default=None
    )
    last_checked = max(
        (r["last_checked_at"] for r in tracked if r["last_checked_at"]), default=None
    )
    last_new = max(
        (r["last_new_listing_at"] for r in tracked if r["last_new_listing_at"]), default=None
    )
    next_check = min((r["next_check_at"] for r in tracked if r["next_check_at"]), default=None)
    human_verification = any(r["status"] == "human_verification_required" for r in tracked)
    backoff = any(r["status"] == "backoff" for r in tracked)
    running = _daemon_pid_running()
    return [
        f"WG watcher: {'running' if running else 'stopped'}",
        f"Searches: {len(searches)}",
        f"Last successful check: {_ago(last_success)}"
        + ("" if last_success else f" (last attempt {_ago(last_checked)})"),
        f"Next check: {_eta(next_check)}",
        f"Last new listing: {_ago(last_new) if last_new else 'none yet'}",
        f"Listings discovered today: {summary['discovered_today']}",
        f"Queue depth: {summary['queue_depth']}",
        f"Human verification: {'YES - run ./login.sh wg' if human_verification else 'no'}",
        f"Rate-limit/backoff: {'yes' if backoff else 'no'}",
    ]


def _status(database: Database, config: dict[str, Any] | None = None) -> None:
    if config is not None:
        for line in _watcher_status_lines(database, config):
            console.print(line)
        console.print("")
    current, preserved = database.status_counts_split()
    counts = database.status_counts()
    console.print(
        "[bold]Current run[/bold] " + (", ".join(f"{k}={v}" for k, v in current.items()) or "empty")
    )
    console.print(
        "[bold]Preserved contact history[/bold] "
        + (", ".join(f"{k}={v}" for k, v in preserved.items()) or "empty")
    )
    table = Table(title="Recent listings")
    for name in (
        "id",
        "platform",
        "listing_id",
        "title",
        "status",
        "message_source",
        "provider",
        "updated_at",
    ):
        table.add_column(name)
    for row in database.recent():
        table.add_row(
            *(
                str(row.get(name) or "")[:60]
                for name in (
                    "id",
                    "platform",
                    "listing_id",
                    "title",
                    "status",
                    "message_source",
                    "provider",
                    "updated_at",
                )
            )
        )
    console.print(table)
    review_count = counts.get("review_required", 0)
    unknown_count = counts.get("send_state_unknown", 0)
    if review_count or unknown_count:
        console.print("")
        console.print("[bold yellow]Human attention:[/bold yellow]")
        if review_count:
            console.print(f"{review_count} review required")
        if unknown_count:
            console.print(f"{unknown_count} send state unknown")
        console.print("")
        console.print("Run:")
        console.print("./review.sh")


def _reconcile(url: str, settings: Settings, database: Database) -> int:
    """Read-only recovery for an ambiguous/interrupted send. NEVER clicks Send: it only
    opens the listing/conversation and compares it against the message we last
    attempted, then updates SQLite with whatever it can positively determine."""
    row = database.find_listing_by_url_or_id(url)
    if row is None:
        console.print(f"[red]No prior record for {url} was found in SQLite.[/red]")
        return 2
    attempt = database.get_actual_contact_attempt(int(row["id"]))
    expected_body = (attempt or {}).get("message_body") or row.get("message_body") or ""
    if not expected_body:
        console.print("[red]No stored message body to reconcile against for this listing.[/red]")
        return 2
    outcome = reconcile_wg_url(url, settings, expected_body, str(row.get("listing_id") or ""))
    database.reconcile_contact(int(row["id"]), outcome.result, outcome.detail)
    console.print(
        Panel(
            f"Result: {outcome.result}\n{outcome.detail}\nSignals: {outcome.signals or 'none'}",
            title="Reconciliation",
        )
    )
    return 0 if outcome.result in {"sent", "not_sent"} else 2


def _reconcile_by_id(listing_db_id: int, settings: Settings, database: Database) -> int:
    action = reconcile_listing(
        database,
        settings,
        listing_db_id,
        reconcile_fn=reconcile_wg_url,
    )
    if action.reconciliation is not None:
        outcome = action.reconciliation
        console.print(
            Panel(
                f"Result: {outcome.result}\n{outcome.detail}\nSignals: {outcome.signals or 'none'}",
                title="Reconciliation",
            )
        )
    else:
        console.print(f"[red]{action.detail}[/red]")
    return 0 if action.ok else 2


def _reconcile_list(database: Database) -> int:
    rows = database.listings_by_status("send_state_unknown")
    if not rows:
        console.print("No listings are in send_state_unknown.")
        return 0
    for row in rows:
        attempt = database.get_actual_contact_attempt(int(row["id"])) or {}
        console.print(
            Panel(
                f"WG {row.get('listing_id') or '?'}\n"
                f"{row.get('title') or '(no title)'}\n"
                f"URL: {row.get('canonical_url') or ''}\n"
                f"Last attempt detail: {attempt.get('detail') or 'none'}\n"
                f"Send clicked at: {attempt.get('send_clicked_at') or 'unknown'}\n"
                f"Updated: {row.get('updated_at')}",
                title=f"[{row['id']}] SEND STATE UNKNOWN",
            )
        )
    console.print("Run ./reconcile.sh <DB_ID> to resolve one of the above.")
    return 0


def _review(database: Database, listing_db_id: int | None) -> int:
    if listing_db_id is not None:
        return _review_detail(database, listing_db_id)
    grouped = rows_needing_review(database)
    total = sum(len(rows) for rows in grouped.values())
    if not total:
        console.print("Nothing needs human attention right now.")
        return 0
    for status, label in REVIEW_GROUPS:
        rows = grouped[status]
        if not rows:
            continue
        console.print(f"[bold]{label}[/bold] ({len(rows)})")
        for row in rows:
            record = load_listing_record(row)
            rent = record.facts.warm_rent_eur or record.facts.cold_rent_eur
            console.print(
                Panel(
                    f"WG {row.get('listing_id') or '?'}\n"
                    f"{row.get('title') or '(no title)'}\n"
                    f"Rent: {f'€{rent:g}' if rent else 'unknown'}\n"
                    f"Location: {record.facts.location or 'unknown'}\n"
                    f"Status: {row['status']}\n"
                    f"Decision: {record.decision.decision}\n"
                    f"Reason: {record.review_reason_human}\n"
                    f"Warnings: {'; '.join(record.warnings) or 'none'}\n"
                    f"Discovered: {row.get('discovered_at')}\n"
                    f"Updated: {row.get('updated_at')}\n"
                    f"URL: {row.get('canonical_url') or ''}",
                    title=f"[{row['id']}] {label}",
                )
            )
    console.print(
        "Run ./review.sh <DB_ID> for full detail, then ./approve.sh <DB_ID> or ./reject.sh <DB_ID>."
    )
    return 0


def _review_detail(database: Database, listing_db_id: int) -> int:
    row = database.get_listing(listing_db_id)
    if row is None:
        console.print(f"[red]No listing with id {listing_db_id} was found.[/red]")
        return 2
    record = load_listing_record(row)
    attempt = database.get_actual_contact_attempt(listing_db_id) or {}
    facts = record.facts
    shot = latest_screenshot(str(row.get("listing_id") or ""))
    ai_failure = record.generation_trace.ai_failure_reason or row.get("error") or "none"
    fallback_answers = [
        item.model_dump() for item in record.generation_trace.hidden_question_answers
    ] or "none"
    if facts.minimum_duration_months is not None:
        duration_display = f"{facts.minimum_duration_months} months"
    else:
        derived_duration = zwischenmiete_duration_months(facts)
        duration_display = (
            f"~{derived_duration} months (derived from dates)"
            if derived_duration is not None
            else "unknown months"
        )
    console.print(
        Panel(
            f"Title: {row.get('title') or '(no title)'}\n"
            f"URL: {row.get('canonical_url') or ''}\n"
            f"Rent: warm €{facts.warm_rent_eur or '?'} / cold €{facts.cold_rent_eur or '?'}\n"
            f"Room size: {facts.room_size_m2 or 'unknown'} m²\n"
            f"Available from: {facts.move_in or 'unknown'}\n"
            f"Duration: {duration_display} (end: {facts.end_date or 'open-ended'})\n"
            f"Later contract requirements: "
            f"{', '.join(facts.later_contract_requirements) or 'none'}\n"
            f"Advertiser/context: {facts.advertiser_type}\n"
            f"Age range: {facts.age_min or '?'}-{facts.age_max or '?'} "
            f"({facts.age_requirement_strength}); women_only={facts.women_only}",
            title="LISTING",
        )
    )
    console.print(
        Panel(
            f"Decision: {record.decision.decision}\n"
            f"Hard filters: {record.decision.hard_skip_reasons or 'none'}\n"
            f"Soft warnings: {record.decision.warnings or 'none'}\n"
            f"Scam risk: {facts.scam_risk} ({facts.scam_reasons or 'none'})\n"
            f"Critical ambiguities: {facts.critical_ambiguities or 'none'}\n"
            f"Unresolved required facts: {facts.unresolved_required_facts or 'none'}",
            title="DECISION",
        )
    )
    console.print(
        Panel(
            f"Provider: {row.get('provider') or 'none'} / {row.get('model') or ''}\n"
            f"Message source: {row.get('message_source')}\n"
            f"AI attempted: {record.generation_trace.ai_attempted}\n"
            f"AI failure: {ai_failure}\n"
            f"AI skipped reason: {record.generation_trace.ai_skip_reason or 'none'}\n"
            f"Fallback used: {record.generation_trace.fallback_used}\n"
            f"Fallback scenario: {record.generation_trace.fallback_scenario or 'none'}\n"
            f"Fallback template: {record.generation_trace.fallback_template or 'none'}\n"
            f"Optional clauses: {record.generation_trace.optional_clauses or 'none'}\n"
            f"Fallback hidden answers: {fallback_answers}",
            title="AI",
        )
    )
    if facts.hidden_questions or facts.hidden_commands:
        answered = set(record.message.answered_question_ids) if record.message else set()
        applied = set(record.message.applied_command_ids) if record.message else set()
        lines = [
            f"- Q [{q.id}]: {q.question} -> answer used: "
            f"{'resolved' if q.id in answered else 'unresolved'}"
            for q in facts.hidden_questions
        ] + [
            f"- CMD [{c.id}]: {c.instruction} -> {'applied' if c.id in applied else 'unresolved'}"
            for c in facts.hidden_commands
        ]
        console.print(Panel("\n".join(lines), title="HIDDEN QUESTIONS / COMMANDS"))
    else:
        console.print(Panel("none detected", title="HIDDEN QUESTIONS / COMMANDS"))
    if record.message:
        console.print(
            Panel(
                f"Subject: {record.message.subject or '(none)'}\n\n{record.message.body}",
                title="APPLICATION MESSAGE",
            )
        )
    else:
        console.print(Panel("no drafted message", title="APPLICATION MESSAGE"))
    if has_send_evidence(attempt):
        duplicate_state = "Send evidence exists; reconcile before retry"
    elif attempt and record.status == "bewerbermappe_attachment_failed":
        duplicate_state = "pre-Send attachment failure; retry only after all gates pass"
    elif attempt:
        duplicate_state = "an actual contact attempt is recorded"
    else:
        duplicate_state = "none recorded yet"
    console.print(
        Panel(
            f"Validation errors: {record.validation.errors or 'none'}\n"
            f"Validation warnings: {record.validation.warnings or 'none'}\n"
            f"Premium status: {attempt.get('premium_state') or 'not attempted'}\n"
            f"Bewerbermappe status: {attempt.get('attachment_state') or 'not attempted'}\n"
            f"Duplicate check: {duplicate_state}\n"
            f"Send verification: {attempt.get('status') or 'n/a'} ({attempt.get('detail') or ''})",
            title="SEND GATES",
        )
    )
    console.print(
        Panel(
            f"{current_status_reason(record, attempt)}\n\n"
            f"Historical/debug detail: {record.review_reason_debug}",
            title="STATUS",
        )
    )
    console.print(
        Panel(
            f"Latest screenshot: {shot or 'none found'}\n"
            f"Recorded attempt URL: {attempt.get('browser_url') or 'none'}",
            title="FILES",
        )
    )
    return 0


def _approve(database: Database, settings: Settings, listing_db_id: int) -> int:
    row = database.get_listing(listing_db_id)
    if row is None:
        console.print(f"[red]No listing with id {listing_db_id} was found.[/red]")
        return 2
    if str(row["status"]) == "send_state_unknown":
        console.print(f"[red]{SEND_STATE_UNKNOWN_MESSAGE}[/red]")
        return 2
    record = load_listing_record(row)
    blockers = approval_blockers(record, database.get_actual_contact_attempt(listing_db_id))
    if blockers:
        console.print(f"[red]Cannot approve listing {listing_db_id}:[/red]")
        for blocker in blockers:
            console.print(f"  - {blocker}")
        return 2
    assert record.message is not None
    mode = (
        "LIVE SEND (explicit approval) -- this WILL be sent for real"
        if settings.send_permitted("manual_cli")
        else "DRY_RUN (nothing will actually be sent)"
    )
    console.print(
        Panel(
            f"{row.get('title') or '(no title)'}\n"
            f"URL: {row.get('canonical_url') or '(no url)'}\n"
            f"Current status: {row['status']}\n"
            f"Why it stopped: {'; '.join(record.reasons)}\n"
            f"Warnings: {'; '.join(record.warnings) or 'none'}\n"
            f"Sending mode: {mode}\n\n"
            f"Subject: {record.message.subject or '(none)'}\n\n"
            f"{record.message.body}",
            title=f"Listing {listing_db_id}",
        )
    )
    answer = input("Send this application? [y/N]: ").strip().casefold()
    if answer not in {"y", "yes"}:
        console.print("Not sending. No changes made.")
        return 0
    action = approve_listing(
        database,
        settings,
        listing_db_id,
        confirmed=True,
        trigger="manual_cli",
        contact_fn=contact_listing,
    )
    result = action.contact_result
    if result is None:
        console.print(f"[red]{action.detail}[/red]")
        return 2
    console.print(
        Panel(
            f"Status: {result.status}\n{result.detail}\n"
            f"Screenshot: {result.screenshot_path or '-'}",
            title="Approval result",
        )
    )
    return 0 if result.status in {"sent", "dry_run_ready"} else 2


def _reject(database: Database, listing_db_id: int) -> int:
    row = database.get_listing(listing_db_id)
    if row is None:
        console.print(f"[red]No listing with id {listing_db_id} was found.[/red]")
        return 2
    console.print(
        Panel(
            f"{row.get('title') or '(no title)'}\n"
            f"URL: {row.get('canonical_url') or '(no url)'}\n"
            f"Current status: {row['status']}",
            title=f"Listing {listing_db_id}",
        )
    )
    answer = input("Reject this listing? [y/N]: ").strip().casefold()
    if answer not in {"y", "yes"}:
        console.print("Not rejecting. No changes made.")
        return 0
    action = reject_listing(database, listing_db_id, confirmed=True)
    if action.ok:
        console.print(f"Listing {listing_db_id} marked manually_rejected.")
    else:
        console.print(f"[red]Cannot reject listing {listing_db_id}: {action.detail}[/red]")
    return 0 if action.ok else 2


def _reprocess(
    database: Database,
    settings: Settings,
    config: dict[str, Any],
    answers: dict[str, Any],
    listing_db_id: int,
    *,
    force: bool,
    fallback_only: bool,
) -> int:
    """Re-run analysis/drafting only (never sends) so a listing affected by a since-
    fixed bug can be safely re-evaluated. Use ./approve.sh afterwards to send.
    `fallback_only` exercises the production deterministic-fallback path directly,
    without calling any cloud provider -- a diagnostic, independent of `force`."""
    action = reprocess_listing(
        database,
        settings,
        config,
        answers,
        listing_db_id,
        force=force,
        fallback_only=fallback_only,
    )
    if not action.ok:
        console.print(f"[red]Cannot reprocess listing {listing_db_id}: {action.detail}[/red]")
        return 2
    console.print(f"Listing {listing_db_id} reprocessed. New status: {action.status}")
    if fallback_only:
        console.print("Fallback-only diagnostic: no cloud provider was called.")
    console.print("Run ./review.sh <id> for detail, or ./approve.sh <id> to send.")
    return 0


def _baseline_watcher(settings: Settings, database: Database, config: dict[str, Any]) -> int:
    """Reset ONLY the search watcher's own baseline/already-seen bookkeeping (never
    touches listings/processing history) and immediately record the currently
    visible WG-Gesucht search results as the new baseline. Read-only for listings:
    never analyzes, drafts, or contacts anything -- this is the one step that opens
    the real (already logged-in) browser session, and it only ever reads the page."""
    searches = configured_wg_searches(config)
    if not searches:
        console.print(
            "[red]No WG search URLs configured (see config.yaml wg_watch.searches).[/red]"
        )
        return 2
    console.print(
        "Opening the logged-in WG-Gesucht session (read-only) to record today's baseline..."
    )
    database.reset_watcher_bookkeeping()
    return _watch_once(settings, database, config)


def _reset_run(
    database: Database,
    settings: Settings | None = None,
    config: dict[str, Any] | None = None,
    *,
    backups_dir: Path | None = None,
    baseline_current: bool = False,
) -> int:
    """Clear old processing history for a clean status/review/dashboard view, while
    never touching anything needed to prevent a duplicate application tomorrow. Never
    touches WG-Gesucht, never sends, never deletes browser/session/config/documents --
    this only ever operates on the SQLite database, UNLESS baseline_current=True, in
    which case one extra, explicit, read-only browser step runs afterward (see
    _baseline_watcher)."""
    if _daemon_pid_running():
        console.print("[red]Stop the daemon first: ./stop.sh[/red]")
        return 2
    preview = database.reset_run_preview()
    console.print(
        Panel(
            f"Listings to clear: {preview['to_clear']}\n"
            f"Sent/contacted records preserved: {preview['preserved']}\n"
            f"send_state_unknown preserved: {preview['send_state_unknown']}",
            title="Reset preview",
        )
    )
    if preview["to_clear"] == 0:
        console.print("Nothing to clear.")
        return 0
    answer = input("Reset non-contacted apartment history? [y/N]: ").strip().casefold()
    if answer not in {"y", "yes"}:
        console.print("Not resetting. No changes made.")
        return 0
    backup_path = database.backup_to(backups_dir or ROOT / "backups")
    try:
        backup_display = backup_path.relative_to(ROOT)
    except ValueError:
        backup_display = backup_path
    console.print(f"Backup created: {backup_display}")
    try:
        result = database.reset_run()
    except Exception as exc:
        console.print(f"[red]Reset failed and was rolled back: {exc}[/red]")
        return 2
    console.print(
        Panel(
            f"Cleared: {result['cleared']}\n"
            f"Preserved (contact history): {result['preserved']}\n"
            f"send_state_unknown preserved: {result['send_state_unknown']}",
            title="Reset complete",
        )
    )
    if baseline_current:
        if settings is None or config is None:
            console.print("[red]--baseline-current requires settings/config; skipped.[/red]")
            return 2
        _baseline_watcher(settings, database, config)
    else:
        console.print("Run ./status.sh to see the clean run state.")
    return 0


def _watch_once(settings: Settings, database: Database, config: dict[str, Any]) -> int:
    """Run exactly one WG-Gesucht search-watcher discovery cycle and report what it
    saw. Never analyzes, drafts, or sends anything -- it only exercises discovery, so
    it is safe to use for initial validation regardless of AUTO_SEND/DRY_RUN."""
    searches = configured_wg_searches(config)
    if not searches:
        console.print(
            "[red]No WG search URLs configured (see config.yaml wg_watch.searches).[/red]"
        )
        return 2
    watcher = WGSearchWatcherSource(settings, database, searches)
    new_listings = watcher.discover()
    console.print(f"Search pages checked: {len(searches)}")
    for entry in watcher.last_run_report:
        console.print(
            Panel(
                f"Status: {entry.get('status')}\n"
                f"Visible listing IDs: {entry.get('visible_ids', [])}\n"
                f"Baseline (never enqueued): {entry.get('baseline', [])}\n"
                f"Already seen: {entry.get('seen', [])}\n"
                f"New (would be enqueued): {entry.get('new', [])}\n"
                f"Detail: {entry.get('detail', '')}",
                title=f"Search: {entry.get('search_name')}",
            )
        )
    console.print(f"[bold]Would enqueue {len(new_listings)} listing(s) for the pipeline.[/bold]")
    for listing in new_listings:
        console.print(f"  - {listing.listing_id}: {listing.url}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Münster apartment discovery and contact agent")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("--skip-ai", action="store_true")

    analyze_parser = commands.add_parser("analyze")
    analyze_parser.add_argument("--file")
    analyze_parser.add_argument("--text")
    analyze_parser.add_argument("--url")
    analyze_parser.add_argument("--listing-id")
    analyze_parser.add_argument("--title")
    analyze_parser.add_argument("--email")
    analyze_parser.add_argument(
        "--platform",
        choices=("wg_gesucht", "asta_muenster", "na_dann", "kleinanzeigen", "mock", "unknown"),
        default="unknown",
    )
    analyze_parser.add_argument("--prepare", action="store_true")
    analyze_parser.add_argument("--require-sent", action="store_true", help=argparse.SUPPRESS)

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--once", action="store_true")
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_parser.add_argument("--url")
    reconcile_parser.add_argument("--id", type=int)
    reconcile_parser.add_argument("--list", action="store_true")
    commands.add_parser("watch-once")
    login_parser = commands.add_parser("login")
    login_parser.add_argument("target", choices=("wg", "gmail"))
    commands.add_parser("status")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--output", default="data/listings.csv")
    review_parser = commands.add_parser("review")
    review_parser.add_argument("id", nargs="?", type=int)
    approve_parser = commands.add_parser("approve")
    approve_parser.add_argument("id", type=int)
    reject_parser = commands.add_parser("reject")
    reject_parser.add_argument("id", type=int)
    reprocess_parser = commands.add_parser("reprocess")
    reprocess_parser.add_argument("id", type=int)
    reprocess_parser.add_argument("--force", action="store_true")
    reprocess_parser.add_argument("--fallback-only", action="store_true")
    reset_run_parser = commands.add_parser("reset-run")
    reset_run_parser.add_argument("--baseline-current", action="store_true")
    commands.add_parser("baseline-watcher")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config, answers, settings = load_all()
    configure_logging(ROOT, settings.log_level)
    database = Database(settings.database_path)
    if args.command == "doctor":
        return 0 if doctor(ai_smoke=not args.skip_ai, console=console) else 1
    if args.command == "login":
        if args.target == "wg":
            login_wg(settings)
        else:
            gmail_service(settings, interactive=True)
        return 0
    if args.command == "status":
        _status(database, config)
        return 0
    if args.command == "watch-once":
        return _watch_once(settings, database, config)
    if args.command == "export":
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = ROOT / output
        count = database.export_csv(output)
        console.print(f"Exported {count} listings to {output}")
        return 0
    if args.command == "run":
        ApartmentDaemon(settings, database=database).run(once=args.once)
        return 0
    if args.command == "reconcile":
        if args.id is not None:
            return _reconcile_by_id(args.id, settings, database)
        if not args.url or args.list:
            return _reconcile_list(database)
        return _reconcile(args.url, settings, database)
    if args.command == "review":
        return _review(database, args.id)
    if args.command == "approve":
        return _approve(database, settings, args.id)
    if args.command == "reject":
        return _reject(database, args.id)
    if args.command == "reprocess":
        return _reprocess(
            database,
            settings,
            config,
            answers,
            args.id,
            force=args.force,
            fallback_only=args.fallback_only,
        )
    if args.command == "reset-run":
        return _reset_run(database, settings, config, baseline_current=args.baseline_current)
    if args.command == "baseline-watcher":
        return _baseline_watcher(settings, database, config)
    listing = _read_listing(args, settings)
    if args.require_sent and not args.prepare:
        console.print("[red]--require-sent requires --prepare[/red]")
        return 2
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=database,
    )
    _show_outcome(outcome)
    if args.prepare and outcome.status == "drafted":
        result = contact_listing(outcome, settings, database, "manual_cli")
        console.print(
            Panel(
                f"Status: {result.status}\n{result.detail}\n"
                f"Screenshot: {result.screenshot_path or '-'}",
                title="Contact preparation",
            )
        )
        return _contact_exit_code(result.status, require_sent=args.require_sent)
    if args.require_sent:
        return 2
    return 0 if outcome.status not in {"ai_failed", "review_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
