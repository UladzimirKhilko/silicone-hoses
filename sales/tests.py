"""Договоры и спецификации, заявки, заказ в Китай (этап 5)."""

import datetime as dt
from decimal import Decimal

import pytest
from django.urls import reverse

from catalog.models import Customer, Product, ProductAlias
from stock import services as stock
from stock.models import Batch, CartonSpec, ReceiptLine, Shipment, ShipmentLine

from . import china, services
from .models import Contract, CustomerRequest, RequestLine, SpecLine, Specification


@pytest.fixture
def mtz(db):
    return Customer.objects.create(name="МТЗ")


@pytest.fixture
def amk(db):
    return Customer.objects.create(name="Амкадор")


@pytest.fixture
def p75(mtz):
    return Product.objects.create(code="BSI D75L100-2", yapib="24.1119", customer=mtz, color="blue")


@pytest.fixture
def p90(mtz):
    return Product.objects.create(code="BSI 90/60", yapib="24.1155", customer=mtz)


@pytest.fixture
def client_in(client, django_user_model):
    client.force_login(django_user_model.objects.create_user("m", password="pass12345"))
    return client


def received(product, qty, date=dt.date(2025, 1, 1)):
    batch = Batch.objects.create(status=Batch.Status.RECEIVED, received_date=date)
    ReceiptLine.objects.create(batch=batch, product=product, qty_expected=qty, qty_received=qty)
    return batch


def spec(customer, number, date, lines, **kw):
    contract = Contract.objects.get_or_create(customer=customer, number="09/185")[0]
    s = Specification.objects.create(contract=contract, number=number, date=date, **kw)
    for product, price, limit in lines:
        SpecLine.objects.create(spec=s, product=product, price_byn=Decimal(price), qty_limit=limit)
    return s


def post_shipment(client, customer, rows, date="2025-07-01", **extra):
    data = {"customer": customer.pk, "date": date, "ttn_number": "", "notes": "", "lines-TOTAL_FORMS": str(len(rows)),
            "lines-INITIAL_FORMS": "0", **extra}
    for i, (product, qty, price) in enumerate(rows):
        data |= {f"lines-{i}-product": product.pk, f"lines-{i}-qty": qty, f"lines-{i}-price_byn": price, f"lines-{i}-batch": ""}
    return client.post(reverse("stock:shipment_create"), data, follow=True)


# --- Спецификации ---

def test_shipment_takes_price_from_newest_active_spec_and_tracks_limit(client_in, mtz, p75):
    received(p75, 1000)
    spec(mtz, "1", dt.date(2025, 1, 10), [(p75, "6.50", 500)])
    s2 = spec(mtz, "2", dt.date(2025, 6, 10), [(p75, "6.85", 300)])
    spec(mtz, "3", dt.date(2025, 9, 1), [(p75, "9.99", None)])  # ещё не действует на 01.07
    post_shipment(client_in, mtz, [(p75, 200, "")])
    line = ShipmentLine.objects.get()
    assert line.price_byn == Decimal("6.85") and line.spec_line.spec == s2
    resp = post_shipment(client_in, mtz, [(p75, 150, "")])
    assert "превышен лимит" in resp.content.decode()  # 350 из 300 — предупреждение, но отгрузка проведена
    rows = services.spec_rows(s2)
    assert (rows[0].shipped, rows[0].left, rows[0].percent) == (350, -50, 117)


def test_explicit_price_kept_and_difference_warned(client_in, mtz, p75):
    received(p75, 100)
    spec(mtz, "1", dt.date(2025, 1, 10), [(p75, "6.50", None)])
    resp = post_shipment(client_in, mtz, [(p75, 10, "7,00")])
    assert ShipmentLine.objects.get().price_byn == Decimal("7.00")
    assert "отличается от спецификации" in resp.content.decode()


def test_closed_or_other_customer_spec_not_used(client_in, mtz, amk, p75):
    received(p75, 100)
    spec(mtz, "1", dt.date(2025, 1, 10), [(p75, "6.50", None)], closed=True)
    spec(amk, "1", dt.date(2025, 1, 10), [(p75, "8.00", None)])
    post_shipment(client_in, mtz, [(p75, 10, "")])
    assert ShipmentLine.objects.get().spec_line is None


