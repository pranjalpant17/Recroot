"""
Recroot — Recruiter Module

Implements the Recruiter role's feature set from Section 4 of the
blueprint:
    * Create jobs
    * Define job requirements
    * View applications
    * View candidate rankings
    * Review duplicate/consistency results
    * Shortlist/reject candidates

Step 35 (recruiter dashboard update):
    * Ranking: only SCORED applications (processing finished and not
      "Not scored") receive a rank. Ties share a rank using standard
      competition ranking (1, 1, 3). Ranks are computed per job, over that
      job's active (non-withdrawn) scored applications, independently of
      any status filter.
    * Withdrawn applications (status = 'withdrawn', set only by
      candidate.withdraw_application) are excluded from rankings,
      dashboard counts and the default candidates list. They are reachable
      through the Withdrawn status filter, and recruiters cannot change
      their status.
Step 38 (hiring pipeline service):
    * Stage changes go through modules/pipeline_service.py, which owns the
      allowed-transition dictionary and performs the status UPDATE and the
      application_stage_history INSERT in one transaction. This module no
      longer writes applications.status directly.
    * The route accepts an optional private `note` form field; it is stored
      only in application_stage_history and is never shown to candidates.
    * Audit action for stage changes is `recruiter_update_application_stage`
      (replaces the pre-Step-38 `recruiter_update_application_status`).

Step 39 (Kanban board + stage counts):
    * New route /recruiter/board?job_id=<id> shows one job's active
      applications as five stage columns (pending, shortlisted, interview,
      offer, hired) plus a collapsed Rejected list. Withdrawn applications
      are never shown. Card actions are built from
      pipeline_service.allowed_next_stages(), so the board can only offer
      moves the service accepts, and every action is a plain POST to the
      existing update_application_status route (no JavaScript).
    * update_application_status accepts an optional `return_to=board` form
      field. The redirect URL is built on the server from the application's
      own job_id; no URL is ever taken from the request.
    * The dashboard stage counts (interview / offer / hired added to the
      existing pending / shortlisted) come from the same single aggregate
      query as before: active applications of this recruiter's jobs only.

Step 35 (continued):
    * Score maxima shown in breakdowns come from
      Config.DEFAULT_SCORE_WEIGHTS, the same weights the worker scores with.
    * No schema changes: everything is read from existing `applications`
      columns written by modules/worker.py and modules/scoring_engine.py.

Architecture notes (consistent with modules/auth.py and admin.py):
    * `from app import get_db` imported lazily inside each route.
    * Every route uses `roles_required("recruiter")` from
      modules/auth.py.
    * Ownership is enforced explicitly: a recruiter may only manage
      jobs they created (job_postings.created_by) and may only view/
      act on applications to their own jobs — checked with a helper
      rather than trusted from the URL.
    * Mutations (create job, close/reopen job, shortlist/reject) are
      written to audit_log via modules.auth.log_action().
    * No ORM — raw SQL via the DictCursor connection from
      app.get_db(), matching every other module.
"""

from collections import Counter

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

from config import Config
from modules.auth import log_action, roles_required
from modules.resume_files import send_resume
from modules.job_service import (
    insert_job,
    parse_and_validate_job_form,
    set_job_status,
)
from modules.pipeline_service import (
    ACTOR_RECRUITER,
    ALL_STATUSES,
    InvalidStageNote,
    InvalidTransition,
    StageConflict,
    allowed_next_stages,
    change_stage,
    get_application_status,
)


recruiter_bp = Blueprint("recruiter", __name__)


# Ownership helpers

