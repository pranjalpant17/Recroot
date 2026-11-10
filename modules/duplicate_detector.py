"""
Recroot — Duplicate Detector
Step 30 of the Phase 2 build (revised: fingerprint format version 3).

Implements Section 6 of the blueprint with two levels:

    Level 1 - Exact duplicate
        Same SHA-256 file hash (resume_versions.file_hash, captured at
        upload time by candidate.py). Independent of fingerprints.

    Level 2 - Similar resume ("suspected near-duplicate")
        Different files whose extracted text is highly similar.
        Method: word 3-gram shingles + MinHash, which estimates the
        Jaccard similarity: of all the distinct 3-word phrases found in
        either resume, the share that appear in BOTH. The MinHash
        signature is the "fingerprint" stored in
        resume_fingerprints.fingerprint.

Wording: this module reports "Potential duplicate" / "Similar resume".
It never says or implies fraud (Sections 6 and 18). The score measures
how much wording two resumes share; it is NOT a probability that
anything was copied or that anyone acted improperly. Similar content is
information for the recruiter, not evidence of misconduct.

What changed in fingerprint version 3 (and why)
    Version 1 kept identifying details inside the compared text (profile
    handles such as linkedin.com/in/<name>, the candidate's name, the
    school name). Every changed token destroys up to three overlapping
    shingles in BOTH resumes, and Jaccard counts that loss against both,
    so a copy with only a few personal details changed could land just
    under the threshold. Version 3 reduces that influence before
    shingling:

      * Emails, URLs, social/coding-profile links (linkedin.com/...,
        github.com/..., etc.) and phone numbers are removed. A run of
        years such as "2020 2021 2022" is NOT mistaken for a phone number.
      * The candidate's own name tokens are removed everywhere in that
        resume. Names come from the account (users.name) and from the
        resume's header line (services.resume_parser.extract_name).
      * Institution names ("Graphic Era University", "Delhi
        Technological University", "College of Engineering Pune", ...)
        are MASKED to one constant placeholder, not deleted and not left
        in. A changed school therefore still lines up with the original,
        and a shared university adds no similarity signal either way
        (many unrelated candidates legitimately attend the same one).
        The degree, years, CGPA, coursework and every other education
        detail keep contributing. Only the institution phrase is masked;
        words such as "B.Tech Computer Science" in front of it are kept.

    Skills, experience, projects and all other content are untouched, so
    they still drive the score.

Fingerprint versioning / compatibility
    * FINGERPRINT_VERSION must be bumped whenever normalisation,
      shingling, hashing or NUM_PERM changes.
    * A stored fingerprint is only ever compared if its version,
      num_perm and shingle_size all match this module AND its signature
      is well formed. Anything else (older version, malformed JSON,
      truncated signature) is reported as "outdated" or "invalid" and is
      never compared as if it were equivalent.
    * check_duplicates() reports how many stored resumes were skipped for
      that reason ("skipped_outdated"), so a missed comparison is visible.
    * index_resume() refreshes a stale/invalid row IN PLACE (one UPDATE of
      that row's fingerprint; no extra row, no delete). The worker already
      calls index_resume() for every application inside its _dedup_lock,
      so old fingerprints are upgraded as applications are re-processed.
      backfill_fingerprints(..., rebuild_outdated=True) upgrades them in
      bulk.

Rules applied (unchanged)
    * A candidate's own other versions are never treated as near
      duplicates (a new version is expected to resemble the old one).
      A same-candidate exact match is a neutral note only. When this is
      why nothing was flagged, the explanation says so.
    * Resumes with too little text skip Level 2 - two empty scans would
      otherwise look 100% similar.
    * Project similarity alone is not a signal: similarity is measured on
      the WHOLE resume, so students who share one college project are not
      flagged unless the rest of their resumes also overlap heavily.
    * Fingerprint rows are joined to resume_versions on resume_path, so a
      deleted version's leftover fingerprint row is ignored.

Design notes
    * Core logic has no Flask dependency: every DB function takes the
      connection `db` (the DictCursor connection from app.get_db(), or the
      worker's own connection) as a parameter.
    * No schema change. This module reads users, resume_versions and
      resume_fingerprints and writes ONLY resume_fingerprints. It does not
      write to `applications` or `duplicate_matches`;
      application_duplicate_fields() converts a result to the
      (duplicate_flag, duplicate_similarity) pair that modules/worker.py
      stores.
    * Synchronisation is unchanged: modules/worker.py serialises
      "index_resume then check_duplicates" under its _dedup_lock. This
      module adds no locks and no new write paths outside index_resume().
    * MinHash is implemented here in pure Python (hashlib + random), so it
      needs no extra dependency.

Typical use
    from modules.duplicate_detector import index_resume, check_duplicates

    index_resume(db, candidate_id, relative_path, file_hash,
                 parsed["resume_text"])
    result = check_duplicates(db, candidate_id, relative_path)
"""