def test_chosen_spec_of_other_customer_rejected(client_in, mtz, amk, p75):
    received(p75, 100)
    other = spec(amk, "1", dt.date(2025, 1, 10), [(p75, "8.00", None)])
    resp = post_shipment(client_in, mtz, [(p75, 10, "")], specification=other.pk)
    assert "другого заказчика" in resp.content.decode() and not ShipmentLine.objects.exists()


def test_link_past_shipments(mtz, p75):
    received(p75, 100)
    for date in (dt.date(2025, 5, 1), dt.date(2025, 7, 1)):
        stock.post_shipment(Shipment.objects.create(customer=mtz, date=date), [{"product": p75, "qty": 10, "price_byn": Decimal("6")}])
    s = spec(mtz, "1", dt.date(2025, 6, 10), [(p75, "6.50", 100)])
    assert services.link_past_shipments(s) == 1  # только с даты спецификации
    assert services.spec_rows(s)[0].shipped == 10


def test_spec_form_with_paste(client_in, mtz, p75, p90):
    contract = Contract.objects.create(customer=mtz, number="09/185")
    ProductAlias.objects.create(product=p90, text="Патрубок BSI 90-60", source="other")
    pasted = "1\tBSI D75L100-2\tшт\t1000\t6,85\t6850,00\n2\tПатрубок BSI 90-60\t50\t28,57\n3\tнепонятное изделие\t5\t1"
    url = reverse("sales:spec_create", args=[contract.pk])
    base = {"number": "2", "date": "2025-06-10", "valid_until": "", "notes": "", "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0",
            "lines-0-product": "", "lines-0-price_byn": "", "lines-0-qty_limit": ""}
    resp = client_in.post(url, {**base, "action": "paste", "paste": pasted})
    initial = resp.context["formset"].initial
    assert [(r["product"], r["qty_limit"], r["price_byn"]) for r in initial] == [(p75, 1000, Decimal("6.85")), (p90, 50, Decimal("28.57"))]
    assert resp.context["unknown"] == ["3\tнепонятное изделие\t5\t1"]
    data = {**base, "action": "save", "lines-TOTAL_FORMS": "2", "lines-0-product": p75.pk, "lines-0-price_byn": "6,85",
            "lines-0-qty_limit": "1000", "lines-1-product": p90.pk, "lines-1-price_byn": "28,57", "lines-1-qty_limit": ""}
    assert client_in.post(url, data).status_code == 302
    s = Specification.objects.get()
    assert [(line.product, line.price_byn, line.qty_limit) for line in s.lines.all()] == [(p90, Decimal("28.57"), None), (p75, Decimal("6.85"), 1000)]
    # копия в новую спецификацию
    resp = client_in.get(url + f"?copy={s.pk}")
    assert len(resp.context["formset"].initial) == 2


def test_parse_pasted_yapib_and_number_rules(mtz, p75):
    rows, unknown = services.parse_pasted("ЯПИБ 24.1119   300   7.10\nBSI D75L100-2;6,85", mtz)
    assert [(r.product, r.qty, r.price) for r in rows] == [(p75, 300, Decimal("7.10")), (p75, None, Decimal("6.85"))]


def test_contract_and_spec_pages(client_in, mtz, p75):
    s = spec(mtz, "1", dt.date(2025, 1, 10), [(p75, "6.50", 500)])
    assert "Спецификация № 1" in client_in.get(reverse("sales:contract_list")).content.decode()
    assert "6,50" in client_in.get(s.get_absolute_url()).content.decode()
    assert "6,50" in client_in.get(p75.get_absolute_url()).content.decode()  # цена по спецификации в карточке


# --- Заявки ---

def make_request(customer, date, lines, **kw):
    req = CustomerRequest.objects.create(customer=customer, date=date, **kw)
    for product, qty in lines:
        RequestLine.objects.create(request=req, product=product, qty=qty)
    return req


