"""
Recroot — Admin Module

Implements the Admin role's feature set from Section 4 of the
blueprint:
    * Manage users
    * View jobs
    * View system activity
    * View audit logs

Architecture notes (consistent with modules/auth.py):
    * `from app import get_db` is imported lazily, inside each route
      function, to avoid the circular import (app.py imports this
      module while building the app).
    * Every route is protected with `roles_required("admin")` from
      modules/auth.py — the first real usage of that decorator.
    * Mutating actions (create/delete user) are written to audit_log
      via modules.auth.log_action(), the same helper auth.py itself
      uses for login/logout — one audit-logging code path for the
      whole app.
    * No ORM — raw SQL via the DictCursor connection from
      app.get_db(), matching every other module.

Jobs page (upgraded):
    * Admin can open/close ANY job (explicit target status, no toggle) and
      create a job assigned to an existing recruiter. Validation, INSERT
      and status UPDATE come from modules/job_service.py, the same code
      the recruiter module uses.
    * There is deliberately no delete-job action: closing a job only
      changes job_postings.status, so applications, scores and resumes
      are never lost.

Step 42 (read-only pages, admin only):
    * /admin/worker    — Worker Monitor: live statistics from the real public
      API of modules/worker.py (view-model in modules/worker_monitor.py).
    * /admin/analytics — applications per job, average candidate score, most
      requested skills, skill gaps (queries in modules/admin_analytics.py).

Deadlock Monitoring & Recovery (admin only):
    * /admin/deadlock            — dashboard: REAL MySQL deadlocks (1213), lock
      wait timeouts (1205), retries, rollbacks and recoveries observed by the
      worker pool (modules/db_retry.py), worker status, and a read-only view of
      what the database persisted (modules/deadlock_monitor.py). The
      simulation sits in its own clearly separate section.
    * /admin/deadlock/simulate   — POST: circular wait between two isolated
      in-memory locks, ended by a timeout (modules/deadlock_demo.py).
    * /admin/deadlock/prevention — POST: the same two locks acquired in a
      consistent order; both threads finish.
    * /admin/deadlock/real-test  — POST: a GENUINE MySQL deadlock between two
      transactions on two separate connections, on a dedicated test table
      only (modules/real_deadlock_test.py). Shown in its own section with its
      own counters; never mixed into the worker's counters or the simulation.
      Writes one audit_log row (admin_real_deadlock_test).
    Simulation results are always labelled SIMULATED and never touch the real
    counters or the database. Apart from the real test, the only database
    access on this page is two SELECTs (processing-status counts, recent
    failure audit rows).
"""

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from modules.admin_analytics import get_analytics
from modules.auth import roles_required, log_action
from modules.deadlock_demo import run_prevention_demo, simulate_deadlock
from modules.deadlock_monitor import (
    DEFAULT_REFRESH_SECONDS as DEADLOCK_REFRESH_SECONDS,
    build_real_test_view,
    get_dashboard_view,
    get_persisted_view,
    get_real_test_view,
    remember_real_test,
)
from modules.real_deadlock_test import run_real_deadlock_test
from modules.job_service import (
    VALID_JOB_STATUSES,
    get_recruiter,
    insert_job,
    parse_and_validate_job_form,
    set_job_status,
)
from modules.scheduler_view import build_scheduler_view
from modules.worker_monitor import (
    DEFAULT_REFRESH_SECONDS,
    get_monitor_view,
    get_processing_counts,
)
from werkzeug.security import generate_password_hash


admin_bp = Blueprint("admin", __name__)


# Dashboard — system activity overview

@admin_bp.route("/dashboard")
@roles_required("admin")
def dashboard():
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT role, COUNT(*) AS total FROM users GROUP BY role")
        user_counts = {row["role"]: row["total"] for row in cur.fetchall()}

        cur.execute(
            "SELECT status, COUNT(*) AS total FROM job_postings GROUP BY status"
        )
        job_counts = {row["status"]: row["total"] for row in cur.fetchall()}

        cur.execute("SELECT COUNT(*) AS total FROM applications")
        total_applications = cur.fetchone()["total"]

        cur.execute(
            "SELECT ROUND(AVG(match_score), 1) AS avg_score FROM applications"
        )
        avg_match_score = cur.fetchone()["avg_score"] or 0

        cur.execute(
            """
            SELECT audit_log.id, audit_log.action, audit_log.details,
                   audit_log.timestamp, users.name AS user_name,
                   users.role AS user_role
            FROM audit_log
            LEFT JOIN users ON users.id = audit_log.user_id
            ORDER BY audit_log.timestamp DESC
            LIMIT 10
            """
        )
        recent_activity = cur.fetchall()

    stats = {
        "total_users": sum(user_counts.values()),
        "admins": user_counts.get("admin", 0),
        "recruiters": user_counts.get("recruiter", 0),
        "candidates": user_counts.get("candidate", 0),
        "open_jobs": job_counts.get("open", 0),
        "closed_jobs": job_counts.get("closed", 0),
        "total_applications": total_applications,
        "avg_match_score": avg_match_score,
    }

    return render_template(
        "admin/dashboard.html", stats=stats, recent_activity=recent_activity
    )


