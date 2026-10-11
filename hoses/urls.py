from django.contrib import admin
from django.urls import include, path

from reports import views as reports_views

admin.site.site_header = "Патрубки BSI — администрирование"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("stock/", include("stock.urls")),
    path("sales/", include("sales.urls")),
    path("reports/", include("reports.urls")),
    path("help/", reports_views.help_page, name="help"),
    path("", reports_views.dashboard, name="dashboard"),
    path("", include("catalog.urls")),
]
