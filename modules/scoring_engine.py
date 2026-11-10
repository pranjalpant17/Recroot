"""
Recroot — Scoring Engine
Step 32 of the build.

Implements the Job–Resume Match Score (Sections 9-12 of the blueprint):
a 100-point score made of six weighted criteria, returned together with
a per-criterion breakdown and the matched / missing skills so that the
ranking is explainable rather than a bare number.

    Required skills        40   (equal share per required skill)
    Experience             20   (total months of experience vs. required years)
    Education              15   (degree level + branch, tiered)
    Relevant projects      10   (job skills mentioned in the projects section)
    Certifications          5   (best single certification)
    Resume/role relevance  10   (job title/description terms found in resume)

Scope and design rules
----------------------
* PURE MODULE. No Flask, no database access, no file access. The entry
  point takes the dict produced by services.resume_parser.parse_resume()
  and a job row (dict) and returns a plain dict. This keeps it trivially
  testable and lets modules/worker.py (Step 33) call it from a thread.
  The only DB-touching function is save_score(), which takes an already
  open connection and leaves the commit to the caller.
* NO PENALTIES FOR DUPLICATE / CONSISTENCY FLAGS. Sections 6, 7 and 18 of
  the blueprint say similarity and inconsistency are information for the
  recruiter, never evidence of fraud. The engine therefore never looks at
  them: the score only measures how well the resume matches the job.
* BEST-EFFORT INPUT. The parser is heuristic (regex/keywords, no NLP), so
  every field here is treated as best-effort. When something can't be
  determined the `detail` text says so instead of guessing silently.
* CRITERIA THE JOB DOESN'T SPECIFY (no required skills, experience
  requirement 0, no education requirement) award full marks to everyone.
  This follows Section 9.2 ("meets or exceeds the requirement -> full
  marks") and does not change the ranking between candidates; the
  `detail` text reads "not specified" so the recruiter can see why.
* UNREADABLE RESUMES are not scored as 0. The result has scored=False and
  a reason, and save_score() writes nothing for it (so it also can't
  overwrite an earlier valid score with zeros).

Weights (Section 11)
--------------------
Defaults come from config.Config.DEFAULT_SCORE_WEIGHTS. A caller can pass
its own `weights` dict (e.g. a recruiter's per-job configuration): keys are
the six names in WEIGHT_KEYS, values are non-negative integers, a missing
key means 0, and the total must be exactly 100.

Rounding
--------
Each criterion is rounded half-up to an integer number of points and the
total is the SUM OF THE ROUNDED COMPONENTS, so the breakdown always adds up
to the total shown and fits the INT columns in `applications`. Because
weights are integers, no component can exceed its weight and the total can
never exceed 100.

Tunable constants
-----------------
The thresholds below (PROJECT_*, RELEVANCE_*, EDU_TIERS, ...) are plain
module constants so they can be calibrated against the demo data (Step 40)
without touching the logic.
"""

import math
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime

from config import Config
from services.resume_parser import DEFAULT_SKILL_KEYWORDS, find_keywords_in_text

__all__ = [
    "WEIGHT_KEYS",
    "validate_weights",
    "score_application",
    "to_application_columns",
    "save_score",
]

WEIGHT_KEYS = (
    "skills",
    "experience",
    "education",
    "projects",
    "certifications",
    "relevance",
)


# ============================================================
# Tunable constants
# ============================================================

# Projects (Section 9.4): fraction of the job's required skills that the
# projects section mentions -> tier.
PROJECT_HIGH_RATIO = 0.60        # >= this  -> highly relevant
PROJECT_MODERATE_RATIO = 0.30    # >= this  -> moderately relevant; > 0 -> weakly
PROJECT_TIERS = {"high": 1.0, "moderate": 0.6, "weak": 0.3, "none": 0.0}  # 10/6/3/0

# Certifications (Section 9.5): 5 / 3 / 0.
CERT_TIERS = {"high": 1.0, "partial": 0.6, "none": 0.0}

