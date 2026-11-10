"""
Recroot — Consistency Checker
Step 31 of the Phase 2 build.

Implements Section 7 of the blueprint: instead of trying to decide whether
a candidate's information is "genuine", look for OBJECTIVE inconsistencies
in the data we already have:

    * Profile vs resume   - account name / email against what the resume says
    * Within the resume   - dates that conflict with each other

Wording: this module reports "Potential inconsistency ... recruiter
verification recommended". It never says or implies that a candidate lied
or submitted false information (Sections 7, 17 and 18). Every finding is an
observation for the recruiter, not a conclusion.

Checks
    Name vs account        notice  no overlap between the two names
                           info    partial overlap (surname change, nickname)
    Email vs account       info    resume email differs from account email
    Reversed date range    notice  "2025 - 2021"
    Future-dated job       notice  experience that starts/ends after today
                                   and is not marked Present/Current
    Experience vs          notice  a (non-internship) job overlaps an
    education overlap              education period by >= 12 months
    Overlapping jobs       info    two non-internship jobs overlap by >= 6
                                   months (often legitimate: consulting)

Only `notice` findings set the flag. `info` findings are shown to the
recruiter but never flag. A resume with too little text, or one that could
not be parsed at all, is never flagged: the result carries a neutral
"skipped" / "not checked" status instead (same approach as the duplicate
detector's "too_short").

Design notes (consistent with the rest of the project)
    * Pure Python, no Flask and no database access. The caller (worker.py,
      Step 33) passes in the account details and the dict returned by
      resume_parser.parse_resume(); nothing is read from or written to the
      DB here, and there is no schema change.
    * This module does NOT write to `applications`.
      application_consistency_fields() converts a result to the
      (consistency_flag, consistency_notes) pair for that table.
    * Date handling deliberately avoids false alarms. Resumes often give
      only years ("2019 - 2023"), so for overlap tests a year-only START is
      taken as the LATEST month it could be (December) and a year-only END
      as the EARLIEST (January). A job starting in 2023 straight after a
      degree ending in 2023 is therefore not reported; an overlap is only
      reported when it exists whatever the unstated months were.
    * Known limits: the parser merges "Internships" into "experience", so
      internships are recognised by keywords on the date line or the line
      above it (intern, trainee, part-time, freelance, ...). A layout with
      the title BELOW the dates is only understood for the first entry in
      the section, so a later internship in that layout can be treated as
      a regular job (the overlap message says it may be legitimate).
      Resumes whose date lines use an unusual layout may yield no ranges;
      then the timeline checks simply find nothing. Every field is
      best-effort, as resume_parser.py notes.

Typical use
    from modules.consistency_checker import (
        check_consistency, application_consistency_fields)

    result = check_consistency(
        {"name": user["name"], "email": user["email"]}, parsed)
    flag, notes = application_consistency_fields(result)
"""

import difflib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Optional


# ============================================================
# Tunables
# ============================================================

OVERLAP_MONTHS_THRESHOLD = 12      # job vs education overlap that raises a notice
JOB_OVERLAP_MONTHS_THRESHOLD = 6   # job vs job overlap that raises an info note
NAME_HEADER_LINES = 15             # lines at the top of the resume searched for the account name
TOKEN_TYPO_RATIO = 0.85            # similarity for two name words to count as the same word

SEV_NOTICE = "notice"   # shown prominently, sets the flag
SEV_INFO = "info"       # shown as a note, never sets the flag

STATUS_CHECKED = "checked"
STATUS_SKIPPED_LOW_TEXT = "skipped_low_text"
STATUS_NOT_CHECKED = "not_checked"

CATEGORY_NAME = "Name"
CATEGORY_EMAIL = "Email"
CATEGORY_TIMELINE = "Timeline"

__all__ = [
    "check_consistency",
    "application_consistency_fields",
    "extract_date_ranges",
    "DateRange",
    "OVERLAP_MONTHS_THRESHOLD",
    "JOB_OVERLAP_MONTHS_THRESHOLD",
]


# ============================================================
# Name comparison
# ============================================================

_NAME_TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt", "sri"}


