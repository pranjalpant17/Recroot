-- =====================================================================
-- Recroot - Step 37: Schema Migration  (Blueprint v2, sections 4.2-4.5)
-- Target: MySQL 8.4, InnoDB, utf8mb4
--
-- WHAT THIS DOES
--   1. applications.status  -> adds 'interview', 'offer', 'hired'
--                              (pending, shortlisted, rejected, withdrawn kept)
--   2. applications.stage_changed_at          (new column)
--   3. applications.duplicate_review_status   (new column)
--   4. CREATE TABLE application_stage_history (table 7)
--   5. CREATE TABLE duplicate_matches          (table 8)
--   6. idx_applications_job_status, idx_stage_history_app, idx_dup_app_review
--
-- SAFETY
--   * No DROP TABLE / DROP COLUMN / DROP INDEX / TRUNCATE / DELETE.
--   * Re-runnable: every step checks information_schema first and is
--     skipped if it is already applied.
--   * The only data-touching statement is a one-time fill of the NEW,
--     empty column stage_changed_at (from applied_at). No existing value
--     is changed.
--   * Does not insert history rows or duplicate matches (see notes in
--     the hand-off message; those belong to steps 38 and 40).
--
-- RUN
--   Stop the Flask app / worker first, take a backup, then:
--     mysql -u <user> -p <db_name> < migration_step37.sql
--   Do NOT use --force: the script should stop at the first error.
-- =====================================================================


-- ---------------------------------------------------------------------
-- 1. applications.status: add interview / offer / hired
--    The ENUM is re-declared in blueprint order. MySQL converts existing
--    rows by their string value, so every old status is preserved.
--    The table is rebuilt (needed because values are inserted before
--    'rejected'), which is quick at this data size.
--    If any row has status NULL, the NOT NULL change fails and, because
--    DDL is atomic in MySQL 8, nothing is changed.
-- ---------------------------------------------------------------------
SET @needs_status_change = (
    SELECT COUNT(*) = 0
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME   = 'applications'
      AND COLUMN_NAME  = 'status'
      AND IS_NULLABLE  = 'NO'
      AND COLUMN_TYPE  = 'enum(''pending'',''shortlisted'',''interview'',''offer'',''hired'',''rejected'',''withdrawn'')'
);

SET @sql = IF(@needs_status_change = 1,
    'ALTER TABLE applications MODIFY COLUMN status ENUM(''pending'',''shortlisted'',''interview'',''offer'',''hired'',''rejected'',''withdrawn'') NOT NULL DEFAULT ''pending''',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- ---------------------------------------------------------------------
-- 2. applications.stage_changed_at
--    Existing rows are filled once with applied_at, because it is the
--    best available "last changed" time. This runs only in the run that
--    adds the column, and only touches rows where the new column is NULL.
-- ---------------------------------------------------------------------
SET @add_stage_col = (
    SELECT COUNT(*) = 0
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME   = 'applications'
      AND COLUMN_NAME  = 'stage_changed_at'
);

SET @sql = IF(@add_stage_col = 1,
    'ALTER TABLE applications ADD COLUMN stage_changed_at TIMESTAMP NULL DEFAULT NULL AFTER status',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = IF(@add_stage_col = 1,
    'UPDATE applications SET stage_changed_at = applied_at WHERE id > 0 AND stage_changed_at IS NULL',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- ---------------------------------------------------------------------
-- 3. applications.duplicate_review_status
--    Existing rows receive the default 'none'. The value is derived from
--    duplicate_matches, which is still empty after this migration, so
--    'none' is the consistent value until matches are written.
-- ---------------------------------------------------------------------
SET @add_dup_col = (
    SELECT COUNT(*) = 0
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME   = 'applications'
      AND COLUMN_NAME  = 'duplicate_review_status'
);

SET @sql = IF(@add_dup_col = 1,
    'ALTER TABLE applications ADD COLUMN duplicate_review_status ENUM(''none'',''open'',''reviewed'') NOT NULL DEFAULT ''none'' AFTER duplicate_similarity',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- ---------------------------------------------------------------------
-- 4. Table 7: application_stage_history  (blueprint 4.3, verbatim)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS application_stage_history (
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


-- ---------------------------------------------------------------------
-- 5. Table 8: duplicate_matches  (blueprint 4.4, verbatim)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS duplicate_matches (
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


-- ---------------------------------------------------------------------
-- 6. New indexes (blueprint 4.5). Existing indexes are left untouched,
--    including idx_applications_job.
-- ---------------------------------------------------------------------
SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME   = 'applications'
        AND INDEX_NAME   = 'idx_applications_job_status') = 0,
    'CREATE INDEX idx_applications_job_status ON applications(job_id, status)',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME   = 'application_stage_history'
        AND INDEX_NAME   = 'idx_stage_history_app') = 0,
    'CREATE INDEX idx_stage_history_app ON application_stage_history(application_id, changed_at)',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @sql = IF(
    (SELECT COUNT(*) FROM information_schema.STATISTICS
      WHERE TABLE_SCHEMA = DATABASE()
        AND TABLE_NAME   = 'duplicate_matches'
        AND INDEX_NAME   = 'idx_dup_app_review') = 0,
    'CREATE INDEX idx_dup_app_review ON duplicate_matches(application_id, review_status)',
    'DO 0');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;


-- ---------------------------------------------------------------------
-- 7. Read-only verification (changes nothing)
-- ---------------------------------------------------------------------
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME   = 'applications'
  AND COLUMN_NAME IN ('status', 'stage_changed_at', 'duplicate_review_status');

SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) AS index_columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE()
  AND INDEX_NAME IN ('idx_applications_job_status', 'idx_stage_history_app',
                     'idx_dup_app_review', 'uq_dup_pair')
GROUP BY TABLE_NAME, INDEX_NAME;

SELECT status, COUNT(*) AS applications
FROM applications
GROUP BY status;