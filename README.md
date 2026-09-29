# MFC Store Scraper

Rather than manually adding each product to our Shopify store, this tool scrapes supplier catalogs into a local Postgres database and syncs the results to Shopify. The database is the source of truth: scrapes mark rows as "dirty," and sync commands push only what has changed.

Currently supported suppliers:
- **Azure Standard** (primary)
- *Hummingbird Wholesale* — legacy scraper archived under [`archive/hummingbird`](./archive/hummingbird)

## Using the repo

### Pre-requisites
- [uv](https://docs.astral.sh/uv/)
- [docker](https://www.docker.com/products/docker-desktop/)

Optionally, a database UI like [DBeaver](https://dbeaver.io/download/) is helpful.

### Getting started locally
1. Run `uv sync` to install dependencies.
2. Create a local environment file: `cp .env.example .env.local`.
3. Start your local Docker daemon.
4. Run `docker compose up -d` to start the local Postgres database.
5. Verify things are running via Docker Desktop or `docker compose ps` (the `azure-db` service should be up).

Setting up DBeaver:
1. Create a new PostgreSQL connection.
2. Host: `localhost`
3. Port: `5999`
4. Database: `azure`
5. Username: `root`
6. Password: `localpassword` (or whatever you set in `.env.local`)

## CLI

All commands are invoked through `main`:

```bash
uv run python -m main <command> [options]
```

Run `uv run python -m main --help` to see the full list.

### Top-level commands (cross-cutting)

| Command | Description |
| --- | --- |
| `run` | Run the full pipeline: scrape → create new → update dirty products → update dirty variants → dump DB. |
| `status` | Show a summary of items pending sync to Shopify. `--verbose` also shows samples. |
| `sync-products` | Create new products and push dirty ones to Shopify. `--only new` or `--only dirty` to restrict. `--no-draft` creates new products as ACTIVE. |
| `pull-status` | Reconcile local status with Shopify: pull each product's status, mark rows whose product was deleted in Shopify as `DELETED`, and report orphans (Shopify products with vendor Azure Standard but no DB row). `--dry-run` reports only. |
| `set-status <ACTIVE\|DRAFT\|ARCHIVED>` | Set `shopify_status` for one product (`--product-id`) or all products not already in that status, and push to Shopify. `--no-push` only marks rows dirty. |
| `sync-product <id>` | Push a single product (and its variants) to Shopify. |
| `sync-variants` | Push dirty packaging rows to Shopify (price, cost, stock). |
| `sync-handles` | Update Shopify product handles from DB values. |
| `dump-db` | Dump the Postgres database to a timestamped SQL file. |

Common options: `--product-id`, `--packaging-code`, `--max-workers`, `--limit`.

### Azure sub-commands

| Command | Description |
| --- | --- |
| `azure scrape` | Scrape all products from Azure Standard into the DB. |
| `azure upload-remaining-images` | Push any un-uploaded media to Shopify. |

### Typical workflows

**Full sync (recommended for scheduled runs):**
```bash
uv run python -m main run
```

**Just scrape and see what changed:**
```bash
uv run python -m main azure scrape
uv run python -m main status --verbose
```

**Push only new products, in small batches, for a smoke test:**
```bash
uv run python -m main sync-products --only new --limit 5
```

**Check the DB against what is actually in Shopify:**
```bash
uv run python -m main pull-status --dry-run
uv run python -m main pull-status
```

**Publish every draft product:**
```bash
uv run python -m main set-status active
```

**Update prices/stock for a single product:**
```bash
uv run python -m main sync-variants --product-id 12345
```

## How dirty tracking works

Each scrape upserts rows into `azure.products` and `azure.packaging`. A trigger (`azure.set_updated_at_if_changed`) only bumps `updated_at` and records `last_changed_fields` when a tracked column actually changes.

`shopify_status` lives in `azure.products` and is the source of truth for product status: it is sent on every product update, so a status changed by hand in the Shopify admin is overwritten on the next dirty push. Change it with `set-status`, or run `pull-status` to adopt whatever Shopify currently has. `pull-status` does not mark rows dirty, and it overwrites any local status change that has not been pushed yet.

`DELETED` is a local-only status set by `pull-status` when the Shopify product no longer exists. Rows in that state are excluded from every push and from the dirty counts in `status`. To recreate such a product, clear its `shopify_product_id`; it will then be picked up as new.

Sync commands compare `shopify_updated_at` against `updated_at` (and, for variants, against the latest `azure.prices.created_at`) to decide what to push. On success, `shopify_updated_at` is bumped so the row is no longer considered dirty.

This means:
- Re-running `azure scrape` with unchanged data won't dirty anything.
- Re-running `run` after a successful pipeline is a no-op.
- You can safely use `--limit` and re-run to chunk large syncs.

## Collaborate

Request access from `@awarnes` in Slack.

Open pull requests on [GitHub](https://github.com/awarnes/mfc-store-scraper). Please update documentation and tests for anything you change.

We use `pylint` on each PR. Run it locally before pushing:

```bash
uv run pylint .
```

### Running tests
Tests use the [`unittest`](https://docs.python.org/3/library/unittest.html) module.

All tests:
```bash
python -m unittest discover -s tests
```

Unit tests only:
```bash
python -m unittest discover -s tests/unit
```

Integration tests *(under construction)*:
```bash
python -m unittest discover -s tests/integration
