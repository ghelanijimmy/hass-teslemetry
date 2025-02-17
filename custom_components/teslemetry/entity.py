"""Teslemetry parent entity class."""

import asyncio
from typing import Any

from tesla_fleet_api import EnergySpecific, VehicleSpecific
from tesla_fleet_api.exceptions import TeslaFleetError
from tesla_fleet_api.const import TelemetryField

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, TeslemetryState, TeslemetryTimestamp
from .coordinator import (
    TeslemetryEnergySiteInfoCoordinator,
    TeslemetryEnergySiteLiveCoordinator,
    TeslemetryVehicleDataCoordinator,
)
from .models import TeslemetryEnergyData, TeslemetryVehicleData
from .helpers import wake_up_vehicle, handle_command


class TeslemetryVehicleStreamEntity:
    """Parent class for Teslemetry Vehicle Stream entities."""

    _attr_has_entity_name = True

    def __init__(
        self, data: TeslemetryVehicleData, streaming_key: TelemetryField
    ) -> None:
        """Initialize common aspects of a Teslemetry entity."""
        self.streaming_key = streaming_key

        self._attr_translation_key = f"stream_{streaming_key.lower()}"
        self.stream = data.stream
        self.vin = data.vin

        self._attr_unique_id = f"{data.vin}-stream_{streaming_key.lower()}"
        self._attr_device_info = data.device

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        if self.stream.server:
            self.async_on_remove(
                self.stream.async_add_listener(
                    self._handle_stream_update,
                    {"vin": self.vin, "data": {self.streaming_key: None}},
                )
            )

    def _handle_stream_update(self, data: dict[str, Any]) -> None:
        """Handle updated data from the stream."""
        self._async_value_from_stream(data["data"][self.streaming_key])
        self.async_write_ha_state()


