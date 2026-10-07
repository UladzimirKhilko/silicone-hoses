from django.urls import path

from . import views

app_name = "catalog"

urlpatterns = [
    path("", views.product_list, name="product_list"),
    path("products/new/", views.product_edit, name="product_create"),
    path("products/<int:pk>/", views.product_detail, name="product_detail"),
    path("products/<int:pk>/edit/", views.product_edit, name="product_edit"),
    path("products/<int:pk>/aliases/", views.alias_add, name="alias_add"),
    path("products/<int:pk>/aliases/<int:alias_pk>/delete/", views.alias_delete, name="alias_delete"),
    path("products/<int:pk>/drawings/", views.drawing_upload, name="drawing_upload"),
    path("drawings/<int:file_pk>/", views.drawing_view, name="drawing_view"),
    path("drawings/<int:file_pk>/preview.png", views.drawing_preview, name="drawing_preview"),
    path("drawings/<int:file_pk>/download/", views.drawing_download, name="drawing_download"),
    path("drawings/archive/", views.drawings_archive, name="drawings_archive"),
]