def _get_own_job(db, job_id):
    """
    Return the job row if it exists AND was created by the logged-in
    recruiter, else abort with 404 (don't leak whether the job exists
    at all if it belongs to someone else).
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM job_postings WHERE id = %s AND created_by = %s",
            (job_id, session["user_id"]),
        )
        job = cur.fetchone()
    if job is None:
        abort(404)
    return job


def _get_own_application(db, application_id):
    """
    Return the application row (joined with candidate + job info) if
    it exists AND belongs to a job created by the logged-in recruiter,
    else abort with 404.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT applications.*,
                   users.name AS candidate_name,
                   users.email AS candidate_email,
                   job_postings.title AS job_title,
                   job_postings.id AS job_id,
                   job_postings.required_skills,
                   job_postings.preferred_skills
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            JOIN users ON users.id = applications.candidate_id
            WHERE applications.id = %s AND job_postings.created_by = %s
            """,
            (application_id, session["user_id"]),
        )
        application = cur.fetchone()
    if application is None:
        abort(404)
    return application


def _split_skills(csv_text):
    """matched_skills / missing_skills / required_skills are stored as
    comma-separated TEXT columns; split defensively for display."""
    if not csv_text:
        return []
    return [s.strip() for s in csv_text.split(",") if s.strip()]


# Step 35: ranking / scoring / withdrawn helpers

WITHDRAWN = "withdrawn"

# SQL form of "this application has a real score": processing finished AND
# the worker did not record "Not scored: ..." (unreadable resume). Contains
# %% because every query that uses it is executed with parameters.
SCORED_SQL = (
    "(applications.processing_status = 'done' AND "
    "(applications.processing_note IS NULL OR "
    "applications.processing_note NOT LIKE '%%Not scored:%%'))"
)

# (label, applications column, key in Config.DEFAULT_SCORE_WEIGHTS)
_BREAKDOWN_ROWS = (
    ("Required Skills", "skill_score", "skills"),
    ("Experience", "experience_score", "experience"),
    ("Education", "education_score", "education"),
    ("Relevant Projects", "project_score", "projects"),
    ("Certifications", "certification_score", "certifications"),
    ("Resume / Role Relevance", "relevance_score", "relevance"),
)

# Blueprint section 9 defaults; only used if config.py has no weights.
_FALLBACK_WEIGHTS = {
    "skills": 40, "experience": 20, "education": 15,
    "projects": 10, "certifications": 5, "relevance": 10,
}


def _is_scored(row):
    """Python twin of SCORED_SQL (the worker writes 'Not scored: <reason>')."""
    return (
        row.get("processing_status") == "done"
        and "Not scored:" not in (row.get("processing_note") or "")
    )


def _score_weights():
    """The weights the worker scores with (worker passes weights=None, so
    the scoring engine falls back to Config.DEFAULT_SCORE_WEIGHTS)."""
    try:
        configured = Config.DEFAULT_SCORE_WEIGHTS
        return {
            key: int(configured.get(key, 0)) for key in _FALLBACK_WEIGHTS
        }
    except Exception:
        return dict(_FALLBACK_WEIGHTS)


def _score_breakdown(row):
    """List of {label, score, max} for the six criteria plus the total max."""
    weights = _score_weights()
    breakdown = [
        {
            "label": label,
            "score": row.get(column) or 0,
            "max": weights[key],
        }
        for label, column, key in _BREAKDOWN_ROWS
    ]
    return breakdown, sum(weights.values())


def _attach_score_info(row):
    """Add breakdown / skill-count fields to one application row (in place)."""
    breakdown, total_max = _score_breakdown(row)
    matched = _split_skills(row.get("matched_skills"))
    missing = _split_skills(row.get("missing_skills"))
    row["breakdown"] = breakdown
    row["total_max"] = total_max
    row["breakdown_text"] = " · ".join(
        f"{item['label']} {item['score']}/{item['max']}" for item in breakdown
    )
    row["skills_matched"] = len(matched)
    row["skills_required"] = len(matched) + len(missing)
    return row


def _rank_applications(rows):
    """
    Set row["rank"], row["rank_tied"] and row["ranked_total"] on every row.

    Only rows that are not withdrawn AND scored are ranked, and ranking is
    done separately within each job. Standard competition ranking: tied
    scores share the best rank and the next rank is skipped (1, 1, 3).
    Unranked rows get rank None. Rows are modified in place and returned.
    """
    by_job = {}
    for row in rows:
        row["rank"] = None
        row["rank_tied"] = False
        row["ranked_total"] = None
        if row.get("status") != WITHDRAWN and _is_scored(row):
            by_job.setdefault(row["job_id"], []).append(row)

    for job_rows in by_job.values():
        # Stable sort: equal scores keep the order the query gave them.
        ordered = sorted(job_rows, key=lambda r: -(r["match_score"] or 0))
        score_counts = Counter(r["match_score"] or 0 for r in ordered)
        rank = 0
        previous_score = None
        for position, row in enumerate(ordered, start=1):
            score = row["match_score"] or 0
            if score != previous_score:
                rank = position
                previous_score = score
            row["rank"] = rank
            row["rank_tied"] = score_counts[score] > 1
            row["ranked_total"] = len(ordered)
    return rows


def _top_candidates_by_job(rows, limit=3):
    """
    {job_id: [rows]} - the best `limit` ranked, non-rejected candidates per
    job. Everyone tied with the last place at the cutoff is included, so a
    list can be longer than `limit`. `rows` must already be ranked.
    """
    per_job = {}
    for row in rows:
        if row["rank"] is None or row["status"] == "rejected":
            continue
        per_job.setdefault(row["job_id"], []).append(row)

    top = {}
    for job_id, job_rows in per_job.items():
        job_rows.sort(key=lambda r: -(r["match_score"] or 0))
        if len(job_rows) > limit:
            cutoff = job_rows[limit - 1]["match_score"] or 0
            job_rows = [r for r in job_rows if (r["match_score"] or 0) >= cutoff]
        top[job_id] = job_rows
    return top


# Step 39: Kanban board helpers

# The five main board columns, left to right (blueprint section 6.3).
# Rejected is shown separately; withdrawn is never shown.
BOARD_STAGES = ("pending", "shortlisted", "interview", "offer", "hired")
REJECTED = "rejected"

_STAGE_LABELS = {
    "pending": "Pending",
    "shortlisted": "Shortlisted",
    "interview": "Interview",
    "offer": "Offer",
    "hired": "Hired",
    "rejected": "Rejected",
}

# Display order of the buttons on a card, and the button style per kind.
_ACTION_ORDER = {"forward": 0, "back": 1, "reject": 2, "reopen": 3}
_ACTION_CSS = {
    "forward": "btn-primary",
    "back": "btn-secondary",
    "reject": "btn-danger",
    "reopen": "btn-secondary",
}


def _board_actions(status):
    """
    Buttons to show on a card whose application is in `status`.

    The list is derived from pipeline_service.allowed_next_stages(), the
    single source of truth for transitions, so a final stage (hired,
    withdrawn) gets no buttons and no invalid move is ever offered. The
    server still re-validates every POST in change_stage().
    """
    actions = []
    for target in allowed_next_stages(status):
        if target == REJECTED:
            kind, label = "reject", "Reject"
        elif status == REJECTED:
            kind, label = "reopen", f"Reopen to {_STAGE_LABELS.get(target, target)}"
        elif status in BOARD_STAGES and target in BOARD_STAGES:
            if BOARD_STAGES.index(target) > BOARD_STAGES.index(status):
                kind, label = "forward", f"Move to {_STAGE_LABELS[target]}"
            else:
                kind, label = "back", f"Back to {_STAGE_LABELS[target]}"
        else:
            continue  # a stage the board has no column for: don't offer it
        actions.append({
            "status": target,
            "label": label,
            "kind": kind,
            "css": _ACTION_CSS[kind],
        })
    actions.sort(key=lambda a: _ACTION_ORDER[a["kind"]])
    return actions


# Dashboard

@recruiter_bp.route("/dashboard")
@roles_required("recruiter")
def dashboard():
    from app import get_db

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, COUNT(*) AS total FROM job_postings "
            "WHERE created_by = %s GROUP BY status",
            (session["user_id"],),
        )
        job_counts = {row["status"]: row["total"] for row in cur.fetchall()}

        # Active applications only: withdrawn ones are excluded from every
        # dashboard count and reported separately below.
        cur.execute(
            """
            SELECT COUNT(applications.id) AS total_applications,
                   ROUND(AVG(CASE WHEN """ + SCORED_SQL + """
                                  THEN applications.match_score END), 1) AS avg_score,
                   SUM(CASE WHEN """ + SCORED_SQL + """
                            THEN 1 ELSE 0 END) AS scored,
                   SUM(CASE WHEN applications.processing_status IN ('queued', 'processing')
                            THEN 1 ELSE 0 END) AS processing,
                   SUM(CASE WHEN applications.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN applications.status = 'shortlisted' THEN 1 ELSE 0 END) AS shortlisted,
                   SUM(CASE WHEN applications.status = 'interview' THEN 1 ELSE 0 END) AS interview,
                   SUM(CASE WHEN applications.status = 'offer' THEN 1 ELSE 0 END) AS offer,
                   SUM(CASE WHEN applications.status = 'hired' THEN 1 ELSE 0 END) AS hired
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            WHERE job_postings.created_by = %s
              AND applications.status <> 'withdrawn'
            """,
            (session["user_id"],),
        )
        app_stats = cur.fetchone()

        cur.execute(
            """
            SELECT COUNT(applications.id) AS withdrawn
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            WHERE job_postings.created_by = %s
              AND applications.status = 'withdrawn'
            """,
            (session["user_id"],),
        )
        withdrawn_stats = cur.fetchone()

        cur.execute(
            """
            SELECT applications.id, applications.match_score, applications.status,
                   applications.processing_status, applications.processing_note,
                   applications.applied_at, users.name AS candidate_name,
                   job_postings.title AS job_title
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            JOIN users ON users.id = applications.candidate_id
            WHERE job_postings.created_by = %s
              AND applications.status <> 'withdrawn'
            ORDER BY applications.applied_at DESC
            LIMIT 10
            """,
            (session["user_id"],),
        )
        recent_applications = cur.fetchall()

        # Per-job summary. The withdrawn filter is in the ON clause (not
        # WHERE) so jobs with no active applicants still appear.
        cur.execute(
            """
            SELECT job_postings.id, job_postings.title, job_postings.status,
                   COUNT(applications.id) AS applicants,
                   SUM(CASE WHEN """ + SCORED_SQL + """
                            THEN 1 ELSE 0 END) AS scored,
                   ROUND(AVG(CASE WHEN """ + SCORED_SQL + """
                                  THEN applications.match_score END), 1) AS avg_score,
                   MAX(CASE WHEN """ + SCORED_SQL + """
                            THEN applications.match_score END) AS top_score,
                   SUM(CASE WHEN applications.status = 'shortlisted' THEN 1 ELSE 0 END) AS shortlisted,
                   SUM(CASE WHEN applications.duplicate_flag = 1
                              OR applications.consistency_flag = 1
                            THEN 1 ELSE 0 END) AS flagged,
                   SUM(CASE WHEN applications.processing_status IN ('queued', 'processing')
                            THEN 1 ELSE 0 END) AS processing
            FROM job_postings
            LEFT JOIN applications
                   ON applications.job_id = job_postings.id
                  AND applications.status <> 'withdrawn'
            WHERE job_postings.created_by = %s
            GROUP BY job_postings.id
            ORDER BY (job_postings.status = 'open') DESC, job_postings.created_at DESC
            """,
            (session["user_id"],),
        )
        job_summaries = cur.fetchall()

        # Every active scored application of this recruiter's jobs. Rejected
        # ones are included on purpose: ranks are computed over the whole
        # active pool of a job, and rejected rows are only hidden from the
        # top list afterwards.
        cur.execute(
            """
            SELECT applications.id, applications.job_id, applications.match_score,
                   applications.status, applications.processing_status,
                   applications.processing_note, applications.applied_at,
                   users.name AS candidate_name
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            JOIN users ON users.id = applications.candidate_id
            WHERE job_postings.created_by = %s
              AND applications.status <> 'withdrawn'
              AND """ + SCORED_SQL + """
            ORDER BY applications.applied_at ASC
            """,
            (session["user_id"],),
        )
        scored_rows = _rank_applications(cur.fetchall())

    top_by_job = _top_candidates_by_job(scored_rows, limit=3)
    for summary in job_summaries:
        summary["applicants"] = summary["applicants"] or 0
        summary["scored"] = summary["scored"] or 0
        summary["shortlisted"] = summary["shortlisted"] or 0
        summary["flagged"] = summary["flagged"] or 0
        summary["processing"] = summary["processing"] or 0
        summary["top_candidates"] = top_by_job.get(summary["id"], [])

    stats = {
        "open_jobs": job_counts.get("open", 0),
        "closed_jobs": job_counts.get("closed", 0),
        "total_applications": app_stats["total_applications"] or 0,
        "avg_score": app_stats["avg_score"] or 0,
        "scored": app_stats["scored"] or 0,
        "processing": app_stats["processing"] or 0,
        "pending": app_stats["pending"] or 0,
        "shortlisted": app_stats["shortlisted"] or 0,
        "interview": app_stats["interview"] or 0,
        "offer": app_stats["offer"] or 0,
        "hired": app_stats["hired"] or 0,
        "withdrawn": withdrawn_stats["withdrawn"] or 0,
    }

    return render_template(
        "recruiter/dashboard.html",
        stats=stats,
        recent_applications=recent_applications,
        job_summaries=job_summaries,
    )


# Jobs — create + list own jobs, open/close

@recruiter_bp.route("/jobs", methods=["GET", "POST"])
@roles_required("recruiter")
def jobs():
    from app import get_db

    db = get_db()

    if request.method == "POST":
        # Validation + INSERT live in modules/job_service.py (shared with
        # the admin Jobs page). Ownership is unchanged: created_by is
        # always the logged-in recruiter.
        data, errors = parse_and_validate_job_form(request.form)

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            insert_job(db, data, session["user_id"])
            db.commit()

            log_action(
                session["user_id"],
                "recruiter_create_job",
                f"Created job '{data['title']}'",
            )
            flash(f"Job '{data['title']}' created.", "success")

        return redirect(url_for("recruiter.jobs"))

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT job_postings.*, COUNT(applications.id) AS application_count
            FROM job_postings
            LEFT JOIN applications
                   ON applications.job_id = job_postings.id
                  AND applications.status <> 'withdrawn'
            WHERE job_postings.created_by = %s
            GROUP BY job_postings.id
            ORDER BY job_postings.created_at DESC
            """,
            (session["user_id"],),
        )
        own_jobs = cur.fetchall()

    return render_template("recruiter/jobs.html", jobs=own_jobs)


