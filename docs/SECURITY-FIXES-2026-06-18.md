# Urgent Security Fixes — 2026-06-18

## Overview
Fixed two CRITICAL network security gaps in multi-machine cluster deployments:

1. **Unauthenticated Whisper service** — was accepting transcription requests from any LAN device without verification
2. **Hardcoded secrets in config examples** — encouraged users to commit bearer tokens to version control

---

## Fix 1: Whisper Service Authentication (C2)

### What Changed
- `sensors/remote_whisper_service.py`: Added Bearer token middleware (modeled on the existing indexer C2 implementation)
- `sensors/remote_whisper_client.py`: Now accepts and passes `Authorization: Bearer <token>` header
- Default bind address changed from `0.0.0.0` to `127.0.0.1` (loopback)
- Token sourced from `WHISPER_TOKEN` environment variable or `--token` CLI argument

### How to Deploy

**On the laptop (RTX 4070 inference node):**

1. Set the environment variable:
   ```bash
   export WHISPER_TOKEN="your-shared-secret-token-here"
   ```

2. Start the Whisper service (token will be required):
   ```bash
   python sensors/remote_whisper_service.py --port 8888
   # Logs: "RemoteWhisperService listening on 127.0.0.1:8888 (auth: token-required)"
   ```

   OR for multi-machine with explicit network binding:
   ```bash
   python sensors/remote_whisper_service.py --host 192.168.18.12 --port 8888
   # Must have WHISPER_TOKEN env var set (fail-closed)
   ```

**On the desktop (Windows PC):**

Ensure `cluster_config.json` does NOT include the token (use env var instead):
```json
{
  "laptop": {
    "hostname": "Brad_Laptop",
    "whisper_url": "http://192.168.18.12:8888"
  }
}
```

The token is automatically sourced from `WHISPER_TOKEN` env var at runtime.

### Backward Compatibility
- **Loopback deployments (single machine):** No token required — use `--host 127.0.0.1` (new default)
- **Multi-machine with auth:** Token is **required**; service refuses to start without it on non-loopback interfaces
- **Existing tests:** Update `test_remote_whisper_smoke.py` to pass token if testing cross-machine:
  ```python
  c = RemoteWhisperClient("http://192.168.18.12:8888", token=os.environ.get("WHISPER_TOKEN"))
  ```

---

## Fix 2: Configuration Secret Hardening

### What Changed
- `.gitignore`: Added explicit entries for `cluster_config.json` and `approval_config.json`
- `core/cluster_config.py`:
  - Updated example to **NOT** include hardcoded tokens
  - Added warnings in docstring and field comments
  - Added new `laptop_whisper_token` field (for consistency with `laptop_indexer_token`)
- Pre-commit hook (`.git/hooks/pre-commit`):
  - Scans staged commits for secret patterns (API keys, bearer tokens, AWS credentials)
  - Blocks commits if secrets detected

### How to Use

**Install the pre-commit hook:**

```bash
chmod +x .git/hooks/pre-commit
```

This happens automatically on the first `git commit`; you'll see:
```
❌ Pre-commit check FAILED: potential secret detected in cluster_config.json
   Pattern: Bearer [A-Za-z0-9_-]{32,}
   Offending diff line:
     "indexer_token": "Bearer-your-secret-xyz..."
```

**If you accidentally try to commit a secret:**

1. Remove it from the file
2. Set it as an environment variable instead:
   ```bash
   export INDEXER_TOKEN="your-secret"
   export WHISPER_TOKEN="your-secret"
   ```
3. Re-stage and commit:
   ```bash
   git add cluster_config.json
   git commit -m "..."
   ```

### Secrets Checklist

Never commit to version control:
- ✅ Do: Set `WHISPER_TOKEN`, `INDEXER_TOKEN`, `AWS_BEARER_TOKEN_BEDROCK` as environment variables
- ❌ Don't: Hardcode in `cluster_config.json`, `approval_config.json`, `.env` files, or anywhere in git
- ✅ Do: Document env vars in your deployment scripts / systemd service files
- ❌ Don't: Check those scripts into git either (mark them `.gitignore`d or use a separate secure config management system)

---

## Testing the Fixes

### Test 1: Loopback Whisper (No Token Required)

```bash
# Terminal 1: Start Whisper on loopback (default)
python sensors/remote_whisper_service.py --port 8888

# Terminal 2: Test without token
python -c "
import os, sys, numpy as np
sys.path.insert(0, '.')
from sensors.remote_whisper_client import RemoteWhisperClient
c = RemoteWhisperClient('http://127.0.0.1:8888', timeout=30)
audio = (0.05 * np.random.RandomState(1).randn(16000 * 2)).astype(np.float32)
segs, info = c.transcribe(audio)
print(f'✅ Loopback (no token) OK: {len(segs)} segments')
"
```

### Test 2: Multi-Machine Whisper (Token Required)

