import asyncio
import json
import logging
import re
import time
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
# Every read path must go through the cached status method.
STATUS_CACHE_TTL_SEC = 300

# The petsafe library counts feed amount in 1/8-cup increments; the
# smallest possible dispense is 1 (= 1/8 cup).
EIGHTHS_PER_CUP = 8

# PetSafe's slow-feed mode spreads a meal over roughly 15 minutes. If
# feed_now fires again inside that window we get two overlapping feeds
# (a very fed dog). Refuse feed_now if a schedule fired inside this
# window, OR if last_feeding.created_at is within it.
SLOW_FEED_WINDOW_MIN = 15

# PetSafe has used a few different keys for the schedule id across
# firmware / API versions. Check them in order — first non-empty wins.
_SCHEDULE_ID_KEYS = ("id", "schedule_id", "_id", "scheduleId", "feedingId")


def _extract_schedule_id(entry: dict) -> str | None:
    for key in _SCHEDULE_ID_KEYS:
        value = entry.get(key)
        if value:
            return str(value)
    return None

DEFAULT_STATE_PATH = "~/.viam/petsafe-smart-feed-state.json"

# How often the background loop wakes to process pending state
# transitions. Local check only — no PetSafe hits unless there is
# actual work to do.
BG_LOOP_INTERVAL_SEC = 60

# Buffer after a scheduled feeding's original time before we restore
# a skipped or delayed entry. Wide enough that PetSafe has moved past
# the fire moment; tight enough that same-day resumption is likely.
RESTORE_MARGIN_MIN = 15

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


def _find_recently_fired(schedules: list, now: datetime, window: timedelta) -> dict | None:
    """Return a schedule whose most recent fire time falls in [now - window, now].

    Used before feed_now to guess whether a scheduled feed is currently
    dispensing so we don't stack a second feed on top of it.
    """
    threshold = now - window
    for s in schedules or []:
        t = s.get("time") or ""
        if ":" not in t:
            continue
        try:
            h, mi = map(int, t.split(":"))
        except ValueError:
            continue
        today = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        # Also consider yesterday's fire in case we're just past midnight.
        for candidate in (today, today - timedelta(days=1)):
            if threshold <= candidate <= now:
                return s
    return None


def _find_next_schedule(schedules: list, now: datetime) -> tuple | None:
    """Return (schedule_entry, datetime_when_it_next_fires) or None.

    Schedules recur daily, so a 07:00 entry with now=08:00 fires at 07:00
    tomorrow. Assumes both `now` and schedule times are the same timezone.
    """
    best_dt = None
    best_sched = None
    for s in schedules or []:
        t = s.get("time") or ""
        if ":" not in t:
            continue
        try:
            h, mi = map(int, t.split(":"))
        except ValueError:
            continue
        today = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        candidate = today if today > now else today + timedelta(days=1)
        if best_dt is None or candidate < best_dt:
            best_dt = candidate
            best_sched = s
    if best_sched is None:
        return None
    return best_sched, best_dt


def _empty_state() -> dict:
    return {"pause_until": None, "delays": {}, "skips": []}


