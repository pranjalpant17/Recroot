"""
Recroot — Candidate Module
Step 23 of the Phase 1 build.

Implements the Candidate role's feature set from Section 4 of the
blueprint:
    * Manage profile
    * Upload resume
    * View jobs
    * Apply for jobs
    * View applications
    * Manage resume versions

Scope note on "profile": the `users` table (Table 1, schema.sql) only
has name/email/password/role — there's no separate candidate-profile
table (education, etc.) in the current schema. "Manage profile" here
is therefore account info (name, email, password) only. The Section 7
consistency-checker examples (e.g. "Profile: B.Tech CSE" vs "Resume:
B.Tech Mechanical") describe a Phase 2 feature (modules/
consistency_checker.py, Step 31) that will need its own data source;
that's out of scope for this module and left for that step.

File storage convention (this module is the only writer of these
paths, so it sets the convention other modules read):
    * Uploaded resumes are saved under Config.UPLOAD_FOLDER
      (static/uploads/resumes/, per config.py).
    * Every `resume_path` column — in both resume_versions and
      applications — stores the path RELATIVE TO THE STATIC FOLDER,
      e.g. "uploads/resumes/<filename>.pdf". Since Step 36 these files are
      NOT linked through /static (app.py blocks that); templates link to the
      permission-checked download routes instead (download_resume_version,
      download_application_resume here, and the recruiter's equivalent),
      which serve the file through modules/resume_files.py.
    * file_hash is a SHA-256 hex digest of the file's bytes. Level-1
      exact-duplicate detection (Section 6) compares this column across
      resume_versions (modules/duplicate_detector.py, Step 30). After
      each successful parse, the upload route also stores the resume's
      MinHash fingerprint (Level-2 near-duplicate detection) in
      resume_fingerprints via duplicate_detector.index_resume().

Architecture notes (consistent with auth.py / admin.py / recruiter.py):
    * `from app import get_db` imported lazily inside each route.
    * Every route uses `roles_required("candidate")`.
    * Ownership is enforced explicitly (a candidate may only see/act
      on their own resume versions and applications).
    * Mutations are written to audit_log via modules.auth.log_action().
    * No ORM — raw SQL via the DictCursor connection from
      app.get_db(), matching every other module.

Step 38 (hiring pipeline service, modules/pipeline_service.py):
    * apply_to_job() inserts the application AND its first
      application_stage_history row (NULL -> 'pending', actor 'candidate')
      in one transaction; if either insert fails both are rolled back.
    * withdraw_application() may withdraw from pending, shortlisted,
      interview or offer (withdrawing from offer = declining it). The
      status UPDATE and its history row commit together, guarded by a
      conditional UPDATE so concurrent changes are handled safely.
    * Recruiter notes live in application_stage_history.note and must never
      be selected by any candidate query; nothing in this module reads
      that table.

Step 39 (stage counts + candidate timeline):
    * The dashboard shows a count per hiring stage (pending, shortlisted,
      interview, offer, hired) from the same single GROUP BY query it already
      used. Withdrawn applications are never part of a stage count.
    * applications() now reads application_stage_history, in ONE query for
      all of this candidate's applications (joined to applications on
      candidate_id, so only their own history can come back). The query lists
      its columns explicitly and never selects `note` (the recruiter's
      private note) or `changed_by`. Applications that predate Step 38 may
      have no history rows; nothing is invented for them.
"""

import hashlib
import os
import uuid

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
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from modules.auth import hash_password, log_action, roles_required
from modules.duplicate_detector import index_resume
from modules.pipeline_service import (
    ACTOR_CANDIDATE,
    InvalidTransition,
    StageConflict,
    can_candidate_withdraw,
    change_stage,
    get_application_status,
    record_initial_stage,
)
from modules.resume_files import send_resume
from modules.scoring_engine import validate_weights
from modules.worker import enqueue_application
from services.resume_parser import ResumeParseError, parse_resume


candidate_bp = Blueprint("candidate", __name__)


# ============================================================
# Resume file helpers
# ============================================================

def _allowed_resume_file(filename):
    allowed = current_app.config.get("ALLOWED_RESUME_EXTENSIONS", {"pdf"})
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in allowed


