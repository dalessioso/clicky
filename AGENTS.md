# Clicky - Agent Instructions

<!-- This is the single source of truth for all AI coding agents. CLAUDE.md is a symlink to this file. -->
<!-- AGENTS.md spec: https://github.com/agentsmd/agents.md — supported by Claude Code, Cursor, Copilot, Gemini CLI, and others. -->

## Overview

macOS menu bar companion app. The local dev build is currently branded as `LoClicky` so it can live alongside the real Clicky app. It lives entirely in the macOS status bar (no dock icon, no main window). Clicking the menu bar icon opens a custom floating panel with companion voice controls. Uses push-to-talk (ctrl+option) to capture voice input, sends locally recorded audio to a Python gateway running on `http://127.0.0.1:5000`, and sends the transcript + a screenshot of the user's screen to that same localhost service for AI routing. The gateway returns dual-channel JSON with `spoken_summary` for TTS and `detailed_text` for the Swift UI. A blue cursor overlay can still fly to and point at UI elements referenced by the gateway response.

## Architecture

- **App Type**: Menu bar-only (`LSUIElement=true`), no dock icon or main window
- **Framework**: SwiftUI (macOS native) with AppKit bridging for menu bar panel and cursor overlay
- **Pattern**: MVVM with `@StateObject` / `@Published` state management
- **Backend Routing**: Swift communicates only with the local Python gateway on `127.0.0.1:5000`
- **AI Chat**: Routed through the local gateway, which decides whether to use local models (currently `llama.cpp` or Ollama) or user-provided cloud APIs
- **Speech-to-Text**: Push-to-talk audio is recorded locally and uploaded to the local gateway as WAV
- **Text-to-Speech**: Spoken summaries are sent to the local gateway, which returns audio bytes for playback
- **Screen Capture**: ScreenCaptureKit (macOS 14.2+), multi-monitor support
- **Voice Input**: Push-to-talk via `AVAudioEngine` + pluggable transcription-provider layer. System-wide keyboard shortcut via listen-only CGEvent tap.
- **Element Pointing**: The local gateway can return an optional `point_target` payload with screen coordinates. The overlay maps coordinates to the correct monitor and animates the blue cursor along a bezier arc to the target.
- **Concurrency**: `@MainActor` isolation, async/await throughout
- **Telemetry**: Disabled. `ClickyAnalytics.swift` is now a no-op shim during the local-first migration.

### Key Architecture Decisions

**Menu Bar Panel Pattern**: The companion panel uses `NSStatusItem` for the menu bar icon and a custom borderless `NSPanel` for the floating control panel. This gives full control over appearance (dark, rounded corners, custom shadow) and avoids the standard macOS menu/popover chrome. The panel is non-activating so it doesn't steal focus. A global event monitor auto-dismisses it on outside clicks.

**Cursor Overlay**: A full-screen transparent `NSPanel` hosts the blue cursor companion. It's non-activating, joins all Spaces, and never steals focus. The cursor position, response text, waveform, and pointing animations all render in this overlay via SwiftUI through `NSHostingView`.

**Global Push-To-Talk Shortcut**: Background push-to-talk uses a listen-only `CGEvent` tap instead of an AppKit global monitor so modifier-based shortcuts like `ctrl + option` are detected more reliably while the app is running in the background.

**Single Local Gateway Boundary**: The Swift app is not allowed to talk to cloud services directly. Chat, transcription, and TTS all go through the localhost Python gateway so provider selection and BYOK routing stay outside the app bundle.

**Transient Cursor Mode**: When "Show Clicky" is off, pressing the hotkey fades in the cursor overlay for the duration of the interaction (recording → response → TTS → optional pointing), then fades it out automatically after 1 second of inactivity.

## Key Files

