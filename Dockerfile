# Stage 1: Builder
FROM python:3.12-alpine as builder
WORKDIR /usr/src

# Install build/runtime dependencies
RUN apk add --no-cache bluez

# Install python dependencies to a local directory
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Set PATH for the locally installed packages
ENV PATH=/root/.local/bin:$PATH
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Copy your application source code
COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
