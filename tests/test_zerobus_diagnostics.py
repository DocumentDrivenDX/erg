import io
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "central"))
from erg_central.zerobus import Zerobus, error_reason


class ZerobusDiagnosticsTests(unittest.TestCase):
    def test_classification_never_returns_arbitrary_service_text(self):
        self.assertEqual(error_reason({"message": "Insufficient scope SECRET"}), "oauth_scope")
        self.assertEqual(
            error_reason({"message": "storage account PRIVATE"}), "private_storage_access"
        )
        self.assertEqual(error_reason({"message": "SECRET"}), "unknown")
        self.assertEqual(error_reason({"message": []}), "unknown")

    def test_http_error_does_not_disclose_message(self):
        client = Zerobus(
            "https://example.test", "123", "https://zero.test", "erg.ingest.raw", "client", "SECRET"
        )
        client.opener = Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            "https://zero.test",
            403,
            "",
            {},
            io.BytesIO(b'{"error_code":"PERMISSION_DENIED","message":"insufficient scope SECRET"}'),
        )
        with self.assertRaisesRegex(
            RuntimeError, r"PERMISSION_DENIED; reason=oauth_scope"
        ) as caught:
            client._request(urllib.request.Request("https://zero.test/insert"))
        self.assertNotIn("SECRET", str(caught.exception))

    def test_scope_cannot_silently_widen(self):
        with self.assertRaises(ValueError):
            Zerobus(
                "https://example.test",
                "123",
                "https://zero.test",
                "erg.ingest.raw",
                "client",
                "SECRET",
                oauth_scope="all-apis",
            )
