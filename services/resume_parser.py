"""
Recroot — Resume Parser

Extracts the structured fields listed in Section 5 of the blueprint
from an already-uploaded PDF resume:
    * Name
    * Email
    * Phone
    * Education
    * Skills
    * Experience
    * Projects
    * Certifications
    * Resume text (full raw text)

Scope — this module does NOT touch the database or the upload flow.
Uploading is modules/candidate.py's job (already built, Step 23):
candidates upload a PDF there, it's saved under UPLOAD_FOLDER, and a
row goes into resume_versions. This module is a pure function over a
PDF file — give it a path (or bytes), get back a dict — matching the
pipeline in Section 13:

    Upload Resume (candidate.py)
            |
    File Management (candidate.py)
            |
    Resume Parser (THIS MODULE)
            |
    Structured Resume Data
            |
    Duplicate Detection / Consistency Analysis / Scoring Engine
    (modules/duplicate_detector.py, consistency_checker.py,
     scoring_engine.py — Steps 30-32, not built yet)

There is no dedicated table in schema.sql for parsed fields (no
"education", "phone", etc. columns anywhere) — the structured data
this module returns is meant to be consumed in-memory by whatever
calls it (modules/worker.py, Step 33, will be the orchestrator that
calls parse_resume() and passes the result along the pipeline), not
persisted as-is.

Dependency: this module needs `pypdf` for text extraction (pure
text — no OCR, matching Section 1's "Python PDF text extraction").
Add `pypdf>=4.0` to requirements.txt if it isn't there already; the
import is deferred into extract_text_from_pdf() so importing this
module doesn't fail before that dependency is installed.

Extraction approach and its limits: with no NLP/ML library in the
tech stack (Section 1), section detection and skill extraction here
are regex/keyword heuristics, not language understanding. They work
well on resumes with conventional, clearly-labeled sections (most
templates) and are noticeably weaker on heavily stylized or
non-standard layouts, and on scanned/image-only PDFs (no OCR is
performed — see `is_low_text` in the result). That's an accepted
trade-off for a college-project-scale system; consistency_checker.py
and scoring_engine.py should treat every field as best-effort, not
ground truth, consistent with Section 7's "Important limitation".
"""

import re
import unicodedata


class ResumeParseError(Exception):
    """Raised when the PDF itself can't be opened/read at all
    (missing file, corrupt file, wrong file type) — as opposed to a
    valid PDF that just doesn't have much extractable text, which is
    reported via the `is_low_text` flag in the result instead."""


# ============================================================
# Section header aliases
#
# Detection is deliberately conservative: a line only counts as a
# section header if, once trimmed and stripped of a trailing colon,
# it matches one of these aliases exactly (case-insensitive). This
# avoids false positives like "...gained strong skills in Python..."
# inside a paragraph, at the cost of missing headers phrased outside
# this list — add aliases here as real-world resumes surface them.
# ============================================================

SECTION_ALIASES = {
    "education": [
        "education", "academics", "academic background",
        "academic qualifications", "educational qualifications",
        "academic details", "education details", "educational details",
    ],
    "experience": [
        "experience", "work experience", "professional experience",
        "employment history", "work history",
        "internships", "internship experience",
    ],
    "skills": [
        "skills", "technical skills", "core competencies",
        "key skills", "skills & tools", "skills and tools",
    ],
    "projects": [
        "projects", "academic projects", "personal projects",
        "key projects",
        "major projects", "major projects and seminar",
    ],
    "certifications": [
        "certifications", "certificates", "licenses & certifications",
        "certifications & courses", "courses & certifications",
    ],
    # Recognised so that the section BEFORE it (e.g. Skills) stops here.
    # parse_resume() does not return this section — the returned
    # fields are unchanged — but it must be known so it acts as a
    # boundary.
    "achievements": [
        "achievements", "awards", "honours and awards", "honors and awards",
        "awards and achievements", "algorithmic competitions/achievements",
        "competitions and achievements",
    ],
    # "Stop" headers: sections Recroot does not extract. They exist only
    # so they terminate the section above them instead of being swallowed
    # by it (e.g. Strengths/Hobbies ending up inside Skills or Projects).
    "other": [
        "strengths", "fields of interest", "areas of interest",
        "interests", "interest and hobbies", "interests and hobbies",
        "hobbies", "declaration", "references", "personal details",
        "personal information", "summary", "professional summary",
        "objective", "career objective", "profile",
        "languages known", "extracurricular activities",
        "positions of responsibility",
    ],
}

