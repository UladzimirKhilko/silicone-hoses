"""Договоры и спецификации заказчиков (цены реализации и лимиты), заявки заказчиков.

Цена реализации задаётся спецификацией к договору (docs/10): изделие, цена без НДС, лимит штук.
Отгрузка каждой строкой привязывается к строке спецификации — так виден остаток по лимиту.
"""

from django.conf import settings
from django.db import models
from django.urls import reverse

from catalog.models import Customer, Product


class Contract(models.Model):
    customer = models.ForeignKey(Customer, verbose_name="Заказчик", on_delete=models.PROTECT, related_name="contracts")
    number = models.CharField("Номер договора", max_length=60)
    date = models.DateField("Дата договора", null=True, blank=True)
    valid_until = models.DateField("Действует до", null=True, blank=True)
    file = models.FileField("Скан договора", upload_to="contracts/%Y/", blank=True)
    notes = models.TextField("Примечание", blank=True)

    class Meta:
        ordering = ["customer__name", "-date"]
        verbose_name = "договор"
        verbose_name_plural = "договоры"

    def __str__(self):
        date = f" от {self.date:%d.%m.%Y}" if self.date else ""
        return f"Договор № {self.number}{date}"

    def get_absolute_url(self):
        return reverse("sales:contract_detail", args=[self.pk])


class Specification(models.Model):
    contract = models.ForeignKey(Contract, verbose_name="Договор", on_delete=models.CASCADE, related_name="specifications")
    number = models.CharField("Номер спецификации", max_length=30)
    date = models.DateField("Дата спецификации")
    valid_until = models.DateField("Действует до", null=True, blank=True, help_text="Пусто — пока не закроете вручную")
    closed = models.BooleanField("Закрыта", default=False, help_text="Закрытая спецификация не подставляется в новые отгрузки")
    file = models.FileField("Скан спецификации", upload_to="specifications/%Y/", blank=True)
    notes = models.TextField("Примечание", blank=True)

    class Meta:
        ordering = ["-date", "-pk"]
        verbose_name = "спецификация"
        verbose_name_plural = "спецификации"

    def __str__(self):
        return f"Спецификация № {self.number} от {self.date:%d.%m.%Y}"

    def get_absolute_url(self):
        return reverse("sales:spec_detail", args=[self.pk])

    @property
    def customer(self):
        return self.contract.customer

    def is_active(self, on_date):
        return not self.closed and self.date <= on_date and (self.valid_until is None or on_date <= self.valid_until)


class SpecLine(models.Model):
    spec = models.ForeignKey(Specification, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, verbose_name="Изделие", on_delete=models.PROTECT, related_name="spec_lines")
    price_byn = models.DecimalField("Цена без НДС, BYN", max_digits=10, decimal_places=2)
    qty_limit = models.PositiveIntegerField("Лимит, шт", null=True, blank=True, help_text="Пусто — без лимита")

    class Meta:
        ordering = ["product__code"]
        constraints = [models.UniqueConstraint(fields=["spec", "product"], name="one_line_per_product_in_spec")]
        verbose_name = "строка спецификации"
        verbose_name_plural = "строки спецификации"

    def __str__(self):
        return f"{self.spec}: {self.product.code}"


class CustomerRequest(models.Model):
    """Заявка заказчика: что и сколько хотят. Пока заявка открыта, невыполненный остаток —
    резерв на складе (в порядке очереди: более ранние заявки первыми)."""

    class Source(models.TextChoices):
        TENDER = "tender", "тендерная площадка"
        LETTER = "letter", "письмо (скан)"
        EXCEL = "excel", "файл Excel"
        PHONE = "phone", "телефон / мессенджер"
        OTHER = "other", "другое"

    class Status(models.TextChoices):
        OPEN = "open", "в работе"
        DONE = "done", "выполнена"
        CANCELLED = "cancelled", "отменена"

    customer = models.ForeignKey(Customer, verbose_name="Заказчик", on_delete=models.PROTECT, related_name="requests")
    date = models.DateField("Дата заявки")
    number = models.CharField("Номер / тендер", max_length=80, blank=True)
    source = models.CharField("Откуда", max_length=10, choices=Source.choices, default=Source.LETTER)
    due_date = models.DateField("Срок поставки", null=True, blank=True)
    status = models.CharField("Статус", max_length=10, choices=Status.choices, default=Status.OPEN)
    file = models.FileField("Файл заявки", upload_to="requests/%Y/", blank=True)
    notes = models.TextField("Примечание", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-pk"]
        verbose_name = "заявка"
        verbose_name_plural = "заявки"

    def __str__(self):
        number = f" № {self.number}" if self.number else ""
        return f"Заявка {self.customer}{number} от {self.date:%d.%m.%Y}"

    def get_absolute_url(self):
        return reverse("sales:request_detail", args=[self.pk])

    @property
    def is_open(self):
        return self.status == self.Status.OPEN


class RequestLine(models.Model):
    request = models.ForeignKey(CustomerRequest, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, verbose_name="Изделие", on_delete=models.PROTECT, related_name="request_lines")
    qty = models.PositiveIntegerField("Кол-во, шт")
    price_byn = models.DecimalField("Цена без НДС, BYN", max_digits=10, decimal_places=2, null=True, blank=True,
                                    help_text="Если в заявке своя цена (тендер)")

    class Meta:
        ordering = ["product__code"]
        constraints = [models.UniqueConstraint(fields=["request", "product"], name="one_line_per_product_in_request")]
