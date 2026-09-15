"""Public dashboard.

Full page: GET /
HTMX partials:
    GET /partial/game-table/   -> table body only
    GET /partial/stats/        -> hero stat cards only

Both partials read the same query params, so `hx-include` on the filter bar is
the only wiring needed:
    geo, period, start_date, end_date, category, q
"""

from __future__ import annotations

from django.db.models import Count, Max, Q
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .models import Region, ScrapeLog, ScrapeRun
from .services.analytics import (
    PERIOD_CHOICES,
    build_rows,
    category_split,
    geo_options,
    headline_stats,
    resolve_window,
)

CATEGORY_FILTERS = [
    ("all", "All"),
    ("live", "Live Casino"),
    ("slots", "Slots"),
    ("crash", "Crash"),
    ("blackjack", "Blackjack"),
]
PAGE_SIZE = 60


def _filters(request):
    """Normalise query params once. Every view and partial goes through here."""
    geo = request.GET.get("geo") or "all"
    window = resolve_window(
        request.GET.get("period"),
        request.GET.get("start_date"),
        request.GET.get("end_date"),
    )
    return {
        "geo": geo,
        "window": window,
        "category": request.GET.get("category") or "all",
        "q": (request.GET.get("q") or "").strip()[:80],
    }


def _table_context(request):
    f = _filters(request)
    rows = build_rows(f["window"], geo=f["geo"], category=f["category"], search=f["q"])
    return {
        **f,
        "rows": rows[:PAGE_SIZE],
        "row_total": len(rows),
        "truncated": len(rows) > PAGE_SIZE,
        "period_choices": PERIOD_CHOICES,
        "category_filters": CATEGORY_FILTERS,
    }


@require_GET
def dashboard(request):
    context = _table_context(request)
    context.update(
        {
            "stats": headline_stats(context["window"], context["geo"], context["rows"]),
            "category_split": category_split(context["window"], context["geo"]),
            "geo_options": geo_options(),
            "regions": Region.objects.filter(is_active=True),
            "last_run": ScrapeRun.objects.first(),
            "today": timezone.localdate().isoformat(),
        }
    )
    return render(request, "index/dashboard.html", context)


@require_GET
def partial_game_table(request):
    """hx-get target for the matrix. Returns the table only."""
    return render(request, "index/partials/game_table.html", _table_context(request))


@require_GET
def partial_stats(request):
    """hx-get target for the hero cards, swapped alongside the table."""
    context = _table_context(request)
    context["stats"] = headline_stats(context["window"], context["geo"], context["rows"])
    context["category_split"] = category_split(context["window"], context["geo"])
    return render(request, "index/partials/stats.html", context)


@require_GET
def partial_game_detail(request, slug: str):
    """Expandable row body: which operators featured this title in the window."""
    context = _table_context(request)
    row = next((r for r in context["rows"] if r.slug == slug), None)
    if row is None:
        return HttpResponse(status=404)
    return render(request, "index/partials/game_detail.html", {**context, "row": row})


@require_GET
def monitoring(request):
    """Operational view: did last night's run actually work?"""
    runs = ScrapeRun.objects.all()[:30]
    recent_logs = (
        ScrapeLog.objects.select_related("brand", "brand__region")
        .order_by("-executed_at")[:200]
    )
    by_brand = (
        ScrapeLog.objects.values("brand__name", "brand__region__code")
        .annotate(
            runs=Count("id"),
            failures=Count("id", filter=Q(status=ScrapeLog.Status.FAILED)),
            last_run=Max("executed_at"),
        )
        .order_by("-failures", "brand__name")
    )
    return render(
        request,
        "index/monitoring.html",
        {"runs": runs, "logs": recent_logs, "by_brand": by_brand},
    )
