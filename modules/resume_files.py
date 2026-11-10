"""
Recroot — Resume file access (Step 36)

Resumes are stored in Config.UPLOAD_FOLDER (static/uploads/resumes/), but they
are NOT served from the public /static URL any more (app.py blocks that
path). Instead the candidate and recruiter blueprints expose download routes
that first check who is asking and then call send_resume() below.

This module only answers "is this stored path a safe, existing resume file,
and can we send it?". WHO may see a given resume is decided by the routes:

    candidate.download_resume_version      own resume versions only
    candidate.download_application_resume  own applications only
    recruiter.download_application_resume  applications for jobs the recruiter
                                           created (job_postings.created_by)

Admins have no application or resume pages in the current role rules, so they
get no download route.

The path stored in the database ("uploads/resumes/<file>.pdf") is never used
as given. Only its final file name is kept, and the result is resolved against
the real upload folder, so a bad or tampered value such as
"uploads/resumes/../../app.py", an absolute path or a symlink pointing
elsewhere cannot leave the folder.
"""

import logging
import os
import posixpath

from flask import abort, current_app, send_file

logger = logging.getLogger(__name__)

STORED_PREFIX = "uploads/resumes/"


def is_inside_upload_folder(path):
    """True if `path` resolves to a location inside Config.UPLOAD_FOLDER."""
    root = os.path.normcase(os.path.realpath(current_app.config["UPLOAD_FOLDER"]))
    target = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([root, target]) == root and target != root
    except ValueError:  # e.g. different drives on Windows
        return False


def resolve_resume_path(stored_path):
    """
    Map a database resume_path to an absolute file path, or return None if the
    value is not a well-formed "uploads/resumes/<name>" path or the file is
    missing or outside the upload folder.
    """
    if not stored_path or not isinstance(stored_path, str):
        return None
    if "\\" in stored_path or "\x00" in stored_path:
        return None
    if not stored_path.startswith(STORED_PREFIX):
        return None

    name = stored_path[len(STORED_PREFIX):]
    # A single plain file name only: no sub-folders, no "." / ".." segments.
    if not name or posixpath.basename(name) != name or name in (".", ".."):
        return None

    candidate = os.path.join(current_app.config["UPLOAD_FOLDER"], name)
    if not is_inside_upload_folder(candidate) or not os.path.isfile(candidate):
        return None
    return os.path.realpath(candidate)


def send_resume(stored_path):
    """
    Send the resume PDF inline (opens in the browser tab, like the old static
    link did). Call this only AFTER the route has checked permissions.
    Aborts with 404 if the file cannot be served.
    """
    absolute_path = resolve_resume_path(stored_path)
    if absolute_path is None:
        logger.warning("Resume file unavailable for stored path %r", stored_path)
        abort(404)

    response = send_file(
        absolute_path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=os.path.basename(absolute_path),
        conditional=True,
        max_age=0,
    )
    # Personal documents: never let a shared cache keep them, never sniff.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response