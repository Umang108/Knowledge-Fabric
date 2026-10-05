"""Patterns shared by PII detection in spreadsheets (extraction.py) and documents (rag.py)."""

import re

# values
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,12}\d")
PAN = re.compile(r"[A-Z]{5}\d{4}[A-Z]")  # Indian permanent account number
AADHAAR = re.compile(r"\d{4}\s?\d{4}\s?\d{4}")

# Legal-form and common business words found in organisation names.
COMPANY = re.compile(
    r"\b(ltd|limited|pvt|private|inc|llc|llp|plc|gmbh|ag|sa|sarl|bv|aps|co|corp|corporation|company|"
    r"group|holdings|services|solutions|technologies|enterprises|industries|international|bank)\b",
    re.I,
)
# Column headers that name an organisation or thing, never a person.
NOT_PERSON_HEADER = re.compile(
    r"company|supplier|vendor|business|organi[sz]ation|firm|brand|product|store|shop|"
    r"warehouse|city|country|region|department|team|category",
    re.I,
)
