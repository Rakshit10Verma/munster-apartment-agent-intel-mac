#!/bin/bash
set -e
export PATH="$HOME/.local/bin:$PATH"
echo "Alternative local model: Qwen3 4B Q4_K_M"
llama serve -hf ggml-org/Qwen3-4B-GGUF:Q4_K_M --port 8080
