"""
Tests for modules/scoring_engine.py (Step 32).

Standalone: no database, no PDF files, no Flask app. Inputs are hand-built
`parsed` / `job` dicts shaped like services.resume_parser.parse_resume()
output and job_postings rows. One end-to-end test runs resume *text*
through the real parser's extractors to confirm the engine consumes the
parser's actual output shape.

Run from the project root with either:
    python -m unittest tests.test_scoring_engine -v
    pytest tests/test_scoring_engine.py
"""

import copy
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from modules import scoring_engine as se
from modules.scoring_engine import (
    WEIGHT_KEYS,
    save_score,
    score_application,
    to_application_columns,
    validate_weights,
)
from services import resume_parser as rp

TODAY = date(2025, 6, 15)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def make_job(**overrides):
    job = {
        "title": "Backend Developer",
        "description": "",
        "required_skills": "Python, Flask, MySQL, REST API, Git",
        "preferred_skills": "",
        "experience_required": 0,
        "education_required": "",
    }
    job.update(overrides)
    return job


def make_parsed(**overrides):
    parsed = {
        "name": "Rahul Sharma",
        "email": "rahul@example.com",
        "phone": None,
        "education": "",
        "skills": [],
        "experience": "",
        "projects": "",
        "certifications": [],
        "is_low_text": False,
    }
    parsed.update(overrides)
    if "resume_text" not in overrides:
        pieces = [parsed["education"], parsed["experience"], parsed["projects"]]
        pieces += list(parsed["skills"]) + list(parsed["certifications"])
        parsed["resume_text"] = "\n".join(p for p in pieces if p) or "x" * 60
    return parsed


def score(parsed=None, job=None, weights=None, today=TODAY):
    return score_application(
        parsed if parsed is not None else make_parsed(),
        job if job is not None else make_job(),
        weights=weights,
        today=today,
    )


def parsed_from_text(text):
    """What parse_resume() does after the PDF -> text step."""
    sections = rp._find_section_blocks(text)
    return {
        "name": rp.extract_name(text),
        "email": rp.extract_email(text),
        "phone": rp.extract_phone(text),
        "education": rp.extract_education(text, sections),
        "skills": rp.extract_skills(text, sections),
        "experience": rp.extract_experience(text, sections),
        "projects": rp.extract_projects(text, sections),
        "certifications": rp.extract_certifications(text, sections),
        "resume_text": text,
        "is_low_text": rp.is_low_text(text),
    }


# ------------------------------------------------------------
# Weights
# ------------------------------------------------------------

class WeightTests(unittest.TestCase):
    def test_defaults_match_config_and_sum_to_100(self):
        w = validate_weights()
        self.assertEqual(w, Config.DEFAULT_SCORE_WEIGHTS)
        self.assertEqual(sum(w.values()), 100)
        self.assertEqual(set(w), set(WEIGHT_KEYS))

    def test_defaults_are_not_mutated_or_aliased(self):
        before = copy.deepcopy(Config.DEFAULT_SCORE_WEIGHTS)
        w = validate_weights()
        w["skills"] = 0
        self.assertEqual(Config.DEFAULT_SCORE_WEIGHTS, before)

    def test_missing_keys_become_zero(self):
        w = validate_weights(
            {"skills": 50, "experience": 25, "education": 10,
             "projects": 10, "certifications": 5}
        )
        self.assertEqual(w["relevance"], 0)

    def test_rejects_bad_weights(self):
        bad = {
            "sum != 100": {"skills": 50, "experience": 20},
            "unknown key": {**Config.DEFAULT_SCORE_WEIGHTS, "luck": 0},
            "negative": {"skills": 110, "experience": -10},
            "non-integer": {"skills": 40.5, "experience": 59.5},
            "bool": {"skills": True, "experience": 99},
            "string": {"skills": "100"},
            "not a dict": [40, 20, 15, 10, 5, 10],
        }
        for label, weights in bad.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    validate_weights(weights)

    def test_integral_float_is_accepted(self):
        w = validate_weights({"skills": 100.0})
        self.assertEqual(w["skills"], 100)
        self.assertIsInstance(w["skills"], int)

    def test_score_application_rejects_bad_weights(self):
        with self.assertRaises(ValueError):
            score(weights={"skills": 99})


# ------------------------------------------------------------
# Skills (Section 9.1)
# ------------------------------------------------------------

