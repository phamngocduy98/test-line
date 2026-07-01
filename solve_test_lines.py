#!/usr/bin/env python3
"""Read testcase and RU-band CSV files and parse their cells into tokens."""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


PLUS_SEPARATOR = re.compile(r"\s*\+\s*")
SLASH_SEPARATOR = re.compile(r"\s*/\s*")


@dataclass(frozen=True)
class Token:
    """One required slot in a CSV cell.

    A token can contain one or more alternatives. For example, the cell
    ``rf-1/rf-2 + any`` is parsed into two tokens:
    ``("rf-1", "rf-2")`` and ``("any",)``.
    """

    alternatives: tuple[str, ...]

    def as_text(self) -> str:
        return "/".join(self.alternatives)


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    raw: dict[str, str]
    tokens: dict[str, tuple[Token, ...]]


@dataclass(frozen=True)
class ParsedCsv:
    path: Path
    columns: tuple[str, ...]
    rows: tuple[ParsedRow, ...]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read input.csv and ru-band.csv, then print parsed tokens."
    )
    parser.add_argument(
        "--input",
        default="input.csv",
        help="Path to the testcase input CSV. Default: input.csv",
    )
    parser.add_argument(
        "--ru-band",
        "--ru-band-support",
        dest="ru_band",
        default="ru-band.csv",
        help="Path to the RU-band support CSV. Default: ru-band.csv",
    )
    return parser.parse_args(argv)


def parse_cell(value: str | None) -> tuple[Token, ...]:
    """Parse a CSV cell into ``+``-separated tokens with ``/`` alternatives."""
    if value is None or value == "":
        return ()

    tokens: list[Token] = []
    for raw_token in PLUS_SEPARATOR.split(value):
        alternatives: list[str] = []
        seen: set[str] = set()
        for raw_alternative in SLASH_SEPARATOR.split(raw_token):
            alternative = raw_alternative.strip()
            if not alternative or alternative in seen:
                continue
            alternatives.append(alternative)
            seen.add(alternative)
        if alternatives:
            tokens.append(Token(tuple(alternatives)))
    return tuple(tokens)


def read_csv(path: Path, required_columns: Sequence[str] = ()) -> ParsedCsv:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")

        columns = tuple(reader.fieldnames)
        missing_columns = [column for column in required_columns if column not in columns]
        if missing_columns:
            joined = ", ".join(missing_columns)
            raise SystemExit(f"{path} is missing required column(s): {joined}")

        rows: list[ParsedRow] = []
        for row_number, row in enumerate(reader, start=2):
            if None in row:
                raise SystemExit(f"{path}:{row_number} has more values than headers")

            raw = {column: row.get(column) or "" for column in columns}
            tokens = {column: parse_cell(raw[column]) for column in columns}
            rows.append(ParsedRow(row_number=row_number, raw=raw, tokens=tokens))

    return ParsedCsv(path=path, columns=columns, rows=tuple(rows))


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


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    input_csv = read_csv(Path(args.input), required_columns=("tc_id",))
    ru_band_csv = read_csv(
        Path(args.ru_band),
        required_columns=("ru", "lte_band", "nr_band"),
    )

    payload = {
        "input": parsed_csv_to_json(input_csv),
        "ru_band": parsed_csv_to_json(ru_band_csv),
    }
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
