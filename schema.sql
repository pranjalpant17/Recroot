-- Recroot — Database Schema
-- MySQL 8.x (tested target: 8.4)
--
-- !!! WARNING — DESTRUCTIVE !!!
-- This file is for FRESH-DATABASE INITIALIZATION ONLY.
-- It DROPS and RECREATES every Recroot table, permanently deleting all
-- existing data. Never run it against a database you want to keep.
-- To upgrade an existing database in place, use migration_step37.sql.
--
-- Order of creation respects foreign-key dependencies:
--   users
--   job_postings              (FK -> users)
--   applications              (FK -> users, job_postings)
--   resume_fingerprints       (FK -> users)
--   resume_versions           (FK -> users)
--   audit_log                 (FK -> users)
--   application_stage_history (FK -> applications, users)   [Step 37]
--   duplicate_matches         (FK -> applications, users)   [Step 37]

-- Clean rebuild support (safe drop order = reverse of creation)
DROP TABLE IF EXISTS duplicate_matches;
DROP TABLE IF EXISTS application_stage_history;
DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS resume_versions;
DROP TABLE IF EXISTS resume_fingerprints;
DROP TABLE IF EXISTS applications;
DROP TABLE IF EXISTS job_postings;
DROP TABLE IF EXISTS users;

-- Table 1: Users

CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) UNIQUE NOT NULL,
    password VARCHAR(256) NOT NULL,
    role ENUM('admin','recruiter','candidate') NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 2: Job Postings
CREATE TABLE job_postings (
    id INT AUTO_INCREMENT PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    required_skills TEXT,
    preferred_skills TEXT,
    experience_required INT DEFAULT 0,
    education_required VARCHAR(200),
    created_by INT,
    status ENUM('open','closed') DEFAULT 'open',
    deadline DATE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id)
        ON DELETE SET NULL
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 3: Applications
CREATE TABLE applications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    candidate_id INT,
    job_id INT,
    resume_path VARCHAR(500),

    -- Hiring stage (people-owned). rejected and withdrawn are exits.
    -- 'withdrawn' is set by candidate.withdraw_application().
    -- interview / offer / hired added in Step 37.
    status ENUM('pending','shortlisted','interview','offer','hired','rejected','withdrawn')
        NOT NULL DEFAULT 'pending',
    -- Time of the last stage change (Step 37).
    stage_changed_at TIMESTAMP NULL DEFAULT NULL,

    match_score INT DEFAULT 0,
    skill_score INT DEFAULT 0,
    experience_score INT DEFAULT 0,
    education_score INT DEFAULT 0,
    project_score INT DEFAULT 0,
    certification_score INT DEFAULT 0,
    relevance_score INT DEFAULT 0,

    matched_skills TEXT,
    missing_skills TEXT,

    duplicate_flag BOOLEAN DEFAULT FALSE,
    duplicate_similarity INT DEFAULT 0,
    -- Summary of the duplicate review state, derived from duplicate_matches
    -- (Step 37). duplicate_flag remains the detector's raw result.
    duplicate_review_status ENUM('none','open','reviewed') NOT NULL DEFAULT 'none',

    consistency_flag BOOLEAN DEFAULT FALSE,
    consistency_notes TEXT,

    -- Step 33: background processing state (written only by modules/worker.py -
    -- 'status' above remains the recruiter/candidate decision and is never
    -- touched by the worker). New applications start 'queued'.
    processing_status ENUM('queued','processing','done','failed')
        NOT NULL DEFAULT 'queued',
    processing_note VARCHAR(500) NULL,
    processed_at TIMESTAMP NULL DEFAULT NULL,

    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (candidate_id) REFERENCES users(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,
    FOREIGN KEY (job_id) REFERENCES job_postings(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE,

    -- A candidate should only have one application per job
    UNIQUE KEY uq_candidate_job (candidate_id, job_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 4: Resume Fingerprints
-- Used for exact and near-duplicate resume detection.
CREATE TABLE resume_fingerprints (
    id INT AUTO_INCREMENT PRIMARY KEY,
    candidate_id INT,
    fingerprint TEXT NOT NULL,
    file_hash VARCHAR(64),
    resume_path VARCHAR(500),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES users(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 5: Resume Versions
-- Stores different versions of a candidate's resume.
CREATE TABLE resume_versions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    candidate_id INT,
    resume_path VARCHAR(500) NOT NULL,
    file_hash VARCHAR(64),
    version_number INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES users(id)
        ON DELETE CASCADE
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 6: Audit Log
CREATE TABLE audit_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT,
    action VARCHAR(100) NOT NULL,
    details TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
        ON DELETE SET NULL
        ON UPDATE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 7: Application Stage History (Step 37)
-- One row per stage change; the audit trail of the hiring pipeline.
-- from_status is NULL for the first row (application created -> pending).
CREATE TABLE application_stage_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    application_id INT NOT NULL,
    from_status ENUM('pending','shortlisted','interview','offer','hired','rejected','withdrawn') NULL,
    to_status ENUM('pending','shortlisted','interview','offer','hired','rejected','withdrawn') NOT NULL,
    changed_by INT NULL,
    actor_role ENUM('recruiter','candidate','system') NOT NULL,
    note VARCHAR(500) NULL,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
    FOREIGN KEY (changed_by) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Table 8: Duplicate Matches (Step 37)
-- Which resumes a flagged application matched, for recruiter review.
-- The unique key makes re-processing safe (upsert keeps review state).
CREATE TABLE duplicate_matches (
    id INT AUTO_INCREMENT PRIMARY KEY,
    application_id INT NOT NULL,
    matched_candidate_id INT NOT NULL,
    matched_resume_path VARCHAR(500) NOT NULL,
    match_type ENUM('exact','similar') NOT NULL,
    similarity INT NOT NULL,
    review_status ENUM('open','reviewed_ok') NOT NULL DEFAULT 'open',
    reviewed_by INT NULL,
    reviewed_at TIMESTAMP NULL,
    review_note VARCHAR(500) NULL,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
    FOREIGN KEY (matched_candidate_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE SET NULL,
    UNIQUE KEY uq_dup_pair (application_id, matched_candidate_id, matched_resume_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Indexes
CREATE INDEX idx_applications_job
ON applications(job_id);

CREATE INDEX idx_applications_candidate
ON applications(candidate_id);

CREATE INDEX idx_applications_score
ON applications(match_score DESC);

CREATE INDEX idx_applications_processing
ON applications(processing_status);

-- Step 37: kanban board columns (job + stage)
CREATE INDEX idx_applications_job_status
ON applications(job_id, status);

CREATE INDEX idx_audit_timestamp
ON audit_log(timestamp);

CREATE INDEX idx_jobs_status
ON job_postings(status);

CREATE INDEX idx_resume_hash
ON resume_fingerprints(file_hash);

-- Step 37: stage timeline and duplicate review lookups
CREATE INDEX idx_stage_history_app
ON application_stage_history(application_id, changed_at);

CREATE INDEX idx_dup_app_review
ON duplicate_matches(application_id, review_status);