class PetSafeFeeder(Generic):
    MODEL: ClassVar[Model] = Model(ModelFamily("viam", "petsafe"), "smart-feed")

    TOKEN_KEYS: ClassVar[tuple[str, ...]] = ("id_token", "refresh_token", "access_token")

    email: str
    feeder_id: str | None = None
    target_meal_cups: float | None = None
    state_path: str = DEFAULT_STATE_PATH
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
    _last_feeding_cache: dict | None = None
    _last_feeding_cache_expires: float = 0.0
    _last_feeding_lock: asyncio.Lock | None = None
    _state: dict | None = None
    _state_lock: asyncio.Lock | None = None
    _bg_task: asyncio.Task | None = None

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
        self._last_feeding_cache = None
        self._last_feeding_cache_expires = 0.0
        self._last_feeding_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state = self._load_state()
        # Cancel any running background loop before starting a fresh one
        # so config changes take effect immediately.
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
        try:
            self._bg_task = asyncio.create_task(self._bg_loop())
        except RuntimeError:
            # No running event loop yet — the module server will call
            # reconfigure again once the loop is up.
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
        merged = _empty_state()
        merged.update({k: v for k, v in loaded.items() if k in merged})
        return merged

    def _save_state(self) -> None:
        """Write self._state atomically. Caller must hold _state_lock."""
        assert self._state is not None
        path = Path(self.state_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._state, indent=2))
        tmp.replace(path)

    def _state_snapshot(self) -> dict:
        s = self._state or {}
        return {
            "pause_until": s.get("pause_until"),
            "delayed_schedule_ids": list((s.get("delays") or {}).keys()),
            "skipped_count": len(s.get("skips") or []),
        }

    # ------------------------------------------------------------------
    # Background loop

    async def _bg_loop(self) -> None:
        while True:
            try:
                await self._process_state()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOGGER.warning("bg state loop tick failed: %s", e)
            await asyncio.sleep(BG_LOOP_INTERVAL_SEC)

    async def _process_state(self) -> None:
        assert self._state is not None
        assert self._state_lock is not None
        now = datetime.now(UTC)
        async with self._state_lock:
            changed = False
            schedule_changed = False

            pause_until_iso = self._state.get("pause_until")
            if pause_until_iso:
                try:
                    pu_dt = datetime.fromisoformat(pause_until_iso)
                except ValueError:
                    LOGGER.warning("invalid pause_until %r in state; clearing", pause_until_iso)
                    self._state["pause_until"] = None
                    changed = True
                else:
                    if now >= pu_dt:
                        feeder = await self._resolve_feeder()
                        await feeder.pause_schedules(False, update_data=False)
                        self._state["pause_until"] = None
                        changed = True

            delays = dict(self._state.get("delays") or {})
            for sid, entry in delays.items():
                try:
                    restore_at = datetime.fromisoformat(entry["restore_at"])
                except (KeyError, ValueError, TypeError):
                    LOGGER.warning("invalid delay entry for %s; dropping", sid)
                    del self._state["delays"][sid]
                    changed = True
                    continue
                if now < restore_at:
                    continue
                feeder = await self._resolve_feeder()
                try:
                    await feeder.modify_schedule(
                        time=entry["original_time"],
                        amount=entry["original_amount"],
                        schedule_id=sid,
                        update_data=False,
                    )
                    del self._state["delays"][sid]
                    changed = True
                    schedule_changed = True
                except Exception as e:
                    LOGGER.warning("failed to restore delay %s (will retry): %s", sid, e)

            skips = list(self._state.get("skips") or [])
            ready = []
            pending = []
            for entry in skips:
                try:
                    restore_at = datetime.fromisoformat(entry["restore_at"])
                except (KeyError, ValueError, TypeError):
                    LOGGER.warning("invalid skip entry; dropping: %r", entry)
                    continue
                (pending if now < restore_at else ready).append(entry)

            if ready:
                # Dedupe against current PetSafe schedules. If a matching
                # (time, amount) already exists, treat the skip as restored
                # and drop the state entry — this handles the case where a
                # previous schedule_feed call ambiguously failed (network
                # error after PetSafe committed) and we would otherwise
                # create a duplicate on retry.
                feeder = await self._resolve_feeder()
                current = await feeder.get_schedules()
                existing = {
                    (s.get("time"), s.get("amount")) for s in (current or [])
                }
                remaining = list(pending)
                for entry in ready:
                    signature = (entry["original_time"], entry["original_amount"])
                    if signature in existing:
                        schedule_changed = True
                        continue
                    try:
                        await feeder.schedule_feed(
                            time=entry["original_time"],
                            amount=entry["original_amount"],
                            update_data=False,
                        )
                        existing.add(signature)
                        schedule_changed = True
                    except Exception as e:
                        LOGGER.warning("failed to restore skip %r (will retry): %s", entry, e)
                        remaining.append(entry)
            else:
                remaining = pending

            if len(remaining) != len(skips):
                self._state["skips"] = remaining
                changed = True

            if changed:
                self._save_state()
            if schedule_changed:
                self._schedule_cache = None
                self._status_cache = None

    # ------------------------------------------------------------------
    # Reads

    async def _get_petsafe_status(self) -> dict:
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

    async def _status(self) -> dict:
        petsafe = await self._get_petsafe_status()
        return {**petsafe, **self._state_snapshot()}

    async def _schedule(self) -> dict:
        if self._schedule_cache is not None and time.time() < self._schedule_cache_expires:
            return {"schedules": self._schedule_cache, "cached": True}

        assert self._schedule_lock is not None
        async with self._schedule_lock:
            if self._schedule_cache is not None and time.time() < self._schedule_cache_expires:
                return {"schedules": self._schedule_cache, "cached": True}

            feeder = await self._resolve_feeder()
            raw = await feeder.get_schedules()
            schedules = []
            for entry in (raw or []):
                if not isinstance(entry, dict):
                    continue
                sid = _extract_schedule_id(entry)
                if sid is None:
                    LOGGER.warning(
                        "schedule entry has no recognized id key; keys=%s",
                        sorted(entry.keys()),
                    )
                schedules.append({
                    "id": sid,
                    "time": entry.get("time"),
                    "amount_eighths": entry.get("amount"),
                    "cups": (entry.get("amount") or 0) / EIGHTHS_PER_CUP,
                })
            self._schedule_cache = schedules
            self._schedule_cache_expires = time.time() + STATUS_CACHE_TTL_SEC
            return {"schedules": schedules, "cached": False}

    # ------------------------------------------------------------------
    # Writes

    async def _feed(self, cups: float, slow: bool | None = None) -> dict:
        eighths = max(1, round(cups * EIGHTHS_PER_CUP))
        feeder = await self._resolve_feeder()
        # slow=None → petsafe SDK falls back to the feeder's own
        # slow_feed setting. Passing False (as we used to) forced fast
        # regardless of what the feeder is configured for.
        # update_data=False so we don't burn a read call after every
        # feed; the status cache TTL is what decides when to refresh.
        await feeder.feed(amount=eighths, slow_feed=slow, update_data=False)
        return {"ok": True, "cups": eighths / EIGHTHS_PER_CUP, "slow": slow}

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

    async def _pause_schedule(self, paused: bool) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        async with self._state_lock:
            feeder = await self._resolve_feeder()
            await feeder.pause_schedules(paused, update_data=False)
            # A manual unpause clears any pending vacation-style pause_until
            # so the background loop doesn't try to unpause an already-active
            # schedule later.
            if not paused and self._state.get("pause_until"):
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
            # Naive input from a browser <input type="datetime-local"> —
            # interpret as local machine time.
            until = until.astimezone()
        until_utc = until.astimezone(UTC)
        if until_utc <= datetime.now(UTC):
            raise ValueError("`until` must be in the future")
        async with self._state_lock:
            feeder = await self._resolve_feeder()
            await feeder.pause_schedules(True, update_data=False)
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
        schedule_result = await self._schedule()
        schedules = schedule_result["schedules"]
        now_local = datetime.now().astimezone()
        found = _find_next_schedule(schedules, now_local)
        if not found:
            raise RuntimeError("No upcoming scheduled feedings.")
        next_sched, next_fire = found
        # Shift the scheduled fire time by `hours` — positive delays,
        # negative moves earlier. Computed from `next_fire` (not `now`)
        # so a small "delay" doesn't accidentally end up earlier than
        # the original.
        moved_local = next_fire + timedelta(hours=hours)
        if moved_local <= now_local:
            raise ValueError("resulting time is in the past")
        moved_hhmm = moved_local.strftime("%H:%M")
        restore_at = (
            moved_local + timedelta(minutes=RESTORE_MARGIN_MIN)
        ).astimezone(UTC)
        async with self._state_lock:
            feeder = await self._resolve_feeder()
            await feeder.modify_schedule(
                time=moved_hhmm,
                amount=next_sched["amount_eighths"],
                schedule_id=next_sched["id"],
                update_data=False,
            )
            self._state["delays"][next_sched["id"]] = {
                "restore_at": restore_at.isoformat(),
                "original_time": next_sched["time"],
                "original_amount": next_sched["amount_eighths"],
            }
            self._save_state()
        self._schedule_cache = None
        return {
            "ok": True,
            "schedule_id": next_sched["id"],
            "moved_to": moved_hhmm,
            "restore_at": restore_at.isoformat(),
        }

    async def _skip_next(self) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        schedule_result = await self._schedule()
        schedules = schedule_result["schedules"]
        now_local = datetime.now().astimezone()
        found = _find_next_schedule(schedules, now_local)
        if not found:
            raise RuntimeError("No upcoming scheduled feedings.")
        next_sched, next_fire = found
        restore_at = (
            next_fire + timedelta(minutes=RESTORE_MARGIN_MIN)
        ).astimezone(UTC)
        async with self._state_lock:
            feeder = await self._resolve_feeder()
            await feeder.delete_schedule(next_sched["id"], update_data=False)
            self._state["skips"].append({
                "restore_at": restore_at.isoformat(),
                "original_time": next_sched["time"],
                "original_amount": next_sched["amount_eighths"],
            })
            self._save_state()
        self._schedule_cache = None
        return {
            "ok": True,
            "skipped_time": next_sched["time"],
            "skipped_cups": next_sched["cups"],
            "restore_at": restore_at.isoformat(),
        }

    async def _feed_now(self) -> dict:
        assert self._state is not None
        assert self._state_lock is not None
        schedule_result = await self._schedule()
        schedules = schedule_result["schedules"]
        now_local = datetime.now().astimezone()

        # Refuse if a feed is likely still in progress (from a scheduled
        # fire, from the PetSafe app, from a previous feed_now, or
        # anywhere else the feeder motor is currently active). Two
        # checks: any schedule whose fire time falls in the slow-feed
        # window, and last_feeding.created_at within the window.
        window = timedelta(minutes=SLOW_FEED_WINDOW_MIN)
        recent_sched = _find_recently_fired(schedules, now_local, window)
        if recent_sched is not None:
            raise RuntimeError(
                f"Schedule at {recent_sched.get('time')} fired within the last "
                f"{SLOW_FEED_WINDOW_MIN} minutes; refusing feed_now to avoid a "
                "double feed. Try again once slow-feed finishes."
            )
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

        found = _find_next_schedule(schedules, now_local)
        skip_info = None
        if found:
            next_sched, next_fire = found
            amount_cups = next_sched["cups"]
            restore_at = (
                next_fire + timedelta(minutes=RESTORE_MARGIN_MIN)
            ).astimezone(UTC)
            async with self._state_lock:
                feeder = await self._resolve_feeder()
                await feeder.delete_schedule(next_sched["id"], update_data=False)
                self._state["skips"].append({
                    "restore_at": restore_at.isoformat(),
                    "original_time": next_sched["time"],
                    "original_amount": next_sched["amount_eighths"],
                })
                self._save_state()
            self._schedule_cache = None
            skip_info = {
                "original_time": next_sched["time"],
                "restore_at": restore_at.isoformat(),
            }
        else:
            if self.target_meal_cups is None:
                raise RuntimeError(
                    "No upcoming schedule to substitute and `target_meal_cups` is not configured."
                )
            amount_cups = self.target_meal_cups
        # slow=None → honor the feeder's own slow_feed setting instead
        # of forcing fast. Users who set the feeder to slow expect
        # feed_now to respect that.
        await self._feed(amount_cups, slow=None)
        return {"ok": True, "fed_cups": amount_cups, "skip": skip_info}

    async def _add_schedule(self, time_value: Any, cups: Any) -> dict:
        hhmm = _normalize_time(time_value)
        eighths = _cups_to_eighths(cups)
        feeder = await self._resolve_feeder()
        response = await feeder.schedule_feed(
            time=hhmm, amount=eighths, update_data=False
        )
        self._schedule_cache = None
        new_id = None
        if isinstance(response, dict):
            new_id = _extract_schedule_id(response)
        return {
            "ok": True,
            "schedule": {
                "id": new_id,
                "time": hhmm,
                "amount_eighths": eighths,
                "cups": eighths / EIGHTHS_PER_CUP,
            },
        }

    async def _modify_schedule(self, schedule_id: Any, time_value: Any, cups: Any) -> dict:
        if not isinstance(schedule_id, str) or not schedule_id:
            raise ValueError("`id` is required")
        hhmm = _normalize_time(time_value)
        eighths = _cups_to_eighths(cups)
        feeder = await self._resolve_feeder()
        await feeder.modify_schedule(
            time=hhmm,
            amount=eighths,
            schedule_id=schedule_id,
            update_data=False,
        )
        self._schedule_cache = None
        # A manual edit invalidates any pending auto-restore for this
        # entry: the user has taken over, so drop the ghost restore.
        assert self._state is not None
        assert self._state_lock is not None
        async with self._state_lock:
            if schedule_id in (self._state.get("delays") or {}):
                del self._state["delays"][schedule_id]
                self._save_state()
        return {
            "ok": True,
            "id": schedule_id,
            "time": hhmm,
            "amount_eighths": eighths,
            "cups": eighths / EIGHTHS_PER_CUP,
        }

    async def _delete_schedule(self, schedule_id: Any) -> dict:
        if not isinstance(schedule_id, str) or not schedule_id:
            raise ValueError("`id` is required")
        feeder = await self._resolve_feeder()
        await feeder.delete_schedule(schedule_id, update_data=False)
        self._schedule_cache = None
        assert self._state is not None
        assert self._state_lock is not None
        async with self._state_lock:
            if schedule_id in (self._state.get("delays") or {}):
                del self._state["delays"][schedule_id]
                self._save_state()
        return {"ok": True, "id": schedule_id}

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
            return await self._add_schedule(command.get("time"), command.get("cups"))
        if cmd == "modify_schedule":
            return await self._modify_schedule(
                command.get("id"), command.get("time"), command.get("cups")
            )
        if cmd == "delete_schedule":
            return await self._delete_schedule(command.get("id"))
        raise ValueError(f"Unknown command: {cmd!r}")


Registry.register_resource_creator(
    Generic.API,
    PetSafeFeeder.MODEL,
    ResourceCreatorRegistration(PetSafeFeeder.new, PetSafeFeeder.validate),
)
