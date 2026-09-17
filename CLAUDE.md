# CLAUDE.md

## Working in this repo

- Default to no code comments; add terse one-liners only when the WHY (not the WHAT) is non-obvious from the code.
- Do NOT bandaid over upstream bugs or platform gaps in any dependency — raise them immediately with a severity (critical / high / medium / low).

## Platform shortcomings

Known gaps this module cannot fix on its own. Do not paper over them — reference this section when you hit them and, if new ones surface, add them here.

- **PetSafe has no public developer API (severity: high).** All PetSafe integrations, including [ThomasHFWright/petsafe-api](https://github.com/ThomasHFWright/petsafe-api), are reverse-engineered from the mobile app's AWS-Cognito-backed cloud endpoints. PetSafe has actively made this harder in the past (obfuscation, SSL cert pinning). Expect the library to break on a PetSafe app update someday. When it does, escalate — do not try to patch the wire format in this repo.

- **5-minute account lockout on frequent reads (severity: high).** PetSafe locks accounts that read device data more than once per 5 minutes. The `_status` cache TTL (`STATUS_CACHE_TTL_SEC = 300`) is a hard requirement, not a preference. Do not shorten it. Do not add new read commands without either sharing the same cache or introducing an equivalent one.

- **`petsafe-api` feeder methods are synchronous (severity: low).** The library's Smart Feed API is blocking; only Smart Door methods are async. Every feeder call in this module wraps the underlying call in `asyncio.to_thread` to avoid stalling Viam's event loop. If a new feeder method is added upstream as async, drop the wrapper — do not leave a `to_thread` call around an already-async method.

- **No browser JS SDK for PetSafe (severity: medium).** Clients calling this module must go through the Viam SDK / `do_command`; there is no browser-side alternative. If someone wants a direct-from-frontend flow, this module is the only path.
