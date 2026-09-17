# petsafe-smart-feed

A Viam module for PetSafe Smart Feed automatic pet feeders. Wraps the
[petsafe-api](https://github.com/ThomasHFWright/petsafe-api) library so
you can dispense food and read battery / food-level status from any
Viam machine.

Exposes a single Generic component (`viam:petsafe:smart-feed`) that
accepts commands via `do_command`.

Tested against the PetSafe Smart Feed 2nd generation (PFD00-16828). The
underlying library also supports ScoopFree and Smart Door devices;
this module currently exposes only the feeder.

## Prerequisites

1. A PetSafe Smart Feed linked to a PetSafe account.
2. A machine (e.g. a Raspberry Pi) running `viam-server`.
3. Access tokens for your PetSafe account — see below.

## Getting tokens (one-time)

PetSafe's cloud API uses AWS Cognito with an email-code login flow.
Run this once on any machine with Python (your laptop is fine). On
macOS with Homebrew Python, use a throwaway venv to sidestep PEP 668:

```bash
python3 -m venv /tmp/petsafe-venv
/tmp/petsafe-venv/bin/pip install petsafe-api
/tmp/petsafe-venv/bin/python -m petsafe your.email@example.com
# Check your email for a 6-digit code, paste it in.
# The command prints id_token, refresh_token, access_token.
```

Copy the three tokens — they go directly into the component config
below. You can delete the venv when you're done.

## Configuration

Add a Generic component with model `viam:petsafe:smart-feed`:

```json
{
  "name": "feeder",
  "type": "generic",
  "model": "viam:petsafe:smart-feed",
  "attributes": {
    "email": "your.email@example.com",
    "tokens": {
      "id_token": "...",
      "refresh_token": "...",
      "access_token": "..."
    },
    "feeder_id": "optional-specific-feeder-id",
    "target_meal_cups": 1,
    "state_path": "~/.viam/petsafe-smart-feed-state.json"
  }
}
```

If `feeder_id` is omitted, the first feeder on the account is used —
fine if you only have one.

`target_meal_cups` (optional) is the pet's normal meal size in cups.
Clients use it to default the Feed Now amount and to visually compare
against scheduled amounts. It's echoed back in `status` responses.

`state_path` (optional, default `~/.viam/petsafe-smart-feed-state.json`)
is where the module stores pending auto-restore state for `pause_until`,
`delay_next`, `skip_next`, and `feed_now`. See "Auto-restore" below.

Tokens live inline in the machine config; they're stored in Viam Cloud
alongside the rest of the config. The refresh token typically lasts
~30 days, at which point you'll need to re-run the token dance and
update the config. Access tokens are refreshed in memory during
runtime but are not persisted back to config, so the original tokens
you paste in are what's used every time the module restarts.

## Commands

All commands are dispatched via `do_command`.

### Feed

```json
{ "command": "feed", "cups": 0.5, "slow": false }
```

`cups` is in cup units. PetSafe internally rounds to 1/8-cup increments;
the smallest possible dispense is 0.125 cups. `slow` is optional and
defaults to `false`.

Response:
```json
{ "ok": true, "cups": 0.5, "slow": false }
```

### Status

```json
{ "command": "status" }
```

Response:
```json
{
  "id": "PFD00-...",
  "name": "Kitchen Feeder",
  "battery_level": 87,
  "food_low_status": 0,
  "food_state": "ok",
  "is_paused": false,
  "is_slow_feed": false,
  "target_meal_cups": 1,
  "cached": false
}
```

`food_low_status`: `0` = has food, `1` = low, `2` = out.

`cached: true` means you received the cached value (see rate limiting).

### Schedule

```json
{ "command": "schedule" }
```

Response:
```json
{
  "schedules": [
    { "id": "123456", "time": "07:00", "amount_eighths": 8, "cups": 1.0 },
    { "id": "234567", "time": "18:00", "amount_eighths": 8, "cups": 1.0 }
  ],
  "cached": false
}
```

Times are 24-hour local. `amount_eighths` is the raw PetSafe unit (1 =
1/8 cup); `cups` is the same value in cups for convenience. Schedules
recur daily — there's no date component.

Cached separately from `status` on the same 5-minute TTL.

### Last feeding

```json
{ "command": "last_feeding" }
```

Returns the most recent `FEED_DONE` event from PetSafe's message log
for the feeder (last 7 days).

Response:
```json
{
  "last_feeding": {
    "message_type": "FEED_DONE",
    "created_at": "2026-09-17T18:00:00Z",
    "payload": { "amount": 8 }
  },
  "cached": false
}
```

`last_feeding` is `null` if no feeding was found in the past 7 days.
Cached for 5 minutes.

### Pause schedule

```json
{ "command": "pause_schedule", "paused": true }
```

Pauses (or unpauses, with `paused: false`) all scheduled feedings.
Manual `feed` commands still work while paused.

Response:
```json
{ "ok": true, "paused": true }
```

### Add schedule

```json
{ "command": "add_schedule", "time": "07:00", "cups": 1 }
```

Creates a new scheduled feeding. `time` is 24-hour local. `cups` is
rounded to the nearest 1/8 cup (minimum dispense is 0.125).

Response:
```json
{
  "ok": true,
  "schedule": { "id": "123456", "time": "07:00", "amount_eighths": 8, "cups": 1.0 }
}
```

### Modify schedule

```json
{ "command": "modify_schedule", "id": "123456", "time": "07:30", "cups": 0.5 }
```

Response:
```json
{ "ok": true, "id": "123456", "time": "07:30", "amount_eighths": 4, "cups": 0.5 }
```

### Delete schedule

```json
{ "command": "delete_schedule", "id": "123456" }
```

Response:
```json
{ "ok": true, "id": "123456" }
```

### Pause until

```json
{ "command": "pause_until", "until": "2026-09-25T12:00:00" }
```

Pauses all scheduled feedings and records a wake time in the state
file. A background loop wakes every minute and unpauses schedules
once the wake time has passed — even if the app is never opened.
Naive datetimes are interpreted as machine local time; ISO strings
with a timezone (e.g. `...+00:00`) are honored as-is.

Response:
```json
{ "ok": true, "pause_until": "2026-09-25T16:00:00+00:00" }
```

A manual `pause_schedule` with `paused: false` clears any pending
`pause_until`.

### Delay next

```json
{ "command": "delay_next", "hours": 1 }
```

Finds the next upcoming scheduled feeding (spanning to tomorrow if
none remain today) and shifts its time by `hours` — positive delays,
negative moves earlier. Records an auto-restore in the state file so
the background loop puts the original time back after the moved
firing completes (plus a 15-minute margin). Rejects a value that
would put the resulting time in the past.

Response:
```json
{
  "ok": true,
  "schedule_id": "123456",
  "moved_to": "20:30",
  "restore_at": "2026-09-17T20:45:00+00:00"
}
```

### Skip next

```json
{ "command": "skip_next" }
```

Deletes the next upcoming scheduled feeding and records an
auto-restore. The background loop re-adds the entry with the
original time and amount after its "would-have-fired" moment plus
the restore margin.

Response:
```json
{
  "ok": true,
  "skipped_time": "18:00",
  "skipped_cups": 1.0,
  "restore_at": "2026-09-17T18:15:00+00:00"
}
```

### Feed now

```json
{ "command": "feed_now" }
```

Composite action: feeds the pet with the next scheduled meal's
amount **and** skips that scheduled entry (auto-restored later).
Falls back to `target_meal_cups` from config if no upcoming
schedule exists.

Response:
```json
{
  "ok": true,
  "fed_cups": 1.0,
  "skip": {
    "original_time": "18:00",
    "restore_at": "2026-09-17T18:15:00+00:00"
  }
}
```

## Auto-restore state

Time-triggered commands (`pause_until`, `delay_next`, `skip_next`,
`feed_now`) modify the schedule on PetSafe's side and record a
pending "put it back" action in `state_path` (default
`~/.viam/petsafe-smart-feed-state.json`).

A background loop wakes every 60 seconds and processes any expired
entries. Idle ticks make zero PetSafe calls — the loop only reaches
PetSafe when it actually has work to do. Any failed restore is
retried on the next tick, so a transient PetSafe outage doesn't
leave the schedule permanently modified.

If the machine is off during a restore window, the entry stays
pending — the loop restores it on the first tick after the machine
boots.

`status` responses include a snapshot of the current state:
`pause_until`, `delayed_schedule_ids`, `skipped_count`.

## Rate limiting

**PetSafe locks your account** if you make data reads more than once
per 5 minutes. `status` responses are cached for exactly this reason.
Do not shorten the cache TTL. Do not poll `status` from clients in a
tight loop; the module already refuses to hit the API more than once
per 5 minutes and will return cached data instead.

Write operations (`feed`) are not rate-limited.

## Development

```bash
make setup           # create venv, install deps
make lint            # ruff
make module          # build module.tar.gz for upload
```

## Releases

`main` auto-releases: every merge to `main` bumps the patch version
(`v0.0.N` → `v0.0.N+1`), tags the commit, and uploads the tarball to
the Viam module registry. For a manual minor or major bump, use the
`workflow_dispatch` input on the Release workflow.
