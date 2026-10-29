#!/usr/bin/env python3
"""
Crosswalk the harvested metadata snapshots to MODS 3.8.

Reads each source's latest snapshot CSV (see crosswalk/config.json), maps every record
to MODS, and writes up to three outputs per source:

    mods/<source>/<source>_mods.csv      the MODS template layout (crosswalk/MODS_template.csv):
                                         row 1 = MODS XPaths, row 2 = labels, then one row per record
    build/<source>/<source>_mods.xml     a <modsCollection> of MODS 3.8 records      (--xml)
    build/<source>/<source>_solr.json    Solr documents for Blacklight                (--solr-json)

Usage:
    python scripts/mods_crosswalk.py                       # every source that has a snapshot
    python scripts/mods_crosswalk.py --source archivesspace --xml --solr-json
    python scripts/mods_crosswalk.py --xml --validate      # also check each record against the MODS 3.8 XSD
    python scripts/mods_crosswalk.py --solr-json --solr-url https://solr.example.edu/solr/blacklight-core

Mapping rules live in this script (one function per source system); the values that
are likely to change live in crosswalk/: config.json (repository names, link patterns),
genre_rules.csv (genre terms), languages.csv (language codes) and MODS_template.csv
(the CSV column layout). See mods/README.md for the field-by-field crosswalk.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CROSSWALK = ROOT / "crosswalk"
SEP = " || "          # same separator the harvest CSVs use for repeated values
MODS_NS = "http://www.loc.gov/mods/v3"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", MODS_NS)
ET.register_namespace("xlink", XLINK_NS)
ET.register_namespace("xsi", XSI_NS)

csv.field_size_limit(sys.maxsize)


# ==========================================================================
# Lookup tables (crosswalk/)
# ==========================================================================
def load_config():
    with open(CROSSWALK / "config.json", encoding="utf-8") as f:
        return json.load(f)


def load_languages():
    """variant (lower case) -> (code, label)."""
    table, labels = {}, {}
    with open(CROSSWALK / "languages.csv", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code, label = row["code"].strip(), row["label"].strip()
            labels[code] = label
            for v in [code, label] + row["variants"].split("|"):
                if v.strip():
                    table[v.strip().lower()] = code
    return table, labels


def load_genre_rules():
    rules = defaultdict(list)
    with open(CROSSWALK / "genre_rules.csv", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rules[row["source"].strip()].append(
                (row["field"].strip(), re.compile(row["pattern"]), row["genre"].strip(),
                 row["authority"].strip()))
    return rules


def load_template():
    """The two header rows of the MODS CSV template: XPaths and labels."""
    with open(CROSSWALK / "MODS_template.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1]


LANG_TABLE, LANG_LABELS = load_languages()
GENRE_RULES = load_genre_rules()


# ==========================================================================
# Small helpers
# ==========================================================================
def split(value):
    """Split a harvest cell on ' || ' and drop blanks."""
    return [v.strip() for v in (value or "").split("||") if v.strip()]


def dedupe(values):
    seen, out = set(), []
    for v in values:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def clean_title(t):
    return re.sub(r"[\s,;:/]+$", "", t.strip())


def strip_period(t):
    t = t.strip()
    # keep the period on initials/abbreviations ("Inc.", "Jr.", "U.S.")
    if t.endswith(".") and not re.search(r"(\b[A-Z]|\b(?:Inc|Co|Jr|Sr|etc|Ltd|St))\.$", t):
        t = t[:-1]
    return t.strip()


def clean_markup(t):
    """ArchivesSpace notes can carry HTML or BBCode links: keep the text and the address."""
    t = re.sub(r"\[url=([^\]]+)\](.*?)\[/url\]", r"\2 (\1)", t)
    t = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", t, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"</?(p|emph|title|lb|span|i|b|em|strong)\b[^>]*>", " ", t)).strip()


def is_url(v):
    return v.startswith(("http://", "https://"))


def normalize_languages(values):
    codes = []
    for v in values:
        for part in re.split(r"\s*[;,/]\s*|\s+and\s+", v):
            key = part.strip().rstrip(".").lower()
            if not key or key in ("und", "zxx", "mul"):
                continue
            code = LANG_TABLE.get(key)
            if not code:  # prose such as "Materials in English and German."
                code_list = [c for name, c in LANG_TABLE.items()
                             if len(name) > 3 and re.search(rf"\b{re.escape(name)}\b", key)]
                codes.extend(code_list)
                continue
            codes.append(code)
    return dedupe(codes)


def apply_genre_rules(source, fields):
    """fields: dict of field name -> list of values to test."""
    out = []
    for field, pattern, genre, authority in GENRE_RULES.get(source, []):
        values = [v for vs in fields.values() for v in vs] if field == "*" else fields.get(field, [])
        if field == "*" and not values:
            values = [""]
        if any(pattern.search(v) for v in values):
            out.append((genre, authority))
    return dedupe(out)


MIME_TYPES = [
    (r"^image/", "still image"), (r"^audio/", "sound recording"), (r"^video/", "moving image"),
    (r"^(application/pdf|text/|application/msword|application/vnd.openxmlformats-officedocument.word)", "text"),
    (r"^application/vnd\.(ms-excel|openxmlformats-officedocument\.spreadsheetml)", "software, multimedia"),
    (r"^application/vnd\.(ms-powerpoint|openxmlformats-officedocument\.presentationml)", "software, multimedia"),
]
EXTENSIONS = {
    "still image": "jpg jpeg tif tiff png gif bmp jp2 heic dng cr2 nef psd",
    "text": "pdf txt doc docx rtf odt xml html htm epub",
    "sound recording": "mp3 wav aif aiff flac m4a ogg wma",
    "moving image": "mp4 mov avi mkv mpg mpeg wmv m4v webm dv",
    "software, multimedia": "xls xlsx csv ppt pptx zip",
}
EXT_TO_TYPE = {e: t for t, exts in EXTENSIONS.items() for e in exts.split()}


def type_from_mime(mime):
    for pattern, t in MIME_TYPES:
        if re.search(pattern, mime):
            return t
    return ""


# ==========================================================================
# Dates
# ==========================================================================
MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
YEAR_RE = re.compile(r"(?<!\d)(1[0-9]\d\d|20\d\d)(?![\d])")
UNDATED_RE = re.compile(r"(?i)^(undated|unknown|unk(-unk)?|n\.?\s?d\.?|no date|date unknown|\?+)$")


def parse_date(text):
    """Free-text date -> (edtf, start_year, end_year). Unparseable -> ('', None, None)."""
    t = (text or "").strip()
    if not t or UNDATED_RE.match(t):
        return "", None, None
    # MuseumPlus "Early: 1837; Late: 1901"
    m = re.match(r"(?i)^early:\s*(.*?)\s*(?:;\s*late:?\s*(.*))?$", t)
    if m and (m.group(1) or m.group(2)):
        a = parse_date(m.group(1))
        b = parse_date(m.group(2)) if m.group(2) else a
        if a[1] is None and b[1] is None:
            return "", None, None
        start = a[1] if a[1] is not None else b[1]
        end = b[2] if b[2] is not None else a[2]
        if start == end and a[0] and not m.group(2):
            return a
        return (f"{start}/{end}" if start != end else str(start)), start, end

    approx = bool(re.search(r"(?i)\b(c\.|ca\.?|circa|approx\.?|about)\s*\d|\bc\d{4}", t))
    lower = t.lower()

    # one complete date: "December 18, 1989" / "18 December 1989" / "3/20/1964"
    full = re.findall(r"\b([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", lower)
    full = [(MONTHS.get(mo), int(d), int(y)) for mo, d, y in full if MONTHS.get(mo)]
    if not full:
        full = [(int(mo), int(d), int(y)) for mo, d, y in
                re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", lower) if 0 < int(mo) <= 12 and 0 < int(d) <= 31]
    years = [int(y) for y in YEAR_RE.findall(t)]

    # centuries and decades widen the range
    spans = []
    for n, _ in re.findall(r"(\d{1,2})(st|nd|rd|th)\s+century", lower):
        c = int(n)
        lo, hi = (c - 1) * 100, (c - 1) * 100 + 99
        if re.search(rf"early\s+{n}", lower):
            hi = lo + 33
        elif re.search(rf"(mid|middle)[\s-]+{n}", lower):
            lo, hi = lo + 33, lo + 66
        elif re.search(rf"late\s+{n}", lower):
            lo = lo + 66
        spans.append((lo, hi))
    for d in re.findall(r"\b(1[0-9]\d|20\d)0'?s\b", lower):
        spans.append((int(d) * 10, int(d) * 10 + 9))
    years_all = years + [y for s in spans for y in s]
    years_all = [y for y in years_all if 1000 <= y <= 2100]
    if not years_all:
        return "", None, None
    start, end = min(years_all), max(years_all)

    if len(full) == 1 and len(set(years)) == 1 and not spans:
        mo, d, y = full[0]
        try:
            edtf = dt.date(y, mo, d).isoformat()
        except ValueError:
            edtf = str(y)
    elif len(set(years)) == 1 and not spans:
        mo = next((MONTHS[w] for w in re.findall(r"[a-z]+", lower) if w in MONTHS), None)
        single_month = len({w for w in re.findall(r"[a-z]+", lower) if w in MONTHS}) == 1
        edtf = f"{start}-{mo:02d}" if mo and single_month and not re.search(r"\d\s*-\s*\d|\bto\b", lower) \
            else str(start)
        if re.search(r"\d{4}\s*-\s*$", t):         # "1945-" open-ended
            return f"{start}/..", start, start
    else:
        edtf = f"{start}/{end}" if start != end else str(start)
    if approx and "/" not in edtf:
        edtf += "~"
    return edtf, start, end


def display_date(text):
    t = (text or "").strip()
    if UNDATED_RE.match(t):
        return "undated"
    m = re.match(r"(?i)^early:\s*(.*?)\s*(?:;\s*late:?\s*(.*))?$", t)
    if m and m.group(1):
        t = f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)
    return "undated" if UNDATED_RE.match(t) else t


def iso_display(d):
    """'1999-12-31' -> 'December 31, 1999'."""
    try:
        x = dt.date.fromisoformat(d)
        return f"{x.strftime('%B')} {x.day}, {x.year}"
    except ValueError:
        return d


# ==========================================================================
# Names and subjects
# ==========================================================================
CORPORATE_WORDS = re.compile(
    r"(?i)\b(university|college|library|libraries|society|association|inc|company|corporation|board|"
    r"department|office|school|church|club|committee|council|museum|institute|academy|foundation|"
    r"senate|government|commission|center|centre|press|archives|agency|bureau|league|union|"
    r"federation|cooperative|hospital|program|team)\b")
JURISDICTION = re.compile(r"^(Iowa|United States|Cedar Falls|Waterloo)(\s\(.*?\))?\.\s")
US_STATES = (
    "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|"
    "Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|"
    "Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
    "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|"
    "South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming")
STATE_ABBR = r"Ala\.|Ariz\.|Ark\.|Calif\.|Colo\.|Conn\.|Del\.|Fla\.|Ga\.|Ill\.|Ind\.|Kan\.|Ky\.|La\.|Md\.|" \
             r"Mass\.|Mich\.|Minn\.|Miss\.|Mo\.|Mont\.|Neb\.|Nev\.|N\.H\.|N\.J\.|N\.M\.|N\.Y\.|N\.C\.|N\.D\.|" \
             r"Okla\.|Or\.|Pa\.|R\.I\.|S\.C\.|S\.D\.|Tenn\.|Tex\.|Vt\.|Va\.|Wash\.|W\. Va\.|Wis\.|Wyo\."
PLACE_RE = re.compile(
    rf"^(({US_STATES}|United States|Canada|Mexico|England|Great Britain|Europe|Africa|Asia|China|Japan|"
    rf"Germany|France|Russia|Soviet Union)$|.+\s\(({US_STATES}|{STATE_ABBR}|Iowa|Canada|England|"
    rf"Germany|France|Mexico|Africa|China|Japan|Russia)\)$)")
TEMPORAL_RE = re.compile(
    r"(?i)^((early|mid|late)[\s-]+)?\d{1,2}(st|nd|rd|th) century$|^\d{4}(\s*-\s*\d{0,4})?$|^(to|since) \d{4}$"
    r"|^\d{4}s$|^(world war|civil war|depression)")
PERSON_DATES = re.compile(r",\s*(\d{4}|b\.|d\.|ca\.)[\d\s?-]*\.?$")


def name_type(n):
    if JURISDICTION.match(n) or (CORPORATE_WORDS.search(n) and not PERSON_DATES.search(n)):
        return "corporate"
    if re.match(r"^[^,\d]+,\s*[^,\d]", n):
        return "personal"
    return ""


def subject_kind(term):
    """Classify the first part of a subject heading."""
    t = term.strip()
    if PLACE_RE.match(t):
        return "geographic"
    if TEMPORAL_RE.match(t):
        return "temporal"
    if PERSON_DATES.search(t) or JURISDICTION.match(t) or re.match(
            r"^(University of Northern Iowa|Iowa State (Normal School|Teachers College)|State College of Iowa)\b", t):
        return "name"
    return "topic"


def parse_heading(h):
    """'Rural schools--Iowa--History--20th century.' -> [('topic','Rural schools'),
    ('geographic','Iowa'), ('topic','History'), ('temporal','20th century')]"""
    h = re.sub(r"[\s;.,]+$", "", h.strip())
    if not h:
        return []
    parts = [p.strip() for p in re.split(r"\s*--\s*", h) if p.strip()]
    out = [(subject_kind(parts[0]), parts[0])]
    for p in parts[1:]:
        k = subject_kind(p)
        out.append(("topic" if k == "name" else k, p))
    return out


# ==========================================================================
# The record model
# ==========================================================================
def new_record(source, local_id):
    r = defaultdict(list)
    r["source"] = source
    r["local_id"] = [local_id]
    r["names"] = []          # dicts: part, role, role_code, identifier, dates, type
    r["subjects"] = []       # list of components [(kind, value), ...] per heading
    r["genres"] = []         # (term, authority)
    r["collection"] = False
    r["title_supplied"] = False
    r["years"] = []          # (start, end) pairs for Solr
    return r


def add_name(r, part, role="", role_code="", ntype=None, dates="", identifier=""):
    part = part.strip().rstrip(",")
    if not part:
        return
    if any(n["part"] == part and n["role"] == role for n in r["names"]):
        return
    r["names"].append({"part": part, "role": role, "role_code": role_code,
                       "type": name_type(part) if ntype is None else ntype,
                       "dates": dates, "identifier": identifier})


def add_subject(r, heading):
    comps = parse_heading(heading)
    if comps and comps not in r["subjects"]:
        r["subjects"].append(comps)


def add_date(r, raw, edtf=None, start=None, end=None):
    if edtf is None:
        edtf, start, end = parse_date(raw)
    disp = display_date(raw)
    if disp and disp not in r["display_date"]:
        r["display_date"].append(disp)
    if edtf and edtf not in r["date_created"]:
        r["date_created"].append(edtf)
    if start is not None:
        r["years"].append((start, end if end is not None else start))


def add_rights(r, text):
    """Sort a rights value into the template's accessCondition types."""
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()     # drop HTML markup
    if not t or t.lower() in ("none indicated", "none"):
        return
    m = re.search(r"creativecommons\.org/(licenses|publicdomain)/([a-z-]+)/?([\d.]+)?", t)
    if m:
        kind, code, ver = m.groups()
        label = ("CC0 1.0" if code == "zero" else "Public Domain Mark 1.0") if kind == "publicdomain" \
            else f"CC {code.upper()} {ver or ''}".strip()
        r["license"].append(label)
        r["license_url"].append(t if is_url(t) else "https://" + m.group(0))
        return
    if "rightsstatements.org" in t:
        r["rights_statement"].append(t)
        return
    low = t.lower()
    if t.startswith("©") or low.startswith("copyright"):
        r["rights_statement"].append(t)
    elif re.search(r"open for research|restrict|closed to|not available for|access to|by appointment", low) \
            and not re.search(r"copyright|publication rights|permission to (publish|reproduce)|unrestricted", low):
        r["access"].append(t)
    elif re.search(r"copyright|publication rights|reproduc|permission|privacy|disclosure|use of", low):
        r["use"].append(t)
    else:
        r["rights_statement"].append(t)


