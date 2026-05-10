# Requirements Document

## Introduction

This feature adds Wake-on-LAN (WoL) capability to the Personal Desktop Agent system, allowing the iPad web client to wake the Windows PC from sleep. When the PC sleeps, the WebSocket bridge (`ipad_bridge.py`) stops and the connection drops. The user needs a way to wake the PC remotely from the iPad so the bridge can restart and the session can resume.

The solution requires a lightweight always-on network relay (e.g., a Raspberry Pi or router-based service) because browsers cannot send raw UDP packets. The relay receives an HTTP request from the iPad web client and broadcasts the WoL magic packet on the local network.

## Glossary

- **WoL_Relay**: A lightweight always-on service running on the local network (separate from the Windows PC) that receives wake requests over HTTP and broadcasts UDP magic packets to wake the target PC.
- **Web_Client**: The HTML/JS application served by `ipad_bridge.py` and used on the iPad via Safari. It communicates with the bridge over WebSocket and with the WoL_Relay over HTTP.
- **Magic_Packet**: A UDP broadcast packet consisting of 6 bytes of `0xFF` followed by the target MAC address repeated 16 times, sent to port 9 on the broadcast address.
- **NIC**: The Intel Ethernet Controller I226-V network interface card in the Windows PC (MAC: `34-5A-60-AA-95-79`).
- **Bridge**: The `ipad_bridge.py` WebSocket server running on the Windows PC that serves the Web_Client and handles commands.
- **Reconnect_Logic**: The existing exponential backoff reconnection mechanism in the Web_Client that automatically attempts to re-establish the WebSocket connection after disconnection.

## Requirements

### Requirement 1: WoL Hardware Enablement

**User Story:** As the user, I want Wake-on-LAN enabled on my Windows PC's NIC, so that the PC can be woken from sleep by a magic packet received over Ethernet.

#### Acceptance Criteria

1. THE NIC SHALL have Wake-on-LAN enabled in the system BIOS/UEFI settings.
2. THE NIC SHALL have "Wake on Magic Packet" enabled in the Windows Device Manager power management properties.
3. THE NIC SHALL have "Allow this device to wake the computer" enabled in the Windows Device Manager power management properties.
4. WHEN the PC is in sleep state (S3) and a valid Magic_Packet addressed to the NIC MAC is received on the Ethernet interface, THE NIC SHALL wake the PC to a running state.

### Requirement 2: WoL Relay Service

**User Story:** As the user, I want a lightweight always-on relay service on my local network, so that the iPad can trigger a wake packet without needing raw UDP socket access.

#### Acceptance Criteria

1. THE WoL_Relay SHALL listen for HTTP POST requests on a configurable port (default: 9 wakeonlan or 7777).
2. WHEN the WoL_Relay receives a valid HTTP POST request to the `/wake` endpoint, THE WoL_Relay SHALL construct a Magic_Packet using the configured target MAC address and broadcast it as a UDP packet to the local network broadcast address on port 9.
3. WHEN the WoL_Relay successfully sends the Magic_Packet, THE WoL_Relay SHALL respond with HTTP 200 and a JSON body containing `{"status": "sent", "mac": "<target_mac>"}`.
4. IF the WoL_Relay fails to send the Magic_Packet, THEN THE WoL_Relay SHALL respond with HTTP 500 and a JSON body containing `{"status": "error", "message": "<description>"}`.
5. THE WoL_Relay SHALL start automatically on boot of the relay host device.
6. THE WoL_Relay SHALL consume minimal resources (less than 50 MB RAM, negligible CPU at idle).
7. THE WoL_Relay SHALL accept an optional `mac` field in the POST body to override the default target MAC address.

### Requirement 3: Wake PC Button in Web Client

**User Story:** As the user, I want a "Wake PC" button visible in the web client when the WebSocket connection is lost, so that I can wake the PC with a single tap.

#### Acceptance Criteria

1. WHILE the Web_Client WebSocket connection is in a disconnected state, THE Web_Client SHALL display a "Wake PC" button prominently in the UI.
2. WHEN the user taps the "Wake PC" button, THE Web_Client SHALL send an HTTP POST request to the configured WoL_Relay URL at the `/wake` endpoint.
3. WHEN the Web_Client receives an HTTP 200 response from the WoL_Relay, THE Web_Client SHALL display a status message "Wake packet sent — waiting for PC…" for at least 5 seconds.
4. IF the Web_Client does not receive a response from the WoL_Relay within 3 seconds, THEN THE Web_Client SHALL display a status message "Relay unreachable — check relay is running".
5. WHILE the "Wake PC" button is visible, THE Web_Client SHALL disable the button for 10 seconds after each tap to prevent packet flooding.
6. WHEN the WebSocket connection is re-established after a wake request, THE Web_Client SHALL hide the "Wake PC" button and display the normal connected state.
7. THE Web_Client SHALL store the WoL_Relay URL in localStorage settings with a configurable default (e.g., `http://192.168.18.1:7777`).

### Requirement 4: Auto-Reconnection After Wake

**User Story:** As the user, I want the web client to automatically reconnect after the PC wakes, so that I do not need to manually refresh or re-establish the session.

#### Acceptance Criteria

1. WHILE the WebSocket connection is disconnected, THE Reconnect_Logic SHALL continue attempting reconnection using exponential backoff (1s, 2s, 4s, … up to 30s maximum interval).
2. WHEN the "Wake PC" button is tapped, THE Reconnect_Logic SHALL reset the backoff interval to 1 second and begin reconnection attempts immediately.
3. WHEN the WebSocket connection is successfully re-established, THE Web_Client SHALL restore full functionality without requiring a page reload.
4. WHILE reconnection attempts are in progress after a wake request, THE Web_Client SHALL display the attempt count in the status banner (e.g., "Reconnecting (3)…").

### Requirement 5: WoL Relay Configuration in Settings

**User Story:** As the user, I want to configure the WoL relay address in the web client settings, so that I can point to the correct relay device on my network.

#### Acceptance Criteria

1. THE Web_Client settings page SHALL include a "Wake-on-LAN" section with a text input for the WoL_Relay URL.
2. THE Web_Client SHALL persist the WoL_Relay URL in localStorage alongside other settings.
3. WHEN the WoL_Relay URL setting is changed, THE Web_Client SHALL use the new URL for subsequent wake requests without requiring a page reload.
4. THE Web_Client SHALL provide a default WoL_Relay URL value of `http://192.168.18.1:7777` if no value has been configured.

### Requirement 6: WoL Relay Implementation as Python Script

**User Story:** As the user, I want the WoL relay implemented as a simple Python script, so that it can run on any always-on device on my network (e.g., a Raspberry Pi, router with Python, or a second PC).

#### Acceptance Criteria

1. THE WoL_Relay SHALL be implemented as a single Python file (`wol_relay.py`) with no dependencies beyond the Python standard library.
2. THE WoL_Relay SHALL accept command-line arguments for `--port` (default 7777), `--mac` (default target MAC), and `--broadcast` (default `255.255.255.255`).
3. WHEN started, THE WoL_Relay SHALL log its listening address and configured target MAC to stdout.
4. THE WoL_Relay SHALL handle CORS preflight (OPTIONS) requests by responding with appropriate `Access-Control-Allow-Origin: *` headers, enabling cross-origin requests from the Web_Client.
