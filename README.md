# DukesBot

A personal iMessage AI chatbot that responds in the owner's texting voice when iPhone Focus modes are active. Runs entirely on local hardware - no cloud APIs, no data leaves the home network.

---

## Overview

When Focus mode (Driving, Sleep, AutoRespond) is active on an iPhone, incoming iMessages get automatic replies that sound like the actual owner. The system is designed to be trained on a personal text message corpus and uses a two-stage Ollama pipeline: a lightweight intent classifier followed by a fine-tuned voice generator.

Privacy is a first-class constraint: the model runs on a local Linux machine, all contact data stays in macOS AddressBook, and message content never touches an external API. This public release has been sanitized for privacy: personal voice configuration, contact names, and location data are replaced with documented environment variables.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Mac Mini                                │
│                                                                 │
│  focus_daemon.py ──► focus_state.json                          │
│   (polls DoNotDisturb DB every 5s)                              │
│                            │                                    │
│                     session_state.py                            │
│                            │                                    │
│                            ▼                                    │
│  jason_bot.py ◄────── chat.db (Messages.app SQLite)            │
│       │                                                         │
│       ├─ 1. AddressBook blocklist check                        │
│       ├─ 2. Security filter (injection / rate limit)           │
│       ├─ 3. Safety filter (controversial topics)               │
│       ├─ 4. Message batcher (25s quiet window)                 │
│       ├─ 5. Action detector (calendar/reminder/note)           │
│       ├─ 6. Context builder (live calendar + reminders)        │
│       └─ 7. Two-stage Ollama call ──────────────────────────►  │
│                                                    HTTP :11434  │
│  Messages.app ◄── AppleScript ◄── cleaned reply               │
└────────────────────────────────────────────────┬───────────────┘
                                                 │
                                                 ▼
                        ┌────────────────────────────────────────┐
                        │            Linux Tower                 │
                        │                                        │
                        │  Ollama                                │
                        │   ├── Stage 1: Intent Classifier       │
                        │   │     (gemma4:31b → JSON schema)     │
                        │   └── Stage 2: Voice Generator         │
                        │         (fine-tuned voice model)       │
                        │                                        │
                        │  Docker / Dockge                       │
                        │   └── Unsloth container :8400          │
                        │         └── LoRA training pipeline     │
                        └────────────────────────────────────────┘
