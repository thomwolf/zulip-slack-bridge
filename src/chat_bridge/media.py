"""Bounded transfer of uploaded raster images; never expose authenticated source URLs."""

import io
import os
import re
import time
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import requests
from slack_sdk.errors import SlackApiError

from .config import secret
from .content import label
from .model import DeliveryError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 5
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
UPLOAD_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^\s)]+)\)")


def zulip_images(text: str, site: str) -> list[dict[str, Any]]:
    """Extract uploaded-file links belonging to this Zulip organization only."""
    images = []
    seen = set()
    for match in UPLOAD_LINK.finditer(text):
        url = urlsplit(urljoin(site + "/", match[1]))
        if (url.scheme, url.netloc) != (urlsplit(site).scheme, urlsplit(site).netloc):
            continue
        if not url.path.startswith("/user_uploads/") or url.path in seen:
            continue
        seen.add(url.path)
        images.append(
            {"id": url.path, "name": unquote(url.path.rsplit("/", 1)[-1]), "markup": match[0]}
        )
    return images


def image_extension(data: bytes) -> str:
    """Check image signatures before forwarding bytes; SVG and HTML are not supported."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    return ""


def read_image(url: str, *, token: str = "", ca: str | bool = True) -> bytes:
    """Read at most 10 MiB without forwarding credentials through redirects."""
    try:
        with requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            verify=ca,
            timeout=(10, 30),
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.status_code == 429 or response.status_code >= 500:
                raise DeliveryError("image_download_unavailable", "retry", 30)
            if response.status_code != 200:
                raise DeliveryError("image_download_unavailable", "failed")
            if int(response.headers.get("Content-Length", "0")) > MAX_IMAGE_BYTES:
                raise DeliveryError("image_too_large", "failed")
            chunks = bytearray()
            for chunk in response.iter_content(65536):
                chunks.extend(chunk)
                if len(chunks) > MAX_IMAGE_BYTES:
                    raise DeliveryError("image_too_large", "failed")
            return bytes(chunks)
    except requests.RequestException:
        raise DeliveryError("image_download_unavailable", "retry") from None


def source_metadata(fetch: Any) -> Any:
    """Metadata retrieval precedes all uploads and is safe to retry after disconnects."""
    try:
        return fetch()
    except DeliveryError as error:
        if error.category == "uncertain":
            raise DeliveryError(error.code, "retry", 30) from None
        raise
    except SlackApiError as error:
        if error.response.status_code == 429 or error.response.status_code >= 500:
            raise DeliveryError(
                "image_metadata_unavailable",
                "retry",
                float(error.response.headers.get("Retry-After", 30)),
            ) from None
        raise
    except Exception:
        raise DeliveryError("image_metadata_unavailable", "retry", 30) from None


def transfer_image(transport: Any, destination: str, attachment: dict[str, Any]) -> dict[str, Any]:
    """Download from the source and upload privately to the destination."""
    name = label(attachment.get("name", "image")).replace("|", "")[:100] or "image"
    if name.rsplit(".", 1)[-1].lower() not in IMAGE_EXTENSIONS:
        return {"skipped": "unsupported format", "name": name}
    if destination == "zulip":
        info = source_metadata(lambda: transport.slack.files_info(file=attachment["id"]))["file"]
        if info.get("size", 0) > MAX_IMAGE_BYTES:
            return {"skipped": "larger than 10 MB", "name": name}
        url = info.get("url_private_download") or info.get("url_private", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "files.slack.com"
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            return {"skipped": "unsupported download address", "name": name}
        data = read_image(url, token=secret("SLACK_BOT_TOKEN"))
    else:
        path = attachment["id"]
        if not path.startswith("/user_uploads/") or ".." in unquote(path).split("/"):
            return {"skipped": "unsupported upload path", "name": name}
        response = source_metadata(
            lambda: transport.check_zulip(
                transport.zulip.call_endpoint(path.lstrip("/"), method="GET", timeout=20)
            )
        )
        url = urljoin(transport.cfg.zulip_site + "/", response["url"])
        # The temporary URL is used immediately and never stored. Remote storage
        # redirects/CDN URLs need a separate allowlist; keep this MVP same-origin.
        if (
            urlsplit(url).netloc != urlsplit(transport.cfg.zulip_site).netloc
            or urlsplit(url).scheme != "https"
        ):
            return {"skipped": "external image storage", "name": name}
        data = read_image(url, ca=os.environ.get("ZULIP_CA_BUNDLE") or True)
    extension = image_extension(data)
    if not extension:
        return {"skipped": "unsupported image contents", "name": name}
    name = name.rsplit(".", 1)[0] + "." + extension
    if destination == "zulip":
        file = io.BytesIO(data)
        file.name = name
        result = transport.check_zulip(transport.zulip.upload_file(file), mutation=True)
        return {"url": result.get("url") or result["uri"], "name": name}
    result = transport.slack.files_upload_v2(file=data, filename=name, title=name)
    return {"id": result["files"][0]["id"], "name": name, "uploaded_at": time.time()}
