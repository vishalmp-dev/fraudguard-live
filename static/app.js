const $ = id => document.getElementById(id);

let state = null;
let selected = null;
let currentTab = "overview";

const meta = {
    BANK_A: { name: "Bank A", letter: "A", accent: "" },
    BANK_B: { name: "Bank B", letter: "B", accent: "violet" },
    BANK_C: { name: "Bank C", letter: "C", accent: "teal" }
};

function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
    }[c]));
}

function money(n) {
    return "₹" + Number(n || 0).toLocaleString("en-IN", {
        maximumFractionDigits: 2
    });
}

function time(s) {
    if (!s) return "—";

    const d = new Date(s);

    return isNaN(d)
        ? "—"
        : d.toLocaleTimeString("en-IN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit"
        });
}

function toast(msg) {
    $("toast").textContent = msg;
    $("toast").classList.add("show");

    setTimeout(
        () => $("toast").classList.remove("show"),
        3000
    );
}

async function api(path, opts = {}) {
    const r = await fetch(path, {
        ...opts,
        headers: {
            "Content-Type": "application/json",
            ...(opts.headers || {})
        }
    });

    let d = {};

    try {
        d = await r.json();
    } catch {
        // The response may not contain JSON.
    }

    if (!r.ok) {
        throw Error(
            d.detail || `Request failed: ${r.status}`
        );
    }

    return d;
}

function bankSymbol(id) {
    const b = meta[id] || meta.BANK_A;

    return `<span class="bankletter ${b.accent}">${b.letter}</span>`;
}

function statePill(e) {
    if (!e.model_ready) {
        return '<span class="state neutral">Learning</span>';
    }

    return e.is_alert
        ? '<span class="state warn">Anomaly</span>'
        : '<span class="state good">Within baseline</span>';
}

function score(e) {
    if (
        e.anomaly_score === null ||
        e.anomaly_score === undefined
    ) {
        return '<span class="state neutral">Warming up</span>';
    }

    const n = Number(e.anomaly_score);
    const c = e.is_alert ? "danger" : n > 40 ? "alert" : "";

    return `
        <b>${n.toFixed(0)}<small style="display:inline"> / 100</small></b>
        <div class="scorebar">
            <span class="${c}" style="width:${Math.max(2, n)}%"></span>
        </div>
    `;
}

function signalChips(e) {
    const f = e.features || {};

    return `
        <div style="display:flex;gap:3px;flex-wrap:wrap">
            ${
                [
                    ["V", f.velocity_score],
                    ["B", f.behavior_deviation],
                    ["S", f.security_context_score],
                    ["F", f.fund_flow_risk]
                ].map(([k, v]) => `
                    <span
                        title="${k}: ${v}"
                        style="background:#f1f4f9;padding:3px 4px;border-radius:4px;font:8px monospace"
                    >
                        ${k}:${v === undefined ? "—" : Number(v).toFixed(1)}
                    </span>
                `).join("")
            }
        </div>
    `;
}

function rowOverview(e) {
    return `
        <tr class="click" data-event="${esc(e.event_id)}">
            <td>
                <b>${esc(e.event_id.slice(0, 8))}…</b>
                <small>
                    ${esc(meta[e.bank_id]?.name || e.bank_id)}
                    · ${esc(e.channel)}
                </small>
            </td>
            <td>
                <b>${esc(e.account_id)}</b>
                <small>Counterparty ${esc(e.counterparty_id)}</small>
            </td>
            <td>
                <b>${money(e.amount)}</b>
                <small>${esc(e.direction)}</small>
            </td>
            <td>${score(e)}</td>
            <td>${statePill(e)}</td>
        </tr>
    `;
}

function rowEvent(e) {
    return `
        <tr class="click" data-event="${esc(e.event_id)}">
            <td>
                <b>${time(e.occurred_at)}</b>
                <small>${esc(meta[e.bank_id]?.name || e.bank_id)}</small>
            </td>
            <td>
                <b>${esc(e.account_id)}</b>
                <small>${esc(e.counterparty_id)}</small>
            </td>
            <td>
                <b>${money(e.amount)}</b>
                <small>${esc(e.direction)} · ${esc(e.channel)}</small>
            </td>
            <td>${signalChips(e)}</td>
            <td>${score(e)}</td>
            <td>${statePill(e)}</td>
        </tr>
    `;
}