@recruiter_bp.route("/jobs/<int:job_id>/toggle-status", methods=["POST"])
@roles_required("recruiter")
def toggle_job_status(job_id):
    from app import get_db

    db = get_db()
    job = _get_own_job(db, job_id)
    new_status = "closed" if job["status"] == "open" else "open"

    set_job_status(db, job_id, new_status)
    db.commit()

    log_action(
        session["user_id"],
        "recruiter_update_job_status",
        f"Set job '{job['title']}' to {new_status}",
    )
    flash(f"Job '{job['title']}' is now {new_status}.", "success")
    return redirect(url_for("recruiter.jobs"))


# Candidates — ranked applications for a job (or across all own jobs)

@recruiter_bp.route("/candidates")
@roles_required("recruiter")
def candidates():
    from app import get_db

    db = get_db()
    job_id = request.args.get("job_id", type=int)
    status_filter = request.args.get("status") or ""
    if status_filter not in ("pending", "shortlisted", "rejected", WITHDRAWN):
        status_filter = ""  # default: every active (non-withdrawn) status

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, title, status FROM job_postings "
            "WHERE created_by = %s ORDER BY created_at DESC",
            (session["user_id"],),
        )
        own_jobs = cur.fetchall()

    selected_job = None
    if job_id:
        selected_job = _get_own_job(db, job_id)

    # The status filter is applied AFTER ranking (below), so a candidate's
    # rank does not change when the recruiter filters the list.
    query = """
        SELECT applications.id, applications.job_id, applications.match_score,
               applications.status, applications.processing_status,
               applications.processing_note,
               applications.skill_score, applications.experience_score,
               applications.education_score, applications.project_score,
               applications.certification_score, applications.relevance_score,
               applications.matched_skills, applications.missing_skills,
               applications.duplicate_flag, applications.duplicate_similarity,
               applications.consistency_flag, applications.applied_at,
               users.name AS candidate_name, users.email AS candidate_email,
               job_postings.title AS job_title,
               job_postings.status AS job_status
        FROM applications
        JOIN job_postings ON job_postings.id = applications.job_id
        JOIN users ON users.id = applications.candidate_id
        WHERE job_postings.created_by = %s
    """
    params = [session["user_id"]]

    if job_id:
        query += " AND applications.job_id = %s"
        params.append(job_id)

    # Per job (newest job first); within a job, scored applications come
    # first by score, then everything else (still analysing, failed, not
    # scored, withdrawn) by application time. A placeholder 0 is never
    # treated as a real score.
    query += (
        " ORDER BY job_postings.created_at DESC, job_postings.id DESC,"
        " (CASE WHEN " + SCORED_SQL + " THEN 0 ELSE 1 END),"
        " (CASE WHEN " + SCORED_SQL + " THEN applications.match_score END) DESC,"
        " applications.applied_at ASC"
    )

    with db.cursor() as cur:
        cur.execute(query, params)
        all_rows = cur.fetchall()

    _rank_applications(all_rows)

    if status_filter == WITHDRAWN:
        visible = [r for r in all_rows if r["status"] == WITHDRAWN]
    elif status_filter:
        visible = [r for r in all_rows if r["status"] == status_filter]
    else:
        visible = [r for r in all_rows if r["status"] != WITHDRAWN]

    groups = []
    group_by_job = {}
    for row in visible:
        _attach_score_info(row)
        group = group_by_job.get(row["job_id"])
        if group is None:
            group = {
                "job_id": row["job_id"],
                "job_title": row["job_title"],
                "job_status": row["job_status"],
                "rows": [],
            }
            group_by_job[row["job_id"]] = group
            groups.append(group)
        group["rows"].append(row)

    return render_template(
        "recruiter/candidates.html",
        own_jobs=own_jobs,
        selected_job=selected_job,
        applications=visible,
        groups=groups,
        status_filter=status_filter,
    )


