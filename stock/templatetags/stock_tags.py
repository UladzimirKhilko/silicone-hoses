from django import template

from stock.paperwork import format_kg

register = template.Library()


@register.filter
def kg(value):
    """Масса как на бирке: 16,0 · 5,65 · 21,4."""
    return "—" if value is None else format_kg(value)


@register.filter
def money(value, places=2):
    """Деньги по-русски: 158 705,24."""
    if value in (None, ""):
        return "—"
    text = f"{value:,.{int(places)}f}"
    return text.replace(",", " ").replace(".", ",")


@register.filter
def price(value):
    """Цена завода: до 4 знаков, без лишних нулей (12,6761 · 5,09)."""
    if value in (None, ""):
        return "—"
    text = f"{value:.4f}".rstrip("0")
    if text.endswith("."):
        text += "00"
    elif len(text.split(".")[1]) == 1:
        text += "0"
    return text.replace(".", ",")


@register.filter
def pct(value):
    return "—" if value is None else f"{value:.1f}".replace(".", ",") + " %"


@register.filter
def short_name(value):
    """«Патрубок силиконовый BSI 90-60» → «BSI 90-60» (для узких таблиц; полное название — в подсказке)."""
    import re

    return re.sub(r"^\s*патрубок\s+силиконовый\s+", "", str(value or ""), flags=re.IGNORECASE)
