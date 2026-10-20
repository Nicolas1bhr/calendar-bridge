"""Command-line interface.

Everything that changes synchronization state takes the same lock as the daemon's cycles, so
CLI commands and the daemon never interleave. Read-only commands (status, health, doctor,
listings) never take the lock and never write to either provider.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import click

from calendar_bridge import __version__
from calendar_bridge.app import App, LockBusy, cycle_lock, daemon_lock, open_app
from calendar_bridge.clock import from_iso, seconds_since
from calendar_bridge.config import Settings, load_settings
from calendar_bridge.crypto import MISSING_KEY, generate_key, key_file_is_empty, load_keys, read_key_file
from calendar_bridge.db.backup import create_backup, list_backups, restore_backup
from calendar_bridge.db.repo import ITEM_STATES
from calendar_bridge.errors import AuthError, BridgeError, TransientError
from calendar_bridge.logs import setup_logging
from calendar_bridge.model import timeutil
from calendar_bridge.model.canonical import CanonicalEvent, group_values
from calendar_bridge.sync.context import HOME_TIMEZONE, MappingContext

if TYPE_CHECKING:
    from calendar_bridge.db.repo import CalendarRow
    from calendar_bridge.providers.google.auth import ClientSecrets
    from calendar_bridge.providers.icloud.client import Discovery
    from calendar_bridge.sync.initial import InitialPlan

ICLOUD_APP_PASSWORD = re.compile(r"[a-z]{16}")
LIVE_STATES = ("active", "pending_initial", "paused")
ISSUES_URL = "https://github.com/Nicolas1bhr/calendar-bridge/issues"


def icloud_app_password(raw: str) -> str | None:
    """Apple's app-specific passwords are 16 letters, shown as abcd-efgh-ijkl-mnop; None otherwise."""
    letters = re.sub(r"[\s-]", "", raw).lower()
    if not ICLOUD_APP_PASSWORD.fullmatch(letters):
        return None
    return "-".join(letters[i : i + 4] for i in range(0, 16, 4))


