// Regionen und Gruppen: Auf-/Zugeklappt-Zustand im Browser-Tab merken (sessionStorage), damit er beim
// Filtern und nach dem Ändern einer Einstufung erhalten bleibt. Zugeklappte Gruppen werden erst beim
// Aufklappen nachgeladen (data-src), damit die Seite auch mit ganz Deutschland schnell bleibt.
(function () {
  function read(key) {
    try { return sessionStorage.getItem("gruppe-" + key); } catch (e) { return null; }
  }
  function write(key, value) {
    try { sessionStorage.setItem("gruppe-" + key, value); } catch (e) { /* Speicher gesperrt */ }
  }

  function isVisible(el) {
    for (let node = el.parentElement; node; node = node.parentElement) {
      if (node.tagName === "DETAILS" && !node.open) return false;
    }
    return true;
  }

  function load(el) {
    const src = el.dataset.src;
    if (!src || el.dataset.loading || !el.open || !isVisible(el)) return;
    el.dataset.loading = "1";
    const body = el.querySelector(":scope > .group-body");
    fetch(src, { headers: { "X-Requested-With": "fetch" } })
      .then(function (res) { if (!res.ok) throw new Error(res.status); return res.text(); })
      .then(function (html) { body.innerHTML = html; el.removeAttribute("data-src"); })
      .catch(function () { body.innerHTML = '<div class="group-empty urgent">Laden fehlgeschlagen – Seite neu laden.</div>'; })
      .finally(function () { delete el.dataset.loading; });
  }

  const all = document.querySelectorAll("details.region, details.group");
  all.forEach(function (el) {
    const stored = read(el.dataset.group);
    if (stored === "offen") el.open = true;
    else if (stored === "zu" && !el.hasAttribute("data-force-open")) el.open = false;
  });

  // Sprungmarke (z. B. nach „weitere anzeigen“ oder Einstufung) öffnet die Gruppe samt Region
  if (location.hash.indexOf("#gruppe-") === 0) {
    const target = document.getElementById(location.hash.slice(1));
    if (target && target.tagName === "DETAILS") {
      for (let node = target; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true;
      target.scrollIntoView();
    }
  }

  all.forEach(function (el) {
    el.addEventListener("toggle", function () {
      write(el.dataset.group, el.open ? "offen" : "zu");
      if (el.classList.contains("region")) el.querySelectorAll("details.group[data-src]").forEach(load);
      else load(el);
    });
  });
  document.querySelectorAll("details.group[data-src]").forEach(load);
})();

// Fortschritt eines laufenden Abrufs anzeigen; nach Ende die Liste neu laden.
(function () {
  const banner = document.getElementById("fetch-banner");
  const text = document.getElementById("fetch-text");
  if (!banner || banner.hidden) return;

  const button = document.querySelector(".fetch-form button");

  async function poll() {
    try {
      const res = await fetch("/api/status", { cache: "no-store" });
      const s = await res.json();
      if (s.running) {
        let label = "Abruf läuft";
        if (s.phase) label += " – " + s.phase;
        if (s.total) label += " (" + s.done + "/" + s.total + ")";
        text.textContent = label;
        setTimeout(poll, 2000);
        return;
      }
      if (document.body.dataset.page === "list") {
        window.location.href = window.location.pathname + window.location.search.replace(/([?&])meldung=[^&]*&?/, "$1").replace(/[?&]$/, "");
      } else {
        banner.hidden = true;
        if (button) button.disabled = false;
      }
    } catch (e) {
      setTimeout(poll, 5000);
    }
  }
  setTimeout(poll, 1500);
})();

// Einstellungen: Regel-Listen (an/aus, hinzufügen, löschen). Gespeichert wird erst mit „Speichern“.
(function () {
  const panels = document.querySelectorAll(".rule-panel");
  if (!panels.length) return;
  const form = panels[0].closest("form");
  const saveBar = form.querySelector(".save-bar");
  let dirty = Boolean(saveBar && !saveBar.hidden);
  let submitting = false;

  function markDirty() {
    dirty = true;
    if (saveBar) saveBar.hidden = false;
  }

  function updateCount(panel) {
    const rows = panel.querySelectorAll(".rule-row");
    let active = 0;
    rows.forEach(function (row) {
      const on = row.querySelector("input[type=checkbox]").checked;
      row.classList.toggle("is-inactive", !on);
      if (on) active += 1;
    });
    panel.querySelector(".rule-count").textContent = active + " von " + rows.length + " aktiv";
  }

  function showError(panel, message) {
    const box = panel.querySelector(".rule-add-error");
    box.textContent = message || "";
    box.hidden = !message;
  }

  function existingValues(panel) {
    return Array.from(panel.querySelectorAll('.rule-list input[type=hidden][name$="-value"]'))
      .map(function (input) { return input.value.trim().toLowerCase(); });
  }

  function addRule(panel) {
    const kind = panel.dataset.kind;
    const valueInput = panel.querySelector(".rule-add-value");
    const labelInput = panel.querySelector(".rule-add-label");
    let value = valueInput.value.trim();
    const label = labelInput ? labelInput.value.trim() : "";
    if (!value) { valueInput.focus(); return; }

    if (kind === "cpv") {
      const m = value.match(/^(\d{8})(?:-\d)?$/);
      if (!m) { showError(panel, "CPV-Code: 8 Ziffern, z. B. 71314000"); valueInput.focus(); return; }
      value = m[1];
    } else {
      // kombinierte Stichwörter einheitlich schreiben: „Konzept+Energie“ -> „Konzept + Energie“
      value = value.split("+").map(function (part) { return part.trim(); }).filter(Boolean).join(" + ");
      if (!/[\p{L}\p{N}]/u.test(value)) { showError(panel, "Das Stichwort enthält keine Buchstaben."); return; }
    }
    if (existingValues(panel).indexOf(value.toLowerCase()) !== -1) {
      showError(panel, "„" + value + "“ ist schon in der Liste.");
      return;
    }
    showError(panel, "");

    // Neue Zeilen bekommen Nummern hinter den vorhandenen – so bleibt die Reihenfolge beim Speichern erhalten
    const index = 1000 + Number(panel.dataset.next);
    panel.dataset.next = String(Number(panel.dataset.next) + 1);
    const holder = document.createElement("ul");
    holder.innerHTML = panel.querySelector(".rule-template").innerHTML.split("__INDEX__").join(String(index)).trim();
    const row = holder.firstElementChild;
    row.querySelector('input[type=hidden][name$="-value"]').value = value;
    const shown = row.querySelector(".rule-value");
    if (kind === "cpv") {
      const code = document.createElement("code");
      code.textContent = value;
      shown.replaceChildren(code);
      row.querySelector('input[type=hidden][name$="-label"]').value = label;
      if (label) {
        const span = document.createElement("span");
        span.className = "rule-label muted";
        span.textContent = label;
        shown.after(span);
      }
    } else {
      shown.textContent = value;
      const addScope = panel.querySelector(".rule-add-scope");
      const rowScope = row.querySelector(".rule-scope");
      if (addScope && rowScope) rowScope.value = addScope.value;
    }
    row.querySelector(".rule-delete").setAttribute("aria-label", value + " löschen");
    row.classList.add("is-new-rule");
    panel.querySelector(".rule-list").appendChild(row);
    row.scrollIntoView({ block: "nearest" });

    valueInput.value = "";
    if (labelInput) labelInput.value = "";
    valueInput.focus();
    updateCount(panel);
    markDirty();
  }

  panels.forEach(function (panel) {
    panel.addEventListener("click", function (event) {
      const del = event.target.closest(".rule-delete");
      if (del) {
        del.closest(".rule-row").remove();
        updateCount(panel);
        markDirty();
        return;
      }
      if (event.target.closest(".rule-add-button")) {
        addRule(panel);
        return;
      }
      const bulk = event.target.closest("[data-bulk]");
      if (bulk) {
        panel.querySelectorAll(".rule-list input[type=checkbox]").forEach(function (box) {
          box.checked = bulk.dataset.bulk === "on";
        });
        updateCount(panel);
        markDirty();
      }
    });
    panel.addEventListener("change", function (event) {
      if (event.target.matches(".rule-list input[type=checkbox]")) {
        updateCount(panel);
        markDirty();
      } else if (event.target.closest(".rule-options") || event.target.matches(".rule-list .rule-scope")) {
        markDirty();
      }
    });
    panel.querySelectorAll(".rule-add input").forEach(function (input) {
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {  // Enter fügt hinzu, statt das ganze Formular abzuschicken
          event.preventDefault();
          addRule(panel);
        }
      });
      input.addEventListener("input", function () { showError(panel, ""); });
    });
  });

  form.querySelectorAll(".options input").forEach(function (input) { input.addEventListener("change", markDirty); });
  form.addEventListener("submit", function () { submitting = true; });
  window.addEventListener("beforeunload", function (event) {
    if (dirty && !submitting) { event.preventDefault(); event.returnValue = ""; }
  });
})();

// Rückfrage vor dem Absenden (data-confirm am Formular). Früher ein onsubmit-Attribut – die
// Sicherheitsrichtlinie der Seite (CSP) erlaubt keine Skripte im HTML.
document.addEventListener("submit", function (event) {
  const form = event.target;
  const message = form && form.dataset ? form.dataset.confirm : "";
  if (message && !window.confirm(message)) event.preventDefault();
});