def _name_tokens(name):
    """
    Reduce a name to comparable lower-case words: accents folded
    ('Rāhul' -> 'rahul'), punctuation and digits dropped ('R.' -> 'r',
    "O'Brien" -> 'obrien'), honorifics removed.
    """
    if not name:
        return []
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[\W\d_]+", " ", text)
    return [t for t in text.split() if t not in _NAME_TITLES]


def _tokens_match(a, b):
    """Same word, an initial of the other word, or a near-identical spelling."""
    if a == b:
        return True
    if len(a) == 1 or len(b) == 1:
        return a[0] == b[0]
    return difflib.SequenceMatcher(None, a, b).ratio() >= TOKEN_TYPO_RATIO


def _compare_names(name_a, name_b):
    """
    Returns "match", "partial", "mismatch" or "unknown".

    "match"    every word of the shorter name appears in the longer one
               (middle names, initials, swapped order and small spelling
               differences are all fine).
    "partial"  some, but not all, words match.
    "mismatch" no word matches.
    """
    tokens_a, tokens_b = _name_tokens(name_a), _name_tokens(name_b)
    if not tokens_a or not tokens_b:
        return "unknown"

    shorter, longer = sorted((tokens_a, tokens_b), key=len)
    used, matched = set(), 0
    for word in shorter:
        for idx, candidate in enumerate(longer):
            if idx not in used and _tokens_match(word, candidate):
                used.add(idx)
                matched += 1
                break

    if matched == len(shorter):
        return "match"
    return "partial" if matched else "mismatch"


def _name_in_header(account_tokens, resume_text):
    """
    Safety net for parser mistakes: extract_name() takes the first short
    letters-only line, which can be e.g. "Curriculum Vitae". If every word
    of the account name appears near the top of the resume, the name is
    present and no finding is raised.
    """
    lines = [ln for ln in (resume_text or "").splitlines() if ln.strip()]
    header_tokens = _name_tokens(" ".join(lines[:NAME_HEADER_LINES]))
    if not header_tokens:
        return False
    return all(
        any(_tokens_match(word, h) for h in header_tokens) for word in account_tokens
    )


def _check_name(profile, parsed, findings, skipped):
    account_name = (profile.get("name") or "").strip()
    account_tokens = _name_tokens(account_name)
    if not account_tokens:
        skipped.append("name")
        return

    if _name_in_header(account_tokens, parsed.get("resume_text")):
        return

    resume_name = (parsed.get("name") or "").strip()
    if not _name_tokens(resume_name):
        skipped.append("name")
        return

    outcome = _compare_names(account_name, resume_name)
    if outcome == "mismatch":
        findings.append(_finding(
            "name_mismatch", CATEGORY_NAME, SEV_NOTICE,
            f"The name on the resume ('{resume_name}') does not appear to match "
            f"the account name ('{account_name}')."))
    elif outcome == "partial":
        findings.append(_finding(
            "name_partial", CATEGORY_NAME, SEV_INFO,
            f"The name on the resume ('{resume_name}') only partly matches the "
            f"account name ('{account_name}'). This can be a surname change, "
            f"nickname or abbreviation."))


# ============================================================
# Email comparison
# ============================================================

def _check_email(profile, parsed, findings, skipped):
    account_email = (profile.get("email") or "").strip().lower()
    resume_email = (parsed.get("email") or "").strip().lower()
    if not account_email or not resume_email:
        skipped.append("email")
        return
    if account_email != resume_email:
        findings.append(_finding(
            "email_differs", CATEGORY_EMAIL, SEV_INFO,
            f"The email on the resume ({resume_email}) differs from the account "
            f"email ({account_email}). People often use more than one address."))


