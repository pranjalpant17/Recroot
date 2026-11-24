"""
Recroot — Main Flask Application

This file wires together configuration, the MySQL connection,
blueprints, and error handling. It uses the application-factory
pattern (create_app) so config.py's DevelopmentConfig / TestingConfig
/ ProductionConfig can be swapped in cleanly (e.g. for tests).

Blueprints for auth, admin, recruiter and candidate are registered
defensively: if a module hasn't been built yet (see the roadmap in
the blueprint doc — auth.py is Step 6, admin.py Step 11, recruiter.py
Step 17, candidate.py Step 23), the app still starts, just without
those routes, so this file is runnable at every stage of the build
rather than only once everything else exists.

Run directly for local development:
    python app.py

Or with the Flask CLI:
    export FLASK_APP=app:create_app
    flask run
"""

import atexit
import os
import posixpath
import sys
import logging

from flask_wtf.csrf import CSRFProtect

from flask import Flask, abort, g, current_app, request, session, redirect, url_for
from werkzeug.routing import BuildError

from config import get_config


# Database connection (lazy, per-request, via flask.g)

def get_db():
    """
    Return the MySQL connection for the current request, opening
    one on first use and reusing it for the rest of the request.
    Other modules should call this instead of connecting directly:

        from app import get_db

        def some_route():
            db = get_db()
            with db.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
                row = cur.fetchone()
    """
    if "db" not in g:
        import pymysql
        import pymysql.cursors

        cfg = current_app.config["DB_CONFIG"]
        g.db = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=cfg.get("autocommit", False),
        )
    return g.db