class SkillTests(unittest.TestCase):
    def skills_component(self, **parsed_kw):
        return score(make_parsed(**parsed_kw))["components"]["skills"]

    def test_section_9_1_example_scores_32_of_40(self):
        # Python, Flask, MySQL, Git present; REST API missing.
        result = score(make_parsed(skills=["Python", "Flask", "MySQL", "Git"]))
        comp = result["components"]["skills"]
        self.assertEqual((comp["score"], comp["max"]), (32, 40))
        self.assertEqual(result["missing_skills"], ["REST API"])
        self.assertEqual(result["matched_skills"], ["Python", "Flask", "MySQL", "Git"])

    def test_all_and_none_matched(self):
        full = self.skills_component(
            skills=["Python", "Flask", "MySQL", "REST API", "Git"]
        )
        self.assertEqual(full["score"], 40)
        none = self.skills_component(skills=["Excel"], resume_text="I like Excel " * 10)
        self.assertEqual(none["score"], 0)

    def test_skill_found_in_free_text_counts_and_is_case_insensitive(self):
        comp = self.skills_component(
            skills=[],
            resume_text="Built services in PYTHON with flask and mysql, versioned in git. "
                        "Exposed REST APIs.",
        )
        self.assertEqual(comp["score"], 40)

    def test_alias_matching_both_ways(self):
        job = make_job(required_skills="REST API, Node.js, PostgreSQL")
        result = score(
            make_parsed(skills=[], resume_text="Designed RESTful APIs on NodeJS with Postgres."),
            job,
        )
        self.assertEqual(result["missing_skills"], [])

        job = make_job(required_skills="RESTful, nodejs, Postgres")
        result = score(
            make_parsed(skills=["REST API", "Node.js", "PostgreSQL"]), job
        )
        self.assertEqual(result["missing_skills"], [])

    def test_word_boundaries_prevent_partial_matches(self):
        job = make_job(required_skills="Java")
        result = score(
            make_parsed(skills=[], resume_text="Strong JavaScript developer " * 5), job
        )
        self.assertEqual(result["missing_skills"], ["Java"])

    def test_ambiguous_names_only_count_from_the_skills_list(self):
        job = make_job(required_skills="Go, C")
        # Ordinary prose + a C++-only resume must not match Go or C.
        prose = score(
            make_parsed(skills=["C++"], resume_text="Go to market strategy. C++ projects. " * 3),
            job,
        )
        self.assertEqual(prose["matched_skills"], [])
        # An explicit Skills-list entry does match.
        listed = score(make_parsed(skills=["Go", "C", "C++"]), job)
        self.assertEqual(listed["matched_skills"], ["Go", "C"])

    def test_short_acronyms_need_capitals_in_text(self):
        job = make_job(required_skills="ML")
        lower = score(make_parsed(skills=[], resume_text="html and xml markup " * 5), job)
        self.assertEqual(lower["missing_skills"], ["ML"])
        upper = score(make_parsed(skills=[], resume_text="Applied ML models " * 5), job)
        self.assertEqual(upper["matched_skills"], ["ML"])
        spelled = score(
            make_parsed(skills=[], resume_text="Studied machine learning " * 5), job
        )
        self.assertEqual(spelled["matched_skills"], ["ML"])

    def test_cpp_job_skill_matches_cpp_text(self):
        job = make_job(required_skills="C++")
        result = score(make_parsed(skills=[], resume_text="Wrote C++ services " * 5), job)
        self.assertEqual(result["matched_skills"], ["C++"])

    def test_job_skill_parsing_dedupes_and_ignores_blanks(self):
        job = make_job(required_skills=" Python ,python, , Flask ,, ")
        result = score(make_parsed(skills=["Python"]), job)
        self.assertEqual(result["matched_skills"], ["Python"])
        self.assertEqual(result["missing_skills"], ["Flask"])
        self.assertEqual(result["components"]["skills"]["score"], 20)

    def test_no_required_skills_gives_full_marks(self):
        comp = score(job=make_job(required_skills="")).get("components")["skills"]
        self.assertEqual(comp["score"], 40)
        self.assertIn("No required skills", comp["detail"])

    def test_preferred_skills_reported_but_not_in_skill_score(self):
        job = make_job(required_skills="Python", preferred_skills="Docker, AWS")
        result = score(make_parsed(skills=["Python", "Docker"]), job)
        self.assertEqual(result["components"]["skills"]["score"], 40)
        self.assertEqual(result["preferred_matched"], ["Docker"])
        self.assertEqual(result["preferred_missing"], ["AWS"])


