#!/bin/bash

set -euo pipefail

exec llama-server \
  --hf-repo ggml-org/gemma-3-4b-it-GGUF \
  --alias gemma-3-4b-it \
  --host 127.0.0.1 \
  --port 8081 \
  --ctx-size 4096 \
  --no-webui