# Education (Section 9.3): 15 / 12 / 8 / 3 / 0 expressed as fractions of
# the weight so they still make sense when a recruiter changes the weight.
EDU_TIERS = {
    "exact": 1.0,                    # degree level + branch satisfied
    "related_branch": 12 / 15,       # required degree, related branch
    "related_qualification": 8 / 15,
    "weak": 3 / 15,
    "none": 0.0,
}

# Relevance (Section 9.6)
RELEVANCE_TITLE_SHARE = 0.3      # share of the relevance score from title terms
RELEVANCE_DESC_SHARE = 0.5       # ... from description terms
RELEVANCE_PREFERRED_SHARE = 0.2  # ... from preferred skills matched
RELEVANCE_FULL_AT = 0.60         # raw coverage that earns full marks
DESC_TERM_LIMIT = 25             # most frequent description terms considered


# ============================================================
# Weights
# ============================================================

def validate_weights(weights=None):
    """
    Return a complete, validated weights dict with exactly the six keys
    in WEIGHT_KEYS (missing keys become 0).

    Raises ValueError for unknown keys, non-integer / negative / boolean
    values, or a total that isn't exactly 100. `None` means the defaults
    from config.Config.DEFAULT_SCORE_WEIGHTS. The input is never mutated.
    """
    if weights is None:
        weights = Config.DEFAULT_SCORE_WEIGHTS
    if not isinstance(weights, Mapping):
        raise ValueError("Scoring weights must be a dict.")

    unknown = set(weights) - set(WEIGHT_KEYS)
    if unknown:
        raise ValueError(f"Unknown scoring criteria: {sorted(unknown)}")

    clean = {}
    for key in WEIGHT_KEYS:
        value = weights.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Weight for '{key}' must be a number.")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"Weight for '{key}' must be a finite number.")
        if value < 0:
            raise ValueError(f"Weight for '{key}' can't be negative.")
        if int(value) != value:
            raise ValueError(f"Weight for '{key}' must be a whole number.")
        clean[key] = int(value)

    total = sum(clean.values())
    if total != 100:
        raise ValueError(f"Scoring weights must sum to 100 (got {total}).")
    return clean


def _round_points(value):
    """Round half-up (not banker's rounding) to a whole number of points."""
    return int(math.floor(value + 0.5 + 1e-9))


def _component(weight, fraction, detail):
    """Build one breakdown entry: score = round(weight x fraction)."""
    fraction = max(0.0, min(1.0, fraction))
    score = min(weight, _round_points(weight * fraction))
    return {"score": score, "max": weight, "detail": detail}


# ============================================================
# Skill matching (Section 9.1)
# ============================================================

# Groups of interchangeable spellings. Any member of a group matches any
# other member. Everything is lower-case.
_ALIAS_GROUPS = [
    ["rest api", "rest apis", "restful api", "restful apis", "restful"],
    ["node.js", "nodejs", "node js"],
    ["react", "react.js", "reactjs"],
    ["vue", "vue.js", "vuejs"],
    ["angular", "angularjs", "angular.js"],
    ["express.js", "expressjs"],
    ["postgresql", "postgres"],
    ["mongodb", "mongo db", "mongo"],
    ["mysql", "my sql"],
    ["javascript", "java script"],
    ["html", "html5"],
    ["css", "css3"],
    ["c++", "cpp"],
    ["c#", "csharp", "c sharp"],
    ["go", "golang"],
    ["machine learning", "ml"],
    ["artificial intelligence", "ai"],
    ["ci/cd", "cicd", "ci cd", "continuous integration"],
    ["scikit-learn", "sklearn", "scikit learn"],
    ["aws", "amazon web services"],
    ["gcp", "google cloud", "google cloud platform"],
    ["kubernetes", "k8s"],
    ["object oriented programming", "object-oriented programming", "oop", "oops"],
    ["data structures", "dsa"],
]
_ALIAS_INDEX = {
    variant: tuple(group) for group in _ALIAS_GROUPS for variant in group
}

# Names that are also ordinary English words / single letters. Searching
# the free text for them gives false positives ("go to market", or "C"
# inside "C++"), so they only count when they appear as an entry in the
# resume's parsed Skills list.
_LIST_ONLY_VARIANTS = {"c", "r", "go"}


