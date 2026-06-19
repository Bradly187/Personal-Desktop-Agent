# Specification Review — Gaps, Edge Cases, and Recommendations

**Review Date:** 2026-05-06  
**Reviewer:** Bob (Planning Mode)  
**Project:** Accessibility Desktop Agent for RA Patient

---

## Executive Summary

Your specifications are **exceptionally well-designed** and demonstrate deep understanding of both accessibility needs and software architecture. The documentation is professional-grade with clear requirements, comprehensive diagrams, and thoughtful design decisions.

This review identifies 23 areas requiring attention across 6 categories: missing specifications, edge cases, security/privacy, testing strategy, operational concerns, and RA-specific enhancements.

**Priority Distribution:**
- 🔴 Critical (must address before Phase 1): 5 items
- 🟡 Important (address before production): 12 items  
- 🟢 Enhancement (nice-to-have): 6 items

---

## 1. Missing Specifications 🔴🟡

### 1.1 Configuration Management 🔴
**Gap:** No specification for how thresholds, sensor settings, and user preferences are stored and loaded.

**Impact:** System cannot persist user customizations between sessions.

**Recommendation:**
- Create `config.yaml` or `config.json` with sections for:
  - Threshold values (all 4 gates)
  - Sensor preferences (which stack to use)
  - iPad connection settings (IP, port, dwell timeout)
  - Gesture debounce timings
  - Voice activation keywords
- Add `ConfigManager` class in [`design.md`](specs/accessibility-agent/design.md:1)
- Specify config file location (e.g., `~/.kiro/config.yaml`)
- Add validation on load with sensible defaults

**Files to Update:**
- [`design.md`](specs/accessibility-agent/design.md:1) — add ConfigManager component
- [`structure.md`](specs/accessibility-agent/structure.md:1) — add config file to persistent files table
- [`tasks.md`](specs/accessibility-agent/tasks.md:1) — add config implementation to Phase 1

---

### 1.2 Logging Strategy 🟡
**Gap:** No specification for application logging (distinct from routing_log.jsonl).

**Current State:** [`tech.md`](specs/accessibility-agent/tech.md:96) mentions log levels but not implementation.

**Recommendation:**
- Specify Python `logging` module usage with:
  - Rotating file handler (`~/.kiro/logs/agent.log`, max 10MB, 5 backups)
  - Console handler for INFO+ during development
  - Structured logging format: `[timestamp] [level] [module] message`
- Add logging configuration to startup sequence
- Specify what gets logged at each level:
  - DEBUG: per-frame sensor data, gate evaluations
  - INFO: commands, routing decisions, sensor connections
  - WARNING: sensor failures, fallbacks, low confidence
  - ERROR: unrecoverable failures, crashes

---

### 1.3 Error Recovery and Retry Logic 🟡
**Gap:** No specification for transient failure handling.

**Scenarios Not Covered:**
- AWS API rate limiting or temporary network failures
- Ollama server restart or temporary unavailability
- GPU memory spike causing CUDA OOM
- Sensor USB disconnect/reconnect

**Recommendation:**
Add to [`requirements.md`](specs/accessibility-agent/requirements.md:1):

```markdown
## 8. Error recovery

**8.1** WHEN an AWS API call fails with a retryable error (429, 503, network timeout)  
THE SYSTEM SHALL retry with exponential backoff (100ms, 200ms, 400ms) up to 3 attempts

**8.2** WHEN Ollama server is unreachable  
THE SYSTEM SHALL fall back to cloud inference and log a WARNING

**8.3** WHEN a CUDA OOM error occurs during inference  
THE SYSTEM SHALL clear GPU cache, wait 2 seconds, and retry once before falling back to cloud

**8.4** WHEN a sensor disconnects mid-session  
THE SYSTEM SHALL attempt reconnection every 5 seconds for up to 1 minute before marking it as failed

**8.5** WHEN the iPad WebSocket connection drops  
THE SYSTEM SHALL maintain command history and reconnect automatically when Safari refreshes
```

---

### 1.4 Startup Sequence and Initialization Order 🟡
**Gap:** [`diagrams/03-sequence-diagrams.md`](specs/accessibility-agent/diagrams/03-sequence-diagrams.md:160) shows startup but doesn't specify critical ordering.

