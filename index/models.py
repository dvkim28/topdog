from django.conf import settings
from django.db import models
from django.utils import timezone


class ExtractionMode(models.TextChoices):
    """How a scrape actually got its tiles, recorded per ScrapeLog.

    NETWORK_API is the primary path: a lobby/games JSON payload was sniffed
    straight off the wire. DOM_AI / DOM_CSS are the HTML fallback, used when
    no matching JSON response showed up within the sniff deadline. MOCK is
    the deterministic fake-data path used when SCRAPER_MODE=mock.
    """

    NETWORK_API = "network_api", "Network API sniff"
    DOM_JSON = "dom_json", "DOM (embedded JSON)"
    DOM_AI = "dom_ai", "DOM (AI extraction)"
    DOM_CSS = "dom_css", "DOM (CSS selectors)"
    UNCHANGED = "unchanged", "Unchanged (hash match, skipped)"
    MOCK = "mock", "Mock"


class Category(models.TextChoices):
    LIVE_ROULETTE = "live_roulette", "Live Roulette"
    LIVE_BLACKJACK = "live_blackjack", "Live Blackjack"
    GAME_SHOW = "game_show", "Game Show"
    MEGAWAYS = "megaways", "Megaways"
    CLASSIC_SLOT = "classic_slot", "Classic Slot"
    CRASH = "crash", "Crash Game"
    TABLE = "table", "Blackjack / Table"

    @classmethod
    def group_of(cls, value: str) -> str:
        return {
            cls.LIVE_ROULETTE: "live",
            cls.GAME_SHOW: "live",
            cls.LIVE_BLACKJACK: "blackjack",
            cls.TABLE: "blackjack",
            cls.MEGAWAYS: "slots",
            cls.CLASSIC_SLOT: "slots",
            cls.CRASH: "crash",
        }.get(value, "slots")


class Placement(models.TextChoices):
    HERO = "hero", "Hero Carousel"
    GRID = "grid", "Top Pick Grid"
    LIVE_SECTION = "live_section", "Live Section"
    OTHER = "other", "Other"


PLACEMENT_WEIGHT = {
    Placement.HERO: 1.0,
    Placement.GRID: 0.7,
    Placement.LIVE_SECTION: 0.6,
    Placement.OTHER: 0.3,
}


class Region(models.Model):
    """A regulated market. The GEO selector is driven by this table."""

    code = models.CharField(max_length=8, unique=True, help_text="ES, MX, IT, ON (Ontario)")
    name = models.CharField(max_length=80)
    regulator = models.CharField(max_length=120, help_text="DGOJ, SEGOB, ADM, AGCO")
    tld = models.CharField(max_length=12, default=".es")
    currency = models.CharField(max_length=8, default="EUR")
    legal_notice_en = models.TextField(blank=True)
    legal_notice_es = models.TextField(blank=True)
    help_line = models.CharField(max_length=160, blank=True)
    is_active = models.BooleanField(default=True)
    auto_activate_discovered_brands = models.BooleanField(
        default=False,
        help_text="If off (recommended), brands found by the discovery scraper are "
                  "created as Paused so someone reviews selectors before they go live.",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.regulator})"