# ==========================================================================
# Source mappers: each yields records built from one snapshot row
# ==========================================================================
def rejoin_headings(values):
    """Digital Commons splits subject strings at commas, so 'World War, 1939-1945--Women'
    arrives as 'World War' || '1939-1945--Women'. Put those pieces back together."""
    def fragment(x):          # a bare piece such as 'Wright' or 'Frank Lloyd'
        return ";" not in x and "--" not in x and len(x.split()) <= 3

    merged = []
    for v in values:
        if merged and re.match(r"^(\d{4}(-\d{0,4})?|\d{1,2}(st|nd|rd|th) century)(\s*(--|;|$))", v):
            # personal names arrive in three pieces: 'Wright' || 'Frank Lloyd' || '1867-1959--Posters'
            take = 2 if len(merged) >= 2 and fragment(merged[-1]) and fragment(merged[-2]) else 1
            head = ", ".join(x.rstrip("; ") for x in merged[-take:])
            merged[-take:] = [head + ", " + v]
        else:
            merged.append(v)
    return merged


def map_digital_commons(rows, cfg, source):
    """UNI ScholarWorks (bepress Digital Commons) oai_dc."""
    home = cfg.get("home_publisher", "")
    for row in rows:
        publishers = split(row.get("publisher"))
        external = bool(home) and home not in publishers
        if external and not cfg.get("include_external", False):
            continue
        r = new_record(source, row["oai_identifier"])
        r["key"] = row["oai_identifier"].split(":")[-1]
        r["datestamp"] = row.get("datestamp", "")
        r["title"] = [clean_title(t) for t in split(row.get("title"))]
        for d in split(row.get("description")):
            (r["thumbnail"] if is_url(d) and re.search(r"thumbnail|\.(jpe?g|png|gif)$", d, re.I)
             else r["abstract"]).append(d)
        for c in split(row.get("creator")):
            add_name(r, c, "creator", "cre")
        for c in split(row.get("contributor")):
            add_name(r, c, "contributor", "ctb")

        mimes = split(row.get("format"))
        r["media_type"] = mimes
        types = [type_from_mime(m) for m in mimes]
        dc_types = [t.lower() for t in split(row.get("type"))]
        if "video" in dc_types:
            types.append("moving image")
        types = dedupe([t for t in types if t]) or ["text"]
        r["type_of_resource"] = types
        r["genres"] += [(t.replace("_", " "), "") for t in dc_types if t not in ("text", "video", "image")]

        for s in rejoin_headings(split(row.get("subject"))):
            for heading in s.split(";"):
                add_subject(r, heading)

        for d in split(row.get("date")):
            day = d[:10]
            if re.match(r"\d{4}-\d\d-\d\d$", day):
                year = int(day[:4])
                if day[5:] == "01-01":      # Digital Commons stores year-only dates as Jan 1
                    add_date(r, str(year), str(year), year, year)
                else:
                    r["date_created"].append(day)
                    r["display_date"].append(iso_display(day))
                    r["years"].append((year, year))
            else:
                add_date(r, d)

        r["language"] = normalize_languages(split(row.get("language")))
        hosts = split(row.get("source"))
        r["host_title"] = hosts
        for ident in split(row.get("identifier")):
            if "doi.org/" in ident or ident.lower().startswith("doi:"):
                r["doi"].append(ident)
            elif is_url(ident):
                if "/viewcontent/" in ident or "/context/" in ident:
                    r["file_url"].append(ident)
                elif not r["url"]:
                    r["url"].append(ident)
                else:
                    r["related_url"].append(ident)
            elif ident:
                r["other_id"].append(ident)
        for rel in split(row.get("relation")):
            (r["related_url"] if is_url(rel) else r["note"]).append(rel)
        for c in split(row.get("coverage")):
            add_subject(r, c)
        for t in split(row.get("rights")):
            add_rights(r, t)
        r["repository"] = [", ".join(publishers)] if external else [cfg["repository"]]
        if external:
            r["note"].append(f"Record contributed by {', '.join(publishers)}.")
        r["genres"] += apply_genre_rules(source, {"source": hosts, "format": mimes,
                                                  "setSpec": split(row.get("setSpec"))})
        yield r