**Issues:**
- What happens if Ollama isn't running yet?
- Should sensors initialize in parallel or sequentially?
- When does the system become "ready" to accept commands?

**Recommendation:**
Add startup specification to [`design.md`](specs/accessibility-agent/design.md:1):

1. Load configuration file
2. Initialize logging
3. Start Ollama health check (retry until available or timeout after 30s)
4. Initialize VRAM monitor and latency tracker
5. Load persistent state (gesture_calibration.json, few_shot_memory.db)
6. Initialize sensors in parallel with 10s timeout each
7. Start continuous trainer background loops
8. Print sensor status table
9. Enter main event loop (system ready)

---

### 1.5 Graceful Shutdown Procedure 🔴
**Gap:** [`requirements.md`](specs/accessibility-agent/requirements.md:139) mentions Ctrl-C handling but lacks detail.

**Recommendation:**
Specify shutdown sequence in [`design.md`](specs/accessibility-agent/design.md:1):

1. Catch SIGINT/SIGTERM
2. Stop accepting new commands (set shutdown flag)
3. Wait for in-flight commands to complete (max 5s timeout)
4. Save gesture_calibration.json
5. Flush routing_log.jsonl buffer
6. Close all sensor connections
7. Stop continuous trainer loops
8. Close SQLite connections
9. Log "Shutdown complete"
10. Exit with code 0

---

## 2. Edge Cases and Corner Cases 🟡🟢

### 2.1 Simultaneous Multimodal Input 🟡
**Gap:** What happens if user speaks AND gestures simultaneously?

**Current:** [`structure.md`](specs/accessibility-agent/structure.md:73) shows priority rules but not conflict resolution.

**Scenario:**
- User says "scroll down" while making SWIPE_UP gesture
- Both arrive at FusionEngine within same 60Hz tick

**Recommendation:**
Add to [`requirements.md`](specs/accessibility-agent/requirements.md:1):

```markdown
**5.6** WHEN multiple input modalities produce conflicting commands within the same tick  
THE SYSTEM SHALL execute only the highest-priority command according to the fusion rules and discard others
```

---

### 2.2 Rapid Command Succession 🟡
**Gap:** No rate limiting on command execution.

**Scenario:**
- User rapidly taps iPad "scroll down" button 10 times in 2 seconds
- System attempts to execute all 10 commands, causing UI chaos

**Recommendation:**
Add command rate limiter:
- Max 5 commands per second
- Queue overflow: discard oldest pending commands
- Log WARNING when rate limit hit

---

### 2.3 Ambiguous Target Names 🟡
**Gap:** [`diagrams/06-routing-flowchart.md`](specs/accessibility-agent/diagrams/06-routing-flowchart.md:156) shows ElementFinder but not disambiguation.

**Scenario:**
- User says "click submit" but there are 3 "Submit" buttons visible
- ElementFinder finds multiple matches

**Recommendation:**
Add to [`requirements.md`](specs/accessibility-agent/requirements.md:1):

```markdown
**5.7** WHEN ElementFinder locates multiple elements matching the target name  
THE SYSTEM SHALL prefer the element closest to the current cursor position

**5.8** WHEN gaze coordinates are available and multiple matches exist  
THE SYSTEM SHALL prefer the element closest to the gaze point
```

---

### 2.4 Empty or Malformed LLM Responses 🟡
**Gap:** No handling for when LLM returns invalid action strings.

**Scenarios:**
- LLM returns empty string
- LLM returns "I don't understand" instead of action verb
- LLM returns malformed action like "CLIK submit" (typo)

**Recommendation:**
Add validation in [`desktop_agent.py`](specs/accessibility-agent/design.md:94):
- Validate action verb against allowed list
- If invalid, log ERROR and return CLARIFY action
- Track malformed response rate in routing_log.jsonl

---

### 2.5 Gaze Calibration Drift 🟢
**Gap:** No mechanism to detect or correct gaze calibration degradation over time.

**Issue:** Iris gaze estimation accuracy degrades as user shifts position or lighting changes.

**Recommendation:**
Add to [`requirements.md`](specs/accessibility-agent/requirements.md:1):

```markdown
**3.5** WHEN gaze-targeted clicks consistently miss their targets (>3 misses in 10 attempts)  
THE SYSTEM SHALL prompt the user to recalibrate gaze tracking

**3.6** WHEN the user says "calibrate gaze"  
THE SYSTEM SHALL launch the 9-point calibration routine
```