| File | Lines | Purpose |
|------|-------|---------|
| `leanring_buddyApp.swift` | ~68 | Menu bar app entry point. Uses `@NSApplicationDelegateAdaptor` with `CompanionAppDelegate` which creates `MenuBarPanelManager` and starts `CompanionManager`. No main window — the app lives entirely in the status bar. |
| `CompanionManager.swift` | ~707 | Central state machine. Owns dictation, shortcut monitoring, screen capture, localhost gateway chat/TTS routing, and overlay management. Tracks voice state, conversation history, response channels, and cursor visibility. Coordinates the full push-to-talk → screenshot → local gateway → TTS → pointing pipeline. |
| `MenuBarPanelManager.swift` | ~243 | NSStatusItem + custom NSPanel lifecycle. Creates the menu bar icon, manages the floating companion panel (show/hide/position), installs click-outside-to-dismiss monitor. |
| `CompanionPanelView.swift` | ~685 | SwiftUI panel content for the menu bar dropdown. Shows companion status, push-to-talk instructions, localhost gateway status, setup progress, permissions UI, and quit button. Dark aesthetic using `DS` design system. |
| `OverlayWindow.swift` | ~881 | Full-screen transparent overlay hosting the blue cursor, response text, waveform, and spinner. Handles cursor animation, element pointing with bezier arcs, multi-monitor coordinate mapping, and fade-out transitions. |
| `CompanionResponseOverlay.swift` | ~217 | SwiftUI view for the response text bubble and waveform displayed next to the cursor in the overlay. |
| `CompanionHistoryView.swift` | ~217 | Native SwiftUI scrollable history panel for reviewing previous interactions with copy functionality. |
| `CompanionScreenCaptureUtility.swift` | ~132 | Multi-monitor screenshot capture using ScreenCaptureKit. Returns labeled image data for each connected display. |
| `BuddyDictationManager.swift` | ~866 | Push-to-talk voice pipeline. Handles microphone capture via `AVAudioEngine`, provider-aware permission checks, keyboard/button dictation sessions, transcript finalization, shortcut parsing, contextual keyterms, and live audio-level reporting for waveform feedback. |
| `BuddyTranscriptionProvider.swift` | ~38 | Protocol surface and provider factory for voice transcription backends. Currently resolves to the localhost gateway transcription provider. |
| `LocalGatewayAudioTranscriptionProvider.swift` | ~214 | Localhost-only transcription provider. Buffers push-to-talk audio locally, converts it to WAV, and posts it to the Python gateway for transcription. |
| `AppleSpeechTranscriptionProvider.swift` | ~147 | Local fallback transcription provider backed by Apple's Speech framework. |
| `BuddyAudioConversionSupport.swift` | ~108 | Audio conversion helpers. Converts live mic buffers to PCM16 mono audio and builds WAV payloads for upload-based providers. |
| `GlobalPushToTalkShortcutMonitor.swift` | ~132 | System-wide push-to-talk monitor. Owns the listen-only `CGEvent` tap and publishes press/release transitions. |
| `BackendManager.swift` | ~158 | Manages the lifecycle of the bundled gateway binary (PyInstaller executable). Launches it as a child process, monitors first-boot stdout logs, and terminates it gracefully on quit. Relies on the Xcode target to copy `gateway-server-mac` and `config.json` into the app bundle resources at build time. |
| `LocalGatewayChatClient.swift` | ~184 | Localhost-only chat client. Sends transcripts, screenshots, and conversation history to the Python gateway and decodes the dual-channel JSON response plus optional point target. |
| `LocalGatewayTTSClient.swift` | ~75 | Localhost-only TTS client. Sends `spoken_summary` text to the Python gateway and plays back the returned audio via `AVAudioPlayer`. |
| `DesignSystem.swift` | ~880 | Design system tokens — colors, corner radii, shared styles. All UI references `DS.Colors`, `DS.CornerRadius`, etc. |
| `ClickyAnalytics.swift` | ~28 | No-op telemetry shim kept temporarily so analytics call sites can be removed incrementally without reintroducing network egress. |
| `WindowPositionManager.swift` | ~262 | Window placement logic, Screen Recording permission flow, and accessibility permission helpers. |
| `AppBundleConfiguration.swift` | ~35 | Runtime configuration reader plus the shared localhost gateway URL helper used by all Swift-side network clients. |
| `gateway/server.py` | ~1560 | FastAPI localhost gateway. Implements `/chat`, `/transcribe`, `/tts`, and `/history`. Handles offline persistence via encrypted SQLite in Application Support, validates BYOK routing config at startup, and supports both `ollama` and `llama_cpp` as local chat providers. |
| `gateway/config.json` | ~66 | BYOK configuration manifest. Controls provider selection and model hot-swapping for chat, transcription, and TTS. Set `mode` to `local` or `cloud` per service domain. |
| `gateway/start_llama_cpp_server.sh` | ~11 | Helper launcher for the recommended local `llama.cpp` vision/chat server using the Gemma 3 4B GGUF model on `127.0.0.1:8081`. |
| `gateway/build_backend.sh` | ~35 | PyInstaller execution script. Compiles server.py and dependencies into a single frozen `gateway-server-mac` macOS binary. |
| `gateway/requirements.txt` | ~18 | Python dependencies for the local gateway. |
| `worker/src/index.ts` | ~142 | Legacy Cloudflare Worker proxy kept in the repo for reference during migration. The Swift app no longer routes through it. |