def _is_icloud(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "icloud.com" or host.endswith(".icloud.com")


class BridgeGroup(click.Group):
    """Turns every failure into a short message: expected ones explain what to do, anything else
    is reported as a bug (the traceback is logged with --log-level DEBUG)."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (click.ClickException, click.Abort, click.exceptions.Exit):
            raise
        except LockBusy as exc:
            raise click.ClickException(str(exc)) from exc
        except AuthError as exc:
            raise click.ClickException(f"authorization failed: {exc}") from exc
        except TransientError as exc:
            who = {"google": "Google", "icloud": "iCloud"}.get(exc.provider, "The provider")
            raise click.ClickException(
                f"{who} could not be reached or failed temporarily ({exc}). Check the internet connection "
                "and try again in a minute."
            ) from exc
        except (BridgeError, KeyError) as exc:
            raise click.ClickException(str(exc).strip("'")) from exc
        except sqlite3.Error as exc:
            raise click.ClickException(f"database error: {exc}") from exc
        except OSError as exc:
            raise click.ClickException(f"{exc.strerror or exc}: {exc.filename or ''}".rstrip(": ")) from exc
        except Exception as exc:
            logging.getLogger("calendar_bridge.cli").debug("unexpected error", exc_info=True)
            raise click.ClickException(
                f"unexpected error ({type(exc).__name__}: {exc}). Run the command again with --log-level DEBUG "
                f"to see the details, and please report it at {ISSUES_URL}"
            ) from exc


class State:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._app: App | None = None

    @property
    def app(self) -> App:
        if self._app is None:
            self._app = open_app(self.settings)
        return self._app

    def close(self) -> None:
        if self._app is not None:
            self._app.close()
            self._app = None


pass_state = click.make_pass_decorator(State)


def _json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _table(rows: list[list[Any]], headers: list[str]) -> None:
    text_rows = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [max(len(h), *(len(r[i]) for r in text_rows)) if text_rows else len(h) for i, h in enumerate(headers)]
    click.echo("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    click.echo("  ".join("-" * w for w in widths))
    for row in text_rows:
        click.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)))


def _ago(ts: str | None) -> str:
    seconds = seconds_since(ts, dt.datetime.now(dt.UTC))
    if seconds is None:
        return "never"
    if seconds < 120:
        return f"{int(seconds)}s ago"
    if seconds < 7200:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def _credentials_changed(app: App) -> None:
    with app.db.tx():
        app.repo.set_meta("credentials_changed_at", app.repo.now())


def _lock(app: App, timeout: float | None = None) -> Any:
    return cycle_lock(app.settings, timeout=timeout if timeout is not None else app.settings.sync_interval * 3)


def _local_time(app: App, ts: str | None) -> str:
    if not ts:
        return "-"
    return timeutil.utc_to_local(from_iso(ts), app.ctx.default_tz).strftime("%Y-%m-%d %H:%M")


def _history_days(value: str | None, default: int | None) -> int | None:
    if value is None:
        return default
    if value.strip().lower() == "all":
        return None
    if not value.strip().isdigit():
        raise click.ClickException(f"--history-days must be a number of days or 'all', not {value!r}")
    return int(value)


# ====================================================================== steps shared by commands


def _connect_google(
    app: App,
    secrets_: ClientSecrets,
    *,
    scope_profile: str = "minimal",
    listen: bool = False,
    open_browser: bool = False,
    allow_account_change: bool = False,
) -> str:
    """Run Google's consent flow for the bridge's own OAuth client and store the result.
    Returns the Google account."""
    from calendar_bridge.providers.google.auth import (
        ConsentFlow,
        authorized_session,
        build_credentials,
        scopes_for,
    )
    from calendar_bridge.providers.google.client import GoogleCalendarClient
    from calendar_bridge.sync.discovery import google_account_identity, refresh_google_calendars

    if listen and any(Path(marker).exists() for marker in ("/.dockerenv", "/run/.containerenv")):
        # The browser's redirect to 127.0.0.1 would reach the host, not this container.
        raise click.ClickException(
            "--listen only works when the bridge runs on the computer that has the browser; inside a container, "
            "leave it out and paste the address instead"
        )
    flow = ConsentFlow(secrets_, scopes_for(scope_profile), app.settings.google_oauth_port)
    if listen:
        flow.listen()  # before the address is built: a taken port is replaced by a free one
    url = flow.authorization_url()
    click.echo("Open this address in a browser, sign in to the Google account to synchronize")
    click.echo("(for ChatGPT: the account its Google Calendar connector uses) and allow access:\n")
    click.echo(f"   {url}\n")
    if listen:
        click.echo(f"Waiting for Google's answer on {flow.redirect_uri} (5 minutes)...")
        token = flow.run_local_server(open_browser)
    else:
        click.echo("Google then sends the browser to an address starting with")
        click.echo(f"{flow.redirect_uri} that fails to load. That is expected:")
        click.echo("copy the complete address from the address bar and paste it here.\n")
        token = flow.finish(click.prompt("Address", prompt_suffix=": "))

    client = GoogleCalendarClient(
        authorized_session(build_credentials(secrets_, token)), timeout=app.settings.http_timeout
    )
    entries = client.calendar_list()
    identity = google_account_identity(entries)
    if identity is None:
        raise click.ClickException("could not determine the account's primary calendar")
    previous = app.repo.get_account("google")
    if previous is not None and previous["identity"] != identity and not allow_account_change:
        raise click.ClickException(
            f"this is a different Google account ({identity}) than the one already configured "
            f"({previous['identity']}); nothing was saved. Use --allow-account-change only if you intend "
            "to switch accounts (existing mappings must then be reviewed)."
        )
    with _lock(app):
        with app.db.tx():
            app.store.save_google_client_secrets(secrets_)
            app.store.save_google_token(token)
            app.repo.set_account("google", identity, identity)
        refresh_google_calendars(app.ctx, client)
    _credentials_changed(app)
    writable = sum(1 for e in entries if e.get("accessRole") in ("owner", "writer"))
    click.echo(f"Google authorized for {identity}: {len(entries)} calendars ({writable} writable).")
    return identity


def _connect_icloud(
    app: App, apple_id: str, password: str, *, url: str | None = None, allow_account_change: bool = False
) -> Discovery:
    """Verify an app-specific password through CalDAV discovery and store it."""
    from calendar_bridge.providers.icloud.client import CalDAVClient
    from calendar_bridge.sync.discovery import refresh_icloud_calendars

    base = url or app.settings.icloud_url
    apple_id = apple_id.strip()
    if _is_icloud(base):
        # Refuse anything else before it leaves this machine: a mistyped main Apple Account
        # password must never be sent, and failed attempts can make Apple lock the account.
        app_password = icloud_app_password(password)
        if app_password is None:
            raise click.ClickException(
                "that is not an app-specific password, so nothing was sent to Apple. Create one at "
                "https://account.apple.com (Sign-In and Security -> App-Specific Passwords): 16 letters "
                "like abcd-efgh-ijkl-mnop."
            )
        password = app_password
    password = password.strip()
    client = CalDAVClient(base_url=base, username=apple_id, password=password, timeout=app.settings.http_timeout)
    discovery = client.discover()
    previous = app.repo.get_account("icloud")
    if previous is not None and previous["identity"] != discovery.principal_url and not allow_account_change:
        raise click.ClickException(
            "this Apple Account resolves to a different CalDAV principal than before; nothing was saved. "
            "Use --allow-account-change only if you intend to switch accounts."
        )
    with _lock(app):
        with app.db.tx():
            app.store.save_icloud(username=apple_id, password=password, url=base)
            app.repo.set_account("icloud", discovery.principal_url, apple_id)
        refresh_icloud_calendars(app.ctx, client)
    _credentials_changed(app)
    default = next((c.display_name for c in discovery.calendars if c.is_default), None)
    click.echo(
        f"iCloud authorized: principal discovered, {len(discovery.calendars)} calendars"
        + (f", default calendar '{default}'." if default else ".")
    )
    return discovery


def _create_mapping(
    app: App,
    icloud: CalendarRow,
    *,
    google: CalendarRow | None = None,
    new_google_name: str | None = None,
    name: str | None = None,
    history_days: int | None = None,
) -> int:
    """Validate and record a one-to-one mapping (pending its initial reconciliation). With
    ``new_google_name`` a secondary Google calendar is created for it, after validation."""
    name = (name or icloud.label()).strip()
    problems = []
    if not icloud.writable or not icloud.supports_events:
        problems.append(f"iCloud calendar '{icloud.label()}' is not a writable event calendar")
    if google is not None and not google.writable:
        problems.append(f"Google calendar '{google.label()}' is read-only for this account ({google.access})")
    if google is not None and google.role == "status":
        problems.append("the status calendar cannot be mapped")
    for m in app.repo.mappings(LIVE_STATES):
        if (google is not None and m.google_calendar_id == google.id) or m.icloud_calendar_id == icloud.id:
            problems.append(f"a calendar is already part of mapping '{m.name}' (#{m.id})")
        elif m.name == name:
            problems.append(f"a mapping named '{name}' already exists (#{m.id}); choose another --name")
    if google is None and any(
        c.display_name == new_google_name and not c.missing_since for c in app.repo.calendars("google")
    ):
        problems.append(f"a Google calendar named '{new_google_name}' already exists: map it with --google instead")
    if problems:
        raise click.ClickException("; ".join(problems))
    if google is None:
        from calendar_bridge.sync.discovery import refresh_google_calendars

        assert new_google_name
        created = app.providers.google().insert_calendar(new_google_name, app.ctx.default_tz)
        refresh_google_calendars(app.ctx, app.providers.google())
        google = app.repo.calendar_by_remote("google", created["id"])
        if google is None:
            raise click.ClickException(f"the new Google calendar '{new_google_name}' did not appear in the list")
        click.echo(f"Created Google calendar '{google.label()}'.")
    with _lock(app), app.db.tx():
        mapping_id = app.repo.create_mapping(
            name=name, google_calendar_id=google.id, icloud_calendar_id=icloud.id, history_days=history_days
        )
        app.repo.audit("mapping_created", mapping_id=mapping_id)
    return mapping_id


def _initial_plan(app: App, mapping_id: int) -> tuple[MappingContext, InitialPlan]:
    from calendar_bridge.sync.initial import build_plan

    with _lock(app):
        mc = MappingContext.build(app.ctx, app.repo.mapping(mapping_id))
        return mc, build_plan(app.ctx, mc, app.providers.google(), app.providers.icloud())


@click.group(cls=BridgeGroup)
@click.option("--config", type=click.Path(path_type=Path), help="TOML settings file (default: $CB_CONFIG if present).")
@click.option(
    "--log-level",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default=None,
    help="How much the command logs (default: CB_LOG_LEVEL or INFO).",
)
@click.option("--log-format", type=click.Choice(["json", "text"]), default=None)
@click.version_option(__version__, prog_name="calendar-bridge")
@click.pass_context
def cli(ctx: click.Context, config: Path | None, log_level: str | None, log_format: str | None) -> None:
    """Calendar Bridge: bidirectional iCloud <-> Google Calendar synchronization."""
    settings = load_settings(config)
    if log_level:
        settings.log_level = log_level.upper()
    fmt = log_format or (settings.log_format if ctx.invoked_subcommand == "daemon" else "text")
    setup_logging(settings.log_level, fmt)
    ctx.obj = State(settings)
    ctx.call_on_close(ctx.obj.close)


def main() -> None:
    cli(prog_name="calendar-bridge")


# ====================================================================== setup


@cli.command()
@pass_state
def init(state: State) -> None:
    """Create the data directory and database (safe to run repeatedly)."""
    app = state.app
    click.echo(f"Data directory : {app.settings.data_dir.resolve()}")
    click.echo(f"Database       : {app.settings.db_path.name} (schema v{app.db.schema_version()}, WAL)")
    try:
        app.box()
        click.echo("Encryption key : configured")
    except BridgeError as exc:
        click.echo(f"Encryption key : PROBLEM - {exc}")
    click.echo("\nNext step: calendar-bridge setup")


@cli.command()
@click.option(
    "--client-secrets",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="OAuth client JSON (type 'Desktop app'); otherwise you are asked for it.",
)
@click.option("--listen", is_flag=True, help="Receive Google's answer on 127.0.0.1 instead of pasting it.")
@click.option("--open-browser", is_flag=True, help="With --listen: open the consent page automatically.")
@pass_state
def setup(state: State, client_secrets: Path | None, listen: bool, open_browser: bool) -> None:
    """Guided setup: connect Google and iCloud, choose calendars, review, start.

    Run it again at any time: finished steps are skipped, and nothing is written to a calendar
    before you approve the plan shown in the last step."""
    from calendar_bridge.credentials import GOOGLE_TOKEN, ICLOUD
    from calendar_bridge.providers.google.auth import load_client_secrets
    from calendar_bridge.sync.initial import apply_plan

    app = state.app
    click.secho("Calendar Bridge setup", bold=True)
    click.echo("Connects a Google account and an iCloud account and keeps the calendars you choose in sync.")
    click.echo("Stop at any time with Ctrl+C and run `setup` again: finished steps are skipped.")

    _heading("1/5  Encryption key")
    if not load_keys(app.settings.key_file):
        raise click.ClickException(MISSING_KEY)
    app.box()  # rejects a malformed key before anything is stored with it
    click.echo("Configured.")

    _heading("2/5  Google")
    if not _keep_connection(app, "google", GOOGLE_TOKEN):
        secrets_ = load_client_secrets(client_secrets) if client_secrets else None
        stored_secrets = None if secrets_ else _stored_client_secrets(app)
        if stored_secrets is not None and click.confirm("Use the same Google OAuth client as last time?", default=True):
            secrets_ = stored_secrets
        chosen = secrets_ or _ask_client_secrets()
        _retry(lambda: _connect_google(app, chosen, listen=listen, open_browser=open_browser))

    _heading("3/5  iCloud")
    if not _keep_connection(app, "icloud", ICLOUD):
        previous = app.repo.get_account("icloud")

        def connect_icloud() -> None:
            apple_id = click.prompt("Apple Account email", default=previous["display"] if previous else None)
            _connect_icloud(app, apple_id, _ask_app_password())

        _retry(connect_icloud)

    _heading("4/5  Calendars")
    _refresh_calendars(app)
    _choose_mappings(app)

    _heading("5/5  Review and start")
    pending = app.repo.mappings(("pending_initial",))
    if not pending:
        click.echo("Nothing new to review.")
    for mapping in pending:
        click.echo(f"Reading both calendars of '{mapping.name}' (nothing is written)...")
        mc, plan = _initial_plan(app, mapping.id)
        _print_plan_summary(plan)
        if plan.blockers and not (plan.ambiguous and len(plan.blockers) == 1):
            click.echo("This pair cannot start as it is: fix the problem above, then run setup again.")
            continue
        if plan.ambiguous:
            click.echo(
                f"{len(plan.ambiguous)} group(s) of look-alike events are not paired automatically; they stay "
                "unsynchronized until you decide (`calendar-bridge conflicts list`)."
            )
        if click.confirm("Show the events in each category?", default=False):
            _print_plan_details(plan)
        if not click.confirm(f"Start synchronizing '{mapping.name}'?", default=True):
            click.echo("Not started. Run setup again when you are ready.")
            continue
        with _lock(app):
            counts = apply_plan(app.ctx, mc, plan, accept_ambiguous=bool(plan.ambiguous))
        click.echo(f"Started: {counts['linked']} event(s) already in both calendars were paired.")
    _setup_done(app)


def _heading(text: str) -> None:
    click.echo("")
    click.secho(text, bold=True)


def _keep_connection(app: App, provider: str, credential: str) -> bool:
    """True when the provider is already connected and the user keeps that connection."""
    account = app.repo.get_account(provider)
    if account is None or account["auth_state"] != "ok" or credential not in app.repo.credential_names():
        return False
    who = account["display"] or account["identity"]
    return not click.confirm(f"Connected as {who}. Connect again?", default=False)


def _retry(attempt: Any) -> Any:
    """Run an interactive step until it succeeds or the user gives up."""
    while True:
        try:
            return attempt()
        except (click.ClickException, BridgeError) as exc:
            message = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
            click.secho(f"Error: {message}", fg="red", err=True)
            if not click.confirm("Try again?", default=True):
                raise click.Abort() from exc


def _stored_client_secrets(app: App) -> ClientSecrets | None:
    try:
        return app.store.google_client_secrets()
    except BridgeError:  # unreadable with the current key: ask again
        return None


def _ask_client_secrets() -> ClientSecrets:
    from calendar_bridge.providers.google.auth import client_secrets_from_input

    click.echo("The bridge signs in to Google with your own OAuth client of type 'Desktop app'. If you do not")
    click.echo("have one yet, create it as described in docs/setup.md (about 10 minutes).")
    click.echo("Inside Docker, a file must be in the config folder: enter /config/<file name>.")
    while True:
        answer = click.prompt("Path of the downloaded client JSON file, or paste its content")
        try:
            return client_secrets_from_input(answer)
        except BridgeError as exc:
            click.secho(f"Error: {exc}", fg="red", err=True)


def _refresh_calendars(app: App) -> None:
    from calendar_bridge.sync.discovery import refresh_google_calendars, refresh_icloud_calendars

    refresh_google_calendars(app.ctx, app.providers.google())
    refresh_icloud_calendars(app.ctx, app.providers.icloud())


def _free_calendars(app: App, provider: str) -> list[CalendarRow]:
    """Calendars that can still be mapped: present, writable, holding events, in no live mapping."""
    taken = set()
    for m in app.repo.mappings(LIVE_STATES):
        taken |= {m.google_calendar_id, m.icloud_calendar_id}
    return [
        c
        for c in app.repo.calendars(provider)
        if c.writable and c.supports_events and not c.missing_since and c.role is None and c.id not in taken
    ]


def _pick(rows: list[CalendarRow], what: str) -> CalendarRow:
    for number, row in enumerate(rows, 1):
        click.echo(f"  {number}. {row.label()}")
    choice = click.prompt(f"Number of the {what}", type=click.IntRange(1, len(rows)))
    return rows[choice - 1]


def _choose_mappings(app: App) -> None:
    history = app.settings.default_history_days
    live = app.repo.mappings(LIVE_STATES)
    for m in live:
        click.echo(
            f"Already set up: '{m.name}' ({m.state}): iCloud '{app.repo.calendar(m.icloud_calendar_id).label()}' "
            f"<-> Google '{app.repo.calendar(m.google_calendar_id).label()}'"
        )
        if m.state == "paused" and m.activated_at and click.confirm(f"Resume '{m.name}'?", default=True):
            with _lock(app), app.db.tx():
                app.repo.update_mapping(m.id, state="active")
                app.repo.request_full_resync(m.id)
                app.repo.audit("mapping_active", mapping_id=m.id)
    if not live:
        icloud_free, google_free = _free_calendars(app, "icloud"), _free_calendars(app, "google")
        if not icloud_free or not google_free:
            raise click.ClickException("no writable calendar found on one side; check both accounts' calendars")
        icloud = next((c for c in icloud_free if c.is_primary), None)
        google = next((c for c in google_free if c.is_primary), None)
        click.echo("ChatGPT and most Google tools use your main (primary) Google calendar.")
        if not (
            icloud
            and google
            and click.confirm(
                f"Sync your main iCloud calendar '{icloud.label()}' with your main Google calendar '{google.label()}'?",
                default=True,
            )
        ):
            icloud = _pick(icloud_free, "iCloud calendar to sync")
            google = _pick(google_free, "Google calendar to keep in sync with it")
        _create_mapping(app, icloud, google=google, name="main", history_days=history)
    for calendar in _free_calendars(app, "icloud"):
        target = f"{calendar.label()} (iCloud)"
        if click.confirm(f"Also sync iCloud '{calendar.label()}' into the Google calendar '{target}'?", default=False):
            existing = next((c for c in _free_calendars(app, "google") if c.display_name == target), None)
            _create_mapping(
                app, calendar, google=existing, new_google_name=None if existing else target, history_days=history
            )


def _setup_done(app: App) -> None:
    from calendar_bridge.status.alerts import channels_from_settings

    _heading("Done")
    if not app.repo.mappings(("active",)):
        click.echo("No calendar pair is synchronizing yet. Run `calendar-bridge setup` again when you are ready.")
        return
    age = seconds_since(app.repo.get_meta("heartbeat_at"), app.clock.now())
    if age is not None and age < app.settings.sync_interval * 3:
        click.echo("The running service picks this up within a minute.")
    else:
        click.echo("Start the service:  docker compose up -d     (without Docker: calendar-bridge daemon)")
    click.echo("See what it does:   calendar-bridge status   and   calendar-bridge activity")
    if not channels_from_settings(app.settings):
        topic = "calendar-bridge-" + secrets.token_urlsafe(15)
        click.echo("\nRecommended: a phone alert when something needs you. Install the ntfy app, subscribe to")
        click.echo("this private topic, then add the line to your .env file and restart the service:")
        click.echo(f"  CB_ALERT_NTFY_URL=https://ntfy.sh/{topic}")
    if app.settings.status_calendar:
        click.echo(
            f"\nUsing ChatGPT? The '{app.settings.status_calendar_name}' calendar in Google says what is confirmed"
        )
        click.echo("in iCloud. The README has a custom instruction that makes ChatGPT check it.")


@cli.group()
def keys() -> None:
    """Credential encryption keys."""


@keys.command("generate")
def keys_generate() -> None:
    """Print a new encryption key (store it outside the data volume)."""
    click.echo(generate_key())


@keys.command("init")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--owner", default="10001:10001", show_default=True, help="uid:gid given the file when run as root.")
def keys_init(path: Path, owner: str) -> None:
    """Create a key file once; an existing key is never replaced (`docker compose run --rm keygen`)."""
    if path.is_dir():
        if any(path.iterdir()):
            raise click.ClickException(f"{path} is a directory, not a key file")
        path.rmdir()  # Docker creates an empty directory when a bind-mounted file is missing
        click.echo("Removed the empty directory Docker had created in place of the missing key file.")
    if key_file_is_empty(path):
        path.unlink()  # holds no key, so nothing can depend on it
        click.echo("Replaced the empty key file.")
    if path.exists():
        read_key_file(path)  # refuses an empty or unreadable file
        click.echo(f"{path} already holds a key: left unchanged.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(generate_key() + "\n")
    if sys.platform != "win32" and os.geteuid() == 0:
        uid, _, gid = owner.partition(":")
        try:
            os.chown(path, int(uid), int(gid or uid))
        except (OSError, ValueError) as exc:
            click.echo(f"Note: could not give the file to {owner} ({type(exc).__name__}).")
    click.echo(f"Created the encryption key {path}.")
    click.echo("Keep a copy somewhere safe (a password manager): stored credentials and encrypted backups need it.")


@keys.command("rotate")
@pass_state
def keys_rotate(state: State) -> None:
    """Re-encrypt stored credentials with the first configured key."""
    app = state.app
    with _lock(app), app.db.tx():
        count = app.store.rotate()
    _credentials_changed(app)
    click.echo(f"Re-encrypted {count} credential record(s) with the first key.")
    click.echo("Keep the older keys below it while you may need backups made before today; new backups use the")
    click.echo("first key.")


# ====================================================================== auth


@cli.group()
def auth() -> None:
    """Authorize, check and revoke provider access."""


@auth.command("google")
@click.option(
    "--client-secrets",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="OAuth client JSON (type 'Desktop app') downloaded from Google Cloud Console.",
)
@click.option("--listen", is_flag=True, help="Receive the redirect on 127.0.0.1 instead of pasting it.")
@click.option("--open-browser", is_flag=True, help="With --listen: open the consent page automatically.")
@click.option(
    "--scope-profile",
    type=click.Choice(["minimal", "shared"]),
    default="minimal",
    show_default=True,
    help="'shared' is only needed to sync a Google calendar owned by someone else.",
)
@click.option("--allow-account-change", is_flag=True, help="Accept a different Google account than before.")
@pass_state
def auth_google(
    state: State,
    client_secrets: Path | None,
    listen: bool,
    open_browser: bool,
    scope_profile: str,
    allow_account_change: bool,
) -> None:
    """Authorize the bridge's own OAuth client (separate from ChatGPT's connector)."""
    from calendar_bridge.providers.google.auth import load_client_secrets

    app = state.app
    secrets_ = load_client_secrets(client_secrets) if client_secrets else app.store.google_client_secrets()
    if secrets_ is None:
        raise click.ClickException(
            "the first authorization needs the OAuth client: --client-secrets <file> (see docs/setup.md), "
            "or run `calendar-bridge setup`"
        )
    _connect_google(
        app,
        secrets_,
        scope_profile=scope_profile,
        listen=listen,
        open_browser=open_browser,
        allow_account_change=allow_account_change,
    )
    click.echo("Stored encrypted. The refresh token is renewed automatically.")


@auth.command("icloud")
@click.option("--apple-id", prompt="Apple Account email", help="Your Apple Account (Apple ID) email.")
@click.option(
    "--password-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="File holding the app-specific password (otherwise you are prompted).",
)
@click.option("--url", default=None, help="CalDAV entry point (default https://caldav.icloud.com/).")
@click.option("--allow-account-change", is_flag=True)
@pass_state
def auth_icloud(
    state: State, apple_id: str, password_file: Path | None, url: str | None, allow_account_change: bool
) -> None:
    """Store an Apple app-specific password and verify CalDAV access."""
    password = password_file.read_text(encoding="utf-8").strip() if password_file else _ask_app_password()
    _connect_icloud(state.app, apple_id, password, url=url, allow_account_change=allow_account_change)


def _ask_app_password() -> str:
    click.echo("Create an app-specific password at https://account.apple.com (Sign-In and Security ->")
    click.echo("App-Specific Passwords). Never use your main Apple Account password here.")
    return str(click.prompt("App-specific password (abcd-efgh-ijkl-mnop, stays invisible)", hide_input=True))


@auth.command("status")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def auth_status(state: State, as_json: bool) -> None:
    """Show stored credentials and their last verified state (no network)."""
    from calendar_bridge.status.report import build_status

    status = build_status(state.app.ctx)
    data = {"google": status["google"], "icloud": status["icloud"]}
    if as_json:
        _json(data)
        return
    _table(
        [[p, d["state"], d.get("account"), _ago(d.get("last_verified_at")), d.get("error")] for p, d in data.items()],
        ["provider", "state", "account", "verified", "error"],
    )


@auth.command("revoke")
@click.argument("provider", type=click.Choice(["google", "icloud"]))
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
def auth_revoke(state: State, provider: str, yes: bool) -> None:
    """Revoke and delete stored access. Synchronization state is kept."""
    from calendar_bridge.credentials import GOOGLE_TOKEN, ICLOUD
    from calendar_bridge.providers.google.auth import revoke

    app = state.app
    if not yes:
        click.confirm(f"Revoke {provider} access? Synchronization stops until you authorize again.", abort=True)
    with _lock(app):
        if provider == "google":
            token = app.store.google_token()
            if token:
                confirmed = revoke(token["refresh_token"], timeout=app.settings.http_timeout)
                click.echo(
                    "Google confirmed the revocation."
                    if confirmed
                    else "Google did not confirm; also remove access at https://myaccount.google.com/permissions"
                )
            with app.db.tx():
                app.store.delete(GOOGLE_TOKEN)
                app.repo.set_auth_state("google", "revoked", "revoked by user")
        else:
            with app.db.tx():
                app.store.delete(ICLOUD)
                app.repo.set_auth_state("icloud", "revoked", "revoked by user")
            click.echo("Stored password deleted. Also revoke the app-specific password at https://account.apple.com")
    _credentials_changed(app)


@cli.command()
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
def uninstall(state: State, yes: bool) -> None:
    """Stop synchronizing and remove the bridge's access to both accounts. Events stay where they are."""
    from calendar_bridge.credentials import GOOGLE_CLIENT, GOOGLE_TOKEN, ICLOUD
    from calendar_bridge.errors import NotFound
    from calendar_bridge.providers.google.auth import revoke

    app = state.app
    click.echo("This will:")
    click.echo("  - stop synchronizing every calendar pair (all events stay in both calendars)")
    click.echo(f"  - delete the '{app.settings.status_calendar_name}' calendar the bridge created in Google")
    click.echo("  - revoke the bridge's Google access and delete its stored iCloud app-specific password")
    if not yes:
        click.confirm("Continue?", abort=True)
    with _lock(app):
        with app.db.tx():
            for mapping in app.repo.mappings(("active",)):
                app.repo.update_mapping(mapping.id, state="paused")
                app.repo.audit("mapping_paused", mapping_id=mapping.id, detail="uninstall")
        status_calendar = app.repo.status_calendar()
        if status_calendar is not None and not status_calendar.missing_since:
            try:
                app.providers.google().delete_calendar(status_calendar.remote_id)
                click.echo("Deleted the status calendar.")
            except NotFound:
                pass
            except BridgeError as exc:
                click.echo(f"Could not delete the status calendar ({exc}); delete it in Google Calendar yourself.")
            with app.db.tx():
                app.repo.set_calendar_role(status_calendar.id, None)
        try:
            token = app.store.google_token()
        except BridgeError:
            token = None
        if token and token.get("refresh_token"):
            confirmed = revoke(token["refresh_token"], timeout=app.settings.http_timeout)
            click.echo(
                "Google confirmed the revocation."
                if confirmed
                else "Google did not confirm; remove access at https://myaccount.google.com/permissions"
            )
        with app.db.tx():
            for name in (GOOGLE_TOKEN, GOOGLE_CLIENT, ICLOUD):
                app.store.delete(name)
            for provider in ("google", "icloud"):
                app.repo.set_auth_state(provider, "revoked", "uninstalled")
            app.repo.audit("uninstalled")
    _credentials_changed(app)
    click.echo("\nThe bridge no longer has access to either account. To finish:")
    click.echo("  1. Revoke its app-specific password at https://account.apple.com")
    click.echo("     (Sign-In and Security -> App-Specific Passwords).")
    click.echo("  2. Remove the service and its data:  docker compose down -v")
    click.echo("  3. Optionally delete its Google Cloud project at https://console.cloud.google.com")


