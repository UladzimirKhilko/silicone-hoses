from django.contrib import admin

from .models import Customer, DrawingFile, Product, ProductAlias


class AliasInline(admin.TabularInline):
    model = ProductAlias
    extra = 0
    fields = ["text", "source", "created_by", "created_at"]
    readonly_fields = ["created_by", "created_at"]


class DrawingInline(admin.TabularInline):
    model = DrawingFile
    extra = 0
    fields = ["kind", "file", "original_name", "uploaded_at"]
    readonly_fields = ["original_name", "uploaded_at"]


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ["code", "yapib", "customer", "kind", "color", "status"]
    list_filter = ["customer", "status", "kind", "color"]
    search_fields = ["code", "yapib", "search_text"]
    inlines = [AliasInline, DrawingInline]


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ["name", "full_name"]


@admin.register(ProductAlias)
class ProductAliasAdmin(admin.ModelAdmin):
    list_display = ["text", "product", "source", "created_at"]
    list_filter = ["source"]
    search_fields = ["text", "product__code"]
