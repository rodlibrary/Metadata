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

## Extra fields

Simple Dublin Core has no embargo, department or note fields, so each run also harvests a richer
Digital Commons format (the first of `document-export`, `dcq`, `qualified-dublin-core` that ScholarWorks
offers) and adds the fields whose names match `embargo|department|note|comment` as extra columns at the
end of the CSV (e.g. `embargo_date`, `department`, `comments`). They're joined to the main rows by OAI identifier.

The run's summary page lists every field name in that format with record counts. To add or drop
fields, edit the `--extra-fields` pattern in
[`.github/workflows/scholarworks-weekly.yml`](../.github/workflows/scholarworks-weekly.yml), e.g.
`embargo|department|note|comment|advisor`. If the richer format can't be harvested, the run still writes
the Dublin Core CSV and shows a warning.

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
