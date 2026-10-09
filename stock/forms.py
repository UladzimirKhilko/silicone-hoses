from django import forms
from django.forms import formset_factory

from catalog.models import Customer, Product

from .models import Adjustment, Batch, CartonSpec, Shipment


class ProductField(forms.ModelChoiceField):
    def __init__(self, **kwargs):
        qs = Product.objects.select_related("customer").order_by("customer__name", "code")
        super().__init__(queryset=qs, label="Изделие", **kwargs)

    def label_from_instance(self, p):
        bits = [p.code, f"ЯПИБ {p.yapib}" if p.yapib else "", p.customer.name if p.customer_id else ""]
        return " · ".join(b for b in bits if b)


class BatchField(forms.ModelChoiceField):
    def __init__(self, **kwargs):
        super().__init__(
            queryset=Batch.objects.filter(status=Batch.Status.RECEIVED),
            label="Партия",
            required=False,
            empty_label="авто",
            **kwargs,
        )


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class BatchForm(forms.ModelForm):
    class Meta:
        model = Batch
        fields = ["invoice", "factory_orders", "number", "manufactured", "passport_number", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class ReceiptLineForm(forms.Form):
    product = ProductField(required=False)
    qty_expected = forms.IntegerField(label="Кол-во по документам", min_value=1, required=False)
    price_cny = forms.DecimalField(label="Цена Китая, CNY", required=False, max_digits=16, decimal_places=10, localize=True)

    def clean(self):
        data = super().clean()
        if data.get("product") and not data.get("qty_expected"):
            self.add_error("qty_expected", "Укажите количество.")
        return data


ReceiptLineFormSet = formset_factory(ReceiptLineForm, extra=8)


class ReceiveForm(forms.Form):
    received_date = forms.DateField(label="Дата приёмки", widget=DateInput())


class ShipmentForm(forms.ModelForm):
    customer = forms.ModelChoiceField(Customer.objects.all(), label="Заказчик")

    class Meta:
        model = Shipment
        fields = ["customer", "date", "ttn_number", "ttn_scan", "notes"]
        widgets = {"date": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class ShipmentLineForm(forms.Form):
    product = ProductField(required=False)
    qty = forms.IntegerField(label="Кол-во", min_value=1, required=False)
    price_byn = forms.DecimalField(label="Цена без НДС", required=False, max_digits=10, decimal_places=2, localize=True,
                                   help_text="пусто — как в прошлой отгрузке этому заказчику")
    batch = BatchField()

    def clean(self):
        data = super().clean()
        if data.get("product") and not data.get("qty"):
            self.add_error("qty", "Укажите количество.")
        return data


ShipmentLineFormSet = formset_factory(ShipmentLineForm, extra=6)


class AdjustmentForm(forms.ModelForm):
    class Meta:
        model = Adjustment
        fields = ["kind", "date", "customer", "notes"]
        widgets = {"date": DateInput(), "notes": forms.Textarea(attrs={"rows": 2})}


class AdjustmentLineForm(forms.Form):
    product = ProductField(required=False)
    qty = forms.IntegerField(label="Кол-во", min_value=1, required=False)
    batch = BatchField()

    def clean(self):
        data = super().clean()
        if data.get("product") and not data.get("qty"):
            self.add_error("qty", "Укажите количество.")
        return data


AdjustmentLineFormSet = formset_factory(AdjustmentLineForm, extra=4)


def filled(formset):
    return [f.cleaned_data for f in formset.forms if f.cleaned_data.get("product")]


class CartonForm(forms.ModelForm):
    """Коробка из упаковочного листа завода; изделия — только из этой партии."""

    class Meta:
        model = CartonSpec
        fields = ["product", "qty_per_carton", "gross_kg", "net_kg", "cartons"]
        localized_fields = ["gross_kg", "net_kg"]  # можно вводить «16,0»

    def __init__(self, *args, batch, **kwargs):
        super().__init__(*args, **kwargs)
        self.batch = batch
        self.fields["product"] = ProductField()
        self.fields["product"].queryset = self.fields["product"].queryset.filter(receipt_lines__batch=batch)

    def clean(self):
        data = super().clean()
        if data.get("product") and data.get("qty_per_carton") and CartonSpec.objects.filter(
            batch=self.batch, product=data["product"], qty_per_carton=data["qty_per_carton"]
        ).exists():
            raise forms.ValidationError("Такая коробка для этого изделия уже есть — удалите старую, чтобы заменить.")
        return data
