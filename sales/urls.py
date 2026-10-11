from django.urls import path

from . import views

app_name = "sales"

urlpatterns = [
    path("contracts/", views.contract_list, name="contract_list"),
    path("contracts/new/", views.contract_edit, name="contract_create"),
    path("contracts/<int:pk>/", views.contract_detail, name="contract_detail"),
    path("contracts/<int:pk>/edit/", views.contract_edit, name="contract_edit"),
    path("contracts/<int:pk>/file/", views.contract_file, name="contract_file"),
    path("contracts/<int:contract_pk>/specs/new/", views.spec_edit, name="spec_create"),
    path("specs/<int:pk>/", views.spec_detail, name="spec_detail"),
    path("specs/<int:pk>/edit/", views.spec_edit, name="spec_edit"),
    path("specs/<int:pk>/link/", views.spec_link_past, name="spec_link_past"),
    path("specs/<int:pk>/close/", views.spec_toggle_closed, name="spec_toggle_closed"),
    path("specs/<int:pk>/delete/", views.spec_delete, name="spec_delete"),
    path("specs/<int:pk>/file/", views.spec_file, name="spec_file"),
    path("requests/", views.request_list, name="request_list"),
    path("requests/new/", views.request_edit, name="request_create"),
    path("requests/<int:pk>/", views.request_detail, name="request_detail"),
    path("requests/<int:pk>/edit/", views.request_edit, name="request_edit"),
    path("requests/<int:pk>/status/", views.request_status, name="request_status"),
    path("requests/<int:pk>/delete/", views.request_delete, name="request_delete"),
    path("requests/<int:pk>/file/", views.request_file, name="request_file"),
    path("china-order/", views.china_order, name="china_order"),
]