---

### 2.6 High-Pain Day Adaptation 🟢
**Gap:** System adapts thresholds but doesn't explicitly detect "high-pain days."

**Opportunity:** Proactively adjust settings when RA symptoms are elevated.

**Recommendation:**
Add pain level detection heuristics:
- Track gesture confidence variance (high variance = tremor = pain)
- Track voice command retry rate
- Track touch dwell activation usage increase
- When indicators suggest high-pain day, automatically:
  - Relax all confidence thresholds by 0.1
  - Increase gesture debounce to 1200ms
  - Increase dwell timeout by 200ms
  - Log "High-pain mode activated"

---

## 3. Security and Privacy 🔴🟡

### 3.1 AWS Credentials Management 🔴
**Gap:** No specification for secure credential storage.

**Current:** [`tech.md`](specs/accessibility-agent/tech.md:19) mentions boto3 but not credential handling.

**Recommendation:**
- Use AWS credentials file (`~/.aws/credentials`) or IAM roles
- Never hardcode credentials in config files
- Add credential validation at startup
- Document in README: "AWS credentials required for cloud fallback"

---

### 3.2 Voice Data Privacy 🟡
**Gap:** No specification for audio data retention or privacy.

**Concerns:**
- Is audio buffered to disk?
- How long is audio retained?
- Is audio sent to AWS encrypted?

**Recommendation:**
Add privacy policy to [`product.md`](specs/accessibility-agent/product.md:1):

```markdown
## Privacy and data handling

- **Audio:** Processed in-memory only, never written to disk
- **Transcriptions:** Stored in routing_log.jsonl for learning, rotated after 30 days
- **Cloud fallback:** Audio sent to AWS Transcribe over HTTPS, not retained by AWS
- **Gaze data:** Never leaves local machine
- **Touch events:** Transmitted over local network only (WiFi/USB)
```

---

### 3.3 iPad Touch Interface Authentication 🟡
**Gap:** TouchInputServer has no authentication mechanism.

**Risk:** Anyone on local network can access http://PC_IP:8765 and control desktop.

**Recommendation:**
Add simple token-based auth:
- Generate random token at startup
- Include token in QR code URL: `http://PC_IP:8765?token=abc123`
- Validate token on WebSocket connection
- Reject connections without valid token

---

## 4. Testing Strategy 🟡🟢

### 4.1 Unit Test Coverage 🟡
**Gap:** [`tasks.md`](specs/accessibility-agent/tasks.md:1) has integration tests but no unit test specification.

**Recommendation:**
Add unit test requirements for:
- Gate evaluation logic (all 4 gates with boundary conditions)
- Gesture classification (all 10 gestures with edge cases)
- Action parsing (all 8 verbs + malformed inputs)
- Few-shot retrieval ranking algorithm
- Threshold adaptation formulas

Target: 80% code coverage for core logic.

---

### 4.2 Latency Benchmarking 🟡
**Gap:** No specification for measuring and validating latency targets.

**Recommendation:**
Add benchmarking suite:
- Measure Whisper transcription latency (target: <400ms)
- Measure Ollama inference latency (target: <600ms)
- Measure end-to-end voice→action latency (target: <1200ms)
- Measure gaze+click latency (target: <450ms)
- Run benchmarks on target hardware before deployment

---

### 4.3 Stress Testing 🟢
**Gap:** No specification for system behavior under load.

**Scenarios to Test:**
- 100 commands in 60 seconds (sustained load)
- Rapid modality switching (voice→gesture→touch→voice)
- All sensors active simultaneously
- VRAM at 95% capacity
- Network latency to AWS >2 seconds

---

## 5. Operational Concerns 🟡🟢

### 5.1 System Requirements Documentation 🔴
**Gap:** No minimum system requirements specified.

**Recommendation:**
Add to README:

```markdown
## System Requirements

### Minimum (Voice-only mode)
- Windows 10/11 or Ubuntu 20.04+
- 8 GB RAM
- Any microphone
- Python 3.11+

### Recommended (Full multimodal)
- Windows 11 or Ubuntu 22.04
- NVIDIA RTX 5090 (32 GB VRAM) or RTX 4090 (24 GB VRAM)
- 32 GB RAM
- Hardware sensors (see Hardware section)
- Python 3.11+

### Network
- Local network for iPad integration (WiFi or USB)
- Internet connection for AWS fallback (optional)
```

