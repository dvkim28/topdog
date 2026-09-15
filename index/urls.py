from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("monitoring/", views.monitoring, name="monitoring"),
    path("partial/game-table/", views.partial_game_table, name="partial-game-table"),
    path("partial/stats/", views.partial_stats, name="partial-stats"),
    path("partial/game/<slug:slug>/", views.partial_game_detail, name="partial-game-detail"),
]
