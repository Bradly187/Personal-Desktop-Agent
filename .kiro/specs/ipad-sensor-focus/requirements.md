# Requirements Document

## Introduction

An iPad-centric multimodal accessibility desktop control agent for a user with rheumatoid arthritis. The iPad Pro (2020+) serves as both the primary sensor hub and a native control surface, running a custom Swift app built in Xcode that leverages Core Motion, ARKit, the Speech framework, and AVFoundation on-device. The desktop PC with an RTX 5090 GPU remains the inference and execution host, running the Python-based coordinator, desktop agent, and continuous learning system. The iPad and PC communicate over a persistent WebSocket connection (USB or local network).

All standalone sensor hardware (ReSpeaker, RealSense, Ultraleap, Tobii, OAK-D Lite, FIFINE mic, Leap Motion v1, C920 webcam) is out of scope. The iPad's built-in hardware replaces all external sensors.

## Glossary

- **System**: The full accessibility agent spanning both the iPad app and the PC-side Python services
- **iPadApp**: The native Swift/SwiftUI application running on the iPad Pro (2020+), built in Xcode, that captures all sensor data and provides the touch control surface
- **PC_Service**: The Python-side services running on the desktop PC (HybridCoordinator, DesktopAgent, ContinuousTrainer, WhisperStream)
- **iPad**: An iPad Pro (2020+) with LiDAR scanner, TrueDepth camera, accelerometer, gyroscope, and microphone
- **WhisperStream**: The voice pipeline on the PC that receives audio from the iPad mic (or system default mic), segments speech with Silero VAD, and transcribes via faster-whisper on CUDA
- **IPadBridge**: The PC-side component that receives sensor data streams (LiDAR depth, gaze coordinates, motion events, gesture classifications) from the iPadApp over WebSocket
- **FusionEngine**: The PC-side component that merges inputs from all iPad sources using priority rules and emits a single Command per tick
- **HybridCoordinator**: The 4-gate router on the PC that decides whether a Command is processed locally (Ollama) or escalated to AWS cloud services
- **DesktopAgent**: The PC-side component that parses action strings and executes them via the accessibility tree and pyautogui
- **ContinuousTrainer**: The PC-side background learning system that adapts routing thresholds, Whisper vocabulary, gesture confidence floors, and few-shot examples
- **Command**: The universal dataclass that crosses every pipeline boundary, carrying text, confidence scores, source tag, session context, and optional gaze coordinates
- **Core_Motion**: Apple's framework providing accelerometer and gyroscope data for tilt-to-navigate and tap-on-table detection
- **ARKit**: Apple's augmented reality framework used for real-time eye gaze tracking and head pose estimation via the TrueDepth camera
- **Speech_Framework**: Apple's on-device speech recognition framework used for custom keyword listening on the iPad
- **AVFoundation**: Apple's media framework used for audio capture and sound action detection on the iPad
- **Record3D**: The iOS app that streams iPad LiDAR depth data to the PC over USB or Wi-Fi
- **Eyeware_Beam**: The iOS app that uses the iPad TrueDepth camera to provide head and eye tracking data to the PC (fallback if ARKit gaze is insufficient)
- **MediaPipe**: Google's on-device ML framework used on the PC for hand landmark detection and iris gaze estimation from the iPad camera feed
- **Silero_VAD**: A lightweight voice activity detection model that runs on CPU to segment speech from silence
- **Gate**: One of four sequential checks in the HybridCoordinator that determine whether a command is processed locally or escalated to the cloud
- **Dwell_Activation**: A touch or gaze interaction where resting on a target for a configured duration triggers activation without requiring tap force
- **Palm_Rejection**: Logic that ignores touch contacts with a radius exceeding a threshold, preventing accidental input from a resting hand
- **Tilt_Navigation**: An input mode where gentle tilts of the iPad (detected via Core_Motion) are mapped to cursor movement or UI navigation on the PC
- **Sound_Action**: An input mode where specific mouth sounds (e.g., cluck, pop) detected by AVFoundation trigger predefined commands
- **Hit_Box_Expansion**: A SwiftUI technique using .contentShape and .padding to make tappable areas significantly larger than visual button size, accommodating tremor and reduced precision