# ============================================================
# Date-range extraction
# ============================================================

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_YEAR = r"(?:19|20)\d{2}(?!\d)"
_MONTH_NAME = (
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?,?"
)
# 'Jan 2021', 'Sept. 2021', '06/2021', '2021'
_DATE_PAT = (
    rf"(?:{_MONTH_NAME}[ \t]*{_YEAR}"
    rf"|(?<!\d)\d{{1,2}}[ \t]*[/.][ \t]*{_YEAR}"
    rf"|(?<!\d){_YEAR})"
)
_PRESENT_PAT = (
    r"\b(?:present|current(?:ly)?|ongoing|now|today|continuing|"
    r"till[ \t]+date|to[ \t]+date|date)\b"
)
_SEP_PAT = r"[ \t]*(?:[-\u2010-\u2015\u2212]|\bto\b|\buntil\b|\btill\b)[ \t]*(?:\n[ \t]*)?"
# End of a range: a full date, "Present", or a 2-digit year ('2019-23').
_END_PAT = rf"(?:{_DATE_PAT}|{_PRESENT_PAT}|(?<!\d)\d{{2}}(?![\d%]))"

_RANGE_RE = re.compile(
    rf"(?P<start>{_DATE_PAT}){_SEP_PAT}(?P<end>{_END_PAT})", re.IGNORECASE
)
_PRESENT_RE = re.compile(_PRESENT_PAT, re.IGNORECASE)
_MAX_SHORT_YEAR_SPAN = 12   # '2021-10' is not a range ending in 2110


@dataclass(frozen=True)
class DateRange:
    """
    One 'start - end' period found in a resume section. A month of None
    means the resume gave only a year. `is_present` ranges end today.
    `line_no` is the zero-based line (within the text searched) it was on.
    """
    start_year: int
    start_month: Optional[int]
    end_year: Optional[int]
    end_month: Optional[int]
    is_present: bool
    text: str
    line_no: int

    def _end_idx(self, today, default_month):
        if self.is_present:
            return today.year * 12 + today.month - 1
        return self.end_year * 12 + (self.end_month or default_month) - 1

    def widest(self, today):
        """(first, last) month index the period could possibly cover."""
        start = self.start_year * 12 + (self.start_month or 1) - 1
        return start, self._end_idx(today, 12)

    def narrowest(self, today):
        """(first, last) month index the period surely covers, given the
        months the resume left unstated."""
        start = self.start_year * 12 + (self.start_month or 12) - 1
        return start, self._end_idx(today, 1)


def _parse_date_token(token):
    """'Jan 2021' / '06/2021' / '2021' -> (year, month or None)."""
    token = token.strip()
    year_match = re.search(r"(?:19|20)\d{2}", token)
    year = int(year_match.group())
    prefix = token[:year_match.start()].strip(" \t.,/")
    month = None
    if prefix:
        if prefix.isdigit():
            month = int(prefix) if 1 <= int(prefix) <= 12 else None
        else:
            month = _MONTHS.get(prefix[:3].lower())
    return year, month


def extract_date_ranges(text):
    """
    Find every 'start - end' period in `text` (an education or experience
    section). Public so Step 32 (scoring) can reuse it for experience years.

    Handles '2021 - 2026', 'Jan 2021 - Present', '06/2021 - 08/2022',
    'Sept. 2021 - Till date', '2021 to present' and the short form '2019-23'.
    Only years 1900-2099 are recognised, so CGPAs and percentages are not
    mistaken for dates.
    """
    ranges = []
    if not text:
        return ranges

    for match in _RANGE_RE.finditer(text):
        start_year, start_month = _parse_date_token(match.group("start"))
        end_token = match.group("end").strip()
        is_present, end_year, end_month = False, None, None

        if re.fullmatch(r"\d{2}", end_token):
            end_year = start_year // 100 * 100 + int(end_token)
            if end_year < start_year:
                end_year += 100
            if end_year - start_year > _MAX_SHORT_YEAR_SPAN:
                continue  # e.g. '2021 - 10 projects': not a date range
        elif _PRESENT_RE.fullmatch(end_token):
            is_present = True
        else:
            end_year, end_month = _parse_date_token(end_token)

        ranges.append(DateRange(
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            is_present=is_present,
            text=" ".join(match.group(0).split()),
            line_no=text.count("\n", 0, match.start()),
        ))
    return ranges


# ============================================================
# Timeline checks
# ============================================================

