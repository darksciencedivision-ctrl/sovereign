
# SOVEREIGN
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.x-green)
![Status](https://img.shields.io/badge/status-research-orange)

## Autonomous Multi‑Model Research Pipeline

SOVEREIGN is a deterministic local AI research system that orchestrates multiple language models
into an adversarial debate pipeline capable of generating, synthesizing, and storing research artifacts.

The system runs completely offline and was designed for independent AI labs using consumer hardware.

---

## Architecture

See `docs/architecture.png`

Pipeline Overview:

Corpus → Topic Extraction → Domain Filter → Multi‑Model Debate →
Synthesis → Quality Gate → Memory Commit → Corpus Expansion

---

## Hardware Target

CPU: Intel i5‑13500T  
RAM: 64GB DDR4  
GPU: RTX 3070  
Storage: NVMe SSD  

---

## Default Model Stack

| Role | Model |
|-----|------|
Debater A | deepseek-r1:8b |
Debater B | dolphin-llama3:8b |
Debater C | qwen3:8b |
Synthesizer | dolphin3:8b |
Embeddings | nomic-embed-text |

---

## Installation

Install Ollama

```
https://ollama.ai
```

Pull models

```
ollama pull deepseek-r1:8b
ollama pull dolphin-llama3:8b
ollama pull qwen3:8b
ollama pull dolphin3:8b
ollama pull nomic-embed-text
```

Initialize memory

```
python praxis/init_praxis.py
```

Start broker

```
powershell broker/broker.ps1
```

Run research cycle

```
python orchestra/cycle_runner.py
```

---

## License

MIT for research and non‑commercial use.  
Commercial use requires a separate commercial license.
