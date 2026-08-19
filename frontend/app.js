/* =================================================================
 * Vox Orchestrator — browser client.
 *
 * Responsibilities:
 *   - keep a WebSocket open to the orchestrator
 *   - speak the agent's lines (TTS) and CANCEL them instantly on barge-in
 *   - capture the user's voice (hold-to-talk) or typed text
 *   - render the live state, plan, and event log
 *
 * Barge-in model: the instant the user starts talking over the agent we
 * kill local audio (no round-trip, so it feels immediate); the actual
 * re-plan is sent once we have the finished transcript.
 * ================================================================= */

const el = (id) => document.getElementById(id);
const ui = {
  connChip: el("conn-chip"), connText: el("conn-text"),
  engineChip: el("engine-chip"), engineText: el("engine-text"),
  signal: el("signal"), stateWord: el("state-word"), stateSub: el("state-sub"),
  sayLine: el("say-line"),
  micBtn: el("mic-btn"), micHint: el("mic-hint"),
  textInput: el("text-input"), sendBtn: el("send-btn"),
  quick: el("quick"),
  goal: el("goal"), plan: el("plan"),
  log: el("log"), clearLog: el("clear-log"),
  stage: document.querySelector(".stage"),
  backendMode: el("backend-mode"),
  scope: el("scope"),
};

const CALM_STATES = new Set(["listening", "planning", "executing", "idle"]);
const BUSY_STATES = new Set(["planning", "speaking", "executing", "replanning", "interrupted"]);
const SUBTITLES = {
  idle: "waiting for you",
  planning: "working out a plan",
  speaking: "responding",
  executing: "running a workflow",
  interrupted: "you cut in — standing down",
  replanning: "re-planning around you",
};

let ws = null;
let agentState = "idle";
let listening = false;
let spokeSinceUserStart = false;

/* ------------------------------------------------------------ WebSocket */
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1200); };
  ws.onerror = () => setConn(false);
  ws.onmessage = (e) => handleServer(JSON.parse(e.data));
}

function setConn(ok) {
  ui.connChip.classList.toggle("ok", ok);
  ui.connChip.classList.toggle("bad", !ok);
  ui.connText.textContent = ok ? "connected" : "reconnecting";
}

function send(type, text = "") {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type, text }));
  }
}

/* Send an instruction, choosing utterance vs barge-in by current state. */
function sendInstruction(text) {
  if (!text) return;
  if (BUSY_STATES.has(agentState)) {
    cancelSpeech();
    send("barge_in", text);
    flashInterrupt();
  } else {
    send("user_utterance", text);
  }
}

/* ------------------------------------------------------------ server events */
function handleServer(ev) {
  const d = ev.data || {};
  switch (ev.type) {
    case "state":
      setState(d.state);
      break;
    case "planned":
      ui.goal.textContent = d.goal || "—";
      renderPlan(d.steps, { fresh: true });
      logLine("planned", `plan ready — ${d.steps.length} step(s)`);
      break;
    case "replanned":
      ui.goal.textContent = d.goal || "—";
      renderPlan(d.steps, { fresh: true, replan: true });
      logLine("interrupt", `re-planned — ${d.steps.length} step(s)`);
      break;
    case "say":
      showSay(d.text);
      speak(d.text);
      logLine("calm", `“${d.text}”`);
      break;
    case "tool_start":
      markStep(d.id, "running");
      logLine("tool", `→ <b>${d.tool}</b>(${fmtArgs(d.args)})`);
      break;
    case "tool_end":
      markStep(d.id, endClass(d.status));
      logLine(d.status === "completed" ? "tool" : "interrupt",
        `✓ <b>${d.tool}</b> — ${d.status}`);
      break;
    case "interrupted":
      onInterrupted(d.heard);
      break;
    case "done":
      logLine("calm", "— turn complete —");
      break;
    case "error":
      logLine("interrupt", `error: ${d.message}`);
      break;
  }
}

function endClass(status) {
  if (status === "completed") return "done";
  if (status === "uncertain") return "abandoned";
  if (status === "cancelled") return "abandoned";
  return "done";
}

function onInterrupted(heard) {
  cancelSpeech();
  flashInterrupt();
  // Strike whatever step was live.
  const running = ui.plan.querySelector(".step.running");
  if (running) running.classList.add("abandoned");
  if (heard) logLine("interrupt", `↯ barge-in: “${heard}”`);
}

