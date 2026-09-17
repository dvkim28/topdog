import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-for-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
    "django_celery_results",
    "index",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]


def _database_from_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme.startswith("sqlite"):
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parsed.path.lstrip("/"),
        "USER": parsed.username,
        "PASSWORD": parsed.password,
        "HOST": parsed.hostname,
        "PORT": parsed.port or 5432,
        "CONN_MAX_AGE": 60,
    }


DATABASES = {"default": _database_from_url(os.environ.get("DATABASE_URL", "sqlite:///db.sqlite3"))}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    "index.auth_backends.EmailBackend",
    "django.contrib.auth.backends.ModelBackend",
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "landing"

LANGUAGE_CODE = "en-us"
LANGUAGES = [("en", "English"), ("es", "Espanol")]
TIME_ZONE = "Europe/Madrid"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.AnonRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"anon": "120/min"},
}

CORS_ALLOWED_ORIGINS = os.environ.get(
    "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173"
).split(",")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = "django-db"

# Separate DB index from the Celery broker above, same Redis instance. Used
# to dedupe Tier 2 AI matcher calls across brands within a scrape run - see
# index/services/normalization.py.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ.get("REDIS_CACHE_URL") or urlparse(REDIS_URL)._replace(path="/1").geturl(),
    }
}
CELERY_TASK_TIME_LIMIT = 60 * 20
CELERY_TASK_SOFT_TIME_LIMIT = 60 * 18
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

SCRAPER_MODE = os.environ.get("SCRAPER_MODE", "mock" if DEBUG else "live")

CRAWLER = {
    "USER_AGENT": os.environ.get(
        "CRAWLER_USER_AGENT", "FrequencyIndexBot/1.0 (+https://example.com/bot)"
    ),
    "TIMEOUT": float(os.environ.get("CRAWLER_REQUEST_TIMEOUT", 20)),
    "DELAY_SECONDS": float(os.environ.get("CRAWLER_DELAY_SECONDS", 4)),
    "RESPECT_ROBOTS": os.environ.get("CRAWLER_RESPECT_ROBOTS", "1") == "1",
    "MAX_RETRIES": 3,
    "SNAPSHOT_RETENTION_DAYS": 180,
    # Network-sniffing scraper (index/services/scraper.py): how long to listen
    # for a matching lobby/games JSON response before falling back to DOM.
    "NETWORK_SNIFF_TIMEOUT": float(os.environ.get("NETWORK_SNIFF_TIMEOUT", 5)),
    "NETWORK_URL_PATTERNS": os.environ.get(
        "NETWORK_URL_PATTERNS", "/games,/lobby,/tiles,/api/casino,/casino/api"
    ).split(","),
    # Brand discovery registry pages (index/scraping/discovery.py): caps on
    # classic "next page" pagination and lazy-load / "load more" scrolling.
    "DISCOVERY_MAX_PAGES": int(os.environ.get("DISCOVERY_MAX_PAGES", 5)),
    "DISCOVERY_MAX_SCROLLS": int(os.environ.get("DISCOVERY_MAX_SCROLLS", 8)),
}

AI = {
    "API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "ENABLED": bool(os.environ.get("ANTHROPIC_API_KEY")),
    "MODEL": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
    # Tier 2 title matching (services/normalization.py) is a small structured
    # classification task, not open-ended HTML extraction, so it defaults to
    # a cheaper model than full-page extraction does.
    "MATCH_MODEL": os.environ.get("ANTHROPIC_MATCH_MODEL", "claude-haiku-4-5"),
    # Non-streaming requests stay well under SDK HTTP timeouts up to ~16k;
    # a large lobby's tile list alone can run several thousand tokens, and
    # this is a ceiling, not a spend - raising it costs nothing unless the
    # model actually needs it. Measured live on leovegas.es (a large,
    # 280+-game lobby): the old 8192 cap was fine for output length, but the
    # HTML cap below was cutting a quarter of the games before extraction
    # ever ran - raised together so a big lobby's full tile list both fits
    # in the input and isn't silently cut off mid-array in the output.
    "MAX_TOKENS": int(os.environ.get("AI_MAX_TOKENS", 16000)),
    # Casino lobbies are markup-heavy; 60k chars routinely cut off before the
    # game grid on a long homepage, silently dropping tiles. Measured live on
    # leovegas.es: its cleaned lobby page alone was 224k chars, and the old
    # 150k cap here silently dropped 65 of its 284 games before the AI
    # extractor (or a human) ever saw them - raised with headroom above that.
    # This is still just the AI-extraction fallback path's limit: the CSS
    # selector and embedded-JSON paths (services/scraper.py) parse the full,
    # uncapped DOM and aren't affected by this at all.
    "MAX_HTML_CHARS": int(os.environ.get("AI_MAX_HTML_CHARS", 350000)),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
