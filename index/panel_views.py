"""Admin execution panel: /panel/.

Separate from Django admin (/admin/) by design - this is the operational
surface non-technical staff use day to day: bulk brand status, one-click
pipeline triggers, a live-ish log feed, and the Tier 1 + Tier 2 review queue.
Everything here is HTMX partial swaps against a handful of small views;
Django admin remains the tool of record for editing individual rows.
"""

from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    Brand,
    BrandDiscoveryLog,
    BrandDiscoveryRun,
    BrandDiscoverySource,
    Category,
    Game,
    GameAlias,
    HomepagePlacement,
    NetworkCaptureLog,
    Provider,
    Region,
    ScrapeLog,
    ScrapeRun,
    UnmatchedTileReview,
)
from .services.normalization import get_or_create_provider, unique_slug
from .services.scoring import positional_score
from .tasks import discover_brands_now, run_nightly_brand_scraping, run_nightly_pipeline

LOG_FEED_SIZE = 40
RECENT_RUNS_SIZE = 6


def _staff_required(view):
    return login_required(staff_member_required(view))


def _worker_online() -> bool:
    """Best-effort ping of any Celery worker on the configured broker.

    A queued task with no worker consuming it looks identical to a task that
    just hasn't run yet - both leave the panel silent - so this is the only
    way to tell "it's queued and will run" from "it's queued and never will"
    without waiting for the run to time out.
    """
    from config.celery import app

    try:
        return bool(app.control.inspect(timeout=0.5).ping())
    except Exception:  # noqa: BLE001 - broker unreachable reads the same as "no worker"
        return False


def _recent_runs(limit: int = RECENT_RUNS_SIZE) -> list[dict]:
    rows = []
    for r in ScrapeRun.objects.order_by("-started_at")[:limit]:
        done = r.brands_ok + r.brands_failed
        rows.append(
            {
                "kind": "Game scrape", "id": r.pk, "started_at": r.started_at, "trigger": r.trigger,
                "status": r.status, "get_status_display": r.get_status_display(),
                "total": r.brands_total, "done": done,
                "pct": int(100 * done / r.brands_total) if r.brands_total else 100,
            }
        )
    for r in BrandDiscoveryRun.objects.order_by("-started_at")[:limit]:
        done = r.sources_ok + r.sources_failed
        rows.append(
            {
                "kind": "Discovery", "id": r.pk, "started_at": r.started_at, "trigger": r.trigger,
                "status": r.status, "get_status_display": r.get_status_display(),
                "total": r.sources_total, "done": done,
                "pct": int(100 * done / r.sources_total) if r.sources_total else 100,
            }
        )
    rows.sort(key=lambda row: row["started_at"], reverse=True)
    return rows[:limit]


def _brands_for(region_code: str | None):
    brands = Brand.objects.select_related("region").order_by("region__code", "name")
    if region_code:
        brands = brands.filter(region__code=region_code)
    return brands


@_staff_required
@require_GET
def panel_home(request):
    region_code = request.GET.get("region") or ""
    return render(
        request,
        "index/panel.html",
        {
            "status_choices": Brand.Status.choices,
            "regions": Region.objects.filter(is_active=True).order_by("code"),
            "selected_region": region_code,
            "review_count": _review_count(region_code),
            "worker_online": _worker_online(),
            "recent_runs": _recent_runs(),
        },
    )


@_staff_required
@require_GET
def panel_brands(request):
    """The market selector re-fetches this on change - see #market-filter in panel.html."""
    return render(request, "index/partials/panel_brands.html", {"brands": _brands_for(request.GET.get("region"))})


@_staff_required
@require_POST
def panel_bulk_status(request):
    brand_ids = request.POST.getlist("brand_ids")
    status = request.POST.get("status")
    if brand_ids and status in Brand.Status.values:
        Brand.objects.filter(pk__in=brand_ids).update(status=status)
    return render(
        request, "index/partials/panel_brands.html", {"brands": _brands_for(request.POST.get("region"))}
    )


