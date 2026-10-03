# Choice claim fixtures

Captured 2026-05-04 by running:

```
humble-bundle-keys diagnose --membership-page march-2026
```

…then clicking "GET GAME ON STEAM" on Zero Hour in the resulting browser
window. Every request and response was sanitised by the diagnose
sanitiser before landing here, so:

- The actual Steam key is `REDACTED-KEY`
- The order's `gamekey` URL parameter is `REDACTED-GAMEKEY`
- Auth-bearing headers (Cookie, Set-Cookie, Authorization, CSRF-Prevention-Token, X-CSRF-Token, X-XSRF-Token) were stripped before write

These four files together represent the complete wire trace of one
"Get Game on Steam" click. See `docs/CHOICE_CLAIM_SPEC.md` for the
contract derived from them.

| file | role |
|---|---|
| `choosecontent_request_response.json` | Step 1 — `POST /humbler/choosecontent` registers the user's choice |
| `redeemkey_request_response.json` | Step 2 — `POST /humbler/redeemkey` reveals the actual key |
| `analytics_tile_click.json` | Telemetry on card click — ignore |
| `analytics_get_game.json` | Telemetry on "Get Game on Steam" click — ignore |
