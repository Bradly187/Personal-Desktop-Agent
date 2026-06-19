# Requirements Document

## Introduction

This document specifies the requirements for completing four integration tests that validate core accessibility paths and wiring the Phase 6 AWS cloud fallback into the Personal Desktop Agent pipeline. The integration tests cover gaze dwell click, dwell activation timeout, voice end-to-end, and ModelRouter VRAM fallback. The cloud fallback covers Bedrock inference, Transcribe re-transcription, Polly TTS clarification, and cloud latency logging that feeds the ContinuousTrainer threshold tuner.

## Glossary

- **FusionEngine**: The 60 Hz sensor priority engine that consumes iPad sensor events and emits at most one Command per tick to the HybridCoordinator.
- **HybridCoordinator**: The 4-gate routing engine that decides whether a Command runs locally or falls back to AWS cloud services.
- **CommandExecutor**: The component that translates a Command's action verb into desktop tool calls (mouse, keyboard, etc.).
- **Command**: The sole dataclass DTO crossing pipeline boundaries, containing text, action, source, confidence scores, and params.
- **Gate_1**: Confidence gate — checks whisper_logprob and gesture_confidence against thresholds.
- **Gate_2**: Complexity gate — checks token count and complexity keywords.
- **Gate_3**: VRAM gate — checks free GPU memory via pynvml.
- **Gate_4**: Latency EMA gate — checks whether local inference latency exceeds budget.
- **ModelRouter**: The component that selects the best-fitting local LLM model based on available VRAM and domain.
- **CloudInference**: The AWS Bedrock Claude backend used when local gates fail.
- **Transcribe_Retranscription**: The Amazon Transcribe streaming fallback invoked when Gate_1 fires for low-confidence voice commands.
- **Polly_TTS**: The Amazon Polly text-to-speech component used for audio clarification feedback on cloud routes.
- **AgentDB**: The async SQLite persistence layer (agent.db) that stores command logs, inference records, and latency data.
- **ContinuousTrainer**: The background learning component that reads AgentDB to adapt routing thresholds over time.
- **Bypass_Sources**: The set of Command sources (touch, sound_action, gaze_dwell, multimodal) that skip all gates and route locally.
- **Dwell_Activation**: The mechanism where stable gaze at a screen location for a configured duration triggers a click without physical input.
- **WhisperStream**: The audio pipeline that captures iPad mic audio, runs Silero VAD, and transcribes via faster-whisper on GPU.

## Requirements

### Requirement 1: Gaze Dwell Click Integration

**User Story:** As a user with rheumatoid arthritis, I want gaze dwell to fire a click at the screen coordinates where my eyes rest, so that I can interact with the desktop without physical hand movement.

#### Acceptance Criteria

1. WHEN the FusionEngine receives a gaze_dwell event with normalized coordinates (x, y) where both x and y are within the range 0.0 to 1.0 inclusive, THE FusionEngine SHALL create a Command with action="CLICK", source="gaze_dwell", and gaze_coords mapped to pixel coordinates (int(x * screen_width), int(y * screen_height)).
2. WHEN a Command with source="gaze_dwell" reaches the HybridCoordinator, THE HybridCoordinator SHALL route the Command through the bypass path without invoking any cloud service or LLM inference.
3. WHEN the CommandExecutor receives a CLICK action with gaze_coords, THE CommandExecutor SHALL invoke mouse_click at the specified pixel coordinates with button="left".
4. IF the FusionEngine receives a gaze_dwell event with normalized coordinates where x or y is outside the range 0.0 to 1.0, THEN THE FusionEngine SHALL discard the event without creating a Command and SHALL log a warning indicating the out-of-range coordinates.
5. WHEN the gaze_dwell pipeline executes from FusionEngine event receipt through CommandExecutor mouse_click completion, THE system SHALL complete the end-to-end execution in under 100 milliseconds.
6. WHEN the gaze_dwell integration test runs with mocked I/O, THE test SHALL complete in under 5 seconds.

### Requirement 2: Dwell Activation Timeout

**User Story:** As a user with rheumatoid arthritis, I want the dwell click to fire only after my gaze has been stable for the configured duration, so that accidental brief glances do not trigger unintended clicks.

#### Acceptance Criteria

