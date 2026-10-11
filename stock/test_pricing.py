"""Цены и прибыль (этап 5)."""

import datetime as dt
from decimal import Decimal

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from catalog.models import Customer, Product

from . import pricing, services
from .models import Batch, ReceiptLine, Shipment


@pytest.fixture
def mtz(db):
    return Customer.objects.create(name="МТЗ")


@pytest.fixture
def product(mtz):
    return Product.objects.create(code="BSI D75L100-2", yapib="24.1119", customer=mtz)


def _user(django_user_model, name, role):
    call_command("setup_roles")
    user = django_user_model.objects.create_user(name, password="pass12345")
    user.groups.add(Group.objects.get(name=role))
    return user


@pytest.fixture
def boss(client, django_user_model):
    client.force_login(_user(django_user_model, "boss", "Руководитель"))
    return client


@pytest.fixture
def manager(client, django_user_model):
    client.force_login(_user(django_user_model, "manager", "Менеджер"))
    return client


def received(product, qty, date, price_cny=None, landed=None, **kw):
    batch = Batch.objects.create(status=Batch.Status.RECEIVED, received_date=date, **kw)
    ReceiptLine.objects.create(batch=batch, product=product, qty_expected=qty, qty_received=qty,
                               price_cny=price_cny, landed_price_byn=landed)
    return batch


def ship(customer, product, qty, price, date=dt.date(2025, 3, 1), batch=None):
    shipment = Shipment.objects.create(customer=customer, date=date)
    services.post_shipment(shipment, [{"product": product, "qty": qty, "price_byn": price, "batch": batch}])
    return shipment


def test_calc_landed_spreads_costs_by_value(mtz):
    a = Product.objects.create(code="A", customer=mtz)
    b = Product.objects.create(code="B", customer=mtz)
    batch = Batch.objects.create()
    la = ReceiptLine.objects.create(batch=batch, product=a, qty_expected=100, price_cny=Decimal("10"))
    lb = ReceiptLine.objects.create(batch=batch, product=b, qty_expected=50, price_cny=Decimal("20"))
    ReceiptLine.objects.create(batch=batch, product=Product.objects.create(code="C", customer=mtz), qty_expected=5)
    # стоимость: (100×10 + 50×20) × 0,45 = 900 BYN; расходы 450 → коэффициент 1,5
    prices, k = pricing.calc_landed(batch.lines.all(), Decimal("0.45"), Decimal("450"))
    assert k == Decimal("1.5") and prices == {la.pk: Decimal("6.75"), lb.pk: Decimal("13.50")}
    # без расходов — просто по курсу
    assert pricing.calc_landed(batch.lines.all(), Decimal("0.45"))[0][la.pk] == Decimal("4.50")


def test_profit_uses_batch_cost_and_falls_back_to_latest(product, mtz):
    old = received(product, 100, dt.date(2025, 1, 1))  # без учётной цены (как старые партии из Excel)
    new = received(product, 100, dt.date(2025, 2, 1), landed=Decimal("3.00"))
    s1 = ship(mtz, product, 10, Decimal("5.00"), batch=new)
    s2 = ship(mtz, product, 10, Decimal("5.00"), batch=old)
    l1, l2 = pricing.with_costs(list(s1.lines.all()) + list(s2.lines.all()))
    assert (l1.unit_cost, l1.cost_approx, l1.profit) == (Decimal("3.00"), False, Decimal("20.00"))
    assert (l2.unit_cost, l2.cost_approx) == (Decimal("3.00"), True)  # «≈»
    t = pricing.totals([l1, l2])
    assert (t.amount, t.cost, t.profit, t.margin, t.approx) == (Decimal("100.00"), Decimal("60.00"), Decimal("40.00"), Decimal("40.0"), 1)


def test_lines_without_cost_or_price_are_not_in_profit(product, mtz):
    received(product, 100, dt.date(2025, 1, 1))
    s = ship(mtz, product, 10, Decimal("5.00"))
    s2 = ship(mtz, product, 5, None)
    t = pricing.totals(pricing.with_costs(list(s.lines.all()) + list(s2.lines.all())))
    assert (t.amount, t.revenue, t.profit, t.qty_no_cost, t.qty_no_price, t.complete) == (Decimal("50.00"), 0, 0, 10, 5, False)


