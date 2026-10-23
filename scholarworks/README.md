# ScholarWorks (Digital Commons) Metadata Snapshots

Weekly CSV snapshots of metadata from [UNI ScholarWorks](https://scholarworks.uni.edu/),
harvested over OAI-PMH (`https://scholarworks.uni.edu/do/oai/`).

## Files

| Path | What it is |
|---|---|
| `scholarworks/snapshots/scholarworks_oai_dc_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `scholarworks/snapshots/latest_oai_dc.csv` | Copy of the newest snapshot (stable link) |
| `scholarworks/harvest.py` | The harvester |
| `.github/workflows/weekly-harvest.yml` | GitHub Action that runs it every Monday |

## CSV format

- One row per item, sorted by OAI identifier, so week-to-week diffs are readable.
- The first columns are always `oai_identifier, datestamp, setSpec, deleted`, followed by the Dublin Core
  elements (`title, creator, contributor, date, description, subject, type, format, identifier, ...`).
- Repeated values (several creators, subjects, etc.) are joined with ` || ` in one cell.
- UTF-8 with BOM so Excel opens accented characters correctly.
- Records the repository reports as deleted are left out unless `--include-deleted` is used.

## Running locally

```bash
cd scholarworks
pip install -r requirements.txt
python harvest.py                         # full harvest, simple Dublin Core
python harvest.py --list-sets             # list collections (sets)
python harvest.py --set publication:etd   # one collection only
python harvest.py --prefix dcq            # qualified Dublin Core, if the server offers it
python harvest.py --keep 12               # keep only the 12 newest dated snapshots
```

## Automation

The workflow runs every Monday at 11:00 UTC and can also be started from the **Actions** tab
(**Run workflow**). It commits the new snapshot only when something changed.
