# Real-account test runbook

This is the procedure for getting `humble-bundle-keys` working against your real
account for the first time. Selectors and API parsers are written speculatively
based on screenshots — this run validates them against the live site and
produces fixtures we can write regression tests against.

The whole thing is **read-only**. No keys are revealed, no buttons are clicked.

## Prerequisites

```bash
cd /path/to/humble-bundle-key-extractor
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Step 1 — log in once

A real Chrome window will open. Log into Humble normally (including the
2FA email if asked). The window will close itself when /home/keys is reachable.

```bash
humble-bundle-keys --dry-run --max-pages 1 -vv
```

Expected outcome:

* A browser window opens, you log in, it closes.
* `~/.humble-bundle-keys/storage_state.json` now exists.
* The CLI prints a summary table — **check it carefully**:
  - `Rows written` should be > 0
  - `Errors` should be 0 (or low and benign)

If `Rows written` is 0, something is wrong with the row selector. Skip to
Step 3 and send back the diagnose bundle.

## Step 2 — try a couple more pages, still read-only

```bash
humble-bundle-keys --dry-run --max-pages 3 -v -o /tmp/sample.csv
```

Then open `/tmp/sample.csv` and eyeball it. You're looking for:

* Game titles look right (no garbage / no `(unknown)` rows).
* `platform` column is mostly populated with `steam`, `gog`, etc. — not blank.
* `bundle_name` and `bundle_date` are populated for keys that came from a
  named bundle.
* If you have any already-revealed keys, the `key` column should contain
  the real key string.
* `redeemed_on_humble` matches what you see on the site.

If anything is off, go to Step 3.

## Step 3 — capture diagnostic artifacts

This is the command that produces a sanitised bundle for me:

```bash
humble-bundle-keys diagnose --pages 3 -v
```

It will create:

```
./humble-diagnose/diagnose-<timestamp>/
  raw/                  ← contains your real keys; do NOT share
  safe-to-share/        ← sanitised; safe to attach to GitHub issues
  safe-to-share.zip     ← convenience zip of the above
```

**Verify the sanitiser before sharing.** Open the zip, spot-check a couple of
the HTML and JSON files. Real keys (anything looking like `XXXXX-XXXXX-XXXXX`)
should appear as `REDACTED-KEY`. Email addresses should appear as
`REDACTED@example.com`. If you see anything sensitive that wasn't redacted,
**don't share it** — file an issue describing what slipped through.

If the sanitiser looks correct, attach `safe-to-share.zip` to the GitHub issue
or send it back here.

## Step 4 — first real reveal (only after Steps 1–3 look healthy)

This is the first run that actually changes things on Humble's side. Do this
after the dry-run output looks correct.

```bash
# Reveal+claim everything, no bundle expansion yet.
humble-bundle-keys --merge -o ~/Documents/humble-bundle-keys.csv -v
```

`--merge` means subsequent runs won't lose keys you've already revealed even
if Humble's UI later hides them.

Optional follow-up — auto-claim Humble Choice / monthly bundles via the
"REDEEM ALL ON YOUR ACCOUNT" button:

```bash
humble-bundle-keys --expand-bundles --merge -o ~/Documents/humble-bundle-keys.csv -v
```

This is opt-in because some legacy Choice tiers require you to *pick* games
manually rather than redeem all.

## Troubleshooting matrix

| symptom | likely cause | next step |
| --- | --- | --- |
| `Auth error: Login appeared to succeed but /home/keys still redirects to login` | Region / age gate prompt blocking | Open keys page in normal browser, satisfy prompt, then `humble-bundle-keys logout` and re-run |
| `Rows written: 0` | Row selector miss | Run `humble-bundle-keys diagnose` and share `safe-to-share.zip` |
| `Rows written` looks right but `platform` is blank everywhere | Platform-icon selector miss | Same as above |
| Lots of `Errors` in the summary, scraping continues | Per-row extractors throwing | Run with `-vv --debug-dir ./debug`; share contents of `./debug/` (after eyeballing for keys) |
| Browser window doesn't open | `playwright install chromium` missing | Re-run that command |
| Session keeps expiring | Cookie tied to IP — VPN / network change | `humble-bundle-keys logout && humble-bundle-keys --force-login` |

## What gets fixed from your diagnose bundle

When I receive a `safe-to-share.zip` I will:

1. Extract the HTML pages and use them as fixtures in `tests/fixtures/`.
2. Inspect what the row, key, redeem, and pagination selectors actually
   match against and update them in `humble_bundle_keys/scraper.py`.
3. Inspect the captured `/api/v1/*.json` files to confirm or correct the
   field shapes the API scraper assumes (`humble_bundle_keys/api.py`).
4. Add a `pytest` test that loads the fixture and asserts the expected
   number of rows / fields, so future Humble redesigns get caught
   automatically.

## Step 5 — discovering the Humble Choice "Get Game on Steam" flow

Read [`docs/CHOICE_CLAIM_SPEC.md`](../docs/CHOICE_CLAIM_SPEC.md) first for
context. The short version: there's a second claim flow on Humble's site —
the one where you click "GET MY GAMES" on a Choice month, then click each
game card, then click "GET GAME ON STEAM" — that the tool currently doesn't
know how to drive. The spec lays out what we know, what we don't, and how
we plan to close the gap.

To capture that flow on the wire, run:

```bash
humble-bundle-keys diagnose --membership-page march-2026
```

(Replace `march-2026` with any month-slug you have unclaimed games in.)

This forces a HEADED browser (you'll see the window) and runs in two phases:

1. The usual /home/keys capture (1–2 pages).
2. The same browser tab navigates to `/membership/<slug>` and the script
   **stops automating**. You will see a banner in the terminal telling you
   what to do:
   - Click **one** game card you haven't claimed yet.
   - Click **GET GAME ON STEAM** in the modal.
   - Wait the 3–10 seconds for the modal to update with the key.
   - **Press ENTER in the terminal** to signal completion.
     (Closing the tab also works as a backup, but Enter is the most reliable
     signal — Playwright's tab-close detection is flaky in headed mode.)

Everything that flies over the network during that interaction is captured
under `raw/membership/` and `raw/api/membership/`. The sanitiser then
redacts the resulting key (it'll appear as `REDACTED-KEY` in
`safe-to-share/`) but **the API endpoint URL and request shape are
preserved**, which is exactly what we need to implement the flow.

Send the resulting `safe-to-share.zip` back and I'll pin down the API
contract in `humble_bundle_keys/api.py`.
