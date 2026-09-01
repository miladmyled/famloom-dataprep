import os
from typing import Optional
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

load_dotenv(override=True)


def get_db_pool(
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
) -> ConnectionPool:
    """
    Creates and configures a psycopg_pool.ConnectionPool for Azure PostgreSQL.
    Configurable min_size and max_size protect against Azure PostgreSQL connection exhaustion.
    Includes check_connection and max_idle settings to discard stale Azure TCP sockets safely.
    """
    pool_min = min_size if min_size is not None else int(os.getenv("DB_POOL_MIN_SIZE", "1"))
    pool_max = max_size if max_size is not None else int(os.getenv("DB_POOL_MAX_SIZE", "5"))

    connection_kwargs = {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "sslmode": "require",
        "row_factory": dict_row,
    }

    pool = ConnectionPool(
        min_size=pool_min,
        max_size=pool_max,
        open=True,
        check=ConnectionPool.check_connection,
        max_idle=300.0,
        max_lifetime=3600.0,
        kwargs=connection_kwargs,
    )

    return pool


if __name__ == "__main__":
    try:
        print("\n--- CONNECTING TO AZURE POSTGRESQL ---")
        pool = get_db_pool()

        with pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database();")
                result = cursor.fetchone()
                print(f"✅ SUCCESS! Securely connected to database: {result['current_database']}")

        pool.close()
        print("🔌 Connection pool closed cleanly.")

    except Exception as e:
        print(f"\n❌ Connection Failed:\n{e}")