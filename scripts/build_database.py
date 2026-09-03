"""Build the NorthStar demo database.

Deterministic: a fixed RNG seed means every developer gets byte-comparable
data, so evaluation answers are stable and the repo is reproducible.

The output location comes from the project's own settings
(`AGENTCREW_DATABASE_PATH`, default `data/northstar.db`), so this script writes
wherever the application reads. Tests use that to build into a temp directory
instead of destroying the shared database.

The data is not uniform noise. Two real signals are planted so that analytical
questions have genuine, discoverable answers rather than coin flips:

  * APAC suffers a churn spike in Q3 2025 (a support-quality regression that
    shows up in `support_tickets` first).
  * Overall revenue declines Q3 vs Q2 2025, driven mostly by the Hardware
    category and by APAC, and preceded by a marketing-spend cut.

That means "why did revenue decrease in Q3?" has a defensible answer that
requires joining three tables - which is exactly the behaviour we want to
demonstrate and evaluate.
"""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agentcrew.config import get_settings  # noqa: E402
from agentcrew.db.engine import writable_engine  # noqa: E402

SEED = 20260215

DDL = """
DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS support_tickets;
DROP TABLE IF EXISTS marketing_spend;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS regions;

CREATE TABLE regions (
    region_id   INTEGER PRIMARY KEY,
    region_name TEXT    NOT NULL,
    country     TEXT    NOT NULL
);

CREATE TABLE products (
    product_id   INTEGER PRIMARY KEY,
    product_name TEXT    NOT NULL,
    category     TEXT    NOT NULL,
    unit_cost    REAL    NOT NULL,
    list_price   REAL    NOT NULL
);

CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    customer_name TEXT    NOT NULL,
    region_id     INTEGER NOT NULL REFERENCES regions(region_id),
    segment       TEXT    NOT NULL,
    signup_date   TEXT    NOT NULL,
    churn_date    TEXT
);

CREATE TABLE orders (
    order_id    INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    channel     TEXT    NOT NULL
);

CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY,
    order_id      INTEGER NOT NULL REFERENCES orders(order_id),
    product_id    INTEGER NOT NULL REFERENCES products(product_id),
    quantity      INTEGER NOT NULL,
    unit_price    REAL    NOT NULL,
    discount      REAL    NOT NULL DEFAULT 0.0
);

CREATE TABLE support_tickets (
    ticket_id     INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    created_date  TEXT    NOT NULL,
    severity      TEXT    NOT NULL,
    resolved_date TEXT
);

CREATE TABLE marketing_spend (
    spend_id    INTEGER PRIMARY KEY,
    region_id   INTEGER NOT NULL REFERENCES regions(region_id),
    month       TEXT    NOT NULL,
    channel     TEXT    NOT NULL,
    amount      REAL    NOT NULL
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_customers_region ON customers(region_id);
"""

REGIONS = [
    (1, "North America", "United States"),
    (2, "EMEA", "Germany"),
    (3, "APAC", "Singapore"),
    (4, "LATAM", "Brazil"),
]

CATEGORIES = {
    "Hardware": (180.0, 900.0),
    "Software": (20.0, 240.0),
    "Accessories": (5.0, 70.0),
    "Services": (40.0, 400.0),
}

SEGMENTS = ["Enterprise", "Mid-Market", "SMB"]
CHANNELS = ["Direct", "Partner", "Online"]
STATUSES = ["completed", "completed", "completed", "completed", "refunded", "cancelled"]
SEVERITIES = ["low", "medium", "high", "critical"]

FIRST_NAMES = """Aarav Mia Liam Sofia Noah Emma Kai Yuki Chen Priya Omar Lena Diego
Anna Hugo Sara Nils Zara Tomas Ines Ravi Elena Marco Nadia Felix Amara Jonas Rina
Pablo Freya Idris Clara""".split()
LAST_NAMES = """Sharma Muller Silva Tan Kim Novak Rossi Dubois Weber Okafor Haddad
Lindqvist Costa Nakamura Ivanov Fischer Moreau Santos Yilmaz Bakker Popescu Reyes
Andersen Kowalski Mensah Duarte Larsen Farah Bianchi Vargas""".split()

PRODUCT_WORDS = """Atlas Vertex Nimbus Quartz Beacon Harbor Lumen Pivot Cobalt Onyx
Summit Cascade Meridian Anchor Prism Forge Delta Ridge Halo Lattice""".split()


def daterange_days(start: date, end: date) -> int:
    return (end - start).days


