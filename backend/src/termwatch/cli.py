"""The ``termwatch`` command-line interface."""

from __future__ import annotations

import os
import sys
import time
from typing import Annotated

import typer

from termwatch import __version__
from termwatch.auth import CredentialsError, ensure_credentials, ensure_private_dir, rotate_credentials
from termwatch.config import ConfigError, Settings, is_loopback_host, load_settings, write_default_config

app = typer.Typer(
    name="termwatch",
    help=(
        "Monitor long-running terminal processes from your phone.\n\n"
        "Start the server in one terminal (`termwatch server`), then run commands through "
        "`termwatch run --name NAME -- COMMAND...` in others and watch them in the dashboard."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
token_app = typer.Typer(help="Show or rotate the dashboard access token.", no_args_is_help=True)
app.add_typer(token_app, name="token")

EXIT_USAGE = 2


def _style(text: str, color: str, *, stream: object = sys.stderr) -> str:
    isatty = getattr(stream, "isatty", lambda: False)
    if os.environ.get("NO_COLOR") or not isatty():
        return text
    return typer.style(text, fg=color)


def _notice(message: str) -> None:
    typer.echo(_style(f"[termwatch] {message}", "cyan"), err=True)


def _warn(message: str) -> None:
    typer.echo(_style(f"[termwatch] warning: {message}", "yellow"), err=True)


def _fail(message: str, code: int = 1) -> typer.Exit:
    typer.echo(_style(f"termwatch: error: {message}", "red"), err=True)
    return typer.Exit(code)


def _load(overrides: dict[str, object] | None = None) -> Settings:
    try:
        return load_settings(overrides)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"termwatch {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool, typer.Option("--version", callback=_version_callback, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """Monitor long-running terminal processes from your phone."""


@app.command()
def init() -> None:
    """Create the configuration directory, a config template and access tokens."""
    settings = _load()
    ensure_private_dir(settings.config_dir)
    ensure_private_dir(settings.data_dir)
    created_config = write_default_config(settings.config_file)
    try:
        _, created_tokens = ensure_credentials(settings.credentials_file)
    except CredentialsError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Config file:  {settings.config_file}{'  (created)' if created_config else ''}")
    typer.echo(f"Credentials:  {settings.credentials_file}{'  (created, mode 600)' if created_tokens else ''}")
    typer.echo(f"Data dir:     {settings.data_dir}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo("  1. termwatch server                         # keep this terminal open")
    typer.echo('  2. termwatch run --name "My job" -- python script.py')
    typer.echo("  3. termwatch token show                     # sign-in link for the dashboard")


@app.command()
def server(
    host: Annotated[
        str | None,
        typer.Option(help="Interface to bind (default 127.0.0.1). See docs/security.md before changing it."),
    ] = None,
    port: Annotated[int | None, typer.Option(help="Port to listen on (default 8765).")] = None,
    log_level: Annotated[str, typer.Option(help="debug, info, warning or error.")] = "info",
) -> None:
    """Start the TermWatch server and dashboard. Keep it running while you monitor commands."""
    from termwatch.server import ServerStartError, check_port_available, run_server

    settings = _load({"host": host, "port": port})
    try:
        _, created = ensure_credentials(settings.credentials_file)
        check_port_available(settings.host, settings.port)
    except (CredentialsError, ServerStartError) as exc:
        raise _fail(str(exc)) from exc
    write_default_config(settings.config_file)

    typer.echo(f"TermWatch {__version__} listening on http://{settings.host}:{settings.port}", err=True)
    typer.echo(f"  dashboard (this computer): {settings.local_url}", err=True)
    typer.echo("  sign-in link:              run `termwatch token show` in another terminal", err=True)
    if created:
        typer.echo(f"  new access token saved in  {settings.credentials_file}", err=True)
    if not is_loopback_host(settings.host):
        _warn(
            f"listening on {settings.host}, so other machines that can reach this address can "
            "connect. Only do this on a private network or VPN you trust (e.g. Tailscale), keep "
            "the port firewalled from everything else, and never expose it to the public internet. "
            "Traffic is plain HTTP unless you put an HTTPS proxy such as `tailscale serve` in front."
        )
    try:
        run_server(settings, log_level=log_level)
    except ServerStartError as exc:
        raise _fail(str(exc)) from exc


def _resolve_server_url(explicit: str | None, settings: Settings) -> str:
    if explicit:
        return explicit.rstrip("/")
    from termwatch.server import read_runtime_url

    return read_runtime_url(settings.runtime_file) or settings.local_url


@app.command(context_settings={"allow_interspersed_args": False})
def run(
    command: Annotated[
        list[str],
        typer.Argument(metavar="-- COMMAND [ARGS]...", help="The command to run, after `--`.", show_default=False),
    ],
    name: Annotated[
        str | None, typer.Option("--name", "-n", help="Session name shown in the dashboard (need not be unique).")
    ] = None,
    server_url: Annotated[
        str | None,
        typer.Option("--server", envvar="TERMWATCH_SERVER_URL", help="Server URL (default: the local server)."),
    ] = None,
    hide_command: Annotated[
        bool, typer.Option("--hide-command", help="Do not send the command line or working directory.")
    ] = False,
    flush_timeout: Annotated[
        float, typer.Option(min=0, help="Seconds to keep trying to deliver output after the command exits.")
    ] = 15.0,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only print TermWatch warnings.")] = False,
) -> None:
    """Run COMMAND and stream its output to the dashboard. Exits with COMMAND's exit code."""
    from termwatch.client import ApiClient
    from termwatch.runner import default_session_name, run_monitored

    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise _fail("no command given. Usage: termwatch run --name NAME -- COMMAND [ARGS]...", EXIT_USAGE)
    settings = _load()
    try:
        credentials, _ = ensure_credentials(settings.credentials_file)
    except CredentialsError as exc:
        raise _fail(str(exc)) from exc

    session_name = (name or "").strip() or ("Unnamed session" if hide_command else default_session_name(command))
    url = _resolve_server_url(server_url, settings)
    client = ApiClient(url, credentials.agent_token)
    notice = (lambda _message: None) if quiet else _notice

    def announce(session_id: str) -> None:
        notice(f'Monitoring "{session_name}" -> {url}/sessions/{session_id}')

    try:
        outcome = run_monitored(
            command,
            name=session_name,
            client=client,
            hide_command=hide_command,
            flush_timeout=flush_timeout,
            local_out=sys.stdout.buffer,
            notice=notice,
            warn=_warn,
            on_session=announce,
        )
    finally:
        client.close()
    detail = f"exit code {outcome.exit_code}"
    if outcome.signal_name:
        detail += f", {outcome.signal_name}"
    notice(f'"{session_name}" {outcome.status.value} after {_format_duration(outcome.duration)} ({detail})')
    raise typer.Exit(outcome.exit_code)


@app.command("list")
def list_sessions(
    limit: Annotated[int, typer.Option(min=1, max=1000, help="Maximum sessions to show.")] = 20,
    server_url: Annotated[str | None, typer.Option("--server", envvar="TERMWATCH_SERVER_URL")] = None,
) -> None:
    """List recent sessions."""
    from termwatch.client import ApiClient, ApiError

    settings = _load()
    try:
        credentials, _ = ensure_credentials(settings.credentials_file)
    except CredentialsError as exc:
        raise _fail(str(exc)) from exc
    client = ApiClient(_resolve_server_url(server_url, settings), credentials.agent_token)
    try:
        data = client.list_sessions(limit)
    except ApiError as exc:
        raise _fail(f"cannot list sessions from {client.base_url}: {exc}") from exc
    finally:
        client.close()
    sessions = data["sessions"]
    if not sessions:
        typer.echo('No sessions yet. Try: termwatch run --name "Hello" -- echo hello')
        return
    now = data["server_time"]
    for session in sessions:
        end = session["ended_at"] or now
        runtime = _format_duration(end - session["started_at"])
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(session["started_at"]))
        code = "" if session["exit_code"] is None else f" exit={session['exit_code']}"
        typer.echo(f"{session['id'][:8]}  {session['status']:<12} {started}  {runtime:>11}  {session['name']}{code}")


@app.command("config")
def show_config() -> None:
    """Show configuration paths and effective settings (never secrets)."""
    settings = _load()
    for key, value in settings.model_dump().items():
        typer.echo(f"{key:<28} {value}")
    typer.echo(f"{'config_file':<28} {settings.config_file}")
    typer.echo(f"{'credentials_file':<28} {settings.credentials_file}")
    typer.echo(f"{'database_file':<28} {settings.database_file}")


@token_app.command("show")
def token_show(
    url: Annotated[
        str | None,
        typer.Option(help="Dashboard URL your phone will open, e.g. https://mybox.tailnet-name.ts.net"),
    ] = None,
    qr: Annotated[bool, typer.Option("--qr", help="Also print the sign-in link as a QR code.")] = False,
) -> None:
    """Print the read-only dashboard token and a sign-in link."""
    settings = _load()
    try:
        credentials, _ = ensure_credentials(settings.credentials_file)
    except CredentialsError as exc:
        raise _fail(str(exc)) from exc
    base = (url or settings.local_url).rstrip("/")
    link = f"{base}/#token={credentials.access_token}"
    typer.echo("Dashboard access token (read-only; treat it like a password):")
    typer.echo(f"  {credentials.access_token}")
    typer.echo("")
    typer.echo("Sign-in link (the token is in the #fragment, which browsers never send to servers):")
    typer.echo(f"  {link}")
    if qr:
        import segno

        typer.echo("")
        segno.make(link, error="m").terminal(compact=True)
    elif url is None:
        typer.echo("")
        typer.echo("For your phone: termwatch token show --url https://<your-machine>.<tailnet>.ts.net --qr")


@token_app.command("rotate")
def token_rotate(
    all_tokens: Annotated[
        bool,
        typer.Option("--all", help="Also rotate the CLI agent token (restart running `termwatch run` commands)."),
    ] = False,
) -> None:
    """Replace the dashboard token. Signed-in dashboards are disconnected within seconds."""
    settings = _load()
    try:
        credentials = rotate_credentials(settings.credentials_file, rotate_agent=all_tokens)
    except CredentialsError as exc:
        raise _fail(f"{exc}. Use --all to regenerate both tokens.") from exc
    typer.echo("New dashboard access token:")
    typer.echo(f"  {credentials.access_token}")
    typer.echo("The running server picks this up automatically; devices using the old token are signed out.")
    if all_tokens:
        typer.echo("The agent token was rotated too: `termwatch run` commands started before now can no longer upload.")


def main() -> None:
    app()