1. WHILE the gaze position remains within the stability threshold (spread below gaze_stability_pct of screen diagonal, default 4%) for less than dwell_duration_s (default 1.0 second), THE FusionEngine SHALL NOT emit a gaze_dwell Command.
2. WHEN the gaze position remains within the stability threshold for at least dwell_duration_s, THE FusionEngine SHALL emit exactly one gaze_dwell Command with the centroid of all gaze samples collected during the stable window, and SHALL reset the dwell timer so that a subsequent dwell requires a new full-duration stable period.
3. WHEN the gaze position moves outside the stability threshold before dwell_duration_s elapses, THE FusionEngine SHALL reset the dwell timer and discard accumulated stable samples without emitting a Command.
4. IF the gaze buffer contains fewer than 5 samples within the most recent 300 milliseconds, THEN THE FusionEngine SHALL treat the gaze as unstable and SHALL NOT start or continue the dwell timer.
5. WHEN the dwell activation test runs with mocked I/O, THE test SHALL complete in under 5 seconds.

### Requirement 3: Voice End-to-End Pipeline

**User Story:** As a user, I want voice commands to flow from WhisperStream through FusionEngine to CommandExecutor and produce the correct desktop action, so that I can control my computer by speaking.

#### Acceptance Criteria

1. WHEN WhisperStream produces a non-empty transcription, THE WhisperStream SHALL emit a Command with source="voice", action="DICTATE", the transcribed text, and the whisper_logprob confidence score (average log-probability across all segments).
2. WHEN the FusionEngine receives a voice Command at Rule 10 priority, THE FusionEngine SHALL forward the Command to the HybridCoordinator for full 4-gate evaluation (Gate 1 confidence, Gate 2 complexity, Gate 3 VRAM, Gate 4 latency EMA).
3. WHEN the HybridCoordinator routes a voice Command through all gates successfully, THE CommandExecutor SHALL execute the inferred action and return a result with status "ok" and the action verb from the constrained vocabulary (CLICK, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, CLARIFY, SCREENSHOT).
4. IF the voice Command's whisper_logprob is below the configured minimum threshold (default -1.0), THEN THE HybridCoordinator SHALL invoke the Transcribe re-transcription fallback before continuing to Gate 2 evaluation.
5. IF WhisperStream's VAD detects no speech segments in the buffered audio, THEN THE WhisperStream SHALL discard the buffer without emitting a Command.
6. WHEN the voice end-to-end pipeline runs with mocked inference at 50ms, THE total pipeline latency (WhisperStream emit to CommandExecutor completion) SHALL be under 600ms.
7. WHEN the voice end-to-end test runs with mocked I/O, THE test SHALL complete in under 5 seconds.

### Requirement 4: ModelRouter VRAM Fallback

**User Story:** As a user, I want the ModelRouter to automatically select a smaller model when GPU memory is insufficient for the preferred model, so that inference continues without failure.

#### Acceptance Criteria

1. WHEN ModelRouter.select_profile is called for a domain, THE ModelRouter SHALL query free VRAM in gigabytes via pynvml for GPU index 0.
2. WHEN the preferred model for a domain requires more VRAM than available free VRAM plus a 2.0 GB tolerance buffer, THE ModelRouter SHALL iterate the domain's fallback chain in order and select the first model whose vram_gb is less than or equal to free VRAM plus 2.0 GB.
3. IF no model in the fallback chain fits within available VRAM plus the 2.0 GB tolerance, THEN THE ModelRouter SHALL return the "command" domain profile (llama3.1:8b) as the ultimate fallback.
4. THE ModelRouter SHALL always return a ModelProfile with non-empty name, domain, and system_prompt fields, and SHALL never return None.
5. WHEN pynvml is unavailable or raises any exception during VRAM query, THE ModelRouter SHALL assume 999.0 GB of free VRAM and select the preferred (first) model in the fallback chain.
6. IF the domain argument is not present in the fallback chain mapping, THEN THE ModelRouter SHALL use a default chain containing "llama3.1:8b" as the sole entry.
7. WHEN the ModelRouter VRAM fallback test runs with mocked pynvml, THE test SHALL complete in under 5 seconds.

### Requirement 5: Bedrock Cloud Inference

