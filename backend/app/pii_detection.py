"""Evidence-checked PII detection for tabular data."""

import datetime as dt
import logging
import re
from collections.abc import Callable

from app import nist_pii, rules
from app.config import get_settings
from app.pii_review import PII_CATEGORIES
from app.tabular import Sheet, coerce, is_blank

log = logging.getLogger(__name__)

PII_SYSTEM = (
    "You are a data-privacy auditor applying NIST SP 800-122 (PII confidentiality) and the NIST Privacy Framework. "
    "You classify spreadsheet columns. Reply with one JSON object only."
)

PII_PROMPT = """Classify every column of sheet "{sheet}" for personal data (PII) about individual people.

Columns (type; example values):
{columns}

{data_instruction}

PII (NIST SP 800-122) is information about an individual PERSON that distinguishes or traces their identity
(direct identifiers) or is linked or linkable to them. For EACH column choose one category:
Direct identifiers:
  person_name (a person's full or partial name), email (personal e-mail), phone (personal phone number),
  address (street/postal address of a person), government_id (SSN, PAN, Aadhaar, passport, driver's licence,
  tax id), bank_account (bank/IBAN/card number), personal_id (number assigned to a person: patient MRN/UHID,
  employee or member number), biometric (fingerprint, face image, voice print), online_identifier (IP/MAC
  address or device id of a person)
Linked or linkable information about a person:
  date_of_birth, demographic (gender, race, religion, caste, nationality, marital status), health (diagnosis,
  condition, allergy, medical history), financial (salary, income, credit score), location (precise GPS of a
  person), free_text (notes/comments that can mention people)
or none.
Not PII (choose none): company/organisation names, product names, cities/regions/countries alone, record IDs of
orders/invoices/products/tickets, quantities, prices, dates of business events and status values.

JSON, with one entry per column:
{{"columns": [{{"column": "<name>", "category": "<category>", "confidence": 0.0-1.0}}]}}"""