/* ------------------------------------------------------------ state */
function setState(state) {
  agentState = state;
  ui.stateWord.textContent = state;
  ui.stateSub.textContent = SUBTITLES[state] || "";
  const interrupt = (state === "interrupted" || state === "replanning");
  ui.signal.classList.toggle("interrupt", interrupt);
  if (state === "idle") clearSay();
}

/* ------------------------------------------------------------ plan render */
function renderPlan(steps, { fresh = false, replan = false } = {}) {
  if (fresh) ui.plan.innerHTML = "";
  steps.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "step enter";
    if (replan) li.classList.add("enter");
    const isComp = s.tool && (s.tool.startsWith("delete_") || s.tool.startsWith("remove_"));
    if (isComp) li.classList.add("compensate");
    li.dataset.stepIndex = i;

    const idx = document.createElement("span");
    idx.className = "idx";
    idx.textContent = String(i + 1).padStart(2, "0");

    const body = document.createElement("div");
    body.className = "body";
    if (s.say) {
      const say = document.createElement("div");
      say.className = "say"; say.textContent = s.say; body.appendChild(say);
    }
    if (s.tool) {
      const tool = document.createElement("div");
      tool.className = "tool"; tool.textContent = s.tool + "()"; body.appendChild(tool);
      if (s.args && Object.keys(s.args).length) {
        const args = document.createElement("div");
        args.className = "args"; args.textContent = fmtArgs(s.args); body.appendChild(args);
      }
    }

    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = s.tool ? (isComp ? "undo" : "action") : "say";

    li.append(idx, body, tag);
    ui.plan.appendChild(li);
  });
}

/* Mark the step currently tied to a tool id. We match by the tool name in
 * order since the backend sends a fresh id per call; simplest robust approach
 * is to advance the first not-yet-resolved matching step. */
function markStep(_id, cls) {
  const steps = [...ui.plan.querySelectorAll(".step")];
  if (cls === "running") {
    const next = steps.find((s) => s.querySelector(".tool") &&
      !s.classList.contains("done") && !s.classList.contains("running") &&
      !s.classList.contains("abandoned"));
    if (next) { next.classList.add("running", "active"); }
  } else {
    const running = ui.plan.querySelector(".step.running");
    if (running) {
      running.classList.remove("running", "active");
      running.classList.add(cls);
    }
  }
}

/* ------------------------------------------------------------ say line */
let sayTimer = null;
function showSay(text) {
  ui.sayLine.textContent = text;
  ui.sayLine.classList.add("show");
  ui.sayLine.classList.toggle("interrupt", agentState === "replanning");
  clearTimeout(sayTimer);
}
function clearSay() {
  sayTimer = setTimeout(() => ui.sayLine.classList.remove("show"), 400);
}

/* ------------------------------------------------------------ log */
function logLine(kind, html) {
  const line = document.createElement("div");
  line.className = `log-line ${kind}`;
  const t = new Date();
  const stamp = t.toTimeString().slice(0, 8) + "." +
    String(t.getMilliseconds()).padStart(3, "0");
  line.innerHTML = `<span class="t">${stamp}</span><span class="m">${html}</span>`;
  ui.log.appendChild(line);
  ui.log.scrollTop = ui.log.scrollHeight;
}
ui.clearLog.onclick = () => (ui.log.innerHTML = "");

function fmtArgs(args) {
  if (!args) return "";
  return Object.entries(args)
    .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");
}

function flashInterrupt() {
  ui.stage.classList.remove("flash");
  void ui.stage.offsetWidth; // reflow to restart animation
  ui.stage.classList.add("flash");
}

/* ------------------------------------------------------------ TTS */
let currentUtterance = null;
function speak(text) {
  if (!("speechSynthesis" in window)) return;
  cancelSpeech();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.05; u.pitch = 1.0;
  currentUtterance = u;
  window.speechSynthesis.speak(u);
}
function cancelSpeech() {
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  currentUtterance = null;
}

/* ------------------------------------------------------------ STT (hold-to-talk) */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog = null;
let finalTranscript = "";

