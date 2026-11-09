"""Tests for Kospel binary sensors (connectivity and PPE.4 insufficient flow)."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# Mock homeassistant before importing integration modules.
class _HAModule:
    __path__ = []
    __file__ = ""
    __name__ = "homeassistant"
    __spec__ = None


_ha = _HAModule()
sys.modules["homeassistant"] = _ha
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()


class _FakeHomeAssistantError(Exception):
    """Stand-in for HomeAssistantError in tests."""


class _FakeConfigEntryNotReady(_FakeHomeAssistantError):
    """Stand-in for ConfigEntryNotReady (package __init__ imports it)."""


# Real exception classes: other modules of the package bind these at import time,
# and this file may be the first to import the package.
sys.modules["homeassistant.exceptions"] = SimpleNamespace(
    HomeAssistantError=_FakeHomeAssistantError,
    ConfigEntryNotReady=_FakeConfigEntryNotReady,
)
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.entity"] = MagicMock()
sys.modules["homeassistant.helpers.entity_platform"] = MagicMock()
sys.modules["homeassistant.helpers.update_coordinator"] = MagicMock()


def _device_info(**kwargs):
    return kwargs


sys.modules["homeassistant.helpers.entity"].DeviceInfo = _device_info


class _CoordinatorEntityBase:
    """Minimal CoordinatorEntity stand-in for testing."""

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @classmethod
    def __class_getitem__(cls, item):
        return cls


class _BinarySensorEntityBase:
    """Minimal BinarySensorEntity stand-in for testing."""



binary_sensor_mock = MagicMock()
binary_sensor_mock.BinarySensorEntity = _BinarySensorEntityBase
sys.modules["homeassistant.components.binary_sensor"] = binary_sensor_mock
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = (
    _CoordinatorEntityBase
)

from custom_components.kospel_ppe4_custom.binary_sensor import (
    InsufficientFlowSensor,
    async_setup_entry,
)
from custom_components.kospel_ppe4_custom.const import DOMAIN


@pytest.fixture
def mock_entry():
    """Config entry with stable entry_id for unique_id."""
    entry = MagicMock()
    entry.data = {}
    entry.entry_id = "test-entry-id"
    return entry


@pytest.fixture
def mock_coordinator(mock_entry):
    """Mock coordinator with configurable controller data."""
    coordinator = MagicMock()
    coordinator.entry = mock_entry
    coordinator.last_update_success = True
    coordinator.communication_ok = True
    return coordinator


class TestInsufficientFlowSensor:
    """Tests for the PPE.4 insufficient flow binary sensor."""

    @pytest.mark.parametrize("flag", [True, False, None])
    def test_is_on_follows_controller_flag(
        self, mock_coordinator, mock_entry, flag
    ) -> None:
        mock_controller = MagicMock()
        mock_controller.insufficient_flow = flag
        mock_coordinator.data = mock_controller

        entity = InsufficientFlowSensor(mock_coordinator, mock_entry)
        assert entity.is_on is flag

    def test_unavailable_when_communication_lost(
        self, mock_coordinator, mock_entry
    ) -> None:
        mock_coordinator.communication_ok = False
        entity = InsufficientFlowSensor(mock_coordinator, mock_entry)
        assert entity.available is False

        mock_coordinator.communication_ok = True
        assert entity.available is True

    def test_translation_key_and_unique_id(
        self, mock_coordinator, mock_entry
    ) -> None:
        entity = InsufficientFlowSensor(mock_coordinator, mock_entry)
        assert entity._attr_translation_key == "insufficient_flow"
        assert entity._attr_unique_id.endswith("_insufficient_flow")


class TestAsyncSetupEntry:
    """The low flow sensor is only added for devices that expose the flag."""

    @staticmethod
    def _hass(mock_coordinator, mock_entry):
        hass = MagicMock()
        hass.data = {DOMAIN: {mock_entry.entry_id: mock_coordinator}}
        return hass

    @pytest.mark.asyncio
    async def test_ppe4_gets_connectivity_and_insufficient_flow(
        self, mock_coordinator, mock_entry
    ) -> None:
        mock_coordinator.data = MagicMock()  # has insufficient_flow
        added = []

        await async_setup_entry(
            self._hass(mock_coordinator, mock_entry), mock_entry, added.extend
        )

        assert len(added) == 2
        assert any(isinstance(e, InsufficientFlowSensor) for e in added)

    @pytest.mark.asyncio
    async def test_device_without_flag_gets_only_connectivity(
        self, mock_coordinator, mock_entry
    ) -> None:
        mock_coordinator.data = MagicMock(spec=[])  # no insufficient_flow
        added = []

        await async_setup_entry(
            self._hass(mock_coordinator, mock_entry), mock_entry, added.extend
        )

        assert len(added) == 1
        assert not any(isinstance(e, InsufficientFlowSensor) for e in added)