function render() {
    if (!state) return;

    const banks = state.banks || [];
    const events = state.events || [];
    const alerts = state.alerts || [];
    const f = state.federation || {};

    $("mEvents").textContent =
        Number(state.transactions_processed).toLocaleString("en-IN");

    $("mAlerts").textContent =
        Number(state.alerts_total).toLocaleString("en-IN");

    $("mReady").textContent =
        banks.filter(b => b.model_ready).length;

    $("mVersion").textContent = "v" + (f.version || 0);

    $("mRound").textContent = f.last_round_at
        ? "Last round " + time(f.last_round_at)
        : "No aggregation yet";

    $("globalName").textContent = "Global model v" + (f.version || 0);

    $("globalInfo").textContent = f.last_round_at
        ? "Updated " + time(f.last_round_at)
        : "Waiting for first aggregation";

    $("status").textContent = state.stream.running
        ? "Live event feed active"
        : "System ready";

    $("led").style.background = state.stream.running
        ? "#35b991"
        : "#9ba8b9";

    $("stream").textContent = state.stream.running
        ? "Ⅱ Pause live feed"
        : "▶ Start live feed";

    document.querySelector('[data-action="stream"]').textContent =
        state.stream.running
            ? "Ⅱ Pause live feed"
            : "▶ Start live feed";

    $("sourceHealth").textContent = state.stream.running
        ? "Simulator running"
        : "Simulator stopped";

    $("apiHealth").textContent = "Connected";

    $("overviewRows").innerHTML = events.length
        ? events.slice(0, 8).map(rowOverview).join("")
        : '<tr><td colspan="5" class="empty">No events yet. Initialize the nodes to start.</td></tr>';

    const filtered = events.filter(e =>
        (
            $("bankFilter").value === "all" ||
            e.bank_id === $("bankFilter").value
        ) &&
        (
            $("stateFilter").value === "all" ||
            (
                $("stateFilter").value === "alerts" &&
                e.is_alert
            ) ||
            (
                $("stateFilter").value === "normal" &&
                !e.is_alert &&
                e.model_ready
            ) ||
            (
                $("stateFilter").value === "learning" &&
                !e.model_ready
            )
        )
    );

    $("eventRows").innerHTML = filtered.length
        ? filtered.slice(0, 100).map(rowEvent).join("")
        : '<tr><td colspan="6" class="empty">No events match this filter.</td></tr>';

    $("eventcount").textContent =
        filtered.length + " recent events shown";

    $("fedNodes").innerHTML = banks.map(b => `
        <div class="fednode ${b.accent}">
            <b>${b.bank_id.slice(-1)}</b>
            ${esc(b.name)}
            <small>${b.model_updates} updates</small>
        </div>
    `).join("");

    $("overviewBanks").innerHTML = banks.map(b => `
        <div class="bankcard">
            <div class="bankcardhead">
                <span class="bankletter ${b.accent}">
                    ${b.bank_id.slice(-1)}
                </span>
                <div>
                    <b>${esc(b.name)}</b>
                    <small>${esc(b.focus)}</small>
                </div>
                <em>${b.model_ready ? "Monitoring" : "Learning"}</em>
            </div>
            <p>
                ${
                    b.model_ready
                        ? "Online model is scoring new events."
                        : "Building unlabelled baseline."
                }
            </p>
            <div class="bar">
                <span style="width:${
                    b.model_ready
                        ? 100
                        : Math.min(100, b.model_updates / 30 * 100)
                }%"></span>
            </div>
            <div class="bankstats">
                <span>${b.events_processed} events</span>
                <span>${b.alerts} alerts</span>
            </div>
        </div>
    `).join("");

    $("bankDetails").innerHTML = banks.map(b => `
        <article class="bankdetail">
            <div class="bankcardhead">
                ${bankSymbol(b.bank_id)}
                <div>
                    <b>${esc(b.name)}</b>
                    <small>${esc(b.focus)}</small>
                </div>
                <em>${b.model_ready ? "Monitoring" : "Learning"}</em>
            </div>

            <h2>${Number(b.events_processed).toLocaleString()} events</h2>

            <p>
                Local model updates: <b>${b.model_updates}</b><br>
                Alerts: <b>${b.alerts}</b><br>
                State: <b>${b.model_ready ? "Ready" : "Warm-up"}</b>
            </p>

            <div class="bar">
                <span style="width:${
                    b.model_ready
                        ? 100
                        : Math.min(100, b.model_updates / 30 * 100)
                }%"></span>
            </div>

            <small style="color:#91a0b3">
                ${esc(b.local_store)}
            </small>
        </article>
    `).join("");

    $("fVersion").textContent = "v" + (f.version || 0);

    $("fClients").textContent =
        banks.filter(b => b.model_updates > 0).length + " / 3";

    $("fUpdates").textContent =
        banks.reduce((n, b) => n + b.model_updates, 0);

    $("progress").innerHTML = banks.map(b => `
        <div class="progressrow">
            <div>
                <b>${esc(b.name)}</b>
                <small>${esc(b.focus)}</small>
            </div>

            <div class="bar">
                <span style="width:${
                    b.model_ready
                        ? 100
                        : Math.min(100, b.model_updates / 30 * 100)
                }%"></span>
            </div>

            <div class="right">
                <b>${b.model_updates}</b> updates
                <small>
                    ${b.model_ready ? "Monitoring" : "Learning baseline"}
                </small>
            </div>
        </div>
    `).join("");

    $("rounds").innerHTML = (f.rounds || []).length
        ? f.rounds.map(r => `
            <tr>
                <td><b>Round ${r.round}</b></td>
                <td>${time(r.timestamp)}</td>
                <td><span class="badge green">${r.clients}/3</span></td>
                <td>${Object.values(r.local_updates).reduce((a, b) => a + b, 0)}</td>
                <td>No</td>
                <td>${esc(r.aggregation)}</td>
            </tr>
        `).join("")
        : '<tr><td colspan="6" class="empty">No aggregation rounds yet.</td></tr>';

    $("alertCount").textContent =
        alerts.length + " alerts in current history";

    const rf = $("reviewFilter").value;

    const ar = alerts.filter(
        a => rf === "all" || a.review_status === rf
    );

    $("alertRows").innerHTML = ar.length
        ? ar.map(e => `
            <tr class="click" data-alert="${esc(e.event_id)}">
                <td>
                    <b>${esc(e.event_id.slice(0, 8))}…</b>
                    <small>${time(e.occurred_at)}</small>
                </td>
                <td>${esc(meta[e.bank_id]?.name || e.bank_id)}</td>
                <td>${money(e.amount)}</td>
                <td>
                    <span class="badge amber">
                        ${
                            e.anomaly_score === null
                                ? "—"
                                : Number(e.anomaly_score).toFixed(0) + "/100"
                        }
                    </span>
                </td>
                <td>${esc(e.review_status)}</td>
            </tr>
        `).join("")
        : '<tr><td colspan="5" class="empty">No alerts in this filter.</td></tr>';

    $("sens").value = state.settings.threshold_multiplier;

    $("sensVal").textContent =
        Number(state.settings.threshold_multiplier).toFixed(1) + "×";

    document.querySelectorAll("[data-event]").forEach(tr => {
        tr.onclick = () => showDetail(tr.dataset.event);
    });

    document.querySelectorAll("[data-alert]").forEach(tr => {
        tr.onclick = () => showDetail(tr.dataset.alert);
    });
}

