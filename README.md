# Metadata
Metadata exports from Rod Library systems, including Digital Commons, ArchivesSpace, MusueumPlus, and Preservica.

## Contents

- [`scholarworks/`](scholarworks/): weekly CSV snapshots of UNI ScholarWorks (Digital Commons) metadata, harvested automatically over OAI-PMH.
- [`archivesspace/`](archivesspace/): weekly CSV snapshots of ArchivesSpace collection and component metadata, harvested automatically over OAI-PMH.
- [`preservica/`](preservica/): weekly CSV snapshots of Preservica folder and asset metadata, harvested over OAI-PMH (needs a Preservica login; see its README for setup).
- [`scripts/oai_harvest.py`](scripts/oai_harvest.py): the shared OAI-PMH harvester used by all three.

## How the snapshots are made

Each collection has a GitHub Actions workflow in [`.github/workflows/`](.github/workflows/) that runs weekly
(and on demand from the **Actions** tab with **Run workflow**). It harvests the full set of records,
writes a dated CSV plus `latest_oai_dc.csv`, keeps the 4 newest dated snapshots, and commits only when something changed.

## CSV format

- One row per record, sorted by OAI identifier, so week-to-week diffs are readable.
- The first columns are always `oai_identifier, datestamp, setSpec, deleted` (plus `record_type` for ArchivesSpace),
  followed by the Dublin Core elements (`title, creator, contributor, date, description, subject, type, format, identifier, ...`).
- Repeated values (several creators, subjects, etc.) are joined with ` || ` in one cell.
- UTF-8 with BOM so Excel opens accented characters correctly.
- Records the source reports as deleted are left out unless `--include-deleted` is used.
