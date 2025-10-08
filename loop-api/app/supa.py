import os
from typing import Any, Dict, Optional

import httpx
from dotenv import load_dotenv
from pathlib import Path

# Load env from project root
ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env.dev")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY in environment")

class Supa:
    def __init__(self, base_url: str, service_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self.client = httpx.Client(timeout=20.0, headers=self.headers)

    def rpc(self, func: str, args: Dict[str, Any]) -> Any:
        r = self.client.post(f"{self.base}/rest/v1/rpc/{func}", json=args)
        r.raise_for_status()
        return r.json()

    def select_one(self, table: str, eq: Dict[str, str], select: str = "*") -> Optional[Dict[str, Any]]:
        params = {"select": select}
        for k, v in eq.items():
            params[k] = f"eq.{v}"
        r = self.client.get(f"{self.base}/rest/v1/{table}", params=params)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None

    def insert(self, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        r = self.client.post(f"{self.base}/rest/v1/{table}", json=row)
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else data

    def select_many(self, table: str, filters: Dict[str, str], select: str = "*", order: Optional[str] = None, limit: Optional[int] = None) -> Any:
        """
        filters: {"thread_id": "<uuid>", "visibility": "shared"} -> thread_id=eq.<uuid>&visibility=eq.shared
        order: "created_at.asc" or "created_at.desc"
        """
        params = {"select": select}
        for k, v in filters.items():
            params[k] = f"eq.{v}"
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        r = self.client.get(f"{self.base}/rest/v1/{table}", params=params)
        r.raise_for_status()
        return r.json()

supa = Supa(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
