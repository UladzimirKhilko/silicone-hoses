import datetime as dt
from io import BytesIO

import openpyxl
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, ProtectedError, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from catalog.models import Customer, Product
from catalog.views import file_response, search_products

from . import paperwork, receipt_import, services
from .importers import ParseError, parse_invoice, parse_packing_list, read_rows
from .forms import (
    AdjustmentForm,
    CartonForm,
    AdjustmentLineFormSet,
    BatchForm,
    ReceiptLineFormSet,
    ReceiveForm,
    ShipmentForm,
    ShipmentLineFormSet,
    filled,
)
from .models import Adjustment, Batch, CartonSpec, ReceiptLine, Shipment, ShipmentLine


# --- Приходы (партии) ---

@login_required
def batch_list(request):
    # Meta.ordering не применяется к запросам с агрегатами — сортируем явно.
    batches = (
        Batch.objects.annotate(qty=Sum("lines__qty_expected"))
        .prefetch_related("lines")
        .order_by(F("received_date").desc(nulls_first=True), "-created_at")
    )
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
    return render(request, "stock/batch_detail.html", {
        "batch": batch, "lines": lines,
        "receive_form": ReceiveForm(initial={"received_date": dt.date.today()}),
        "cartons": batch.cartons.select_related("product"),
        "carton_form": CartonForm(batch=batch),
    })


@login_required
@require_POST
def batch_info(request, pk):
    """Номер партии, дата изготовления, номер паспорта — нужны для паспорта и бирок, правятся и после приёмки."""
    batch = get_object_or_404(Batch, pk=pk)
    for field in ("number", "manufactured", "passport_number"):
        setattr(batch, field, request.POST.get(field, getattr(batch, field)).strip())
    batch.save()
    messages.success(request, "Данные партии сохранены.")
    return redirect("stock:batch_detail", pk=pk)


