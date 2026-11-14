/* Mining Mini App — talks to the Flask API in bot.py */
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { tg.ready(); tg.expand(); }

const initData = tg ? tg.initData : "";
let state = null;

const $ = (id) => document.getElementById(id);

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  setTimeout(() => t.classList.add("hidden"), 2500);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({ initData }, body || {})),
  });
  return res.json();
}

function levelName(lvl) {
  if (lvl < 10) return "Apprentice";
  if (lvl < 25) return "Miner";
  if (lvl < 50) return "Expert";
  if (lvl < 75) return "Master";
  return "Legend";
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return h + "h " + String(m).padStart(2, "0") + "m";
}

function renderLevelPath(lvl) {
  const start = Math.max(1, Math.min(lvl - 2, 96));
  const wrap = $("levelPath");
  wrap.innerHTML = "";
  for (let i = start; i < start + 5 && i <= 100; i++) {
    const d = document.createElement("div");
    d.className = "level-node" + (i < lvl ? " done" : i === lvl ? " current" : " locked");
    d.textContent = i < lvl ? "✓" : i;
    wrap.appendChild(d);
  }
}

function render(s) {
  state = s;
  document.querySelectorAll(".sym").forEach((el) => (el.textContent = s.token_symbol));
  $("balance").textContent = (s.balance + s.pending).toFixed(4);
  $("hashrate").textContent = s.hashrate.toFixed(2) + " TH/s";
  $("timerText").textContent = s.mining_on ? fmtTime(s.timer_left) : "24h 00m";
  $("timerBar").style.width = (s.timer_progress * 100).toFixed(1) + "%";
  $("levelNum").textContent = s.level;
  $("levelName").textContent = levelName(s.level);
  $("levelBar").style.width = (s.level_progress * 100).toFixed(1) + "%";
  $("totalEarned").textContent = s.total_earned.toFixed(2);
  $("minerBalance").textContent = s.balance.toFixed(2);
  $("walletBalance").textContent = s.balance.toFixed(4);
  $("walletTotal").textContent = s.total_earned.toFixed(2);
  $("walletHash").textContent = s.hashrate.toFixed(2) + " TH/s";
  $("walletDaily").textContent = s.free_daily;
  renderLevelPath(s.level);

  const on = !!s.mining_on;
  $("startBtn").classList.toggle("hidden", on);
  $("collectBtn").disabled = !on || s.pending <= 0;
  $("collectBtn").textContent = on
    ? "⛏️ Collect earnings (" + s.pending.toFixed(4) + " " + s.token_symbol + ")"
    : "⛏️ Collect earnings";
  $("miningStatus").textContent = on
    ? "⛏️ Mining is ON — tokens are accruing"
    : "⏸️ Mining is OFF — press Start Mining";

  // force join
  const fj = $("forceJoin");
  if (s.force_channel && !s.joined) {
    fj.classList.remove("hidden");
    $("joinBtn").href = "https://t.me/" + s.force_channel;
  } else {
    fj.classList.add("hidden");
  }
}

async function refresh() {
  try {
    const s = await api("/api/state");
    if (s.ok) render(s);
    else if (s.error) console.warn(s.error);
  } catch (e) { console.warn(e); }
}

async function loadTasks() {
  const r = await api("/api/tasks");
  const list = $("taskList");
  list.innerHTML = "";
  if (!r.ok) { list.innerHTML = "<p class='sub'>Could not load tasks.</p>"; return; }
  r.tasks.forEach((t) => {
    const div = document.createElement("div");
    div.className = "list-item";
    div.innerHTML = "<div><div class='name'>" + t.name + "</div>" +
      "<div class='desc'>Reward: " + t.reward + " MINE</div></div>";
    const btn = document.createElement("button");
    btn.textContent = t.done ? "✓ Done" : t.available ? "Claim" : "Locked";
    btn.disabled = !t.available;
    btn.onclick = async () => {
      const res = await api("/api/claim_task", { task_id: t.id });
      if (res.ok) { toast("+" + res.reward + " MINE"); refresh(); loadTasks(); }
      else toast(res.error || "Failed");
    };
    div.appendChild(btn);
    list.appendChild(div);
  });
}

async function loadMiners() {
  const r = await api("/api/miners");
  const list = $("minerList");
  list.innerHTML = "";
  if (!r.ok) { list.innerHTML = "<p class='sub'>Could not load miners.</p>"; return; }
  $("minerBalance").textContent = r.balance.toFixed(2);
  r.miners.forEach((m) => {
    const owned = r.owned[m.id] || 0;
    const div = document.createElement("div");
    div.className = "list-item";
    div.innerHTML = "<div><div class='name'>" + m.name + " ×" + owned + "</div>" +
      "<div class='desc'>+" + m.bonus + " TH/s · Price: " + m.price + " MINE</div></div>";
    const btn = document.createElement("button");
    btn.textContent = "Buy";
    btn.disabled = r.balance < m.price;
    btn.onclick = async () => {
      const res = await api("/api/buy_miner", { miner_id: m.id });
      if (res.ok) { toast("Miner bought! ⚡"); render(res); loadMiners(); }
      else toast(res.error || "Failed");
    };
    div.appendChild(btn);
    list.appendChild(div);
  });
}

async function loadProfile() {
  const r = await api("/api/profile");
  if (!r.ok) return;
  $("pName").textContent = r.name || "—";
  $("pUser").textContent = r.username ? "@" + r.username : "—";
  $("pId").textContent = r.user_id;
  $("pLevel").textContent = r.level + " · " + levelName(r.level);
  $("pMining").textContent = r.mining_on ? "ON ⛏️" : "OFF";
}

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  $("view-" + name).classList.add("active");
  document.querySelectorAll(".bottom-nav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  if (name === "tasks") loadTasks();
  if (name === "miners") loadMiners();
  if (name === "profile") loadProfile();
  if (name === "wallet" || name === "mine") refresh();
}

document.querySelectorAll(".bottom-nav button").forEach((b) =>
  b.addEventListener("click", () => showView(b.dataset.view)));

$("collectBtn").addEventListener("click", async () => {
  const res = await api("/api/collect");
  if (res.ok) { toast("Collected +" + (res.collected || 0).toFixed(4) + " " + res.token_symbol); render(res); }
  else toast(res.error || "Failed");
});

$("startBtn").addEventListener("click", async () => {
  const res = await api("/api/start_mining");
  if (res.ok) { toast("Mining started! ⛏️"); render(res); }
  else toast(res.error || "Failed");
});

$("joinedCheckBtn").addEventListener("click", async () => {
  await refresh();
  if (state && state.joined) toast("Welcome! Mining unlocked ✅");
  else toast("Still not joined — join the channel first");
});

refresh();
setInterval(refresh, 5000);