## Requirements

### Requirement 1: Native iPad App (SwiftUI + Xcode)

**User Story:** As a user with RA, I want a native iPad app that captures all sensor data and provides an accessible control surface, so that I get the best performance from the iPad's hardware and a UI designed for my needs.

#### Acceptance Criteria

1. THE iPadApp SHALL be a native SwiftUI application built in Xcode, targeting iPad Pro (2020+) running iPadOS 17 or later
2. THE iPadApp SHALL communicate with the PC_Service over a persistent WebSocket connection (USB or local network) on a configurable port (default 8765)
3. THE iPadApp SHALL use Assistive Access design patterns: high-contrast colors, minimum 44pt touch targets, and Dynamic Type support
4. ALL interactive elements in the iPadApp SHALL use Hit_Box_Expansion via .contentShape(Rectangle()) and generous .padding() to make tappable areas significantly larger than their visual size, accommodating tremor and reduced precision during RA flare-ups
5. THE iPadApp SHALL display a connection status indicator showing whether the PC_Service WebSocket is connected, reconnecting, or disconnected
6. WHEN the WebSocket connection drops, THE iPadApp SHALL attempt automatic reconnection with exponential backoff and notify the user visually

### Requirement 2: Tilt-to-Navigate via Core Motion

**User Story:** As a user with RA, I want to navigate my desktop by gently tilting the iPad, so that I can move a cursor without swiping or tapping which strains my finger joints.

#### Acceptance Criteria

1. WHEN tilt navigation mode is active, THE iPadApp SHALL read Core_Motion rotationRate on the X and Y axes at 60 Hz and stream tilt vectors to the PC_Service over WebSocket
2. WHEN the PC_Service receives tilt vectors, THE FusionEngine SHALL map them to proportional cursor movement on the desktop, with configurable sensitivity and dead zone
3. THE iPadApp settings SHALL allow the user to configure tilt sensitivity, dead zone radius, and axis inversion
4. WHEN the iPad is resting on a stand and the accelerometer detects a sharp impulse (table tap), THE iPadApp SHALL send a tap event to the PC_Service, which the FusionEngine SHALL interpret as a click at the current cursor position
5. WHEN Core_Motion data is unavailable, THE iPadApp SHALL log a warning and the System SHALL continue operating with remaining input modalities

### Requirement 3: Eye Gaze Tracking via ARKit

**User Story:** As a user with RA, I want my iPad to track where I'm looking on screen, so that I can target UI elements with my eyes during flare-ups when even tilting is painful.

#### Acceptance Criteria

1. WHEN the iPadApp ARKit session detects a valid face anchor with eye tracking, THE iPadApp SHALL stream gaze direction vectors to the PC_Service at the ARKit frame rate
2. WHEN the PC_Service receives gaze data that is stable (spread below 4% of screen diagonal) and the user says "click", THE FusionEngine SHALL produce a Command that moves the cursor to the mapped gaze coordinates and clicks
3. WHEN gaze is stable and a dwell timer exceeds the configured duration (default 1 second), THE FusionEngine SHALL produce a click Command at the gaze coordinates without requiring any voice or touch input
4. THE iPadApp settings SHALL allow the user to configure the dwell timer duration and enable or disable gaze dwell activation
5. WHEN ARKit eye tracking confidence is below 0.55, THE FusionEngine SHALL not use gaze coordinates for targeting
6. WHEN ARKit eye tracking is unavailable, THE iPadApp SHALL fall back to Eyeware_Beam if installed, then to MediaPipe iris estimation on the PC from the iPad camera feed

### Requirement 4: Head Tracking via ARKit

**User Story:** As a user with RA, I want to move the cursor with subtle head tilts, so that I have a stable hands-free pointing method that works even when my eye tracking is unreliable.

#### Acceptance Criteria

