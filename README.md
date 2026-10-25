# FraudGuard Live — Stage 1 demo

A runnable local research prototype for a privacy-preserving bank-fraud project. It provides three simulated bank nodes, generated live event streams, a from-scratch unlabelled autoencoder, dynamic feature calculation, anomaly alerts, review workflow, FedAvg-style aggregation and CSV export.

**It is not a production-certified banking service.** The built-in transactions are synthetic. The three bank nodes are separate logical SQLite stores running in one local app process, not independent bank infrastructure. Anomaly scores are not fraud probabilities. Do not put real account, card, OTP, PIN or customer data into this prototype.

## Windows quick start

1. Extract the ZIP.
2. Double-click `run_windows.bat`.
3. Wait for Uvicorn to start and open http://127.0.0.1:8000.

Python 3.10+ is recommended. The script creates a virtual environment and installs `requirements.txt` on first run. If Python 3.13 gives a dependency/build issue, use a supported Python 3.12 environment.

## Manual start

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
$env:FRAUDGUARD_INGEST_API_KEY="local-demo-key"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

## Demo flow

1. Click **Initialize all nodes**. It generates 48 unlabeled synthetic observations for each bank (144 total) and processes them through the runtime feature engine/model. Models start with random weights; no pretrained file or fraud/genuine labels are loaded.
2. Go to **Federated learning** and click **Run aggregation round**.
3. Return to **Live transactions**, start the live feed, and watch the events update.
4. Use a manual event form or the OTP, ATM and money-mule scenario buttons. Patterns are generated from raw fields; they are not passed as fraud labels.
5. Open **Investigations**, inspect an anomaly's feature values, and save a review status and note.
6. Export CSV. Reset the demo to clear stored test events and reinitialize the models.

## Dynamic features

- `velocity_score`: recent account transactions in a rolling 2-minute window.
- `behavior_deviation`: robust deviation of current amount from recent account amount history.
- `security_context_score`: failed authentication attempts plus device/location-change signals.
- `fund_flow_risk`: recipient novelty and recent incoming/outgoing flow indicators.

These are risk signals, not proof of fraud. The online autoencoder has a 4-8-2-8-4 architecture and learns reconstruction of unlabelled event features. After warm-up, a high reconstruction error can trigger an anomaly alert. No supervised fraud classifier is trained by this app.

## Federated learning

Each simulated bank has its own SQLite event store and a local copy of the same model architecture. Aggregation averages compatible parameter arrays weighted by local update counts and distributes the resulting global model back to the clients. This is an educational FedAvg-style demonstration. FedAvg alone does not provide differential privacy or protection against model-update leakage/poisoning.

## API

- `GET /healthz` — health check.
- `GET /api/state` — dashboard metrics and recent events.
- `POST /api/bootstrap` — `{ "count": 48 }` or `{ "count": 48, "bank_id": "BANK_A" }` (30–120 events per selected bank).
- `POST /api/stream/start` and `/api/stream/stop` — control the synthetic feed.
- `POST /api/transaction` — manually submit an event in the local demo.
- `POST /api/ingest` — external event ingestion; requires `X-API-Key` header.
- `POST /api/scenario/otp`, `/api/scenario/atm`, `/api/scenario/mule` — generate synthetic event sequences.
- `POST /api/federation/aggregate` — run a model aggregation round.
- `POST /api/settings` — `{ "threshold_multiplier": 1.0 }` (0.7–2.5).
- `POST /api/reviews/{event_id}` — `{ "status": "Reviewed", "note": "..." }`.
- `GET /api/export.csv` — export recent event history and calculated features.
- `POST /api/reset` — clears demo history and resets model weights.

Example external event (demo key only):

```bash
curl -X POST http://127.0.0.1:8000/api/ingest \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: local-demo-key' \
  -d '{"bank_id":"BANK_A","account_id":"TEST_01","counterparty_id":"PAYEE_09","amount":1200,"direction":"OUT","channel":"UPI"}'
```

## Real-bank connection boundary

Actual financial institutions require authorised data feeds/sandbox access, independent deployments, TLS and strong service identity, managed secrets, access controls, audit logging, encryption/retention rules, resilience and security testing, poisoning/leakage protections, drift monitoring, verified model evaluation and a bank-owned payment decision workflow. The sample key and same-process bank nodes are for local Stage 1 only.
