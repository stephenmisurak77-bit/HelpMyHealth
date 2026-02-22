const chat = document.getElementById("chat");
const input = document.getElementById("input");
const sendBtn = document.getElementById("sendBtn");

const drawer = document.getElementById("drawer");
const backdrop = document.getElementById("backdrop");
const sourcesList = document.getElementById("sourcesList");
const toggleSourcesBtn = document.getElementById("toggleSourcesBtn");
const closeDrawerBtn = document.getElementById("closeDrawerBtn");
const emergencyBtn = document.getElementById("emergencyBtn");
const newChatBtn = document.getElementById("newChatBtn");

const safetyModal = document.getElementById("safetyModal");
const safetyAcceptBtn = document.getElementById("safetyAcceptBtn");
const safetyCancelBtn = document.getElementById("safetyCancelBtn");

function openSafetyModal() {
  safetyModal?.classList.add("open");
  safetyModal?.setAttribute("aria-hidden", "false");
}

function closeSafetyModal() {
  safetyModal?.classList.remove("open");
  safetyModal?.setAttribute("aria-hidden", "true");
}

function hasAcceptedSafety() {
  return localStorage.getItem("hmh_safety_ack") === "1";
}

function setAcceptedSafety() {
  localStorage.setItem("hmh_safety_ack", "1");
}

window.addEventListener("DOMContentLoaded", () => {
  if (!hasAcceptedSafety()) openSafetyModal();
});

safetyAcceptBtn?.addEventListener("click", () => {
  setAcceptedSafety();
  closeSafetyModal();
});

safetyCancelBtn?.addEventListener("click", () => {
  window.location.href = "about:blank";
});


let lastSources = [];

function openDrawer() {
  drawer.classList.add("open");
  backdrop.classList.add("open");
}

function closeDrawer() {
  drawer.classList.remove("open");
  backdrop.classList.remove("open");
}

toggleSourcesBtn.addEventListener("click", () => {
  if (drawer.classList.contains("open")) closeDrawer();
  else openDrawer();
});

closeDrawerBtn.addEventListener("click", closeDrawer);
backdrop.addEventListener("click", closeDrawer);

