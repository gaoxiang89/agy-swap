# agy-swap

Multi-account switcher for Antigravity CLI (`agy`), with a live quota dashboard.
Switch between Google/Gemini accounts without logging out, and watch every
account's quota in a TUI.

Works on **macOS, Linux and Windows 11** — credentials go through the OS keyring
(Keychain / Secret Service / Credential Manager) with a file fallback when no
keyring is available.

## Installation

```bash
uv tool install agy-swap       # uv (recommended)
pipx install agy-swap          # pipx
uv tool upgrade agy-swap       # update
pipx upgrade agy-swap
```

From source:

```bash
git clone https://github.com/pr656d/agy-swap.git
cd agy-swap
uv sync
uv run agyswap help
```

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
`gemini-5h`, `gemini-weekly`, `3p-5h`, `3p-weekly`, each with a reset time — with
no token and no network involved. Enable the hook in `~/.gemini/settings.json`:

```json
"statusLine": {
  "type": "",
  "command": "python -m agyswap.statusline",
  "enabled": true
}
```

Already have a status-line script? Chain it so both run:

```json
"command": "python -m agyswap.statusline --chain \"C:/path/to/your-statusline.bat\""
```

The chained command receives the same stdin and its output is forwarded verbatim.

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

agy has moved this data around across versions, so agy-swap reads and writes both
known locations:

| Data | Path |
|---|---|
| Active token (agy ≥ 1.4) | `~/.gemini/jetski-standalone-oauth-token` |
| Active token (older agy) | `~/.gemini/antigravity-cli/antigravity-oauth-token` |
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
python -m unittest discover -s tests -t .
```

The suite runs against a throwaway home directory; it never touches your real
credentials, and the Windows keyring test uses a separate credential target that
it deletes afterwards.

## Uninstall

```bash
rm -rf ~/.agy-swap
uv tool uninstall agy-swap     # or: pipx uninstall agy-swap
```

## Requirements

- Python 3.10+
- Antigravity CLI (`agy`) installed and logged in

## Acknowledgements

Inspired by [claude-swap](https://github.com/realiti4/claude-swap).

## License

MIT
