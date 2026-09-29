"""Wrapper class for scraping data from the Azure website"""

import json
import re
from typing import Dict, List

import requests
from psycopg.types.json import Jsonb
from urllib.parse import urlencode
import json

from src.lib.logger import logger
from src.settings import settings

ATTRIBUTES_TO_RETRIEVE = [
    "id",
    "brand.name",
    "name",
    "substitutions",
    "favorites",
    "storageClimate",
    "slug",
    "maxStorageDays",
    "treatAsActive",
    "unshippableRegions",
    "packaging.code",
    "packaging.price",
    "packaging.weight",
    "packaging.volume",
    "packaging.tags",
    "packaging.images",
    "packaging.size",
    "packaging.stock",
    "packaging.next-purchase-arrival",
    "packaging.favorites",
    "packaging.bargain-bin-notes",
    "packaging.rewardsEnabled",
    "packaging.freightHandlingRequired",
    "packaging.vendorShortedLastPurchase",
    "packaging.primary-category",
    "description",
    "shortDescription",
]


class AzureScraper:
    """Wrapper class for scraping data from the Azure Standard website"""

    app_id = ""
    api_key = ""
    __categories = None

    def __init__(self, app_id=None, api_key=None):
        self.app_id = app_id or settings.app_id
        self.api_key = api_key or settings.api_key


    def _post(self, search_index: str, params: str):
        """Make a POST request to Azure API with error handling"""
        try:
            resp = requests.post(
                self.get_url(search_index),
                headers=self.headers(),
                json={"params": params},
                timeout=5,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(
                f"HTTP error for {search_index}: {e.response.status_code} - {e.response.text}"
            )
            raise
        except requests.exceptions.Timeout:
            logger.error(f"Request to {search_index} timed out")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request to {search_index} failed: {e}")
            raise
        except ValueError as e:
            logger.error(f"Failed to parse JSON response from {search_index}: {e}")
            raise

    def get_url(self, search_index):
        """Get the URL for Azure API access"""
        # pylint: disable=line-too-long
        return f"https://{self.app_id}-dsn.algolia.net/1/indexes/{search_index}/query?x-algolia-application-id={self.app_id}&x-algolia-api-key={self.api_key}"

    def headers(self):
        """Common headers for requests"""
        return {
            "content-type": "application/x-www-form-urlencoded",
            "accept": "application/json",
        }

    def get_d1_categories(self):
        """Get depth=1 categories for Azure"""
        if self.__categories is None:
            logger.info("Fetching categories")
            data = self._post(
                "categories",
                "query=&attributesToHighlight=&filters=depth=1&hitsPerPage=5000",
            )
            self.__categories = data.get("hits")
        return self.__categories

    def get_products_for_category(self, category_id, page=0):
        """Get all products for a given category."""
        params = urlencode(
            {
                "query": "",
                "filters": "packaging.stock > 0",
                "attributesToHighlight": "",
                "attributesToRetrieve": ",".join(ATTRIBUTES_TO_RETRIEVE),
                "queryType": "prefixNone",
                "facetFilters": json.dumps([[], f"category-ids:{category_id}", []]),
                "optionalFilters": json.dumps(["isPromoted:true"]),
                "hitsPerPage": 5000,
                "page": page,
            }
        )
        return self._post("products", params)

    def get_all_products(self, limit: int | None = None):
        """Get all Azure products"""
        hits = []
        seen_ids = set()
        for category in self.get_d1_categories():
            if limit and len(hits) >= limit:
                break
            page = 0
            num_pages = 1
            while page < num_pages:
                if limit and len(hits) >= limit:
                    break

                category_id = category.get("id")
                ancestors = category.get("ancestors") or [{"slug": "Root"}]
                parent_slug = ancestors[0].get("slug")
                category_name = f"{parent_slug}.{category.get('slug')}"

                resp = self.get_products_for_category(category_id, page)
                if resp.get("nbHits") >= 2000:
                    print(f"More than 2k products in category: {category_id}")
                _hits = resp.get("hits")

                for hit in _hits:
                    if limit and len(hits) >= limit:
                        break
                    if hit.get("id") in seen_ids:
                        continue
                    seen_ids.add(hit.get("id"))
                    hit["category"] = category_name
                    hits.append(hit)

                num_pages = resp.get("nbPages")
                page += 1

        return hits

    def get_local_products(self):
        """Get products from a local file"""
        with open("../../outputs/data.json", "r", encoding="utf8") as f:
            return json.loads(f.read())

    def format_products(
        self, unformatted_products
    ) -> tuple[List[Dict], List[Dict], List[Dict], List[Dict]]:
        """Format Azure products for insertion into database"""
        products = []
        packaging = []
        prices = []

        media = []

        for product in unformatted_products:
            products.append(
                {
                    "id": product.get("id"),
                    "name": product.get("name"),
                    "short_description": product.get("shortDescription"),
                    "description": product.get("description"),
                    "slug": f"{product.get("slug")}-azure-{product.get("id")}",
                    "storage_climate": product.get("storageClimate"),
                    "unshippable_regions": Jsonb(product.get("unshippableRegions")),
                    "brand": Jsonb(product.get("brand")),
                    "substitutions": Jsonb(product.get("substitutions")),
                    "category": product.get("category"),
                }
            )

            sizes: List[str] = []

            for pack in product.get("packaging"):
                if pack.get("size") in sizes:
                    # We cannot have duplicate sizes for products
                    # as it will fail to create variants in Shopify.
                    # Generally, duplicates are for bargain-bin or in-house
                    # tagged goods that we don't need to track anyway.
                    continue

                packaging.append(
                    {
                        "products_id": product.get("id"),
                        "code": pack.get("code"),
                        "size": pack.get("size"),
                        "weight": Jsonb(pack.get("weight")),
                        "stock": pack.get("stock"),
                        "rewards_enabled": pack.get("rewardsEnabled"),
                        "freight_handling_required": pack.get(
                            "freightHandlingRequired"
                        ),
                        "tags": Jsonb(pack.get("tags")),
                        "primary_category": pack.get("primary-category"),
                        "favorites": pack.get("favorites"),
                        "next_purchase_arrival": pack.get("next-purchase-arrival"),
                    }
                )

                sizes.append(pack.get("size"))

                for image_url in pack.get("images"):
                    media.append(
                        {
                            "packaging_code": pack.get("code"),
                            "original_url": image_url,
                            "file_name": re.search(
                                r"([0-9\-a-z]+)$", image_url
                            ).group(),
                        }
                    )

                prices.append(
                    {
                        "packaging_code": pack.get("code"),
                        "retail_dollars": pack.get("price")
                        .get("retail", {})
                        .get("dollars"),
                        "retail_unit": pack.get("price").get("retail", {}).get("unit"),
                        "wholesale_dollars": pack.get("price")
                        .get("wholesale", {})
                        .get("dollars"),
                        "wholesale_unit": pack.get("price")
                        .get("wholesale", {})
                        .get("unit"),
                    }
                )

        return (products, packaging, prices, media)