@login_required
@require_POST
def carton_add(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    form = CartonForm(request.POST, batch=batch)
    if form.is_valid():
        spec = form.save(commit=False)
        spec.batch = batch
        spec.source = spec.source or "введено вручную"
        spec.save()
        messages.success(request, f"Коробка добавлена: {spec}.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("stock:batch_detail", pk=pk)


@login_required
@require_POST
def carton_delete(request, pk, carton_pk):
    get_object_or_404(CartonSpec, pk=carton_pk, batch_id=pk).delete()
    messages.success(request, "Коробка удалена.")
    return redirect("stock:batch_detail", pk=pk)


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


# --- Поставка из файлов завода (этап 4) ---

IMPORT_SESSION_KEY = "receipt_import_draft"


@login_required
def batch_import(request):
    """Шаг 1: загрузить инвойс и/или упаковочный лист завода."""
    if request.method == "POST":
        invoice_file, pl_file = request.FILES.get("invoice"), request.FILES.get("packing_list")
        if not invoice_file and not pl_file:
            messages.error(request, "Выберите инвойс и/или упаковочный лист завода.")
            return redirect("stock:batch_import")
        try:
            invoice = parse_invoice(read_rows(invoice_file, invoice_file.name)) if invoice_file else None
            packing = parse_packing_list(read_rows(pl_file, pl_file.name)) if pl_file else None
        except ParseError as e:
            messages.error(request, str(e))
            return redirect("stock:batch_import")
        except Exception:  # битый или чужой файл — не падаем с 500, а объясняем
            messages.error(request, "Не удалось прочитать файл. Нужен Excel (.xlsx или .xls) — инвойс или упаковочный лист завода.")
            return redirect("stock:batch_import")
        draft = receipt_import.build_draft(invoice, packing)
        draft["files"] = [f.name for f in (invoice_file, pl_file) if f]
        request.session[IMPORT_SESSION_KEY] = draft
        return redirect("stock:batch_import_preview")
    return render(request, "stock/batch_import.html")


@login_required
def batch_import_preview(request):
    """Шаг 2: проверить сопоставление и количества, выбрать изделия для неопознанных строк, создать поставку."""
    draft = request.session.get(IMPORT_SESSION_KEY)
    if not draft:
        return redirect("stock:batch_import")
    rows = receipt_import.rows_from(draft)
    header = {
        "invoice": draft["header"].get("invoice", ""),
        "factory_orders": "", "number": "", "manufactured": "", "passport_number": "",
        "notes": "Загружено из файлов: " + ", ".join(draft.get("files", [])),
    }
    if request.method == "POST":
        header = {k: request.POST.get(k, v).strip() for k, v in header.items()}
        choices = {r.key: int(request.POST[f"product_{r.key}"]) for r in rows
                   if not r.product_id and request.POST.get(f"product_{r.key}", "").isdigit()}
        missing = [r for r in rows if not r.product_id and r.key not in choices]
        if missing:
            messages.error(request, f"Выберите изделие для строк: {', '.join(r.invoice_name or r.pl_name for r in missing)}.")
        else:
            batch = receipt_import.create_batch(draft, choices, header, remember=bool(request.POST.get("remember")), user=request.user)
            del request.session[IMPORT_SESSION_KEY]
            messages.success(request, f"Поставка создана: {batch}. Товар «в пути» — примите его, когда придёт на склад.")
            return redirect("stock:batch_detail", pk=batch.pk)
    products = Product.objects.in_bulk([r.product_id for r in rows if r.product_id] + [c for r in rows for c in r.candidate_ids])
    for r in rows:
        r.product = products.get(r.product_id)
        r.candidates = [products[c] for c in r.candidate_ids if c in products]
    return render(request, "stock/batch_import_preview.html", {
        "draft": draft, "h": draft["header"], "rows": rows, "header": header,
        "warnings": receipt_import.draft_warnings(draft, rows),
        "all_products": Product.objects.select_related("customer").order_by("customer__name", "code"),
        "unmatched": sum(1 for r in rows if not r.product_id),
        "total_qty": sum(r.qty or 0 for r in rows),
    })


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
    shipments = Shipment.objects.select_related("customer").annotate(qty=Sum("lines__qty")).order_by("-date", "-created_at")
    if request.GET.get("customer"):
        shipments = shipments.filter(customer_id=request.GET["customer"])
    if request.GET.get("month"):
        y, m = map(int, request.GET["month"].split("-"))
        shipments = shipments.filter(date__year=y, date__month=m)
    return render(request, "stock/shipment_list.html", {"shipments": shipments, "customers": Customer.objects.all(), "filters": request.GET})


def _paperwork_warnings(shipment, lines, boxes):
    warnings = []
    for batch in {line.batch for line in lines}:
        missing = [label for label, value in (("номер партии", batch.number), ("номер паспорта", batch.passport_number),
                                              ("дату изготовления", batch.manufactured)) if not value]
        if missing:
            warnings.append(f"В партии «{batch}» не указаны: {', '.join(missing)} — заполните в карточке партии.")
    for box in boxes:
        if box.gross_kg is None:
            warnings.append(f"{box.product.code} (партия «{box.batch.short_label}»): нет данных о коробке — масса на бирке будет пустой. Добавьте коробку в карточке партии.")
    if not shipment.ttn_number:
        warnings.append("Не указан номер ТТН — в паспорте поле останется пустым.")
    if not shipment.customer.full_name:
        warnings.append(f"У заказчика «{shipment.customer}» нет полного наименования — в паспорт пойдёт краткое.")
    return warnings


@login_required
def shipment_detail(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("customer"), pk=pk)
    lines = list(shipment.lines.select_related("product", "batch"))
    total = sum((line.amount or 0) for line in lines)
    boxes = paperwork.box_plan(sorted(lines, key=lambda line: (line.product.code, line.batch_id)))
    return render(request, "stock/shipment_detail.html", {
        "shipment": shipment, "lines": lines, "total": total, "boxes": boxes,
        "box_total": sum(b.count for b in boxes),
        "paperwork_warnings": _paperwork_warnings(shipment, lines, boxes),
    })


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@login_required
def shipment_passport(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("customer"), pk=pk)
    name = f"Паспорт {shipment.customer} {shipment.date:%d.%m.%Y}" + (f" ТТН {shipment.ttn_number}" if shipment.ttn_number else "")
    return file_response(BytesIO(paperwork.passport_docx(shipment)), f"{name}.docx", content_type=DOCX)


@login_required
def shipment_labels(request, pk):
    shipment = get_object_or_404(Shipment.objects.select_related("customer"), pk=pk)
    name = f"Бирки {shipment.customer} {shipment.date:%d.%m.%Y}"
    return file_response(BytesIO(paperwork.labels_docx(shipment)), f"{name}.docx", content_type=DOCX)


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
    return file_response(shipment.ttn_scan.open("rb"), f"ТТН {shipment.ttn_number or shipment.pk}.{ext}", as_attachment=False)


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
    items = Adjustment.objects.select_related("customer").annotate(qty=Sum("lines__qty")).order_by("-date", "-created_at")
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
