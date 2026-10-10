"""Read CSV/XLSX files into sheets and profile their columns.

Implements the ingest contract in data/generate_dataset.py: offset header rows, blank rows,
BOM / ';' CSVs, currency and accounting numbers, mixed date formats, Y/N booleans.
"""

import csv
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from app.config import get_settings


class TabularError(ValueError):
    """A file the user uploaded can't be used; the message is shown to them."""


@dataclass
class Column:
    name: str
    type: str = "string"  # string | integer | float | date | boolean
    fill_rate: float = 0.0
    distinct: int = 0
    distinct_ratio: float = 0.0
    id_like: bool = False
    samples: list[str] = field(default_factory=list)


@dataclass
class Sheet:
    name: str
    header_row: int  # 1-based row number in the file
    columns: list[str]
    rows: list[dict]  # {column: raw value, "_row": file row number}
    profile: dict[str, Column] = field(default_factory=dict)

    def values(self, column: str) -> list:
        return [r.get(column) for r in self.rows]


# ------------------------------------------------------------------ value parsing
def is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())


def norm_key(v) -> str:
    """Key comparison form: strip + upper. 5100.0 -> '5100'."""
    if is_blank(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, dt.datetime | dt.date):
        v = v.isoformat()[:10]
    return str(v).strip().upper()


_CURRENCY = re.compile(r"^(?:INR|RS\.?|USD|EUR|GBP|₹|\$|€|£)\s*|\s*(?:INR|USD|EUR|GBP)$", re.I)
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def parse_number(v):
    if isinstance(v, bool) or is_blank(v):
        return None
    if isinstance(v, int | float):
        return v
    s = str(v).strip()
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    s = _CURRENCY.sub("", s).strip()
    s = re.sub(r"\s*/-$", "", s)  # "1,250/-"
    # 1,250,000 (international) or 12,50,000 (Indian lakh grouping)
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", s) or re.fullmatch(r"-?\d{1,2}(,\d{2})*,\d{3}(\.\d+)?", s):
        s = s.replace(",", "")
    if not _NUMBER.match(s):
        return None
    n = float(s) if "." in s else int(s)
    return -n if negative else n


_DATE_FORMATS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%Y/%m/%d",
]


def parse_date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if is_blank(v) or not isinstance(v, str):
        return None
    s = v.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        s = s[:10]
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


_TRUE = {"y", "yes", "true", "t", "1"}
_FALSE = {"n", "no", "false", "f", "0"}


def parse_bool(v):
    if isinstance(v, bool):
        return v
    if is_blank(v):
        return None
    s = str(v).strip().lower()
    if isinstance(v, float) and v.is_integer():
        s = str(int(v))
    return True if s in _TRUE else False if s in _FALSE else None


def coerce(v, type_: str):
    """Value in its column type, or None when blank/unparseable."""
    if is_blank(v):
        return None
    if type_ == "integer":
        n = parse_number(v)
        return int(n) if n is not None and float(n).is_integer() else None
    if type_ == "float":
        n = parse_number(v)
        return float(n) if n is not None else None
    if type_ == "date":
        return parse_date(v)
    if type_ == "boolean":
        return parse_bool(v)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, dt.datetime | dt.date):
        return v.isoformat()[:10]
    return str(v).strip()


# ------------------------------------------------------------------ reading
def _rows_from_csv(data: bytes) -> list[list]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if "\x00" in text:
        raise TabularError("The file is not a readable CSV (binary content).")
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = max(",;\t|", key=sample.count)
    return [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]


def _find_header(rows: list[list]) -> int | None:
    header_scan = get_settings().tabular_header_scan_rows
    width = max((sum(not is_blank(c) for c in r) for r in rows[:header_scan]), default=0)
    for i, r in enumerate(rows[:header_scan]):
        cells = [c for c in r if not is_blank(c)]
        if (
            len(cells) >= max(2, round(0.6 * width))
            and all(isinstance(c, str) for c in cells)
            and not all(parse_number(c) is not None for c in cells)
        ):
            return i
    return None