def close_db(exception=None):
    """Close the request's MySQL connection, if one was opened."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Dev-time database setup (flask init-db / flask seed-db)

def _execute_sql_file(connection, path):
    """
    Run each ';'-terminated statement in a .sql file against an open
    connection. Good enough for schema.sql / seed.sql, which contain
    no semicolons inside string literals.
    """
    with open(path, "r", encoding="utf-8") as f:
        sql_text = f.read()

    statements = [s.strip() for s in sql_text.split(";")]
    with connection.cursor() as cur:
        for statement in statements:
            if statement:
                cur.execute(statement)
    connection.commit()


def register_cli_commands(app):
    @app.cli.command("init-db")
    def init_db_command():
        """Create/reset all tables from schema.sql."""
        import pymysql
        import pymysql.cursors

        cfg = app.config["DB_CONFIG"]
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            schema_path = os.path.join(app.root_path, "schema.sql")
            _execute_sql_file(conn, schema_path)
            print(f"Database '{cfg['database']}' initialized from schema.sql.")
        finally:
            conn.close()

    @app.cli.command("seed-db")
    def seed_db_command():
        """Load demo data from seed.sql (run after init-db)."""
        import pymysql
        import pymysql.cursors

        cfg = app.config["DB_CONFIG"]
        conn = pymysql.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"],
            password=cfg["password"], database=cfg["database"],
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            seed_path = os.path.join(app.root_path, "seed.sql")
            _execute_sql_file(conn, seed_path)
            print(f"Demo data loaded into '{cfg['database']}' from seed.sql.")
        finally:
            conn.close()


# App factory

def create_app(config_object=None):
    app = Flask(__name__)
    app.config.from_object(config_object or get_config())

    # CSRF protection for every POST/PUT/PATCH/DELETE request (Step 36).
    # Initialised exactly once, after the configuration is loaded, so the
    # WTF_CSRF_* settings (e.g. TestingConfig) are honoured. Every HTML
    # form that POSTs carries {{ csrf_token() }} as a hidden field.
    CSRFProtect(app)
    _configure_logging(app)
    _ensure_upload_folder(app)
    _protect_uploaded_files(app)

    app.teardown_appcontext(close_db)

    register_cli_commands(app)
    _register_blueprints(app)
    _register_error_handlers(app)
    _register_core_routes(app)
    _start_background_worker(app)

    return app


def _configure_logging(app):
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app.logger.setLevel(level)


def _ensure_upload_folder(app):
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


def _protect_uploaded_files(app):
    """
    Resumes are stored under static/uploads/resumes/ (UPLOAD_FOLDER), which
    Flask's built-in /static route would otherwise hand to anyone who knows or
    guesses the URL. Refuse every static request that resolves into
    static/uploads/; resumes are served only by the authenticated download
    routes (modules/resume_files.py). CSS and other static files are unaffected.
    """

    @app.before_request
    def _block_public_uploads():
        if request.endpoint != "static":
            return None

        filename = (request.view_args or {}).get("filename", "")
        # Name check: normalise ".." segments and (for Windows' case-insensitive
        # file system) case and backslashes.
        normalised = posixpath.normpath(filename.replace("\\", "/")).lstrip("/").lower()
        if normalised == "uploads" or normalised.startswith("uploads/"):
            abort(404)

        # Location check: whatever the name looks like, never serve a file that
        # resolves into the uploads folder (covers short names / symlinks).
        static_root = app.static_folder or ""
        target = os.path.realpath(os.path.join(static_root, filename))
        uploads_root = os.path.realpath(os.path.join(static_root, "uploads"))
        try:
            inside = os.path.commonpath(
                [os.path.normcase(uploads_root), os.path.normcase(target)]
            ) == os.path.normcase(uploads_root)
        except ValueError:
            inside = False
        if inside:
            abort(404)
        return None


# Background resume-processing worker (Step 33)

_worker_atexit_registered = False


def _worker_autostart_enabled(app):
    """
    WORKER_AUTOSTART (environment variable, else app.config, default on).
    Set WORKER_AUTOSTART=0 to run the web app without the worker pool,
    e.g. when importing app.py from a script or a test.
    """
    value = os.environ.get("WORKER_AUTOSTART")
    if value is None:
        value = app.config.get("WORKER_AUTOSTART", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _is_non_server_cli_command():
    """
    True for `flask init-db`, `flask seed-db`, etc. Those only need the
    database, so they should not spin up worker threads or run the
    recovery sweep against a schema that may not exist yet.
    `flask run`, `python app.py` and WSGI servers are not affected.
    """
    program = os.path.basename(sys.argv[0]).lower()
    invoked_via_flask_cli = program in ("flask", "flask.exe") or (
        program == "__main__.py" and "flask" in sys.argv[0].lower()
    )
    return invoked_via_flask_cli and "run" not in sys.argv[1:]


def _start_background_worker(app):
    """
    Start the process-wide worker pool from modules/worker.py once per
    server process:

      * never while TESTING, or when WORKER_AUTOSTART is off, or for
        non-server CLI commands (init-db / seed-db);
      * in debug mode Werkzeug's reloader runs this module twice (a
        watcher parent and the real child, WERKZEUG_RUN_MAIN=true), so
        only the child starts the pool;
      * init_worker() is itself idempotent, so a repeated create_app()
        in the same process reuses the same pool.
    """
    global _worker_atexit_registered

    if app.config.get("TESTING") or not _worker_autostart_enabled(app):
        app.logger.info("Background worker not started (testing or disabled).")
        return
    if _is_non_server_cli_command():
        return
    if app.config.get("DEBUG") and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        app.logger.info("Background worker not started in the reloader parent process.")
        return

    try:
        from modules.worker import init_worker, shutdown_worker
    except ImportError:
        app.logger.warning("modules/worker.py not found yet — background processing disabled.")
        return

    init_worker(app)

    if not _worker_atexit_registered:
        atexit.register(shutdown_worker, drain=False, timeout=5.0)
        _worker_atexit_registered = True


def _register_blueprints(app):
    # modules/auth.py — Step 6: login, logout, sessions, RBAC
    try:
        from modules.auth import auth_bp
        app.register_blueprint(auth_bp)
    except ImportError:
        app.logger.warning("modules/auth.py not found yet — auth routes disabled.")

    # modules/admin.py — Step 11
    try:
        from modules.admin import admin_bp
        app.register_blueprint(admin_bp, url_prefix="/admin")
    except ImportError:
        app.logger.info("modules/admin.py not found yet — admin routes disabled.")

    # modules/recruiter.py — Step 17
    try:
        from modules.recruiter import recruiter_bp
        app.register_blueprint(recruiter_bp, url_prefix="/recruiter")
    except ImportError:
        app.logger.info("modules/recruiter.py not found yet — recruiter routes disabled.")

    # modules/candidate.py — Step 23
    try:
        from modules.candidate import candidate_bp
        app.register_blueprint(candidate_bp, url_prefix="/candidate")
    except ImportError:
        app.logger.info("modules/candidate.py not found yet — candidate routes disabled.")


def _register_error_handlers(app):
    @app.errorhandler(404)
    def not_found(err):
        return {"error": "Not found"}, 404

    @app.errorhandler(403)
    def forbidden(err):
        return {"error": "Forbidden"}, 403

    @app.errorhandler(500)
    def server_error(err):
        app.logger.exception("Internal server error")
        return {"error": "Internal server error"}, 500


def _register_core_routes(app):
    @app.route("/")
    def index():
        """
        Send logged-in users to their role's dashboard, everyone
        else to the login page. Falls back to a plain scaffold
        message if those blueprints haven't been built yet.
        """
        if "user_id" in session:
            target = {
                "admin": "admin.dashboard",
                "recruiter": "recruiter.dashboard",
                "candidate": "candidate.dashboard",
            }.get(session.get("role"))
            if target:
                try:
                    return redirect(url_for(target))
                except BuildError:
                    pass

        try:
            return redirect(url_for("auth.login"))
        except BuildError:
            return (
                "<h1>Recroot</h1>"
                "<p>Application scaffold is running. "
                "Build modules/auth.py next to enable login.</p>",
                200,
            )

    @app.route("/health")
    def health():
        return {"status": "ok"}, 200


# Local dev entrypoint

app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"),
        port=int(os.environ.get("FLASK_RUN_PORT", 5000)),
        debug=app.config.get("DEBUG", False),
    )
