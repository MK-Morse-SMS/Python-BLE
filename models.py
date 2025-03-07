from pydantic import BaseModel, Field
from typing import List

class BatchConnectRequest(BaseModel):
    mac_addresses: List[str] = Field(..., description="List of device MAC addresses")

class StartScanRequest(BaseModel):
    service_uuids: List[str] = Field([], description="Service UUIDs to filter the scan")
