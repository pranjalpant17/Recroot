"""
Recroot — Admin Analytics (Step 42, Part B)

Read-only aggregate queries for the admin analytics page:

    * Applications per job
    * Average candidate score
    * Most requested skills
    * Skill gaps

Conventions taken from the existing schema and blueprint (Section 12):

    scored application = processing_status = 'done'
                         AND status <> 'withdrawn'
                         AND processing_note does not contain 'Not scored:'

    A placeholder match_score of 0 on an unscored row is therefore never
    averaged in, and withdrawn applications never count as scored.

    job_postings.required_skills, applications.matched_skills and
    applications.missing_skills are comma-separated text. Skills are compared
    case-insensitively with whitespace collapsed; the first spelling seen is
    the one displayed. The scoring engine's alias groups (REST API / RESTful,
    Node.js / nodejs, ...) are NOT applied here, so spelling variants are
    counted separately.

Raw SQL through the DictCursor connection from app.get_db(), no ORM, no
parameters (so no '%' escaping concerns), SELECT statements only.
"""

from collections import OrderedDict

TOP_SKILLS = 10

# Shared SQL predicate for "a scored, non-withdrawn application" (alias `a`).
# LOCATE() instead of LIKE keeps '%' characters out of the SQL text.
SCORED_SQL = (
    "(a.status <> 'withdrawn' AND a.processing_status = 'done' "
    "AND (a.processing_note IS NULL "
    "OR LOCATE('Not scored:', a.processing_note) = 0))"
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _round1(value):
    """Round half-up to one decimal (scores are never negative). None stays None."""
    if value is None:
        return None
    try:
        return int(float(value) * 10 + 0.5) / 10.0
    except (TypeError, ValueError):
        return None


def _percent(part, whole):
    if whole <= 0:
        return 0
    return max(0, min(100, int(round(part * 100.0 / whole))))


def split_skills(text):
    """'Python, Flask;MySQL\\nGit' -> ['Python', 'Flask', 'MySQL', 'Git'] (no empties)."""
    if not text:
        return []
    cleaned = str(text).replace(";", ",").replace("\n", ",").replace("\r", ",")
    skills = []
    for part in cleaned.split(","):
        skill = " ".join(part.split())
        if skill:
            skills.append(skill)
    return skills


def skill_key(skill):
    """Comparison key: case-insensitive, whitespace-collapsed."""
    return " ".join(str(skill).split()).casefold()


def _unique_skills(text):
    """OrderedDict key -> display spelling, de-duplicated within one field."""
    found = OrderedDict()
    for skill in split_skills(text):
        found.setdefault(skill_key(skill), skill)
    return found


def build_skill_analytics(jobs, scored_applications, top_n=TOP_SKILLS):
    """
    jobs                 iterable of dicts with 'status' and 'required_skills'
    scored_applications  iterable of dicts with 'matched_skills', 'missing_skills'
                         (already filtered to scored, non-withdrawn applications)

    Returns {'requested': [...], 'gaps': [...], 'jobs_with_skills': n,
             'distinct_requested': n, 'distinct_gaps': n}.

    requested row: skill, jobs, open_jobs, pct_of_jobs
    gap row:       skill, missing, evaluated, gap_pct
                   (evaluated = scored applications where the skill was either
                    matched or missing, so gap_pct = share of evaluated
                    candidates who lack it)
    """
    # ---- most requested skills (from job postings) -----------------------
    display = {}
    job_count = {}
    open_count = {}
    jobs_with_skills = 0
    for job in jobs:
        skills = _unique_skills(job.get("required_skills"))
        if not skills:
            continue
        jobs_with_skills += 1
        is_open = job.get("status") == "open"
        for key, name in skills.items():
            display.setdefault(key, name)
            job_count[key] = job_count.get(key, 0) + 1
            if is_open:
                open_count[key] = open_count.get(key, 0) + 1

    requested = [
        {
            "skill": display[key],
            "jobs": count,
            "open_jobs": open_count.get(key, 0),
            "pct_of_jobs": _percent(count, jobs_with_skills),
        }
        for key, count in job_count.items()
    ]
    requested.sort(key=lambda r: (-r["jobs"], -r["open_jobs"], r["skill"].casefold()))

    # ---- skill gaps (from scored applications) ---------------------------
    gap_display = {}
    missing_count = {}
    evaluated_count = {}
    for app in scored_applications:
        matched = _unique_skills(app.get("matched_skills"))
        missing = _unique_skills(app.get("missing_skills"))
        for key, name in matched.items():
            gap_display.setdefault(key, name)
            evaluated_count[key] = evaluated_count.get(key, 0) + 1
        for key, name in missing.items():
            if key in matched:        # contradictory row: matched wins
                continue
            gap_display.setdefault(key, name)
            evaluated_count[key] = evaluated_count.get(key, 0) + 1
            missing_count[key] = missing_count.get(key, 0) + 1

    gaps = [
        {
            "skill": gap_display[key],
            "missing": count,
            "evaluated": evaluated_count[key],
            "gap_pct": _percent(count, evaluated_count[key]),
        }
        for key, count in missing_count.items()
    ]
    gaps.sort(key=lambda g: (-g["missing"], -g["gap_pct"], g["skill"].casefold()))

    return {
        "requested": requested[:top_n],
        "gaps": gaps[:top_n],
        "jobs_with_skills": jobs_with_skills,
        "distinct_requested": len(requested),
        "distinct_gaps": len(gaps),
    }


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------

def get_analytics(db):
    """Run the analytics queries and return everything the page needs."""
    with db.cursor() as cur:
        # Applications per job (every job appears, even with zero applications)
        cur.execute(
            f"""
            SELECT j.id, j.title, j.status, u.name AS owner_name,
                   COUNT(a.id) AS total_applications,
                   COALESCE(SUM(CASE WHEN a.status <> 'withdrawn'
                                     THEN 1 ELSE 0 END), 0) AS active_applications,
                   COALESCE(SUM(CASE WHEN {SCORED_SQL}
                                     THEN 1 ELSE 0 END), 0) AS scored_applications,
                   AVG(CASE WHEN {SCORED_SQL} THEN a.match_score END) AS avg_score,
                   MAX(CASE WHEN {SCORED_SQL} THEN a.match_score END) AS top_score
            FROM job_postings j
            LEFT JOIN users u ON u.id = j.created_by
            LEFT JOIN applications a ON a.job_id = j.id
            GROUP BY j.id, j.title, j.status, u.name
            ORDER BY total_applications DESC, j.title ASC
            """
        )
        job_rows = cur.fetchall()

        # Overall numbers (one pass, same scored definition)
        cur.execute(
            f"""
            SELECT COUNT(*) AS total_applications,
                   COALESCE(SUM(CASE WHEN a.status = 'withdrawn'
                                     THEN 1 ELSE 0 END), 0) AS withdrawn,
                   COALESCE(SUM(CASE WHEN {SCORED_SQL}
                                     THEN 1 ELSE 0 END), 0) AS scored,
                   AVG(CASE WHEN {SCORED_SQL} THEN a.match_score END) AS avg_score,
                   MIN(CASE WHEN {SCORED_SQL} THEN a.match_score END) AS min_score,
                   MAX(CASE WHEN {SCORED_SQL} THEN a.match_score END) AS max_score
            FROM applications a
            """
        )
        overall_row = cur.fetchone() or {}

        # Inputs for the skill statistics
        cur.execute("SELECT id, status, required_skills FROM job_postings")
        skill_jobs = cur.fetchall()

        cur.execute(
            f"""
            SELECT a.id, a.matched_skills, a.missing_skills
            FROM applications a
            WHERE {SCORED_SQL}
            """
        )
        scored_apps = cur.fetchall()

    jobs = []
    max_total = 0
    for row in job_rows:
        total = _to_int(row["total_applications"])
        max_total = max(max_total, total)
        jobs.append({
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "owner_name": row["owner_name"],
            "total": total,
            "active": _to_int(row["active_applications"]),
            "scored": _to_int(row["scored_applications"]),
            "avg_score": _round1(row["avg_score"]),
            "top_score": _to_int(row["top_score"]) if row["top_score"] is not None else None,
        })
    for job in jobs:
        job["bar_pct"] = _percent(job["total"], max_total)

    total_apps = _to_int(overall_row.get("total_applications"))
    withdrawn = _to_int(overall_row.get("withdrawn"))
    scored = _to_int(overall_row.get("scored"))
    active = total_apps - withdrawn

    overall = {
        "total_applications": total_apps,
        "withdrawn": withdrawn,
        "active": active,
        "scored": scored,
        "unscored_active": max(0, active - scored),   # still analysing, failed, not scored
        "avg_score": _round1(overall_row.get("avg_score")),
        "min_score": (_to_int(overall_row["min_score"])
                      if overall_row.get("min_score") is not None else None),
        "max_score": (_to_int(overall_row["max_score"])
                      if overall_row.get("max_score") is not None else None),
        "job_count": len(jobs),
    }

    skills = build_skill_analytics(skill_jobs, scored_apps)
    max_req = max((r["jobs"] for r in skills["requested"]), default=0)
    for row in skills["requested"]:
        row["bar_pct"] = _percent(row["jobs"], max_req)

    return {"overall": overall, "jobs": jobs, "skills": skills}