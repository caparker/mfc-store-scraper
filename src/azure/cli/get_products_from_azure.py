"""Action for scraping and saving all products from Azure Standard"""

from psycopg import sql

from src.azure.scraper import AzureScraper
from src.db.postgres import Database
from src.lib.logger import logger


def get_products_from_azure(limit: int | None = None):
    logger.info("Starting Azure product scraper")

    scraper = AzureScraper()

    logger.info(
        f"Retrieving {'up to ' + str(limit) if limit else 'all'} products..."
    )
    all_products = scraper.get_all_products(limit=limit)
    logger.debug(f"Retrieved {len(all_products)} products")

    logger.info("Formatting products...")
    (formatted_products, formatted_variants, formatted_prices, formatted_media) = (
        scraper.format_products(all_products)
    )

    logger.debug(
        f"Formatted {len(formatted_products)} products, \
        {len(formatted_variants)} variants, {len(formatted_prices)} prices"
    )

    database = Database()
    logger.info("Connected to database")

    # pylint: disable=line-too-long
    product_query = sql.SQL(
        """
        INSERT INTO azure.products (id, name, short_description, description, slug, storage_climate, unshippable_regions, brand, substitutions, category)
        VALUES
        (%(id)s,%(name)s,%(short_description)s,%(description)s,%(slug)s,%(storage_climate)s,%(unshippable_regions)s,%(brand)s,%(substitutions)s, %(category)s)
        ON CONFLICT(id)
        DO UPDATE SET
            name = EXCLUDED.name,
            short_description = EXCLUDED.short_description,
            description = EXCLUDED.description,
            slug = EXCLUDED.slug,
            storage_climate = EXCLUDED.storage_climate,
            unshippable_regions = EXCLUDED.unshippable_regions,
            brand = EXCLUDED.brand,
            substitutions = EXCLUDED.substitutions,
            category = EXCLUDED.category,
            updated_at = now()
    """
    )

    logger.info("Inserting products...")
    database.batch_execute(product_query, formatted_products)
    logger.info(f"Inserted {len(formatted_products)} products")

    variants_query = sql.SQL(
        """
        INSERT INTO azure.variants (products_id, code, size, weight, stock, rewards_enabled, freight_handling_required, tags, primary_category,favorites,next_purchase_arrival, last_seen_at)
        VALUES (%(products_id)s,%(code)s,%(size)s,%(weight)s,%(stock)s,%(rewards_enabled)s,%(freight_handling_required)s,%(tags)s,%(primary_category)s,%(favorites)s,%(next_purchase_arrival)s, now())
        ON CONFLICT(code)
        DO UPDATE SET
            size = EXCLUDED.size,
            weight = EXCLUDED.weight,
            stock = EXCLUDED.stock,
            rewards_enabled = EXCLUDED.rewards_enabled,
            freight_handling_required = EXCLUDED.freight_handling_required,
            tags = EXCLUDED.tags,
            primary_category = EXCLUDED.primary_category,
            favorites = EXCLUDED.favorites,
            next_purchase_arrival = EXCLUDED.next_purchase_arrival,
            last_seen_at = now();
    """
    )

    logger.info("Inserting variants...")
    database.batch_execute(variants_query, formatted_variants)
    logger.info(f"Inserted {len(formatted_variants)} variant records")

    if limit is None:
        # A full scrape returned every variant Azure still lists. Anything not
        # touched by this run is gone from Azure, so it has no stock. All rows in
        # the batch above share one transaction timestamp, so max(last_seen_at)
        # identifies this run. Skipped under --limit, which is a partial scrape.
        zeroed = database.execute(
            sql.SQL(
                """
                UPDATE azure.variants
                SET stock = 0
                WHERE stock <> 0
                  AND last_seen_at IS DISTINCT FROM (
                    SELECT max(last_seen_at) FROM azure.variants
                  )
                """
            )
        )
        logger.info(f"Zeroed stock on {zeroed} variant(s) no longer listed by Azure")

    # A price row is written only when the variant exists and the price differs
    # from the latest row already held.
    prices_query = sql.SQL(
        """
        INSERT INTO azure.prices (variants_id, retail_dollars, retail_unit, wholesale_dollars, wholesale_unit)
        SELECT v.id, %(retail_dollars)s, %(retail_unit)s, %(wholesale_dollars)s, %(wholesale_unit)s
        FROM azure.variants v
        WHERE v.code = %(code)s
        AND (
            SELECT (retail_dollars, retail_unit, wholesale_dollars, wholesale_unit)
            FROM azure.prices
            WHERE variants_id = v.id
            ORDER BY created_at DESC
            LIMIT 1
        ) IS DISTINCT FROM (
            %(retail_dollars)s::real,
            %(retail_unit)s::text,
            %(wholesale_dollars)s::real,
            %(wholesale_unit)s::text
        );
        """
    )

    logger.info("Inserting prices...")
    database.batch_execute(prices_query, formatted_prices)
    logger.info(f"Inserted {len(formatted_prices)} price records")

    media_query = sql.SQL(
        """
        INSERT INTO azure.media (variants_id, original_url, file_name, position)
        SELECT v.id, %(original_url)s, %(file_name)s, %(position)s
        FROM azure.variants v
        WHERE v.code = %(code)s
        ON CONFLICT (variants_id, original_url) DO UPDATE SET
            position = EXCLUDED.position;
        """
    )

    logger.info("Inserting media...")
    database.batch_execute(media_query, formatted_media)
    logger.info(f"Inserted {len(formatted_media)} media records")

    logger.success("Azure product scraper completed successfully")