def _is_list_only(variant):
    return len(variant) <= 1 or variant in _LIST_ONLY_VARIANTS


# Vocabulary used to decide whether a certification is at least generally
# technical. Free-text-unsafe names are dropped for the reason above.
_VOCAB_FOR_TEXT = [k for k in DEFAULT_SKILL_KEYWORDS if not _is_list_only(k)]


def _norm_skill(text):
    return " ".join(text.lower().split())


def _as_skill_list(value):
    """
    Job skills are stored as comma-separated text (candidate.py splits them
    the same way). Returns a de-duplicated list in the original order and
    casing; also accepts an already-split list.
    """
    if not value:
        return []
    parts = value.split(",") if isinstance(value, str) else [
        p for p in value if isinstance(p, str)
    ]
    skills, seen = [], set()
    for part in parts:
        cleaned = " ".join(part.split())
        key = cleaned.lower()
        if cleaned and key not in seen:
            skills.append(cleaned)
            seen.add(key)
    return skills


def _variants(skill):
    key = _norm_skill(skill)
    return _ALIAS_INDEX.get(key, (key,))


def _variant_in_text(variant, text):
    """Is this one spelling of a skill present in free text?"""
    if _is_list_only(variant):
        return False
    if len(variant) == 2:
        # "ML", "AI", "C#": only the upper-case form counts, otherwise
        # "ai"/"ml" would match fragments of ordinary text.
        pattern = (
            r"(?<![A-Za-z0-9])" + re.escape(variant.upper()) + r"(?![A-Za-z0-9+#])"
        )
        return re.search(pattern, text) is not None
    return bool(find_keywords_in_text(text, [variant]))


def _skill_in_text(skill, text):
    return any(_variant_in_text(v, text) for v in _variants(skill))


def _text_checkable(skill):
    """False for names that can only be verified from the Skills list."""
    return any(not _is_list_only(v) for v in _variants(skill))


def _skill_matches(skill, listed, text):
    """Matched if it's in the parsed Skills list or found in the resume text."""
    if any(v in listed for v in _variants(skill)):
        return True
    return _skill_in_text(skill, text)


def _score_skills(weight, required, preferred, parsed, text):
    listed = {
        _norm_skill(s) for s in (parsed.get("skills") or []) if isinstance(s, str)
    }

    matched = [s for s in required if _skill_matches(s, listed, text)]
    missing = [s for s in required if s not in matched]
    pref_matched = [s for s in preferred if _skill_matches(s, listed, text)]
    pref_missing = [s for s in preferred if s not in pref_matched]

    if not required:
        comp = _component(weight, 1.0, "No required skills specified for this job")
    else:
        comp = _component(
            weight,
            len(matched) / len(required),
            f"{len(matched)} of {len(required)} required skills matched",
        )
    return comp, matched, missing, pref_matched, pref_missing


# ============================================================
# Experience (Section 9.2)
# ============================================================

_MONTH_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAMES = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_YEAR = r"(?:19|20)\d{2}"


def _date_pattern(prefix):
    # "Jan 2023", "January, 2023", "05/2021" or a bare "2021".
    return (
        rf"(?:(?:(?P<{prefix}mon>{_MONTH_NAMES})\.?,?\s*"
        rf"|(?P<{prefix}num>\d{{1,2}})\s*/\s*)?"
        rf"(?P<{prefix}year>{_YEAR}))"
    )


_PRESENT = r"(?P<present>present|current(?:ly)?|ongoing|till\s+date|to\s+date|now)"
_RANGE_SEP = r"\s*(?:[-\u2010-\u2015]|\bto\b|\buntil\b|\btill\b)\s*"
_RANGE_RE = re.compile(
    rf"(?<![A-Za-z0-9]){_date_pattern('s')}{_RANGE_SEP}"
    rf"(?:{_date_pattern('e')}|{_PRESENT})",
    re.IGNORECASE,
)
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\+?\s*(years?|yrs?|months?|mos?)\b", re.IGNORECASE
)


def _month_index(year, month):
    return year * 12 + (month - 1)


