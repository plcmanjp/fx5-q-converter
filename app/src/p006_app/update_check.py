"""Read-only, bounded lookup of this application's published Windows release."""
from dataclasses import dataclass
import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

REPOSITORY_URL = "https://github.com/plcmanjp/fx5-q-converter"
LATEST_API = "https://api.github.com/repos/plcmanjp/fx5-q-converter/releases/latest"
MAX_RESPONSE_BYTES = 256 * 1024
TIMEOUT_SECONDS = 3


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    page_url: str
    asset_name: str


@dataclass(frozen=True)
class UpdateResult:
    status: str
    release: ReleaseInfo | None = None
    reason: str = ""


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", value):
        raise ValueError("Expected a stable semantic version")
    return tuple(map(int, value.split(".")))


def release_page_url(tag):
    if not isinstance(tag, str):
        raise ValueError("Expected a stable release tag")
    version_tuple(tag.removeprefix("v"))
    return REPOSITORY_URL + "/releases/tag/" + tag


def parse_release(payload):
    if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ValueError("Published stable release required")
    tag = payload.get("tag_name")
    page = release_page_url(tag)
    if payload.get("html_url") != page:
        raise ValueError("Unexpected release page")
    version = tag.removeprefix("v")
    asset_name = f"Q-FX-Converter-v{version}.zip"
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Windows asset missing")
    matches = [a for a in assets if isinstance(a, dict) and a.get("name") == asset_name]
    if len(matches) != 1:
        raise ValueError("Exactly one Windows asset required")
    asset = matches[0]
    if (asset.get("state") != "uploaded" or type(asset.get("size")) is not int or asset["size"] <= 0
            or asset.get("browser_download_url") != f"{REPOSITORY_URL}/releases/download/{tag}/{asset_name}"):
        raise ValueError("Windows asset is not ready")
    return ReleaseInfo(version, tag, page, asset_name)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "Release lookup redirect refused", headers, None)


def check_for_update(current_version, *, opener=None):
    try:
        current = version_tuple(current_version)
        request = Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "FX5-Q-Converter/" + current_version,
                          "X-GitHub-Api-Version": "2022-11-28"})
        if opener is None:
            opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != LATEST_API:
                return UpdateResult("unavailable", reason="unexpected-response-url")
            data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            return UpdateResult("unavailable", reason="response-too-large")
        release = parse_release(json.loads(data.decode("utf-8")))
        return UpdateResult("available" if version_tuple(release.version) > current else "current", release)
    except HTTPError as error:
        error.close()
        return UpdateResult("unavailable", reason=f"http-{error.code}")
    except (URLError, TimeoutError, OSError):
        return UpdateResult("unavailable", reason="network-error")
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return UpdateResult("unavailable", reason="invalid-release")
