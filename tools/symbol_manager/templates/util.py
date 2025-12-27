#services/util.py   
from __future__ import annotations
import re
import decimal

def to_numeric(val):
    if val is None: return None
    s = str(val).strip()
    if not s: return None
    # remove commas and %
    s = s.replace(',', '').replace('%', '')
    # market cap shortcuts like '1.2T','500B','12.3M'
    m = re.match(r'^([0-9.]+)\s*([TtBbMmKk])?$', s)
    mul = 1
    if m and m.group(2):
        mul = {'K':1e3,'k':1e3,'M':1e6,'m':1e6,'B':1e9,'b':1e9,'T':1e12,'t':1e12}[m.group(2)]
        s = m.group(1)
    try:
        d = decimal.Decimal(s) * decimal.Decimal(mul)
        return d
    except decimal.InvalidOperation:
        try:
            return decimal.Decimal(s)
        except decimal.InvalidOperation:
            return None

def to_bigint(val):
    if val is None: return None
    s = str(val).strip().replace(',', '')
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None
