import argparse
import platform
import sys

from agyswap import __version__, credentials, paths, quota
from agyswap.switcher import AccountNotFoundError, AgyError, AgySwitcher, current_email

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()


def platform_label() -> str:
    """Human OS name.

    ``platform.release()`` is useless here: Windows 11 still reports "10",
    because the NT major version never moved.  The build number is what
    actually distinguishes 11 (22000+).
    """
    if sys.platform == "win32":
        try:
            build = sys.getwindowsversion().build
        except Exception:
            build = 0
        if build >= 22000:
            return "Windows 11"
        if build >= 10240:
            return "Windows 10"
        return f"Windows {platform.release()}"
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0] or platform.release()}".strip()
    return f"Linux {platform.release()}".strip()


def _quota_summary(row: dict) -> str:
    """One column can only say so much: report the most constrained window."""
    windows = row.get("quota_windows") or []
    if not windows:
        status = row.get("quota_status") or "unknown"
        return "—" if status in ("unknown", "") else status
    worst = max(windows, key=lambda window: window["used_pct"])
    return f"{worst['label']} {worst['used_pct']:.0f}%"


def cmd_list(sw: AgySwitcher, args) -> None:
    rows = sw.list_accounts()
    if not rows:
        console.print("[dim]No accounts managed yet.[/dim]")
        console.print("Login with [bold]agy[/bold] first, then run [bold]agyswap add[/bold].")
        return

    table = Table(box=box.ROUNDED)
    table.add_column("#", style="bold")
    table.add_column("Email")
    table.add_column("Alias")
    table.add_column("Status")
    table.add_column("Token")
    table.add_column("Quota")

    for row in rows:
        status_parts = []
        if row["active"]:
            status_parts.append("[green]active[/green]")
        if row["disabled"]:
            status_parts.append("[yellow]disabled[/yellow]")
        status = " ".join(status_parts) if status_parts else "[dim]—[/dim]"
        token = "[green]✓[/green]" if row["has_token"] else "[red]✗[/red]"
        table.add_row(
            str(row["number"]),
            row["email"],
            row["alias"] or "",
            status,
            token,
            _quota_summary(row),
        )

    console.print(table)


def cmd_status(sw: AgySwitcher, args) -> None:
    info = sw.status()
    if info["active"] is None:
        console.print("[yellow]No active agy login detected.[/yellow]")
        console.print("Run [bold]agy[/bold] to login, then [bold]agyswap add[/bold].")
        return
    managed = "yes" if info["managed"] else "[yellow]not managed[/yellow]"
    console.print(f"Active account: [bold]{info['active']}[/bold]")
    console.print(f"Managed: {managed}")
    if info["number"]:
        console.print(f"Slot: #{info['number']}")