def _month_of(match, prefix, default):
    name = match.group(f"{prefix}mon")
    if name:
        return _MONTH_NUM[name[:3].lower()]
    num = match.group(f"{prefix}num")
    if num:
        value = int(num)
        return value if 1 <= value <= 12 else None
    return default


def _experience_months(text, today):
    """
    Total months of experience described in an experience block.

    * Date ranges ("Jan 2023 - Aug 2023", "05/2021 - 08/2021", "2021 -
      Present") are counted inclusively by month. A bare year counts as
      the whole calendar year (start -> January, end -> December), which
      favours the candidate by up to a few months; recruiters verify.
    * "Present" / "Current" end at `today`; ranges are clamped to `today`
      and ranges that start in the future are ignored.
    * Overlapping ranges (e.g. two concurrent internships) are merged so
      the same month is never counted twice.
    * Only if NO date range is found, stated durations such as "2 years"
      or "6 months" are summed as a fallback.
    """
    if not text:
        return 0

    today_idx = _month_index(today.year, today.month)
    intervals = []
    for m in _RANGE_RE.finditer(text):
        start_month = _month_of(m, "s", 1)
        if start_month is None:
            continue
        start = _month_index(int(m.group("syear")), start_month)

        if m.group("present"):
            end = today_idx
        else:
            end_month = _month_of(m, "e", 12)
            if end_month is None:
                continue
            end = _month_index(int(m.group("eyear")), end_month)

        end = min(end, today_idx)
        if start > today_idx or end < start:
            continue
        intervals.append((start, end))

    if intervals:
        intervals.sort()
        merged = [list(intervals[0])]
        for start, end in intervals[1:]:
            if start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return sum(end - start + 1 for start, end in merged)

    months = 0.0
    for amount, unit in _DURATION_RE.findall(text):
        value = float(amount)
        months += value * 12 if unit.lower().startswith(("y",)) else value
    return int(round(months))


def _score_experience(weight, required_years, experience_text, today):
    required_years = int(required_years or 0)
    months = _experience_months(experience_text or "", today)
    years = months / 12

    if required_years <= 0:
        return _component(
            weight, 1.0, "No experience required for this job"
        ), months

    return _component(
        weight,
        years / required_years,
        f"{months} months ({years:.1f} years) of experience found; "
        f"{required_years} years required",
    ), months


# ============================================================
# Education (Section 9.3)
# ============================================================

_LEVEL_NAMES = {1: "diploma", 2: "bachelor's", 3: "master's", 4: "doctorate"}

_LEVEL_PATTERNS = [
    (4, [r"\bph\s?d\b", r"\bdoctorate\b", r"doctor of philosophy"]),
    (3, [r"\bm\s?tech\b", r"\bmca\b", r"\bm\s?sc\b", r"\bmba\b", r"\bm\s?com\b",
         r"\bmasters?\b", r"\bpost\s?graduate\b", r"\bpgdm\b"]),
    (2, [r"\bb\s?tech\b", r"\bbachelors?\b", r"\bb\s?sc\b", r"\bbca\b",
         r"\bb\s?com\b", r"\bbba\b", r"\bb\s?arch\b", r"\bundergraduate\b",
         r"\bgraduate\b"]),
    (1, [r"\bdiploma\b", r"\bpolytechnic\b"]),
]
_LEVEL_RES = [
    (rank, re.compile("|".join(pats), re.IGNORECASE)) for rank, pats in _LEVEL_PATTERNS
]
# "BE" (Bachelor of Engineering) is only recognised in capitals; in
# lower/mixed case it is just the English word.
_LEVEL_CASE_SENSITIVE = [(2, re.compile(r"\bBE\b"))]

