const $ = (id) => document.getElementById(id);

const LABELS = {
  deficit: "Enerji Açığı",
  surplus: "Enerji Fazlası",
  balanced: "Dengede",
  missing: "Veri yok",
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function fmtDate(value) {
  if (!value) return "—";
  const [year, month, day] = String(value).split("-");
  return `${day}.${month}.${year}`;
}

function isoToday() {
  const now = new Date();
  const offset = now.getTimezoneOffset() * 60_000;
  return new Date(now.getTime() - offset).toISOString().slice(0, 10);
}

function fmtNumber(value, digits = 1) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return number.toLocaleString("tr-TR", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function setText(id, value) {
  const node = $(id);
  if (node) node.textContent = value;
}

function directionLabel(category) {
  return LABELS[category] || LABELS.missing;
}

function showError(message) {
  const error = $("forecastError");
  if (!error) return;
  error.hidden = !message;
  error.textContent = message || "";
}

function renderTimeline(rows = []) {
  const target = $("forecastTimeline");
  if (!target) return;
  if (!rows.length) {
    target.innerHTML = '<div class="system-loading">Tahmin için yeterli geçmiş veri bulunamadı.</div>';
    return;
  }
  const hours = rows.map((row) => {
    const category = row.category || "missing";
    return `
      <div class="forecast-hour ${esc(category)}" title="${esc(row.time)} · ${esc(row.label)} · %${fmtNumber(row.confidence)} güven">
        <b>${esc(row.time)}</b>
        <strong>${esc(row.label || directionLabel(category))}</strong>
        <small>%${fmtNumber(row.confidence)}</small>
      </div>
    `;
  }).join("");
  target.innerHTML = `
    <div class="forecast-hours">${hours}</div>
    <div class="forecast-scale"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>23:00</span></div>
  `;
}

function renderProbabilities(rows = []) {
  const target = $("probabilityRows");
  if (!target) return;
  target.innerHTML = rows.map((row) => {
    const probabilities = row.probabilities || {};
    const category = row.category || "missing";
    return `
      <div class="probability-row">
        <time>${esc(row.time)}</time>
        <strong><i class="probability-dot ${esc(category)}"></i>${esc(row.label || directionLabel(category))}</strong>
        <div class="probability-stack" aria-label="${esc(row.time)} olasılık dağılımı">
          <span class="deficit" style="width:${Number(probabilities.deficit || 0)}%"></span>
          <span class="surplus" style="width:${Number(probabilities.surplus || 0)}%"></span>
          <span class="balanced" style="width:${Number(probabilities.balanced || 0)}%"></span>
        </div>
        <em>%${fmtNumber(row.confidence)} · ${Number(row.support || 0)} örnek</em>
      </div>
    `;
  }).join("");
}

function renderMethod(method = {}) {
  setText("methodDescription", method.description || "Geçmiş sistem yönü verisi saat bazında ağırlıklandırılır.");
  const target = $("methodBuckets");
  if (!target) return;
  target.innerHTML = (method.sourceBuckets || []).map((bucket) => `
    <div class="method-bucket">
      <span>${esc(bucket.label)}</span>
      <strong>${Number(bucket.sampleCount || 0)} gün</strong>
      <small>Ağırlık katsayısı ${fmtNumber(bucket.weight, 2)}</small>
    </div>
  `).join("");
}

function renderSamples(samples = []) {
  const body = $("sampleTableBody");
  if (!body) return;
  setText("sampleMeta", `${samples.length} referans gün listeleniyor`);
  if (!samples.length) {
    body.innerHTML = '<tr><td colspan="5">Kullanılabilir referans gün bulunamadı.</td></tr>';
    return;
  }
  body.innerHTML = samples.map((sample) => `
    <tr>
      <td><b>${fmtDate(sample.date)}</b></td>
      <td><div class="sample-source-tags">${(sample.sources || []).map((source) => `<span>${esc(source)}</span>`).join("")}</div></td>
      <td>${esc(sample.dominantLabel || directionLabel(sample.dominantCategory))}</td>
      <td>${Number(sample.publishedHours || 0)} / 24</td>
      <td>${fmtNumber(sample.weight, 2)}</td>
    </tr>
  `).join("");
}

function validationResultLabel(row) {
  if (!row.actualPublished) return "Bekleniyor";
  if (row.match) return "Tuttu";
  return "Sapma";
}

function renderValidation(data) {
  const summary = data.summary || {};
  const rows = data.rows || [];
  setText("validationMeta", `${fmtDate(data.date)} · ${summary.statusLabel || "Karşılaştırma"}`);
  setText(
    "validationAccuracy",
    summary.accuracy == null ? "—" : `%${fmtNumber(summary.accuracy)}`,
  );
  setText(
    "validationStatus",
    `${Number(summary.publishedHours || 0)} / 24 gerçek saat yayınlandı`,
  );
  setText("validationCorrect", `${Number(summary.correctHours || 0)} saat`);
  setText(
    "validationCompared",
    `${Number(summary.comparedHours || 0)} saat karşılaştırıldı · ${Number(summary.wrongHours || 0)} sapma`,
  );
  setText("validationMissing", `${Number(summary.missingHours || 0)} saat`);
  setText("validationNote", data.note || "Tahmin, gerçek EPİAŞ sistem yönüyle saat saat kıyaslanır.");

  const body = $("validationTableBody");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4">Karşılaştırılacak saat bulunamadı.</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => {
    const state = !row.actualPublished ? "waiting" : row.match ? "match" : "miss";
    return `
      <tr class="${state}">
        <td><b>${esc(row.time)}</b></td>
        <td><span class="direction-chip ${esc(row.forecastCategory)}">${esc(row.forecastLabel)}</span><small>%${fmtNumber(row.forecastConfidence)} güven</small></td>
        <td><span class="direction-chip ${esc(row.actualCategory)}">${esc(row.actualLabel)}</span></td>
        <td><strong>${validationResultLabel(row)}</strong></td>
      </tr>
    `;
  }).join("");
}

