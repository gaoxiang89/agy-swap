"""Account registry and switching logic.

This module owns *what* gets swapped; :mod:`agyswap.credentials` owns *how* the
platform stores it.  Keeping that split is what lets the same logic run on
macOS (Keychain), Windows (Credential Manager) and Linux (Secret Service).
"""

from __future__ import annotations

import json
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Optional

from agyswap import credentials, paths, quota
from agyswap.locking import exclusive_file_lock
from agyswap.models import Account, SequenceData, now_iso


class AgyError(Exception):
    pass


class AccountNotFoundError(AgyError):
    pass


def _registry_mutation(method):
    @wraps(method)
    def update(self, *args, **kwargs):
        with exclusive_file_lock(paths.BACKUP_ROOT / ".sequence.lock"):
            self.reload()
            return method(self, *args, **kwargs)

    return update


def _read_json(path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path, data: dict) -> None:
    credentials.atomic_write(path, json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# identity helpers
# ---------------------------------------------------------------------------


def _email_from_oauth_creds() -> Optional[str]:
    """Last-resort email hint from an old agy install."""
    data = _read_json(paths.OAUTH_CREDS_PATH)
    if not data:
        return None
    id_token = data.get("id_token")
    claims = credentials._jwt_claims(id_token)
    email = claims.get("email")
    return email if isinstance(email, str) and email else None


def _legacy_active_email() -> Optional[str]:
    data = _read_json(paths.GOOGLE_ACCOUNTS_PATH)
    if not data:
        return None
    active = data.get("active")
    return active if isinstance(active, str) and active else None


def current_email(*, allow_network: bool = True) -> Optional[str]:
    """Which account agy is actually logged in as.

    The signed ``id_token`` inside the stored token is authoritative and works
    offline; legacy bookkeeping files are only consulted when it is absent.
    """
    token, _source = credentials.read_token()
    if token:
        email = credentials.email_from_token(token, allow_network=allow_network)
        if email:
            return email
    return _legacy_active_email() or _email_from_oauth_creds()


def _write_google_accounts(active: str) -> None:
    """Update the legacy ``active`` pointer without discarding ``old``.

    agy >= 1.4 no longer reads this file, but older builds do, and clobbering
    their history list was gratuitous.  Read-modify-write keeps both happy.
    """
    data = _read_json(paths.GOOGLE_ACCOUNTS_PATH) or {}
    if not isinstance(data.get("old"), list):
        data["old"] = []
    data["active"] = active
    _write_json(paths.GOOGLE_ACCOUNTS_PATH, data)


# ---------------------------------------------------------------------------
# switcher
# ---------------------------------------------------------------------------


class AgySwitcher:
    def __init__(self) -> None:
        self._ensure_dirs()
        self._seq = self._load_sequence()

    # -- storage ----------------------------------------------------------

    def _ensure_dirs(self) -> None:
        for directory in (paths.BACKUP_ROOT, paths.CREDENTIALS_DIR):
            directory.mkdir(parents=True, exist_ok=True)
            credentials.secure_dir(directory)

    def _load_sequence(self) -> SequenceData:
        data = _read_json(paths.SEQUENCE_FILE)
        if data is None:
            return SequenceData()
        try:
            return SequenceData.from_dict(data)
        except Exception:
            return SequenceData()

    def _save_sequence(self) -> None:
        self._seq.last_updated = now_iso()
        _write_json(paths.SEQUENCE_FILE, self._seq.to_dict())

    def reload(self) -> None:
        """Re-read the registry so external edits (another CLI call) are seen."""
        self._seq = self._load_sequence()

    # -- lookup -----------------------------------------------------------

    def _next_number(self) -> int:
        if not self._seq.accounts:
            return 1
        return max(self._seq.accounts.keys()) + 1

    def _find_account_by_email(self, email: str) -> Optional[Account]:
        lowered = email.lower()
        for account in self._seq.accounts.values():
            if account.email.lower() == lowered:
                return account
        return None

    def _find_account_by_number(self, number: int) -> Optional[Account]:
        return self._seq.accounts.get(number)

    def _active_account(self) -> Optional[Account]:
        """The account ``sync`` falls back to when no live token exists."""
        if self._seq.active_email:
            account = self._find_account_by_email(self._seq.active_email)
            if account and not account.disabled:
                return account
        for number in self._seq.sequence:
            account = self._seq.accounts.get(number)
            if account and not account.disabled:
                return account
        return None

    def _save_backup_if_fresher(
        self, owner: Account, content: str, *, required: bool = False
    ) -> None:
        """Persist ``content`` to ``owner``'s backup without clobbering a newer copy."""
        existing = credentials.read_token_backup(owner.credential_number, owner.email)
        if existing:
            cand_exp = credentials.token_expiry(content)
            exist_exp = credentials.token_expiry(existing)
            if exist_exp is not None and (cand_exp is None or cand_exp < exist_exp):
                return
        try:
            credentials.save_token_backup(owner.credential_number, owner.email, content)
        except OSError:
            if required:
                raise

    def _resolve_account(self, identifier: str) -> Account:
        try:
            account = self._find_account_by_number(int(identifier))
        except ValueError:
            account = None
        if account:
            return account
        account = self._find_account_by_email(identifier)
        if account:
            return account
        for candidate in self._seq.accounts.values():
            if candidate.alias and candidate.alias.lower() == identifier.lower():
                return candidate
        raise AccountNotFoundError(f"No account found: {identifier}")

    # -- queries ----------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        seq = self._load_sequence()
        active_email = current_email()
        cache = quota.load_cache()
        rows: list[dict] = []
        for number in seq.sequence:
            account = seq.accounts.get(number)
            if not account:
                continue
            details = quota.describe(account.email, cache)
            rows.append(
                {
                    "number": number,
                    "email": account.email,
                    "alias": account.alias,
                    "disabled": account.disabled,
                    "active": bool(
                        active_email and account.email.lower() == active_email.lower()
                    ),
                    "has_token": credentials.read_token_backup(
                        account.credential_number, account.email
                    ) is not None,
                    "quota_windows": details["windows"],
                    "quota_status": details["status"],
                    "quota_detail": details["detail"],
                    "quota_fetched_at": details["captured_at"],
                }
            )
        return rows

    def status(self) -> dict:
        active_email = current_email()
        if not active_email:
            return {"active": None, "message": "No active agy login found."}
        account = self._find_account_by_email(active_email)
        return {
            "active": active_email,
            "managed": account is not None,
            "number": account.number if account else None,
        }

    def sync(self, *, if_missing: bool = False) -> dict:
        """Restore the active token into agy's file store.

        agy skips the OS keyring whenever it detects an SSH session and reads
        ``~/.gemini/antigravity-cli/antigravity-oauth-token`` instead.  On a
        Windows SSH logon the keyring is unreachable at the OS level as well,
        so that file is the only way in.

        The token is the freshest one agy-swap can see -- the keyring or a token
        file on a desktop, agy-swap's mirror or the active account's backup over
        SSH.  ``if_missing`` makes the call safe to run before every agy start:
        an existing file is left alone, so a token agy refreshed itself is never
        replaced with an older copy.
        """
        target = paths.SSH_TOKEN_PATH
        if if_missing and credentials.has_wrapped_token(target):
            return {
                "status": "present",
                "source": None,
                "email": None,
                "path": str(target),
                "targets": [],
            }
        return self._sync_locked(target)

    @_registry_mutation
    def _sync_locked(self, target) -> dict:
        scored: list[tuple[tuple[float, int], str, str]] = []

        live, live_source = credentials.read_token()
        if live:
            scored.append(
                ((credentials.token_expiry(live) or 0.0, 3), live, live_source)
            )

        mirror = credentials.read_token_mirror()
        if mirror:
            scored.append(
                (
                    (credentials.token_expiry(mirror) or 0.0, 2),
                    mirror,
                    str(paths.SSH_TOKEN_MIRROR),
                )
            )

        account = self._active_account()
        if account:
            backup = credentials.read_token_backup(
                account.credential_number, account.email
            )
            if backup:
                scored.append(
                    (
                        (credentials.token_expiry(backup) or 0.0, 1),
                        backup,
                        f"backup:{account.email}",
                    )
                )

        if not scored:
            raise AgyError(
                "No token available to sync. Log in with agy on a desktop "
                "session, run 'agyswap add' there, then run 'agyswap sync'."
            )

        scored.sort(key=lambda item: item[0])
        _, content, source = scored[-1]

        email = credentials.email_from_token(content, allow_network=False)
        if email:
            _write_google_accounts(email)
            owner = self._find_account_by_email(email)
            if owner:
                # Capture a refresh agy performed while this account was active,
                # without overwriting a fresher backup saved earlier.
                self._save_backup_if_fresher(owner, content)
                if self._seq.active_email != owner.email:
                    self._seq.active_email = owner.email
                    self._save_sequence()

        written = credentials.write_token(content)
        return {
            "status": "synced",
            "source": source,
            "email": email,
            "path": str(target),
            "targets": written,
        }

    # -- isolated profiles ------------------------------------------------

    def profile_dir(self, identifier: str) -> Path:
        """Gemini root ``agyswap run`` will pass to agy for this account."""
        account = self._resolve_account(identifier)
        return paths.profile_gemini_dir(account.email)

    def _capture_profile_token(self, account: Account, profile: Path) -> Optional[str]:
        """Fold a profile's refreshed token into the account backup.

        The profile file is authoritative while an isolated session runs, so it
        may be newer than the backup.  A token that belongs to a different
        account is ignored rather than written into the wrong slot.
        """
        token = credentials.read_profile_token(profile)
        if not token:
            return None
        email = credentials.email_from_token(token, allow_network=False)
        if email and email.lower() != account.email.lower():
            return None
        self._save_backup_if_fresher(account, token)
        return token

    @_registry_mutation
    def prepare_profile(self, identifier: str) -> dict:
        """Materialize one account's token into its own Gemini root.

        This deliberately does **not** touch the shared keyring or the default
        ``~/.gemini`` files: plain ``agy`` keeps using exactly what it used
        before, while ``agyswap run`` gets a private credential store.
        """
        account = self._resolve_account(identifier)
        profile = paths.profile_gemini_dir(account.email)

        # Capture a refresh from an earlier run that exited before we could.
        self._capture_profile_token(account, profile)

        token = credentials.read_token_backup(account.credential_number, account.email)
        if not token:
            raise AgyError(
                f"No stored token for {account.email}. "
                f"Log in with agy and run 'agyswap add' first."
            )

        expected = credentials.email_from_token(token, allow_network=False)
        if expected and expected.lower() != account.email.lower():
            raise AgyError(
                f"Stored token for {account.email} actually belongs to {expected}. "
                f"Log in to {account.email} in agy and run 'agyswap add' to fix."
            )

        try:
            profile.mkdir(parents=True, exist_ok=True)
            credentials.secure_dir(profile)
            written = credentials.write_profile_token(profile, token)
        except OSError as exc:
            raise AgyError(
                f"Could not write the isolated token for {account.email}: {exc}"
            ) from exc
        if not written:
            raise AgyError(f"Could not write the isolated token for {account.email}.")

        seeded = credentials.seed_profile_config(profile)
        return {
            "number": account.number,
            "email": account.email,
            "profile": str(profile),
            "targets": written,
            "seeded": seeded,
        }

    @_registry_mutation
    def capture_profile(self, email: str, profile: Path) -> dict:
        """Save the token from the account and directory selected at launch."""
        account = self._find_account_by_email(email)
        if not account:
            raise AccountNotFoundError(f"No account found: {email}")
        token = credentials.read_profile_token(profile)
        result = {
            "number": account.number,
            "email": account.email,
            "profile": str(profile),
            "captured": False,
        }
        if not token:
            result["reason"] = "no token in profile"
            return result

        email = credentials.email_from_token(token, allow_network=False)
        if email and email.lower() != account.email.lower():
            result["reason"] = f"profile token belongs to {email}"
            return result

        self._save_backup_if_fresher(account, token)
        result["captured"] = True
        result["expiry"] = credentials.token_expiry(token)
        return result

    # -- mutations --------------------------------------------------------

    def add_current(self, email_override: Optional[str] = None) -> Account:
        """Capture the newest live login (see :meth:`add_sources`)."""
        return self.add_sources(email_override=email_override)[-1][1]

    @_registry_mutation
    def add_sources(
        self, email_override: Optional[str] = None
    ) -> list[tuple[str, Account]]:
        """Capture every distinct agy login currently live on this machine.

        agy keeps more than one credential store -- Credential Manager on
        Windows, a token file for the CLI -- and a machine logged in from two
        contexts (a desktop session and an SSH session, say) can hold two
        *different* accounts at once.  Taking only the freshest token would
        silently drop the other, so every distinct token is registered, oldest
        first; when two sources carry the same account the newest backup wins.
        """
        tokens = credentials.live_tokens()
        if not tokens:
            raise AgyError(
                "No agy token found. Log in with agy first, then run 'agyswap add'."
            )
        if email_override and len(tokens) > 1:
            found = ", ".join(
                f"{credentials.email_from_token(text) or 'unknown'} ({source})"
                for source, text in tokens
            )
            raise AgyError(
                f"Multiple live logins found: {found}. --email cannot say which "
                f"one it names; run 'agyswap add' without --email, then "
                f"'agyswap alias' to give them labels."
            )
        return [
            (source, self._register_token(text, email_override))
            for source, text in tokens
        ]

    @_registry_mutation
    def import_profile_login(self, profile: Path) -> Account:
        """Register a fresh isolated login without activating it."""
        token = credentials.read_profile_token(profile)
        if not token:
            raise AgyError("The isolated agy login did not produce a usable token.")
        return self._register_token(token, None, isolated=True)

    def _register_token(
        self, token: str, email_override: Optional[str], *, isolated: bool = False
    ) -> Account:
        verified = credentials.email_from_token(token)
        if isolated and not verified:
            raise AgyError("Could not determine the email for the isolated agy login.")

        # The token's signed identity outranks a typed-in label.  Accepting a
        # mismatching override would create an account whose backup can never
        # be activated (the switch-time guard would reject it), so refuse early
        # and point at the right tool.
        if (
            email_override
            and verified
            and email_override.lower() != verified.lower()
        ):
            raise AgyError(
                f"This token belongs to {verified}, not {email_override}. "
                f"Run 'agyswap add' without --email, then "
                f"'agyswap alias' to give it a label."
            )

        email = (
            email_override
            or verified
            or _legacy_active_email()
            or _email_from_oauth_creds()
            or f"account-{self._next_number()}"
        )

        account = self._find_account_by_email(email)
        if account:
            # Only overwrite when the token's identity is confirmed, so a
            # half-finished login can never corrupt a good backup.
            if not verified:
                return account
            self._save_backup_if_fresher(account, token, required=isolated)
            account.updated_at = now_iso()
        else:
            number = self._next_number()
            last_backup = max(
                (entry.credential_number for entry in self._seq.accounts.values()),
                default=0,
            )
            account = Account(number=number, email=email, backup_number=max(number, last_backup + 1))
            credentials.save_token_backup(account.credential_number, email, token)
            self._seq.accounts[number] = account
            if number not in self._seq.sequence:
                self._seq.sequence.append(number)

        if not isolated:
            self._seq.active_email = account.email
        self._save_sequence()
        if not isolated and paths.SSH_TOKEN_MIRROR.exists():
            credentials.write_token_mirror(token)
        return account

    def orphan_backups(self) -> list[dict]:
        """Backups on disk whose account is not in the registry, oldest first."""
        known = {account.email.lower() for account in self._seq.accounts.values()}
        orphaned: list[dict] = []
        for entry in credentials.scan_token_backups():
            email = self._backup_email(entry)
            if email and email.lower() not in known:
                orphaned.append(entry)
        orphaned.sort(key=lambda entry: (entry["expiry"] or 0.0, entry["number"]))
        return orphaned

    @_registry_mutation
    def recover_accounts(self) -> list[Account]:
        """Re-adopt backups whose registry entry was lost.

        The account list lives in ``sequence.json`` while the tokens live in
        ``~/.agy-swap/credentials``; losing the former used to mean losing the
        list even though every token was still on disk.  Each backup whose
        account is not registered is adopted again, keeping the slot number
        encoded in its file name when that slot is still free.
        """
        recovered: list[Account] = []
        touched = False
        for entry in credentials.scan_token_backups():
            email = self._backup_email(entry)
            if not email:
                continue
            existing = self._find_account_by_email(email)
            if existing:
                # A stale copy of a managed account: only adopt it when it is
                # actually newer than the backup already registered.
                stored = credentials.read_token_backup(
                    existing.credential_number, existing.email
                )
                stored_expiry = credentials.token_expiry(stored) if stored else None
                if entry["expiry"] and (
                    stored_expiry is None or entry["expiry"] > stored_expiry
                ):
                    credentials.save_token_backup(
                        existing.credential_number, existing.email, entry["token"]
                    )
                    existing.updated_at = now_iso()
                    touched = True
                continue

            number = entry["number"]
            if number in self._seq.accounts:
                number = self._next_number()
            account = Account(number=number, email=email, backup_number=entry["number"])
            self._seq.accounts[number] = account
            self._seq.sequence.append(number)
            credentials.save_token_backup(account.credential_number, email, entry["token"])
            recovered.append(account)
            touched = True

        if touched:
            self._seq.sequence.sort()
            self._save_sequence()
        return recovered

    @staticmethod
    def _backup_email(entry: dict) -> Optional[str]:
        return entry["email"] or credentials.email_from_backup_slug(entry["slug"])

    def remove_account(self, identifier: str) -> Account:
        return self.remove_accounts([identifier])[0]

    @_registry_mutation
    def remove_accounts(self, identifiers: list[str]) -> list[Account]:
        removed = {}
        for identifier in identifiers:
            account = self._resolve_account(identifier)
            removed[account.number] = account
        self._renumber(exclude=set(removed))
        removed_emails = {account.email.lower() for account in removed.values()}
        for account in removed.values():
            credentials.delete_token_backup(account.credential_number, account.email)
            # The profile may still hold a live token after the account leaves
            # the registry.  Drop the credential; keep history and settings.
            credentials.delete_profile_token(paths.profile_gemini_dir(account.email))
        mirror = credentials.read_token_mirror()
        if mirror:
            mirror_email = credentials.email_from_token(mirror, allow_network=False)
            if mirror_email and mirror_email.lower() in removed_emails:
                credentials.delete_token_mirror()
        return list(removed.values())

    @_registry_mutation
    def renumber(self) -> int:
        return self._renumber(exclude=set())

    def _renumber(self, *, exclude: set[int]) -> int:
        ordered = dict.fromkeys([*self._seq.sequence, *self._seq.accounts])
        remaining = [
            self._seq.accounts[number] for number in ordered
            if number in self._seq.accounts and number not in exclude
        ]
        accounts = {
            number: replace(account, number=number, backup_number=account.credential_number)
            for number, account in enumerate(remaining, start=1)
        }
        changed = sum(account.number != number for number, account in enumerate(remaining, 1))
        if exclude or list(accounts) != self._seq.sequence or changed:
            previous = self._seq
            removed_emails = {
                previous.accounts[n].email.lower()
                for n in exclude
                if n in previous.accounts
            }
            active_email = (
                None
                if previous.active_email and previous.active_email.lower() in removed_emails
                else previous.active_email
            )
            self._seq = replace(
                previous,
                active_email=active_email,
                accounts=accounts,
                sequence=list(accounts),
            )
            try:
                self._save_sequence()
            except OSError:
                self._seq = previous
                raise
        return changed

    @_registry_mutation
    def set_disabled(self, identifier: str, disabled: bool) -> Account:
        account = self._resolve_account(identifier)
        account.disabled = disabled
        account.updated_at = now_iso()
        self._save_sequence()
        return account

    @_registry_mutation
    def switch(self, identifier: Optional[str] = None) -> dict:
        if identifier is None:
            return self._rotate()
        return self._activate(self._resolve_account(identifier))

    def _rotate(self) -> dict:
        enabled = [
            self._seq.accounts[number]
            for number in self._seq.sequence
            if number in self._seq.accounts and not self._seq.accounts[number].disabled
        ]
        if not enabled:
            raise AgyError("No enabled accounts to switch to.")

        active_email = current_email()
        index = -1
        if active_email:
            index = next(
                (i for i, a in enumerate(enabled) if a.email.lower() == active_email.lower()),
                -1,
            )
        target = enabled[(index + 1) % len(enabled)]
        return self._activate(target)

    def _activate(self, target: Account) -> dict:
        token = credentials.read_token_backup(target.credential_number, target.email)
        if not token:
            raise AgyError(
                f"No stored token for {target.email}. "
                f"Log in with agy and run 'agyswap add' first."
            )

        expected = credentials.email_from_token(token, allow_network=False)
        if expected and expected.lower() != target.email.lower():
            raise AgyError(
                f"Stored token for {target.email} actually belongs to {expected}. "
                f"Log in to {target.email} in agy and run 'agyswap add' to fix."
            )

        previous = current_email()
        live = credentials.read_token()[0]
        if live:
            live_email = credentials.email_from_token(live, allow_network=False)
            owner = self._find_account_by_email(live_email) if live_email else None
            if owner:
                # Capture any refresh agy performed while this account was active.
                self._save_backup_if_fresher(owner, live)

        written = credentials.write_token(token)
        _write_google_accounts(target.email)
        self._seq.active_email = target.email
        self._save_sequence()
        return {
            "previous": previous,
            "active": target.email,
            "number": target.number,
            "targets": written,
        }

    # -- aliases ----------------------------------------------------------

    @_registry_mutation
    def alias(self, identifier: str, alias: Optional[str] = None) -> Account:
        account = self._resolve_account(identifier)
        if alias is None:
            return account
        account.alias = alias
        account.updated_at = now_iso()
        self._save_sequence()
        return account

    @_registry_mutation
    def unalias(self, identifier: str) -> Account:
        account = self._resolve_account(identifier)
        account.alias = None
        account.updated_at = now_iso()
        self._save_sequence()
        return account


__all__ = [
    "AgySwitcher",
    "AgyError",
    "AccountNotFoundError",
    "current_email",
]
