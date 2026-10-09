from django.urls import path

from . import views

app_name = "stock"

urlpatterns = [
    path("batches/", views.batch_list, name="batch_list"),
    path("batches/new/", views.batch_edit, name="batch_create"),
    path("batches/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batches/<int:pk>/edit/", views.batch_edit, name="batch_edit"),
    path("batches/<int:pk>/receive/", views.batch_receive, name="batch_receive"),
    path("batches/<int:pk>/delete/", views.batch_delete, name="batch_delete"),
    path("batches/<int:pk>/info/", views.batch_info, name="batch_info"),
    path("batches/<int:pk>/cartons/", views.carton_add, name="carton_add"),
    path("batches/<int:pk>/cartons/<int:carton_pk>/delete/", views.carton_delete, name="carton_delete"),
    path("shipments/", views.shipment_list, name="shipment_list"),
    path("shipments/new/", views.shipment_create, name="shipment_create"),
    path("shipments/<int:pk>/", views.shipment_detail, name="shipment_detail"),
    path("shipments/<int:pk>/ttn/", views.shipment_attach_ttn, name="shipment_attach_ttn"),
    path("shipments/<int:pk>/ttn/scan/", views.shipment_ttn_scan, name="shipment_ttn_scan"),
    path("shipments/<int:pk>/passport.docx", views.shipment_passport, name="shipment_passport"),
    path("shipments/<int:pk>/labels.docx", views.shipment_labels, name="shipment_labels"),
    path("shipments/<int:pk>/delete/", views.shipment_delete, name="shipment_delete"),
    path("adjustments/", views.adjustment_list, name="adjustment_list"),
    path("adjustments/new/", views.adjustment_create, name="adjustment_create"),
    path("adjustments/<int:pk>/", views.adjustment_detail, name="adjustment_detail"),
    path("adjustments/<int:pk>/delete/", views.adjustment_delete, name="adjustment_delete"),
    path("export/stock.xlsx", views.stock_export, name="stock_export"),
]
