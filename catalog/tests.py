import csv
import io
import zipfile
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.urls import reverse

from .codes import normalize_name, parse_parametric
from .models import Customer, DrawingFile, Product, ProductAlias, find_product

PDF = b"%PDF-1.4\n%test\n"


@pytest.fixture(autouse=True)
def media(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "media"


@pytest.fixture
def mtz(db):
    return Customer.objects.create(name="МТЗ")


@pytest.fixture
def client_in(client, django_user_model):
    user = django_user_model.objects.create_user("manager", password="pass12345")
    client.force_login(user)
    return client


# --- Нормализация: снимаем только безопасные отличия (docs/09) ---

@pytest.mark.parametrize(
    "a,b",
    [
        ("BSI 63,5", "BSI 63.5"),
        ("Патрубок силиконовый BSI D75L100-2 (Китай)", "BSI D75L100-2"),
        ("bsi  d60l80-2", "BSI D60L80-2"),
        ("D63L80-3", "BSI D63L80-3"),
    ],
)
def test_safe_differences_are_equal(a, b):
    assert normalize_name(a) == normalize_name(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("BSI 90-60", "BSI 90/60"),  # переход Ø90→60 vs угол 90° Ø60
        ("BSI 90-38", "BSI 90-38-01"),  # разные колена ММЗ
        ("BSI D60L80-2", "BSI D60L80-3"),  # разный цвет = разный чертёж
        ("BSI D63,5", "BSI 63,5"),  # «D» не убираем автоматически
        ("63L80-2", "BSI D63L80-2"),
    ],
)
def test_different_products_are_not_merged(a, b):
    assert normalize_name(a) != normalize_name(b)


def test_parse_parametric_codes():
    p = parse_parametric("BSI 90-D75/50L100/100-3")
    assert (p.kind, p.angle, p.d1, p.d2, p.l1, p.l2, p.color) == (
        "angle_reducer", 90, Decimal("75"), Decimal("50"), Decimal("100"), Decimal("100"), "red",
    )
    assert parse_parametric("BSI D90L180-COR-2").kind == "corrugated"
    assert parse_parametric("BSI D80/90L140-3").kind == "reducer"
    assert parse_parametric("BSI D54L80-1").color == "black"
    assert parse_parametric("BSI 90-38-01") is None  # система ММЗ — параметры только на чертеже


# --- Сопоставление и синонимы ---

def test_find_product_exact_code_and_alias(mtz):
    reducer = Product.objects.create(code="BSI 90/60", yapib="24.1155", customer=mtz)
    elbow = Product.objects.create(code="BSI 90-38", yapib="24.1162", customer=mtz)
    assert find_product("BSI 90-60") is None  # без подтверждённого синонима — не угадываем
    ProductAlias.objects.create(product=reducer, text="BSI 90-60", source="invoice")
    assert find_product("Патрубок силиконовый BSI 90-60") == reducer
    assert find_product("BSI 90-38") == elbow
    assert find_product("BSI 90-38-01") is None


def test_alias_cannot_point_to_another_products_code(mtz):
    a = Product.objects.create(code="BSI D60L80-2", customer=mtz)
    Product.objects.create(code="BSI D60L80-3", customer=mtz)
    with pytest.raises(ValidationError):
        ProductAlias(product=a, text="D60L80-3").full_clean(exclude=["normalized"])


def test_alias_cannot_be_reused(mtz):
    a = Product.objects.create(code="BSI 90/60", customer=mtz)
    b = Product.objects.create(code="BSI 90-38", customer=mtz)
    ProductAlias.objects.create(product=a, text="BSI 90-60")
    with pytest.raises(ValidationError):
        ProductAlias(product=b, text="bsi 90-60").full_clean(exclude=["normalized"])


def test_alias_is_searchable(mtz):
    p = Product.objects.create(code="BSI 90-D38L150/140-1", customer=mtz)
    ProductAlias.objects.create(product=p, text="38*150*140 90度", source="carton")
    p.refresh_from_db()
    assert "38*150*140" in p.search_text


# --- Экраны ---

def test_pages_require_login(client, db):
    assert client.get(reverse("catalog:product_list")).status_code == 302


