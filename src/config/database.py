import os
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row

load_dotenv(override=True)

def get_db_pool():
    connection_kwargs = {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "sslmode": "require",
        "row_factory": dict_row
    }
    
    pool = ConnectionPool(
        min_size=1,
        max_size=5,
        open=True,
        kwargs=connection_kwargs
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
                
        # THE FIX: Explicitly close the pool so the background threads shut down cleanly
        pool.close()
        print("🔌 Connection pool closed cleanly.")
                
    except Exception as e:
        print(f"\n❌ Connection Failed:\n{e}")