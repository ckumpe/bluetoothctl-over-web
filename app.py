"""bluetoothctl-over-web – Flask backend using dbus-fast / BlueZ D-Bus API."""

import argparse
import asyncio
import json
import logging
import queue
import threading
import time
import uuid

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# D-Bus / BlueZ constants
# ---------------------------------------------------------------------------

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
AGENT_IFACE = "org.bluez.Agent1"
AGENTMANAGER_IFACE = "org.bluez.AgentManager1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OBJMANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"

# D-Bus object path under which we export the pairing agent
AGENT_PATH = "/com/github/bluetoothctloverweb/agent"

# How long to wait before retrying adapter discovery on startup
ADAPTER_RETRY_DELAY_S = 10

# ---------------------------------------------------------------------------
# Optional dbus-fast imports with graceful fallback stubs
# ---------------------------------------------------------------------------

try:
    from dbus_fast.service import ServiceInterface, method as dbus_method
    from dbus_fast import DBusError, MessageType, Variant, BusType
    from dbus_fast.aio import MessageBus as AsyncMessageBus

    _DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _DBUS_AVAILABLE = False

    class ServiceInterface:  # type: ignore[no-redef]
        def __init__(self, iface_name: str) -> None:
            pass

    def dbus_method(*args, **kwargs):  # type: ignore[no-redef]
        def decorator(fn):
            return fn

        return decorator

    class DBusError(Exception):  # type: ignore[no-redef]
        def __init__(self, name: str, msg: str) -> None:
            super().__init__(msg)

    Variant = None  # type: ignore[assignment]
    BusType = None  # type: ignore[assignment]
    MessageType = None  # type: ignore[assignment]
    AsyncMessageBus = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# BlueZ pairing agent (org.bluez.Agent1)
# ---------------------------------------------------------------------------


class PairingAgent(ServiceInterface):
    """BlueZ pairing agent exported on D-Bus.

    BlueZ calls these methods when a remote device initiates pairing.
    Each request is forwarded to ``BluetoothManager._handle_agent_request``,
    which suspends until the user responds via the web UI.
    """

    def __init__(self, manager: "BluetoothManager") -> None:
        super().__init__(AGENT_IFACE)
        self._mgr = manager

    @dbus_method()
    def Release(self) -> None:  # noqa: N802
        logger.debug("Released by BlueZ")

    @dbus_method()
    def Cancel(self) -> None:  # noqa: N802
        logger.debug("Cancelled by BlueZ")

    @dbus_method()
    async def RequestConfirmation(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]  # D-Bus object path
        passkey: "u",  # type: ignore[name-defined]  # D-Bus uint32
    ) -> None:
        """Called when a passkey needs to be confirmed by the user."""
        accepted = await self._mgr._handle_agent_request(
            device, "confirm_passkey", str(passkey).zfill(6)
        )
        if not accepted:
            raise DBusError("org.bluez.Error.Rejected", "Rejected by user")

    @dbus_method()
    async def RequestAuthorization(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]
    ) -> None:
        """Called for simple authorization without a passkey."""
        accepted = await self._mgr._handle_agent_request(
            device, "confirm", None
        )
        if not accepted:
            raise DBusError("org.bluez.Error.Rejected", "Rejected by user")

    @dbus_method()
    async def RequestPinCode(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]
    ) -> "s":  # type: ignore[name-defined]  # D-Bus string
        """Called for legacy PIN-based pairing."""
        accepted = await self._mgr._handle_agent_request(
            device, "pin_code", None
        )
        if not accepted:
            raise DBusError("org.bluez.Error.Rejected", "Rejected by user")
        return "0000"

    @dbus_method()
    async def AuthorizeService(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]
        uuid: "s",  # type: ignore[name-defined]  # noqa: A002
    ) -> None:
        """Called when a paired device requests access to a service."""
        # Auto-authorize connected paired devices.
        pass

    @dbus_method()
    def DisplayPasskey(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]
        passkey: "u",  # type: ignore[name-defined]
        entered: "q",  # type: ignore[name-defined]  # D-Bus uint16
    ) -> None:
        """BlueZ informational callback – passkey display only."""

    @dbus_method()
    def DisplayPinCode(  # noqa: N802
        self,
        device: "o",  # type: ignore[name-defined]
        pincode: "s",  # type: ignore[name-defined]
    ) -> None:
        """BlueZ informational callback – PIN display only."""


