"""
Standalone tests for modules/consistency_checker.py (Step 31).

No database, no Flask, no pytest needed. Run from the project root:

    python -m unittest tests.test_consistency_checker -v

or directly:

    python tests/test_consistency_checker.py

`today` is fixed to 8 Oct 2026 so every date-dependent result is repeatable.
"""

import os
import re
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.consistency_checker import (  # noqa: E402
    OVERLAP_MONTHS_THRESHOLD,
    application_consistency_fields,
    check_consistency,
    extract_date_ranges,
)

TODAY = date(2026, 10, 8)

ACCOUNT = {"name": "Rahul Sharma", "email": "rahul@recroot.com"}

# Words that would imply a conclusion about the candidate (Sections 7, 18).
_ACCUSATORY = re.compile(
    r"\b(fake|fraud\w*|false|fals\w+|lie|lies|lying|liar|fabricat\w*|forg\w*|"
    r"dishonest\w*|cheat\w*)\b",
    re.IGNORECASE,
)


def make_parsed(name="Rahul Sharma", email="rahul@recroot.com",
                education="", experience="", resume_text=None, is_low_text=False):
    """Build a dict shaped like resume_parser.parse_resume() output."""
    if resume_text is None:
        resume_text = f"{name}\n{email}\nEducation\n{education}\nExperience\n{experience}"
    return {
        "name": name, "email": email, "phone": None,
        "education": education, "skills": [], "experience": experience,
        "projects": "", "certifications": [], "resume_text": resume_text,
        "is_low_text": is_low_text,
    }


def run(parsed, profile=ACCOUNT):
    return check_consistency(profile, parsed, today=TODAY)


def codes(result, severity=None):
    return [f["code"] for f in result["findings"]
            if severity is None or f["severity"] == severity]


# ============================================================
# Name vs account
# ============================================================

class NameChecks(unittest.TestCase):

    def test_identical_name_no_finding(self):
        r = run(make_parsed())
        self.assertEqual(r["findings"], [])
        self.assertFalse(r["flag"])

    def test_blueprint_name_mismatch_is_flagged(self):
        # Blueprint Section 7: Account "Rahul Sharma" vs Resume "Amit Kumar".
        r = run(make_parsed(name="Amit Kumar"))
        self.assertEqual(codes(r, "notice"), ["name_mismatch"])
        self.assertTrue(r["flag"])
        self.assertEqual(r["label"], "Potential inconsistency detected")

    def test_case_and_accent_differences_match(self):
        self.assertEqual(codes(run(make_parsed(name="RAHUL SHARMA"))), [])
        self.assertEqual(codes(run(make_parsed(name="Rāhul Sharma"))), [])

    def test_middle_name_matches(self):
        self.assertEqual(codes(run(make_parsed(name="Rahul Kumar Sharma"))), [])

    def test_initial_matches(self):
        self.assertEqual(codes(run(make_parsed(name="R. Sharma"))), [])

    def test_swapped_order_matches(self):
        self.assertEqual(codes(run(make_parsed(name="Sharma Rahul"))), [])

    def test_small_typo_matches(self):
        self.assertEqual(codes(run(make_parsed(name="Rahul Sharmaa"))), [])

    def test_honorific_ignored(self):
        self.assertEqual(codes(run(make_parsed(name="Mr. Rahul Sharma"))), [])

    def test_partial_overlap_is_info_only(self):
        # Same first name, different surname (e.g. marriage): not flagged.
        r = run(make_parsed(name="Rahul Kumar"))
        self.assertEqual(codes(r), ["name_partial"])
        self.assertFalse(r["flag"])

    def test_similar_but_different_first_names_are_not_conflated(self):
        r = run(make_parsed(name="Amir Verma"), {"name": "Amit Verma", "email": None})
        self.assertNotIn("name_mismatch", codes(r, "notice"))  # surname matches
        self.assertEqual(codes(r), ["name_partial"])

    def test_parser_picked_wrong_line_but_name_is_in_header(self):
        # extract_name() can return "Curriculum Vitae"; the account name is
        # still right there in the header, so nothing is raised.
        parsed = make_parsed(
            name="Curriculum Vitae",
            resume_text="Curriculum Vitae\nRahul Sharma\nrahul@recroot.com\n",
        )
        r = run(parsed)
        self.assertEqual(r["findings"], [])
        self.assertFalse(r["flag"])

    def test_no_name_extracted_and_not_in_header_is_skipped(self):
        parsed = make_parsed(name=None, resume_text="Some text without the name\n" * 5)
        r = run(parsed)
        self.assertNotIn("name_mismatch", codes(r))
        self.assertIn("name", r["skipped_checks"])
        self.assertFalse(r["flag"])

    def test_missing_account_name_is_skipped(self):
        r = run(make_parsed(name="Amit Kumar"), {"name": "", "email": "rahul@recroot.com"})
        self.assertEqual(codes(r), [])
        self.assertIn("name", r["skipped_checks"])


