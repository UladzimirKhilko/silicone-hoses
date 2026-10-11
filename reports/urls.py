from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    path("", views.index, name="index"),
    path("shipments/", views.shipments_monthly, name="shipments_monthly"),
    path("low-stock/", views.low_stock, name="low_stock"),
]
