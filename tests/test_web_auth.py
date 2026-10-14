"""Offline checks of the official portal protocol and failure handling."""
import base64
import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock

from custom_components.dmss.client import APIError, DMSSError, VerificationRequired
from custom_components.dmss.web_auth import LOGIN_PATH, web_login


def http_response(payload=None, status=200, invalid_json=False):
    reply = Mock(status_code=status)
    reply.json = Mock(side_effect=ValueError() if invalid_json else None,
                      return_value=payload)
    return Mock(post=Mock(return_value=reply))


class DesktopAuthTests(unittest.TestCase):
    def login(self, http):
        return web_login("https://dmss.dolynkcloud.com", "dummy@example.com",
                         "dummy-password", http=http)

    def test_official_portal_encryption_and_signature(self):
        http = http_response({"code": 10000, "data": {"token": "dummy-session"}})
        self.assertEqual(self.login(http), {"token": "dummy-session"})
        call = http.post.call_args
        self.assertEqual(call.args[0], "https://dmss.dolynkcloud.com" + LOGIN_PATH)
        body, headers = call.kwargs["data"], call.kwargs["headers"]
        payload = json.loads(body)
        self.assertEqual(set(payload["data"]), {"account", "password", "timezoneOffset"})
        self.assertEqual(payload["data"]["password"],
                         "BtBAyh+ieQKqzjJGwu6+4QAG1fHNaOgd77MixBKYTwSMB6dED1FGnUmIdaQ21n/a")
        self.assertNotIn(b"dummy-password", body)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["x-pcs-username"], "account\\dummy@example.com")
        self.assertEqual(headers["x-pcs-apiver"], "225932")
        self.assertEqual(headers["Content-MD5"], base64.b64encode(hashlib.md5(body).digest()).decode())
        ua = json.loads(base64.b64decode(headers["x-pcs-client-ua"]))
        self.assertEqual(ua["clientType"], "web")
        self.assertEqual(ua["clientVersion"], "V1.99.710")
        canonical = ("POST\n" + LOGIN_PATH + "\n" + headers["Content-MD5"] + "\napplication/json\n" +
                     "".join(k + ":" + headers[k] + "\n" for k in (
                         "x-pcs-apiver", "x-pcs-client-ua", "x-pcs-date", "x-pcs-nonce", "x-pcs-username")))
        # Independent fixture for the portal's double-MD5 password signing key.
        key = hashlib.md5(hashlib.md5(b"dummy-password").hexdigest().encode()).hexdigest().encode()
        expected = base64.b64encode(hmac.new(key, canonical.encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(headers["x-pcs-signature"], expected)
        self.assertEqual(call.kwargs["timeout"], 30)
        self.assertIs(call.kwargs["allow_redirects"], False)
        http.post.assert_called_once()

    def test_integer_and_string_success_codes(self):
        for code in (10000, "10000"):
            with self.subTest(code=code):
                self.assertEqual(self.login(http_response({"code": code, "data": {"token": "session"}}))["token"], "session")

    def test_password_failure_even_if_token_present(self):
        http = http_response({"code": 10000, "data": {"token": "session", "failNum": 0}})
        with self.assertRaisesRegex(APIError, "password rejected"):
            self.login(http)
        http.post.assert_called_once()

    def test_official_account_errors_preserve_code(self):
        for code, reason in ((12318, "does not exist"), ("10009", "locked")):
            with self.subTest(code=code):
                http = http_response({"code": code, "desc": "secret-server-text"})
                with self.assertRaisesRegex(APIError, reason) as raised:
                    self.login(http)
                self.assertEqual(raised.exception.code, str(code))
                self.assertNotIn("secret-server-text", str(raised.exception))
                http.post.assert_called_once()

    def test_future_verification_challenge_is_not_bypassed(self):
        http = http_response({"code": "DC00050525", "data": {"token": "session"}})
        with self.assertRaises(VerificationRequired):
            self.login(http)
        http.post.assert_called_once()

    def test_http_failure_does_not_accept_token(self):
        for status in (301, 401, 403, 429, 500):
            with self.subTest(status=status):
                http = http_response({"code": 10000, "data": {"token": "session"}}, status)
                with self.assertRaises(APIError) as raised:
                    self.login(http)
                self.assertEqual(raised.exception.status, status)
                http.post.assert_called_once()

    def test_malformed_envelopes_and_tokens(self):
        for payload in ([], None, "server-secret", {"code": 10000},
                        {"code": 10000, "data": []},
                        *({"code": 10000, "data": {"token": token}} for token in (None, "", "  ", False, [], {}, 99))):
            with self.subTest(payload=payload):
                with self.assertRaises(DMSSError):
                    self.login(http_response(payload))

    def test_non_json_body_does_not_leak_response(self):
        with self.assertRaisesRegex(DMSSError, "HTTP 502 without JSON"):
            self.login(http_response(status=502, invalid_json=True))

    def test_server_code_objects_are_not_logged(self):
        for code in ({"secret": "hidden"}, "hidden", True):
            with self.subTest(code=code):
                with self.assertRaises(APIError) as raised:
                    self.login(http_response({"code": code}))
                self.assertEqual(raised.exception.code, "unknown")
                self.assertNotIn("hidden", str(raised.exception))

    def test_rejects_insecure_host_before_network(self):
        http = http_response({"code": 10000, "data": {"token": "session"}})
        with self.assertRaises(DMSSError):
            web_login("http://dmss.dolynkcloud.com", "dummy@example.com", "dummy-password", http=http)
        http.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