def _save_resume_file(file_storage, candidate_id, version_number):
    """
    Save the uploaded file under UPLOAD_FOLDER with a collision-proof
    name, and return (relative_path, file_hash) where relative_path
    is relative to the static folder (see module docstring).
    """
    original_name = secure_filename(file_storage.filename)
    ext = original_name.rsplit(".", 1)[-1].lower()
    #file managment 
    unique_suffix = uuid.uuid4().hex[:8]
    stored_filename = f"candidate_{candidate_id}_v{version_number}_{unique_suffix}.{ext}"

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    absolute_path = os.path.join(upload_folder, stored_filename)
# sha-256
    file_bytes = file_storage.read()
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    with open(absolute_path, "wb") as f:
        f.write(file_bytes)

    relative_path = f"uploads/resumes/{stored_filename}"
    return relative_path, file_hash


def _parse_saved_resume(relative_path):
    """
    Run the resume parser on an already-saved upload.

    Returns (parsed, status) where status is one of:
        "ok"       - parsed, normal amount of text
        "low_text" - parsed, but almost no text (likely a scanned PDF)
        "failed"   - could not be parsed at all (parsed is None)

    Never raises: the upload has already been saved and committed by
    the time this runs, so a parser problem must not turn a successful
    upload into a 500 error.
    """
    absolute_path = os.path.join(
        current_app.config["UPLOAD_FOLDER"], os.path.basename(relative_path)
    )
    try:
        parsed = parse_resume(absolute_path)
    except ResumeParseError as exc:
        current_app.logger.warning("Resume parse failed for %s: %s", relative_path, exc)
        return None, "failed"
    except Exception:
        current_app.logger.exception("Unexpected error parsing %s", relative_path)
        return None, "failed"

    if parsed["is_low_text"]:
        return parsed, "low_text"
    return parsed, "ok"


def _get_own_resume_version(db, version_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM resume_versions WHERE id = %s AND candidate_id = %s",
            (version_id, session["user_id"]),
        )
        version = cur.fetchone()
    if version is None:
        abort(404)
    return version


