from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from typing import Any

from flask import Flask, abort, render_template, request, session

from .config_loader import Settings, load_all
from .database import Database
from .review import (
    approve_listing,
    current_status_reason,
    latest_screenshot,
    load_listing_record,
    reconcile_listing,
    reject_listing,
    send_gates,
)

DASHBOARD_FILTERS = (
    "review_required",
    "send_state_unknown",
    "drafted",
    "sent",
    "filtered_skip",
    "premium_boost_failed",
    "bewerbermappe_attachment_failed",
    "ai_failed",
    "all",
)


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)


def _require_csrf() -> None:
    supplied = request.form.get("csrf_token", "")
    if not supplied or not secrets.compare_digest(supplied, str(session.get("csrf_token", ""))):
        abort(400, "invalid CSRF token")


def _generation_label(row: dict[str, Any]) -> str:
    source = str(row.get("message_source") or "none")
    if source == "cloud_ai":
        return "AI"
    if source.startswith("universal_"):
        return "fallback"
    return source


def _row_view(database: Database, row: dict[str, Any]) -> dict[str, Any]:
    record = load_listing_record(row)
    attempt = database.get_actual_contact_attempt(record.id) or {}
    rent = record.facts.warm_rent_eur or record.facts.cold_rent_eur
    return {
        "row": row,
        "record": record,
        "rent": rent,
        "reason": current_status_reason(record, attempt),
        "reason_debug": record.review_reason_debug,
        "generation": _generation_label(row),
        "has_hidden_questions": bool(record.facts.hidden_questions or record.facts.hidden_commands),
        "send_state": attempt.get("status") or row.get("status") or "not attempted",
    }


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    contact_fn: Callable[..., Any] | None = None,
    reconcile_fn: Callable[..., Any] | None = None,
) -> Flask:
    if settings is None:
        _config, _answers, settings = load_all()
    database = database or Database(settings.database_path)
    app = Flask(__name__)
    app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
    app.config.update(
        DASHBOARD_SETTINGS=settings,
        DASHBOARD_DATABASE=database,
        DASHBOARD_CONTACT_FN=contact_fn,
        DASHBOARD_RECONCILE_FN=reconcile_fn,
    )

    @app.context_processor
    def template_context() -> dict[str, Any]:
        return {
            "csrf_token": _csrf_token,
            "settings": settings,
        }

    @app.get("/")
    def index() -> str:
        selected = request.args.get("status", "review_required")
        if selected not in DASHBOARD_FILTERS:
            abort(400, "unknown status filter")
        rows = [_row_view(database, row) for row in database.listings_for_dashboard(selected)]
        return render_template(
            "dashboard/index.html",
            rows=rows,
            filters=DASHBOARD_FILTERS,
            selected=selected,
        )

    def detail_context(listing_db_id: int) -> dict[str, Any]:
        row = database.get_listing(listing_db_id)
        if row is None:
            abort(404)
        record = load_listing_record(row)
        attempt = database.get_actual_contact_attempt(listing_db_id) or {}
        return {
            "row": row,
            "record": record,
            "attempt": attempt,
            "current_reason": current_status_reason(record, attempt),
            "history": database.listing_history(listing_db_id),
            "gates": send_gates(record, settings, attempt),
            "screenshot": latest_screenshot(str(row.get("listing_id") or "")),
        }

    @app.get("/listing/<int:listing_db_id>")
    def detail(listing_db_id: int) -> str:
        return render_template("dashboard/detail.html", **detail_context(listing_db_id))

    @app.route("/listing/<int:listing_db_id>/approve", methods=["GET", "POST"])
    def approve(listing_db_id: int) -> str:
        context = detail_context(listing_db_id)
        if request.method == "GET":
            return render_template("dashboard/approve.html", **context)
        _require_csrf()
        confirmed = request.form.get("confirm") == "yes"
        action = approve_listing(
            database,
            settings,
            listing_db_id,
            confirmed=confirmed,
            trigger="dashboard",
            contact_fn=contact_fn,
        )
        return render_template("dashboard/action_result.html", action=action, **context)

    @app.route("/listing/<int:listing_db_id>/reject", methods=["GET", "POST"])
    def reject(listing_db_id: int) -> str:
        context = detail_context(listing_db_id)
        if request.method == "GET":
            return render_template("dashboard/reject.html", **context)
        _require_csrf()
        action = reject_listing(
            database,
            listing_db_id,
            confirmed=request.form.get("confirm") == "yes",
        )
        return render_template("dashboard/action_result.html", action=action, **context)

    @app.post("/listing/<int:listing_db_id>/reconcile")
    def reconcile(listing_db_id: int) -> str:
        _require_csrf()
        action = reconcile_listing(
            database,
            settings,
            listing_db_id,
            reconcile_fn=reconcile_fn,
        )
        return render_template(
            "dashboard/action_result.html",
            action=action,
            **detail_context(listing_db_id),
        )

    return app


def main() -> None:
    app = create_app()
    port = int(os.getenv("DASHBOARD_PORT", "8765"))
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