# ====================================================================== calendars & mappings


@cli.command()
@click.option("--refresh/--no-refresh", default=True, help="Query the providers (read-only).")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def calendars(state: State, refresh: bool, as_json: bool) -> None:
    """Discover and list calendars on both providers."""
    from calendar_bridge.sync.discovery import refresh_google_calendars, refresh_icloud_calendars

    app = state.app
    if refresh:
        try:
            refresh_google_calendars(app.ctx, app.providers.google())
        except BridgeError as exc:
            click.echo(f"(google: {exc})", err=True)
        try:
            refresh_icloud_calendars(app.ctx, app.providers.icloud())
        except BridgeError as exc:
            click.echo(f"(icloud: {exc})", err=True)
    mapped = {}
    for mapping in app.repo.mappings(("active", "pending_initial", "paused")):
        mapped[mapping.google_calendar_id] = mapping.name
        mapped[mapping.icloud_calendar_id] = mapping.name
    rows = app.repo.calendars()
    if as_json:
        _json(
            [
                {
                    "id": r.id,
                    "provider": r.provider,
                    "name": r.label(),
                    "access": r.access,
                    "writable": r.writable,
                    "primary": r.is_primary,
                    "role": r.role,
                    "mapping": mapped.get(r.id),
                    "remote_id": r.remote_id,
                    "missing_since": r.missing_since,
                }
                for r in rows
            ]
        )
        return
    _table(
        [
            [
                r.id,
                r.provider,
                r.label(),
                r.access,
                "yes" if r.writable else "no",
                "primary" if r.is_primary and r.provider == "google" else ("default" if r.is_primary else ""),
                r.role or "",
                mapped.get(r.id, ""),
                "MISSING" if r.missing_since else "",
            ]
            for r in rows
        ],
        ["id", "provider", "name", "access", "writable", "main", "role", "mapping", ""],
    )


