import io
import zipfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import AliasForm, DrawingUploadForm, ProductForm
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
    products = search_products(request.GET)
    return render(
        request,
        "catalog/product_list.html",
        {
            "products": products,
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
    return render(
        request,
        "catalog/product_detail.html",
        {
            "product": product,
            "files": files,
            "current": current,
            "alias_form": AliasForm(),
            "upload_form": DrawingUploadForm(),
        },
    )


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


def _drawing_response(drawing, as_attachment):
    if not drawing.file or not drawing.file.storage.exists(drawing.file.name):
        raise Http404("Файл чертежа не найден")
    return FileResponse(drawing.file.open("rb"), as_attachment=as_attachment, filename=drawing.download_name)


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
    return FileResponse(buf, as_attachment=True, filename=f"Чертежи BSI ({len(drawings)} шт.).zip")
