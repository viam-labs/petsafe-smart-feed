import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
STATUS_CACHE_TTL_SEC = 300

EIGHTHS_PER_CUP = 8

# PetSafe's slow-feed mode spreads a meal over roughly 15 minutes.
SLOW_FEED_WINDOW_MIN = 15

# PetSafe returns schedule ids under different keys across firmware
# versions; try them all.
_SCHEDULE_ID_KEYS: tuple[str, ...] = (
    "id", "schedule_id", "_id", "scheduleId", "feedingId",
)


def _extract_schedule_id(entry: dict) -> str | None:
    for key in _SCHEDULE_ID_KEYS:
        value = entry.get(key)
        if value:
            return str(value)
    return None


DEFAULT_STATE_PATH = "~/.viam/petsafe-smart-feed-state.json"

BG_LOOP_INTERVAL_SEC = 60

CATCH_UP_WINDOW_MIN = 30

SCHEMA_VERSION = 2

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _normalize_time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("`time` must be a string like 'HH:MM'")
    m = _TIME_RE.match(value)
    if not m:
        raise ValueError("`time` must be in HH:MM format")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError("`time` values out of range (00:00 through 23:59)")
    return f"{h:02d}:{mi:02d}"


def _cups_to_eighths(value: Any) -> int:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError("`cups` must be a positive number")
    return max(1, round(value * EIGHTHS_PER_CUP))


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _normalize_days(days: Any) -> list[int]:
    """0..6 = Mon..Sun. Empty = every day."""
    if days is None:
        return []
    if not isinstance(days, list):
        raise ValueError("`days_of_week` must be a list of integers 0..6")
    out = set()
    for d in days:
        if not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6:
            raise ValueError("`days_of_week` values must be integers 0..6")
        out.add(d)
    return sorted(out)


def _normalize_schedule(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    hhmm = _normalize_time(raw.get("time"))
    cups = raw.get("cups")
    if cups is None:
        raise ValueError("`cups` is required")
    if not isinstance(cups, int | float) or isinstance(cups, bool) or cups <= 0:
        raise ValueError("`cups` must be a positive number")
    delayed_until = raw.get("delayed_until")
    if delayed_until is not None and not isinstance(delayed_until, str):
        raise ValueError("`delayed_until` must be an ISO-8601 string if set")
    return {
        "id": raw.get("id") or _new_id(),
        "name": str(raw.get("name") or f"Feed {hhmm}"),
        "time": hhmm,
        "cups": float(cups),
        "days_of_week": _normalize_days(raw.get("days_of_week")),
        "enabled": bool(raw.get("enabled", True)),
        "skip_next_fire": bool(raw.get("skip_next_fire", False)),
        "delayed_until": delayed_until,
        "last_processed_at": raw.get("last_processed_at"),
        "last_fired_at": raw.get("last_fired_at"),
    }


def _empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "schedules": [],
        "pause_until": None,
    }


def _find_next_schedule(schedules: list, now: datetime) -> tuple | None:
    """Return (schedule_entry, datetime_when_it_next_fires) or None."""
    best_dt = None
    best_sched = None
    for s in schedules or []:
        if not s.get("enabled", True):
            continue
        t = s.get("time") or ""
        if ":" not in t:
            continue
        try:
            h, mi = map(int, t.split(":"))
        except ValueError:
            continue
        dows = s.get("days_of_week") or []
        delayed = s.get("delayed_until")
        if delayed:
            try:
                dt = datetime.fromisoformat(delayed)
                if dt.astimezone() > now:
                    candidate = dt.astimezone()
                    if best_dt is None or candidate < best_dt:
                        best_dt = candidate
                        best_sched = s
                    continue
            except ValueError:
                pass
        for offset in range(0, 8):
            candidate = (now + timedelta(days=offset)).replace(
                hour=h, minute=mi, second=0, microsecond=0
            )
            if candidate <= now:
                continue
            if dows and candidate.weekday() not in dows:
                continue
            if best_dt is None or candidate < best_dt:
                best_dt = candidate
                best_sched = s
            break
    if best_sched is None:
        return None
    return best_sched, best_dt


