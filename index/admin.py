from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from .models import (
    Brand,
    BrandDiscoveryLog,
    BrandDiscoveryRun,
    BrandDiscoverySource,
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
from .tasks import discover_brands_now, run_nightly_brand_scraping, run_nightly_pipeline


class GameAliasInline(admin.TabularInline):
    model = GameAlias
    extra = 1
    fields = ["text"]


class BrandDiscoverySourceInline(admin.TabularInline):
    model = BrandDiscoverySource
    extra = 0
    fields = ["name", "discovery_url", "use_ai_extraction", "enabled"]


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "regulator", "tld", "currency", "is_active",
                    "auto_activate_discovered_brands"]
    list_filter = ["is_active"]
    inlines = [BrandDiscoverySourceInline]


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    """Status is the activate/disable switch: Active brands are scraped for
    games every night, Paused and Delisted are skipped. Discovered brands
    default to Paused (unless the region auto-activates) so selectors get set
    up before the first real scrape.
    """

    list_display = ["name", "region", "short_code", "domain", "status_badge", "created_at",
                    "use_ai_extraction", "discovered", "last_checked_at", "consecutive_failures"]
    list_filter = ["status", "region", "use_ai_extraction", "discovered", "operator_group"]
    search_fields = ["name", "domain"]
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ["discovered", "discovery_source", "discovered_at", "created_at"]
    actions = ["scrape_selected_now", "mark_active", "mark_paused", "mark_delisted"]

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        colour = {"active": "#059669", "paused": "#d97706", "delisted": "#6b7280"}[obj.status]
        return format_html('<span style="color:{};font-weight:600">{}</span>', colour, obj.get_status_display())

    @admin.action(description="Scrape selected brands now")
    def scrape_selected_now(self, request, queryset):
        from .tasks import scrape_brand

        run = ScrapeRun.objects.create(trigger="admin-selection", brands_total=queryset.count())
        for brand in queryset:
            scrape_brand.delay(run.pk, brand.pk)
        self.message_user(request, f"Queued {queryset.count()} brand(s) as run {run.pk}.", messages.SUCCESS)

    @admin.action(description="Activate (include in nightly scrape)")
    def mark_active(self, request, queryset):
        n = queryset.update(status=Brand.Status.ACTIVE)
        self.message_user(request, f"Activated {n} brand(s).", messages.SUCCESS)

    @admin.action(description="Pause (skip nightly scrape)")
    def mark_paused(self, request, queryset):
        n = queryset.update(status=Brand.Status.PAUSED)
        self.message_user(request, f"Paused {n} brand(s).", messages.SUCCESS)

    @admin.action(description="Delist (no longer licensed)")
    def mark_delisted(self, request, queryset):
        n = queryset.update(status=Brand.Status.DELISTED)
        self.message_user(request, f"Delisted {n} brand(s).", messages.SUCCESS)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    """Hiding a game from the product is `is_active=False`, not deletion -
    deleting cascades to every HomepagePlacement (and GameAlias) for it, which
    is exactly what wiped the catalog's scrape history before. Bulk delete is
    removed here for that reason; disable/enable is the reversible path.
    """

    list_display = ["title", "provider", "category", "is_active", "is_local_favourite"]
    list_filter = ["is_active", "category", "provider", "is_local_favourite"]
    search_fields = ["title", "aliases__text"]
    prepopulated_fields = {"slug": ("title",)}
    inlines = [GameAliasInline]
    actions = ["disable_selected", "enable_selected"]

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Disable selected (hide from the product)")
    def disable_selected(self, request, queryset):
        n = queryset.update(is_active=False)
        self.message_user(request, f"Disabled {n} game(s). They stay in the catalog but drop off the product.")

    @admin.action(description="Enable selected (show on the product)")
    def enable_selected(self, request, queryset):
        n = queryset.update(is_active=True)
        self.message_user(request, f"Enabled {n} game(s).")


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    """Includes the 'Run Full Pipeline Now' button: discovery, then game scraping."""

    change_list_template = "admin/index/scraperun/change_list.html"
    list_display = ["id", "started_at", "status_badge", "trigger", "brands_ok",
                    "brands_failed", "placements_captured", "duration"]
    list_filter = ["status", "trigger"]

    def get_urls(self):
        return [
            path(
                "run-all-now/",
                self.admin_site.admin_view(self.run_all_now),
                name="index_scraperun_run_all_now",
            ),
            path(
                "scrape-now/",
                self.admin_site.admin_view(self.scrape_now),
                name="index_scraperun_scrape_now",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["regions"] = Region.objects.filter(is_active=True).order_by("code")
        return super().changelist_view(request, extra_context=extra_context)

    def run_all_now(self, request):
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))
        region_code = request.POST.get("region") or None
        try:
            run_nightly_pipeline.delay(trigger="admin", region_code=region_code)
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin, not a raw 500
            self.message_user(
                request, f"Could not queue the task: {exc}. Is the Celery broker (REDIS_URL) running?",
                messages.ERROR,
            )
            return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))
        self.message_user(
            request,
            f"Queued the full pipeline{f' for {region_code}' if region_code else ''}: "
            "brand discovery, then game scraping. Refresh in a moment for results.",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))

    def scrape_now(self, request):
        """Game scraping only, skipping discovery - for one market or all of them."""
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))
        region_code = request.POST.get("region") or None
        try:
            run_nightly_brand_scraping.delay(trigger="admin", region_code=region_code)
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin, not a raw 500
            self.message_user(
                request, f"Could not queue the task: {exc}. Is the Celery broker (REDIS_URL) running?",
                messages.ERROR,
            )
            return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))
        self.message_user(
            request,
            f"Queued game scraping{f' for {region_code}' if region_code else ''} "
            "of every active brand. Refresh in a moment for results.",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))

    @admin.display(description="Status")
    def status_badge(self, obj):
        colour = {"success": "#059669", "partial": "#d97706",
                  "failed": "#dc2626", "running": "#4f46e5"}[obj.status]
        return format_html(
            '<span style="color:{};font-weight:600">{}</span>', colour, obj.get_status_display()
        )

    @admin.display(description="Duration")
    def duration(self, obj):
        if not obj.finished_at:
            return "—"
        return f"{(obj.finished_at - obj.started_at).total_seconds():.0f}s"


