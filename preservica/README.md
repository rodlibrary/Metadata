# Preservica Metadata Snapshots

Weekly CSV snapshots of metadata from Rod Library's Preservica
(public site: [uni.access.preservica.com](https://uni.access.preservica.com/)), harvested over OAI-PMH.

| Path | What it is |
|---|---|
| `snapshots/preservica_oai_dc_YYYY-MM-DD.csv` | Dated weekly snapshot (the 4 most recent are kept; older ones stay in git history) |
| `snapshots/latest_oai_dc.csv` | Copy of the newest snapshot (stable link) |

- **Schedule:** Mondays at 13:00 UTC, via [`.github/workflows/preservica-weekly.yml`](../.github/workflows/preservica-weekly.yml).
- **Harvester:** [`scripts/oai_harvest.py`](../scripts/oai_harvest.py), shared with the other collections.
  The CSV format is described in the [main README](../README.md#csv-format).

## Setup (required before the first run)

Unlike ScholarWorks and ArchivesSpace, Preservica's OAI-PMH service is not public. It lives on the
Preservica server (not the public `access.preservica.com` site), and every request needs a login token.
The harvester logs in through Preservica's Access Token API (`/api/accesstoken/login`) and renews the
15-minute token automatically during long harvests.

In the repository, open **Settings → Secrets and variables → Actions** and add:

| Kind | Name | Value |
|---|---|---|
| Variable | `PRESERVICA_OAI_URL` | The OAI-PMH address on your Preservica server, e.g. `https://us.preservica.com/OAI-PMH` |
| Variable | `PRESERVICA_TENANT` | Your Preservica tenant ID (hosted Preservica; ask Preservica support if unsure) |
| Secret | `PRESERVICA_USERNAME` | A Preservica account with read-only access, ideally one made just for harvesting |
| Secret | `PRESERVICA_PASSWORD` | That account's password |

Until these are set, the weekly run is skipped with a warning rather than failing.

**Tip:** the account must not use two-factor authentication, since the harvest runs unattended.

## What's included

Preservica's OAI-PMH covers both folders (structural objects) and assets (information objects). `oai_dc`
is always available: it is built from the record's embedded Dublin Core, or from basic Preservica
fields when there is none. Other formats (e.g. MODS, EAD) are offered only for records that contain that
metadata. Preservica does not support OAI sets, so `--set` and `--list-sets` don't apply.

## Running locally

From the repository root:

```bash
pip install -r requirements.txt
export PRESERVICA_USERNAME=... PRESERVICA_PASSWORD=... PRESERVICA_TENANT=...
python scripts/oai_harvest.py --name preservica --auth preservica \
    --base-url https://us.preservica.com/OAI-PMH --out-dir preservica/snapshots
python scripts/oai_harvest.py --auth preservica \
    --base-url https://us.preservica.com/OAI-PMH --list-formats
```
