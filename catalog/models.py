"""Справочник: заказчики, изделия, синонимы названий, файлы чертежей.

Изделие = чертёж (docs/09): ключ изделия — обозначение ЯПИБ, код — как в штампе чертежа.
"""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .codes import normalize_name


class Customer(models.Model):
    name = models.CharField("Заказчик", max_length=100, unique=True)
    full_name = models.CharField("Полное наименование", max_length=255, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "заказчик"
        verbose_name_plural = "заказчики"

    def __str__(self):
        return self.name


class Product(models.Model):
    class Kind(models.TextChoices):
        STRAIGHT = "straight", "прямой"
        ANGLE = "angle", "угловой"
        REDUCER = "reducer", "переход"
        ANGLE_REDUCER = "angle_reducer", "угловой переход"
        CORRUGATED = "corrugated", "гофрированный"
        HOSE = "hose", "рукав"

    class Color(models.TextChoices):
        BLACK = "black", "чёрный"
        BLUE = "blue", "синий"
        RED = "red", "красный"

    class Status(models.TextChoices):
        DRAWING_PENDING = "drawing_pending", "чертёж ожидается"
        SAMPLE = "sample", "образец"
        SERIAL = "serial", "серия"
        ARCHIVED = "archived", "снят"

    code = models.CharField("Код (по штампу чертежа)", max_length=100, unique=True)
    code_key = models.CharField(max_length=100, unique=True, editable=False)
    yapib = models.CharField("Обозначение ЯПИБ", max_length=20, unique=True, null=True, blank=True)
    customer = models.ForeignKey(
        Customer, verbose_name="Заказчик", on_delete=models.PROTECT, null=True, blank=True, related_name="products"
    )
    kind = models.CharField("Тип", max_length=20, choices=Kind.choices, blank=True)
    color = models.CharField("Цвет", max_length=10, choices=Color.choices, blank=True)
    angle = models.PositiveSmallIntegerField("Угол, °", null=True, blank=True)
    d1 = models.DecimalField("Ø1, мм", max_digits=6, decimal_places=1, null=True, blank=True)
    d2 = models.DecimalField("Ø2, мм", max_digits=6, decimal_places=1, null=True, blank=True)
    l1 = models.DecimalField("L1, мм", max_digits=7, decimal_places=1, null=True, blank=True)
    l2 = models.DecimalField("L2, мм", max_digits=7, decimal_places=1, null=True, blank=True)
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.SAMPLE)
    notes = models.TextField("Примечание", blank=True)
    search_text = models.TextField(editable=False, blank=True)

    class Meta:
        ordering = ["customer__name", "code"]
        verbose_name = "изделие"
        verbose_name_plural = "изделия"

    def __str__(self):
        return self.code

    @property
    def size_label(self):
        """Размеры для таблицы: «Ø50→63,5 · 90° · 95×70»."""

        def fmt(v):
            return f"{v.normalize():f}".replace(".", ",") if v is not None else ""

        parts = []
        if self.d1:
            parts.append(f"Ø{fmt(self.d1)}" + (f"→{fmt(self.d2)}" if self.d2 else ""))
        if self.angle:
            parts.append(f"{self.angle}°")
        if self.l1:
            parts.append(fmt(self.l1) + (f"×{fmt(self.l2)}" if self.l2 else ""))
        return " · ".join(parts)

    def rebuild_search_text(self):
        bits = [
            self.code,
            self.code.replace(",", "."),
            self.code.replace(".", ","),
            normalize_name(self.code),
            self.yapib or "",
            f"ЯПИБ {self.yapib}" if self.yapib else "",
            self.customer.name if self.customer_id else "",
            self.get_color_display(),
            self.get_kind_display(),
            self.get_status_display(),
        ]
        if self.pk:
            bits += [a.text for a in self.aliases.all()]
        self.search_text = " ".join(b for b in bits if b).lower()

    def save(self, *args, **kwargs):
        self.code_key = normalize_name(self.code)
        self.rebuild_search_text()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("catalog:product_detail", args=[self.pk])

    def drawing(self, kind):
        return next((f for f in self.drawing_files.all() if f.kind == kind), None)


