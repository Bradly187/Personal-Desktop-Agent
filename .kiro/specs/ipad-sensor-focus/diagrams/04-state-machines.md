# State Machine Diagrams — iPad-Focused Architecture

State machines for both the iPad app (Swift) and the PC agent backend (Python).
All diagrams use [Mermaid](https://mermaid.js.org/) `stateDiagram-v2` syntax.

> **Currency:** refreshed 2026-06-13. Gaze-dwell, head-pose, and mouth-sound
> machines were **removed** (the standard iPad lacks a TrueDepth sensor; the
> sound surface was unreliable). The FusionEngine is now **6-level**. The
> backend agent machines (§6–§12) reflect the post-AIOS-alignment kernel:
> circuit breaker, gyro calibrator, resource governor, supervisor, and the
> durable goal/run lifecycles.

**Contents**

| # | Machine | Layer | Source |
|---|---------|-------|--------|
| 1 | App top-level lifecycle | iPad (Swift) | `DesktopAgentApp`, `SensorManager` |
| 2 | WebSocket connection | iPad (Swift) | `WebSocketManager.swift` |
| 3 | Tilt navigation | iPad (Swift) | `TiltSensor.swift` |
| 4 | Keyword listener | iPad (Swift) | `KeywordListener.swift` |
| 5 | UI view mode | iPad (Swift) | SwiftUI views |
| 6 | FusionEngine 6-level tick | PC | `core/fusion_engine.py` |
| 7 | Gyro bias calibrator | PC | `calibration/gyro_bias_calibrator.py` |
| 8 | Circuit breaker | PC | `core/circuit_breaker.py` |
| 9 | Resource governor (flare) | PC | `core/resource_governor.py` |
| 10 | Supervisor (per subsystem) | PC | `core/supervisor.py` |
| 11 | Goal-queue row lifecycle | PC | `storage/db.py` (`goal_queue`) |
| 12 | Agent-run lifecycle | PC | `storage/db.py` (`agent_runs`) |

---

## 1. iPadApp — Top-Level Application Lifecycle

```mermaid
stateDiagram-v2
    [*] --> LAUNCHING

    LAUNCHING --> SEARCHING : App finished launching\nStart Bonjour/mDNS discovery

    SEARCHING --> CONNECTING : PC service found\nor manual IP entered

    CONNECTING --> PAIRING : WebSocket handshake (101)\nsend X-Agent-Token / ?token=

    CONNECTING --> SEARCHING : Timeout (10s)\nretry with backoff

    PAIRING --> CONNECTED : Token accepted by bridge
    PAIRING --> BLOCKED : 401 / token rejected

    BLOCKED --> CONNECTING : User sets pairing token\nin Settings, retry

    CONNECTED --> SENSOR_INIT : Receive ack/status\nfrom PC

    SENSOR_INIT --> ACTIVE : Available sensors\nstarted successfully

    ACTIVE --> ACTIVE : Normal operation\n(sensor streaming, touch events)

    ACTIVE --> RECONNECTING : WebSocket drops

    RECONNECTING --> CONNECTING : Exponential backoff\n(1s, 2s, 4s, 8s…)

    ACTIVE --> [*] : App background / terminate\nstop all sensors

    note right of PAIRING
        Bridge requires a pairing token
        (hmac.compare_digest before ws.prepare).
        Token stored in SettingsStore.pairingToken;
        redacted from logs. No hot-loop on 401.
    end note

    note right of RECONNECTING
        Visual indicator shown to user.
        Sensors keep running locally;
        output is buffered.
    end note
```

---

## 2. WebSocketManager — Connection State Machine (Swift)

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> DISCOVERING : App enters foreground\nBonjour/mDNS scan starts

    DISCOVERING --> CONNECTING : PC found on network\nor manual IP set

    CONNECTING --> CONNECTED : 101 Switching Protocols\n+ token accepted

    CONNECTING --> BACKING_OFF : Timeout (10s),\nrefused, or token rejected

    BACKING_OFF --> CONNECTING : Backoff delay elapsed\n(1s → 2s → 4s → 8s → 30s max)

    CONNECTED --> SENDING : send(SensorMessage) called
    SENDING --> CONNECTED : Frame sent

    CONNECTED --> BACKING_OFF : Connection error\nor server closed

    CONNECTED --> IDLE : User disconnects manually

    IDLE --> [*] : App terminates

    note right of CONNECTED
        Status indicator:
        green = connected
        yellow = reconnecting
        red = disconnected / blocked
    end note
```

---

## 3. TiltSensor (Core Motion) — Navigation State Machine

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> MONITORING : start() called\nCMMotionManager at 60Hz

    MONITORING --> IN_DEAD_ZONE : |rx| < dead_zone\nAND |ry| < dead_zone

    MONITORING --> TILTING : |rx| or |ry|\nexceeds dead_zone

    IN_DEAD_ZONE --> TILTING : Tilt exceeds dead_zone

    TILTING --> IN_DEAD_ZONE : Returns inside dead_zone\n(stop sending tilt messages)

    TILTING --> TILTING : Send tilt vector\nevery 16ms (60Hz)

    MONITORING --> TAP_DETECTED : Accelerometer detects\nsharp impulse (table tap)

    TAP_DETECTED --> MONITORING : Send tilt_tap event\nto WebSocket

    MONITORING --> STOPPED : stop() called

    note right of IN_DEAD_ZONE
        No messages sent to PC.
        Cursor stops moving.
        Prevents drift from slight tremor.
        Gyro bias subtracted upstream
        (see §7 GyroBiasCalibrator).
    end note

    note right of TAP_DETECTED
        Sends {"type":"tilt_tap"}.
        PC clicks at the current cursor
        position (magnetic snap applied).
    end note
```

---

## 4. KeywordListener (Speech Framework) — Recognition State Machine

```mermaid
stateDiagram-v2
    [*] --> STOPPED

    STOPPED --> LISTENING : start() called\nSFSpeechRecognizer ready

    LISTENING --> RECOGNIZING : Speech detected\n(audio above threshold)

    RECOGNIZING --> KEYWORD_FIRED : New transcript content\nmatches keyword & cooldown elapsed

    RECOGNIZING --> KEYWORD_SUPPRESSED : Keyword found but\ncooldown (0.5s) still active

    RECOGNIZING --> NO_MATCH : Recognition completes,\nno keyword matched

    KEYWORD_FIRED --> RECOGNIZING : Send keyword event\n(no restart — keep recognizing)

    KEYWORD_SUPPRESSED --> RECOGNIZING : Skip duplicate\nwithin 0.5s

    RECOGNIZING --> LISTENING : Recognition ends\n(timeout/error) → auto-restart

    NO_MATCH --> STREAMING_AUDIO : Buffer audio,\nstream to PC for Whisper

    STREAMING_AUDIO --> LISTENING : audio_stream sent\nvia WebSocket

    LISTENING --> STOPPED : stop() called

    note right of KEYWORD_FIRED
        Fast path — no PC round-trip.
        Incremental scan: only new
        transcript content is evaluated,
        so partial results don't re-fire.
    end note
```

---

## 5. AppMode — UI View State Machine (SwiftUI)

```mermaid
stateDiagram-v2
    [*] --> COMMAND_PAD

    COMMAND_PAD --> FULL_TRACKPAD : Edge swipe\nor mode button
    FULL_TRACKPAD --> COMMAND_PAD : Edge swipe\nor mode button

    COMMAND_PAD --> WRITE : Tab → Write\n(Math / Text)
    WRITE --> COMMAND_PAD : Tab back

    COMMAND_PAD --> SETTINGS : Settings button
    FULL_TRACKPAD --> SETTINGS : Settings button
    SETTINGS --> COMMAND_PAD : Done / back

    state COMMAND_PAD {
        [*] --> NORMAL
        NORMAL --> DWELL_COUNTDOWN : Finger rests on button\n(dwell enabled)
        DWELL_COUNTDOWN --> NORMAL : Finger lifted\nbefore timer
        DWELL_COUNTDOWN --> TRIGGERED : Timer expires
        TRIGGERED --> NORMAL : Command sent
    }

    state FULL_TRACKPAD {
        [*] --> IDLE_PAD
        IDLE_PAD --> DRAGGING : Touch down
        DRAGGING --> IDLE_PAD : Touch up
        IDLE_PAD --> TAPPED : Single tap
        TAPPED --> IDLE_PAD : Left click sent
    }

    note right of FULL_TRACKPAD
        Full screen = mouse.
        Palm rejection active.
        iPad flat on desk use case.
    end note
```

---

## 6. FusionEngine (PC) — 6-Level Priority Tick

The 60 Hz tick evaluates inputs in strict priority order; the first match wins
and the rest are skipped for that tick. *(Gaze dwell, gaze+voice, gaze+gesture,
head-pose, and mouth-sound branches were all removed — 10 levels → 6.)*

```mermaid
stateDiagram-v2
    [*] --> WAITING

    WAITING --> TICK : 60Hz timer fires

    state TICK {
        [*] --> CHECK_TOUCH
        CHECK_TOUCH --> EMIT_TOUCH : Touch command pending
        CHECK_TOUCH --> CHECK_VOICE_CLICK : No touch

        CHECK_VOICE_CLICK --> EMIT_VOICE_CLICK : Voice "click" keyword\n(click at cursor)
        CHECK_VOICE_CLICK --> CHECK_TILT : No voice-click

        CHECK_TILT --> EMIT_TILT : Tilt active\n(3a position / 3b velocity)
        CHECK_TILT --> CHECK_GESTURE : Inside dead zone

        CHECK_GESTURE --> EMIT_GESTURE : Gesture command pending
        CHECK_GESTURE --> CHECK_VOICE_LOCAL : No gesture

        CHECK_VOICE_LOCAL --> EMIT_VOICE_LOCAL : On-device keyword pending
        CHECK_VOICE_LOCAL --> CHECK_VOICE_PC : No local keyword

        CHECK_VOICE_PC --> EMIT_VOICE_PC : Whisper transcript pending
        CHECK_VOICE_PC --> EMIT_NONE : Nothing pending
    }

    TICK --> WAITING : Emitted (or nothing)

    note right of EMIT_TILT
        Tilt emits directly to pyautogui —
        cursor movement, not a Command.
        Cursor gravity (_apply_gravity) is
        applied at end of a position tick.
    end note

    note right of EMIT_VOICE_PC
        PC-transcribed voice → HybridCoordinator
        → 4-gate routing → CommandExecutor.
        Magnetic snap reads the target cache.
    end note
```

---

## 7. GyroBiasCalibrator — Tilt Drift Compensation

Detects stationary periods, averages the gyro zero-rate offset, and lerps to the
new bias so tilt-velocity mode doesn't drift. Source: `BiasState` enum.

```mermaid
stateDiagram-v2
    [*] --> UNCALIBRATED

    UNCALIBRATED --> COLLECTING : Stationary ≥ 1.0s\n(|rx|,|ry| < 0.02 rad/s)

    COLLECTING --> CALIBRATED : ≥ max_samples (200)\nOR motion after ≥ min_samples (50)

    COLLECTING --> UNCALIBRATED : Motion before min_samples\n(discard, restart)

    CALIBRATED --> FROZEN : Motion detected\n(stop adjusting bias)

    FROZEN --> COLLECTING : New stationary ≥ 1.0s\n(recalibrate)

    note right of UNCALIBRATED
        should_suppress() zeroes small
        velocities (< 0.05 rad/s) so an
        unknown bias can't drift the cursor.
    end note

    note right of CALIBRATED
        On finalize: if |Δbias| > 0.005,
        lerp old→new over 0.5s; else apply
        immediately. get_current_bias()
        interpolates during the lerp.
    end note
```

---

## 8. CircuitBreaker — Inference Backend Latch

Stops a down backend from costing a full timeout on every call. Source:
`core/circuit_breaker.py` (`closed` / `open` / `half_open`).

```mermaid
stateDiagram-v2
    [*] --> closed

    closed --> open : fail_threshold (3)\nconsecutive failures

    open --> half_open : allow() after cooldown (30s)\nadmit ONE probe (probe_gen++)

    half_open --> closed : probe record_success()
    half_open --> open : probe record_failure()

    half_open --> half_open : Probe lost (no outcome\nin cooldown) → admit fresh probe

    closed --> closed : record_success()\n(reset failure count)

    note right of open
        allow() returns False → caller
        fast-fails to the fallback path,
        no network attempt, no wait.
    end note

    note right of half_open
        Outcomes carry a probe generation;
        a superseded (lost) probe's late
        success/failure is ignored (#16).
    end note
```

---

## 9. ResourceGovernor — Pain-Aware Flare Mode

A kernel primitive that reacts to the pain-day score with hysteresis. Source:
`core/resource_governor.py` (activate ≥ 0.6, deactivate < 0.4).

```mermaid
stateDiagram-v2
    [*] --> NORMAL

    NORMAL --> FLARE : pain_day_score ≥ 0.6\n(_on_flare_start)

    FLARE --> NORMAL : pain_day_score < 0.4\n(_on_flare_end, hysteresis)

    NORMAL --> NORMAL : 0.4 ≤ score < 0.6\n(no change — dead band)
    FLARE --> FLARE : 0.4 ≤ score < 0.6\n(stay in flare)

    note right of FLARE
        - Relax sensor thresholds
        - Pause codebase indexer
        - Raise Whisper VAD thread priority
        - Evict heavy specialist models
          (router-derived set, keep_alive=0)
        - scheduler.pause_dev() — gate new
          dev/background admission
        Accessibility path is NEVER gated.
    end note

    note right of NORMAL
        On recovery: restore thresholds,
        resume indexer, scheduler.resume_dev().
        Manual flare toggle fires start/end
        in <100ms via call_soon_threadsafe.
    end note
```

---

## 10. Supervisor — Per-Subsystem Restart Policy

One-for-one liveness watchdog (Erlang-style) over critical loops (scheduler
worker, governor loop). Source: `core/supervisor.py`.

```mermaid
stateDiagram-v2
    [*] --> HEALTHY

    HEALTHY --> HEALTHY : is_alive() && enabled()\n(2s poll — nothing to do)

    HEALTHY --> DISABLED : enabled() == False\n(deliberate teardown)
    DISABLED --> HEALTHY : Re-enabled

    HEALTHY --> RESTARTING : Dead but enabled\n(task died unexpectedly)

    RESTARTING --> HEALTHY : restart() succeeded\n(within budget)

    RESTARTING --> FAILED : Exceeded budget\n(5 / 60s OR 20 total)

    FAILED --> [*] : Latched — no further restarts

    note right of RESTARTING
        Restart timestamps pruned to a
        sliding 60s window before the
        budget check.
    end note

    note right of FAILED
        Fires on_failed(name) ONCE → main.py
        speaks a TTS warning and degrades
        (e.g. scheduler FAILED →
        fusion.set_scheduler(None), direct
        dispatch; accessibility unaffected).
    end note
```

---

## 11. Goal-Queue Row Lifecycle

Durable goal backlog with N+2 proactivity (future-dated + recurring goals).
Source: `storage/db.py` (`goal_queue`).

```mermaid
stateDiagram-v2
    [*] --> scheduled : enqueue_scheduled_goal()\n(execute_at in future)
    [*] --> queued : enqueue_goal()\n(run ASAP)

    scheduled --> queued : ProactiveScheduler promotes\nwhen now ≥ execute_at
    scheduled --> cancelled : cancel_scheduled_goal()

    queued --> running : claim_next_goal()\n(owner_pid, claimed_at, attempts++)

    running --> done : complete_goal('done')
    running --> failed : complete_goal('failed')
    running --> cancelled : complete_goal('cancelled')

    running --> queued : requeue_stale_running()\n(owner_pid dead, attempts < max)
    running --> failed : requeue_stale_running()\n(attempts ≥ max — poison guard)

    done --> scheduled : recurrence re-lay\n(daily/interval next occurrence)

    note right of running
        owner_pid lets requeue_stale_running
        recover only goals whose claiming
        process is no longer alive (pid check).
    end note

    note right of queued
        Hot drain path sees ONLY 'queued'
        rows — scheduled/running are invisible
        to claim_next_goal, so promotion is
        the single entry into the drain.
    end note
```

---

## 12. Agent-Run Lifecycle

Tracks each DevAgent plan execution with crash reconciliation. Source:
`storage/db.py` (`agent_runs`) + `inference/dev_agent.py`.

```mermaid
stateDiagram-v2
    [*] --> running : start_run()\n(insert status='running')

    running --> completed : finish_run(succeeded=True)
    running --> failed : finish_run(succeeded=False)
    running --> cancelled : finish_run(cancelled=True)

    running --> interrupted : startup reconciliation\n(process crashed mid-run)

    interrupted --> running : resume_pending_plan()\n(voice-gated "resume task")
    interrupted --> [*] : Left for human review

    note right of interrupted
        mark_interrupted_runs() flips every
        stale status='running' row to
        'interrupted' at startup, so a crash
        never leaves a run falsely "running".
    end note

    note right of failed
        A max_replans / max_steps halt rolls
        back (saga), then _record_escalation
        persists the goal to dev_escalations
        for human review ("review queue").
    end note
```

---

## Removed machines (historical note)

These were documented in earlier revisions and have been **deleted** from the
codebase — do not re-add diagrams for them:

- **GazeTracker dwell** — gaze tracking removed 2026-05-30 (no TrueDepth sensor).
- **HeadTracker / head-pose** — removed 2026-05-30 (same reason).
- **SoundDetector (cluck/pop/hiss)** — removed 2026-06-05 (unreliable surface).

The FusionEngine priority dropped from 10 levels to 6 as a result (see §6).
