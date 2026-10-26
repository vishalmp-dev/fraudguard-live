from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import random
import sqlite3
import uuid
import threading
import logging

from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal, Optional

import numpy as np
from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger("uvicorn.error")

ROOT = Path(__file__).resolve().parent
RUNTIME = Path(
    os.getenv("FRAUDGUARD_RUNTIME", str(ROOT / "runtime"))
).resolve()

DB_DIR = RUNTIME / "banks"
MODEL_DIR = RUNTIME / "models"

DB_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

API_KEY = os.getenv("FRAUDGUARD_INGEST_API_KEY", "local-demo-key")

BANKS = {
    "BANK_A": {
        "name": "Bank A",
        "focus": "OTP / account takeover",
        "accent": "blue",
    },
    "BANK_B": {
        "name": "Bank B",
        "focus": "ATM / card misuse",
        "accent": "violet",
    },
    "BANK_C": {
        "name": "Bank C",
        "focus": "Money mule / suspicious transfers",
        "accent": "teal",
    },
}

FEATURES = [
    "velocity_score",
    "behavior_deviation",
    "security_context_score",
    "fund_flow_risk",
]

WARMUP = 30


def now():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or now()).astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )


def dtparse(s):
    if not s:
        return now()

    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        raise HTTPException(422, "occurred_at must be ISO-8601")


def clamp(x, a=0.0, b=1.0):
    return float(max(a, min(b, x)))


class Autoencoder:
    def __init__(self, seed=42):
        r = np.random.default_rng(seed)

        self.p = {
            "W1": r.normal(0, 0.35, (4, 8)),
            "b1": np.zeros(8),
            "W2": r.normal(0, 0.3, (8, 2)),
            "b2": np.zeros(2),
            "W3": r.normal(0, 0.3, (2, 8)),
            "b3": np.zeros(8),
            "W4": r.normal(0, 0.3, (8, 4)),
            "b4": np.zeros(4),
        }
        self.updates = 0

    def state(self):
        return {k: v.copy() for k, v in self.p.items()}

    def load_state(self, s):
        if set(s) != set(self.p):
            raise ValueError("Incompatible model parameters")

        for k, v in s.items():
            if v.shape != self.p[k].shape:
                raise ValueError("Incompatible parameter shape")
            self.p[k] = v.astype(float, copy=True)

    def copy_from(self, other):
        self.load_state(other.p)

    def forward(self, x, cache=False):
        p = self.p

        z1 = x @ p["W1"] + p["b1"]
        h1 = np.maximum(z1, 0)

        z2 = h1 @ p["W2"] + p["b2"]
        h2 = np.tanh(z2)

        z3 = h2 @ p["W3"] + p["b3"]
        h3 = np.maximum(z3, 0)

        z4 = h3 @ p["W4"] + p["b4"]
        out = 1 / (1 + np.exp(-np.clip(z4, -25, 25)))

        if cache:
            return out, (z1, h1, h2, z3, h3, out)

        return out

    def error(self, x):
        return float(np.mean((self.forward(x) - x) ** 2))

    def train_one(self, x, epochs=1, lr=0.015):
        x = np.asarray(x, float).reshape(4)
        p = self.p

        for _ in range(epochs):
            out, (z1, h1, h2, z3, h3, out) = self.forward(x, True)

            d4 = (2 / 4) * (out - x) * out * (1 - out)
            grads = {
                "W4": np.outer(h3, d4),
                "b4": d4.copy(),
            }

            d3 = (d4 @ p["W4"].T) * (z3 > 0)
            grads.update(
                W3=np.outer(h2, d3),
                b3=d3.copy(),
            )

            d2 = (d3 @ p["W3"].T) * (1 - h2 * h2)
            grads.update(
                W2=np.outer(h1, d2),
                b2=d2.copy(),
            )

            d1 = (d2 @ p["W2"].T) * (z1 > 0)
            grads.update(
                W1=np.outer(x, d1),
                b1=d1.copy(),
            )

            for k, g in grads.items():
                p[k] -= lr * np.clip(g, -2, 2)

        self.updates += 1

    def save(self, path):
        temp = path.with_name(path.stem + ".tmp.npz")
        np.savez_compressed(
            temp,
            **self.p,
            __updates=np.array([self.updates]),
        )
        temp.replace(path)

    def load(self, path):
        if not path.exists():
            return False

        try:
            with np.load(path, allow_pickle=False) as d:
                self.load_state({k: d[k] for k in self.p})
                self.updates = int(d["__updates"][0])
            return True
        except Exception:
            return False


