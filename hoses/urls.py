from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Патрубки BSI — администрирование"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("stock/", include("stock.urls")),
    path("", include("catalog.urls")),
]