def cmd_add(sw: AgySwitcher, args) -> None:
    try:
        account = sw.add_current(email_override=args.email)
    except (AgyError, AccountNotFoundError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    console.print(f"[green]Added[/green] account #{account.number}: {account.email}")


def cmd_remove(sw: AgySwitcher, args) -> None:
    try:
        account = sw.remove_account(args.identifier)
    except AccountNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    console.print(f"[red]Removed[/red] account #{account.number}: {account.email}")


def cmd_switch(sw: AgySwitcher, args) -> None:
    try:
        result = sw.switch(args.identifier)
    except (AgyError, AccountNotFoundError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    previous = result["previous"] or "(none)"
    console.print(
        f"Switched: [dim]{previous}[/dim] → [bold green]{result['active']}[/bold green]"
    )
    if not result.get("targets"):
        console.print(
            "[yellow]Warning:[/yellow] could not write the token to any store. "
            "Run [bold]agyswap doctor[/bold]."
        )
    console.print(
        "[dim]agy caches its token at startup — the change applies to new sessions.[/dim]"
    )


def cmd_disable(sw: AgySwitcher, args) -> None:
    try:
        account = sw.set_disabled(args.identifier, True)
    except AccountNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    console.print(f"[yellow]Disabled[/yellow] account #{account.number}: {account.email}")


def cmd_enable(sw: AgySwitcher, args) -> None:
    try:
        account = sw.set_disabled(args.identifier, False)
    except AccountNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    console.print(f"[green]Enabled[/green] account #{account.number}: {account.email}")


def cmd_alias(sw: AgySwitcher, args) -> None:
    try:
        if args.unset:
            account = sw.unalias(args.identifier)
            console.print(f"Removed alias for #{account.number}: {account.email}")
        elif args.name:
            account = sw.alias(args.identifier, args.name)
            console.print(
                f"Set alias [bold]'{args.name}'[/bold] for #{account.number}: {account.email}"
            )
        else:
            rows = sw.list_accounts()
            found = False
            for row in rows:
                if row["alias"]:
                    console.print(
                        f"  {row['number']}: {row['alias']} [dim]({row['email']})[/dim]"
                    )
                    found = True
            if not found:
                console.print("[dim]No aliases set.[/dim]")
    except AccountNotFoundError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)


def cmd_tui(sw: AgySwitcher, args) -> None:
    try:
        from agyswap.tui.app import run_tui
    except ImportError as exc:
        console.print(f"[red]TUI unavailable:[/red] {exc}")
        console.print("Reinstall with: pip install --force-reinstall agy-swap")
        sys.exit(1)
    run_tui(sw, start_watch=getattr(args, "command", "") == "watch")


def cmd_doctor(sw: AgySwitcher, args) -> None:
    """Print what agy-swap can actually see on this machine."""
    from agyswap.timeutil import format_duration
    import time

    table = Table(box=box.SIMPLE_HEAD, show_header=False)
    table.add_column("", style="dim", width=12)
    table.add_column("")

    table.add_row("version", __version__)
    table.add_row(
        "platform",
        f"{platform_label()} · {sys.platform} · Python {platform.python_version()}",
    )
    table.add_row("gemini dir", str(paths.GEMINI_DIR) + ("" if paths.GEMINI_DIR.exists() else " [red](missing)[/red]"))

    ring = credentials.keyring_status()
    ring_state = "[green]available[/green]" if ring["available"] else "[yellow]not found[/yellow]"
    table.add_row("keyring", f"{ring['backend']} · {ring_state}")

    token, source = credentials.read_token()
    if token:
        email = credentials.email_from_token(token, allow_network=False)
        expiry = credentials.token_expiry(token)
        if expiry is None:
            expiry_text = "unknown"
        else:
            delta = expiry - time.time()
            expiry_text = "expired" if delta <= 0 else f"{format_duration(delta)} left"
        table.add_row("token", f"{source} · {expiry_text}")
        table.add_row("identity", email or "[yellow]unknown[/yellow]")

        # agy can leave the keyring and the file holding *different* accounts
        # (they are written at different times).  Say so rather than silently
        # picking one and looking confident.
        disagreement = credentials.token_sources_disagree()
        if disagreement:
            table.add_row("note", f"[yellow]{disagreement}[/yellow]")
    else:
        table.add_row("token", "[red]not found[/red]")
        table.add_row("identity", current_email(allow_network=False) or "[yellow]unknown[/yellow]")

    accounts = sw.list_accounts()
    table.add_row("accounts", str(len(accounts)) if accounts else "[dim]none[/dim]")

    cache = quota.load_cache()
    entries = cache.get("accounts") or {}
    if paths.QUOTA_CACHE.exists():
        table.add_row(
            "quota cache",
            f"schema {cache.get('schema')} · {len(entries)} entry(ies) · {paths.QUOTA_CACHE}",
        )
    else:
        table.add_row("quota cache", "[dim]empty — no samples yet[/dim]")

    statusline_command = (
        f'python -m agyswap.statusline --chain "{args.chain}"'
        if getattr(args, "chain", None)
        else "python -m agyswap.statusline"
    )

    console.print(table)
    console.print(
        "\n[dim]Status-line hook — quota with no token and no network:[/dim]\n"
        f'  settings.json → statusLine.command = "{statusline_command}"'
    )

    if token and credentials.token_is_expired(token):
        console.print(
            "\n[yellow]The active token has expired.[/yellow] Run [bold]agy[/bold] once "
            "to refresh it, or configure OAuth refresh (see README)."
        )


def cmd_statusline(sw, args) -> None:
    if args.setup:
        command = "python -m agyswap.statusline"
        if args.chain:
            command += f' --chain "{args.chain}"'
        # The snippet must be valid JSON, so inner quotes are escaped; and the
        # path may contain brackets, so rich markup stays off.
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        console.print("Add this to [bold]~/.gemini/settings.json[/bold]:\n")
        console.print(
            '  "statusLine": {\n'
            '    "type": "",\n'
            f'    "command": "{escaped}",\n'
            '    "enabled": true\n'
            "  }",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        console.print()
        console.print(
            "[dim]This captures agy's real quota windows "
            "(gemini-weekly / 3p-weekly) with no network or token needed.[/dim]"
        )
        if args.chain:
            console.print(
                "[dim]--chain forwards the same input to your existing script.[/dim]"
            )
        return

    from agyswap.statusline import main as statusline_main

    argv = ["--chain", args.chain] if args.chain else []
    sys.exit(statusline_main(argv))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agyswap",
        description="Multi-account switcher for Antigravity CLI (agy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  agyswap add              capture current login as a managed account
  agyswap list             list managed accounts
  agyswap switch           rotate to next enabled account
  agyswap switch 2         switch to account #2
  agyswap switch email     switch to account by email
  agyswap disable 2        hold an account out of rotation
  agyswap enable 2         return an account to rotation
  agyswap remove 2         remove an account
  agyswap alias 2 work     set an alias
  agyswap alias 2 --unset  remove an alias
  agyswap tui              interactive dashboard
  agyswap watch            dashboard on the watch page
  agyswap doctor           show what agy-swap can see on this machine
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list managed accounts")
    sub.add_parser("status", help="show current active account")
    p_add = sub.add_parser("add", help="capture current agy login as a managed account")
    p_add.add_argument("--email", help="email for the account (auto-detected if omitted)")

    p_switch = sub.add_parser("switch", help="switch accounts")
    p_switch.add_argument("identifier", nargs="?", help="account number, email, or alias")

    p_remove = sub.add_parser("remove", aliases=["rm"], help="remove an account")
    p_remove.add_argument("identifier", help="account number, email, or alias")

    p_disable = sub.add_parser("disable", help="hold account out of rotation")
    p_disable.add_argument("identifier", help="account number, email, or alias")

    p_enable = sub.add_parser("enable", help="return account to rotation")
    p_enable.add_argument("identifier", help="account number, email, or alias")

    p_alias = sub.add_parser("alias", help="manage account aliases")
    p_alias.add_argument("identifier", nargs="?", help="account number, email, or alias")
    p_alias.add_argument("name", nargs="?", help="alias name")
    p_alias.add_argument("--unset", action="store_true", help="remove alias")

    sub.add_parser("tui", help="launch interactive dashboard")
    sub.add_parser("watch", help="dashboard on the watch page")

    p_doctor = sub.add_parser("doctor", help="diagnose token/keyring/quota on this machine")
    p_doctor.add_argument("--chain", help="status-line command to chain, for the hint")

    p_statusline = sub.add_parser("statusline", help="capture quota from agy's status line")
    p_statusline.add_argument("--setup", action="store_true", help="print setup instructions")
    p_statusline.add_argument("--chain", help="also run this command, forwarding its output")

    args = parser.parse_args()

    switcher = AgySwitcher()

    if not args.command:
        cmd_tui(switcher, args)
        return

    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "add": cmd_add,
        "switch": cmd_switch,
        "remove": cmd_remove,
        "rm": cmd_remove,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "alias": cmd_alias,
        "tui": cmd_tui,
        "watch": cmd_tui,
        "doctor": cmd_doctor,
        "statusline": cmd_statusline,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(switcher, args)


if __name__ == "__main__":
    main()