def _get_own_application(db, application_id):
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT applications.*, job_postings.title AS job_title,
                   job_postings.status AS job_status
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            WHERE applications.id = %s AND applications.candidate_id = %s
            """,
            (application_id, session["user_id"]),
        )
        application = cur.fetchone()
    if application is None:
        abort(404)
    return application


def _split_skills(csv_text):
    if not csv_text:
        return []
    return [s.strip() for s in csv_text.split(",") if s.strip()]


def _timeline_events(history_rows):
    """
    Turn one application's stage-history rows (oldest first) into the simple
    candidate-facing timeline. Rows carry no recruiter note by construction
    (see applications()). Wording is chosen here so the template stays plain.
    """
    events = []
    for row in history_rows:
        from_status = row["from_status"]
        to_status = row["to_status"]
        if from_status is None:
            label = "Application submitted"
        elif to_status == "withdrawn" and row["actor_role"] == "candidate":
            if from_status == "offer":
                label = "You declined the offer and withdrew your application"
            else:
                label = "You withdrew your application"
        else:
            label = (
                f"Moved from {from_status.capitalize()} "
                f"to {to_status.capitalize()}"
            )
        events.append({
            "from_status": from_status,
            "to_status": to_status,
            "label": label,
            "changed_at": row["changed_at"],
        })
    return events


# ============================================================
# Dashboard — stats overview + inline profile management
#
# "Manage profile" (Section 4) is folded into this route/template
# rather than given its own page, since the blueprint's file
# structure (Section 2) only lists dashboard/jobs/applications/
# resume_versions.html for the candidate role. The dashboard both
# GETs the overview and POSTs profile edits back to itself.
# ============================================================

@candidate_bp.route("/dashboard", methods=["GET", "POST"])
@roles_required("candidate")
def dashboard():
    from app import get_db

    db = get_db()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""

        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
            user = cur.fetchone()

        errors = []
        if not name:
            errors.append("Name is required.")
        if not email:
            errors.append("Email is required.")

        if email != user["email"]:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id FROM users WHERE email = %s AND id != %s",
                    (email, session["user_id"]),
                )
                if cur.fetchone():
                    errors.append(f"Email {email} is already in use.")

        wants_password_change = bool(new_password)
        if wants_password_change:
            if not check_password_hash(user["password"], current_password):
                errors.append("Current password is incorrect.")
            elif len(new_password) < 6:
                errors.append("New password must be at least 6 characters.")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            if wants_password_change:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s, password = %s WHERE id = %s",
                        (name, email, hash_password(new_password), session["user_id"]),
                    )
            else:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET name = %s, email = %s WHERE id = %s",
                        (name, email, session["user_id"]),
                    )
            db.commit()

            session["name"] = name
            session["email"] = email

            log_action(
                session["user_id"],
                "candidate_update_profile",
                "Updated profile" + (" and password" if wants_password_change else ""),
            )
            flash("Profile updated.", "success")

        return redirect(url_for("candidate.dashboard"))

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, role, created_at FROM users WHERE id = %s",
            (session["user_id"],),
        )
        user = cur.fetchone()

        cur.execute(
            "SELECT COUNT(*) AS total FROM resume_versions WHERE candidate_id = %s",
            (session["user_id"],),
        )
        resume_count = cur.fetchone()["total"]

        cur.execute(
            """
            SELECT status, COUNT(*) AS total FROM applications
            WHERE candidate_id = %s GROUP BY status
            """,
            (session["user_id"],),
        )
        app_counts = {row["status"]: row["total"] for row in cur.fetchall()}

        cur.execute(
            "SELECT COUNT(*) AS total FROM job_postings WHERE status = 'open'"
        )
        open_job_count = cur.fetchone()["total"]

        cur.execute(
            """
            SELECT applications.id, applications.match_score, applications.status,
                applications.processing_status, applications.processing_note,
                applications.applied_at, job_postings.title AS job_title
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            WHERE applications.candidate_id = %s
            ORDER BY applications.applied_at DESC
            LIMIT 5
            """,
            (session["user_id"],),
        )
        recent_applications = cur.fetchall()

    stats = {
        "resume_count": resume_count,
        "open_jobs": open_job_count,
        "pending": app_counts.get("pending", 0),
        "shortlisted": app_counts.get("shortlisted", 0),
        "interview": app_counts.get("interview", 0),
        "offer": app_counts.get("offer", 0),
        "hired": app_counts.get("hired", 0),
        "rejected": app_counts.get("rejected", 0),
        "withdrawn": app_counts.get("withdrawn", 0),
        "total_applications": sum(app_counts.values()),
    }

    return render_template(
        "candidate/dashboard.html",
        user=user,
        stats=stats,
        recent_applications=recent_applications,
        has_resume=resume_count > 0,
    )


# ============================================================
# Resume versions — upload + manage
# ============================================================

@candidate_bp.route("/resumes", methods=["GET", "POST"])
@roles_required("candidate")
def resume_versions():
    from app import get_db

    db = get_db()

    if request.method == "POST":
        uploaded_file = request.files.get("resume")

        if uploaded_file is None or uploaded_file.filename == "":
            flash("Please choose a PDF file to upload.", "error")
            return redirect(url_for("candidate.resume_versions"))

        if not _allowed_resume_file(uploaded_file.filename):
            flash("Only PDF resumes are accepted.", "error")
            return redirect(url_for("candidate.resume_versions"))

        with db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS max_version "
                "FROM resume_versions WHERE candidate_id = %s",
                (session["user_id"],),
            )
            next_version = cur.fetchone()["max_version"] + 1

        relative_path, file_hash = _save_resume_file(
            uploaded_file, session["user_id"], next_version
        )

        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO resume_versions
                    (candidate_id, resume_path, file_hash, version_number)
                VALUES (%s, %s, %s, %s)
                """,
                (session["user_id"], relative_path, file_hash, next_version),
            )
        db.commit()

        log_action(
            session["user_id"],
            "candidate_upload_resume",
            f"Uploaded resume version {next_version}",
        )
        flash(f"Resume version {next_version} uploaded.", "success")

        # Parse the saved PDF. The parsed data itself is kept in memory
        # only; the duplicate-detection fingerprint derived from its text
        # is stored below. Later steps (consistency checks, scoring) will
        # consume the parsed data too.
        parsed, parse_status = _parse_saved_resume(relative_path)

        # Step 30: store this resume's duplicate-detection fingerprint.
        # Runs only if the PDF was parsed (status "ok" or "low_text";
        # a low-text resume is recorded with a "too_short" fingerprint
        # and skipped by similarity checks). The upload is already
        # saved and committed, so a failure here is logged and must
        # never turn a successful upload into an error.
        if parsed is not None:
            try:
                index_resume(
                    db,
                    session["user_id"],
                    relative_path,
                    file_hash,
                    parsed["resume_text"],
                )
            except Exception:
                current_app.logger.exception(
                    "Fingerprinting failed for %s", relative_path
                )

        if parse_status == "failed":
            log_action(
                session["user_id"],
                "candidate_resume_parse_failed",
                f"Resume version {next_version} could not be parsed",
            )
            flash(
                "The file was saved, but we couldn't read its text automatically. "
                "Please check it's a valid, text-based PDF.",
                "error",
            )
        elif parse_status == "low_text":
            log_action(
                session["user_id"],
                "candidate_resume_low_text",
                f"Resume version {next_version} has very little extractable text",
            )
            flash(
                "The file was saved, but very little text could be extracted. "
                "It may be a scanned image and could need manual review.",
                "info",
            )

        return redirect(url_for("candidate.resume_versions"))

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, resume_path, file_hash, version_number, created_at
            FROM resume_versions
            WHERE candidate_id = %s
            ORDER BY version_number DESC
            """,
            (session["user_id"],),
        )
        versions = cur.fetchall()

    return render_template("candidate/resume_versions.html", versions=versions)


@candidate_bp.route("/resumes/<int:version_id>/download")
@roles_required("candidate")
def download_resume_version(version_id):
    """Serve one of the logged-in candidate's own resume versions."""
    from app import get_db

    # 404 (not 403) when the version belongs to someone else, so ids can't be
    # probed to find out which exist.
    version = _get_own_resume_version(get_db(), version_id)
    return send_resume(version["resume_path"])


