# MODS crosswalk

The weekly snapshots, mapped to MODS 3.8 for indexing in Solr and discovery in Blacklight.

| Path | What it is |
|---|---|
| `<source>/<source>_mods.csv` | Every record in the layout of [`crosswalk/MODS_template.csv`](../crosswalk/MODS_template.csv): row 1 is the MODS XPath, row 2 the label, then one row per record. Repeated values are joined with ` \|\| `. |
| Run artifact `mods-<run>` | MODS XML (`<modsCollection>`, one per source) and Solr JSON, from **Actions → MODS crosswalk → (run) → Artifacts**. Kept 30 days. Not committed: the XML is over GitHub's 100 MB file limit for ScholarWorks and ArchivesSpace. |

- **Schedule:** runs after each weekly harvest succeeds, via [`.github/workflows/mods-crosswalk.yml`](../.github/workflows/mods-crosswalk.yml); also **Run workflow** by hand.
- **Script:** [`scripts/mods_crosswalk.py`](../scripts/mods_crosswalk.py). Each run validates every record against the MODS 3.8 schema and lists, per source, how many records have each field (in the run's summary page).
- **Settings you can edit without touching code** (in [`crosswalk/`](../crosswalk/)):
  - `config.json`: repository names, link patterns (Preservica, MuseumPlus), which records to include
  - `genre_rules.csv`: genre terms assigned by pattern (e.g. any ScholarWorks collection whose name contains "Theses" → *Academic theses*, lcgft)
  - `languages.csv`: language names and codes → ISO 639-2/B
  - `MODS_template.csv`: the CSV layout. Reorder or remove columns freely; a new column needs a line in the script's `COLUMN_VALUES`.

## Running locally

```bash
pip install -r requirements.txt
python scripts/mods_crosswalk.py                                  # CSVs for every source with a snapshot
python scripts/mods_crosswalk.py --source preservica --xml --solr-json   # + build/preservica/*.xml, *.json
python scripts/mods_crosswalk.py --xml --validate                 # check against the MODS 3.8 XSD (needs internet)
```

## Indexing in Solr / Blacklight

Set the repository variable `SOLR_URL` to the core (e.g. `https://solr.library.uni.edu/solr/blacklight-core`) and each
run posts its documents, then deletes that source's documents that weren't in the run. The runner must be able
to reach Solr; if Solr is campus-only, download the artifact and post `*_solr.json` to `/update` from inside
the network, or run the script there with `--solr-json --solr-url ...`.

Field names follow Blacklight's default dynamic fields (`*_tsim` searchable, `*_ssim` facetable, `*_ssm` stored,
`*_si` sortable, `*_isim` integers):

| Solr field | From MODS |
|---|---|
| `id` | `<source>-<local identifier>` |
| `source_ssi`, `record_type_ssi` | which system; resource / archival_object, folder / asset, or MuseumPlus category |
| `title_tsim`, `title_ssm`, `title_si` | titleInfo/title |
| `author_tsim`, `author_ssim`, `author_display_ssm` | name/namePart (display adds the role) |
| `abstract_tesim`, `note_tesim` | abstract, note |
| `format_ssim`, `genre_ssim` | typeOfResource, genre |
| `subject_tsim`, `subject_ssim` | full subject headings |
| `subject_topic_ssim`, `subject_geo_ssim`, `subject_era_ssim`, `subject_name_ssim` | subject parts, for facets |
| `language_ssim` | language name (from the ISO 639-2 code) |
| `date_display_ssm`, `date_created_ssm`, `pub_date_si`, `date_isim` | displayDate, dateCreated (EDTF), first year, every year covered (for a date-range facet) |
| `collection_ssim`, `collection_url_ssm`, `repository_ssim`, `call_number_ssim`, `extent_ssm` | relatedItem host title / URL, physicalLocation, shelfLocator, extent |
| `url_ssm`, `file_url_ssm`, `thumbnail_url_ss` | location/url: landing page, file, thumbnail |
| `access_ssm`, `rights_ssm`, `license_ssm`, `license_url_ssm` | accessCondition |
| `mods_xml_ss` | the whole MODS record, only with `--solr-include-xml` |

## Crosswalk

How each source's fields reach the template columns. "—" means the source has nothing for that column today.

