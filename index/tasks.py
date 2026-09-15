"""Nightly scraping pipeline.

    run_nightly_brand_scraping          (beat: 02:00 UTC)
      -> chord(
             scrape_brand.s(run_id, brand_id) for each active Brand,
             finalise_run.s(run_id)
         )

Set SCRAPER_MODE=mock in the environment to generate plausible placements
without touching any operator site. That is the default in DEBUG, so a fresh
clone has a working dashboard after `seed_catalog` + `backfill_history`.
"""

from __future__ import annotations

import logging
import random
import time

from celery import chord, shared_task
from celery.utils.log import get_task_logger
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Brand,
    HomepagePlacement,
    Placement,
    ScrapeLog,
    ScrapeRun,
    UnmatchedTile,
)

logger = get_task_logger(__name__)


# --------------------------------------------------------------------------
# Scraper front end
# --------------------------------------------------------------------------

def _scrape_live(brand: Brand) -> list[dict]:
    """Real fetch + parse. Respects robots.txt and per-host delay."""
    from .scraping.extractor import extract_tiles
    from .scraping.fetcher import fetch_homepage
    from .scraping.matching import GameMatcher, normalize

    result = fetch_homepage(brand.homepage_url)
    tiles = extract_tiles(result.html, brand.selectors)
    matcher = GameMatcher.from_db()

    rows, unmatched = [], []
    for tile in tiles:
        outcome = matcher.match(tile.raw_label)
        if outcome.game_id:
            rows.append(
                {
                    "game_id": outcome.game_id,
                    "placement": tile.placement,
                    "position": tile.position,
                    "raw_label": tile.raw_label,
                }
            )
        else:
            unmatched.append(
                UnmatchedTile(
                    brand=brand,
                    raw_label=tile.raw_label[:220],
                    normalized=normalize(tile.raw_label)[:220],
                    placement=tile.placement,
                    best_score=outcome.score,
                )
            )
    UnmatchedTile.objects.bulk_create(unmatched, ignore_conflicts=True)
    return rows


def _scrape_mock(brand: Brand, day_seed: int = 0) -> list[dict]:
    """Deterministic fake lobby. Same brand + same day gives the same page.

    Popular titles appear on nearly every brand; long-tail titles drift in and
    out, which is what makes the trend badges move.
    """
    from .models import Game

    games = list(Game.objects.values_list("id", "title", "is_local_favourite"))
    if not games:
        return []

    rng = random.Random(f"{brand.slug}:{day_seed}")
    rows = []
    for rank, (game_id, _title, is_local) in enumerate(games):
        # Head titles: high base probability. Tail: low, and noisier.
        base = max(0.12, 0.95 - rank * 0.055)
        if is_local and brand.region.code != "ES":
            base *= 0.25
        if rng.random() > base:
            continue
        placement = rng.choices(
            [Placement.HERO, Placement.GRID, Placement.LIVE_SECTION],
            weights=[0.2, 0.5, 0.3],
        )[0]
        rows.append(
            {
                "game_id": game_id,
                "placement": placement,
                "position": len(rows),
                "raw_label": "",
            }
        )
    if rng.random() < 0.04:  # occasional flaky brand, to exercise ScrapeLog
        raise RuntimeError("Simulated timeout reading homepage")
    return rows