emergencyBtn.addEventListener("click", async () => {
  // 1) Try GPS location first (user permission)
  if (!navigator.geolocation) {
    alert("Location not supported. If this is an emergency, call your local emergency number now.");
    return;
  }

  emergencyBtn.disabled = true;
  emergencyBtn.textContent = "Detecting…";

  navigator.geolocation.getCurrentPosition(
    async (pos) => {
      try {
        const lat = pos.coords.latitude;
        const lon = pos.coords.longitude;

        // 2) Ask backend to reverse-geocode + map to number
        const res = await fetch(`/api/emergency?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        const where = data.country ? `${data.country} (${data.country_code || "?"})` : "your region";
        alert(`Emergency info for ${where}:\n\nCall: ${data.number}\n\n${data.note}`);
      } catch (e) {
        alert("Could not determine emergency number. If this is urgent, call your local emergency number now.");
      } finally {
        emergencyBtn.disabled = false;
        emergencyBtn.textContent = "Emergency";
      }
    },
    (err) => {
      // If user denies permission or it fails:
      emergencyBtn.disabled = false;
      emergencyBtn.textContent = "Emergency";

      if (err && err.code === 1) {
        alert("Location permission denied.\n\nIf this is an emergency, call your local emergency number now (often 911 or 112).");
      } else {
        alert("Could not get your location.\n\nIf this is an emergency, call your local emergency number now (often 911 or 112).");
      }
    },
    { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }
  );
});

newChatBtn?.addEventListener("click", () => {
  chat.innerHTML = "";
  lastSources = [];
  renderSources([]);
  seedAssistantMessage();
});

document.querySelectorAll(".chip").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const text = btn.dataset.template || "";
    if (!text) return;

    input.value = text;

    // instantly run the search/chat request
    await send();
  });
});

function addMessage(role, html) {
  const row = document.createElement("div");
  row.className = `msg-row ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = html;
  row.appendChild(bubble);
  chat.appendChild(row);
  chat.scrollTop = chat.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  }[c]));
}

function renderAssistantPayload(payload) {
  const triage = payload.triage; 
  const steps = payload.steps || [];
  const seek = payload.seekCareNow || [];
  const prevention = payload.prevention || [];

  const triageHtml = triage ? `
  <div class="card">
    <div class="triage">
      <div>
        <div class="kicker">Triage</div>
        <div><b>${escapeHtml(triage.headline || "")}</b></div>
        <div style="margin-top:6px;color:var(--muted);font-size:13px;">
          ${escapeHtml(triage.suggestedAction || "")}
        </div>
      </div>
      <div class="badge">${escapeHtml(triage.level || "")}</div>
    </div>

    ${triage.redFlags && triage.redFlags.length ? `
      <details style="margin-top:10px;">
        <summary style="cursor:pointer;font-weight:700;font-size:13px;">
          Urgent symptoms checklist
        </summary>
        <ul class="list">
          ${triage.redFlags.map(x => `<li>${escapeHtml(x)}</li>`).join("")}
        </ul>
      </details>
    ` : ""}
  </div>
` : "";

  const stepsHtml = steps.map((s, idx) => {
    const actions = (s.actions || []).map(a => `
      <div class="action">
        <input type="checkbox" />
        <div>${escapeHtml(a)}</div>
      </div>
    `).join("");

    return `
      <div class="step">
        <div class="step-title">Recommended actions to take</div>
        <div class="actions">${actions}</div>
        <div class="step-why"><b>Why:</b> ${escapeHtml(s.why || "")}</div>
      </div>
    `;
  }).join("");

  const preventionHtml = prevention.length ? `
  <div class="card">
    <div class="kicker">Prevention / avoid next time</div>
    <ul class="list">
      ${prevention.map(x => `<li>${escapeHtml(x)}</li>`).join("")}
    </ul>
  </div>
` : "";

  const seekHtml = seek.length ? `
    <div class="card">
      <div class="kicker">When to seek care now</div>
      <ul class="list">
        ${seek.map(x => `<li>${escapeHtml(x)}</li>`).join("")}
      </ul>
    </div>
  ` : "";

  const noteHtml = `
    <div class="card" style="color:var(--muted);font-size:12px;">
      <b>Note:</b> This is general health information, not a diagnosis. If symptoms are severe or worsening, seek professional medical care.
    </div>
  `;

  addMessage("assistant", triageHtml + stepsHtml + preventionHtml + seekHtml + noteHtml);
}

function relClass(rel) {
  const r = (rel || "").toLowerCase();
  if (r === "high") return "high";
  if (r === "moderate") return "moderate";
  return "low";
}

function sourceLinkLabel(url) {
  try {
    const host = new URL(url).hostname.replace("www.", "");

    if (host.includes("ncbi.nlm.nih.gov")) return "Open in PubMed";
    if (host.includes("medlineplus.gov")) return "Open in MedlinePlus";
    if (host.includes("nhs.uk")) return "Open on NHS";
    if (host.includes("cdc.gov")) return "Open on CDC";

    return `Open source (${host})`;
  } catch {
    return "Open source";
  }
}

function renderSources(sources) {
  lastSources = sources || [];
  if (!lastSources.length) {
    sourcesList.innerHTML = `<div class="empty">Ask a question to fetch trusted sources.</div>`;
    return;
  }

  sourcesList.innerHTML = lastSources.map(s => `
    <div class="source">
      <div class="source-top">
        <div class="source-title">${escapeHtml(s.title)}</div>
        <div class="rel ${relClass(s.reliability)}">${escapeHtml(s.reliability)}</div>
      </div>
      <div class="source-meta">
        ${escapeHtml(s.publisher)} • ${escapeHtml(s.type)} • ${escapeHtml(String(s.year))}${s.sample_size ? ` • n=${escapeHtml(String(s.sample_size))}` : ""}
      </div>
      <div class="source-why">${escapeHtml(s.rationale)}</div>
      <div style="margin-top:10px;">
        <a href="${s.url}" target="_blank" rel="noreferrer">${sourceLinkLabel(s.url)}</a>
      </div>
    </div>
  `).join("");
}

async function send() {
  const text = input.value.trim();
  if (!text) return;

  addMessage("user", escapeHtml(text));
  input.value = "";
  addMessage("assistant", `<span style="color:rgba(255,255,255,0.65);">Searching trusted sources…</span>`);

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text })
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Remove the "Searching..." message (last assistant bubble) and replace with real output
    chat.removeChild(chat.lastChild);

    renderAssistantPayload(data);
    renderSources(data.sources || []);
    // do NOT auto-open — user opens manually
  } catch (e) {
    chat.removeChild(chat.lastChild);
    addMessage("assistant", `<div class="card"><b>Sorry — something went wrong.</b><div style="margin-top:6px;color:rgba(255,255,255,0.72);font-size:13px;">Try again. If you have severe symptoms, seek medical care.</div></div>`);
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter") send();
});

function seedAssistantMessage() {
  addMessage("assistant", `
  <div class="card">
    <div class="kicker">Fix your health problems</div>
    <div><b>Search using trusted sources…</b></div>
    <div style="margin-top:6px;color:var(--muted);font-size:13px;">
      Pulling from NHS, MedlinePlus, and PubMed.
    </div>
  </div>
`);
  }

seedAssistantMessage();