---

### 5.2 Dependency Version Pinning 🟡
**Gap:** [`tasks.md`](specs/accessibility-agent/tasks.md:163) mentions requirements.txt but no version strategy.

**Recommendation:**
- Pin all dependencies to specific versions
- Test with pinned versions before release
- Document known incompatibilities
- Provide `requirements-dev.txt` for development dependencies

---

### 5.3 Update and Maintenance Strategy 🟢
**Gap:** No specification for updating models or dependencies.

**Recommendation:**
Add maintenance documentation:
- How to update Whisper model
- How to update Ollama model
- How to update sensor SDKs
- Backward compatibility policy for config files

---

### 5.4 Performance Monitoring 🟢
**Gap:** No real-time performance dashboard.

**Recommendation:**
Add optional web dashboard:
- Current VRAM usage
- Latency EMA graph
- Command success rate (last 100)
- Active sensors status
- Threshold values
- Accessible at http://localhost:8766

---

## 6. RA-Specific Enhancements 🟢

### 6.1 Fatigue Detection 🟢
**Opportunity:** Detect user fatigue and suggest breaks.

**Indicators:**
- Increased command retry rate
- Decreased gesture confidence
- Longer pauses between commands

**Recommendation:**
Add fatigue detection:
- Track command patterns over 30-minute windows
- When fatigue indicators detected, speak: "You've been working for a while. Would you like to take a break?"
- Offer to pause system or continue

---

### 6.2 Medication Reminder Integration 🟢
**Opportunity:** Integrate with medication schedule.

**Recommendation:**
Add optional medication reminders:
- Configure medication schedule in config
- Speak reminder at scheduled times
- Dismiss with voice command "taken" or iPad button

---

### 6.3 Posture Change Prompts 🟢
**Opportunity:** Remind user to change position to prevent stiffness.

**Recommendation:**
- Every 45 minutes, suggest posture change
- "Consider adjusting your position to prevent stiffness"
- Configurable interval and enable/disable

---

## 7. Documentation Gaps 🟡

### 7.1 Troubleshooting Guide 🟡
**Gap:** No troubleshooting documentation.

**Recommendation:**
Create `TROUBLESHOOTING.md` with:
- Common sensor connection issues
- Ollama not starting
- AWS credential errors
- iPad not connecting
- Poor gaze accuracy
- Voice recognition issues

---

### 7.2 Hardware Setup Guide 🟡
**Gap:** Hardware mentioned but no setup instructions.

**Recommendation:**
Create `HARDWARE_SETUP.md` with:
- Physical placement recommendations for each sensor
- Cable management for RA-friendly setup
- Optimal lighting for gaze tracking
- Microphone positioning for best voice recognition

---

## 8. Priority Implementation Order

### Phase 0 (Before Phase 1) 🔴
1. Configuration management system
2. Graceful shutdown procedure
3. AWS credentials handling
4. System requirements documentation
5. Startup sequence specification

### Phase 1 Additions 🟡
6. Error recovery and retry logic
7. Logging strategy implementation
8. Unit test framework
9. Ambiguous target disambiguation
10. LLM response validation

### Pre-Production (Before Phase 8) 🟡
11. iPad touch authentication
12. Privacy policy documentation
13. Latency benchmarking suite
14. Troubleshooting guide
15. Hardware setup guide

### Post-Launch Enhancements 🟢
16. High-pain day detection
17. Gaze calibration drift detection
18. Performance monitoring dashboard
19. Fatigue detection
20. Medication reminders

---

## Summary Statistics

- **Total Items Identified:** 23
- **Critical (🔴):** 5 items
- **Important (🟡):** 12 items
- **Enhancement (🟢):** 6 items

**Estimated Additional Work:**
- Critical items: ~8-12 hours
- Important items: ~20-30 hours
- Enhancement items: ~15-20 hours

**Overall Assessment:** Your specifications are excellent. Addressing the 5 critical items before starting Phase 1 implementation will ensure a solid foundation. The important items can be integrated during development, and enhancements can be added post-launch based on real-world usage.

---

## Next Steps

1. Review this document and prioritize which items to address
2. Update specification files with accepted recommendations
3. Create implementation plan incorporating critical items
4. Begin Phase 1 development with enhanced specifications