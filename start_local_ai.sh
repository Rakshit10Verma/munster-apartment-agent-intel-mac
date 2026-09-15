#!/bin/bash
set -e
export PATH="$HOME/.local/bin:$PATH"

echo "Starting Gemma 3 4B locally through llama.cpp."
echo "On first run it downloads the Q4_K_M model from Hugging Face."
echo "Leave this Terminal window OPEN while the apartment agent is running."
echo ""
llama serve -hf ggml-org/gemma-3-4b-it-GGUF:Q4_K_M --port 8080
