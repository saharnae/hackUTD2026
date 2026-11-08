# Latency Arena: Nemotron Supervisor

> **Current design (Nov 8):** an orchestrator plus 4 Nemotron subagents (monitor, stress-test, proposal, post-mortem) supervise the trading system through the **adapter interface** in `backend/app/interface.py`. Pick the scenario with `SCENARIO_ADAPTER` in `.env` (`mock_trading` now; `trading` = Varish, `disaster` = Haashim). Register new adapters in `backend/app/adapters/__init__.py`.
> Demo: Inject gate trip → Run supervisor → Accept → post-mortem. `app/agent.py`, `app/tools.py` and `app/quant.py` are the old version and are unused.

---
## Run

```bash
# backend
cd backend && python3 -m venv .venv && source .venv/bin/activate
python -m pip install -r requirements.txt
AGENT_MOCK=1 python -m uvicorn app.main:app --port 8001
# frontend (new terminal)
cd frontend && npm install && npm run dev      # http://localhost:5173
```

Real Nemotron: set `AGENT_MOCK=0` plus `NVIDIA_BASE_URL`, `NEMOTRON_MODEL` and `NVIDIA_API_KEY` in `backend/.env`,
then check the endpoint with `python -m scripts.check_llm`. Brev self-hosting: `brev/start_nim.sh`.
