# WebSocket Protocol

Bridge listens on `:8765`. Gaze and head-pose message types were removed — the standard iPad has no TrueDepth sensor.

## iPad → PC (26 types)

| Category | Message types |
|----------|--------------|
| Sensor streams | `tilt` `tilt_position` `tilt_tap` `tilt_ratchet` `keyword` `audio_stream` `camera_frame` `depth_frame` |
| Direct control | `touch_command` `trackpad` `handwriting_image` `dwell_click` `ping` |
| Settings/UX | `set_dwell_action` `set_feature_toggle` `sensor_switch` `cursor_pause` `cursor_resume` `gesture_assessment` `pain_day_override` `flare_profile` `calibration_start` `calibration_cancel` `mic_mute` |
| A2UI | `a2ui_event` (interactive-surface tap/click/canvas/clarify response) |
| Diagnostics | `ipad_log` |

## PC → iPad (12 types)

| Type | When sent |
|------|-----------|
| `ack` | Every message received |
| `pong` | Reply to `ping` (latency measurement) |
| `status` | After each command (window + cursor state) |
| `screenshot` | After SCREENSHOT action (base64 PNG) |
| `handwriting_result` | After `handwriting_image` (LaTeX + unicode) |
| `recalibration_request` | Voice drift / seasonal re-cal trigger → QuickRecalSheet on iPad |
| `mic_state` | Mute/unmute echo for two-way `MicMuteIndicator` sync |
| `calibration_result` | Per-phrase voice-calibration progress |
| `calibration_phrase` | Next phrase prompt during voice calibration |
| `calibration_complete` | Voice-calibration session finished |
| `calibration_error` | Voice-calibration session error |
| `a2ui_clear` | Tear down an interactive A2UI surface |

## Routing notes

- `touch_command` and `trackpad` bypass FusionEngine entirely
- `handwriting_image` is handled inline by the bridge (never reaches FusionEngine)
- `audio_stream` feeds WhisperStream → FusionEngine priority 6
- `depth_frame` + `camera_frame` sent by `LiDARStreamer.swift` (enabled via `lidarEnabled` toggle) → `LiDARReceiver` and `GestureProcessor`
- `set_feature_toggle` is wired but currently has no valid features (all prior toggles were gaze features)
- `ipad_log` batches structured `AppLogger` entries; warning+ entries are persisted to `ipad_logs` AgentDB table
- All other sensor types (tilt, keyword, etc.) dispatch to FusionEngine
