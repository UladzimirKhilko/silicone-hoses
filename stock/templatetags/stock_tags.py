from django import template

from stock.paperwork import format_kg

register = template.Library()


@register.filter
def kg(value):
    """Масса как на бирке: 16,0 · 5,65 · 21,4."""
    return "—" if value is None else format_kg(value)