# ---------------------------------------------------------------------------
# Bluetooth manager
# ---------------------------------------------------------------------------


class BluetoothManager:
    """Manages the local Bluetooth adapter via the BlueZ D-Bus API.

    A dedicated daemon thread owns a private asyncio event loop that:

    * Connects to the system D-Bus.
    * Discovers the first available BlueZ adapter via ObjectManager.
    * Caches a ``org.freedesktop.DBus.Properties`` proxy for the adapter.
    * Exports and registers a :class:`PairingAgent` as the default BlueZ agent.
    * Handles incoming pairing requests; each request suspends the agent
      coroutine until the user responds through the web UI.

    Flask route handlers communicate with the event loop via
    ``asyncio.run_coroutine_threadsafe`` (for async reads/writes) and
    ``loop.call_soon_threadsafe`` (for resolving pairing request futures).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Pending pairing requests: id → {id, type, passkey, device,
        #                                  timestamp, future}
        self._pairing_requests: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bus = None
        self._adapter_path: str | None = None
        # Cached org.freedesktop.DBus.Properties proxy for the adapter
        self._adapter_props = None
        # SSE subscriber queues
        self._sse_queues: set[queue.Queue] = set()
        self._sse_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the background D-Bus / asyncio thread."""
        thread = threading.Thread(
            target=self._run_loop, daemon=True, name="dbus-bt"
        )
        thread.start()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:  # pragma: no cover
            logger.critical("Fatal error in D-Bus thread: %s", exc)

    async def _async_main(self) -> None:
        if not _DBUS_AVAILABLE:
            logger.warning("dbus-fast is not installed – Bluetooth unavailable")
            return

        # Connect to the system D-Bus
        try:
            self._bus = await AsyncMessageBus(bus_type=BusType.SYSTEM).connect()
            logger.info("Connected to system D-Bus")
        except Exception as exc:
            logger.error("Cannot connect to system D-Bus: %s", exc)
            return

        # Discover the first Bluetooth adapter
        self._adapter_path = await self._find_adapter()
        if not self._adapter_path:
            logger.warning("No Bluetooth adapter found – retrying in %d s", ADAPTER_RETRY_DELAY_S)
            await asyncio.sleep(ADAPTER_RETRY_DELAY_S)
            self._adapter_path = await self._find_adapter()
        if not self._adapter_path:
            logger.error("No Bluetooth adapter found; giving up")
            return

        # Cache the Properties proxy for efficient property reads/writes
        try:
            intro = await self._bus.introspect(
                BLUEZ_SERVICE, self._adapter_path
            )
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, self._adapter_path, intro
            )
            self._adapter_props = proxy.get_interface(PROPS_IFACE)
        except Exception as exc:
            logger.error("Cannot introspect adapter at %s: %s", self._adapter_path, exc)
            return

        # Register the pairing agent with BlueZ
        await self._register_agent()

        # Subscribe to PropertiesChanged signals via add_message_handler so that
        # both adapter state changes and device connection changes are pushed to
        # SSE clients immediately, without requiring an explicit user action.
        def _on_properties_changed(msg) -> None:
            if (
                msg.message_type is not MessageType.SIGNAL
                or msg.interface != PROPS_IFACE
                or msg.member != "PropertiesChanged"
                or not msg.body
            ):
                return None
            iface_name = msg.body[0]
            changed = msg.body[1] if len(msg.body) > 1 else {}
            if iface_name == DEVICE_IFACE and "Connected" in changed:
                task = asyncio.create_task(self._notify_devices())
                task.add_done_callback(
                    lambda t: None
                    if t.cancelled() or not t.exception()
                    else logger.error("Error notifying devices: %s", t.exception())
                )
            elif iface_name == ADAPTER_IFACE:
                task = asyncio.create_task(self._notify_status())
                task.add_done_callback(
                    lambda t: None
                    if t.cancelled() or not t.exception()
                    else logger.error("Error notifying status: %s", t.exception())
                )
            return None

        try:
            self._bus.add_message_handler(_on_properties_changed)
            logger.info("Subscribed to PropertiesChanged signals")
        except Exception as exc:
            logger.warning("Could not subscribe to PropertiesChanged: %s", exc)

        logger.info("Ready – adapter %s", self._adapter_path)

        # Keep the event loop alive indefinitely (daemon thread exits with app)
        await asyncio.get_running_loop().create_future()

    async def _find_adapter(self) -> str | None:
        """Return the D-Bus object path of the first Bluetooth adapter."""
        try:
            intro = await self._bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            objmgr = proxy.get_interface(OBJMANAGER_IFACE)
            objects = await objmgr.call_get_managed_objects()
            for path in sorted(objects.keys()):
                if ADAPTER_IFACE in objects[path]:
                    return path
        except Exception as exc:
            logger.error("ObjectManager error: %s", exc)
        return None

    async def _register_agent(self) -> None:
        """Export the pairing agent object and register it with BlueZ."""
        try:
            agent = PairingAgent(self)
            self._bus.export(AGENT_PATH, agent)
            intro = await self._bus.introspect(BLUEZ_SERVICE, "/org/bluez")
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, "/org/bluez", intro
            )
            agentmgr = proxy.get_interface(AGENTMANAGER_IFACE)
            await agentmgr.call_register_agent(AGENT_PATH, "KeyboardDisplay")
            await agentmgr.call_request_default_agent(AGENT_PATH)
            logger.info("Pairing agent registered at %s", AGENT_PATH)
        except Exception as exc:
            logger.error("Agent registration failed: %s", exc)

    # ------------------------------------------------------------------
    # Agent request handling (runs inside the event loop)
    # ------------------------------------------------------------------

    async def _get_devices_async(self) -> list:
        """Return a list of connected devices under the current adapter."""
        try:
            intro = await self._bus.introspect(BLUEZ_SERVICE, "/")
            proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, "/", intro)
            objmgr = proxy.get_interface(OBJMANAGER_IFACE)
            objects = await objmgr.call_get_managed_objects()
            devices = []
            for path, ifaces in objects.items():
                if DEVICE_IFACE not in ifaces:
                    continue
                if self._adapter_path and not path.startswith(
                    self._adapter_path + "/"
                ):
                    continue
                props = ifaces[DEVICE_IFACE]
                connected = props.get("Connected", Variant("b", False)).value
                paired = props.get("Paired", Variant("b", False)).value
                if not connected:
                    continue
                address = props.get("Address", Variant("s", "")).value
                name = props.get(
                    "Name",
                    props.get("Alias", Variant("s", "Unknown")),
                ).value
                devices.append(
                    {
                        "address": address,
                        "name": name,
                        "connected": connected,
                        "paired": paired,
                    }
                )
            return sorted(devices, key=lambda d: d["name"].lower())
        except Exception as exc:
            logger.error("Error fetching devices: %s", exc)
            return []

    async def _notify_devices(self) -> None:
        """Fetch the current device list and push it to all SSE subscribers."""
        devices = await self._get_devices_async()
        self._notify_clients("devices", devices)

    async def _notify_status(self) -> None:
        """Fetch the current adapter status and push it to all SSE subscribers."""
        status = await self._get_status_async()
        self._notify_clients("status", status)

    async def _get_device_info(self, device_path: str) -> dict:
        """Fetch device name and address from the D-Bus Device1 interface."""
        try:
            intro = await self._bus.introspect(BLUEZ_SERVICE, device_path)
            proxy = self._bus.get_proxy_object(
                BLUEZ_SERVICE, device_path, intro
            )
            props = proxy.get_interface(PROPS_IFACE)
            all_props = await props.call_get_all(DEVICE_IFACE)
            address = all_props.get("Address", Variant("s", "")).value
            # Prefer human-readable Name, fall back to Alias
            name = all_props.get(
                "Name",
                all_props.get("Alias", Variant("s", "Unknown")),
            ).value
            return {"address": address, "name": name}
        except Exception:
            return {"address": str(device_path), "name": "Unknown"}

    async def _handle_agent_request(
        self, device_path: str, req_type: str, passkey: str | None
    ) -> bool:
        """Add a pairing request to the pending queue and wait for the user.

        The coroutine suspends here until :meth:`respond_to_pairing` resolves
        the future (or until the 120-second timeout expires).
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        req_id = str(uuid.uuid4())
        device_info = await self._get_device_info(device_path)
        with self._lock:
            self._pairing_requests[req_id] = {
                "id": req_id,
                "type": req_type,
                "passkey": passkey,
                "device": device_info,
                "timestamp": time.time(),
                "future": fut,
            }
        logger.info("Pairing request %s (%s) from %s", req_id, req_type, device_info)
        self._notify_clients("pairing_requests", self.get_pairing_requests())
        try:
            # shield() prevents wait_for from cancelling the underlying future
            # so respond_to_pairing can still resolve it after a timeout.
            return await asyncio.wait_for(asyncio.shield(fut), timeout=120.0)
        except asyncio.TimeoutError:
            logger.warning("Pairing request %s timed out", req_id)
            return False
        finally:
            with self._lock:
                self._pairing_requests.pop(req_id, None)
            self._notify_clients("pairing_requests", self.get_pairing_requests())

    # ------------------------------------------------------------------
    # SSE subscriber management
    # ------------------------------------------------------------------

    def subscribe(self) -> "queue.Queue[tuple[str, object]]":
        """Return a new queue that will receive (event_type, data) tuples."""
        q: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=100)
        with self._sse_lock:
            self._sse_queues.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        """Remove *q* from the subscriber set."""
        with self._sse_lock:
            self._sse_queues.discard(q)

    def _notify_clients(self, event_type: str, data: object) -> None:
        """Push *data* to every active SSE subscriber queue."""
        with self._sse_lock:
            dead: set["queue.Queue"] = set()
            for q in self._sse_queues:
                try:
                    q.put_nowait((event_type, data))
                except queue.Full:
                    dead.add(q)
            self._sse_queues -= dead

    # ------------------------------------------------------------------
    # Helpers for Flask threads
    # ------------------------------------------------------------------

    def _run_async(self, coro):
        """Schedule *coro* in the background event loop and block until done."""
        if not self._loop:
            raise RuntimeError("D-Bus event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=5)

    # ------------------------------------------------------------------
    # Adapter status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        """Return a dict with the current adapter properties."""
        if not self._loop or not self._adapter_path:
            return {
                "powered": False,
                "discoverable": False,
                "pairable": False,
                "address": "",
                "name": "",
                "error": (
                    "Bluetooth not available – "
                    "D-Bus not connected or no adapter found"
                ),
            }
        try:
            return self._run_async(self._get_status_async())
        except Exception as exc:
            return {
                "powered": False,
                "discoverable": False,
                "pairable": False,
                "address": "",
                "name": "",
                "error": str(exc),
            }

    async def _get_status_async(self) -> dict:
        all_props = await self._adapter_props.call_get_all(ADAPTER_IFACE)
        return {
            "address": all_props.get("Address", Variant("s", "")).value,
            "name": all_props.get("Name", Variant("s", "")).value,
            "powered": all_props.get("Powered", Variant("b", False)).value,
            "discoverable": all_props.get(
                "Discoverable", Variant("b", False)
            ).value,
            "pairable": all_props.get("Pairable", Variant("b", False)).value,
        }

    # ------------------------------------------------------------------
    # Property setters
    # ------------------------------------------------------------------

    def set_discoverable(self, enabled: bool) -> bool:
        if not self._loop or not self._adapter_path:
            return False
        try:
            return self._run_async(
                self._set_prop_async("Discoverable", enabled)
            )
        except Exception:
            return False

    def set_pairable(self, enabled: bool) -> bool:
        if not self._loop or not self._adapter_path:
            return False
        try:
            return self._run_async(self._set_prop_async("Pairable", enabled))
        except Exception:
            return False

    async def _set_prop_async(self, prop: str, value: bool) -> bool:
        try:
            await self._adapter_props.call_set(
                ADAPTER_IFACE, prop, Variant("b", value)
            )
            return True
        except Exception as exc:
            logger.error("Failed to set %s=%s: %s", prop, value, exc)
            return False

    # ------------------------------------------------------------------
    # Pairing requests
    # ------------------------------------------------------------------

    def get_pairing_requests(self) -> list:
        """Return serialisable pending pairing requests (no asyncio Futures)."""
        with self._lock:
            return [
                {k: v for k, v in req.items() if k != "future"}
                for req in self._pairing_requests.values()
            ]

    def respond_to_pairing(self, request_id: str, accepted: bool) -> bool:
        """Resolve the pending pairing future from a Flask thread."""
        with self._lock:
            req = self._pairing_requests.get(request_id)
            if not req:
                return False
            fut = req["future"]
        if self._loop and not fut.done():
            def _resolve():
                if not fut.done():
                    fut.set_result(accepted)

            self._loop.call_soon_threadsafe(_resolve)
            return True
        return False

    # ------------------------------------------------------------------
    # Connected devices
    # ------------------------------------------------------------------

    def get_devices(self) -> list:
        """Return a serialisable list of currently connected devices."""
        if not self._loop or not self._adapter_path:
            return []
        try:
            return self._run_async(self._get_devices_async())
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)
bt_manager = BluetoothManager()


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
    if success:
        bt_manager._notify_clients("status", bt_manager.get_status())
    return jsonify({"success": success})


@app.route("/api/pairable", methods=["POST"])
def api_pairable():
    data = request.get_json(force=True)
    enabled = bool(data.get("enabled", False))
    success = bt_manager.set_pairable(enabled)
    if success:
        bt_manager._notify_clients("status", bt_manager.get_status())
    return jsonify({"success": success})


@app.route("/api/pairing-requests")
def api_pairing_requests():
    return jsonify(bt_manager.get_pairing_requests())


@app.route("/api/devices")
def api_devices():
    return jsonify(bt_manager.get_devices())


@app.route("/api/pairing-requests/<request_id>", methods=["POST"])
def api_respond_pairing(request_id):
    data = request.get_json(force=True)
    accepted = bool(data.get("accepted", False))
    success = bt_manager.respond_to_pairing(request_id, accepted)
    return jsonify({"success": success})


@app.route("/api/stream")
def api_stream():
    """Server-Sent Events endpoint – pushes status and pairing-request updates."""

    def generate():
        q = bt_manager.subscribe()
        try:
            # Send the current state immediately on connect
            yield f"event: status\ndata: {json.dumps(bt_manager.get_status())}\n\n"
            yield (
                f"event: pairing_requests\n"
                f"data: {json.dumps(bt_manager.get_pairing_requests())}\n\n"
            )
            yield f"event: devices\ndata: {json.dumps(bt_manager.get_devices())}\n\n"

            while True:
                try:
                    event_type, data = q.get(timeout=15)
                    yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
                except queue.Empty:
                    # Keepalive comment to prevent proxy/browser timeouts
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            bt_manager.unsubscribe(q)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="bluetoothctl-over-web")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    # Route Werkzeug access logs through the root logger so they share the
    # same format and respect --log-level (e.g. hidden at WARNING and above).
    werkzeug_logger = logging.getLogger("werkzeug")
    werkzeug_logger.handlers.clear()
    werkzeug_logger.setLevel(getattr(logging, args.log_level))

    bt_manager.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
