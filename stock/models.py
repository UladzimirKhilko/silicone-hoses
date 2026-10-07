"""Движение товара: партии (приходы из Китая), отгрузки заказчикам, списания и корректировки.

Остаток не хранится, а считается из документов (stock/services.py) — так он не может разойтись
с историей, как это бывает в Excel (docs/11).
"""

from django.conf import settings
from django.db import models

from catalog.models import Customer, Product


class Batch(models.Model):
    """Партия = одна поставка из Китая (docs/10, решение 07.10.2026)."""

    class Status(models.TextChoices):
        IN_TRANSIT = "in_transit", "в пути"
        RECEIVED = "received", "принята"

    number = models.CharField("Номер партии", max_length=30, blank=True, help_text="Например 02/25 — печатается в паспорте и на бирках")
    invoice = models.CharField("Инвойс", max_length=60, blank=True, help_text="Например TS2407202")
    factory_orders = models.CharField("Заказы завода", max_length=120, blank=True, help_text="Например Q571, Q595, Q604")
    status = models.CharField("Статус", max_length=12, choices=Status.choices, default=Status.IN_TRANSIT)
    received_date = models.DateField("Дата приёмки", null=True, blank=True)
    manufactured = models.CharField("Дата изготовления", max_length=20, blank=True, help_text="Например 07.2025")
    passport_number = models.CharField("Номер паспорта", max_length=30, blank=True, help_text="Например 09/002/25")
    notes = models.TextField("Примечание", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_date", "-created_at"]
        verbose_name = "партия"
        verbose_name_plural = "партии"

    def __str__(self):
        bits = [f"№ {self.number}" if self.number else "", self.invoice]
        if self.received_date:
            bits.append(self.received_date.strftime("%d.%m.%Y"))
        return "Партия " + " · ".join(b for b in bits if b) if any(bits) else f"Партия #{self.pk}"

    @property
    def short_label(self):
        if self.number:
            return f"№ {self.number}"
        if self.invoice:
            return self.invoice
        return self.received_date.strftime("%d.%m.%Y") if self.received_date else f"#{self.pk}"

    @property
    def is_received(self):
        return self.status == self.Status.RECEIVED


class ReceiptLine(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, verbose_name="Изделие", on_delete=models.PROTECT, related_name="receipt_lines")
    qty_expected = models.PositiveIntegerField("По документам, шт")
    qty_received = models.PositiveIntegerField("Принято, шт", null=True, blank=True)
    price_cny = models.DecimalField("Цена Китая, CNY", max_digits=16, decimal_places=10, null=True, blank=True)
    landed_price_byn = models.DecimalField("Приходная цена, BYN/шт", max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["product__code"]
        constraints = [models.UniqueConstraint(fields=["batch", "product"], name="one_line_per_product_in_batch")]

    @property
    def shortage(self):
        if self.qty_received is None:
            return 0
        return self.qty_expected - self.qty_received


class Shipment(models.Model):
    customer = models.ForeignKey(Customer, verbose_name="Заказчик", on_delete=models.PROTECT, related_name="shipments")
    date = models.DateField("Дата отгрузки")
    ttn_number = models.CharField("№ ТТН", max_length=30, blank=True)
    ttn_scan = models.FileField("Скан ТТН", upload_to="ttn/%Y/", blank=True)
    notes = models.TextField("Примечание", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "отгрузка"
        verbose_name_plural = "отгрузки"

    def __str__(self):
        ttn = f" · ТТН {self.ttn_number}" if self.ttn_number else ""
        return f"Отгрузка {self.customer} {self.date:%d.%m.%Y}{ttn}"


class ShipmentLine(models.Model):
    shipment = models.ForeignKey(Shipment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, verbose_name="Изделие", on_delete=models.PROTECT, related_name="shipment_lines")
    batch = models.ForeignKey(Batch, verbose_name="Партия", on_delete=models.PROTECT, related_name="shipment_lines")
    qty = models.PositiveIntegerField("Кол-во, шт")
    price_byn = models.DecimalField("Цена без НДС, BYN", max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["product__code", "batch__received_date"]

    @property
    def amount(self):
        return self.qty * self.price_byn if self.price_byn is not None else None


class Adjustment(models.Model):
    """Списание брака, недостачи, бесплатных образцов; корректировка по инвентаризации."""

    class Kind(models.TextChoices):
        DEFECT = "defect", "брак из Китая"
        SHORTAGE = "shortage", "недостача"
        FREE_SAMPLE = "free_sample", "бесплатные образцы заказчику"
        INVENTORY_MINUS = "inventory_minus", "инвентаризация: меньше"
        INVENTORY_PLUS = "inventory_plus", "инвентаризация: больше"

    kind = models.CharField("Причина", max_length=20, choices=Kind.choices)
    date = models.DateField("Дата")
    customer = models.ForeignKey(Customer, verbose_name="Заказчик (для образцов)", null=True, blank=True, on_delete=models.PROTECT)
    notes = models.TextField("Комментарий", blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "списание / корректировка"
        verbose_name_plural = "списания и корректировки"

    def __str__(self):
        return f"{self.get_kind_display()} {self.date:%d.%m.%Y}"

    @property
    def sign(self):
        return 1 if self.kind == self.Kind.INVENTORY_PLUS else -1


class AdjustmentLine(models.Model):
    adjustment = models.ForeignKey(Adjustment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, verbose_name="Изделие", on_delete=models.PROTECT, related_name="adjustment_lines")
    batch = models.ForeignKey(Batch, verbose_name="Партия", on_delete=models.PROTECT, related_name="adjustment_lines")
    qty = models.PositiveIntegerField("Кол-во, шт")

    class Meta:
        ordering = ["product__code"]