class Provider(models.Model):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=120, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Brand(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PAUSED = "paused", "Paused"
        DELISTED = "delisted", "Delisted"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=120, unique=True)
    short_code = models.CharField(max_length=6, help_text="Badge label, e.g. CODE")
    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="brands")
    domain = models.CharField(max_length=180, unique=True)
    homepage_url = models.URLField(help_text="Main lobby page")
    live_casino_url = models.URLField(
        blank=True, help_text="Live casino / live dealer section, if separate from the lobby"
    )
    operator_group = models.CharField(max_length=160, blank=True)
    licence_number = models.CharField(max_length=80, blank=True)

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.ACTIVE)
    use_ai_extraction = models.BooleanField(
        default=True,
        help_text="Use the AI extractor to read this brand's pages. Off falls back to CSS selectors below.",
    )
    # CSS fallback for brands where use_ai_extraction is off, e.g.
    # {"hero": ".hero-carousel .tile", "grid": "[data-section=top] .game-card"}
    selectors = models.JSONField(default=dict, blank=True)

    last_checked_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)

    content_hash = models.CharField(
        max_length=64, blank=True,
        help_text="SHA-256 of the last scraped page content (sniffed JSON tiles, or cleaned homepage "
                  "HTML when falling back to DOM extraction). Comparing against this - not against "
                  "'yesterday' - lets a scrape short-circuit before any parsing/matching/AI cost when "
                  "nothing has changed since the last check, whenever that was: manual checks can run "
                  "several times a day, or be skipped entirely.",
    )
    latest_snapshot = models.JSONField(
        default=list, blank=True,
        help_text="The last resolved list of homepage placements for this brand: "
                  "[{'game_id', 'placement', 'position', 'raw_label', 'position_score'}, ...]. "
                  "Replayed straight into HomepagePlacement on a hash-unchanged run (so the daily "
                  "dashboard snapshot keeps a row for that day at zero extraction/matching cost), and "
                  "diffed against on a changed run to log what was added/removed/moved.",
    )
    created_at = models.DateTimeField(
        null=True, blank=True, auto_now_add=True,
        help_text="When this brand row was added. Null for brands that existed before this field did.",
    )

    discovered = models.BooleanField(
        default=False, help_text="Created by the discovery scraper rather than seeded by hand"
    )
    discovery_source = models.ForeignKey(
        "BrandDiscoverySource", null=True, blank=True, on_delete=models.SET_NULL, related_name="brands_found"
    )
    discovered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["region__code", "name"]
        indexes = [models.Index(fields=["status", "region"])]

    def __str__(self):
        return f"{self.name} [{self.region.code}]"


class Game(models.Model):
    title = models.CharField(max_length=180)
    slug = models.SlugField(max_length=180, unique=True)
    provider = models.ForeignKey(Provider, on_delete=models.PROTECT, related_name="games")
    category = models.CharField(max_length=32, choices=Category.choices)
    is_local_favourite = models.BooleanField(
        default=False, help_text="Market-specific title, e.g. MGA Games licensed IP in Spain"
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Unchecking hides this game from the product (dashboard, brand pages) without "
                  "deleting it or its scrape history - the reversible alternative to deletion.",
    )
    note_en = models.TextField(blank=True)
    note_es = models.TextField(blank=True)

    class Meta:
        ordering = ["title"]
        indexes = [models.Index(fields=["category"])]

    def __str__(self):
        return f"{self.title} ({self.provider})"

    @property
    def category_group(self) -> str:
        return Category.group_of(self.category)


class GameAlias(models.Model):
    """Lobby tiles rarely use the canonical title. Aliases drive matching."""

    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="aliases")
    text = models.CharField(max_length=200)
    normalized = models.CharField(max_length=200, db_index=True)

    class Meta:
        unique_together = [("game", "normalized")]

    def save(self, *args, **kwargs):
        from .scraping.matching import normalize

        self.normalized = normalize(self.text)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.text


class BrandDiscoverySource(models.Model):
    """Where to look for licensed operators in a given region.

    Typically the regulator's own public licence registry. Discovery runs
    against this page every night, before game scraping, to find operators
    that aren't in the Brand table yet.
    """

    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="discovery_sources")
    name = models.CharField(max_length=160, help_text="e.g. 'DGOJ public operator registry'")
    discovery_url = models.URLField()
    use_ai_extraction = models.BooleanField(
        default=True,
        help_text="Use the AI extractor to read this registry page. Off falls back to CSS selectors below.",
    )
    # CSS fallback for use_ai_extraction=False, e.g.
    # {"row": "table.operators tr", "name": "td.operator-name",
    #  "domain": "td.website a", "licence": "td.licence-no",
    #  "next_page": "a.pagination-next", "load_more": "button.load-more"}
    # next_page/load_more are optional overrides for pagination/lazy-load
    # detection (index/scraping/discovery.py) and apply to both AI and CSS
    # sources - auto-detection is tried first and usually doesn't need them.
    selectors = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["region__code", "name"]

    def __str__(self):
        return f"{self.name} [{self.region.code}]"


