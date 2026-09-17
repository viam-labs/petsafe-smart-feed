# CLAUDE.md

## Working in this repo

- Default to no code comments; add terse one-liners only when the WHY (not the WHAT) is non-obvious from the code.
- Do NOT bandaid over upstream bugs or platform gaps in any dependency — raise them immediately with a severity (critical / high / medium / low).

## Platform shortcomings

Known gaps this module cannot fix on its own. Do not paper over them — reference this section when you hit them and, if new ones surface, add them here.

- **PetSafe has no public developer API (severity: high).** All PetSafe integrations, including [ThomasHFWright/petsafe-api](https://github.com/ThomasHFWright/petsafe-api), are reverse-engineered from the mobile app's AWS-Cognito-backed cloud endpoints. PetSafe has actively made this harder in the past (obfuscation, SSL cert pinning). Expect the library to break on a PetSafe app update someday. When it does, escalate — do not try to patch the wire format in this repo.

- **5-minute account lockout on frequent reads (severity: high).** PetSafe locks accounts that read device data more than once per 5 minutes. The `_status` cache TTL (`STATUS_CACHE_TTL_SEC = 300`) is a hard requirement, not a preference. Do not shorten it. Do not add new read commands without either sharing the same cache or introducing an equivalent one.

- **`petsafe-api` README is out of date (severity: medium).** The README's Smart Feed examples show a sync API (`sf.devices.get_feeders(client)`, `feeder.feed(...)` without await), but the actual code as of v2.x is fully async — methods live on the `PetSafeClient` (`await client.get_feeders()`) and every device method is a coroutine. If you're changing behavior, read `petsafe/client.py` and `petsafe/devices.py` directly, not the README. Upstream issues are disabled on the repo, so no filing path — mention when you update the pinned dep.

- **No browser JS SDK for PetSafe (severity: medium).** Clients calling this module must go through the Viam SDK / `do_command`; there is no browser-side alternative. If someone wants a direct-from-frontend flow, this module is the only path.

- **Refreshed tokens are not persisted (severity: low).** Tokens live inline in the machine config; `petsafe-api` refreshes access tokens in memory during runtime but there's no path to write refreshed values back to Viam config from a running module. Every module restart re-uses the original config-provided tokens. Refresh tokens last ~30 days, at which point the user has to redo the token dance and update config manually. Fixing this properly needs a Viam SDK config-mutation API that doesn't restart the module (chicken-and-egg today).