_NAME_RULES = [
    (
        re.compile(r"\b(bank|iban|ifsc|account\s*(no|number))\b", re.I),
        "bank_account",
        "high",
        "column holds bank account details",
    ),
    (
        re.compile(r"\b(dob|date\s*of\s*birth|birth\s*date)\b", re.I),
        "date_of_birth",
        "high",
        "column holds dates of birth",
    ),
    (
        re.compile(r"\b(pan|aadhaar|aadhar|ssn|passport|national\s*id)\b", re.I),
        "government_id",
        "high",
        "column holds a national ID",
    ),
    (re.compile(r"\b(address|street)\b", re.I), "address", "medium", "column holds postal addresses"),
    (
        re.compile(r"\b(gender|sex|race|ethnicity|religion|caste|nationality|marital[\s_]*status)\b", re.I),
        "demographic",
        "medium",
        "column holds personal characteristics (NIST SP 800-122)",
    ),
    (
        re.compile(r"\b(diagnos\w*|medical[\s_]*(condition|history)|allerg\w*|disease|blood[\s_]*group)\b", re.I),
        "health",
        "high",
        "column holds medical information",
    ),
    (
        re.compile(r"\b(biometric|fingerprint|face[\s_]*(image|id)|voice[\s_]*print)\b", re.I),
        "biometric",
        "high",
        "column holds biometric records",
    ),
    (
        re.compile(r"\b(mrn|uhid|patient[\s_]*(id|no|number)|employee[\s_]*(id|no|number)|emp[\s_]*id)\b", re.I),
        "personal_id",
        "medium",
        "column holds identification numbers assigned to people",
    ),
]
_LOCATION_NAME = re.compile(r"latitude|longitude|\blat\b|\blon\b|\bgps\b|geo[\s_]*location|coordinates", re.I)
_IP_OR_MAC = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}", re.I)
_MACHINE_ID = re.compile(r"[0-9a-f]{16,}|[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def _machine_ids(texts: list[str]) -> bool:
    """GUIDs and hex record IDs are not phone numbers or other personal identifiers."""
    return bool(texts) and sum(bool(_MACHINE_ID.fullmatch(text)) for text in texts) >= 0.9 * len(texts)


def rule_pii(sheet: Sheet, column: str) -> dict | None:
    """Use column names and value patterns as deterministic PII evidence."""
    values = [str(value).strip() for value in sheet.values(column) if not is_blank(value)]
    if not values:
        return None
    if sheet.profile[column].type in ("string", "date") or (
        sheet.profile[column].type == "integer" and re.search(r"mrn|uhid|patient|employee|emp", column, re.I)
    ):
        for pattern, category, sensitivity, reason in _NAME_RULES:
            if pattern.search(column):
                return {"category": category, "sensitivity": sensitivity, "confidence": 0.85, "reason": reason}
    if _machine_ids(values):
        return None
    count = len(values)
    ratio = lambda pattern, full=True: sum(  # noqa: E731
        bool(pattern.fullmatch(value) if full else pattern.search(value)) for value in values
    ) / count
    if ratio(rules.EMAIL) >= 0.5:
        return {
            "category": "email",
            "sensitivity": "medium",
            "confidence": 0.95,
            "reason": "values are e-mail addresses",
        }
    if ratio(_IP_OR_MAC) >= 0.5:
        return {
            "category": "online_identifier",
            "sensitivity": "medium",
            "confidence": 0.9,
            "reason": "values are IP or MAC addresses",
        }
    if ratio(rules.PHONE) >= 0.5:
        return {"category": "phone", "sensitivity": "medium", "confidence": 0.9, "reason": "values are phone numbers"}
    if ratio(rules.PAN) >= 0.5 or ratio(rules.AADHAAR) >= 0.5:
        return {
            "category": "government_id",
            "sensitivity": "high",
            "confidence": 0.9,
            "reason": "values match a national ID format",
        }
    if sheet.profile[column].type == "string" and sum(len(value) for value in values) / count > 15:
        hits = sum(bool(rules.EMAIL.search(value) or rules.PHONE.search(value)) for value in values)
        if hits and hits / count >= 0.02:
            return {
                "category": "free_text",
                "sensitivity": "medium",
                "confidence": 0.7,
                "reason": f"{hits} free-text values contain a phone number or e-mail",
            }
    return None


def verify_pii(sheet: Sheet, column: str, category: str) -> bool:
    """Require the observed values to support the category suggested by the LLM."""
    profile = sheet.profile[column]
    values = [value for value in sheet.values(column) if not is_blank(value)]
    if not values:
        return False
    texts = [str(value).strip() for value in values]

    def share(predicate: Callable[[str], bool]) -> float:
        return sum(1 for text in texts if predicate(text)) / len(texts)

    if category == "date_of_birth":
        if re.search(r"dob|birth", column, re.I):
            return True
        dates = sorted(date for date in (coerce(value, "date") for value in values) if date)
        return bool(dates) and dates[len(dates) // 2] < dt.date.today() - dt.timedelta(days=16 * 365)
    if category in ("government_id", "bank_account"):
        finding = rule_pii(sheet, column)
        return bool(finding and finding["category"] == category)
    if category == "address":
        return profile.type == "string" and share(lambda text: bool(re.search(r"\d", text)) and len(text.split()) >= 3) >= 0.5
    if category == "person_name":
        return (
            profile.type == "string"
            and not rules.NOT_PERSON_HEADER.search(column)
            and share(lambda text: 2 <= len(text.split()) <= 5 and not re.search(r"\d", text)) >= 0.7
            and share(lambda text: bool(rules.COMPANY.search(text))) < 0.2
        )
    if category == "email":
        return share(lambda text: bool(rules.EMAIL.search(text))) >= 0.5
    if category == "phone":
        return not _machine_ids(texts) and share(lambda text: bool(rules.PHONE.search(text))) >= 0.5
    if category == "free_text":
        return profile.type == "string" and not _machine_ids(texts) and sum(len(text) for text in texts) / len(texts) > 15
    if category == "financial":
        return bool(re.search(r"salary|income|wage|card|credit|payroll", column, re.I))
    if category in ("demographic", "health", "biometric", "personal_id"):
        finding = rule_pii(sheet, column)
        return bool(finding and finding["category"] == category)
    if category == "online_identifier":
        return share(lambda text: bool(_IP_OR_MAC.fullmatch(text))) >= 0.5 or bool(
            re.search(r"\b(ip|mac)[\s_]*(address)?\b|device[\s_]*id", column, re.I)
        )
    if category == "location":
        return bool(_LOCATION_NAME.search(column))
    return False


def detect_pii(
    sheet: Sheet,
    columns: list[str],
    *,
    ask_json: Callable,
    analysis_data: Callable[[Sheet], list[str]],
    fix_column: Callable[[str | None, Sheet], str | None],
    rule_pii_fn: Callable[[Sheet, str], dict | None],
    verify_pii_fn: Callable[[Sheet, str, str], bool],
) -> list[dict]:
    """Combine LLM proposals with value-based checks and deterministic rules."""
    if not columns:
        return []
    lines = "\n".join(
        f'- "{column}": {sheet.profile[column].type}; e.g. {", ".join(sheet.profile[column].samples[:3])}'
        for column in columns
    )
    found = {}
    for data in analysis_data(sheet):
        data_instruction = (
            "Complete sheet data; classify using every supplied row:\n" + data
            if get_settings().full_sheet_llm_analysis
            else "Profile-only analysis is enabled:\n" + data
        )
        try:
            response = ask_json(
                PII_SYSTEM,
                PII_PROMPT.format(sheet=sheet.name, columns=lines, data_instruction=data_instruction),
            )
            items = response.get("columns") or response.get("pii") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                column = fix_column(item.get("column"), sheet)
                category = str(item.get("category") or "none").strip().lower()
                if column not in columns or category not in PII_CATEGORIES or category == "other":
                    continue
                try:
                    confidence = min(max(float(item.get("confidence", 0.7)), 0.0), 1.0)
                except (TypeError, ValueError):
                    confidence = 0.7
                if confidence < 0.5 or not verify_pii_fn(sheet, column, category):
                    log.info("PII suggestion rejected by evidence: %s.%s as %s", sheet.name, column, category)
                    continue
                previous = found.get(column)
                if not previous or confidence > previous["confidence"]:
                    found[column] = {
                        "column": column,
                        "category": category,
                        "sensitivity": nist_pii.SENSITIVITY[nist_pii.CATEGORIES[category][2]],
                        "confidence": confidence,
                        "reason": f"LLM classified the column as {category.replace('_', ' ')}; values are consistent",
                        "detected_by": "llm",
                        "status": "detected",
                    }
        except Exception as exc:
            log.warning("PII detection failed for sheet %s: %s", sheet.name, exc)
    for column in columns:
        finding = rule_pii_fn(sheet, column)
        if finding and column not in found:
            found[column] = {"column": column, **finding, "detected_by": "rules", "status": "detected"}
    return list(found.values())
