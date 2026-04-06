"""Thin entrypoint for local development.
Reads host/port from environment and starts Uvicorn.
"""
import os
import uvicorn
from dotenv import load_dotenv

if __name__ == "__main__":
    # Load .env for host/port only; app config loads in app.config
    try:
        load_dotenv()
    except Exception:
        pass
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    trusted_proxy = os.getenv("TRUSTED_PROXY", None)
    uvicorn.run("app.app:app", host=host, port=port, reload=True, proxy_headers=True, forwarded_allow_ips=trusted_proxy)
