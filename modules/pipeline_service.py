"""
Recroot — Hiring Pipeline Service
Step 38 of the build.

Single home for the hiring-stage rules, shared by modules/recruiter.py
(recruiter stage changes) and modules/candidate.py (apply + withdraw).

What lives here
    * STAGE_TRANSITIONS  — the ONE dictionary of allowed recruiter stage
                           changes. Nothing else in the project should
                           hard-code transition rules.
    * validate_transition() / allowed_next_stages() — server-side checks.
    * change_stage()     — the atomic stage change: conditional UPDATE on
                           `applications` + INSERT into
                           `application_stage_history`, one transaction.
    * insert_stage_history() / record_initial_stage() — history-row
                           helpers that work on a caller-owned cursor, so
                           candidate.apply_to_job() can put the first
                           history row in the same transaction as the
                           application INSERT.

Conventions (same as the rest of the project)
    * No ORM. Raw SQL on the DictCursor connection returned by
      app.get_db() (pymysql, autocommit=False). This module never opens
      connections itself: callers pass `db` in, so there is no
      `from app import get_db` here and no circular import.
    * Transaction ownership: change_stage() commits or rolls back the
      caller's connection. insert_stage_history() / record_initial_stage()
      never commit — the caller owns the transaction.
    * Audit logging (modules.auth.log_action) is NOT done here. It opens
      its own commit, so routes call it AFTER change_stage() has committed.

Privacy
    * `application_stage_history.note` is the recruiter's private note.
      It must never be selected by any candidate-facing query or shown on
      any candidate template. This module only ever writes it.

Concurrency
    * The conditional UPDATE (`... WHERE id = %s AND status = %s`) is the
      guard. InnoDB's UPDATE performs a "current read", so it sees the
      latest committed status even if the caller's earlier SELECT came
      from an older REPEATABLE READ snapshot. If another request changed
      the status first, the UPDATE matches no row, change_stage() rolls
      back and raises StageConflict. A successful UPDATE always changes
      the row (status differs from the new status for every valid
      transition), so MySQL's "changed rows" count is reliable here.
"""

PENDING = "pending"
SHORTLISTED = "shortlisted"
INTERVIEW = "interview"
OFFER = "offer"
HIRED = "hired"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"

# Same values, same order, as the `applications.status` ENUM in schema.sql.
ALL_STATUSES = (
    PENDING, SHORTLISTED, INTERVIEW, OFFER, HIRED, REJECTED, WITHDRAWN,
)

# Allowed RECRUITER (and system) transitions: current status -> next statuses.
# `withdrawn` is reached only through the candidate's withdraw action, and is
# final: nobody can move an application out of it. `hired` is final too.
STAGE_TRANSITIONS = {
    PENDING:     (SHORTLISTED, REJECTED),
    SHORTLISTED: (INTERVIEW, REJECTED, PENDING),
    INTERVIEW:   (OFFER, REJECTED, SHORTLISTED),
    OFFER:       (HIRED, REJECTED, INTERVIEW),
    HIRED:       (),
    REJECTED:    (PENDING,),   # reopening
    WITHDRAWN:   (),
}

# Statuses a candidate may withdraw from. Withdrawing from `offer` means
# declining the offer. hired / rejected / withdrawn cannot be withdrawn.
CANDIDATE_WITHDRAWABLE_STATUSES = (PENDING, SHORTLISTED, INTERVIEW, OFFER)

# application_stage_history.actor_role ENUM
ACTOR_RECRUITER = "recruiter"
ACTOR_CANDIDATE = "candidate"
ACTOR_SYSTEM = "system"
ACTOR_ROLES = (ACTOR_RECRUITER, ACTOR_CANDIDATE, ACTOR_SYSTEM)

# application_stage_history.note is VARCHAR(500)
NOTE_MAX_LENGTH = 500


# Exceptions

class PipelineError(Exception):
    """Base class for pipeline-service errors."""


class InvalidTransition(PipelineError):
    """The requested stage change is not allowed for this actor."""

    def __init__(self, from_status, to_status, actor_role):
        self.from_status = from_status
        self.to_status = to_status
        self.actor_role = actor_role
        super().__init__(
            f"{actor_role} cannot move an application from "
            f"{from_status!r} to {to_status!r}"
        )


class StageConflict(PipelineError):
    """
    The conditional UPDATE matched no row: the application's status is no
    longer what the caller read (changed or withdrawn in the meantime).
    """


class InvalidStageNote(PipelineError):
    """The note is not a string or is longer than NOTE_MAX_LENGTH."""


# Validation helpers (pure functions, no database access)

def allowed_next_stages(from_status):
    """Statuses a recruiter may move an application to from `from_status`."""
    return list(STAGE_TRANSITIONS.get(from_status, ()))


def can_candidate_withdraw(status):
    return status in CANDIDATE_WITHDRAWABLE_STATUSES


