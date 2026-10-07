from django.contrib import admin

from .models import Adjustment, AdjustmentLine, Batch, ReceiptLine, Shipment, ShipmentLine


class ReceiptLineInline(admin.TabularInline):
    model = ReceiptLine
    extra = 0
    autocomplete_fields = ["product"]


class ShipmentLineInline(admin.TabularInline):
    model = ShipmentLine
    extra = 0
    autocomplete_fields = ["product"]


class AdjustmentLineInline(admin.TabularInline):
    model = AdjustmentLine
    extra = 0
    autocomplete_fields = ["product"]


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ["__str__", "status", "received_date", "manufactured", "passport_number"]
    list_filter = ["status"]
    inlines = [ReceiptLineInline]


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    list_display = ["date", "customer", "ttn_number"]
    list_filter = ["customer"]
    date_hierarchy = "date"
    inlines = [ShipmentLineInline]


@admin.register(Adjustment)
class AdjustmentAdmin(admin.ModelAdmin):
    list_display = ["date", "kind", "customer"]
    list_filter = ["kind"]
    inlines = [AdjustmentLineInline]