def test_request_reserve_queue_and_fulfillment(mtz, amk, p75):
    received(p75, 100)
    first = make_request(amk, dt.date(2025, 6, 1), [(p75, 70)])
    second = make_request(mtz, dt.date(2025, 6, 5), [(p75, 50)])
    line = services.request_rows(second)[0]
    assert (line.reserved_before, line.free, line.covered, line.short, line.state) == (70, 30, 30, 20, "short")
    # первая заявка частично отгружена — резерв под неё уменьшился
    stock.post_shipment(Shipment.objects.create(customer=amk, date=dt.date(2025, 6, 10), request=first), [{"product": p75, "qty": 40}])
    line = services.request_rows(second)[0]
    assert (line.on_hand, line.reserved_before, line.short) == (60, 30, 20)
    assert services.request_rows(first)[0].shipped == 40
    assert services.reserved() == {p75.pk: 80}
    # отменённая заявка ничего не резервирует
    first.status = CustomerRequest.Status.CANCELLED
    first.save()
    assert services.request_rows(second)[0].short == 0


def test_shipment_from_request_prefills_remaining(client_in, mtz, p75):
    received(p75, 30)
    req = make_request(mtz, dt.date(2025, 6, 1), [(p75, 50)])
    resp = client_in.get(reverse("stock:shipment_create") + f"?request={req.pk}")
    assert resp.context["formset"].initial == [{"product": p75, "qty": 30, "price_byn": None}]  # сколько есть
    post_shipment(client_in, mtz, [(p75, 30, "6")], request=req.pk)
    page = client_in.get(req.get_absolute_url()).content.decode()
    assert "отгружено 30 шт · 60 %" in page and "не хватает 20 шт" in page


def test_request_create_with_paste_and_list(client_in, mtz, p75):
    url = reverse("sales:request_create")
    base = {"customer": mtz.pk, "date": "2025-06-01", "number": "T-15", "source": "tender", "due_date": "", "notes": "",
            "lines-TOTAL_FORMS": "1", "lines-INITIAL_FORMS": "0", "lines-0-product": "", "lines-0-qty": "", "lines-0-price_byn": ""}
    resp = client_in.post(url, {**base, "action": "paste", "paste": "BSI D75L100-2\t120"})
    assert resp.context["formset"].initial[0]["qty"] == 120
    resp = client_in.post(url, {**base, "action": "save", "lines-0-product": p75.pk, "lines-0-qty": "120"})
    req = CustomerRequest.objects.get()
    assert resp.status_code == 302 and req.lines.get().qty == 120
    assert "не хватает 120 шт" in client_in.get(reverse("sales:request_list")).content.decode()


# --- Заказ в Китай ---

def test_china_order_suggests_rounded_to_cartons_and_creates_order(client_in, mtz, p75, p90):
    batch = received(p75, 10)
    CartonSpec.objects.create(batch=batch, product=p75, qty_per_carton=85, gross_kg=Decimal("16"), cartons=10)
    ProductAlias.objects.create(product=p75, text="75*100", source="carton")
    transit = Batch.objects.create(status=Batch.Status.IN_TRANSIT)
    ReceiptLine.objects.create(batch=transit, product=p75, qty_expected=15, price_cny=Decimal("6.67"))
    make_request(mtz, dt.date(2025, 6, 1), [(p75, 200), (p90, 5)])
    rows = {r["product"]: r for r in china.suggestions()}
    assert (rows[p75]["short"], rows[p75]["suggested"]) == (175, 255)  # 200 − 10 − 15 → 3 коробки по 85
    assert rows[p90]["suggested"] == 5
    data = {f"use_{p75.pk}": "1", f"qty_{p75.pk}": "255", f"qty_{p90.pk}": "5",  # 90/60 не отмечено
            "extra-TOTAL_FORMS": "1", "extra-INITIAL_FORMS": "0", "extra-0-product": "", "extra-0-qty": ""}
    resp = client_in.post(reverse("sales:china_order"), {**data, "action": "text"})
    assert "1. BSI D75L100-2 (75*100, blue, drawing 24.1119) — 255 pcs" in resp.context["text"]
    assert "90/60" not in resp.context["text"]
    resp = client_in.post(reverse("sales:china_order"), {**data, "action": "zip"})
    assert resp.status_code == 302 and "kind=china" in resp.url
    client_in.post(reverse("sales:china_order"), {**data, "action": "batch", "factory_orders": "Q700"})
    order = Batch.objects.get(factory_orders="Q700")
    assert order.status == Batch.Status.IN_TRANSIT and order.lines.get().price_cny == Decimal("6.67")
    assert stock.in_transit()[p75.pk] == 270
