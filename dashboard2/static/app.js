/* Price Monitor — live dashboard client.
 *
 * Polls /api/state every POLL_INTERVAL_MS and patches the DOM in place.
 * No full-page reloads → no flicker, no flash of stale content.
 */

(() => {
  "use strict";

  const POLL_INTERVAL_MS = 2000;
  const BUSINESS_TZ = document.body.dataset.businessTz || "Europe/London";
  const STALE_AFTER_SECONDS = parseInt(document.body.dataset.staleAfter || "180", 10);

  // ── small helpers ────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const fmtAge = (sec) => {
    if (sec == null) return "—";
    if (sec < 1) return "just now";
    if (sec < 60) return `${Math.round(sec)}s ago`;
    const m = Math.floor(sec / 60);
    const s = Math.round(sec % 60);
    if (m < 60) return s ? `${m}m ${s}s ago` : `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m ago`;
  };
  const fmtBytes = (n) => {
    if (n == null) return "—";
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
  };
  const toBusinessClock = (iso) => {
    if (!iso) return "—";
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        timeZone: BUSINESS_TZ,
      });
    } catch (_) {
      return iso;
    }
  };

  // ── filter state (preserved between polls) ───────────────────────────────
  const filterState = {
    text: "",
    source: "all", // "all" | "both" | "uk" | "us" | "pending"
  };

  $("filter-input").addEventListener("input", (e) => {
    filterState.text = e.target.value.trim().toLowerCase();
    applyFilter();
    refreshSummary();
  });
  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      filterState.source = chip.dataset.source;
      applyFilter();
      refreshSummary();
    });
  });

  // Copy merged CSV
  $("copy-csv").addEventListener("click", async () => {
    const pre = $("merged-csv");
    try {
      await navigator.clipboard.writeText(pre.textContent || "");
      $("copy-csv").textContent = "Copied!";
      setTimeout(() => ($("copy-csv").textContent = "Copy"), 1200);
    } catch (_) {
      $("copy-csv").textContent = "Failed";
      setTimeout(() => ($("copy-csv").textContent = "Copy"), 1200);
    }
  });

  // ── site card renderer ───────────────────────────────────────────────────
  function renderSite(siteKey, summary) {
    const card = $(`${siteKey}-card`);
    const kv = $(`${siteKey}-kv`);
    const waiting = $(`${siteKey}-waiting`);
    const pill = $(`${siteKey}-freshness`);

    if (!summary || !summary.present) {
      card.classList.remove("site-ok", "site-stale");
      card.classList.add("site-waiting");
      kv.hidden = true;
      waiting.hidden = false;
      pill.textContent = "no data";
      pill.className = "freshness-pill pill-waiting";
      return;
    }

    kv.hidden = false;
    waiting.hidden = true;
    card.classList.remove("site-waiting");

    if (summary.fresh) {
      card.classList.add("site-ok");
      card.classList.remove("site-stale");
      pill.className = "freshness-pill pill-live";
      pill.textContent = `LIVE · ${fmtAge(summary.age_seconds)}`;
    } else {
      card.classList.remove("site-ok");
      card.classList.add("site-stale");
      pill.className = "freshness-pill pill-stale";
      const reasonText = {
        stale_age: `STALE · last ${fmtAge(summary.age_seconds)}`,
        stale_day: `STALE · previous business day`,
        never_received: "no data",
      }[summary.stale_reason] || `STALE · ${fmtAge(summary.age_seconds)}`;
      pill.textContent = reasonText;
    }

    $(`${siteKey}-received`).textContent =
      summary.received_at ? `${toBusinessClock(summary.received_at)}  (${fmtAge(summary.age_seconds)})` : "—";
    $(`${siteKey}-cycle`).textContent = summary.cycle_id || "—";
    $(`${siteKey}-host`).innerHTML =
      `${summary.sender_host ?? "—"} <span class="muted">(${summary.sender_env ?? "—"})</span>`;
    $(`${siteKey}-bytes`).textContent = fmtBytes(summary.bytes);
    $(`${siteKey}-rows`).textContent = summary.row_count;
    $(`${siteKey}-complete`).textContent = `✓ ${summary.complete_count}`;
    $(`${siteKey}-pending`).textContent = `⏳ ${summary.pending_count}`;
    $(`${siteKey}-bday`).textContent = summary.business_day || "—";
  }

  // ── KPI + progress ───────────────────────────────────────────────────────
  function renderKpis(stats) {
    $("kpi-total").textContent = stats.total ?? 0;
    $("kpi-uk").textContent = stats.uk_done ?? 0;
    $("kpi-us").textContent = stats.us_done ?? 0;
    $("kpi-both").textContent = stats.both_done ?? 0;
    $("kpi-done").textContent = stats.merged_done ?? 0;
    $("kpi-pending").textContent = stats.pending ?? 0;

    const total = stats.total || 0;
    const pct = total > 0 ? (stats.merged_done / total) * 100 : 0;
    $("progress-fill").style.width = `${pct.toFixed(1)}%`;
    $("progress-pct").textContent = total > 0 ? `${pct.toFixed(1)}% complete` : "—";
  }

  // ── rows table (re-renders whole tbody but preserves filter state) ───────
  let lastRowsKey = "";
  function renderRows(rows) {
    // Cheap dirty-check to avoid rebuilding identical DOM every 2s.
    const key = rows
      .map((r) => `${r.price_group_name}|${r.merged_timestamp}|${r.source}|${r.uk_timestamp}|${r.us_timestamp}`)
      .join("\n");
    if (key === lastRowsKey) {
      applyFilter();
      refreshSummary();
      return;
    }
    lastRowsKey = key;

    const tbody = $("rows-body");
    if (!rows.length) {
      tbody.innerHTML = `<tr class="row-placeholder"><td colspan="7" class="muted">No rows yet — waiting for a sender…</td></tr>`;
      $("row-summary").textContent = "";
      return;
    }

    const html = rows.map((r, i) => {
      const sourceBadge = {
        both: '<span class="badge src-both">both</span>',
        uk:   '<span class="badge src-uk">UK</span>',
        us:   '<span class="badge src-us">US</span>',
        pending: '<span class="badge src-pending">pending</span>',
      }[r.source];
      const typeBadge = r.is_composite
        ? '<span class="badge composite">composite</span>'
        : '<span class="badge single">single</span>';
      const cell = (val, klass) =>
        val ? `<code class="${klass}">${escapeHtml(val)}</code>`
            : '<span class="muted">—</span>';
      return `
        <tr class="row row-${r.source}"
            data-name="${escapeHtml(r.price_group_name.toLowerCase())}"
            data-source="${r.source}"
            data-type="${r.is_composite ? 'composite' : 'single'}">
          <td class="num muted">${i + 1}</td>
          <td><strong>${escapeHtml(r.price_group_name)}</strong></td>
          <td>${cell(r.uk_timestamp, "ts-uk")}</td>
          <td>${cell(r.us_timestamp, "ts-us")}</td>
          <td>${cell(r.merged_timestamp, "ts-merged")}</td>
          <td>${sourceBadge}</td>
          <td>${typeBadge}</td>
        </tr>`;
    }).join("");

    tbody.innerHTML = html;
    applyFilter();
    refreshSummary();
  }

  function applyFilter() {
    const text = filterState.text;
    const source = filterState.source;
    const rows = document.querySelectorAll("#rows-body tr.row");
    rows.forEach((tr) => {
      const matchesText = !text
        || tr.dataset.name.includes(text)
        || tr.dataset.source.includes(text)
        || tr.dataset.type.includes(text);
      const matchesSource = source === "all" || tr.dataset.source === source;
      tr.style.display = matchesText && matchesSource ? "" : "none";
    });
  }

  function refreshSummary() {
    const total = document.querySelectorAll("#rows-body tr.row").length;
    const visible = document.querySelectorAll("#rows-body tr.row:not([style*='display: none'])").length;
    if (!total) {
      $("row-summary").textContent = "";
      return;
    }
    $("row-summary").textContent = visible === total
      ? `Showing all ${total} rows.`
      : `Showing ${visible} of ${total} rows.`;
  }

  // ── escape HTML (defense in depth) ───────────────────────────────────────
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // ── main poll loop ───────────────────────────────────────────────────────
  let consecutiveFailures = 0;

  async function poll() {
    try {
      const res = await fetch("/api/state", {
        cache: "no-store",
        headers: { "Cache-Control": "no-cache" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      consecutiveFailures = 0;
      $("live-indicator").classList.remove("offline");

      $("business-day-pill").textContent = `business day ${data.business_day}`;
      $("merge-cycles").textContent = data.merge_cycles ?? 0;
      $("last-merged").textContent = data.last_merged_at
        ? toBusinessClock(data.last_merged_at)
        : "—";

      renderSite("uk", data.uk);
      renderSite("us", data.us);
      renderKpis(data.stats || {});
      renderRows(data.merged_rows || []);
      $("merged-csv").textContent = data.merged_csv || "—";
    } catch (err) {
      consecutiveFailures += 1;
      if (consecutiveFailures >= 2) {
        $("live-indicator").classList.add("offline");
      }
      console.warn("poll failed:", err);
    } finally {
      setTimeout(poll, POLL_INTERVAL_MS);
    }
  }

  poll();
})();
