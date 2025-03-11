# Stage 1: Builder
FROM python:3.12-alpine as builder
WORKDIR /usr/src

# Install build/runtime dependencies
RUN apk add --no-cache py3-dbus libc6-compat

# Install python dependencies to a local directory
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final Image
FROM python:3.12-alpine
WORKDIR /usr/src

# Set PATH for the locally installed packages
ENV PATH=/root/.local/bin:$PATH
ENV DBUS_SYSTEM_BUS_ADDRESS=unix:path=/host/run/dbus/system_bus_socket

# Copy only necessary files from builder
COPY --from=builder /root/.local /root/.local
# Copy your application source code
COPY . .

EXPOSE 8000
CMD ["python", "main.py"]
