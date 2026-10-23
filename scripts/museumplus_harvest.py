#!/usr/bin/env python3
"""
Harvest object metadata from MuseumPlus (zetcom RIA web services) and write a
dated CSV snapshot. MuseumPlus does not offer OAI-PMH, so this pages through
the module search API instead:

    POST <server>/ria-ws/application/module/<Module>/search   (XML, limit/offset)

Credentials come from environment variables:
    MUSEUMPLUS_USERNAME, MUSEUMPLUS_PASSWORD   HTTP Basic login (standard)
    MUSEUMPLUS_API_KEY                         optional; see --auth
    MUSEUMPLUS_API_KEY_HEADER                  optional header name to send the key in

Because a logged-in account usually sees internal data (valuations, locations,
donor contacts, unpublished records), output is limited two ways:
    --require FIELD=VALUE[|VALUE...]   keep only records whose FIELD has one of
                                       these values (e.g. a "publish online" flag)
    --fields-file FILE                 write only the field paths listed in FILE
--inspect writes no CSV: it reports field names (no values) and the values of
publication-like flag fields, so the filter and field list can be chosen safely.

Usage:
    python scripts/museumplus_harvest.py --base-url https://HOST --inspect
    python scripts/museumplus_harvest.py --base-url https://HOST \\
        --require "ObjPublicationGrp.PublicationVoc=Yes" \\
        --fields-file museumplus/fields.txt --out-dir museumplus/snapshots --keep 4

Field paths in the CSV:
    FieldName                    a top-level field (dataField, virtualField, systemField)
    VocName / RefName            a vocabulary or linked-record field (display values)
    GroupName.FieldName          a field inside a repeatable group
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
from collections import Counter, defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

import requests

USER_AGENT = "RodLibrary-Metadata-Harvester/1.0 (+https://github.com/rodlibrary/Metadata)"
NS = "http://www.zetcom.com/ria/ws/module"
SEP = " || "
LEAF_FIELDS = ("dataField", "virtualField", "systemField")
# Fields whose values --inspect reports, to help pick the --require filter.
FLAG_RE = re.compile(r"publi|online|internet|web|portal|release|freigabe", re.I)

SEARCH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<application xmlns="http://www.zetcom.com/ria/ws/module/search" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.zetcom.com/ria/ws/module/search http://docs.zetcom.com/ws/module/search/search_1_4.xsd">
  <modules>
    <module name="{module}">
      <search limit="{limit}" offset="{offset}">
      </search>
    </module>
  </modules>
</application>"""


def q(tag):
    return f"{{{NS}}}{tag}"


def local(tag):
    return tag.rsplit("}", 1)[-1]


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def api_root(url: str) -> str:
    """Accept any URL on the server and return <server>/ria-ws/application."""
    url = url.rstrip("/")
    if "/ria-ws/application" in url:
        return url.split("/ria-ws/application")[0] + "/ria-ws/application"
    return url + "/ria-ws/application"


