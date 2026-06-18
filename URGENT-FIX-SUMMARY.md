# URGENT FIXES COMPLETED — 2026-06-18

## Two CRITICAL Network Security Gaps Fixed

### Summary
- **Fix 1:** Added Bearer token authentication to `remote_whisper_service.py` (was accepting requests from any LAN device without verification)
- **Fix 2:** Removed hardcoded secrets from config examples; added `.gitignore` guards and pre-commit hook secret scanning

### Files Changed (8 total)

1. **sensors/remote_whisper_service.py** — Added `_make_auth_middleware()` (copies indexer C2 pattern), changed default `--host` from `0.0.0.0` → `127.0.0.1`, added `--token` / `WHISPER_TOKEN` env var support, fail-closed on non-loopback without token
2. **sensors/remote_whisper_client.py** — Added `token` parameter, Authorization header support
3. **sensors/whisper_stream.py** — `set_remote_url()` now accepts `token` parameter
4. **inference/remote_indexer_service.py** — Changed default `--host` from `0.0.0.0` → `127.0.0.1`
5. **core/cluster_config.py** — Updated example (removed hardcoded token), added `laptop_whisper_token` field, added warnings in docstring/field comments
6. **.gitignore** — Added `cluster_config.json` and `approval_config.json` entries
7. **.git/hooks/pre-commit** — New bash hook scans for secret patterns (Anthropic API keys, AWS credentials, bearer tokens, etc.); blocks commits with secrets
8. **main.py** (L885) — Updated to pass `laptop_whisper_token` from cluster config to WhisperStream

### Tests Added
**tests/test_whisper_service_auth.py** — 18 comprehensive tests covering:
- Token generation and Authorization header passing
- Middleware auth enforcement on `/transcribe` and `/devices` endpoints
- `/health` endpoint remains open (no token required)
- Constant-time token comparison (HMAC)
- Config secrets removal verification
- `.gitignore` blocking of sensitive files
- Pre-commit hook existence and patterns

**All 18 tests pass** ✓

### Key Changes for Deployments

#### Loopback (Single Machine)
- **Before:** Whisper service listened on `0.0.0.0`, accepting requests from anywhere
- **After:** Defaults to `127.0.0.1` (loopback), no token required
- **Migration:** No action needed if running on a single machine

#### Multi-Machine (Laptop Cluster)
- **Before:** Service required explicit `--host`, no auth, secrets could be hardcoded in git
- **After:** Service binds to specified `--host` (or `127.0.0.1`), requires `WHISPER_TOKEN` env var
- **Migration:** 
  1. Set `export WHISPER_TOKEN="your-secret"` on laptop and desktop
  2. Remove any hardcoded tokens from `cluster_config.json`
  3. Restart services

### Backward Compatibility
- ✓ Loopback deployments: no token required (default changed to 127.0.0.1)
- ✓ Multi-machine: token is **required**; service refuses to start without it on non-loopback interfaces
- ✓ Indexer service: already had auth (C2) in place; changed default host for consistency

### Next Steps (High-Priority Follow-Ups)
1. **FINDING 4 (HIGH):** Silent audit trail gaps in approval/execution paths → durable audit logging for critical failures
2. **FINDING 3 (HIGH):** Route-task circuit breaker + in-flight metrics → prevent accessibility lag during acoustic storms
3. **FINDING 5 (MEDIUM):** Ollama semaphore timeout → prevent 10+ minute inference hangs

---

## How to Review

1. **Security:** Read `docs/SECURITY-FIXES-2026-06-18.md` (full deployment + migration guide)
2. **Testing:** Run `pytest tests/test_whisper_service_auth.py -v` (all 18 tests should pass)
3. **Code:** Review the 8 file changes (all are defensive, no behavioral changes for loopback deployments)
4. **Integration:** Verify cluster config loads correctly: `python -c "from core.cluster_config import ClusterConfig; print(ClusterConfig.load())"`

---

## Threat Model

**Before:**
- Any LAN device could POST audio to the Whisper service without authentication
- Audio samples could be exfiltrated for voice profiling / PII recovery
- Hardcoded secrets could accidentally be committed to git

**After:**
- Whisper service requires Bearer token on non-loopback interfaces (C2 authentication)
- Secrets are stored in environment variables, not version control
- Pre-commit hook prevents accidental secret commits (with clear blocking message)

---

## Verification Checklist

- [x] All imports clean (no syntax errors)
- [x] 18 new auth tests pass (middleware, client, config, git guards)
- [x] Backward compatible (loopback deployments unaffected)
- [x] Documentation complete (migration guide + deployment instructions)
- [x] Pre-commit hook blocks secrets (tested locally)
- [x] Config example updated (no hardcoded tokens)

Ready for deployment.