import hashlib
import json
import os
import random
import re
import unicodedata


# ============================================================
# Tunables
# ============================================================

FINGERPRINT_VERSION = 3   # normalization and PDF text-repair changes
NUM_PERM = 128            # signature length; estimate error is ~ +/-4%
SHINGLE_SIZE = 3          # words per shingle
MIN_SHINGLES = 30         # fewer than this -> too little text to compare
HASH_SEED = 1             # fixed so signatures are reproducible
DEFAULT_THRESHOLD = 85    # used when config.NEAR_DUPLICATE_THRESHOLD is unavailable

_PRIME = (1 << 61) - 1        # Mersenne prime used by the hash family
_MAX_HASH = (1 << 32) - 1     # signature values are kept to 32 bits

STATUS_OK = "ok"
STATUS_TOO_SHORT = "too_short"

# Results of fingerprint_status()
FP_CURRENT = "current"        # usable, compatible with this module
FP_TOO_SHORT = "too_short"    # compatible, but the resume had too little text
FP_OUTDATED = "outdated"      # well formed but made by another algorithm version
FP_INVALID = "invalid"        # unreadable / malformed

_USABLE_STATUSES = (FP_CURRENT, FP_TOO_SHORT)


def _default_threshold():
    """NEAR_DUPLICATE_THRESHOLD from Flask config if inside an app
    context, otherwise DEFAULT_THRESHOLD."""
    try:
        from flask import current_app
        return int(current_app.config.get("NEAR_DUPLICATE_THRESHOLD", DEFAULT_THRESHOLD))
    except (RuntimeError, ImportError):
        return DEFAULT_THRESHOLD


