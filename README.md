# Metadata
Metadata exports from Rod Library systems, including Digital Commons, ArchivesSpace, MusueumPlus, Preservica, and Archive-It.

## Contents

- [`scholarworks/`](scholarworks/): weekly CSV snapshots of UNI ScholarWorks (Digital Commons) metadata, harvested automatically over OAI-PMH.
- [`archivesspace/`](archivesspace/): weekly CSV snapshots of ArchivesSpace collection and component metadata, harvested automatically over OAI-PMH in MODS.
- [`preservica/`](preservica/): weekly CSV snapshots of Preservica folder and asset metadata, harvested over OAI-PMH (needs a Preservica login; see its README for setup).
- [`archiveit/`](archiveit/): weekly CSV snapshots of Archive-It web archive collection metadata, harvested over OAI-PMH; see its README for setup.
- [`museumplus/`](museumplus/): weekly CSV snapshots of published MuseumPlus object records (collections.uni.edu), harvested through the MuseumPlus API; see its README for setup.
- [`mods/`](mods/): the snapshots crosswalked to MODS 3.8 (template-layout CSVs; MODS XML and Solr JSON as run artifacts), rebuilt after every harvest for Solr/Blacklight. See its README for the field-by-field crosswalk.
- [`crosswalk/`](crosswalk/): the MODS CSV template and the editable settings and lookup tables the crosswalk uses.
- [`scripts/mods_crosswalk.py`](scripts/mods_crosswalk.py): the MODS crosswalk.
- [`scripts/oai_harvest.py`](scripts/oai_harvest.py): the shared OAI-PMH harvester used by ScholarWorks, ArchivesSpace, Preservica and Archive-It.
- [`scripts/museumplus_harvest.py`](scripts/museumplus_harvest.py): the MuseumPlus API harvester.

## How the snapshots are made

Each collection has a GitHub Actions workflow in [`.github/workflows/`](.github/workflows/) that runs weekly
(and on demand from the **Actions** tab with **Run workflow**). It harvests the full set of records,
writes a dated CSV plus a `latest_….csv` copy, keeps the 4 newest dated snapshots, and commits only when something changed.
When a harvest succeeds, the **MODS crosswalk** workflow maps the latest snapshots to MODS and commits `mods/`.

## CSV format

- One row per record, sorted by OAI identifier, so week-to-week diffs are readable.
- The first columns are always `oai_identifier, datestamp, setSpec, deleted` (plus `record_type` for ArchivesSpace),
  followed by the Dublin Core elements (`title, creator, contributor, date, description, subject, type, format, identifier, ...`).
- ArchivesSpace is harvested in MODS instead: one column per MODS path (`titleinfo/title`, `name/namePart`,
  `note[@type='bioghist']`, ...), plus a few `oai_dc:` columns. See [`archivesspace/README.md`](archivesspace/README.md#columns).
- Repeated values (several creators, subjects, etc.) are joined with ` || ` in one cell.
- UTF-8 with BOM so Excel opens accented characters correctly.
- Records the source reports as deleted are left out unless `--include-deleted` is used.
