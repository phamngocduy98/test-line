"""Input validation for parsed solver CSVs."""

from __future__ import annotations

from itertools import product

from .constants import BAND_COLUMNS, NUMERIC_COLUMNS, SPECIAL_VALUES
from .errors import InputError
from .models import ParsedCsv, SupportTable, Token


def is_special(value: str) -> bool:
    return value.casefold() in SPECIAL_VALUES


def is_any_token(token: Token) -> bool:
    return any(alternative.casefold() == "any" for alternative in token.alternatives)


def validate_testcases(parsed: ParsedCsv, support: SupportTable, *, final_solver: bool) -> None:
    if not parsed.rows:
        raise InputError(f"{parsed.path} has no testcase rows")

    seen_tc_ids: set[str] = set()
    for row in parsed.rows:
        tc_id = row.raw["tc_id"].strip()
        if not tc_id:
            raise InputError(f"{parsed.path}:{row.row_number} has empty tc_id")
        if tc_id in seen_tc_ids:
            raise InputError(f"{parsed.path}:{row.row_number} has duplicate tc_id {tc_id!r}")
        seen_tc_ids.add(tc_id)

        for column in NUMERIC_COLUMNS.intersection(row.tokens):
            _validate_numeric_cell(str(parsed.path), row.row_number, column, row.tokens[column])

        if final_solver:
            _validate_known_ru_and_bands(str(parsed.path), row.row_number, row.tokens, support)
            if not _has_compatible_realization(row.tokens, support):
                raise InputError(f"{parsed.path}:{row.row_number} has no compatible RU-band realization")


def _validate_numeric_cell(path: str, row_number: int, column: str, tokens: tuple[Token, ...]) -> None:
    if not tokens:
        return
    if len(tokens) != 1 or len(tokens[0].alternatives) != 1:
        raise InputError(f"{path}:{row_number} {column} must be one non-negative integer")
    value = tokens[0].alternatives[0]
    if not value.isdecimal():
        raise InputError(f"{path}:{row_number} {column} must be one non-negative integer")


def _validate_known_ru_and_bands(path: str, row_number: int, tokens_by_column: dict[str, tuple[Token, ...]], support: SupportTable) -> None:
    for token in tokens_by_column.get("ru", ()):
        for alternative in token.alternatives:
            folded = alternative.casefold()
            if folded == "any":
                continue
            if folded in {"intra", "inter"} or folded not in support.ru_display:
                raise InputError(f"{path}:{row_number} unknown concrete RU {alternative!r}")

    for column, display in (("lte band", support.lte_display), ("nr band", support.nr_display)):
        for token in tokens_by_column.get(column, ()):
            for alternative in token.alternatives:
                folded = alternative.casefold()
                if folded in SPECIAL_VALUES:
                    continue
                if folded not in display:
                    raise InputError(f"{path}:{row_number} unknown concrete {column} {alternative!r}")


def _has_compatible_realization(tokens_by_column: dict[str, tuple[Token, ...]], support: SupportTable) -> bool:
    ru_domains: list[tuple[str, ...]] = []
    for token in tokens_by_column.get("ru", ()):
        domain = tuple(
            support.ru_order if alternative.casefold() == "any" else (alternative.casefold(),)
            for alternative in token.alternatives
            if alternative.casefold() in support.ru_display or alternative.casefold() == "any"
        )
        flat = tuple(dict.fromkeys(value for group in domain for value in group))
        if not flat:
            return False
        ru_domains.append(flat)
    if not ru_domains:
        return False

    lte_options = _band_token_options(tokens_by_column.get("lte band", ()), support.lte_display)
    nr_options = _band_token_options(tokens_by_column.get("nr band", ()), support.nr_display)
    if lte_options is None or nr_options is None:
        return False

    max_products = 20000
    total = 1
    for domain in ru_domains:
        total *= max(1, len(domain))
        if total > max_products:
            selected_rus = set().union(*(set(domain) for domain in ru_domains))
            return _bands_supported(selected_rus, lte_options, support.lte_by_ru) and _bands_supported(selected_rus, nr_options, support.nr_by_ru)

    for selected in product(*ru_domains):
        selected_rus = set(selected)
        if _bands_supported(selected_rus, lte_options, support.lte_by_ru) and _bands_supported(selected_rus, nr_options, support.nr_by_ru):
            return True
    return False


def _band_token_options(tokens: tuple[Token, ...], display: dict[str, str]) -> list[set[str]] | None:
    options: list[set[str]] = []
    all_bands = set(display)
    for token in tokens:
        if any(alternative.casefold() in {"intra", "inter"} for alternative in token.alternatives):
            continue
        if any(alternative.casefold() == "any" for alternative in token.alternatives):
            if not all_bands:
                return None
            options.append(set(all_bands))
            continue
        values = {alternative.casefold() for alternative in token.alternatives if alternative.casefold() in display}
        if not values:
            return None
        options.append(values)
    return options


def _bands_supported(selected_rus: set[str], token_options: list[set[str]], support_by_ru: dict[str, tuple[str, ...]]) -> bool:
    supported = set().union(*(set(support_by_ru.get(ru, ())) for ru in selected_rus))
    return all(options & supported for options in token_options)