# Roles that legitimately run alongside a degree.
_INTERN_RE = re.compile(
    r"\b(?:intern(?:ship)?s?|trainee|apprentice(?:ship)?|part[\s-]?time|"
    r"freelanc\w*|volunteer\w*|co-?op|(?:research|teaching)\s+assistant)\b",
    re.IGNORECASE,
)


def _is_intern_like(lines, line_no):
    """
    Look for internship-style words in the entry that owns this date range:
    the date line itself plus the line ABOVE it (the usual "Title, Company /
    dates" layout, or a title on the same line as the dates).

    The line BELOW is only consulted when the dates are the very first line
    of the section (a "dates first, title underneath" layout). Otherwise the
    line below is normally the NEXT entry's title, and using it would let an
    "Intern" role hide a neighbouring full-time job.
    """
    start = max(0, line_no - 1)
    end = line_no + 2 if line_no == 0 else line_no + 1
    return bool(_INTERN_RE.search(" ".join(lines[start:end])))


def _overlap_months(range_a, range_b, today):
    start_a, end_a = range_a.narrowest(today)
    start_b, end_b = range_b.narrowest(today)
    return min(end_a, end_b) - max(start_a, start_b) + 1


def _check_timeline(parsed, today, findings):
    education_text = parsed.get("education") or ""
    experience_text = parsed.get("experience") or ""
    experience_lines = experience_text.splitlines()
    today_idx = today.year * 12 + today.month - 1

    education = extract_date_ranges(education_text)
    experience = extract_date_ranges(experience_text)

    # ---- Reversed ranges (education and experience) ------------------
    def not_reversed(section_label, ranges):
        valid = []
        for r in ranges:
            first, last = r.widest(today)
            if not r.is_present and first > last:
                findings.append(_finding(
                    "timeline_reversed", CATEGORY_TIMELINE, SEV_NOTICE,
                    f"{section_label} period '{r.text}' ends before it starts. "
                    f"Possible timeline inconsistency."))
            else:
                valid.append(r)
        return valid

    education = not_reversed("Education", education)
    experience = not_reversed("Experience", experience)

    # ---- Future-dated experience ---------------------------------------
    for r in experience:
        first_start, _ = r.widest(today)
        _, earliest_end = r.narrowest(today)
        if first_start > today_idx:
            findings.append(_finding(
                "timeline_future", CATEGORY_TIMELINE, SEV_NOTICE,
                f"Experience period '{r.text}' starts after today's date. "
                f"Possible timeline inconsistency."))
        elif not r.is_present and earliest_end > today_idx:
            findings.append(_finding(
                "timeline_future", CATEGORY_TIMELINE, SEV_NOTICE,
                f"Experience period '{r.text}' ends after today's date but is "
                f"not marked as current. Possible timeline inconsistency."))

    # Internship-style roles are excluded from the overlap checks below.
    regular_jobs = [
        r for r in experience if not _is_intern_like(experience_lines, r.line_no)
    ]

    # ---- Experience vs education overlap -------------------------------
    for job in regular_jobs:
        best = None
        for study in education:
            months = _overlap_months(job, study, today)
            if months >= OVERLAP_MONTHS_THRESHOLD and (best is None or months > best[0]):
                best = (months, study)
        if best:
            months, study = best
            findings.append(_finding(
                "timeline_overlap", CATEGORY_TIMELINE, SEV_NOTICE,
                f"Experience period '{job.text}' overlaps the education period "
                f"'{study.text}' by about {months} months. Possible timeline "
                f"inconsistency - may be legitimate (e.g. part-time work while "
                f"studying)."))

    # ---- Overlapping jobs (informational) ------------------------------
    for i in range(len(regular_jobs)):
        for j in range(i + 1, len(regular_jobs)):
            months = _overlap_months(regular_jobs[i], regular_jobs[j], today)
            if months >= JOB_OVERLAP_MONTHS_THRESHOLD:
                findings.append(_finding(
                    "job_overlap", CATEGORY_TIMELINE, SEV_INFO,
                    f"Experience periods '{regular_jobs[i].text}' and "
                    f"'{regular_jobs[j].text}' overlap by about {months} months. "
                    f"This is often legitimate (e.g. consulting)."))