1. WHEN head tracking mode is active, THE iPadApp SHALL use ARKit face anchor transform data to detect head pitch and yaw and stream head pose deltas to the PC_Service
2. WHEN the PC_Service receives head pose deltas, THE FusionEngine SHALL map them to cursor movement on the desktop with configurable sensitivity and smoothing
3. THE iPadApp settings SHALL allow the user to configure head tracking sensitivity, smoothing factor, and axis mapping
4. WHEN both head tracking and eye gaze are active, THE FusionEngine SHALL use eye gaze for fine targeting and head tracking for coarse navigation

### Requirement 5: Custom Voice Commands via Speech Framework

**User Story:** As a user with RA, I want the iPad to listen for specific command keywords locally, so that simple commands like "Select" or "Scroll Down" execute instantly without waiting for PC-side transcription.

#### Acceptance Criteria

1. THE iPadApp SHALL run a continuous on-device keyword listener using Speech_Framework that recognizes a configurable set of command keywords including "Select", "Click", "Scroll Up", "Scroll Down", "Open", "Close", "Back", "Undo", and "Dictate"
2. WHEN the keyword listener recognizes a command keyword with confidence above the configured threshold, THE iPadApp SHALL send the recognized keyword to the PC_Service as a Command with source "voice_local", bypassing WhisperStream transcription
3. WHEN the keyword listener does not match a known keyword, THE iPadApp SHALL stream the audio to the PC_Service for full WhisperStream transcription on the GPU
4. THE iPadApp settings SHALL allow the user to add, remove, and reorder command keywords
5. WHEN Speech_Framework is unavailable, THE iPadApp SHALL fall back to streaming all audio to the PC_Service for WhisperStream processing

### Requirement 6: Sound Actions via AVFoundation

**User Story:** As a user with RA, I want to trigger commands with simple mouth sounds, so that I have a zero-hand input method that doesn't require forming words.

#### Acceptance Criteria

1. THE iPadApp SHALL use AVFoundation audio analysis to detect configurable mouth sounds including cluck, pop, and hiss patterns
2. WHEN a recognized sound is detected with confidence above the configured threshold, THE iPadApp SHALL send the mapped command to the PC_Service as a Command with source "sound_action"
3. THE iPadApp settings SHALL allow the user to map each recognized sound to a specific action (e.g., cluck maps to "click", pop maps to "scroll down")
4. THE iPadApp SHALL debounce sound actions with a configurable cooldown period (default 500 ms) to prevent accidental double-triggers
5. WHEN sound action detection is disabled or AVFoundation is unavailable, THE System SHALL continue operating with remaining input modalities

### Requirement 7: iPad LiDAR Depth via Record3D

**User Story:** As a user with RA, I want my iPad's LiDAR sensor to provide depth data, so that gesture recognition can use real 3D distances for more accurate classification.

#### Acceptance Criteria

1. WHEN the iPad is running Record3D and is connected via USB or local network, THE IPadBridge SHALL stream LiDAR depth frames with confidence map filtering
2. WHEN a LiDAR depth frame has a confidence value below the configured minimum for a region, THE IPadBridge SHALL exclude that region from the depth data provided to the PC_Service gesture processing
3. WHEN Record3D is not running or the iPad is disconnected, THE IPadBridge SHALL log a warning and the PC_Service gesture processing SHALL fall back to 2D MediaPipe classification without depth

### Requirement 8: Native Touch Interface (SwiftUI)

**User Story:** As a user with RA, I want a large-button touch interface on my iPad with oversized targets and dwell activation, so that I can tap common commands directly even on high-pain days.

#### Acceptance Criteria

1. THE iPadApp SHALL display a command pad with a configurable grid of large buttons, each with a minimum touch target of 80x80 points and Hit_Box_Expansion applied
2. WHEN the user taps a command button, THE iPadApp SHALL send the command to the PC_Service and the PC_Service SHALL execute it immediately, bypassing voice and gesture fusion
3. THE iPadApp SHALL include a virtual trackpad panel where finger drags are translated to proportional cursor movement sent to the PC_Service via WebSocket
4. WHEN dwell activation is enabled and the user rests a finger on a button for the configured duration, THE iPadApp SHALL activate that command without requiring a physical tap press, showing a CSS ring animation countdown
5. WHEN a touch contact has a radius greater than the palm rejection threshold, THE iPadApp SHALL ignore it to prevent accidental activation from a resting hand
6. THE iPadApp SHALL display a settings panel allowing the user to configure dwell timeout, trackpad speed, button layout, palm rejection radius, and all sensor mode preferences, persisting settings to UserDefaults

