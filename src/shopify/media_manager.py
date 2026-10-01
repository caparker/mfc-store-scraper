"""Fetch a vendor image and put it in front of Shopify's staged upload endpoint.

Shopify will not fetch Azure's image URLs itself: Azure serves them as
application/octet-stream with no extension and Shopify rejects that. So the
bytes are downloaded here, resized, re-encoded with a real content type, and
posted to a staged upload target whose resourceUrl is then handed to
productCreateMedia.
"""

from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps, UnidentifiedImageError

from src.shopify.mutations import Mutations
from src.shopify.queries import Queries
from src.shopify.shopify import Shopify, ShopifyQueryError

# Longest edge after resizing. Shopify's own limit is 4472px on either side.
MAX_EDGE = 2048
JPEG_QUALITY = 85
DOWNLOAD_TIMEOUT = 60
UPLOAD_TIMEOUT = 120


class MediaDownloadFailedError(Exception):
    """The vendor did not return a usable image."""

    def __init__(self, message: str, code: int | None = None, permanent: bool = False):
        self.message = message
        self.code = code
        # permanent: retrying will not help (404, not an image).
        self.permanent = permanent
        super().__init__(self.message)


class StagedUploadFailedError(Exception):
    """Shopify's staged upload endpoint rejected the file."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


@dataclass
class ImageFile:
    """An image ready to upload."""

    file_name: str
    mime_type: str
    data: bytes


def fetch_image(file_name: str, url: str) -> ImageFile:
    """Download `url`, resize it to fit MAX_EDGE, and return encoded bytes.

    Output is JPEG unless the source has transparency, in which case PNG.
    """
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
    except requests.RequestException as err:
        raise MediaDownloadFailedError(f"download error: {err}") from err

    if resp.status_code != 200:
        raise MediaDownloadFailedError(
            f"download response code: {resp.status_code}",
            code=resp.status_code,
            permanent=resp.status_code in (404, 410),
        )

    try:
        image = Image.open(BytesIO(resp.content))
        image.load()
    except (UnidentifiedImageError, OSError) as err:
        raise MediaDownloadFailedError(
            f"not an image: {err}", permanent=True
        ) from err

    image = ImageOps.exif_transpose(image)
    image.thumbnail((MAX_EDGE, MAX_EDGE))

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    out = BytesIO()
    if has_alpha:
        image.convert("RGBA").save(out, format="PNG", optimize=True)
        return ImageFile(f"{file_name}.png", "image/png", out.getvalue())

    image.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return ImageFile(f"{file_name}.jpg", "image/jpeg", out.getvalue())


def stage_uploads(shopify: Shopify, files: list[ImageFile]) -> list[dict]:
    """Ask Shopify for one staged upload target per file, in the same order."""
    resp = shopify.query_file(
        Mutations.generate_staged_uploads,
        {
            "input": [
                {
                    "filename": f.file_name,
                    "mimeType": f.mime_type,
                    "fileSize": str(len(f.data)),
                    "resource": "IMAGE",
                    "httpMethod": "POST",
                }
                for f in files
            ]
        },
    )
    if resp.get("errors"):
        raise StagedUploadFailedError(f"{resp['errors']}")

    data = resp["data"]["stagedUploadsCreate"]
    if data["userErrors"]:
        raise StagedUploadFailedError(f"{data['userErrors']}")

    targets = data["stagedTargets"]
    if len(targets) != len(files):
        raise StagedUploadFailedError(
            f"asked for {len(files)} staged target(s), got {len(targets)}"
        )
    return targets


def upload_to_target(target: dict, image: ImageFile) -> str:
    """POST the file to its staged target and return the resourceUrl for fileCreate."""
    form = {p["name"]: p["value"] for p in target["parameters"]}
    resp = requests.post(
        target["url"],
        data=form,
        files={"file": (image.file_name, image.data, image.mime_type)},
        timeout=UPLOAD_TIMEOUT,
    )
    if resp.status_code not in (200, 201, 204):
        raise StagedUploadFailedError(
            f"staged upload failed: {resp.status_code} {resp.text[:300]}"
        )
    return target["resourceUrl"]


# ---------------------------------------------------------------------- #
# Reading media back from Shopify
# ---------------------------------------------------------------------- #

PRODUCTS_PER_REQUEST = 10


def fetch_products_media(shopify: Shopify, product_gids: list[str]) -> dict[str, dict]:
    """Return {product gid: node} with each product's media and variant media.

    Products Shopify no longer has are omitted. Media lists are capped at the
    query's page size; `node["media"]["pageInfo"]["hasNextPage"]` says whether
    the list is complete.
    """
    found: dict[str, dict] = {}
    for start in range(0, len(product_gids), PRODUCTS_PER_REQUEST):
        chunk = product_gids[start:start + PRODUCTS_PER_REQUEST]
        resp = shopify.query_file(Queries.products_media_by_ids, {"ids": chunk})
        if resp.get("errors"):
            raise ShopifyQueryError(f"{resp['errors']}")
        for node in resp["data"]["nodes"]:
            if node:
                found[node["id"]] = node
    return found


def cdn_file_name(url: str | None) -> str | None:
    """`.../files/<name>.jpg?v=1` -> `<name>`; None when there is no url."""
    if not url:
        return None
    base = urlparse(url).path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def match_media_by_name(media_nodes: list[dict], file_name: str) -> dict | None:
    """Find the product media whose CDN file name is `file_name`.

    Shopify keeps the uploaded file name, adding a suffix (`<name>_1`) when a
    file with that name already exists, so a prefix match is also accepted.
    """
    for node in media_nodes:
        name = cdn_file_name((node.get("image") or {}).get("url"))
        if name and (name == file_name or name.startswith(f"{file_name}_")):
            return node
    return None
