# bluetoothctl-over-web

Simple Web UI exposing functionality like `bluetoothctl` for an Ubuntu-based
headless home server.

## Features

- View adapter status (powered, name, address)
- Toggle **Discoverable** flag (allow nearby devices to find the adapter)
- Toggle **Pairable** flag (allow new devices to pair)
- **Confirm or reject pairing requests** directly in the browser

## Requirements

- Python 3.8+
- BlueZ D-Bus service available on the system (`sudo apt install bluez`)

## Setup

```bash
# Install Python dependencies
pip install -r requirements.txt

# Start the server (needs D-Bus access to the BlueZ service – run as a user
# in the 'bluetooth' group or with appropriate permissions)
python app.py
```

Then open <http://localhost:5000> in your browser.

## Screenshots

The UI automatically follows the system colour-scheme preference.

**Dark theme** (default on dark-mode systems)

![Dark theme](https://github.com/user-attachments/assets/10407616-5db1-4144-9206-3f5aae42c176)

**Light theme** (used on light-mode systems)

![Light theme](https://github.com/user-attachments/assets/4db48ca6-bc8d-423a-9327-438dab6f1dd2)

## Running as a systemd service

```ini
[Unit]
Description=bluetoothctl-over-web
After=network.target bluetooth.target

[Service]
ExecStart=/usr/bin/python3 /opt/bluetoothctl-over-web/app.py
WorkingDirectory=/opt/bluetoothctl-over-web
Restart=on-failure
User=<your-user>

[Install]
WantedBy=multi-user.target
```
