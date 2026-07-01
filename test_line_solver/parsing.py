"""CSV and requirement-cell parsing."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterable, Sequence

from .constants import NUMERIC_COLUMNS, SUPPORT_BAND_COLUMNS
from .errors import InputError
from .models import ParsedCsv, ParsedRow, Token

PLUS_SEPARATOR = re.compile(r"\s*\+\s*")
SLASH_SEPARATOR = re.compile(r"\s*/\s*")


def parse_cell(value: str | None, *, blank_tokens: tuple[Token, ...] = ()) -> tuple[Token, ...]:
    """Parse a cell into ``+``-separated tokens with ``/`` alternatives."""
    if value is None or value == "":
        return blank_tokens

    tokens: list[Token] = []
    for raw_token in PLUS_SEPARATOR.split(value):
        alternatives: list[str] = []
        seen: set[str] = set()
        for raw_alternative in SLASH_SEPARATOR.split(raw_token):
            alternative = raw_alternative.strip()
            folded = alternative.casefold()
            if not alternative or folded in seen:
                continue
            alternatives.append(alternative)
            seen.add(folded)
        if alternatives:
            tokens.append(Token(tuple(alternatives)))
    return tuple(tokens)


def _read_csv_rows(path: Path, required_columns: Sequence[str]) -> tuple[tuple[str, ...], list[tuple[int, dict[str, str]]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise InputError(f"{path} has no header row")

        columns = tuple(reader.fieldnames)
        missing_columns = [column for column in required_columns if column not in columns]
        if missing_columns:
            joined = ", ".join(missing_columns)
            raise InputError(f"{path} is missing required column(s): {joined}")

        rows: list[tuple[int, dict[str, str]]] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise InputError(f"{path}:{row_number} has more values than headers")
            rows.append((row_number, {column: row.get(column) or "" for column in columns}))
        return columns, rows


def read_testcase_csv(path: Path, *, require_ru: bool) -> ParsedCsv:
    required = ("tc_id", "ru") if require_ru else ("tc_id",)
    columns, raw_rows = _read_csv_rows(path, required)
    parsed_rows: list[ParsedRow] = []
    for row_number, raw in raw_rows:
        tokens: dict[str, tuple[Token, ...]] = {}
        for column in columns:
            if column == "tc_id":
                continue
            blank = () if column in NUMERIC_COLUMNS else (Token(("any",)),)
            tokens[column] = parse_cell(raw[column], blank_tokens=blank)
        parsed_rows.append(ParsedRow(row_number=row_number, raw=raw, tokens=tokens))
    return ParsedCsv(path=path, columns=columns, rows=tuple(parsed_rows))


def read_ru_band_csv(path: Path) -> ParsedCsv:
    columns, raw_rows = _read_csv_rows(path, ("ru", "lte_band", "nr_band"))
    parsed_rows: list[ParsedRow] = []
    for row_number, raw in raw_rows:
        tokens: dict[str, tuple[Token, ...]] = {}
        for column in columns:
            blank = () if column in SUPPORT_BAND_COLUMNS else ()
            tokens[column] = parse_cell(raw[column], blank_tokens=blank)
        parsed_rows.append(ParsedRow(row_number=row_number, raw=raw, tokens=tokens))
    return ParsedCsv(path=path, columns=columns, rows=tuple(parsed_rows))


def parsed_csv_to_json(parsed: ParsedCsv) -> dict[str, object]:
    return {
        "path": str(parsed.path),
        "columns": list(parsed.columns),
        "rows": [
            {
                "row_number": row.row_number,
                "raw": row.raw,
                "tokens": {
                    column: [list(token.alternatives) for token in column_tokens]
                    for column, column_tokens in row.tokens.items()
                },
            }
            for row in parsed.rows
        ],
    }


def parsed_payload_to_json(input_csv: ParsedCsv, ru_band_csv: ParsedCsv) -> str:
    payload = {
        "input": parsed_csv_to_json(input_csv),
        "ru_band": parsed_csv_to_json(ru_band_csv),
    }
    return json.dumps(payload, indent=2)


def render_tokens(tokens: Iterable[Token]) -> str:
    return " + ".join(token.as_text() for token in tokens)

