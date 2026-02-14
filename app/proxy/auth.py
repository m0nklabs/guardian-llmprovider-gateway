import json
import secrets
import time
import logging
from pathlib import Path
from typing import Dict, Optional
from fastapi import HTTPException, Security, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.status import HTTP_401_UNAUTHORIZED

logger = logging.getLogger("Auth")

API_KEYS_FILE = Path(__file__).parent.parent.parent / "config" / "api_keys.json"
security_scheme = HTTPBearer()

def load_api_keys() -> Dict[str, dict]:
    if not API_KEYS_FILE.exists():
        return {}
    try:
        with open(API_KEYS_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load API keys: {e}")
        return {}

def save_api_keys(keys: Dict[str, dict]):
    API_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(API_KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2)

def generate_api_key(name: str, metadata: dict = None) -> str:
    """Generate a new API key with 'flip_' prefix."""
    prefix = "flip_"
    random_part = secrets.token_hex(16)
    api_key = f"{prefix}{random_part}"
    
    keys = load_api_keys()
    keys[api_key] = {
        "name": name,
        "created_at": time.time(),
        "metadata": metadata or {}
    }
    save_api_keys(keys)
    logger.info(f"Generated new API key for '{name}'")
    return api_key

async def verify_api_key(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Security(security_scheme)):
    """
    Verify API key from Bearer token.
    Returns the metadata associated with the key (including name).
    """
    if not creds:
         raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API Key required",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    token = creds.credentials
    if not token.startswith("flip_"):
        # Allow non-prefixed keys if they exist in file (backward compat or manual keys)
        pass

    keys = load_api_keys()
    if token in keys:
        user_data = keys[token]
        # Attach user info to request state for logging
        request.state.user = user_data
        logger.info(f"🔑 Auth success: {user_data.get('name', 'Unknown')}")
        return user_data["name"]  # Return client_id/name as expected by endpoints

    logger.warning(f"❌ Invalid API key attempt: {token[:10]}...")
    raise HTTPException(
        status_code=HTTP_401_UNAUTHORIZED,
        detail="Invalid API Key",
        headers={"WWW-Authenticate": "Bearer"},
    )

if __name__ == "__main__":
    # Helper CLI to generate key
    import sys
    if len(sys.argv) > 1:
        name = sys.argv[1]
        key = generate_api_key(name)
        print(f"Generated API Key for {name}: {key}")
    else:
        print("Usage: python3 -m app.proxy.auth <name>")
