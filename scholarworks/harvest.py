#!/usr/bin/env python3
"""
Harvest metadata from UNI ScholarWorks (bepress Digital Commons) via OAI-PMH
and write a dated CSV snapshot.

Usage:
    python harvest.py                          # full harvest, oai_dc
    python harvest.py --prefix dcq             # qualified Dublin Core
    python harvest.py --set publication:etd    # one set only
    python harvest.py --list-sets              # print available sets and exit
    python harvest.py --keep 12                # keep only the 12 newest snapshots

Output:
    snapshots/scholarworks_<prefix>_<YYYY-MM-DD>.csv   dated snapshot
    snapshots/latest_<prefix>.csv                      copy of the newest snapshot
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://scholarworks.uni.edu/do/oai/"
USER_AGENT = (
    "RodLibrary-Metadata-Harvester/1.0 "
    "(+https://github.com/rodlibrary/Metadata)"
)
OAI_NS = "http://www.openarchives.org/OAI/2.0/"
NS = {"oai": OAI_NS}
SEP = " || "  # joins repeated values (e.g. multiple creators) in one cell

# Columns that always come first, in this order. Any other metadata element
# found in the harvest is appended after these, alphabetically.
BASE_COLUMNS = ["oai_identifier", "datestamp", "setSpec", "deleted"]
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


def oai_request(session, base_url, params, max_retries=6, timeout=120):
    """GET an OAI-PMH response, retrying on 503/429/5xx and network errors."""
    delay = 10
    for attempt in range(1, max_retries + 1):
        try:
            r = session.get(base_url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise
            print(f"  network error ({exc}); retrying in {delay}s", file=sys.stderr)
            time.sleep(delay)
            delay = min(delay * 2, 300)
            continue

        if r.status_code == 200:
            return ET.fromstring(r.content)

        if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            wait = r.headers.get("Retry-After")
            wait = int(wait) if wait and wait.isdigit() else delay
            print(f"  HTTP {r.status_code}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            delay = min(delay * 2, 300)
            continue

        r.raise_for_status()
    raise RuntimeError("unreachable")


def check_oai_error(root):
    """Return the OAI error code (or None). Raises on anything but noRecordsMatch."""
    err = root.find("oai:error", NS)
    if err is None:
        return None
    code = err.get("code", "")
    if code == "noRecordsMatch":
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
            return
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


def parse_record(rec) -> OrderedDict:
    """Flatten one OAI <record> into {column: value}."""
    row: OrderedDict[str, list[str]] = OrderedDict()
    header = rec.find("oai:header", NS)
    row["oai_identifier"] = [header.findtext("oai:identifier", "", NS).strip()]
    row["datestamp"] = [header.findtext("oai:datestamp", "", NS).strip()]
    row["setSpec"] = [s.text.strip() for s in header.findall("oai:setSpec", NS) if s.text]
    row["deleted"] = ["true" if header.get("status") == "deleted" else "false"]

    md = rec.find("oai:metadata", NS)
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
def list_sets(session, base_url):
    for container in paged(session, base_url, "ListSets", {}):
        for s in container.findall("oai:set", NS):
            print(f"{s.findtext('oai:setSpec', '', NS)}\t{s.findtext('oai:setName', '', NS)}")


def harvest(session, base_url, prefix, set_spec=None, from_date=None, delay=1.0):
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
    return rows


def column_order(rows):
    seen = set()
    for r in rows:
        seen.update(r.keys())
    cols = list(BASE_COLUMNS)
    cols += [c for c in DC_ORDER if c in seen and c not in cols]
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


def prune(out_dir: Path, prefix: str, keep: int):
    if keep <= 0:
        return
    snaps = sorted(out_dir.glob(f"scholarworks_{prefix}_*.csv"))
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
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
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
    args = p.parse_args(argv)

    session = make_session()

    if args.list_sets:
        list_sets(session, args.base_url)
        return 0

    print(f"Harvesting {args.base_url} (prefix={args.prefix}"
          f"{', set=' + args.set_spec if args.set_spec else ''})", file=sys.stderr)
    rows = harvest(session, args.base_url, args.prefix, args.set_spec,
                   args.from_date, args.delay)

    n_deleted = sum(r.get("deleted") == "true" for r in rows)
    if not args.include_deleted:
        rows = [r for r in rows if r.get("deleted") != "true"]

    if not rows:
        print("No records harvested; not writing a snapshot.", file=sys.stderr)
        return 1

    today = dt.date.today().isoformat()
    out_dir = Path(args.out_dir)
    snap = out_dir / f"scholarworks_{args.prefix}_{today}.csv"
    cols = write_csv(rows, snap)
    shutil.copyfile(snap, out_dir / f"latest_{args.prefix}.csv")
    prune(out_dir, args.prefix, args.keep)

    msg = (f"Wrote {len(rows)} records, {len(cols)} columns to {snap}"
           f" ({n_deleted} deleted records {'kept' if args.include_deleted else 'skipped'})")
    print(msg, file=sys.stderr)
    write_step_summary(f"### ScholarWorks harvest {today}\n\n{msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