def map_archivesspace(rows, cfg, source):
    """ArchivesSpace oai_dc: collections (resources) and their components (archival objects)."""
    # Index collections by call number and by title so components can link to them.
    by_callno, by_title = {}, {}
    for row in rows:
        if row.get("record_type") == "resource":
            url = next((i for i in split(row.get("identifier")) if is_url(i)), "")
            for i in split(row.get("identifier")):
                if not is_url(i):
                    by_callno[i.strip()] = url
            by_title[row["title"].strip()] = url

    for row in rows:
        uri = row["oai_identifier"].replace("oai:archivesspace:", "")
        r = new_record(source, uri)
        r["key"] = uri.strip("/").replace("/", "_")
        r["datestamp"] = row.get("datestamp", "")
        r["record_type"] = row.get("record_type", "")
        is_collection = r["record_type"] == "resource"
        r["collection"] = is_collection
        r["title"] = [clean_title(t) for t in split(row.get("title"))]
        r["abstract"] = [clean_markup(d) for d in split(row.get("description"))]
        for c in split(row.get("creator")):
            add_name(r, strip_period(c) if not c.endswith(("Inc.", "Jr.", "Sr.")) else c, "creator", "cre")
        r["type_of_resource"] = [cfg.get("type_of_resource", "mixed material")]
        r["genres"] += [(strip_period(t), "") for t in split(row.get("type"))]
        for s in split(row.get("subject")) + split(row.get("coverage")):
            add_subject(r, s)
        for d in split(row.get("date")):
            if d.lower().startswith("date acquired"):
                continue
            add_date(r, d)
        r["extent"] = [re.sub(r"\s*other_unmapped$", "", x) for x in split(row.get("format"))]
        r["language"] = normalize_languages(split(row.get("language")))
        for i in split(row.get("identifier")):
            (r["url"] if is_url(i) else r["shelf"]).append(i)

        relations = [clean_markup(x) for x in split(row.get("relation"))]
        if is_collection:
            r["note"] += relations                     # related-materials notes
        else:
            for rel in relations[:1]:
                m = re.match(r"^(.*),\s*([^,]*\d[^,]*)$", rel)
                title, callno = (m.group(1), m.group(2)) if m else (rel, "")
                r["host_title"].append(title.strip())
                if callno:
                    r["host_id"].append(callno.strip())
                link = by_callno.get(callno.strip()) or by_title.get(title.strip())
                if link:
                    r["host_url"].append(link)
            r["note"] += relations[1:]
        for t in split(row.get("rights")):
            add_rights(r, t)
        repos = [p for p in split(row.get("publisher")) if p != "University of Northern Iowa"]
        prefix = cfg.get("repository_prefix", "")
        r["repository"] = [f"{prefix}. {p}" if prefix else p for p in repos] or [prefix]
        r["genres"] += apply_genre_rules(source, {"record_type": [r["record_type"]]})
        yield r