@cli.group()
def mappings() -> None:
    """Explicit one-to-one calendar mappings."""


@mappings.command("list")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def mappings_list(state: State, as_json: bool) -> None:
    app = state.app
    rows: list[dict[str, Any]] = []
    for m in app.repo.mappings():
        g, i = app.repo.calendar(m.google_calendar_id), app.repo.calendar(m.icloud_calendar_id)
        counts = app.repo.item_counts(m.id)
        rows.append(
            {
                "id": m.id,
                "name": m.name,
                "state": m.state,
                "icloud": i.label(),
                "google": g.label(),
                "history_days": m.history_days,
                "items": counts,
                "deletions": "suspended" if m.destructive_hold else "allowed",
            }
        )
    if as_json:
        _json(rows)
        return
    _table(
        [
            [
                r["id"],
                r["name"],
                r["state"],
                r["icloud"],
                r["google"],
                "all" if r["history_days"] is None else f"{r['history_days']}d",
                r["items"]["synced"],
                r["items"]["conflicted"],
                r["deletions"],
            ]
            for r in rows
        ],
        ["id", "name", "state", "iCloud", "Google", "history", "synced", "conflicts", "deletions"],
    )


@mappings.command("suggest")
@pass_state
def mappings_suggest(state: State) -> None:
    """Suggest the main mapping: iCloud default calendar <-> Google primary calendar."""
    app = state.app
    google = next((c for c in app.repo.calendars("google") if c.is_primary), None)
    icloud = next((c for c in app.repo.calendars("icloud") if c.is_primary), None)
    if google is None or icloud is None:
        raise click.ClickException("run `calendar-bridge calendars` after authorizing both providers first")
    click.echo(
        f"Suggested main mapping: iCloud '{icloud.label()}' (default calendar) <-> Google '{google.label()}' (primary)."
    )
    click.echo("ChatGPT's Google connector uses the primary calendar by default, so this makes ChatGPT")
    click.echo("operate on your main iCloud calendar. Create it with:\n")
    click.echo(f"  calendar-bridge mappings add --name main --icloud {icloud.id} --google {google.id}\n")
    click.echo("Then review the dry-run plan: calendar-bridge reconcile --mapping <id>")


@mappings.command("add")
@click.option("--icloud", "icloud_sel", required=True, help="iCloud calendar: id from `calendars`, name or URL.")
@click.option("--google", "google_sel", help="Google calendar: id from `calendars`, name, calendar id or 'primary'.")
@click.option("--create-google-calendar", "new_google", help="Create a new secondary Google calendar with this name.")
@click.option("--name", help="Mapping name (default: the iCloud calendar's name).")
@click.option("--history-days", default=None, help="Ignore events that ended more than N days ago, or 'all'.")
@pass_state
def mappings_add(
    state: State,
    icloud_sel: str,
    google_sel: str | None,
    new_google: str | None,
    name: str | None,
    history_days: str | None,
) -> None:
    """Map one iCloud calendar to one Google calendar (initial reconciliation comes next)."""
    from calendar_bridge.sync.discovery import (
        find_calendar,
        refresh_google_calendars,
        refresh_icloud_calendars,
    )

    app = state.app
    if bool(google_sel) == bool(new_google):
        raise click.ClickException("give exactly one of --google or --create-google-calendar")
    days = _history_days(history_days, app.settings.default_history_days)
    refresh_google_calendars(app.ctx, app.providers.google())
    refresh_icloud_calendars(app.ctx, app.providers.icloud())
    icloud = find_calendar(app.repo.calendars("icloud"), icloud_sel)
    if icloud is None:
        raise click.ClickException(
            f"no unique iCloud calendar matches {icloud_sel!r} (see `calendar-bridge calendars`)"
        )
    google = None
    if google_sel:
        google = find_calendar(app.repo.calendars("google"), google_sel)
        if google is None:
            raise click.ClickException(
                f"no unique Google calendar matches {google_sel!r} (see `calendar-bridge calendars`)"
            )
    mapping_id = _create_mapping(app, icloud, google=google, new_google_name=new_google, name=name, history_days=days)
    mapping = app.repo.mapping(mapping_id)
    click.echo(f"Mapping #{mapping_id} created (pending initial reconciliation):")
    click.echo(f"  iCloud '{icloud.label()}' <-> Google '{app.repo.calendar(mapping.google_calendar_id).label()}'")
    click.echo(f"Review the plan (nothing is written): calendar-bridge reconcile --mapping {mapping_id}")


def _set_mapping_state(state: State, mapping_id: int, new_state: str) -> None:
    app = state.app
    mapping = app.repo.mapping(mapping_id)
    if mapping.state == "removed":
        raise click.ClickException(
            f"mapping #{mapping_id} was removed; set the pair up again with `calendar-bridge setup`"
        )
    if new_state == "active" and mapping.activated_at is None:
        raise click.ClickException("start a new mapping with `calendar-bridge setup` or `reconcile --apply`")
    with _lock(app), app.db.tx():
        app.repo.update_mapping(mapping_id, state=new_state)
        if new_state == "active":
            app.repo.request_full_resync(mapping_id)
        app.repo.audit(f"mapping_{new_state}", mapping_id=mapping_id)
    click.echo(f"Mapping #{mapping_id} is now {new_state}.")


@mappings.command("pause")
@click.argument("mapping_id", type=int)
@pass_state
def mappings_pause(state: State, mapping_id: int) -> None:
    """Stop synchronizing a mapping (changes are caught up on resume)."""
    _set_mapping_state(state, mapping_id, "paused")


@mappings.command("resume")
@click.argument("mapping_id", type=int)
@pass_state
def mappings_resume(state: State, mapping_id: int) -> None:
    _set_mapping_state(state, mapping_id, "active")


