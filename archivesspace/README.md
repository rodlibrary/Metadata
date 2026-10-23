# ArchivesSpace Metadata Snapshots

Weekly CSV snapshots of metadata from the Rod Library [ArchivesSpace](https://aspace.library.uni.edu/)
(Special Collections & University Archives), harvested over OAI-PMH from
`https://aspace.library.uni.edu:8082`.

| Path | What it is |
|---|---|
| `snapshots/archivesspace_oai_dc_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_oai_dc.csv` | Copy of the newest snapshot (stable link) |

- **Schedule:** Mondays at 12:00 UTC, via [`.github/workflows/archivesspace-weekly.yml`](../.github/workflows/archivesspace-weekly.yml).
- **Harvester:** [`scripts/oai_harvest.py`](../scripts/oai_harvest.py), shared with the other collections.
  The CSV format is described in the [main README](../README.md#csv-format).

## What's included

ArchivesSpace publishes both **collection-level records** (resources) and **component records**
(archival objects: series, boxes, folders, items). The `record_type` column says which each row is
(`resource` or `archival_object`), so you can filter to collection-level descriptions in a spreadsheet.
Only records published to the public interface are exposed over OAI-PMH.

## Running locally

From the repository root:

```bash
pip install -r requirements.txt
python scripts/oai_harvest.py --name archivesspace \
    --base-url https://aspace.library.uni.edu:8082 --out-dir archivesspace/snapshots

# See what the endpoint offers
python scripts/oai_harvest.py --base-url https://aspace.library.uni.edu:8082 --list-formats
python scripts/oai_harvest.py --base-url https://aspace.library.uni.edu:8082 --list-sets
#   --prefix oai_dcterms    richer Dublin Core terms (also oai_mods, oai_marc; oai_ead for resources only)
#   --set SETSPEC           one repository or set only
```