class PetSafeFeeder(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "petsafe"), "smart-feed")

    TOKEN_KEYS: ClassVar[tuple[str, ...]] = ("id_token", "refresh_token", "access_token")

    email: str
    feeder_id: str | None = None
    target_meal_cups: float | None = None
    state_path: str = DEFAULT_STATE_PATH
    catch_up_within_min: int = CATCH_UP_WINDOW_MIN
    _tokens: dict | None = None
    _client: sf.PetSafeClient | None = None
    _feeder: Any | None = None
    _feeder_data_fresh: bool = False
    _status_cache: dict | None = None
    _status_cache_expires: float = 0.0
    _status_lock: asyncio.Lock | None = None
    _last_feeding_cache: dict | None = None
    _last_feeding_cache_expires: float = 0.0
    _last_feeding_lock: asyncio.Lock | None = None
    _state: dict | None = None
    _state_lock: asyncio.Lock | None = None
    _bg_task: asyncio.Task | None = None
    _migrated: bool = False

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
        state_path = attrs.get("state_path")
        if state_path is not None and not isinstance(state_path, str):
            raise ValueError("`state_path` must be a string if set")
        catch_up = attrs.get("catch_up_within_min")
        if catch_up is not None and (
            not isinstance(catch_up, int) or isinstance(catch_up, bool) or catch_up < 0
        ):
            raise ValueError("`catch_up_within_min` must be a non-negative integer")
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
        self.state_path = str(attrs.get("state_path") or DEFAULT_STATE_PATH)
        catch_up = attrs.get("catch_up_within_min")
        self.catch_up_within_min = int(catch_up) if catch_up is not None else CATCH_UP_WINDOW_MIN
        self._client = None
        self._feeder = None
        self._feeder_data_fresh = False
        self._status_cache = None
        self._status_cache_expires = 0.0
        self._status_lock = asyncio.Lock()
        self._last_feeding_cache = None
        self._last_feeding_cache_expires = 0.0
        self._last_feeding_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state = self._load_state()
        self._migrated = self._state.get("schema_version") == SCHEMA_VERSION
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._bg_loop())
        except RuntimeError:
            self._bg_task = None

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

    # ------------------------------------------------------------------
    # State persistence

    def _load_state(self) -> dict:
        path = Path(self.state_path).expanduser()
        if not path.exists():
            return _empty_state()
        try:
            loaded = json.loads(path.read_text())
        except Exception as e:
            LOGGER.warning("failed to load state from %s (using empty): %s", path, e)
            return _empty_state()
        if not isinstance(loaded, dict):
            return _empty_state()
        merged = _empty_state()
        if isinstance(loaded.get("schedules"), list):
            valid = []
            for entry in loaded["schedules"]:
                try:
                    valid.append(_normalize_schedule(entry))
                except ValueError as e:
                    LOGGER.warning("dropping malformed persisted schedule: %s", e)
            merged["schedules"] = valid
        if isinstance(loaded.get("pause_until"), str):
            merged["pause_until"] = loaded["pause_until"]
        if loaded.get("schema_version") == SCHEMA_VERSION:
            merged["schema_version"] = SCHEMA_VERSION
        else:
            merged["schema_version"] = None
        return merged

    def _save_state(self) -> None:
        """Write self._state atomically. Caller must hold _state_lock."""
        assert self._state is not None
        path = Path(self.state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _find_schedule(self, sid: str) -> dict | None:
        for s in (self._state or {}).get("schedules", []):
            if s.get("id") == sid:
                return s
        return None

    # ------------------------------------------------------------------
    # Migration + background loop

    async def _migrate_if_needed(self) -> None:
        """Delete pre-v2 PetSafe cloud schedules and stamp schema_version."""
        if self._migrated:
            return
        assert self._state is not None
        assert self._state_lock is not None
        try:
            feeder = await self._resolve_feeder()
            existing = await feeder.get_schedules()
            for entry in existing or []:
                sid = _extract_schedule_id(entry) if isinstance(entry, dict) else None
                if not sid:
                    continue
                try:
                    await feeder.delete_schedule(sid, update_data=False)
                except Exception as e:
                    LOGGER.warning("failed to delete PetSafe schedule %s: %s", sid, e)
        except Exception as e:
            LOGGER.warning("migration deferred: %s", e)
            return
        async with self._state_lock:
            self._state["schema_version"] = SCHEMA_VERSION
            self._save_state()
        self._migrated = True

    async def _bg_loop(self) -> None:
        while True:
            try:
                await self._migrate_if_needed()
                if self._migrated:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("bg loop tick failed: %s", e)
            await asyncio.sleep(BG_LOOP_INTERVAL_SEC)

    async def _tick(self) -> None:
        assert self._state is not None
        assert self._state_lock is not None
        now_local = datetime.now().astimezone()

        pause_until_iso = (self._state or {}).get("pause_until")
        if pause_until_iso:
            try:
                pu = datetime.fromisoformat(pause_until_iso)
                if now_local.astimezone(UTC) < pu.astimezone(UTC):
                    return
                async with self._state_lock:
                    self._state["pause_until"] = None
                    self._save_state()
            except ValueError:
                async with self._state_lock:
                    self._state["pause_until"] = None
                    self._save_state()

        state_changed = False
        async with self._state_lock:
            for schedule in list(self._state.get("schedules", [])):
                try:
                    if await self._process_schedule(schedule, now_local):
                        state_changed = True
                except Exception as e:
                    LOGGER.warning("failed to process schedule %s: %s", schedule.get("id"), e)
            if state_changed:
                self._save_state()

    async def _process_schedule(self, schedule: dict, now_local: datetime) -> bool:
        delayed_iso = schedule.get("delayed_until")
        if delayed_iso:
            try:
                delayed_at = datetime.fromisoformat(delayed_iso).astimezone()
            except ValueError:
                schedule["delayed_until"] = None
                return True
            if now_local < delayed_at:
                return False
            fired_at_iso = datetime.now(UTC).isoformat()
            schedule["delayed_until"] = None
            schedule["last_processed_at"] = fired_at_iso
            if schedule.get("enabled", True) and not schedule.get("skip_next_fire", False):
                await self._feed(schedule["cups"], slow=None)
                schedule["last_fired_at"] = fired_at_iso
            schedule["skip_next_fire"] = False
            return True

        if not schedule.get("enabled", True):
            return False

        time_str = schedule.get("time") or ""
        if ":" not in time_str:
            return False
        try:
            h, mi = map(int, time_str.split(":"))
        except ValueError:
            return False
        today_fire = now_local.replace(hour=h, minute=mi, second=0, microsecond=0)
        if now_local < today_fire:
            return False

        dows = schedule.get("days_of_week") or []
        if dows and now_local.weekday() not in dows:
            return False

        last_proc_iso = schedule.get("last_processed_at")
        if last_proc_iso:
            try:
                last_proc = datetime.fromisoformat(last_proc_iso).astimezone(now_local.tzinfo)
                if last_proc >= today_fire:
                    return False
            except ValueError:
                pass

        age = (now_local - today_fire).total_seconds() / 60
        if age > self.catch_up_within_min:
            LOGGER.warning(
                "missed feed for schedule %s at %s (%.0f min past catch-up window)",
                schedule.get("id"), today_fire.isoformat(), age - self.catch_up_within_min,
            )
            schedule["last_processed_at"] = today_fire.astimezone(UTC).isoformat()
            return True

        if schedule.get("skip_next_fire", False):
            LOGGER.info(
                "skipping feed for schedule %s at %s",
                schedule.get("id"), today_fire.isoformat(),
            )
            schedule["last_processed_at"] = today_fire.astimezone(UTC).isoformat()
            schedule["skip_next_fire"] = False
            return True

        fired_at_iso = datetime.now(UTC).isoformat()
        await self._feed(schedule["cups"], slow=None)
        schedule["last_processed_at"] = fired_at_iso
        schedule["last_fired_at"] = fired_at_iso
        return True

    # ------------------------------------------------------------------
    # Reads

    async def _get_petsafe_status(self) -> dict:
        if self._status_cache is not None and time.time() < self._status_cache_expires:
            return {**self._status_cache, "cached": True}
        assert self._status_lock is not None
        async with self._status_lock:
            if self._status_cache is not None and time.time() < self._status_cache_expires:
                return {**self._status_cache, "cached": True}
            feeder = await self._resolve_feeder()
            if not self._feeder_data_fresh:
                await feeder.update_data()
            self._feeder_data_fresh = False
            payload = {
                "food_state": feeder.food_low_status,
                "food_low_status": feeder.food_low_status,
                "battery_pct": feeder.battery_level,
                # petsafe-api's DeviceSmartFeed does not expose `is_online`;
                # tolerate its absence so a status probe doesn't AttributeError.
                "is_connected": getattr(feeder, "is_online", None),
                "target_meal_cups": self.target_meal_cups,
                "is_slow_feed": feeder.is_slow_feed,
                "paused": feeder.is_paused,
            }
            self._status_cache = payload
            self._status_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {**payload, "cached": False}

    async def _status(self) -> dict:
        petsafe = await self._get_petsafe_status()
        return {
            **petsafe,
            "schedules": list((self._state or {}).get("schedules", [])),
            "pause_until": (self._state or {}).get("pause_until"),
            "migrated": self._migrated,
        }

    async def _schedule(self) -> dict:
        return {
            "schedules": list((self._state or {}).get("schedules", [])),
            "cached": False,
        }

    async def _last_feeding(self) -> dict:
        if (
            self._last_feeding_cache is not None
            and time.time() < self._last_feeding_cache_expires
        ):
            return {**self._last_feeding_cache, "cached": True}
        assert self._last_feeding_lock is not None
        async with self._last_feeding_lock:
            if (
                self._last_feeding_cache is not None
                and time.time() < self._last_feeding_cache_expires
            ):
                return {**self._last_feeding_cache, "cached": True}
            feeder = await self._resolve_feeder()
            raw = await feeder.get_last_feeding()
            payload = {"last_feeding": raw if isinstance(raw, dict) else None}
            self._last_feeding_cache = payload
            self._last_feeding_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {**payload, "cached": False}

    # ------------------------------------------------------------------
    # Writes

    async def _feed(self, cups: float, slow: bool | None = None) -> dict:
        eighths = max(1, round(cups * EIGHTHS_PER_CUP))
        feeder = await self._resolve_feeder()
        # slow=None defers to the feeder's own slow_feed setting.
        # update_data=False so we don't burn a status read after each feed.
        await feeder.feed(amount=eighths, slow_feed=slow, update_data=False)
        return {"ok": True, "cups": eighths / EIGHTHS_PER_CUP, "slow": slow}

    async def _feed_now(self) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if self.target_meal_cups is None:
            raise RuntimeError(
                "`target_meal_cups` is not configured; set it in the module "
                "config to enable feed_now."
            )

        window = timedelta(minutes=SLOW_FEED_WINDOW_MIN)
        last_feeding = (await self._last_feeding()).get("last_feeding") or {}
        last_ts_raw = last_feeding.get("created_at")
        if isinstance(last_ts_raw, str):
            try:
                last_ts = datetime.fromisoformat(last_ts_raw.replace("Z", "+00:00"))
                age = datetime.now(UTC) - last_ts.astimezone(UTC)
                if age < window:
                    raise RuntimeError(
                        f"Last feeding recorded {int(age.total_seconds() / 60)} "
                        f"min ago; refusing feed_now inside the "
                        f"{SLOW_FEED_WINDOW_MIN}-min slow-feed window."
                    )
            except ValueError:
                pass

        await self._feed(self.target_meal_cups, slow=None)
        return {"ok": True, "fed_cups": self.target_meal_cups}

    async def _pause_schedule(self, paused: bool) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        async with self._state_lock:
            if paused:
                self._state["pause_until"] = datetime(2100, 1, 1, tzinfo=UTC).isoformat()
            else:
                self._state["pause_until"] = None
            self._save_state()
        return {"ok": True, "paused": paused}

    async def _pause_until(self, until_value: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if not isinstance(until_value, str):
            raise ValueError("`until` must be an ISO-8601 datetime string")
        try:
            until = datetime.fromisoformat(until_value)
        except ValueError as e:
            raise ValueError(f"invalid `until`: {e}") from e
        if until.tzinfo is None:
            until = until.astimezone()
        until_utc = until.astimezone(UTC)
        if until_utc <= datetime.now(UTC):
            raise ValueError("`until` must be in the future")
        async with self._state_lock:
            self._state["pause_until"] = until_utc.isoformat()
            self._save_state()
        return {"ok": True, "pause_until": until_utc.isoformat()}

    async def _delay_next(self, hours: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if not isinstance(hours, int | float) or isinstance(hours, bool) or hours == 0:
            raise ValueError(
                "`hours` must be a non-zero number (positive = later, negative = earlier)"
            )
        now_local = datetime.now().astimezone()
        schedules = list((self._state or {}).get("schedules", []))
        found = _find_next_schedule(schedules, now_local)
        if not found:
            raise RuntimeError("No upcoming scheduled feedings.")
        next_sched, next_fire = found
        moved = next_fire + timedelta(hours=hours)
        if moved <= now_local:
            raise ValueError("resulting time is in the past")
        async with self._state_lock:
            live = self._find_schedule(next_sched["id"])
            if live is None:
                raise RuntimeError("schedule disappeared before delay could apply")
            live["delayed_until"] = moved.astimezone(UTC).isoformat()
            self._save_state()
        return {
            "ok": True,
            "schedule_id": next_sched["id"],
            "delayed_until": moved.astimezone(UTC).isoformat(),
        }

    async def _skip_next(self) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        now_local = datetime.now().astimezone()
        schedules = list((self._state or {}).get("schedules", []))
        found = _find_next_schedule(schedules, now_local)
        if not found:
            raise RuntimeError("No upcoming scheduled feedings.")
        next_sched, next_fire = found
        async with self._state_lock:
            live = self._find_schedule(next_sched["id"])
            if live is None:
                raise RuntimeError("schedule disappeared before skip could apply")
            live["skip_next_fire"] = True
            self._save_state()
        return {
            "ok": True,
            "schedule_id": next_sched["id"],
            "skipped_time": next_sched["time"],
            "skipped_cups": next_sched["cups"],
        }

    async def _add_schedule(self, payload: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        schedule = _normalize_schedule({**(payload or {}), "id": _new_id()})
        async with self._state_lock:
            self._state["schedules"].append(schedule)
            self._save_state()
        return {"ok": True, "schedule": schedule}

    async def _modify_schedule(self, payload: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        async with self._state_lock:
            existing = self._find_schedule(str(payload["id"]))
            if existing is None:
                raise ValueError(f"no schedule with id={payload['id']!r}")
            merged = {**existing}
            for k, v in payload.items():
                if v is None and k in ("delayed_until", "days_of_week"):
                    merged[k] = v
                elif v is not None:
                    merged[k] = v
            normalized = _normalize_schedule(merged)
            normalized["id"] = existing["id"]
            for k in ("last_processed_at", "last_fired_at"):
                if k not in payload:
                    normalized[k] = existing.get(k)
            for i, s in enumerate(self._state["schedules"]):
                if s["id"] == existing["id"]:
                    self._state["schedules"][i] = normalized
                    break
            self._save_state()
        return {"ok": True, "schedule": normalized}

    async def _delete_schedule(self, payload: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        sid = payload if isinstance(payload, str) else (payload or {}).get("id")
        if not isinstance(sid, str) or not sid:
            raise ValueError("`id` is required")
        async with self._state_lock:
            before = len(self._state["schedules"])
            self._state["schedules"] = [
                s for s in self._state["schedules"] if s.get("id") != sid
            ]
            if len(self._state["schedules"]) == before:
                raise ValueError(f"no schedule with id={sid!r}")
            self._save_state()
        return {"ok": True, "id": sid}

    async def _set_schedule_enabled(self, payload: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        sid = str(payload["id"])
        enabled = bool(payload.get("enabled"))
        async with self._state_lock:
            live = self._find_schedule(sid)
            if live is None:
                raise ValueError(f"no schedule with id={sid!r}")
            live["enabled"] = enabled
            self._save_state()
        return {"ok": True, "id": sid, "enabled": enabled}

    async def _set_skip_next(self, payload: Any) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        if not isinstance(payload, dict) or not payload.get("id"):
            raise ValueError("`id` is required")
        sid = str(payload["id"])
        skip = bool(payload.get("skip", True))
        async with self._state_lock:
            live = self._find_schedule(sid)
            if live is None:
                raise ValueError(f"no schedule with id={sid!r}")
            live["skip_next_fire"] = skip
            self._save_state()
        return {"ok": True, "id": sid, "skip_next_fire": skip}

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
            slow_raw = command.get("slow")
            slow = bool(slow_raw) if slow_raw is not None else None
            return await self._feed(cups, slow)
        if cmd == "status":
            return await self._status()
        if cmd == "schedule":
            return await self._schedule()
        if cmd == "last_feeding":
            return await self._last_feeding()
        if cmd == "pause_schedule":
            return await self._pause_schedule(bool(command.get("paused")))
        if cmd == "pause_until":
            return await self._pause_until(command.get("until"))
        if cmd == "delay_next":
            return await self._delay_next(command.get("hours"))
        if cmd == "skip_next":
            return await self._skip_next()
        if cmd == "feed_now":
            return await self._feed_now()
        if cmd == "add_schedule":
            return await self._add_schedule(command.get("schedule") or command)
        if cmd == "modify_schedule":
            return await self._modify_schedule(command.get("schedule") or command)
        if cmd == "delete_schedule":
            return await self._delete_schedule(command)
        if cmd == "set_schedule_enabled":
            return await self._set_schedule_enabled(command)
        if cmd == "set_skip_next":
            return await self._set_skip_next(command)
        raise ValueError(f"Unknown command: {cmd!r}")


Registry.register_resource_creator(
    Generic.API,
    PetSafeFeeder.MODEL,
    ResourceCreatorRegistration(PetSafeFeeder.new, PetSafeFeeder.validate),
)
