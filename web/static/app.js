// nightops console. All text is inserted with textContent (never innerHTML),
// so nothing from a job posting or GitHub can run as code in your browser.
(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf"]').content;
  const pollMs = parseInt(document.querySelector('meta[name="poll"]').content, 10) * 1000;
  const out = document.getElementById("out");
  const feed = document.getElementById("feed");
  const input = document.getElementById("cmd");
  const badge = document.getElementById("badge");
  let lastId = 0, ready = false, sound = false, audio = null;
  const history = []; let hpos = 0;

  function addLine(box, text, cls) {
    const div = document.createElement("div");
    if (cls) div.className = cls;
    for (const part of String(text).split(/(https:\/\/[^\s]+)/g)) {
      if (/^https:\/\/[^\s]+$/.test(part)) {
        const a = document.createElement("a");
        a.href = part; a.textContent = part; a.target = "_blank"; a.rel = "noopener noreferrer";
        div.appendChild(a);
      } else if (part) {
        div.appendChild(document.createTextNode(part));
      }
    }
    box.appendChild(div);
    while (box.childNodes.length > 800) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }

  function beep() {
    if (!audio) return;
    const o = audio.createOscillator(), g = audio.createGain();
    o.type = "square"; o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g); g.connect(audio.destination);
    o.start(); setTimeout(() => { o.frequency.value = 660; }, 180); setTimeout(() => o.stop(), 380);
  }

  function alarm(e) {
    if (sound) beep();
    if (window.Notification && Notification.permission === "granted") {
      try { new Notification("nightops alarm", { body: e.title }); } catch (_) {}
    }
    document.title = "(!) nightops";
  }

  async function poll() {
    try {
      const r = await fetch("/api/events?after=" + lastId, { credentials: "same-origin" });
      if (r.status === 401) { location.href = "/login"; return; }
      const d = await r.json();
      for (const e of d.events) {
        lastId = Math.max(lastId, e.id);
        addLine(feed, "#" + e.id + " " + e.created.slice(5, 16).replace("T", " ") + " [" +
                e.level.toUpperCase() + "] " + e.title + (e.url ? " " + e.url : ""),
                "ev-" + e.level + (e.acked ? " acked" : ""));
        if (ready && e.level === "alarm" && !e.acked) alarm(e);
      }
      badge.textContent = d.unacked_alarms;
      badge.className = d.unacked_alarms > 0 ? "hot" : "";
      if (d.unacked_alarms === 0) document.title = "nightops";
      ready = true;
    } catch (_) { /* network blip: try again next tick */ }
    setTimeout(poll, pollMs);
  }

  async function run(line) {
    addLine(out, "nightops> " + line, "echo");
    if (line === "clear") { out.textContent = ""; return; }
    try {
      const r = await fetch("/api/cmd", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
        body: JSON.stringify({ cmd: line })
      });
      if (r.status === 401) { location.href = "/login"; return; }
      const d = await r.json();
      for (const l of String(d.output || d.error || "").split("\n")) addLine(out, l);
    } catch (_) { addLine(out, "request failed", "ev-warn"); }
  }

  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const line = input.value.trim(); input.value = "";
      if (line) { history.push(line); hpos = history.length; run(line); }
    } else if (ev.key === "ArrowUp" && hpos > 0) {
      hpos--; input.value = history[hpos]; ev.preventDefault();
    } else if (ev.key === "ArrowDown") {
      hpos = Math.min(history.length, hpos + 1); input.value = history[hpos] || ""; ev.preventDefault();
    }
  });

  document.getElementById("sound").addEventListener("click", (ev) => {
    sound = true;
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    beep();
    if (window.Notification && Notification.permission === "default") Notification.requestPermission();
    ev.target.textContent = "alarm sound on";
    ev.target.disabled = true;
  });

  document.getElementById("logout").addEventListener("click", async () => {
    await fetch("/logout", { method: "POST", credentials: "same-origin",
                             headers: { "X-CSRF-Token": csrf } });
    location.href = "/login";
  });

  // Auto-logout after inactivity is enforced by the server; this just tidies the UI.
  addLine(out, "type 'help' for commands. approvals need: approve <id> <2FA code>");
  poll();
})();
