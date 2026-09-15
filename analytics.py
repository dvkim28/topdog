"""Date-windowed aggregation for the public dashboard.

Everything the dashboard shows is computed from HomepagePlacement rows inside a
window on `created_at`. Nothing is precomputed per day, so a custom range works
the same way a preset does.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from django.db.models.functions import TruncDate
from django.utils import timezone

from index.models import (
    PLACEMENT_WEIGHT,
    Brand,
    Category,
    HomepagePlacement,
    Placement,
    Region,
)

PERIOD_CHOICES = [
    ("today", "Today", 1),
    ("last_7_days", "7 days", 7),
    ("last_30_days", "30 days", 30),
    ("custom", "Custom", None),
]
PERIOD_DAYS = {key: days for key, _label, days in PERIOD_CHOICES if days}
MAX_CUSTOM_DAYS = 365


@dataclass
class Window:
    start: dt.datetime
    end: dt.datetime
    previous_start: dt.datetime
    previous_end: dt.datetime
    period: str
    label: str
    start_date: dt.date
    end_date: dt.date
    days: int

    @property
    def is_single_day(self) -> bool:
        return self.days <= 1


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def resolve_window(period: str | None, start_date: str | None, end_date: str | None) -> Window:
    """Turn query params into a validated window. Bad input falls back to 7 days."""
    period = period if period in PERIOD_DAYS or period == "custom" else "last_7_days"
    today = timezone.localdate()

    if period == "custom":
        start = _parse_date(start_date)
        end = _parse_date(end_date)
        if not start or not end:
            period, start, end = "last_7_days", today - dt.timedelta(days=6), today
        else:
            if start > end:
                start, end = end, start
            end = min(end, today)
            start = max(start, end - dt.timedelta(days=MAX_CUSTOM_DAYS - 1))
        label = f"{start:%d %b} to {end:%d %b %Y}"
    else:
        span = PERIOD_DAYS[period]
        end = today
        start = today - dt.timedelta(days=span - 1)
        label = dict((k, l) for k, l, _ in PERIOD_CHOICES)[period]

    days = (end - start).days + 1
    start_dt = timezone.make_aware(dt.datetime.combine(start, dt.time.min))
    end_dt = timezone.make_aware(dt.datetime.combine(end, dt.time.max))

    return Window(
        start=start_dt,
        end=end_dt,
        previous_start=start_dt - dt.timedelta(days=days),
        previous_end=start_dt - dt.timedelta(microseconds=1),
        period=period,
        label=label,
        start_date=start,
        end_date=end,
        days=days,
    )


@dataclass
class GameRow:
    game_id: int
    title: str
    slug: str
    provider: str
    category: str
    category_label: str
    category_group: str
    days_featured: int = 0
    period_days: int = 1
    brand_count: int = 0
    peak_brand_count: int = 0
    brands_total: int = 0
    hero_count: int = 0
    frequency_score: int = 0
    dominant_placement: str = Placement.OTHER
    geo_codes: list[str] = field(default_factory=list)
    brands: list[dict] = field(default_factory=list)
    rank: int = 0
    previous_rank: int | None = None

    @property
    def rank_change(self) -> int:
        if not self.previous_rank:
            return 0
        return self.previous_rank - self.rank

    @property
    def trend(self) -> str:
        if self.previous_rank is None:
            return "new"
        if self.rank_change > 1:
            return "rising"
        if self.rank_change < -1:
            return "falling"
        return "steady"

    @property
    def consistency(self) -> int:
        """Share of days in the period on which the title was seen anywhere."""
        return round(self.days_featured * 100 / max(1, self.period_days))


def _base_queryset(window: Window, geo: str | None, previous: bool = False):
    start = window.previous_start if previous else window.start
    end = window.previous_end if previous else window.end
    qs = HomepagePlacement.objects.filter(created_at__range=(start, end))
    if geo and geo != "all":
        qs = qs.filter(brand__region__code=geo)
    return qs


def _ranking(window: Window, geo: str | None, previous: bool = False) -> dict[int, int]:
    """game_id -> rank, using the same ordering as the main table."""
    rows = (
        _base_queryset(window, geo, previous)
        .annotate(day=TruncDate("created_at"))
        .values_list("game_id", "brand_id", "day")
    )
    brands_by_game: dict[int, set] = defaultdict(set)
    days_by_game: dict[int, set] = defaultdict(set)
    for game_id, brand_id, day in rows:
        brands_by_game[game_id].add(brand_id)
        days_by_game[game_id].add(day)

    ordered = sorted(
        brands_by_game,
        key=lambda gid: (-len(brands_by_game[gid]), -len(days_by_game[gid])),
    )
    return {game_id: position for position, game_id in enumerate(ordered, start=1)}


def build_rows(
    window: Window,
    geo: str | None = "all",
    category: str | None = "all",
    search: str | None = "",
) -> list[GameRow]:
    """One pass over the window, plus one cheap pass over the previous window."""
    qs = _base_queryset(window, geo).select_related(
        "game", "game__provider", "brand", "brand__region"
    )
    if category and category != "all":
        categories = [c for c in Category.values if Category.group_of(c) == category]
        qs = qs.filter(game__category__in=categories)
    if search:
        from django.db.models import Q

        qs = qs.filter(
            Q(game__title__icontains=search)
            | Q(game__provider__name__icontains=search)
            | Q(brand__name__icontains=search)
        )

    rows = (
        qs.annotate(day=TruncDate("created_at")).values_list(
            "game_id",
            "game__title",
            "game__slug",
            "game__provider__name",
            "game__category",
            "brand_id",
            "brand__name",
            "brand__short_code",
            "brand__region__code",
            "placement",
            "day",
        )
    )

    games: dict[int, GameRow] = {}
    brands_by_game: dict[int, set] = defaultdict(set)
    days_by_game: dict[int, set] = defaultdict(set)
    brands_per_day: dict[int, dict] = defaultdict(lambda: defaultdict(set))
    placements_by_game: dict[int, Counter] = defaultdict(Counter)
    geo_by_game: dict[int, set] = defaultdict(set)
    brand_meta: dict[int, dict] = defaultdict(dict)

    for (
        game_id, title, slug, provider, category_value,
        brand_id, brand_name, short_code, region_code, placement, day,
    ) in rows:
        if game_id not in games:
            games[game_id] = GameRow(
                game_id=game_id,
                title=title,
                slug=slug,
                provider=provider,
                category=category_value,
                category_label=Category(category_value).label,
                category_group=Category.group_of(category_value),
                period_days=window.days,
            )
        brands_by_game[game_id].add(brand_id)
        days_by_game[game_id].add(day)
        brands_per_day[game_id][day].add(brand_id)
        placements_by_game[game_id][placement] += 1
        geo_by_game[game_id].add(region_code)
        # Strongest placement seen for this brand wins the badge.
        current = brand_meta[game_id].get(brand_id)
        weight = PLACEMENT_WEIGHT[Placement(placement)]
        if not current or weight > current["weight"]:
            brand_meta[game_id][brand_id] = {
                "name": brand_name,
                "short_code": short_code,
                "region": region_code,
                "placement": placement,
                "placement_label": Placement(placement).label,
                "weight": weight,
            }

    brands_total = Brand.objects.filter(status=Brand.Status.ACTIVE)
    if geo and geo != "all":
        brands_total = brands_total.filter(region__code=geo)
    brands_total = brands_total.count()

    for game_id, row in games.items():
        counter = placements_by_game[game_id]
        row.brand_count = len(brands_by_game[game_id])
        row.days_featured = len(days_by_game[game_id])
        row.peak_brand_count = max(
            (len(b) for b in brands_per_day[game_id].values()), default=0
        )
        row.brands_total = brands_total
        row.hero_count = counter.get(Placement.HERO.value, 0)
        row.dominant_placement = counter.most_common(1)[0][0] if counter else Placement.OTHER
        row.geo_codes = sorted(geo_by_game[game_id])
        row.brands = sorted(
            brand_meta[game_id].values(), key=lambda b: (-b["weight"], b["name"])
        )
        row.frequency_score = _score(row, counter)

    ordered = sorted(
        games.values(), key=lambda r: (-r.brand_count, -r.days_featured, -r.frequency_score)
    )
    previous_ranks = _ranking(window, geo, previous=True)
    for position, row in enumerate(ordered, start=1):
        row.rank = position
        row.previous_rank = previous_ranks.get(row.game_id)
    return ordered


def _score(row: GameRow, placement_counter: Counter) -> int:
    """Coverage 45%, consistency across the period 30%, placement quality 25%."""
    if not row.brands_total or not row.brand_count:
        return 0
    coverage = row.brand_count / row.brands_total
    consistency = row.days_featured / max(1, row.period_days)
    total_tiles = sum(placement_counter.values()) or 1
    quality = sum(
        PLACEMENT_WEIGHT[Placement(p)] * n for p, n in placement_counter.items()
    ) / total_tiles
    return round(min(100, (coverage * 0.45 + consistency * 0.30 + quality * 0.25) * 100))


def headline_stats(window: Window, geo: str | None, rows: list[GameRow]) -> dict:
    qs = _base_queryset(window, geo)
    brands = Brand.objects.filter(status=Brand.Status.ACTIVE)
    if geo and geo != "all":
        brands = brands.filter(region__code=geo)

    scanned_brands = qs.values("brand_id").distinct().count()
    scan_days = qs.annotate(day=TruncDate("created_at")).values("day").distinct().count()
    tiles = qs.count()

    return {
        "brands_tracked": brands.count(),
        "brands_with_data": scanned_brands,
        "unique_titles": len(rows),
        "scan_days": scan_days,
        "avg_games_per_homepage": round(tiles / max(1, scanned_brands * max(1, scan_days))),
        "top_game": rows[0] if rows else None,
        "risers": sum(1 for r in rows if r.trend == "rising"),
        "new_entries": sum(1 for r in rows if r.trend == "new"),
    }


def category_split(window: Window, geo: str | None) -> list[dict]:
    rows = _base_queryset(window, geo).values_list("game__category", flat=True)
    grouped: Counter = Counter(Category.group_of(c) for c in rows)
    total = sum(grouped.values()) or 1
    labels = {
        "live": "Live Casino",
        "slots": "Video Slots",
        "crash": "Crash / Instant",
        "blackjack": "Blackjack / Table",
    }
    return [
        {
            "key": key,
            "label": labels.get(key, key.title()),
            "tiles": count,
            "percent": round(count * 100 / total, 1),
        }
        for key, count in grouped.most_common()
    ]


def geo_options() -> list[dict]:
    return [{"code": "all", "name": "All markets", "regulator": ""}] + [
        {"code": r.code, "name": r.name, "regulator": r.regulator}
        for r in Region.objects.filter(is_active=True)
    ]
