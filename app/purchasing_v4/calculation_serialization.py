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
    # Preserve canonical_bytes({as_of, rows, source_exceptions}) byte-for-byte,
    # without constructing another complete 126-day source snapshot in memory.
    hashed=hashlib.sha256()
    hashed.update(b'{"as_of":');hashed.update(canonical_bytes(as_of))
    hashed.update(b',"rows":[')
    for index,row in enumerate(rows):
        if index:hashed.update(b',')
        hashed.update(canonical_bytes(row))
    hashed.update(b'],"source_exceptions":')
    hashed.update(canonical_bytes(source_exceptions));hashed.update(b'}')
    return hashed.hexdigest()


def result_digest(batch):
    return hashlib.sha256(canonical_bytes(batch)).hexdigest()