def map_preservica(rows, cfg, source):
    """Preservica oai_dc: folders (StructuralObject) and assets (InformationObject)."""
    # The OAI feed can list a record several times; keep the newest version of each.
    latest = {}
    for row in rows:
        k = row["oai_identifier"]
        if k not in latest or row.get("datestamp", "") > latest[k].get("datestamp", ""):
            latest[k] = row
    skip = re.compile(cfg.get("skip_titles") or r"(?!x)x")
    by_uuid = {row["identifier"]: row for row in latest.values()}
    url_tpl = cfg.get("item_url", "")

    def link(row):
        kind = "SO" if row.get("type") == "StructuralObject" else "IO"
        return url_tpl.format(kind=kind, uuid=row["identifier"]) if url_tpl else ""

    def parent(row):
        seen = set()
        p = by_uuid.get(row.get("relation", ""))
        while p is not None and skip.search(p.get("title", "")) and p["identifier"] not in seen:
            seen.add(p["identifier"])
            p = by_uuid.get(p.get("relation", ""))
        return p

    for row in sorted(latest.values(), key=lambda x: x["oai_identifier"]):
        is_folder = row.get("type") == "StructuralObject"
        if is_folder and skip.search(row.get("title", "")):
            continue
        r = new_record(source, row["identifier"])
        r["key"] = row.get("title", "")
        r["datestamp"] = row.get("datestamp", "")
        r["record_type"] = "folder" if is_folder else "asset"
        r["collection"] = is_folder
        r["title"] = [t for t in split(row.get("title"))]
        r["abstract"] = split(row.get("description"))
        if link(row):
            r["url"].append(link(row))
        if is_folder:
            r["type_of_resource"] = ["mixed material"]
        else:
            ext = (row.get("title", "").rsplit(".", 1)[-1].lower() if "." in row.get("title", "") else "")
            if ext in EXT_TO_TYPE:
                r["type_of_resource"] = [EXT_TO_TYPE[ext]]
        p = parent(row)
        if p is not None:
            r["host_title"].append(p.get("title", ""))
            r["host_id"].append(p["identifier"])
            if link(p):
                r["host_url"].append(link(p))
        for c in split(row.get("creator")):
            add_name(r, c, "creator", "cre")
        for s in split(row.get("subject")):
            add_subject(r, s)
        # dc:date in Preservica's feed is a system (ingest/modified) date, not a creation date,
        # so it is not mapped. Rights, if any, are.
        for t in split(row.get("rights")):
            add_rights(r, t)
        r["repository"] = [cfg["repository"]]
        r["genres"] += apply_genre_rules(source, {"title": r["title"]})
        yield r


