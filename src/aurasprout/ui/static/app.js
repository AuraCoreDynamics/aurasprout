(function () {
  "use strict";

  var csrfToken = "";

  function setStatus(text, ok) {
    var el = document.getElementById("auth-status");
    if (!el) return;
    el.textContent = text;
    el.style.color = ok ? "#34d399" : "#fbbf24";
  }

  async function exchangeTokenIfPresent() {
    var hash = window.location.hash || "";
    var rawToken = hash.startsWith("#") ? hash.slice(1) : hash;
    if (!rawToken) {
      setStatus("Missing URL #token fragment", false);
      return false;
    }
    try {
      window.history.replaceState(null, document.title, window.location.pathname + window.location.search);
    } catch (_) {}

    var resp = await fetch("/api/v1/auth/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: rawToken })
    });
    if (!resp.ok) {
      setStatus("Session authentication failed", false);
      return false;
    }
    var data = await resp.json();
    csrfToken = data.csrfToken || "";
    setStatus("Authenticated (Loopback)", true);
    return true;
  }

  async function apiFetch(path, options) {
    var opts = Object.assign({ credentials: "same-origin" }, options || {});
    opts.headers = Object.assign({}, opts.headers || {}, {
      "X-CSRF-Token": csrfToken
    });
    var resp = await fetch(path, opts);
    if (!resp.ok) {
      throw new Error("HTTP " + resp.status + " on " + path);
    }
    return resp.json();
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refreshAll() {
    await Promise.all([
      loadApps(),
      loadCatalog(),
      loadVolunteer(),
      loadConflicts(),
      loadRuntimes()
    ]);
  }

  async function loadApps() {
    var box = document.getElementById("apps-list");
    var data = await apiFetch("/api/v1/apps");
    var apps = data.apps || [];
    if (!apps.length) {
      box.textContent = "No applications installed yet.";
      return;
    }
    var html = "<table><tr><th>App</th><th>Version</th><th>MVenv</th><th>Actions</th></tr>";
    apps.forEach(function (a) {
      html += "<tr><td><strong>" + escapeHtml(a.name) + "</strong></td><td>" +
        escapeHtml(a.version) + "</td><td>" + escapeHtml(a.mvenvName || "-") +
        "</td><td><button data-launch='" + escapeHtml(a.appId) + "'>Launch</button>" +
        "<button class='danger' data-uninstall='" + escapeHtml(a.appId) + "'>Uninstall</button></td></tr>";
    });
    box.innerHTML = html + "</table>";
    box.querySelectorAll("[data-launch]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        await apiFetch("/api/v1/apps/" + encodeURIComponent(btn.dataset.launch) + "/launch", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({})
        });
        await refreshAll();
      });
    });
    box.querySelectorAll("[data-uninstall]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        await apiFetch("/api/v1/apps/" + encodeURIComponent(btn.dataset.uninstall), { method: "DELETE" });
        await refreshAll();
      });
    });
  }

  async function loadCatalog() {
    var box = document.getElementById("catalog-list");
    var qEl = document.getElementById("catalog-search");
    var q = qEl ? qEl.value.trim() : "";
    var data = await apiFetch("/api/v1/catalog" + (q ? ("?q=" + encodeURIComponent(q)) : ""));
    var apps = data.apps || [];
    if (!apps.length) {
      box.textContent = "Catalog empty.";
      return;
    }
    var html = "<table><tr><th>App</th><th>Latest</th><th>Channel</th><th>Action</th></tr>";
    apps.forEach(function (c) {
      html += "<tr><td>" + escapeHtml(c.displayName || c.name) + "</td><td>" +
        escapeHtml(c.latestVersion) + "</td><td>" + escapeHtml(c.channel) +
        "</td><td><button data-install='" + escapeHtml(c.appId) + "'>Install</button></td></tr>";
    });
    box.innerHTML = html + "</table>";
    box.querySelectorAll("[data-install]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        await apiFetch("/api/v1/apps/install", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ appId: btn.dataset.install })
        });
        await refreshAll();
      });
    });
  }

  async function loadVolunteer() {
    var box = document.getElementById("volunteer-list");
    var data = await apiFetch("/api/v1/volunteer");
    var svcs = data.services || [];
    if (!svcs.length) {
      box.textContent = "No services installed.";
      return;
    }
    var html = "<table><tr><th>MAS ID</th><th>Mode</th><th>Volunteer</th><th>State</th></tr>";
    svcs.forEach(function (s) {
      var label = s.volunteerEnabled ? "Disable" : "Enable";
      html += "<tr><td>" + escapeHtml(s.masId) + "</td><td>" + escapeHtml(s.mode) +
        "</td><td>" + (s.mode === "LocalOnly" ? "N/A (Local)" :
        "<button data-vol='" + escapeHtml(s.masId) + "' data-next='" + (!s.volunteerEnabled) + "'>" + label + "</button>") +
        "</td><td>" + escapeHtml(s.state) + "</td></tr>";
    });
    box.innerHTML = html + "</table>";
    box.querySelectorAll("[data-vol]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        await apiFetch("/api/v1/volunteer/" + encodeURIComponent(btn.dataset.vol), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: btn.dataset.next === "true" })
        });
        await refreshAll();
      });
    });
  }

  async function loadConflicts() {
    var box = document.getElementById("conflicts-list");
    var data = await apiFetch("/api/v1/conflicts");
    var conflicts = data.conflicts || [];
    if (!conflicts.length) {
      box.textContent = "No active conflicts.";
      return;
    }
    var html = "";
    conflicts.forEach(function (c) {
      html += "<div style='margin-bottom:0.75rem;'><strong>" + escapeHtml(c.masId) +
        "</strong> (Remote: " + escapeHtml(c.remoteHolderUser) + "@" + escapeHtml(c.remoteHolderNodeId) +
        ")<br/>";
      (c.availableResolutions || []).forEach(function (act) {
        html += "<button class='warn' data-cid='" + escapeHtml(c.conflictId) +
          "' data-action='" + escapeHtml(act) + "'>" + escapeHtml(act) + "</button>";
      });
      html += "</div>";
    });
    box.innerHTML = html;
    box.querySelectorAll("[data-cid]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        await apiFetch("/api/v1/conflicts/" + encodeURIComponent(btn.dataset.cid) + "/resolve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: btn.dataset.action })
        });
        await refreshAll();
      });
    });
  }

  async function loadRuntimes() {
    var box = document.getElementById("runtimes-list");
    var data = await apiFetch("/api/v1/runtimes");
    var runtimes = data.runtimes || [];
    if (!runtimes.length) {
      box.textContent = "No runtimes discovered.";
      return;
    }
    var html = "<table><tr><th>Name</th><th>Type</th><th>Version</th><th>Executable</th></tr>";
    runtimes.forEach(function (r) {
      html += "<tr><td>" + escapeHtml(r.name) + "</td><td>" +
        (r.isMvenv ? "Sovereign MVenv" : "Host Python") + "</td><td>" +
        escapeHtml(r.version) + "</td><td><code>" + escapeHtml(r.pythonExecutable) + "</code></td></tr>";
    });
    box.innerHTML = html + "</table>";
  }

  document.addEventListener("DOMContentLoaded", async function () {
    var searchBtn = document.getElementById("btn-refresh-catalog");
    if (searchBtn) {
      searchBtn.addEventListener("click", loadCatalog);
    }
    if (await exchangeTokenIfPresent()) {
      await refreshAll();
    }
  });
})();

