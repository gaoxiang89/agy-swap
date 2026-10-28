import base64
import json
import time
import unittest

from tests import bootstrap

bootstrap.reset()

from agyswap import credentials, paths  # noqa: E402


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def make_jwt(claims: dict) -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.signature"


def make_token(email: str = "a@example.com", *, expiry: float | None = None,
               access: str = "access", refresh: str = "refresh") -> str:
    inner = {
        "access_token": access,
        "token_type": "Bearer",
        "refresh_token": refresh,
        "id_token": make_jwt({"email": email}),
    }
    if expiry is not None:
        inner["expiry"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expiry))
    return json.dumps({"token": inner, "auth_method": "consumer"})


class LooksLikeTokenTest(unittest.TestCase):
    def test_accepts_real_shape(self):
        self.assertTrue(credentials.looks_like_token(make_token()))

    def test_rejects_garbage(self):
        for value in (None, "", "not json", "{}", '{"a": 1}'):
            self.assertFalse(credentials.looks_like_token(value), value)

    def test_rejects_json_list(self):
        self.assertFalse(credentials.looks_like_token('[{"access_token": "x"}]'))


class EmailExtractionTest(unittest.TestCase):
    def test_reads_id_token_offline(self):
        token = make_token("alice@example.com")
        self.assertEqual(
            credentials.email_from_token(token, allow_network=False),
            "alice@example.com",
        )

    def test_email_survives_bad_jwt(self):
        payload = {"token": {"access_token": "a", "id_token": "broken"}}
        self.assertIsNone(
            credentials.email_from_token(json.dumps(payload), allow_network=False)
        )

    def test_top_level_email_hint(self):
        token = json.dumps({"token": {"access_token": "a"}, "email": "b@example.com"})
        self.assertEqual(
            credentials.email_from_token(token, allow_network=False), "b@example.com"
        )

    def test_id_token_email_field(self):
        """agy's Credential Manager payload carries only a plain email string."""
        token = json.dumps(
            {
                "token": {"access_token": "a"},
                "auth_method": "consumer",
                "id_token_email": "c@example.com",
            }
        )
        self.assertEqual(
            credentials.email_from_token(token, allow_network=False), "c@example.com"
        )

    def test_jwt_wins_over_plain_field(self):
        token = json.dumps(
            {
                "token": {"access_token": "a"},
                "id_token": make_jwt({"email": "jwt@example.com"}),
                "id_token_email": "plain@example.com",
            }
        )
        self.assertEqual(
            credentials.email_from_token(token, allow_network=False), "jwt@example.com"
        )


class ExpiryTest(unittest.TestCase):
    def test_parses_iso_expiry(self):
        future = time.time() + 3600
        token = make_token(expiry=future)
        parsed = credentials.token_expiry(token)
        self.assertIsNotNone(parsed)
        self.assertAlmostEqual(parsed, future, delta=2)

    def test_missing_expiry_is_not_expired(self):
        self.assertFalse(credentials.token_is_expired(make_token()))

    def test_past_expiry_is_expired(self):
        token = make_token(expiry=time.time() - 60)
        self.assertTrue(credentials.token_is_expired(token))

    def test_skew_treats_near_expiry_as_expired(self):
        token = make_token(expiry=time.time() + 10)
        self.assertTrue(credentials.token_is_expired(token, skew=60))


