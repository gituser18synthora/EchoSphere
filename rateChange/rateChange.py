import os
import sys
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone

import requests
import pymysql


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

print(os.getenv("MYSQL_HOST"))
exit("not set")
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST"),
    "port": int(os.getenv("MYSQL_PORT")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "autocommit": False,
}

BASE_CODE = "USD"
TARGET_CODE = "INR"

FRANKFURTER_URL = (
    f"https://api.frankfurter.dev/v2/rate/{BASE_CODE}/{TARGET_CODE}"
)


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Fetch exchange rate
# ---------------------------------------------------------

def get_usd_to_inr() -> Decimal:
    """
    Fetch the latest USD -> INR exchange rate from Frankfurter.
    """

    response = requests.get(
        FRANKFURTER_URL,
        timeout=10,
        headers={
            "User-Agent": "exchange-rate-cron/1.0"
        },
    )

    response.raise_for_status()

    data = response.json()

    if "rate" not in data:
        raise ValueError(
            f"Rate not found in API response: {data}"
        )

    try:
        rate = Decimal(str(data["rate"]))
    except (InvalidOperation, TypeError):
        raise ValueError(
            f"Invalid exchange rate received: {data.get('rate')}"
        )

    if rate <= 0:
        raise ValueError(
            f"Invalid exchange rate received: {rate}"
        )

    return rate


# ---------------------------------------------------------
# Update database
# ---------------------------------------------------------

def update_exchange_rate(rate: Decimal) -> None:
    connection = None

    try:
        connection = pymysql.connect(**DB_CONFIG)

        with connection.cursor() as cursor:

            # Lock and find the currently active USD -> INR rate.
            select_sql = """
                SELECT
                    id,
                    rate,
                    source,
                    effective_from
                FROM exchange_rates
                WHERE id = %s
                  AND status = 'active'
                  AND is_deleted = 0
                ORDER BY sort_order ASC, updated_at DESC
                LIMIT 1
                FOR UPDATE
            """

            cursor.execute(
                select_sql,
                ('fxr_a368d3c1221a'),
            )

            existing = cursor.fetchone()

            if not existing:
                raise RuntimeError(
                    f"No active exchange rate found for "
                    f"{BASE_CODE}/{TARGET_CODE}"
                )

            old_rate = Decimal(str(existing["rate"]))

            now = datetime.now(timezone.utc).replace(tzinfo=None)

            update_sql = """
                UPDATE exchange_rates
                SET
                    rate = %s,
                    effective_from = %s,
                    source = %s,
                    updated_at = %s
                WHERE id = %s
                  AND is_deleted = 0
            """

            cursor.execute(
                update_sql,
                (
                    rate,
                    now,
                    "frankfurter",
                    now,
                    existing["id"],
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Exchange rate update failed. "
                    f"Updated rows: {cursor.rowcount}"
                )

        connection.commit()

        logger.info(
            "USD/INR exchange rate updated: %s -> %s",
            old_rate,
            rate,
        )

    except Exception:
        if connection:
            connection.rollback()

        raise

    finally:
        if connection:
            connection.close()


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    try:
        logger.info("Fetching latest USD/INR exchange rate...")

        rate = get_usd_to_inr()

        logger.info(
            "Latest USD/INR rate received: %s",
            rate,
        )

        update_exchange_rate(rate)

        logger.info("Exchange rate update completed successfully.")

    except requests.RequestException as exc:
        logger.exception(
            "Failed to fetch exchange rate: %s",
            exc,
        )
        sys.exit(1)

    except Exception as exc:
        logger.exception(
            "Exchange rate update failed: %s",
            exc,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()