| Template column (MODS) | ScholarWorks (Digital Commons, oai_dc) | ArchivesSpace (oai_dc) | Preservica (oai_dc) | MuseumPlus (API) |
|---|---|---|---|---|
| file or folder name | OAI ID (last part) | record URI | title (file/folder name) | `museumplus-<id>` |
| Location (URL) | first `identifier` URL (landing page)¹ | `identifier` URL | `item_url` pattern + reference¹ | `item_url` pattern (blank until set) |
| Source ID | OAI identifier | `/repositories/…` URI | Preservica reference (UUID) | object number, else `__id` |
| Abstract | `description` (thumbnail URLs moved to location/url) | `description` | `description` | `ObjBriefDescriptionClb` |
| Title | `title` (trailing punctuation removed) | `title` | `title` | `ObjObjectTitleGrp.TitleTxt`; if none, first sentence of the description in [brackets], `supplied="yes"` |
| Name / Role | `creator` (creator, cre); `contributor` (contributor, ctb); personal vs corporate guessed from form | `creator` | `creator` | `ObjPerAssociationRef`: "Name (dates) (Role)" split into name, date, role |
| Type Of Resource | from MIME type in `format` (PDF → text, image/* → still image, …) | mixed material (`collection="yes"` on resources) | from file extension; folders mixed material, `collection="yes"` | three dimensional object |
| Genre | `type` when not "text"; genre_rules.csv on collection name and format | `type` | — | `ObjClassificationVoc`; genre_rules.csv on category |
| Subject (Topical / Name / Temporal / Geographic) | `subject` and `coverage`, split on ";" and "--"; each heading's main term classified (place, period, name or topic)² | `subject` and `coverage`, same way | `subject` | — |
| Date Created / Display Date | `date`: year only when stored as Jan 1, otherwise full date | `date` parsed to EDTF (e.g. "c. 1934-1935" → `1934/1935`), original text kept as display date; "Date acquired" values dropped | not mapped³ | `ObjDateGrp.DateTxt`, same parsing ("Early: 1837; Late: 1901" → `1837/1901`) |
| Language | `language` → ISO 639-2/B | `language` (codes and prose) → ISO 639-2/B | — | — |
| Call Number | — | non-URL `identifier` values (call number, box/folder) | — | — |
| Collection / URL (Finding Aid) | `source` (publication title) | parent collection from `relation`, linked to its ArchivesSpace page | parent folder, linked to its page | `ObjCategoryVoc` |
| Extent / Physical Form | — (MIME type goes to internetMediaType in the XML) | `format` | — | `ObjDimAllGrp`, `ObjMaterialTechniqueGrp` |
| Note | `relation`; "Record contributed by …" for other institutions' records | related-materials text from `relation` (resources) | — | `ObjCreditLineVoc` |
| Repository | config `repository`; for other institutions' records, their `publisher` | config prefix + ArchivesSpace repository (`publisher`) | config `repository` | config `repository` |
| Access / Rights Statement / License / Use | `rights` sorted: CC URLs → license label + URL; "©…" → rights statement; access or reuse wording → access or use | `rights`, same sorting | `rights` | — |

¹ The XML puts URLs at `location/url` (with `usage="primary"`), because `location/physicalLocation/url` and
`subject/coordinates` in the template aren't valid MODS; coordinates go to `subject/cartographics/coordinates`.
The CSV keeps the template's columns as they are.
² Digital Commons breaks headings at commas ("Wright || Frank Lloyd || 1867-1959--Posters"); the script rejoins them.
³ Preservica's `dc:date` is a system date (ingest/modification), not when the item was made.

Every XML record also gets `recordInfo` (source, record ID, the harvest datestamp and a conversion note), and
`identifier type="doi"` where ScholarWorks has a DOI.

## Known gaps in the source data

These come from the harvests, not the crosswalk, and limit what the MODS can say:

- **Preservica** exposes only titles (often filenames such as `IMG_8810`) and a few descriptions over oai_dc. Its
  descriptive metadata would need a richer format (its README notes MODS is offered for records that have it). The
  feed also lists some records several times; the crosswalk keeps the newest. There is a "Test items" folder in the feed.
- **MuseumPlus**: `ObjObjectNumberTxt`, `ObjCreditLineVoc`, `ObjClassificationVoc` and `ObjMaterialTechniqueGrp` are
  empty in every record, `__id` is doubled ("10 || 10"), and `ObjDimAllGrp.PreviewENVrt` holds the literal text
  "PreviewENVrt". These look like field-path problems in `museumplus/fields.txt` or the harvester. About 90% of objects
  have no title, so most titles are devised from the description.
- **ArchivesSpace** oai_dc carries subjects, agents and languages for only a small share of records; `oai_mods`
  (offered by the endpoint) would carry more.
- **ScholarWorks**' harvest includes about 320 records from other institutions' Digital Commons sites (their
  `publisher` isn't "UNI ScholarWorks"). The crosswalk leaves them out; set `include_external` to `true` in
  `config.json` to include them.