### Requirement 9: Full-Screen Trackpad Mode

**User Story:** As a user with RA, I want to lay my iPad flat on my desk and use the entire screen as a mouse replacement, so that I can control the cursor with light finger drags instead of gripping a mouse.

#### Acceptance Criteria

1. WHEN full-screen trackpad mode is active, THE iPadApp SHALL treat the entire display as a trackpad surface, mapping finger drags to proportional cursor movement on the desktop
2. WHEN the user performs a single tap in full-screen trackpad mode, THE iPadApp SHALL send a left-click command to the PC_Service
3. WHEN the user performs a two-finger tap in full-screen trackpad mode, THE iPadApp SHALL send a right-click command to the PC_Service
4. WHEN the user performs a two-finger drag in full-screen trackpad mode, THE iPadApp SHALL send a scroll command to the PC_Service in the direction of the drag
5. WHEN full-screen trackpad mode is active, THE iPadApp SHALL apply Palm_Rejection to ignore accidental resting-hand contacts while still accepting deliberate fingertip input
6. THE iPadApp settings SHALL allow the user to configure trackpad sensitivity, scroll speed, and tap-to-click enable/disable for full-screen trackpad mode
7. THE iPadApp SHALL provide a gesture or edge swipe to switch between full-screen trackpad mode and the command pad view without requiring precise taps

### Requirement 10: Gesture Recognition via iPad Camera

**User Story:** As a user with RA, I want my iPad's camera to detect hand gestures, so that I can issue commands through simple hand movements when speaking is difficult.

#### Acceptance Criteria

1. WHEN the iPad camera feed is available and the PC_Service detects a hand with gesture confidence at or above 0.65 via MediaPipe, THE PC_Service SHALL map the gesture to the corresponding desktop Command
2. WHEN a gesture is classified below the confidence threshold, THE PC_Service SHALL discard it silently and not produce a Command
3. WHEN the same gesture fires within 800 ms of the previous firing, THE PC_Service SHALL debounce it and not re-fire
4. WHEN iPad LiDAR depth data is available from Record3D, THE PC_Service SHALL use real millimetre distances for pinch and grab classification instead of 2D pixel-space estimation
5. WHEN the iPad camera feed is unavailable, THE PC_Service SHALL log a warning and the System SHALL continue operating with remaining input modalities

### Requirement 11: Voice Command Transcription (PC-side)

**User Story:** As a user with RA, I want complex voice commands to be transcribed on my GPU with high accuracy, so that I can issue multi-step instructions naturally.

#### Acceptance Criteria

1. WHEN the iPadApp streams audio to the PC_Service (commands not matched by the on-device keyword listener), THE WhisperStream SHALL transcribe it using Whisper large-v3 on the local GPU within 400 ms
2. WHEN a transcription has a mean log-probability below the configured threshold, THE HybridCoordinator SHALL route the command to Amazon Transcribe for re-transcription
3. WHEN a transcription is clean and the command is simple (token count at or below 12 with no complexity markers), THE HybridCoordinator SHALL resolve it using the local Ollama LLM without any cloud call
4. WHEN a voice command contains multi-step language such as "and then", "after that", or "for each", THE HybridCoordinator SHALL route it to Amazon Bedrock for reasoning
5. WHEN Whisper produces an empty transcription or a single-character result, THE WhisperStream SHALL silently discard it without producing a Command

### Requirement 12: Sensor Fusion (iPad Sources)

**User Story:** As a user with RA, I want all my iPad sensor inputs to be merged intelligently, so that the most reliable input wins on each interaction.

#### Acceptance Criteria

