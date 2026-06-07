import requests
import time
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.prod.paradex.trade/v1"
POSITIONS_URL = f"{API_BASE_URL}/positions"
FILLS_URL = f"{API_BASE_URL}/fills"
ORDERS_HISTORY_URL = f"{API_BASE_URL}/orders-history"
TRANSFERS_URL = f"{API_BASE_URL}/transfers"
SYSTEM_STATE_URL = f"{API_BASE_URL}/system/state"

class ParadexClient:
    def __init__(self, jwt_token: str):
        self.jwt_token = jwt_token
        self.headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/json"
        }

    def get_open_positions(self) -> Optional[List[Dict]]:
        """
        Fetches open positions from Paradex API.
        Returns a list of position dictionaries if successful, None otherwise.
        """
        max_retries = 3
        backoff_factor = 1
        
        for attempt in range(max_retries + 1):
            try:
                response = requests.get(
                    POSITIONS_URL, 
                    headers=self.headers, 
                    timeout=10
                )
                response.raise_for_status()
                data = response.json()
                
                # Filter for OPEN positions
                results = data.get("results", [])
                open_positions = [
                    p for p in results 
                    if p.get("status") == "OPEN"
                ]
                return open_positions

            except requests.exceptions.RequestException as e:
                logger.error(f"Attempt {attempt + 1}/{max_retries + 1} failed: {e}")
                if attempt < max_retries:
                    sleep_time = backoff_factor * (2 ** attempt)
                    logger.info(f"Retrying in {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    logger.error("Max retries reached. Skipping this cycle.")
                    return None
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return None
        
        return None

    def get_fills(self, start_at: Optional[int] = None, page_size: int = 5000) -> Optional[List[Dict]]:
        """Fetch recent fills, optionally from a millisecond timestamp."""
        params = {"page_size": page_size}
        if start_at is not None:
            params["start_at"] = start_at

        try:
            response = requests.get(
                FILLS_URL,
                headers=self.headers,
                params=params,
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch fills: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while fetching fills: {e}")
            return None

    def get_order_history(self, client_id: Optional[str] = None, start_at: Optional[int] = None, page_size: int = 100) -> Optional[List[Dict]]:
        """Fetch order history, optionally filtered by client_id."""
        params = {"page_size": page_size}
        if client_id is not None:
            params["client_id"] = client_id
        if start_at is not None:
            params["start_at"] = start_at

        try:
            response = requests.get(
                ORDERS_HISTORY_URL,
                headers=self.headers,
                params=params,
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch order history: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while fetching order history: {e}")
            return None

    def get_order_by_client_id(self, client_id: str) -> Optional[Dict]:
        """Fetch the latest history record for an exact client_id."""
        orders = self.get_order_history(client_id=client_id, page_size=20)
        if not orders:
            return None

        exact = [o for o in orders if o.get("client_id") == client_id]
        if not exact:
            return None
        return sorted(exact, key=lambda o: int(o.get("created_at") or 0))[-1]

    def get_transfers(self, page_size: int = 20) -> Optional[List[Dict]]:
        """Fetch recent account transfers for stablecoin operation notices."""
        try:
            response = requests.get(
                TRANSFERS_URL,
                headers=self.headers,
                params={"page_size": page_size},
                timeout=20
            )
            response.raise_for_status()
            data = response.json()
            return data.get("results", [])
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch transfers: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while fetching transfers: {e}")
            return None

    def get_system_state(self) -> Optional[str]:
        """Fetch Paradex system status: ok, maintenance, or cancel_only."""
        try:
            response = requests.get(
                SYSTEM_STATE_URL,
                headers=self.headers,
                timeout=10
            )
            response.raise_for_status()
            data = response.json()
            status = data.get("status")
            return str(status).lower() if status else None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch system state: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error while fetching system state: {e}")
            return None
