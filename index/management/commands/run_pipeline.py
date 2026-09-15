"""Trigger the pipeline manually, without waiting for the 02:00 UTC schedule.

    python manage.py run_pipeline                     # queue full pipeline on Celery
    python manage.py run_pipeline --sync               # run inline, no worker needed
    python manage.py run_pipeline --sync --discovery-only
    python manage.py run_pipeline --sync --scrape-only
    python manage.py run_pipeline --sync --region ES
    python manage.py run_pipeline --sync --brand codere-es
"""

from django.core.management.base import BaseCommand, CommandError

from index.models import Brand, ScrapeLog
from index.tasks import (
    discover_all_brands,
    discover_brands_now,
    run_nightly_brand_scraping,
    run_nightly_pipeline,
    scrape_brand_sync,
)


class Command(BaseCommand):
    help = "Run brand discovery and/or game scraping outside the nightly schedule."

    def add_arguments(self, parser):
        parser.add_argument("--sync", action="store_true", help="Run inline instead of queueing on Celery")
        parser.add_argument("--discovery-only", action="store_true", help="Skip game scraping")
        parser.add_argument("--scrape-only", action="store_true", help="Skip brand discovery")
        parser.add_argument("--region", help="Limit game scraping to one region code, e.g. ES")
        parser.add_argument("--brand", help="Limit game scraping to one brand slug")

    def handle(self, *args, **options):
        if options["discovery_only"] and options["scrape_only"]:
            raise CommandError("--discovery-only and --scrape-only are mutually exclusive")

        if not options["sync"]:
            self._queue(options)
            return
        self._run_sync(options)

    # -- async: hand off to Celery, return immediately -----------------------

    def _queue(self, options):
        if options["discovery_only"]:
            task = discover_brands_now.delay(trigger="manual")
            self.stdout.write(self.style.SUCCESS(f"Queued discovery task {task.id}"))
        elif options["scrape_only"]:
            run_nightly_brand_scraping.delay(trigger="manual", region_code=options["region"])
            self.stdout.write(self.style.SUCCESS("Queued game scraping"))
        else:
            run_nightly_pipeline.delay(trigger="manual")
            self.stdout.write(self.style.SUCCESS("Queued full pipeline (discovery, then scraping)"))
        self.stdout.write("Needs a running worker: celery -A config worker -l info")

    # -- sync: run inline in this process, print progress as it happens ------

    def _run_sync(self, options):
        if not options["scrape_only"]:
            self.stdout.write("Discovering brands…")
            run = discover_all_brands(trigger="manual-sync", region_code=options["region"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"Discovery {run.status}: {run.sources_ok}/{run.sources_total} sources ok, "
                    f"{run.brands_created} new brand(s)"
                )
            )

        if options["discovery_only"]:
            return

        brands = Brand.objects.filter(status=Brand.Status.ACTIVE).select_related("region")
        if options["region"]:
            brands = brands.filter(region__code=options["region"])
        if options["brand"]:
            brands = brands.filter(slug=options["brand"])
        brands = list(brands)

        if not brands:
            self.stderr.write("No matching active brands to scrape.")
            return

        self.stdout.write(f"\nScraping {len(brands)} brand(s)…")
        ok = 0
        for brand in brands:
            log = scrape_brand_sync(brand)
            flag = self.style.SUCCESS("ok") if log.status == ScrapeLog.Status.SUCCESS else self.style.WARNING(log.status)
            self.stdout.write(f"  {brand.name:<26} {flag}  {log.games_found} games found")
            ok += log.status == ScrapeLog.Status.SUCCESS

        self.stdout.write(self.style.SUCCESS(f"\nDone: {ok}/{len(brands)} brands scraped successfully"))
