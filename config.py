"""
Recroot — Application Configuration

Loads settings from environment variables (with sane local-dev
defaults) so the same code works in dev, testing, and production
just by changing the environment.

Usage in app.py:

    from config import get_config
    app.config.from_object(get_config())

Or, if a specific environment is needed explicitly:

    from config import DevelopmentConfig
    app.config.from_object(DevelopmentConfig)
"""

import os

# Load a local .env file in development if python-dotenv is installed.
# (Add `python-dotenv` to requirements.txt if you want this — it's
# optional; the app still works with plain environment variables.)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Base configuration shared by every environment."""

    # ------------------------------------------------------------
    # Flask core
    # ------------------------------------------------------------
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

    # ------------------------------------------------------------
    # MySQL connection
    # (used directly by modules that open raw DB connections, e.g.
    #  via mysql-connector-python or PyMySQL — no ORM in this project)
    # ------------------------------------------------------------
    MYSQL_HOST = os.environ.get("MYSQL_HOST", "localhost")
    MYSQL_PORT = int(os.environ.get("MYSQL_PORT", 3306))
    MYSQL_USER = os.environ.get("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "7668082065")
    MYSQL_DB = os.environ.get("MYSQL_DB", "recroot")

    # Plain dict form, handy for mysql.connector.connect(**DB_CONFIG)
    # or pymysql.connect(**DB_CONFIG)
    DB_CONFIG = {
        "host": MYSQL_HOST,
        "port": MYSQL_PORT,
        "user": MYSQL_USER,
        "password": MYSQL_PASSWORD,
        "database": MYSQL_DB,
        "autocommit": False,
    }

    # ------------------------------------------------------------
    # Sessions / auth
    # ------------------------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = int(
        os.environ.get("SESSION_LIFETIME_SECONDS", 60 * 60 * 8)  # 8 hours
    )

    # Password hashing method, used by modules/auth.py with
    # werkzeug.security.generate_password_hash / check_password_hash.
    # Must match the method used to generate the hashes in seed.sql.
    PASSWORD_HASH_METHOD = "pbkdf2:sha256"

    # ------------------------------------------------------------
    # File uploads (resumes)
    # ------------------------------------------------------------
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads", "resumes")
    ALLOWED_RESUME_EXTENSIONS = {"pdf"}
    MAX_CONTENT_LENGTH = int(
        os.environ.get("MAX_UPLOAD_MB", 10)
    ) * 1024 * 1024  # 10 MB default, enforced by Flask on the whole request

    # ------------------------------------------------------------
    # Background worker (modules/worker.py)
    # ------------------------------------------------------------
    WORKER_THREAD_POOL_SIZE = int(os.environ.get("WORKER_THREAD_POOL_SIZE", 4))
    WORKER_QUEUE_MAX_SIZE = int(os.environ.get("WORKER_QUEUE_MAX_SIZE", 100))
    # "fcfs" or "priority" (deadline-based); switchable at run time from
    # /admin/scheduler, this is the value used at server start.
    WORKER_SCHEDULING_ALGORITHM = os.environ.get("WORKER_SCHEDULING_ALGORITHM", "fcfs")
    # DEMO ONLY. Seconds (0-60) that waiting applications may accumulate before
    # a worker picks the next one, so FCFS/Priority ordering becomes visible
    # even when workers are idle. 0 (the default) disables it: every
    # application starts as soon as a worker is free. Adds up to this much
    # latency to every application while enabled.
    WORKER_DISPATCH_WINDOW_SECONDS = os.environ.get("WORKER_DISPATCH_WINDOW_SECONDS", "0")

    # ------------------------------------------------------------
    # Duplicate detection (modules/duplicate_detector.py)
    # ------------------------------------------------------------
    # Similarity threshold (%) above which a near-duplicate flag is
    # raised. Exact duplicates (SHA-256 match) are always flagged.
    NEAR_DUPLICATE_THRESHOLD = int(os.environ.get("NEAR_DUPLICATE_THRESHOLD", 85))

    # ------------------------------------------------------------
    # Scoring engine defaults (modules/scoring_engine.py)
    # Matches Section 9 of the blueprint. Recruiters can override
    # these per job (Section 11 — Configurable Scoring), but these
    # are the system defaults and must always sum to 100.
    # ------------------------------------------------------------
    DEFAULT_SCORE_WEIGHTS = {
        "skills": 40,
        "experience": 20,
        "education": 15,
        "projects": 10,
        "certifications": 5,
        "relevance": 10,
    }

    # ------------------------------------------------------------
    # Pagination (dashboards / listings)
    # ------------------------------------------------------------
    CANDIDATES_PER_PAGE = int(os.environ.get("CANDIDATES_PER_PAGE", 20))
    JOBS_PER_PAGE = int(os.environ.get("JOBS_PER_PAGE", 20))
    AUDIT_LOG_PER_PAGE = int(os.environ.get("AUDIT_LOG_PER_PAGE", 50))

    DEBUG = False
    TESTING = False


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class TestingConfig(Config):
    TESTING = True
    DEBUG = True
    MYSQL_DB = os.environ.get("MYSQL_TEST_DB", "recroot_test")
    DB_CONFIG = {**Config.DB_CONFIG, "database": MYSQL_DB}
    WTF_CSRF_ENABLED = False


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True

    def __init__(self):
        if self.SECRET_KEY == "dev-secret-key-change-me":
            raise RuntimeError(
                "SECRET_KEY must be set via environment variable in production."
            )


CONFIG_BY_NAME = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def get_config():
    """Return the config class selected by the FLASK_ENV env var."""
    env = os.environ.get("FLASK_ENV", "default")
    return CONFIG_BY_NAME.get(env, DevelopmentConfig)