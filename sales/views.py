import datetime as dt

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from catalog.models import Customer
from catalog.views import file_response
from stock.forms import filled
from stock.models import ShipmentLine

from . import services
from .forms import ContractForm, RequestForm, RequestLineFormSet, SpecificationForm, SpecLineFormSet
from .models import Contract, CustomerRequest, RequestLine, SpecLine, Specification


def _edit_lines(request, form, formset_cls, initial, customer, paste_fields):
    """Форма документа со строками + «вставить из Excel».

    Возвращает (formset, rows | None, extra_context). rows — заполненные строки, если нажато
    «Сохранить» и всё верно; при «Разобрать вставку» строки добавляются в таблицу для проверки.
    """
    ctx = {}
    if request.method == "POST" and request.POST.get("action") == "paste":
        current = formset_cls(request.POST, prefix="lines")
        keep = [f.cleaned_data for f in current.forms if f.is_valid() and f.cleaned_data.get("product")]
        pasted, unknown = services.parse_pasted(request.POST.get("paste", ""), customer)
        have = {r["product"].pk for r in keep}
        added = [paste_fields(r) for r in pasted if r.product.pk not in have]
        ctx.update(pasted=len(added), unknown=unknown, paste_text="\n".join(unknown))
        return formset_cls(initial=keep + added, prefix="lines"), None, ctx
    formset = formset_cls(request.POST or None, initial=initial, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        rows = filled(formset)
        products = [r["product"].pk for r in rows]
        if len(products) != len(set(products)):
            messages.error(request, "Одно изделие указано дважды — объедините строки.")
        elif not rows:
            messages.error(request, "Добавьте хотя бы одно изделие.")
        else:
            return formset, rows, ctx
    return formset, None, ctx


def _protected_file(field, name):
    if not field or not field.storage.exists(field.name):
        raise Http404("Файл не загружен")
    ext = field.name.rsplit(".", 1)[-1]
    return file_response(field.open("rb"), f"{name}.{ext}", as_attachment=False)


# --- Договоры и спецификации ---

@login_required
def contract_list(request):
    customers = []
    for customer in Customer.objects.prefetch_related("contracts__specifications__lines"):
        contracts = list(customer.contracts.all())
        if contracts:
            customers.append((customer, contracts))
    usage = services.spec_usage()
    for _, contracts in customers:
        for contract in contracts:
            for spec in contract.specifications.all():
                lines = list(spec.lines.all())
                limits = [line for line in lines if line.qty_limit]
                spec.positions = len(lines)
                spec.limit_total = sum(line.qty_limit for line in limits)
                spec.shipped_total = sum(min(usage.get(line.pk, 0), line.qty_limit) for line in limits)
                spec.percent = round(spec.shipped_total * 100 / spec.limit_total) if spec.limit_total else None
                spec.active = spec.is_active(dt.date.today())
    return render(request, "sales/contract_list.html", {"customers": customers})


@login_required
def contract_edit(request, pk=None):
    contract = get_object_or_404(Contract, pk=pk) if pk else Contract()
    form = ContractForm(request.POST or None, request.FILES or None, instance=contract,
                        initial={"customer": request.GET.get("customer")})
    if request.method == "POST" and form.is_valid():
        contract = form.save()
        messages.success(request, f"Сохранено: {contract}.")
        return redirect(contract)
    return render(request, "sales/contract_form.html", {"form": form, "contract": contract if pk else None})


@login_required
def contract_detail(request, pk):
    contract = get_object_or_404(Contract.objects.select_related("customer"), pk=pk)
    return render(request, "sales/contract_detail.html", {
        "contract": contract, "specs": contract.specifications.prefetch_related("lines"),
    })


@login_required
def contract_file(request, pk):
    contract = get_object_or_404(Contract, pk=pk)
    return _protected_file(contract.file, f"Договор {contract.number}")


@login_required
def spec_edit(request, contract_pk=None, pk=None):
    spec = get_object_or_404(Specification.objects.select_related("contract__customer"), pk=pk) if pk else None
    contract = spec.contract if spec else get_object_or_404(Contract.objects.select_related("customer"), pk=contract_pk)
    source = spec
    if not spec and request.GET.get("copy", "").isdigit():  # новая спецификация на основе прежней
        source = get_object_or_404(Specification, pk=request.GET["copy"], contract__customer=contract.customer)
    initial = [{"product": line.product, "price_byn": line.price_byn, "qty_limit": line.qty_limit}
               for line in source.lines.select_related("product")] if source else []
    form = SpecificationForm(request.POST or None, request.FILES or None, instance=spec or Specification(contract=contract),
                             initial={} if spec else {"date": dt.date.today()})
    formset, rows, ctx = _edit_lines(request, form, SpecLineFormSet, initial, contract.customer,
                                     lambda r: {"product": r.product, "price_byn": r.price, "qty_limit": r.qty})
    if rows is not None:
        with transaction.atomic():
            spec = form.save()
            keep = {r["product"].pk for r in rows}
            spec.lines.exclude(product_id__in=keep).delete()  # отгрузки по удалённым строкам останутся без спецификации
            for r in rows:
                SpecLine.objects.update_or_create(spec=spec, product=r["product"],
                                                  defaults={"price_byn": r["price_byn"], "qty_limit": r["qty_limit"]})
        messages.success(request, f"Сохранено: {spec}.")
        return redirect(spec)
    return render(request, "sales/spec_form.html", {"form": form, "formset": formset, "contract": contract, "spec": spec, **ctx})


@login_required
def spec_detail(request, pk):
    spec = get_object_or_404(Specification.objects.select_related("contract__customer"), pk=pk)
    rows = services.spec_rows(spec)
    if request.GET.get("format") == "xlsx":
        from reports import excel

        data = [[l.product.code, l.product.yapib, l.price_byn, l.qty_limit, l.shipped, l.left, l.on_hand] for l in rows]
        return excel.table("Спецификация", ["Изделие", "ЯПИБ", "Цена без НДС", "Лимит", "Отгружено", "Осталось по лимиту", "На складе"],
                           data, f"{spec.customer} спецификация {spec.number}.xlsx".replace("/", "-"),
                           caption=f"{spec.customer} · {spec.contract} · {spec}")
    unlinked = ShipmentLine.objects.filter(spec_line__isnull=True, product_id__in=[line.product_id for line in rows],
                                           shipment__customer=spec.customer, shipment__date__gte=spec.date)
    if spec.valid_until:
        unlinked = unlinked.filter(shipment__date__lte=spec.valid_until)
    unlinked = unlinked.count()
    return render(request, "sales/spec_detail.html", {
        "spec": spec, "rows": rows, "active": spec.is_active(dt.date.today()), "unlinked": unlinked,
        "others": services.specs_for(spec.customer).exclude(pk=spec.pk)[:10],
    })


@login_required
@require_POST
def spec_link_past(request, pk):
    spec = get_object_or_404(Specification, pk=pk)
    count = services.link_past_shipments(spec)
    messages.success(request, f"Привязано строк отгрузок: {count}." if count else "Подходящих отгрузок без спецификации нет.")
    return redirect(spec)


@login_required
@require_POST
def spec_toggle_closed(request, pk):
    spec = get_object_or_404(Specification, pk=pk)
    spec.closed = not spec.closed
    spec.save(update_fields=["closed"])
    messages.success(request, "Спецификация закрыта — в новые отгрузки не подставляется." if spec.closed else "Спецификация снова действует.")
    return redirect(spec)


@login_required
@require_POST
def spec_delete(request, pk):
    spec = get_object_or_404(Specification, pk=pk)
    contract = spec.contract
    spec.delete()
    messages.success(request, "Спецификация удалена; отгрузки по ней остались, но без привязки.")
    return redirect(contract)


@login_required
def spec_file(request, pk):
    spec = get_object_or_404(Specification, pk=pk)
    return _protected_file(spec.file, f"Спецификация {spec.number}")


# --- Заявки ---

@login_required
def request_list(request):
    status = request.GET.get("status", CustomerRequest.Status.OPEN)
    items = CustomerRequest.objects.select_related("customer").prefetch_related("lines")
    if status:
        items = items.filter(status=status)
    if request.GET.get("customer", "").isdigit():
        items = items.filter(customer_id=request.GET["customer"])
    items = list(items)
    if request.GET.get("format") == "xlsx":
        from reports import excel

        rows = []
        for item in items:
            for line in services.request_rows(item):
                rows.append([item.date, item.customer.name, item.number, item.get_status_display(), item.due_date, line.product.code,
                             line.qty, line.shipped, line.left, line.on_hand, line.short])
        return excel.table("Заявки", ["Дата", "Заказчик", "Номер", "Статус", "Срок", "Изделие", "Заказано", "Отгружено", "Осталось",
                                      "На складе", "Не хватает"], rows, f"Заявки {dt.date.today():%Y-%m-%d}.xlsx")
    for item in items:
        item.summary = services.request_summary(services.request_rows(item))
    return render(request, "sales/request_list.html", {
        "items": items, "status": status, "statuses": CustomerRequest.Status.choices,
        "customers": Customer.objects.all(), "filters": request.GET,
    })


@login_required
def request_edit(request, pk=None):
    req = get_object_or_404(CustomerRequest, pk=pk) if pk else None
    initial = [{"product": line.product, "qty": line.qty, "price_byn": line.price_byn}
               for line in req.lines.select_related("product")] if req else []
    form = RequestForm(request.POST or None, request.FILES or None, instance=req or CustomerRequest(),
                       initial={} if req else {"date": dt.date.today(), "customer": request.GET.get("customer")})
    customer = req.customer if req else Customer.objects.filter(pk=request.POST.get("customer") or 0).first()
    formset, rows, ctx = _edit_lines(request, form, RequestLineFormSet, initial, customer,
                                     lambda r: {"product": r.product, "qty": r.qty, "price_byn": None})
    if rows is not None:
        with transaction.atomic():
            req = form.save(commit=False)
            if not req.pk:
                req.created_by = request.user
            req.save()
            req.lines.all().delete()
            for r in rows:
                RequestLine.objects.create(request=req, product=r["product"], qty=r["qty"], price_byn=r["price_byn"])
        messages.success(request, f"Сохранено: {req}.")
        return redirect(req)
    return render(request, "sales/request_form.html", {"form": form, "formset": formset, "req": req, **ctx})


@login_required
def request_detail(request, pk):
    req = get_object_or_404(CustomerRequest.objects.select_related("customer"), pk=pk)
    rows = services.request_rows(req)
    return render(request, "sales/request_detail.html", {
        "req": req, "rows": rows, "summary": services.request_summary(rows),
        "shipments": req.shipments.order_by("date"),
        "statuses": CustomerRequest.Status.choices,
        "ids": "&".join(f"ids={line.product_id}" for line in rows),
    })


@login_required
@require_POST
def request_status(request, pk):
    req = get_object_or_404(CustomerRequest, pk=pk)
    if request.POST.get("status") in CustomerRequest.Status.values:
        req.status = request.POST["status"]
        req.save(update_fields=["status"])
        messages.success(request, f"Статус заявки: {req.get_status_display()}.")
    return redirect(req)


@login_required
@require_POST
def request_delete(request, pk):
    req = get_object_or_404(CustomerRequest, pk=pk)
    req.delete()
    messages.success(request, "Заявка удалена; отгрузки по ней остались.")
    return redirect(reverse("sales:request_list"))


@login_required
def request_file(request, pk):
    req = get_object_or_404(CustomerRequest, pk=pk)
    return _protected_file(req.file, f"Заявка {req.number or req.pk}")


# --- Заказ в Китай ---

@login_required
def china_order(request):
    """Подсказка заказа под открытые заявки → текст для WeChat, чертежи, поставка «в пути»."""
    from urllib.parse import urlencode

    from catalog.models import Product
    from stock.models import Batch, ReceiptLine

    from . import china
    from .forms import OrderLineFormSet

    rows = china.suggestions()
    rows += china.low_stock_suggestions(exclude={r["product"].pk for r in rows})
    extra = OrderLineFormSet(request.POST or None, prefix="extra")
    ctx = {"rows": rows, "extra": extra, "today": dt.date.today()}
    if request.method == "POST":
        chosen = {}
        for key, value in request.POST.items():
            if key.startswith("qty_") and request.POST.get("use_" + key[4:]) and value.strip().isdigit() and int(value) > 0:
                chosen[int(key[4:])] = int(value)
        if extra.is_valid():
            for r in filled(extra):
                if r.get("qty"):
                    chosen[r["product"].pk] = chosen.get(r["product"].pk, 0) + r["qty"]
        products = Product.objects.in_bulk(list(chosen))
        lines = [{"product": products[pk], "qty": qty} for pk, qty in chosen.items() if pk in products]
        lines.sort(key=lambda line: line["product"].code)
        if not lines:
            messages.error(request, "Отметьте изделия и укажите количество.")
        elif request.POST.get("action") == "zip":
            return redirect(reverse("catalog:drawings_archive") + "?" + urlencode([("kind", "china")] + [("ids", line["product"].pk) for line in lines]))
        elif request.POST.get("action") == "batch":
            prices = china.last_factory_prices(list(chosen))
            with transaction.atomic():
                batch = Batch.objects.create(status=Batch.Status.IN_TRANSIT, factory_orders=request.POST.get("factory_orders", "").strip(),
                                             notes=f"Заказ в Китай от {dt.date.today():%d.%m.%Y} (сервис). Когда придёт инвойс — загрузите его в эту поставку.")
                for line in lines:
                    ReceiptLine.objects.create(batch=batch, product=line["product"], qty_expected=line["qty"], price_cny=prices.get(line["product"].pk))
            messages.success(request, f"Заказ сохранён как поставка «в пути»: {batch}. Количество видно в колонке «в пути».")
            return redirect(batch)
        else:
            ctx.update(text=china.wechat_text(lines, dt.date.today()), chosen=chosen)
    ctx.setdefault("chosen", {r["product"].pk: r["suggested"] for r in rows if r["reason"] == "requests"})
    for r in rows:
        r["qty"] = ctx["chosen"].get(r["product"].pk, r["suggested"])
        r["use"] = r["product"].pk in ctx["chosen"]
    ctx["has_low"] = any(r["reason"] == "low" for r in rows)
    return render(request, "sales/china_order.html", ctx)
