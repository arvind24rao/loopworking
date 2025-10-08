import os
from contextlib import contextmanager
from pathlib import Path

import psycopg  # psycopg v3
from dotenv import load_dotenv

# Load env from project root (.env.dev lives in /Users/arvindrao/loop)
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")

DATABASE_URL = os.getenv("DATABASE_URL")

@contextmanager
def pg_conn():
    # DATABASE_URL should include ?sslmode=require
    with psycopg.connect(DATABASE_URL) as conn:
        yield conn  # auto-closes on exit

def ping_db():
    with pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            cur.fetchone()