function renderForecast(data) {
  const summary = data.summary || {};
  const counts = summary.predictedCounts || {};
  const lowHours = summary.lowConfidenceHours || [];
  const targetLabel = `${data.targetLabel || "Tahmin"} · ${new Date(`${data.targetDate}T00:00:00+03:00`).toLocaleDateString("tr-TR", { weekday: "long" })}`;

  setText("forecastTargetDate", fmtDate(data.targetDate));
  setText("forecastTargetLabel", targetLabel);
  setText("dominantDirection", summary.dominantLabel || "—");
  setText(
    "dominantDetail",
    `${Number(counts.deficit || 0)} açık · ${Number(counts.surplus || 0)} fazla · ${Number(counts.balanced || 0)} dengede`,
  );
  setText("averageConfidence", `%${fmtNumber(summary.averageConfidence)}`);
  setText("sampleCount", `${Number(summary.sampleCount || 0)} gün`);
  setText("publishedHours", `${Number(summary.publishedHourCount || 0)} saatlik geçmiş veri okundu`);
  setText("lowConfidenceCount", `${lowHours.length} saat`);
  setText("lowConfidenceHours", lowHours.length ? lowHours.join(" · ") : "Belirgin düşük güven yok");
  setText("suiteFooterUpdated", data.generatedAt || "—");

  renderTimeline(data.forecastRows || []);
  renderProbabilities(data.forecastRows || []);
  renderMethod(data.method || {});
  renderSamples(data.samples || []);

  if ((data.warnings || []).length) {
    showError(data.warnings.join(" "));
  } else {
    showError("");
  }
}

async function loadForecast(force = false) {
  const button = $("forecastRefresh");
  if (button) {
    button.disabled = true;
    button.textContent = "Tahmin hazırlanıyor…";
  }
  showError("");
  try {
    const response = await fetch(`/sistem-yonu-tahmini/api/forecast${force ? "?refresh=1" : ""}`, {
      headers: { Accept: "application/json" },
      credentials: "include",
    });
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Tahmin alınamadı.");
    renderForecast(payload);
  } catch (error) {
    showError(error.message || "Sistem yönü tahmini hazırlanamadı.");
    renderTimeline([]);
    renderProbabilities([]);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "↻ Tahmini yenile";
    }
  }
}

async function loadValidation(force = false) {
  const input = $("validationDate");
  const button = $("validationRefresh");
  const selectedDate = input?.value || isoToday();
  if (button) {
    button.disabled = true;
    button.textContent = "Karşılaştırılıyor…";
  }
  try {
    const params = new URLSearchParams({ date: selectedDate });
    if (force) params.set("refresh", "1");
    const response = await fetch(`/sistem-yonu-tahmini/api/validation?${params}`, {
      headers: { Accept: "application/json" },
      credentials: "include",
    });
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Tahmin doğrulaması alınamadı.");
    renderValidation(payload);
  } catch (error) {
    setText("validationMeta", "Karşılaştırma hazırlanamadı");
    setText("validationNote", error.message || "Tahmin doğrulaması hazırlanamadı.");
    const body = $("validationTableBody");
    if (body) body.innerHTML = '<tr><td colspan="4">Karşılaştırma alınamadı.</td></tr>';
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "Karşılaştır";
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const validationDate = $("validationDate");
  if (validationDate) {
    validationDate.max = isoToday();
    validationDate.value = validationDate.value || isoToday();
    validationDate.addEventListener("change", () => loadValidation(false));
  }
  $("forecastRefresh")?.addEventListener("click", () => loadForecast(true));
  $("validationRefresh")?.addEventListener("click", () => loadValidation(true));
  loadForecast(false);
  loadValidation(false);
});