**User Story:** As a user, I want complex commands that exceed local capability to fall back to AWS Bedrock Claude, so that multi-step or ambiguous commands still execute correctly.

#### Acceptance Criteria

1. WHEN Gate_2, Gate_3, or Gate_4 fails for a Command, THE HybridCoordinator SHALL invoke CloudInference to process the Command via AWS Bedrock.
2. WHEN CloudInference receives a Command, THE CloudInference SHALL send the command text to the configured Bedrock model and return the first non-empty line from the model response, trimmed of leading and trailing whitespace.
3. IF AWS credentials are missing, invalid, or the boto3 dependency is not installed, THEN THE CloudInference SHALL return "CLARIFY cloud unavailable: {error}" without raising an exception.
4. IF the Bedrock API returns a ThrottlingException, THEN THE CloudInference SHALL return "CLARIFY cloud error: throttled" without raising an exception.
5. IF the Bedrock API returns any error other than ThrottlingException (including network timeout, model-not-found, or service unavailable), THEN THE CloudInference SHALL return "CLARIFY cloud error: {error}" without raising an exception.
6. IF the Bedrock API response contains no text content or is empty, THEN THE CloudInference SHALL return "CLARIFY cloud error: empty response" without raising an exception.
7. WHEN CloudInference completes a request (success or failure), THE HybridCoordinator SHALL record the round-trip latency in milliseconds, measured from immediately before the API call to immediately after the response is received or the error is caught, and update the latency EMA used by Gate_4.

### Requirement 6: Transcribe Re-transcription

**User Story:** As a user, I want low-confidence voice transcriptions to be re-transcribed via Amazon Transcribe, so that misheard commands get a second chance before routing to the cloud.

#### Acceptance Criteria

1. WHEN Gate_1 fires for a voice Command with whisper_logprob below the configured whisper_logprob_min threshold (default -1.0), THE HybridCoordinator SHALL invoke Transcribe_Retranscription before evaluating Gates 2-4.
2. WHEN Transcribe_Retranscription receives a Command with audio_bytes in params, THE Transcribe_Retranscription SHALL stream the audio to Amazon Transcribe within a timeout of 5 seconds and return a new Command with the corrected text, preserving the original Command's source, action, session_context, and gesture_confidence fields.
3. WHEN Transcribe_Retranscription succeeds, THE returned Command SHALL have whisper_logprob set to -0.3 (neutral confidence).
4. IF audio_bytes is absent from the Command params, THEN THE Transcribe_Retranscription SHALL return the original Command unchanged without calling Amazon Transcribe.
5. IF Amazon Transcribe returns an error or does not respond within 5 seconds, THEN THE Transcribe_Retranscription SHALL log a warning and return the original Command unchanged with its original whisper_logprob preserved.
6. THE Transcribe_Retranscription SHALL catch all exceptions internally and never raise an exception to the caller.
7. WHEN Transcribe_Retranscription returns a corrected Command, THE HybridCoordinator SHALL evaluate the corrected Command through Gates 2-4 using the updated text and neutral whisper_logprob.

### Requirement 7: Polly TTS Clarification

**User Story:** As a user with rheumatoid arthritis, I want clarification messages on cloud routes to be spoken aloud via Amazon Polly, so that I receive audio feedback without needing to look at the screen.

#### Acceptance Criteria

1. WHEN the CommandExecutor processes a CLARIFY action whose Command.params contains route="cloud", THE CommandExecutor SHALL invoke Polly_TTS to speak the clarification message text.
2. WHEN Polly_TTS receives a message of 3000 characters or fewer, THE Polly_TTS SHALL call Amazon Polly synthesize_speech with a Neural voice and 16kHz PCM output format.
3. WHEN Polly_TTS receives synthesized audio, THE Polly_TTS SHALL play the audio through the default output device via sounddevice.
4. IF the Amazon Polly API call does not respond within 5 seconds, or returns an error, or the audio output device fails during playback, THEN THE Polly_TTS SHALL return False without raising an exception.
5. IF the clarification message exceeds 3000 characters, THEN THE Polly_TTS SHALL truncate the message to 3000 characters before calling synthesize_speech.
6. WHEN Polly_TTS returns False, THE CommandExecutor SHALL return the clarification result with spoken=False so the message remains available for visual display.
7. THE Polly_TTS SHALL execute audio playback in a separate thread to avoid blocking the async event loop.

