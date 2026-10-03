"""POST through the actual browser via ``page.evaluate(fetch)``.

Why this exists
---------------
Humblebundle.com sits behind Cloudflare. Cloudflare fingerprints the
HTTP/2 + TLS handshake of incoming requests, and Playwright's
``APIRequestContext`` (``context.request.post(...)``) uses a stack that
doesn't match a real Chrome — Cloudflare flags it as bot traffic and
returns a 403 challenge page for state-changing endpoints like
``/humbler/redeemkey`` and ``/humbler/choosecontent``.

GETs (e.g. ``/api/v1/orders``) get through fine; only POSTs are
challenged. The fix is to route POSTs through the actual browser. The
caller keeps a ``Page`` navigated to humblebundle.com, we feed the
request to ``page.evaluate("fetch(...)")``, and Cloudflare sees a real
Chrome request originating from the right origin with full session
cookies attached — exactly what the SPA itself does, exactly the path
our 2026-05-04 diagnose capture confirmed works end-to-end.

We deliberately do NOT use ``credentials: "include"`` — same-origin
fetches send cookies automatically, and ``include`` triggers stricter
CORS / SameSite handling we don't need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Page


@dataclass
class BrowserFetchResponse:
    status: int
    body: Any
    raw_text: str = ""


# JavaScript snippet evaluated inside the page. Single source of truth so
# tests can assert on the shape we send.
FETCH_SCRIPT = """
async ({ url, method, body, headers }) => {
  // Same-origin POST. Cookies attach automatically; the include flag is
  // intentionally omitted to keep CORS/SameSite handling permissive.
  const resp = await fetch(url, {
    method: method || 'POST',
    headers: headers || {},
    body: body,
  });
  const text = await resp.text();
  let parsed = null;
  try { parsed = JSON.parse(text); } catch (e) { /* leave parsed = null */ }
  return {
    status: resp.status,
    raw_text: text,
    body: parsed,
  };
}
""".strip()


def post_form_in_browser(
    page: Page,
    url: str,
    body: str,
    *,
    referer: str | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout_ms: int = 20_000,
) -> BrowserFetchResponse:
    """POST a form-encoded body via fetch() inside ``page``'s real Chrome.

    The ``page`` must already be on a humblebundle.com URL so that ``fetch``
    treats the request as same-origin (cookies attach automatically). The
    caller is responsible for keeping the page navigated.
    """
    headers: dict[str, str] = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "application/json, text/javascript, */*; q=0.01",
        "x-requested-with": "XMLHttpRequest",
    }
    if referer:
        # The browser sets Referer automatically based on the page's URL,
        # but we can reinforce it via the meta-tag-equivalent header where
        # the spec allows. (Most browsers will silently ignore an explicit
        # Referer header set via fetch, falling back to the page's origin.)
        headers["referer"] = referer
    if extra_headers:
        headers.update(extra_headers)

    page.set_default_timeout(timeout_ms)
    raw = page.evaluate(
        FETCH_SCRIPT,
        {"url": url, "method": "POST", "body": body, "headers": headers},
    )
    return BrowserFetchResponse(
        status=int(raw.get("status") or 0),
        body=raw.get("body"),
        raw_text=raw.get("raw_text") or "",
    )
