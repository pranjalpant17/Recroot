-- Recroot — Demo / Seed Data
-- Run AFTER schema.sql (schema.sql drops & recreates all tables,
-- so ids below are predictable on a fresh database).
--
-- !!! FRESH DATABASE ONLY !!!
-- This file inserts rows with explicit ids and never deletes anything.
-- Do not run it on a database that already contains data: it will fail
-- with duplicate-key errors. Use it only right after schema.sql.
--
-- Step 37: updated for the hiring-pipeline schema. Compared with the
-- previous version it adds:
--   * applied_at, stage_changed_at and duplicate_review_status on every
--     application (applied_at is now explicit so history and audit
--     timestamps line up)
--   * application_stage_history rows (one per application, plus
--     Rahul's shortlisting)
--   * duplicate_matches rows (symmetric pairs for applications 5/6
--     and 7/8, three open, one reviewed)
--   * audit actions standardised to snake_case, plus four new audit rows
--
-- Passwords are hashed with Werkzeug's generate_password_hash
-- using method="pbkdf2:sha256", e.g.:
--     from werkzeug.security import generate_password_hash
--     generate_password_hash("admin123", method="pbkdf2:sha256")
-- auth.py should verify with werkzeug.security.check_password_hash,
-- which auto-detects the method from the stored string.
--
-- Plaintext demo passwords (for login testing only):
--   admin@recroot.com       -> admin123
--   pranjal@recroot.com     -> recruiter123
--   piyush@recroot.com      -> recruiter123
--   krish@recroot.com       -> candidate123
--   harshal@recroot.com     -> candidate123
--   rahul@recroot.com       -> candidate123
--   aman@recroot.com        -> candidate123
--   priya@recroot.com       -> candidate123
--   riya@recroot.com        -> candidate123


-- 1. Users

INSERT INTO users (id, name, email, password, role) VALUES
(1, 'Admin',          'admin@recroot.com',    'pbkdf2:sha256:1000000$tp7KVWKzwdG0yS56$d9f3e464c73adbc8ce0e6ee6c1d0c03b6713c200e34566ab7acc1aaa405776b5', 'admin'),
(2, 'Pranjal',        'pranjal@recroot.com',  'pbkdf2:sha256:1000000$nrP7MbxObf8TFxsW$58652c817ca2b7bfb576585f38c827409e43c1d650f74e2aae38b6c6a4a204bc', 'recruiter'),
(3, 'Piyush',         'piyush@recroot.com',   'pbkdf2:sha256:1000000$nrP7MbxObf8TFxsW$58652c817ca2b7bfb576585f38c827409e43c1d650f74e2aae38b6c6a4a204bc', 'recruiter'),
(4, 'Krish',          'krish@recroot.com',    'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate'),
(5, 'Harshal',        'harshal@recroot.com',  'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate'),
(6, 'Rahul Sharma',   'rahul@recroot.com',    'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate'),
(7, 'Aman',           'aman@recroot.com',     'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate'),
(8, 'Priya',          'priya@recroot.com',    'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate'),
(9, 'Riya',           'riya@recroot.com',     'pbkdf2:sha256:1000000$IayE1rUDmcESfayB$31788da3822fab1dddc8006fa8ad7b8f406b5df760fb50b4e60a2139b6168aec', 'candidate');


-- 2. Job Postings
--    Job 1 mirrors the Section 9 scoring example exactly
--    (Python 10 / Flask 8 / MySQL 8 / REST API 8 / Git 6 = 40).
--    Job 2 uses the "Fresher Software Engineer" weighting
--    from Section 11. Job 3 is a closed job to demo status.

INSERT INTO job_postings
    (id, title, description, required_skills, preferred_skills,
     experience_required, education_required, created_by, status, deadline)
VALUES
(1, 'Backend Developer',
    'Build and maintain REST APIs for our recruitment platform using Flask and MySQL.',
    'Python,Flask,MySQL,REST API,Git',
    'Docker,Redis,AWS',
    2, 'B.Tech CSE', 2, 'open', '2026-12-31'),

(2, 'Fresher Software Engineer',
    'Entry-level engineering role for recent graduates. Strong fundamentals and eagerness to learn required.',
    'Python,Data Structures,SQL,Git',
    'Flask,REST API',
    0, 'B.Tech (any branch)', 2, 'open', '2026-11-30'),

(3, 'Data Analyst (Closed)',
    'Analyze recruitment funnel data and build dashboards.',
    'SQL,Python,Excel,Power BI',
    'Tableau',
    1, 'B.Tech / B.Sc', 3, 'closed', '2026-06-30');


-- 3. Applications
--    Job 1 ranking mirrors the Section 12 example:
--    Rahul 91, Aman 84, Priya 76, Riya 63.
--    Krish & Harshal demonstrate exact-duplicate detection.
--
--    Step 37 columns:
--      status                   hiring stage (all values valid in the new ENUM)
--      applied_at               explicit, matches the audit log and the first
--                               application_stage_history row
--      stage_changed_at         time of the latest history row for the application
--      duplicate_review_status  summary of duplicate_matches for the application:
--                                 open     -> at least one open match
--                                 reviewed -> matches exist, all reviewed
--                                 none     -> no matches
--                               Apps 5, 6, 8 = open, app 7 = reviewed.

INSERT INTO applications
    (id, candidate_id, job_id, resume_path, status, stage_changed_at,
     match_score, skill_score, experience_score, education_score,
     project_score, certification_score, relevance_score,
     matched_skills, missing_skills,
     duplicate_flag, duplicate_similarity, duplicate_review_status,
     consistency_flag, consistency_notes, processing_status, processed_at,
     applied_at)
VALUES
-- Rahul: top candidate, no flags (Section 10/12 example)
(1, 6, 1, 'uploads/resumes/rahul_resume_v3.pdf', 'shortlisted', '2026-09-22 10:00:00',
    91, 36, 20, 15, 10, 5, 5,
    'Python,Flask,MySQL,Git', 'REST API',
    FALSE, 0, 'none',
    FALSE, NULL,'done', CURRENT_TIMESTAMP,
    '2026-09-22 08:10:00'),

-- Aman: strong candidate, minor gaps
(2, 7, 1, 'uploads/resumes/aman_resume.pdf', 'pending', '2026-09-21 12:00:00',
    84, 32, 16, 15, 8, 3, 10,
    'Python,Flask,MySQL,REST API', 'Git',
    FALSE, 0, 'none',
    FALSE, NULL,'done', CURRENT_TIMESTAMP,
    '2026-09-21 12:00:00'),

-- Priya: consistency flag (Section 7 profile-vs-resume example)
(3, 8, 1, 'uploads/resumes/priya_resume.pdf', 'pending', '2026-09-21 13:00:00',
    76, 28, 14, 8, 8, 3, 15,
    'Python,MySQL,REST API', 'Flask,Git',
    FALSE, 0, 'none',
    TRUE, 'Profile/resume information differs: profile lists B.Tech CSE, resume lists B.Tech Mechanical Engineering. Recruiter verification recommended.',
    'done', CURRENT_TIMESTAMP,
    '2026-09-21 13:00:00'),

-- Riya: name mismatch (Section 7 example) + lower score
(4, 9, 1, 'uploads/resumes/riya_resume.pdf', 'pending', '2026-09-21 14:00:00',
    63, 24, 10, 8, 6, 0, 15,
    'Python,MySQL', 'Flask,REST API,Git',
    FALSE, 0, 'none',
    TRUE, 'Name mismatch detected: account name is "Riya", resume header reads "Riya Kapoor Verma". Recruiter verification recommended.',
    'done', CURRENT_TIMESTAMP,
    '2026-09-21 14:00:00'),

-- Krish: exact duplicate pair (Section 6, Level 1 example)
(5, 4, 1, 'uploads/resumes/krish_resume.pdf', 'pending', '2026-09-21 15:00:00',
    58, 24, 10, 8, 6, 0, 10,
    'Python,MySQL', 'Flask,REST API,Git',
    TRUE, 100, 'open',
    FALSE, NULL,'done', CURRENT_TIMESTAMP,
    '2026-09-21 15:00:00'),

-- Harshal: identical file hash to Krish's resume (exact duplicate)
(6, 5, 1, 'uploads/resumes/harshal_resume.pdf', 'pending', '2026-09-21 15:05:00',
    58, 24, 10, 8, 6, 0, 10,
    'Python,MySQL', 'Flask,REST API,Git',
    TRUE, 100, 'open',
    FALSE, NULL,'done', CURRENT_TIMESTAMP,
    '2026-09-21 15:05:00'),

-- Aman also applies to the Fresher role (near-duplicate project
-- description vs. Priya below -- Section 6 "four students, same
-- college project" example, NOT flagged as fraud, just similarity).
-- Its similar-match was reviewed by the recruiter -> 'reviewed'.
(7, 7, 2, 'uploads/resumes/aman_resume_fresher.pdf', 'pending', '2026-09-21 12:30:00',
    72, 28, 5, 20, 20, 0, 9,
    'Python,Data Structures,SQL', 'Git',
    TRUE, 94, 'reviewed',
    FALSE, NULL,'done', CURRENT_TIMESTAMP,
    '2026-09-21 12:30:00'),

-- Priya's fresher application: its similar-match is still open.
(8, 8, 2, 'uploads/resumes/priya_resume_fresher.pdf', 'pending', '2026-09-21 13:30:00',
    68, 24, 5, 20, 18, 0, 8,
    'Python,SQL', 'Data Structures,Git',
    TRUE, 94, 'open',
    TRUE, 'Profile/resume information differs: profile lists B.Tech CSE, resume lists B.Tech Mechanical Engineering. Recruiter verification recommended.',
    'done', CURRENT_TIMESTAMP,
    '2026-09-21 13:30:00');


-- 4. Resume Fingerprints
--    Krish & Harshal share an identical file_hash (Level 1 —
--    exact duplicate). Aman & Priya share a near-identical
--    fingerprint but different hashes (Level 2 — near duplicate,
--    94% similarity, same college project).

INSERT INTO resume_fingerprints
    (id, candidate_id, fingerprint, file_hash, resume_path)
VALUES
(1, 6, 'rahul|python,flask,mysql,git|recroot-project|btech-cse-2027',
    'F1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D9E0F1A2',
    'uploads/resumes/rahul_resume_v3.pdf'),

(2, 7, 'aman|python,flask,mysql,restapi|recroot-project|btech-cse-2026',
    '11223344556677889900AABBCCDDEEFF11223344556677889900AABBCCDDEE',
    'uploads/resumes/aman_resume.pdf'),

(3, 8, 'priya|python,mysql,restapi|recroot-project|btech-cse-2026',
    '22334455667788990011BBCCDDEEFF00223344556677889900AABBCCDDEE11',
    'uploads/resumes/priya_resume.pdf'),

(4, 9, 'riya|python,mysql|inventory-mgmt-project|btech-it-2026',
    '33445566778899001122CCDDEEFF001122334455667788990011BBCCDDEE22',
    'uploads/resumes/riya_resume.pdf'),

-- Krish and Harshal: identical hash -> Level 1 exact duplicate
(5, 4, 'krish|python,mysql|recroot-project|btech-cse-2026',
    'ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123AB',
    'uploads/resumes/krish_resume.pdf'),

(6, 5, 'harshal|python,mysql|recroot-project|btech-cse-2026',
    'ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123AB',
    'uploads/resumes/harshal_resume.pdf'),

-- Aman & Priya (fresher-role resumes): near-duplicate, 94% similar
-- text (same college project), different files -> different hashes
(7, 7, 'aman|python,datastructures,sql|recroot-project|btech-cse-2026',
    '44556677889900112233DDEEFF00112233445566778899001122CCDDEE3344',
    'uploads/resumes/aman_resume_fresher.pdf'),

(8, 8, 'priya|python,sql|recroot-project|btech-cse-2026',
    '55667788990011223344EEFF00112233445566778899001122CCDDEE334455',
    'uploads/resumes/priya_resume_fresher.pdf');


-- 5. Resume Versions
--    Rahul: 3 versions, matching the Section 8 example
--    (10 Sept, 15 Sept, 22 Sept).

INSERT INTO resume_versions
    (id, candidate_id, resume_path, file_hash, version_number, created_at)
VALUES
(1, 6, 'uploads/resumes/rahul_resume_v1.pdf',
    'AA11BB22CC33DD44EE55FF66AA11BB22CC33DD44EE55FF66AA11BB22CC33DD',
    1, '2026-09-10 10:15:00'),

(2, 6, 'uploads/resumes/rahul_resume_v2.pdf',
    'BB22CC33DD44EE55FF66AA11BB22CC33DD44EE55FF66AA11BB22CC33DD44EE',
    2, '2026-09-15 09:40:00'),

(3, 6, 'uploads/resumes/rahul_resume_v3.pdf',
    'F1A2B3C4D5E6F7A8B9C0D1E2F3A4B5C6D7E8F9A0B1C2D3E4F5A6B7C8D9E0F1A2',
    3, '2026-09-22 08:05:00'),

(4, 4, 'uploads/resumes/krish_resume.pdf',
    'ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123AB',
    1, '2026-09-12 11:00:00'),

(5, 5, 'uploads/resumes/harshal_resume.pdf',
    'ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123ABC123AB',
    1, '2026-09-13 14:20:00');


-- 6. Audit Log
--    All action names are snake_case. Rows 1-15 are the original rows
--    (rows 13 and 14 were renamed from DUPLICATE_FLAG / CONSISTENCY_FLAG).
--    Rows 16-19 are new in Step 37: they cover the fresher-role applications
--    and the duplicate pair 7/8 so the audit trail matches the stage
--    history and duplicate_matches seed rows.

INSERT INTO audit_log (id, user_id, action, details, timestamp) VALUES
(1, 1, 'login_success',  'Admin logged in.', '2026-09-20 09:00:00'),
(2, 2, 'login_success',  'Recruiter Pranjal logged in.', '2026-09-20 09:05:00'),
(3, 2, 'recruiter_create_job', 'Created job posting "Backend Developer" (id=1).', '2026-09-20 09:10:00'),
(4, 2, 'recruiter_create_job', 'Created job posting "Fresher Software Engineer" (id=2).', '2026-09-20 09:12:00'),
(5, 3, 'recruiter_create_job', 'Created job posting "Data Analyst (Closed)" (id=3).', '2026-09-20 09:20:00'),
(6, 6, 'candidate_upload_resume', 'Rahul Sharma uploaded resume version 3.', '2026-09-22 08:05:00'),
(7, 6, 'candidate_apply', 'Rahul Sharma applied to job id=1 (Backend Developer).', '2026-09-22 08:10:00'),
(8, 7, 'candidate_apply', 'Aman applied to job id=1 (Backend Developer).', '2026-09-21 12:00:00'),
(9, 8, 'candidate_apply', 'Priya applied to job id=1 (Backend Developer).', '2026-09-21 13:00:00'),
(10, 9, 'candidate_apply', 'Riya applied to job id=1 (Backend Developer).', '2026-09-21 14:00:00'),
(11, 4, 'candidate_apply', 'Krish applied to job id=1 (Backend Developer).', '2026-09-21 15:00:00'),
(12, 5, 'candidate_apply', 'Harshal applied to job id=1 (Backend Developer).', '2026-09-21 15:05:00'),
(13, 2, 'duplicate_flag','System flagged applications id=5 and id=6 as exact duplicates (100% match).', '2026-09-21 15:06:00'),
(14, 2, 'consistency_flag','System flagged application id=3 for profile/resume branch mismatch.', '2026-09-21 13:05:00'),
(15, 2, 'recruiter_update_application_status', 'Recruiter Pranjal shortlisted application id=1 (Rahul Sharma).', '2026-09-22 10:00:00'),
(16, 7, 'candidate_apply', 'Aman applied to job id=2 (Fresher Software Engineer).', '2026-09-21 12:30:00'),
(17, 8, 'candidate_apply', 'Priya applied to job id=2 (Fresher Software Engineer).', '2026-09-21 13:30:00'),
(18, 2, 'duplicate_flag','System flagged applications id=7 and id=8 as highly similar (94% text similarity).', '2026-09-21 13:31:00'),
(19, 2, 'recruiter_review_duplicate', 'Recruiter Pranjal marked duplicate match id=3 (application id=7) as reviewed - not an issue.', '2026-09-22 10:30:00');


-- 7. Application Stage History (Step 37)
--    One row per application when it was created (from_status NULL ->
--    pending, actor candidate, time = applied_at), plus Rahul's move to
--    shortlisted (matches audit row 15 and applications.status).
--    The latest changed_at of each application equals its
--    applications.stage_changed_at, and the last to_status equals
--    applications.status.

INSERT INTO application_stage_history
    (id, application_id, from_status, to_status, changed_by, actor_role, note, changed_at)
VALUES
(1, 1, NULL,      'pending',     6, 'candidate', NULL, '2026-09-22 08:10:00'),
(2, 1, 'pending', 'shortlisted', 2, 'recruiter', 'Strong match on required skills.', '2026-09-22 10:00:00'),
(3, 2, NULL,      'pending',     7, 'candidate', NULL, '2026-09-21 12:00:00'),
(4, 3, NULL,      'pending',     8, 'candidate', NULL, '2026-09-21 13:00:00'),
(5, 4, NULL,      'pending',     9, 'candidate', NULL, '2026-09-21 14:00:00'),
(6, 5, NULL,      'pending',     4, 'candidate', NULL, '2026-09-21 15:00:00'),
(7, 6, NULL,      'pending',     5, 'candidate', NULL, '2026-09-21 15:05:00'),
(8, 7, NULL,      'pending',     7, 'candidate', NULL, '2026-09-21 12:30:00'),
(9, 8, NULL,      'pending',     8, 'candidate', NULL, '2026-09-21 13:30:00');


-- 8. Duplicate Matches (Step 37)
--    Symmetric rows, as the worker will write them later: when B matches
--    A's resume, A also gets a row pointing back at B.
--      matches 1 / 2 : applications 5 <-> 6, exact (identical file), 100%, both open
--      matches 3 / 4 : applications 7 <-> 8, similar text, 94%,
--                      match 3 reviewed ("not an issue"), match 4 still open
--    Resulting applications.duplicate_review_status:
--      app 5 = open, app 6 = open, app 7 = reviewed, app 8 = open.

INSERT INTO duplicate_matches
    (id, application_id, matched_candidate_id, matched_resume_path,
     match_type, similarity, review_status,
     reviewed_by, reviewed_at, review_note, detected_at)
VALUES
(1, 5, 5, 'uploads/resumes/harshal_resume.pdf',
    'exact',   100, 'open',
    NULL, NULL, NULL, '2026-09-21 15:06:00'),

(2, 6, 4, 'uploads/resumes/krish_resume.pdf',
    'exact',   100, 'open',
    NULL, NULL, NULL, '2026-09-21 15:06:00'),

(3, 7, 8, 'uploads/resumes/priya_resume_fresher.pdf',
    'similar',  94, 'reviewed_ok',
    2, '2026-09-22 10:30:00', 'Same college project (Recroot), rest of the resumes differ. Reviewed - not an issue.', '2026-09-21 13:31:00'),

(4, 8, 7, 'uploads/resumes/aman_resume_fresher.pdf',
    'similar',  94, 'open',
    NULL, NULL, NULL, '2026-09-21 13:31:00');