from django import forms
from django.forms import formset_factory

from stock.forms import DateInput, ProductField

from .models import Contract, CustomerRequest, Specification


class ContractForm(forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["customer", "number", "date", "valid_until", "file", "notes"]
        widgets = {"date": DateInput(), "valid_until": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class SpecificationForm(forms.ModelForm):
    class Meta:
        model = Specification
        fields = ["number", "date", "valid_until", "closed", "file", "notes"]
        widgets = {"date": DateInput(), "valid_until": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class SpecLineForm(forms.Form):
    product = ProductField(required=False)
    price_byn = forms.DecimalField(label="Цена без НДС", required=False, max_digits=10, decimal_places=2, localize=True)
    qty_limit = forms.IntegerField(label="Лимит, шт", min_value=1, required=False)

    def clean(self):
        data = super().clean()
        if data.get("product") and data.get("price_byn") is None:
            self.add_error("price_byn", "Укажите цену.")
        return data


SpecLineFormSet = formset_factory(SpecLineForm, extra=5)


class RequestForm(forms.ModelForm):
    class Meta:
        model = CustomerRequest
        fields = ["customer", "date", "number", "source", "due_date", "file", "notes"]
        widgets = {"date": DateInput(), "due_date": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class RequestLineForm(forms.Form):
    product = ProductField(required=False)
    qty = forms.IntegerField(label="Кол-во", min_value=1, required=False)
    price_byn = forms.DecimalField(label="Цена без НДС", required=False, max_digits=10, decimal_places=2, localize=True,
                                   help_text="если указана в заявке")

    def clean(self):
        data = super().clean()
        if data.get("product") and not data.get("qty"):
            self.add_error("qty", "Укажите количество.")
        return data


RequestLineFormSet = formset_factory(RequestLineForm, extra=5)


class OrderLineForm(forms.Form):
    product = ProductField(required=False)
    qty = forms.IntegerField(label="Кол-во", min_value=1, required=False)


OrderLineFormSet = formset_factory(OrderLineForm, extra=5)