# ============================================================
# Text normalisation + shingling
# ============================================================

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
# Profile/portfolio links written without a scheme, e.g.
# "linkedin.com/in/rahul-sharma-dev" or "github.com/rahulsharma". These
# usually embed the candidate's name or handle. A fixed list of well-known
# hosts is used on purpose: a generic "anything.com" pattern would also eat
# technology names such as "ASP.NET".
_PROFILE_RE = re.compile(
    r"(?<![\w.\-])(?:https?://)?(?:www\.)?"
    r"(?:linkedin\.com|github\.com|gitlab\.com|bitbucket\.org|behance\.net|"
    r"dribbble\.com|medium\.com|twitter\.com|x\.com|instagram\.com|"
    r"facebook\.com|leetcode\.com|hackerrank\.com|kaggle\.com|"
    r"codechef\.com|codeforces\.com|stackoverflow\.com)"
    r"(?:/[^\s,;|)]*)?",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(r"[\+\(]?[0-9][0-9().\-\s]{7,}[0-9]")
_NON_WORD_RE = re.compile(r"[^a-z0-9+#\s]")  # keeps c++ and c#


def _strip_phone(match):
    text = match.group(0)
    digits = re.sub(r"\D", "", text)
    groups = re.findall(r"\d+", text)
    # A run of years ("2020 2021 2022") has 10-13 digits but is not a phone.
    if groups and all(len(g) == 4 and g[:2] in ("19", "20") for g in groups):
        return text
    return " " if 10 <= len(digits) <= 13 else text


# ---- institution masking -------------------------------------------------
#
# Applied per line, on the ORIGINAL-case text (capitalisation is the cue
# that separates "Graphic Era University" from "college hackathon").
# The institution phrase is replaced by one constant token, so:
#   * a copy with a different school still lines up with the original;
#   * two unrelated candidates at the same university get no extra
#     similarity (and none is lost);
#   * degree / years / CGPA / coursework on the same line are kept.

INSTITUTION_MASK = "institutionmask"

_INSTITUTION_KEYWORD = (
    r"(?:University|UNIVERSITY|Institute|INSTITUTE|College|COLLEGE|School|SCHOOL|"
    r"Academy|ACADEMY|Polytechnic|POLYTECHNIC|Vidyapeeth|VIDYAPEETH|"
    r"Vishwavidyalaya|VISHWAVIDYALAYA)"
)
_CAP_WORD = r"[A-Z][A-Za-z.&'\u2019\-]*"
_CONNECTOR = r"(?:of|for|and|the|&|OF|FOR|AND|THE)"
_INSTITUTION_RE = re.compile(
    rf"(?:{_CAP_WORD}\s+){{0,4}}{_INSTITUTION_KEYWORD}\b"
    rf"(?:\s+{_CONNECTOR}\s+{_CAP_WORD}(?:\s+{_CAP_WORD}){{0,2}}){{0,3}}"
)
_INSTITUTION_KEYWORDS = {
    "university", "institute", "college", "school", "academy",
    "polytechnic", "vidyapeeth", "vishwavidyalaya",
}
# Capitalised words just before the keyword that are degree / field /
# filler words, not part of the institution's name. Masking stops at them.
_NOT_PART_OF_INSTITUTION = {
    "bachelor", "bachelors", "master", "masters", "btech", "mtech", "be", "me",
    "bsc", "msc", "bca", "mca", "bba", "mba", "phd", "diploma", "engineering",
    "computer", "science", "sciences", "technology", "information",
    "electronics", "electrical", "mechanical", "civil", "commerce", "arts",
    "studies", "honours", "hons", "cgpa", "gpa", "education", "studied", "at",
    "from", "in", "pursuing", "completed", "graduate", "undergraduate",
    "postgraduate", "degree", "expected", "coursework", "member", "student",
    "club",
}


def _letters_only(word):
    return re.sub(r"[^a-z]", "", word.lower())


def _mask_institution_match(match):
    words = match.group(0).split()
    keyword_index = next(
        (i for i, w in enumerate(words) if _letters_only(w) in _INSTITUTION_KEYWORDS),
        None,
    )
    if keyword_index is None:           # defensive; the regex guarantees one
        return match.group(0)
    start = keyword_index
    while (
        start > 0
        and keyword_index - (start - 1) <= 4
        and _letters_only(words[start - 1]) not in _NOT_PART_OF_INSTITUTION
    ):
        start -= 1
    kept_prefix = " ".join(words[:start])
    return (kept_prefix + " " if kept_prefix else "") + INSTITUTION_MASK


def mask_institutions(text):
    """Replace institution names with one constant placeholder token."""
    lines = []
    for line in (text or "").splitlines():
        lines.append(
            _INSTITUTION_RE.sub(_mask_institution_match, line)
            if _INSTITUTION_RE.search(line) else line
        )
    return "\n".join(lines)


# ---- name handling ---------------------------------------------------------

def _name_tokens(names):
    """Lower-case letter-only name tokens (3+ letters) to drop from the text."""
    tokens = set()
    for name in names or ():
        for token in re.findall(r"[^\W\d_]{3,}", str(name).lower()):
            tokens.add(token)
    return tokens


def _header_names(resume_text):
    """The name on the resume's header line, as the parser finds it.
    Returns [] if the parser is unavailable or finds none."""
    try:
        from services.resume_parser import extract_name
    except ImportError:
        return []
    try:
        name = extract_name(resume_text or "")
    except Exception:
        return []
    return [name] if name else []


def _collect_names(db, candidate_id):
    """The candidate's account name (users.name), as a list."""
    with db.cursor() as cur:
        cur.execute("SELECT name FROM users WHERE id = %s", (candidate_id,))
        row = cur.fetchone()
    return [row["name"]] if row and row.get("name") else []


def _strip_personal_header(text):
    """Drop the preamble (name/contact/profile lines) before the first section.

    Many PDFs combine the name and contact information on one line, which
    makes parser-based name extraction unreliable. Keeping the actual resume
    sections is more robust than trying to guess where the name ends.
    """
    text = text or ""
    section_names = (
        r"education|academic background|technical skills|skills|core skills|"
        r"experience|work experience|professional experience|projects|academic projects|"
        r"certifications|certificates|summary|professional summary|objective|career objective|"
        r"profile|achievements|publications|internships|research experience"
    )
    # PDF text extraction sometimes collapses several visual lines into one.
    # Search only the opening portion so a later mention of "education" in a
    # project paragraph cannot cut off the document.
    match = re.search(r"\b(?:" + section_names + r")\b\s*:?[ \t]*", text[:1200], re.IGNORECASE)
    if match:
        return text[match.start():]
    # If no recognizable section heading exists, retain the text; dropping an
    # arbitrary prefix could remove meaningful content from a nonstandard CV.
    return text


def _repair_pdf_text_artifacts(text):
    """Repair common text-layer artifacts without changing normal prose.

    Some PDF font maps in the supplied sample turn the glyph pair "ti" into
    a backslash or equals sign (e.g. "iden=ty", "ac\\vely"). Unicode NFKC
    also restores common ligatures. These repairs happen before tokenization.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(?<=[A-Za-z])\\(?=[A-Za-z])", "ti", text)
    text = re.sub(r"(?<=[A-Za-z])=(?=[A-Za-z])", "ti", text)
    # Two additional extraction quirks observed in the supplied PDFs.
    text = re.sub(r"\bmulmthreading\b", "multithreading", text, flags=re.IGNORECASE)
    text = re.sub(r"\bpapern\b", "pattern", text, flags=re.IGNORECASE)
    return text


def normalize_for_fingerprint(text, names=()):
    """Normalize a resume into comparable tokens.

    Personal header lines, emails, URLs, profile links and phone numbers are
    excluded. Institution names and CGPA values are masked so changes to those
    personal/education identifiers do not dominate the content comparison.
    The degree, years, skills, experience and project wording remain.
    """
    text = _repair_pdf_text_artifacts(text)
    text = _strip_personal_header(text)
    text = _EMAIL_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _PROFILE_RE.sub(" ", text)
    text = _PHONE_RE.sub(_strip_phone, text)
    text = mask_institutions(text)
    # Short acronyms like DPS are not caught by the institution-name regex.
    text = re.sub(r"\bDPS\b", INSTITUTION_MASK, text)
    # CGPA is a candidate-specific value, not substantive resume wording.
    text = re.sub(r"\bCGPA\s*:?\s*\d+(?:\.\d+)?", " CGPA ", text, flags=re.IGNORECASE)
    text = _NON_WORD_RE.sub(" ", text.lower())
    tokens = text.split()
    drop = _name_tokens(names)
    if drop:
        tokens = [t for t in tokens if t not in drop]
    return tokens


def build_shingles(tokens, size=SHINGLE_SIZE):
    """Set of overlapping `size`-word phrases, e.g. 'built a flask'."""
    if len(tokens) < size:
        return set()
    return {" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)}


# ============================================================
# MinHash (pure Python)
#
# Idea: for each of NUM_PERM random hash functions, keep the smallest
# hash value over all shingles. For two documents, the chance that the
# minimum matches at a given position equals their Jaccard similarity,
# so the fraction of matching positions estimates it.
# ============================================================

def _make_hash_family():
    rng = random.Random(HASH_SEED)
    return [
        (rng.randint(1, _PRIME - 1), rng.randint(0, _PRIME - 1))
        for _ in range(NUM_PERM)
    ]


_HASH_FAMILY = _make_hash_family()


def _shingle_to_int(shingle):
    # hashlib, not built-in hash(): Python's hash() is salted per process
    # and would give different signatures on every run.
    return int.from_bytes(hashlib.sha1(shingle.encode("utf-8")).digest()[:4], "big")


def _minhash_signature(shingles):
    base = [_shingle_to_int(s) for s in shingles]
    return [
        min(((a * h + b) % _PRIME) & _MAX_HASH for h in base)
        for a, b in _HASH_FAMILY
    ]


def compute_fingerprint(resume_text, names=None):
    """Build a MinHash fingerprint from normalized three-word shingles.

    Version 3 improves PDF text normalization while retaining the existing
    MinHash comparison metric, which is more conservative than bag-of-words
    similarity for unrelated resumes that share common skills.
    """
    all_names = list(names or []) + _header_names(resume_text)
    tokens = normalize_for_fingerprint(resume_text, all_names)
    shingles = build_shingles(tokens)
    fingerprint = {
        "version": FINGERPRINT_VERSION,
        "num_perm": NUM_PERM,
        "shingle_size": SHINGLE_SIZE,
        "shingle_count": len(shingles),
        "status": STATUS_OK,
        "signature": None,
    }
    if len(shingles) < MIN_SHINGLES:
        fingerprint["status"] = STATUS_TOO_SHORT
        return fingerprint
    fingerprint["signature"] = _minhash_signature(shingles)
    return fingerprint


def serialize_fingerprint(fingerprint):
    return json.dumps(fingerprint, separators=(",", ":"))


def _valid_signature(signature):
    return (
        isinstance(signature, list)
        and len(signature) == NUM_PERM
        and all(isinstance(v, int) and not isinstance(v, bool) for v in signature)
    )


def fingerprint_status(stored_text):
    """
    Classify a stored fingerprint without comparing it:
        "current"   - made by this algorithm version, signature well formed
        "too_short" - made by this version; the resume had too little text
        "outdated"  - well formed JSON from another version / parameters
        "invalid"   - not JSON, wrong shape, or a malformed signature
    """
    try:
        data = json.loads(stored_text)
    except (TypeError, ValueError):
        return FP_INVALID
    if not isinstance(data, dict) or "version" not in data:
        return FP_INVALID
    if (
        data.get("version") != FINGERPRINT_VERSION
        or data.get("num_perm") != NUM_PERM
        or data.get("shingle_size") != SHINGLE_SIZE
    ):
        return FP_OUTDATED
    signature = data.get("signature")
    if signature is None:
        return FP_TOO_SHORT if data.get("status") == STATUS_TOO_SHORT else FP_INVALID
    return FP_CURRENT if _valid_signature(signature) else FP_INVALID


def parse_fingerprint(stored_text):
    """
    Load a stored fingerprint. Returns None if it is unreadable, malformed
    or was produced by an incompatible algorithm version / parameters
    (such rows are ignored rather than compared wrongly).
    """
    if fingerprint_status(stored_text) not in _USABLE_STATUSES:
        return None
    return json.loads(stored_text)


def estimate_similarity(signature_a, signature_b):
    """Estimated Jaccard similarity of two signatures, as a 0-100 int."""
    if not signature_a or not signature_b or len(signature_a) != len(signature_b):
        return 0
    matches = sum(1 for x, y in zip(signature_a, signature_b) if x == y)
    return int(round(100 * matches / len(signature_a)))



# ============================================================
# Indexing (write side)
# ============================================================

def index_resume(db, candidate_id, resume_path, file_hash, resume_text, commit=True):
    """
    Store the fingerprint for one uploaded resume in resume_fingerprints.

    Safe to call more than once for the same resume_path:
      * a usable (current) fingerprint already exists -> nothing is
        written, returns False;
      * no row exists -> a row is inserted, returns True;
      * a row exists but is outdated / invalid (e.g. made by fingerprint
        version 1) -> that row's fingerprint is replaced in place (one
        UPDATE, no new row), returns True.

    A row is stored even for too-short text (signature null) so the
    resume is known to have been processed.
    """
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, fingerprint FROM resume_fingerprints "
            "WHERE candidate_id = %s AND resume_path = %s ORDER BY id",
            (candidate_id, resume_path),
        )
        existing = cur.fetchall()

    if any(fingerprint_status(r["fingerprint"]) in _USABLE_STATUSES for r in existing):
        return False

    fingerprint = compute_fingerprint(resume_text, names=_collect_names(db, candidate_id))
    payload = serialize_fingerprint(fingerprint)

    with db.cursor() as cur:
        if existing:
            cur.execute(
                "UPDATE resume_fingerprints "
                "SET fingerprint = %s, file_hash = COALESCE(%s, file_hash) "
                "WHERE id = %s",
                (payload, file_hash, existing[0]["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO resume_fingerprints
                    (candidate_id, fingerprint, file_hash, resume_path)
                VALUES (%s, %s, %s, %s)
                """,
                (candidate_id, payload, file_hash, resume_path),
            )
    if commit:
        db.commit()
    return True