class Txn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_id: Literal["BANK_A", "BANK_B", "BANK_C"]
    account_id: str = Field(min_length=2, max_length=80)
    counterparty_id: str = Field(min_length=2, max_length=80)
    amount: float = Field(gt=0, le=100_000_000)

    direction: Literal["IN", "OUT"] = "OUT"
    channel: Literal[
        "UPI", "ATM", "CARD", "BANK_TRANSFER", "POS", "OTHER"
    ] = "UPI"

    auth_failures: int = Field(default=0, ge=0, le=100)
    device_changed: bool = False
    location_changed: bool = False
    status: Literal["SUCCESS", "DECLINED", "PENDING"] = "SUCCESS"
    occurred_at: Optional[str] = None


class Settings(BaseModel):
    threshold_multiplier: float = Field(ge=0.7, le=2.5)


class Review(BaseModel):
    status: Literal[
        "Open",
        "Reviewed",
        "Escalated",
        "Closed - no action",
    ]
    note: str = Field(default="", max_length=500)


class BankNode:
    def __init__(self, bid, global_model):
        self.id = bid
        self.cfg = BANKS[bid]
        self.db = DB_DIR / f"{bid.lower()}.sqlite3"
        self.modelpath = MODEL_DIR / f"{bid.lower()}_model.npz"

        # Serialize processing per bank to prevent model-file write conflicts.
        self.process_lock = threading.RLock()

        self.model = Autoencoder(42)
        self.model.copy_from(global_model)
        self.model.load(self.modelpath)

        self.history = deque(maxlen=4000)
        self.ref = deque(maxlen=400)
        self.events_count = 0
        self.alerts_count = 0
        self.last = None

        self.initdb()
        self.load_history()

    def con(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def initdb(self):
        with self.con() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS events(
                    event_id TEXT PRIMARY KEY,
                    received_at TEXT,
                    occurred_at TEXT,
                    bank_id TEXT,
                    account_id TEXT,
                    counterparty_id TEXT,
                    amount REAL,
                    direction TEXT,
                    channel TEXT,
                    auth_failures INTEGER,
                    device_changed INTEGER,
                    location_changed INTEGER,
                    status TEXT,
                    features_json TEXT,
                    anomaly_score REAL,
                    reconstruction_error REAL,
                    is_alert INTEGER,
                    model_updated INTEGER,
                    model_ready INTEGER,
                    alert_reason TEXT,
                    review_status TEXT DEFAULT 'Open',
                    review_note TEXT DEFAULT '',
                    source TEXT
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_event_time "
                "ON events(received_at DESC)"
            )

    def load_history(self):
        with self.con() as c:
            rows = c.execute(
                "SELECT * FROM events ORDER BY occurred_at ASC LIMIT 4000"
            ).fetchall()

        for r in rows:
            d = dict(r)

            self.history.append(
                {
                    k: d[k]
                    for k in [
                        "event_id",
                        "bank_id",
                        "account_id",
                        "counterparty_id",
                        "amount",
                        "direction",
                        "channel",
                        "auth_failures",
                        "device_changed",
                        "location_changed",
                        "status",
                        "occurred_at",
                        "received_at",
                    ]
                }
            )

            try:
                f = json.loads(d["features_json"])
                x = np.array([f[k] for k in FEATURES])

                if d["model_updated"]:
                    self.ref.append(x)
            except Exception:
                pass

            self.events_count += 1
            self.alerts_count += int(d["is_alert"])
            self.last = d["received_at"]

    def feats(self, raw, t):
        acct = raw["account_id"]
        prior = []

        for e in self.history:
            if e["account_id"] == acct:
                d = (t - dtparse(e["occurred_at"])).total_seconds()
                if d > 0:
                    prior.append((e, d))

        recent2 = [e for e, d in prior if d <= 120]
        recent10 = [e for e, d in prior if d <= 600]
        recent30 = [e for e, d in prior if d <= 30 * 86400]

        velocity = clamp(len(recent2) / 6)

        amts = np.array([float(e["amount"]) for e in recent30])
        dev = 0.0

        if len(amts) >= 5:
            med = float(np.median(amts))
            mad = float(np.median(np.abs(amts - med)))
            scale = max(1.4826 * mad, med * 0.15, 100)
            z = abs(raw["amount"] - med) / scale
            dev = clamp(z / (z + 2))

        security = clamp(
            min(raw["auth_failures"] / 4, 1) * 0.55
            + (0.25 if raw["device_changed"] else 0)
            + (0.2 if raw["location_changed"] else 0)
        )

        seen = any(
            e["counterparty_id"] == raw["counterparty_id"]
            and e["direction"] == raw["direction"]
            for e, d in prior
        )
        novelty = 0.0 if seen else 1.0

        if raw["direction"] == "OUT":
            inflow = sum(
                e["amount"] for e in recent10 if e["direction"] == "IN"
            )
            recipients = len(
                {
                    e["counterparty_id"]
                    for e in recent10
                    if e["direction"] == "OUT"
                }
            )

            flow = (
                0.55 * (clamp(raw["amount"] / max(inflow, 1)) if inflow else 0)
                + 0.25 * novelty
                + 0.2 * clamp(recipients / 5)
            )
        else:
            senders = len(
                {
                    e["counterparty_id"]
                    for e in recent10
                    if e["direction"] == "IN"
                }
            )
            outflow = sum(
                e["amount"] for e in recent10 if e["direction"] == "OUT"
            )

            flow = (
                0.5 * clamp(senders / 5)
                + 0.25 * novelty
                + 0.25 * clamp(outflow / max(raw["amount"], 1))
            )

        vals = np.array(
            [velocity, dev, security, clamp(flow)],
            float,
        )

        detail = dict(
            zip(FEATURES, [round(float(v), 4) for v in vals])
        )
        detail.update(
            prior_account_events=len(prior),
            recent_events_2min=len(recent2),
            amount_history_count=len(amts),
            new_counterparty=not seen,
        )

        return vals, detail

    def threshold(self, mult):
        if len(self.ref) < 8:
            return 0.012 * mult

        errs = np.array(
            [self.model.error(x) for x in list(self.ref)[-250:]]
        )
        med = float(np.median(errs))
        mad = float(np.median(abs(errs - med)))

        return max(
            float(np.quantile(errs, 0.975)) * 1.22,
            med + 3 * 1.4826 * mad,
            0.0025,
        ) * mult

    def process(self, data, source="manual", bootstrap=False, mult=1.0):
        # Prevent overlapping updates to this bank's model, history and file.
        with self.process_lock:
            return self._process_unlocked(
                data,
                source,
                bootstrap,
                mult,
            )

    def _process_unlocked(
        self,
        data,
        source="manual",
        bootstrap=False,
        mult=1.0,
    ):
        t = dtparse(data.get("occurred_at"))
        recv = now()

        raw = {
            "event_id": str(uuid.uuid4()),
            "received_at": stamp(recv),
            "occurred_at": stamp(t),
            "bank_id": self.id,
            "account_id": data["account_id"].strip(),
            "counterparty_id": data["counterparty_id"].strip(),
            "amount": round(float(data["amount"]), 2),
            "direction": data.get("direction", "OUT"),
            "channel": data.get("channel", "UPI"),
            "auth_failures": int(data.get("auth_failures", 0)),
            "device_changed": bool(data.get("device_changed", False)),
            "location_changed": bool(data.get("location_changed", False)),
            "status": data.get("status", "SUCCESS"),
        }

        x, f = self.feats(raw, t)
        ready = self.model.updates >= WARMUP

        err = None
        score = None
        alert = False
        updated = False

        if bootstrap or not ready:
            self.model.train_one(
                x,
                epochs=20 if bootstrap else 2,
                lr=0.018,
            )
            self.ref.append(x.copy())
            updated = True
            reason = "Unlabelled learning observation; baseline is warming up."
        else:
            err = self.model.error(x)
            ratio = err / max(self.threshold(mult), 1e-6)

            score = round(
                min(100, max(0, 60 * ratio))
                if ratio <= 1
                else min(100, 60 + 40 * min((ratio - 1) / 2, 1)),
                2,
            )
            alert = ratio > 1
            parts = []

            if alert:
                if f["velocity_score"] >= 0.5:
                    parts.append("transaction burst")
                if f["behavior_deviation"] >= 0.55:
                    parts.append("amount differs from account baseline")
                if f["security_context_score"] >= 0.3:
                    parts.append(
                        "authentication/device/location context changed"
                    )
                if f["fund_flow_risk"] >= 0.55:
                    parts.append("unusual counterparty or fund flow")

                reason = ", ".join(parts) or (
                    "Reconstruction error exceeded learned baseline threshold"
                )
            else:
                self.model.train_one(x, epochs=1, lr=0.006)
                self.ref.append(x.copy())
                updated = True
                reason = "Within the currently learned behaviour range"

        self.history.append(raw)
        self.events_count += 1
        self.alerts_count += int(alert)
        self.last = raw["received_at"]
        self.model.save(self.modelpath)

        with self.con() as c:
            c.execute(
                """
                INSERT INTO events(
                    event_id, received_at, occurred_at, bank_id,
                    account_id, counterparty_id, amount, direction,
                    channel, auth_failures, device_changed,
                    location_changed, status, features_json,
                    anomaly_score, reconstruction_error, is_alert,
                    model_updated, model_ready, alert_reason, source
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    raw["event_id"],
                    raw["received_at"],
                    raw["occurred_at"],
                    self.id,
                    raw["account_id"],
                    raw["counterparty_id"],
                    raw["amount"],
                    raw["direction"],
                    raw["channel"],
                    raw["auth_failures"],
                    int(raw["device_changed"]),
                    int(raw["location_changed"]),
                    raw["status"],
                    json.dumps(f),
                    score,
                    err,
                    int(alert),
                    int(updated),
                    int(self.model.updates >= WARMUP),
                    reason,
                    source,
                ),
            )

        return {
            **raw,
            "features": f,
            "anomaly_score": score,
            "reconstruction_error": err,
            "is_alert": alert,
            "model_updated": updated,
            "model_ready": self.model.updates >= WARMUP,
            "alert_reason": reason,
            "review_status": "Open" if alert else "—",
            "review_note": "",
        }

    def recent(self, limit=100, alerts=False, status=None):
        q = "SELECT * FROM events"
        where = []
        args = []

        if alerts:
            where.append("is_alert=1")
        if status:
            where.append("review_status=?")
            args.append(status)

        if where:
            q += " WHERE " + " AND ".join(where)

        q += " ORDER BY received_at DESC LIMIT ?"
        args.append(min(max(limit, 1), 500))

        with self.con() as c:
            rows = c.execute(q, args).fetchall()

        out = []

        for r in rows:
            d = dict(r)
            try:
                d["features"] = json.loads(d.pop("features_json"))
            except Exception:
                d["features"] = {}

            for k in [
                "is_alert",
                "model_updated",
                "model_ready",
                "device_changed",
                "location_changed",
            ]:
                d[k] = bool(d[k])

            out.append(d)

        return out

    def review(self, eid, status, note):
        with self.con() as c:
            r = c.execute(
                "UPDATE events SET review_status=?,review_note=? "
                "WHERE event_id=?",
                (status, note, eid),
            )
            return r.rowcount > 0

    def clear(self, gm):
        with self.con() as c:
            c.execute("DELETE FROM events")

        self.history.clear()
        self.ref.clear()
        self.events_count = 0
        self.alerts_count = 0
        self.last = None

        self.model = Autoencoder()
        self.model.copy_from(gm)
        self.modelpath.unlink(missing_ok=True)


GLOBAL_PATH = MODEL_DIR / "global_model.npz"
META_PATH = RUNTIME / "federation.json"
SETTINGS_PATH = RUNTIME / "settings.json"

global_model = Autoencoder()
global_model.load(GLOBAL_PATH)

nodes = {
    k: BankNode(k, global_model)
    for k in BANKS
}

federation = {
    "version": 0,
    "rounds": [],
    "last_round_at": None,
}

if META_PATH.exists():
    try:
        federation.update(json.loads(META_PATH.read_text()))
    except Exception:
        pass

settings = {"threshold_multiplier": 1.0}

if SETTINGS_PATH.exists():
    try:
        settings.update(json.loads(SETTINGS_PATH.read_text()))
    except Exception:
        pass

stream = {
    "running": False,
    "task": None,
    "epoch": 0,
    "interval": 1.3,
}

# Prevent two bootstrap requests from running at once.
bootstrap_lock = threading.Lock()

app = FastAPI(
    title="FraudGuard Live",
    version="1.0",
    description=(
        "Unlabelled streaming anomaly detection and federated "
        "learning research prototype"
    ),
)

app.mount(
    "/static",
    StaticFiles(directory=ROOT / "static"),
    name="static",
)


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "local-demo"}


def gen_event(bid, profile="routine", idx=0, when=None):
    i = random.randrange(8)

    account = f"ACC_{bid[-1]}_{i:03d}"
    amount = round(
        (350 + i * 165) * random.uniform(0.84, 1.16),
        2,
    )
    cp = f"CP_{bid[-1]}_{i % 6:02d}"

    channel = "UPI"
    direction = "OUT"
    fails = 0
    device = False
    loc = False

    if bid == "BANK_B":
        channel = "ATM"

    if bid == "BANK_C":
        channel = "BANK_TRANSFER"
        direction = random.choice(["IN", "OUT"])

    if profile == "otp":
        amount = random.uniform(1500, 8000)
        cp = "NEW_PAYEE_" + uuid.uuid4().hex[:5]
        channel = "UPI"
        direction = "OUT"
        fails = 4
        device = True
        loc = True

    elif profile == "atm":
        amount = random.uniform(5500, 13000)
        channel = "ATM"
        direction = "OUT"
        loc = idx % 2 == 1
        fails = 2 if idx % 3 == 0 else 0
        cp = f"ATM_{random.randrange(1, 50):03d}"

    elif profile == "mule_in":
        amount = random.uniform(4500, 10500)
        direction = "IN"
        channel = "BANK_TRANSFER"
        cp = "SENDER_" + uuid.uuid4().hex[:5]

    elif profile == "mule_out":
        amount = random.uniform(3800, 10000)
        direction = "OUT"
        channel = "BANK_TRANSFER"
        cp = "NEW_DEST_" + uuid.uuid4().hex[:5]

    return {
        "bank_id": bid,
        "account_id": account,
        "counterparty_id": cp,
        "amount": round(amount, 2),
        "direction": direction,
        "channel": channel,
        "auth_failures": fails,
        "device_changed": device,
        "location_changed": loc,
        "status": "SUCCESS",
        "occurred_at": stamp(when or now()),
    }


def bootstrap_events(count=48, bank_id=None):
    if not 30 <= count <= 120:
        raise HTTPException(422, "count must be 30–120 per bank")

    ids = [bank_id] if bank_id else list(BANKS)
    total = 0

    for bid in ids:
        for i in range(count):
            ai = i % 8
            account = f"ACC_{bid[-1]}_{ai:03d}"
            cp = f"CP_{bid[-1]}_{ai % 6:02d}"
            occur = now() - timedelta(minutes=(count - i) * 5)
            base = 350 + ai * 165

            raw = {
                "bank_id": bid,
                "account_id": account,
                "counterparty_id": cp,
                "amount": round(base * random.uniform(0.85, 1.15), 2),
                "direction": (
                    "IN" if bid == "BANK_C" and i % 5 == 0 else "OUT"
                ),
                "channel": (
                    "ATM"
                    if bid == "BANK_B" and i % 4 == 0
                    else ("BANK_TRANSFER" if bid == "BANK_C" else "UPI")
                ),
                "auth_failures": 0,
                "device_changed": False,
                "location_changed": False,
                "status": "SUCCESS",
                "occurred_at": stamp(occur),
            }

            nodes[bid].process(
                raw,
                "unlabelled-bootstrap",
                True,
                settings["threshold_multiplier"],
            )
            total += 1

    return total


@app.post("/api/bootstrap")
def bootstrap(payload: dict | None = None):
    payload = payload or {}
    bid = payload.get("bank_id")

    if bid and bid not in BANKS:
        raise HTTPException(422, "Unknown bank_id")

    try:
        count = int(payload.get("count", 48))
    except (TypeError, ValueError):
        raise HTTPException(
            422,
            "count must be an integer between 30 and 120 per bank",
        )

    if not 30 <= count <= 120:
        raise HTTPException(422, "count must be 30–120 per bank")

    if not bootstrap_lock.acquire(blocking=False):
        raise HTTPException(
            409,
            "Initialization is already running. Please wait for it to finish.",
        )

    try:
        # Stop the live feed before initialization.
        # In-flight processing finishes under each bank's process lock.
        stream["running"] = False
        stream["epoch"] += 1

        try:
            n = bootstrap_events(count, bid)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Bank-node initialization failed")
            raise HTTPException(
                500,
                detail=(
                    f"Bank-node initialization failed "
                    f"({type(exc).__name__}): {exc}"
                ),
            ) from exc

        return {
            "ok": True,
            "events_added": n,
            "message": (
                "Synthetic unlabelled events processed from raw "
                "event fields; no fraud/genuine labels used."
            ),
        }
    finally:
        bootstrap_lock.release()


async def stream_loop(epoch):
    while stream["running"] and stream["epoch"] == epoch:
        bid = random.choice(list(BANKS))

        nodes[bid].process(
            gen_event(bid),
            "simulated-live-feed",
            False,
            settings["threshold_multiplier"],
        )

        await asyncio.sleep(stream["interval"])


@app.post("/api/stream/start")
async def start_stream():
    if not stream["running"]:
        stream["running"] = True
        stream["epoch"] += 1
        stream["task"] = asyncio.create_task(
            stream_loop(stream["epoch"])
        )

    return {"running": True}


@app.post("/api/stream/stop")
def stop_stream():
    stream["running"] = False
    stream["epoch"] += 1
    return {"running": False}


def authkey(x_api_key: Optional[str] = Header(default=None)):
    if not x_api_key or x_api_key != API_KEY:
        raise HTTPException(401, "Missing or invalid X-API-Key")


@app.post("/api/ingest", dependencies=[Depends(authkey)])
def ingest(p: Txn):
    return {
        "accepted": True,
        "event": nodes[p.bank_id].process(
            p.model_dump(),
            "external-ingest",
            False,
            settings["threshold_multiplier"],
        ),
        "note": "Anomaly score is not a fraud probability.",
    }


@app.post("/api/transaction")
def transaction(p: Txn):
    return {
        "accepted": True,
        "event": nodes[p.bank_id].process(
            p.model_dump(),
            "dashboard-manual-event",
            False,
            settings["threshold_multiplier"],
        ),
    }


@app.post("/api/scenario/{name}")
def scenario(name: str):
    if name not in ["otp", "atm", "mule"]:
        raise HTTPException(404, "Use otp, atm, or mule")

    bid = {
        "otp": "BANK_A",
        "atm": "BANK_B",
        "mule": "BANK_C",
    }[name]

    out = []
    t = now()

    if name == "otp":
        out.append(
            nodes[bid].process(
                gen_event(bid, "otp", when=t),
                "scenario-sequence",
                False,
                settings["threshold_multiplier"],
            )
        )

    elif name == "atm":
        for i in range(5):
            out.append(
                nodes[bid].process(
                    gen_event(bid, "atm", i, t + timedelta(seconds=i)),
                    "scenario-sequence",
                    False,
                    settings["threshold_multiplier"],
                )
            )

    else:
        for i in range(3):
            out.append(
                nodes[bid].process(
                    gen_event(
                        bid,
                        "mule_in",
                        i,
                        t + timedelta(seconds=i),
                    ),
                    "scenario-sequence",
                    False,
                    settings["threshold_multiplier"],
                )
            )

        for i in range(3):
            out.append(
                nodes[bid].process(
                    gen_event(
                        bid,
                        "mule_out",
                        i,
                        t + timedelta(seconds=3 + i),
                    ),
                    "scenario-sequence",
                    False,
                    settings["threshold_multiplier"],
                )
            )

    return {
        "ok": True,
        "scenario": name,
        "events_created": len(out),
        "events": out,
        "note": "Scenario labels are not model inputs.",
    }


@app.post("/api/federation/aggregate")
def aggregate():
    active = [
        n for n in nodes.values()
        if n.model.updates > 0
    ]

    if len(active) < 3:
        raise HTTPException(
            409,
            "Initialize all three bank nodes before aggregation",
        )

    sizes = np.array(
        [max(1, n.model.updates) for n in active],
        float,
    )
    sizes /= sizes.sum()

    averaged = {}

    for k in global_model.p:
        averaged[k] = sum(
            sizes[i] * n.model.p[k]
            for i, n in enumerate(active)
        )

    global_model.load_state(averaged)
    global_model.updates = sum(n.model.updates for n in active)
    federation["version"] += 1

    row = {
        "round": federation["version"],
        "timestamp": stamp(),
        "clients": len(active),
        "local_updates": {
            n.id: n.model.updates for n in active
        },
        "aggregation": (
            "Sample-weighted FedAvg over compatible model parameters"
        ),
        "raw_transactions_shared": False,
    }

    federation["rounds"].insert(0, row)
    federation["rounds"] = federation["rounds"][:50]
    federation["last_round_at"] = row["timestamp"]

    global_model.save(GLOBAL_PATH)

    for n in nodes.values():
        n.model.copy_from(global_model)
        n.model.save(n.modelpath)

    META_PATH.write_text(json.dumps(federation, indent=2))

    return {
        "ok": True,
        "round": row,
        "global_model_version": federation["version"],
    }


@app.post("/api/settings")
def set_settings(p: Settings):
    settings["threshold_multiplier"] = p.threshold_multiplier
    SETTINGS_PATH.write_text(json.dumps(settings))
    return {"settings": settings}


@app.post("/api/reviews/{eid}")
def review(eid: str, p: Review):
    for n in nodes.values():
        if n.review(eid, p.status, p.note):
            return {"ok": True, "status": p.status}

    raise HTTPException(404, "Event not found")


@app.post("/api/reset")
def reset():
    stream["running"] = False
    stream["epoch"] += 1

    global federation

    global_model.__init__(42)
    global_model.save(GLOBAL_PATH)

    federation = {
        "version": 0,
        "rounds": [],
        "last_round_at": None,
    }
    META_PATH.write_text(json.dumps(federation))

    for n in nodes.values():
        n.clear(global_model)

    return {
        "ok": True,
        "message": (
            "Events cleared and local models reset to fresh random weights."
        ),
    }


@app.get("/api/state")
def state():
    bankrows = []
    events = []
    alerts = []

    for bid, n in nodes.items():
        rs = n.recent(100)
        events += rs

        bankrows.append(
            {
                "bank_id": bid,
                "name": n.cfg["name"],
                "focus": n.cfg["focus"],
                "accent": n.cfg["accent"],
                "events_processed": n.events_count,
                "alerts": n.alerts_count,
                "model_updates": n.model.updates,
                "model_ready": n.model.updates >= WARMUP,
                "learning_status": (
                    "Monitoring"
                    if n.model.updates >= WARMUP
                    else "Learning baseline"
                ),
                "last_event_at": n.last,
                "local_store": (
                    f"runtime/banks/{bid.lower()}.sqlite3"
                ),
            }
        )

        alerts += n.recent(500, alerts=True)

    events.sort(
        key=lambda e: e["received_at"],
        reverse=True,
    )
    alerts.sort(
        key=lambda e: e["received_at"],
        reverse=True,
    )

    return {
        "mode": "local-demo",
        "stream": {
            "running": stream["running"],
            "interval_seconds": stream["interval"],
        },
        "banks": bankrows,
        "transactions_processed": sum(
            n.events_count for n in nodes.values()
        ),
        "alerts_total": sum(
            n.alerts_count for n in nodes.values()
        ),
        "unreviewed_alerts": sum(
            1 for e in alerts if e["review_status"] == "Open"
        ),
        "federation": {
            **federation,
            "global_model_updates": global_model.updates,
        },
        "settings": settings,
        "events": events[:100],
        "alerts": alerts[:50],
    }


@app.get("/api/export.csv")
def export_csv():
    rows = []

    for n in nodes.values():
        rows += n.recent(500)

    rows.sort(
        key=lambda e: e["received_at"],
        reverse=True,
    )

    buf = io.StringIO()

    cols = [
        "event_id",
        "received_at",
        "occurred_at",
        "bank_id",
        "account_id",
        "counterparty_id",
        "amount",
        "direction",
        "channel",
        "auth_failures",
        "device_changed",
        "location_changed",
        "status",
        "anomaly_score",
        "is_alert",
        "model_ready",
        "model_updated",
        "alert_reason",
    ] + FEATURES

    w = csv.DictWriter(
        buf,
        fieldnames=cols,
        extrasaction="ignore",
    )
    w.writeheader()

    for e in rows:
        w.writerow({**e, **e.get("features", {})})

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                "attachment; filename=fraudguard_events.csv"
            )
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )