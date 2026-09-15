# READ THIS FIRST — Intel Mac version

Your Mac:
- 2019 Intel MacBook Pro
- Intel Core i9
- 16 GB RAM
- macOS Ventura

Ollama is not required.

This project uses:

**llama.cpp -> Gemma 3 4B -> apartment agent**

Cloud fallbacks remain:
**Claude -> OpenAI -> Gemini**

## STEP 1 — setup

Open Terminal in this folder:

```bash
chmod +x setup_intel_mac.sh start_local_ai.sh start_qwen_instead.sh run_sample.sh
./setup_intel_mac.sh
```

## STEP 2 — start the free local AI

Open a NEW Terminal window, `cd` into this project folder, then:

```bash
./start_local_ai.sh
```

The first run downloads Gemma 3 4B Q4_K_M. Leave this Terminal open.

If Gemma does not work well enough, stop it with Ctrl-C and try:

```bash
./start_qwen_instead.sh
```

## STEP 3 — test the apartment agent

In another Terminal window:

```bash
source .venv/bin/activate
python -m app.cli doctor
./run_sample.sh
```

## STEP 4 — optional cloud keys

```bash
open -e .env
```

Put keys after the equals signs. They are optional:

```text
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GEMINI_API_KEY=
```

Never send your keys to anyone.

Default routing:

```text
Gemma local
  -> if uncertain/fails: Claude
      -> if unavailable: OpenAI GPT-5.6 Luna
          -> if unavailable: Gemini 3.8 Flash
```

## STEP 5 — real listing

```bash
python -m app.cli analyze
```

Paste the full listing, then press Control-D.

Nothing is sent to WG-Gesucht yet.
This phase only tests the brain and message generation.
