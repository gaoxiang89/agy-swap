"""Account registry and switching logic.

This module owns *what* gets swapped; :mod:`agyswap.credentials` owns *how* the
platform stores it.  Keeping that split is what lets the same logic run on
macOS (Keychain), Windows (Credential Manager) and Linux (Secret Service).
"""

from __future__ import annotations

import json
from typing import Optional

from agyswap import credentials, paths, quota
from agyswap.models import Account, SequenceData, now_iso


class AgyError(Exception):
    pass


class AccountNotFoundError(AgyError):
    pass


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
        active_email = current_email()
        cache = quota.load_cache()
        rows: list[dict] = []
        for number in self._seq.sequence:
            account = self._seq.accounts.get(number)
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
                    "has_token": credentials.read_token_backup(number, account.email)
                    is not None,
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

    # -- mutations --------------------------------------------------------

    def add_current(self, email_override: Optional[str] = None) -> Account:
        token = credentials.read_token()[0]
        if not token:
            raise AgyError(
                "No agy token found. Log in with agy first, then run 'agyswap add'."
            )

        verified = credentials.email_from_token(token)

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

        existing = self._find_account_by_email(email)
        if existing:
            # Only overwrite when the token's identity is confirmed, so a
            # half-finished login can never corrupt a good backup.
            if verified and existing.email.lower() == verified.lower():
                credentials.save_token_backup(existing.number, existing.email, token)
                existing.updated_at = now_iso()
                self._save_sequence()
            return existing

        number = self._next_number()
        account = Account(number=number, email=email)
        self._seq.accounts[number] = account
        if number not in self._seq.sequence:
            self._seq.sequence.append(number)
        credentials.save_token_backup(number, email, token)
        self._save_sequence()
        return account

    def remove_account(self, identifier: str) -> Account:
        account = self._resolve_account(identifier)
        credentials.delete_token_backup(account.number, account.email)
        self._seq.accounts.pop(account.number, None)
        if account.number in self._seq.sequence:
            self._seq.sequence.remove(account.number)
        self._save_sequence()
        return account

    def set_disabled(self, identifier: str, disabled: bool) -> Account:
        account = self._resolve_account(identifier)
        account.disabled = disabled
        account.updated_at = now_iso()
        self._save_sequence()
        return account

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
        token = credentials.read_token_backup(target.number, target.email)
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
                credentials.save_token_backup(owner.number, owner.email, live)

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

    def alias(self, identifier: str, alias: Optional[str] = None) -> Account:
        account = self._resolve_account(identifier)
        if alias is None:
            return account
        account.alias = alias
        account.updated_at = now_iso()
        self._save_sequence()
        return account

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
