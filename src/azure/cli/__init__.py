from multiprocessing import Pool

from psycopg import sql, rows
import typer

from src.db.postgres import Database
from src.db.models.media import MediaModel

from .get_products_from_azure import get_products_from_azure
from .upload_images import upload_images

app = typer.Typer()


@app.command()
def scrape(
    limit: int = typer.Option(
        None, "--limit", "-l", help="Limit total products scraped (dev use)"
    ),
):
    """Scrape products from Azure Standard into the DB."""
    get_products_from_azure(limit=limit)


@app.command()
def upload_remaining_images():
    database = Database()

    media = database.fetchall(
        sql.SQL(
            """
            select * from (select distinct on (m.packaging_code)
                m.* from azure.media as m
            join azure.packaging as pack on pack.code = m.packaging_code
            join azure.products as p on p.id = pack.products_id
            where
                p.shopify_product_id is not null and
                pack.shopify_variant_id is not null
            order by m.packaging_code, m.shopify_media_id)
            where shopify_media_id is null limit 10;
            """
        ),
        {},
        rows.class_row(MediaModel),
    )

    number_of_images = len(media)
    print(number_of_images)
    with Pool(processes=7) as pool:
        for index, _ in enumerate(pool.imap_unordered(upload_images, media), 1):
            print(f"\rdone: {index / number_of_images:%}")


__all__ = ["app"]
