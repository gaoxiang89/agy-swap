import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from agyswap import __version__, credentials, paths, quota
from agyswap.switcher import AccountNotFoundError, AgyError, AgySwitcher, current_email

from rich.console import Console
from rich.table import Table
from rich import box

console = Console()
error_console = Console(stderr=True)

_AGY_PROFILE_FLAGS = {"gemini_dir", "app_data_dir", "cli_dir_name"}


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
        # platform.release() itself calls getwindowsversion(), so it can fail for
        # the very reason this branch exists.
        try:
            return f"Windows {platform.release()}"
        except Exception:
            return "Windows"
    if sys.platform == "darwin":
        return f"macOS {platform.mac_ver()[0] or platform.release()}".strip()
    return f"Linux {platform.release()}".strip()


def _source_label(source: str) -> str:
    """Short name for a token store: ``keyring:wincred`` or the file's name."""
    return source if source.startswith("keyring:") else Path(source).name


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
        console.print(
            "Run [bold]agyswap login[/bold] to sign in or "
            "[bold]agyswap add[/bold] to import an existing login."
        )
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
    if credentials.ssh_session() and not credentials.has_wrapped_token(paths.SSH_TOKEN_PATH):
        console.print(
            "[yellow]Note:[/yellow] agy's SSH token file is missing or invalid — "
            "run [bold]agyswap sync[/bold] before starting agy."
        )


def cmd_sync(sw: AgySwitcher, args) -> None:
    try:
        result = sw.sync(if_missing=args.if_missing)
    except AgyError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    if result["status"] == "present":
        if not args.quiet:
            console.print(f"[dim]Token file already present:[/dim] {result['path']}")
        return
    if result["path"] not in result["targets"]:
        console.print(
            f"[red]Error:[/red] could not write {result['path']}. "
            "Run [bold]agyswap doctor[/bold]."
        )
        sys.exit(1)
    if args.quiet:
        return
    console.print(f"[green]Synced[/green] token from {result['source']}")
    for target in result["targets"]:
        console.print(f"  {target}")
    console.print("[dim]Restart agy for the token to take effect.[/dim]")


