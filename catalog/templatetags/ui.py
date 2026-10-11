"""Элементы интерфейса: {% icon "truck" %} — иконка из templates/_icons.html (Lucide)."""

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def icon(name, cls=""):
    return format_html('<svg class="i {}" aria-hidden="true"><use href="#i-{}"></use></svg>', cls, name)