# ------------------------------------------------------------
# Experience (Section 9.2)
# ------------------------------------------------------------

class ExperienceTests(unittest.TestCase):
    def months(self, text, today=TODAY):
        return se._experience_months(text, today)

    def test_month_ranges_are_inclusive(self):
        self.assertEqual(self.months("Intern  Jan 2023 - Jun 2023"), 6)
        self.assertEqual(self.months("Intern  Jan 2023 \u2013 Aug 2023"), 8)  # en dash
        self.assertEqual(self.months("Jan 2023 to Aug 2023"), 8)
        self.assertEqual(self.months("January 2023 - March 2023"), 3)
        self.assertEqual(self.months("Sept 2022 - Nov 2022"), 3)

    def test_numeric_month_format(self):
        self.assertEqual(self.months("05/2021 - 08/2021"), 4)

    def test_year_only_ranges_count_whole_calendar_years(self):
        self.assertEqual(self.months("2022 - 2023"), 24)

    def test_present_uses_today(self):
        self.assertEqual(self.months("Jun 2024 - Present"), 13)
        self.assertEqual(self.months("Jun 2024 - Current"), 13)
        self.assertEqual(self.months("Jun 2024 - Present", date(2024, 12, 1)), 7)

    def test_overlapping_ranges_are_merged(self):
        text = "Jan 2022 - Jun 2022\nApr 2022 - Sep 2022"
        self.assertEqual(self.months(text), 9)

    def test_separate_ranges_add_up(self):
        text = "Jan 2022 - Mar 2022\nJul 2022 - Aug 2022"
        self.assertEqual(self.months(text), 5)

    def test_future_and_reversed_ranges_are_ignored_or_clamped(self):
        self.assertEqual(self.months("Jan 2030 - Dec 2030"), 0)
        self.assertEqual(self.months("Aug 2023 - Jan 2023"), 0)
        # Starts in the past, "ends" in the future -> clamped to today.
        self.assertEqual(self.months("Jan 2025 - Dec 2030"), 6)

    def test_stated_durations_are_a_fallback_only(self):
        self.assertEqual(self.months("2 years of experience at Acme"), 24)
        self.assertEqual(self.months("Intern for 6 months"), 6)
        # Date ranges win; the stated duration isn't added on top.
        self.assertEqual(self.months("Jan 2023 - Jun 2023 (about 2 years of work)"), 6)

    def test_no_experience(self):
        self.assertEqual(self.months(""), 0)
        self.assertEqual(self.months("Worked at a company."), 0)

    def comp(self, experience, required, weight_default=20):
        result = score(
            make_parsed(experience=experience),
            make_job(experience_required=required),
        )
        return result["components"]["experience"]

    def test_section_9_2_example_half_credit(self):
        # Required 2 years, candidate has 12 months -> (1/2) x 20 = 10.
        comp = self.comp("Dev  Jan 2024 - Dec 2024", 2)
        self.assertEqual((comp["score"], comp["max"]), (10, 20))

    def test_meeting_or_exceeding_is_capped_at_20(self):
        self.assertEqual(self.comp("Jan 2020 - Dec 2024", 2)["score"], 20)
        self.assertEqual(self.comp("Jan 2023 - Dec 2024", 2)["score"], 20)

    def test_zero_requirement_gives_full_marks(self):
        comp = self.comp("", 0)
        self.assertEqual(comp["score"], 20)
        self.assertIn("No experience required", comp["detail"])

    def test_missing_requirement_value_is_treated_as_zero(self):
        result = score(job=make_job(experience_required=None))
        self.assertEqual(result["components"]["experience"]["score"], 20)

    def test_no_experience_with_requirement_scores_zero(self):
        self.assertEqual(self.comp("", 3)["score"], 0)


# ------------------------------------------------------------
# Education (Section 9.3)
# ------------------------------------------------------------

