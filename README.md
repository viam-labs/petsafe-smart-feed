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

## Architecture

**Scheduling lives on the Pi**, not in PetSafe's cloud. The module
holds an ordered list of schedules in a local state file. A background
loop wakes every minute and fires schedules whose time has arrived,
using PetSafe's ad-hoc `feed()` API (the same one manual "Feed Now"
uses) to dispense.

This is a deliberate departure from the earlier design (which stored
schedules on PetSafe's cloud and let PetSafe fire them). Reasons:

- **Full visibility.** Missed fires are logged and surfaced in `status`
  rather than silently vanishing.
- **Days-of-week.** PetSafe's cloud schedules are time-only; days-of-week
  requires local logic.
- **Editable skip / delay.** Skipping or delaying a schedule flips a
  flag on the schedule itself; the schedule stays visible and editable
  in the dashboard. No hidden auto-restore state you can't cancel.
- **Retry.** Transient PetSafe API failures at fire time are retried
  next tick.

Trade-off: if the Pi is offline at a scheduled fire time, that meal is
missed (up to the catch-up window). Configure alerts on the dashboard's
"missed feeds" status field.

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
    "target_meal_cups": 1.25,
    "catch_up_within_min": 30,
    "state_path": "~/.viam/petsafe-smart-feed-state.json"
  }
}
```

- `feeder_id` — optional. Omit if you only have one feeder on the
  account (module picks the first).
- `target_meal_cups` — required for `feed_now`; the module dispenses
  this amount when the button is hit.
- `catch_up_within_min` (default 30) — after a scheduled fire time, how
  many minutes late we'll still fire the meal. Past that, we log a
  missed feed and move on.
- `state_path` (default `~/.viam/petsafe-smart-feed-state.json`) —
  where schedules and pause state persist.

## Migration from earlier versions

On first boot of v2+, the module deletes any pre-existing schedules
from PetSafe's cloud (they'd otherwise double-fire alongside our own)
and stamps the state file with `schema_version: 2`. The list of local
schedules starts empty. Re-add your schedules through the dashboard
after the module reports `migrated: true` in `status`.

## Commands

All commands are dispatched via `do_command`.

### Feed

```json
{ "command": "feed", "cups": 0.5, "slow": false }
```

`cups` is in cup units. PetSafe internally rounds to 1/8-cup increments;
the smallest possible dispense is 0.125 cups.

`slow` is optional. Omit it (or pass `null`) to defer to the feeder's
own `slow_feed` setting. Pass `true` or `false` to override for this
one call only.

### Feed now

```json
{ "command": "feed_now" }
```

Dispenses `target_meal_cups` immediately, honoring the feeder's slow-feed
setting. Refuses if a feed was recorded in the last 15 minutes (assumes
slow-feed is still dispensing). Does NOT touch schedules — the next
scheduled fire still happens normally.

### Status

```json
{ "command": "status" }
```

Response includes the feeder state plus the local schedule list and
pause state:

```json
{
  "food_state": 0,
  "food_low_status": 0,
  "battery_pct": 87,
  "is_connected": true,
  "target_meal_cups": 1.25,
  "is_slow_feed": true,
  "paused": false,
  "schedules": [ ... ],
  "pause_until": null,
  "migrated": true,
  "cached": false
}
```

### Schedule list

```json
{ "command": "schedule" }
```

Returns the local schedule list:

```json
{
  "schedules": [
    {
      "id": "a1b2c3d4",
      "name": "Breakfast",
      "time": "07:00",
      "cups": 1.25,
      "days_of_week": [0, 1, 2, 3, 4],
      "enabled": true,
      "skip_next_fire": false,
      "delayed_until": null,
      "last_processed_at": "2026-09-18T11:00:03+00:00",
      "last_fired_at": "2026-09-18T11:00:03+00:00"
    }
  ]
}
```

### Add schedule

```json
{
  "command": "add_schedule",
  "schedule": {
    "name": "Breakfast",
    "time": "07:00",
    "cups": 1.25,
    "days_of_week": [0, 1, 2, 3, 4],
    "enabled": true
  }
}
```

`days_of_week` is a list of integers 0..6 (Mon..Sun). Empty or omitted
= every day.

### Modify schedule

```json
{
  "command": "modify_schedule",
  "schedule": { "id": "a1b2c3d4", "cups": 1.5 }
}
```

Any subset of fields; missing ones are preserved.

### Delete schedule

```json
{ "command": "delete_schedule", "id": "a1b2c3d4" }
```

### Enable / disable a schedule

```json
{ "command": "set_schedule_enabled", "id": "a1b2c3d4", "enabled": false }
```

Disabled schedules don't fire.

### Skip the next fire

```json
{ "command": "skip_next" }
```

Sets `skip_next_fire: true` on whichever schedule is up next. That
schedule's next fire is a no-op; the flag then clears. The schedule
itself stays visible and editable — no hidden deletion.

Or target a specific schedule:

```json
{ "command": "set_skip_next", "id": "a1b2c3d4", "skip": true }
```

### Delay the next fire

```json
{ "command": "delay_next", "hours": 1 }
```

Sets `delayed_until` on the next schedule to fire `hours` later than
its natural time. Once fired, the delay clears and the schedule
resumes normal timing.

### Pause / resume all schedules

```json
{ "command": "pause_schedule", "paused": true }
```

Global pause. No schedules fire while paused. `feed_now` still works.

### Pause until

```json
{ "command": "pause_until", "until": "2026-09-25T12:00:00" }
```

Naive datetimes are interpreted as machine local time. The background
loop clears the pause automatically once the time passes.

### Last feeding

```json
{ "command": "last_feeding" }
```

Returns the most recent `FEED_DONE` event from PetSafe's message log.
Cached for 5 minutes.

## Rate limiting

PetSafe locks your account if you make data reads more than once per
5 minutes. `status`, `schedule`, and `last_feeding` responses are
cached for exactly this reason. Write operations (`feed`, and every
scheduled fire) are not rate-limited.

## Development

```bash
make setup           # create venv, install deps
make lint            # ruff
make module          # build module.tar.gz for upload
```

## Releases

`main` auto-releases: every merge to `main` bumps the patch version
(`v0.0.N` → `v0.0.N+1`), tags the commit, and uploads the tarball to
the Viam module registry.
