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
| `run` | Run the full pipeline: scrape → create new → update dirty products → update dirty variants → push changed stock → pull customers → push membership → pull open orders → dump DB. |
| `status` | Show a summary of items pending sync to Shopify. `--verbose` also shows samples. |
| `sync-products` | Create new products and push dirty ones to Shopify. `--only new` or `--only dirty` to restrict. `--no-draft` creates new products as ACTIVE. |
| `pull-status` | Reconcile local status with Shopify: pull each product's status, mark rows whose product was deleted in Shopify as `DELETED`, and report orphans (Shopify products with vendor Azure Standard but no DB row) and duplicates (orphans whose `internal.id` metafield names an Azure product the DB links to a different Shopify product). `--delete-duplicates` deletes non-ACTIVE duplicates from Shopify; add `--include-active` to delete ACTIVE ones too. `--dry-run` reports only. |
| `set-status <ACTIVE\|DRAFT\|ARCHIVED>` | Set `shopify_status` for one product (`--product-id`) or all products not already in that status, and push to Shopify. `--no-push` only marks rows dirty. |
| `sync-product <id>` | Push a single product (and its variants) to Shopify. |
| `sync-variants` | Push dirty packaging rows to Shopify (price, cost, inventory policy). |
| `sync-stock` | Push changed stock to Shopify, 250 variants per request. |
| `sync-handles` | Update Shopify product handles from DB values. |
| `pull-customers` | Pull customer contact info from Shopify into `azure.customers`. Incremental by default; `--full` fetches everyone. Never touches membership columns. |
| `sync-customers` | Create local-only customers in Shopify and push membership metafields for dirty ones. `--only new` or `--only dirty`; `--customer-id` forces a push. |
| `set-membership <email>` | Set membership columns for a customer by email (`--member-number`, `--status`, `--since`, `--expires`, `--notes`). `--create` adds a local row if none exists. Pushes to Shopify unless `--no-push`. Pass `''` to clear a value. |
| `pull-orders` | Mirror open, unfulfilled Shopify orders and their line items into `azure.orders` / `azure.order_items`, and refresh locally open orders Shopify no longer lists. |
| `purchase-list` | Show outstanding demand per product from open orders. `--by-order` lists per order line. `--commit` records the list as a placed supplier order so it drops off next time. |
| `dump-db` | Dump the Postgres database to a timestamped SQL file. Customer and order rows are excluded unless `--include-customers`. |

Common options: `--product-id`, `--packaging-code`, `--max-workers`, `--limit`.

### Azure sub-commands

| Command | Description |
| --- | --- |
| `azure scrape` | Scrape all products from Azure Standard into the DB. |
| `azure upload-remaining-images` | Push any un-uploaded media to Shopify. |
| `azure quick-order` | Add the Azure purchase list to the azurestandard.com cart through the site's quick-add form, in a visible browser you log in to. `--limit N`, `--dry-run`, `--no-commit`, `--notes`. |

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

**Update prices or stock for a single product:**
```bash
uv run python -m main sync-variants --product-id 12345
uv run python -m main sync-stock --product-id 12345
```

**Buy from Azure to fill open orders:**
```bash
uv run python -m main pull-orders
uv run python -m main purchase-list
uv run python -m main azure quick-order --limit 1   # first time: watch one item go in
uv run python -m main azure quick-order --notes "Azure drop 2026-10-03"
```
Or skip the browser and record the order by hand after placing it: `purchase-list --commit`.

**Record a new member:**
```bash
uv run python -m main set-membership someone@example.com --member-number 0142 --status active --since 2026-09-29
```

## How dirty tracking works

Each scrape upserts rows into `azure.products` and `azure.packaging`. A trigger (`azure.set_updated_at_if_changed`) only bumps `updated_at` and records `last_changed_fields` when a tracked column actually changes. On `azure.packaging`, only `size` is tracked, since it is the only packaging column sent on a variant update; a new `azure.prices` row also makes the variant dirty. `stock` has its own sync, and the remaining columns are never sent to Shopify.

**Stock** is compared directly: a row is stock-dirty when `stock` differs from `shopify_stock`, the last value pushed. `sync-stock` sends dirty rows through `inventorySetOnHandQuantities` in batches of 250 across products, so a full catalog push is under 70 requests. Variants use `inventoryPolicy: DENY`, so an out-of-stock variant cannot be ordered.

A full scrape (no `--limit`) stamps `last_seen_at` on every packaging row it returns and then sets `stock = 0` on rows it did not return, since Azure no longer lists them. Partial scrapes skip this step.