class EducationTests(unittest.TestCase):
    REQ = "B.Tech CSE"

    def edu(self, education, requirement=REQ):
        result = score(
            make_parsed(education=education),
            make_job(education_required=requirement),
        )
        return result["components"]["education"]

    def assertEdu(self, education, expected, requirement=REQ):
        comp = self.edu(education, requirement)
        self.assertEqual(comp["score"], expected, comp["detail"])
        self.assertEqual(comp["max"], 15)

    def test_exact_degree_and_branch(self):
        self.assertEdu("B.Tech in Computer Science and Engineering, XYZ University", 15)
        self.assertEdu("BTech CSE 2020 - 2024", 15)
        self.assertEdu("Bachelor of Technology, Computer Science", 15)
        self.assertEdu("B.E. Computer Engineering", 15)

    def test_required_degree_related_branch(self):
        self.assertEdu("B.Tech Information Technology", 12)
        self.assertEdu("BCA", 12)

    def test_related_qualification(self):
        # Adjacent branch (electronics) with the right degree level.
        self.assertEdu("B.Tech Electronics and Communication Engineering", 8)
        # Right branch but a lower degree level.
        self.assertEdu("Diploma in Computer Science", 8)

    def test_weakly_related(self):
        # Blueprint Section 7 example: CSE required, Mechanical found.
        self.assertEdu("B.Tech Mechanical Engineering", 3)
        self.assertEdu("Diploma in Mechanical Engineering", 3)

    def test_does_not_satisfy(self):
        self.assertEdu("Higher secondary, 12th grade, XYZ School", 0)

    def test_higher_degree_satisfies_lower_requirement(self):
        self.assertEdu("M.Tech Computer Science", 15)
        self.assertEdu("MCA", 12)  # master's level, IT-family branch

    def test_requirement_without_branch_only_checks_level(self):
        self.assertEdu("B.Tech Mechanical Engineering", 15, requirement="B.Tech")
        # No branch required -> branch trivially satisfied; a lower degree
        # level in a satisfied branch is a "related qualification" (8).
        self.assertEdu("Diploma in Mechanical", 8, requirement="B.Tech")
        self.assertEdu("B.Tech Mechanical Engineering", 15, requirement="Any graduate")

    def test_requirement_without_level_only_checks_branch(self):
        self.assertEdu("B.Tech Computer Science", 15, requirement="Computer Science")
        self.assertEdu("B.Tech Civil Engineering", 3, requirement="Computer Science")

    def test_unreadable_requirement_accepts_any_degree(self):
        self.assertEdu("B.Tech Mechanical", 15, requirement="Relevant degree")
        self.assertEdu("12th grade", 0, requirement="Relevant degree")

    def test_no_requirement_gives_full_marks(self):
        comp = self.edu("", requirement="")
        self.assertEqual(comp["score"], 15)
        self.assertIn("No education requirement", comp["detail"])

    def test_empty_education_with_requirement_scores_zero(self):
        self.assertEqual(self.edu("")["score"], 0)

    def test_coursework_lines_do_not_create_a_field(self):
        text = (
            "B.Tech Mechanical Engineering, ABC College\n"
            "Relevant coursework: Computer Science fundamentals, Data Structures\n"
        )
        self.assertEdu(text, 3)

    def test_uppercase_IT_is_a_field_but_lowercase_it_is_not(self):
        self.assertEdu("B.Tech IT", 12)
        self.assertEdu("B.Tech in mechanical engineering; I did it well", 3)

    def test_lowercase_be_is_not_a_degree(self):
        self.assertEdu("to be or not to be", 0)

    def test_best_degree_across_multiple_entries(self):
        text = "B.Tech Mechanical Engineering\nM.Tech Computer Science"
        self.assertEdu(text, 15)

    def test_scales_with_custom_weight(self):
        w = {"skills": 40, "experience": 20, "education": 20, "projects": 10,
             "certifications": 5, "relevance": 5}
        result = score(
            make_parsed(education="B.Tech Information Technology"),
            make_job(education_required=self.REQ),
            weights=w,
        )
        comp = result["components"]["education"]
        self.assertEqual((comp["score"], comp["max"]), (16, 20))  # 0.8 x 20


# ------------------------------------------------------------
# Projects (Section 9.4)
# ------------------------------------------------------------

