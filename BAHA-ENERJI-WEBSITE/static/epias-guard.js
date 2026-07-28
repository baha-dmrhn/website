const $ = (id) => document.getElementById(id);

function fmt(value, suffix = "") {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") {
    const rounded = Math.round(value * 10) / 10;
    return `${rounded.toLocaleString("tr-TR")}${suffix}`;
  }
  return `${value}${suffix}`;
}

function cooldown(value) {
  const seconds = Number(value || 0);
  return seconds > 0 ? `${Math.ceil(seconds)} sn` : "Yok";
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

async function loadGuard() {
  const status = $("guardStatus");
  try {
    const response = await fetch("/api/epias-limits", {
      headers: { Accept: "application/json" },
      credentials: "include",
    });
    if (!response.ok) throw new Error("Koruma verisi alınamadı.");
    const data = await response.json();
    const tgt = data.configured.tgt;
    const api = data.configured.api;
    const official = data.official;
    const runtime = data.runtime || {};
    const login = data.configured.usernameFailedAttempts;
    const background = data.background || {};

    status.classList.toggle("cooldown", data.status === "cooldown");
    status.classList.remove("error");
    setText(
      "guardState",
      data.status === "cooldown"
        ? "Acil fren aktif, EPİAŞ çağrıları bekletiliyor"
        : "Koruma aktif, limitler güvenli aralıkta"
    );
    setText("guardUpdated", `Son kontrol ${data.generatedAt || "—"}`);

    setText("tgtLimit", `${tgt.perMinute}/dk · ${tgt.perSecond}/sn`);
    setText("tgtOfficialUser", `${official.tgt.usernamePerMinute}/dk · ${official.tgt.usernamePerSecond}/sn`);
    setText("tgtOfficialIp", `${official.tgt.ipPerMinute}/dk · ${official.tgt.ipPerSecond}/sn`);
    setText("tgtMinute", `${tgt.lastMinute} / ${tgt.perMinute}`);
    setText("tgtCooldown", cooldown(tgt.cooldownSeconds));

    setText("apiLimit", `${api.perMinute}/dk · ${api.perSecond}/sn`);
    setText("apiMinute", `${api.lastMinute} / ${api.perMinute}`);
    setText("apiWaits", `${fmt(api.totalWaits)} kez · ${fmt(api.totalWaitSeconds, " sn")}`);
    setText("apiBackoffs", `${fmt(api.totalBackoffs)} kez`);
    setText("apiCooldown", cooldown(api.cooldownSeconds));

    setText("singleStatus", data.configured.singleFlight.enabled ? "Aktif" : "Kapalı");
    setText("singleJoined", `${fmt(runtime.singleflightJoined)} istek paylaştı`);
    setText("singleActive", `${fmt(runtime.singleflightActive)} aktif`);
    setText("singleKey", runtime.lastSingleflightKey || "Henüz yok");

    setText("loginLimit", `${login.maxAttempts} hata / ${login.windowSeconds} sn`);
    setText("loginOfficial", `${official.tgt.failedAttemptsPerWindow} hata / ${official.tgt.failedAttemptsWindowSeconds} sn`);
    setText("loginBlock", `${login.blockSeconds} sn`);
    setText("tgtTtl", `${Math.round(data.configured.tgtCacheSeconds / 60)} dk / EPİAŞ 480 dk`);

    setText("bgStatus", background.lastStatus || (background.enabled ? "Beklemede" : "Kapalı"));
    setText("bgLastWake", background.lastWakeAt || "Henüz yok");
    setText("bgNextWake", background.nextWakeAt || "—");
    setText("bgSession", background.lastSessionUser || "Aktif oturum yok");

    setText("httpTotal", fmt(runtime.httpRequests));
    setText("httpErrors", fmt(runtime.httpErrors));
    setText(
      "lastError",
      runtime.lastError
        ? `${runtime.lastError.statusCode} · ${runtime.lastError.at}`
        : "Yok"
    );
    setText("lastRequest", runtime.lastRequestAt || "Henüz yok");

    const notes = $("guardNotes");
    notes.innerHTML = "";
    (data.notes || []).forEach((note) => {
      const li = document.createElement("li");
      li.textContent = note;
      notes.appendChild(li);
    });
  } catch (error) {
    status.classList.add("error");
    setText("guardState", error.message || "Koruma paneli yüklenemedi.");
    setText("guardUpdated", "Bağlantı hatası");
  }
}

loadGuard();
setInterval(loadGuard, 15000);