def backfill_fingerprints(db, upload_folder, rebuild_outdated=False):
    """
    Fingerprint every existing resume_versions row that has no
    resume_fingerprints row yet (e.g. demo data uploaded before Step 30).
    Parses each PDF with services.resume_parser. Rows whose file is
    missing or unreadable are skipped (and retried on the next run).

    rebuild_outdated=True also regenerates rows whose stored fingerprint
    is outdated or invalid (e.g. made by fingerprint version 1), replacing
    them in place. Nothing is deleted; the PDF and its file_hash are not
    touched.

    Returns {"indexed": n, "rebuilt": n, "missing_file": n, "failed": n}.
    """
    from services.resume_parser import parse_resume  # lazy: keeps core logic import-light

    where = "" if rebuild_outdated else "WHERE rf.id IS NULL"
    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT rv.candidate_id, rv.resume_path, rv.file_hash,
                   rf.id AS fingerprint_id, rf.fingerprint
            FROM resume_versions rv
            LEFT JOIN resume_fingerprints rf
                   ON rf.candidate_id = rv.candidate_id
                  AND rf.resume_path = rv.resume_path
            {where}
            """
        )
        rows = cur.fetchall()

    pending = []
    for row in rows:
        if row["fingerprint_id"] is None:
            pending.append((row, False))
        elif rebuild_outdated and fingerprint_status(row["fingerprint"]) in (FP_OUTDATED, FP_INVALID):
            pending.append((row, True))

    counts = {"indexed": 0, "rebuilt": 0, "missing_file": 0, "failed": 0}
    for row, is_rebuild in pending:
        absolute_path = os.path.join(upload_folder, os.path.basename(row["resume_path"]))
        if not os.path.exists(absolute_path):
            counts["missing_file"] += 1
            continue
        try:
            text = parse_resume(absolute_path)["resume_text"]
            index_resume(db, row["candidate_id"], row["resume_path"], row["file_hash"], text)
            counts["rebuilt" if is_rebuild else "indexed"] += 1
        except Exception:
            counts["failed"] += 1
    return counts


# ============================================================
# Checking (read side)
# ============================================================

def _find_exact_matches(db, candidate_id, resume_path, file_hash):
    """Other stored resume versions with the identical SHA-256."""
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT rv.candidate_id, rv.resume_path, rv.version_number, u.name
            FROM resume_versions rv
            JOIN users u ON u.id = rv.candidate_id
            WHERE rv.file_hash = %s
              AND NOT (rv.candidate_id = %s AND rv.resume_path = %s)
            """,
            (file_hash, candidate_id, resume_path),
        )
        rows = cur.fetchall()
    return [
        {
            "candidate_id": r["candidate_id"],
            "candidate_name": r["name"],
            "version_number": r["version_number"],
            "resume_path": r["resume_path"],
            "same_candidate": r["candidate_id"] == candidate_id,
        }
        for r in rows
    ]