@candidate_bp.route("/resumes/<int:version_id>/delete", methods=["POST"])
@roles_required("candidate")
def delete_resume_version(version_id):
    from app import get_db

    db = get_db()
    version = _get_own_resume_version(db, version_id)

    # Don't delete a resume version that's actively referenced by an
    # application — recruiters need to keep seeing what was submitted.
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS total FROM applications "
            "WHERE candidate_id = %s AND resume_path = %s",
            (session["user_id"], version["resume_path"]),
        )
        in_use = cur.fetchone()["total"]

    if in_use:
        flash(
            "This resume version is attached to an existing application "
            "and can't be deleted.",
            "error",
        )
        return redirect(url_for("candidate.resume_versions"))

    absolute_path = os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        os.path.basename(version["resume_path"]),
    )
    if os.path.exists(absolute_path):
        os.remove(absolute_path)

    with db.cursor() as cur:
        cur.execute("DELETE FROM resume_versions WHERE id = %s", (version_id,))
    db.commit()

    log_action(
        session["user_id"],
        "candidate_delete_resume",
        f"Deleted resume version {version['version_number']}",
    )
    flash(f"Resume version {version['version_number']} deleted.", "success")
    return redirect(url_for("candidate.resume_versions"))


# ============================================================
# Jobs — browse + apply
# ============================================================