@mappings.command("remove")
@click.argument("mapping_id", type=int)
@click.option("--yes", is_flag=True)
@pass_state
def mappings_remove(state: State, mapping_id: int, yes: bool) -> None:
    """Stop and forget a mapping. No event is deleted from either calendar."""
    mapping = state.app.repo.mapping(mapping_id)
    if not yes:
        click.confirm(
            f"Remove mapping '{mapping.name}' (#{mapping.id})? Its events stay in both calendars but stop syncing.",
            abort=True,
        )
    _set_mapping_state(state, mapping_id, "removed")


@cli.command()
@click.option("--mapping", "mapping_id", type=int, required=True)
@click.option("--apply", "do_apply", is_flag=True, help="Record the matches and activate the mapping.")
@click.option("--accept-ambiguous", is_flag=True, help="Activate even with ambiguous matches (they stay unsynced).")
@click.option("--details", is_flag=True, help="List the individual events in each category.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def reconcile(
    state: State, mapping_id: int, do_apply: bool, accept_ambiguous: bool, details: bool, as_json: bool
) -> None:
    """Initial reconciliation: dry-run plan by default (nothing is written to any calendar)."""
    from calendar_bridge.sync.initial import apply_plan, build_plan

    app = state.app
    with _lock(app):
        mc = MappingContext.build(app.ctx, app.repo.mapping(mapping_id))
        plan = build_plan(app.ctx, mc, app.providers.google(), app.providers.icloud())
        summary = plan.summary()
        if as_json:
            payload = dict(summary)
            if details:
                payload["pairs"] = [
                    {"google": p.google.label(), "icloud": p.icloud.label(), "how": p.how, "differences": p.differences}
                    for p in plan.pairs
                ]
                payload["ambiguous_groups"] = plan.ambiguous
            _json(payload)
        else:
            _print_plan(plan, details)
        if not do_apply:
            if not as_json:
                click.echo("\nDry run only. Re-run with --apply to activate.")
            return
        counts = apply_plan(app.ctx, mc, plan, accept_ambiguous=accept_ambiguous)
    click.echo(
        f"\nMapping activated: {counts['linked']} pairs linked, {counts['ambiguous']} ambiguous group(s) "
        "recorded as conflicts. The daemon now copies the remaining events (journaled and verified)."
    )


def _print_plan(plan: Any, details: bool) -> None:
    _print_plan_summary(plan)
    if details:
        _print_plan_details(plan)


def _print_plan_summary(plan: Any) -> None:
    s = plan.summary()
    click.echo(f"Google events: {s['google_events']}   iCloud events: {s['icloud_events']}")
    click.echo(f"Already in both calendars      : {s['already_matching']}")
    click.echo(f"In both, but details differ    : {s['matched_with_differences']}  (you choose which to keep)")
    click.echo(f"Ambiguous (not paired)         : {s['ambiguous']}")
    click.echo(f"Will be copied Google -> iCloud: {s['copy_to_icloud']}")
    click.echo(f"Will be copied iCloud -> Google: {s['copy_to_google']}")
    click.echo(f"Cannot be synchronized         : {s['unsupported']}")
    click.echo(
        f"Older than the history window  : google {s['outside_history_window']['google']}, "
        f"icloud {s['outside_history_window']['icloud']} (left alone)"
    )
    for warning in s["warnings"]:
        click.echo(f"WARNING: {warning}")
    for blocker in s["blockers"]:
        click.echo(f"BLOCKED: {blocker}")
    click.echo(f"Verdict: {s['verdict'].upper()}")


def _print_plan_details(plan: Any) -> None:
    for title, entries in (
        (
            "Matched with differences",
            [f"{p.icloud.label()}  [{', '.join(p.differences)}]" for p in plan.pairs if not p.identical],
        ),
        ("Copy to iCloud", [side.label() for side in plan.copy_to_icloud]),
        ("Copy to Google", [side.label() for side in plan.copy_to_google]),
        ("Not syncable", [f"{side.label()} ({reason})" for side, reason in plan.unsupported]),
    ):
        if entries:
            click.echo(f"\n{title}:")
            for entry in entries[:200]:
                click.echo(f"  - {entry}")
    for group in plan.ambiguous:
        click.echo(f"\nAmbiguous ({group['why']}):")
        for label, cand in zip(group["labels"], group["candidates"], strict=True):
            click.echo(f"  - [{cand['provider']}] {label}  id={cand['id']}")


# ====================================================================== running


@cli.command()
@click.option("--once", is_flag=True, default=True, hidden=True)
@click.option("--dry-run", is_flag=True, help="Observe and plan, but write nothing to the calendars.")
@click.option("--full", is_flag=True, help="Force complete re-reads of both calendars.")
@click.option("--mapping", "mapping_id", type=int)
@click.option("--json", "as_json", is_flag=True)
@pass_state
def sync(state: State, once: bool, dry_run: bool, full: bool, mapping_id: int | None, as_json: bool) -> None:
    """Run one synchronization cycle now."""
    del once
    app = state.app
    if mapping_id is not None and app.repo.mapping(mapping_id).state != "active":
        raise click.ClickException(f"mapping #{mapping_id} is {app.repo.mapping(mapping_id).state}, not active")
    with _lock(app):
        report = app.engine().run_cycle(
            mapping_ids=[mapping_id] if mapping_id else None, force_full=full, dry_run=dry_run
        )
    if as_json:
        _json(report.as_dict())
        return
    for m in report.mappings:
        click.echo(f"Mapping '{m.name}' #{m.mapping_id}: {'ok' if m.ok else 'problems'}{' (full)' if m.full else ''}")
        if m.reconcile:
            click.echo(f"  planned {m.reconcile['planned']}  done {m.reconcile['done']}  held {m.reconcile['held']}")
        if dry_run:
            for plan in m.plans:
                if plan["action"] not in ("noop", "wait"):
                    click.echo(f"  item {plan['item_id']}: {plan['action']} {plan.get('reason', '')}")
        for error in m.errors:
            click.echo(f"  ! {error}")
    for error in report.errors:
        click.echo(f"! {error}")
    if not report.ok:
        sys.exit(1)


@cli.command()
@pass_state
def daemon(state: State) -> None:
    """Run the synchronization service in the foreground (Docker entrypoint)."""
    from calendar_bridge.daemon import Daemon

    try:
        code = Daemon(state.app).run()
    except BridgeError:
        raise
    except Exception:
        # Keep the traceback in the service log; the restart policy brings the service back.
        logging.getLogger("calendar_bridge.daemon").critical("service stopped by an unexpected error", exc_info=True)
        sys.exit(1)
    sys.exit(code)


# ====================================================================== status & health


@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.option("--no-titles", is_flag=True, help="Omit event titles.")
@pass_state
def status(state: State, as_json: bool, no_titles: bool) -> None:
    """Show synchronization status (read-only)."""
    from calendar_bridge.status.report import build_status

    data = build_status(state.app.ctx, include_titles=not no_titles)
    if as_json:
        _json(data)
        return
    daemon_info = data["daemon"]
    click.echo(
        f"Overall: {data['overall'].upper()}   (daemon heartbeat {_ago(daemon_info['heartbeat_at'])}, "
        f"last good cycle {_ago(daemon_info['last_ok_cycle_at'])})"
    )
    click.echo(
        f"Google: {data['google']['state']} ({data['google'].get('account') or '-'})   "
        f"iCloud: {data['icloud']['state']} ({data['icloud'].get('account') or '-'})"
    )
    if data["global_hold"]:
        click.echo(f"DESTRUCTIVE WRITES SUSPENDED: {data['global_hold']}")
    for m in data["mappings"]:
        items = m["items"]
        click.echo(
            f"\n[{m['id']}] {m['name']} ({m['state']}): iCloud '{m['icloud_calendar']['name']}' <-> "
            f"Google '{m['google_calendar']['name']}'"
        )
        click.echo(
            f"  read: google {_ago(m['last_google_read_at'])}, icloud {_ago(m['last_icloud_read_at'])}   "
            f"verified write: google {_ago(m['last_google_write_verified_at'])}, "
            f"icloud {_ago(m['last_icloud_write_verified_at'])}"
        )
        click.echo("  items: " + ", ".join(f"{k} {v}" for k, v in items.items() if v))
        if m["open_write_intents"]:
            click.echo(f"  open write intents: {m['open_write_intents']}")
        if m["destructive_writes"] == "suspended":
            click.echo(f"  DELETIONS SUSPENDED: {m['destructive_hold_reason']}")
        for provider, error in m["read_errors"].items():
            if error:
                click.echo(f"  {provider} read error: {error}")
        for entry in m["pending"][:10]:
            click.echo(f"  waiting: {entry.get('title', '')} [{entry.get('start', '')}] -> {entry['waiting_for']}")
    click.echo(f"\nOpen conflicts: {data['conflicts']['open']}   permanent failures: {data['failures']['permanent']}")
    for alert in data["alerts"]:
        click.echo(f"ALERT {alert['key']}: {alert['message']}")
    for step in _next_steps(data):
        click.echo(f"Next: {step}")


def _next_steps(status: dict[str, Any]) -> list[str]:
    """What the user can do about the state shown by `status`, most important first."""
    steps = []
    if status["overall"] == "not_configured":
        steps.append("set up a calendar pair with `calendar-bridge setup`")
    if any(status[p]["state"] != "ok" for p in ("google", "icloud")) and status["mappings"]:
        steps.append("reconnect the account with `calendar-bridge setup`")
    if status["global_hold"] or any(m["destructive_writes"] == "suspended" for m in status["mappings"]):
        steps.append("review the held deletions with `calendar-bridge safety status`")
    if status["conflicts"]["open"]:
        steps.append("decide the conflicts with `calendar-bridge conflicts list`")
    if status["failures"]["permanent"]:
        steps.append(
            "see why events could not be synchronized: `calendar-bridge items list --state failed` (or rejected)"
        )
    if any(e for m in status["mappings"] for e in m["read_errors"].values()):
        steps.append("check connectivity; details in `docker compose logs calendar-bridge`")
    return steps