# Kanban board (Step 39)

@recruiter_bp.route("/board")
@roles_required("recruiter")
def board():
    """
    One job's active applications as stage columns.

    Without ?job_id= the page lists the recruiter's own jobs to choose from.
    With ?job_id= the job must belong to the logged-in recruiter
    (_get_own_job 404s otherwise), and the applications query repeats the
    created_by condition as defence in depth.

    Ranks are computed over ALL of the job's active (non-withdrawn) scored
    applications, rejected ones included, exactly as on the candidates page
    (blueprint 6.2 rule 5); stages never change a rank. Cards are ordered
    scored-first by descending score, then everything else by application
    time, the same order candidates() uses.
    """
    from app import get_db

    db = get_db()
    job_id = request.args.get("job_id", type=int)

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, title, status FROM job_postings "
            "WHERE created_by = %s ORDER BY created_at DESC",
            (session["user_id"],),
        )
        own_jobs = cur.fetchall()

    selected_job = _get_own_job(db, job_id) if job_id else None

    rows = []
    columns = []
    rejected = []
    if selected_job is not None:
        query = (
            """
            SELECT applications.id, applications.job_id, applications.status,
                   applications.match_score, applications.processing_status,
                   applications.processing_note,
                   applications.duplicate_flag, applications.duplicate_similarity,
                   applications.consistency_flag, applications.applied_at,
                   users.name AS candidate_name
            FROM applications
            JOIN job_postings ON job_postings.id = applications.job_id
            JOIN users ON users.id = applications.candidate_id
            WHERE applications.job_id = %s
              AND job_postings.created_by = %s
              AND applications.status <> 'withdrawn'
            ORDER BY (CASE WHEN """ + SCORED_SQL + """ THEN 0 ELSE 1 END),
                     (CASE WHEN """ + SCORED_SQL + """
                           THEN applications.match_score END) DESC,
                     applications.applied_at ASC,
                     applications.id ASC
            """
        )
        with db.cursor() as cur:
            cur.execute(query, (selected_job["id"], session["user_id"]))
            rows = cur.fetchall()

        _rank_applications(rows)

        by_stage = {stage: [] for stage in BOARD_STAGES}
        for row in rows:
            row["actions"] = _board_actions(row["status"])
            if row["status"] == REJECTED:
                rejected.append(row)
            elif row["status"] in by_stage:
                by_stage[row["status"]].append(row)

        columns = [
            {"key": stage, "label": _STAGE_LABELS[stage], "rows": by_stage[stage]}
            for stage in BOARD_STAGES
        ]

    return render_template(
        "recruiter/board.html",
        own_jobs=own_jobs,
        selected_job=selected_job,
        applications=rows,
        columns=columns,
        rejected=rejected,
    )


