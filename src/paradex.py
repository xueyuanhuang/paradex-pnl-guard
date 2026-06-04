import requests
import time
import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.prod.paradex.trade/v1"
POSITIONS_URL = f"{API_BASE_URL}/positions"
FILLS_URL = f"{API_BASE_URL}/fills"

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