PERSON_ROLE_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)\s*$")


def map_museumplus(rows, cfg, source):
    """MuseumPlus API object records (zetcom standard field names)."""
    url_tpl = cfg.get("item_url", "")
    for row in rows:
        mid = dedupe(split(row.get("__id")))
        mid = mid[0] if mid else ""
        objnum = dedupe(split(row.get("ObjObjectNumberTxt")))
        r = new_record(source, objnum[0] if objnum else mid)
        if objnum and mid:
            r["other_id"].append(mid)
        r["key"] = objnum[0] if objnum else f"museumplus-{mid}"
        m = re.match(r"(\d\d)/(\d\d)/(\d{4})\s+(\d\d:\d\d)", row.get("__lastModified", ""))
        r["datestamp"] = f"{m.group(3)}-{m.group(1)}-{m.group(2)}T{m.group(4)}" if m else row.get("__lastModified", "")
        category = split(row.get("ObjCategoryVoc"))
        r["record_type"] = category[0] if category else ""
        r["abstract"] = dedupe(split(row.get("ObjBriefDescriptionClb")))
        titles = dedupe(clean_title(t) for t in split(row.get("ObjObjectTitleGrp.TitleTxt")))
        if not titles:
            # Devised title: first sentence of the description, or the category and ID.
            desc = r["abstract"][0] if r["abstract"] else ""
            first = re.split(r"(?<=[a-z0-9)])\.\s", desc, maxsplit=1)[0].strip().rstrip(".")
            if len(first) > 90:
                first = first[:90].rsplit(" ", 1)[0] + "..."
            titles = [f"[{first}]" if first else f"[{category[0] if category else 'Object'} {mid}]"]
            r["title_supplied"] = True
        r["title"] = titles

        for ref in split(row.get("ObjPerAssociationRef")):
            name, role, dates = ref, "", ""
            m = PERSON_ROLE_RE.match(name)
            if m and not re.search(r"\d", m.group(2)) and len(m.group(2).split()) <= 3:
                name, role = m.group(1), m.group(2)
            m = PERSON_ROLE_RE.match(name)
            if m and re.search(r"\d|born|died|b\.|d\.", m.group(2), re.I):
                name, dates = m.group(1), m.group(2)
            add_name(r, name, role.lower(), "", None, dates)

        for d in split(row.get("ObjDateGrp.DateTxt")):
            add_date(r, d)
        r["form"] = [x for x in split(row.get("ObjMaterialTechniqueGrp.ExportClb"))]
        r["extent"] = [x for x in split(row.get("ObjDimAllGrp.PreviewENVrt"))
                       if x != "PreviewENVrt"]           # harvester placeholder, not a value
        for c in split(row.get("ObjCreditLineVoc")):
            r["note"].append(c)
        r["genres"] += [(c, "") for c in split(row.get("ObjClassificationVoc"))]
        r["genres"] += apply_genre_rules(source, {"ObjCategoryVoc": category})
        r["type_of_resource"] = [cfg.get("type_of_resource", "three dimensional object")]
        r["host_title"] = category
        if url_tpl and mid:
            r["url"].append(url_tpl.format(id=mid))
        r["repository"] = [cfg["repository"]]
        yield r


def map_dublin_core(rows, cfg, source):
    """Generic oai_dc (used for Archive-It, and for any new OAI source)."""
    for row in rows:
        r = new_record(source, row["oai_identifier"])
        r["key"] = row["oai_identifier"].split(":")[-1]
        r["datestamp"] = row.get("datestamp", "")
        r["collection"] = bool(cfg.get("collection"))
        r["title"] = [clean_title(t) for t in split(row.get("title"))]
        r["abstract"] = split(row.get("description"))
        for c in split(row.get("creator")):
            add_name(r, c, "creator", "cre")
        for c in split(row.get("contributor")):
            add_name(r, c, "contributor", "ctb")
        for s in split(row.get("subject")):
            for h in s.split(";"):
                add_subject(r, h)
        for c in split(row.get("coverage")):
            add_subject(r, c)
        for d in split(row.get("date")):
            add_date(r, d)
        types = [type_from_mime(m) for m in split(row.get("format"))]
        r["type_of_resource"] = dedupe([t for t in types if t]) or (
            [cfg["type_of_resource"]] if cfg.get("type_of_resource") else [])
        r["genres"] += [(t, "") for t in split(row.get("type"))]
        r["language"] = normalize_languages(split(row.get("language")))
        for i in split(row.get("identifier")):
            if is_url(i):
                (r["url"] if not r["url"] else r["related_url"]).append(i)
            else:
                r["other_id"].append(i)
        r["host_title"] = split(row.get("source"))
        r["note"] += split(row.get("relation"))
        for t in split(row.get("rights")):
            add_rights(r, t)
        if cfg.get("digital_origin"):
            r["digital_origin"] = [cfg["digital_origin"]]
        r["repository"] = [cfg["repository"]] if cfg.get("repository") else split(row.get("publisher"))
        r["genres"] += apply_genre_rules(source, {k: split(v) for k, v in row.items()})
        yield r


MAPPERS = {
    "digital_commons": map_digital_commons,
    "archivesspace": map_archivesspace,
    "preservica": map_preservica,
    "museumplus": map_museumplus,
    "dublin_core": map_dublin_core,
}


