"""
Recroot — Authentication Module

Provides:
    * auth_bp           — Blueprint with /login and /logout routes
    * login_required     — decorator: any authenticated user
    * roles_required(*)  — decorator: authenticated user with one of the given roles
    * current_user()     — helper to fetch the logged-in user's row (or None)
    * log_action()        — helper to write a row to audit_log

Architecture notes (kept consistent with the rest of the project):
    * No ORM. Raw SQL via the DictCursor connection from app.get_db(),
      exactly like every other module will do (see app.py's get_db()
      docstring).
    * `from app import get_db` is done INSIDE the route functions, not
      at module import time. app.py imports modules.auth while building
      the app (_register_blueprints), so importing app at module load
      time here would create a circular import. Doing it lazily, inside
      each function, is safe because by the time a request comes in,
      app.py has finished executing and `app` is fully defined.
    * Passwords are hashed with werkzeug.security using the method
      configured in config.py (Config.PASSWORD_HASH_METHOD =
      "pbkdf2:sha256"). seed.sql must store hashes generated the same
      way for the demo credentials (Section 19 of the blueprint) to work.
    * Session keys set on login: user_id, role, name, email — mirroring
      the columns on the `users` table (Section 3, Table 1) so other
      modules can read session["role"] / session["user_id"] directly,
      the same way app.py's index() route already does.
    * Every login attempt (success or failure) and every logout is
      written to audit_log (Table 6), which is what Section 4 lets the
      Admin role view ("View system activity", "View audit logs").
"""

from functools import wraps

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


auth_bp = Blueprint("auth", __name__)


# Audit logging helper

def log_action(user_id, action, details=None):
    """
    Insert a row into audit_log. user_id may be None (e.g. a failed
    login attempt for an email that doesn't exist) since the column
    allows NULL and has ON DELETE SET NULL.
    """
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_log (user_id, action, details)
            VALUES (%s, %s, %s)
            """,
            (user_id, action, details),
        )
    db.commit()


# RBAC decorators

def login_required(view_func):
    """Require any authenticated session (admin, recruiter, or candidate)."""

    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "error")
            return redirect(url_for("auth.login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapped


def roles_required(*roles):
    """
    Require an authenticated session AND session["role"] in `roles`.

    Usage in other modules, e.g. modules/admin.py:

        from modules.auth import roles_required

        @admin_bp.route("/dashboard")
        @roles_required("admin")
        def dashboard():
            ...

        @recruiter_bp.route("/jobs")
        @roles_required("recruiter", "admin")
        def jobs():
            ...
    """

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(*args, **kwargs):
            if "user_id" not in session:
                flash("Please log in to continue.", "error")
                return redirect(url_for("auth.login", next=request.path))
            if session.get("role") not in roles:
                flash("You do not have permission to view that page.", "error")
                return redirect(url_for("auth.login"))
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


# Current-user helper

def current_user():
    """
    Return the logged-in user's row (as a dict, via DictCursor) or
    None if nobody is logged in. Cached on flask.g for the duration
    of the request so repeated calls don't hit the DB more than once.
    """
    if "user_id" not in session:
        return None

    if "current_user" not in g:
        from app import get_db

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, role, created_at FROM users WHERE id = %s",
                (session["user_id"],),
            )
            g.current_user = cur.fetchone()

    return g.current_user


# Routes

_ROLE_DASHBOARD_ENDPOINT = {
    "admin": "admin.dashboard",
    "recruiter": "recruiter.dashboard",
    "candidate": "candidate.dashboard",
}


def _redirect_to_dashboard(role):
    """
    Redirect to the dashboard for `role`, falling back to the login
    page itself if that blueprint hasn't been built yet — same
    defensive pattern app.py's index() route uses with BuildError.
    """
    from werkzeug.routing import BuildError

    endpoint = _ROLE_DASHBOARD_ENDPOINT.get(role)
    if endpoint:
        try:
            return redirect(url_for(endpoint))
        except BuildError:
            pass
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # Already logged in? Send them straight to their dashboard.
    if "user_id" in session:
        return _redirect_to_dashboard(session.get("role"))

    if request.method == "GET":
        return render_template("login.html")

    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""

    if not email or not password:
        flash("Email and password are required.", "error")
        return render_template("login.html"), 400

    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, password, role FROM users WHERE email = %s",
            (email,),
        )
        user = cur.fetchone()

    if user is None or not check_password_hash(user["password"], password):
        # Don't reveal whether the email exists — same generic message
        # either way. user_id is None here since we may not have a
        # valid user row (or the row exists but the password was wrong,
        # in which case we still log against that user_id).
        log_action(
            user["id"] if user else None,
            "login_failed",
            f"Failed login attempt for {email}",
        )
        flash("Invalid email or password.", "error")
        return render_template("login.html"), 401

    # Success — establish the session.
    session.clear()
    session["user_id"] = user["id"]
    session["name"] = user["name"]
    session["email"] = user["email"]
    session["role"] = user["role"]
    session.permanent = True  # honors PERMANENT_SESSION_LIFETIME from config.py

    log_action(user["id"], "login_success", f"{user['role']} logged in")

    next_url = request.args.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return _redirect_to_dashboard(user["role"])


@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    user_id = session.get("user_id")
    role = session.get("role")

    if user_id is not None:
        log_action(user_id, "logout", f"{role} logged out")

    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# Dev/demo helper — not routed, used by seed.sql-adjacent tooling

def hash_password(plain_password):
    """
    Hash a plaintext password using the method configured in
    config.py (Config.PASSWORD_HASH_METHOD = "pbkdf2:sha256"), so
    anything generating demo/user passwords (e.g. a one-off script
    for seed.sql, or a future "create user" admin route) stays
    consistent with what login() checks against.
    """
    method = current_app.config.get("PASSWORD_HASH_METHOD", "pbkdf2:sha256")
    return generate_password_hash(plain_password, method=method)