`shopify_status` lives in `azure.products` and is the source of truth for product status: it is sent on every product update, so a status changed by hand in the Shopify admin is overwritten on the next dirty push. Change it with `set-status`, or run `pull-status` to adopt whatever Shopify currently has. `pull-status` does not mark rows dirty, and it overwrites any local status change that has not been pushed yet.

If `sync-products` tries to create a product whose handle already exists in Shopify (typically after a DB rebuild lost the Shopify ids), it adopts the existing product instead: the Shopify product's `internal.id` metafield must equal the Azure product id, then `shopify_product_id` and `shopify_status` are recorded and variants are linked to `azure.packaging` by SKU (`AZ-<code>`). The row is left dirty so the next update pushes the DB state.

`DELETED` is a local-only status set by `pull-status` when the Shopify product no longer exists. Rows in that state are excluded from every push and from the dirty counts in `status`. To recreate such a product, clear its `shopify_product_id`; it will then be picked up as new.

Sync commands compare `shopify_updated_at` against `updated_at` (and, for variants, against the latest `azure.prices.created_at`) to decide what to push. On success, `shopify_updated_at` is bumped so the row is no longer considered dirty.

This means:
- Re-running `azure scrape` with unchanged data won't dirty anything.
- Re-running `run` after a successful pipeline is a no-op.
- You can safely use `--limit` and re-run to chunk large syncs.

## Customers and orders

**Customers** live in `azure.customers` and have split ownership. Shopify owns the contact columns (email, name, phone, default address): `pull-customers` writes them and they are excluded from the dirty trigger, so a pull never marks a row dirty. The DB owns the membership columns (`member_number`, `membership_status`, `member_since`, `membership_expires`); changing any of them marks the row dirty and `sync-customers` pushes them to Shopify as metafields under the `membership` namespace. A status of `active` also adds the `member` tag in Shopify, anything else removes it. `notes` is local only.

A customer created locally (no `shopify_customer_id`) is created in Shopify by `sync-customers`, sending contact columns once. If the same email later shows up in a pull under an existing Shopify customer, the local row is linked to it instead of duplicated. `pull-customers` is incremental: it asks Shopify for customers updated since the newest `remote_updated_at` held locally.

**Orders** are a read-only mirror. `pull-orders` fetches every order Shopify lists as `status:open` and `fulfillment_status:unfulfilled` (which includes partially fulfilled), upserts it with its line items, and links each line item to `azure.packaging` by the `AZ-<code>` SKU. Locally open orders that Shopify no longer lists are re-fetched by id so their fulfillment or cancellation is recorded; orders that no longer exist are marked closed. Nothing is ever pushed to Shopify for orders.

Each pull also stores money fields in shop currency: per order the current subtotal, tax, discounts, shipping, total, and net payment (collected minus refunded); per line item the original unit price, the discounted unit price with order-level discounts allocated, and the discounted line total. Because an order is only re-pulled while it is open, these values are as of the last pull before fulfillment; a refund issued later is not reflected. `azure.order_item_margin` joins line prices to the *current* Azure wholesale price for a per-line margin.

**Purchase list.** `azure.purchase_demand` computes, per open line item, `unfulfilled_quantity` minus what is already on a supplier order. `azure.purchase_list` sums that per packaging code with product name, size, Azure stock, and current wholesale price. Line items whose SKU does not match an Azure packaging row are listed separately as "Not from Azure". `purchase-list --commit` snapshots the current Azure demand into `azure.supplier_orders` / `azure.supplier_order_items`; with a `--supplier` other than `azure` it snapshots the unlinked items instead. If an order is later cancelled or fulfilled from stock, its committed quantity is simply no longer counted.

**Quick order.** `azure quick-order` needs a one-time `uv run playwright install chromium`. It opens a real Chromium window with a profile stored in `.playwright-profile/` (gitignored), so you log in to azurestandard.com by hand the first time and stay logged in afterwards; the tool never stores your Azure password. It opens the cart drawer, and for each item on the purchase list types the packaging code and quantity into the quick-add form and clicks Quick-Add. An item counts as failed when error text appears in the cart drawer; those stay on the purchase list. When all items are done it records a supplier order for the items that went in (unless `--no-commit`), then leaves the browser open on the cart for you to review and check out. The tool never checks out. Success detection is heuristic, so use `--limit 1` the first time and compare the cart with the log.

**Shopify setup.** The app needs the `read_orders`, `read_customers`, and `write_customers` scopes and protected customer data access enabled in the Shopify dev dashboard. Regenerate the token after changing scopes; `check-connection` reports any missing scope. `read_orders` only returns the last 60 days of orders, which is enough for open orders.

**Personal data.** `dump-db` excludes the rows of `azure.customers`, `azure.orders`, and `azure.order_items` by default so dump files carry no personal data. Pass `--include-customers` to include them.

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
