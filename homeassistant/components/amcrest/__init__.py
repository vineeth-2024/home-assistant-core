"""Support for Amcrest IP cameras."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import threading
from typing import Any

import aiohttp
from amcrest import AmcrestError, ApiWrapper, LoginError
import httpx
import voluptuous as vol

from homeassistant.auth.models import User
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_AUTHENTICATION,
    CONF_BINARY_SENSORS,
    CONF_HOST,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_SENSORS,
    CONF_SWITCHES,
    CONF_USERNAME,
    ENTITY_MATCH_ALL,
    ENTITY_MATCH_NONE,
    HTTP_BASIC_AUTHENTICATION,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send, dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_extract_entity_ids
from homeassistant.helpers.typing import ConfigType

from .binary_sensor import BINARY_SENSOR_KEYS, BINARY_SENSORS, check_binary_sensors
from .camera import CAMERA_SERVICES, STREAM_SOURCE_LIST
from .const import (
    CAMERAS,
    COMM_RETRIES,
    COMM_TIMEOUT,
    DATA_AMCREST,
    DEVICES,
    DOMAIN,
    RESOLUTION_LIST,
    SERVICE_EVENT,
    SERVICE_UPDATE,
)
from .helpers import service_signal
from .sensor import SENSOR_KEYS
from .switch import SWITCH_KEYS

_LOGGER = logging.getLogger(__name__)

CONF_RESOLUTION = "resolution"
CONF_STREAM_SOURCE = "stream_source"
CONF_FFMPEG_ARGUMENTS = "ffmpeg_arguments"
CONF_CONTROL_LIGHT = "control_light"

DEFAULT_NAME = "Amcrest Camera"
DEFAULT_PORT = 80
DEFAULT_RESOLUTION = "high"
DEFAULT_ARGUMENTS = "-pred 1"
MAX_ERRORS = 5
RECHECK_INTERVAL = timedelta(minutes=1)

NOTIFICATION_ID = "amcrest_notification"
NOTIFICATION_TITLE = "Amcrest Camera Setup"

SCAN_INTERVAL = timedelta(seconds=10)

AUTHENTICATION_LIST = {"basic": "basic"}


def _has_unique_names(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    names = [device[CONF_NAME] for device in devices]
    vol.Schema(vol.Unique())(names)
    return devices


AMCREST_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_AUTHENTICATION, default=HTTP_BASIC_AUTHENTICATION): vol.All(
            vol.In(AUTHENTICATION_LIST)
        ),
        vol.Optional(CONF_RESOLUTION, default=DEFAULT_RESOLUTION): vol.All(
            vol.In(RESOLUTION_LIST)
        ),
        vol.Optional(CONF_STREAM_SOURCE, default=STREAM_SOURCE_LIST[0]): vol.All(
            vol.In(STREAM_SOURCE_LIST)
        ),
        vol.Optional(CONF_FFMPEG_ARGUMENTS, default=DEFAULT_ARGUMENTS): cv.string,
        vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL): cv.time_period,
        vol.Optional(CONF_BINARY_SENSORS): vol.All(
            cv.ensure_list,
            [vol.In(BINARY_SENSOR_KEYS)],
            vol.Unique(),
            check_binary_sensors,
        ),
        vol.Optional(CONF_SWITCHES): vol.All(
            cv.ensure_list, [vol.In(SWITCH_KEYS)], vol.Unique()
        ),
        vol.Optional(CONF_SENSORS): vol.All(
            cv.ensure_list, [vol.In(SENSOR_KEYS)], vol.Unique()
        ),
        vol.Optional(CONF_CONTROL_LIGHT, default=True): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [AMCREST_SCHEMA], _has_unique_names)},
    extra=vol.ALLOW_EXTRA,
)


class AmcrestChecker(ApiWrapper):
    """amcrest.ApiWrapper wrapper for catching errors."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        port: int,
        user: str,
        password: str,
    ) -> None:
        """Initialize."""
        self._hass = hass
        self._wrap_name = name
        self._wrap_errors = 0
        self._wrap_lock = threading.Lock()
        self._async_wrap_lock = asyncio.Lock()
        self._wrap_login_err = False
        self._wrap_event_flag = threading.Event()
        self._wrap_event_flag.set()
        self._async_wrap_event_flag = asyncio.Event()
        self._async_wrap_event_flag.set()
        self._unsub_recheck: Callable[[], None] | None = None
        super().__init__(
            host,
            port,
            user,
            password,
            retries_connection=COMM_RETRIES,
            timeout_protocol=COMM_TIMEOUT,
        )

    @property
    def available(self) -> bool:
        """Return if camera's API is responding."""
        return self._wrap_errors <= MAX_ERRORS and not self._wrap_login_err

    @property
    def available_flag(self) -> threading.Event:
        """Return event flag that indicates if camera's API is responding."""
        return self._wrap_event_flag

    @property
    def async_available_flag(self) -> asyncio.Event:
        """Return event flag that indicates if camera's API is responding."""
        return self._async_wrap_event_flag

    @callback
    def _async_start_recovery(self) -> None:
        self.available_flag.clear()
        self.async_available_flag.clear()
        async_dispatcher_send(
            self._hass, service_signal(SERVICE_UPDATE, self._wrap_name)
        )
        self._unsub_recheck = async_track_time_interval(
            self._hass, self._wrap_test_online, RECHECK_INTERVAL
        )

    def command(self, *args: Any, **kwargs: Any) -> Any:
        """amcrest.ApiWrapper.command wrapper to catch errors."""
        try:
            ret = super().command(*args, **kwargs)
        except LoginError as ex:
            self._handle_offline(ex)
            raise
        except AmcrestError:
            self._handle_error()
            raise
        self._set_online()
        return ret

    async def async_command(self, *args: Any, **kwargs: Any) -> httpx.Response:
        """amcrest.ApiWrapper.command wrapper to catch errors."""
        async with self._async_command_wrapper():
            return await super().async_command(*args, **kwargs)

    @asynccontextmanager
    async def async_stream_command(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[httpx.Response]:
        """amcrest.ApiWrapper.command wrapper to catch errors."""
        async with (
            self._async_command_wrapper(),
            super().async_stream_command(*args, **kwargs) as ret,
        ):
            yield ret

    @asynccontextmanager
    async def _async_command_wrapper(self) -> AsyncIterator[None]:
        try:
            yield
        except LoginError as ex:
            async with self._async_wrap_lock:
                self._async_handle_offline(ex)
            raise
        except AmcrestError:
            async with self._async_wrap_lock:
                self._async_handle_error()
            raise
        async with self._async_wrap_lock:
            self._async_set_online()

    def _handle_offline_thread_safe(self, ex: Exception) -> bool:
        """Handle camera offline status shared between threads and event loop.

        Returns if the camera was online as a bool.
        """
        with self._wrap_lock:
            was_online = self.available
            was_login_err = self._wrap_login_err
            self._wrap_login_err = True
        if not was_login_err:
            _LOGGER.error("%s camera offline: Login error: %s", self._wrap_name, ex)
        return was_online

    def _handle_offline(self, ex: Exception) -> None:
        """Handle camera offline status from a thread."""
        if self._handle_offline_thread_safe(ex):
            self._hass.loop.call_soon_threadsafe(self._async_start_recovery)

    @callback
    def _async_handle_offline(self, ex: Exception) -> None:
        if self._handle_offline_thread_safe(ex):
            self._async_start_recovery()

    def _handle_error_thread_safe(self) -> bool:
        """Handle camera error status shared between threads and event loop.

        Returns if the camera was online and is now offline as
        a bool.
        """
        with self._wrap_lock:
            was_online = self.available
            errs = self._wrap_errors = self._wrap_errors + 1
            offline = not self.available
        _LOGGER.debug("%s camera errs: %i", self._wrap_name, errs)
        return was_online and offline

    def _handle_error(self) -> None:
        """Handle camera error status from a thread."""
        if self._handle_error_thread_safe():
            _LOGGER.error("%s camera offline: Too many errors", self._wrap_name)
            self._hass.loop.call_soon_threadsafe(self._async_start_recovery)

    @callback
    def _async_handle_error(self) -> None:
        """Handle camera error status from the event loop."""
        if self._handle_error_thread_safe():
            _LOGGER.error("%s camera offline: Too many errors", self._wrap_name)
            self._async_start_recovery()

    def _set_online_thread_safe(self) -> bool:
        """Set camera online status shared between threads and event loop.

        Returns if the camera was offline as a bool.
        """
        with self._wrap_lock:
            was_offline = not self.available
            self._wrap_errors = 0
            self._wrap_login_err = False
        return was_offline

    def _set_online(self) -> None:
        """Set camera online status from a thread."""
        if self._set_online_thread_safe():
            self._hass.loop.call_soon_threadsafe(self._async_signal_online)

    @callback
    def _async_set_online(self) -> None:
        """Set camera online status from the event loop."""
        if self._set_online_thread_safe():
            self._async_signal_online()

    @callback
    def _async_signal_online(self) -> None:
        """Signal that camera is back online."""
        assert self._unsub_recheck is not None
        self._unsub_recheck()
        self._unsub_recheck = None
        _LOGGER.info("%s camera back online", self._wrap_name)  # Log informational message
        self.available_flag.set()
        self.async_available_flag.set()
        async_dispatcher_send(
            self._hass, service_signal(SERVICE_UPDATE, self._wrap_name)
        )



    async def _wrap_test_online(self, now: datetime) -> None:
        """Test if camera is back online."""
        _LOGGER.debug("Testing if %s back online", self._wrap_name)
        with suppress(AmcrestError):
            await self.async_current_time


def _monitor_events(
    hass: HomeAssistant,
    name: str,
    api: AmcrestChecker,
    event_codes: set[str],
) -> None:
    while True:
        api.available_flag.wait()
        try:
            for code, payload in api.event_actions("All"):
                event_data = {"camera": name, "event": code, "payload": payload}
                hass.bus.fire("amcrest", event_data)
                if code in event_codes:
                    signal = service_signal(SERVICE_EVENT, name, code)
                    start = any(
                        str(key).lower() == "action" and str(val).lower() == "start"
                        for key, val in payload.items()
                    )
                    _LOGGER.debug("Sending signal: '%s': %s", signal, start)
                    dispatcher_send(hass, signal, start)
        except AmcrestError as error:
            _LOGGER.warning(
                "Error while processing events from %s camera: %r", name, error
            )


def _start_event_monitor(
    hass: HomeAssistant,
    name: str,
    api: AmcrestChecker,
    event_codes: set[str],
) -> None:
    thread = threading.Thread(
        target=_monitor_events,
        name=f"Amcrest {name}",
        args=(hass, name, api, event_codes),
        daemon=True,
    )
    thread.start()


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Amcrest IP Camera component."""
    hass.data.setdefault(DATA_AMCREST, {DEVICES: {}, CAMERAS: []})

    for device in config[DOMAIN]:
        await _setup_device(hass, device, config)  # Moved logic into _setup_device helper function

    if not hass.data[DATA_AMCREST][DEVICES]:
        return False

    _register_services(hass)  # Moved services registration logic into _register_services helper function

    return True


async def _setup_device(hass: HomeAssistant, device: dict, config: ConfigType) -> None:
    """Set up a single Amcrest device."""
    name = device[CONF_NAME]
    username = device[CONF_USERNAME]
    password = device[CONF_PASSWORD]

    api = AmcrestChecker(
        hass, name, device[CONF_HOST], device[CONF_PORT], username, password
    )

    hass.data[DATA_AMCREST][DEVICES][name] = AmcrestDevice(
        api,
        _get_authentication(device, username, password),
        device[CONF_FFMPEG_ARGUMENTS],
        device[CONF_STREAM_SOURCE],
        RESOLUTION_LIST[device[CONF_RESOLUTION]],
        device.get(CONF_CONTROL_LIGHT),
    )

    await _load_platforms(hass, name, device, config)  # Calls another helper for platform setup
    _start_event_monitor(hass, name, api, _get_event_codes(device))  # Calls event monitor helper


def _get_authentication(device: dict, username: str, password: str) -> aiohttp.BasicAuth | None:
    """Get authentication for the device."""
    if device[CONF_AUTHENTICATION] == HTTP_BASIC_AUTHENTICATION:
        return aiohttp.BasicAuth(username, password)
    return None


async def _load_platforms(hass: HomeAssistant, name: str, device: dict, config: ConfigType) -> None:
    """Load necessary platforms for the device."""
    await hass.async_create_task(
        discovery.async_load_platform(hass, Platform.CAMERA, DOMAIN, {CONF_NAME: name}, config)
    )

    if binary_sensors := device.get(CONF_BINARY_SENSORS):
        await hass.async_create_task(
            discovery.async_load_platform(
                hass,
                Platform.BINARY_SENSOR,
                DOMAIN,
                {CONF_NAME: name, CONF_BINARY_SENSORS: binary_sensors},
                config,
            )
        )

    if sensors := device.get(CONF_SENSORS):
        await hass.async_create_task(
            discovery.async_load_platform(
                hass,
                Platform.SENSOR,
                DOMAIN,
                {CONF_NAME: name, CONF_SENSORS: sensors},
                config,
            )
        )

    if switches := device.get(CONF_SWITCHES):
        await hass.async_create_task(
            discovery.async_load_platform(
                hass,
                Platform.SWITCH,
                DOMAIN,
                {CONF_NAME: name, CONF_SWITCHES: switches},
                config,
            )
        )


def _get_event_codes(device: dict) -> set[str]:
    """Get event codes for the device."""
    if binary_sensors := device.get(CONF_BINARY_SENSORS):
        return {
            event_code
            for sensor in BINARY_SENSORS
            if sensor.key in binary_sensors
            and not sensor.should_poll
            and sensor.event_codes is not None
            for event_code in sensor.event_codes
        }
    return set()


def _register_services(hass: HomeAssistant) -> None:
    """Register services for the component."""
    for service, params in CAMERA_SERVICES.items():
        hass.services.async_register(
            DOMAIN, service, _create_service_handler(hass), params[0]
        )


def _create_service_handler(hass: HomeAssistant) -> Callable[[ServiceCall], Awaitable[None]]:
    """Create a service handler for the component."""

    async def async_service_handler(call: ServiceCall) -> None:
        args = [call.data[arg] for arg in CAMERA_SERVICES[call.service][2]]
        for entity_id in await _extract_entity_ids_from_service(hass, call):
            async_dispatcher_send(hass, service_signal(call.service, entity_id), *args)

    return async_service_handler


async def _extract_entity_ids_from_service(hass: HomeAssistant, call: ServiceCall) -> list[str]:
    """Extract entity IDs from a service call."""
    user = await _get_user(hass, call)

    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
        return [
            entity_id
            for entity_id in hass.data[DATA_AMCREST][CAMERAS]
            if _has_permission(user, entity_id)
        ]

    if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_NONE:
        return []

    call_ids = await async_extract_entity_ids(hass, call)
    return [
        entity_id
        for entity_id in hass.data[DATA_AMCREST][CAMERAS]
        if entity_id in call_ids and _has_permission(user, entity_id)
    ]


async def _get_user(hass: HomeAssistant, call: ServiceCall) -> User | None:
    """Get the user associated with a service call."""
    if call.context.user_id:
        user = await hass.auth.async_get_user(call.context.user_id)
        if user is None:
            raise UnknownUser(context=call.context)
        return user
    return None


def _has_permission(user: User | None, entity_id: str) -> bool:
    """Check if a user has permission to control an entity."""
    return not user or user.permissions.check_entity(entity_id, POLICY_CONTROL)


@dataclass
class AmcrestDevice:
    """Representation of a base Amcrest discovery device."""

    api: AmcrestChecker
    authentication: aiohttp.BasicAuth | None
    ffmpeg_arguments: list[str]
    stream_source: str
    resolution: int
    control_light: bool
    channel: int = 0