# ==========================================================================
# Output 1: the MODS template CSV
# ==========================================================================
def subjects_of(r, kind):
    """Full headings whose main term is of this kind; temporal also takes chronological subdivisions."""
    out = []
    for comps in r["subjects"]:
        if comps[0][0] == kind:
            out.append("--".join(v for _, v in comps))
        elif kind == "temporal":
            out += [v for k, v in comps[1:] if k == "temporal"]
    return dedupe(out)


def aligned(r, field):
    vals = [n[field] for n in r["names"]]
    return SEP.join(vals) if any(vals) else ""


def j(values):
    return SEP.join(dedupe(v for v in values if v))


COLUMN_VALUES = {
    "identifier[@type='ark']": lambda r: j(r["ark"]),
    "location/ns:physicalLocation/ns:url": lambda r: j(r["url"]),
    "identifier[@type='local']": lambda r: j(r["local_id"]),
    "abstract": lambda r: j(r["abstract"]),
    "titleInfo/ns:title": lambda r: j(r["title"]),
    "name/ns:namePart": lambda r: SEP.join(n["part"] for n in r["names"]),
    "name/ns:nameIdentifier": lambda r: aligned(r, "identifier"),
    "name/ns:role/ns:roleTerm": lambda r: aligned(r, "role"),
    "typeOfResource": lambda r: j(r["type_of_resource"]),
    "genre": lambda r: j(g for g, _ in r["genres"]),
    "subject/ns:topic": lambda r: j(subjects_of(r, "topic")),
    "subject/ns:name": lambda r: j(subjects_of(r, "name")),
    "subject/ns:temporal": lambda r: j(subjects_of(r, "temporal")),
    "subject/ns:geographic": lambda r: j(subjects_of(r, "geographic")),
    "subject/ns:coordinates": lambda r: j(r["coordinates"]),
    "originInfo/ns:dateCreated": lambda r: j(r["date_created"]),
    "originInfo/ns:displayDate": lambda r: j(r["display_date"]),
    "language/ns:languageTerm": lambda r: j(r["language"]),
    "location/ns:shelfLocator": lambda r: j(r["shelf"]),
    "relatedItem[@type='host']/ns:titleInfo/ns:title": lambda r: j(r["host_title"]),
    "relatedItem/ns:location/ns:url": lambda r: j(r["host_url"]),
    "physicalDescription/ns:extent": lambda r: j(r["extent"]),
    "physicalDescription/ns:digitalOrigin": lambda r: j(r["digital_origin"]),
    "physicalDescription/ns:form": lambda r: j(r["form"]),
    "note": lambda r: j(r["note"]),
    "location/ns:physicalLocation[@type='original']": lambda r: j(r["repository"]),
    "accessCondition[@type='access']": lambda r: j(r["access"]),
    "accessCondition[@type='rights_statement']": lambda r: j(r["rights_statement"]),
    "accessCondition[@type='cc_license']": lambda r: j(r["license"]),
    "accessCondition[@type='license_url']": lambda r: j(r["license_url"]),
    "accessCondition[@type='use']": lambda r: j(r["use"]),
}


def column_getter(xpath):
    if xpath == "PRESERVICA_CSV_TEMPLATE":
        return lambda r: r.get("key", "")
    tail = xpath.split("//ns:mods/ns:", 1)[-1] if "//ns:mods/" in xpath else None
    return COLUMN_VALUES.get(tail)


def csv_row(r, getters):
    return [(g(r) if g else "").replace("\r", " ").replace("\n", " ") for g in getters]


# ==========================================================================
# Output 2: MODS XML
# ==========================================================================
def q(tag):
    return f"{{{MODS_NS}}}{tag}"


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, q(tag), {k.replace("_", ":") if k.startswith("xlink") else k: v
                                        for k, v in attrs.items() if v})
    if text is not None:
        el.text = text
    return el


def to_mods(r, record_content_source):
    m = ET.Element(q("mods"), {"version": "3.8"})
    for t in r["title"][:1]:
        ti = sub(m, "titleInfo", supplied="yes" if r["title_supplied"] else None)
        sub(ti, "title", t)
    for t in r["title"][1:]:
        sub(sub(m, "titleInfo", type="alternative"), "title", t)

    for n in r["names"]:
        ne = sub(m, "name", type=n["type"] or None)
        sub(ne, "namePart", n["part"])
        if n["dates"]:
            sub(ne, "namePart", n["dates"], type="date")
        if n["identifier"]:
            sub(ne, "nameIdentifier", n["identifier"])
        if n["role"]:
            role = sub(ne, "role")
            if n["role_code"]:
                sub(role, "roleTerm", n["role_code"], type="code", authority="marcrelator")
            sub(role, "roleTerm", n["role"], type="text",
                authority="marcrelator" if n["role_code"] else None)

    for t in r["type_of_resource"][:1]:
        sub(m, "typeOfResource", t, collection="yes" if r["collection"] else None)
    for t in r["type_of_resource"][1:]:
        sub(m, "typeOfResource", t)
    if r["collection"] and not r["type_of_resource"]:
        sub(m, "typeOfResource", "mixed material", collection="yes")
    for g, auth in r["genres"]:
        sub(m, "genre", g, authority=auth or None)

    if r["date_created"] or r["display_date"]:
        oi = sub(m, "originInfo")
        for i, d in enumerate(r["date_created"]):
            sub(oi, "dateCreated", d, encoding="edtf", keyDate="yes" if i == 0 else None)
        for d in r["display_date"]:
            sub(oi, "displayDate", d)

    for code in r["language"]:
        sub(sub(m, "language"), "languageTerm", code, type="code", authority="iso639-2b")

    pd_items = [("form", r["form"]), ("internetMediaType", dedupe(r["media_type"])),
                ("extent", r["extent"]), ("digitalOrigin", r["digital_origin"])]
    if any(v for _, v in pd_items):
        pd = sub(m, "physicalDescription")
        for tag, vals in pd_items:
            for v in vals:
                sub(pd, tag, v)

    for a in r["abstract"]:
        sub(m, "abstract", a)
    for n in r["note"]:
        sub(m, "note", n)

    for comps in r["subjects"]:
        s = sub(m, "subject")
        for kind, val in comps:
            if kind == "name":
                sub(sub(s, "name"), "namePart", val)
            else:
                sub(s, kind, val)
    for c in r["coordinates"]:
        sub(sub(sub(m, "subject"), "cartographics"), "coordinates", c)

    if r["host_title"] or r["host_url"] or r["host_id"]:
        ri = sub(m, "relatedItem", type="host")
        for t in r["host_title"]:
            sub(sub(ri, "titleInfo"), "title", t)
        for i in r["host_id"]:
            sub(ri, "identifier", i, type="local")
        for u in r["host_url"]:
            sub(sub(ri, "location"), "url", u)
    for u in r["related_url"]:
        sub(sub(sub(m, "relatedItem"), "location"), "url", u)

    for v in r["local_id"]:
        sub(m, "identifier", v, type="local")
    for v in r["other_id"]:
        sub(m, "identifier", v, type="local")
    for v in r["ark"]:
        sub(m, "identifier", v, type="ark")
    for v in r["doi"]:
        sub(m, "identifier", v, type="doi")

    if r["repository"] or r["shelf"] or r["url"] or r["file_url"] or r["thumbnail"]:
        loc = sub(m, "location")
        for v in r["repository"]:
            sub(loc, "physicalLocation", v, type="original")
        for v in r["shelf"]:
            sub(loc, "shelfLocator", v)
        for i, u in enumerate(r["url"]):
            sub(loc, "url", u, usage="primary" if i == 0 else None, access="object in context")
        for u in r["file_url"]:
            sub(loc, "url", u, access="raw object")
        for u in r["thumbnail"]:
            sub(loc, "url", u, access="preview")

    for kind, field in [("access", "access"), ("rights_statement", "rights_statement"),
                        ("cc_license", "license"), ("license_url", "license_url"), ("use", "use")]:
        for v in r[field]:
            sub(m, "accessCondition", v, type=kind)

    ri = sub(m, "recordInfo")
    sub(ri, "recordContentSource", record_content_source)
    if r.get("datestamp"):
        sub(ri, "recordChangeDate", r["datestamp"])
    sub(ri, "recordIdentifier", r["local_id"][0], source=r["source"])
    sub(ri, "recordOrigin", f"Converted from the {r['source']} metadata harvest by scripts/mods_crosswalk.py "
                            f"(https://github.com/rodlibrary/Metadata) on {dt.date.today().isoformat()}.")
    return m


