"""Command-line interface for humble-bundle-keys."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from humble_bundle_keys import __version__
from humble_bundle_keys.api import ApiError, ApiOptions, ApiScraper, ApiUnsupported
from humble_bundle_keys.auth import (
    DEFAULT_STATE_PATH,
    AuthError,
    AuthOptions,
    get_authenticated_context,
)
from humble_bundle_keys.browser_choice import BrowserChoiceClaimer, BrowserClaimOptions
from humble_bundle_keys.choice import ChoiceClaimer, ChoiceOptions
from humble_bundle_keys.diagnose import DiagnoseOptions, run_diagnose
from humble_bundle_keys.exporter import merge_with_existing, write_csv
from humble_bundle_keys.scraper import KeysScraper, ScrapeOptions

console = Console()

SUBCOMMANDS = {"diagnose", "logout"}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="humble-bundle-keys",
        description=(
            "Extract your Humble Bundle game keys to a CSV. Optionally reveal "
            "(and mark as claimed) any keys you haven't redeemed yet."
        ),
        epilog=(
            "First run will open a browser window so you can log into Humble "
            "(including any 2FA). The session is saved and reused on later runs.\n\n"
            "Subcommands:\n"
            "  diagnose    Capture sanitised debug artifacts (read-only).\n"
            "  logout      Delete the saved session.\n\n"
            "Run `humble-bundle-keys diagnose --help` for subcommand-specific options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("humble-bundle-keys.csv"),
        help="Path to write the CSV. Default: ./humble-bundle-keys.csv",
    )
    p.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Where to store the persisted Humble session. Default: {DEFAULT_STATE_PATH}",
    )
    p.add_argument(
        "--headless",
        dest="headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the scraping browser headlessly. Default: --headless. "
        "First-time login is always headed regardless.",
    )
    p.add_argument(
        "--force-login",
        action="store_true",
        help="Delete any saved session and force a fresh interactive login.",
    )
    p.add_argument(
        "--no-interactive",
        action="store_true",
        help="Fail if the session is missing/expired instead of opening a login browser. "
        "Use for scheduled runs.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Read-only: extract whatever's already visible on the page. "
        "Don't click Redeem, don't expand bundles, don't change anything on Humble's side.",
    )
    p.add_argument(
        "--no-reveal",
        dest="reveal_keys",
        action="store_false",
        help="Don't click Redeem to unmask hidden keys. "
        "(Already-revealed keys are still extracted.)",
    )
    p.add_argument(
        "--expand-bundles",
        action="store_true",
        help="Also click 'GET MY GAMES' and 'REDEEM ALL ON YOUR ACCOUNT' for "
        "bundles still pending claim (e.g. Humble Choice). Off by default "
        "because some bundles require manual game selection.",
    )
    p.add_argument(
        "--polite-delay-ms",
        type=int,
        default=800,
        help="Delay between key reveals, in ms. Default: 800.",
    )
    p.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Stop after N pages. Useful for debugging.",
    )
    p.add_argument(
        "--merge",
        action="store_true",
        help="Merge with any existing CSV at --output instead of overwriting. "
        "Preserves keys revealed in previous runs.",
    )
    p.add_argument(
        "--debug-dir",
        type=Path,
        default=None,
        help="If set, save screenshots and HTML dumps here whenever scraping hits an error.",
    )
    p.add_argument(
        "--no-cache",
        dest="cache",
        action="store_false",
        default=True,
        help="Don't cache /api/v1/order/<gk> responses to "
        "~/.humble-bundle-keys/orders-cache/. Default is to cache for 6 hours, "
        "which dramatically speeds up repeated runs.",
    )
    p.add_argument(
        "--cache-ttl-h",
        type=float,
        default=6.0,
        help="How many hours to keep cached order JSON before re-fetching. "
        "Default: 6.",
    )
    p.add_argument(
        "--clear-cache",
        action="store_true",
        help="Delete the orders cache before running. Useful if you suspect "
        "stale data (e.g. after refunds, gifts, or manual claims on Humble's site).",
    )
    p.add_argument(
        "--scraper",
        choices=["auto", "api", "dom"],
        default="auto",
        help="Scraping strategy. 'auto' (default) tries the JSON API first and "
        "falls back to DOM scraping if the API shape is unrecognised. "
        "'api' forces API only (fails loud on unknown shape). "
        "'dom' forces DOM-only.",
    )
    p.add_argument(
        "--claim-choice",
        action="store_true",
        help="Also claim Humble Choice subscription games via the two-step "
        "POST /humbler/choosecontent + POST /humbler/redeemkey flow. "
        "OFF by default — this mutates state on Humble's side (consumes a "
        "Choice slot per game). See docs/CHOICE_CLAIM_SPEC.md.",
    )
    p.add_argument(
        "--browser-claim",
        action="store_true",
        help="Drive each /membership/<slug> page in the actual browser: "
        "click each unclaimed game card, click 'GET GAME ON STEAM' in the "
        "modal, wait for the key, extract it. Slower than --claim-choice but "
        "handles cases the API can't (legacy 'pick N of M' months where the "
        "menu of available games never appears in tpks). OFF by default; "
        "MUTATES state.",
    )
    p.add_argument(
        "--membership-only",
        type=str,
        default=None,
        metavar="SLUG",
        help="When used with --browser-claim, restrict claiming to this one "
        "membership slug (e.g. 'june-2025' or 'december-2025'). Useful for "
        "surgical testing.",
    )
    p.add_argument(
        "--max-claims",
        type=int,
        default=100,
        help="Hard cap on Choice claims per run (when --claim-choice or "
        "--browser-claim is set). Default: 100.",
    )
    p.add_argument(
        "--claim-delay-s",
        type=float,
        default=3.0,
        help="Polite delay between Choice claims, in seconds. Default: 3.0.",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before running --claim-choice. "
        "Use only in non-interactive contexts where you've already verified "
        "the dry-run output.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase CONSOLE log verbosity (-v info, -vv debug). The on-disk "
        "run log always captures DEBUG.",
    )
    p.add_argument(
        "--log-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"Write the full DEBUG-level log here. Default: a timestamped "
        f"file under {DEFAULT_LOG_DIR} (last {LOG_RETENTION} retained).",
    )
    p.add_argument(
        "--no-log-file",
        dest="log_file_enabled",
        action="store_false",
        default=True,
        help="Disable on-disk run logging entirely (console only).",
    )
    p.add_argument("--version", action="version", version=f"humble-bundle-keys {__version__}")
    return p


DEFAULT_LOG_DIR = Path.home() / ".humble-bundle-keys" / "runs"
LOG_RETENTION = 20  # keep this many most-recent run logs


def _prune_old_logs(log_dir: Path, keep: int = LOG_RETENTION) -> None:
    """Delete all but the ``keep`` most-recent run logs in ``log_dir``."""
    try:
        files = sorted(
            log_dir.glob("run-*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except Exception:
            pass


def _setup_logging(
    verbosity: int,
    *,
    log_file: Path | None = None,
    enable_run_log: bool = True,
) -> Path | None:
    """Wire up Rich console handler + (optional) DEBUG file handler.

    Returns the resolved log file path so the caller can mention it in the
    final summary, or None if file logging was disabled.
    """
    console_level = logging.WARNING
    if verbosity == 1:
        console_level = logging.INFO
    elif verbosity >= 2:
        console_level = logging.DEBUG

    handlers: list[logging.Handler] = [
        RichHandler(console=console, rich_tracebacks=True, show_path=False),
    ]
    handlers[0].setLevel(console_level)

    resolved_log_file: Path | None = None
    if log_file is not None:
        resolved_log_file = log_file
    elif enable_run_log:
        DEFAULT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        resolved_log_file = DEFAULT_LOG_DIR / f"run-{ts}.log"
        _prune_old_logs(DEFAULT_LOG_DIR)

    if resolved_log_file is not None:
        try:
            resolved_log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(resolved_log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            handlers.append(fh)
        except Exception as e:
            console.print(f"[yellow]Could not open log file {resolved_log_file}: {e}[/]")
            resolved_log_file = None

    logging.basicConfig(
        level=logging.DEBUG,  # root captures everything; handlers filter
        format="%(message)s",
        datefmt="[%X]",
        handlers=handlers,
        force=True,
    )
    return resolved_log_file


def _run_scraper(
    mode: str,
    context,
    api_opts: ApiOptions,
    scrape_opts: ScrapeOptions,
):
    """Run the chosen scraper. ``mode`` is 'auto', 'api', or 'dom'.

    'auto' tries API first; on ApiUnsupported (unknown shape) or ApiError it
    falls back to the DOM scraper. 'api' raises on failure. 'dom' skips API.

    Returns (rows, stats, api_scraper_or_none). The API scraper is returned
    when used (so callers can access ``.orders`` for the Choice claim flow);
    None when we fell back to DOM.
    """
    if mode in ("auto", "api"):
        try:
            console.print("[cyan]Trying JSON API path...[/]")
            api = ApiScraper(context, api_opts)
            rows, stats = api.scrape()
            cache_msg = ""
            if api_opts.cache is not None:
                h = api_opts.cache.hits
                m = api_opts.cache.misses
                cache_msg = (
                    f" [dim](cache: {h} served from cache, "
                    f"{m} fetched fresh)[/]"
                )
            console.print(
                f"[green]API path succeeded[/] — {stats.total_rows} rows from "
                f"{stats.bundles_processed} orders{cache_msg}"
            )
            return rows, stats, api
        except (ApiUnsupported, ApiError) as e:
            if mode == "api":
                console.print(f"[red]API path failed and --scraper=api:[/] {e}")
                raise
            console.print(
                f"[yellow]API path unavailable ({e}); falling back to DOM scraping.[/]"
            )

    console.print("[cyan]Using DOM scraper...[/]")
    dom = KeysScraper(context, scrape_opts)
    rows, stats = dom.scrape()
    return rows, stats, None


def _print_summary(stats, csv_path: Path, n_written: int) -> None:
    table = Table(title="Humble keys — summary", show_lines=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold")
    table.add_row("Rows written", str(n_written))
    table.add_row("Keys revealed this run", str(stats.keys_revealed))
    table.add_row("Keys already revealed", str(stats.keys_already_revealed))
    silent = getattr(stats, "keys_silent_no_response", 0)
    if silent:
        table.add_row("Reveals that returned no key", str(silent))
    table.add_row("Bundles processed", str(stats.bundles_processed))
    skipped = getattr(stats, "skipped_structural", 0)
    if skipped:
        table.add_row("Pre-skipped (vendor/keyless)", str(skipped))
    table.add_row("Errors", str(len(stats.errors)))
    console.print(table)
    console.print(f"[green]CSV:[/] {csv_path.resolve()}")
    if skipped:
        from collections import Counter

        skipped_details = getattr(stats, "skipped_structural_tpks", []) or []
        skip_cats: Counter[str] = Counter()
        for _title, _mn, cat in skipped_details:
            skip_cats[cat] += 1
        skip_advice = {
            "softwarebundle": "Vendor-specific flow — claim manually on vendor's site",
            "voucher": "Store credit / gift card — not a Steam-shaped key",
            "keyless": "Epic keyless — added directly to Epic library, no key exists",
            "freegame": (
                "Free-game promo — uses a different endpoint; "
                "file a feature request with the machine_name if you need these"
            ),
        }
        console.print(f"\n[dim]{skipped} entries pre-skipped (not auto-redeemable):[/]")
        for cat, n in skip_cats.most_common():
            hint = skip_advice.get(cat, "")
            console.print(f"  [dim]• {cat}: {n} — {hint}[/]")
        # Show freegame entries specifically since they're potentially supportable
        freegame_entries = [
            (title, mn) for title, mn, cat in skipped_details if cat == "freegame"
        ]
        if freegame_entries:
            console.print(
                "\n[dim]Freegame entries (potentially supportable in a future version):[/]"
            )
            for title, mn in freegame_entries[:5]:
                console.print(f"  [dim]• {title!r}  ({mn})[/]")
            if len(freegame_entries) > 5:
                console.print(f"  [dim]…and {len(freegame_entries) - 5} more[/]")
    if silent:
        # Categorize the silent-no-keys so users see at a glance which are
        # actually fixable vs structurally manual.
        from collections import Counter

        from humble_bundle_keys.choice import categorize_keytype

        details = getattr(stats, "silent_no_response_tpks", []) or []
        cats: Counter[str] = Counter()
        for _title, tpk_mn, _order_mn in details:
            cats[categorize_keytype(tpk_mn)] += 1

        console.print(
            f"\n[yellow]{silent} reveal calls succeeded but didn't return a key.[/]"
        )
        if cats:
            console.print("[yellow]By category:[/]")
            # Friendly per-category guidance
            advice = {
                "choice": "Humble Choice — re-run with [bold]--claim-choice[/]",
                "monthly": (
                    "Legacy Humble Monthly — likely already-gifted or "
                    "expired upstream; usually no fix"
                ),
                "freegame": "Free-game promo — different endpoint, not yet supported",
                "keyless": (
                    "Epic 'keyless' delivery — there is no key "
                    "(added directly to Epic library)"
                ),
                "softwarebundle": (
                    "Audio/software vendor bundle — vendor-specific flow, "
                    "not yet supported"
                ),
                "voucher": "Store credit voucher — not a Steam-shaped key",
                "bundle": (
                    "Bundle key — should have worked via redeemkey; "
                    "investigate the order"
                ),
                "other": "Unrecognized shape — paste the line below into a GitHub issue",
            }
            for cat, n in cats.most_common():
                hint = advice.get(cat, "")
                console.print(f"  [yellow]• {cat}: {n}[/]  [dim]— {hint}[/]")

            # Print exact re-run command for choice items
            if "choice" in cats:
                from humble_bundle_keys.browser_choice import derive_membership_slug

                choice_slugs: set[str] = set()
                for _title, _tpk_mn, order_mn in details:
                    if categorize_keytype(_tpk_mn) == "choice":
                        slug = derive_membership_slug({"machine_name": order_mn})
                        if slug:
                            choice_slugs.add(slug)

                console.print("\n[green]To claim these, re-run:[/]")
                if len(choice_slugs) == 1:
                    slug = next(iter(choice_slugs))
                    console.print(
                        f"  [bold]humble-bundle-keys --claim-choice "
                        f"--membership-only {slug}[/]"
                    )
                elif len(choice_slugs) <= 5:
                    # Show per-slug commands
                    console.print(
                        "  [bold]humble-bundle-keys --claim-choice[/]"
                    )
                    console.print("\n[dim]Or target specific months:[/]")
                    for slug in sorted(choice_slugs):
                        console.print(
                            f"  [dim]humble-bundle-keys --claim-choice "
                            f"--membership-only {slug}[/]"
                        )
                else:
                    # Too many slugs — just show the general command
                    console.print(
                        "  [bold]humble-bundle-keys --claim-choice[/]"
                    )
        n_show = min(10, len(details))
        if n_show:
            console.print(
                f"\n[dim]First {n_show} silent-no-key tpks "
                "(title | tpk.machine_name | order.machine_name):[/]"
            )
            for title, tpk_mn, order_mn in details[:n_show]:
                console.print(f"  [dim]• {title!r}  |  {tpk_mn}  |  {order_mn}[/]")
            if len(details) > n_show:
                console.print(f"  [dim]…and {len(details) - n_show} more[/]")
    if stats.errors:
        n_show = min(5, len(stats.errors))
        console.print(
            f"\n[yellow]{len(stats.errors)} non-fatal errors occurred. "
            f"Showing first {n_show}:[/]"
        )
        for err in stats.errors[:n_show]:
            console.print(f"  [yellow]•[/] {err}")
        if len(stats.errors) > n_show:
            console.print(
                f"  [dim]…and {len(stats.errors) - n_show} more. "
                "Re-run with -v to log all of them.[/]"
            )


def _build_diagnose_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="humble-bundle-keys diagnose",
        description=(
            "Capture sanitised diagnostic artifacts from your Humble keys page. "
            "Read-only — never clicks Redeem, never modifies anything on Humble's side. "
            "Produces a safe-to-share.zip you can attach to a GitHub issue."
        ),
    )
    p.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("./humble-diagnose"),
        help="Where to write the diagnose-<timestamp>/ directory. Default: ./humble-diagnose",
    )
    p.add_argument(
        "-p",
        "--pages",
        type=int,
        default=2,
        help="Number of pages of the keys table to capture. Default: 2.",
    )
    p.add_argument(
        "--headless",
        dest="headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run the browser headlessly. Default: --headless. "
        "Forced off when --membership-page is set (which needs interaction).",
    )
    p.add_argument(
        "--membership-page",
        type=str,
        default=None,
        metavar="SLUG",
        help="Also visit /membership/<SLUG> (e.g. 'march-2026') in a HEADED "
        "browser and capture the XHRs that result when you click 'Get Game on "
        "Steam'. Used to discover the Choice claim flow's API shape — see "
        "docs/CHOICE_CLAIM_SPEC.md.",
    )
    p.add_argument(
        "--membership-wait-s",
        type=int,
        default=600,
        help="How long to keep the membership browser open before timing out. "
        "Default: 600 (10 minutes).",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def _build_logout_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="humble-bundle-keys logout",
        description="Delete the saved Humble session so the next run forces a fresh login.",
    )
    p.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to delete. Default: {DEFAULT_STATE_PATH}",
    )
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


def _diagnose_main(argv: list[str]) -> int:
    args = _build_diagnose_parser().parse_args(argv)
    _setup_logging(args.verbose)
    opts = DiagnoseOptions(
        output_dir=args.output_dir,
        pages=args.pages,
        headless=args.headless,
        membership_page=args.membership_page,
        membership_wait_s=args.membership_wait_s,
    )
    try:
        bundle = run_diagnose(opts)
    except AuthError as e:
        console.print(f"[red]Auth error:[/] {e}")
        return 2
    console.print(
        f"\n[green]Done.[/] Sanitised bundle: [bold]{bundle}[/]\n"
        "[yellow]The 'raw/' folder next to it contains your real keys — do NOT share it.[/]"
    )
    return 0


def _logout_main(argv: list[str]) -> int:
    args = _build_logout_parser().parse_args(argv)
    _setup_logging(args.verbose)
    path: Path = args.storage_state
    if not path.exists():
        console.print(f"[yellow]No saved session at[/] {path}")
        return 0
    path.unlink()
    console.print(f"[green]Removed[/] {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Subcommand dispatch: `humble-bundle-keys diagnose ...`, `humble-bundle-keys logout ...`
    if argv and argv[0] in SUBCOMMANDS:
        sub = argv[0]
        rest = argv[1:]
        if sub == "diagnose":
            return _diagnose_main(rest)
        if sub == "logout":
            return _logout_main(rest)

    parser = _build_parser()
    args = parser.parse_args(argv)
    log_file = _setup_logging(
        args.verbose,
        log_file=args.log_file,
        enable_run_log=args.log_file_enabled,
    )
    if log_file:
        logging.getLogger(__name__).info("Run log: %s", log_file)

    # Headless Chromium gets blocked by Cloudflare's bot management on
    # /humbler/* POSTs. If the user is going to mutate state (reveal keys
    # or claim choice content), force headed mode and surface the reason.
    will_mutate = (
        not args.dry_run
        and (
            args.reveal_keys
            or args.claim_choice
            or args.expand_bundles
            or args.browser_claim
        )
    )
    effective_headless = args.headless
    if will_mutate and args.headless:
        console.print(
            "[yellow]Note:[/] Forcing [bold]--no-headless[/] for this run. "
            "Humble's Cloudflare WAF rejects POSTs from headless Chromium, "
            "so the reveal/claim phase needs a visible browser window. "
            "Pass [bold]--no-reveal --dry-run[/] to keep headless."
        )
        effective_headless = False

    auth_opts = AuthOptions(
        storage_state_path=args.storage_state,
        headless=effective_headless,
        no_interactive=args.no_interactive,
        force_login=args.force_login,
    )
    scrape_opts = ScrapeOptions(
        reveal_keys=args.reveal_keys and not args.dry_run,
        expand_bundles=args.expand_bundles and not args.dry_run,
        dry_run=args.dry_run,
        polite_delay_ms=args.polite_delay_ms,
        max_pages=args.max_pages,
        debug_dir=args.debug_dir,
    )

    # Order cache. Cuts ~2 minutes off a 195-order run on a warm cache.
    from humble_bundle_keys._orders_cache import OrderCache

    cache: OrderCache | None = None
    if args.cache:
        cache = OrderCache(ttl_s=int(args.cache_ttl_h * 3600))
    if args.clear_cache:
        # Use a temporary always-enabled cache to clear, even if --no-cache.
        n = OrderCache().clear_all()
        if n:
            console.print(f"[yellow]Cleared {n} cached order file(s).[/]")

    api_opts = ApiOptions(
        reveal_keys=args.reveal_keys and not args.dry_run,
        dry_run=args.dry_run,
        polite_delay_ms=args.polite_delay_ms,
        cache=cache,
    )

    with sync_playwright() as p:
        try:
            browser, context = get_authenticated_context(p, auth_opts)
        except AuthError as e:
            console.print(f"[red]Auth error:[/] {e}")
            return 2

        try:
            rows, stats, api = _run_scraper(args.scraper, context, api_opts, scrape_opts)

            # Optional second phase: claim Humble Choice subscription games.
            if args.claim_choice:
                if api is None:
                    console.print(
                        "[red]--claim-choice requires the API scraper "
                        "(it needs the order JSON it fetched). Re-run "
                        "with --scraper api or --scraper auto.[/]"
                    )
                else:
                    extra_rows = _run_claim_choice(context, api.orders, args)
                    rows.extend(extra_rows)
                    stats.keys_revealed += len(extra_rows)

            # Optional third phase: drive each membership page in the browser.
            # Catches games the API can't see (legacy "pick N of M" months
            # where the menu of available games never appears in tpks).
            if args.browser_claim:
                if api is None:
                    console.print(
                        "[red]--browser-claim requires the API scraper "
                        "(uses its order list to derive membership slugs). "
                        "Re-run with --scraper api or --scraper auto.[/]"
                    )
                else:
                    extra_rows = _run_browser_claim(context, api.orders, args)
                    rows.extend(extra_rows)
                    stats.keys_revealed += len(extra_rows)
        finally:
            context.close()
            browser.close()

    if args.merge:
        rows = merge_with_existing(rows, args.output)

    n_written = write_csv(rows, args.output)
    _print_summary(stats, args.output, n_written)
    if log_file:
        console.print(f"[dim]Run log:[/] [dim]{log_file}[/]")
    return 0


def _run_browser_claim(context, orders, args) -> list:
    """Drive each /membership/<slug> page in the actual browser."""
    from humble_bundle_keys.browser_choice import derive_membership_slug

    candidate_slugs = [
        s
        for s in (
            derive_membership_slug(o.get("product") or {})
            for o in orders
            if (o.get("product") or {}).get("category") == "subscriptioncontent"
        )
        if s
    ]
    candidate_slugs = sorted(set(candidate_slugs))
    if args.membership_only:
        only = args.membership_only.strip("/")
        if only not in candidate_slugs:
            console.print(
                f"[red]--membership-only {only!r} matched no order. "
                f"Available slugs: {', '.join(candidate_slugs[:10])}"
                + (f" (and {len(candidate_slugs) - 10} more)" if len(candidate_slugs) > 10 else "")
                + "[/]"
            )
            return []
        candidate_slugs = [only]
    if not candidate_slugs:
        console.print("[yellow]--browser-claim: no Choice/Monthly orders found.[/]")
        return []

    console.print(
        f"\n[bold]--browser-claim:[/] [cyan]{len(candidate_slugs)}[/] membership "
        f"page(s) to walk. Will claim up to [cyan]{args.max_claims}[/] games "
        f"(--max-claims={args.max_claims}, "
        f"--claim-delay-s={args.claim_delay_s})."
    )
    console.print(
        "[yellow]This MUTATES STATE on Humble's side — "
        "each successful claim consumes a Choice slot for that game. "
        "You'll see the browser navigate to each membership page; "
        "the script will click cards itself.[/]"
    )
    if not args.yes:
        try:
            answer = input("Type 'yes' to proceed: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            console.print("[yellow]Aborted by user.[/]")
            return []

    claimer = BrowserChoiceClaimer(
        context,
        BrowserClaimOptions(
            dry_run=args.dry_run,
            polite_delay_s=args.claim_delay_s,
            max_claims=args.max_claims,
            only_slug=args.membership_only,
        ),
    )
    result = claimer.claim_all(orders)
    n_success = len(result.revealed_keys)
    n_already = sum(1 for a in result.attempts if a.already_claimed)
    # Dry-run skips are bookkept as "not success" but aren't real failures.
    n_dryrun = sum(
        1 for a in result.attempts if not a.success and "dry-run" in (a.error or "")
    )
    n_real_fail = sum(
        1
        for a in result.attempts
        if not a.success and not a.already_claimed and "dry-run" not in (a.error or "")
    )
    n_exhausted = sum(1 for a in result.attempts if a.key_exhausted)
    if args.dry_run:
        console.print(
            f"[green]Browser claim complete (dry-run):[/] "
            f"would have attempted {n_dryrun + n_already} cards "
            f"({n_already} already-claimed, {n_dryrun} would-be-clicks). "
            f"No state was changed."
        )
    else:
        parts = [f"{n_success} keys revealed"]
        if n_already:
            parts.append(f"{n_already} already-claimed")
        if n_exhausted:
            parts.append(f"{n_exhausted} keys exhausted (no stock)")
        fail_minus_exhausted = n_real_fail - n_exhausted
        if fail_minus_exhausted > 0:
            parts.append(f"{fail_minus_exhausted} failures")
        console.print(
            "[green]Browser claim complete:[/] " + ", ".join(parts) + "."
        )
    return result.revealed_keys


def _run_claim_choice(context, orders, args) -> list:
    """Drive the Choice claim phase: confirm, claim, return GameKey rows."""
    from humble_bundle_keys.choice import looks_like_choice_order, unclaimed_choice_tpks

    candidate_count = sum(
        len(unclaimed_choice_tpks(o)) for o in orders if looks_like_choice_order(o)
    )
    if candidate_count == 0:
        console.print("[yellow]--claim-choice: no unclaimed Choice games found.[/]")
        return []

    cap = min(candidate_count, args.max_claims)
    console.print(
        f"\n[bold]--claim-choice:[/] [cyan]{candidate_count}[/] unclaimed Choice "
        f"games found. Will claim up to [cyan]{cap}[/] this run "
        f"(--max-claims={args.max_claims})."
    )
    console.print(
        "[yellow]This MUTATES STATE on Humble's side — "
        "each claim consumes a Choice slot for that game.[/]"
    )
    if not args.yes:
        try:
            answer = input("Type 'yes' to proceed: ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "yes":
            console.print("[yellow]Aborted by user.[/]")
            return []

    claimer = ChoiceClaimer(
        context,
        ChoiceOptions(
            dry_run=args.dry_run,
            polite_delay_s=args.claim_delay_s,
            max_claims=args.max_claims,
        ),
    )
    result = claimer.claim_all(orders)
    console.print(
        f"[green]Choice claim complete:[/] "
        f"{len(result.revealed_keys)} keys revealed, "
        f"{sum(1 for a in result.attempts if not a.success)} failures."
    )
    return result.revealed_keys


if __name__ == "__main__":
    sys.exit(main())