class ProductAlias(models.Model):
    """Написание изделия в чужом документе, подтверждённое человеком (docs/09)."""

    class Source(models.TextChoices):
        INVOICE = "invoice", "инвойс"
        PACKING_LIST = "packing_list", "упаковочный лист завода"
        CARTON = "carton", "маркировка коробки"
        TTN = "ttn", "ТТН"
        REQUEST = "request", "заявка заказчика"
        OTHER = "other", "другое"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="aliases")
    text = models.CharField("Написание", max_length=255)
    normalized = models.CharField(max_length=255, unique=True, editable=False)
    source = models.CharField("Источник", max_length=20, choices=Source.choices, default=Source.OTHER)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["text"]
        verbose_name = "синоним"
        verbose_name_plural = "синонимы"

    def __str__(self):
        return self.text

    def clean(self):
        key = normalize_name(self.text)
        if not key:
            raise ValidationError({"text": "Пустое написание."})
        other = Product.objects.filter(code_key=key).exclude(pk=self.product_id).first()
        if other:
            raise ValidationError({"text": f"Это код другого изделия: {other.code}."})
        if key == normalize_name(self.product.code):
            raise ValidationError({"text": "Совпадает с кодом этого изделия — синоним не нужен."})
        taken = ProductAlias.objects.filter(normalized=key).exclude(pk=self.pk).select_related("product").first()
        if taken:
            raise ValidationError({"text": f"Уже привязано к изделию {taken.product.code}."})

    def save(self, *args, **kwargs):
        self.normalized = normalize_name(self.text)
        super().save(*args, **kwargs)
        self.product.save()


def drawing_upload_to(instance, filename):
    ext = Path(filename).suffix.lower() or ".pdf"
    key = instance.product.yapib or f"id{instance.product_id}"
    return f"drawings/{key}/{instance.kind}{ext}"


class DrawingFile(models.Model):
    class Kind(models.TextChoices):
        PDF = "pdf", "чертёж (PDF)"
        SIGNED = "signed", "скан с подписями"
        CHINA = "china", "для китайского завода"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="drawing_files")
    kind = models.CharField("Вид", max_length=10, choices=Kind.choices, default=Kind.PDF)
    file = models.FileField("Файл", upload_to=drawing_upload_to)
    preview = models.ImageField("Картинка для просмотра", upload_to=drawing_upload_to, blank=True, editable=False)
    original_name = models.CharField(max_length=255, blank=True)
    uploaded_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["product", "kind"], name="one_drawing_file_per_kind")]
        ordering = ["kind"]
        verbose_name = "файл чертежа"
        verbose_name_plural = "файлы чертежей"

    def __str__(self):
        return f"{self.product} — {self.get_kind_display()}"

    def build_preview(self):
        """PNG первой страницы: картинка открывается в любом браузере и на любом телефоне,
        в отличие от PDF во встроенном окне."""
        import pymupdf
        from django.core.files.base import ContentFile

        if self.preview:
            self.preview.delete(save=False)
        with self.file.open("rb") as fh:
            data = fh.read()
        try:
            with pymupdf.open(stream=data, filetype=Path(self.file.name).suffix.lstrip(".") or "pdf") as doc:
                pix = doc[0].get_pixmap(dpi=150)
                png = pix.tobytes("png")
        except Exception:  # не PDF/картинка или битый файл — останется только скачивание
            return
        self.preview.save(f"{self.kind}-preview.png", ContentFile(png), save=False)

    @property
    def download_name(self):
        ext = Path(self.file.name).suffix
        base = f"{self.product.yapib} {self.product.code}" if self.product.yapib else self.product.code
        suffix = "" if self.kind == self.Kind.PDF else f" ({self.get_kind_display()})"
        return f"{base}{suffix}{ext}".replace("/", "_")


def find_product(name):
    """Точное сопоставление названия из документа: код по штампу или подтверждённый синоним.
    Никаких догадок — если не нашли, решает человек."""
    key = normalize_name(name)
    if not key:
        return None
    alias = ProductAlias.objects.select_related("product").filter(normalized=key).first()
    if alias:
        return alias.product
    return Product.objects.filter(code_key=key).first()
