#!/usr/bin/env python3
"""Generate random testcase CSVs and run solve_test_lines.py for benchmarking."""

from __future__ import annotations

import argparse
import csv
import random
import subprocess
import sys
import time
from pathlib import Path


COLUMNS = [
    "tc_id",
    "tech lte",
    "tech nsa",
    "tech nr sa",
    "enb",
    "vdu",
    "au",
    "cu",
    "lte band",
    "nr band",
    "ru",
    "cc location",
    "ca type",
    "rf condition",
    "ue",
    "ue capa lte",
    "ue capa nr",
    "ue capa special",
]

LTE_BANDS = [f"b{i}" for i in (1, 2, 3, 5, 7, 8, 20, 28)]
NR_BANDS = [f"n{i}" for i in (1, 3, 5, 7, 28, 41, 77, 78)]
RU_VALUES = [f"rf-{i}" for i in range(1000, 1008)]
CC_LOCATIONS = ["", "any", "intra cc", "inter cc"]
CA_TYPES = ["non ca", "intra DU", "inter DU", "inter CU", "any"]
RF_CONDITIONS = ["", "mobility", "peak 2 path", "peak 4 path"]
UE_LTE = ["emtc", "volte", "spid"]
UE_NR = ["nr"]
UE_SPECIAL = ["", "", "", "", "", "6cc"] # small probably
ANY_RATE = 0.50
RU_OR_RATE = 0.30


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a random input CSV and run solve_test_lines.py on it."
    )
    parser.add_argument("--rows", type=int, default=1000, help="Number of random testcases.")
    parser.add_argument("--seed", type=int, default=1, help="Random seed.")
    parser.add_argument(
        "--input",
        default="random_input.csv",
        help="Generated input CSV path. Default: random_input.csv",
    )
    parser.add_argument(
        "--output",
        default="random_output_specs.csv",
        help="Solver output CSV path. Default: random_output_specs.csv",
    )
    parser.add_argument(
        "--ru-band-support",
        default="random_ru_band_support.csv",
        help="Generated RU support CSV path. Default: random_ru_band_support.csv",
    )
    parser.add_argument(
        "--solver",
        default="solve_test_lines.py",
        help="Solver script path. Default: solve_test_lines.py",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Solver timeout in seconds. Default: 600.",
    )
    parser.add_argument(
        "--max-candidates-per-bucket",
        type=int,
        default=250,
        help="Passed through to solve_test_lines.py.",
    )
    parser.add_argument(
        "--max-cover-checks-per-candidate",
        type=int,
        default=0,
        help="Passed through to solve_test_lines.py. 0 checks all rows.",
    )
    parser.add_argument(
        "--ignore-tech-and-ue-capa",
        action="store_true",
        help="Pass --ignore-tech-and-ue-capa to the solver.",
    )
    parser.add_argument(
        "--auto-assign",
        action="store_true",
        help="Pass --auto-assign to the solver.",
    )
    parser.add_argument(
        "--variation",
        choices=("low", "medium", "high"),
        default="medium",
        help="How diverse the generated requirements should be.",
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Only generate the random input CSV; do not run the solver.",
    )
    return parser.parse_args()


def join_tokens(tokens: list[str]) -> str:
    return " + ".join(tokens)


def weighted_choice(rng: random.Random, choices: list[tuple[str, int]]) -> str:
    total = sum(weight for _, weight in choices)
    pick = rng.randint(1, total)
    upto = 0
    for value, weight in choices:
        upto += weight
        if pick <= upto:
            return value
    return choices[-1][0]


def sample_band(rng: random.Random, bands: list[str], variation: str, allow_blank: bool) -> str:
    if rng.random() < ANY_RATE:
        mode = weighted_choice(
            rng,
            [
                ("any", 60),
                ("same", 20),
                ("different", 20),
            ],
        )
        if mode == "any":
            return "any"
        return join_tokens(["any", "intra" if mode == "same" else "inter"])

    blank_weight = {"low": 20, "medium": 10, "high": 5}[variation]
    relation_weight = {"low": 15, "medium": 20, "high": 25}[variation]

    mode = weighted_choice(
        rng,
        [
            ("blank", blank_weight if allow_blank else 0),
            ("one", 30),
            ("same", relation_weight),
            ("different", relation_weight),
        ],
    )
    if mode == "blank":
        return ""
    if mode == "any":
        return "any"
    if mode == "one":
        return rng.choice(bands)
    if mode == "same":
        band = rng.choice(bands)
        return join_tokens([band, band])
    if mode == "different":
        return join_tokens(rng.sample(bands, 2))
    return rng.choice(bands)


def sample_ru(rng: random.Random, variation: str) -> str:
    width_weights = {
        "low": [(1, 75), (2, 20), (3, 5)],
        "medium": [(1, 55), (2, 30), (3, 12), (4, 3)],
        "high": [(1, 35), (2, 35), (3, 20), (4, 10)],
    }[variation]
    width = int(weighted_choice(rng, [(str(value), weight) for value, weight in width_weights]))
    if rng.random() < ANY_RATE:
        return join_tokens(["any"] * width)

    primary_values = rng.sample(RU_VALUES, width)
    slots = []
    for primary in primary_values:
        if rng.random() < RU_OR_RATE:
            alternative = rng.choice([value for value in RU_VALUES if value != primary])
            slots.append(f"{primary}/{alternative}")
        else:
            slots.append(primary)
    return join_tokens(slots)


def maybe_any(rng: random.Random, concrete: str) -> str:
    return "any" if rng.random() < ANY_RATE else concrete