def test_search_by_code_yapib_customer_color(client_in, mtz):
    Product.objects.create(code="BSI D60L80-3", yapib="24.1114", customer=mtz, color="red")
    Product.objects.create(code="BSI 90-38-01", yapib="24.1163", color="blue")
    url = reverse("catalog:product_list")
    for q, expected in [("D60", "D60L80-3"), ("24.1163", "90-38-01"), ("мтз", "D60L80-3"), ("красный", "D60L80-3")]:
        body = client_in.get(url, {"q": q}).content.decode()
        assert expected in body
    assert "90-38-01" not in client_in.get(url, {"q": "D60"}).content.decode()


def test_search_decimal_comma_or_dot(client_in, mtz):
    Product.objects.create(code="BSI 63,5", customer=mtz)
    assert "BSI 63,5" in client_in.get(reverse("catalog:product_list"), {"q": "63.5"}).content.decode()


def _with_drawing(product, kind="pdf", name="d.pdf"):
    d = DrawingFile(product=product, kind=kind)
    d.file.save(name, ContentFile(PDF))
    return d


def test_drawing_view_inline_and_download(client_in, mtz):
    p = Product.objects.create(code="BSI 90/60", yapib="24.1155", customer=mtz)
    d = _with_drawing(p)
    view = client_in.get(reverse("catalog:drawing_view", args=[d.pk]))
    assert view.status_code == 200 and "inline" in view["Content-Disposition"]
    dl = client_in.get(reverse("catalog:drawing_download", args=[d.pk]))
    assert "attachment" in dl["Content-Disposition"]
    assert b"".join(dl.streaming_content) == PDF


def test_archive_contains_selected_drawings_with_fallback(client_in, mtz):
    a = Product.objects.create(code="BSI 90/60", yapib="24.1155", customer=mtz)
    b = Product.objects.create(code="BSI D76L160-COR-3", yapib="24.1184", customer=mtz)
    _with_drawing(a)
    _with_drawing(b)
    _with_drawing(b, kind="signed", name="scan.pdf")
    resp = client_in.get(reverse("catalog:drawings_archive"), {"ids": [a.pk, b.pk], "kind": "signed"})
    names = zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content))).namelist()
    assert sorted(names) == ["24.1155 BSI 90_60.pdf", "24.1184 BSI D76L160-COR-3 (скан с подписями).pdf"]


def test_upload_replaces_drawing(client_in, mtz):
    p = Product.objects.create(code="BSI D54L80-1", customer=mtz)
    url = reverse("catalog:drawing_upload", args=[p.pk])
    for content in (b"%PDF-1 old", b"%PDF-1 new"):
        client_in.post(url, {"kind": "pdf", "file": ContentFile(content, name="x.pdf")})
    assert DrawingFile.objects.filter(product=p).count() == 1
    assert p.drawing_files.get().file.read() == b"%PDF-1 new"


def test_add_alias_via_card_rejects_conflict(client_in, mtz):
    a = Product.objects.create(code="BSI D60L80-2", customer=mtz)
    Product.objects.create(code="BSI D60L80-3", customer=mtz)
    url = reverse("catalog:alias_add", args=[a.pk])
    client_in.post(url, {"text": "BSI D60L80-3", "source": "invoice"})
    client_in.post(url, {"text": "60*80 蓝", "source": "carton"})
    assert list(a.aliases.values_list("text", flat=True)) == ["60*80 蓝"]


# --- Загрузка справочника ---

def test_seed_and_import_drawings(tmp_path, db):
    root = tmp_path / "Патрубки"
    (root / "МТЗ").mkdir(parents=True)
    (root / "МТЗ" / "a.pdf").write_bytes(PDF)
    catalog = tmp_path / "catalog.csv"
    fields = ["yapib", "code", "customer", "status", "kind", "angle", "d1", "d2", "l1", "l2", "color", "drawing_pdf", "drawing_signed", "drawing_china", "drawing_customer", "notes"]
    with open(catalog, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerow(dict.fromkeys(fields, "") | {"yapib": "24.1131", "code": "BSI D54L80-1", "customer": "МТЗ", "status": "serial", "drawing_pdf": "МТЗ/a.pdf"})
        w.writerow(dict.fromkeys(fields, "") | {"yapib": "24.1155", "code": "BSI 90/60", "customer": "ММЗ", "status": "sample", "kind": "reducer", "d1": "90", "d2": "60", "l1": "148", "color": "red"})
    aliases = tmp_path / "aliases.csv"
    aliases.write_text("yapib,text,source\n24.1155,BSI 90-60,invoice\n", encoding="utf-8")

    for _ in range(2):  # повторный запуск не плодит дублей
        call_command("seed_catalog", catalog=str(catalog), aliases=str(aliases), stdout=io.StringIO())
    call_command("import_drawings", str(root), catalog=str(catalog), stdout=io.StringIO())

    assert Product.objects.count() == 2 and ProductAlias.objects.count() == 1
    d54 = Product.objects.get(yapib="24.1131")
    assert (d54.kind, d54.color, d54.d1) == ("straight", "black", Decimal("54"))
    assert Product.objects.get(yapib="24.1155").size_label == "Ø90→60 · 148"
    assert d54.drawing_files.get().file.read() == PDF


def test_upload_builds_png_preview(client_in, mtz):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "BSI 90/60")
    pdf = doc.tobytes()
    p = Product.objects.create(code="BSI 90/60", customer=mtz)
    client_in.post(reverse("catalog:drawing_upload", args=[p.pk]), {"kind": "pdf", "file": ContentFile(pdf, name="d.pdf")})
    drawing = p.drawing_files.get()
    resp = client_in.get(reverse("catalog:drawing_preview", args=[drawing.pk]))
    assert resp["Content-Type"] == "image/png"
    assert b"".join(resp.streaming_content).startswith(b"\x89PNG")


