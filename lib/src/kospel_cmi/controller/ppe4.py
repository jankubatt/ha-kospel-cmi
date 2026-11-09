"""
Device controller for Kospel PPE.4 instantaneous water heater (MODBUS-over-HTTP).
"""

import logging
import time
from typing import Any, ClassVar, Optional

from ..exceptions import (
    IncompleteRegisterRefreshError,
    KospelConnectionError,
    RegisterMissingError,
)
from ..kospel.backend import ModbusBackend
from ..registers.enums import HeatingStatus

logger = logging.getLogger(__name__)

# Register addresses (confirmed on PPE.4)
REG_STATUS_FLAGS = 1129  # observed on PPE.4; bit 0x02 = insufficient flow
REG_TEMP_INLET = 1134
REG_TEMP_OUTLET = 1135
REG_TEMP_INTERNAL = 1136
REG_POWER_W = 1138
REG_FLOW = 1139
REG_TARGET_TEMP = 1140
REG_INSTALLED_POWER_W = 1143
REG_BOILER_MAX_POWER_W = 1144
# Volume counters in ml. Observed on PPE.4 with a 6.5 min draw at ~1.2 L/min:
# 1162 counts up from 0 while water flows, including while the heater is
# below its minimum flow and reports flow (1139) as 0. When the draw ends it
# resets to 0 and 1164 keeps the finished draw's total. The high words
# (1163, 1165) are assumed by analogy with the other 32-bit counters; they
# were always 0 in the captured data.
REG_DRAW_VOLUME_LO = 1162
REG_DRAW_VOLUME_HI = 1163
REG_LAST_DRAW_VOLUME_LO = 1164
REG_LAST_DRAW_VOLUME_HI = 1165
REG_SERIAL_SUFFIX = 1276
REG_MAX_TEMP = 1388
REG_ACTIVE_PROFILE = 1390
REG_MANUAL_SETPOINT = 1391
REG_PROFILE_BASE = 1392  # 1392=Profile 1, 1393=Profile 2, 1394=Profile 3
REG_ENERGY_TODAY_LO = 1450
REG_ENERGY_TODAY_HI = 1451
REG_WATER_TODAY_LO = 1578
REG_WATER_TODAY_HI = 1579

# Poll blocks: (start_register, count)
_POLL_BLOCKS: tuple[tuple[int, int], ...] = (
    (1130, 30),  # Live sensors, target, power, RTC
    (1388, 8),   # Max temp, active profile, manual setpoint, profiles 1..3
    (1448, 4),   # Daily energy counter block (must start at 1448)
    (1576, 4),   # Daily water counter block (must start at 1576)
)

# Optional blocks are read after the required ones. If the firmware refuses
# them, the rest of the device keeps working and the dependent properties
# return None.
_OPTIONAL_POLL_BLOCKS: tuple[tuple[int, int], ...] = (
    (REG_STATUS_FLAGS, 1),  # Status flag word
    (REG_DRAW_VOLUME_LO, 4),  # Current and last draw volume (32-bit pairs)
)

# Bit in REG_STATUS_FLAGS set while the heater is not heating because the water
# flow is below its minimum (the LCD shows "insufficient flow"). Idle word is
# 0x11; it became 0x13 when heating stopped at ~1.2 L/min and returned to 0x11
# when the draw ended.
FLAG_INSUFFICIENT_FLOW = 0x0002

_PPE4_REQUIRED_REGISTERS: frozenset[int] = frozenset(
    {
        REG_TEMP_INLET,
        REG_TEMP_OUTLET,
        REG_TEMP_INTERNAL,
        REG_POWER_W,
        REG_FLOW,
        REG_TARGET_TEMP,
        REG_INSTALLED_POWER_W,
        REG_BOILER_MAX_POWER_W,
        REG_MAX_TEMP,
        REG_ACTIVE_PROFILE,
        REG_MANUAL_SETPOINT,
        1392,
        1393,
        1394,
        REG_ENERGY_TODAY_LO,
        REG_ENERGY_TODAY_HI,
        REG_WATER_TODAY_LO,
        REG_WATER_TODAY_HI,
    }
)

