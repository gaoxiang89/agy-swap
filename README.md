# agy-swap

Multi-account switcher for Antigravity CLI (`agy`), with a live quota dashboard.
Switch between Google/Gemini accounts without logging out, and watch every
account's quota in a TUI.

Works on **macOS, Linux and Windows 11** — credentials go through the OS keyring
(Keychain / Secret Service / Credential Manager) with a file fallback, including
SSH sessions where agy never touches the keyring.

> This is a fork of [pr656d/agy-swap](https://github.com/pr656d/agy-swap) adding
> cross-platform support and a corrected quota model. The published PyPI package
> is the upstream one; install from this repository instead (see below).

## Install

`agy-swap` is not published to PyPI from this fork, so every command below
installs **from git**. That matters: `uv tool install agy-swap` would fetch the
upstream release, which only works on macOS.

### uv (recommended)

```bash
uv tool install --from "git+https://github.com/gaoxiang89/agy-swap" agy-swap
```

### pipx

```bash
pipx install "git+https://github.com/gaoxiang89/agy-swap"
```

### Pin a version

Useful for keeping several machines identical:

```bash
uv tool install --from "git+https://github.com/gaoxiang89/agy-swap@v0.3.0" agy-swap

# no tag needed — any commit works
uv tool install --from "git+https://github.com/gaoxiang89/agy-swap@72288cc" agy-swap
```

### From a clone (development)

```bash
git clone https://github.com/gaoxiang89/agy-swap
cd agy-swap
uv tool install --editable --from . agy-swap
```

`--editable` links the source tree into the tool environment, so edits take
effect immediately without reinstalling. Drop it if you want a stable snapshot;
then re-run with `--refresh` after pulling changes.

### Windows: command not found?

uv installs to `%USERPROFILE%\.local\bin`. If `agyswap` is not on `PATH`:

```powershell
uv tool update-shell
```

### Update

```bash
agyswap upgrade              # alias: agyswap update
agyswap upgrade --dry-run    # print the commands without running them
```

`agyswap upgrade` works out how it was installed and runs that tool:

| Installed with | What runs |
|---|---|
| `uv tool install` | `uv tool upgrade agy-swap` |
| `pipx install` | `pipx upgrade agy-swap` |
| `--editable` from a clone | `git pull --ff-only` in the clone, then the uv/pipx step |

Both re-resolve from the recorded git source, so they never swap in the
macOS-only PyPI release. An install pinned to a tag or commit stays on it;
re-run the install command with a newer revision, or without one, to move on.

On Windows a running `agyswap.exe` cannot be replaced, so the upgrade finishes
in the same window right after `agyswap` exits. If it reports that a file is in
use, close any other agyswap (a dashboard, or agy's status line) and run
`uv tool upgrade --reinstall agy-swap`.

## First run

```bash
agyswap doctor               # what agy-swap can see: keyring, token, identity
agyswap add                  # capture every live agy login as accounts
agyswap statusline --setup   # enable live quota (see "Quota data")
agyswap                      # open the dashboard
```

To add another account, log into it with `agy`, then run `agyswap add` again.

`agyswap add` registers **every** live login it can see, not just the newest
one. An agy install can hold two different accounts at once — for example a
desktop login in Credential Manager next to an SSH login in the token file —
and both are captured in a single run; the output names the store each account
came from. Duplicate copies of the same token collapse into one account.

Backups outlive the registry: if `~/.agy-swap/sequence.json` is lost or reset,
`agyswap recover` re-registers every account whose token is still in
`~/.agy-swap/credentials/`, keeping the old slot number where it is free.
`agyswap doctor` reports any unregistered backups it finds.

## Usage

```bash
agyswap add                    # capture the live agy login(s) as accounts
agyswap recover               # re-adopt accounts from orphaned token backups
agyswap list                   # list accounts, tokens and quota
agyswap switch                 # rotate to the next enabled account
agyswap switch 2               # by slot number
agyswap switch user@gmail.com  # by email
agyswap switch work            # by alias
agyswap run 2                  # launch agy with #2's isolated credentials
agyswap run work --print "hi"  # pass arguments through to agy
agyswap sync                   # restore the token file agy uses over SSH
agyswap disable 2              # hold out of rotation (keeps credentials)
agyswap enable 2
agyswap alias 2 work
agyswap alias 2 --unset
agyswap remove 2
agyswap remove 1 2             # remove both slots as numbered before the command
agyswap renumber               # close gaps left by older versions
agyswap doctor                 # what agy-swap can see on this machine
```

Run `agyswap` with no arguments for the interactive dashboard.
Removing accounts closes gaps in the remaining slot numbers while preserving rotation order.

### Dashboard

| Key | Action |
|-----|--------|
| `s` | Switch accounts |
| `w` | Watch screen |
| `a` | Add current account |
| `f` | Refresh now |
| `j` / `k` | Move |
| `q` | Back / quit |

### Isolated profiles (`agyswap run`)

`agyswap switch` keeps the historical single-slot model: it rewrites the one
token that the next plain `agy` will read. That remains the default and is
unchanged.

When several accounts must stay online at the same time, `agyswap run` gives
one account a private Gemini root and launches agy against it:

```bash
agyswap run 2                  # interactive session as account #2
agyswap run 2 --print "hello"  # pass agy options after the id
agyswap run work --model ...   # aliases work too
```

What a profile contains:

```text
~/.agy-swap/profiles/<account-hash>/gemini/
  antigravity-cli/antigravity-oauth-token   <- this account's token
  antigravity-cli/settings.json             <- copied once from the default root
  antigravity-cli/...                        <- history, plugins, logs
```

agy is launched as `agy --gemini_dir=<profile>` and with
`AGY_CLI_FORCE_FILE_TOKEN_STORAGE=1`. Both parts matter:

- `--gemini_dir` is agy's hidden global switch for the Gemini root. It is not
  `AGY_GEMINI_DIR`, which only changes agy-swap's view and is ignored by agy.
- The OS keyring has a single global target (`gemini:antigravity`) that is not
  namespaced by config directory. Forcing file storage is what keeps profiles
  from sharing it.

Because each profile has its own token file, an agy process refreshing an
account in the background can no longer overwrite the account another profile
uses. The shared `~/.gemini` slot is left alone, so plain `agy` behaves exactly
as before.

Notes:

- Each profile has its own conversation history, plugins and settings.
  `settings.json` is seeded once from the default root; later changes stay in
  that profile.
- The `--gemini_dir` flag is hidden and verified against agy 1.5.19. Check
  `agy acp --help | grep gemini` after an agy upgrade.
- Set `AGY_EXE` if agy is installed somewhere `PATH` does not cover.
- Refreshed tokens are copied back into `~/.agy-swap/credentials/` when the
  session exits, so the existing backup and `switch` workflow stays current.
- The profile directory uses a hash of the account email and stays in place
  when slots are renumbered. Removing an account deletes its profile token
  and keeps its history and settings.
- `agyswap run` reserves agy's `gemini_dir`, `app_data_dir` and `cli_dir_name`
  flags. Launcher messages go to stderr so stdout carries agy's output.

## Platform support

| | macOS | Linux | Windows 11 |
|---|---|---|---|
| Credential store | Keychain (`security`) | Secret Service (`secret-tool`) | Credential Manager (`advapi32`) |
| Keyring entry created by agy | yes | no (file only) | yes |
| File fallback | yes | yes | yes |
| No extra dependencies | yes | yes | yes |
| Verified | code path only | full suite + CLI + TUI + Secret Service | full suite + CLI + TUI + WinCred |

Nothing outside the standard library is used for credential access: the Windows
backend talks to `advapi32` directly through `ctypes`, and the Linux backend
shells out to `secret-tool` (part of `libsecret`) when it exists.

On Linux agy itself only writes its token file — its binary contains WinCred
support but no Secret Service code — so agy-swap never *creates* a Secret Service
entry there. It only keeps one in sync if it already exists. Windows (WinCred)
and macOS (Keychain) entries are written normally.

If no keyring is usable (containers, WSL without a session bus, minimal Linux),
agy-swap falls back to agy's own token file and keeps working. Force that mode
with `AGYSWAP_DISABLE_KEYRING=1`.

Run `agyswap doctor` to see exactly which path is in use:

```
  keyring        wincred · available
  token          keyring:wincred · 53m left
  identity       you@example.com
  accounts       2
  quota cache    schema 2 · 2 entry(ies)
```

## Quota data

agy is the only source. It pushes a JSON blob to its configured status-line
command ~3x/second, and that blob contains the real, windowed quota —
`gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly` (plus `gemini-argon-wtu-weekly`
on plans that include Gemini 4 Argon) — with no token and no network involved.

Wire it up in one command:

```bash
agyswap statusline --setup --install
```

That writes a tiny `agy-statusline` launcher next to the `agyswap` executable and
prints the `statusLine` block to paste into `~/.gemini/settings.json`:

```json
"statusLine": {
  "type": "",
  "command": "agy-statusline",
  "enabled": true
}
```

Restart agy afterwards — it reads `settings.json` at startup. From then on, quota
updates itself while you work.

The launcher exists because agy may spawn that command through a shell or
directly; a single bare word with no arguments behaves the same either way,
whereas `python -m agyswap.statusline` is unreliable under `uv tool`/`pipx`
installs (the module lives in an isolated environment, not in agy's `python`).

Already have a status-line script? Chain it — both run from the same stdin, and
your script's output is forwarded verbatim:

```bash
agyswap statusline --setup --install --chain "agy-tps-statusline.bat"   # Windows
agyswap statusline --setup --install --chain "agy-tps-statusline"       # Linux
```

Readings are cached per account with their age, so once you have been on an
account its usage stays visible after you switch away.

> **Why not call Google's quota API?** Because it does not answer for this
> product. `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` returns
> `403 PERMISSION_DENIED — "You do not have a valid license of this product"` for
> Antigravity accounts, while `loadCodeAssist` on the same host accepts the same
> token and reports the tier as `UNSUPPORTED_CLIENT` ("migrate to the Antigravity
> suite"). Earlier versions probed that endpoint; it cost a request per minute
> per account and mislabelled healthy accounts as "expired".

## Limitations

- **Switching does not affect a running agy session.** agy reads its token at
  startup and caches it in memory. Exit the session (or open a new terminal) for
  the change to take effect.
- **`agyswap run` profiles do not share history or plugins.** Each profile is a
  separate Gemini root; only `settings.json` is seeded from the default root.
  Use `agyswap run` for concurrent sessions, and plain `agy` when one account
  at a time is enough.
- **A desktop agy can delete the SSH token file.** agy 1.5.x removes the file
  when it refreshes a keyring token from a non-SSH session. Run
  `agyswap sync --if-missing` before starting agy over SSH to restore it; see
  [SSH and headless sessions](#ssh-and-headless-sessions).
- **Quota is only known for accounts agy has reported on.** An account never used
  since you installed agy-swap shows `no samples yet` rather than a guess.
- **Each machine has its own data.** `~/.agy-swap` (registry, backups, cache) is
  per-machine; it is not synced. A WSL install and a Windows install keep
  separate account lists.
- Credential backups are base64-encoded, not encrypted. They are written
  atomically and locked down (`0600` on POSIX, an ACL on Windows), but anyone
  who can read your home directory as you can read them.

## How it works

- Backs up the OAuth token per account in `~/.agy-swap/credentials/`
- Switching rewrites the live token in **every** store agy might read: the OS
  keyring, both token files, and agy-swap's SSH mirror, so they can never drift
  apart
- `agyswap run` gives one account its own Gemini root and launches agy with
  `--gemini_dir` plus file-only token storage, so concurrent sessions cannot
  overwrite each other; refreshed tokens are folded back into the backups
- Account identity is read from the token itself — the signed `id_token` JWT in
  the file, the `id_token_email` field some agy builds write to the Credential
  Manager, or Google's `userinfo` endpoint when a store keeps neither — so it
  does not depend on any bookkeeping file agy may drop
- Rejects a switch whose stored token belongs to a different account than the
  slot claims (prevents silently swapping in the wrong login)

agy reads the OS keyring first, but switches to a file whenever the keyring is
unreachable. agy 1.5.x treats an SSH session as exactly that case on every
platform, and on Windows the keyring is also unusable at the OS level there
(`ERROR_NO_SUCH_LOGON_SESSION`), so the file token is the only way in:

| Data | Path |
|---|---|
| Active token (primary) | OS keyring, target `gemini:antigravity` |
| File token agy uses over SSH, in containers, and after a keyring failure | `~/.gemini/antigravity-cli/antigravity-oauth-token` |
| Token file kept for older builds and third-party tools | `~/.gemini/jetski-standalone-oauth-token` |
| SSH mirror (agy-swap) | `~/.agy-swap/ssh-token.json` |
| Legacy account pointer | `~/.gemini/google_accounts.json` (only `active` is touched) |
| Credential backups | `~/.agy-swap/credentials/` |
| Isolated profiles | `~/.agy-swap/profiles/<account-hash>/gemini/` |
| Account registry | `~/.agy-swap/sequence.json` |
| Quota cache | `~/.agy-swap/cache/quota.json` |

### SSH and headless sessions

agy deliberately skips the OS keyring when `SSH_CONNECTION`, `SSH_CLIENT`, or
`SSH_TTY` is set. On Linux and macOS, `env -u SSH_CONNECTION -u SSH_CLIENT -u
SSH_TTY agy` makes it read the keyring again. On Windows that cannot work: an
SSH logon is a network logon, and Credential Manager refuses every call with
`ERROR_NO_SUCH_LOGON_SESSION` (1312).

`agyswap sync` writes the active token to both file locations and refreshes the
keyring when it is reachable. Run it once from a desktop session to mirror the
keyring token, then restore it over SSH after a reboot or after agy prunes the
file:

```bash
agyswap sync                 # desktop: keyring -> files + ~/.agy-swap/ssh-token.json
agyswap sync --if-missing    # SSH: restore only when agy's file is absent
```

To make each SSH `agy` start self-healing, wrap it in your PowerShell profile on
the machine:

```powershell
function agy {
    if ($env:SSH_CONNECTION -or $env:SSH_CLIENT -or $env:SSH_TTY) {
        agyswap sync --if-missing --quiet
    }
    & "$env:LOCALAPPDATA\agy\bin\agy.exe" @args
}
```

`--if-missing` never overwrites a token agy refreshed itself, and `--quiet`
keeps the wrapper silent. `agyswap doctor` reports which storage agy will use in
the current session, the file's state, and whether the mirror exists.

> Upstream agy has two open issues here: there is no way to opt back into the
> keyring over SSH, and a desktop refresh can delete the SSH token file. Until
> those are fixed, the mirror plus `sync --if-missing` is the durable workaround.

### Environment overrides

| Variable | Effect |
|---|---|
| `AGY_GEMINI_DIR` | agy-swap's view of agy's config root (default `~/.gemini`). agy itself ignores this; use agy's `--gemini_dir` flag to move agy. |
| `AGYSWAP_DATA_DIR` | agy-swap's data root (default `~/.agy-swap`) |
| `AGYSWAP_DISABLE_KEYRING` | `1` forces the file-only credential path |
| `AGY_EXE` | agy executable used by `agyswap run` (default: `PATH`) |
| `AGYSWAP_PROFILE_EMAIL` | set automatically by `agyswap run` for quota attribution |

## Development

```bash
git clone https://github.com/gaoxiang89/agy-swap
cd agy-swap
uv tool install --editable --from . agy-swap
python -m unittest discover -s tests -t .
```

The suite runs against a throwaway home directory; it never touches your real
credentials, and the Windows keyring test uses a separate credential target that
it deletes afterwards.

## Uninstall

```bash
uv tool uninstall agy-swap     # or: pipx uninstall agy-swap
rm -rf ~/.agy-swap             # drop accounts, backups and cache
```

## Requirements

- Python 3.10+ (uv and pipx fetch a suitable interpreter automatically)
- Antigravity CLI (`agy`) installed and logged in

## Acknowledgements

Fork of [pr656d/agy-swap](https://github.com/pr656d/agy-swap), itself inspired by
[claude-swap](https://github.com/realiti4/claude-swap).

## License

MIT
