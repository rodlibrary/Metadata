# ArchivesSpace Metadata Snapshots

Weekly CSV snapshots of metadata from the Rod Library [ArchivesSpace](https://aspace.library.uni.edu/)
(Special Collections & University Archives), harvested over OAI-PMH in MODS from
`https://aspace.library.uni.edu:8082`.

| Path | What it is |
|---|---|
| `snapshots/archivesspace_oai_mods_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_oai_mods.csv` | Copy of the newest snapshot (stable link) |

- **Schedule:** Mondays at 12:00 UTC, via [`.github/workflows/archivesspace-weekly.yml`](../.github/workflows/archivesspace-weekly.yml).
  It takes about 3 hours (two passes; see below).
- **Harvester:** [`scripts/oai_harvest.py`](../scripts/oai_harvest.py), shared with the other collections.
  The CSV format is described in the [main README](../README.md#csv-format).
- Snapshots before October 2026 were Dublin Core (`archivesspace_oai_dc_*.csv`); they're in git history.

## What's included

ArchivesSpace publishes both **collection-level records** (resources) and **component records**
(archival objects: series, boxes, folders, items). The `record_type` column says which each row is
(`resource` or `archival_object`), so you can filter to collection-level descriptions in a spreadsheet.
Only records published to the public interface are exposed over OAI-PMH.

### Columns

Each MODS element becomes a column named by its path, for example:

| Column | MODS |
|---|---|
| `titleinfo/title` | title (ArchivesSpace writes `titleinfo` in lower case) |
| `name/namePart` | creator names |
| `originInfo/dateCreated` | normalized date, when ArchivesSpace has one (e.g. `1965/1997`) |
| `physicalDescription/extent`, `abstract` | extent, scope and contents |
| `note[@type='biographical']`, `note[@type='acquisition']`, ... | notes, one column per note type |
| `accessCondition[@type='restrictionOnAccess']`, `accessCondition[@type='useAndReproduction']` | access and use conditions |
| `subject`, `subject/topic`, `subject/geographic`, ... | the whole subject heading, and its parts by type |
| `relatedItem[@type='host']` | for components: the collection title and call number |
| `location/physicalLocation` | ArchivesSpace repository (University Archives, Manuscripts Collection, ...) |
| `oai_dc:identifier`, `oai_dc:date`, `oai_dc:publisher` | merged in from Dublin Core, see below |

When an element repeats (several names, several subjects), each column has one value per element,
joined with ` || `, and they line up: the 3rd value of `name/namePart` and of `name/@type` describe the
same name, with blanks where a name has no value. Several values inside one element are joined with ` ; `.

### Why it also harvests Dublin Core

ArchivesSpace's MODS has richer notes and rights than its Dublin Core, but leaves out each record's
ArchivesSpace link, its call number (collections) or box/folder (components), and the dates of most
records. So after the MODS pass the harvester also reads `oai_dc` and adds just those fields as
`oai_dc:identifier`, `oai_dc:date` and `oai_dc:publisher`. Records ArchivesSpace serves only in
Dublin Core (there shouldn't be any) are kept with all their `oai_dc:` fields and
`metadata_format` = `oai_dc`.

## Test run

**Actions → Weekly ArchivesSpace metadata snapshot → Run workflow**, with a number in *Test run*,
harvests only that many records, attaches the CSV to the run (under **Artifacts**) and commits nothing.

## Running locally

From the repository root:

```bash
pip install -r requirements.txt
python scripts/oai_harvest.py --name archivesspace \
    --base-url https://aspace.library.uni.edu:8082 --out-dir archivesspace/snapshots \
    --prefix oai_mods --merge-prefix oai_dc --merge-fields "^(identifier|date|publisher)$"

# See what the endpoint offers
python scripts/oai_harvest.py --base-url https://aspace.library.uni.edu:8082 --list-formats
python scripts/oai_harvest.py --base-url https://aspace.library.uni.edu:8082 --list-sets
#   --prefix oai_dc / oai_dcterms / oai_marc / oai_ead (resources only)
#   --max-records 300       stop early, for testing
```