def is_valid_transition(from_status, to_status, actor_role=ACTOR_RECRUITER):
    """
    True if `actor_role` may move an application from `from_status` to
    `to_status`. Unknown statuses, unknown roles and same-status "moves"
    are all invalid.
    """
    if actor_role == ACTOR_CANDIDATE:
        return to_status == WITHDRAWN and can_candidate_withdraw(from_status)
    if actor_role in (ACTOR_RECRUITER, ACTOR_SYSTEM):
        return to_status in STAGE_TRANSITIONS.get(from_status, ())
    return False


def validate_transition(from_status, to_status, actor_role=ACTOR_RECRUITER):
    """Raise InvalidTransition unless the change is allowed."""
    if not is_valid_transition(from_status, to_status, actor_role):
        raise InvalidTransition(from_status, to_status, actor_role)


def normalize_note(note):
    """
    Trim a note; empty/whitespace-only becomes None. Raises InvalidStageNote
    for a non-string or a note longer than NOTE_MAX_LENGTH. Never truncates
    silently, so a recruiter never loses part of what they wrote.
    """
    if note is None:
        return None
    if not isinstance(note, str):
        raise InvalidStageNote("Note must be text.")
    note = note.strip()
    if not note:
        return None
    if len(note) > NOTE_MAX_LENGTH:
        raise InvalidStageNote(
            f"Note is too long ({len(note)} characters; "
            f"the limit is {NOTE_MAX_LENGTH})."
        )
    return note


# History rows (caller owns the transaction — these never commit)

def insert_stage_history(
    cur, application_id, from_status, to_status, changed_by, actor_role,
    note=None,
):
    """
    INSERT one application_stage_history row using the caller's cursor.
    `from_status` is None only for the first row of an application.
    """
    if actor_role not in ACTOR_ROLES:
        raise ValueError(f"Unknown actor_role: {actor_role!r}")
    if to_status not in ALL_STATUSES:
        raise ValueError(f"Unknown to_status: {to_status!r}")
    if from_status is not None and from_status not in ALL_STATUSES:
        raise ValueError(f"Unknown from_status: {from_status!r}")

    cur.execute(
        """
        INSERT INTO application_stage_history
            (application_id, from_status, to_status,
             changed_by, actor_role, note)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (application_id, from_status, to_status, changed_by, actor_role, note),
    )


def record_initial_stage(cur, application_id, candidate_id):
    """
    First history row of a new application: NULL -> 'pending', made by the
    candidate who applied. Call it on the same cursor/transaction as the
    applications INSERT.
    """
    insert_stage_history(
        cur,
        application_id,
        None,
        PENDING,
        candidate_id,
        ACTOR_CANDIDATE,
    )


# The atomic stage change

def change_stage(
    db, application_id, from_status, to_status, changed_by, actor_role,
    note=None, candidate_id=None,
):
    """
    Move an application from `from_status` to `to_status` atomically.

    `from_status` is the status the caller observed. Steps:
        1. Validate the transition on the server (InvalidTransition).
        2. UPDATE applications SET status = %s, stage_changed_at = NOW()
           WHERE id = %s AND status = %s   [AND candidate_id = %s]
        3. rowcount == 0  ->  roll back, raise StageConflict.
        4. INSERT the application_stage_history row, same transaction.
        5. COMMIT. Any failure on the way rolls back BOTH statements.

    `candidate_id` is an optional extra ownership condition for candidate
    actions (defence in depth; the route has already checked ownership).

    The caller must have done its own authorization (e.g. the recruiter
    owns the job, the candidate owns the application) — this function
    checks the transition, not who is asking.

    Returns {"application_id", "from_status", "to_status"}.
    Raises InvalidTransition, InvalidStageNote, StageConflict; any database
    error is rolled back and re-raised unchanged.
    """
    validate_transition(from_status, to_status, actor_role)
    note = normalize_note(note)

    sql = (
        "UPDATE applications SET status = %s, stage_changed_at = NOW() "
        "WHERE id = %s AND status = %s"
    )
    params = [to_status, application_id, from_status]
    if candidate_id is not None:
        sql += " AND candidate_id = %s"
        params.append(candidate_id)

    try:
        with db.cursor() as cur:
            cur.execute(sql, params)
            if cur.rowcount < 1:
                raise StageConflict(
                    f"Application {application_id} is no longer "
                    f"{from_status!r}."
                )
            insert_stage_history(
                cur, application_id, from_status, to_status,
                changed_by, actor_role, note,
            )
        db.commit()
    except Exception:
        _safe_rollback(db)
        raise

    return {
        "application_id": application_id,
        "from_status": from_status,
        "to_status": to_status,
    }


def get_application_status(db, application_id):
    """
    Fresh read of an application's current status (None if the row is gone).
    Used after a StageConflict to say what actually happened. The read runs
    right after change_stage()'s rollback, so it opens a new snapshot; it is
    committed afterwards so no stale snapshot is left behind.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT status FROM applications WHERE id = %s",
            (application_id,),
        )
        row = cur.fetchone()
    db.commit()
    return row["status"] if row else None


def _safe_rollback(db):
    try:
        db.rollback()
    except Exception:
        # The original exception is what matters; don't mask it.
        pass