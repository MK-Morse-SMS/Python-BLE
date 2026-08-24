import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from ble_manager import BLEManager
from routes import router

# Configure logging. LOG_LEVEL is set by hand from the Balena dashboard, so an
# unrecognised value falls back to INFO rather than stopping the service booting.
_level = logging.getLevelNamesMapping().get(
    os.getenv("LOG_LEVEL", "INFO").strip().upper(), logging.INFO
)
logging.basicConfig(level=_level)
# os.environ["BLEAK_LOGGING"] = "1"
# Set the logging level for bleak to DEBUG for detailed output
# logging.getLogger("bleak").setLevel(logging.DEBUG)

# --- BLE Manager Implementation --

# Create a single instance of BLEManager.
ble_manager = BLEManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize BLE manager during startup
    await ble_manager.initialize()
    yield
    await ble_manager.disconnect_all()


# Create the FastAPI application
app = FastAPI(lifespan=lifespan)

# Include the router
app.include_router(router)

# To run the service, use:
# uvicorn main:app --host 0.0.0.0 --port 8000
if __name__ == "__main__":
    import uvicorn

    # One access-log line per GATT op from the local backend; off by default.
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        access_log=os.getenv("ACCESS_LOG", "").lower() in ("1", "true", "yes"),
    )