class ProjectTests(unittest.TestCase):
    JOB = make_job(required_skills="Python, Flask, MySQL, REST API")

    def projects(self, text):
        result = score(make_parsed(projects=text, skills=["Excel"]), self.JOB)
        return result["components"]["projects"]

    def test_section_9_4_example_is_highly_relevant(self):
        comp = self.projects(
            "Developed a Flask recruitment platform using Python, MySQL and REST APIs."
        )
        self.assertEqual((comp["score"], comp["max"]), (10, 10))

    def test_moderate_weak_and_none(self):
        self.assertEqual(self.projects("Built a blog with Python and Flask.")["score"], 6)
        self.assertEqual(self.projects("Wrote scripts in Python.")["score"], 3)
        self.assertEqual(self.projects("Designed a poster for the college fest.")["score"], 0)

    def test_no_projects_section_scores_zero(self):
        comp = self.projects("")
        self.assertEqual(comp["score"], 0)
        self.assertIn("No projects", comp["detail"])

    def test_similar_projects_are_not_penalised(self):
        # Four students sharing the same college project (Section 6) all
        # score identically; similarity is never held against anyone.
        text = "Recroot: resume screening built with Python, Flask, MySQL and REST APIs."
        scores = {
            score(make_parsed(projects=text, skills=["Python"]), self.JOB)
            ["components"]["projects"]["score"]
            for _ in range(4)
        }
        self.assertEqual(scores, {10})

    def test_falls_back_to_job_terms_without_required_skills(self):
        job = make_job(
            required_skills="",
            title="Data Analyst",
            description="Analyze sales data and build dashboards for reporting.",
        )
        good = score(
            make_parsed(projects="Built reporting dashboards to analyze sales data."), job
        )["components"]["projects"]
        bad = score(make_parsed(projects="Painted a mural for the library."), job)[
            "components"]["projects"]
        self.assertGreaterEqual(good["score"], 6)
        self.assertEqual(bad["score"], 0)

    def test_no_basis_for_comparison_gives_full_marks(self):
        job = make_job(required_skills="", title="", description="")
        comp = score(make_parsed(projects="Some project"), job)["components"]["projects"]
        self.assertEqual(comp["score"], 10)


# ------------------------------------------------------------
# Certifications (Section 9.5)
# ------------------------------------------------------------

class CertificationTests(unittest.TestCase):
    JOB = make_job(required_skills="Python, Flask", preferred_skills="Docker")

    def certs(self, certifications, job=None):
        result = score(
            make_parsed(certifications=certifications, skills=["Python"]),
            job or self.JOB,
        )
        return result["components"]["certifications"]

    def test_highly_relevant(self):
        comp = self.certs(["Python for Everybody Specialization"])
        self.assertEqual((comp["score"], comp["max"]), (5, 5))

    def test_partially_relevant(self):
        # Technical (AWS) but not one of the job's required skills.
        self.assertEqual(self.certs(["AWS Certified Cloud Practitioner"])["score"], 3)
        # Preferred skill.
        self.assertEqual(self.certs(["Docker Certified Associate"])["score"], 3)

    def test_not_relevant(self):
        self.assertEqual(self.certs(["Spoken English Certificate"])["score"], 0)

    def test_best_certification_wins(self):
        comp = self.certs(
            ["Spoken English Certificate", "AWS Cloud Practitioner",
             "Flask Web Development Certificate"]
        )
        self.assertEqual(comp["score"], 5)

    def test_none_listed(self):
        comp = self.certs([])
        self.assertEqual(comp["score"], 0)
        self.assertIn("No certifications", comp["detail"])

    def test_job_title_term_gives_partial_credit(self):
        job = make_job(required_skills="Tableau", title="Data Analyst")
        comp = self.certs(["Google Data Analytics Certificate"], job)
        self.assertEqual(comp["score"], 3)


# ------------------------------------------------------------
# Relevance (Section 9.6)
# ------------------------------------------------------------

