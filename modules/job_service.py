"""
Recroot — Job Service (shared job logic)

Single home for the job rules that both the Recruiter and Admin modules
need, so they are written once:

    * parse_and_validate_job_form()  - read + validate the job form
    * insert_job()                   - INSERT a new, open job_postings row
    * set_job_status()               - explicit open/closed status update
    * get_recruiter()                - look up a user ONLY if role = 'recruiter'

Design rules:
    * No Flask routing, no session access, no audit logging and no
      commit/rollback in here. Authorisation (roles_required, ownership
      checks), transaction control (db.commit()) and audit logging
      (modules.auth.log_action) stay in the calling route, exactly where
      they were before this module existed.
    * set_job_status() only ever touches job_postings.status. It never
      deletes or modifies applications, scores, resumes or fingerprints,
      so closing a job can never lose candidate data.
    * There is intentionally NO delete-job function.
    * No ORM - raw SQL on the DictCursor connection from app.get_db().
"""

VALID_JOB_STATUSES = ("open", "closed")


def parse_and_validate_job_form(form):
    """
    Read the job form fields and validate them.

    Returns (data, errors):
        data   - dict with title, description, required_skills,
                 preferred_skills, experience_required (int),
                 education_required, deadline (str or None)
        errors - list of human-readable messages (empty when valid)

    Rules and messages are identical to the original recruiter.jobs() code.
    """
    title = (form.get("title") or "").strip()
    description = (form.get("description") or "").strip()
    required_skills = (form.get("required_skills") or "").strip()
    preferred_skills = (form.get("preferred_skills") or "").strip()
    experience_required = form.get("experience_required") or "0"
    education_required = (form.get("education_required") or "").strip()
    deadline = (form.get("deadline") or "").strip() or None

    errors = []
    if not title:
        errors.append("Job title is required.")
    if not required_skills:
        errors.append("At least one required skill is needed.")
    try:
        experience_required = int(experience_required)
        if experience_required < 0:
            raise ValueError
    except ValueError:
        errors.append("Experience required must be a non-negative number.")
        experience_required = 0

    data = {
        "title": title,
        "description": description,
        "required_skills": required_skills,
        "preferred_skills": preferred_skills,
        "experience_required": experience_required,
        "education_required": education_required,
        "deadline": deadline,
    }
    return data, errors


def insert_job(db, data, created_by):
    """
    Insert a new job with status 'open', owned by `created_by`
    (job_postings.created_by - the recruiter who manages it).

    Does NOT commit; the caller commits. Returns the new job id.
    """
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_postings
                (title, description, required_skills, preferred_skills,
                 experience_required, education_required, created_by,
                 status, deadline)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'open', %s)
            """,
            (
                data["title"],
                data["description"],
                data["required_skills"],
                data["preferred_skills"],
                data["experience_required"],
                data["education_required"],
                created_by,
                data["deadline"],
            ),
        )
        return cur.lastrowid


def set_job_status(db, job_id, new_status):
    """
    Set a job's status to an explicit target ('open' or 'closed').

    Only job_postings.status is updated. Applications, scores and resumes
    are never touched. Does NOT commit; the caller commits.
    """
    if new_status not in VALID_JOB_STATUSES:
        raise ValueError(f"Invalid job status: {new_status!r}")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE job_postings SET status = %s WHERE id = %s",
            (new_status, job_id),
        )


def get_recruiter(db, user_id):
    """
    Return {id, name, email} for `user_id` only if that user exists AND has
    the 'recruiter' role; otherwise None. Used to validate the admin's
    "Assign to Recruiter" selection on the server.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, email FROM users WHERE id = %s AND role = 'recruiter'",
            (user_id,),
        )
        return cur.fetchone()