import datetime as dt
import io
import zipfile
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import AliasForm, DrawingUploadForm, ProductForm
from sales import services as sales_services
from stock import pricing
from stock.models import Batch, ShipmentLine
from stock.services import batch_balances, in_transit, movements, on_hand

from .models import Customer, DrawingFile, Product, ProductAlias


def search_products(params):
    qs = Product.objects.select_related("customer").prefetch_related(
        Prefetch("drawing_files", queryset=DrawingFile.objects.only("id", "kind", "product_id"))
    )
    for term in (params.get("q") or "").lower().split():
        term = term.removeprefix("bsi") or term
        qs = qs.filter(Q(search_text__contains=term) | Q(search_text__contains=term.replace(",", ".")))
    for field in ("customer", "color", "kind", "status"):
        if params.get(field):
            qs = qs.filter(**{f"{field}_id" if field == "customer" else field: params[field]})
    return qs


@login_required
def product_list(request):
    products = list(search_products(request.GET))
    ids = [p.pk for p in products]
    balances = batch_balances(ids)
    have, transit = on_hand(ids, balances), in_transit(ids)
    prices = sales_services.sale_prices((p.pk, p.customer_id) for p in products)
    costs = pricing.can_see_costs(request.user)
    purchase, currency = pricing.purchase_figures(balances, ids, costs)
    for p in products:
        p.on_hand, p.in_transit = have.get(p.pk, 0), transit.get(p.pk, 0)
        p.price, p.price_source = prices[p.pk, p.customer_id]
        p.stock_amount = p.on_hand * p.price if p.price is not None and p.on_hand else None
        p.buy_price, p.buy_amount = purchase.get(p.pk, (None, None))
        p.margin_amount = p.stock_amount - p.buy_amount if costs and p.stock_amount and p.buy_amount else None
    if request.GET.get("in_stock"):
        products = [p for p in products if p.on_hand > 0]
    value = pricing.stock_value(balances) if costs else None
    return render(
        request,
        "catalog/product_list.html",
        {
            "products": products,
            "total_on_hand": sum(p.on_hand for p in products),
            "total_amount": sum((p.stock_amount or 0) for p in products),
            "total_buy": sum((p.buy_amount or 0) for p in products),
            "buy_currency": currency,
            "total_margin": sum((p.margin_amount or 0) for p in products) if costs else None,
            "no_price_qty": sum(p.on_hand for p in products if p.on_hand and p.price is None),
            "stock_value": value[1] if value else None,
            "stock_value_missing": value[2] if value else 0,
            "customers": Customer.objects.all(),
            "colors": Product.Color.choices,
            "kinds": Product.Kind.choices,
            "statuses": Product.Status.choices,
            "drawing_kinds": DrawingFile.Kind.choices,
            "filters": request.GET,
        },
    )


@login_required
def product_detail(request, pk):
    product = get_object_or_404(
        Product.objects.select_related("customer").prefetch_related("drawing_files", "aliases"), pk=pk
    )
    files = sorted(product.drawing_files.all(), key=lambda f: DRAWING_PRIORITY.index(f.kind))
    current = next((f for f in files if f.kind == request.GET.get("file")), files[0] if files else None)
    balances = batch_balances([product.pk])
    batches = [(b, q) for (p, b), q in balances.items() if q]
    reserved = sales_services.reserved([product.pk]).get(product.pk, 0)
    if request.GET.get("format") == "xlsx":
        from reports import excel

        rows = [[m.date, m.title, m.batch.short_label, m.qty, m.balance] for m in movements(product)]
        return excel.table("Движение", ["Дата", "Документ", "Партия", "Кол-во", "Остаток"], rows,
                           f"Движение {product.code}.xlsx".replace("/", "-"), caption=f"{product.code} · ЯПИБ {product.yapib}")
    return render(
        request,
        "catalog/product_detail.html",
        {
            "product": product,
            "files": files,
            "current": current,
            "alias_form": AliasForm(),
            "upload_form": DrawingUploadForm(),
            "movements": movements(product),
            "on_hand": sum(q for _, q in batches),
            "in_transit": in_transit([product.pk]).get(product.pk, 0),
            "batch_left": _batch_rows(batches),
            "price_rows": pricing.product_prices(product, balances),
            "sale_prices": _sale_prices(product),
            "spec_prices": _spec_prices(product),
            "reserved": reserved,
            "free": max(sum(q for _, q in batches) - reserved, 0),
            "stock_value": pricing.stock_value(balances)[1] if pricing.can_see_costs(request.user) else None,
        },
    )


def _spec_prices(product):
    """Цены в спецификациях (новые первыми) с остатком лимита."""
    lines = list(product.spec_lines.select_related("spec__contract__customer").order_by("-spec__date"))
    used = sales_services.spec_usage([line.pk for line in lines])
    today = dt.date.today()
    for line in lines:
        line.left = line.qty_limit - used.get(line.pk, 0) if line.qty_limit is not None else None
        line.active = line.spec.is_active(today)
    return lines


def _sale_prices(product):
    """Последняя цена продажи изделия каждому заказчику: [(заказчик, дата, цена)]."""
    last = {}
    lines = (ShipmentLine.objects.filter(product=product, price_byn__isnull=False)
             .select_related("shipment__customer").order_by("shipment__date", "pk"))
    for line in lines:
        last[line.shipment.customer_id] = (line.shipment.customer, line.shipment.date, line.price_byn)
    return sorted(last.values(), key=lambda r: r[0].name)