class RelevanceTests(unittest.TestCase):
    JOB = make_job(
        title="Backend Developer",
        description=(
            "Build REST APIs with Python and Flask for our recruitment platform. "
            "Work with MySQL databases and write clean, tested code."
        ),
    )

    def relevance(self, text):
        return score(
            make_parsed(skills=[], resume_text=text), self.JOB
        )["components"]["relevance"]

    def test_strong_match_scores_high(self):
        comp = self.relevance(
            "Backend developer. Built REST APIs with Python and Flask for a recruitment "
            "platform, worked with MySQL databases, wrote clean tested code."
        )
        self.assertEqual(comp["max"], 10)
        self.assertGreaterEqual(comp["score"], 9)

    def test_unrelated_resume_scores_near_zero(self):
        comp = self.relevance(
            "Pastry chef with ten years in a busy bakery, specialising in sourdough "
            "and wedding cakes."
        )
        self.assertLessEqual(comp["score"], 1)

    def test_partial_match_is_in_between(self):
        comp = self.relevance("Developer who has written Python scripts for a platform.")
        self.assertTrue(1 <= comp["score"] <= 9, comp)

    def test_no_title_description_or_preferred_gives_full_marks(self):
        job = make_job(title="", description="", preferred_skills="")
        comp = score(job=job)["components"]["relevance"]
        self.assertEqual(comp["score"], 10)

    def test_preferred_skills_feed_relevance(self):
        job = make_job(title="", description="", preferred_skills="Docker, AWS")
        hit = score(make_parsed(skills=["Docker", "AWS"]), job)["components"]["relevance"]
        miss = score(make_parsed(skills=["Excel"]), job)["components"]["relevance"]
        self.assertEqual(hit["score"], 10)
        self.assertEqual(miss["score"], 0)


# ------------------------------------------------------------
# Whole-result behaviour
# ------------------------------------------------------------