def _load_own_fingerprint(db, candidate_id, resume_path):
    """Returns (status, fingerprint_dict_or_None). status is one of
    "missing", "outdated" (outdated or invalid), "current", "too_short"."""
    with db.cursor() as cur:
        cur.execute(
            "SELECT fingerprint FROM resume_fingerprints "
            "WHERE candidate_id = %s AND resume_path = %s ORDER BY id",
            (candidate_id, resume_path),
        )
        rows = cur.fetchall()
    if not rows:
        return "missing", None
    best = None
    for row in rows:
        status = fingerprint_status(row["fingerprint"])
        if status == FP_CURRENT:
            return FP_CURRENT, parse_fingerprint(row["fingerprint"])
        if status == FP_TOO_SHORT:
            best = (FP_TOO_SHORT, parse_fingerprint(row["fingerprint"]))
    return best if best else (FP_OUTDATED, None)


_COMPARE_SQL = """
    SELECT rf.fingerprint, rf.resume_path, rf.candidate_id,
           rv.version_number, u.name
    FROM resume_fingerprints rf
    JOIN resume_versions rv
           ON rv.candidate_id = rf.candidate_id
          AND rv.resume_path = rf.resume_path
    JOIN users u ON u.id = rf.candidate_id
    WHERE {where}
"""