function showDetail(id) {
    selected = id;

    const e = [
        ...(state?.events || []),
        ...(state?.alerts || [])
    ].find(x => x.event_id === id);

    if (!e) return;

    const f = e.features || {};

    $("detail").innerHTML = `
        <div class="detailhead">
            <b>${esc(meta[e.bank_id]?.name || e.bank_id)} · Event details</b>
            <small>${esc(e.event_id)} · ${time(e.occurred_at)}</small>
        </div>

        <div class="scorebox">
            <div class="scorecircle">
                ${
                    e.anomaly_score === null
                        ? "—"
                        : Number(e.anomaly_score).toFixed(0)
                }
            </div>
            <div>
                <b>
                    ${
                        e.is_alert
                            ? "Anomaly threshold exceeded"
                            : e.model_ready
                                ? "Within learned range"
                                : "Model warming up"
                    }
                </b>
                <p>${esc(e.alert_reason)}. Score is not a fraud probability.</p>
            </div>
        </div>

        <div class="features">
            ${
                [
                    ["velocity_score", "Transaction velocity"],
                    ["behavior_deviation", "Behaviour deviation"],
                    ["security_context_score", "Security context"],
                    ["fund_flow_risk", "Recipient / fund-flow risk"]
                ].map(([k, label]) => `
                    <div>
                        <div class="featuretitle">
                            <span>${label}</span>
                            <b>${Number(f[k] || 0).toFixed(2)}</b>
                        </div>
                        <div class="bar">
                            <span style="width:${Number(f[k] || 0) * 100}%"></span>
                        </div>
                    </div>
                `).join("")
            }

            <p class="hint">
                Account: ${esc(e.account_id)}<br>
                Counterparty: ${esc(e.counterparty_id)}<br>
                ${money(e.amount)} · ${esc(e.direction)} · ${esc(e.channel)}<br>
                Auth failures: ${e.auth_failures};
                device changed: ${e.device_changed};
                location changed: ${e.location_changed}
            </p>
        </div>

        <form class="reviewform" id="reviewForm">
            <label>
                Review status
                <select id="reviewStatus">
                    <option>Open</option>
                    <option>Reviewed</option>
                    <option>Escalated</option>
                    <option>Closed - no action</option>
                </select>
            </label>

            <label>
                Analyst note
                <textarea
                    id="reviewNote"
                    maxlength="500"
                    placeholder="What did you observe?"
                >${esc(e.review_note || "")}</textarea>
            </label>

            <button class="primary">Save review</button>
        </form>
    `;

    $("reviewStatus").value =
        e.review_status === "—" ? "Open" : e.review_status;

    $("reviewForm").onsubmit = async ev => {
        ev.preventDefault();

        try {
            await api(
                "/api/reviews/" + encodeURIComponent(id),
                {
                    method: "POST",
                    body: JSON.stringify({
                        status: $("reviewStatus").value,
                        note: $("reviewNote").value
                    })
                }
            );

            toast("Review saved");

            await refresh();

            if (selected) {
                showDetail(selected);
            }
        } catch (err) {
            toast(err.message);
        }
    };
}

