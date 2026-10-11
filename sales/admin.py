from django.contrib import admin

from .models import Contract, CustomerRequest, RequestLine, SpecLine, Specification


class SpecLineInline(admin.TabularInline):
    model = SpecLine
    extra = 0
    autocomplete_fields = ["product"]


@admin.register(Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ["number", "customer", "date", "valid_until"]
    list_filter = ["customer"]


@admin.register(Specification)
class SpecificationAdmin(admin.ModelAdmin):
    list_display = ["number", "contract", "date", "valid_until", "closed"]
    list_filter = ["contract__customer", "closed"]
    inlines = [SpecLineInline]


class RequestLineInline(admin.TabularInline):
    model = RequestLine
    extra = 0
    autocomplete_fields = ["product"]


@admin.register(CustomerRequest)
class CustomerRequestAdmin(admin.ModelAdmin):
    list_display = ["date", "customer", "number", "status"]
    list_filter = ["customer", "status"]
    inlines = [RequestLineInline]
