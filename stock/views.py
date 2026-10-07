import datetime as dt
from io import BytesIO

import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import ProtectedError, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from catalog.models import Customer
from catalog.views import search_products

from . import services
from .forms import (
    AdjustmentForm,
    AdjustmentLineFormSet,
    BatchForm,
    ReceiptLineFormSet,
    ReceiveForm,
    ShipmentForm,
    ShipmentLineFormSet,
    filled,
)
from .models import Adjustment, Batch, ReceiptLine, Shipment, ShipmentLine


# --- Приходы (партии) ---

@login_required
def batch_list(request):
    batches = Batch.objects.annotate(qty=Sum("lines__qty_expected")).prefetch_related("lines")
    return render(request, "stock/batch_list.html", {"batches": batches})


@login_required
def batch_detail(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    lines = batch.lines.select_related("product__customer")
    shipped = {
        r["product_id"]: r["q"]
        for r in ShipmentLine.objects.filter(batch=batch).values("product_id").annotate(q=Sum("qty"))
    }
    balances = services.batch_balances([line.product_id for line in lines])
    for line in lines:
        line.shipped = shipped.get(line.product_id, 0)
        line.left = balances.get((line.product_id, batch.pk), 0)
    return render(request, "stock/batch_detail.html", {"batch": batch, "lines": lines, "receive_form": ReceiveForm(initial={"received_date": dt.date.today()})})


@login_required
def batch_edit(request, pk=None):
    batch = get_object_or_404(Batch, pk=pk) if pk else Batch()
    if batch.is_received and request.method == "POST":
        messages.error(request, "Партия уже принята — состав менять нельзя, только приёмку.")
        return redirect("stock:batch_detail", pk=batch.pk)
    initial = [{"product": line.product, "qty_expected": line.qty_expected, "price_cny": line.price_cny} for line in batch.lines.all()] if pk else []
    form = BatchForm(request.POST or None, instance=batch)
    formset = ReceiptLineFormSet(request.POST or None, initial=initial, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        rows = filled(formset)
        products = [r["product"].pk for r in rows]
        if len(products) != len(set(products)):
            messages.error(request, "Одно изделие указано дважды — объедините строки.")
        elif not rows:
            messages.error(request, "Добавьте хотя бы одно изделие.")
        else:
            with transaction.atomic():
                batch = form.save()
                batch.lines.all().delete()
                for r in rows:
                    ReceiptLine.objects.create(batch=batch, product=r["product"], qty_expected=r["qty_expected"], price_cny=r["price_cny"])
            messages.success(request, f"Сохранено: {batch}. Товар в пути — примите его, когда придёт на склад.")
            return redirect("stock:batch_detail", pk=batch.pk)
    return render(request, "stock/batch_form.html", {"form": form, "formset": formset, "batch": batch if pk else None})


@login_required
@require_POST
def batch_receive(request, pk):
    """Приёмка: фактическое количество по строкам; расхождение остаётся видно в партии."""
    batch = get_object_or_404(Batch, pk=pk)
    form = ReceiveForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Укажите дату приёмки.")
        return redirect("stock:batch_detail", pk=pk)
    lines = list(batch.lines.select_related("product"))
    shipped = {
        r["product_id"]: r["q"] for r in ShipmentLine.objects.filter(batch=batch).values("product_id").annotate(q=Sum("qty"))
    }
    errors = []
    for line in lines:
        raw = request.POST.get(f"received_{line.pk}", "").strip()
        try:
            qty = int(raw) if raw else line.qty_expected
        except ValueError:
            errors.append(f"{line.product}: «{raw}» — не число.")
            continue
        if qty < 0:
            errors.append(f"{line.product}: количество не может быть отрицательным.")
        elif qty < shipped.get(line.product_id, 0):
            errors.append(f"{line.product}: из партии уже отгружено {shipped[line.product_id]} шт.")
        line.qty_received = qty
        landed = request.POST.get(f"landed_{line.pk}", "").replace(",", ".").strip()
        line.landed_price_byn = landed or None
    if errors:
        for e in errors:
            messages.error(request, e)
        return redirect("stock:batch_detail", pk=pk)
    with transaction.atomic():
        for line in lines:
            line.save()
        batch.status = Batch.Status.RECEIVED
        batch.received_date = form.cleaned_data["received_date"]
        batch.save()
    short = [f"{line.product.code}: −{line.shortage}" for line in lines if line.shortage > 0]
    messages.success(request, "Партия принята." + (" Недостача: " + ", ".join(short) if short else ""))
    return redirect("stock:batch_detail", pk=pk)


@login_required
@require_POST
def batch_delete(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    try:
        batch.delete()
    except ProtectedError:
        messages.error(request, "Из партии уже были отгрузки или списания — удалить нельзя.")
        return redirect("stock:batch_detail", pk=pk)
    messages.success(request, "Партия удалена.")
    return redirect("stock:batch_list")


# --- Отгрузки ---

def _fill_prices(customer, rows):
    """Пустая цена = цена из последней отгрузки этого изделия этому заказчику."""
    for r in rows:
        if r.get("price_byn") is None:
            last = (
                ShipmentLine.objects.filter(shipment__customer=customer, product=r["product"], price_byn__isnull=False)
                .order_by("-shipment__date", "-pk")
                .first()
            )
            r["price_byn"] = last.price_byn if last else None
    return rows


@login_required
def shipment_list(request):
    shipments = Shipment.objects.select_related("customer").annotate(qty=Sum("lines__qty"))
    if request.GET.get("customer"):
        shipments = shipments.filter(customer_id=request.GET["customer"])
    if request.GET.get("month"):
        y, m = map(int, request.GET["month"].split("-"))
        shipments = shipments.filter(date__year=y, date__month=m)
    return render(request, "stock/shipment_list.html", {"shipments": shipments, "customers": Customer.objects.all(), "filters": request.GET})


@login_required
def shipment_detail(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("customer"), pk=pk)
    lines = shipment.lines.select_related("product", "batch")
    total = sum((line.amount or 0) for line in lines)
    return render(request, "stock/shipment_detail.html", {"shipment": shipment, "lines": lines, "total": total})


@login_required
def shipment_create(request):
    form = ShipmentForm(request.POST or None, request.FILES or None, initial={"date": dt.date.today()})
    formset = ShipmentLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        rows = filled(formset)
        if not rows:
            messages.error(request, "Добавьте хотя бы одно изделие.")
        else:
            try:
                with transaction.atomic():
                    shipment = form.save(commit=False)
                    shipment.created_by = request.user
                    shipment.save()
                    services.post_shipment(shipment, _fill_prices(shipment.customer, rows))
            except services.InsufficientStock as e:
                messages.error(request, f"Не хватает товара. {e}")
            else:
                messages.success(request, f"Отгрузка проведена: {shipment}.")
                return redirect("stock:shipment_detail", pk=shipment.pk)
    return render(request, "stock/shipment_form.html", {"form": form, "formset": formset})


@login_required
@require_POST
def shipment_attach_ttn(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    if request.FILES.get("ttn_scan"):
        if shipment.ttn_scan:
            shipment.ttn_scan.delete(save=False)
        shipment.ttn_scan = request.FILES["ttn_scan"]
    shipment.ttn_number = request.POST.get("ttn_number", shipment.ttn_number).strip()
    shipment.save()
    messages.success(request, "ТТН сохранена.")
    return redirect("stock:shipment_detail", pk=pk)


@login_required
def shipment_ttn_scan(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    if not shipment.ttn_scan or not shipment.ttn_scan.storage.exists(shipment.ttn_scan.name):
        raise Http404("Скан ТТН не загружен")
    ext = shipment.ttn_scan.name.rsplit(".", 1)[-1]
    return FileResponse(shipment.ttn_scan.open("rb"), filename=f"ТТН {shipment.ttn_number or shipment.pk}.{ext}")


@login_required
@require_POST
def shipment_delete(request, pk):
    shipment = get_object_or_404(Shipment, pk=pk)
    shipment.delete()
    messages.success(request, f"Отгрузка удалена, товар вернулся в остаток: {shipment}.")
    return redirect("stock:shipment_list")


# --- Списания и корректировки ---

@login_required
def adjustment_list(request):
    items = Adjustment.objects.select_related("customer").annotate(qty=Sum("lines__qty"))
    return render(request, "stock/adjustment_list.html", {"items": items})


@login_required
def adjustment_detail(request, pk):
    adjustment = get_object_or_404(Adjustment, pk=pk)
    return render(request, "stock/adjustment_detail.html", {"adjustment": adjustment, "lines": adjustment.lines.select_related("product", "batch")})


@login_required
def adjustment_create(request):
    form = AdjustmentForm(request.POST or None, initial={"date": dt.date.today(), "kind": request.GET.get("kind")})
    formset = AdjustmentLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        rows = filled(formset)
        if not rows:
            messages.error(request, "Добавьте хотя бы одно изделие.")
        else:
            try:
                with transaction.atomic():
                    adjustment = form.save(commit=False)
                    adjustment.created_by = request.user
                    adjustment.save()
                    services.post_adjustment(adjustment, rows)
            except services.InsufficientStock as e:
                messages.error(request, f"Не хватает товара. {e}")
            else:
                messages.success(request, f"Проведено: {adjustment}.")
                return redirect("stock:adjustment_detail", pk=adjustment.pk)
    return render(request, "stock/adjustment_form.html", {"form": form, "formset": formset})


@login_required
@require_POST
def adjustment_delete(request, pk):
    adjustment = get_object_or_404(Adjustment, pk=pk)
    if adjustment.sign > 0:
        # Удаление прихода по инвентаризации не должно увести партию в минус.
        balances = services.batch_balances([line.product_id for line in adjustment.lines.all()])
        for line in adjustment.lines.all():
            if balances.get((line.product_id, line.batch_id), 0) < line.qty:
                messages.error(request, f"{line.product}: этот товар уже отгружен — удалить нельзя.")
                return redirect("stock:adjustment_detail", pk=pk)
    adjustment.delete()
    messages.success(request, "Удалено.")
    return redirect("stock:adjustment_list")


# --- Выгрузка остатков ---

@login_required
def stock_export(request):
    products = list(search_products(request.GET))
    ids = [p.pk for p in products]
    have, transit = services.on_hand(ids), services.in_transit(ids)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Остатки"
    ws.append(["Код", "ЯПИБ", "Заказчик", "Тип", "Размеры", "Цвет", "Остаток, шт", "В пути, шт"])
    for p in products:
        ws.append([p.code, p.yapib or "", p.customer.name if p.customer_id else "", p.get_kind_display(), p.size_label,
                   p.get_color_display(), have.get(p.pk, 0), transit.get(p.pk, 0)])
    for col, width in zip("ABCDEFGH", (28, 10, 14, 16, 26, 10, 12, 12)):
        ws.column_dimensions[col].width = width
    buf = BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="ostatki-{dt.date.today():%Y-%m-%d}.xlsx"'
    return resp