def test_cyrillic_lookalikes_are_safe():
    assert normalize_name("QЕ135Е50-CID50L=100х100") == normalize_name("QE135E50-CID50L=100x100")


def test_same_code_two_drawings_needs_customer(db):
    mtz = Customer.objects.create(name="МТЗ")
    amk = Customer.objects.create(name="Амкадор")
    a = Product.objects.create(code="BSI D63L80-2", yapib="24.1115", customer=mtz)
    b = Product.objects.create(code="BSI D63L80-2", yapib="26.1497", customer=amk)
    assert find_product("BSI D63L80-2") is None  # два чертежа — не угадываем
    assert find_product("BSI D63L80-2", customer=mtz) == a
    assert find_product("Патрубок силиконовый BSI D63L80-2 (Китай)", customer=amk) == b


def test_import_single_page_from_multipage_pdf(tmp_path, db):
    import pymupdf

    from .management.commands.import_drawings import read_ref

    doc = pymupdf.open()
    for text in ("letter", "D127", "D102"):
        doc.new_page().insert_text((72, 72), text)
    (tmp_path / "pack.pdf").write_bytes(doc.tobytes())
    name, data = read_ref(tmp_path, "pack.pdf#page=3")
    with pymupdf.open(stream=data, filetype="pdf") as one:
        assert one.page_count == 1 and "D102" in one[0].get_text()
    assert name == "pack стр.3.pdf"
    assert read_ref(tmp_path, "missing.pdf") is None


def test_card_shows_bsi_drawing_before_factory_drawing(client_in, mtz):
    p = Product.objects.create(code="BSI D50L100-2", yapib="26.1490", customer=mtz)
    _with_drawing(p, kind="china", name="q.pdf")
    signed = _with_drawing(p, kind="signed", name="s.pdf")
    resp = client_in.get(reverse("catalog:product_detail", args=[p.pk]))
    assert resp.context["current"] == signed


def test_archive_falls_back_when_kind_missing(client_in, mtz):
    scan_only = Product.objects.create(code="BSI D60L1000-2", yapib="25.1333", customer=mtz)
    _with_drawing(scan_only, kind="signed", name="s.pdf")
    _with_drawing(scan_only, kind="china", name="q.pdf")
    resp = client_in.get(reverse("catalog:drawings_archive"), {"ids": [scan_only.pk], "kind": "pdf"})
    names = zipfile.ZipFile(io.BytesIO(b"".join(resp.streaming_content))).namelist()
    assert names == ["25.1333 BSI D60L1000-2 (скан с подписями).pdf"]


def test_download_names_have_ascii_fallback(client_in, mtz):
    p = Product.objects.create(code="BSI 90/60", yapib="24.1155", customer=mtz)
    d = _with_drawing(p)
    cd = client_in.get(reverse("catalog:drawings_archive"), {"ids": [p.pk]})["Content-Disposition"]
    assert cd.startswith('attachment; filename="BSI') and "filename*=utf-8''" in cd and cd.count(".zip") == 2
    cd = client_in.get(reverse("catalog:drawing_view", args=[d.pk]))["Content-Disposition"]
    assert cd.startswith('inline; filename="24.1155 BSI 90_60.pdf"')