# ==========================================================================
# Output 3: Solr documents for Blacklight
# ==========================================================================
def solr_id(r):
    return f"{r['source']}-" + re.sub(r"[^A-Za-z0-9._-]+", "_", r["local_id"][0]).strip("_")


def to_solr(r, mods_xml=None, batch=""):
    years = sorted({y for s, e in r["years"] if s is not None for y in range(s, min(e, s + 300) + 1)})
    all_subjects = ["--".join(v for _, v in c) for c in r["subjects"]]
    doc = {
        "id": solr_id(r),
        "source_ssi": r["source"],
        "record_type_ssi": r.get("record_type") or None,
        "local_id_ssim": r["local_id"] + r["other_id"],
        "title_tsim": r["title"],
        "title_ssm": r["title"][:1],
        "title_si": (r["title"][0].lower().lstrip("[\"'(").strip() if r["title"] else None),
        "author_tsim": [n["part"] for n in r["names"]],
        "author_ssim": dedupe(n["part"] for n in r["names"]),
        "author_display_ssm": [n["part"] + (f" ({n['role']})" if n["role"] else "") for n in r["names"]],
        "abstract_tesim": r["abstract"],
        "note_tesim": r["note"],
        "format_ssim": r["type_of_resource"],
        "genre_ssim": dedupe(g for g, _ in r["genres"]),
        "subject_tsim": all_subjects,
        "subject_ssim": dedupe(all_subjects),
        "subject_topic_ssim": dedupe(v for c in r["subjects"] for k, v in c if k == "topic"),
        "subject_geo_ssim": dedupe(v for c in r["subjects"] for k, v in c if k == "geographic"),
        "subject_era_ssim": dedupe(v for c in r["subjects"] for k, v in c if k == "temporal"),
        "subject_name_ssim": dedupe(v for c in r["subjects"] for k, v in c if k == "name"),
        "language_ssim": [LANG_LABELS.get(c, c) for c in r["language"]],
        "date_display_ssm": r["display_date"],
        "date_created_ssm": r["date_created"],
        "pub_date_si": str(years[0]) if years else None,
        "date_isim": years,
        "collection_ssim": r["host_title"],
        "collection_url_ssm": r["host_url"],
        "repository_ssim": r["repository"],
        "call_number_ssim": r["shelf"],
        "extent_ssm": r["extent"],
        "url_ssm": r["url"],
        "file_url_ssm": r["file_url"],
        "thumbnail_url_ss": r["thumbnail"][0] if r["thumbnail"] else None,
        "access_ssm": r["access"],
        "rights_ssm": r["rights_statement"] + r["use"],
        "license_ssm": r["license"],
        "license_url_ssm": r["license_url"],
        "batch_ssi": batch or None,
    }
    if mods_xml is not None:
        doc["mods_xml_ss"] = mods_xml
    return {k: v for k, v in doc.items() if v not in (None, [], "")}


def post_to_solr(url, docs_path, source, batch, batch_size=1000):
    import requests
    s = requests.Session()
    upd = url.rstrip("/") + "/update"
    with open(docs_path, encoding="utf-8") as f:
        docs = json.load(f)
    for i in range(0, len(docs), batch_size):
        resp = s.post(upd, json=docs[i:i + batch_size], params={"wt": "json"}, timeout=300)
        resp.raise_for_status()
    # Remove this source's records that were not in this run (deleted or withdrawn at the source).
    resp = s.post(upd, json={"delete": {"query": f'source_ssi:"{source}" AND -batch_ssi:"{batch}"'}},
                  params={"commit": "true", "wt": "json"}, timeout=300)
    resp.raise_for_status()
    return len(docs)


# ==========================================================================
# Validation (needs internet access to loc.gov)
# ==========================================================================
_SCHEMA = None