# ============================================================
# Email vs account
# ============================================================

class EmailChecks(unittest.TestCase):

    def test_same_email_ignoring_case(self):
        self.assertEqual(codes(run(make_parsed(email="RAHUL@Recroot.com"))), [])

    def test_different_email_is_info_not_flag(self):
        r = run(make_parsed(email="rahul.sharma@gmail.com"))
        self.assertEqual(codes(r), ["email_differs"])
        self.assertFalse(r["flag"])
        self.assertEqual(r["label"], "No inconsistencies detected")
        self.assertIn("informational note", r["explanation"])

    def test_missing_resume_email_is_skipped(self):
        r = run(make_parsed(email=None))
        self.assertEqual(codes(r), [])
        self.assertIn("email", r["skipped_checks"])


# ============================================================
# Date-range extraction
# ============================================================

class DateRangeExtraction(unittest.TestCase):

    def test_year_only(self):
        (r,) = extract_date_ranges("B.Tech CSE\n2023 - 2027")
        self.assertEqual((r.start_year, r.end_year), (2023, 2027))
        self.assertIsNone(r.start_month)
        self.assertFalse(r.is_present)

    def test_en_dash_and_month_names(self):
        (r,) = extract_date_ranges("Jan 2021 \u2013 Mar 2023")
        self.assertEqual((r.start_year, r.start_month, r.end_year, r.end_month),
                         (2021, 1, 2023, 3))

    def test_present_variants(self):
        for text in ("Jan 2021 - Present", "2021 to present", "Sept. 2021 - Till date",
                     "2021 \u2014 Current", "March 2020 - ongoing"):
            (r,) = extract_date_ranges(text)
            self.assertTrue(r.is_present, text)

    def test_numeric_month_format(self):
        (r,) = extract_date_ranges("06/2021 - 08/2022")
        self.assertEqual((r.start_year, r.start_month, r.end_year, r.end_month),
                         (2021, 6, 2022, 8))

    def test_two_digit_end_year(self):
        (r,) = extract_date_ranges("B.Tech 2019-23")
        self.assertEqual((r.start_year, r.end_year), (2019, 2023))

    def test_two_digit_end_that_is_not_a_year_is_ignored(self):
        self.assertEqual(extract_date_ranges("2021 - 10 projects completed"), [])

    def test_cgpa_and_percentages_are_not_dates(self):
        self.assertEqual(extract_date_ranges("CGPA 8.5/10, 85.2% in 12th, rank 1200-1300"), [])

    def test_multiple_ranges_report_line_numbers(self):
        ranges = extract_date_ranges("Dev, Acme\n2020 - 2022\nDev, Beta\n2022 - Present")
        self.assertEqual([r.line_no for r in ranges], [1, 3])

    def test_range_wrapped_after_dash(self):
        (r,) = extract_date_ranges("2019 -\n2023")
        self.assertEqual((r.start_year, r.end_year), (2019, 2023))

    def test_years_on_separate_bullets_are_not_joined(self):
        self.assertEqual(extract_date_ranges("Graduated 2021\n- 2022 award winner"), [])


# ============================================================
# Timeline checks
# ============================================================

