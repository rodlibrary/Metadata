# ScholarWorks (Digital Commons) Metadata Snapshots

Weekly CSV snapshots of metadata from [UNI ScholarWorks](https://scholarworks.uni.edu/),
harvested over OAI-PMH from `https://scholarworks.uni.edu/do/oai/`.

| Path | What it is |
|---|---|
| `snapshots/scholarworks_oai_dc_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_oai_dc.csv` | Copy of the newest snapshot (stable link) |

- **Schedule:** Mondays at 11:00 UTC, via [`.github/workflows/scholarworks-weekly.yml`](../.github/workflows/scholarworks-weekly.yml).
- **Harvester:** [`scripts/oai_harvest.py`](../scripts/oai_harvest.py), shared with the other collections.
  The CSV format is described in the [main README](../README.md#csv-format).

## Running locally

From the repository root:

```bash
pip install -r requirements.txt
python scripts/oai_harvest.py --name scholarworks \
    --base-url https://scholarworks.uni.edu/do/oai/ --out-dir scholarworks/snapshots

# Other options
python scripts/oai_harvest.py --base-url https://scholarworks.uni.edu/do/oai/ --list-sets
#   --set publication:etd   one collection only
#   --prefix dcq            qualified Dublin Core
```
