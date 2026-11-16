"""Zero-CDN self-contained static HTML5 Application Catalog Portal generator."""

from __future__ import annotations

import html
import json
from typing import Optional

from aurasprout.catalog.models import CatalogManifest


def generate_catalog_html(manifest: CatalogManifest, catalog_url: Optional[str] = None) -> str:
    """Render a completely offline, zero-CDN HTML5 portal for a CatalogManifest."""
    title = html.escape(manifest.catalog_name)
    desc = html.escape(manifest.description or "Unified Zero-Privilege Application Catalog")
    updated = html.escape(manifest.updated_at[:19].replace("T", " ") + " UTC")
    effective_catalog_url = html.escape(catalog_url or manifest.base_url or "./catalog.json")

    all_tags = sorted({tag for app in manifest.apps.values() for tag in app.tags})
    cards_html = []
    for app in sorted(manifest.apps.values(), key=lambda a: a.app_id):
        v_info = app.get_version(app.latest_version)
        v_str = html.escape(v_info.version if v_info else app.latest_version)
        ch_str = html.escape(v_info.channel if v_info else app.channel)
        app_disp = html.escape(app.display_name or app.name)
        app_id_esc = html.escape(app.app_id)
        desc_esc = html.escape(app.description or "No description provided.")
        tags_html = " ".join(f'<span class="pill">{html.escape(t)}</span>' for t in app.tags)
        sha_str = html.escape(v_info.bundle_sha256 if v_info else "")
        sha_short = f"{sha_str[:16]}..." if sha_str else "none"
        letter = html.escape((app.display_name or app.app_id)[:1].upper() or "A")
        bundle_url = html.escape(
            (v_info.bundle_url if v_info and v_info.bundle_url else f"apps/{app_id_esc}/{app_id_esc}-{v_str}.apkg")
        )
        ver_options = "".join(
            f'<option value="{html.escape(ver_k)}">v{html.escape(ver_k)} ({html.escape(ver_obj.channel)})</option>'
            for ver_k, ver_obj in sorted(app.versions.items(), reverse=True)
        )

        cards_html.append(
            f"""
        <div class="app-card" data-app-id="{app_id_esc}" data-channel="{ch_str}">
          <div>
            <div class="card-header">
              <div class="app-avatar">{letter}</div>
              <div class="header-text">
                <div class="app-title" title="{app_disp}">{app_disp}</div>
                <div class="app-meta">
                  <span class="app-slug">{app_id_esc}</span>
                  <span class="version-badge">v{v_str}</span>
                  <span class="channel-badge">{ch_str}</span>
                </div>
              </div>
            </div>
            <p class="app-desc">{desc_esc}</p>
            <div class="tags-list">{tags_html}</div>
          </div>
          <div class="card-actions">
            <select class="ver-select" aria-label="Available versions">{ver_options}</select>
            <div class="checksum-bar" title="SHA-256: {sha_str}">
              <span>SHA-256:</span>
              <span class="checksum-hash">{sha_short}</span>
              <button class="copy-btn" type="button" onclick="copyText('{sha_str}', this)">Copy</button>
            </div>
            {f'''<div style="margin: 0.4rem 0;">
              <a href="{catalog_url.rstrip('/') if catalog_url else '.'}/installers/aurasprout-{v_str}-installer.zip" class="btn-native-installer" download>
                &#11015; Download Native Installer (.zip)
              </a>
            </div>''' if app.app_id == 'aurasprout' else ''}
            <div class="buttons-row">
              <button class="btn-install" type="button" onclick="openInstallModal('{app_id_esc}', '{v_str}')">
                Install CLI Command
              </button>
              <a href="{bundle_url}" class="btn-download" download>Download .apkg</a>
            </div>
          </div>
        </div>"""
        )

    tag_buttons = '<button class="tag-btn active" type="button" data-tag="all">All</button>\n' + "\n".join(
        f'<button class="tag-btn" type="button" data-tag="{html.escape(t)}">{html.escape(t)}</button>'
        for t in all_tags
    )
    manifest_json_escaped = json.dumps(manifest.to_dict()).replace("</script>", "<\\/script>")
    rendered_cards = "\n".join(cards_html) or "<p class='empty'>No applications published yet.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #0d1117;
      --card-bg: #161b22;
      --card-border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --green: #238636;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: var(--font); background: var(--bg); color: var(--text); padding: 1.5rem; }}
    header {{ max-width: 1200px; margin: 0 auto 1.5rem auto; border-bottom: 1px solid var(--card-border); padding-bottom: 1rem; }}
    h1 {{ font-size: 1.75rem; margin-bottom: 0.25rem; }}
    .sub {{ color: var(--muted); font-size: 0.9rem; }}
    .controls {{ max-width: 1200px; margin: 0 auto 1.25rem auto; display: flex; gap: 0.75rem; flex-wrap: wrap; align-items: center; }}
    input, select {{ background: var(--card-bg); color: var(--text); border: 1px solid var(--card-border); border-radius: 6px; padding: 0.45rem 0.75rem; }}
    .tags-bar {{ display: flex; gap: 0.4rem; flex-wrap: wrap; }}
    .tag-btn {{ background: var(--card-bg); color: var(--text); border: 1px solid var(--card-border); border-radius: 999px; padding: 0.3rem 0.75rem; cursor: pointer; }}
    .tag-btn.active {{ border-color: var(--accent); color: var(--accent); }}
    .grid {{ max-width: 1200px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }}
    .app-card {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; padding: 1rem; display: flex; flex-direction: column; justify-content: space-between; gap: 0.85rem; }}
    .card-header {{ display: flex; align-items: center; gap: 0.75rem; }}
    .app-avatar {{ width: 38px; height: 38px; border-radius: 8px; background: #1f6feb; display: grid; place-items: center; font-weight: 700; }}
    .app-title {{ font-weight: 600; font-size: 1.05rem; }}
    .app-meta {{ font-size: 0.8rem; color: var(--muted); display: flex; gap: 0.4rem; margin-top: 0.15rem; }}
    .version-badge, .channel-badge, .pill {{ background: #21262d; padding: 0.1rem 0.45rem; border-radius: 6px; font-size: 0.75rem; }}
    .app-desc {{ color: var(--muted); font-size: 0.88rem; margin-top: 0.6rem; }}
    .tags-list {{ display: flex; gap: 0.3rem; flex-wrap: wrap; margin-top: 0.5rem; }}
    .checksum-bar {{ font-size: 0.75rem; color: var(--muted); display: flex; align-items: center; justify-content: space-between; margin: 0.4rem 0; }}
    .copy-btn {{ background: #21262d; color: var(--text); border: 1px solid var(--card-border); border-radius: 5px; padding: 0.15rem 0.45rem; cursor: pointer; }}
    .buttons-row {{ display: flex; gap: 0.5rem; }}
    .btn-native-installer {{
      display: block;
      background: linear-gradient(135deg, rgba(88, 166, 255, 0.15) 0%, rgba(35, 134, 54, 0.2) 100%);
      border: 1px solid #58a6ff;
      color: #58a6ff;
      text-decoration: none;
      text-align: center;
      padding: 0.45rem 0.75rem;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      transition: all 0.2s ease;
    }}
    .btn-native-installer:hover {{
      background: rgba(88, 166, 255, 0.25);
      box-shadow: 0 0 12px rgba(88, 166, 255, 0.3);
      color: #ffffff;
    }}
    .btn-install {{ flex: 1; background: var(--green); color: #fff; border: none; border-radius: 6px; padding: 0.45rem; cursor: pointer; font-weight: 600; }}
    .btn-download {{ background: #21262d; color: var(--text); border: 1px solid var(--card-border); border-radius: 6px; padding: 0.45rem 0.75rem; text-decoration: none; font-size: 0.85rem; }}
    .modal {{ position: fixed; inset: 0; background: rgba(0, 0, 0, 0.7); display: none; align-items: center; justify-content: center; }}
    .modal-box {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; max-width: 600px; width: 92%; padding: 1.25rem; }}
    pre {{ background: #0d1117; border: 1px solid var(--card-border); padding: 0.75rem; border-radius: 6px; overflow-x: auto; margin: 0.75rem 0; }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <p class="sub">{desc} | Updated: {updated} | Apps: {len(manifest.apps)}</p>
  </header>
  <div class="controls">
    <input id="search-input" type="search" placeholder="Filter applications...">
    <select id="channel-filter">
      <option value="all">All channels</option>
      <option value="stable">stable</option>
      <option value="preview">preview</option>
      <option value="canary">canary</option>
      <option value="nightly">nightly</option>
    </select>
    <div class="tags-bar">{tag_buttons}</div>
  </div>
  <div id="app-grid" class="grid">{rendered_cards}</div>
  <div id="install-modal" class="modal">
    <div class="modal-box">
      <h2 id="modal-app-title">Deploy Application</h2>
      <pre id="modal-code"></pre>
      <button id="modal-copy-btn" class="btn-install" type="button" onclick="copyModalCode()">Copy Command</button>
      <button class="copy-btn" type="button" onclick="closeModal()">Close</button>
    </div>
  </div>
  <script>
    const CATALOG_URL = {json.dumps(effective_catalog_url)};
    const MANIFEST = {manifest_json_escaped};
    let activeTag = "all";
    function filterCards() {{
      const q = (document.getElementById("search-input").value || "").toLowerCase();
      const ch = document.getElementById("channel-filter").value;
      document.querySelectorAll(".app-card").forEach((card) => {{
        const appId = card.dataset.appId;
        const entry = (MANIFEST.apps || {{}})[appId] || {{}};
        const text = (appId + " " + (entry.displayName || "") + " " + (entry.description || "")).toLowerCase();
        const tags = entry.tags || [];
        const matchQ = !q || text.includes(q);
        const matchCh = ch === "all" || card.dataset.channel === ch;
        const matchTag = activeTag === "all" || tags.includes(activeTag);
        card.style.display = (matchQ && matchCh && matchTag) ? "flex" : "none";
      }});
    }}
    document.getElementById("search-input").addEventListener("input", filterCards);
    document.getElementById("channel-filter").addEventListener("change", filterCards);
    document.querySelectorAll(".tag-btn").forEach((btn) => {{
      btn.addEventListener("click", () => {{
        document.querySelectorAll(".tag-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        activeTag = btn.dataset.tag;
        filterCards();
      }});
    }});
    function copyText(text, btn) {{
      if (navigator.clipboard) navigator.clipboard.writeText(text);
      const orig = btn.textContent;
      btn.textContent = "Copied";
      setTimeout(() => {{ btn.textContent = orig; }}, 1200);
    }}
    function openInstallModal(appId, version) {{
      document.getElementById("modal-app-title").textContent = "Install " + appId + " v" + version;
      document.getElementById("modal-code").textContent =
        'aurasprout install ' + appId + ' --version ' + version + ' --from-catalog "' + CATALOG_URL + '"';
      document.getElementById("install-modal").style.display = "flex";
    }}
    function closeModal() {{ document.getElementById("install-modal").style.display = "none"; }}
    function copyModalCode() {{
      const code = document.getElementById("modal-code").textContent;
      copyText(code, document.getElementById("modal-copy-btn"));
    }}
  </script>
</body>
</html>
"""