## Build & Run

```bash
# Open in Xcode
open leanring-buddy.xcodeproj

# Select the leanring-buddy scheme, set signing team, Cmd+R to build and run
# The app target copies gateway/dist/gateway-server-mac and gateway/config.json
# into LoClicky.app/Contents/Resources during the Xcode build.
# Rebuild in Xcode after changing either of those gateway files.

# Known non-blocking warnings: Swift 6 concurrency warnings,
# deprecated onChange warning in OverlayWindow.swift. Do NOT attempt to fix these.
```

## Local Gateway

```bash
# Set up the Python gateway (requires Python 3.10+)
cd gateway
pip install -r requirements.txt

# Edit config.json to set mode (local/cloud) and provider keys
# Then start the gateway
python server.py

# The gateway binds to 127.0.0.1:5000 — the Swift app expects this.
# For local chat mode, start the configured provider first:
#   ./start_llama_cpp_server.sh
# or ensure Ollama is running if config.json is set back to provider "ollama".
# For local faster-whisper mode, provide a local model path or pre-cache the
# configured model. The gateway checks readiness but does not auto-download it.
```

**Do NOT run `xcodebuild` from the terminal** — it invalidates TCC (Transparency, Consent, and Control) permissions and the app will need to re-request screen recording, accessibility, etc.

## Legacy Worker

```bash
# Legacy only — the Swift app no longer calls this worker directly.
cd worker
npm install

# Add secrets
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put ASSEMBLYAI_API_KEY
npx wrangler secret put ELEVENLABS_API_KEY

# Deploy
npx wrangler deploy

# Local dev (create worker/.dev.vars with your keys)
npx wrangler dev
```

## Code Style & Conventions

### Variable and Method Naming

IMPORTANT: Follow these naming rules strictly. Clarity is the top priority.

- Be as clear and specific with variable and method names as possible
- **Optimize for clarity over concision.** A developer with zero context on the codebase should immediately understand what a variable or method does just from reading its name
- Use longer names when it improves clarity. Do NOT use single-character variable names
- Example: use `originalQuestionLastAnsweredDate` instead of `originalAnswered`
- When passing props or arguments to functions, keep the same names as the original variable. Do not shorten or abbreviate parameter names. If you have `currentCardData`, pass it as `currentCardData`, not `card` or `cardData`

### Code Clarity

- **Clear is better than clever.** Do not write functionality in fewer lines if it makes the code harder to understand
- Write more lines of code if additional lines improve readability and comprehension
- Make things so clear that someone with zero context would completely understand the variable names, method names, what things do, and why they exist
- When a variable or method name alone cannot fully explain something, add a comment explaining what is happening and why

### Swift/SwiftUI Conventions

- Use SwiftUI for all UI unless a feature is only supported in AppKit (e.g., `NSPanel` for floating windows)
- All UI state updates must be on `@MainActor`
- Use async/await for all asynchronous operations
- Comments should explain "why" not just "what", especially for non-obvious AppKit bridging
- AppKit `NSPanel`/`NSWindow` bridged into SwiftUI via `NSHostingView`
- All buttons must show a pointer cursor on hover
- For any interactive element, explicitly think through its hover behavior (cursor, visual feedback, and whether hover should communicate clickability)

### Do NOT

- Do not add features, refactor code, or make "improvements" beyond what was asked
- Do not add docstrings, comments, or type annotations to code you did not change
- Do not try to fix the known non-blocking warnings (Swift 6 concurrency, deprecated onChange)
- Do not rename the project directory or scheme (the "leanring" typo is intentional/legacy)
- Do not run `xcodebuild` from the terminal — it invalidates TCC permissions

## Git Workflow

- Branch naming: `feature/description` or `fix/description`
- Commit messages: imperative mood, concise, explain the "why" not the "what"
- Do not force-push to main

## Self-Update Instructions

<!-- AI agents: follow these instructions to keep this file accurate. -->

When you make changes to this project that affect the information in this file, update this file to reflect those changes. Specifically:

1. **New files**: Add new source files to the "Key Files" table with their purpose and approximate line count
2. **Deleted files**: Remove entries for files that no longer exist
3. **Architecture changes**: Update the architecture section if you introduce new patterns, frameworks, or significant structural changes
4. **Build changes**: Update build commands if the build process changes
5. **New conventions**: If the user establishes a new coding convention during a session, add it to the appropriate conventions section
6. **Line count drift**: If a file's line count changes significantly (>50 lines), update the approximate count in the Key Files table

Do NOT update this file for minor edits, bug fixes, or changes that don't affect the documented architecture or conventions.