class TimelineChecks(unittest.TestCase):

    def test_blueprint_timeline_example_is_flagged(self):
        # Blueprint Section 7: education 2023-2027, full-time SWE 2021-2026.
        r = run(make_parsed(
            education="B.Tech CSE\n2023 - 2027",
            experience="Software Engineer, Acme Corp (Full-time)\n2021 - 2026",
        ))
        self.assertEqual(codes(r, "notice"), ["timeline_overlap"])
        self.assertTrue(r["flag"])
        self.assertIn("verification", " ".join(application_consistency_fields(r)[1].split()))

    def test_internship_during_degree_is_not_flagged(self):
        r = run(make_parsed(
            education="B.Tech CSE\n2020 - 2024",
            experience="Software Engineer Intern, Acme\nJan 2022 - Dec 2023",
        ))
        self.assertEqual(codes(r, "notice"), [])

    def test_next_entrys_intern_title_does_not_hide_a_full_time_job(self):
        # Regression: found by running the checker on real parser output.
        # The line below the job's dates is the NEXT entry's title
        # ("... Intern ..."), which must not be attributed to this job.
        r = run(make_parsed(
            education="B.Tech\n2019 - 2023",
            experience=("Software Engineer, Acme Corp\nAug 2020 - Present\n"
                        "Software Engineer Intern, Beta Labs\nJan 2022 - Jun 2022"),
        ))
        self.assertEqual(codes(r, "notice"), ["timeline_overlap"])

    def test_intern_entry_after_a_full_time_job_is_still_skipped(self):
        # Same resume as above: the intern entry itself must not be flagged,
        # so there is exactly one overlap finding (the full-time job).
        r = run(make_parsed(
            education="B.Tech\n2019 - 2023",
            experience=("Software Engineer Intern, Beta Labs\nJan 2019 - Dec 2022\n"
                        "Software Engineer, Acme Corp\nJun 2023 - Present"),
        ))
        self.assertEqual(codes(r, "notice"), [])

    def test_title_below_dates_for_first_entry(self):
        r = run(make_parsed(
            education="B.Tech\n2020 - 2024",
            experience="Jan 2022 - Dec 2023\nSoftware Engineer Intern, Acme",
        ))
        self.assertEqual(codes(r, "notice"), [])

    def test_part_time_and_freelance_not_flagged(self):
        for label in ("Part-time Web Developer", "Freelance Designer"):
            r = run(make_parsed(
                education="B.Tech\n2020 - 2024",
                experience=f"{label}\n2021 - 2024",
            ))
            self.assertEqual(codes(r, "notice"), [], label)

    def test_job_starting_same_year_degree_ends_is_not_flagged(self):
        # 2019-2023 degree, job from 2023: year-only dates must not cause a
        # false alarm (the degree may have ended in May, the job begun in Aug).
        r = run(make_parsed(
            education="B.Tech\n2019 - 2023",
            experience="Software Engineer\n2023 - Present",
        ))
        self.assertEqual(r["findings"], [])

    def test_month_precise_dates_after_graduation_not_flagged(self):
        r = run(make_parsed(
            education="B.Tech\nAug 2019 - May 2023",
            experience="Software Engineer\nJun 2023 - Present",
        ))
        self.assertEqual(r["findings"], [])

    def test_short_overlap_below_threshold_not_flagged(self):
        r = run(make_parsed(
            education="B.Tech 2019-23",
            experience="Developer\n2022-24",
        ))
        self.assertEqual(codes(r, "notice"), [])

    def test_two_digit_years_overlap_is_flagged(self):
        r = run(make_parsed(
            education="B.Tech 2019-23",
            experience="Developer\n2020-24",
        ))
        self.assertEqual(codes(r, "notice"), ["timeline_overlap"])

    def test_threshold_constant_is_twelve_months(self):
        self.assertEqual(OVERLAP_MONTHS_THRESHOLD, 12)

    def test_reversed_range_is_flagged(self):
        r = run(make_parsed(experience="Developer\n2025 - 2021"))
        self.assertEqual(codes(r, "notice"), ["timeline_reversed"])

    def test_future_dated_experience_is_flagged(self):
        r = run(make_parsed(experience="Developer\n2027 - 2028"))
        self.assertEqual(codes(r, "notice"), ["timeline_future"])

    def test_ended_in_future_without_present_is_flagged(self):
        r = run(make_parsed(experience="Developer\n2025 - 2027"))
        self.assertEqual(codes(r, "notice"), ["timeline_future"])

    def test_current_job_is_not_future(self):
        r = run(make_parsed(experience="Developer\nJan 2025 - Present"))
        self.assertEqual(r["findings"], [])

    def test_expected_future_graduation_is_fine(self):
        r = run(make_parsed(education="B.Tech CSE\n2023 - 2027"))
        self.assertEqual(r["findings"], [])

    def test_overlapping_jobs_are_info_only(self):
        r = run(make_parsed(
            experience="Developer, A\nJan 2020 - Dec 2022\nDeveloper, B\nJun 2021 - Dec 2022",
        ))
        self.assertEqual(codes(r), ["job_overlap"])
        self.assertFalse(r["flag"])

    def test_no_dates_means_no_timeline_findings(self):
        r = run(make_parsed(education="B.Tech CSE", experience="Worked on Flask apps"))
        self.assertEqual(r["findings"], [])


