import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("casino_index")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Beat runs on UTC so the nightly window does not shift with DST in any market.
app.conf.timezone = "UTC"
app.conf.enable_utc = True

CRAWL_HOUR = int(os.environ.get("CRAWL_HOUR", 2))
CRAWL_MINUTE = int(os.environ.get("CRAWL_MINUTE", 0))

# django-celery-beat is installed, so these entries are written to the database
# on first start and can then be edited in the admin without a redeploy.
app.conf.beat_schedule = {
    "nightly-pipeline": {
        # Discovers new brands first, then scrapes games for all active ones.
        "task": "index.tasks.run_nightly_pipeline",
        "schedule": crontab(hour=CRAWL_HOUR, minute=CRAWL_MINUTE),  # 02:00 UTC
        "options": {"expires": 60 * 60 * 3},
    },
    "prune-old-placements": {
        "task": "index.tasks.prune_placements",
        "schedule": crontab(hour=5, minute=0, day_of_week="sun"),
    },
}
