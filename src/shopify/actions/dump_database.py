"""Action for dumping the Postgres database to a timestamped SQL file"""

import subprocess
from datetime import datetime
from pathlib import Path

from src.lib.logger import logger

# Tables holding customer personal data. Their schema is always dumped; their
# rows only with include_customers.
CUSTOMER_TABLES = ("azure.customers", "azure.orders", "azure.order_items")


def dump_database(output_dir: str = "./dumps", include_customers: bool = False):
    """Dump the database to a timestamped SQL file via docker exec.

    Customer and order rows are excluded unless include_customers is set, so
    dump files do not carry personal data by default.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = out_path / f"dump-coop-{timestamp}.sql"

    logger.info(f"Dumping database to {filename}")

    command = ["docker", "exec", "-t", "azure-db", "pg_dump", "-U", "root", "-d", "azure"]
    if not include_customers:
        for table in CUSTOMER_TABLES:
            command.append(f"--exclude-table-data={table}")
        logger.info("Excluding customer and order rows; pass --include-customers to keep them")

    with open(filename, "w", encoding="utf-8") as f:
        result = subprocess.run(
            command,
            stdout=f,
            stderr=subprocess.PIPE,
            check=False,
        )

    if result.returncode != 0:
        logger.error(f"pg_dump failed: {result.stderr.decode()}")
        filename.unlink(missing_ok=True)
        raise RuntimeError("Database dump failed")

    logger.success(f"Database dumped to {filename}")
    return filename
