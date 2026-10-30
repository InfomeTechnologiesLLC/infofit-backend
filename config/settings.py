"""Django settings for INFOME FIT. All secrets come from environment variables."""
from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
    CSRF_TRUSTED_ORIGINS=(list, []),
    TRUST_PROXY_SSL_HEADER=(bool, False),
    JWT_ACCESS_MINUTES=(int, 15),
    JWT_REFRESH_DAYS=(int, 14),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "accounts",
    "fitness",
    "challenges",
    "community",
    "ai_service",
    "notifications",
    "nutrition",
    "wellness",
    "automations",
    "assistant",
    "portal",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "portal.context_processors.nav_counts",
                "portal.context_processors.brand",
            ]
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}
if DATABASES["default"]["ENGINE"] == "django.db.backends.mysql":
    DATABASES["default"]["OPTIONS"] = {
        "charset": "utf8mb4",
        "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
    }

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    # No "too similar to your email" check: the company allows a password like the email address.
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"  # store UTC; clients convert to local time
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {"anon": "30/min", "user": "300/min", "login": "10/min", "register": "10/hour", "ai": "10/min", "nutrition": "20/min"},
    "EXCEPTION_HANDLER": "config.api_errors.api_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env("JWT_ACCESS_MINUTES")),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env("JWT_REFRESH_DAYS")),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
}

CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-2.5-flash-lite")
GEMINI_VISION_MODEL = env("GEMINI_VISION_MODEL", default="gemini-2.5-flash-lite")
# Anyone with the app can create an ordinary employee account. Set False to make HR create every account.
ALLOW_SELF_REGISTRATION = env.bool("ALLOW_SELF_REGISTRATION", default=True)
# Hold community posts that look harmful for staff review before they appear (falls back to simple rules offline).
COMMUNITY_AI_MODERATION = env.bool("COMMUNITY_AI_MODERATION", default=True)
# USDA's DEMO_KEY is suitable only for local evaluation; deployed servers need their own key.
USDA_FDC_API_KEY = env("USDA_FDC_API_KEY", default="DEMO_KEY" if DEBUG else "")
FIREBASE_CREDENTIALS_FILE = env("FIREBASE_CREDENTIALS_FILE", default="")

# The test suite must never call real AI, food or push services, whatever the local .env holds.
# Tests that need a key set one with override_settings.
import sys as _sys
if len(_sys.argv) > 1 and _sys.argv[1] == "test":
    GEMINI_API_KEY = ""
    USDA_FDC_API_KEY = ""
    FIREBASE_CREDENTIALS_FILE = ""
    PASSWORD_HASHERS = ["config.test_hashers.FastPBKDF2PasswordHasher"]  # speed only; never used outside tests
CELERY_BROKER_URL = env("REDIS_URL", default="redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = None
CELERY_BEAT_SCHEDULE = {
    # Servers without Celery can run all of these from cron instead: manage.py run_scheduled_jobs (see the portal's
    # Notifications page for the exact cron line).
    "deliver-notifications": {"task": "notifications.tasks.deliver_pending_pushes", "schedule": 60.0},
    "create-wellness-reminders": {"task": "wellness.tasks.create_wellness_reminders", "schedule": 300.0},
    "run-scheduled-automations": {"task": "automations.tasks.run_scheduled_automations", "schedule": 300.0},
    "create-weekly-bmi-reminders": {"task": "wellness.tasks.create_weekly_bmi_reminders", "schedule": 300.0},
    "create-challenge-reminders": {"task": "challenges.tasks.create_challenge_reminders", "schedule": 300.0},
}

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    # Only trust this header when a controlled reverse proxy strips any client-supplied value.
    if env("TRUST_PROXY_SSL_HEADER"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "infome.audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # Warnings and errors from the app's own code (IVA, community photos, automations) reach the console too.
        "infome": {"handlers": ["console"], "level": "WARNING"},
        "django.request": {"handlers": ["console"], "level": "WARNING"},
    },
}
