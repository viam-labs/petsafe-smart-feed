import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, ClassVar, Mapping, Optional, Sequence

import petsafe as sf
from typing_extensions import Self
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

    email: str
    token_path: str
    feeder_id: Optional[str] = None
    _client: Optional[sf.PetSafeClient] = None
    _feeder: Optional[Any] = None
    _status_cache: Optional[dict] = None
    _status_cache_expires: float = 0.0
    _status_lock: Optional[asyncio.Lock] = None

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
        if not attrs.get("token_path"):
            raise ValueError("`token_path` attribute is required")
        return []

    def reconfigure(
        self,
        config: ComponentConfig,
        dependencies: Mapping[ResourceName, ResourceBase],
    ) -> None:
        attrs = struct_to_dict(config.attributes)
        self.email = attrs["email"]
        self.token_path = attrs["token_path"]
        self.feeder_id = attrs.get("feeder_id")
        # Bust caches so token or feeder changes take effect immediately.
        self._client = None
        self._feeder = None
        self._status_cache = None
        self._status_cache_expires = 0.0
        self._status_lock = asyncio.Lock()

    def _load_tokens(self) -> dict:
        path = Path(self.token_path).expanduser()
        if not path.exists():
            raise RuntimeError(
                f"PetSafe token file not found at {path}. "
                "Run `python -m petsafe <email>` to generate tokens."
            )
        return json.loads(path.read_text())

    def _get_client(self) -> sf.PetSafeClient:
        if self._client is None:
            tokens = self._load_tokens()
            self._client = sf.PetSafeClient(
                email=self.email,
                id_token=tokens["id_token"],
                refresh_token=tokens["refresh_token"],
                access_token=tokens["access_token"],
            )
        return self._client

    def _resolve_feeder(self) -> Any:
        if self._feeder is not None:
            return self._feeder
        client = self._get_client()
        feeders = sf.devices.get_feeders(client)
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
        return self._feeder

    async def _feed(self, cups: float, slow: bool) -> dict:
        eighths = max(1, round(cups * EIGHTHS_PER_CUP))

        def call() -> None:
            feeder = self._resolve_feeder()
            feeder.feed(amount=eighths, slow_feed=slow)

        await asyncio.to_thread(call)
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

            def call() -> dict:
                feeder = self._resolve_feeder()
                food_low = feeder.food_low_status
                return {
                    "id": getattr(feeder, "id", None),
                    "name": getattr(feeder, "name", None),
                    "battery_level": feeder.battery_level,
                    "food_low_status": food_low,
                    "food_state": ["ok", "low", "empty"][food_low],
                }

            status = await asyncio.to_thread(call)
            self._status_cache = status
            self._status_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {**status, "cached": False}

    async def do_command(
        self,
        command: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Mapping[str, Any]:
        cmd = command.get("command")
        if cmd == "feed":
            cups = float(command.get("cups", 1 / EIGHTHS_PER_CUP))
            slow = bool(command.get("slow", False))
            return await self._feed(cups, slow)
        if cmd == "status":
            return await self._status()
        raise ValueError(f"Unknown command: {cmd!r}")


Registry.register_resource_creator(
    Generic.API,
    PetSafeFeeder.MODEL,
    ResourceCreatorRegistration(PetSafeFeeder.new, PetSafeFeeder.validate),
)