@recruiter_bp.route("/candidates/<int:application_id>")
@roles_required("recruiter")
def candidate_detail(application_id):
    from app import get_db

    db = get_db()
    application = _get_own_application(db, application_id)

    with db.cursor() as cur:
        cur.execute(
            """
            SELECT id, resume_path, version_number, created_at
            FROM resume_versions
            WHERE candidate_id = %s
            ORDER BY version_number DESC
            """,
            (application["candidate_id"],),
        )
        resume_versions = cur.fetchall()

        # Step 35: this application's rank among the job's active scored
        # applications (ownership of the job was already verified above).
        cur.execute(
            """
            SELECT id, job_id, status, match_score,
                   processing_status, processing_note
            FROM applications
            WHERE job_id = %s AND status <> 'withdrawn'
            ORDER BY applied_at ASC
            """,
            (application["job_id"],),
        )
        job_rows = _rank_applications(cur.fetchall())

    rank_row = next((r for r in job_rows if r["id"] == application["id"]), None)

    # Step 34: work out which stored version this application was submitted
    # with. applications.resume_path is copied from the chosen version at
    # apply time, and a version attached to an application can't be deleted
    # (candidate.delete_resume_version), so the path identifies it reliably.
    # No match -> used_version stays None and the template falls back to the
    # plain "Open submitted resume" link.
    used_version = None
    for version in resume_versions:
        version["is_used"] = (
            application["resume_path"] is not None
            and version["resume_path"] == application["resume_path"]
        )
        # Compared by timestamp rather than version number, because numbers
        # can be reused after a deletion.
        version["uploaded_after_applying"] = bool(
            not version["is_used"]
            and version["created_at"] is not None
            and application["applied_at"] is not None
            and version["created_at"] > application["applied_at"]
        )
        if version["is_used"]:
            used_version = version

    matched_skills = _split_skills(application.get("matched_skills"))
    missing_skills = _split_skills(application.get("missing_skills"))
    breakdown, total_max = _score_breakdown(application)

    return render_template(
        "recruiter/candidate_detail.html",
        application=application,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        resume_versions=resume_versions,
        used_version=used_version,
        breakdown=breakdown,
        total_max=total_max,
        rank=rank_row["rank"] if rank_row else None,
        rank_tied=rank_row["rank_tied"] if rank_row else False,
        ranked_total=rank_row["ranked_total"] if rank_row else None,
        is_withdrawn=application["status"] == WITHDRAWN,
    )