_ALL_HEADER_LINES = {
    alias: section
    for section, aliases in SECTION_ALIASES.items()
    for alias in aliases
}

# A reasonable default skill vocabulary used as a fallback when a
# resume has no clearly-labeled Skills section (or the section is
# too sparse to be useful on its own). Scoring against a specific
# job's required_skills is scoring_engine.py's job (Step 32) — this
# list exists only so extract_skills() returns something useful on
# its own. Callers needing to match resume text against an arbitrary
# skill list (e.g. a job's required_skills) should use
# find_keywords_in_text() directly instead of relying on this list.
DEFAULT_SKILL_KEYWORDS = [
    "python", "java", "javascript", "typescript", "c++", "c#", "php", "go", "ruby",
    "html", "css", "sql", "nosql", "mysql", "postgresql", "mongodb", "sqlite",
    "flask", "django", "fastapi", "spring", "react", "angular", "vue", "node.js",
    "rest api", "graphql", "git", "docker", "kubernetes", "aws", "azure", "gcp",
    "linux", "bash", "ci/cd", "jenkins", "agile", "scrum", "machine learning",
    "data structures", "algorithms", "pandas", "numpy", "tensorflow", "pytorch",
]


# ============================================================
# PDF text extraction
# ============================================================

def extract_text_from_pdf(source):
    """
    Return the full extracted text of a PDF as a single string, with
    pages joined by a form-feed-style blank line.

    `source` may be:
        * a filesystem path (str or os.PathLike), or
        * raw PDF bytes.

    Raises ResumeParseError if the file can't be opened/read as a
    PDF at all. Returns "" (not an error) for a PDF that opens fine
    but yields no extractable text (e.g. a scanned image with no
    text layer) — see is_low_text() for how callers should treat that.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ResumeParseError(
            "pypdf is not installed. Add 'pypdf>=4.0' to requirements.txt "
            "and install it (pip install pypdf)."
        ) from exc

    import io

    try:
        if isinstance(source, (bytes, bytearray)):
            reader = PdfReader(io.BytesIO(source))
        else:
            reader = PdfReader(str(source))
    except Exception as exc:  # pypdf raises several distinct exception types
        raise ResumeParseError(f"Could not open '{source}' as a PDF: {exc}") from exc

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")  # try an empty password before giving up
        except Exception:
            pass

    pages_text = []
    for page in reader.pages:
        try:
            pages_text.append(page.extract_text() or "")
        except Exception:
            # A single malformed page shouldn't fail the whole resume.
            pages_text.append("")

    return _normalize_text("\n\n".join(pages_text)).strip()


def _normalize_text(text):
    """
    Clean up common PDF-extraction artifacts so later steps
    (keyword matching, MinHash similarity) see consistent text:
        * Unicode NFKC — folds ligatures such as 'ﬁ' -> 'fi'.
        * Rejoin words hyphenated across a line break
          ('sce-\\nnarios' -> 'scenarios'). Only joins when a letter
          precedes the hyphen and a lowercase letter starts the next
          line, so 'Jan - Aug' ranges and '2021-\\n2025' are left alone.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"([A-Za-z])-[ \t]*\n[ \t]*([a-z])", r"\1\2", text)
    return text


def is_low_text(resume_text, min_chars=50):
    """
    Heuristic: True if the extracted text is too sparse to have come
    from a normal text-based PDF resume — most likely a scanned
    image with no OCR performed, or a near-empty file. Callers
    (modules/worker.py) should surface this to the recruiter rather
    than silently scoring an empty resume.
    """
    return len((resume_text or "").strip()) < min_chars


# ============================================================
# Field extractors
# ============================================================

EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9\-.]+")

# Matches digit groups separated by spaces/dashes/dots/parens, long
# enough (10-13 digits total) to plausibly be a phone number.
_PHONE_CANDIDATE_RE = re.compile(r"[\+\(]?[0-9][0-9().\-\s]{7,}[0-9]")


def extract_email(text):
    match = EMAIL_RE.search(text)
    return match.group(0) if match else None


def extract_phone(text):
    for candidate in _PHONE_CANDIDATE_RE.findall(text):
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 13:
            return candidate.strip()
    return None


