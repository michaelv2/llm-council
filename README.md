# LLM Council

> **This fork runs in service mode:** a one-shot `council` CLI (plus Vibe
> skills) instead of the web app, using Mistral's API and a local Ollama
> model. Setup and usage: [README-service.md](README-service.md); design
> notes: [DESIGN.md](DESIGN.md). The web backend (`backend/main.py`,
> `openrouter.py`, `storage.py`) was removed; `frontend/` is kept from
> upstream but has no backend to talk to in this mode. The text below is
> upstream's original description.

![llmcouncil](header.jpg)

The idea of this repo is that instead of asking a question to your favorite LLM provider (e.g. OpenAI GPT 5.1, Google Gemini 3.0 Pro, Anthropic Claude Sonnet 4.5, xAI Grok 4, eg.c), you can group them into your "LLM Council". This repo is a simple, local web app that essentially looks like ChatGPT except it uses OpenRouter to send your query to multiple LLMs, it then asks them to review and rank each other's work, and finally a Chairman LLM produces the final response.

In a bit more detail, here is what happens when you submit a query:

1. **Stage 1: First opinions**. The user query is given to all LLMs individually, and the responses are collected. The individual responses are shown in a "tab view", so that the user can inspect them all one by one.
2. **Stage 2: Review**. Each individual LLM is given the responses of the other LLMs. Under the hood, the LLM identities are anonymized so that the LLM can't play favorites when judging their outputs. The LLM is asked to rank them in accuracy and insight.
3. **Stage 3: Final response**. The designated Chairman of the LLM Council takes all of the model's responses and compiles them into a single final answer that is presented to the user.

## Vibe Code Alert

This project was 99% vibe coded as a fun Saturday hack because I wanted to explore and evaluate a number of LLMs side by side in the process of [reading books together with LLMs](https://x.com/karpathy/status/1990577951671509438). It's nice and useful to see multiple responses side by side, and also the cross-opinions of all LLMs on each other's outputs. I'm not going to support it in any way, it's provided here as is for other people's inspiration and I don't intend to improve it. Code is ephemeral now and libraries are over, ask your LLM to change it in whatever way you like.

## Quick start (service mode)

```bash
uv sync                          # also installs pytest (dev group)
cp .env.example .env             # then fill in MISTRAL_API_KEY
uv run python -m pytest tests -q # offline
uv run council "What is 2+2? Reply with just the number." --json
```

Councils, chairmen and per-provider timeouts are configured in `council.yaml`.

## Tech Stack

- **CLI:** Python 3.10+, async httpx, OpenAI-compatible APIs (Mistral, Ollama) and the Anthropic API
- **Frontend (upstream, unused here):** React + Vite, react-markdown
- **Package Management:** uv for Python, npm for JavaScript
