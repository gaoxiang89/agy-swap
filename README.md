# agy-swap

Multi-account switcher for Antigravity CLI (`agy`), with a live quota dashboard.
Switch between Google/Gemini accounts without logging out, and watch every
account's quota in a TUI.

Works on **macOS, Linux and Windows 11** — credentials go through the OS keyring
(Keychain / Secret Service / Credential Manager) with a file fallback when no
keyring is available.

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
uv tool install --from "git+https://github.com/gaoxiang89/agy-swap@v0.2.0" agy-swap

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
uv tool upgrade agy-swap     # re-resolves from the recorded git source
pipx upgrade agy-swap
```

`uv tool upgrade` follows whatever source the tool was installed from — it will
not silently swap in the PyPI release.

## First run

```bash
agyswap doctor               # what agy-swap can see: keyring, token, identity
agyswap add                  # capture the account agy is currently logged in as
agyswap statusline --setup   # enable live quota (see "Quota data")
agyswap                      # open the dashboard
```

To add another account, log into it with `agy`, then run `agyswap add` again.

## Usage

```bash
agyswap add                    # capture the current agy login as an account
agyswap list                   # list accounts, tokens and quota
agyswap switch                 # rotate to the next enabled account
agyswap switch 2               # by slot number
agyswap switch user@gmail.com  # by email
agyswap switch work            # by alias
agyswap disable 2              # hold out of rotation (keeps credentials)
agyswap enable 2
agyswap alias 2 work
agyswap alias 2 --unset
agyswap remove 2
agyswap doctor                 # what agy-swap can see on this machine
```

Run `agyswap` with no arguments for the interactive dashboard.

### Dashboard

| Key | Action |
|-----|--------|
| `s` | Switch accounts |
| `w` | Watch screen |
| `a` | Add current account |
| `f` | Refresh now |
| `j` / `k` | Move |
| `q` | Back / quit |

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
  keyring and the token file(s), so the two can never drift apart
- Account identity is read from the token itself — the signed `id_token` JWT in
  the file, or the `id_token_email` field agy writes to the Credential Manager —
  so it is correct offline and does not depend on any bookkeeping file agy may
  drop
- Rejects a switch whose stored token belongs to a different account than the
  slot claims (prevents silently swapping in the wrong login)

agy names its token file per platform rather than per version, so agy-swap
probes both locations and uses whichever holds the fresher token:

| Data | Path |
|---|---|
| Active token (Windows) | `~/.gemini/jetski-standalone-oauth-token` |
| Active token (Linux, and some older builds) | `~/.gemini/antigravity-cli/antigravity-oauth-token` |
| Legacy account pointer | `~/.gemini/google_accounts.json` (only `active` is touched) |
| Credential backups | `~/.agy-swap/credentials/` |
| Account registry | `~/.agy-swap/sequence.json` |
| Quota cache | `~/.agy-swap/cache/quota.json` |

### Environment overrides

| Variable | Effect |
|---|---|
| `AGY_GEMINI_DIR` | agy's config root (default `~/.gemini`) |
| `AGYSWAP_DATA_DIR` | agy-swap's data root (default `~/.agy-swap`) |
| `AGYSWAP_DISABLE_KEYRING` | `1` forces the file-only credential path |

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