def extract_name(text, max_lines_checked=6):
    """
    Heuristic: resumes conventionally lead with the candidate's name
    as the very first line of real content. Skip blank lines, lines
    that are clearly a section header, and lines that look like
    contact info (contain '@' or several digits); take the first
    remaining short, letters-only-ish line.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    for line in lines[:max_lines_checked]:
        lowered = line.lower().rstrip(":")
        if lowered in _ALL_HEADER_LINES:
            continue
        if "@" in line:
            continue
        if sum(ch.isdigit() for ch in line) >= 3:
            continue

        words = line.split()
        if 1 <= len(words) <= 4 and all(
            re.match(r"^[A-Za-z.\-']+$", w) for w in words
        ):
            return line

    return None


def _find_section_blocks(text):
    """
    Return {canonical_section: block_text} for every recognized
    section header found in the resume, using the first occurrence
    of each content section. Header lines are matched exactly (after
    trimming, lower-casing, collapsing whitespace and dropping a
    trailing colon) against SECTION_ALIASES.

    EVERY recognised header — including "stop" headers (section
    "other") and ones whose content we don't return (section
    "achievements") — ends the block above it, so one section can't
    swallow the next.
    """
    lines = text.splitlines()

    headers = []  # (line_index, canonical_section), in document order
    for idx, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped or len(stripped) > 40:
            continue
        key = " ".join(stripped.lower().rstrip(":").split())
        section = _ALL_HEADER_LINES.get(key)
        if section:
            headers.append((idx, section))

    blocks = {}
    for i, (start_idx, section) in enumerate(headers):
        if section in blocks:
            continue  # first occurrence wins
        end_idx = headers[i + 1][0] if i + 1 < len(headers) else len(lines)
        blocks[section] = "\n".join(lines[start_idx + 1 : end_idx]).strip()

    return blocks


def find_keywords_in_text(text, keywords):
    """
    Case-insensitive, word-boundary keyword scan. Returns the subset
    of `keywords` (in their original casing) found anywhere in
    `text`. Shared helper — scoring_engine.py (Step 32) can reuse
    this to match resume text against a job's required_skills /
    preferred_skills without duplicating this logic.
    """
    if not text or not keywords:
        return []

    lowered_text = text.lower()
    found = []
    for keyword in keywords:
        pattern = r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])"
        if re.search(pattern, lowered_text):
            found.append(keyword)
    return found


# Words that, as the last word of a label, mark a "Category (a, b, c)"
# group, e.g. "Languages (C, C++, Java)" or "Developer Tools (Git)".
_SKILL_CATEGORY_WORDS = {
    "language", "languages", "database", "databases", "script", "scripts",
    "scripting", "tool", "tools", "framework", "frameworks", "library",
    "libraries", "platform", "platforms", "technology", "technologies",
    "cloud", "clouds", "backend", "frontend", "os", "systems",
}

# Skills that legitimately contain '/' and must not be split on it.
_SLASH_SKILLS = {"ci/cd", "tcp/ip", "pl/sql", "ui/ux", "i/o", "a/b testing"}

_CATEGORY_LABEL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 &/+.\-]{0,40}?)\s*:\s*(.+)$")
_CATEGORY_GROUP_RE = re.compile(r"\b([A-Za-z][A-Za-z &]*?)\s*\(([^()]*)\)")
_BULLET_CHARS = " \t\u2022\u25e6\u25aa\u25cf*\u2013-"


def _paren_depth(text):
    return text.count("(") - text.count(")")


def _split_outside_parens(text, separators=",;|"):
    """Split on any of `separators`, but not inside (...)."""
    parts, buf, depth = [], [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in separators and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _expand_category_groups(line):
    """'Languages (C, C++), Tools(Git)' -> ', C, C++, , Git, '."""
    def repl(match):
        label, inner = match.group(1), match.group(2)
        if label.split()[-1].lower() in _SKILL_CATEGORY_WORDS:
            return ", " + inner + ", "
        return match.group(0)
    return _CATEGORY_GROUP_RE.sub(repl, line)


def _logical_skill_lines(block):
    """
    Join physical lines into logical entries: a line that ends inside an
    open bracket or with a trailing comma continues on the next line
    (PDF line-wrapping), so '(... Gnuplot,\\nRstudio)' stays together.
    """
    entries, buf = [], ""
    for raw in block.splitlines():
        line = raw.strip(_BULLET_CHARS)
        if not line:
            continue
        if buf and (_paren_depth(buf) > 0 or buf.rstrip().endswith(",")):
            buf = buf + " " + line
        else:
            if buf:
                entries.append(buf)
            buf = line
    if buf:
        entries.append(buf)
    return entries


def _parse_skills_block(block):
    """
    Turn a Skills section into a clean list of skill names. Handles:
        * 'Languages: Python, Java'      (colon-labelled category lines)
        * 'Languages (C, Java), Tools(Git)'  (bracketed category groups)
        * plain comma / bullet / pipe separated lists
    """
    skills = []
    for entry in _logical_skill_lines(block):
        match = _CATEGORY_LABEL_RE.match(entry)
        if match:
            entry = match.group(2)
        entry = _expand_category_groups(entry)

        for token in _split_outside_parens(entry):
            token = token.strip(_BULLET_CHARS + ".")
            if not token:
                continue
            if "/" in token and token.lower() not in _SLASH_SKILLS:
                pieces = [p.strip(_BULLET_CHARS + ".") for p in token.split("/")]
            else:
                pieces = [token]
            for piece in pieces:
                if piece and len(piece) <= 40 and len(piece.split()) <= 5:
                    skills.append(piece)
    return skills


def extract_skills(text, sections=None, extra_keywords=None):
    """
    Return a de-duplicated list of skills. Prefers parsing an
    explicit Skills section (see _parse_skills_block); falls back to
    (and supplements with) a keyword scan of the full resume text
    against DEFAULT_SKILL_KEYWORDS plus any `extra_keywords`
    supplied by the caller.
    """
    sections = sections if sections is not None else _find_section_blocks(text)
    skills = []

    skills_block = sections.get("skills", "")
    if skills_block:
        skills.extend(_parse_skills_block(skills_block))

    keyword_pool = list(DEFAULT_SKILL_KEYWORDS)
    if extra_keywords:
        keyword_pool.extend(extra_keywords)
    keyword_hits = find_keywords_in_text(text, keyword_pool)

    deduped, seen_lower = [], set()
    for skill in skills:
        if skill.lower() not in seen_lower:
            deduped.append(skill)
            seen_lower.add(skill.lower())
    for hit in keyword_hits:
        if hit.lower() not in seen_lower:
            deduped.append(hit)
            seen_lower.add(hit.lower())

    return deduped


def extract_certifications(text, sections=None):
    """Return certification section content as a list of line items."""
    sections = sections if sections is not None else _find_section_blocks(text)
    block = sections.get("certifications", "")
    if not block:
        return []

    items = []
    for line in block.splitlines():
        cleaned = line.strip(" \t-*•.")
        if cleaned:
            items.append(cleaned)
    return items


def extract_education(text, sections=None):
    sections = sections if sections is not None else _find_section_blocks(text)
    return sections.get("education", "")


def extract_experience(text, sections=None):
    sections = sections if sections is not None else _find_section_blocks(text)
    return sections.get("experience", "")


def extract_projects(text, sections=None):
    sections = sections if sections is not None else _find_section_blocks(text)
    return sections.get("projects", "")


# ============================================================
# Top-level entry point
# ============================================================

def parse_resume(source):
    """
    Parse a PDF resume and return the structured fields from
    Section 5 of the blueprint as a dict:

        {
            "name": str | None,
            "email": str | None,
            "phone": str | None,
            "education": str,
            "skills": list[str],
            "experience": str,
            "projects": str,
            "certifications": list[str],
            "resume_text": str,
            "is_low_text": bool,
        }

    `source` is a filesystem path or raw PDF bytes (see
    extract_text_from_pdf). Raises ResumeParseError if the PDF
    itself can't be opened/read; does NOT raise for a readable PDF
    with little/no text — that's reported via `is_low_text` so
    callers (modules/worker.py) can flag it for recruiter review
    instead of crashing a background job.
    """
    resume_text = extract_text_from_pdf(source)
    sections = _find_section_blocks(resume_text)

    return {
        "name": extract_name(resume_text),
        "email": extract_email(resume_text),
        "phone": extract_phone(resume_text),
        "education": extract_education(resume_text, sections),
        "skills": extract_skills(resume_text, sections),
        "experience": extract_experience(resume_text, sections),
        "projects": extract_projects(resume_text, sections),
        "certifications": extract_certifications(resume_text, sections),
        "resume_text": resume_text,
        "is_low_text": is_low_text(resume_text),
    }