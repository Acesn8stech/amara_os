"""Create the schema in DATABASE_URL (SQLite or Postgres/Supabase). Idempotent."""
from agents.base_worker import Settings, load_dotenv
from db.store import Store

if __name__ == "__main__":
    load_dotenv()
    s = Store(Settings.from_env().db_url)
    s.init_schema()
    print("schema ready:", s.url.split("@")[-1])
