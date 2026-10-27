"""Finds the stock(s) a query is about, for the '#Stockname @member' line under Doc's answer.

Uses nse_stocks.json (all NSE companies with their common names; the same file the View Finder uses), plus the
commodities and indices members ask about. Tags are written the way Doc writes them: '#Kpittech', '#Silver'.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
EXTRA = {  # not NSE companies, or names members use that the list doesn't have
    "gold": "gold", "silver": "silver", "crude": "crude", "crudeoil": "crude", "crude oil": "crude",
    "natural gas": "naturalgas", "naturalgas": "naturalgas", "natgas": "naturalgas", "ng": "naturalgas",
    "copper": "copper", "aluminium": "aluminium", "zinc": "zinc", "nifty": "nifty", "nifty 50": "nifty",
    "banknifty": "banknifty", "bank nifty": "banknifty", "bnf": "banknifty", "finnifty": "finnifty",
    "sensex": "sensex", "nasdaq": "nasdaq", "bitcoin": "bitcoin", "btc": "bitcoin",
    "hul": "hindunilvr", "sbi": "sbin", "ril": "reliance", "infosys": "infy", "l&t": "lt", "hdfc": "hdfcbank",
    "icici": "icicibank", "kotak": "kotakbank", "axis": "axisbank", "airtel": "bhartiartl",
    "icici pru": "icicipruli", "icici pru life": "icicipruli", "icici prudential": "icicipruli", "icici lombard": "icicigi",
    "shree refrigeration": "shreeref", "shree refrigerations": "shreeref",
}
SKIP = {"query", "doc", "sir", "pls", "please", "view", "views", "latest", "update", "updated", "chart", "for", "the", "and",
        "long", "short", "term", "target", "targets", "hold", "buy", "sell", "on", "in", "of", "me", "is", "it", "my",
        # Hindi/Hinglish words that are also NSE symbols ("crude oil par thoda long term view")
        "kya", "hai", "par", "pe", "ka", "ki", "ke", "ko", "se", "mein", "bhi", "aur", "nahi", "abhi", "aaj", "kal",
        "thoda", "sab", "ab", "jo", "yeh", "ye", "wo", "kaise", "kab", "tak", "liye", "wala", "wali", "ek", "do"}


def _load():
    data = json.load(open(os.path.join(_HERE, "nse_stocks.json"), encoding="utf-8"))
    not_stocks = set(data.get("not_stocks", []))
    alias, strict = {}, set()
    for st in data["stocks"]:
        for a in st["aliases"]:
            if a in not_stocks or len(a) < 2:
                continue
            alias.setdefault(a, st["symbol"])
            if " " in a and len(a.replace(" ", "")) >= 7:
                alias.setdefault(a.replace(" ", ""), st["symbol"])  # "kalyanjewellers"
        strict.update(st.get("strict", []))
    alias.update(EXTRA)
    return alias, strict


ALIAS, STRICT = _load()
_MULTI = sorted((a for a in ALIAS if " " in a), key=len, reverse=True)


def find_stocks(text, limit=3):
    """Symbols named in a query, in order. Falls back to the query's first word when nothing is recognised."""
    caps = {w.lower() for w in re.findall(r"\b[A-Z][A-Z0-9&]+\b", text or "")}  # "SAIL", "TCS": written as a ticker
    t = re.sub(r"@\w+", " ", (text or "").lower())
    t = re.sub(r"\(saved on [^)]*\)", " ", t)
    t = re.sub(r"[^a-z0-9& ]", " ", t)
    t = " " + re.sub(r"\s+", " ", t).strip() + " "
    hits = []  # (position in the query, symbol), so tags keep the query's order
    original = t
    for a in _MULTI:  # longest names first: "kalyan jewellers" before "kalyan"
        if f" {a} " in t:
            hits.append((original.find(f" {a} "), ALIAS[a]))
            t = t.replace(f" {a} ", " " + "_" * len(a) + " ")  # blank it out, keep positions
    words = t.split()
    pos = 0
    for i, w in enumerate(words):
        pos = t.find(f" {w} ", pos)
        if w in SKIP or w not in ALIAS:
            continue
        if (w in STRICT or len(w) <= 3) and i > 0 and w not in EXTRA and w not in caps:
            continue  # everyday words and very short symbols ("deep", "star", "par") only count as the first word
        hits.append((pos, ALIAS[w]))
    found = []
    for _, sym in sorted(hits):
        if sym not in found:
            found.append(sym)
    if not found:
        # the member's own word for it ("botz", "rblx"), but only if it is the query's first word: a later word
        # is usually not a name at all ("BT.A again as seeing..." -> "again"), and no tag is better than a wrong one
        first = next((w for w in words if w not in SKIP), "")
        if len(first) >= 3 and not first.isdigit():
            found.append(first)
    return found[:limit]


def tags(text):
    """'#Kpittech', or '#Sail #Tatasteel'; '' when the query names nothing."""
    return " ".join("#" + re.sub(r"[^a-z0-9]", "", s).capitalize() for s in find_stocks(text))
