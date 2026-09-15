from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .analyzer import process_listing
from .browser import fetch_full_listing, login_wg
from .config_loader import ROOT, load_all
from .contact import contact_listing
from .daemon import ApartmentDaemon
from .database import Database
from .doctor import doctor
from .gmail_client import gmail_service
from .logging_setup import configure_logging
from .schemas import AnalysisOutcome, SourceListing

console = Console()


def _read_listing(args: argparse.Namespace, settings: Any) -> SourceListing:
    if args.file:
        text = Path(args.file).expanduser().read_text(encoding="utf-8")
    elif args.text:
        text = args.text
    elif args.url:
        if args.platform == "wg_gesucht":
            text = fetch_full_listing(args.url, settings)
        else:
            response = httpx.get(args.url, follow_redirects=True, timeout=20)
            response.raise_for_status()
            text = response.text
    else:
        console.print("[bold]Paste the full listing, then press Ctrl-D:[/bold]")
        text = sys.stdin.read()
    listing_id = args.listing_id or (Path(args.file).stem if args.file else "")
    return SourceListing(
        platform=args.platform,
        listing_id=listing_id,
        url=args.url or "",
        title=args.title or "",
        raw_text=text,
        contact_email=args.email,
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


def _status(database: Database) -> None:
    counts = database.status_counts()
    console.print(
        "[bold]Counts[/bold] " + (", ".join(f"{k}={v}" for k, v in counts.items()) or "empty")
    )
    table = Table(title="Recent listings")
    for name in ("id", "platform", "listing_id", "title", "status", "provider", "updated_at"):
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
                    "provider",
                    "updated_at",
                )
            )
        )
    console.print(table)


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

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--once", action="store_true")
    login_parser = commands.add_parser("login")
    login_parser.add_argument("target", choices=("wg", "gmail"))
    commands.add_parser("status")
    export_parser = commands.add_parser("export")
    export_parser.add_argument("--output", default="data/listings.csv")
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
        _status(database)
        return 0
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
    listing = _read_listing(args, settings)
    outcome = process_listing(
        listing,
        config=config,
        answers=answers,
        settings=settings,
        database=database,
    )
    _show_outcome(outcome)
    if args.prepare and outcome.status == "drafted":
        result = contact_listing(outcome, settings, database)
        console.print(
            Panel(
                f"Status: {result.status}\n{result.detail}\n"
                f"Screenshot: {result.screenshot_path or '-'}",
                title="Contact preparation",
            )
        )
        return 0 if result.status in {"dry_run_ready", "sent"} else 2
    return 0 if outcome.status not in {"ai_failed", "review_required"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
