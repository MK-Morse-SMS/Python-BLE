FROM python:3.12-alpine
WORKDIR /usr/src

# DBUS is required for bluetooth-agent
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Install dependencies
RUN apk add --no-cache \
    py3-dbus \
    libc6-compat

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

EXPOSE 8000

# Run the application
CMD ["python", "main.py"]