def _build_sheet(name: str, raw: list[list]) -> Sheet | None:
    if not any(any(not is_blank(c) for c in r) for r in raw):
        return None
    h = _find_header(raw)
    if h is None:  # notes, cover pages, charts: not a table
        return None
    columns, seen = [], {}
    for i, c in enumerate(raw[h]):
        col = str(c).strip() if not is_blank(c) else f"column_{i + 1}"
        if col in seen:
            seen[col] += 1
            col = f"{col}_{seen[col]}"
        seen.setdefault(col, 1)
        columns.append(col)
    # drop trailing unnamed columns that are empty everywhere
    rows = []
    for offset, r in enumerate(raw[h + 1 :], start=h + 2):
        if all(is_blank(c) for c in r):
            continue
        row = {col: (r[i] if i < len(r) else None) for i, col in enumerate(columns)}
        row["_row"] = offset
        rows.append(row)
    columns = [c for c in columns if not (c.startswith("column_") and all(is_blank(r.get(c)) for r in rows))]
    sheet = Sheet(name=name, header_row=h + 1, columns=columns, rows=rows)
    sheet.profile = {c: profile_column(c, sheet.values(c)) for c in columns}
    return sheet


def read_table_file(path: str | Path, original_name: str | None = None) -> list[Sheet]:
    path = Path(path)
    name = (original_name or path.name).lower()
    data = path.read_bytes()
    if not data.strip():
        raise TabularError("The file is empty.")
    sheets: list[Sheet] = []
    if name.endswith((".xlsx", ".xlsm")):
        try:
            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as exc:
            raise TabularError(f"The file is not a valid Excel workbook ({type(exc).__name__}).") from exc
        for ws in wb.worksheets:
            raw = [list(r) for r in ws.iter_rows(values_only=True)]
            s = _build_sheet(ws.title, raw)
            if s:
                sheets.append(s)
        wb.close()
    elif name.endswith((".csv", ".tsv", ".txt")):
        s = _build_sheet(Path(original_name or path.name).stem, _rows_from_csv(data))
        if s:
            sheets.append(s)
    else:
        raise TabularError("Only CSV and XLSX files are supported for knowledge graphs.")
    tables_found = len(sheets)
    sheets = [s for s in sheets if s.rows]
    if not sheets:
        if tables_found:
            raise TabularError("The file has a header but no data rows.")
        raise TabularError(f"No table found: no sheet has a header row in its first {header_scan} rows.")
    return sheets


# ------------------------------------------------------------------ profiling
def infer_type(values: list) -> str:
    present = [v for v in values if not is_blank(v)]
    if not present:
        return "string"
    n = len(present)
    if all(parse_bool(v) is not None for v in present):
        tokens = {str(v).strip().lower() for v in present if not isinstance(v, bool | int | float)}
        if any(isinstance(v, bool) for v in present) or tokens - {"0", "1"}:
            return "boolean"
    # 90%: a few "N/A"-style cells shouldn't turn a numeric column into text; they become null
    if sum(parse_date(v) is not None for v in present) >= 0.9 * n:
        return "date"
    nums = [parse_number(v) for v in present]
    if sum(x is not None for x in nums) >= 0.9 * n:
        ok = [x for x in nums if x is not None]
        # leading zeros (e.g. account numbers) must stay strings
        if any(isinstance(v, str) and re.match(r"^0\d", v.strip()) for v in present):
            return "string"
        return "integer" if all(float(x).is_integer() for x in ok) else "float"
    return "string"


def profile_column(name: str, values: list) -> Column:
    present = [v for v in values if not is_blank(v)]
    keys = [norm_key(v) for v in present]
    distinct = len(set(keys))
    samples = []
    for v in present:
        s = str(coerce(v, "string"))[:40]
        if s not in samples:
            samples.append(s)
        if len(samples) == 5:
            break
    id_like = bool(present) and sum(
        bool(re.fullmatch(r"[A-Z0-9][A-Z0-9\-_/.]*", k)) and any(ch.isdigit() for ch in k) for k in keys
    ) >= 0.9 * len(keys)
    return Column(
        name=name,
        type=infer_type(values),
        fill_rate=round(len(present) / max(len(values), 1), 4),
        distinct=distinct,
        distinct_ratio=round(distinct / max(len(present), 1), 4),
        id_like=id_like,
        samples=samples,
    )


def match_columns(schema_columns: list[str], file_columns: list[str]) -> dict[str, str]:
    """Map schema column names to file column names, ignoring case, spaces and punctuation."""
    by_norm = {norm_header(c): c for c in file_columns}
    return {c: by_norm[norm_header(c)] for c in schema_columns if norm_header(c) in by_norm}