PROVIDER_LABEL = {"google": "Google", "icloud": "iCloud", "both": "both calendars", "user": "you"}
# Internal bookkeeping that says nothing about what happened to an event.
QUIET_ACTIONS = (
    "new_google_event",
    "new_icloud_event",
    "linked",
    "linked_identical",
    "conflict_resolution_requested",
)


def _describe_activity(row: Any) -> str:
    action, detail = row["action"], row["detail"] or ""
    where = " and ".join(PROVIDER_LABEL.get(p, p) for p in (row["provider"] or "").split(",") if p)
    if action == "created":
        return f"copied to {where}"
    if action == "updated":
        return f"updated in {where}" + (f" ({detail.replace(',', ', ')})" if detail else "")
    if action == "deleted":
        by = detail.removeprefix("by ")
        if by == "user":
            return f"duplicate deleted in {where}, as you chose"
        source = PROVIDER_LABEL.get(by, by)
        return f"deleted in {where} (deleted in {source} first)" if where else f"deleted in {source}"
    if action == "conflict":
        return f"conflict {detail.split(' ')[0]}: needs your decision"
    if action == "conflict_resolved":
        number, _, resolution = detail.partition(" ")
        return f"conflict {number} resolved ({resolution.replace('_', ' ')})"
    if action in ("failed", "rejected"):
        return f"could not synchronize: {detail}"
    descriptions = {
        "suppressed": "removed from Google (cancelled in iCloud)",
        "recovered_create": f"finished a copy to {where} interrupted by a restart",
        "linked_existing": f"paired with a copy that already existed in {where}",
        "linked_existing_uid": f"paired with a copy that already existed in {where}",
        "relinked_by_marker": "paired again with its copy after a restore",
        "tombstone_restore": "restored from the deleted events",
        "destructive_hold": f"deletions suspended: {detail}",
        "destructive_hold_released": "deletions released by you",
        "destructive_hold_cleared": "deletion hold lifted: nothing waiting any more",
        "global_hold": f"all deletions suspended: {detail}",
        "global_hold_cleared": "deletions allowed again",
        "mapping_created": "calendar pair created",
        "mapping_activated": "calendar pair started",
        "mapping_paused": "calendar pair paused",
        "mapping_active": "calendar pair resumed",
        "mapping_removed": "calendar pair removed",
        "database_restored": f"database restored from {detail}",
        "status_calendar_created": "status calendar created in Google",
        "uninstalled": "access removed (uninstall)",
        "failed_retried": f"retrying {detail} event(s) that had failed",
    }
    return descriptions.get(action, action.replace("_", " ") + (f": {detail}" if detail else ""))


