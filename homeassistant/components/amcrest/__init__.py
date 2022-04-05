"""Support for Amcrest IP cameras."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
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
    EVENT_HOMEASSISTANT_STOP,
    HTTP_BASIC_AUTHENTICATION,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import Unauthorized, UnknownUser
from homeassistant.helpers import discovery
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
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
        self._wrap_lock = asyncio.Lock()
        self._wrap_login_err = False
        self._wrap_event_flag = asyncio.Event()
        self._wrap_event_flag.set()
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
    def available_flag(self) -> asyncio.Event:
        """Return event flag that indicates if camera's API is responding."""
        return self._wrap_event_flag

    def _start_recovery(self) -> None:
        self._wrap_event_flag.clear()
        async_dispatcher_send(
            self._hass, service_signal(SERVICE_UPDATE, self._wrap_name)
        )
        self._unsub_recheck = async_track_time_interval(
            self._hass, self._wrap_test_online, RECHECK_INTERVAL
        )

    async def async_command(self, *args: Any, **kwargs: Any) -> httpx.Response:
        """amcrest.ApiWrapper.command wrapper to catch errors."""
        async with self._command_wrapper():
            ret = await super().async_command(*args, **kwargs)
        return ret

    @asynccontextmanager
    async def async_stream_command(
        self, *args: Any, **kwargs: Any
    ) -> AsyncIterator[httpx.Response]:
        """amcrest.ApiWrapper.command wrapper to catch errors."""
        async with self._command_wrapper():
            async with super().async_stream_command(*args, **kwargs) as ret:
                yield ret

    @asynccontextmanager
    async def _command_wrapper(self) -> AsyncIterator[None]:
        try:
            yield
        except LoginError as ex:
            async with self._wrap_lock:
                was_online = self.available
                was_login_err = self._wrap_login_err
                self._wrap_login_err = True
            if not was_login_err:
                _LOGGER.error("%s camera offline: Login error: %s", self._wrap_name, ex)
            if was_online:
                self._start_recovery()
            raise
        except AmcrestError:
            async with self._wrap_lock:
                was_online = self.available
                errs = self._wrap_errors = self._wrap_errors + 1
                offline = not self.available
            _LOGGER.debug("%s camera errs: %i", self._wrap_name, errs)
            if was_online and offline:
                _LOGGER.error("%s camera offline: Too many errors", self._wrap_name)
                self._start_recovery()
            raise
        async with self._wrap_lock:
            was_offline = not self.available
            self._wrap_errors = 0
            self._wrap_login_err = False
        if was_offline:
            assert self._unsub_recheck is not None
            self._unsub_recheck()
            self._unsub_recheck = None
            _LOGGER.error("%s camera back online", self._wrap_name)
            self._wrap_event_flag.set()
            async_dispatcher_send(
                self._hass, service_signal(SERVICE_UPDATE, self._wrap_name)
            )

    async def _wrap_test_online(self, now: datetime) -> None:
        """Test if camera is back online."""
        _LOGGER.debug("Testing if %s back online", self._wrap_name)
        with suppress(AmcrestError):
            await self.async_current_time


