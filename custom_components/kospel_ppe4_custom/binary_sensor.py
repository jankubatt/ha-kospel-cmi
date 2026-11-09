"""Binary sensor entities for Kospel integration (connectivity, low flow)."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, get_device_identifier, get_device_info
from .coordinator import KospelDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Kospel binary sensor entities."""
    coordinator: KospelDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [ConnectivitySensor(coordinator, entry)]
    if hasattr(coordinator.data, "insufficient_flow"):
        # Instantaneous water heater (PPE.4)
        entities.append(InsufficientFlowSensor(coordinator, entry))
    async_add_entities(entities)


class ConnectivitySensor(
    CoordinatorEntity[KospelDataUpdateCoordinator], BinarySensorEntity
):
    """Hub connectivity: on when communication is OK (debounced failed polls)."""

    _attr_has_entity_name = True
    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: KospelDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the connectivity binary sensor."""
        super().__init__(coordinator)
        device_id = get_device_identifier(entry)
        self._attr_unique_id = f"{device_id}_connectivity"
        self._attr_device_info = get_device_info(entry)

    @property
    def is_on(self) -> bool | None:
        """Return True when the heater is reachable within debounce threshold."""
        return self.coordinator.communication_ok

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class InsufficientFlowSensor(
    CoordinatorEntity[KospelDataUpdateCoordinator], BinarySensorEntity
):
    """On while the heater is not heating because the water flow is too low.

    This is the "insufficient flow" message shown on the heater's LCD.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "insufficient_flow"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self,
        coordinator: KospelDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the insufficient flow binary sensor."""
        super().__init__(coordinator)
        device_id = get_device_identifier(entry)
        self._attr_unique_id = f"{device_id}_insufficient_flow"
        self._attr_device_info = get_device_info(entry)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.communication_ok

    @property
    def is_on(self) -> bool | None:
        """Return True while the heater reports insufficient flow."""
        return getattr(self.coordinator.data, "insufficient_flow", None)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