class TeslemetryEntity(
    CoordinatorEntity[
        TeslemetryVehicleDataCoordinator
        | TeslemetryEnergySiteLiveCoordinator
        | TeslemetryEnergySiteInfoCoordinator
    ]
):
    """Parent class for all Teslemetry entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TeslemetryVehicleDataCoordinator
        | TeslemetryEnergySiteLiveCoordinator
        | TeslemetryEnergySiteInfoCoordinator,
        api: VehicleSpecific | EnergySpecific,
        key: str,
    ) -> None:
        """Initialize common aspects of a Teslemetry entity."""
        super().__init__(coordinator)
        self.api = api
        self.key = key
        self._attr_translation_key = self.key
        self._async_update_attrs()

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self.coordinator.last_update_success and self._attr_available

    @property
    def _value(self) -> Any | None:
        """Return a specific value from coordinator data."""
        return self.coordinator.data.get(self.key)

    def get(self, key: str, default: Any | None = None) -> Any | None:
        """Return a specific value from coordinator data."""
        return self.coordinator.data.get(key, default)

    def exactly(self, value: Any, key: str | None = None) -> bool | None:
        """Return if a key exactly matches the valug but retain None."""
        key = key or self.key
        if value is None:
            return self.get(key, False) is None
        current = self.get(key)
        if current is None:
            return None
        return current == value

    def has(self, key: str | None = None) -> bool:
        """Return True if a specific value is in coordinator data."""
        return (key or self.key) in self.coordinator.data

    def raise_for_scope(self):
        """Raise an error if a scope is not available."""
        if not self.scoped:
            raise ServiceValidationError(
                f"Missing required scope: {' or '.join(self.entity_description.scopes)}"
            )

    async def handle_command(self, command) -> dict[str, Any]:
        """Handle a command."""
        return await handle_command(command)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._async_update_attrs()
        self.async_write_ha_state()

    def _async_update_attrs(self) -> None:
        """Update the attributes of the entity."""
        raise NotImplementedError()


class TeslemetryVehicleEntity(TeslemetryEntity):
    """Parent class for Teslemetry Vehicle entities."""

    _last_update: int = 0

    def __init__(
        self,
        data: TeslemetryVehicleData,
        key: str,
        timestamp_key: TeslemetryTimestamp | None = None,
        streaming_key: TelemetryField | None = None,
    ) -> None:
        """Initialize common aspects of a Teslemetry entity."""
        self.timestamp_key = timestamp_key
        self.streaming_key = streaming_key
        self.stream = data.stream
        self.vin = data.vin

        self._attr_unique_id = f"{data.vin}-{key}"
        self.wakelock = data.wakelock

        self._attr_device_info = data.device
        super().__init__(data.coordinator, data.api, key)

    async def async_added_to_hass(self) -> None:
        """When entity is added to hass."""
        await super().async_added_to_hass()
        if self.stream.server and self.streaming_key:
            self.async_on_remove(
                self.stream.async_add_listener(
                    self._handle_stream_update,
                    {"vin": self.vin, "data": {self.streaming_key: None}},
                )
            )

    def _handle_stream_update(self, data: dict[str, Any]) -> None:
        """Handle updated data from the stream."""
        if data["timestamp"] < self._last_update:
            LOGGER.warning(
                "Streaming data of %s was %s seconds older than polling data",
                self.name,
                self._last_update - data["timestamp"] / 1000,
            )
            return
        self._last_update = data["timestamp"]
        self._async_value_from_stream(data["data"][self.streaming_key])
        self.async_write_ha_state()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        timestamp = self.timestamp_key and self.get(self.timestamp_key)
        if not timestamp:
            self._async_update_attrs()
            self.async_write_ha_state()
        elif timestamp > self._last_update:
            self._last_update = timestamp
            self._async_update_attrs()
            self.async_write_ha_state()
        elif timestamp < self._last_update:
            LOGGER.debug(
                "Skipping update of %s, new timestamp is %s older",
                self.name,
                self._last_update - timestamp / 1000,
            )

    async def wake_up_if_asleep(self) -> None:
        """Wake up the vehicle if its asleep."""
        await wake_up_vehicle(self)

    async def handle_command(self, command) -> dict[str, Any]:
        """Handle a vehicle command."""
        result = await super().handle_command(command)
        if (response := result.get("response")) is None:
            if message := result.get("error"):
                # No response with error
                LOGGER.info("Command failure: %s", message)
                raise ServiceValidationError(message)
            # No response without error (unexpected)
            LOGGER.error("Unknown response: %s", response)
            raise ServiceValidationError("Unknown response")
        if (message := response.get("result")) is not True:
            if message := response.get("reason"):
                # Result of false with reason
                LOGGER.info("Command failure: %s", message)
                raise ServiceValidationError(message)
            # Result of false without reason (unexpected)
            LOGGER.error("Unknown response: %s", response)
            raise ServiceValidationError("Unknown response")
        # Response with result of true
        return result


class TeslemetryEnergyLiveEntity(TeslemetryEntity):
    """Parent class for Teslemetry Energy Site Live entities."""

    def __init__(
        self,
        data: TeslemetryEnergyData,
        key: str,
    ) -> None:
        """Initialize common aspects of a Teslemetry Energy Site Live entity."""
        self._attr_unique_id = f"{data.id}-{key}"
        self._attr_device_info = data.device

        super().__init__(data.live_coordinator, data.api, key)


class TeslemetryEnergyInfoEntity(TeslemetryEntity):
    """Parent class for Teslemetry Energy Site Info Entities."""

    def __init__(
        self,
        data: TeslemetryEnergyData,
        key: str,
    ) -> None:
        """Initialize common aspects of a Teslemetry Energy Site Info entity."""
        self._attr_unique_id = f"{data.id}-{key}"
        self._attr_device_info = data.device

        super().__init__(data.info_coordinator, data.api, key)


class TeslemetryWallConnectorEntity(
    TeslemetryEntity, CoordinatorEntity[TeslemetryEnergySiteLiveCoordinator]
):
    """Parent class for Teslemetry Wall Connector Entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        data: TeslemetryEnergyData,
        din: str,
        key: str,
    ) -> None:
        """Initialize common aspects of a Teslemetry entity."""
        self.din = din
        self._attr_unique_id = f"{data.id}-{din}-{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, din)},
            manufacturer="Tesla",
            configuration_url="https://teslemetry.com/console",
            name="Wall Connector",
            via_device=(DOMAIN, str(data.id)),
            serial_number=din.split("-")[-1],
        )

        super().__init__(data.live_coordinator, data.api, key)

    @property
    def _value(self) -> int:
        """Return a specific wall connector value from coordinator data."""
        return (
            self.coordinator.data.get("wall_connectors", {})
            .get(self.din, {})
            .get(self.key)
        )