```bash
# Terminal 1: Start Whisper with token on 0.0.0.0
export WHISPER_TOKEN="test-secret-xyz"
python sensors/remote_whisper_service.py --host 192.168.18.12 --port 8888
# Logs: "RemoteWhisperService listening on 192.168.18.12:8888 (auth: token-required)"

# Terminal 2: Test WITH token (succeeds)
export WHISPER_TOKEN="test-secret-xyz"
python -c "
import os, sys, numpy as np
sys.path.insert(0, '.')
from sensors.remote_whisper_client import RemoteWhisperClient
c = RemoteWhisperClient('http://192.168.18.12:8888', token=os.environ['WHISPER_TOKEN'], timeout=30)
audio = (0.05 * np.random.RandomState(1).randn(16000 * 2)).astype(np.float32)
segs, info = c.transcribe(audio)
print(f'✅ Multi-machine (token correct) OK: {len(segs)} segments')
"

# Terminal 3: Test WITHOUT token (fails with 401)
python -c "
import os, sys, numpy as np
sys.path.insert(0, '.')
from sensors.remote_whisper_client import RemoteWhisperClient
try:
    c = RemoteWhisperClient('http://192.168.18.12:8888', timeout=5)
    audio = (0.05 * np.random.RandomState(1).randn(16000 * 2)).astype(np.float32)
    segs, info = c.transcribe(audio)
    print('❌ Should have failed (no token)')
except Exception as e:
    if '401' in str(e) or 'unauthorized' in str(e).lower():
        print(f'✅ Multi-machine (no token) correctly rejected: {e}')
    else:
        print(f'❌ Wrong error: {e}')
"
```

### Test 3: Pre-Commit Hook

```bash
# Attempt to commit a secret
echo '{"indexer_token": "Bearer secret-xyz"}' > cluster_config.json
git add cluster_config.json
git commit -m "test"
# Output: ❌ Pre-commit check FAILED: potential secret detected
# Commit is blocked ✅

# Fix: Use env var instead
echo '{}' > cluster_config.json
git add cluster_config.json
git commit -m "test"
# Output: Commit accepted ✅
```

---

## Migration Guide (Existing Deployments)

If you have an existing multi-machine deployment with `cluster_config.json`:

1. **Generate a new shared secret** (if you haven't already):
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   # Output: "abc123XYZ_-..."
   ```

2. **Set environment variables** on both the laptop and desktop:
   ```bash
   # On laptop (RTX 4070 inference node)
   export WHISPER_TOKEN="abc123XYZ_-..."
   export INDEXER_TOKEN="abc123XYZ_-..."

   # On desktop (Windows RTX 5090)
   set WHISPER_TOKEN=abc123XYZ_-...
   set INDEXER_TOKEN=abc123XYZ_-...
   ```

3. **Remove any hardcoded tokens from `cluster_config.json`**:
   ```json
   {
     "laptop": {
       "hostname": "Brad_Laptop",
       "ollama_url": "http://192.168.18.12:11434",
       "whisper_url": "http://192.168.18.12:8888",
       "indexer_url": "http://192.168.18.12:9000"
     },
     "routing": { "lightweight_host": "laptop" }
   }
   ```

4. **Restart the services**:
   ```bash
   # Terminal 1: Laptop Whisper
   python sensors/remote_whisper_service.py --port 8888

   # Terminal 2: Laptop Indexer
   python inference/remote_indexer_service.py --port 9000

   # Terminal 3: Desktop agent
   python main.py
   ```

5. **Verify in logs**:
   - Whisper: "RemoteWhisperService listening on ... (auth: token-required)"
   - Indexer: "RemoteIndexerService listening on ... (auth: token-required)"
   - Desktop: "ClusterConfig: loaded from ... — whisper=http://... indexer=http://..."

---

## Files Modified

1. `sensors/remote_whisper_service.py` — Added auth middleware, changed default host
2. `sensors/remote_whisper_client.py` — Added token parameter, Authorization header support
3. `sensors/whisper_stream.py` — `set_remote_url()` now accepts token parameter
4. `inference/remote_indexer_service.py` — Changed default host from 0.0.0.0 → 127.0.0.1
5. `core/cluster_config.py` — Updated example, added `laptop_whisper_token` field
6. `.gitignore` — Added `cluster_config.json` and `approval_config.json` entries
7. `.git/hooks/pre-commit` — New secret-scanning pre-commit hook
8. `main.py` (L885) — Updated to pass whisper token from cluster config

---

## Related Findings (Follow-Up Work)

- **FINDING 2 (HIGH):** Indexer service also defaults to `0.0.0.0` — ✅ **FIXED** (changed to 127.0.0.1)
- **FINDING 4 (HIGH):** Silent audit trail gaps in approval/execution paths — *Deferred to separate PR*
- **FINDING 3 (HIGH):** Route-task circuit breaker + metrics — *Deferred to separate PR*

---

## References

- C2 (Service Authentication): `inference/remote_indexer_service.py` (existing pattern)
- Cluster offload docs: `CLAUDE.md` "Multi-machine cluster" section
- Pre-commit hook setup: `.git/hooks/pre-commit` (Git hook installed automatically)
