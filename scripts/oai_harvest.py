#!/usr/bin/env python3
"""
Harvest metadata from an OAI-PMH endpoint and write a dated CSV snapshot.
Used for UNI ScholarWorks (Digital Commons), ArchivesSpace and Preservica.

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

Options:
    --prefix dcq / oai_dcterms ...   another metadata format (default oai_dc)
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
from collections import OrderedDict
from pathlib import Path

import requests

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


def oai_request(session, base_url, params, max_retries=6, timeout=120):
    """GET an OAI-PMH response, retrying on 503/429/5xx and network errors."""
    delay = 10
    auth = getattr(session, "oai_auth", None)
    relogged = False
    for attempt in range(1, max_retries + 1):
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

        if r.status_code == 200:
            return ET.fromstring(r.content)

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
    m = RECORD_TYPE_RE.search(row["oai_identifier"][0])
    if m:
        # "archival_objects" -> "archival_object", "accessions" -> "accession"
        row["record_type"] = [m.group(1)[:-1]]

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
    if "record_type" in seen:
        cols.append("record_type")
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

    print(f"Harvesting {args.base_url} (prefix={args.prefix}"
          f"{', set=' + args.set_spec if args.set_spec else ''})", file=sys.stderr)
    rows = harvest(session, args.base_url, args.prefix, args.set_spec,
                   args.from_date, args.delay)

    n_deleted = sum(r.get("deleted") == "true" for r in rows)
    if not args.include_deleted:
        rows = [r for r in rows if r.get("deleted") != "true"]

    if not rows:
        raise SystemExit("No records harvested; not writing a snapshot.")

    today = dt.date.today().isoformat()
    out_dir = Path(args.out_dir)
    snap = out_dir / f"{args.name}_{args.prefix}_{today}.csv"
    cols = write_csv(rows, snap)
    shutil.copyfile(snap, out_dir / f"latest_{args.prefix}.csv")
    prune(out_dir, args.name, args.prefix, args.keep)

    msg = (f"Wrote {len(rows)} records, {len(cols)} columns to {snap}"
           f" ({n_deleted} deleted records {'kept' if args.include_deleted else 'skipped'})")
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
