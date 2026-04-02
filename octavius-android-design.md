# Octavius Android App — Design Document

## Overview

A native Android app that serves as both a **client** for the Octavius voice assistant
and a **phone-side MCP server** that lets Octavius trigger actions on the device. All
communication stays on the Tailscale network — no cloud relay, no third-party services.

---

## Two Components, One App

### 1. Octavius Client (voice assistant UI)

The app connects to Octavius on lilbuddy over the existing WebSocket protocol. It
replaces the browser-based UI with a native Android experience.

**What it does:**
- Records audio from the phone's microphone
- Sends raw audio to Octavius via WebSocket (binary frames)
- Receives JSON control messages (status, transcript, response text)
- Receives audio frames (TTS) and plays them through the phone speaker
- Supports push-to-talk and (eventually) wake word detection

**Why native instead of WebView:**
- Direct microphone access with proper Android audio APIs (AudioRecord/Oboe)
- Background operation as a foreground service
- Integration with the phone MCP server (same process)
- Notification-based quick-talk without opening the full app
- Better audio latency and echo cancellation

### 2. Phone MCP Server (device actions)

A lightweight HTTP server running inside the app, accessible over Tailscale. Octavius
registers it as just another MCP server — same pattern as evangeline-email or searxng.

**Why this architecture:**
- Zero changes to Octavius server-side code beyond adding a config entry
- Tool discovery, argument schemas, and error handling all come free from MCP
- Any MCP client (Octavius, Claude Code, other agents) can use phone tools
- Tailscale handles auth and encryption — no API keys needed

---

## Project Structure

```
octavius-android/
  app/
    src/main/
      java/xyz/riegert/octavius/
        MainActivity.kt              — Jetpack Compose UI, voice button, conversation display
        OctaviusService.kt           — Foreground service: keeps WebSocket + MCP alive
        
        client/
          WebSocketClient.kt         — WebSocket connection to lilbuddy
          AudioRecorder.kt           — Mic capture, outputs PCM/WAV frames
          AudioPlayer.kt             — Queued playback of TTS audio frames
          
        mcp/
          PhoneMcpServer.kt          — Ktor/NanoHTTPD streamable-HTTP MCP server
          McpToolRegistry.kt         — Tool registration and dispatch
          tools/
            PhoneCallTool.kt         — Make/end phone calls
            SmsTool.kt               — Send SMS messages
            ContactsTool.kt          — Search/read contacts
            LocationTool.kt          — Get current GPS location
            NotificationsTool.kt     — Read recent notifications
            AlarmTool.kt             — Set alarms and timers
            BatteryTool.kt           — Battery level and charging status
            FlashlightTool.kt        — Toggle flashlight
            CalendarTool.kt          — Query/create calendar events
            ClipboardTool.kt         — Read/write clipboard
            AppLaunchTool.kt         — Open apps by name/package
            MediaControlTool.kt      — Play/pause/skip media (MediaSession API)
            
      res/
        layout/                      — (Compose, so minimal XML)
        drawable/                    — App icon, status icons
        
    AndroidManifest.xml              — Permissions, foreground service declaration
    
  build.gradle.kts                   — Dependencies (Ktor, OkHttp, Compose, etc.)
```

---

## MCP Tool Inventory

### Phase 1 — Core (initial release)

| Tool | Android API | Permission | Notes |
|------|-------------|------------|-------|
| `make_call` | `Intent.ACTION_CALL` | `CALL_PHONE` | Direct dial, no user confirmation |
| `send_sms` | `SmsManager.sendTextMessage()` | `SEND_SMS` | Direct send |
| `get_battery` | `BatteryManager` | None | Level, charging status, temperature |
| `get_location` | Fused Location Provider | `ACCESS_FINE_LOCATION` | Last known + fresh fix |
| `toggle_flashlight` | `CameraManager.setTorchMode()` | None | On/off |
| `set_alarm` | `AlarmClock.ACTION_SET_ALARM` | None | Delegates to clock app |
| `set_timer` | `AlarmClock.ACTION_SET_TIMER` | None | Delegates to clock app |
| `open_url` | `Intent.ACTION_VIEW` | None | Opens in default browser |
| `launch_app` | `PackageManager` + `Intent` | None | Launch by name or package |

### Phase 2 — Extended

| Tool | Android API | Permission | Notes |
|------|-------------|------------|-------|
| `search_contacts` | `ContactsContract` | `READ_CONTACTS` | Name/number/email lookup |
| `read_notifications` | `NotificationListenerService` | Special access | Requires manual user grant in settings |
| `query_calendar` | `CalendarContract` | `READ_CALENDAR` | Events in a date range |
| `create_calendar_event` | `CalendarContract` | `WRITE_CALENDAR` | Title, time, location, reminders |
| `media_control` | `MediaSessionManager` | `MEDIA_CONTENT_CONTROL` | Play/pause/skip across apps |
| `get_clipboard` | `ClipboardManager` | None | Read current clipboard |
| `set_clipboard` | `ClipboardManager` | None | Write to clipboard |
| `take_photo` | Camera2 API or Intent | `CAMERA` | Capture and return path/thumbnail |

### Phase 3 — Advanced

| Tool | Notes |
|------|-------|
| `read_screen` | Accessibility Service — reads current screen content |
| `file_browser` | List/read/share files on device storage |
| `wifi_status` | Network info, connected SSID, signal strength |
| `do_not_disturb` | Toggle DND mode |
| `screen_brightness` | Get/set brightness |

---

## Octavius Server-Side Config

The only change needed on lilbuddy. Add to `config.py` `MCP_SERVERS`:

```python
"phone": {
    "transport": "http",
    "url": "http://<phone-tailscale-ip>:8260/mcp",
},
```

And update the system prompt to mention phone capabilities:

```
- Phone control for making calls, sending texts, checking battery, setting alarms,
  and other device actions (requires the Android app to be running)
```

---

## Key Technical Challenges

### 1. Keeping the MCP Server Alive

Android aggressively kills background processes to save battery. The app must:

- Run as a **foreground service** with a persistent notification ("Octavius is listening")
- Request battery optimization exemption (`REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`)
- Use `START_STICKY` to restart if killed
- Handle Doze mode (the Ktor server may not respond during deep sleep)
  - Option A: acquire a partial wake lock (bad for battery)
  - Option B: accept that the phone MCP is unavailable during sleep, handle gracefully
  - Option C: use `AlarmManager.setExactAndAllowWhileIdle()` for periodic wake

**Recommendation:** Accept occasional unavailability. Octavius should handle MCP
connection failures gracefully (it already does for other servers). The MCP server
wakes up when the user interacts with the phone or opens the app.

### 2. Audio Pipeline

- **Recording**: `AudioRecord` with 16kHz/16-bit PCM (matching Whisper's expected input)
- **Encoding**: Send raw WAV frames over WebSocket, same as the browser UI
  - Consider Opus encoding to reduce bandwidth if on mobile data
- **Playback**: `AudioTrack` with a queue for sequential TTS chunks
  - Need to handle the same silence-trimming the browser does, but natively
- **Echo cancellation**: Use `AcousticEchoCanceler` to prevent TTS playback from
  being picked up by the mic during continuous conversation

### 3. Network Assumptions

- The phone must be on Tailscale to reach lilbuddy
- Tailscale on Android works well but can disconnect during deep sleep
- The app should show connection status clearly (connected/disconnected to Octavius,
  MCP server running/stopped)
- Consider a reconnection strategy with exponential backoff

### 4. Permissions UX

The app needs several sensitive permissions. Request them incrementally:

1. **On first launch**: Microphone (required for voice), notification (foreground service)
2. **On first phone action**: CALL_PHONE, SEND_SMS — explain why before requesting
3. **Manual grant**: Notification listener access (Android requires Settings UI navigation)

---

## Dependencies

| Library | Purpose |
|---------|---------|
| Jetpack Compose | UI framework |
| OkHttp | WebSocket client to Octavius |
| Ktor Server (CIO) | Phone-side MCP HTTP server |
| kotlinx.serialization | JSON for MCP protocol + WebSocket messages |
| Google Play Services Location | Fused location provider |
| Accompanist Permissions | Runtime permission handling |
| Tailscale (system) | Network connectivity (user installs separately) |

---

## Wake Word Detection (Future)

Always-on listening for "Hey Octavius" or similar. Options:

- **Vosk** (offline, open-source) — small keyword model runs on-device, ~50MB
- **Porcupine** (Picovoice) — purpose-built wake word engine, very low power, but
  requires a license for custom wake words
- **Android's built-in hotword** — only works for Google Assistant, not usable

Vosk is the most aligned with the self-hosted philosophy. The flow would be:
1. Vosk listens continuously with a tiny keyword-spotting model
2. On wake word detection, start recording and open WebSocket
3. On silence detection, send audio and wait for response
4. Return to listening state

This is Phase 3+ territory — push-to-talk (via notification button or in-app) is
sufficient for initial release.

---

## UI Sketch

```
+------------------------------------------+
|  Octavius                    [settings]   |
|                                           |
|  Status: Connected to lilbuddy            |
|  MCP Server: Running on :8260             |
|                                           |
|  +--------------------------------------+ |
|  | Conversation transcript scrolls here | |
|  |                                      | |
|  | You: What's the weather like?        | |
|  |                                      | |
|  | Octavius: Let me check that for you. | |
|  | [tool: searxng/search_web]           | |
|  | It's currently 12 degrees and sunny  | |
|  | in Peterborough.                     | |
|  |                                      | |
|  +--------------------------------------+ |
|                                           |
|        [ Tap to Talk / Stop ]             |
|                                           |
|  [Reset] [Mute MCP] [Text Input]         |
+------------------------------------------+
```

The talk button is **toggle** (tap to start recording, tap to stop) rather than
hold-to-talk. This is more natural on a phone — holding a button while talking is
awkward on a touchscreen, especially one-handed. A persistent notification also offers
a quick-talk button so you can speak without opening the app.

---

## Development Approach

1. **Start with the WebSocket client** — get audio round-tripping working first
2. **Add foreground service** — keep it alive in the background
3. **Add MCP server** — start with `get_battery` and `toggle_flashlight` (no permissions needed) to prove the architecture
4. **Add permission-gated tools** — calls, SMS, location one at a time
5. **Polish** — reconnection handling, notification quick-talk, conversation history recording

---

## Open Questions

- **Conversation history**: Should the Android app record to the same
  `octavius_history.db` via a history-recording MCP endpoint, or should Octavius
  server-side handle it (it already does for WebSocket sessions)?
  - Likely answer: server-side is fine since all audio goes through lilbuddy anyway.
  
- **Multiple phones**: If Dave gets a tablet or second phone, should each device
  register as a separate MCP server (`phone-pixel`, `tablet-samsung`) or share a
  config? Separate is simpler.

- **Security**: Tailscale provides the network boundary, but should the MCP server
  require an additional auth token? Probably not for single-user, but worth noting.

- **Phone-to-phone calls via Octavius**: "Call Mom" requires contact lookup + call.
  The LLM can chain `search_contacts` -> `make_call` naturally if both tools exist.