def test_stock_value(product, mtz):
    received(product, 100, dt.date(2025, 1, 1), landed=Decimal("2.00"))
    received(product, 10, dt.date(2025, 2, 1), landed=Decimal("3.00"))
    ship(mtz, product, 40, Decimal("5"))
    by_product, total, missing = pricing.stock_value(services.batch_balances())
    assert total == Decimal("150.00") and missing == 0  # 60 × 2 + 10 × 3


def test_manager_does_not_see_costs(manager, product, mtz):
    received(product, 100, dt.date(2025, 1, 1), price_cny=Decimal("6.6747474747"), landed=Decimal("3.47"))
    s = ship(mtz, product, 10, Decimal("5.00"))
    batch = Batch.objects.get()
    for url in (reverse("stock:shipment_detail", args=[s.pk]), reverse("stock:shipment_list"),
                reverse("stock:batch_detail", args=[batch.pk]), product.get_absolute_url(), reverse("catalog:product_list")):
        html = manager.get(url).content.decode()
        assert "Прибыль" not in html and "3,47" not in html and "Учётная" not in html, url
    assert "6,6747" in manager.get(reverse("stock:batch_detail", args=[batch.pk])).content.decode()  # цену завода видит
    assert manager.get(reverse("stock:profit_report")).status_code == 403
    assert manager.post(reverse("stock:batch_prices", args=[batch.pk]), {"landed_1": "9"}).status_code == 403


def test_boss_sees_profit_everywhere(boss, product, mtz):
    received(product, 100, dt.date(2025, 1, 1), landed=Decimal("3.47"))
    s = ship(mtz, product, 10, Decimal("5.00"))
    html = boss.get(reverse("stock:shipment_detail", args=[s.pk])).content.decode()
    assert "15,30" in html and "30,6 %" in html  # (5 − 3,47) × 10; 15,30 / 50
    assert "15,30" in boss.get(reverse("stock:shipment_list")).content.decode()
    assert "312,30" in boss.get(reverse("catalog:product_list")).content.decode()  # 90 × 3,47
    resp = boss.get(reverse("stock:profit_report"), {"date_from": "2025-01-01", "date_to": "2025-12-31"})
    assert "март 2025" in resp.content.decode() and resp.context["total"].profit == Decimal("15.30")
    xlsx = boss.get(reverse("stock:profit_report"), {"date_from": "2025-01-01", "date_to": "2025-12-31", "format": "xlsx"})
    import io

    wb = openpyxl.load_workbook(io.BytesIO(xlsx.content))
    assert wb.sheetnames == ["По месяцам", "По заказчикам", "По изделиям", "Строки отгрузок"]
    assert wb["По заказчикам"]["A3"].value == "МТЗ" and wb["По заказчикам"]["E3"].value == 15.3


def test_batch_prices_calc_and_manual(boss, product, mtz):
    batch = received(product, 100, dt.date(2025, 1, 1), price_cny=Decimal("10"))
    line = batch.lines.get()
    boss.post(reverse("stock:batch_prices", args=[batch.pk]), {"cny_rate": "0,45", "extra_costs_byn": "90", "action": "calc", f"landed_{line.pk}": ""})
    line.refresh_from_db()
    batch.refresh_from_db()
    assert line.landed_price_byn == Decimal("5.40") and batch.cny_rate == Decimal("0.45")  # 10 × 0,45 × 1,2
    boss.post(reverse("stock:batch_prices", args=[batch.pk]), {"cny_rate": "0,45", "extra_costs_byn": "90", "action": "save", f"landed_{line.pk}": "5,5"})
    line.refresh_from_db()
    assert line.landed_price_byn == Decimal("5.50")
    resp = boss.post(reverse("stock:batch_prices", args=[batch.pk]), {"cny_rate": "abc", f"landed_{line.pk}": "5"}, follow=True)
    assert "не число" in resp.content.decode()


def test_superuser_sees_costs(client, django_user_model, product):
    client.force_login(django_user_model.objects.create_superuser("root", password="pass12345"))
    assert client.get(reverse("stock:profit_report")).status_code == 200