_UNUSED_MARKERS: frozenset[int] = frozenset({0xFFFF, 0xAAAA})


class Ppe4:
    """Kospel PPE.4 instantaneous water heater controller."""

    REQUIRED_REGISTERS: ClassVar[frozenset[int]] = _PPE4_REQUIRED_REGISTERS
    PROFILE_COUNT: ClassVar[int] = 3

    def __init__(
        self,
        backend: ModbusBackend,
        *,
        strict_refresh: bool = False,
    ) -> None:
        self._backend = backend
        self._strict_refresh = strict_refresh
        self._registers: dict[int, int] = {}
        self._optional_blocks_warned = False
        self._clock = time.monotonic
        # (monotonic time, draw volume in ml) from the previous refresh
        self._volume_sample: Optional[tuple[float, int]] = None
        self._measured_flow: Optional[float] = None

    @property
    def water_heater_writable(self) -> bool:
        """PPE.4 supports direct target temperature and profile writes."""
        return True

    @property
    def water_min_temperature(self) -> float:
        """Minimum supported water temperature (°C)."""
        return 20.0

    @property
    def water_max_temperature(self) -> float:
        """Maximum supported water temperature (°C), dynamic from register 1388."""
        try:
            max_temp = self.max_temperature
            if max_temp is not None and max_temp > 0:
                return max_temp
        except RegisterMissingError:
            pass
        return 60.0

    def _get_register(self, register: int) -> int:
        if register not in self._registers:
            raise RegisterMissingError(
                str(register),
                detail="not in cache; call refresh() or load via from_registers()",
            )
        return self._registers[register]

    def _decode_deci(self, register: int) -> Optional[float]:
        raw = self._get_register(register)
        if raw in _UNUSED_MARKERS:
            return None
        return raw / 10.0

    def _decode_u32(self, reg_lo: int, reg_hi: int) -> Optional[int]:
        lo = self._get_register(reg_lo)
        hi = self._get_register(reg_hi)
        if lo in _UNUSED_MARKERS and hi in _UNUSED_MARKERS:
            return None
        return (hi << 16) | lo

    def _decode_optional_u32(self, reg_lo: int, reg_hi: int) -> Optional[int]:
        """Decode a 32-bit pair from optional registers; None if the low word is absent.

        A missing or unused high word counts as 0 so that a firmware that only
        exposes the low word still works.
        """
        lo = self._registers.get(reg_lo)
        if lo is None or lo in _UNUSED_MARKERS:
            return None
        hi = self._registers.get(reg_hi, 0)
        if hi in _UNUSED_MARKERS:
            hi = 0
        return (hi << 16) | lo

    async def refresh(self) -> None:
        """Read all PPE.4 register blocks from the backend."""
        logger.info("Refreshing PPE.4 settings from backend")
        merged: dict[int, int] = {}
        for start, count in _POLL_BLOCKS:
            block = await self._backend.read_registers(start, count)
            merged.update(block)

        for start, count in _OPTIONAL_POLL_BLOCKS:
            try:
                block = await self._backend.read_registers(start, count)
            except KospelConnectionError as err:
                if not self._optional_blocks_warned:
                    self._optional_blocks_warned = True
                    logger.warning(
                        "PPE.4 optional registers %s..%s unavailable, "
                        "continuing without them: %s",
                        start,
                        start + count - 1,
                        err,
                    )
                continue
            merged.update(block)

        if self._strict_refresh:
            present = frozenset(merged.keys())
            required = type(self).REQUIRED_REGISTERS
            if not required <= present:
                missing_strs = frozenset(str(r) for r in (required - present))
                raise IncompleteRegisterRefreshError(
                    missing_registers=missing_strs
                )
        self._registers = merged
        self._update_measured_flow(self._clock())
        logger.info("PPE.4 settings refreshed successfully")

    def _update_measured_flow(self, now: float) -> None:
        """Derive the real flow from the change of the draw volume counter.

        The result is the average flow since the previous refresh. A counter
        that went down means the draw ended (value 0) or a new one started
        since the previous refresh, so the new value is the volume since then.
        """
        volume = self.draw_volume_ml
        previous = self._volume_sample
        self._measured_flow = None
        if volume is None:
            self._volume_sample = None
            return
        if previous is not None:
            elapsed = now - previous[0]
            if elapsed > 0:
                delta_ml = volume - previous[1] if volume >= previous[1] else volume
                self._measured_flow = round(delta_ml / elapsed * 60.0 / 1000.0, 2)
        elif volume == 0:
            self._measured_flow = 0.0
        self._volume_sample = (now, volume)

    def from_registers(self, registers: dict[int, int]) -> None:
        """Load pre-fetched integer register data."""
        self._registers = dict(registers)

    async def aclose(self) -> None:
        """Release backend resources (idempotent)."""
        aclose = getattr(self._backend, "aclose", None)
        if aclose is not None:
            await aclose()

    async def __aenter__(self) -> "Ppe4":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    # --- Read-only properties ---

    @property
    def inlet_temperature(self) -> Optional[float]:
        """Cold water intake temperature (1134), °C."""
        return self._decode_deci(REG_TEMP_INLET)

    @property
    def outlet_temperature(self) -> Optional[float]:
        """Heated water outlet temperature (1135), °C."""
        return self._decode_deci(REG_TEMP_OUTLET)

    @property
    def water_current_temperature(self) -> Optional[float]:
        """Current hot water temperature (alias for outlet_temperature), °C."""
        return self.outlet_temperature

    @property
    def internal_temperature(self) -> Optional[float]:
        """Internal electronics temperature probe (1136), °C."""
        return self._decode_deci(REG_TEMP_INTERNAL)

    @property
    def power(self) -> Optional[float]:
        """Live electrical power draw (1138), converted from W to kW."""
        raw = self._get_register(REG_POWER_W)
        if raw in _UNUSED_MARKERS:
            return None
        return raw / 1000.0

    @property
    def dhw_heating_status(self) -> HeatingStatus:
        """DHW heating status: RUNNING when live power > 0, else IDLE."""
        power_val = self.power
        if power_val is not None and power_val > 0:
            return HeatingStatus.RUNNING
        return HeatingStatus.IDLE

    @property
    def flow(self) -> Optional[float]:
        """Water flow rate (1139), L/min."""
        return self._decode_deci(REG_FLOW)

    @property
    def measured_flow(self) -> Optional[float]:
        """Actual water flow (L/min) derived from the draw volume counter.

        Unlike :attr:`flow`, this keeps reporting while the heater is below its
        minimum flow and has stopped heating. It is the average over the time
        between the last two refreshes, so it is None until the second refresh.
        """
        return self._measured_flow

    @property
    def status_flags(self) -> Optional[int]:
        """Raw status flag word (1129), or None when unavailable."""
        raw = self._registers.get(REG_STATUS_FLAGS)
        if raw is None or raw in _UNUSED_MARKERS:
            return None
        return raw

    @property
    def insufficient_flow(self) -> Optional[bool]:
        """True while the heater is not heating because the flow is too low."""
        flags = self.status_flags
        if flags is None:
            return None
        return bool(flags & FLAG_INSUFFICIENT_FLOW)

    @property
    def draw_volume_ml(self) -> Optional[int]:
        """Volume of the draw in progress (1162..1163), ml; 0 when idle."""
        return self._decode_optional_u32(REG_DRAW_VOLUME_LO, REG_DRAW_VOLUME_HI)

    @property
    def last_draw_volume_liters(self) -> Optional[float]:
        """Total volume of the last finished draw (1164..1165), L."""
        ml = self._decode_optional_u32(
            REG_LAST_DRAW_VOLUME_LO, REG_LAST_DRAW_VOLUME_HI
        )
        return None if ml is None else ml / 1000.0

    @property
    def target_temperature(self) -> Optional[float]:
        """Effective target temperature the heater aims for (1140), °C."""
        return self._decode_deci(REG_TARGET_TEMP)

    @property
    def supply_setpoint(self) -> Optional[float]:
        """Alias for target_temperature to satisfy WaterHeaterController."""
        return self.target_temperature

    @property
    def installed_power_kw(self) -> Optional[float]:
        """Hardware nameplate power (1143), converted from W to kW."""
        raw = self._get_register(REG_INSTALLED_POWER_W)
        if raw in _UNUSED_MARKERS:
            return None
        return raw / 1000.0

    @property
    def boiler_max_power_kw(self) -> Optional[float]:
        """Configured heater max power limit (1144), converted from W to kW."""
        raw = self._get_register(REG_BOILER_MAX_POWER_W)
        if raw in _UNUSED_MARKERS:
            return None
        return raw / 1000.0

    @property
    def max_temperature(self) -> Optional[float]:
        """Configured maximum temperature limit (1388), °C."""
        return self._decode_deci(REG_MAX_TEMP)

    @property
    def active_profile(self) -> Optional[int]:
        """Active operating profile (1390): 0=Manual, 1..3=Profile 1..3."""
        raw = self._get_register(REG_ACTIVE_PROFILE)
        if raw in _UNUSED_MARKERS or not (0 <= raw <= self.PROFILE_COUNT):
            return None
        return raw

    @property
    def manual_temperature(self) -> Optional[float]:
        """Manual setpoint temperature (1391), °C."""
        return self._decode_deci(REG_MANUAL_SETPOINT)

    def get_profile_temperature(self, profile: int) -> Optional[float]:
        """Return saved preset temperature (°C) for profile 1..3."""
        if not 1 <= profile <= self.PROFILE_COUNT:
            raise ValueError(
                f"profile must be between 1 and {self.PROFILE_COUNT}, got {profile}"
            )
        return self._decode_deci(REG_PROFILE_BASE + profile - 1)

    @property
    def energy_today_kwh(self) -> Optional[float]:
        """Daily energy consumption counter (1450..1451), kWh."""
        wh = self._decode_u32(REG_ENERGY_TODAY_LO, REG_ENERGY_TODAY_HI)
        return None if wh is None else wh / 1000.0

    @property
    def water_today_liters(self) -> Optional[float]:
        """Daily water consumption counter (1578..1579), L."""
        deci_liters = self._decode_u32(REG_WATER_TODAY_LO, REG_WATER_TODAY_HI)
        return None if deci_liters is None else deci_liters / 10.0

    @property
    def serial_number(self) -> Optional[str]:
        """Numeric serial suffix from register 1276 formatted as 8 digits."""
        raw = self._get_register(REG_SERIAL_SUFFIX)
        if raw in _UNUSED_MARKERS:
            return None
        return f"ppe4_{raw:08d}"

    # --- Async setters ---

    async def set_active_profile(self, profile: int) -> None:
        """Select operating profile: 0=Manual, 1..3=Profile 1..3."""
        idx = int(profile)
        if not 0 <= idx <= self.PROFILE_COUNT:
            raise ValueError(
                f"profile must be between 0 and {self.PROFILE_COUNT}, got {idx}"
            )
        await self._backend.write_registers({REG_ACTIVE_PROFILE: idx})
        self._registers[REG_ACTIVE_PROFILE] = idx

    async def set_manual_temperature(self, value: float) -> None:
        """Write manual temperature setpoint (1391) in °C."""
        encoded = int(round(value * 10))
        await self._backend.write_registers({REG_MANUAL_SETPOINT: encoded})
        self._registers[REG_MANUAL_SETPOINT] = encoded

    async def set_manual_heating(self, temperature: float) -> None:
        """Switch to Manual mode (1390=0) and set manual target (1391) atomically."""
        encoded = int(round(temperature * 10))
        updates = {REG_ACTIVE_PROFILE: 0, REG_MANUAL_SETPOINT: encoded}
        await self._backend.write_registers(updates)
        self._registers.update(updates)

    async def set_profile_temperature(self, profile: int, value: float) -> None:
        """Write saved preset temperature (°C) for profile 1..3."""
        if not 1 <= profile <= self.PROFILE_COUNT:
            raise ValueError(
                f"profile must be between 1 and {self.PROFILE_COUNT}, got {profile}"
            )
        reg = REG_PROFILE_BASE + profile - 1
        encoded = int(round(value * 10))
        await self._backend.write_registers({reg: encoded})
        self._registers[reg] = encoded

    def get_setting(self, name: str) -> Any:
        """Get a setting by name for compatibility with getattr."""
        return getattr(self, name, None)