def scrape_brand_sync(brand: Brand, run: ScrapeRun | None = None, day_seed: int = 0) -> ScrapeLog:
    """Scrape one brand and persist placements + a ScrapeLog row.

    Never raises: a brand failure is data, not an outage.
    """
    started = time.monotonic()
    mock = getattr(settings, "SCRAPER_MODE", "mock") == "mock"

    try:
        rows = _scrape_mock(brand, day_seed) if mock else _scrape_live(brand)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        logger.warning("Scrape failed for %s: %s", brand, exc)
        Brand.objects.filter(pk=brand.pk).update(
            last_checked_at=timezone.now(),
            consecutive_failures=brand.consecutive_failures + 1,
        )
        return ScrapeLog.objects.create(
            run=run,
            brand=brand,
            status=ScrapeLog.Status.FAILED,
            games_found=0,
            error_message=f"{type(exc).__name__}: {exc}"[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    now = timezone.now()
    with transaction.atomic():
        HomepagePlacement.objects.bulk_create(
            [
                HomepagePlacement(
                    run=run,
                    brand=brand,
                    game_id=row["game_id"],
                    placement=row["placement"],
                    position=row["position"],
                    raw_label=row["raw_label"][:220],
                    detected_at=now,
                )
                for row in rows
            ],
            batch_size=500,
        )
        Brand.objects.filter(pk=brand.pk).update(
            last_checked_at=now, consecutive_failures=0
        )
        log = ScrapeLog.objects.create(
            run=run,
            brand=brand,
            status=ScrapeLog.Status.SUCCESS,
            games_found=len(rows),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    logger.info("%s: %s placements", brand, len(rows))
    return log


# --------------------------------------------------------------------------
# Celery tasks
# --------------------------------------------------------------------------

@shared_task(name="index.tasks.run_nightly_brand_scraping")
def run_nightly_brand_scraping(trigger: str = "beat", region_code: str | None = None) -> int:
    """Entry point for the 02:00 UTC schedule. Fans out one task per brand."""
    brands = Brand.objects.filter(status=Brand.Status.ACTIVE).select_related("region")
    if region_code:
        brands = brands.filter(region__code=region_code)
    brands = list(brands.order_by("region__code", "name"))

    if not brands:
        logger.warning("No active brands; nothing to scrape.")
        return 0

    run = ScrapeRun.objects.create(trigger=trigger, brands_total=len(brands))
    logger.info("Scrape run %s started for %s brands", run.pk, len(brands))

    chord([scrape_brand.s(run.pk, b.pk) for b in brands])(finalise_run.s(run_id=run.pk))
    return run.pk


@shared_task(
    name="index.tasks.scrape_brand",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=900,
    retry_jitter=True,
    max_retries=2,
    rate_limit="10/m",
)
def scrape_brand(self, run_id: int, brand_id: int) -> dict:
    """One brand, one homepage. Isolated so a single failure can't sink the run."""
    run = ScrapeRun.objects.get(pk=run_id)
    brand = Brand.objects.select_related("region").get(pk=brand_id)
    log = scrape_brand_sync(brand, run=run)
    return {"brand": brand.slug, "status": log.status, "games_found": log.games_found}


@shared_task(name="index.tasks.finalise_run")
def finalise_run(results: list, run_id: int) -> dict:
    results = [r for r in results if isinstance(r, dict)]
    ok = sum(1 for r in results if r.get("status") == ScrapeLog.Status.SUCCESS)
    failed = len(results) - ok
    captured = sum(r.get("games_found", 0) for r in results)

    if ok == 0:
        status = ScrapeRun.Status.FAILED
    elif failed:
        status = ScrapeRun.Status.PARTIAL
    else:
        status = ScrapeRun.Status.SUCCESS

    ScrapeRun.objects.filter(pk=run_id).update(
        finished_at=timezone.now(),
        status=status,
        brands_ok=ok,
        brands_failed=failed,
        placements_captured=captured,
    )
    logger.info("Run %s finished: %s ok, %s failed, %s placements", run_id, ok, failed, captured)

    if status == ScrapeRun.Status.FAILED:
        alert_on_failed_run.delay(run_id)
    return {"run_id": run_id, "status": status, "ok": ok, "failed": failed}


@shared_task(name="index.tasks.prune_placements")
def prune_placements() -> dict:
    """Weekly housekeeping. Raw placements age out; ScrapeLog rows are kept."""
    import datetime as dt

    days = settings.CRAWLER["SNAPSHOT_RETENTION_DAYS"]
    cutoff = timezone.now() - dt.timedelta(days=days)
    deleted, _ = HomepagePlacement.objects.filter(created_at__lt=cutoff).delete()
    UnmatchedTile.objects.filter(resolved=True).delete()
    logger.info("Pruned %s placements older than %s days", deleted, days)
    return {"placements": deleted}


@shared_task(name="index.tasks.alert_on_failed_run")
def alert_on_failed_run(run_id: int) -> None:
    """Hook for your alerting channel. Logs loudly by default."""
    run = ScrapeRun.objects.get(pk=run_id)
    logger.error(
        "Nightly scrape %s failed: 0 of %s brands returned a usable homepage.",
        run_id,
        run.brands_total,
    )
