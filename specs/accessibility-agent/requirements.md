# Requirements

## User story

As a person with rheumatoid arthritis, I want to control my desktop using voice,
gestures, eye gaze, and touch — without requiring precise mouse or keyboard use —
so that I can work comfortably on any day regardless of my pain level.

---

## 1. Voice control

**1.1** WHEN the microphone captures a spoken command  
THE SYSTEM SHALL transcribe it using Whisper large-v3 on the local GPU within 400 ms

**1.2** WHEN a transcription has a mean log-probability below the configured threshold  
THE SYSTEM SHALL route the command to Amazon Transcribe instead of processing it locally

**1.3** WHEN a transcription is clean and the command is simple (≤ 12 tokens, no complexity markers)  
THE SYSTEM SHALL resolve it using the local Ollama LLM without any cloud call

**1.4** WHEN a voice command contains multi-step language ("and then", "after that", "for each")  
THE SYSTEM SHALL route it to Amazon Bedrock for reasoning

**1.5** WHEN Whisper produces an empty transcription or single-character result  
THE SYSTEM SHALL silently discard it without producing a command

---

## 2. Gesture control

**2.1** WHEN the camera detects a hand and classifies a gesture with confidence ≥ 0.65  
THE SYSTEM SHALL map it to the corresponding desktop command

**2.2** WHEN a gesture is classified below the confidence threshold  
THE SYSTEM SHALL discard it silently and not produce a command

**2.3** WHEN the same gesture fires  
THE SYSTEM SHALL debounce it and not re-fire within 800 ms

**2.4** WHEN 3D hand data is available from Ultraleap or Leap Motion  
THE SYSTEM SHALL prefer 3D classification over 2D MediaPipe pixel-space classification

**2.5** WHEN RealSense or iPad LiDAR depth is available  
THE SYSTEM SHALL use real millimetre distances for pinch and grab classification

---

## 3. Eye gaze

**3.1** WHEN gaze is valid and stable (spread < 4% of screen) AND the user says "click"  
THE SYSTEM SHALL move the cursor to the gaze coordinates and click

**3.2** WHEN gaze is valid and the user forms a POINT gesture  
THE SYSTEM SHALL click at the gaze coordinates without requiring a voice command

**3.3** WHEN Tobii or Beam gaze is unavailable  
THE SYSTEM SHALL fall back to MediaPipe iris gaze estimation from the webcam

**3.4** WHEN gaze confidence is below 0.55  
THE SYSTEM SHALL not use gaze coordinates for targeting

---

## 4. iPad touch

**4.1** WHEN the iPad is on the same network or USB-connected  
THE SYSTEM SHALL serve a touch interface accessible at http://\<pc-ip\>:8765 in Safari

**4.2** WHEN the user taps a command button on the iPad  
THE SYSTEM SHALL execute that command immediately, bypassing voice/gesture fusion

**4.3** WHEN the user drags a finger on the trackpad panel  
THE SYSTEM SHALL move the cursor proportionally without routing through the LLM

**4.4** WHEN dwell activation is enabled and the user rests a finger on a button for the configured duration  
THE SYSTEM SHALL activate that command without requiring a physical tap press

**4.5** WHEN a touch contact has a radius greater than the palm rejection threshold  
THE SYSTEM SHALL ignore it to prevent accidental activation from resting the hand

**4.6** WHEN the iPad is an iPad Pro (2020+) running Record3D  
THE SYSTEM SHALL use its LiDAR stream as the depth source instead of OAK-D Lite

**4.7** WHEN the iPad has a TrueDepth camera running Eyeware Beam  
THE SYSTEM SHALL use it as the gaze source in preference to webcam iris estimation

---

## 5. Routing and execution

**5.1** WHEN free GPU VRAM drops below the configured floor  
THE SYSTEM SHALL route commands to the cloud until VRAM recovers

**5.2** WHEN the local latency EMA exceeds the configured budget  
THE SYSTEM SHALL route commands to the cloud until latency recovers

**5.3** WHEN the coordinator produces an action string  
THE SYSTEM SHALL execute it via the accessibility tree if the target is found there, otherwise fall back to EasyOCR screen text matching

**5.4** WHEN the action is CLICK and gaze coordinates are present on the Command  
THE SYSTEM SHALL move the cursor to those coordinates before clicking

**5.5** WHEN the LLM returns a CLARIFY action  
THE SYSTEM SHALL speak the clarification question via TTS and not execute a desktop action

---

## 6. Continuous learning

**6.1** WHEN a command succeeds  
THE SYSTEM SHALL record the (input → action) pair in the few-shot SQLite database

**6.2** WHEN retrieving few-shot examples for a new command  
THE SYSTEM SHALL rank stored examples by token overlap weighted by recency and usage count

**6.3** WHEN Gate 1 escalates more than 30% of commands to the cloud AND local failure rate is below 10%  
THE SYSTEM SHALL relax the confidence threshold by 0.05

**6.4** WHEN the vocabulary builder identifies a word appearing ≥ 3 times in successful transcriptions  
THE SYSTEM SHALL add it to the Whisper hotwords list

**6.5** WHEN the gesture calibrator has ≥ 10 samples for a gesture  
THE SYSTEM SHALL set that gesture's confidence floor to the p10 of observed confidences minus 0.05

---

## 7. Accessibility and resilience

**7.1** WHEN any sensor fails to connect or its SDK is not installed  
THE SYSTEM SHALL log a warning and continue operating with remaining sensors

**7.2** WHEN all sensors are unavailable  
THE SYSTEM SHALL still accept commands from the iPad touch interface

**7.3** WHEN the system starts  
THE SYSTEM SHALL print which sensors are connected and which fallbacks are active

**7.4** WHEN the user presses Ctrl-C  
THE SYSTEM SHALL save the gesture calibration file and flush the routing log before exiting
