from app import create_app, get_db
from modules.duplicate_detector import (
    fingerprint_status,
    FP_CURRENT,
    FP_TOO_SHORT,
    FP_OUTDATED,
    FP_INVALID,
)
import os

app = create_app()

with app.app_context():
    db = get_db()
    upload_folder = app.config["UPLOAD_FOLDER"]

    counts = {
        "current": 0,
        "too_short": 0,
        "outdated": 0,
        "invalid": 0,
        "missing_pdf": 0,
    }

    with db.cursor() as cur:
        cur.execute("""
            SELECT rv.candidate_id, rv.resume_path,
                   rf.fingerprint
            FROM resume_versions rv
            LEFT JOIN resume_fingerprints rf
              ON rf.candidate_id = rv.candidate_id
             AND rf.resume_path = rv.resume_path
            ORDER BY rv.candidate_id, rv.resume_path
        """)
        rows = cur.fetchall()

    print("\n--- Fingerprint diagnostic (read-only) ---")
    for row in rows:
        path = os.path.join(
            upload_folder, os.path.basename(row["resume_path"])
        )
        exists = os.path.isfile(path)

        if row["fingerprint"] is None:
            status = "missing"
        else:
            status = fingerprint_status(row["fingerprint"])

        if status in counts:
            counts[status] += 1
        if not exists:
            counts["missing_pdf"] += 1

        print(
            f'Candidate {row["candidate_id"]}: '
            f'{row["resume_path"]} | '
            f'fingerprint={status} | PDF_exists={exists}'
        )

    print("\n--- Summary ---")
    for name, count in counts.items():
        print(f"{name}: {count}")

    db.rollback()