class Client:
    def __init__(self, base_url, module, auth_mode="auto", language="en"):
        self.search_url = f"{api_root(base_url)}/module/{module}/search"
        self.module = module
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT,
                               "Content-Type": "application/xml",
                               "Accept": "application/xml",
                               "Accept-Language": language})
        user = os.environ.get("MUSEUMPLUS_USERNAME", "")
        pw = os.environ.get("MUSEUMPLUS_PASSWORD", "")
        key = os.environ.get("MUSEUMPLUS_API_KEY", "")
        key_header = os.environ.get("MUSEUMPLUS_API_KEY_HEADER", "")
        if key and key_header:
            self.s.headers[key_header] = key

        # Ways to log in, tried in order on the first request until one works.
        options = {
            "password": ("username + password", (user, pw)) if user and pw else None,
            "key-as-password": ("username + API key", (user, key)) if user and key else None,
            "header-only": (f"API key in {key_header} header", None) if key and key_header else None,
        }
        order = (["password", "key-as-password", "header-only"] if auth_mode == "auto"
                 else [auth_mode])
        self.candidates = [options[m] for m in order if options.get(m)]
        if not self.candidates:
            raise SystemExit("No MuseumPlus credentials: set MUSEUMPLUS_USERNAME and "
                             "MUSEUMPLUS_PASSWORD (and/or MUSEUMPLUS_API_KEY).")
        self.auth_label = None

    def post(self, body: bytes, max_retries=6):
        delay = 10
        attempt = 0
        while True:
            attempt += 1
            if self.auth_label is None:
                return self._first_request(body)
            try:
                r = self.s.post(self.search_url, data=body, timeout=180)
            except requests.RequestException as exc:
                if attempt >= max_retries:
                    raise
                print(f"  network error ({exc}); retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            if r.status_code == 200:
                return ET.fromstring(r.content)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                print(f"  HTTP {r.status_code}; retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            raise SystemExit(f"MuseumPlus request failed: HTTP {r.status_code} "
                             f"{r.text[:300]}")

    def _first_request(self, body):
        statuses = []
        for label, auth in self.candidates:
            self.s.auth = auth
            r = self.s.post(self.search_url, data=body, timeout=180)
            if r.status_code == 200:
                self.auth_label = label
                print(f"  logged in to MuseumPlus with {label}", file=sys.stderr)
                return ET.fromstring(r.content)
            statuses.append(f"{label}: HTTP {r.status_code}")
            if r.status_code not in (401, 403):
                break
        raise SystemExit("MuseumPlus login/search failed (" + "; ".join(statuses) +
                         f") at {self.search_url}")

    def search_page(self, offset, limit):
        body = SEARCH_XML.format(module=escape(self.module), limit=limit, offset=offset)
        root = self.post(body.encode("utf-8"))
        mod = root.find(f".//{q('module')}")
        if mod is None:
            return 0, []
        total = int(mod.get("totalSize", "0") or 0)
        return total, mod.findall(q("moduleItem"))


# --------------------------------------------------------------------------
# Flattening
# --------------------------------------------------------------------------
def display(el, language):
    """Best human-readable value of a field/item element."""
    fvs = el.findall(q("formattedValue"))
    for fv in fvs:
        if (fv.get("language") or "").lower().startswith(language) and fv.text:
            return fv.text
    for fv in fvs:
        if fv.text:
            return fv.text
    v = el.find(q("value"))
    if v is not None and v.text:
        return v.text
    return el.get("name") or ""


def ref_item_text(item, language):
    """'Name (Role)' for a linked record; role comes from a nested vocabulary, if any."""
    text = display(item, language) or item.get("moduleItemId", "")
    roles = [display(v, language) for v in item.iter(q("vocabularyReferenceItem"))]
    roles = [r for r in roles if r and r != text]
    return f"{text} ({', '.join(roles)})" if roles else text


def flatten(item, language="en"):
    row = defaultdict(list)
    row["__id"].append(item.get("id", ""))
    if item.get("uuid"):
        row["__uuid"].append(item.get("uuid"))

    def walk(el, prefix):
        for child in el:
            kind, name = local(child.tag), child.get("name")
            if not name:
                continue
            path = f"{prefix}{name}"
            if kind in LEAF_FIELDS:
                val = display(child, language)
                if val:
                    row[path].append(val)
            elif kind == "vocabularyReference":
                for vi in child.findall(q("vocabularyReferenceItem")):
                    val = display(vi, language)
                    if val:
                        row[path].append(val)
            elif kind == "moduleReference":
                for mi in child.findall(q("moduleReferenceItem")):
                    val = ref_item_text(mi, language)
                    if val:
                        row[path].append(val)
            elif kind == "repeatableGroup" and not prefix:
                for gi in child.findall(q("repeatableGroupItem")):
                    walk(gi, f"{path}.")

    walk(item, "")
    return {k: SEP.join(" ".join(v.split()) for v in vals) for k, vals in row.items()}


# --------------------------------------------------------------------------
# Filters and output
# --------------------------------------------------------------------------
def parse_require(specs):
    reqs = []
    for spec in specs or []:
        field, _, values = spec.partition("=")
        if not field or not values:
            raise SystemExit(f"--require must look like FIELD=VALUE[|VALUE]: {spec!r}")
        reqs.append((field.strip(), {v.strip().lower() for v in values.split("|")}))
    return reqs


def passes(row, reqs):
    for field, allowed in reqs:
        vals = {v.strip().lower() for v in row.get(field, "").split(SEP.strip()) if v.strip()}
        if not vals & allowed:
            return False
    return True


def read_fields(path):
    fields = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            fields.append(line)
    if "__id" not in fields:
        fields.insert(0, "__id")
    return fields


def write_step_summary(text):
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def inspect_report(rows, module):
    counts = Counter()
    flags = defaultdict(Counter)
    for r in rows:
        for k, v in r.items():
            counts[k] += 1
            if FLAG_RE.search(k):
                for part in v.split(SEP.strip()):
                    if part.strip():
                        flags[k][part.strip()[:80]] += 1
    lines = [f"### MuseumPlus inspect: {module}", "",
             f"{len(rows)} records read. No CSV was written.", "",
             "#### Publication-like fields and their values",
             "Use one of these for `MUSEUMPLUS_REQUIRE`, e.g. `FieldPath=Value`.", ""]
    if flags:
        lines += ["| Field path | Value | Records |", "|---|---|---|"]
        for k in sorted(flags):
            for val, n in flags[k].most_common(15):
                lines.append(f"| `{k}` | {val} | {n} |")
    else:
        lines.append("_None found._")
    lines += ["", "#### All field paths (names only, no values)",
              "Copy the ones that are public into `museumplus/fields.txt`.", "",
              "| Field path | Records with a value |", "|---|---|"]
    lines += [f"| `{k}` | {n} |" for k, n in sorted(counts.items())]
    text = "\n".join(lines)
    print(text)
    write_step_summary(text)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True,
                   help="MuseumPlus server, e.g. https://example.zetcom.app (any URL on it works)")
    p.add_argument("--module", default="Object")
    p.add_argument("--name", default="museumplus")
    p.add_argument("--limit", type=int, default=100, help="records per request")
    p.add_argument("--max-records", type=int, default=0, help="stop after N (testing)")
    p.add_argument("--language", default="en")
    p.add_argument("--auth", default="auto",
                   choices=["auto", "password", "key-as-password", "header-only"])
    p.add_argument("--require", action="append", metavar="FIELD=VALUE[|VALUE]")
    p.add_argument("--fields-file")
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--out-dir", default="museumplus/snapshots")
    p.add_argument("--keep", type=int, default=0)
    p.add_argument("--delay", type=float, default=1.0)
    args = p.parse_args(argv)

    if not args.inspect and not (args.require and args.fields_file):
        raise SystemExit("Refusing to write a CSV without both --require (a publication "
                         "filter) and --fields-file (public fields). Use --inspect first.")
    reqs = parse_require(args.require)
    fields = read_fields(args.fields_file) if args.fields_file else None

    client = Client(args.base_url, args.module, args.auth, args.language)
    print(f"Harvesting {client.search_url}", file=sys.stderr)
    rows, offset, total = [], 0, None
    while total is None or offset < total:
        total, items = client.search_page(offset, args.limit)
        if not items:
            break
        rows.extend(flatten(it, args.language) for it in items)
        offset += len(items)
        print(f"  {offset} of {total} records", file=sys.stderr)
        if args.max_records and offset >= args.max_records:
            break
        time.sleep(args.delay)

    if args.inspect:
        inspect_report(rows, args.module)
        return 0

    kept = [r for r in rows if passes(r, reqs)]
    if not kept:
        raise SystemExit(f"Read {len(rows)} records but none passed the --require filter; "
                         "not writing a snapshot. Check MUSEUMPLUS_REQUIRE.")
    missing = [f for f in fields if not any(f in r for r in kept)]
    kept.sort(key=lambda r: int(r["__id"]) if r["__id"].isdigit() else r["__id"])

    today = dt.date.today().isoformat()
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    snap = out / f"{args.name}_{args.module.lower()}_{today}.csv"
    with snap.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)
    shutil.copyfile(snap, out / f"latest_{args.module.lower()}.csv")
    if args.keep > 0:
        for old in sorted(out.glob(f"{args.name}_{args.module.lower()}_*.csv"))[:-args.keep]:
            print(f"  removing old snapshot {old.name}", file=sys.stderr)
            old.unlink()

    msg = (f"Read {len(rows)} {args.module} records; {len(kept)} passed the publication "
           f"filter and were written to {snap} with {len(fields)} columns.")
    if missing:
        msg += " Fields in fields.txt with no values: " + ", ".join(f"`{m}`" for m in missing)
    print(msg, file=sys.stderr)
    write_step_summary(f"### MuseumPlus harvest {today}\n\n{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
