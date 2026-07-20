"""
gpio_driver.py — Thin abstraction over RPi.GPIO and gpiozero.

At startup the controller tries RPi.GPIO first; if it's not available
(e.g. you're developing on a laptop) it falls back to gpiozero, and
finally to a software mock that just logs what would happen.

You can force a specific backend by setting GPIO_BACKEND in config.yaml
to "rpigpio", "gpiozero", or "mock".
"""

import logging

logger = logging.getLogger(__name__)


# ── Try importing the available GPIO backend ─────────────────────────────────

def _load_backend(preferred: str = "auto"):
    if preferred in ("rpigpio", "auto"):
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            logger.info("GPIO backend: RPi.GPIO (BCM mode)")
            return _RPiGPIODriver(GPIO)
        except (ImportError, RuntimeError):
            if preferred == "rpigpio":
                raise RuntimeError("RPi.GPIO not available — is this a Raspberry Pi?")

    if preferred in ("gpiozero", "auto"):
        try:
            from gpiozero import OutputDevice
            logger.info("GPIO backend: gpiozero")
            return _GPIOZeroDriver(OutputDevice)
        except ImportError:
            if preferred == "gpiozero":
                raise RuntimeError("gpiozero not installed. Run: pip install gpiozero")

    logger.warning("No GPIO library found — using MOCK driver (no real pins toggled).")
    return _MockDriver()


# ── Backend implementations ───────────────────────────────────────────────────

class _RPiGPIODriver:
    """Wraps RPi.GPIO for direct BCM pin control."""

    def __init__(self, GPIO):
        self._GPIO = GPIO
        self._pins: dict[int, bool] = {}   # pin → current state (True=HIGH)

    def setup(self, pin: int, normally_open: bool):
        """Configure pin as output; default to relay-OFF state."""
        self._GPIO.setup(pin, self._GPIO.OUT)
        initial = self._GPIO.LOW if normally_open else self._GPIO.HIGH
        self._GPIO.output(pin, initial)
        self._pins[pin] = not normally_open   # track logical state

    def set_relay(self, pin: int, on: bool, normally_open: bool):
        """
        Turn the relay ON or OFF.
        normally_open=True  → HIGH = coil energised = relay closed = appliance ON
        normally_open=False → LOW  = coil energised = relay closed = appliance ON
        """
        if normally_open:
            level = self._GPIO.HIGH if on else self._GPIO.LOW
        else:
            level = self._GPIO.LOW if on else self._GPIO.HIGH
        self._GPIO.output(pin, level)
        self._pins[pin] = on

    def get_state(self, pin: int) -> bool:
        return self._pins.get(pin, False)

    def cleanup(self):
        self._GPIO.cleanup()
        logger.info("RPi.GPIO cleaned up.")


class _GPIOZeroDriver:
    """Wraps gpiozero OutputDevice."""

    def __init__(self, OutputDevice):
        self._OutputDevice = OutputDevice
        self._devices: dict[int, object] = {}
        self._states: dict[int, bool] = {}

    def setup(self, pin: int, normally_open: bool):
        # active_high=True → device.on() drives pin HIGH
        dev = self._OutputDevice(pin, active_high=normally_open, initial_value=False)
        self._devices[pin] = dev
        self._states[pin] = False

    def set_relay(self, pin: int, on: bool, normally_open: bool):
        dev = self._devices[pin]
        dev.on() if on else dev.off()
        self._states[pin] = on

    def get_state(self, pin: int) -> bool:
        return self._states.get(pin, False)

    def cleanup(self):
        for dev in self._devices.values():
            dev.close()
        logger.info("gpiozero devices closed.")


class _MockDriver:
    """Simulates GPIO without touching any hardware — safe for development."""

    def __init__(self):
        self._states: dict[int, bool] = {}

    def setup(self, pin: int, normally_open: bool):
        self._states[pin] = False
        logger.debug(f"[MOCK] Setup pin {pin} (normally_open={normally_open})")

    def set_relay(self, pin: int, on: bool, normally_open: bool):
        self._states[pin] = on
        state_str = "ON" if on else "OFF"
        logger.info(f"[MOCK] Pin {pin} → {state_str}")

    def get_state(self, pin: int) -> bool:
        return self._states.get(pin, False)

    def cleanup(self):
        logger.info("[MOCK] GPIO cleanup (no-op).")


# ── Public factory ────────────────────────────────────────────────────────────

def create_driver(preferred: str = "auto") -> object:
    """Return an initialised GPIO driver. preferred ∈ {'auto','rpigpio','gpiozero','mock'}"""
    return _load_backend(preferred)