_FIELD_PATTERNS = {
    "computer": [r"computer science", r"computer sci\b", r"computer engineering",
                 r"\bcse\b", r"\bcs\b"],
    "it": [r"information technology", r"information systems",
           r"computer applications", r"\bbca\b", r"\bmca\b"],
    "software": [r"software engineering"],
    "data": [r"data science", r"artificial intelligence", r"machine learning",
             r"data analytics", r"\baiml\b", r"ai and ml"],
    "electronics": [r"electronics", r"communication", r"\bece\b",
                    r"instrumentation"],
    "electrical": [r"electrical", r"\beee\b"],
    "mechanical": [r"mechanical", r"\bmech\b", r"mechatronics", r"automobile",
                   r"production engineering"],
    "civil": [r"\bcivil\b", r"structural"],
    "chemical": [r"chemical"],
    "business": [r"business administration", r"\bmba\b", r"\bbba\b",
                 r"commerce", r"\bb\s?com\b", r"\bm\s?com\b", r"management"],
    "mathematics": [r"mathematics", r"\bmaths\b", r"statistics", r"physics"],
}
_FIELD_RES = {
    name: re.compile("|".join(pats), re.IGNORECASE)
    for name, pats in _FIELD_PATTERNS.items()
}
_FIELD_CASE_SENSITIVE = {"it": re.compile(r"\bIT\b")}

# Fields in the same cluster are "related branches"; certain clusters are
# "adjacent" (related qualification).
_FIELD_CLUSTER = {
    "computer": "computing", "it": "computing", "software": "computing",
    "data": "computing",
    "electronics": "electrical", "electrical": "electrical",
    "mechanical": "core_engineering", "civil": "core_engineering",
    "chemical": "core_engineering",
    "business": "business",
    "mathematics": "science",
}
_ADJACENT_CLUSTERS = {
    frozenset({"computing", "electrical"}),
    frozenset({"computing", "science"}),
}

_TIER_LABELS = {
    "exact": "required degree and branch satisfied",
    "related_branch": "required degree level, related branch",
    "related_qualification": "related qualification",
    "weak": "weakly related",
    "none": "does not satisfy the requirement",
}

_COURSEWORK_LINE_RE = re.compile(
    r"^\W*(?:relevant\s+)?(?:course\s?work|courses|subjects)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _clean_education_text(text):
    """
    Normalise degree abbreviations ("B.Tech." -> "BTech", "B.E." -> "BE",
    "&" -> "and") and drop "Relevant coursework: ..." lines, whose subject
    names would otherwise be mistaken for the candidate's field of study.
    """
    text = _COURSEWORK_LINE_RE.sub("", text)
    return text.replace(".", "").replace("&", " and ")


def _detect_levels(text):
    levels = {rank for rank, rx in _LEVEL_RES if rx.search(text)}
    levels |= {rank for rank, rx in _LEVEL_CASE_SENSITIVE if rx.search(text)}
    return levels


def _detect_fields(text):
    fields = {name for name, rx in _FIELD_RES.items() if rx.search(text)}
    fields |= {name for name, rx in _FIELD_CASE_SENSITIVE.items() if rx.search(text)}
    return fields


def _field_relation(required_fields, candidate_fields):
    """exact | related | adjacent | unrelated | unknown"""
    if not required_fields:
        return "exact"  # no branch requirement
    if not candidate_fields:
        return "unknown"

    best = "unrelated"
    order = {"unrelated": 0, "adjacent": 1, "related": 2, "exact": 3}
    for req in required_fields:
        for cand in candidate_fields:
            if req == cand:
                return "exact"
            req_cluster, cand_cluster = _FIELD_CLUSTER[req], _FIELD_CLUSTER[cand]
            if req_cluster == cand_cluster:
                rel = "related"
            elif frozenset({req_cluster, cand_cluster}) in _ADJACENT_CLUSTERS:
                rel = "adjacent"
            else:
                rel = "unrelated"
            if order[rel] > order[best]:
                best = rel
    return best


def _score_education(weight, requirement, education_text):
    requirement = (requirement or "").strip()
    if not requirement:
        return _component(weight, 1.0, "No education requirement specified")

    edu = _clean_education_text(education_text or "")
    cand_levels, cand_fields = _detect_levels(edu), _detect_fields(edu)
    if not cand_levels and not cand_fields:
        return _component(weight, 0.0, "No education details found in the resume")

    req_text = _clean_education_text(requirement)
    req_levels, req_fields = _detect_levels(req_text), _detect_fields(req_text)
    cand_level = max(cand_levels) if cand_levels else None
    found = (
        f"found {_LEVEL_NAMES[cand_level]} degree" if cand_level else "found no degree level"
    ) + (f", field: {', '.join(sorted(cand_fields))}" if cand_fields else "")

    # A requirement we can't interpret ("Relevant degree"): any degree counts.
    if not req_levels and not req_fields:
        tier = "exact" if cand_level else "weak"
        return _component(
            weight, EDU_TIERS[tier],
            f"Requirement '{requirement}' is not machine-readable; {found}",
        )

    req_level = min(req_levels) if req_levels else None
    level_ok = req_level is None or (cand_level is not None and cand_level >= req_level)
    relation = _field_relation(req_fields, cand_fields)

    if level_ok:
        tier = {
            "exact": "exact",
            "related": "related_branch",
            "adjacent": "related_qualification",
        }.get(relation, "weak")
    elif relation in ("exact", "related"):
        tier = "related_qualification"   # right field, lower degree level
    elif cand_level is not None:
        tier = "weak"
    else:
        tier = "none"

    return _component(
        weight, EDU_TIERS[tier],
        f"Requirement '{requirement}': {found} -> {_TIER_LABELS[tier]}",
    )


# ============================================================
# Projects (Section 9.4) and certifications (Section 9.5)
# ============================================================

_STOPWORDS = frozenset("""
a about above across after all also am an and any are as at be been being both
but by can could do does doing done during each either etc for from had has have
having he her here him his how i if in including into is it its just like may
me more most must my need needs no nor not of off on one only or other our out
over own per plus preferred required requires same she should so some such than
that the their them then there these they this those through to too under until
up upon us use used using very via was we well were what when where which while
who whom why will with within without would you your
ability able candidate company excellent experience good great job knowledge
looking opportunity responsibilities responsibility role skill skills strong
team work working year years
""".split())

_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#]*")
_RESUME_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#]*")


def _stem(token):
    """Very light stemmer so 'databases'/'database', 'APIs'/'API' compare equal."""
    if token.endswith("ing") and len(token) > 5:
        token = token[:-3]
    elif token.endswith("ed") and len(token) > 4:
        token = token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
        token = token[:-1]
    if token.endswith("e") and len(token) > 3:
        token = token[:-1]
    return token


def _terms(text):
    """Significant (non-stopword, >=3 chars) tokens of `text`, with counts."""
    counts = Counter()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) >= 3 and token not in _STOPWORDS:
            counts[token] += 1
    return counts