class ResultTests(unittest.TestCase):
    def strong_parsed(self):
        return make_parsed(
            education="B.Tech in Computer Science and Engineering",
            skills=["Python", "Flask", "MySQL", "REST API", "Git"],
            experience="Backend Developer  Jan 2022 - Dec 2024",
            projects="Flask recruitment platform using Python, MySQL and REST APIs.",
            certifications=["Python Certification"],
        )

    def strong_job(self):
        return make_job(
            experience_required=2,
            education_required="B.Tech CSE",
            description="Develop REST APIs with Python and Flask.",
        )

    def test_total_is_sum_of_rounded_components_and_within_limits(self):
        result = score(self.strong_parsed(), self.strong_job())
        comps = result["components"]
        self.assertEqual(result["total"], sum(c["score"] for c in comps.values()))
        for key, comp in comps.items():
            self.assertEqual(comp["max"], Config.DEFAULT_SCORE_WEIGHTS[key])
            self.assertTrue(0 <= comp["score"] <= comp["max"], key)
            self.assertIsInstance(comp["score"], int)
            self.assertTrue(comp["detail"])
        self.assertTrue(0 <= result["total"] <= 100)

    def test_perfect_match_scores_100(self):
        result = score(self.strong_parsed(), self.strong_job())
        self.assertTrue(result["scored"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["missing_skills"], [])

    def test_section_10_style_breakdown(self):
        # Section 10 shape: strong but not perfect. 9 of 10 required skills
        # (Docker missing) = 36/40, 54 of 60 required months = 18/20, and
        # the exact degree = 15/15. (The blueprint's literal 87 total can't
        # be reproduced: Projects only awards 10/6/3/0, never 8.)
        job = make_job(
            required_skills="Python, Flask, MySQL, Git, REST API, HTML, CSS, "
                            "JavaScript, SQL, Docker",
            experience_required=5,
            education_required="B.Tech CSE",
        )
        parsed = make_parsed(
            education="B.Tech Computer Science and Engineering",
            skills=["Python", "Flask", "MySQL", "Git", "REST API", "HTML", "CSS",
                    "JavaScript", "SQL"],
            experience="Developer  Jan 2020 - Jun 2024",   # 54 months
        )
        result = score(parsed, job)
        c = result["components"]
        self.assertEqual(c["skills"]["score"], 36)
        self.assertEqual(c["experience"]["score"], 18)
        self.assertEqual(c["education"]["score"], 15)
        self.assertEqual(result["missing_skills"], ["Docker"])
        self.assertEqual(result["total"], sum(x["score"] for x in c.values()))

    def test_custom_weights_backend_developer_example(self):
        # Section 11: Skills 50, Experience 25, Education 10, Projects 10,
        # Certifications 5; relevance not used.
        w = {"skills": 50, "experience": 25, "education": 10, "projects": 10,
             "certifications": 5}
        result = score(self.strong_parsed(), self.strong_job(), weights=w)
        c = result["components"]
        self.assertEqual(c["skills"]["max"], 50)
        self.assertEqual(c["skills"]["score"], 50)
        self.assertEqual((c["relevance"]["max"], c["relevance"]["score"]), (0, 0))
        self.assertEqual(result["total"], 100)
        self.assertEqual(result["weights"]["relevance"], 0)

    def test_custom_weights_fresher_example(self):
        w = {"skills": 40, "projects": 25, "education": 20, "experience": 5,
             "certifications": 10}
        result = score(self.strong_parsed(), self.strong_job(), weights=w)
        self.assertEqual(result["components"]["projects"]["max"], 25)
        self.assertEqual(result["components"]["projects"]["score"], 25)
        self.assertLessEqual(result["total"], 100)

    def test_ranking_orders_candidates_by_fit(self):
        job = self.strong_job()
        strong = score(self.strong_parsed(), job)["total"]
        medium = score(
            make_parsed(
                education="B.Tech Information Technology",
                skills=["Python", "Flask"],
                experience="Intern  Jan 2024 - Jun 2024",
                projects="Wrote Python scripts.",
            ),
            job,
        )["total"]
        weak = score(
            make_parsed(
                education="B.Tech Mechanical Engineering",
                skills=["AutoCAD"],
                experience="",
                projects="Designed a gearbox.",
            ),
            job,
        )["total"]
        self.assertGreater(strong, medium)
        self.assertGreater(medium, weak)

    def test_inputs_are_not_mutated(self):
        parsed, job = self.strong_parsed(), self.strong_job()
        parsed_before, job_before = copy.deepcopy(parsed), copy.deepcopy(job)
        score(parsed, job)
        self.assertEqual(parsed, parsed_before)
        self.assertEqual(job, job_before)

    def test_deterministic(self):
        a = score(self.strong_parsed(), self.strong_job())
        b = score(self.strong_parsed(), self.strong_job())
        self.assertEqual(a, b)

    def test_duplicate_and_consistency_flags_never_change_the_score(self):
        parsed, job = self.strong_parsed(), self.strong_job()
        baseline = score(parsed, job)
        job_flagged = dict(job, duplicate_flag=True, consistency_flag=True)
        parsed_flagged = dict(
            parsed, duplicate_flag=True, duplicate_similarity=99,
            consistency_flag=True, consistency_notes="Name mismatch detected.",
        )
        self.assertEqual(score(parsed_flagged, job_flagged), baseline)

    def test_dictcursor_style_job_rows_work(self):
        # Rows from pymysql DictCursor are plain dicts and may carry extra
        # columns such as datetime/date values.
        job = make_job(id=7, created_by=1, status="open", deadline=date(2026, 1, 1))
        self.assertTrue(score(job=job)["scored"])


class UnscoredTests(unittest.TestCase):
    def assertUnscored(self, result):
        self.assertFalse(result["scored"])
        self.assertTrue(result["reason"])
        self.assertEqual(result["total"], 0)
        self.assertEqual(set(result["components"]), set(WEIGHT_KEYS))
        for comp in result["components"].values():
            self.assertEqual(comp["score"], 0)

    def test_parse_failure(self):
        self.assertUnscored(score_application(None, make_job(), today=TODAY))

    def test_low_text_resume(self):
        parsed = make_parsed(resume_text="Name", is_low_text=True)
        self.assertUnscored(score(parsed))

    def test_empty_everything(self):
        parsed = {"resume_text": "", "is_low_text": False, "skills": [],
                  "certifications": []}
        self.assertUnscored(score(parsed))

    def test_invalid_job_raises(self):
        with self.assertRaises(ValueError):
            score_application(make_parsed(), None)
        with self.assertRaises(ValueError):
            score_application(make_parsed(), ["not", "a", "job"])

    def test_missing_optional_parsed_keys_are_tolerated(self):
        parsed = {"resume_text": "Python developer with Flask and MySQL experience. " * 3}
        result = score(parsed)
        self.assertTrue(result["scored"])
        self.assertEqual(result["components"]["skills"]["score"], 24)  # 3 of 5


# ------------------------------------------------------------
# Persistence helpers
# ------------------------------------------------------------

class FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))


class FakeDB:
    def __init__(self):
        self.executed = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.executed)

    def commit(self):
        self.commits += 1


