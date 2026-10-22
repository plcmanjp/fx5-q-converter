import copy
import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import Mock

from p006_app.update_check import (LATEST_API, MAX_RESPONSE_BYTES, REPOSITORY_URL,
                                   NoRedirect, check_for_update, parse_release)


def release_payload(version="2.2.64"):
    tag = "v" + version
    name = f"Q-FX-Converter-v{version}.zip"
    return {"draft": False, "prerelease": False, "tag_name": tag,
            "html_url": REPOSITORY_URL + "/releases/tag/" + tag,
            "assets": [{"name": name, "state": "uploaded", "size": 100,
                        "browser_download_url": f"{REPOSITORY_URL}/releases/download/{tag}/{name}"}]}


class Response(io.BytesIO):
    def geturl(self):
        return LATEST_API


class UpdateTests(unittest.TestCase):
    def lookup(self, payload, current="2.2.63"):
        opener = Mock()
        opener.open.return_value = Response(json.dumps(payload).encode())
        result = check_for_update(current, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(LATEST_API, request.full_url)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(3, opener.open.call_args.kwargs["timeout"])
        return result

    def test_numeric_version_comparison(self):
        for current, latest, status in [("2.2.9", "2.2.10", "available"), ("2.2.63", "2.2.63", "current"),
                                         ("2.3.0", "2.2.99", "current"), ("2.2.63", "3.0.0", "available")]:
            with self.subTest(current=current, latest=latest):
                self.assertEqual(status, self.lookup(release_payload(latest), current).status)

    def test_draft_prerelease_bad_version_and_wrong_page_rejected(self):
        for key, value in [("draft", True), ("prerelease", True), ("tag_name", "v2.2.64-rc1"),
                           ("tag_name", "../../x"), ("tag_name", "v02.2.64"),
                           ("html_url", "https://example.com/payload")]:
            payload = release_payload()
            payload[key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual("unavailable", self.lookup(payload).status)

    def test_asset_readiness_and_destination(self):
        for key, value in [("size", 0), ("size", True), ("state", "new"),
                           ("browser_download_url", "https://example.com/app.zip")]:
            payload = release_payload()
            payload["assets"][0][key] = value
            with self.subTest(key=key):
                self.assertEqual("unavailable", self.lookup(payload).status)
        for assets in ([], None, [release_payload()["assets"][0]] * 2):
            payload = release_payload()
            payload["assets"] = assets
            self.assertEqual("unavailable", self.lookup(payload).status)

    def test_network_errors_are_nonfatal(self):
        for error in (TimeoutError(), URLError("offline"), HTTPError(LATEST_API, 404, "not found", {}, None),
                      HTTPError(LATEST_API, 403, "rate limit", {}, None)):
            self.assertEqual("unavailable", check_for_update("2.2.63", opener=Mock(open=Mock(side_effect=error))).status)

    def test_payload_limit_and_invalid_json(self):
        for data in (b"x" * (MAX_RESPONSE_BYTES + 1), b"not json", b"\xff", b"[]"):
            self.assertEqual("unavailable", check_for_update("2.2.63", opener=Mock(open=Mock(return_value=Response(data)))).status)

    def test_redirects_are_rejected(self):
        request = Mock(full_url=LATEST_API)
        with self.assertRaises(HTTPError):
            NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://example.com/")
        response = Response(json.dumps(release_payload()).encode())
        response.geturl = lambda: "https://example.com/"
        result = check_for_update("2.2.63", opener=Mock(open=Mock(return_value=response)))
        self.assertEqual("unexpected-response-url", result.reason)


if __name__ == "__main__":
    unittest.main()
