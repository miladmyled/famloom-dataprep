from src.config.database import get_db_pool

def get_active_cities():
    """
    Fetches the distinct list of active cities from the family_profiles table.
    """
    pool = get_db_pool()
    cities = []
    
    try:
        with pool.connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT DISTINCT location FROM family_profiles WHERE location IS NOT NULL;")
                records = cursor.fetchall()
                
                for row in records:
                    cities.append(row["location"])
                    
        return cities
    except Exception as e:
        print(f"Extraction Error: {e}")
        return []
    finally:
        pool.close()

if __name__ == "__main__":
    print("Fetching active cities from Azure...")
    active_cities = get_active_cities()
    print(f"Cities found: {active_cities}")