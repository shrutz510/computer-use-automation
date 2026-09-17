"""LegacyCore routes.

The markup is hostile on purpose, to mimic legacy core-banking screens:
a frameset (nav + content frames), table layouts, no ids or test ids,
field labels in adjacent <td> cells instead of <label for>, and cryptic
input names. Headings are bold <font> text, not <h1>.

Faults can be injected at startup (--fault / FAULT env) or at runtime via
POST /admin/fault. See FAULTS for the list.
"""

import os
import random
import re
import time
from decimal import Decimal, InvalidOperation

from flask import Flask, redirect, render_template, request, session

from target_app.data import ACCOUNT_TYPES, Store

FAULTS = {
    "slow",               # content pages take 2-5s to respond
    "interstitial",       # a "System Notice" overlay blocks the page once per session
    "session_timeout",    # the next content request finds the session expired (one-shot)
    "permission_denied",  # content pages return "Insufficient privileges"
    "app_error",          # content pages return a 500 "Application Error"
}
MIN_DEPOSIT = Decimal("25.00")
MAX_DEPOSIT = Decimal("100000.00")
MEMBER_ID_RE = re.compile(r"^\d{5}$")
OPEN_PATHS = ("/login", "/logout", "/admin/", "/static/")


def create_app(fault: str | None = None) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("TARGET_APP_SECRET", "dev-only-not-a-secret")
    app.config["FAULT"] = fault or os.environ.get("FAULT") or None
    store = Store()
    user = os.environ.get("TARGET_APP_USER", "teller")
    password = os.environ.get("TARGET_APP_PASSWORD", "teller-pass")

    # ---- faults + auth gate for every content page ---------------------------

    @app.before_request
    def gate():
        if request.path.startswith(OPEN_PATHS):
            return None
        if not session.get("user"):
            if request.path == "/":
                return redirect("/login")
            return render_template("expired.html"), 401
        if request.path in ("/", "/nav"):
            return None

        fault_name = app.config["FAULT"]
        if fault_name == "slow":
            time.sleep(random.uniform(2, 5))
        elif fault_name == "session_timeout":
            app.config["FAULT"] = None  # one-shot
            session.clear()
            return render_template("expired.html"), 401
        elif fault_name == "permission_denied":
            return render_template("message.html", title="Access Denied",
                                   message="Insufficient privileges for this function. Contact your supervisor."), 403
        elif fault_name == "app_error":
            return render_template("app_error.html"), 500
        return None

    @app.context_processor
    def inject_notice():
        show = app.config["FAULT"] == "interstitial" and not session.get("notice_shown")
        if show:
            session["notice_shown"] = True
        return {"show_notice": show}

    # ---- auth ---------------------------------------------------------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if request.form.get("u") == user and request.form.get("p") == password:
                session.clear()
                session["user"] = user
                return redirect("/")
            error = "Invalid user ID or password."
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    # ---- shell --------------------------------------------------------------

    @app.route("/")
    def shell():
        return render_template("frameset.html")

    @app.route("/nav")
    def nav():
        return render_template("nav.html", user=session["user"])

    # ---- read flow: search -> detail ---------------------------------------

    @app.route("/members/search")
    def search():
        return render_template("search.html")

    @app.route("/members/find", methods=["POST"])
    def find():
        mid = request.form.get("mid", "").strip()
        if not MEMBER_ID_RE.match(mid):
            return render_template("search.html", error="Member ID must be 5 digits.", mid=mid)
        if store.get(mid) is None:
            return render_template("search.html", error=f"No member found for ID {mid}", mid=mid)
        return redirect(f"/members/{mid}")

    @app.route("/members/<mid>")
    def detail(mid: str):
        member = store.get(mid)
        if member is None:
            return render_template("search.html", error=f"No member found for ID {mid}", mid=mid)
        return render_template("detail.html", m=member)

    # ---- write flow: new sub-account -> review -> confirm -------------------

    @app.route("/accounts/new")
    def new_account():
        member = store.get(request.args.get("mid", ""))
        if member is None:
            return render_template("search.html", error="No member found for ID " + request.args.get("mid", ""))
        if not member.eligible_for_new_account:
            return render_template("message.html", title="Open Sub-Account",
                                   message="Member not eligible for new sub-accounts. (Status: Frozen)")
        return render_template("new_account.html", m=member, types=ACCOUNT_TYPES, form={}, errors={})

    @app.route("/accounts/review", methods=["POST"])
    def review():
        member = store.get(request.form.get("mid", ""))
        if member is None or not member.eligible_for_new_account:
            return render_template("message.html", title="Open Sub-Account",
                                   message="Member not eligible for new sub-accounts.")
        form, errors, deposit = _validate(request.form)
        if errors:
            return render_template("new_account.html", m=member, types=ACCOUNT_TYPES, form=form, errors=errors)
        return render_template("review.html", m=member, form=form, deposit=deposit)

    @app.route("/accounts/confirm", methods=["POST"])
    def confirm():
        member = store.get(request.form.get("mid", ""))
        if member is None or not member.eligible_for_new_account:
            return render_template("message.html", title="Open Sub-Account",
                                   message="Member not eligible for new sub-accounts.")
        form, errors, deposit = _validate(request.form)
        if errors:
            return render_template("new_account.html", m=member, types=ACCOUNT_TYPES, form=form, errors=errors)
        account = store.open_sub_account(member, form["f1"], form["f2"], deposit)
        return render_template("confirmed.html", m=member, a=account)

    # ---- test hooks (not for the agent; excluded by the policy allowlist) ---

    @app.route("/admin/fault", methods=["GET", "POST"])
    def admin_fault():
        if request.method == "POST":
            name = request.form.get("name") or None
            if name is not None and name not in FAULTS:
                return {"error": f"unknown fault {name!r}", "known": sorted(FAULTS)}, 400
            app.config["FAULT"] = name
        return {"fault": app.config["FAULT"]}

    @app.route("/admin/reset", methods=["POST"])
    def admin_reset():
        nonlocal store
        store = Store()
        app.config["FAULT"] = None
        return {"ok": True}

    return app


def _validate(data) -> tuple[dict, dict, Decimal | None]:
    """Validate the sub-account form. Field names are cryptic on purpose (f1/f2/f3)."""
    form = {k: data.get(k, "").strip() for k in ("f1", "f2", "f3")}
    errors: dict[str, str] = {}
    if form["f1"] not in ACCOUNT_TYPES:
        errors["f1"] = "Select an account type."
    if not form["f2"] or len(form["f2"]) > 20:
        errors["f2"] = "Nickname is required (max 20 characters)."
    deposit = None
    try:
        deposit = Decimal(form["f3"].replace(",", "").lstrip("$"))
        if not MIN_DEPOSIT <= deposit <= MAX_DEPOSIT:
            errors["f3"] = f"Initial deposit must be between ${MIN_DEPOSIT} and ${MAX_DEPOSIT:,}."
    except InvalidOperation:
        errors["f3"] = "Initial deposit must be a dollar amount."
    return form, errors, deposit