@_staff_required
@require_POST
def panel_trigger(request, kind: str):
    """Queue discovery / scrape-only / full-pipeline, optionally scoped to one
    market. Reports back exactly what matched the filter *before* queuing
    anything, so "nothing happened" and "0 brands matched your market" don't
    look identical - and skips the round trip entirely when nothing would run.
    """
    region_code = request.POST.get("region") or None
    region_label = (
        Region.objects.filter(code=region_code).values_list("name", flat=True).first() or region_code
        if region_code else "all markets"
    )

    active_brands = Brand.objects.filter(status=Brand.Status.ACTIVE)
    if region_code:
        active_brands = active_brands.filter(region__code=region_code)
    brand_count = active_brands.count()

    enabled_sources = BrandDiscoverySource.objects.filter(enabled=True, region__is_active=True)
    if region_code:
        enabled_sources = enabled_sources.filter(region__code=region_code)
    source_count = enabled_sources.count()

    error = message = None
    try:
        if kind == "discovery":
            if source_count == 0:
                error = f"No enabled discovery sources for {region_label} - nothing queued."
            else:
                discover_brands_now.delay(trigger="panel", region_code=region_code)
                message = f"Queued brand discovery for {region_label}: {source_count} source(s)."
        elif kind == "scrape":
            if brand_count == 0:
                error = f"No active brands in {region_label} - nothing queued. Check brand status."
            else:
                run_nightly_brand_scraping.delay(trigger="panel", region_code=region_code)
                message = f"Queued game scraping for {region_label}: {brand_count} active brand(s)."
        elif kind == "pipeline":
            if brand_count == 0 and source_count == 0:
                error = f"No enabled sources or active brands for {region_label} - nothing queued."
            else:
                run_nightly_pipeline.delay(trigger="panel", region_code=region_code)
                message = (
                    f"Queued the full pipeline for {region_label}: {source_count} source(s), "
                    f"then {brand_count} active brand(s)."
                )
    except Exception as exc:  # noqa: BLE001 - surfaced to the panel, not swallowed
        error = f"Could not queue the task: {exc}. Is the Celery broker (REDIS_URL) running?"

    if message and not _worker_online():
        message += " No Celery worker is connected right now, so it will sit queued until one starts."

    return render(
        request,
        "index/partials/panel_logs.html",
        {"entries": _log_feed_entries(region_code), "trigger_error": error, "trigger_message": message},
    )


@_staff_required
@require_GET
def panel_logs(request):
    return render(
        request, "index/partials/panel_logs.html", {"entries": _log_feed_entries(request.GET.get("region"))}
    )


@_staff_required
@require_GET
def panel_status(request):
    """Worker health + recent run progress. Polled independently of the log
    feed so "is it running" stays answerable even while the feed is quiet
    (a slow first brand, or a run scoped to a market with very few brands).
    """
    return render(
        request,
        "index/partials/panel_status.html",
        {"worker_online": _worker_online(), "recent_runs": _recent_runs()},
    )


def _log_feed_entries(region_code: str | None = None) -> list[dict]:
    scrape_events = ScrapeLog.objects.select_related("brand")
    discovery_events = BrandDiscoveryLog.objects.select_related("source")
    network_events = NetworkCaptureLog.objects.filter(used=True).select_related("brand")
    if region_code:
        scrape_events = scrape_events.filter(brand__region__code=region_code)
        discovery_events = discovery_events.filter(source__region__code=region_code)
        network_events = network_events.filter(brand__region__code=region_code)

    scrape_events = scrape_events.order_by("-executed_at")[:LOG_FEED_SIZE].values(
        "executed_at", "status", "extraction_mode", "games_found", "error_message", "brand__name"
    )
    discovery_events = discovery_events.order_by("-executed_at")[:LOG_FEED_SIZE].values(
        "executed_at", "status", "candidates_found", "brands_created", "error_message", "source__name"
    )
    network_events = network_events.order_by("-captured_at")[:LOG_FEED_SIZE].values(
        "captured_at", "brand__name", "url", "matched_pattern", "tile_count"
    )

    entries = []
    for e in scrape_events:
        entries.append(
            {
                "at": e["executed_at"],
                "kind": "scrape",
                "ok": e["status"] == ScrapeLog.Status.SUCCESS,
                "text": f"Scrape · {e['brand__name']} · {e['extraction_mode'] or '—'} · "
                f"{e['games_found']} games" + (f" · {e['error_message'][:120]}" if e["error_message"] else ""),
            }
        )
    for e in discovery_events:
        entries.append(
            {
                "at": e["executed_at"],
                "kind": "discovery",
                "ok": e["status"] == BrandDiscoveryLog.Status.SUCCESS,
                "text": f"Discovery · {e['source__name']} · {e['candidates_found']} candidates · "
                f"{e['brands_created']} new" + (f" · {e['error_message'][:120]}" if e["error_message"] else ""),
            }
        )
    for e in network_events:
        entries.append(
            {
                "at": e["captured_at"],
                "kind": "network",
                "ok": True,
                "text": f"Network sniff · {e['brand__name']} · {e['matched_pattern']} · {e['tile_count']} tiles",
            }
        )

    entries.sort(key=lambda e: e["at"], reverse=True)
    return entries[:LOG_FEED_SIZE]


