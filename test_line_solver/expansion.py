"""Final RU/band domain expansion for emitted specs."""

from __future__ import annotations

from .models import SupportTable, Token


def expanded_spec(spec: dict[str, tuple[Token, ...]], support: SupportTable) -> dict[str, tuple[Token, ...]]:
    expanded = dict(spec)
    expanded_ru_tokens = _expand_ru_tokens(spec.get("ru", ()), spec, support)
    selected_rus = _selected_ru_domain(expanded_ru_tokens, support)
    expanded["ru"] = expanded_ru_tokens
    expanded["lte band"] = tuple(_expand_band_token(token, selected_rus, support.lte_by_ru, support.lte_display) for token in spec.get("lte band", ()))
    expanded["nr band"] = tuple(_expand_band_token(token, selected_rus, support.nr_by_ru, support.nr_display) for token in spec.get("nr band", ()))
    return expanded


def _selected_ru_domain(tokens: tuple[Token, ...], support: SupportTable) -> set[str]:
    selected: set[str] = set()
    for token in tokens:
        if token.has_any():
            selected.update(support.ru_order)
        else:
            selected.update(alternative.casefold() for alternative in token.alternatives if alternative.casefold() in support.ru_display)
    return selected


def _expand_ru_tokens(tokens: tuple[Token, ...], spec: dict[str, tuple[Token, ...]], support: SupportTable) -> tuple[Token, ...]:
    domains = [_ru_token_domain(token, support) for token in tokens]
    expanded: list[Token] = []
    for index, domain in enumerate(domains):
        compatible = [
            ru
            for ru in support.ru_order
            if ru in domain and _ru_choice_can_satisfy(index, ru, domains, spec, support)
        ]
        expanded.append(Token(tuple(support.ru_display[ru] for ru in compatible)))
    return tuple(expanded)


def _ru_token_domain(token: Token, support: SupportTable) -> set[str]:
    if token.has_any():
        return set(support.ru_order)
    return {alternative.casefold() for alternative in token.alternatives if alternative.casefold() in support.ru_display}


def _ru_choice_can_satisfy(
    slot_index: int,
    ru: str,
    domains: list[set[str]],
    spec: dict[str, tuple[Token, ...]],
    support: SupportTable,
) -> bool:
    possible = {ru}
    for index, domain in enumerate(domains):
        if index != slot_index:
            possible.update(domain)
    return _band_slots_satisfied(possible, spec.get("lte band", ()), support.lte_by_ru, support.lte_display) and _band_slots_satisfied(
        possible, spec.get("nr band", ()), support.nr_by_ru, support.nr_display
    )


def _band_slots_satisfied(
    selected_rus: set[str],
    tokens: tuple[Token, ...],
    support_by_ru: dict[str, tuple[str, ...]],
    display: dict[str, str],
) -> bool:
    supported = set().union(*(set(support_by_ru.get(ru, ())) for ru in selected_rus))
    for token in tokens:
        if any(alternative.casefold() in {"intra", "inter"} for alternative in token.alternatives):
            continue
        if token.has_any():
            if not supported:
                return False
            continue
        values = {alternative.casefold() for alternative in token.alternatives if alternative.casefold() in display}
        if values and not values & supported:
            return False
    return True


def _expand_band_token(token: Token, selected_rus: set[str], support_by_ru: dict[str, tuple[str, ...]], display: dict[str, str]) -> Token:
    if any(alternative.casefold() in {"intra", "inter"} for alternative in token.alternatives):
        return token
    if not token.has_any():
        return Token(tuple(display.get(alternative.casefold(), alternative) for alternative in token.alternatives))
    seen: set[str] = set()
    for ru in selected_rus:
        for band in support_by_ru.get(ru, ()):
            seen.add(band)
    return Token(tuple(display[band] for band in display if band in seen))
