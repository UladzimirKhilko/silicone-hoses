"""Меню приложения: разделы, иконки, активный пункт, роль пользователя."""

from django.urls import reverse

from hoses.version import VERSION
from stock.pricing import can_see_costs

# (заголовок группы, [(название, url name, иконка, префиксы путей для подсветки, только руководителю)])
NAV = [
    ("", [("Сводка", "dashboard", "layout-dashboard", ("/$",), False)]),
    ("Склад", [
        ("Остатки", "catalog:product_list", "boxes", ("/products/",), False),
        ("Приходы", "stock:batch_list", "package-plus", ("/stock/batches/",), False),
        ("Отгрузки", "stock:shipment_list", "truck", ("/stock/shipments/",), False),
        ("Списания", "stock:adjustment_list", "package-minus", ("/stock/adjustments/",), False),
    ]),
    ("Продажи", [
        ("Заявки", "sales:request_list", "clipboard-list", ("/sales/requests/",), False),
        ("Договоры", "sales:contract_list", "file-text", ("/sales/contracts/", "/sales/specs/"), False),
        ("Заказ в Китай", "sales:china_order", "ship", ("/sales/china-order/",), False),
    ]),
    ("Аналитика", [
        ("Отчёты", "reports:index", "chart-column", ("/reports/",), False),
        ("Прибыль", "stock:profit_report", "trending-up", ("/stock/reports/profit/",), True),
    ]),
]

# нижняя панель на телефоне
TABS = ["dashboard", "catalog:product_list", "stock:shipment_list", "sales:request_list"]


def _active(path, prefixes):
    return any(path == "/" if p == "/$" else path.startswith(p) for p in prefixes)


def ui(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"app_version": VERSION}
    costs = can_see_costs(user)
    path = request.path
    groups, flat = [], {}
    for title, items in NAV:
        rows = []
        for label, name, icon, prefixes, boss_only in items:
            if boss_only and not costs:
                continue
            item = {"label": label, "url": reverse(name), "icon": icon, "active": _active(path, prefixes), "name": name}
            rows.append(item)
            flat[name] = item
        groups.append({"title": title, "items": rows})
    if user.is_superuser:
        role = "Администратор"
    else:
        role = ", ".join(user.groups.values_list("name", flat=True)) or "Пользователь"
    return {
        "app_version": VERSION,
        "nav_groups": groups,
        "nav_tabs": [flat[n] for n in TABS if n in flat],
        "user_role": role,
        "user_initials": (user.get_full_name() or user.get_username())[:2].upper(),
    }
