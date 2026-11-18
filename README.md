# Python BLE

A Python-based Bluetooth Low Energy (BLE) management service that exposes a REST API for scanning, connecting to, and interacting with BLE devices. Built with [FastAPI](https://fastapi.tiangolo.com/) and [Bleak](https://bleak.readthedocs.io/), it runs on Linux using BlueZ over D-Bus.

## Features

- **BLE Scanning** — Scan for nearby BLE devices with optional service UUID filtering
- **Connection Management** — Batch-connect to devices by MAC address; dropped sensors are reconnected with backoff, and a wedged BlueZ stack is recovered (see [Connection recovery](#connection-recovery))
- **GATT Operations** — Read and write GATT characteristics by UUID
- **Notifications** — Enable or disable BLE characteristic notifications
- **MTU Negotiation** — Query the negotiated MTU size for any connected device
- **Server-Sent Events (SSE)** — Real-time streaming of scan results, connection events, and notifications to HTTP clients
- **Health** — `/health` reports adapter, scanner and per-sensor connection state
- **Docker Support** — Containerised deployment with host BlueZ D-Bus socket pass-through

## Architecture

```
main.py               FastAPI application entry point & lifespan management
├── ble_manager.py    High-level facade composing scanner and connection manager
├── ble_scanner.py    BLE scanning loop (Bleak BleakScanner)
├── ble_connections.py  GATT connection management (Bleak BleakClient)
├── event_broadcaster.py  Async per-client queue for SSE fanout
├── dbus_utils.py     BlueZ helpers: startup disconnect, RemoveDevice, adapter power, state lookups
├── routes.py         FastAPI router defining all HTTP endpoints
└── models.py         Pydantic request models
```

## Requirements

- Python 3.12+
- Linux with BlueZ installed (`bluez` package)
- D-Bus system bus access (for BlueZ)

Python dependencies (see `requirements.txt`):

| Package | Version |
|---------|---------|
| fastapi | 0.129.0 |
| uvicorn | 0.41.0 |
| pydantic | 2.12.5 |
| bleak | 2.1.1 |
| dbus-fast | 4.0.0 |

## Installation

```bash
# Install system dependency
sudo apt-get install bluez

# Install Python dependencies
pip install -r requirements.txt
```

## Running the Service

```bash
python main.py
# or
uvicorn main:app --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`.

Interactive API documentation (Swagger UI) is available at `http://localhost:8000/docs`.

## Docker

Build and run the container, mounting the host BlueZ D-Bus socket so the container can control Bluetooth:

```bash
docker build -t python-ble .

docker run --rm \
  -p 8000:8000 \
  -v /run/dbus:/host/run/dbus \
  python-ble
```

> **Note:** The `DBUS_SYSTEM_BUS_ADDRESS` environment variable inside the container is pre-configured to `unix:path=/host/run/dbus/system_bus_socket`.

## API Reference

All endpoints are served under the base URL of the running service (e.g. `http://localhost:8000`).

### Scanning

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/gap/start-scan` | Start BLE scanning, optionally filtering by service UUIDs |
| `DELETE` | `/gap/stop-scan` | Stop active BLE scanning |

**Start scan request body** (optional):
```json
{
  "service_uuids": ["0000180d-0000-1000-8000-00805f9b34fb"]
}
```

### Connection Management

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/gap/batch-connect` | Add one or more devices to the connection list |
| `GET` | `/gap/nodes` | List all currently connected devices |
| `DELETE` | `/gap/nodes/{mac}` | Disconnect a specific device |

**Batch connect request body**:
```json
{
  "mac_addresses": ["DE:6D:5D:2A:BD:58"]
}
```

### GATT Operations

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/gatt/nodes/{mac}/characteristics` | List all GATT characteristics on a connected device |
| `GET` | `/gatt/nodes/{mac}/characteristic/{char_uuid}` | Read a characteristic value (returns hex string) |
| `GET` | `/gatt/nodes/{mac}/characteristic/{char_uuid}/value/{value}` | Write a hex-encoded value to a characteristic |
| `GET` | `/gatt/nodes/{mac}/mtu` | Get the negotiated MTU size for a connected device |

### Notifications

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/gatt/nodes/{mac}/characteristic/{char_uuid}` | Enable notifications for a characteristic |
| `DELETE` | `/gatt/nodes/{mac}/characteristic/{char_uuid}` | Disable notifications for a characteristic |
| `DELETE` | `/gatt/nodes/notifications` | Disable all notifications on all connected devices |

### Health

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Adapter power/discovery, scanner session, SSE backlog, adapter resets, and per-sensor counters (attempts, failures by kind, drops, last seen, RSSI, next attempt) |

### Events (SSE)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/events` | Server-Sent Events stream: scan results, connection events, and notifications |

**Event types:**

```jsonc
// Scan result
{ "type": "scan", "results": [{ "bdaddr": "DE:6D:5D:2A:BD:58", "name": "MyDevice", "rssi": -65 }] }

// Connection status change
{ "type": "connection", "bdaddr": "DE:6D:5D:2A:BD:58", "status": "connected" }

// Characteristic notification
{ "type": "notification", "bdaddr": "DE:6D:5D:2A:BD:58", "characteristic": "<uuid>", "data": "0a1b2c" }
```

## Connection recovery

One supervisor task in `ble_connections.py` makes every connect attempt and every recovery step, one at a time. It shares a lock with the scanner's session restarts, so a connect never runs while discovery is off. (Kernel 6.x BlueZ can't connect without an active scan.)

- **Sensor not in BlueZ** (`device '...' not found`): not a failure. The supervisor waits for the sensor to advertise again. If it stays missing, the scan session is restarted at 60 s, 120 s and 240 s.
- **Failed connects** (timeouts, errors, connections that drop within 15 s): exponential backoff per sensor. Every 3rd failure in a row removes the sensor from BlueZ and restarts the scan; every 6th backs it off for 5 minutes.
- **Adapter power-cycle**: only when no sensor has connected for 10 minutes despite at least 6 failed connects, and at most every 15 minutes. One failing sensor never resets the adapter while another is connected. After 2 resets with no connection in between, the process exits so the container restarts.
- **Scan that won't start**: blocks every connect, so the rule above never trips. After 2 minutes of failed starts the adapter is power-cycled whenever a wanted sensor is disconnected, even if others are connected, at most every 15 minutes, and the process never exits. This is the state after an Intel controller crash (`Hardware error 0x0c` in dmesg): the kernel refuses discovery, BlueZ reports `org.bluez.Error.InProgress`, and a container restart doesn't clear it.
- **Scanner**: restarts discovery after every adapter reset, every 5 minutes, and whenever a watchdog sees BlueZ stop discovering. It powers the adapter back on if something left it off.

The thresholds are module constants at the top of `ble_connections.py` and `ble_scanner.py`.

## Tests

```bash
python -m unittest discover -s tests
```

The tests replace BlueZ and bleak with fakes (`tests/fakes.py`), so they need no Bluetooth hardware.

## API Collection (Bruno)

The `API/` directory contains a [Bruno](https://www.usebruno.com/) collection with pre-built requests for all endpoints. Import the folder into Bruno and set the `URL` environment variable to your service address (e.g. `http://localhost:8000`).

## Project Structure

```
Python-BLE/
├── API/                    Bruno API collection
│   ├── environments/       Bruno environment configs
│   ├── *.bru               Individual request definitions
│   └── bruno.json          Collection metadata
├── ble_connections.py      BLE connection and GATT management
├── ble_manager.py          High-level BLE manager (façade)
├── ble_scanner.py          BLE scanning logic
├── dbus_utils.py           D-Bus BlueZ utilities
├── event_broadcaster.py    Async SSE event fanout
├── main.py                 FastAPI app entry point
├── models.py               Pydantic request models
├── routes.py               HTTP route definitions
├── tests/                  Unit tests (fakes for BlueZ and bleak)
├── requirements.txt        Python dependencies
└── Dockerfile              Container build definition
```
