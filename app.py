"""bluetoothctl-over-web – Flask backend."""

import re
import subprocess
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


class BluetoothManager:
    """Manage local Bluetooth adapter via bluetoothctl."""

    def __init__(self):
        self._process = None
        self._lock = threading.Lock()
        self._pairing_requests = []
        self._last_device = None
        self._output_thread = None

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def start_agent(self):
        """Start a persistent bluetoothctl process registered as the default
        pairing agent so that incoming pairing requests are intercepted."""
        try:
            self._process = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("[WARN] bluetoothctl not found – pairing agent disabled")
            return

        self._send("agent on")
        # Give bluetoothctl a moment to acknowledge the agent registration
        # before promoting it to the default agent.
        time.sleep(0.3)
        self._send("default-agent")

        self._output_thread = threading.Thread(
            target=self._read_output, daemon=True
        )
        self._output_thread.start()

    def _send(self, command: str):
        if self._process and self._process.poll() is None:
            self._process.stdin.write(command + "\n")
            self._process.stdin.flush()

    def _read_output(self):
        """Background thread – watch bluetoothctl stdout for pairing prompts."""
        try:
            for line in self._process.stdout:
                line = line.strip()
                if line:
                    print(f"[BT] {line}")
                    self._parse_line(line)
        except ValueError:
            # stdout was closed (e.g. process exited)
            pass
        print("[BT] agent process exited")

    def _parse_line(self, line: str):
        # Track the most recently seen device from e.g.
        # "[NEW] Device AA:BB:CC:DD:EE:FF DeviceName"
        new_device = re.search(
            r"\[NEW\] Device ([0-9A-Fa-f:]{17}) (.+)", line
        )
        if new_device:
            self._last_device = {
                "address": new_device.group(1),
                "name": new_device.group(2),
            }

        # "[agent] Confirm passkey 123456 (yes/no):"
        passkey_match = re.search(r"\[agent\] Confirm passkey (\d+)", line)
        if passkey_match:
            with self._lock:
                self._pairing_requests.append(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "confirm_passkey",
                        "passkey": passkey_match.group(1),
                        "device": self._last_device,
                        "timestamp": time.time(),
                    }
                )
            return

        # "[agent] Request confirmation" (no passkey, just yes/no)
        if re.search(r"\[agent\] Request confirmation", line):
            with self._lock:
                self._pairing_requests.append(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "confirm",
                        "passkey": None,
                        "device": self._last_device,
                        "timestamp": time.time(),
                    }
                )
            return

        # "[agent] Enter PIN code:"
        if re.search(r"\[agent\] Enter PIN code", line):
            with self._lock:
                self._pairing_requests.append(
                    {
                        "id": str(uuid.uuid4()),
                        "type": "pin_code",
                        "passkey": None,
                        "device": self._last_device,
                        "timestamp": time.time(),
                    }
                )

    # ------------------------------------------------------------------
    # Bluetooth status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a dict with the current adapter properties."""
        try:
            result = subprocess.run(
                ["bluetoothctl", "show"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return self._parse_status(result.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return {
                "powered": False,
                "discoverable": False,
                "pairable": False,
                "address": "",
                "name": "",
                "error": str(exc),
            }

    def _parse_status(self, output: str) -> dict:
        status = {
            "powered": False,
            "discoverable": False,
            "pairable": False,
            "address": "",
            "name": "",
        }
        for line in output.splitlines():
            line = line.strip()
            # "Controller AA:BB:CC:DD:EE:FF (public)"
            ctrl_match = re.match(r"Controller\s+([0-9A-Fa-f:]{17})", line)
            if ctrl_match:
                status["address"] = ctrl_match.group(1)
            elif line.startswith("Name:"):
                status["name"] = line.split(":", 1)[1].strip()
            elif line.startswith("Powered:"):
                status["powered"] = "yes" in line.lower()
            elif line.startswith("Discoverable:"):
                status["discoverable"] = "yes" in line.lower()
            elif line.startswith("Pairable:"):
                status["pairable"] = "yes" in line.lower()
        return status

    # ------------------------------------------------------------------
    # Toggle helpers
    # ------------------------------------------------------------------

    def _run_btctl(self, *args) -> bool:
        try:
            result = subprocess.run(
                ["bluetoothctl", *args],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def set_discoverable(self, enabled: bool) -> bool:
        return self._run_btctl("discoverable", "on" if enabled else "off")

    def set_pairable(self, enabled: bool) -> bool:
        return self._run_btctl("pairable", "on" if enabled else "off")

    # ------------------------------------------------------------------
    # Pairing requests
    # ------------------------------------------------------------------

    def get_pairing_requests(self) -> list:
        with self._lock:
            return list(self._pairing_requests)

    def respond_to_pairing(self, request_id: str, accepted: bool) -> bool:
        with self._lock:
            matched = next(
                (r for r in self._pairing_requests if r["id"] == request_id),
                None,
            )
            if matched:
                self._pairing_requests = [
                    r for r in self._pairing_requests if r["id"] != request_id
                ]
                self._send("yes" if accepted else "no")
                return True
        return False


bt_manager = BluetoothManager()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    return jsonify(bt_manager.get_status())


@app.route("/api/discoverable", methods=["POST"])
def api_discoverable():
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    success = bt_manager.set_discoverable(enabled)
    return jsonify({"success": success})


@app.route("/api/pairable", methods=["POST"])
def api_pairable():
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    success = bt_manager.set_pairable(enabled)
    return jsonify({"success": success})


@app.route("/api/pairing-requests")
def api_pairing_requests():
    return jsonify(bt_manager.get_pairing_requests())


@app.route("/api/pairing-requests/<request_id>", methods=["POST"])
def api_respond_pairing(request_id):
    data = request.get_json(force=True)
    accepted = bool(data.get("accepted", False))
    success = bt_manager.respond_to_pairing(request_id, accepted)
    return jsonify({"success": success})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    bt_manager.start_agent()
    app.run(host="0.0.0.0", port=5000, debug=False)
