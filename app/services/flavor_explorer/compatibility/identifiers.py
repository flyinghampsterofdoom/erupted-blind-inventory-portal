"""Exact string identities. Name similarity never establishes product identity."""
import re
import unicodedata
from decimal import Decimal, InvalidOperation

GTIN_KINDS = {"upc", "ean", "gtin", "barcode"}
KINDS = GTIN_KINDS | {"sku", "mpn", "alternate"}
def identifier(value):
    if not isinstance(value, str):
        raise ValueError("Identifiers must be strings; leading zeros are significant.")
    clean = value.strip(" \t\r\n")
    if not clean or len(clean) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in clean):
        raise ValueError("Enter one complete identifier of 1–512 characters.")
    return clean

def valid_gtin(value):
    return bool(re.fullmatch(r"(?:\d{8}|\d{12}|\d{13}|\d{14})", value, re.ASCII)) and (
        sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(value[:-1])))
        + int(value[-1])) % 10 == 0

def gtin_key(value):
    """Only UPC-A and its valid zero-prefixed EAN-13 are equivalent here.

    GTIN-14 package indicators, EAN-8 and SKU leading zeros remain distinct.
    A numeric SKU does not acquire a GTIN namespace from its appearance.
    """
    try: value = identifier(value)
    except ValueError: return None
    digits = re.sub(r"[ -]", "", value)
    if not valid_gtin(digits):
        return None
    if len(digits) == 12:
        return "upca:" + digits
    if len(digits) == 13 and digits.startswith("0"):
        return "upca:" + digits[1:]
    return str(len(digits)) + ":" + digits

def identity_fields(value, kind):
    kind = str(kind).lower()
    if kind not in KINDS:
        raise ValueError("Choose SKU, GTIN/UPC/EAN, manufacturer part number or alternate identifier.")
    value = identifier(value)
    key = gtin_key(value) if kind in GTIN_KINDS else None
    if kind in GTIN_KINDS and key is None:
        raise ValueError("The GTIN/UPC/EAN length or check digit is invalid.")
    return {"kind": kind, "value": value, "exact_key": value, "gtin_key": key}

def identifier_intent(value):
    # Exact lookup always runs first. These inputs never silently become a
    # fuzzy product-name result when no exact identity is known.
    return bool(re.fullmatch(r"[0-9 -]+", value, re.ASCII)
                or re.fullmatch(r"(?=.*\d)[A-Z0-9_-]{6,}", value, re.ASCII))

def words(value):
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9.]+", " ", text).split())

def resistance(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).lower().replace("ohm", "").replace("ω", "").strip())
    except InvalidOperation:
        raise ValueError("Resistance must be a positive decimal value.")
    if not number.is_finite() or not 0 < number <= 10:
        raise ValueError("Resistance must be from 0 exclusive to 10 ohms.")
    return format(number.normalize(), "f")

def query_resistance(value):
    value = re.sub(r"\b(?:corex|version|gen(?:eration)?|v)\s*\d+(?:\.\d+)?", " ", value, flags=re.I)
    match = re.search(r"(?<!\w)(\d*\.\d+|\d+\s*(?:ohm|Ω|ω))", value, re.I)
    return resistance(match.group(1)) if match else None