# ============================================================
# Status handling
# ============================================================

class StatusHandling(unittest.TestCase):

    def test_parse_failure_is_not_flagged(self):
        r = run(None)
        self.assertEqual(r["status"], "not_checked")
        self.assertFalse(r["flag"])
        self.assertEqual(r["findings"], [])

    def test_low_text_is_not_flagged(self):
        r = run(make_parsed(name="Amit Kumar", is_low_text=True))
        self.assertEqual(r["status"], "skipped_low_text")
        self.assertFalse(r["flag"])
        self.assertEqual(r["findings"], [])

    def test_missing_profile_does_not_crash(self):
        r = check_consistency(None, make_parsed(), today=TODAY)
        self.assertFalse(r["flag"])

    def test_defaults_to_real_today(self):
        r = check_consistency(ACCOUNT, make_parsed())
        self.assertEqual(r["status"], "checked")

    def test_multiple_notices_are_counted(self):
        r = run(make_parsed(name="Amit Kumar", experience="Developer\n2025 - 2021"))
        self.assertEqual(r["notice_count"], 2)
        self.assertIn("2 potential inconsistencies", r["explanation"])


# ============================================================
# applications-table mapping
# ============================================================

class ApplicationFields(unittest.TestCase):

    def test_flagged_result_maps_to_true_and_notes(self):
        r = run(make_parsed(name="Amit Kumar"))
        flag, notes = application_consistency_fields(r)
        self.assertTrue(flag)
        self.assertIn("- Name:", notes)
        self.assertIn("recruiter verification recommended", notes)

    def test_info_findings_are_not_put_in_notes(self):
        r = run(make_parsed(email="other@gmail.com"))
        self.assertEqual(application_consistency_fields(r), (False, None))

    def test_clean_result_maps_to_false_none(self):
        self.assertEqual(application_consistency_fields(run(make_parsed())), (False, None))

    def test_skipped_result_keeps_explanation_in_notes(self):
        flag, notes = application_consistency_fields(run(make_parsed(is_low_text=True)))
        self.assertFalse(flag)
        self.assertIn("skipped", notes)

    def test_failed_parse_keeps_explanation_in_notes(self):
        flag, notes = application_consistency_fields(run(None))
        self.assertFalse(flag)
        self.assertIn("not run", notes)


# ============================================================
# Wording (Sections 7, 17, 18: never accuse)
# ============================================================

class NeutralWording(unittest.TestCase):

    def test_no_accusatory_words_anywhere(self):
        scenarios = [
            run(None),
            run(make_parsed(is_low_text=True)),
            run(make_parsed()),
            run(make_parsed(name="Amit Kumar")),
            run(make_parsed(name="Rahul Kumar", email="x@gmail.com")),
            run(make_parsed(education="B.Tech\n2023 - 2027",
                            experience="Engineer\n2021 - 2026")),
            run(make_parsed(experience="Dev\n2025 - 2021\nDev\n2027 - 2028")),
            run(make_parsed(experience="Dev, A\nJan 2020 - Dec 2022\nDev, B\nJun 2021 - Dec 2022")),
        ]
        for result in scenarios:
            texts = [result["label"], result["explanation"]]
            texts += [f["message"] for f in result["findings"]]
            texts.append(application_consistency_fields(result)[1] or "")
            for text in texts:
                self.assertIsNone(_ACCUSATORY.search(text), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)