async function refresh() {
    try {
        state = await api("/api/state");
        render();
    } catch (e) {
        $("status").textContent = "API disconnected";
        $("apiHealth").textContent = "Disconnected";
        $("led").style.background = "#be4656";
    }
}

async function act(path, body = {}, success = "Done") {
    try {
        const d = await api(path, {
            method: "POST",
            body: JSON.stringify(body)
        });

        toast(success);
        await refresh();

        return d;
    } catch (e) {
        toast(e.message);
        throw e;
    }
}

async function toggleStream() {
    if (state?.stream?.running) {
        await act("/api/stream/stop", {}, "Live feed paused");
    } else {
        await act("/api/stream/start", {}, "Live feed started");
    }
}

// FIX: prevent overlapping initializations and disable the buttons
// while bootstrap is running.
async function bootstrap() {
    const buttons = [
        ...new Set([
            $("bootstrap"),
            $("bootstrap2"),
            document.querySelector('[data-action="bootstrap"]')
        ].filter(Boolean))
    ];

    if (buttons.some(b => b.disabled)) {
        return;
    }

    const previous = buttons.map(b => ({
        b,
        disabled: b.disabled,
        text: b.textContent
    }));

    buttons.forEach(({ b }) => {
        b.disabled = true;
        b.setAttribute("aria-busy", "true");
        b.textContent = "Initializing…";
    });

    try {
        await act(
            "/api/bootstrap",
            { count: 48 },
            "Processed 144 unlabelled bootstrap events across three banks"
        );
    } catch (err) {
        // act() already displays the error toast.
    } finally {
        previous.forEach(({ b, disabled, text }) => {
            b.disabled = disabled;
            b.removeAttribute("aria-busy");
            b.textContent = text;
        });
    }
}

