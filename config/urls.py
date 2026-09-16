from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "TopDog Admin"
admin.site.site_title = "TopDog Admin"
admin.site.index_title = "Catalog & Scraping"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("index.urls")),
]