async def _monitor_events(
    hass: HomeAssistant,
    name: str,
    api: AmcrestChecker,
    event_codes: set[str],
) -> None:
    while True:
        await api.available_flag.wait()
        try:
            async for code, payload in api.async_event_actions("All"):
                event_data = {"camera": name, "event": code, "payload": payload}
                hass.bus.async_fire("amcrest", event_data)
                if code in event_codes:
                    signal = service_signal(SERVICE_EVENT, name, code)
                    start = any(
                        str(key).lower() == "action" and str(val).lower() == "start"
                        for key, val in payload.items()
                    )
                    _LOGGER.debug("Sending signal: '%s': %s", signal, start)
                    async_dispatcher_send(hass, signal, start)
        except AmcrestError as error:
            _LOGGER.warning(
                "Error while processing events from %s camera: %r", name, error
            )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Amcrest IP Camera component."""
    hass.data.setdefault(DATA_AMCREST, {DEVICES: {}, CAMERAS: []})

    monitor_tasks = []
    for device in config[DOMAIN]:
        name: str = device[CONF_NAME]
        username: str = device[CONF_USERNAME]
        password: str = device[CONF_PASSWORD]

        api = AmcrestChecker(
            hass, name, device[CONF_HOST], device[CONF_PORT], username, password
        )

        ffmpeg_arguments = device[CONF_FFMPEG_ARGUMENTS]
        resolution = RESOLUTION_LIST[device[CONF_RESOLUTION]]
        binary_sensors = device.get(CONF_BINARY_SENSORS)
        sensors = device.get(CONF_SENSORS)
        switches = device.get(CONF_SWITCHES)
        stream_source = device[CONF_STREAM_SOURCE]
        control_light = device.get(CONF_CONTROL_LIGHT)

        # currently aiohttp only works with basic authentication
        # only valid for mjpeg streaming
        if device[CONF_AUTHENTICATION] == HTTP_BASIC_AUTHENTICATION:
            authentication: aiohttp.BasicAuth | None = aiohttp.BasicAuth(
                username, password
            )
        else:
            authentication = None

        hass.data[DATA_AMCREST][DEVICES][name] = AmcrestDevice(
            api,
            authentication,
            ffmpeg_arguments,
            stream_source,
            resolution,
            control_light,
        )

        hass.async_create_task(
            discovery.async_load_platform(
                hass, Platform.CAMERA, DOMAIN, {CONF_NAME: name}, config
            )
        )

        event_codes = set()
        if binary_sensors:
            hass.async_create_task(
                discovery.async_load_platform(
                    hass,
                    Platform.BINARY_SENSOR,
                    DOMAIN,
                    {CONF_NAME: name, CONF_BINARY_SENSORS: binary_sensors},
                    config,
                )
            )
            event_codes = {
                sensor.event_code
                for sensor in BINARY_SENSORS
                if sensor.key in binary_sensors
                and not sensor.should_poll
                and sensor.event_code is not None
            }

        monitor_tasks.append(
            asyncio.create_task(_monitor_events(hass, name, api, event_codes))
        )

        if sensors:
            hass.async_create_task(
                discovery.async_load_platform(
                    hass,
                    Platform.SENSOR,
                    DOMAIN,
                    {CONF_NAME: name, CONF_SENSORS: sensors},
                    config,
                )
            )

        if switches:
            hass.async_create_task(
                discovery.async_load_platform(
                    hass,
                    Platform.SWITCH,
                    DOMAIN,
                    {CONF_NAME: name, CONF_SWITCHES: switches},
                    config,
                )
            )

    @callback
    def cancel_monitors(event: Event) -> None:
        for monitor_task in monitor_tasks:
            monitor_task.cancel()

    hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, cancel_monitors)

    if not hass.data[DATA_AMCREST][DEVICES]:
        return False

    def have_permission(user: User | None, entity_id: str) -> bool:
        return not user or user.permissions.check_entity(entity_id, POLICY_CONTROL)

    async def async_extract_from_service(call: ServiceCall) -> list[str]:
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None:
                raise UnknownUser(context=call.context)
        else:
            user = None

        if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_ALL:
            # Return all entity_ids user has permission to control.
            return [
                entity_id
                for entity_id in hass.data[DATA_AMCREST][CAMERAS]
                if have_permission(user, entity_id)
            ]

        if call.data.get(ATTR_ENTITY_ID) == ENTITY_MATCH_NONE:
            return []

        call_ids = await async_extract_entity_ids(hass, call)
        entity_ids = []
        for entity_id in hass.data[DATA_AMCREST][CAMERAS]:
            if entity_id not in call_ids:
                continue
            if not have_permission(user, entity_id):
                raise Unauthorized(
                    context=call.context, entity_id=entity_id, permission=POLICY_CONTROL
                )
            entity_ids.append(entity_id)
        return entity_ids

    async def async_service_handler(call: ServiceCall) -> None:
        args = []
        for arg in CAMERA_SERVICES[call.service][2]:
            args.append(call.data[arg])
        for entity_id in await async_extract_from_service(call):
            async_dispatcher_send(hass, service_signal(call.service, entity_id), *args)

    for service, params in CAMERA_SERVICES.items():
        hass.services.async_register(DOMAIN, service, async_service_handler, params[0])

    return True


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