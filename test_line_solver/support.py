"""RU-band support-table normalization and validation."""

from __future__ import annotations

from .constants import SPECIAL_VALUES
from .errors import InputError
from .models import ParsedCsv, SupportTable, Token


def _is_special(value: str) -> bool:
    return value.casefold() in SPECIAL_VALUES


def _validate_support_ru(path: str, row_number: int, tokens: tuple[Token, ...]) -> str:
    if len(tokens) != 1 or len(tokens[0].alternatives) != 1:
        raise InputError(f"{path}:{row_number} support-table ru must be one concrete value")
    ru = tokens[0].alternatives[0]
    if ru.strip() == "" or _is_special(ru):
        raise InputError(f"{path}:{row_number} support-table ru must be one concrete value")
    return ru


def _validate_support_bands(path: str, row_number: int, column: str, tokens: tuple[Token, ...]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        for alternative in token.alternatives:
            if _is_special(alternative):
                raise InputError(f"{path}:{row_number} {column} must not contain special value {alternative!r}")
            folded = alternative.casefold()
            if folded not in seen:
                values.append(alternative)
                seen.add(folded)
    return tuple(values)


def build_support_table(parsed: ParsedCsv) -> SupportTable:
    ru_order: list[str] = []
    ru_display: dict[str, str] = {}
    lte_display: dict[str, str] = {}
    nr_display: dict[str, str] = {}
    lte_by_ru_lists: dict[str, list[str]] = {}
    nr_by_ru_lists: dict[str, list[str]] = {}

    for row in parsed.rows:
        ru = _validate_support_ru(str(parsed.path), row.row_number, row.tokens["ru"])
        ru_key = ru.casefold()
        if ru_key not in ru_display:
            ru_order.append(ru_key)
            ru_display[ru_key] = ru
            lte_by_ru_lists[ru_key] = []
            nr_by_ru_lists[ru_key] = []

        lte_bands = _validate_support_bands(str(parsed.path), row.row_number, "lte_band", row.tokens["lte_band"])
        nr_bands = _validate_support_bands(str(parsed.path), row.row_number, "nr_band", row.tokens["nr_band"])
        for band in lte_bands:
            band_key = band.casefold()
            lte_display.setdefault(band_key, band)
            if band_key not in lte_by_ru_lists[ru_key]:
                lte_by_ru_lists[ru_key].append(band_key)
        for band in nr_bands:
            band_key = band.casefold()
            nr_display.setdefault(band_key, band)
            if band_key not in nr_by_ru_lists[ru_key]:
                nr_by_ru_lists[ru_key].append(band_key)

    return SupportTable(
        ru_order=tuple(ru_order),
        lte_by_ru={ru: tuple(values) for ru, values in lte_by_ru_lists.items()},
        nr_by_ru={ru: tuple(values) for ru, values in nr_by_ru_lists.items()},
        ru_display=ru_display,
        lte_display=lte_display,
        nr_display=nr_display,
    )