@admin.register(BrandDiscoverySource)
class BrandDiscoverySourceAdmin(admin.ModelAdmin):
    list_display = ["name", "region", "discovery_url", "use_ai_extraction", "enabled"]
    list_filter = ["enabled", "use_ai_extraction", "region"]
    actions = ["discover_now"]

    @admin.action(description="Discover from selected sources now")
    def discover_now(self, request, queryset):
        from django.utils import timezone as tz

        from .tasks import _discover_source_sync

        sources = list(queryset)
        run = BrandDiscoveryRun.objects.create(trigger="admin-selection", sources_total=len(sources))
        ok = created_total = 0
        for source in sources:
            log = _discover_source_sync(source, run, day_seed=tz.localdate().toordinal())
            ok += log.status == BrandDiscoveryLog.Status.SUCCESS
            created_total += log.brands_created

        run.finished_at = tz.now()
        run.sources_ok = ok
        run.sources_failed = len(sources) - ok
        run.brands_created = created_total
        run.status = (
            BrandDiscoveryRun.Status.SUCCESS if ok == len(sources)
            else (BrandDiscoveryRun.Status.PARTIAL if ok else BrandDiscoveryRun.Status.FAILED)
        )
        run.save()
        self.message_user(
            request,
            f"Ran discovery for {len(sources)} source(s): {created_total} new brand(s) found.",
            messages.SUCCESS,
        )


@admin.register(BrandDiscoveryRun)
class BrandDiscoveryRunAdmin(admin.ModelAdmin):
    """Includes the 'Discover Brands Now' button, run across every enabled source."""

    change_list_template = "admin/index/branddiscoveryrun/change_list.html"
    list_display = ["id", "started_at", "status_badge", "trigger", "sources_ok",
                    "sources_failed", "candidates_found", "brands_created"]
    list_filter = ["status", "trigger"]

    def get_urls(self):
        return [
            path(
                "discover-now/",
                self.admin_site.admin_view(self.discover_now),
                name="index_branddiscoveryrun_discover_now",
            ),
            *super().get_urls(),
        ]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["regions"] = Region.objects.filter(is_active=True).order_by("code")
        return super().changelist_view(request, extra_context=extra_context)

    def discover_now(self, request):
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index_branddiscoveryrun_changelist"))
        region_code = request.POST.get("region") or None
        try:
            discover_brands_now.delay(trigger="admin", region_code=region_code)
        except Exception as exc:  # noqa: BLE001 - surfaced to the admin, not a raw 500
            self.message_user(
                request, f"Could not queue the task: {exc}. Is the Celery broker (REDIS_URL) running?",
                messages.ERROR,
            )
            return HttpResponseRedirect(reverse("admin:index_branddiscoveryrun_changelist"))
        self.message_user(
            request,
            f"Queued brand discovery{f' for {region_code}' if region_code else ''} "
            "across every enabled source. Refresh in a moment for results.",
            messages.SUCCESS,
        )
        return HttpResponseRedirect(reverse("admin:index_branddiscoveryrun_changelist"))

    @admin.display(description="Status")
    def status_badge(self, obj):
        colour = {"success": "#059669", "partial": "#d97706",
                  "failed": "#dc2626", "running": "#4f46e5"}[obj.status]
        return format_html('<span style="color:{};font-weight:600">{}</span>', colour, obj.get_status_display())