@recruiter_bp.route("/candidates/<int:application_id>/resume")
@roles_required("recruiter")
def download_application_resume(application_id):
    """
    Serve the resume submitted with an application. _get_own_application only
    matches applications for jobs this recruiter created (created_by), and
    404s otherwise, so a recruiter can't open another recruiter's candidates.
    """
    from app import get_db

    application = _get_own_application(get_db(), application_id)
    return send_resume(application["resume_path"])


@recruiter_bp.route("/candidates/<int:application_id>/status", methods=["POST"])
@roles_required("recruiter")
def update_application_status(application_id):
    """
    Step 38: move an application to another hiring stage.

    Authorization/ownership is unchanged (_get_own_application 404s for
    applications on other recruiters' jobs). The transition itself is
    validated on the server by modules/pipeline_service.py, so a crafted
    POST can't skip stages or touch a withdrawn application. The status
    UPDATE and its application_stage_history row commit together or not at
    all; the audit-log entry is written only after that commit.
    """
    from app import get_db

    db = get_db()
    application = _get_own_application(db, application_id)
    detail_url = url_for("recruiter.candidate_detail", application_id=application_id)
    current_status = application["status"]

    # Step 39: actions taken on the Kanban board come back to the board.
    # The form only sends the keyword "board"; the URL is built here from the
    # application's own job (already verified as this recruiter's), so no
    # redirect target ever comes from the request.
    if request.form.get("return_to") == "board":
        return_url = url_for("recruiter.board", job_id=application["job_id"])
    else:
        return_url = detail_url

    # Withdrawn applications are final: the candidate withdrew them, so the
    # recruiter cannot move them to any stage.
    if current_status == WITHDRAWN:
        flash(
            "This application was withdrawn by the candidate; "
            "its status can't be changed.",
            "error",
        )
        return redirect(return_url)

    new_status = (request.form.get("status") or "").strip()
    if new_status not in ALL_STATUSES:
        flash("Invalid status.", "error")
        return redirect(return_url)

    # Same status as now: nothing to change, nothing to record.
    if new_status == current_status:
        flash(f"Candidate is already marked as {new_status}.", "success")
        return redirect(return_url)

    # Optional private recruiter note (stored in application_stage_history
    # only; never shown to candidates).
    note = request.form.get("note")

    try:
        change_stage(
            db,
            application_id,
            from_status=current_status,
            to_status=new_status,
            changed_by=session["user_id"],
            actor_role=ACTOR_RECRUITER,
            note=note,
        )
    except InvalidStageNote as exc:
        flash(str(exc), "error")
        return redirect(return_url)
    except InvalidTransition:
        allowed = allowed_next_stages(current_status)
        if allowed:
            hint = "Allowed next stages: " + ", ".join(allowed) + "."
        else:
            hint = "No further changes are allowed from this stage."
        flash(
            f"An application can't be moved from {current_status} "
            f"to {new_status}. {hint}",
            "error",
        )
        return redirect(return_url)
    except StageConflict:
        # The page the recruiter is looking at is out of date: the candidate
        # may have withdrawn, or another session changed the stage first.
        if get_application_status(db, application_id) == WITHDRAWN:
            flash(
                "This application was just withdrawn by the candidate; "
                "its status can't be changed. Please refresh the page.",
                "error",
            )
        else:
            flash(
                "This application was changed by someone else while you were "
                "viewing it, so your update was not applied. "
                "Please refresh the page and try again.",
                "error",
            )
        return redirect(return_url)

    # Only after the stage change + history row have committed.
    log_action(
        session["user_id"],
        "recruiter_update_application_stage",
        f"Moved application for '{application['candidate_name']}' "
        f"on job '{application['job_title']}' "
        f"from {current_status} to {new_status}",
    )
    flash(f"Candidate marked as {new_status}.", "success")
    return redirect(return_url)