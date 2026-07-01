"""Candidate spec construction and deterministic rendering."""

from __future__ import annotations

from .constants import NUMERIC_COLUMNS
from .coverage import numeric_value
from .models import Token
from .parsing import render_tokens


def exact_spec(row_tokens: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> dict[str, tuple[Token, ...]]:
    return {column: row_tokens.get(column, ()) for column in columns}


def merge_specs(
    left: dict[str, tuple[Token, ...]],
    right: dict[str, tuple[Token, ...]],
    columns: tuple[str, ...],
) -> dict[str, tuple[Token, ...]]:
    merged: dict[str, tuple[Token, ...]] = {}
    for column in columns:
        if column in NUMERIC_COLUMNS:
            value = max(numeric_value(left.get(column, ())), numeric_value(right.get(column, ())))
            merged[column] = () if value == 0 else (Token((str(value),)),)
        else:
            merged[column] = _merge_token_lists(left.get(column, ()), right.get(column, ()))
    return merged


def _merge_token_lists(left: tuple[Token, ...], right: tuple[Token, ...]) -> tuple[Token, ...]:
    result = list(left)
    for token in right:
        for index, existing in enumerate(result):
            merged = _merge_tokens(existing, token)
            if merged is not None:
                result[index] = merged
                break
        else:
            result.append(token)
    return tuple(result)


def _merge_tokens(left: Token, right: Token) -> Token | None:
    left_values = {value.casefold() for value in left.alternatives}
    right_values = {value.casefold() for value in right.alternatives}
    if "any" in left_values or "any" in right_values:
        return Token(("any",))
    if not (left_values & right_values):
        return None
    alternatives = list(left.alternatives)
    seen = set(left_values)
    for alternative in right.alternatives:
        folded = alternative.casefold()
        if folded not in seen:
            alternatives.append(alternative)
            seen.add(folded)
    return Token(tuple(alternatives))


def spec_signature(spec: dict[str, tuple[Token, ...]], columns: tuple[str, ...]) -> str:
    return "|".join(f"{column}={render_tokens(spec.get(column, ())) }" for column in columns)

