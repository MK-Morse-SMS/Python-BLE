# Stage 1: resolve the dependency tree into /root/.local
FROM python:3.12-alpine AS builder
WORKDIR /usr/src

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: runtime. Previously this file had only the stage above, so the build
# tooling shipped to the fleet. bluez is also deliberately gone: nothing here
# shells out to bluetoothctl/hciconfig, and bleak reaches the host's bluetoothd
# through dbus-fast over the socket balena mounts at /host/run/dbus.
FROM python:3.12-alpine
WORKDIR /usr/src

COPY --from=builder /root/.local /root/.local

# Set PATH for the locally installed packages
ENV PATH=/root/.local/bin:$PATH
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Copy your application source code
COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