class KeyringEncodingTest(unittest.TestCase):
    def test_unwrap_base64_prefix(self):
        encoded = base64.b64encode(b'{"access_token": "x"}').decode()
        raw = paths.KEYRING_PREFIX + encoded
        self.assertEqual(credentials._unwrap_secret(raw), '{"access_token": "x"}')

    def test_unwrap_plain_passthrough(self):
        self.assertEqual(credentials._unwrap_secret('{"a": 1}'), '{"a": 1}')

    def test_wrap_roundtrip(self):
        text = '{"access_token": "x"}'
        self.assertEqual(
            credentials._unwrap_secret(credentials._wrap_secret(text, True)), text
        )
        self.assertEqual(credentials._wrap_secret(text, False), text)


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        bootstrap.reset()

    def test_writes_content(self):
        target = paths.BACKUP_ROOT / "nested" / "file.json"
        credentials.atomic_write(target, '{"a": 1}')
        self.assertEqual(target.read_text(encoding="utf-8"), '{"a": 1}')

    def test_leaves_no_temp_files(self):
        target = paths.BACKUP_ROOT / "nested" / "file.json"
        credentials.atomic_write(target, "x")
        leftovers = list(target.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_backup_roundtrip(self):
        token = make_token("round@example.com")
        paths.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        credentials.save_token_backup(3, "round@example.com", token)
        self.assertEqual(
            credentials.read_token_backup(3, "round@example.com"), token
        )

    def test_backup_is_base64_not_plaintext(self):
        token = make_token("secret@example.com")
        credentials.save_token_backup(1, "secret@example.com", token)
        raw = credentials.backup_path(1, "secret@example.com").read_text(encoding="utf-8")
        self.assertNotIn("access_token", raw)
        self.assertEqual(base64.b64decode(raw).decode(), token)

    def test_corrupt_backup_returns_none(self):
        path = credentials.backup_path(9, "bad@example.com")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("!!!not-base64!!!", encoding="utf-8")
        self.assertIsNone(credentials.read_token_backup(9, "bad@example.com"))


class TokenSelectionTest(unittest.TestCase):
    """Reading must pick the freshest token, not the first file it finds."""

    def setUp(self):
        bootstrap.reset()
        self._saved = (paths.TOKEN_PATH, paths.LEGACY_TOKEN_PATH, paths.TOKEN_CANDIDATES)

    def tearDown(self):
        (
            paths.TOKEN_PATH,
            paths.LEGACY_TOKEN_PATH,
            paths.TOKEN_CANDIDATES,
        ) = self._saved

    def _use_paths(self, primary, legacy):
        paths.TOKEN_PATH = primary
        paths.LEGACY_TOKEN_PATH = legacy
        paths.TOKEN_CANDIDATES = (primary, legacy)

    def test_prefers_later_expiry(self):
        from unittest import mock

        older = paths.GEMINI_DIR / "old-token"
        newer = paths.GEMINI_DIR / "new-token"
        paths.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self._use_paths(newer, older)
        older.write_text(make_token(expiry=time.time() + 60), encoding="utf-8")
        newer.write_text(make_token(expiry=time.time() + 7200), encoding="utf-8")
        with mock.patch.object(credentials, "read_keyring", return_value=None):
            token, source = credentials.read_token()
        self.assertEqual(source, str(newer))
        self.assertEqual(credentials.email_from_token(token, allow_network=False),
                         "a@example.com")

    def test_keyring_wins_when_fresher(self):
        from unittest import mock

        file_token = paths.GEMINI_DIR / "token"
        paths.GEMINI_DIR.mkdir(parents=True, exist_ok=True)
        self._use_paths(file_token, paths.GEMINI_DIR / "absent")
        file_token.write_text(make_token(expiry=time.time() + 60), encoding="utf-8")
        ring = make_token("ring@example.com", expiry=time.time() + 7200)
        with mock.patch.object(credentials, "read_keyring", return_value=ring):
            _token, source = credentials.read_token()
        self.assertTrue(source.startswith("keyring:"))

    def test_returns_none_when_absent(self):
        from unittest import mock

        self._use_paths(
            paths.GEMINI_DIR / "nope1", paths.GEMINI_DIR / "nope2"
        )
        with mock.patch.object(credentials, "read_keyring", return_value=None):
            token, source = credentials.read_token()
        self.assertIsNone(token)
        self.assertEqual(source, "none")


if __name__ == "__main__":
    unittest.main()
