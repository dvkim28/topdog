from django.contrib.auth import views as auth_views
from django.urls import path

from . import panel_views, views
from .forms import EmailAuthenticationForm

urlpatterns = [
    path("", views.landing, name="landing"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("monitoring/", views.monitoring, name="monitoring"),
    path("partial/game-table/", views.partial_game_table, name="partial-game-table"),
    path("partial/stats/", views.partial_stats, name="partial-stats"),
    path("partial/game/<slug:slug>/", views.partial_game_detail, name="partial-game-detail"),
    # Auth
    path("register/", views.register, name="register"),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html", authentication_form=EmailAuthenticationForm
        ),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Admin execution panel
    path("panel/", panel_views.panel_home, name="panel-home"),
    path("panel/brands/bulk-status/", panel_views.panel_bulk_status, name="panel-bulk-status"),
    path("panel/trigger/<str:kind>/", panel_views.panel_trigger, name="panel-trigger"),
    path("panel/logs/", panel_views.panel_logs, name="panel-logs"),
    path("panel/status/", panel_views.panel_status, name="panel-status"),
    path("panel/review/", panel_views.panel_review_queue, name="panel-review-queue"),
    path("panel/review/<int:pk>/resolve/", panel_views.panel_review_resolve, name="panel-review-resolve"),
]