def _job_term_sets(job):
    """(title_terms, description_terms) used for relevance scoring."""
    title_terms = list(_terms(job.get("title")))
    desc_counts = _terms(job.get("description"))
    for term in title_terms:
        desc_counts.pop(term, None)
    desc_terms = [t for t, _ in desc_counts.most_common(DESC_TERM_LIMIT)]
    return title_terms, desc_terms


def _stem_set(text):
    return {_stem(t) for t in _RESUME_TOKEN_RE.findall((text or "").lower())}


def _coverage(terms, stems):
    if not terms:
        return 0.0
    return sum(1 for t in terms if _stem(t) in stems) / len(terms)


def _project_tier(ratio):
    if ratio >= PROJECT_HIGH_RATIO:
        return "high", "highly relevant"
    if ratio >= PROJECT_MODERATE_RATIO:
        return "moderate", "moderately relevant"
    if ratio > 0:
        return "weak", "weakly relevant"
    return "none", "not relevant"


def _score_projects(weight, required, job, projects_text):
    projects_text = projects_text or ""
    if not projects_text.strip():
        return _component(weight, 0.0, "No projects section found in the resume")

    checkable = [s for s in required if _text_checkable(s)]
    if checkable:
        hits = [s for s in checkable if _skill_in_text(s, projects_text)]
        ratio = len(hits) / len(checkable)
        basis = f"{len(hits)} of {len(checkable)} required skills mentioned in projects"
    else:
        # No usable required skills: fall back to the job's title/description terms.
        title_terms, desc_terms = _job_term_sets(job)
        terms = title_terms + desc_terms
        if not terms:
            return _component(
                weight, 1.0, "Job has no skills or role terms to compare projects with"
            )
        coverage = _coverage(terms, _stem_set(projects_text))
        ratio = min(1.0, coverage / RELEVANCE_FULL_AT)
        basis = f"{coverage:.0%} of job terms appear in projects"

    tier, label = _project_tier(ratio)
    return _component(weight, PROJECT_TIERS[tier], f"Projects {label} ({basis})")


