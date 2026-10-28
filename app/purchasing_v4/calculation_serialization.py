"""Canonical frozen evidence/result documents, exact fractions and explicit states."""
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from fractions import Fraction
import hashlib
import json


def primitive(value):
    if is_dataclass(value):
        return {f.name:primitive(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value,(tuple,list)):
        return [primitive(x) for x in value]
    if isinstance(value,dict):
        return {str(key):primitive(v) for key,v in value.items()}
    if isinstance(value,Enum):return value.value
    if isinstance(value,Fraction):return dict(numerator=value.numerator,denominator=value.denominator)
    if isinstance(value,(Decimal,date,datetime)):return str(value) if isinstance(value,Decimal) else value.isoformat()
    return value


def canonical_bytes(value):
    return json.dumps(primitive(value),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()


def evidence_digest(rows,*,as_of,source_exceptions=()):
    return hashlib.sha256(canonical_bytes(dict(rows=rows,as_of=as_of,source_exceptions=source_exceptions))).hexdigest()


def result_digest(batch):
    return hashlib.sha256(canonical_bytes(batch)).hexdigest()