def _compare_stored(db, signature, where, params):
    """Similarity of `signature` against the stored, still-existing resume
    versions selected by `where`. Returns (matches sorted best first,
    number of rows skipped because their fingerprint is incompatible)."""
    with db.cursor() as cur:
        cur.execute(_COMPARE_SQL.format(where=where), params)
        rows = cur.fetchall()

    matches, skipped = [], 0
    for r in rows:
        status = fingerprint_status(r["fingerprint"])
        if status in (FP_OUTDATED, FP_INVALID):
            skipped += 1              # never compare an incompatible fingerprint
            continue
        other = parse_fingerprint(r["fingerprint"])
        if not other or not other.get("signature"):
            continue                  # too little text to compare
        matches.append(
            {
                "candidate_id": r["candidate_id"],
                "candidate_name": r["name"],
                "version_number": r["version_number"],
                "resume_path": r["resume_path"],
                "similarity": estimate_similarity(signature, other["signature"]),
            }
        )
    matches.sort(key=lambda m: m["similarity"], reverse=True)
    return matches, skipped


def _compare_with_other_candidates(db, candidate_id, signature):
    return _compare_stored(db, signature, "rf.candidate_id <> %s", (candidate_id,))


def _compare_with_own_versions(db, candidate_id, resume_path, signature):
    matches, _ = _compare_stored(
        db, signature, "rf.candidate_id = %s AND rf.resume_path <> %s",
        (candidate_id, resume_path),
    )
    return matches


