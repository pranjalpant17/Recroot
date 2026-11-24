from app import create_app, get_db
from modules.duplicate_detector import (
    fingerprint_status,
    parse_fingerprint,
    estimate_similarity,
)

app = create_app()

with app.app_context():
    db = get_db()

    paths = [
        (13, "uploads/resumes/candidate_13_v2_ebdcf524.pdf"),
        (15, "uploads/resumes/candidate_15_v1_a24c604e.pdf"),
    ]

    fingerprints = []

    with db.cursor() as cur:
        for candidate_id, path in paths:
            cur.execute(
                "SELECT fingerprint FROM resume_fingerprints "
                "WHERE candidate_id = %s AND resume_path = %s",
                (candidate_id, path),
            )
            row = cur.fetchone()

            if not row:
                raise RuntimeError(f"Fingerprint not found: {path}")

            status = fingerprint_status(row["fingerprint"])
            print(f"{path}: {status}")

            parsed = parse_fingerprint(row["fingerprint"])
            if not parsed or not parsed.get("signature"):
                raise RuntimeError(f"Fingerprint is not comparable: {path}")

            fingerprints.append(parsed["signature"])

    score = estimate_similarity(fingerprints[0], fingerprints[1])
    print(f"\nAyush v2 vs Bhupesh v1 similarity: {score}%")
    db.rollback()