@candidate_bp.route("/jobs")
@roles_required("candidate")
def jobs():
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT job_postings.*,
                   applications.id AS application_id,
                   applications.status AS application_status
            FROM job_postings
            LEFT JOIN applications
                ON applications.job_id = job_postings.id
                AND applications.candidate_id = %s
            WHERE job_postings.status = 'open'
            ORDER BY job_postings.created_at DESC
            """,
            (session["user_id"],),
        )
        open_jobs = cur.fetchall()

        cur.execute(
            """
            SELECT id, version_number FROM resume_versions
            WHERE candidate_id = %s
            ORDER BY version_number DESC
            """,
            (session["user_id"],),
        )
        resume_choices = cur.fetchall()

    for job in open_jobs:
        job["required_skills_list"] = _split_skills(job.get("required_skills"))
        job["preferred_skills_list"] = _split_skills(job.get("preferred_skills"))

    return render_template(
        "candidate/jobs.html",
        jobs=open_jobs,
        resume_choices=resume_choices,
        has_resume=bool(resume_choices),
    )


@candidate_bp.route("/jobs/<int:job_id>/apply", methods=["POST"])
@roles_required("candidate")
def apply_to_job(job_id):
    from app import get_db

    db = get_db()

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM job_postings WHERE id = %s AND status = 'open'",
            (job_id,),
        )
        job = cur.fetchone()

    if job is None:
        flash("That job is no longer accepting applications.", "error")
        return redirect(url_for("candidate.jobs"))

    resume_version_id = request.form.get("resume_version_id", type=int)
    if not resume_version_id:
        flash("Please select a resume to apply with.", "error")
        return redirect(url_for("candidate.jobs"))

    resume_version = _get_own_resume_version(db, resume_version_id)

    with db.cursor() as cur:
        cur.execute(
            "SELECT id FROM applications WHERE candidate_id = %s AND job_id = %s",
            (session["user_id"], job_id),
        )
        if cur.fetchone():
            flash("You've already applied to this job.", "error")
            return redirect(url_for("candidate.jobs"))

    # Step 33: the application is saved immediately as 'queued'. Parsing,
    # duplicate detection, consistency analysis and scoring all happen in
    # the background worker (modules/worker.py), so this request does not
    # wait for any of it.
    import pymysql.err

    #
    # Step 38: the application row and its first application_stage_history
    # row (NULL -> 'pending', by this candidate) are written in ONE
    # transaction, so an application can never exist without its first
    # history row.
    try:
        with db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO applications
                    (candidate_id, job_id, resume_path, status, processing_status)
                VALUES (%s, %s, %s, 'pending', 'queued')
                """,
                (session["user_id"], job_id, resume_version["resume_path"]),
            )
            application_id = cur.lastrowid
            record_initial_stage(cur, application_id, session["user_id"])
        db.commit()
    except pymysql.err.IntegrityError as exc:
        db.rollback()
        if exc.args and exc.args[0] == 1062:
            # uq_candidate_job: a double-click or two tabs applying at the
            # same moment both passed the check above, and the database let
            # only one of them in.
            flash("You've already applied to this job.", "error")
        else:
            # Any other integrity failure (e.g. the history insert): both
            # inserts were rolled back, so no application was created.
            current_app.logger.exception(
                "Application insert failed for candidate %s, job %s",
                session["user_id"], job_id,
            )
            flash("We couldn't submit your application. Please try again.", "error")
        return redirect(url_for("candidate.jobs"))
    except Exception:
        db.rollback()
        current_app.logger.exception(
            "Application insert failed for candidate %s, job %s",
            session["user_id"], job_id,
        )
        flash("We couldn't submit your application. Please try again.", "error")
        return redirect(url_for("candidate.jobs"))

    # Only after the commit, so the worker is guaranteed to find the row.
    # If the queue is full this returns False and the row simply stays
    # 'queued' for the recovery sweep to pick up.
    if not enqueue_application(application_id):
        current_app.logger.warning(
            "Application %s saved but not queued for processing right now.",
            application_id,
        )

    log_action(
        session["user_id"],
        "candidate_apply",
        f"Applied to job '{job['title']}' with resume v{resume_version['version_number']}",
    )
    flash(
        f"Applied to '{job['title']}'. Your resume is being analysed.",
        "success",
    )
    return redirect(url_for("candidate.applications"))


# ============================================================
# Applications — view own
# ============================================================