def _agy_executable() -> Optional[str]:
    """Locate agy without assuming where the install put it."""
    override = os.environ.get("AGY_EXE")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found

    found = shutil.which("agy")
    if found:
        return found

    candidates: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "agy" / "bin" / "agy.exe")
    candidates.extend(
        [
            Path.home() / ".local" / "bin" / "agy",
            Path("/usr/local/bin/agy"),
            Path("/opt/homebrew/bin/agy"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _launch_agy(argv: list[str], env: dict) -> int:
    """Start agy and return its exit status (kept separate for testing)."""
    return subprocess.run(argv, env=env).returncode


def _isolated_agy_env() -> dict[str, str]:
    # The OS keyring's single token slot is shared across Gemini roots.
    return {**os.environ, "AGY_CLI_FORCE_FILE_TOKEN_STORAGE": "1"}


def cmd_login(sw: AgySwitcher, args) -> None:
    executable = _agy_executable()
    if not executable:
        error_console.print(
            "[red]Error:[/red] cannot find agy. Install Antigravity CLI or set "
            "[bold]AGY_EXE[/bold] to its executable."
        )
        sys.exit(1)

    try:
        with tempfile.TemporaryDirectory(
            prefix="login-", dir=paths.BACKUP_ROOT
        ) as directory:
            profile = Path(directory)
            credentials.secure_dir(profile)
            argv = [executable, "acp", "--login", f"--gemini_dir={profile}"]
            code = _launch_agy(argv, _isolated_agy_env())
            if code != 0:
                error_console.print(f"[red]agy login failed[/red] (exit {code}).")
                sys.exit(code)
            account = sw.import_profile_login(profile)
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, AgyError) as exc:
        error_console.print(f"Error: {exc}", markup=False, highlight=False)
        sys.exit(1)

    console.print(
        f"Saved account #{account.number}: {account.email}. "
        f"Start it with agyswap run {account.number}.",
        markup=False,
    )


def cmd_run(sw: AgySwitcher, args) -> None:
    """Launch agy with one account's private Gemini root."""
    agy_args = list(args.agy_args or [])
    for arg in agy_args:
        flag = arg.partition("=")[0]
        name = flag.lstrip("-").replace("-", "_")
        if flag.startswith("-") and name in _AGY_PROFILE_FLAGS:
            error_console.print(f"[red]Error:[/red] agyswap run manages {flag}.")
            sys.exit(1)

    executable = _agy_executable()
    if not executable:
        error_console.print(
            "[red]Error:[/red] cannot find agy. Install Antigravity CLI or set "
            "[bold]AGY_EXE[/bold] to its executable."
        )
        sys.exit(1)

    try:
        info = sw.prepare_profile(args.identifier)
    except AgyError as exc:
        error_console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    profile = Path(info["profile"])
    env = _isolated_agy_env()
    # Statusline quota capture runs as a child of agy; this tells it which
    # account the samples belong to even when the payload omits an email.
    env["AGYSWAP_PROFILE_EMAIL"] = info["email"]
    env["AGYSWAP_DATA_DIR"] = str(paths.BACKUP_ROOT)

    argv = [executable, f"--gemini_dir={profile}", *agy_args]
    error_console.print(f"Running agy with profile for {info['email']}", markup=False)
    error_console.print(str(profile), style="dim", markup=False, soft_wrap=True)

    code = 1
    try:
        code = _launch_agy(argv, env)
    except OSError as exc:
        error_console.print(f"[red]Error:[/red] could not start agy: {exc}")
    finally:
        try:
            sw.capture_profile(info["email"], profile)
        except Exception:
            # A failed capture must never hide agy's own exit status.
            pass
    sys.exit(code)


def cmd_add(sw: AgySwitcher, args) -> None:
    try:
        captured = sw.add_sources(email_override=args.email)
    except (AgyError, AccountNotFoundError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    if len(captured) == 1:
        _source, account = captured[0]
        console.print(f"[green]Added[/green] account #{account.number}: {account.email}")
        return
    console.print(f"[green]Added[/green] {len(captured)} live logins:")
    for source, account in captured:
        console.print(
            f"  #{account.number} {account.email}  [dim]({_source_label(source)})[/dim]"
        )


def cmd_recover(sw: AgySwitcher, args) -> None:
    recovered = sw.recover_accounts()
    if not recovered:
        console.print(
            "[dim]Nothing to recover: every backup is already registered.[/dim]"
        )
        return
    console.print(
        f"[green]Recovered[/green] {len(recovered)} account(s) from backups:"
    )
    for account in recovered:
        console.print(f"  #{account.number} {account.email}")


def cmd_remove(sw: AgySwitcher, args) -> None:
    try:
        accounts = sw.remove_accounts(args.identifiers)
    except (AgyError, OSError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    for account in accounts:
        console.print(f"[red]Removed[/red] account #{account.number}: {account.email}")


def cmd_renumber(sw: AgySwitcher, args) -> None:
    try:
        changed = sw.renumber()
    except OSError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    if changed:
        console.print(f"[green]Renumbered[/green] {changed} account(s).")
    else:
        console.print("Account numbers are already consecutive.")


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

    ssh_marker = credentials.ssh_session()
    if ssh_marker:
        table.add_row(
            "session",
            f"[yellow]SSH[/yellow] ({ssh_marker}) · agy uses file token storage",
        )
    else:
        table.add_row("session", "local · agy may use the keyring")

    ring = credentials.keyring_status()
    if ssh_marker:
        ring_state = "[yellow]bypassed in SSH[/yellow]"
    elif ring["available"]:
        ring_state = "[green]available[/green]"
    else:
        ring_state = "[yellow]not found[/yellow]"
    table.add_row("keyring", f"{ring['backend']} · {ring_state}")

    file_token = paths.SSH_TOKEN_PATH
    if credentials.has_wrapped_token(file_token):
        file_state = "[green]present[/green]"
    elif file_token.exists():
        file_state = "[yellow]invalid/unwrapped — run 'agyswap sync'[/yellow]"
    else:
        file_state = "[red]missing[/red]"
    table.add_row("file token", f"{file_token} · {file_state}")

    mirror = paths.SSH_TOKEN_MIRROR
    if credentials.read_token_mirror() is not None:
        table.add_row("ssh mirror", f"{mirror} · [green]present[/green]")
    elif mirror.exists():
        table.add_row(
            "ssh mirror",
            f"{mirror} · [yellow]corrupt — run 'agyswap sync'[/yellow]",
        )
    else:
        table.add_row(
            "ssh mirror",
            f"{mirror} · [dim]missing — run 'agyswap sync' on a desktop session[/dim]",
        )

    def _expiry_text(text: str) -> str:
        expiry = credentials.token_expiry(text)
        if expiry is None:
            return "unknown expiry"
        delta = expiry - time.time()
        return "expired" if delta <= 0 else f"{format_duration(delta)} left"

    live = credentials.live_tokens()
    if not live:
        table.add_row("token", "[red]not found[/red]")
        table.add_row(
            "identity",
            current_email(allow_network=False) or "[yellow]unknown[/yellow]",
        )
    elif len(live) == 1:
        source, text = live[-1]
        token_detail = f"{_source_label(source)} · {_expiry_text(text)}"
        if ssh_marker and source != str(file_token):
            token_detail += " [yellow](ignored by agy in SSH)[/yellow]"
        table.add_row("token", token_detail)
        table.add_row(
            "identity",
            credentials.email_from_token(text, allow_network=False)
            or "[yellow]unknown[/yellow]",
        )
    else:
        # agy can leave two stores holding *different* accounts: a desktop
        # login in Credential Manager next to an SSH login in the token file.
        # Name each one instead of silently reporting the freshest.
        table.add_row(
            "tokens",
            f"[yellow]{len(live)} different logins live[/yellow] · "
            "'agyswap add' captures all of them",
        )
        for source, text in reversed(live):
            email = credentials.email_from_token(text) or "unknown"
            line = f"{_source_label(source)} · {email} · {_expiry_text(text)}"
            if ssh_marker and source != str(file_token):
                line += " [yellow](ignored by agy in SSH)[/yellow]"
            table.add_row("", line)

    accounts = sw.list_accounts()
    table.add_row("accounts", str(len(accounts)) if accounts else "[dim]none[/dim]")

    profiles = (
        sorted(p for p in paths.PROFILES_DIR.glob("*/gemini") if p.is_dir())
        if paths.PROFILES_DIR.exists()
        else []
    )
    if profiles:
        table.add_row(
            "profiles",
            f"{len(profiles)} isolated · {paths.PROFILES_DIR}",
        )

    orphans = sw.orphan_backups()
    if orphans:
        table.add_row(
            "backups",
            f"[yellow]{len(orphans)} unregistered[/yellow] · "
            "run 'agyswap recover' to adopt them",
        )

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

    if live and credentials.token_is_expired(live[-1][1]):
        console.print(
            "\n[yellow]The active token has expired.[/yellow] Run [bold]agy[/bold] once "
            "to refresh it, or configure OAuth refresh (see README)."
        )

    if ssh_marker and not credentials.has_wrapped_token(file_token):
        console.print(
            "\n[yellow]agy cannot use the OS keyring from this SSH session.[/yellow]\n"
            "Run [bold]agyswap sync[/bold] to restore its token file, or run it once "
            "from a desktop session to mirror the keyring token."
        )


def cmd_statusline(sw, args) -> None:
    if args.setup:
        from agyswap.statusline import wrapper_path, write_wrapper

        chain = args.chain or ""
        if args.install:
            target = write_wrapper(chain)
            console.print(f"[green]Wrote[/green] {target}\n")

        target = wrapper_path()
        command = target.name
        if not target.exists():
            # No launcher yet: name it anyway and tell them to create it, since
            # a bare word is the only form that survives every spawn style.
            console.print(
                "[yellow]Run with --install first[/yellow] to create "
                f"{target}, or write this yourself:\n"
            )
            chain_hint = f' --chain "{chain}"' if chain else ""
            if sys.platform == "win32":
                body = '  @"%USERPROFILE%\\.local\\bin\\agyswap.exe" statusline' + chain_hint
            else:
                body = "  #!/bin/sh\n  exec agyswap statusline" + chain_hint
            console.print(body, markup=False, highlight=False, soft_wrap=True)
            console.print()

        console.print("Add this to [bold]~/.gemini/settings.json[/bold]:\n")
        console.print(
            '  "statusLine": {\n'
            '    "type": "",\n'
            f'    "command": "{command}",\n'
            '    "enabled": true\n'
            "  }",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        console.print()
        console.print(
            "[dim]agy reads this at startup -- restart it for the hook to take "
            "effect. Quota then updates itself while agy runs.[/dim]"
        )
        if chain:
            console.print(
                f"[dim]--chain forwards the same stdin to '{chain}' and passes its "
                "output through untouched.[/dim]"
            )
        return

    from agyswap.statusline import main as statusline_main

    argv: list[str] = []
    if args.chain:
        argv += ["--chain", args.chain]
    if args.install:
        argv.append("--install")
    sys.exit(statusline_main(argv))


def cmd_upgrade(sw, args) -> None:
    from agyswap import upgrade

    install = upgrade.detect()
    try:
        steps = upgrade.plan(install)
    except upgrade.UpgradeError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    console.print(f"Installed: [bold]{install.revision}[/bold] [dim]via {install.label}[/dim]")
    if install.pinned and install.editable is None:
        console.print(
            f"[yellow]Pinned to '{install.pinned}'[/yellow], so upgrades stay there. "
            "Re-run the install command with a newer revision, or without one, to move on."
        )
    if not steps:
        console.print(
            "[yellow]Cannot tell how agy-swap was installed, so it cannot upgrade "
            "itself.[/yellow] Reinstall with one of:\n"
        )
        console.print(
            f'  uv tool install --reinstall --from "git+{upgrade.REPO_URL}" agy-swap\n'
            f'  pipx install --force "git+{upgrade.REPO_URL}"',
            markup=False, highlight=False, soft_wrap=True,
        )
        sys.exit(1)

    for step in steps:
        console.print(f"$ {upgrade.display(step)}", style="dim", markup=False,
                      highlight=False, soft_wrap=True)
        if args.dry_run:
            continue
        try:
            if upgrade.must_hand_off(step):
                upgrade.hand_off(step)
                console.print(
                    "[dim]Windows cannot replace agyswap.exe while it is running, so "
                    "the upgrade finishes in this window right after agyswap exits.[/dim]"
                )
                return
            code = upgrade.run(step)
        except upgrade.UpgradeError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)
        if code:
            console.print(f"[red]Upgrade failed[/red] (exit {code}).")
            if step.retry_hint:
                console.print(step.retry_hint, markup=False, highlight=False)
            sys.exit(code)
    if args.dry_run:
        return

    after = upgrade.detect(install.prefix)
    if after.revision == install.revision:
        console.print(f"[green]Already up to date:[/green] {after.revision}")
    else:
        console.print(
            f"[green]Upgraded:[/green] {install.revision} → [bold]{after.revision}[/bold]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agyswap",
        description="Multi-account switcher for Antigravity CLI (agy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  agyswap login            sign in to an isolated agy profile and save the account
  agyswap add              capture every live login as a managed account
  agyswap recover          re-adopt accounts from orphaned token backups
  agyswap list             list managed accounts
  agyswap switch           rotate to next enabled account
  agyswap switch 2         switch to account #2
  agyswap switch email     switch to account by email
  agyswap run 2            run agy with #2's isolated credential profile
  agyswap run 2 --print hi pass agy options after the id
  agyswap sync             restore the token file agy uses over SSH
  agyswap sync --if-missing  only when agy's token file is missing
  agyswap disable 2        hold an account out of rotation
  agyswap enable 2         return an account to rotation
  agyswap remove 1 2       remove accounts and close gaps
  agyswap renumber         close gaps in existing account numbers
  agyswap alias 2 work     set an alias
  agyswap alias 2 --unset  remove an alias
  agyswap tui              interactive dashboard
  agyswap watch            dashboard on the watch page
  agyswap doctor           show what agy-swap can see on this machine
  agyswap upgrade          upgrade agy-swap with the tool that installed it
""",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="list managed accounts")
    sub.add_parser("status", help="show current active account")
    sub.add_parser("login", help="sign in to an isolated agy profile and save the account")
    p_add = sub.add_parser("add", help="capture current agy login as a managed account")
    p_add.add_argument("--email", help="email for the account (auto-detected if omitted)")

    sub.add_parser(
        "recover",
        help="re-register accounts whose token backups outlived the registry",
    )

    p_switch = sub.add_parser("switch", help="switch accounts")
    p_switch.add_argument("identifier", nargs="?", help="account number, email, or alias")

    p_run = sub.add_parser(
        "run",
        help="run agy with an isolated credential profile for one account",
    )
    p_run.add_argument("identifier", help="account number, email, or alias")
    p_run.add_argument(
        "agy_args",
        nargs=argparse.REMAINDER,
        help="arguments passed through to agy (profile directory flags are reserved)",
    )

    p_sync = sub.add_parser(
        "sync", help="write the active token to agy's file store (SSH sessions)"
    )
    p_sync.add_argument(
        "--if-missing", action="store_true",
        help="do nothing when agy's token file already exists",
    )
    p_sync.add_argument("--quiet", action="store_true", help="no output on success")

    p_remove = sub.add_parser("remove", aliases=["rm"], help="remove accounts and close gaps")
    p_remove.add_argument("identifiers", nargs="+", help="account numbers, emails, or aliases")
    sub.add_parser("renumber", help="close gaps in existing account numbers")

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
    p_statusline.add_argument("--install", action="store_true",
                              help="write the agy-statusline launcher script")
    p_statusline.add_argument("--chain", help="also run this command, forwarding its output")

    p_upgrade = sub.add_parser("upgrade", aliases=["update"],
                               help="upgrade agy-swap with the tool that installed it")
    p_upgrade.add_argument("--dry-run", action="store_true",
                           help="print the commands without running them")

    args = parser.parse_args()

    switcher = AgySwitcher()

    if not args.command:
        cmd_tui(switcher, args)
        return

    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "login": cmd_login,
        "add": cmd_add,
        "recover": cmd_recover,
        "switch": cmd_switch,
        "run": cmd_run,
        "sync": cmd_sync,
        "remove": cmd_remove,
        "rm": cmd_remove,
        "renumber": cmd_renumber,
        "disable": cmd_disable,
        "enable": cmd_enable,
        "alias": cmd_alias,
        "tui": cmd_tui,
        "watch": cmd_tui,
        "doctor": cmd_doctor,
        "statusline": cmd_statusline,
        "upgrade": cmd_upgrade,
        "update": cmd_upgrade,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(switcher, args)


if __name__ == "__main__":
    main()