@admin.register(BrandDiscoveryLog)
class BrandDiscoveryLogAdmin(admin.ModelAdmin):
    list_display = ["executed_at", "source", "status", "candidates_found", "brands_created", "short_error"]
    list_filter = ["status", "source__region"]
    date_hierarchy = "executed_at"

    @admin.display(description="Error")
    def short_error(self, obj):
        return (obj.error_message or "")[:80]


@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    list_display = [
        "executed_at", "brand", "status", "extraction_mode", "games_found", "diff_summary", "duration_ms",
        "short_error",
    ]
    list_filter = ["status", "extraction_mode", "brand__region", "brand"]
    search_fields = ["brand__name", "error_message"]
    date_hierarchy = "executed_at"

    @admin.display(description="Error")
    def short_error(self, obj):
        return (obj.error_message or "")[:80]

    @admin.display(description="Δ vs. last snapshot")
    def diff_summary(self, obj):
        diff = obj.snapshot_diff or {}
        added, removed, moved = len(diff.get("added", [])), len(diff.get("removed", [])), len(diff.get("moved", []))
        if not (added or removed or moved):
            return "—"
        return f"+{added} / -{removed} / ~{moved}"


@admin.register(HomepagePlacement)
class HomepagePlacementAdmin(admin.ModelAdmin):
    list_display = ["created_at", "brand", "game", "placement", "position", "position_score"]
    list_filter = ["placement", "brand__region", "brand"]
    date_hierarchy = "created_at"
    raw_id_fields = ["game", "brand", "run"]


@admin.register(NetworkCaptureLog)
class NetworkCaptureLogAdmin(admin.ModelAdmin):
    """What the Playwright sniffer saw on the wire for each brand."""

    list_display = ["captured_at", "brand", "url", "matched_pattern", "status_code", "tile_count", "used"]
    list_filter = ["used", "matched_pattern", "brand__region", "brand"]
    search_fields = ["url", "brand__name"]
    date_hierarchy = "captured_at"


@admin.register(UnmatchedTileReview)
class UnmatchedTileReviewAdmin(admin.ModelAdmin):
    """Review queue: Tier 1 (RapidFuzz) + Tier 2 (Claude) both failed to resolve.

    Bulk delete is removed: "Reject" is the correct way to dismiss a title
    (it's a status, reversible via "Reset to pending"), and deleting the row
    instead just lets the exact same tile come back as a brand-new row on the
    next scrape - it doesn't record the decision, it erases it.
    """

    list_display = ["raw_label", "brand", "placement", "best_score", "ai_suggested_new", "status"]
    list_filter = ["status", "ai_suggested_new", "placement", "brand"]
    search_fields = ["raw_label"]
    readonly_fields = ["ai_candidates"]
    actions = ["mark_rejected", "reset_to_pending"]

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Reject selected")
    def mark_rejected(self, request, queryset):
        queryset.update(
            status=UnmatchedTileReview.Status.REJECTED,
            reviewed_by=request.user,
            reviewed_at=timezone.now(),
        )

    @admin.action(description="Reset to pending (send back to the review queue)")
    def reset_to_pending(self, request, queryset):
        """The (brand, normalized) unique constraint means a title with any
        review row - rejected included - is never re-added by a future
        scrape. That's correct for a deliberate rejection, but leaves no way
        back for one made in error (or made before the catalog existed to
        approve against). This is that way back.
        """
        n = queryset.update(
            status=UnmatchedTileReview.Status.PENDING,
            resolved_game=None,
            reviewed_by=None,
            reviewed_at=None,
        )
        self.message_user(request, f"Reset {n} item(s) to pending; they'll show up in the /panel/ review queue.")


admin.site.register(Provider)