@cli.command()
@click.option("--limit", type=click.IntRange(1, 1000), default=30, show_default=True)
@click.option("--mapping", "mapping_id", type=int, help="Only this calendar pair.")
@click.option("--no-titles", is_flag=True, help="Omit event titles.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def activity(state: State, limit: int, mapping_id: int | None, no_titles: bool, as_json: bool) -> None:
    """What the bridge changed recently, newest first (read-only)."""
    from calendar_bridge.status.report import event_brief

    app = state.app
    tz = app.ctx.default_tz
    rows = []
    for row in app.repo.audit_entries(limit, mapping_id=mapping_id, exclude=QUIET_ACTIONS):
        entry: dict[str, Any] = {
            "at": row["at"],
            "what": _describe_activity(row),
            "action": row["action"],
            "provider": row["provider"],
            "mapping_id": row["mapping_id"],
            "item_id": row["item_id"],
        }
        if row["item_id"] is not None:
            with contextlib.suppress(KeyError):  # the tombstone may have been purged since
                entry.update(event_brief(app.repo, app.repo.item(row["item_id"]), tz, not no_titles))
        rows.append(entry)
    if as_json:
        _json(rows)
        return
    if not rows:
        click.echo("Nothing has happened yet.")
        return
    click.echo(f"Times in {tz}.")
    _table(
        [
            [
                _local_time(app, r["at"]),
                r["what"][:60],
                " ".join(filter(None, [r.get("title", "")[:40], f"[{r['start']}]" if r.get("start") else ""])),
            ]
            for r in rows
        ],
        ["when", "what", "event"],
    )


@cli.command()
@click.option("--max-age", type=int, default=None, help="Max heartbeat age in seconds.")
@click.option("--strict", is_flag=True, help="Also fail unless the last good cycle is recent.")
@pass_state
def health(state: State, max_age: int | None, strict: bool) -> None:
    """Exit 0 when the daemon is alive (used by the Docker healthcheck)."""
    app = state.app
    limit = max_age or app.settings.sync_interval * 3 + 60
    heartbeat = app.repo.get_meta("heartbeat_at")
    age = seconds_since(heartbeat, app.clock.now())
    started = app.repo.get_meta("cycle_started_at")
    running = seconds_since(started, app.clock.now()) if started and started > (heartbeat or "") else None
    if running is not None and running <= app.settings.cycle_timeout + 60:
        # A long cycle (e.g. a large first copy) is alive: the watchdog ends it if it hangs.
        click.echo(f"OK: cycle running for {int(running)}s")
        return
    if age is None or age > limit:
        click.echo(f"UNHEALTHY: no heartbeat for {'ever' if age is None else f'{int(age)}s'}")
        sys.exit(1)
    if strict:
        last_ok = app.repo.get_meta("last_ok_cycle_at")
        ok_age = seconds_since(last_ok, app.clock.now())
        if ok_age is None or ok_age > app.settings.lag_alert_seconds:
            click.echo("DEGRADED: no fully successful cycle recently")
            sys.exit(2)
    click.echo(f"OK: heartbeat {int(age)}s ago")


@cli.command()
@pass_state
def doctor(state: State) -> None:
    """Read-only diagnostics of configuration, storage and provider access."""
    from calendar_bridge.sync.discovery import google_account_identity

    app = state.app
    failures = 0

    def report(ok: bool | None, label: str, detail: str = "") -> None:
        nonlocal failures
        mark = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
        failures += ok is False
        click.echo(f"[{mark}] {label}" + (f": {detail}" if detail else ""))

    if app.settings.default_timezone != "auto":
        source = "configured"
    elif app.repo.get_meta(HOME_TIMEZONE):
        source = "kept since the first sync"
    else:
        source = "from Google" if app.repo.get_account("google") else "until Google is connected"
    report(
        True, "configuration", f"interval {app.settings.sync_interval}s, default zone {app.ctx.default_tz} ({source})"
    )
    try:
        app.box()
        keys_present = True
        report(True, "encryption key", "configured")
    except BridgeError as exc:
        keys_present = False
        report(False, "encryption key", str(exc))
    problems = app.db.integrity_check()
    report(not problems, "database integrity", "; ".join(problems[:3]) or f"schema v{app.db.schema_version()}")
    journal = app.db.scalar("PRAGMA journal_mode")
    report(journal == "wal", "database journal mode", str(journal))
    if keys_present:
        try:
            google = app.providers.google()
            entries = google.calendar_list()
            identity = google_account_identity(entries)
            stored = app.repo.get_account("google")
            same = stored is None or stored["identity"] == identity
            report(
                same,
                "Google access",
                f"{len(entries)} calendars as {identity}" + ("" if same else " (ACCOUNT CHANGED)"),
            )
        except BridgeError as exc:
            report(False, "Google access", str(exc))
        try:
            icloud = app.providers.icloud()
            discovery = icloud.discover()
            stored = app.repo.get_account("icloud")
            same = stored is None or stored["identity"] == discovery.principal_url
            report(
                same,
                "iCloud access",
                f"{len(discovery.calendars)} calendars" + ("" if same else " (PRINCIPAL CHANGED)"),
            )
        except BridgeError as exc:
            report(False, "iCloud access", str(exc))
    for m in app.repo.mappings(("active", "pending_initial", "paused")):
        g, i = app.repo.calendar(m.google_calendar_id), app.repo.calendar(m.icloud_calendar_id)
        ok = g.writable and i.writable and not g.missing_since and not i.missing_since
        report(
            ok,
            f"mapping '{m.name}' ({m.state})",
            "calendars present and writable" if ok else "calendar missing or read-only",
        )
        if m.destructive_hold:
            report(None, f"mapping '{m.name}' deletions suspended", m.destructive_hold_reason or "")
    heartbeat = app.repo.get_meta("heartbeat_at")
    age = seconds_since(heartbeat, app.clock.now())
    report((age is not None and age < app.settings.sync_interval * 3 + 60) or None, "daemon heartbeat", _ago(heartbeat))
    conflicts = app.repo.open_conflict_count()
    report(None if conflicts else True, "conflicts", f"{conflicts} open")
    if app.repo.get_meta("global_hold"):
        report(False, "global hold", str(app.repo.get_meta("global_hold")))
    from calendar_bridge.status.alerts import channels_from_settings

    channels = [c.name for c in channels_from_settings(app.settings)]
    report(True if channels else None, "alert channels", ", ".join(channels) or "none (see docs/setup.md, step 5)")
    sys.exit(1 if failures else 0)


# ====================================================================== conflicts


@cli.group()
def conflicts() -> None:
    """Inspect and resolve conflicts."""


@conflicts.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include resolved conflicts.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def conflicts_list(state: State, show_all: bool, as_json: bool) -> None:
    app = state.app
    states = ("open", "resolving", "resolved", "superseded") if show_all else ("open", "resolving")
    rows = app.repo.conflicts(states)
    if as_json:
        _json(
            [
                {
                    "id": c.id,
                    "mapping_id": c.mapping_id,
                    "item_id": c.item_id,
                    "kind": c.kind,
                    "fields": c.groups,
                    "state": c.state,
                    "reason": c.reason,
                    "created_at": c.created_at,
                    "resolution": c.resolution,
                }
                for c in rows
            ]
        )
        return
    if not rows:
        click.echo("No conflicts.")
        return
    _table(
        [
            [c.id, c.state, c.kind, c.item_id or "-", ", ".join(c.groups)[:40], _label(c), c.created_at[:16]]
            for c in rows
        ],
        ["id", "state", "kind", "item", "fields", "event", "since"],
    )


def _label(conflict: Any) -> str:
    for text in (conflict.icloud_json, conflict.google_json):
        if text:
            return CanonicalEvent.loads(text).describe()[:50]
    return "(see details)"


@conflicts.command("show")
@click.argument("conflict_id", type=int)
@click.option("--json", "as_json", is_flag=True)
@pass_state
def conflicts_show(state: State, conflict_id: int, as_json: bool) -> None:
    """Show both provider versions of a conflict."""
    app = state.app
    c = app.repo.conflict(conflict_id)
    g = CanonicalEvent.loads(c.google_json) if c.google_json else None
    i = CanonicalEvent.loads(c.icloud_json) if c.icloud_json else None
    if as_json:
        _json(
            {
                "id": c.id,
                "kind": c.kind,
                "state": c.state,
                "reason": c.reason,
                "fields": c.groups,
                "google": g.to_json() if g else None,
                "icloud": i.to_json() if i else None,
                "candidates": c.candidates,
                "resolution": c.resolution,
            }
        )
        return
    click.echo(f"Conflict #{c.id} ({c.kind}, {c.state}) on item {c.item_id}: {c.reason}")
    if c.candidates:
        click.echo("Candidates:")
        for cand in c.candidates:
            click.echo(f"  {cand}")
    gv, iv = (group_values(g) if g else {}), (group_values(i) if i else {})
    fields = sorted(set(c.groups) | {k for k in set(gv) | set(iv) if gv.get(k) != iv.get(k)}) if (g and i) else c.groups
    click.echo(f"\n{'field':<14} {'Google':<40} {'iCloud':<40}")
    for field in fields:
        google_text = _short(gv.get(field)) if g else "(deleted)"
        icloud_text = _short(iv.get(field)) if i else "(deleted)"
        click.echo(f"{field:<14} {google_text:<40} {icloud_text:<40}")
    from calendar_bridge.sync.resolve import RESOLUTIONS

    click.echo("\nResolve with: calendar-bridge conflicts resolve " + str(c.id) + " --keep google|icloud")
    for name, meaning in RESOLUTIONS.items():
        click.echo(f"  {name:<17} {meaning}")


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    text = text.replace("\n", " / ")
    return text if len(text) <= 38 else text[:35] + "..."


@conflicts.command("resolve")
@click.argument("conflict_id", type=int)
@click.option("--keep", type=click.Choice(["google", "icloud"]))
@click.option("--field", "fields", multiple=True, help="Per-field choice for --merge: NAME=google|icloud.")
@click.option("--merge", is_flag=True)
@click.option("--accept-delete", is_flag=True)
@click.option("--restore", is_flag=True)
@click.option("--delete-duplicate", is_flag=True)
@click.option("--keep-separate", is_flag=True)
@click.option("--link", nargs=2, metavar="GOOGLE_EVENT_ID ICLOUD_HREF")
@click.option("--now", "run_now", is_flag=True, help="Apply immediately instead of on the next cycle.")
@click.option("--yes", is_flag=True)
@pass_state
def conflicts_resolve(
    state: State,
    conflict_id: int,
    keep: str | None,
    fields: tuple[str, ...],
    merge: bool,
    accept_delete: bool,
    restore: bool,
    delete_duplicate: bool,
    keep_separate: bool,
    link: tuple[str, str] | None,
    run_now: bool,
    yes: bool,
) -> None:
    """Choose how to resolve a conflict; the engine applies it through the verified write path."""
    app = state.app
    c = app.repo.conflict(conflict_id)
    if c.state not in ("open", "resolving"):
        raise click.ClickException(f"conflict #{conflict_id} is {c.state}")
    chosen = [
        n
        for n, v in (
            ("keep", keep),
            ("merge", merge),
            ("accept_delete", accept_delete),
            ("restore", restore),
            ("delete_duplicate", delete_duplicate),
            ("keep_separate", keep_separate),
            ("link", link),
        )
        if v
    ]
    if len(chosen) != 1:
        raise click.ClickException("choose exactly one resolution option")
    data: dict[str, Any] = {}
    resolution = chosen[0]
    if resolution == "keep":
        resolution = f"keep_{keep}"
    if fields and resolution != "merge":
        raise click.ClickException("--field only goes with --merge")
    if resolution == "merge":
        parsed: dict[str, str] = {}
        for choice in fields:
            name, sep, side = choice.partition("=")
            if not sep or side not in ("google", "icloud"):
                raise click.ClickException(f"--field {choice!r}: write it as NAME=google or NAME=icloud")
            if name not in c.groups:
                raise click.ClickException(
                    f"--field {name!r} is not one of this conflict's fields: {', '.join(c.groups) or 'none'}"
                )
            parsed[name] = side
        missing = [g for g in c.groups if g not in parsed]
        if missing:
            raise click.ClickException("choose a side for every conflicting field; missing: " + ", ".join(missing))
        data["groups"] = parsed
    if resolution == "link":
        assert link is not None
        data.update(google_event_id=link[0], icloud_href=link[1])
    if c.kind == "ambiguous_match" and resolution not in ("link", "keep_separate"):
        raise click.ClickException("ambiguous matches are resolved with --link or --keep-separate")
    if resolution == "link":
        if c.kind != "ambiguous_match":
            raise click.ClickException("--link only applies to ambiguous initial matches")
        ids = {(cand["provider"], cand["id"]) for cand in c.candidates}
        if ("google", data["google_event_id"]) not in ids or ("icloud", data["icloud_href"]) not in ids:
            raise click.ClickException("both --link values must be candidates listed by `conflicts show`")
    if c.kind == "guest_managed" and not yes:
        item = app.repo.item(c.item_id) if c.item_id else None
        owner = item.authority if item else "the owning provider"
        if resolution in (f"keep_{'icloud' if owner == 'google' else 'google'}", "accept_delete"):
            click.confirm(
                f"This applies the mirror's change to {owner}, which manages the guest list; "
                f"{owner} may notify guests. Continue?",
                abort=True,
            )
    with _lock(app), app.db.tx():
        app.repo.update_conflict(conflict_id, state="resolving", resolution=resolution, resolution_data=data)
        if c.item_id is not None:
            app.repo.update_item(c.item_id, dirty=1)
        app.repo.audit(
            "conflict_resolution_requested",
            mapping_id=c.mapping_id,
            item_id=c.item_id,
            detail=f"#{conflict_id} {resolution}",
        )
    if run_now:
        with _lock(app):
            app.engine().run_cycle(mapping_ids=[c.mapping_id])
        c = app.repo.conflict(conflict_id)
        click.echo(f"Conflict #{conflict_id} is now {c.state}.")
    else:
        click.echo("Resolution recorded; it is applied on the next cycle (or use --now).")


# ====================================================================== items, safety, tombstones


@cli.group()
def items() -> None:
    """Inspect synchronized events."""


def _item_label(app: App, item: Any) -> str:
    text = item.icloud_synced_json or item.google_synced_json
    return CanonicalEvent.loads(text).describe()[:60] if text else ""


@items.command("list")
@click.option("--mapping", "mapping_id", type=int)
@click.option("--state", "state_filter", type=click.Choice(ITEM_STATES))
@click.option("--limit", type=click.IntRange(1, None), default=100, show_default=True, help="Most recent N.")
@click.option("--json", "as_json", is_flag=True)
@pass_state
def items_list(state: State, mapping_id: int | None, state_filter: str | None, limit: int, as_json: bool) -> None:
    """Synchronized events with their state (read-only)."""
    app = state.app
    rows = app.repo.items(mapping_id, state_filter)[-limit:]
    if as_json:
        _json(
            [
                {
                    "id": r.id,
                    "mapping_id": r.mapping_id,
                    "state": r.state,
                    "origin": r.origin,
                    "authority": r.authority,
                    "google_event_id": r.google_event_id,
                    "reason": r.state_reason,
                    "updated_at": r.updated_at,
                }
                for r in rows
            ]
        )
        return
    _table(
        [[r.id, r.state, r.origin, _item_label(app, r), (r.state_reason or "")[:50]] for r in rows],
        ["id", "state", "origin", "event", "reason"],
    )


@items.command("show")
@click.argument("item_id", type=int)
@pass_state
def items_show(state: State, item_id: int) -> None:
    app = state.app
    item = app.repo.item(item_id)
    click.echo(
        f"Item {item.id} (mapping {item.mapping_id}) state={item.state} origin={item.origin} "
        f"owner={item.authority} meeting={item.meeting}"
    )
    click.echo(f"  reason: {item.state_reason or '-'}")
    click.echo(f"  google: {item.google_event_id or '-'} (uid {item.google_ical_uid or '-'})")
    click.echo(f"  icloud: {item.icloud_href or '-'} (uid {item.icloud_uid or '-'})")
    click.echo(
        f"  last synced {item.last_synced_at or '-'}; verified google {item.last_google_verified_at or '-'}, "
        f"icloud {item.last_icloud_verified_at or '-'}"
    )
    for note in item.notes:
        click.echo(f"  note: {note}")
    for label, text in (("Google", item.google_synced_json), ("iCloud", item.icloud_synced_json)):
        if text:
            click.echo(f"  {label} baseline: {CanonicalEvent.loads(text).describe()}")
    click.echo("  recent writes:")
    for intent in app.repo.intents_for_item(item_id, 10):
        click.echo(
            f"    #{intent.id} {intent.created_at[:19]} {intent.provider} {intent.op} -> {intent.state}"
            + (f" ({intent.last_error})" if intent.last_error else "")
        )
    click.echo("  audit:")
    for row in app.repo.audit_entries(10, item_id):
        click.echo(f"    {row['at'][:19]} {row['action']} {row['provider'] or ''} {row['detail'] or ''}")


@items.command("retry")
@click.argument("item_id", type=int, required=False)
@click.option("--all-failed", is_flag=True)
@pass_state
def items_retry(state: State, item_id: int | None, all_failed: bool) -> None:
    """Retry failed/rejected items on the next cycle."""
    app = state.app
    targets = [app.repo.item(item_id)] if item_id else []
    if all_failed:
        targets += app.repo.items(state="failed") + app.repo.items(state="rejected")
    if not targets:
        raise click.ClickException("nothing to retry")
    with _lock(app), app.db.tx():
        for item in targets:
            app.repo.update_item(item.id, state="pending", dirty=1, retry_count=0, next_attempt_at=None)
    click.echo(f"{len(targets)} item(s) queued for retry.")


@cli.group()
def safety() -> None:
    """Destructive-write safety holds."""


@safety.command("status")
@pass_state
def safety_status(state: State) -> None:
    app = state.app
    click.echo(f"global hold: {app.repo.get_meta('global_hold') or 'none'}")
    for m in app.repo.mappings(("active", "paused", "pending_initial")):
        click.echo(
            f"mapping {m.id} '{m.name}': "
            + (
                f"SUSPENDED since {m.destructive_hold_since}: {m.destructive_hold_reason}"
                if m.destructive_hold
                else "deletions allowed"
            )
        )


@safety.command("release")
@click.option("--mapping", "mapping_id", type=int, required=True)
@click.option("--yes", is_flag=True)
@pass_state
def safety_release(state: State, mapping_id: int, yes: bool) -> None:
    """Allow the held deletions of a mapping to proceed once (within the next hour)."""
    app = state.app
    mapping = app.repo.mapping(mapping_id)
    if not mapping.destructive_hold:
        hint = " A global hold is set: see `calendar-bridge safety status`." if app.repo.get_meta("global_hold") else ""
        raise click.ClickException(f"no deletions are held for mapping '{mapping.name}'.{hint}")
    from calendar_bridge.sync.reconcile import HELD

    held = [i for i in app.repo.items(mapping_id, "pending") if (i.state_reason or "").startswith(HELD)]
    click.echo(f"Mapping '{mapping.name}': {mapping.destructive_hold_reason}")
    for item in held[:50]:
        click.echo(f"  held: item {item.id} {_item_label(app, item)}")
    if len(held) > 50:
        click.echo(f"  ... and {len(held) - 50} more (calendar-bridge items list --state pending)")
    if not yes:
        click.confirm(f"Let these {len(held)} deletion(s) run on the next cycle?", abort=True)
    with _lock(app), app.db.tx():
        app.repo.clear_hold(mapping_id)
        app.repo.set_meta_json(f"release_once:{mapping_id}", {"at": app.repo.now(), "items": [i.id for i in held]})
        app.repo.audit("destructive_hold_released", mapping_id=mapping_id, detail=f"{len(held)} deletion(s)")
    click.echo(f"Released {len(held)} deletion(s). Deletions requested from now on are checked again.")


@safety.command("clear-global")
@click.option("--yes", is_flag=True)
@pass_state
def safety_clear_global(state: State, yes: bool) -> None:
    """Clear a global hold after verifying the database is healthy."""
    app = state.app
    problems = app.db.integrity_check()
    if problems:
        raise click.ClickException("database still fails integrity checks: " + "; ".join(problems[:3]))
    if not yes:
        click.confirm(f"Clear global hold ({app.repo.get_meta('global_hold')})?", abort=True)
    with _lock(app), app.db.tx():
        app.repo.set_meta("global_hold", None)
        app.repo.audit("global_hold_cleared", detail="by user")
    click.echo("Global hold cleared.")


@cli.group()
def tombstones() -> None:
    """Deleted events kept for recovery."""


@tombstones.command("list")
@click.option("--mapping", "mapping_id", type=int)
@pass_state
def tombstones_list(state: State, mapping_id: int | None) -> None:
    """Deleted events that can still be restored."""
    app = state.app
    rows = app.repo.items(mapping_id, "deleted")
    if not rows:
        click.echo("No deleted events are kept.")
        return
    _table(
        [
            [
                r.id,
                _local_time(app, r.deleted_at),
                r.deleted_by,
                _item_label(app, r),
                ", ".join(f"as item {n}" for n in app.repo.restored_copies(r.id)),
            ]
            for r in rows
        ],
        ["id", "deleted", "by", "event", "restored"],
    )


@tombstones.command("restore")
@click.argument("item_id", type=int)
@pass_state
def tombstones_restore(state: State, item_id: int) -> None:
    """Re-create a deleted event in iCloud from its last synchronized version (it is then
    mirrored to Google as a new event)."""
    import uuid

    from calendar_bridge.sync.executor import Executor

    app = state.app
    item = app.repo.item(item_id)
    if item.state != "deleted":
        raise click.ClickException("only deleted items can be restored")
    copies = app.repo.restored_copies(item_id)
    if copies:
        raise click.ClickException(f"item {item_id} was already restored as item {copies[0]}")
    text = item.icloud_synced_json or item.google_synced_json
    if not text:
        raise click.ClickException("no stored version to restore from")
    event = CanonicalEvent.loads(text)
    with _lock(app):
        mc = MappingContext.build(app.ctx, app.repo.mapping(item.mapping_id))
        with app.db.tx():
            new_id = app.repo.create_item(
                mapping_id=item.mapping_id, origin="icloud", authority="icloud", state="pending"
            )
            app.repo.audit(
                "tombstone_restore", mapping_id=item.mapping_id, item_id=new_id, detail=f"from item {item_id}"
            )
        executor = Executor(app.ctx, mc, app.providers.google(), app.providers.icloud())
        uid = f"{uuid.uuid4()}@calendar-bridge"
        view, _ = executor.create_icloud(app.repo.item(new_id), event, uid, None)
        executor.close("verified")
        with app.db.tx():
            app.repo.update_item(new_id, icloud_synced_json=view.event.dumps() if view.event else None, dirty=1)
    click.echo(f"Restored as item {new_id} in iCloud; the next cycle copies it to Google.")


# ====================================================================== database


@cli.group()
def db() -> None:
    """Database maintenance."""


@db.command("check")
@pass_state
def db_check(state: State) -> None:
    app = state.app
    problems = app.db.integrity_check()
    click.echo(f"schema v{app.db.schema_version()}, pending migrations: {len(app.db.pending_migrations())}")
    if problems:
        click.echo("INTEGRITY PROBLEMS:\n  " + "\n  ".join(problems))
        sys.exit(1)
    click.echo("integrity ok")


@db.command("backup")
@click.option("--label", default="manual")
@pass_state
def db_backup(state: State, label: str) -> None:
    app = state.app
    path = create_backup(
        app.db, app.settings.backup_dir, box=app.maybe_box(), keep=app.settings.backup_keep, label=label
    )
    click.echo(f"Backup written: {path}" + ("" if path.name.endswith(".enc") else "  (UNENCRYPTED: no key configured)"))


@db.command("backups")
@pass_state
def db_backups(state: State) -> None:
    for path in list_backups(state.app.settings.backup_dir):
        click.echo(f"{path.name}  {path.stat().st_size // 1024} KiB")


@db.command("restore")
@click.argument("backup", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--yes", is_flag=True)
@pass_state
def db_restore(state: State, backup: Path, yes: bool) -> None:
    """Restore a backup. Stop the daemon first."""
    settings = state.settings
    if not yes:
        click.confirm("Replace the current database with this backup? (The current one is kept aside.)", abort=True)
    with daemon_lock(settings), cycle_lock(settings, timeout=5):
        if state._app is not None:
            state._app.close()
            state._app = None
        keys = load_keys(settings.key_file)
        from calendar_bridge.crypto import SecretBox

        box = SecretBox(keys) if keys else None
        from calendar_bridge.clock import SystemClock

        aside = restore_backup(backup, settings.db_path, box=box, clock=SystemClock())
        app = open_app(settings)
        state._app = app
        with app.db.tx():
            app.repo.request_full_resync()
            for mapping in app.repo.mappings(("active",)):
                app.repo.mark_all_dirty(mapping.id)
            app.repo.set_meta("restored_at", app.repo.now())
            app.repo.set_meta("credentials_changed_at", app.repo.now())
            app.repo.audit("database_restored", detail=backup.name)
    click.echo(f"Restored from {backup.name}. Previous database kept as {aside.name}.")
    click.echo("All mappings will be fully re-read on the next cycle; events created after the backup are")
    click.echo("re-linked through their bridge markers instead of being duplicated.")


@db.command("migrate")
@pass_state
def db_migrate(state: State) -> None:
    app = state.app  # open_app applies pending migrations (after an automatic backup)
    click.echo(f"schema v{app.db.schema_version()}")


# ====================================================================== status calendar & alerts


@cli.command("publish-status")
@click.option("--force", is_flag=True)
@pass_state
def publish_status(state: State, force: bool) -> None:
    """Publish the status event to the Google status calendar now."""
    from calendar_bridge.status.report import build_status
    from calendar_bridge.status.status_calendar import StatusCalendar

    app = state.app
    if not app.settings.status_calendar:
        raise click.ClickException("the status calendar is turned off (CB_STATUS_CALENDAR=false)")
    status_doc = build_status(app.ctx)
    with _lock(app):
        published = StatusCalendar(app.ctx, app.providers.google()).publish(status_doc, force=force)
    click.echo("Status published." if published else "Status unchanged; nothing published (use --force).")


@cli.command("test-alert")
@pass_state
def test_alert(state: State) -> None:
    """Send a test message through every configured alert channel."""
    from calendar_bridge.status.alerts import channels_from_settings

    channels = channels_from_settings(state.settings)
    if not channels:
        raise click.ClickException("no alert channel configured (CB_ALERT_NTFY_URL, CB_ALERT_WEBHOOK_URL or e-mail)")
    failed = 0
    for channel in channels:
        try:
            channel.send("Test alert", "Calendar Bridge alert channel works.", "info")
            click.echo(f"{channel.name}: sent")
        except Exception as exc:
            failed += 1
            response = getattr(exc, "response", None)
            status = f" HTTP {response.status_code}" if response is not None else ""
            click.echo(f"{channel.name}: FAILED ({type(exc).__name__}{status})")
    if failed:
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