def _score_certifications(weight, certs, required, preferred, job):
    certs = [c for c in (certs or []) if isinstance(c, str) and c.strip()]
    if not certs:
        return _component(weight, 0.0, "No certifications listed")

    primary = [s for s in (required or preferred) if _text_checkable(s)]
    secondary = [s for s in (preferred if required else []) if _text_checkable(s)]
    title_stems = {_stem(t) for t in _job_term_sets(job)[0]}

    best_tier, best_line = "none", None
    for line in certs:
        if any(_skill_in_text(s, line) for s in primary):
            tier = "high"
        elif (
            any(_skill_in_text(s, line) for s in secondary)
            or find_keywords_in_text(line, _VOCAB_FOR_TEXT)
            or (title_stems & _stem_set(line))
        ):
            tier = "partial"
        else:
            tier = "none"

        if CERT_TIERS[tier] > CERT_TIERS[best_tier]:
            best_tier, best_line = tier, line
        if best_tier == "high":
            break

    label = {"high": "highly relevant", "partial": "partially relevant",
             "none": "not relevant"}[best_tier]
    detail = (
        f"Best certification '{best_line}' is {label}"
        if best_line
        else f"{len(certs)} certification(s) listed, none relevant to the job"
    )
    return _component(weight, CERT_TIERS[best_tier], detail)


# ============================================================
# Relevance (Section 9.6)
# ============================================================

def _score_relevance(weight, job, resume_text, preferred_ratio):
    title_terms, desc_terms = _job_term_sets(job)
    stems = _stem_set(resume_text)

    parts = []  # (share, coverage)
    if title_terms:
        parts.append((RELEVANCE_TITLE_SHARE, _coverage(title_terms, stems)))
    if desc_terms:
        parts.append((RELEVANCE_DESC_SHARE, _coverage(desc_terms, stems)))
    if preferred_ratio is not None:
        parts.append((RELEVANCE_PREFERRED_SHARE, preferred_ratio))

    if not parts:
        return _component(
            weight, 1.0, "Job has no title, description or preferred skills to compare"
        )

    raw = sum(share * cov for share, cov in parts) / sum(share for share, _ in parts)
    return _component(
        weight,
        min(1.0, raw / RELEVANCE_FULL_AT),
        f"{raw:.0%} of job title/description/preferred terms found in the resume",
    )


# ============================================================
# Public API
# ============================================================

def _as_date(today):
    if today is None:
        return date.today()
    if isinstance(today, datetime):
        return today.date()
    return today


def _resume_text(parsed):
    """The full text, or a rebuild from the sections if it's missing."""
    text = parsed.get("resume_text")
    if isinstance(text, str) and text.strip():
        return text
    parts = []
    for key in ("education", "experience", "projects"):
        value = parsed.get(key)
        if isinstance(value, str):
            parts.append(value)
    for key in ("skills", "certifications"):
        value = parsed.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(v for v in value if isinstance(v, str))
    return "\n".join(parts)


def _unscored_result(weights, reason):
    return {
        "scored": False,
        "reason": reason,
        "total": 0,
        "weights": dict(weights),
        "components": {
            key: {"score": 0, "max": weights[key], "detail": "Not scored"}
            for key in WEIGHT_KEYS
        },
        "matched_skills": [],
        "missing_skills": [],
        "preferred_matched": [],
        "preferred_missing": [],
    }


