from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html

from .models import (
    Brand,
    Game,
    GameAlias,
    HomepagePlacement,
    Provider,
    Region,
    ScrapeLog,
    ScrapeRun,
    UnmatchedTile,
)
from .tasks import run_nightly_brand_scraping


class GameAliasInline(admin.TabularInline):
    model = GameAlias
    extra = 1
    fields = ["text"]


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "regulator", "tld", "currency", "is_active"]


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ["name", "region", "short_code", "domain", "status",
                    "last_checked_at", "consecutive_failures"]
    list_filter = ["status", "region", "operator_group"]
    search_fields = ["name", "domain"]
    prepopulated_fields = {"slug": ("name",)}
    actions = ["scrape_selected_now", "mark_active", "mark_paused"]

    @admin.action(description="Scrape selected brands now")
    def scrape_selected_now(self, request, queryset):
        from .tasks import scrape_brand

        run = ScrapeRun.objects.create(trigger="admin-selection", brands_total=queryset.count())
        for brand in queryset:
            scrape_brand.delay(run.pk, brand.pk)
        self.message_user(request, f"Queued {queryset.count()} brand(s) as run {run.pk}.", messages.SUCCESS)

    @admin.action(description="Mark as active")
    def mark_active(self, request, queryset):
        queryset.update(status=Brand.Status.ACTIVE)

    @admin.action(description="Mark as paused")
    def mark_paused(self, request, queryset):
        queryset.update(status=Brand.Status.PAUSED)


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ["title", "provider", "category", "is_local_favourite"]
    list_filter = ["category", "provider", "is_local_favourite"]
    search_fields = ["title", "aliases__text"]
    prepopulated_fields = {"slug": ("title",)}
    inlines = [GameAliasInline]


@admin.register(ScrapeRun)
class ScrapeRunAdmin(admin.ModelAdmin):
    """Includes the 'Run All Scrapers Now' button in the changelist toolbar."""

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
            *super().get_urls(),
        ]

    def run_all_now(self, request):
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index_scraperun_changelist"))
        active = Brand.objects.filter(status=Brand.Status.ACTIVE).count()
        if not active:
            self.message_user(request, "No active brands to scrape.", messages.WARNING)
        else:
            run_nightly_brand_scraping.delay(trigger="admin")
            self.message_user(
                request,
                f"Queued a full scrape of {active} active brand(s). Refresh in a moment for results.",
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


@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    list_display = ["executed_at", "brand", "status", "games_found", "duration_ms", "short_error"]
    list_filter = ["status", "brand__region", "brand"]
    search_fields = ["brand__name", "error_message"]
    date_hierarchy = "executed_at"

    @admin.display(description="Error")
    def short_error(self, obj):
        return (obj.error_message or "")[:80]


@admin.register(HomepagePlacement)
class HomepagePlacementAdmin(admin.ModelAdmin):
    list_display = ["created_at", "brand", "game", "placement", "position"]
    list_filter = ["placement", "brand__region", "brand"]
    date_hierarchy = "created_at"
    raw_id_fields = ["game", "brand", "run"]


@admin.register(UnmatchedTile)
class UnmatchedTileAdmin(admin.ModelAdmin):
    """Review queue: labels the matcher could not resolve to a known title."""

    list_display = ["raw_label", "brand", "placement", "best_score", "resolved"]
    list_filter = ["resolved", "placement", "brand"]
    search_fields = ["raw_label"]
    actions = ["mark_resolved"]

    @admin.action(description="Mark as resolved")
    def mark_resolved(self, request, queryset):
        queryset.update(resolved=True)


admin.site.register(Provider)
