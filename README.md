# LoClicky

LoClicky is an experimental macOS menu bar assistant forked from Clicky.

It lives in the status bar, listens with push-to-talk, takes a screenshot, sends the transcript plus screen context through a local gateway on `127.0.0.1:5000`, speaks back a short answer, and can point at UI with a blue cursor overlay.

This fork is useful as a hackable base for a screen-aware desktop assistant. It is not a polished product, and local-only mode on a Mac is a real tradeoff.

## Reality Check

If your goal is a fast, high-quality daily assistant, cloud APIs are still the better fit.

If your goal is privacy, hackability, local routing, or learning how to build a menu bar AI companion, this repo is a good starting point.

What local mode does well:

- keeps the app talking to one localhost gateway instead of shipping secrets in Swift
- supports local transcription, local chat routing, and local TTS
- works as a multimodal prototype that can read screenshots and suggest where to click

What local mode does poorly:

- cold start is slow because the local vision model has to warm up
- screenshot reasoning is noticeably slower than cloud models
- small local vision models are weaker at general reasoning and tool use
- anything that depends on the live internet is still limited by the local model and the rest of the app

## Current Architecture

- `leanring-buddy/`: Swift macOS menu bar app
- `gateway/server.py`: loopback-only FastAPI gateway for chat, transcription, TTS, and history
- `gateway/config.json`: per-service routing config for `local` or `cloud`
- `gateway/build_backend.sh`: builds the frozen `gateway-server-mac` binary with PyInstaller
- `worker/`: legacy Cloudflare Worker kept around for reference; the local gateway is now the main path

The app itself only talks to the local gateway. The gateway decides whether each service uses:

- `local` providers such as `llama.cpp`, `faster-whisper`, and macOS `say`
- `cloud` providers such as Anthropic, OpenAI, AssemblyAI, and ElevenLabs

That means you can mix modes. For example:

- cloud chat + local transcription + local TTS
- local chat + cloud transcription + local TTS
- fully local for experimentation

## Quick Start

### Prerequisites

- macOS 14.2+
- Xcode 15+
- Python 3.10+ for the gateway source workflow
- `llama-server` installed if you want local chat through `llama.cpp`

### 1. Install gateway dependencies

```bash
cd gateway
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure routing

Edit `gateway/config.json` and choose `local` or `cloud` for:

- `chat`
- `transcription`
- `tts`

This file is intentionally ignored by git so you can keep your own keys and local settings out of version control.

### 3. Build the bundled gateway binary

```bash
cd gateway
./build_backend.sh
```

This creates `gateway/dist/gateway-server-mac`.

### 4. Open the app in Xcode

```bash
open leanring-buddy.xcodeproj
```

Then in Xcode:

1. Select the `leanring-buddy` scheme.
2. Set your signing team.
3. Press `Cmd + R`.

Important:

- Do not run `xcodebuild` from the terminal for this repo. It can invalidate TCC permissions and force you to re-grant screen recording, accessibility, and related permissions.

### 5. Grant macOS permissions

LoClicky needs:

- Microphone
- Accessibility
- Screen Recording
- Screen Content

## How To Use It

1. Launch the app from Xcode.
2. Click the menu bar icon to open the panel.
3. Hold `Control + Option` to use push-to-talk.
4. Say what you want help with.
5. LoClicky captures your screen, sends the request through the local gateway, and returns:
   - a short spoken summary for TTS
   - a detailed text response for the Swift UI
   - an optional point target so the blue cursor can indicate where to click

Typical examples:

- “What should I click to start a new project?”
- “Where is the settings button?”
- “What does this error message mean?”

## Local Mode Notes

This repo currently uses `llama.cpp` for local chat when `chat.local.provider` is set to `llama_cpp`.

The gateway can manage `llama-server` for you on app launch, but local multimodal startup is still not instant. Expect:

- a warm-up delay before `127.0.0.1:5000` becomes reachable on a cold launch
- slower screenshot turns than cloud models
- better results with small focused prompts than open-ended assistant tasks

If you want the app to feel genuinely useful day to day, the most practical setup is usually:

- cloud chat
- local transcription if you care about keeping audio local
- local TTS if macOS `say` is good enough

## Cloud Mode Notes

Cloud mode is still the best option for:

- faster responses
- better screenshot understanding
- more reliable reasoning
- tasks that benefit from stronger general intelligence

The value of this repo is that the Swift app stays local-first even when you choose cloud providers, because all service routing still goes through the gateway instead of hardcoding network providers into the app.

## Project Structure

```text
leanring-buddy/          Swift macOS app
gateway/                 Local gateway, config, frozen backend build
worker/                  Legacy worker kept for reference
AGENTS.md                Source-of-truth instructions for coding agents
README.md                Project overview and setup
```

## Recommendation

Don’t think of this as a failed product. Think of it as a useful fork that proved a few things clearly:

- the local gateway boundary is the right architectural move
- local-only multimodal on a Mac is possible, but rough
- cloud-backed mode is still the better user experience

If you keep going, optimize for a good product rather than ideological purity:

- use cloud where quality matters
- keep the app architecture clean and local-first
- use local models only where they are actually good enough