```

**Two-machine setup**: The Mac Mini handles all macOS integration (polling `chat.db`, reading AddressBook, calling Calendar/Reminders/Notes via AppleScript, sending via Messages.app). The Linux tower runs Ollama and hosts the training environment, keeping GPU-heavy work off the Mac.

**Three-stage pipeline**: Every incoming message passes through (1) a fast regex safety/security layer, (2) an LLM classifier that returns a structured JSON intent/tone, and (3) the fine-tuned voice model that generates a reply conditioned on that context.

---

## Technical Stack

| Layer | Technology |
|-------|-----------|
| Language model | Llama 3.2 3B Instruct (Meta, open source) |
| Fine-tuning | Unsloth + LoRA / QLoRA (4-bit quantized training) |
| On-device inference | Ollama (self-hosted, Linux tower) |
| Mac inference option | MLX (Apple Silicon, for local testing) |
| Training data | 13,187 conversation pairs (example dataset) |
| Containerization | Docker + Dockge |
| Message polling | SQLite (macOS `chat.db`) |
| Message sending | AppleScript → Messages.app |
| Contact resolution | macOS AddressBook SQLite |
| Scheduling actions | AppleScript → Calendar, Reminders, Notes |
| Focus detection | macOS DoNotDisturb assertion database |
| Automation | macOS LaunchAgents (`launchctl`) |
| Language | Python 3.12 |
| Key dependencies | `requests`, `openpyxl` |

---

## Key Features

### Focus Mode Detection
Rather than polling a system API, the daemon reads `~/Library/DoNotDisturb/DB/Assertions.json` directly: a private macOS database that contains the currently-active Focus mode identifier. This gives sub-second detection with negligible CPU cost (5s polling, <5ms processing).

### iMessage Integration
Incoming messages are polled from `~/Library/Messages/chat.db` via SQLite. Outgoing replies are sent through AppleScript calling `Messages.app`, no undocumented APIs, no SIP violations.

### Two-Stage Ollama Pipeline
Stage 1 (classifier) sends the raw message to a structured-output prompt and parses the resulting JSON for `intent`, `tone`, and `use_name`. Stage 2 (generator) receives the original message plus a context string built from the classifier output, current calendar events, and pending reminders. Separating these calls prevents the voice model from being distracted by classification formatting.

### Trusted Contact Routing
A small set of trusted contacts (family members, close friends) receive richer responses during Focus mode: the bot can parse requests to create Calendar events, set Reminders, or append items to Notes, all via AppleScript. Pending confirmations are stored in `pending_actions.json` and expire after 30 minutes.

### Security Layer
- Prompt injection detection with leetspeak normalization (14 pattern families)
- Per-contact rate limiting: warn at 10 msgs/60s, block for 5 min at 20 msgs/60s
- Input length gate (5,000 char max, truncate at 1,000)
- AppleScript keyword sanitization on outgoing text
- URL stripping before any LLM processing
- Group chat detection (never responds to group chats)
- `focus_state.json` integrity verified via SHA-256 checksum on every read

### Safety Filter
Pure-regex screening (no LLM) across politics, religion, race, gender/sexuality, health misinformation, and conspiracy content. Controversial replies trigger a canned deflection. The model output is also scanned post-generation and replaced if it contains impairment references or hallucinated specifics.

### Privacy-First Design
Zero cloud dependencies. No data is sent to OpenAI, Anthropic, or any external service. Training uses only one side of conversations: the owner's replies, not messages received from others. The bot discloses its nature when asked ("are you a bot?").

---

## Data Pipeline

### Extraction
A personal message corpus is exported from `~/Library/Messages/chat.db` by joining the `message`, `handle`, and `chat_handle_join` tables. Only 1:1 conversations were included; group chats were filtered out. Messages with tapback reactions, attachments, and very short exchanges were removed.

### Cleaning
- Em-dashes, double-dashes replaced with spaces
- Tapback reaction strings (`Liked "..."`, `Reacted 👍 to "..."`) stripped
- Deduplication on consecutive identical messages
- Minimum exchange length filter (at least 2 turns)
- Sensitive name patterns removed from training pairs

### Format
Conversations were formatted as Alpaca-style instruction-following pairs for Unsloth:
```
{"messages": [
  {"role": "user",      "content": "...incoming message..."},
  {"role": "assistant", "content": "...owner reply..."}
]}
```

### Training Stats
- **Conversations**: 13,187 (after filtering)
- **Base model**: Llama 3.2 3B Instruct
- **Method**: LoRA (rank 16, alpha 16, target all linear layers)
- **Epochs**: 5
- **Max sequence length**: 1,024 tokens
- **Hardware**: RTX 2080 Super (8 GB VRAM)
- **Training time**: ~4 hours
- **Framework**: Unsloth (2× faster than HuggingFace Trainer, 60% less VRAM)

---

## Setup

### Prerequisites
- macOS (tested on Sonoma/Sequoia) - Mac Mini or similar
- Linux machine with NVIDIA GPU (8 GB+ VRAM recommended), or any machine running Ollama
- Python 3.12+
- macOS apps installed and signed in: Messages, Calendar, Reminders, Notes
- Full Disk Access granted to `python3` (System Settings → Privacy & Security → Full Disk Access)

### Installation

**1. Clone and set up Python environment**
```bash
git clone https://github.com/yourusername/dukes-bot.git ~/dukes-bot
cd ~/dukes-bot
python3 -m venv ~/imessage_training/venv
source ~/imessage_training/venv/bin/activate
pip install -r requirements.txt
```

**2. Install and configure Ollama on your Linux machine**
```bash
# On Linux tower
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b   # classifier + fallback
# load your fine-tuned model - see docs/training.md
```

**3. Configure environment**
```bash
# Set the Ollama URL to point at your Linux machine
export OLLAMA_URL=http://YOUR_LINUX_IP:11434/api/chat
export OLLAMA_MODEL=your-finetuned-model
export CLASSIFIER_MODEL=llama3.2:3b
```

**4. Configure trusted contacts**

Edit `action_detector.py` and update:
- `TRUSTED_FULL_NAMES`: full name → display name for contacts needing surname disambiguation
- `TRUSTED_FIRST_NAMES`: first-name-only list for unambiguous contacts
- `FOCUS_RESPONSE_NAMES`: which contacts receive bot-voice responses
- `MANUAL_TRUST_OVERRIDES`: phone overrides for numbers not in AddressBook

**5. Set your own phone number**

In `jason_bot.py`, line 82:
```python
MY_PHONE = "+1XXXXXXXXXX"  # your number, used only in log messages
```

**6. Run the bot**
```bash
cd ~/dukes-bot
source ~/imessage_training/venv/bin/activate
python3 focus_daemon.py &   # Focus state monitor
python3 jason_bot.py        # Main bot loop
```

**7. (Optional) Install as LaunchAgents**

See `docs/launchagents.md` for running both processes at login via `launchctl`.

### Runtime Files

The following files are created at runtime and are excluded from git:

| File | Contents |
|------|---------|
| `trusted_contacts.json` | Phone → name cache (rebuilt from AddressBook at startup) |
| `session_state.json` | Per-contact session tracking |
| `focus_state.json` | Current Focus mode state |
| `pending_actions.json` | Calendar/Reminder confirmations awaiting reply |
| `message_buffer.json` | In-flight message batches |
| `contact_memory.db` | (Planned) persistent conversation memory |
| `response_patterns.db` | Indexed training response patterns |
| `*.log` | Rotating bot logs |

See `*.sample.json` files for schema examples.

---

## What I Learned

### VRAM Is the Real Constraint

With an RTX 2080 Super (8 GB VRAM), the usable model size for LoRA fine-tuning topped out at 3B parameters. I tried Llama 3.2 8B and hit OOM before the first gradient step even with 4-bit quantization and `max_seq_length=512`. The 3B model with `max_seq_length=1024` fit comfortably and trained in ~4 hours. Hardware constraints shaped the entire architecture: the two-stage pipeline (small classifier + fine-tuned generator) only exists because I couldn't run a single large model that could do both well. The constraint turned out to be a feature: the classifier is faster and more reliable at structured output than the voice model would be.

### Why `train_on_responses_only` Is Non-Negotiable

In the first training run I forgot to enable `train_on_responses_only` in Unsloth. The loss converged just fine, but the model was terrible at generating responses; it was instead very good at reproducing the input format. When you compute loss over both the instruction tokens and the response tokens, the model spends most of its gradient budget learning the prompt template, not the thing it's supposed to generate. Switching to response-only training (where loss is masked to zero on all non-assistant tokens) made the model noticeably better at voice quality within the same number of epochs.

### Prompt Engineering Doesn't Replace Fine-Tuning: It Completes It

I initially assumed fine-tuning on 13K conversations would be sufficient and the system prompt would be minimal. Wrong. The fine-tune gives the model the *rhythm*: short messages, casual register, knowing when to ask a follow-up question. But specific vocabulary choices (words to avoid and words to prefer) needed to be explicitly listed in the system prompt to be reliable. The fine-tune and the system prompt aren't alternatives; they work at different levels. The model learned the shape of the conversation from training data; the prompt maintains the specific personality guardrails that would otherwise drift between samples.

### The Gap Between Loss and Voice Quality

Training loss going from 2.1 to 0.8 across 5 epochs felt like progress. It was, for coherence and grammar. But "sounds like the owner" isn't captured in perplexity. The best evaluation was reading 50 random outputs from the deployed model and counting how many the owner would actually send. Early runs: maybe 20%. After tuning the system prompt, adding the classifier context, and capping response length: closer to 75%. Automated metrics told me the model was learning; only reading the outputs told me whether it was working.

### Two LLM Calls Is Better Than One

The early version sent the raw message directly to the voice model and asked it to respond naturally. It worked but was inconsistent: sometimes the model would write a two-paragraph essay, sometimes it'd correctly produce one casual sentence. Adding a separate classifier call (which returns a strict JSON schema) and injecting the classified `intent` + `tone` into the voice model's context improved consistency dramatically. The extra 1–2 seconds of latency (the classifier is fast) is worth it. The lesson: language models are better at individual, well-scoped tasks than at doing everything at once in a single prompt.

---

## Project Structure

```
dukes-bot/
├── jason_bot.py           # Main polling loop, Ollama calls, AppleScript send
├── focus_daemon.py        # DoNotDisturb state monitor
├── action_detector.py     # Trusted contact cache + action intent detection
├── action_handler.py      # Calendar/Reminder/Notes orchestration
├── actions.py             # AppleScript wrappers for macOS apps
├── context_builder.py     # Live calendar/reminder/message context injection
├── focus_messages.py      # Session start, opt-out, about-intent responses
├── message_batcher.py     # 25-second quiet-window message batching
├── session_state.py       # Per-contact session + opt-out tracking
├── safety_filter.py       # Regex safety screening (no LLM)
├── security_filter.py     # Injection detection + output sanitization
├── input_sanitizer.py     # Control char stripping, phone validation, checksums
├── build_response_db.py   # Index training data into response_patterns.db
├── classify_unknowns.py   # Reclassify unknown-intent rows via Ollama
├── test_integration.py    # Integration test suite (47 tests, no LLM required)
├── requirements.txt
├── *.sample.json          # Schema examples for runtime state files
└── CLAUDE.md.example      # Scrubbed developer notes (safe to share)
```

---

## License

MIT. See LICENSE file.

Training data is not included in this repository and must be generated from your own message history following the data pipeline documentation.