def _batch_rows(batches):
    objs = Batch.objects.in_bulk([b for b, _ in batches])
    rows = [(objs[b], q) for b, q in batches]
    return sorted(rows, key=lambda r: (r[0].received_date is None, r[0].received_date))


@login_required
def product_edit(request, pk=None):
    product = get_object_or_404(Product, pk=pk) if pk else None
    form = ProductForm(request.POST or None, instance=product)
    if request.method == "POST" and form.is_valid():
        product = form.save()
        messages.success(request, f"Сохранено: {product.code}")
        return redirect("catalog:product_detail", pk=product.pk)
    return render(request, "catalog/product_form.html", {"form": form, "product": product})


@login_required
@require_POST
def alias_add(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = AliasForm(request.POST, instance=ProductAlias(product=product, created_by=request.user))
    if form.is_valid():
        form.save()
        messages.success(request, f"Синоним «{form.instance.text}» добавлен.")
    else:
        messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("catalog:product_detail", pk=pk)


@login_required
@require_POST
def alias_delete(request, pk, alias_pk):
    alias = get_object_or_404(ProductAlias, pk=alias_pk, product_id=pk)
    alias.delete()
    alias.product.save()
    messages.success(request, f"Синоним «{alias.text}» удалён.")
    return redirect("catalog:product_detail", pk=pk)


@login_required
@require_POST
def drawing_upload(request, pk):
    product = get_object_or_404(Product, pk=pk)
    form = DrawingUploadForm(request.POST, request.FILES)
    if form.is_valid():
        upload = form.cleaned_data["file"]
        drawing = DrawingFile.objects.filter(product=product, kind=form.cleaned_data["kind"]).first() or DrawingFile(
            product=product, kind=form.cleaned_data["kind"]
        )
        if drawing.file:
            drawing.file.delete(save=False)
        drawing.file.save(upload.name, upload, save=False)
        drawing.original_name = upload.name
        drawing.build_preview()
        drawing.save()
        messages.success(request, f"Файл «{drawing.get_kind_display()}» загружен.")
        return redirect(f"{product.get_absolute_url()}?file={drawing.kind}")
    messages.error(request, " ".join(e for errs in form.errors.values() for e in errs))
    return redirect("catalog:product_detail", pk=pk)


def file_response(fh, filename, as_attachment=True, content_type=None):
    """FileResponse с русским именем файла и латинским запасным именем для старых браузеров."""
    resp = FileResponse(fh, as_attachment=as_attachment, filename=filename, content_type=content_type)
    ascii_name = filename.encode("ascii", "ignore").decode().strip(" ._-") or "file"
    if "." in filename and "." not in ascii_name:
        ascii_name += "." + filename.rsplit(".", 1)[-1]
    kind = "attachment" if as_attachment else "inline"
    resp["Content-Disposition"] = f'{kind}; filename="{ascii_name}"; filename*=utf-8\'\'{quote(filename)}'
    return resp


def _drawing_response(drawing, as_attachment):
    if not drawing.file or not drawing.file.storage.exists(drawing.file.name):
        raise Http404("Файл чертежа не найден")
    return file_response(drawing.file.open("rb"), drawing.download_name, as_attachment)


@login_required
def drawing_view(request, file_pk):
    return _drawing_response(get_object_or_404(DrawingFile.objects.select_related("product"), pk=file_pk), False)


@login_required
def drawing_preview(request, file_pk):
    drawing = get_object_or_404(DrawingFile, pk=file_pk)
    if not drawing.preview or not drawing.preview.storage.exists(drawing.preview.name):
        raise Http404("Нет картинки чертежа")
    return FileResponse(drawing.preview.open("rb"), content_type="image/png")


@login_required
def drawing_download(request, file_pk):
    return _drawing_response(get_object_or_404(DrawingFile.objects.select_related("product"), pk=file_pk), True)


DRAWING_PRIORITY = [DrawingFile.Kind.PDF, DrawingFile.Kind.SIGNED, DrawingFile.Kind.CUSTOMER, DrawingFile.Kind.CHINA]


@login_required
def drawings_archive(request):
    """ZIP с чертежами выбранных изделий — для отправки в Китай или заказчику.

    Для каждого изделия берётся файл выбранного вида; если его нет — следующий по важности
    (PDF → скан с подписями → чертёж заказчика → чертёж завода), чтобы в архив попали все изделия.
    """
    ids = [i for i in request.GET.getlist("ids") if i.isdigit()]
    kind = request.GET.get("kind") or DrawingFile.Kind.PDF
    order = [kind] + [k for k in DRAWING_PRIORITY if k != kind]
    by_product = {}
    for d in DrawingFile.objects.filter(product_id__in=ids).select_related("product"):
        if d.file and d.file.storage.exists(d.file.name):
            by_product.setdefault(d.product_id, []).append(d)
    drawings = [min(files, key=lambda d: order.index(d.kind)) for files in by_product.values()]
    if not drawings:
        messages.error(request, "У выбранных изделий нет чертежей.")
        return redirect("catalog:product_list")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for d in drawings:
            with d.file.open("rb") as fh:
                zf.writestr(d.download_name, fh.read())
    buf.seek(0)
    return file_response(buf, f"Чертежи BSI ({len(drawings)} шт.).zip", content_type="application/zip")
