# MuseumPlus Metadata Snapshots

Weekly CSV snapshots of published object records from Rod Library's MuseumPlus
(public site: [collections.uni.edu](https://collections.uni.edu/en/)), covering the Gallery, Museum
and Botanical Center collections.

| Path | What it is |
|---|---|
| `snapshots/museumplus_object_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_object.csv` | Copy of the newest snapshot (stable link) |
| `fields.txt` | The fields included in the CSV, in column order |

- **Schedule:** Mondays at 14:00 UTC, via [`.github/workflows/museumplus-weekly.yml`](../.github/workflows/museumplus-weekly.yml).
- **Harvester:** [`scripts/museumplus_harvest.py`](../scripts/museumplus_harvest.py).

## How it differs from the other harvests

MuseumPlus has no OAI-PMH, so the harvester pages through zetcom's MuseumPlus API
(`https://mpcedarfallsuni.zetcom.app/ria-ws/application/module/Object/search`), 100 records at a time.
The columns are MuseumPlus field names rather than Dublin Core:

| Column style | Meaning |
|---|---|
| `ObjObjectNumberTxt` | a field on the object |
| `ObjPerAssociationRef` | linked records (e.g. people), shown as `Name (Role)` |
| `ObjObjectTitleGrp.TitleTxt` | a field inside a repeating group |

Repeated values are joined with ` || `, as in the other CSVs.

## Keeping private data out

This repository is public, and a MuseumPlus login can see internal information (valuations, locations,
donor contacts, unpublished objects). So the harvester will only write a CSV when **both** are set:

1. **A publication filter** (`MUSEUMPLUS_REQUIRE` variable). Only objects whose publish flag has an
   allowed value are kept, e.g. `ObjPublicationGrp.PublicationVoc=Yes`. Several allowed values can be
   given as `Field=Value1|Value2`.
2. **A field list** ([`fields.txt`](fields.txt)). Only these fields are written.

## Setup

In **Settings → Secrets and variables → Actions**:

| Kind | Name | Value |
|---|---|---|
| Secret | `MUSEUMPLUS_USERNAME` | MuseumPlus API user |
| Secret | `MUSEUMPLUS_PASSWORD` | Its password |
| Secret | `MUSEUMPLUS_API_KEY` | API key, if your host issued one |
| Variable | `MUSEUMPLUS_REQUIRE` | Publication filter (set after the inspect run, below) |
| Variable | `MUSEUMPLUS_API_KEY_HEADER` | Only if zetcom says the key goes in a specific header |

The harvester logs in with the username and password first. If those are refused it tries the
username with the API key as the password. The run log says which worked (never the values).

### First run: inspect

Until `MUSEUMPLUS_REQUIRE` is set, every run is **inspect-only** and commits nothing. Run
**Actions → Weekly MuseumPlus metadata snapshot → Run workflow**, then open the run's summary page. It lists:

- **Publication-like fields and their values**, used to choose `MUSEUMPLUS_REQUIRE`.
- **Every field path** your MuseumPlus uses (names only), used to edit `fields.txt`. Keep only fields
  that already appear on collections.uni.edu.

Then set the variable and edit `fields.txt`. The next run writes the first CSV. To re-check fields later,
run the workflow with **Inspect only** ticked.

## Running locally

```bash
pip install -r requirements.txt
export MUSEUMPLUS_USERNAME=... MUSEUMPLUS_PASSWORD=... MUSEUMPLUS_API_KEY=...
python scripts/museumplus_harvest.py --base-url https://mpcedarfallsuni.zetcom.app --inspect --max-records 500
python scripts/museumplus_harvest.py --base-url https://mpcedarfallsuni.zetcom.app \
    --require "ObjPublicationGrp.PublicationVoc=Yes" --fields-file museumplus/fields.txt \
    --out-dir museumplus/snapshots
```