class BrandDiscoveryRun(models.Model):
    """One nightly sweep across all enabled discovery sources, all regions."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=32, default="beat")
    sources_total = models.PositiveIntegerField(default=0)
    sources_ok = models.PositiveIntegerField(default=0)
    sources_failed = models.PositiveIntegerField(default=0)
    candidates_found = models.PositiveIntegerField(default=0)
    brands_created = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Discovery {self.pk} {self.started_at:%Y-%m-%d} ({self.status})"


class BrandDiscoveryLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    run = models.ForeignKey(BrandDiscoveryRun, on_delete=models.CASCADE, related_name="logs")
    source = models.ForeignKey(BrandDiscoverySource, on_delete=models.CASCADE, related_name="logs")
    status = models.CharField(max_length=12, choices=Status.choices)
    candidates_found = models.PositiveIntegerField(default=0)
    brands_created = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)
    duration_ms = models.PositiveIntegerField(default=0)
    executed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-executed_at"]

    def __str__(self):
        return f"{self.source} {self.status} @ {self.executed_at:%Y-%m-%d %H:%M}"


class ScrapeRun(models.Model):
    """One nightly sweep across all active brands."""

    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=32, default="beat")
    brands_total = models.PositiveIntegerField(default=0)
    brands_ok = models.PositiveIntegerField(default=0)
    brands_failed = models.PositiveIntegerField(default=0)
    placements_captured = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Run {self.pk} {self.started_at:%Y-%m-%d} ({self.status})"


class HomepagePlacement(models.Model):
    """One observed tile: this game, on this brand's homepage, in this section.

    `detected_at` is when the tile was seen on the page. `created_at` is when the
    row was written. They differ only on backfills and replays, and the historical
    dashboard filters on `created_at` so a backfill cannot silently rewrite a past
    period.
    """

    run = models.ForeignKey(
        ScrapeRun, on_delete=models.CASCADE, related_name="placements", null=True, blank=True
    )
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="placements")
    game = models.ForeignKey(Game, on_delete=models.CASCADE, related_name="placements")
    placement = models.CharField(max_length=20, choices=Placement.choices)
    position = models.PositiveIntegerField(default=0, help_text="Index within its section")
    raw_label = models.CharField(max_length=220, blank=True)
    position_score = models.FloatField(
        default=0.0,
        help_text="Positional visibility score (services/scoring.py): section weight "
                  "decayed by on-page position.",
    )

    detected_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            # Range queries scan the window first, then narrow by brand or game.
            models.Index(fields=["detected_at", "brand"], name="hp_detected_brand_idx"),
            models.Index(fields=["created_at", "brand"], name="hp_created_brand_idx"),
            models.Index(fields=["created_at", "game"], name="hp_created_game_idx"),
            models.Index(fields=["game", "brand", "created_at"], name="hp_game_brand_created_idx"),
        ]

    def __str__(self):
        return f"{self.game} on {self.brand} [{self.placement}]"


class ScrapeLog(models.Model):
    class Status(models.TextChoices):
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    run = models.ForeignKey(
        ScrapeRun, on_delete=models.CASCADE, related_name="logs", null=True, blank=True
    )
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="scrape_logs")
    status = models.CharField(max_length=12, choices=Status.choices)
    games_found = models.IntegerField(default=0)
    extraction_mode = models.CharField(
        max_length=16, choices=ExtractionMode.choices, blank=True,
        help_text="How the tiles were actually captured for this run.",
    )
    snapshot_diff = models.JSONField(
        default=dict, blank=True,
        help_text="Change vs. Brand.latest_snapshot at the start of this run: "
                  "{'added': [...], 'removed': [...], 'moved': [...]}. Empty on an UNCHANGED run "
                  "(hash matched, nothing to diff) or on a brand's first successful run.",
    )
    error_message = models.TextField(blank=True, null=True)
    duration_ms = models.PositiveIntegerField(default=0)
    executed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-executed_at"]
        indexes = [models.Index(fields=["brand", "-executed_at"])]

    def __str__(self):
        return f"{self.brand} {self.status} @ {self.executed_at:%Y-%m-%d %H:%M}"


class NetworkCaptureLog(models.Model):
    """One network response inspected by the Playwright sniffer for a scrape.

    Written for every response the scraper checked against the lobby/games
    URL patterns, matched or not, so the admin log viewer and the extraction
    fallback decision (`ScrapeLog.extraction_mode`) can both be audited.
    """

    run = models.ForeignKey(
        ScrapeRun, on_delete=models.CASCADE, related_name="network_logs", null=True, blank=True
    )
    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="network_logs")
    url = models.URLField(max_length=500)
    matched_pattern = models.CharField(
        max_length=40, blank=True, help_text="e.g. /games, /lobby, /tiles - blank if inspected but not matched"
    )
    status_code = models.PositiveIntegerField(null=True, blank=True)
    tile_count = models.PositiveIntegerField(
        default=0, help_text="Tiles successfully parsed out of this payload, 0 if it wasn't usable"
    )
    used = models.BooleanField(default=False, help_text="Whether this payload was the one used for the scrape")
    captured_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-captured_at"]
        indexes = [models.Index(fields=["brand", "-captured_at"])]

    def __str__(self):
        return f"{self.url} [{self.status_code}]"


class UnmatchedTileReview(models.Model):
    """Tiles Tier 1 (RapidFuzz) and Tier 2 (Claude) both failed to resolve.

    Tier 2 batches these through the Claude API before they land here, so
    `ai_candidates` / `ai_suggested_new` already reflect the model's best
    attempt; a human only needs to confirm, pick a different candidate, or
    approve a genuinely new game.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending review"
        APPROVED = "approved", "Approved (matched to game)"
        REJECTED = "rejected", "Rejected"
        NEW_GAME = "new_game", "Approved as new game"

    brand = models.ForeignKey(Brand, on_delete=models.CASCADE, related_name="unmatched")
    raw_label = models.CharField(max_length=220)
    normalized = models.CharField(max_length=220, db_index=True)
    placement = models.CharField(max_length=20, choices=Placement.choices)
    best_guess = models.ForeignKey(
        Game, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    best_score = models.FloatField(default=0, help_text="Tier 1 RapidFuzz score, 0-1")
    ai_candidates = models.JSONField(
        default=list, blank=True,
        help_text="Tier 2 Claude response: [{'game_id': int, 'title': str, 'confidence': float}, ...]",
    )
    ai_suggested_new = models.BooleanField(
        default=False, help_text="Claude judged this a NEW_GAME not yet in the catalogue"
    )
    ai_suggested_provider = models.CharField(
        max_length=120, blank=True,
        help_text="Claude's best guess at the provider/studio, prefilled into the review form - never "
                  "used to create anything without a human clicking Approve.",
    )
    ai_suggested_category = models.CharField(
        max_length=32, blank=True, choices=Category.choices,
        help_text="Claude's best guess at the category, prefilled into the review form.",
    )
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    resolved_game = models.ForeignKey(
        Game, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-best_score"]
        indexes = [models.Index(fields=["status", "-seen_at"])]
        # tasks.py bulk_creates these with ignore_conflicts=True specifically
        # to avoid re-adding a title every night it stays unresolved; without
        # this constraint that call has nothing to conflict on, so an
        # unresolved title piles up a fresh duplicate row per scrape run.
        constraints = [
            models.UniqueConstraint(fields=["brand", "normalized"], name="unique_unmatched_tile_per_brand"),
        ]

    def __str__(self):
        return self.raw_label
