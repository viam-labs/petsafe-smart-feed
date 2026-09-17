import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Self

import petsafe as sf
from viam.components.generic import Generic
from viam.proto.app.robot import ComponentConfig
from viam.proto.common import ResourceName
from viam.resource.base import ResourceBase
from viam.resource.registry import Registry, ResourceCreatorRegistration
from viam.resource.types import Model, ModelFamily
from viam.utils import struct_to_dict

LOGGER = logging.getLogger(__name__)

# PetSafe locks accounts that read data more than once per 5 minutes.
# Every read path must go through the cached status method.
STATUS_CACHE_TTL_SEC = 300

# The petsafe library counts feed amount in 1/8-cup increments; the
# smallest possible dispense is 1 (= 1/8 cup).
EIGHTHS_PER_CUP = 8


class PetSafeFeeder(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "petsafe"), "smart-feed")

    TOKEN_KEYS: ClassVar[tuple[str, ...]] = ("id_token", "refresh_token", "access_token")

    email: str
    feeder_id: str | None = None
    target_meal_cups: float | None = None
    _tokens: dict | None = None
    _client: sf.PetSafeClient | None = None
    _feeder: Any | None = None
    # True right after client.get_feeders() returns; lets us skip an
    # update_data() call on the very first status refresh since the
    # feeder's data is already fresh from the get_feeders response.
    _feeder_data_fresh: bool = False
    _status_cache: dict | None = None
    _status_cache_expires: float = 0.0
    _status_lock: asyncio.Lock | None = None
    _schedule_cache: list | None = None
    _schedule_cache_expires: float = 0.0
    _schedule_lock: asyncio.Lock | None = None

    @classmethod
    def new(
        cls,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> Self:
        instance = cls(config.name)
        instance.reconfigure(config, dependencies)
        return instance

    @classmethod
    def validate(cls, config: ComponentConfig) -> Sequence[str]:
        attrs = struct_to_dict(config.attributes)
        if not attrs.get("email"):
            raise ValueError("`email` attribute is required")
        tokens = attrs.get("tokens")
        if not isinstance(tokens, dict):
            raise ValueError("`tokens` attribute is required and must be an object")
        for key in cls.TOKEN_KEYS:
            if not tokens.get(key):
                raise ValueError(f"`tokens.{key}` is required")
        target = attrs.get("target_meal_cups")
        if target is not None and (not isinstance(target, int | float) or target <= 0):
            raise ValueError("`target_meal_cups` must be a positive number if set")
        return []

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self.email = attrs["email"]
        self._tokens = attrs["tokens"]
        self.feeder_id = attrs.get("feeder_id")
        target = attrs.get("target_meal_cups")
        self.target_meal_cups = float(target) if target is not None else None
        # Bust caches so token or feeder changes take effect immediately.
        self._client = None
        self._feeder = None
        self._feeder_data_fresh = False
        self._status_cache = None
        self._status_cache_expires = 0.0
        self._status_lock = asyncio.Lock()
        self._schedule_cache = None
        self._schedule_cache_expires = 0.0
        self._schedule_lock = asyncio.Lock()

    def _get_client(self) -> sf.PetSafeClient:
        if self._client is None:
            assert self._tokens is not None
            self._client = sf.PetSafeClient(
                email=self.email,
                id_token=self._tokens["id_token"],
                refresh_token=self._tokens["refresh_token"],
                access_token=self._tokens["access_token"],
            )
        return self._client

    async def _resolve_feeder(self) -> Any:
        if self._feeder is not None:
            return self._feeder
        client = self._get_client()
        feeders = await client.get_feeders()
        if not feeders:
            raise RuntimeError("No PetSafe feeders found on this account.")
        if self.feeder_id is None:
            self._feeder = feeders[0]
        else:
            match = next(
                (f for f in feeders if getattr(f, "id", None) == self.feeder_id),
                None,
            )
            if match is None:
                raise RuntimeError(f"No feeder with id={self.feeder_id!r}.")
            self._feeder = match
        self._feeder_data_fresh = True
        return self._feeder

    async def _feed(self, cups: float, slow: bool) -> dict:
        eighths = max(1, round(cups * EIGHTHS_PER_CUP))
        feeder = await self._resolve_feeder()
        # update_data=False so we don't burn a read call after every
        # feed; the status cache TTL is what decides when to refresh.
        await feeder.feed(amount=eighths, slow_feed=slow, update_data=False)
        return {"ok": True, "cups": eighths / EIGHTHS_PER_CUP, "slow": slow}

    async def _status(self) -> dict:
        if self._status_cache is not None and time.time() < self._status_cache_expires:
            return {**self._status_cache, "cached": True}

        # Serialize concurrent first-time reads so we only make one PetSafe
        # request per 5-minute window even if the frontend fires two
        # calls back-to-back before the cache is populated.
        assert self._status_lock is not None
        async with self._status_lock:
            if self._status_cache is not None and time.time() < self._status_cache_expires:
                return {**self._status_cache, "cached": True}

            feeder = await self._resolve_feeder()
            if not self._feeder_data_fresh:
                await feeder.update_data()
            self._feeder_data_fresh = False

            food_low = feeder.food_low_status
            status = {
                "id": feeder.id,
                "name": feeder.friendly_name,
                "battery_level": feeder.battery_level,
                "food_low_status": food_low,
                "food_state": ["ok", "low", "empty"][food_low],
                "is_paused": feeder.is_paused,
                "is_slow_feed": feeder.is_slow_feed,
                "target_meal_cups": self.target_meal_cups,
            }
            self._status_cache = status
            self._status_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {**status, "cached": False}

    async def _schedule(self) -> dict:
        if self._schedule_cache is not None and time.time() < self._schedule_cache_expires:
            return {"schedules": self._schedule_cache, "cached": True}

        assert self._schedule_lock is not None
        async with self._schedule_lock:
            if self._schedule_cache is not None and time.time() < self._schedule_cache_expires:
                return {"schedules": self._schedule_cache, "cached": True}

            feeder = await self._resolve_feeder()
            raw = await feeder.get_schedules()
            schedules = [
                {
                    "id": entry.get("id") or entry.get("schedule_id"),
                    "time": entry.get("time"),
                    "amount_eighths": entry.get("amount"),
                    "cups": (entry.get("amount") or 0) / EIGHTHS_PER_CUP,
                }
                for entry in (raw or [])
            ]
            self._schedule_cache = schedules
            self._schedule_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {"schedules": schedules, "cached": False}

    async def _pause_schedule(self, paused: bool) -> dict:
        feeder = await self._resolve_feeder()
        await feeder.pause_schedules(paused, update_data=False)
        return {"ok": True, "paused": paused}

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        cmd = command.get("command")
        if cmd == "feed":
            cups = float(command.get("cups", 1 / EIGHTHS_PER_CUP))
            slow = bool(command.get("slow", False))
            return await self._feed(cups, slow)
        if cmd == "status":
            return await self._status()
        if cmd == "schedule":
            return await self._schedule()
        if cmd == "pause_schedule":
            return await self._pause_schedule(bool(command.get("paused")))
        raise ValueError(f"Unknown command: {cmd!r}")


Registry.register_resource_creator(
    Generic.API,
    PetSafeFeeder.MODEL,
    ResourceCreatorRegistration(PetSafeFeeder.new, PetSafeFeeder.validate),
)
