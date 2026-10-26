# Archive-It Metadata Snapshots

Weekly CSV snapshots of Rod Library's web archive collection metadata from
[Archive-It](https://archive-it.org/home/uni), harvested over OAI-PMH from
`https://archive-it.org/oai/organizations/<organization number>`.

| Path | What it is |
|---|---|
| `snapshots/archiveit_oai_dc_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_oai_dc.csv` | Copy of the newest snapshot (stable link) |

- **Schedule:** Mondays at 15:00 UTC, via [`.github/workflows/archiveit-weekly.yml`](../.github/workflows/archiveit-weekly.yml).
- **Harvester:** [`scripts/oai_harvest.py`](../scripts/oai_harvest.py), shared with the other collections.
  The CSV format is described in the [main README](../README.md#csv-format).

## Setup

Archive-It's OAI-PMH feed is public, so no login is needed. One repository **variable** is required
(Settings → Secrets and variables → Actions → **Variables**):

| Variable | Value |
|---|---|
| `ARCHIVEIT_ORG_ID` | Your Archive-It organization number: the number after `https://partner.archive-it.org/` in your browser's address bar when signed in to the Archive-It web application |

Until it is set, the weekly run is skipped with a warning.

## What's included

The feed has collection-level Dublin Core (title, description, subject, creator, dates, and so on) for
collections whose metadata is opted in to OAI-PMH. **Collections are excluded by default.** In the
Archive-It web application, open each collection's **Metadata** tab and tick **Export metadata to
OAI-PMH (including WorldCat)**. A collection missing from the CSV usually means that box isn't ticked.
See Archive-It's guide:
[How to provide access to your collection's metadata with OAI-PMH](https://support.archive-it.org/hc/en-us/articles/210510506-How-to-provide-access-to-your-collection-s-metadata-with-OAI-PMH).

Archive-It also offers MARC21 (`--prefix oai_marc21`), converted from the Dublin Core.

## Running locally

From the repository root, replacing `NNNN` with the organization number:

```bash
pip install -r requirements.txt
python scripts/oai_harvest.py --name archiveit \
    --base-url https://archive-it.org/oai/organizations/NNNN --out-dir archiveit/snapshots
```