@candidate_bp.route("/applications")
@roles_required("candidate")
def applications():
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT applications.*, job_postings.title AS job_title,
                   job_postings.status AS job_status
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            WHERE applications.candidate_id = %s
            ORDER BY applications.applied_at DESC
            """,
            (session["user_id"],),
        )
        own_applications = cur.fetchall()

        # Step 34: map resume_path -> version number so each application can
        # show which version it was submitted with. A version attached to an
        # application can't be deleted, so the lookup normally always hits.
        cur.execute(
            "SELECT resume_path, version_number FROM resume_versions "
            "WHERE candidate_id = %s",
            (session["user_id"],),
        )
        version_by_path = {
            row["resume_path"]: row["version_number"] for row in cur.fetchall()
        }

        # Step 39: stage history for ALL of this candidate's applications in
        # one query. The JOIN to applications.candidate_id is the ownership
        # check. `note` and `changed_by` are deliberately not selected: the
        # note is the recruiter's private field.
        cur.execute(
            """
            SELECT history.application_id, history.from_status,
                   history.to_status, history.actor_role, history.changed_at
            FROM application_stage_history AS history
            JOIN applications ON applications.id = history.application_id
            WHERE applications.candidate_id = %s
            ORDER BY history.application_id ASC,
                     history.changed_at ASC,
                     history.id ASC
            """,
            (session["user_id"],),
        )
        history_by_application = {}
        for history_row in cur.fetchall():
            history_by_application.setdefault(
                history_row["application_id"], []
            ).append(history_row)

    for app_row in own_applications:
        app_row["matched_skills_list"] = _split_skills(app_row.get("matched_skills"))
        app_row["missing_skills_list"] = _split_skills(app_row.get("missing_skills"))
        app_row["resume_version_number"] = version_by_path.get(app_row["resume_path"])

        # Step 39: timeline. If the first row is not the initial
        # NULL -> pending submission (older application, or no history at
        # all), the template says earlier history is not available; it never
        # shows a made-up "submitted" event.
        timeline = _timeline_events(history_by_application.get(app_row["id"], []))
        app_row["timeline"] = timeline
        app_row["history_incomplete"] = (
            not timeline or timeline[0]["from_status"] is not None
        )

    # Maximum points per criterion: the same validated weights the worker
    # scores with (scoring_engine falls back to Config.DEFAULT_SCORE_WEIGHTS).
    return render_template(
        "candidate/applications.html",
        applications=own_applications,
        weights=validate_weights(),
    )


@candidate_bp.route("/applications/<int:application_id>/resume")
@roles_required("candidate")
def download_application_resume(application_id):
    """Serve the resume the logged-in candidate submitted with an application."""
    from app import get_db

    application = _get_own_application(get_db(), application_id)
    return send_resume(application["resume_path"])


@candidate_bp.route("/applications/<int:application_id>/withdraw", methods=["POST"])
@roles_required("candidate")
def withdraw_application(application_id):
    """
    Step 38: a candidate may withdraw an application that is pending,
    shortlisted, in interview, or at the offer stage (withdrawing from
    'offer' means declining the offer). hired / rejected / withdrawn
    applications can't be withdrawn.

    The status UPDATE (conditional on the status that was read) and the
    application_stage_history row are committed in one transaction by
    modules/pipeline_service.py. No note is recorded: notes are the
    recruiter's private field.
    """
    from app import get_db

    db = get_db()
    application = _get_own_application(db, application_id)
    applications_url = url_for("candidate.applications")
    previous_status = application["status"]

    if not can_candidate_withdraw(previous_status):
        if previous_status == "withdrawn":
            flash("This application has already been withdrawn.", "error")
        else:
            flash(
                f"An application that is {previous_status} can't be withdrawn.",
                "error",
            )
        return redirect(applications_url)

    try:
        change_stage(
            db,
            application_id,
            from_status=previous_status,
            to_status="withdrawn",
            changed_by=session["user_id"],
            actor_role=ACTOR_CANDIDATE,
            candidate_id=session["user_id"],
        )
    except InvalidTransition:
        # Defensive: can_candidate_withdraw() above already allows only the
        # statuses the service accepts.
        flash("This application can't be withdrawn.", "error")
        return redirect(applications_url)
    except StageConflict:
        # Someone (usually the recruiter) changed the application between
        # our read and our update, or a second tab already withdrew it.
        current = get_application_status(db, application_id)
        if current == "withdrawn":
            flash("This application has already been withdrawn.", "error")
        elif current is not None and not can_candidate_withdraw(current):
            flash(
                f"This application is now {current} and can no longer "
                "be withdrawn.",
                "error",
            )
        else:
            flash(
                "This application was just updated, so it was not withdrawn. "
                "Please refresh the page and try again.",
                "error",
            )
        return redirect(applications_url)

    declined_offer = previous_status == "offer"
    log_action(
        session["user_id"],
        "candidate_withdraw_application",
        f"Declined offer and withdrew application for job '{application['job_title']}'"
        if declined_offer
        else f"Withdrew application for job '{application['job_title']}'",
    )

    if declined_offer:
        flash(
            f"You declined the offer for '{application['job_title']}'; "
            "your application has been withdrawn.",
            "success",
        )
    else:
        flash(
            f"Application for '{application['job_title']}' withdrawn.",
            "success",
        )

    return redirect(applications_url)