def q_of(d: date) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def main() -> None:
    rng = random.Random(SEED)
    db_path = get_settings().database_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    engine = writable_engine(db_path)
    with engine.begin() as conn:
        for stmt in DDL.strip().split(";"):
            if stmt.strip():
                conn.execute(text(stmt))

        conn.execute(
            text(
                "INSERT INTO regions (region_id, region_name, country) "
                "VALUES (:i, :n, :c)"
            ),
            [{"i": i, "n": n, "c": c} for i, n, c in REGIONS],
        )

        # ---- products ----------------------------------------------------
        products = []
        pid = 1
        for cat, (lo, hi) in CATEGORIES.items():
            for _ in range(9):
                cost = round(rng.uniform(lo, hi), 2)
                price = round(cost * rng.uniform(1.35, 2.1), 2)
                name = f"{rng.choice(PRODUCT_WORDS)} {rng.choice(PRODUCT_WORDS)}"
                products.append(
                    {
                        "product_id": pid,
                        "product_name": name,
                        "category": cat,
                        "unit_cost": cost,
                        "list_price": price,
                    }
                )
                pid += 1
        conn.execute(
            text(
                "INSERT INTO products (product_id, product_name, category, unit_cost,"
                " list_price) VALUES (:product_id,:product_name,:category,:unit_cost,"
                ":list_price)"
            ),
            products,
        )

        # ---- customers ---------------------------------------------------
        start_signup = date(2023, 1, 1)
        span = daterange_days(start_signup, date(2025, 9, 1))
        customers = []
        for cid in range(1, 1401):
            region_id = rng.choices([1, 2, 3, 4], weights=[38, 27, 22, 13])[0]
            signup = start_signup + timedelta(days=rng.randint(0, span))
            churn: str | None = None

            # Baseline churn, plus a planted APAC spike in Q3 2025.
            base_p = 0.11
            if region_id == 3:
                base_p = 0.26
            if rng.random() < base_p:
                if region_id == 3 and rng.random() < 0.62:
                    churn_d = date(2025, 7, 1) + timedelta(days=rng.randint(0, 91))
                else:
                    earliest = max(signup + timedelta(days=45), date(2023, 6, 1))
                    latest = date(2025, 12, 20)
                    if earliest < latest:
                        churn_d = earliest + timedelta(
                            days=rng.randint(0, daterange_days(earliest, latest))
                        )
                    else:
                        churn_d = latest
                if churn_d > signup:
                    churn = churn_d.isoformat()

            customers.append(
                {
                    "customer_id": cid,
                    "customer_name": (
                        f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
                    ),
                    "region_id": region_id,
                    "segment": rng.choices(SEGMENTS, weights=[22, 38, 40])[0],
                    "signup_date": signup.isoformat(),
                    "churn_date": churn,
                }
            )
        conn.execute(
            text(
                "INSERT INTO customers (customer_id, customer_name, region_id, segment,"
                " signup_date, churn_date) VALUES (:customer_id,:customer_name,"
                ":region_id,:segment,:signup_date,:churn_date)"
            ),
            customers,
        )

        # ---- orders + items ----------------------------------------------
        by_cat: dict[str, list[dict]] = {}
        for p in products:
            by_cat.setdefault(p["category"], []).append(p)

        orders: list[dict] = []
        items: list[dict] = []
        oid = 1
        iid = 1
        period_start = date(2024, 1, 1)
        period_end = date(2025, 12, 31)

        for cust in customers:
            signup = date.fromisoformat(cust["signup_date"])
            churn = (
                date.fromisoformat(cust["churn_date"]) if cust["churn_date"] else None
            )
            active_from = max(signup, period_start)
            active_to = min(churn or period_end, period_end)
            if active_from >= active_to:
                continue

            months_active = max(1, (active_to - active_from).days // 30)
            n_orders = max(0, int(rng.gauss(months_active * 0.55, 2)))

            for _ in range(n_orders):
                od = active_from + timedelta(
                    days=rng.randint(0, daterange_days(active_from, active_to))
                )
                quarter = q_of(od)

                # Planted signal: Q3-2025 demand dip, sharper in APAC.
                if quarter == "2025-Q3":
                    drop = 0.30 if cust["region_id"] == 3 else 0.13
                    if rng.random() < drop:
                        continue

                orders.append(
                    {
                        "order_id": oid,
                        "customer_id": cust["customer_id"],
                        "order_date": od.isoformat(),
                        "status": rng.choice(STATUSES),
                        "channel": rng.choices(CHANNELS, weights=[30, 25, 45])[0],
                    }
                )

                for _ in range(rng.randint(1, 4)):
                    # Hardware share shrinks in Q3-2025 (the real driver).
                    weights = (
                        [10, 34, 30, 26]
                        if quarter == "2025-Q3"
                        else [30, 27, 24, 19]
                    )
                    cat = rng.choices(list(CATEGORIES), weights=weights)[0]
                    prod = rng.choice(by_cat[cat])
                    disc = rng.choice([0.0, 0.0, 0.0, 0.05, 0.1, 0.15])
                    items.append(
                        {
                            "order_item_id": iid,
                            "order_id": oid,
                            "product_id": prod["product_id"],
                            "quantity": rng.randint(1, 5),
                            "unit_price": prod["list_price"],
                            "discount": disc,
                        }
                    )
                    iid += 1
                oid += 1

        for chunk_start in range(0, len(orders), 5000):
            conn.execute(
                text(
                    "INSERT INTO orders (order_id, customer_id, order_date, status,"
                    " channel) VALUES (:order_id,:customer_id,:order_date,:status,"
                    ":channel)"
                ),
                orders[chunk_start : chunk_start + 5000],
            )
        for chunk_start in range(0, len(items), 5000):
            conn.execute(
                text(
                    "INSERT INTO order_items (order_item_id, order_id, product_id,"
                    " quantity, unit_price, discount) VALUES (:order_item_id,:order_id,"
                    ":product_id,:quantity,:unit_price,:discount)"
                ),
                items[chunk_start : chunk_start + 5000],
            )

        # ---- support tickets (leading indicator for APAC churn) ----------
        tickets = []
        tid = 1
        for cust in customers:
            signup = date.fromisoformat(cust["signup_date"])
            base = 1.1 if cust["region_id"] == 3 else 0.7
            for _ in range(max(0, int(rng.gauss(base * 3, 1.6)))):
                lo = max(signup, date(2024, 1, 1))
                if lo >= period_end:
                    continue
                cd = lo + timedelta(days=rng.randint(0, daterange_days(lo, period_end)))
                if cust["region_id"] == 3 and rng.random() < 0.45:
                    cd = date(2025, 6, 1) + timedelta(days=rng.randint(0, 100))
                sev = (
                    rng.choices(SEVERITIES, weights=[20, 30, 32, 18])[0]
                    if cust["region_id"] == 3
                    else rng.choices(SEVERITIES, weights=[42, 33, 18, 7])[0]
                )
                resolved = None
                if rng.random() < 0.82:
                    resolved = (cd + timedelta(days=rng.randint(1, 21))).isoformat()
                tickets.append(
                    {
                        "ticket_id": tid,
                        "customer_id": cust["customer_id"],
                        "created_date": cd.isoformat(),
                        "severity": sev,
                        "resolved_date": resolved,
                    }
                )
                tid += 1
        for chunk_start in range(0, len(tickets), 5000):
            conn.execute(
                text(
                    "INSERT INTO support_tickets (ticket_id, customer_id, created_date,"
                    " severity, resolved_date) VALUES (:ticket_id,:customer_id,"
                    ":created_date,:severity,:resolved_date)"
                ),
                tickets[chunk_start : chunk_start + 5000],
            )

        # ---- marketing spend (cut precedes the Q3 dip) -------------------
        spends = []
        sid = 1
        for year in (2024, 2025):
            for month in range(1, 13):
                for region_id, *_ in REGIONS:
                    for channel in CHANNELS:
                        amount = rng.uniform(8000, 26000)
                        if year == 2025 and month in (6, 7, 8):
                            amount *= 0.55 if region_id == 3 else 0.8
                        spends.append(
                            {
                                "spend_id": sid,
                                "region_id": region_id,
                                "month": f"{year}-{month:02d}",
                                "channel": channel,
                                "amount": round(amount, 2),
                            }
                        )
                        sid += 1
        conn.execute(
            text(
                "INSERT INTO marketing_spend (spend_id, region_id, month, channel,"
                " amount) VALUES (:spend_id,:region_id,:month,:channel,:amount)"
            ),
            spends,
        )

    with engine.connect() as conn:
        print(f"Built {db_path}")
        for t in (
            "regions",
            "products",
            "customers",
            "orders",
            "order_items",
            "support_tickets",
            "marketing_spend",
        ):
            n = conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()  # noqa: S608
            print(f"  {t:18} {n:>8,}")


if __name__ == "__main__":
    main()