class PersistenceTests(unittest.TestCase):
    def result(self):
        parsed = make_parsed(skills=["Python", "Flask", "MySQL", "Git"],
                             education="B.Tech CSE")
        return score(parsed, make_job(education_required="B.Tech CSE"))

    def test_column_mapping(self):
        result = self.result()
        cols = to_application_columns(result)
        comps = result["components"]
        self.assertEqual(cols["match_score"], result["total"])
        self.assertEqual(cols["skill_score"], comps["skills"]["score"])
        self.assertEqual(cols["experience_score"], comps["experience"]["score"])
        self.assertEqual(cols["education_score"], comps["education"]["score"])
        self.assertEqual(cols["project_score"], comps["projects"]["score"])
        self.assertEqual(cols["certification_score"], comps["certifications"]["score"])
        self.assertEqual(cols["relevance_score"], comps["relevance"]["score"])
        self.assertEqual(cols["matched_skills"], "Python, Flask, MySQL, Git")
        self.assertEqual(cols["missing_skills"], "REST API")

    def test_skill_text_round_trips_through_split_skills(self):
        # candidate.py's _split_skills: split on commas, strip, drop blanks.
        cols = to_application_columns(self.result())
        parts = [s.strip() for s in cols["matched_skills"].split(",") if s.strip()]
        self.assertEqual(parts, ["Python", "Flask", "MySQL", "Git"])

    def test_empty_skill_lists_become_null(self):
        parsed = make_parsed(skills=["Python", "Flask", "MySQL", "REST API", "Git"])
        cols = to_application_columns(score(parsed))
        self.assertIsNone(cols["missing_skills"])

    def test_save_score_issues_one_scoped_update_and_does_not_commit(self):
        db = FakeDB()
        result = self.result()
        self.assertTrue(save_score(db, 42, result))
        self.assertEqual(len(db.executed), 1)
        sql, params = db.executed[0]
        self.assertTrue(sql.startswith("UPDATE applications SET"))
        self.assertIn("WHERE id = %s", sql)
        self.assertEqual(sql.count("%s"), len(params))
        self.assertEqual(params[-1], 42)
        self.assertEqual(params[0], result["total"])
        self.assertEqual(db.commits, 0)  # the caller owns the transaction

    def test_save_score_never_touches_status_or_flag_columns(self):
        db = FakeDB()
        save_score(db, 1, self.result())
        sql = db.executed[0][0].lower()
        for column in ("status", "duplicate_flag", "duplicate_similarity",
                       "consistency_flag", "consistency_notes", "resume_path"):
            self.assertNotIn(column, sql)

    def test_save_score_skips_unscored_results(self):
        db = FakeDB()
        unscored = score_application(None, make_job(), today=TODAY)
        self.assertFalse(save_score(db, 42, unscored))
        self.assertEqual(db.executed, [])


# ------------------------------------------------------------
# End-to-end with the real parser's extractors
# ------------------------------------------------------------

SAMPLE_RESUME = """Rahul Sharma
rahul@example.com | 9876543210

Education
B.Tech in Computer Science and Engineering, XYZ University 2020 - 2024

Skills
Languages: Python, Java, SQL
Frameworks: Flask
Tools: Git, MySQL

Experience
Backend Intern, Acme Corp  Jun 2023 - Aug 2023
Built REST APIs using Flask and MySQL.

Projects
Recroot - Resume screening platform built with Python, Flask, MySQL and REST APIs.

Certifications
Python for Everybody Specialization
"""


class ParserIntegrationTests(unittest.TestCase):
    def test_real_parser_output_scores_sensibly(self):
        parsed = parsed_from_text(SAMPLE_RESUME)
        job = make_job(
            experience_required=1,
            education_required="B.Tech CSE",
            description="Develop and maintain REST APIs using Python and Flask.",
        )
        result = score(parsed, job)
        c = result["components"]

        self.assertTrue(result["scored"])
        self.assertEqual(result["missing_skills"], [])
        self.assertEqual(c["skills"]["score"], 40)
        self.assertEqual(c["experience"]["score"], 5)       # 3 months / 12 x 20
        self.assertEqual(c["education"]["score"], 15)
        self.assertEqual(c["projects"]["score"], 10)
        self.assertEqual(c["certifications"]["score"], 5)
        self.assertGreaterEqual(c["relevance"]["score"], 7)
        self.assertEqual(result["total"], sum(x["score"] for x in c.values()))

    def test_real_parser_low_text_is_unscored(self):
        parsed = parsed_from_text("Rahul")
        self.assertTrue(parsed["is_low_text"])
        self.assertFalse(score(parsed)["scored"])


if __name__ == "__main__":
    unittest.main()