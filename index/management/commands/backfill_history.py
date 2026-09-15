"""Generate mock scan history so the date-range filters have something to show.

    python manage.py backfill_history --days 45

Writes one simulated nightly scan per brand per day. Uses the same mock scraper
the Celery task uses, so the shapes match. Never touches live sites.
"""

import datetime as dt

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from index.models import Brand, HomepagePlacement, ScrapeLog, ScrapeRun
from index.tasks import _scrape_mock


class Command(BaseCommand):
    help = "Backfill simulated nightly scans for the last N days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=45)
        parser.add_argument("--flush", action="store_true", help="Delete existing placements first")

    def handle(self, *args, **options):
        days = options["days"]
        brands = list(Brand.objects.filter(status=Brand.Status.ACTIVE).select_related("region"))
        if not brands:
            self.stderr.write("No active brands. Run seed_catalog first.")
            return

        if options["flush"]:
            HomepagePlacement.objects.all().delete()
            ScrapeLog.objects.all().delete()
            ScrapeRun.objects.all().delete()
            self.stdout.write(self.style.WARNING("Existing history cleared"))

        today = timezone.localdate()
        total = 0

        for offset in range(days, 0, -1):
            day = today - dt.timedelta(days=offset - 1)
            scan_time = timezone.make_aware(dt.datetime.combine(day, dt.time(2, 5)))
            run = ScrapeRun.objects.create(
                started_at=scan_time,
                finished_at=scan_time + dt.timedelta(minutes=12),
                trigger="backfill",
                brands_total=len(brands),
            )
            ok = 0
            rows, logs = [], []

            for brand in brands:
                try:
                    placements = _scrape_mock(brand, day_seed=day.toordinal())
                except Exception as exc:  # noqa: BLE001 - mock failures are the point
                    logs.append(
                        ScrapeLog(run=run, brand=brand, status=ScrapeLog.Status.FAILED,
                                  games_found=0, error_message=str(exc))
                    )
                    continue
                ok += 1
                logs.append(
                    ScrapeLog(run=run, brand=brand, status=ScrapeLog.Status.SUCCESS,
                              games_found=len(placements))
                )
                rows.extend(
                    HomepagePlacement(
                        run=run, brand=brand, game_id=p["game_id"], placement=p["placement"],
                        position=p["position"], detected_at=scan_time,
                    )
                    for p in placements
                )

            with transaction.atomic():
                created = HomepagePlacement.objects.bulk_create(rows, batch_size=1000)
                ScrapeLog.objects.bulk_create(logs)
                # auto_now_add ignores assignment, so backdate created_at in one update.
                HomepagePlacement.objects.filter(run=run).update(created_at=scan_time)
                ScrapeLog.objects.filter(run=run).update(executed_at=scan_time)
                run.brands_ok = ok
                run.brands_failed = len(brands) - ok
                run.placements_captured = len(created)
                run.status = (
                    ScrapeRun.Status.SUCCESS if ok == len(brands) else ScrapeRun.Status.PARTIAL
                )
                run.save()

            total += len(created)

        Brand.objects.update(last_checked_at=timezone.now())
        self.stdout.write(
            self.style.SUCCESS(f"Backfilled {days} days, {total} placements across {len(brands)} brands")
        )