1. THE FusionEngine SHALL evaluate inputs using the following priority order: (1) iPad touch command, (2) sound action, (3) gaze dwell click, (4) gaze plus voice, (5) gaze plus gesture, (6) tilt navigation, (7) head tracking cursor, (8) gesture alone, (9) on-device voice keyword, (10) PC-transcribed voice
2. WHEN multiple input sources produce Commands within the same tick, THE FusionEngine SHALL emit only the single highest-priority Command
3. WHEN a higher-priority source is unavailable, THE FusionEngine SHALL fall through to the next available source without error
4. THE FusionEngine SHALL accept Commands with source tags "touch", "sound_action", "gaze_dwell", "multimodal", "tilt", "head_track", "gesture", "voice_local", and "voice" to distinguish input origins

### Requirement 13: Routing and Execution

**User Story:** As a user with RA, I want commands to be executed quickly on my local GPU and only fall back to the cloud when necessary, so that I get low-latency responses without mandatory internet dependency.

#### Acceptance Criteria

1. WHEN free GPU VRAM drops below the configured floor, THE HybridCoordinator SHALL route commands to the cloud until VRAM recovers above the floor
2. WHEN the local inference latency EMA exceeds the configured budget, THE HybridCoordinator SHALL route commands to the cloud until latency recovers below the budget
3. WHEN the HybridCoordinator produces an action string, THE DesktopAgent SHALL execute it via the accessibility tree if the target element is found there, and fall back to EasyOCR screen text matching if the target is not found in the accessibility tree
4. WHEN the action is CLICK and gaze coordinates are present on the Command, THE DesktopAgent SHALL move the cursor to those coordinates before clicking
5. WHEN the LLM returns a CLARIFY action, THE DesktopAgent SHALL speak the clarification question via TTS and not execute a desktop action

### Requirement 14: Continuous Learning

**User Story:** As a user with RA, I want the system to learn from my usage patterns over time, so that recognition accuracy and routing efficiency improve without manual tuning.

#### Acceptance Criteria

1. WHEN a command succeeds, THE ContinuousTrainer SHALL record the input-to-action pair in the few-shot SQLite database
2. WHEN retrieving few-shot examples for a new command, THE ContinuousTrainer SHALL rank stored examples by token overlap weighted by recency and usage count
3. WHEN Gate 1 escalates more than 30% of commands to the cloud and the local failure rate is below 10%, THE ContinuousTrainer SHALL relax the confidence threshold by 0.05
4. WHEN the vocabulary builder identifies a word appearing 3 or more times in successful transcriptions, THE ContinuousTrainer SHALL add it to the Whisper hotwords list
5. WHEN the gesture calibrator has 10 or more samples for a gesture, THE ContinuousTrainer SHALL set that gesture's confidence floor to the p10 of observed confidences minus 0.05

### Requirement 15: iPad Connection and Discovery

**User Story:** As a user with RA, I want the system to detect my iPad automatically and show me how to connect, so that setup requires minimal manual configuration.

#### Acceptance Criteria

1. WHEN the PC_Service starts, THE PC_Service SHALL listen for WebSocket connections from the iPadApp on the configured port
2. WHEN the iPadApp launches, THE iPadApp SHALL discover the PC_Service via Bonjour/mDNS on the local network, or accept a manually entered IP address
3. WHERE the qrcode Python package is installed, THE PC_Service SHALL print a QR code to the terminal that the user can scan with the iPad camera to configure the iPadApp connection
4. WHEN the iPadApp establishes a WebSocket connection, THE PC_Service SHALL log the connection and begin accepting sensor data streams

### Requirement 16: Resilience and Graceful Degradation

**User Story:** As a user with RA, I want the system to keep working even when some iPad features are unavailable, so that I always have at least one way to control my desktop.

#### Acceptance Criteria

1. WHEN any iPad sensor capability (ARKit gaze, Core_Motion tilt, Speech_Framework keywords, Record3D LiDAR, camera feed) is unavailable, THE System SHALL log a warning and continue operating with remaining capabilities
2. WHEN all iPad sensor streams are unavailable, THE System SHALL still accept commands from the iPadApp touch interface and the system microphone
3. WHEN the System starts, THE PC_Service SHALL print a status table to the terminal showing which iPad sensors are connected and which fallbacks are active
4. WHEN the user presses Ctrl-C on the PC, THE PC_Service SHALL save the gesture calibration file, flush the routing log, and stop all sensor streams before exiting
5. WHEN the iPadApp detects that a framework (ARKit, Core_Motion, Speech_Framework, AVFoundation) is not available on the device, THE iPadApp SHALL hide the corresponding UI controls and not attempt to start that sensor