# Users — manage users

@admin_bp.route("/users", methods=["GET", "POST"])
@roles_required("admin")
def users():
    from app import get_db

    db = get_db()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        role = request.form.get("role") or ""

        errors = []
        if not name:
            errors.append("Name is required.")
        if not email:
            errors.append("Email is required.")
        if not password or len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if role not in ("admin", "recruiter", "candidate"):
            errors.append("A valid role is required.")

        if not errors:
            with db.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE email = %s", (email,))
                if cur.fetchone():
                    errors.append(f"A user with email {email} already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (name, email, password, role)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                    name,
                    email,
                    generate_password_hash(
                    password,
                    method=current_app.config["PASSWORD_HASH_METHOD"]
                ),
                role,
            ),
                )
                new_user_id = cur.lastrowid
            db.commit()

            from flask import session

            log_action(
                session["user_id"],
                "admin_create_user",
                f"Created {role} user '{name}' ({email})",
            )
            flash(f"User '{name}' created.", "success")

        return redirect(url_for("admin.users"))

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, role, created_at FROM users ORDER BY created_at DESC"
        )
        all_users = cur.fetchall()

    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_user(user_id):
    from flask import session

    if user_id == session["user_id"]:
        flash("You can't delete your own account while logged in as it.", "error")
        return redirect(url_for("admin.users"))

    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT name, email, role FROM users WHERE id = %s", (user_id,))
        target = cur.fetchone()

        if target is None:
            flash("User not found.", "error")
            return redirect(url_for("admin.users"))

        # Step 36: never leave job postings without an owner. job_postings.
        # created_by is ON DELETE SET NULL, so deleting a user who owns jobs
        # would silently orphan them (and break the recruiter pages that
        # filter on created_by). Block it instead; reassigning jobs is not
        # part of the current admin features.
        cur.execute(
            "SELECT COUNT(*) AS total FROM job_postings WHERE created_by = %s",
            (user_id,),
        )
        owned_jobs = cur.fetchone()["total"]
        if owned_jobs:
            flash(
                f"Can't delete {target['role']} '{target['name']}': they own "
                f"{owned_jobs} job posting{'s' if owned_jobs != 1 else ''}. "
                "Jobs must always have an owner, so this account has to stay "
                "while they exist.",
                "error",
            )
            return redirect(url_for("admin.users"))

        # The NOT EXISTS makes the check atomic with the delete: if a job was
        # created for this user after the count above, nothing is deleted.
        deleted = cur.execute(
            "DELETE FROM users WHERE id = %s "
            "AND NOT EXISTS (SELECT 1 FROM job_postings WHERE created_by = %s)",
            (user_id, user_id),
        )
    db.commit()

    if not deleted:
        flash(
            f"'{target['name']}' was not deleted because they now own a job posting.",
            "error",
        )
        return redirect(url_for("admin.users"))

    log_action(
        session["user_id"],
        "admin_delete_user",
        f"Deleted {target['role']} user '{target['name']}' ({target['email']})",
    )
    flash(f"User '{target['name']}' deleted.", "success")
    return redirect(url_for("admin.users"))


# Jobs — view jobs (admin visibility, not job creation —
# job creation belongs to the recruiter role per Section 4)