function initSTT() {
  if (!SR) {
    ui.engineText.textContent = "unavailable";
    ui.micHint.textContent = "no speech recognition in this browser — use the console";
    ui.micBtn.disabled = true;
    ui.micBtn.style.opacity = ".5";
    ui.micBtn.style.cursor = "not-allowed";
    return;
  }
  ui.engineText.textContent = "ready";
  recog = new SR();
  recog.continuous = true;
  recog.interimResults = true;
  recog.lang = "en-US";

  recog.onstart = () => {
    listening = true;
    spokeSinceUserStart = false;
    finalTranscript = "";
    ui.micBtn.classList.add("live");
    // Pressing the mic while the agent talks IS the barge-in: cut audio now.
    if (BUSY_STATES.has(agentState)) { cancelSpeech(); }
  };
  recog.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i];
      if (r.isFinal) finalTranscript += r[0].transcript;
      else interim += r[0].transcript;
    }
    // First sign of speech over a talking agent → kill audio instantly.
    if (!spokeSinceUserStart && (interim || finalTranscript) &&
        BUSY_STATES.has(agentState)) {
      spokeSinceUserStart = true;
      cancelSpeech();
    }
    ui.stateSub.textContent = (interim || finalTranscript || "listening…");
  };
  recog.onerror = (e) => { logLine("interrupt", `stt error: ${e.error}`); };
  recog.onend = () => {
    listening = false;
    ui.micBtn.classList.remove("live");
    const text = finalTranscript.trim();
    if (text) sendInstruction(text);
  };
}

function startListen() { if (recog && !listening) { try { recog.start(); } catch (_) {} } }
function stopListen() { if (recog && listening) { try { recog.stop(); } catch (_) {} } }

/* Hold-to-talk bindings (pointer + touch + keyboard). */
ui.micBtn.addEventListener("pointerdown", (e) => { e.preventDefault(); startListen(); });
ui.micBtn.addEventListener("pointerup", () => stopListen());
ui.micBtn.addEventListener("pointerleave", () => { if (listening) stopListen(); });
ui.micBtn.addEventListener("keydown", (e) => {
  if ((e.code === "Space" || e.code === "Enter") && !e.repeat) { e.preventDefault(); startListen(); }
});
ui.micBtn.addEventListener("keyup", (e) => {
  if (e.code === "Space" || e.code === "Enter") { e.preventDefault(); stopListen(); }
});

/* ------------------------------------------------------------ text console */
function submitText() {
  const text = ui.textInput.value.trim();
  if (!text) return;
  sendInstruction(text);
  ui.textInput.value = "";
}
ui.sendBtn.onclick = submitText;
ui.textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") submitText(); });

ui.quick.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (btn.dataset.barge) {
    // Force a barge-in even if timing is tight.
    cancelSpeech();
    send("barge_in", btn.dataset.barge);
    flashInterrupt();
  } else if (btn.dataset.fill) {
    sendInstruction(btn.dataset.fill);
  }
});

/* ------------------------------------------------------------ scope viz */
function initScope() {
  const c = ui.scope, ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  function resize() {
    c.width = c.clientWidth * dpr; c.height = c.clientHeight * dpr;
    ctx.scale(dpr, dpr);
  }
  resize(); window.addEventListener("resize", () => { ctx.setTransform(1,0,0,1,0,0); resize(); });

  let phase = 0;
  const css = getComputedStyle(document.documentElement);
  const calm = css.getPropertyValue("--calm").trim() || "#35D0BA";
  const interrupt = css.getPropertyValue("--interrupt").trim() || "#FF8A5B";

  function frame() {
    const w = c.clientWidth, h = c.clientHeight;
    ctx.clearRect(0, 0, w, h);
    const mid = h / 2;
    const inInterrupt = (agentState === "interrupted" || agentState === "replanning");
    const color = inInterrupt ? interrupt : calm;

    // amplitude + frequency vary by state to read as a living signal
    let amp = 3, freq = 0.03, speed = 0.04;
    if (listening)               { amp = 22; freq = 0.05; speed = 0.18; }
    else if (agentState === "speaking")   { amp = 16; freq = 0.08; speed = 0.14; }
    else if (agentState === "executing")  { amp = 9;  freq = 0.12; speed = 0.10; }
    else if (agentState === "planning")   { amp = 7;  freq = 0.06; speed = 0.08; }
    else if (inInterrupt)        { amp = 26; freq = 0.14; speed = 0.30; }

    ctx.beginPath();
    for (let x = 0; x <= w; x += 2) {
      const env = Math.sin((x / w) * Math.PI); // taper at edges
      const jitter = inInterrupt ? (Math.random() - 0.5) * 6 : 0;
      const y = mid + Math.sin(x * freq + phase) * amp * env + jitter;
      x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.shadowColor = color; ctx.shadowBlur = 12;
    ctx.stroke();
    ctx.shadowBlur = 0;

    phase += speed;
    requestAnimationFrame(frame);
  }
  frame();
}

/* ------------------------------------------------------------ boot */
connect();
initSTT();
initScope();
fetch("/health").catch(() => {});