### Requirement 8: Cloud Latency Logging

**User Story:** As a system operator, I want every cloud-routed command to be logged with timing data in AgentDB, so that the ContinuousTrainer can adapt routing thresholds based on observed cloud performance.

#### Acceptance Criteria

1. WHEN a Command is routed to any cloud service (Bedrock, Transcribe, or Polly), THE HybridCoordinator SHALL insert a row into the AgentDB commands table with route="cloud" and a ts value set to the current time.
2. WHEN logging a cloud-routed command, THE HybridCoordinator SHALL record the gate_that_decided field as one of "gate2_complexity", "gate3_vram", or "gate4_latency" indicating which gate triggered the cloud fallback.
3. WHEN logging a cloud-routed command, THE HybridCoordinator SHALL record latency_ms as the elapsed wall-clock time in milliseconds from the start of the route() call to completion of the cloud response, rounded to one decimal place.
4. IF a cloud call fails with an error, THEN THE HybridCoordinator SHALL still insert the commands row with route="cloud", latency_ms reflecting the time until failure, and success=0.
5. WHEN the ContinuousTrainer adaptation loop runs, THE ContinuousTrainer SHALL query commands rows where route="cloud" and use their latency_ms values to update the Gate_4 EMA threshold via the configured latency_ema_alpha smoothing factor.
6. WHEN the cloud latency logging test runs with mocked boto3, THE test SHALL verify a row exists in the commands table with route="cloud", latency_ms greater than zero, and gate_that_decided set to a valid gate value.

### Requirement 9: Graceful Degradation

**User Story:** As a user, I want all cloud fallback paths to degrade gracefully on failure, so that a cloud outage never crashes the pipeline or corrupts the Command flow.

#### Acceptance Criteria

1. IF any AWS service call (Bedrock, Transcribe, or Polly) raises an exception, THEN THE calling component SHALL catch the exception, log a warning at WARNING level with the exception message, and return a safe fallback value within 10 seconds of the call initiation.
2. WHEN Transcribe_Retranscription fails, THE HybridCoordinator SHALL continue routing the original Command (with its original text and whisper_logprob) through Gates 2-4 unchanged.
3. WHEN Polly_TTS fails, THE CommandExecutor SHALL return the clarification result dict with spoken=False and the message text intact for visual display.
4. WHEN CloudInference fails, THE HybridCoordinator SHALL return a CLARIFY action with the error description string and SHALL NOT retry the cloud call within the same route() invocation.
5. THE pipeline SHALL never send command text matching Gate_0 sensitive patterns (passwords, API keys, SSNs, credit card numbers, private keys) to any cloud service regardless of other gate outcomes.
6. IF AgentDB is unavailable when logging a cloud-routed command, THEN THE HybridCoordinator SHALL log a warning and continue execution without raising an exception.

### Requirement 10: Integration Test Pattern Compliance

**User Story:** As a developer, I want all new integration tests to follow the established standalone async pattern, so that tests are consistent, CI-compatible, and require no external test framework.

#### Acceptance Criteria

1. THE integration test files SHALL use `asyncio.run()` as the entry point with no import of or dependency on pytest, unittest, or any external test runner.
2. THE integration test files SHALL mock all external I/O interfaces — including pyautogui, command_executor.mouse, pynvml, sounddevice, boto3, and aiohttp outbound calls — so that no real desktop interaction, GPU access, audio capture, or network calls occur during execution.
3. IF all test cases in a file pass, THEN THE integration test file SHALL exit with code 0.
4. IF one or more test cases in a file fail, THEN THE integration test file SHALL exit with code 1.
5. THE integration test files SHALL print a result line per test case containing a pass/fail indicator (✓ or ✗) followed by a human-readable sentence describing the scenario under test.
6. THE integration test files SHALL catch exceptions raised within individual test cases and report them as failures without aborting the remaining tests in the file.
7. WHEN an integration test run completes, THE test file SHALL cancel all async tasks, await their termination with CancelledError handling, and close all open connections before the process exits.
8. THE integration test files SHALL insert the project root into `sys.path` and use only standard-library imports (asyncio, sys, pathlib, unittest.mock) plus project-internal modules.