### Requirement 19: Apple Pencil Handwriting Recognition

**User Story:** As a user with RA who works with science and mathematics, I want to write expressions freehand on my iPad with my Apple Pencil and have them recognised and sent to my PC, so that I can enter complex mathematical notation naturally without hunting for symbol buttons.

#### Acceptance Criteria

1. THE iPadApp SHALL include a `HandwritingCanvasView` tab accessible from the main navigation
2. THE `HandwritingCanvasView` SHALL use `PKCanvasView` configured with `.pencilOnly` drawing policy so that finger touches pan/scroll rather than draw, preventing accidental ink from a resting hand
3. WHEN the user taps Recognise, THE iPadApp SHALL render the current `PKDrawing` to a PNG with a white background and send it to the PC_Service as a `handwriting_image` WebSocket message
4. WHEN the PC_Service receives a `handwriting_image` message, THE PC_Service SHALL pass the PNG to `pix2tex` (running on the local GPU) and return a `handwriting_result` message containing the recognised LaTeX string and a unicode approximation
5. THE iPadApp SHALL display the returned LaTeX and unicode strings for the user to review before sending to the PC
6. THE iPadApp SHALL allow the user to edit the unicode string before sending to correct any misrecognitions
7. WHEN the user taps Send to PC, THE iPadApp SHALL transmit a `touch_command` / `DICTATE` message with the (possibly edited) unicode string so the PC delivers it via clipboard paste
8. WHEN `pix2tex` is not installed on the PC, THE PC_Service SHALL return a `handwriting_result` with an `error` field explaining how to install it, and THE iPadApp SHALL display this error to the user
9. THE `HandwritingCanvasView` SHALL provide Clear (reset canvas) and Undo (remove last stroke) controls
10. WHEN the canvas is empty, THE Recognise button SHALL be disabled

### Requirement 18: Scientific Keypad Input Surface

**User Story:** As a user with RA who works with science and mathematics, I want an iPad keypad with scientific calculator symbols, so that I can enter expressions like `sin(π/4) + √2` into any PC application without needing a physical keyboard.

#### Acceptance Criteria

1. THE iPadApp SHALL include a `ScientificKeypadView` tab accessible from the main navigation
2. THE `ScientificKeypadView` SHALL display a scrollable monospace expression area showing the current input and an optional live evaluation preview
3. THE `ScientificKeypadView` SHALL provide a Basic mode (digits, basic operators, parentheses) and a Scientific mode adding: sin, cos, tan, their inverses, log, ln, log₂, √, ^, π, e, abs, factorial, mod, EE, and ±
4. ALL keypad buttons SHALL have a minimum touch target of 64×64 points with Hit_Box_Expansion applied
5. WHEN the user taps Send, THE iPadApp SHALL transmit a `touch_command` message with `action: "DICTATE"` and the expression string as `text` to the PC_Service
6. THE PC_Service SHALL deliver the expression to the focused desktop application via clipboard paste (not individual keystrokes), preserving all unicode mathematical symbols
7. THE `ScientificKeypadView` SHALL provide ANS recall (inserts the most recent evaluation result), CLR (clears expression), and backspace controls
8. WHEN the expression cannot be evaluated on-device, THE `ScientificKeypadView` SHALL show no evaluation preview rather than an error

### Requirement 17: Action Vocabulary

**User Story:** As a user with RA, I want a constrained set of action verbs, so that the system's behavior is predictable and I can learn the full command set.

#### Acceptance Criteria

1. THE DesktopAgent SHALL constrain LLM output to exactly these action verbs: CLICK, SCROLL, TYPE, OPEN, CLOSE, HOTKEY, DICTATE, and CLARIFY
2. WHEN the LLM produces an action string that does not begin with one of the permitted verbs, THE DesktopAgent SHALL reject it and log a warning instead of executing it
3. WHEN the action is DICTATE, THE DesktopAgent SHALL paste the text via the clipboard instead of simulating individual keystrokes
