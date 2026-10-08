"""PII classification following NIST guidance.

- NIST SP 800-122 (Guide to Protecting the Confidentiality of PII) decides WHAT is PII and HOW SENSITIVE it is:
  * identifiability: information that distinguishes or traces an individual (a "direct" identifier: name,
    personal identification numbers, e-mail/street address, phone, biometrics, device/asset identifiers) versus
    information that is "linked or linkable" to an individual (date of birth, demographics, medical, financial,
    employment details...). Linkable data becomes identifying once it sits in the same record as a direct
    identifier.
  * the PII confidentiality impact level (Low / Moderate / High) from the factors identifiability, quantity of PII,
    data field sensitivity, context of use, obligation to protect and access/location.
- NIST Privacy Framework v1.0 organises what to do about it: every finding is a data-inventory entry
  (Identify-P, ID.IM-P Inventory and Mapping) and the impact level selects the safeguards to apply
  (Protect-P PR.AC-P access control, PR.DS-P data security; Control-P CT.DP-P disassociated processing such as
  masking or de-identification).

This module only classifies; detection (patterns + LLM + evidence checks) stays in app/extraction.py and
app/rag.py. Raw values are never stored.
"""

LEVELS = ("low", "moderate", "high")

# category -> (label, identifier type, base data-field sensitivity, SP 800-122 basis)
CATEGORIES: dict[str, tuple[str, str, str, str]] = {
    "person_name": ("Person name", "direct", "low", "full name: distinguishes an individual"),
    "email": ("E-mail address", "direct", "low", "personal e-mail address: distinguishes an individual"),
    "phone": ("Telephone number", "direct", "low", "personal telephone number: distinguishes an individual"),
    "address": ("Postal address", "direct", "moderate", "street address: traces an individual"),
    "government_id": (
        "Government ID",
        "direct",
        "high",
        "personal identification number (SSN/PAN/Aadhaar, passport, driver's licence)",
    ),
    "bank_account": ("Financial account", "direct", "high", "financial account or card number"),
    "personal_id": (
        "Personal ID number",
        "direct",
        "moderate",
        "identification number assigned to a person (patient, employee, member number)",
    ),
    "biometric": ("Biometric", "direct", "high", "biometric record (fingerprint, face image, voice print)"),
    "online_identifier": ("Device / online ID", "direct", "moderate", "asset information: IP or MAC address"),
    "date_of_birth": ("Date of birth", "linkable", "moderate", "date of birth: linkable, aids re-identification"),
    "demographic": (
        "Demographic",
        "linkable",
        "moderate",
        "personal characteristics (gender, race, religion, nationality, marital status)",
    ),
    "health": ("Health / medical", "linkable", "high", "medical information about an individual"),
    "financial": ("Financial (salary, credit)", "linkable", "high", "financial information about an individual"),
    "location": ("Precise location", "linkable", "moderate", "geographical indicators tied to an individual"),
    "free_text": ("Free text", "linkable", "moderate", "unstructured text that can mention people and contacts"),
    "other": ("Other personal data", "linkable", "moderate", "other information linked to an individual"),
}

# Privacy Framework safeguards per impact level (what the reviewer / platform owner should apply)
CONTROLS = {
    "low": ["ID.IM-P: keep in the data inventory", "PR.AC-P: access limited to users granted this knowledge base"],
    "moderate": [
        "ID.IM-P: keep in the data inventory",
        "PR.AC-P: grant access only to users who need it",
        "CT.DP-P: mask or omit in answers and exports where it is not needed",
    ],
    "high": [
        "ID.IM-P: keep in the data inventory",
        "PR.AC-P: restrict access to named users; review grants regularly",
        "PR.DS-P: protect at rest and in transit; avoid copying to other systems",
        "CT.DP-P: mask, tokenise or remove unless the use case requires it",
    ],
}

LARGE_QUANTITY = 10_000  # SP 800-122 "quantity of PII": many individuals raise the impact of a breach

# legacy three-level sensitivity column of kb_pii_fields
SENSITIVITY = {"low": "low", "moderate": "medium", "high": "high"}


def _raise(level: str) -> str:
    return LEVELS[min(LEVELS.index(level) + 1, len(LEVELS) - 1)]


def _lower(level: str) -> str:
    return LEVELS[max(LEVELS.index(level) - 1, 0)]


def _info(category: str) -> tuple[str, str, str, str]:
    return CATEGORIES.get(category, CATEGORIES["other"])


def assess(items: list[dict], sheets: dict) -> list[dict]:
    """Add the NIST fields to every PII finding (graph columns), judged per sheet, i.e. per set of records:

    nist_identifier  "direct" | "linkable"
    nist_impact      "low" | "moderate" | "high"   (PII confidentiality impact level)
    nist_factors     why that level (identifiability, data field sensitivity, linkability, quantity)
    nist_controls    Privacy Framework safeguards for that level
    sensitivity      the same level on the legacy high/medium/low scale
    """
    active_by_sheet: dict[str, list[dict]] = {}
    for p in items:
        if p.get("status", "detected") != "dismissed":
            active_by_sheet.setdefault(p["sheet"], []).append(p)
    out = []
    for p in items:
        _label, ident, level, basis = _info(p["category"])
        factors = [f"identifiability: {'direct identifier' if ident == 'direct' else 'linked or linkable'}"]
        factors.append(f"data field sensitivity: {level} ({basis})")
        same_records = [q for q in active_by_sheet.get(p["sheet"], []) if q["column"] != p["column"]]
        directs = [q["column"] for q in same_records if _info(q["category"])[1] == "direct"]
        sensitive = [q["column"] for q in same_records if _info(q["category"])[2] == "high"]
        if ident == "linkable":
            if directs:
                factors.append(f"linkability: stored with direct identifier(s) {', '.join(directs[:3])}")
            else:  # not linked to an identity in these records: lower risk on its own
                level = _lower(level)
                factors.append("linkability: no direct identifier in the same records")
        if ident == "direct" and sensitive and level != "high":
            level = _raise(level)
            factors.append(f"context: identifies records that also hold {', '.join(sensitive[:3])}")
        rows = (sheets.get(p["sheet"]) or {}).get("rows") or 0
        if rows >= LARGE_QUANTITY and level != "high":
            level = _raise(level)
            factors.append(f"quantity: about {rows:,} records")
        elif rows:
            factors.append(f"quantity: about {rows:,} records")
        out.append(
            {
                **p,
                "nist_identifier": ident,
                "nist_impact": level,
                "nist_factors": factors,
                "nist_controls": CONTROLS[level],
                "sensitivity": SENSITIVITY[level],
            }
        )
    return out


def document_level(category: str) -> str:
    """Impact level for PII found in a document (RAG): data field sensitivity, raised to at least moderate
    because a document links what it mentions to the people in it."""
    level = _info(category)[2]
    return "moderate" if level == "low" else level


def catalogue() -> list[dict]:
    """For the Review screen: category, label, identifier type and base level."""
    return [
        {"category": c, "label": v[0], "identifier": v[1], "base_impact": v[2], "basis": v[3]}
        for c, v in CATEGORIES.items()
    ]