def sample_ue_capabilities(rng: random.Random, tech_lte: str, tech_nsa: str, tech_sa: str) -> tuple[str, str, str]:
    special = "" if rng.random() < 0.45 else maybe_any(rng, rng.choice(UE_SPECIAL))
    has_nr_text = tech_nsa == "1" or tech_sa == "1"

    if tech_nsa == "1" and has_nr_text and rng.random() < 0.65:
        return "any" if rng.random() < ANY_RATE else "nsa", "any" if rng.random() < ANY_RATE else "nsa", special

    lte = ""
    nr = ""
    if tech_lte == "1":
        lte = maybe_any(rng, rng.choice(UE_LTE))
    if has_nr_text:
        nr = maybe_any(rng, rng.choice(UE_NR))
    if not lte and not nr and rng.random() < 0.50:
        lte = "any"
    return lte, nr, special


def sample_numeric_equipment(rng: random.Random, primary: bool) -> str:
    if primary:
        return weighted_choice(rng, [("1", 75), ("2", 20), ("3", 5)])
    return weighted_choice(rng, [("0", 85), ("1", 13), ("2", 2)])


def make_row(tc_id: int, rng: random.Random, variation: str) -> dict[str, str]:
    tech_mode = weighted_choice(
        rng,
        [
            ("lte", 45),
            ("lte_nsa", 30),
            ("lte_nsa_sa", 15),
            ("sa", 10),
        ],
    )
    tech_lte = "1" if tech_mode in {"lte", "lte_nsa", "lte_nsa_sa"} else "0"
    tech_nsa = "1" if tech_mode in {"lte_nsa", "lte_nsa_sa"} else "0"
    tech_sa = "1" if tech_mode in {"lte_nsa_sa", "sa"} else "0"

    ue_lte, ue_nr, ue_special = sample_ue_capabilities(rng, tech_lte, tech_nsa, tech_sa)
    du_counts = {
        "enb": int(sample_numeric_equipment(rng, primary=tech_lte == "1")),
        "vdu": int(
            sample_numeric_equipment(
                rng, primary=tech_nsa == "1" or tech_sa == "1"
            )
        ),
        "au": int(sample_numeric_equipment(rng, primary=False)),
        "cu": int(sample_numeric_equipment(rng, primary=tech_sa == "1")),
    }
    while sum(du_counts.values()) > 4:
        largest = max(du_counts, key=du_counts.get)
        du_counts[largest] -= 1

    return {
        "tc_id": str(tc_id),
        "tech lte": tech_lte,
        "tech nsa": tech_nsa,
        "tech nr sa": tech_sa,
        "enb": str(du_counts["enb"]),
        "vdu": str(du_counts["vdu"]),
        "au": str(du_counts["au"]),
        "cu": str(du_counts["cu"]),
        "lte band": sample_band(rng, LTE_BANDS, variation, allow_blank=tech_lte == "0"),
        "nr band": sample_band(rng, NR_BANDS, variation, allow_blank=False) if tech_nsa == "1" or tech_sa == "1" else "",
        "ru": sample_ru(rng, variation),
        "cc location": "any" if rng.random() < ANY_RATE else rng.choice([v for v in CC_LOCATIONS if v != "any"]),
        "ca type": "any" if rng.random() < ANY_RATE else rng.choice([v for v in CA_TYPES if v != "any"]),
        "rf condition": "any" if rng.random() < ANY_RATE else rng.choice(RF_CONDITIONS),
        "ue": weighted_choice(rng, [("1", 55), ("2", 35), ("3", 10)]),
        "ue capa lte": ue_lte,
        "ue capa nr": ue_nr,
        "ue capa special": ue_special,
    }


def write_random_input(path: Path, rows: int, seed: int, variation: str) -> None:
    rng = random.Random(seed)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for tc_id in range(1, rows + 1):
            writer.writerow(make_row(tc_id, rng, variation))


def write_ru_band_support(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["ru", "lte_band", "nr_band"]
        )
        writer.writeheader()
        for ru in RU_VALUES:
            writer.writerow(
                {
                    "ru": ru,
                    "lte_band": join_tokens(LTE_BANDS),
                    "nr_band": join_tokens(NR_BANDS),
                }
            )


def run_solver(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(Path(args.solver)),
        "--input",
        str(Path(args.input)),
        "--output",
        str(Path(args.output)),
        "--ru-band-support",
        str(Path(args.ru_band_support)),
        "--timeout",
        str(args.timeout),
        "--max-candidates-per-bucket",
        str(args.max_candidates_per_bucket),
        "--max-cover-checks-per-candidate",
        str(args.max_cover_checks_per_candidate),
    ]
    if args.ignore_tech_and_ue_capa:
        command.append("--ignore-tech-and-ue-capa")
    if args.auto_assign:
        command.append("--auto-assign")
    started_at = time.monotonic()
    completed = subprocess.run(command, text=True, capture_output=True)
    elapsed = time.monotonic() - started_at

    print("solver_command=" + " ".join(command))
    print(f"benchmark_runtime_seconds={elapsed:.2f}")
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    return completed.returncode


def main() -> int:
    args = parse_args()
    if args.rows <= 0:
        raise SystemExit("--rows must be positive")

    input_path = Path(args.input)
    support_path = Path(args.ru_band_support)
    write_random_input(input_path, args.rows, args.seed, args.variation)
    write_ru_band_support(support_path)
    print(
        f"generated={input_path} support={support_path} "
        f"rows={args.rows} seed={args.seed} variation={args.variation}"
    )

    if args.no_run:
        return 0
    return run_solver(args)


if __name__ == "__main__":
    raise SystemExit(main())