def load_schema(url):
    global _SCHEMA
    if _SCHEMA is None:
        import requests
        from lxml import etree

        class Fetch(etree.Resolver):     # libxml2 can't fetch https, so fetch the XSD and its imports here
            def resolve(self, system_url, public_id, context):
                if system_url.startswith(("http://", "https://")):
                    resp = requests.get(system_url, timeout=60)
                    resp.raise_for_status()
                    return self.resolve_string(resp.content, context, base_url=resp.url)
                return None

        parser = etree.XMLParser()
        parser.resolvers.add(Fetch())
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        _SCHEMA = etree.XMLSchema(etree.fromstring(resp.content, parser, base_url=resp.url).getroottree())
    return _SCHEMA


def validate(elements, url, limit=20):
    from lxml import etree
    schema = load_schema(url)
    bad = []
    for el in elements:
        doc = etree.fromstring(ET.tostring(el))
        if not schema.validate(doc):
            bad.append((doc.findtext(f".//{{{MODS_NS}}}recordIdentifier"), str(schema.error_log.last_error)))
            if len(bad) >= limit:
                break
    return bad


# ==========================================================================
# Driver
# ==========================================================================
def read_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def summarize(source, records, getters, labels):
    filled = Counter()
    for r in records:
        for lab, g in zip(labels, getters):
            if g and g(r):
                filled[lab] += 1
    n = len(records) or 1
    lines = [f"### {source}: {len(records):,} MODS records", "", "| MODS field | Records with a value |",
             "|---|---|"]
    for lab in labels:
        lines.append(f"| {lab} | {filled[lab]:,} ({100 * filled[lab] / n:.0f}%) |")
    return "\n".join(lines)


def run_source(source, cfg, top, args, xpaths, labels):
    path = ROOT / cfg["input"]
    if not path.exists():
        print(f"[{source}] no snapshot at {cfg['input']}; skipped")
        return None
    mapper = MAPPERS[cfg["mapper"]]
    rows = read_rows(path)
    records = list(mapper(rows, cfg, source))
    records.sort(key=lambda r: r["local_id"][0])

    getters = [column_getter(x) for x in xpaths]
    unknown = [lab for lab, g in zip(labels, getters) if g is None]
    if unknown:
        print(f"[{source}] template columns with no mapping (left blank): {', '.join(unknown)}")

    out_dir = Path(args.out_dir) / source
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{source}_mods.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(xpaths)
        w.writerow(labels)
        for r in records:
            w.writerow(csv_row(r, getters))
    print(f"[{source}] {len(rows):,} snapshot rows -> {len(records):,} MODS records -> {csv_path}")

    batch = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    build = Path(args.build_dir) / source
    if args.xml or args.solr_json or args.validate:
        build.mkdir(parents=True, exist_ok=True)
    xml_out = solr_out = None
    if args.xml:
        xml_out = open(build / f"{source}_mods.xml", "w", encoding="utf-8")
        xml_out.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                      f'<modsCollection xmlns="{MODS_NS}" xmlns:xlink="{XLINK_NS}" xmlns:xsi="{XSI_NS}" '
                      f'xsi:schemaLocation="{MODS_NS} {top["schema_url"]}">\n')
    if args.solr_json:
        solr_out = open(build / f"{source}_solr.json", "w", encoding="utf-8")
        solr_out.write("[\n")
    to_validate, first = [], True
    for r in records:
        if not (xml_out or solr_out or args.validate):
            break
        el = to_mods(r, top["record_content_source"])
        xml_text = None
        if xml_out or args.solr_include_xml:
            ET.indent(el, space="  ")
            xml_text = ET.tostring(el, encoding="unicode")
        if xml_out:
            xml_out.write(re.sub(r'^<mods xmlns="[^"]+"', "<mods", xml_text) + "\n")
        if solr_out:
            doc = to_solr(r, xml_text if args.solr_include_xml else None, batch)
            solr_out.write(("" if first else ",\n") + json.dumps(doc, ensure_ascii=False))
            first = False
        if args.validate:
            to_validate.append(el)
    if xml_out:
        xml_out.write("</modsCollection>\n")
        xml_out.close()
        print(f"[{source}] MODS XML -> {build / f'{source}_mods.xml'}")
    if solr_out:
        solr_out.write("\n]\n")
        solr_out.close()
        print(f"[{source}] Solr JSON -> {build / f'{source}_solr.json'}")

    report = summarize(source, records, getters, labels)
    if args.validate:
        bad = validate(to_validate, top["schema_url"])
        msg = (f"all {len(to_validate):,} records valid against MODS 3.8" if not bad
               else f"{len(bad)}+ records INVALID; first: {bad[0]}")
        print(f"[{source}] validation: {msg}")
        report += f"\n\nSchema validation: {msg}"
        if bad:
            args.failed = True
    if args.solr_url and solr_out:
        n = post_to_solr(args.solr_url, build / f"{source}_solr.json", source, batch)
        print(f"[{source}] indexed {n:,} documents in Solr")
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", action="append", help="source name from crosswalk/config.json (repeatable; "
                                                     "default: all)")
    p.add_argument("--out-dir", default=str(ROOT / "mods"), help="where the template CSVs go (default mods/)")
    p.add_argument("--build-dir", default=str(ROOT / "build"), help="where XML and Solr JSON go (default build/)")
    p.add_argument("--xml", action="store_true", help="also write a MODS XML <modsCollection> per source")
    p.add_argument("--solr-json", action="store_true", help="also write Solr/Blacklight documents per source")
    p.add_argument("--solr-include-xml", action="store_true", help="store each record's MODS XML in mods_xml_ss")
    p.add_argument("--solr-url", default=os.environ.get("SOLR_URL", ""),
                   help="Solr core URL to index into (needs --solr-json); also read from SOLR_URL")
    p.add_argument("--validate", action="store_true", help="validate every record against the MODS 3.8 XSD")
    args = p.parse_args(argv)
    args.failed = False

    top = load_config()
    xpaths, labels = load_template()
    sources = args.source or list(top["sources"])
    reports = []
    for s in sources:
        if s not in top["sources"]:
            p.error(f"unknown source {s!r}; known: {', '.join(top['sources'])}")
        rep = run_source(s, top["sources"][s], top, args, xpaths, labels)
        if rep:
            reports.append(rep)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and reports:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## MODS crosswalk\n\n" + "\n\n".join(reports) + "\n")
    return 1 if args.failed else 0


if __name__ == "__main__":
    sys.exit(main())