def _review_form_context(region_code: str | None = None, **extra):
    return {
        "items": _pending_review_items(region_code),
        "review_count": _review_count(region_code),
        "region": region_code or "",
        "providers": Provider.objects.order_by("name"),
        "category_choices": Category.choices,
        "games": Game.objects.filter(is_active=True).select_related("provider").order_by("title"),
        **extra,
    }


@_staff_required
@require_GET
def panel_review_queue(request):
    return render(request, "index/partials/panel_review.html", _review_form_context(request.GET.get("region")))


def _pending_review_qs(region_code: str | None = None):
    qs = UnmatchedTileReview.objects.filter(status=UnmatchedTileReview.Status.PENDING)
    if region_code:
        qs = qs.filter(brand__region__code=region_code)
    return qs


def _pending_review_items(region_code: str | None = None):
    return _pending_review_qs(region_code).select_related("brand", "best_guess").order_by("-seen_at")[:100]


def _review_count(region_code: str | None = None) -> int:
    return _pending_review_qs(region_code).count()


def _backfill_placement(item: UnmatchedTileReview, game: Game) -> None:
    """Record the brand/game/geo link *now*, for the sighting that actually
    triggered this review.

    Linking the alias only teaches future scrapes to recognize this title -
    it does nothing for the tile already seen tonight, and the review row
    never stored a run/position to replay into a real scrape result. Without
    this, approving looks like it did nothing until the next scrape happens
    to still show the same tile.
    """
    HomepagePlacement.objects.create(
        brand=item.brand,
        game=game,
        placement=item.placement,
        position=0,  # not recorded on the review row; treated as "seen", position unknown
        raw_label=item.raw_label[:220],
        position_score=positional_score(item.placement, 0),
    )


@_staff_required
@require_POST
def panel_review_resolve(request, pk: int):
    """Approve links this raw label to an existing Game (as a new alias) so
    future scrapes Tier-1-match it directly. New game actually creates that
    Game row - title/category/provider are what make it show up anywhere
    (dashboard, brand pages), so without them this used to just relabel the
    review row and the game would never surface. Either way, a placement is
    also backfilled immediately so the brand/game link shows up without
    waiting for the next scrape run.
    """
    item = get_object_or_404(UnmatchedTileReview, pk=pk)
    action = request.POST.get("action")
    error = None
    # A radio pick from the AI's own top-5 candidates, or a manual pick from
    # the full catalog when the right match isn't one of those five.
    game_id = request.POST.get("manual_game_id") or request.POST.get("game_id")

    if action == "approve":
        if not game_id:
            error = f'Pick a candidate or an existing game to link "{item.raw_label}" to.'
        else:
            game = get_object_or_404(Game, pk=game_id)
            GameAlias.objects.get_or_create(game=game, text=item.raw_label)
            item.status = UnmatchedTileReview.Status.APPROVED
            item.resolved_game = game
            _backfill_placement(item, game)
    elif action == "new_game":
        provider_name = (request.POST.get("provider_name") or "").strip()
        category = request.POST.get("category")
        if provider_name and category in Category.values:
            # Re-typing an existing provider's name links to it instead of
            # creating a duplicate - one provider legitimately has many games.
            provider = get_or_create_provider(provider_name)
            title = item.raw_label.strip()
            game = Game.objects.create(
                title=title, slug=unique_slug(Game, title, fallback="game"), provider=provider, category=category,
            )
            GameAlias.objects.get_or_create(game=game, text=item.raw_label)
            item.status = UnmatchedTileReview.Status.NEW_GAME
            item.resolved_game = game
            _backfill_placement(item, game)
        else:
            error = f'Enter a category and a provider name for "{item.raw_label}" before marking it as a new game.'
    else:
        item.status = UnmatchedTileReview.Status.REJECTED

    if not error:
        item.reviewed_by = request.user
        item.reviewed_at = timezone.now()
        item.save(update_fields=["status", "resolved_game", "reviewed_by", "reviewed_at"])

    return render(
        request,
        "index/partials/panel_review.html",
        _review_form_context(request.POST.get("region"), resolve_error=error),
    )
