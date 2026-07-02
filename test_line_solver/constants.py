"""Shared solver constants."""

NUMERIC_COLUMNS = frozenset({"enb", "vdu", "au", "cu", "ue"})
OPTIONAL_COLUMNS = frozenset(
    {
        "tech lte",
        "tech nsa",
        "tech nr sa",
        "ue capa lte",
        "ue capa nr",
        "ue capa special",
    }
)
DU_COLUMNS = ("enb", "vdu", "au", "cu")
BAND_COLUMNS = ("lte band", "nr band")
SUPPORT_BAND_COLUMNS = ("lte_band", "nr_band")
SPECIAL_VALUES = frozenset({"any", "intra", "inter"})

DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_CANDIDATES = 20000
DEFAULT_MAX_CANDIDATES_PER_BUCKET = 250
DEFAULT_MAX_MERGE_WIDTH = 55
DEFAULT_MAX_EXTRA_SLOTS = 1
DEFAULT_MAX_EXTRA_ALTERNATIVES = 1
DEFAULT_MAX_NUMERIC_OVERAGE_RATIO = 2.0
DEFAULT_MAX_NUMERIC_OVERAGE_UNITS = 1
DEFAULT_MIN_ASSIGNED_CASES_PER_SPEC = 10