# ============================================================
# Result assembly
# ============================================================

_SKIPPED_LABELS = {"name": "name", "email": "email"}


def _finding(code, category, severity, message):
    return {"code": code, "category": category, "severity": severity,
            "message": message}


def _empty_result(status, label, explanation):
    return {
        "flag": False,
        "status": status,
        "label": label,
        "explanation": explanation,
        "findings": [],
        "notice_count": 0,
        "info_count": 0,
        "skipped_checks": [],
    }


def check_consistency(profile, parsed, today=None):
    """
    Run all consistency checks for one resume against one account.

    Arguments
        profile  dict with the candidate's account details:
                     {"name": str, "email": str}
                 An optional "education" key is accepted and reserved for
                 a future profile-vs-resume education check (the users
                 table has no education field yet); it is ignored today.
        parsed   the dict returned by resume_parser.parse_resume(), or None
                 if the resume could not be parsed.
        today    datetime.date used for "Present" and future-date checks
                 (default: date.today(); injectable for tests).

    Returns a dict:
        {
          "flag": bool,               # True -> show to recruiter
          "status": "checked" | "skipped_low_text" | "not_checked",
          "label": str,               # short recruiter-facing wording
          "explanation": str,
          "findings": [ {code, category, severity: "notice"|"info",
                         message}, ... ],
          "notice_count": int,
          "info_count": int,
          "skipped_checks": [ "name" | "email", ... ],  # data was missing
        }
    Never raises for ordinary input and never flags a resume that could not
    be read.
    """
    today = today or date.today()
    profile = profile or {}

    if parsed is None:
        return _empty_result(
            STATUS_NOT_CHECKED,
            "Consistency check not run",
            "Consistency check not run: the resume could not be read.",
        )
    if parsed.get("is_low_text"):
        return _empty_result(
            STATUS_SKIPPED_LOW_TEXT,
            "Consistency check skipped",
            "Consistency check skipped: too little text could be extracted "
            "from the resume (it may be a scanned image).",
        )

    findings, skipped = [], []
    _check_name(profile, parsed, findings, skipped)
    _check_email(profile, parsed, findings, skipped)
    _check_timeline(parsed, today, findings)

    notice_count = sum(1 for f in findings if f["severity"] == SEV_NOTICE)
    info_count = len(findings) - notice_count

    if notice_count:
        label = "Potential inconsistency detected"
        plural = "y" if notice_count == 1 else "ies"
        explanation = (
            f"{notice_count} potential inconsistenc{plural} found. These are "
            f"observations from simple rule-based checks, not conclusions about "
            f"the candidate - recruiter verification is recommended."
        )
    else:
        label = "No inconsistencies detected"
        explanation = "No observable inconsistencies were found by the checks that ran."
        if info_count:
            explanation += (
                f" {info_count} informational note{'s' if info_count != 1 else ''} "
                f"available."
            )
        if skipped:
            names = ", ".join(_SKIPPED_LABELS[s] for s in skipped)
            explanation += f" Not compared (information missing): {names}."

    return {
        "flag": notice_count > 0,
        "status": STATUS_CHECKED,
        "label": label,
        "explanation": explanation,
        "findings": findings,
        "notice_count": notice_count,
        "info_count": info_count,
        "skipped_checks": skipped,
    }


def application_consistency_fields(result):
    """
    Map a check_consistency() result to the applications-table columns
    (consistency_flag, consistency_notes). Does not touch the database;
    the caller (worker / recruiter flow) performs the UPDATE.

        flagged           -> (True, one line per notice finding plus a
                             closing "verification recommended" line)
        skipped / failed  -> (False, the explanation, so the recruiter can
                             see the check did not run)
        nothing to report -> (False, None)
    """
    if result["flag"]:
        lines = [
            f"- {f['category']}: {f['message']}"
            for f in result["findings"]
            if f["severity"] == SEV_NOTICE
        ]
        lines.append("Potential inconsistency detected - recruiter verification recommended.")
        return True, "\n".join(lines)
    if result["status"] != STATUS_CHECKED:
        return False, result["explanation"]
    return False, None