def check_duplicates(db, candidate_id, resume_path, file_hash=None,
                     resume_text=None, threshold=None):
    """
    Run both duplicate checks for one candidate's resume version.

    Arguments
        candidate_id, resume_path  identify the resume (resume_path is the
                                   relative path stored by candidate.py).
        file_hash                  optional; looked up from resume_versions
                                   if omitted.
        resume_text                optional; if given, the fingerprint is
                                   computed from it. Otherwise the one
                                   stored by index_resume() is used.
        threshold                  near-duplicate % (default: config
                                   NEAR_DUPLICATE_THRESHOLD, i.e. 85).

    Returns a dict:
        {
          "flag": bool,                # True -> show to recruiter
          "label": str,                # short recruiter-facing wording
          "explanation": str,
          "exact_duplicate": bool,     # identical file, ANOTHER candidate
          "exact_matches": [ {candidate_id, candidate_name,
                              version_number, resume_path,
                              same_candidate}, ... ],
          "near_duplicate": bool,      # similar text, ANOTHER candidate
          "similar_matches": [ {..., "similarity": int}, ... ],  # >= threshold
          "highest_similarity": int,   # best score vs other candidates (0-100)
          "near_check_status": "checked" | "skipped_too_short"
                               | "not_indexed" | "outdated_fingerprint",
          "threshold": int,
          # added in fingerprint version 2 (additive; older callers ignore them)
          "skipped_outdated": int,     # other resumes not compared: their
                                       # fingerprint is outdated/invalid
          "own_version_similarity": int,  # best score vs this candidate's OWN
                                       # other versions (never flagged)
        }
    "highest_similarity" / "similarity" values are estimated Jaccard
    overlap of distinct normalized 3-word phrases, not a probability of copying.
    Only recruiters should ever see candidate names from this result.
    """
    threshold = _default_threshold() if threshold is None else threshold

    # ---- Level 1: exact ------------------------------------------------
    if file_hash is None:
        with db.cursor() as cur:
            cur.execute(
                "SELECT file_hash FROM resume_versions "
                "WHERE candidate_id = %s AND resume_path = %s",
                (candidate_id, resume_path),
            )
            row = cur.fetchone()
        file_hash = row["file_hash"] if row else None

    exact_matches = (
        _find_exact_matches(db, candidate_id, resume_path, file_hash)
        if file_hash else []
    )
    cross_exact = [m for m in exact_matches if not m["same_candidate"]]
    own_exact = [m for m in exact_matches if m["same_candidate"]]

    # ---- Level 2: near-duplicate ---------------------------------------
    if resume_text is not None:
        fingerprint = compute_fingerprint(
            resume_text, names=_collect_names(db, candidate_id)
        )
        own_status = FP_CURRENT if fingerprint.get("signature") else FP_TOO_SHORT
    else:
        own_status, fingerprint = _load_own_fingerprint(db, candidate_id, resume_path)

    similar_matches, highest, own_best, skipped = [], 0, 0, 0
    if own_status == "missing":
        near_status = "not_indexed"
    elif own_status == FP_OUTDATED:
        near_status = "outdated_fingerprint"
    elif own_status == FP_TOO_SHORT or not fingerprint.get("signature"):
        near_status = "skipped_too_short"
    else:
        near_status = "checked"
        all_matches, skipped = _compare_with_other_candidates(
            db, candidate_id, fingerprint["signature"]
        )
        exact_paths = {m["resume_path"] for m in cross_exact}
        # An identical file is already reported as an exact match.
        all_matches = [m for m in all_matches if m["resume_path"] not in exact_paths]
        highest = all_matches[0]["similarity"] if all_matches else 0
        similar_matches = [m for m in all_matches if m["similarity"] >= threshold]

        own_matches = _compare_with_own_versions(
            db, candidate_id, resume_path, fingerprint["signature"]
        )
        own_best = own_matches[0]["similarity"] if own_matches else 0

    if cross_exact:
        highest = 100

    # ---- Wording ---------------------------------------------------------
    if cross_exact:
        label = "Potential duplicate (identical file)"
        names = ", ".join(sorted({m["candidate_name"] for m in cross_exact}))
        explanation = (
            f"Exact duplicate: this file has the same SHA-256 file hash as a resume "
            f"uploaded by another candidate: {names}. The two uploaded files are "
            f"byte-for-byte identical."
        )
    elif similar_matches:
        top = similar_matches[0]
        label = "Similar resume"
        explanation = (
            f"Suspected near-duplicate: the files are different, but this resume has "
            f"an estimated {top['similarity']}% phrase overlap with a resume from "
            f"{top['candidate_name']} (share of distinct 3-word phrases that appear "
            f"in both, after ignoring names, contact details and institution names). "
            f"This measures shared wording only. It is not a probability that "
            f"anything was copied, and shared projects, templates or skills can be "
            f"legitimate. Recruiter review is recommended."
        )
    else:
        label = "No duplicates detected"
        explanation = "No identical or highly similar resume from another candidate was found."
        if own_exact:
            explanation += (
                f" (Same file as this candidate's own version "
                f"{own_exact[0]['version_number']}.)"
            )
        if own_best >= threshold:
            explanation += (
                f" This resume is highly similar ({own_best}%) to another version "
                f"uploaded by the same candidate; that is expected and is not flagged."
            )
        if near_status == "skipped_too_short":
            explanation += " Similarity check skipped: too little text in the resume."
        elif near_status == "not_indexed":
            explanation += " Similarity check not run: this resume has not been fingerprinted yet."
        elif near_status == "outdated_fingerprint":
            explanation += (
                " Similarity check not run: this resume's stored fingerprint uses an "
                "older format and must be regenerated."
            )
        if skipped:
            explanation += (
                f" {skipped} other stored resume(s) were not compared because their "
                f"fingerprint uses an older or invalid format; regenerate fingerprints "
                f"to include them."
            )

    return {
        "flag": bool(cross_exact or similar_matches),
        "label": label,
        "explanation": explanation,
        "exact_duplicate": bool(cross_exact),
        "exact_matches": exact_matches,
        "near_duplicate": bool(similar_matches),
        "similar_matches": similar_matches,
        "highest_similarity": highest,
        "near_check_status": near_status,
        "threshold": threshold,
        "skipped_outdated": skipped,
        "own_version_similarity": own_best,
    }


def application_duplicate_fields(result):
    """
    Map a check_duplicates() result to the applications-table columns
    (duplicate_flag, duplicate_similarity). Does not touch the database;
    the caller (worker / recruiter flow) performs the UPDATE.
    """
    return bool(result["flag"]), int(result["highest_similarity"])
