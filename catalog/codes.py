"""Работа с обозначениями изделий.

Правила — docs/09-product-identification.md. Главное: похожие названия часто означают РАЗНЫЕ
патрубки (`BSI 90-60` ≠ `BSI 90/60`, `BSI 90-38` ≠ `BSI 90-38-01`), поэтому нормализация
снимает только «безопасные» отличия и никогда не трогает `/`, `-`, `D` и суффиксы.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

_PREFIX_RE = re.compile(r"^\s*патрубок\s+силиконовый\s+", re.IGNORECASE)
_ORIGIN_RE = re.compile(r"\s*\(\s*китай\s*\)\s*$", re.IGNORECASE)
_BSI_RE = re.compile(r"^BSI\s*", re.IGNORECASE)


def normalize_name(text):
    """Ключ для точного сравнения названия из документа с кодом или синонимом.

    Безопасные отличия: регистр, пробелы, слова «Патрубок силиконовый», приписка «(Китай)»
    из ТТН, приставка `BSI`, десятичная запятая (`63,5` = `63.5`).
    """
    s = str(text or "").strip()
    s = _PREFIX_RE.sub("", s)
    s = _ORIGIN_RE.sub("", s)
    s = _BSI_RE.sub("", s.strip())
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    s = re.sub(r"\s+", "", s)
    return s.upper()


COLOR_BY_SUFFIX = {"1": "black", "2": "blue", "3": "red"}

# Параметрическая система (МТЗ, Гомсельмаш): BSI [угол-]D<d1>[/<d2>]L<l1>[/<l2>][-COR]-<цвет>
_PARAM_RE = re.compile(
    r"^(?:(?P<angle>\d{2,3})-)?D(?P<d1>\d+(?:\.\d+)?)(?:/(?P<d2>\d+(?:\.\d+)?))?"
    r"L(?P<l1>\d+(?:\.\d+)?)(?:/(?P<l2>\d+(?:\.\d+)?))?(?P<cor>-COR)?-(?P<color>[123])$"
)


@dataclass
class ParsedCode:
    kind: str
    angle: int | None
    d1: Decimal
    d2: Decimal | None
    l1: Decimal
    l2: Decimal | None
    color: str


def parse_parametric(code):
    """Разбирает код параметрической системы. Для кодов ММЗ (`BSI 90-38-01`) возвращает None —
    их параметры есть только на чертеже."""
    m = _PARAM_RE.match(normalize_name(code))
    if not m:
        return None
    g = m.groupdict()
    angle = int(g["angle"]) if g["angle"] else None
    d2 = Decimal(g["d2"]) if g["d2"] else None
    if g["cor"]:
        kind = "corrugated"
    elif angle and d2:
        kind = "angle_reducer"
    elif angle:
        kind = "angle"
    elif d2:
        kind = "reducer"
    else:
        kind = "straight"
    return ParsedCode(
        kind=kind,
        angle=angle,
        d1=Decimal(g["d1"]),
        d2=d2,
        l1=Decimal(g["l1"]),
        l2=Decimal(g["l2"]) if g["l2"] else None,
        color=COLOR_BY_SUFFIX[g["color"]],
    )
