#!/usr/bin/env python3
"""
Harvest metadata from an OAI-PMH endpoint and write a dated CSV snapshot.
Used for UNI ScholarWorks (Digital Commons), ArchivesSpace, Preservica and Archive-It.

Usage:
    python scripts/oai_harvest.py --base-url URL --name NAME [options]

Examples:
    # ScholarWorks (Digital Commons)
    python scripts/oai_harvest.py --name scholarworks \\
        --base-url https://scholarworks.uni.edu/do/oai/ --out-dir scholarworks/snapshots
    # ArchivesSpace
    python scripts/oai_harvest.py --name archivesspace \\
        --base-url https://aspace.library.uni.edu:8082 --out-dir archivesspace/snapshots
    # Preservica (needs a Preservica login; see preservica/README.md)
    python scripts/oai_harvest.py --name preservica --auth preservica \\
        --base-url https://<server>.preservica.com/OAI-PMH --out-dir preservica/snapshots

    # ArchivesSpace in MODS (the weekly workflow does this)
    python scripts/oai_harvest.py --name archivesspace --prefix oai_mods \
        --base-url https://aspace.library.uni.edu:8082 --out-dir archivesspace/snapshots

Options:
    --prefix dcq / oai_dcterms ...   another metadata format (default oai_dc). A comma-separated
                                     list uses the first one the server offers (e.g. MODS,mods)
    --merge-prefix oai_dc            also harvest this format: its fields are added as
                                     "oai_dc:<field>" columns, and records only it has are kept
    --merge-fields "^(date|identifier)$"   only merge these fields (regex; default all)
    --max-records N                  stop after N records (for testing)
    --set SETSPEC                    one OAI set only
    --list-formats / --list-sets     print what the endpoint offers and exit
    --keep 4                         keep only the 4 newest dated snapshots
    --auth preservica                log in first, using PRESERVICA_USERNAME,
                                     PRESERVICA_PASSWORD and PRESERVICA_TENANT

Output (in --out-dir):
    <name>_<prefix>_<YYYY-MM-DD>.csv   dated snapshot
    latest_<prefix>.csv                copy of the newest snapshot
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, OrderedDict
from pathlib import Path

import requests

USER_AGENT = (
    "RodLibrary-Metadata-Harvester/1.0 "
    "(+https://github.com/rodlibrary/Metadata)"
)
OAI_NS = "http://www.openarchives.org/OAI/2.0/"
NS = {"oai": OAI_NS}
EMPTY_REASON = ""  # set when the server reports noRecordsMatch
SEP = " || "  # joins repeated values (e.g. multiple creators) in one cell

# Columns that always come first, in this order. Any other metadata element
# found in the harvest is appended after these, alphabetically.
BASE_COLUMNS = ["oai_identifier", "datestamp", "setSpec", "deleted"]
# Added after BASE_COLUMNS only when some record has it (ArchivesSpace does;
# Digital Commons does not), e.g. "resource" or "archival_object".
RECORD_TYPE_RE = re.compile(
    r"/(resources|archival_objects|digital_objects|accessions|agents|subjects)/")
DC_ORDER = [
    "title", "creator", "contributor", "date", "description", "subject",
    "type", "format", "identifier", "source", "publisher", "language",
    "relation", "coverage", "rights",
]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


class PreservicaAuth:
    """Logs in to Preservica's Access Token API and keeps a fresh token on the session.

    Preservica's OAI-PMH Data Provider needs a 'Preservica-Access-Token' header.
    Tokens last 15 minutes, so a new one is fetched every 10 minutes and after any 401.
    Credentials come from PRESERVICA_USERNAME, PRESERVICA_PASSWORD and (for hosted
    multi-tenant servers) PRESERVICA_TENANT environment variables.
    """
    HEADER = "Preservica-Access-Token"
    MAX_AGE = 600  # seconds

    def __init__(self, login_url):
        self.login_url = login_url
        self.username = os.environ.get("PRESERVICA_USERNAME", "")
        self.password = os.environ.get("PRESERVICA_PASSWORD", "")
        self.tenant = os.environ.get("PRESERVICA_TENANT", "")
        if not (self.username and self.password):
            raise SystemExit("PRESERVICA_USERNAME and PRESERVICA_PASSWORD must be set "
                             "to harvest from Preservica.")
        self.obtained = None  # monotonic time of last login

    def login(self, session):
        data = {"username": self.username, "password": self.password}
        if self.tenant:
            data["tenant"] = self.tenant
        r = session.post(self.login_url, data=data, timeout=60,
                         headers={"Accept": "application/json"})
        if r.status_code != 200:
            try:
                reason = r.json().get("message", "")
            except ValueError:
                reason = r.text[:200]
            raise SystemExit(f"Preservica login failed (HTTP {r.status_code}): {reason}")
        token = r.json().get("token")
        if not token:
            raise SystemExit("Preservica login returned no token.")
        session.headers[self.HEADER] = token
        self.obtained = time.monotonic()
        print("  logged in to Preservica", file=sys.stderr)

    def ensure(self, session, force=False):
        if force or self.obtained is None or time.monotonic() - self.obtained > self.MAX_AGE:
            self.login(session)


def default_login_url(base_url):
    """https://host/anything -> https://host/api/accesstoken/login"""
    from urllib.parse import urlsplit
    u = urlsplit(base_url)
    return f"{u.scheme}://{u.netloc}/api/accesstoken/login"