@admin_bp.route("/jobs")
@roles_required("admin")
def jobs():
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT job_postings.id, job_postings.title, job_postings.status,
                   job_postings.deadline, job_postings.created_at,
                   users.name AS created_by_name,
                   COUNT(applications.id) AS application_count
            FROM job_postings
            LEFT JOIN users ON users.id = job_postings.created_by
            LEFT JOIN applications ON applications.job_id = job_postings.id
            GROUP BY job_postings.id, job_postings.title, job_postings.status,
                     job_postings.deadline, job_postings.created_at, users.name
            ORDER BY job_postings.created_at DESC
            """
        )
        all_jobs = cur.fetchall()

        # Recruiters available for the "Assign to Recruiter" dropdown
        cur.execute(
            "SELECT id, name, email FROM users WHERE role = 'recruiter' ORDER BY name"
        )
        recruiters = cur.fetchall()

    return render_template(
        "admin/jobs.html",
        jobs=all_jobs,
        recruiters=recruiters,
        # Re-open the collapsed create form after a validation error
        open_form=bool(request.args.get("create_error")),
    )


@admin_bp.route("/jobs/create", methods=["POST"])
@roles_required("admin")
def create_job():
    """
    Admin creates a job on behalf of a recruiter. The job is stored with
    created_by = the chosen recruiter, so it shows up in that recruiter's
    existing Jobs / Candidates pages and follows the normal ownership rules.
    """
    from app import get_db

    db = get_db()

    data, errors = parse_and_validate_job_form(request.form)

    # Server-side validation of the selected recruiter: must be a real
    # user whose role is 'recruiter' (never trust the dropdown value).
    recruiter = None
    raw_id = (request.form.get("assigned_recruiter_id") or "").strip()
    try:
        recruiter_id = int(raw_id)
    except ValueError:
        recruiter_id = None
    if recruiter_id is None or recruiter_id < 1:
        errors.append("Select a recruiter to assign this job to.")
    else:
        recruiter = get_recruiter(db, recruiter_id)
        if recruiter is None:
            errors.append("The selected user is not a valid recruiter.")

    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("admin.jobs", create_error=1))

    job_id = insert_job(db, data, recruiter["id"])
    db.commit()

    log_action(
        session["user_id"],
        "admin_create_job",
        f"Created job '{data['title']}' (ID {job_id}) "
        f"assigned to recruiter '{recruiter['name']}' (ID {recruiter['id']})",
    )
    flash(
        f"Job '{data['title']}' created and assigned to {recruiter['name']}.",
        "success",
    )
    return redirect(url_for("admin.jobs"))


@admin_bp.route("/jobs/<int:job_id>/status", methods=["POST"])
@roles_required("admin")
def set_status(job_id):
    """
    Open or close ANY job (admin is not restricted by created_by).
    The target status is explicit ('open' / 'closed') so repeated or stale
    submissions can never flip a job the wrong way. Only job_postings.status
    changes; applications, scores and resumes are untouched.
    """
    from app import get_db

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in VALID_JOB_STATUSES:
        flash("Invalid job status.", "error")
        return redirect(url_for("admin.jobs"))

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT job_postings.id, job_postings.title, job_postings.status,
                   users.name AS owner_name
            FROM job_postings
            LEFT JOIN users ON users.id = job_postings.created_by
            WHERE job_postings.id = %s
            """,
            (job_id,),
        )
        job = cur.fetchone()

    if job is None:
        abort(404)

    if job["status"] == new_status:
        flash(f"Job '{job['title']}' is already {new_status}.", "info")
        return redirect(url_for("admin.jobs"))

    set_job_status(db, job_id, new_status)
    db.commit()

    log_action(
        session["user_id"],
        "admin_update_job_status",
        f"Set job '{job['title']}' (ID {job_id}, owner "
        f"{job['owner_name'] or 'none'}) to {new_status}",
    )
    flash(f"Job '{job['title']}' is now {new_status}.", "success")
    return redirect(url_for("admin.jobs"))


# Audit log — paginated