def score_application(parsed, job, weights=None, today=None):
    """
    Score one resume against one job.

    parsed   dict returned by services.resume_parser.parse_resume(), or
             None if the resume couldn't be parsed.
    job      dict-like job_postings row (title, description,
             required_skills, preferred_skills, experience_required,
             education_required).
    weights  optional per-job weights (see validate_weights); defaults to
             Config.DEFAULT_SCORE_WEIGHTS.
    today    date used for "Present" in experience ranges (default: today).

    Returns:
        {
          "scored": bool,
          "reason": str | None,          # why it wasn't scored
          "total": int,                   # sum of the rounded components
          "weights": {criterion: points},
          "components": {criterion: {"score": int, "max": int, "detail": str}},
          "matched_skills": [...], "missing_skills": [...],     # required
          "preferred_matched": [...], "preferred_missing": [...],
        }

    Raises ValueError for invalid weights or a job that isn't dict-like.
    Never modifies its inputs.
    """
    w = validate_weights(weights)
    if not isinstance(job, Mapping):
        raise ValueError("job must be a dict-like job_postings row.")

    if not isinstance(parsed, Mapping):
        return _unscored_result(w, "The resume could not be parsed.")
    text = _resume_text(parsed)
    if parsed.get("is_low_text") or not text.strip():
        return _unscored_result(
            w, "The resume has too little extractable text (possibly a scanned PDF)."
        )

    today = _as_date(today)
    required = _as_skill_list(job.get("required_skills"))
    preferred = _as_skill_list(job.get("preferred_skills"))

    skills, matched, missing, pref_matched, pref_missing = _score_skills(
        w["skills"], required, preferred, parsed, text
    )
    experience, _months = _score_experience(
        w["experience"], job.get("experience_required"), parsed.get("experience"), today
    )
    education = _score_education(
        w["education"], job.get("education_required"), parsed.get("education")
    )
    projects = _score_projects(w["projects"], required, job, parsed.get("projects"))
    certifications = _score_certifications(
        w["certifications"], parsed.get("certifications"), required, preferred, job
    )
    preferred_ratio = len(pref_matched) / len(preferred) if preferred else None
    relevance = _score_relevance(w["relevance"], job, text, preferred_ratio)

    components = {
        "skills": skills,
        "experience": experience,
        "education": education,
        "projects": projects,
        "certifications": certifications,
        "relevance": relevance,
    }
    return {
        "scored": True,
        "reason": None,
        "total": sum(c["score"] for c in components.values()),
        "weights": w,
        "components": components,
        "matched_skills": matched,
        "missing_skills": missing,
        "preferred_matched": pref_matched,
        "preferred_missing": pref_missing,
    }


# ============================================================
# Persistence helpers (schema.sql, Table 3: applications)
# ============================================================

def to_application_columns(result):
    """
    Map a score_application() result onto the `applications` columns.
    Skill lists become comma-separated text (what candidate.py's
    _split_skills expects); an empty list becomes None (NULL).
    """
    comps = result["components"]
    return {
        "match_score": result["total"],
        "skill_score": comps["skills"]["score"],
        "experience_score": comps["experience"]["score"],
        "education_score": comps["education"]["score"],
        "project_score": comps["projects"]["score"],
        "certification_score": comps["certifications"]["score"],
        "relevance_score": comps["relevance"]["score"],
        "matched_skills": ", ".join(result["matched_skills"]) or None,
        "missing_skills": ", ".join(result["missing_skills"]) or None,
    }


_UPDATE_SCORE_SQL = """
    UPDATE applications
    SET match_score = %s,
        skill_score = %s,
        experience_score = %s,
        education_score = %s,
        project_score = %s,
        certification_score = %s,
        relevance_score = %s,
        matched_skills = %s,
        missing_skills = %s
    WHERE id = %s
"""


def save_score(db, application_id, result):
    """
    Write a score to its `applications` row with a single UPDATE.

    `db` is an open connection exposing `.cursor()` as a context manager
    (the same convention as the rest of the project). The caller commits,
    so this can join a larger transaction.

    Returns True if an UPDATE was issued, False if the result is
    unscored (nothing is written, so any previous valid score survives).
    The UPDATE touches only the score columns, never status, duplicate
    or consistency fields.
    """
    if not result.get("scored"):
        return False

    cols = to_application_columns(result)
    with db.cursor() as cur:
        cur.execute(
            _UPDATE_SCORE_SQL,
            (
                cols["match_score"], cols["skill_score"], cols["experience_score"],
                cols["education_score"], cols["project_score"],
                cols["certification_score"], cols["relevance_score"],
                cols["matched_skills"], cols["missing_skills"],
                application_id,
            ),
        )
    return True