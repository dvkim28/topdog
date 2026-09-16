"""Nightly pipeline: discover brands, then scrape games.

    run_nightly_pipeline                (beat: 02:00 UTC)
      -> discover_all_brands()            sync: one pass per enabled source,
                                           per active region, updates Brand
      -> run_nightly_brand_scraping()      -> chord(
                                                 scrape_brand.s(run_id, brand_id)
                                                 for each active Brand,
                                                 finalise_run.s(run_id)
                                             )

Discovery runs first and *synchronously* within the pipeline task, so the
brand list is settled before the game-scrape chord reads
`Brand.objects.filter(status=ACTIVE)`. A brand discovered tonight is only
scraped for games tonight if its region has `auto_activate_discovered_brands`
on; otherwise it's created Paused and picked up on the next run after review.

Set SCRAPER_MODE=mock in the environment to generate plausible placements and
discoveries without touching any operator site. That is the default in DEBUG,
so a fresh clone has a working dashboard after `seed_catalog` + `backfill_history`.
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
from django.utils.text import slugify

from .models import (
    Brand,
    BrandDiscoveryLog,
    BrandDiscoveryRun,
    BrandDiscoverySource,
    ExtractionMode,
    HomepagePlacement,
    NetworkCaptureLog,
    Placement,
    Region,
    ScrapeLog,
    ScrapeRun,
    UnmatchedTileReview,
)

logger = get_task_logger(__name__)


# --------------------------------------------------------------------------
# Scraper front end
# --------------------------------------------------------------------------

def _scrape_live(brand: Brand) -> tuple[list[dict], str, list]:
    """Real scrape. Playwright network-sniffs the lobby/live JSON first;
    falls back to DOM extraction (AI or CSS) only if nothing usable was
    captured within the sniff window. See services/scraper.py.

    Matching is two-tier (services/normalization.py): RapidFuzz first,
    Claude for whatever Tier 1 can't confidently resolve. Whatever's left
    after both tiers becomes an UnmatchedTileReview row.

    Returns (rows, extraction_mode, network_log_entries).
    """
    from .services.normalization import resolve_tiles
    from .services.scoring import positional_score
    from .services.scraper import scrape_brand as sniff_brand

    outcome = sniff_brand(brand)
    resolved, reviews = resolve_tiles(outcome.tiles, brand)
    UnmatchedTileReview.objects.bulk_create(reviews, ignore_conflicts=True)

    rows = [
        {
            "game_id": r.game_id,
            "placement": r.placement,
            "position": r.position,
            "raw_label": r.raw_label,
            "position_score": positional_score(r.placement, r.position, r.row, r.column),
        }
        for r in resolved
    ]
    return rows, outcome.extraction_mode, outcome.network_logs


def _scrape_mock(brand: Brand, day_seed: int = 0) -> list[dict]:
    """Deterministic fake lobby. Same brand + same day gives the same page.

    Popular titles appear on nearly every brand; long-tail titles drift in and
    out, which is what makes the trend badges move.
    """
    from .models import Game
    from .services.scoring import positional_score

    games = list(Game.objects.values_list("id", "title", "is_local_favourite"))
    if not games:
        return []

    rng = random.Random(f"{brand.slug}:{day_seed}")
    rows = []
    section_positions = {Placement.HERO: 0, Placement.GRID: 0, Placement.LIVE_SECTION: 0}
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
        position = section_positions[placement]
        section_positions[placement] += 1
        rows.append(
            {
                "game_id": game_id,
                "placement": placement,
                "position": position,
                "raw_label": "",
                "position_score": positional_score(placement, position),
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
    extraction_mode = ExtractionMode.MOCK
    network_logs: list = []

    try:
        if mock:
            rows = _scrape_mock(brand, day_seed)
        else:
            rows, extraction_mode, network_logs = _scrape_live(brand)
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
            extraction_mode=extraction_mode,
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
                    position_score=row.get("position_score", 0.0),
                    detected_at=now,
                )
                for row in rows
            ],
            batch_size=500,
        )
        NetworkCaptureLog.objects.bulk_create(
            [
                NetworkCaptureLog(
                    run=run,
                    brand=brand,
                    url=entry.url[:500],
                    matched_pattern=entry.matched_pattern,
                    status_code=entry.status_code,
                    tile_count=entry.tile_count,
                    used=entry.used,
                )
                for entry in network_logs
            ],
            batch_size=200,
        )
        Brand.objects.filter(pk=brand.pk).update(
            last_checked_at=now, consecutive_failures=0
        )
        log = ScrapeLog.objects.create(
            run=run,
            brand=brand,
            status=ScrapeLog.Status.SUCCESS,
            games_found=len(rows),
            extraction_mode=extraction_mode,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    logger.info("%s: %s placements (%s)", brand, len(rows), extraction_mode)
    return log


# --------------------------------------------------------------------------
# Celery tasks
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Brand discovery: find operators not yet in the Brand table
# --------------------------------------------------------------------------

def _discover_source_sync(source: BrandDiscoverySource, run: BrandDiscoveryRun, day_seed: int) -> BrandDiscoveryLog:
    """One registry page. Never raises: a bad source is data, not an outage."""
    from .scraping.discovery import discover_candidates

    started = time.monotonic()
    mock = getattr(settings, "SCRAPER_MODE", "mock") == "mock"

    try:
        candidates = discover_candidates(source, mock=mock, day_seed=day_seed)
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        logger.warning("Discovery failed for %s: %s", source, exc)
        return BrandDiscoveryLog.objects.create(
            run=run, source=source, status=BrandDiscoveryLog.Status.FAILED,
            error_message=f"{type(exc).__name__}: {exc}"[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    created = 0
    default_status = (
        Brand.Status.ACTIVE if source.region.auto_activate_discovered_brands else Brand.Status.PAUSED
    )
    for candidate in candidates:
        if Brand.objects.filter(domain=candidate.domain).exists():
            continue  # already tracked, whatever its current status
        base_slug = slugify(candidate.name) or slugify(candidate.domain)
        slug, n = base_slug, 1
        while Brand.objects.filter(slug=slug).exists():
            n += 1
            slug = f"{base_slug}-{n}"
        Brand.objects.create(
            name=candidate.name,
            slug=slug,
            short_code=candidate.name[:6].upper(),
            region=source.region,
            domain=candidate.domain,
            homepage_url=f"https://www.{candidate.domain}/",
            licence_number=candidate.licence_number,
            status=default_status,
            discovered=True,
            discovery_source=source,
            discovered_at=timezone.now(),
        )
        created += 1

    log = BrandDiscoveryLog.objects.create(
        run=run, source=source, status=BrandDiscoveryLog.Status.SUCCESS,
        candidates_found=len(candidates), brands_created=created,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    if created:
        logger.info("%s: %s new brand(s) discovered (status=%s)", source, created, default_status)
    return log


def discover_all_brands(trigger: str = "beat", region_code: str | None = None) -> BrandDiscoveryRun:
    """Sync pass over every enabled source in every active region.

    Called directly (not `.delay()`-ed) from `run_nightly_pipeline` so the
    brand list is settled before the game-scrape chord reads it. Cheap enough
    to run inline: one page fetch per source, a handful of sources per region.
    """
    sources = BrandDiscoverySource.objects.filter(enabled=True, region__is_active=True)
    if region_code:
        sources = sources.filter(region__code=region_code)
    sources = list(sources.select_related("region"))
    run = BrandDiscoveryRun.objects.create(trigger=trigger, sources_total=len(sources))
    if not sources:
        run.status = BrandDiscoveryRun.Status.SUCCESS
        run.finished_at = timezone.now()
        run.save()
        return run

    day_seed = timezone.localdate().toordinal()
    ok = candidates_total = created_total = 0
    for source in sources:
        log = _discover_source_sync(source, run, day_seed)
        ok += log.status == BrandDiscoveryLog.Status.SUCCESS
        candidates_total += log.candidates_found
        created_total += log.brands_created

    run.finished_at = timezone.now()
    run.sources_ok = ok
    run.sources_failed = len(sources) - ok
    run.candidates_found = candidates_total
    run.brands_created = created_total
    run.status = (
        BrandDiscoveryRun.Status.SUCCESS if ok == len(sources)
        else (BrandDiscoveryRun.Status.PARTIAL if ok else BrandDiscoveryRun.Status.FAILED)
    )
    run.save()
    logger.info(
        "Discovery run %s: %s/%s sources ok, %s new brand(s)",
        run.pk, ok, len(sources), created_total,
    )
    return run


@shared_task(name="index.tasks.discover_brands_now")
def discover_brands_now(trigger: str = "manual", region_code: str | None = None) -> int:
    """Task wrapper for the admin action / manual trigger. Runs in the worker."""
    return discover_all_brands(trigger=trigger, region_code=region_code).pk


@shared_task(name="index.tasks.run_nightly_pipeline")
def run_nightly_pipeline(trigger: str = "beat", region_code: str | None = None) -> dict:
    """Beat's actual entry point. Discover first, then scrape games.

    `region_code` scopes both halves to one region - discovery only looks at
    that region's sources, and the scrape chord only fans out to its brands.
    """
    discovery_run = discover_all_brands(trigger=trigger, region_code=region_code)
    scrape_run_id = run_nightly_brand_scraping(trigger=trigger, region_code=region_code)
    return {"discovery_run_id": discovery_run.pk, "scrape_run_id": scrape_run_id}


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
    UnmatchedTileReview.objects.exclude(status=UnmatchedTileReview.Status.PENDING).delete()
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