# Some servers (e.g. archive-it.org) answer a burst of requests with an HTML
# "Rate limit reached" page, sometimes with HTTP 200. Wait this long (seconds)
# before each retry; about 33 minutes in total before giving up.
RATE_LIMIT_WAITS = [60, 120, 300, 600, 900]
RATE_LIMIT_RE = re.compile(rb"rate.?limit|too many requests", re.I)


def looks_rate_limited(r) -> bool:
    if r.status_code == 429:
        return True
    head = r.content[:2000]
    return (r.status_code in (200, 403, 503) and b"<OAI-PMH" not in head
            and RATE_LIMIT_RE.search(head) is not None)


def oai_request(session, base_url, params, max_retries=6, timeout=120):
    """GET an OAI-PMH response, retrying on rate limits, 5xx and network errors."""
    delay = 10
    auth = getattr(session, "oai_auth", None)
    relogged = False
    rate_waits = list(RATE_LIMIT_WAITS)
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        if auth:
            auth.ensure(session)
        try:
            r = session.get(base_url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            print(f"  network error ({exc}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue

        if looks_rate_limited(r):
            if not rate_waits:
                raise SystemExit(f"{base_url} is still rate-limiting after waiting "
                                 f"{sum(RATE_LIMIT_WAITS) // 60} minutes; try again later.")
            wait = rate_waits.pop(0)
            ra = r.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = max(wait, int(ra))
            print(f"  rate limit reached; waiting {wait}s before retrying", file=sys.stderr)
            time.sleep(wait)
            attempt -= 1  # rate-limit waits don't use up the normal retries
            continue

        if r.status_code == 200:
            try:
                return ET.fromstring(r.content)
            except ET.ParseError:
                raise SystemExit(f"Reply from {base_url} is not XML: "
                                 f"{' '.join(r.text.split())[:300]}")

        if r.status_code in (401, 403) and auth and not relogged:
            print(f"  HTTP {r.status_code}; logging in again", file=sys.stderr)
            auth.ensure(session, force=True)
            relogged = True
            continue

        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            wait = r.headers.get("Retry-After")
            wait = int(wait) if wait and wait.isdigit() else delay
            print(f"  HTTP {r.status_code}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            delay = min(delay * 2, 300)
            continue

        raise SystemExit(f"OAI-PMH request to {base_url} failed: HTTP {r.status_code} "
                         f"{' '.join(r.text.split())[:300]}")
    raise RuntimeError("unreachable")


def check_oai_error(root):
    """Return the OAI error code (or None). Raises on anything but noRecordsMatch."""
    err = root.find("oai:error", NS)
    if err is None:
        return None
    code = err.get("code", "")
    if code == "noRecordsMatch":
        global EMPTY_REASON
        EMPTY_REASON = f"the server answered noRecordsMatch: {(err.text or '').strip() or 'no records'}"
        return code
    raise RuntimeError(f"OAI-PMH error {code}: {(err.text or '').strip()}")


def paged(session, base_url, verb, params, delay=1.0):
    """Yield each page's <verb> element, following resumptionTokens."""
    req = {"verb": verb, **params}
    page = 0
    while True:
        page += 1
        root = oai_request(session, base_url, req)
        if check_oai_error(root):
            return
        container = root.find(f"oai:{verb}", NS)
        if container is None:
            snippet = " ".join(ET.tostring(root, encoding="unicode").split())[:400]
            raise SystemExit(f"Unexpected reply from {base_url} (no <{verb}> element; "
                             f"root is <{local_name(root.tag)}>): {snippet}")
        yield container

        token_el = container.find("oai:resumptionToken", NS)
        token = (token_el.text or "").strip() if token_el is not None else ""
        if not token:
            return
        if token_el is not None and token_el.get("completeListSize"):
            print(f"  page {page} done (list size {token_el.get('completeListSize')})",
                  file=sys.stderr)
        req = {"verb": verb, "resumptionToken": token}
        time.sleep(delay)  # be polite to the server


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


MODS_NS = "http://www.loc.gov/mods/v3"
# MODS attributes that distinguish kinds of the same element; they become part of the
# column name, e.g. identifier[@type='local'], dateCreated[@point='start'].
MODS_QUALIFIERS = ("type", "point", "eventType", "access")
# MODS attributes kept as their own columns, e.g. name/@type, subject/@valueURI.
MODS_ATTR_COLUMNS = ("valueURI", "href", "displayLabel", "collection", "authority", "type")
# Where qualifiers would split one kind of element apart, keep them as attribute columns.
MODS_ATTR_NOT_QUALIFIER = {"name": {"type"}, "subject": set(), "typeOfResource": set()}
MODS_ORDER = ["titleInfo", "name", "typeOfResource", "genre", "originInfo", "language",
              "physicalDescription", "abstract", "tableOfContents", "targetAudience", "note",
              "subject", "classification", "relatedItem", "identifier", "location",
              "accessCondition", "part", "extension", "recordInfo"]
MODS_SUBJECT_PARTS = {"topic", "geographic", "temporal", "genre", "occupation", "titleInfo",
                      "name", "hierarchicalGeographic", "cartographics", "geographicCode"}
INSTANCE_SEP = " ; "   # several values of one field inside one element


def mods_step(el):
    name = local_name(el.tag)
    skip = MODS_ATTR_NOT_QUALIFIER.get(name, ())
    quals = [f"[@{a}='{el.get(a)}']" for a in MODS_QUALIFIERS if el.get(a) and a not in skip]
    return name + "".join(quals)


def mods_attrs(el, top=False):
    """Attribute columns of one element: {"@valueURI": "...", ...}. @authority is kept only on
    top-level elements (subject, genre, name), where it says which vocabulary a term is from."""
    name = local_name(el.tag)
    out = {}
    for key, val in el.attrib.items():
        a = local_name(key)
        if a == "authority" and not top:
            continue
        if a in MODS_ATTR_COLUMNS and val.strip() and not (
                a in MODS_QUALIFIERS and a not in MODS_ATTR_NOT_QUALIFIER.get(name, ())):
            out[f"@{a}"] = " ".join(val.split())
    return out


def flatten_mods(mods_el) -> OrderedDict:
    """Flatten a <mods> record into path-named columns, e.g. "name/namePart",
    "name/role/roleTerm[@type='text']", "relatedItem[@type='host']/titleInfo/title".

    Each repeated top-level element (every <name>, every <subject>, ...) adds exactly one
    value to each of its group's columns, empty if it has none, so the n-th value of
    name/namePart and of name/role/roleTerm belong to the same name. Several values inside
    one element are joined with " ; " (subject parts with "--"). Each <subject> also gets a
    "subject" column holding the whole heading in order (Topic--Place--Period)."""
    groups: OrderedDict[str, list[OrderedDict]] = OrderedDict()
    for top in mods_el:
        if not isinstance(top.tag, str):
            continue
        key = mods_step(top)
        inst: OrderedDict[str, list[str]] = OrderedDict()
        heading = []
        for col, val in mods_attrs(top, top=True).items():
            inst.setdefault(f"{key}/{col}", []).append(val)
        stack = [(top, key)]
        while stack:
            el, path = stack.pop()
            kids = [k for k in el if isinstance(k.tag, str)]
            for k in reversed(kids):
                kpath = f"{path}/{mods_step(k)}"
                for col, val in mods_attrs(k).items():
                    inst.setdefault(f"{kpath}/{col}", []).append(val)
                stack.append((k, kpath))
            text = " ".join((el.text or "").split())
            if text and (not kids or el is top):
                inst.setdefault(path, []).append(text)
        if local_name(top.tag) == "subject":
            # the whole heading in document order; a name's parts (name, dates) stay together
            for part in top:
                if not isinstance(part.tag, str):
                    continue
                if local_name(part.tag) in ("name", "titleInfo"):
                    texts = [" ".join(t.split()) for t in part.itertext() if t.strip()]
                    if texts:
                        heading.append(", ".join(texts))
                else:
                    heading += [" ".join(t.split()) for t in part.itertext() if t.strip()]
            if heading:
                inst["subject"] = ["--".join(heading)]
        if inst:
            groups.setdefault(key, []).append(inst)

    row: OrderedDict[str, str] = OrderedDict()
    for key, insts in groups.items():
        cols = list(OrderedDict.fromkeys(c for i in insts for c in i))
        joiner = "--" if key == "subject" else INSTANCE_SEP
        for c in cols:
            vals = [joiner.join(i.get(c, [])) for i in insts]
            row[c] = SEP.join(vals) if len(vals) > 1 else vals[0]
    return row


def parse_record(rec) -> OrderedDict:
    """Flatten one OAI <record> into {column: value}."""
    row: OrderedDict[str, list[str]] = OrderedDict()
    header = rec.find("oai:header", NS)
    row["oai_identifier"] = [header.findtext("oai:identifier", "", NS).strip()]
    row["datestamp"] = [header.findtext("oai:datestamp", "", NS).strip()]
    row["setSpec"] = [s.text.strip() for s in header.findall("oai:setSpec", NS) if s.text]
    row["deleted"] = ["true" if header.get("status") == "deleted" else "false"]
    m = RECORD_TYPE_RE.search(row["oai_identifier"][0])
    if m:
        # "archival_objects" -> "archival_object", "accessions" -> "accession"
        row["record_type"] = [m.group(1)[:-1]]

    md = rec.find("oai:metadata", NS)
    mods = None
    if md is not None and len(md):
        mods = md[0] if md[0].tag == f"{{{MODS_NS}}}mods" else md[0].find(f".//{{{MODS_NS}}}mods")
    if mods is not None:
        out = OrderedDict((k, SEP.join(v)) for k, v in row.items())
        out.update(flatten_mods(mods))
        return out
    if md is not None and len(md):
        wrapper = md[0]  # e.g. <oai_dc:dc> or <dcq:qualifieddc>
        for el in wrapper.iter():
            if el is wrapper or len(el):  # skip the wrapper and container elements
                continue
            text = " ".join((el.text or "").split())
            if not text:
                continue
            row.setdefault(local_name(el.tag), []).append(text)

    return OrderedDict((k, SEP.join(v)) for k, v in row.items())


# --------------------------------------------------------------------------
# Main tasks
# --------------------------------------------------------------------------
def list_formats(session, base_url):
    root = oai_request(session, base_url, {"verb": "ListMetadataFormats"})
    check_oai_error(root)
    for f in root.iter(f"{{{OAI_NS}}}metadataFormat"):
        print(f"{f.findtext('oai:metadataPrefix', '', NS)}\t"
              f"{f.findtext('oai:metadataNamespace', '', NS)}")


def list_sets(session, base_url):
    for container in paged(session, base_url, "ListSets", {}):
        for s in container.findall("oai:set", NS):
            print(f"{s.findtext('oai:setSpec', '', NS)}\t{s.findtext('oai:setName', '', NS)}")


def harvest(session, base_url, prefix, set_spec=None, from_date=None, delay=1.0, max_records=0):
    params = {"metadataPrefix": prefix}
    if set_spec:
        params["set"] = set_spec
    if from_date:
        params["from"] = from_date
    rows = []
    for container in paged(session, base_url, "ListRecords", params, delay=delay):
        for rec in container.findall("oai:record", NS):
            rows.append(parse_record(rec))
        print(f"  {len(rows)} records so far", file=sys.stderr)
        if max_records and len(rows) >= max_records:
            print(f"  stopping at --max-records {max_records}", file=sys.stderr)
            return rows[:max_records]
    return rows


def resolve_prefix(session, base_url, wanted):
    """'MODS,mods' -> the first of these the server offers (case-insensitive)."""
    options = [p.strip() for p in wanted.split(",") if p.strip()]
    if len(options) == 1:
        return options[0]
    offered = available_formats(session, base_url)
    for o in options:
        for f in offered:
            if f.lower() == o.lower():
                return f
    raise SystemExit(f"None of {options} is offered by {base_url} "
                     f"(it offers: {', '.join(offered) or 'nothing listed'}).")


def merge_format(session, args, rows, prefix):
    """Harvest a second format and merge it in: every field becomes "<prefix>:<field>",
    and records missing from the main harvest are added (Preservica only serves MODS for
    records that have MODS, so oai_dc fills in the rest)."""
    by_id = {r["oai_identifier"]: r for r in rows}
    extra = harvest(session, args.base_url, prefix, args.set_spec, args.from_date, args.delay,
                    args.max_records)
    added = 0
    for x in extra:
        ident = x["oai_identifier"]
        keep = re.compile(args.merge_fields or ".", re.I)
        row = by_id.get(ident)
        # records only this format has keep all their fields; others only --merge-fields
        fields = {f"{prefix}:{k}": v for k, v in x.items()
                  if k not in ("oai_identifier", "datestamp", "setSpec", "deleted", "record_type")
                  and (row is None or keep.search(k))}
        if row is None:
            row = OrderedDict((k, x[k]) for k in ("oai_identifier", "datestamp", "setSpec",
                                                  "deleted", "record_type") if k in x)
            row["metadata_format"] = prefix
            rows.append(row)
            by_id[ident] = row
            added += 1
        elif x.get("datestamp", "") > row.get("datestamp", ""):
            row["datestamp"] = x["datestamp"]
        row.update(fields)
    return len(extra), added


def parse_extra(rec) -> tuple[str, dict]:
    """Flatten one record of a richer format (e.g. Digital Commons document-export
    or qualified DC). <field name="X"><value>..</value></field> becomes column X;
    other leaf elements use their own name. Returns (oai_identifier, {name: values})."""
    header = rec.find("oai:header", NS)
    ident = header.findtext("oai:identifier", "", NS).strip()
    out: OrderedDict[str, list[str]] = OrderedDict()
    md = rec.find("oai:metadata", NS)
    if md is None or not len(md):
        return ident, out
    inside_field = set()
    for el in md[0].iter():
        if local_name(el.tag) == "field" and el.get("name"):
            vals = [" ".join(v.split()) for v in el.itertext() if v.strip()]
            if vals:
                out.setdefault(el.get("name"), []).extend(vals)
            inside_field.update(id(d) for d in el.iter())
            continue
        if id(el) in inside_field or len(el):
            continue
        text = " ".join((el.text or "").split())
        if text:
            out.setdefault(local_name(el.tag), []).append(text)
    return ident, out


def available_formats(session, base_url):
    try:
        root = oai_request(session, base_url, {"verb": "ListMetadataFormats"})
        check_oai_error(root)
    except (SystemExit, RuntimeError) as exc:
        print(f"  could not list metadata formats: {exc}", file=sys.stderr)
        return []
    return [f.findtext("oai:metadataPrefix", "", NS)
            for f in root.iter(f"{{{OAI_NS}}}metadataFormat")]


def add_extra_fields(session, args, rows):
    """Harvest a second, richer format and add the columns whose names match
    --extra-fields to the main rows (joined on the OAI identifier). Never fails
    the main harvest: problems become warnings."""
    def warn(msg):
        print(f"::warning title=Extra fields skipped::{msg}" if os.environ.get("GITHUB_ACTIONS")
              else f"WARNING: {msg}", flush=True)

    offered = available_formats(session, args.base_url)
    wanted = [p.strip() for p in args.extra_prefix.split(",") if p.strip()]
    prefix = next((p for p in wanted if p in offered), None)
    lines = [f"#### Extra fields ({args.extra_prefix})", "",
             f"Formats offered by the server: {', '.join(f'`{o}`' for o in offered) or 'unknown'}"]
    if not prefix:
        warn(f"none of {wanted} is offered by {args.base_url}; only {args.prefix} columns written.")
        write_step_summary("\n".join(lines))
        return []

    print(f"Harvesting extra fields from {prefix}", file=sys.stderr)
    pattern = re.compile(args.extra_fields, re.I)
    by_id = {r["oai_identifier"]: r for r in rows}
    seen, kept, matched = Counter(), [], 0
    params = {"metadataPrefix": prefix}
    if args.set_spec:
        params["set"] = args.set_spec
    if args.from_date:
        params["from"] = args.from_date
    taken = set(by_id[next(iter(by_id))].keys()) if by_id else set()
    rename = {}
    try:
        n = 0
        for container in paged(session, args.base_url, "ListRecords", params, delay=args.delay):
            for rec in container.findall("oai:record", NS):
                ident, fields = parse_extra(rec)
                n += 1
                for name in fields:
                    seen[name] += 1
                row = by_id.get(ident)
                if row is None:
                    continue
                hit = False
                for name, vals in fields.items():
                    if not pattern.search(name):
                        continue
                    col = rename.setdefault(name, name if name not in taken else f"{prefix}_{name}")
                    if col not in kept:
                        kept.append(col)
                    row[col] = SEP.join(dict.fromkeys(vals))
                    hit = True
                matched += hit
            print(f"  {n} {prefix} records so far", file=sys.stderr)
    except (SystemExit, RuntimeError) as exc:
        warn(f"stopped harvesting {prefix}: {exc}. Extra columns may be incomplete.")

    lines += [f"Used `{prefix}`. Columns added: {', '.join(f'`{k}`' for k in kept) or 'none'}"
              f" ({matched} records had at least one).", "",
              f"All field names in `{prefix}` (to adjust `--extra-fields`):", "",
              "| Field | Records |", "|---|---|"]
    lines += [f"| `{k}` | {v} |" for k, v in sorted(seen.items())]
    write_step_summary("\n".join(lines))
    print(f"  extra columns added: {kept or 'none'}", file=sys.stderr)
    return kept


def column_order(rows):
    seen = set()
    for r in rows:
        seen.update(r.keys())
    cols = list(BASE_COLUMNS)
    if "record_type" in seen:
        cols.append("record_type")
    if "metadata_format" in seen:
        cols.append("metadata_format")
    if not any("/" in c for c in seen):     # Dublin Core
        cols += [c for c in DC_ORDER if c in seen and c not in cols]

    order = [m.lower() for m in MODS_ORDER]   # ArchivesSpace writes <titleinfo>

    def mods_key(c):
        top = re.split(r"[/\[]", c, 1)[0].lower()
        return (order.index(top) if top in order else len(order), 0 if c.lower() == top else 1, c)
    mods_cols = [c for c in seen - set(cols) if re.split(r"[/\[:]", c, 1)[0].lower() in order
                 and ":" not in c.split("/", 1)[0]]
    cols += sorted(mods_cols, key=mods_key)
    cols += sorted(seen - set(cols))
    return cols


def write_csv(rows, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = column_order(rows)
    rows = sorted(rows, key=lambda r: r.get("oai_identifier", ""))  # stable diffs
    with path.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return cols


def prune(out_dir: Path, name: str, prefix: str, keep: int):
    if keep <= 0:
        return
    snaps = sorted(out_dir.glob(f"{name}_{prefix}_*.csv"))
    for old in snaps[:-keep]:
        print(f"  removing old snapshot {old.name}", file=sys.stderr)
        old.unlink()


def write_step_summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True, help="OAI-PMH endpoint")
    p.add_argument("--name", default="oai",
                   help="short name used in snapshot file names, e.g. archivesspace")
    p.add_argument("--prefix", default="oai_dc", help="metadataPrefix (oai_dc, dcq, ...)")
    p.add_argument("--set", dest="set_spec", help="limit to one OAI set")
    p.add_argument("--from", dest="from_date", help="only records changed since YYYY-MM-DD")
    p.add_argument("--out-dir", default="snapshots")
    p.add_argument("--keep", type=int, default=0,
                   help="keep only the N newest dated snapshots (0 = keep all)")
    p.add_argument("--delay", type=float, default=1.0, help="seconds between pages")
    p.add_argument("--include-deleted", action="store_true",
                   help="keep records the repository reports as deleted")
    p.add_argument("--list-sets", action="store_true")
    p.add_argument("--list-formats", action="store_true")
    p.add_argument("--merge-prefix",
                   help="also harvest this format (e.g. oai_dc): add its fields as <prefix>:<field> "
                        "columns and keep records that only it has")
    p.add_argument("--merge-fields",
                   help="regex: only merge these fields of --merge-prefix (default: all)")
    p.add_argument("--max-records", type=int, default=0, help="stop after N records (testing)")
    p.add_argument("--extra-prefix",
                   help="also harvest this richer format (comma-separated list: the first one "
                        "the server offers is used) and add matching fields as extra columns")
    p.add_argument("--extra-fields", default="embargo|department|note|comment",
                   help="regex of field names to take from --extra-prefix (case-insensitive)")
    p.add_argument("--auth", choices=["none", "preservica"], default="none",
                   help="log in before harvesting (preservica reads PRESERVICA_* env vars)")
    p.add_argument("--login-url",
                   help="Preservica login URL (default: <server>/api/accesstoken/login)")
    args = p.parse_args(argv)

    session = make_session()
    if args.auth == "preservica":
        session.oai_auth = PreservicaAuth(args.login_url or default_login_url(args.base_url))

    if args.list_formats:
        list_formats(session, args.base_url)
        return 0
    if args.list_sets:
        list_sets(session, args.base_url)
        return 0

    wanted = args.prefix
    try:
        args.prefix = resolve_prefix(session, args.base_url, wanted)
    except SystemExit as exc:
        if not args.merge_prefix:
            raise
        args.prefix = wanted.split(",")[0].strip()
        print(f"::warning title=No {args.prefix} records::{exc} Using {args.merge_prefix} only."
              if os.environ.get("GITHUB_ACTIONS") else f"WARNING: {exc}", flush=True)
        args.skip_main = True
    print(f"Harvesting {args.base_url} (prefix={args.prefix}"
          f"{', set=' + args.set_spec if args.set_spec else ''})", file=sys.stderr)
    rows = [] if getattr(args, "skip_main", False) else harvest(
        session, args.base_url, args.prefix, args.set_spec, args.from_date, args.delay,
        args.max_records)
    merge_note = ""
    if args.merge_prefix:
        n_main = len(rows)
        for r in rows:
            r["metadata_format"] = args.prefix
        print(f"Merging in {args.merge_prefix}", file=sys.stderr)
        n_extra, added = merge_format(session, args, rows, args.merge_prefix)
        merge_note = (f". {n_main} records in {args.prefix}; {n_extra} in {args.merge_prefix}, "
                      f"of which {added} had no {args.prefix} and were added from {args.merge_prefix}")
    extra_cols = add_extra_fields(session, args, rows) if args.extra_prefix and rows else []

    n_deleted = sum(r.get("deleted") == "true" for r in rows)
    if not args.include_deleted:
        rows = [r for r in rows if r.get("deleted") != "true"]

    if not rows:
        raise SystemExit(f"No records harvested from {args.base_url}"
                         f"{f' ({EMPTY_REASON})' if EMPTY_REASON else ''}; not writing a snapshot.")

    today = dt.date.today().isoformat()
    out_dir = Path(args.out_dir)
    label = args.prefix.lower()   # Preservica calls the format "MODS"
    snap = out_dir / f"{args.name}_{label}_{today}.csv"
    cols = write_csv(rows, snap)
    shutil.copyfile(snap, out_dir / f"latest_{label}.csv")
    prune(out_dir, args.name, label, args.keep)

    msg = (f"Wrote {len(rows)} records, {len(cols)} columns to {snap}"
           f" ({n_deleted} deleted records {'kept' if args.include_deleted else 'skipped'})"
           f"{merge_note}")
    types = {}
    for r in rows:
        if r.get("record_type"):
            types[r["record_type"]] = types.get(r["record_type"], 0) + 1
    if types:
        msg += ". By type: " + ", ".join(f"{k} {v}" for k, v in sorted(types.items()))
    print(msg, file=sys.stderr)
    write_step_summary(f"### {args.name} harvest {today}\n\n{msg}")
    return 0


def run_with_annotations():
    """Run main(); on failure also emit a GitHub Actions ::error:: annotation so the
    reason shows on the run's summary page, not only in the step log."""
    def annotate(msg):
        if os.environ.get("GITHUB_ACTIONS"):
            msg = str(msg).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::error title=Harvest failed::{msg}", flush=True)
    try:
        return main()
    except SystemExit as e:
        if e.code not in (None, 0) and not isinstance(e.code, int):
            annotate(e.code)
        raise
    except Exception as e:  # unexpected errors: annotate, then show the traceback
        annotate(f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(run_with_annotations())
