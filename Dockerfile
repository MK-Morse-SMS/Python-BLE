# Single stage on purpose. Nothing here needs a build toolchain -- every pinned
# dependency ships a musllinux wheel -- so a builder stage saved no image size,
# and balena's builder only caches from the previous release's *final* images.
# In an intermediate stage the pip layer could never be reused, so every release
# reinstalled the tree and shipped a full-size delta to the fleet.
#
# bluez is deliberately absent: nothing shells out to bluetoothctl/hciconfig,
# and bleak reaches the host's bluetoothd through dbus-fast over the socket
# balena mounts at /host/run/dbus.
FROM python:3.12-alpine
WORKDIR /usr/src

# Ahead of the source copy so an application change leaves this layer cached.
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Set PATH for the locally installed packages
ENV PATH=/root/.local/bin:$PATH
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Copy your application source code
COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