async function scenario(name) {
    try {
        const d = await api("/api/scenario/" + name, {
            method: "POST",
            body: "{}"
        });

        toast(
            `${d.events_created} synthetic ${name.toUpperCase()} events processed`
        );

        await refresh();
        tab("monitor");
    } catch (e) {
        toast(e.message);
    }
}

function tab(name) {
    currentTab = name;

    document.querySelectorAll(".tab").forEach(x => {
        x.classList.toggle("show", x.id === name);
    });

    document.querySelectorAll(".nav").forEach(x => {
        x.classList.toggle("active", x.dataset.tab === name);
    });

    $("crumb").textContent = ({
        overview: "Overview",
        monitor: "Live transactions",
        banks: "Bank nodes",
        federated: "Federated learning",
        investigations: "Investigations",
        settings: "Settings & integration"
    })[name];

    window.scrollTo({
        top: 0,
        behavior: "smooth"
    });
}

document.querySelectorAll(".nav").forEach(b => {
    b.onclick = () => tab(b.dataset.tab);
});

document.querySelectorAll("[data-goto]").forEach(b => {
    b.onclick = () => tab(b.dataset.goto);
});

$("stream").onclick = toggleStream;

document.querySelector('[data-action="stream"]').onclick =
    toggleStream;

$("bootstrap").onclick = bootstrap;
$("bootstrap2").onclick = bootstrap;

document.querySelector('[data-action="bootstrap"]').onclick =
    bootstrap;

$("aggregate").onclick = async () => {
    try {
        await act(
            "/api/federation/aggregate",
            {},
            "Federated round " +
                (state?.federation?.version + 1 || 1) +
                " completed"
        );
    } catch (e) {
        // The error is displayed by act().
    }
};

$("txnForm").onsubmit = async e => {
    e.preventDefault();

    const p = {
        bank_id: $("bank").value,
        account_id: $("account").value.trim(),
        counterparty_id: $("counterparty").value.trim(),
        amount: Number($("amount").value),
        direction: $("direction").value,
        channel: $("channel").value,
        auth_failures: Number($("failures").value || 0),
        device_changed: $("device").checked,
        location_changed: $("location").checked,
        status: "SUCCESS"
    };

    try {
        const d = await api("/api/transaction", {
            method: "POST",
            body: JSON.stringify(p)
        });

        toast(
            d.event.is_alert
                ? "Event processed; anomaly alert raised."
                : "Event processed by local model."
        );

        await refresh();
    } catch (err) {
        toast(err.message);
    }
};

$("bank").onchange = () => {
    const s = $("bank").value.slice(-1);

    $("account").value = "ACC_" + s + "_003";
    $("counterparty").value = "CP_" + s + "_03";

    $("channel").value =
        s === "B"
            ? "ATM"
            : s === "C"
                ? "BANK_TRANSFER"
                : "UPI";
};

document.querySelectorAll("[data-scenario]").forEach(b => {
    b.onclick = () => scenario(b.dataset.scenario);
});

$("bankFilter").onchange = render;
$("stateFilter").onchange = render;
$("reviewFilter").onchange = render;

$("saveSens").onclick = async () => {
    try {
        await act(
            "/api/settings",
            {
                threshold_multiplier: Number($("sens").value)
            },
            "Sensitivity updated"
        );
    } catch (e) {
        // Error displayed by act().
    }
};

$("sens").oninput = () => {
    $("sensVal").textContent =
        Number($("sens").value).toFixed(1) + "×";
};

$("export").onclick = () => {
    window.location.href = "/api/export.csv";
};

$("reset").onclick = () => {
    $("modal").classList.remove("hidden");
};

$("cancelReset").onclick = () => {
    $("modal").classList.add("hidden");
};

$("confirmReset").onclick = async () => {
    try {
        await act(
            "/api/reset",
            {},
            "Demo reset; local models freshly initialized"
        );

        selected = null;
        $("modal").classList.add("hidden");
    } catch (e) {
        // Error displayed by act().
    }
};

$("copyCurl").onclick = async () => {
    try {
        await navigator.clipboard.writeText(
            $("curl").textContent
        );

        toast("Example copied");
    } catch (e) {
        toast("Clipboard not available; select and copy the command");
    }
};

refresh();
setInterval(refresh, 1600);