@admin_bp.route("/audit")
@roles_required("admin")
def audit():
    from app import get_db

    per_page = current_app.config.get("AUDIT_LOG_PER_PAGE", 50)
    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    offset = (page - 1) * per_page

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM audit_log")
        total_entries = cur.fetchone()["total"]

        cur.execute(
            """
            SELECT audit_log.id, audit_log.action, audit_log.details,
                   audit_log.timestamp, users.name AS user_name,
                   users.role AS user_role
            FROM audit_log
            LEFT JOIN users ON users.id = audit_log.user_id
            ORDER BY audit_log.timestamp DESC
            LIMIT %s OFFSET %s
            """,
            (per_page, offset),
        )
        entries = cur.fetchall()

    total_pages = max(1, (total_entries + per_page - 1) // per_page)

    return render_template(
        "admin/audit.html",
        entries=entries,
        page=page,
        total_pages=total_pages,
        total_entries=total_entries,
    )


# Worker monitor — live statistics of the background worker pool (Step 42)

@admin_bp.route("/worker")
@roles_required("admin")
def worker_monitor():
    """
    Read-only. In-memory counters come from the real worker API
    (modules.worker.get_worker_stats); the processing-status counts come from
    the database and survive restarts.
    """
    from app import get_db

    worker = get_monitor_view()
    processing_counts = get_processing_counts(get_db())

    return render_template(
        "admin/worker.html",
        worker=worker,
        processing_counts=processing_counts,
        refresh_ms=DEFAULT_REFRESH_SECONDS * 1000,
        refresh_seconds=DEFAULT_REFRESH_SECONDS,
    )


# Scheduling — FCFS / deadline priority for the worker queue (Step 41)

@admin_bp.route("/scheduler")
@roles_required("admin")
def scheduler():
    """
    Shows the REAL queue of the running worker pool in the order its threads
    will take it, and lets the admin switch the algorithm. The pool lives in
    this server process, so the page is empty/unavailable when the worker is
    not running here (testing, init-db, debug reloader parent).
    """
    from modules.worker import get_scheduler_snapshot

    view = build_scheduler_view(get_scheduler_snapshot())
    return render_template("admin/scheduler.html", view=view)


@admin_bp.route("/scheduler/algorithm", methods=["POST"])
@roles_required("admin")
def scheduler_set_algorithm():
    from modules.worker import get_scheduling_algorithm, set_scheduling_algorithm

    chosen = (request.form.get("algorithm") or "").strip()
    try:
        previous = set_scheduling_algorithm(chosen)
    except RuntimeError:
        flash("The background worker is not running, so there is no queue to change.", "error")
    except ValueError:
        flash("Unknown scheduling algorithm.", "error")
    else:
        if previous != chosen:
            log_action(session["user_id"], "admin_set_scheduling_algorithm",
                       f"Scheduling algorithm changed from {previous} to {chosen}")
        flash(f"Scheduling algorithm is now '{get_scheduling_algorithm()}'.", "success")
    return redirect(url_for("admin.scheduler"))


# Analytics — applications per job, scores, skills (Step 42)

@admin_bp.route("/analytics")
@roles_required("admin")
def analytics():
    from app import get_db

    data = get_analytics(get_db())
    return render_template(
        "admin/analytics.html",
        overall=data["overall"],
        jobs=data["jobs"],
        skills=data["skills"],
    )


# Deadlock monitoring & recovery (admin only); simulation is a separate section

def _open_db():
    """The request's MySQL connection (lazy import, like every admin route)."""
    from app import get_db

    return get_db()


def _render_deadlock_page(result=None, status=200, real_test=None, posted=False):
    """Real monitoring data first; any simulated result is rendered in its own
    separate section. Auto-refresh is only offered when no POST result is
    shown (reloading a POST result page would run the demo again). The last
    real-test result, if any, is always shown in its own section."""
    view = get_dashboard_view()
    return render_template(
        "admin/deadlock.html",
        result=result,
        real_test=real_test if real_test is not None else get_real_test_view(),
        posted=bool(posted or result is not None),
        view=view,
        real=view["real"],
        persisted=get_persisted_view(_open_db),
        refresh_ms=DEADLOCK_REFRESH_SECONDS * 1000,
        refresh_seconds=DEADLOCK_REFRESH_SECONDS,
    ), status


@admin_bp.route("/deadlock")
@roles_required("admin")
def deadlock_page():
    return _render_deadlock_page()


@admin_bp.route("/deadlock/simulate", methods=["POST"])
@roles_required("admin")
def deadlock_simulate():
    return _render_deadlock_page(simulate_deadlock())


@admin_bp.route("/deadlock/prevention", methods=["POST"])
@roles_required("admin")
def deadlock_prevention():
    return _render_deadlock_page(run_prevention_demo())


@admin_bp.route("/deadlock/real-test", methods=["POST"])
@roles_required("admin")
def deadlock_real_test():
    """Run the real MySQL deadlock test on its own connections and a dedicated
    table. run_real_deadlock_test() never raises and always cleans up; the
    except is a last safety net so the admin still gets a page."""
    try:
        outcome = run_real_deadlock_test(current_app.config["DB_CONFIG"])
    except Exception as exc:
        current_app.logger.exception("Real deadlock test failed unexpectedly.")
        outcome = {"kind": "real_deadlock_test", "outcome": "error", "table": None,
                   "summary": f"Unexpected failure: {type(exc).__name__}",
                   "error": type(exc).__name__}

    if outcome.get("outcome") != "busy":
        remember_real_test(outcome)
        try:
            log_action(
                session.get("user_id"),
                "admin_real_deadlock_test",
                f"Real deadlock test on dedicated table {outcome.get('table')}: "
                f"{outcome.get('outcome')} (run {outcome.get('run_id')})",
            )
        except Exception:
            current_app.logger.exception("Could not write the audit entry for the real deadlock test.")
    return _render_deadlock_page(real_test=build_real_test_view(outcome), posted=True)
