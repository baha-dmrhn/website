const $ = (id) => document.getElementById(id);
let activeHistoricalDate = "";
let activeValidationDate = "";
const FORECAST_CLIENT_CACHE_VERSION = "v2";
const FORECAST_CLIENT_CACHE_LIVE_MS = 90_000;
const FORECAST_CLIENT_CACHE_HISTORY_MS = 30 * 60_000;

const LABELS = {
  deficit: "Enerji Açığı",
  surplus: "Enerji Fazlası",
  balanced: "Dengede",
  uncertain: "Kararsız",
  missing: "Veri yok",
};

function forecastClientCacheKey(query) {
  return `baha-system-direction:${FORECAST_CLIENT_CACHE_VERSION}:${query || "live"}`;
}

function readForecastClientCache(key, historical) {
  try {
    const record = JSON.parse(sessionStorage.getItem(key) || "null");
    const maximumAge = historical
      ? FORECAST_CLIENT_CACHE_HISTORY_MS
      : FORECAST_CLIENT_CACHE_LIVE_MS;
    if (
      !record
      || !record.payload
      || !Number.isFinite(Number(record.storedAt))
      || Date.now() - Number(record.storedAt) > maximumAge
    ) {
      return null;
    }
    return record.payload;
  } catch {
    return null;
  }
}

function writeForecastClientCache(key, payload) {
  try {
    sessionStorage.setItem(
      key,
      JSON.stringify({ storedAt: Date.now(), payload }),
    );
  } catch {
    // Storage can be disabled; server-side and disk caches still protect EPİAŞ.
  }
}

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

function isoDaysAgo(days) {
  const target = new Date();
  target.setDate(target.getDate() - Number(days || 0));
  const offset = target.getTimezoneOffset() * 60_000;
  return new Date(target.getTime() - offset).toISOString().slice(0, 10);
}

function shiftIsoDate(value, days) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return "";
  const target = new Date(Date.UTC(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
  ));
  target.setUTCDate(target.getUTCDate() + Number(days || 0));
  return target.toISOString().slice(0, 10);
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

function contextTooltip(row = {}) {
  const context = row.context || {};
  const weather = context.weather || {};
  const weights = row.modelContributions?.weights || {};
  const parts = [];
  if (Number.isFinite(Number(weather.temperature))) parts.push(`${fmtNumber(weather.temperature)} °C`);
  if (Number.isFinite(Number(weather.windSpeed))) parts.push(`${fmtNumber(weather.windSpeed)} km/sa rüzgâr`);
  if (Number.isFinite(Number(weather.cloudCover))) parts.push(`%${fmtNumber(weather.cloudCover, 0)} bulut`);
  if (context.impact) parts.push(context.impact);
  const modelParts = [
    ["Tarihsel", weights.history],
    ["ML", weights.ml],
    ["Rejim", weights.transition],
    ["Sapma ML", weights.learning],
    ["Canlı", weights.operational],
  ]
    .filter(([, weight]) => Number(weight) > 0)
    .map(([label, weight]) => `${label} %${fmtNumber(Number(weight) * 100, 0)}`);
  if (modelParts.length) parts.push(modelParts.join(" · "));
  return parts.length ? ` · ${parts.join(" · ")}` : "";
}

function renderTimeline(rows = []) {
  const target = $("forecastTimeline");
  if (!target) return;
  if (!rows.length) {
    target.innerHTML = '<div class="system-loading">Tahmin için yeterli geçmiş veri bulunamadı.</div>';
    return;
  }
  const hours = rows.map((row) => {
    const category = row.decisionCategory || row.category || "missing";
    const observed = Boolean(row.observed);
    const statusText = observed ? "Gerçekleşen" : `%${fmtNumber(row.confidence)} güven`;
    return `
      <div class="forecast-hour ${esc(category)}${observed ? " observed" : ""}" title="${esc(row.time)} · ${esc(row.decisionLabel || row.label)} · ${esc(statusText)}${row.decisionReason ? ` · ${esc(row.decisionReason)}` : ""}${esc(contextTooltip(row))}">
        <b>${esc(row.time)}</b>
        <strong>${esc(row.decisionLabel || row.label || directionLabel(category))}</strong>
        <small>${esc(statusText)}</small>
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
    const category = row.decisionCategory || row.category || "missing";
    return `
      <div class="probability-row">
        <time>${esc(row.time)}</time>
        <strong><i class="probability-dot ${esc(category)}"></i>${esc(row.decisionLabel || row.label || directionLabel(category))}</strong>
        <div class="probability-stack" aria-label="${esc(row.time)} olasılık dağılımı">
          <span class="deficit" style="width:${Number(probabilities.deficit || 0)}%"></span>
          <span class="surplus" style="width:${Number(probabilities.surplus || 0)}%"></span>
          <span class="balanced" style="width:${Number(probabilities.balanced || 0)}%"></span>
        </div>
        <em>${row.observed ? "Gerçekleşen" : `%${fmtNumber(row.confidence)} · ${Number(row.support || 0)} örnek`}</em>
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

  const inputTarget = $("contextInputs");
  if (!inputTarget) return;
  inputTarget.innerHTML = (method.inputs || []).map((input) => `
    <div class="context-input ${esc(input.status || "fallback")}">
      <i></i>
      <span>${esc(input.label)}</span>
      <strong>${esc(input.statusLabel || (input.status === "ready" ? "Kullanılıyor" : "Yedek model"))}</strong>
      <small>${esc(input.detail || "")}</small>
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
  if (row.decisionAbstained) return "Kararsız";
  if (row.match) return "Tuttu";
  return "Sapma";
}

function shortDirectionLabel(category) {
  return {
    deficit: "Açık",
    surplus: "Fazla",
    balanced: "Denge",
    uncertain: "Kararsız",
    missing: "Yok",
  }[category] || "Yok";
}

function validationHourNodes() {
  return Array.from(document.querySelectorAll(".validation-direction-hour"));
}

function updateValidationNavState() {
  const scroller = $("validationDirectionScroll");
  if (!scroller) return;
  const maximum = Math.max(0, scroller.scrollWidth - scroller.clientWidth);
  const previous = $("validationHourPrev");
  const next = $("validationHourNext");
  if (previous) previous.disabled = scroller.scrollLeft <= 4;
  if (next) next.disabled = scroller.scrollLeft >= maximum - 4;
}

function focusValidationHour(hour, smooth = true) {
  const scroller = $("validationDirectionScroll");
  const node = validationHourNodes().find(
    (item) => Number(item.dataset.hour) === Number(hour),
  );
  if (!scroller || !node) return;
  const left = node.offsetLeft - ((scroller.clientWidth - node.offsetWidth) / 2);
  scroller.scrollTo({
    left: Math.max(0, left),
    behavior: smooth ? "smooth" : "auto",
  });
  window.setTimeout(updateValidationNavState, smooth ? 320 : 0);
}

function moveValidationHours(direction) {
  const scroller = $("validationDirectionScroll");
  const nodes = validationHourNodes();
  if (!scroller || !nodes.length) return;
  const viewportCenter = scroller.scrollLeft + (scroller.clientWidth / 2);
  const currentIndex = nodes.reduce((bestIndex, node, index) => {
    const center = node.offsetLeft + (node.offsetWidth / 2);
    const best = nodes[bestIndex];
    const bestCenter = best.offsetLeft + (best.offsetWidth / 2);
    return Math.abs(center - viewportCenter) < Math.abs(bestCenter - viewportCenter)
      ? index
      : bestIndex;
  }, 0);
  const nextIndex = Math.max(
    0,
    Math.min(nodes.length - 1, currentIndex + (direction * 3)),
  );
  focusValidationHour(Number(nodes[nextIndex].dataset.hour));
}

function focusLatestValidationHour(smooth = true) {
  const published = validationHourNodes().filter(
    (node) => node.dataset.published === "true",
  );
  const latest = published.at(-1) || validationHourNodes()[0];
  if (latest) focusValidationHour(Number(latest.dataset.hour), smooth);
}

function qualityText(metric = {}) {
  return metric.accuracy == null ? "—" : `%${fmtNumber(metric.accuracy)}`;
}

function renderQualityMetrics(quality = {}) {
  const windows = quality.windows || {};
  const lastDay = windows.lastDay || {};
  const last7 = windows.last7 || {};
  const last30 = windows.last30 || {};
  const categories = quality.categories || {};
  const categoryBindings = [
    ["Deficit", categories.deficit || {}],
    ["Surplus", categories.surplus || {}],
    ["Balanced", categories.balanced || {}],
  ];

  setText("qualityLastDay", qualityText(lastDay));
  setText(
    "qualityLastDayMeta",
    lastDay.date
      ? `${fmtDate(lastDay.date)} · ${Number(lastDay.comparedHours || 0)} karar · %${fmtNumber(lastDay.coverage || 0, 0)} kapsam`
      : "Yeterli veri bekleniyor",
  );
  setText("qualityLast7", qualityText(last7));
  setText(
    "qualityLast7Meta",
    `${Number(last7.dayCount || 0)} gün · ${Number(last7.comparedHours || 0)} karar · %${fmtNumber(last7.coverage || 0, 0)} kapsam`,
  );
  setText("qualityLast30", qualityText(last30));
  setText(
    "qualityLast30Meta",
    `${Number(last30.dayCount || 0)} gün · ${Number(last30.comparedHours || 0)} karar · %${fmtNumber(last30.coverage || 0, 0)} kapsam`,
  );
  categoryBindings.forEach(([suffix, metric]) => {
    setText(`quality${suffix}`, qualityText(metric));
    setText(
      `quality${suffix}Meta`,
      `${Number(metric.comparedHours || 0)} gerçek saat`,
    );
  });

  const confidenceGap = quality.confidenceGap;
  setText(
    "validationQualityMeta",
    last30.comparedHours
      ? `${Number(last30.comparedHours)} kararlı saat · %${fmtNumber(last30.coverage || 0, 0)} karar kapsamı${confidenceGap == null ? "" : ` · güven farkı ${fmtNumber(confidenceGap)} puan`}`
      : "Yeterli geçmiş test verisi bekleniyor",
  );

  const dailyTarget = $("validationDailyQuality");
  if (dailyTarget) {
    const dailyRows = (quality.daily || []).slice(-14);
    dailyTarget.innerHTML = dailyRows.length
      ? dailyRows.map((metric) => {
        const accuracy = Number(metric.accuracy);
        const hasAccuracy = Number.isFinite(accuracy);
        const height = hasAccuracy ? Math.max(8, Math.min(100, accuracy)) : 4;
        const dateLabel = fmtDate(metric.date).slice(0, 5);
        const title = hasAccuracy
          ? `${fmtDate(metric.date)} · %${fmtNumber(accuracy)} başarı · %${fmtNumber(metric.coverage || 0)} kapsam`
          : `${fmtDate(metric.date)} · Yön kararı yok`;
        return `
          <div class="quality-day" title="${esc(title)}">
            <span class="quality-day-value">${hasAccuracy ? `%${fmtNumber(accuracy, 0)}` : "—"}</span>
            <i style="--quality-height:${height}%"></i>
            <small>${esc(dateLabel)}</small>
          </div>
        `;
      }).join("")
      : '<div class="quality-empty">Günlük başarı verisi hazırlanıyor…</div>';
  }

  const target = $("validationHourlyQuality");
  if (!target) return;
  const hoursByNumber = new Map(
    (quality.hours || []).map((metric) => [Number(metric.hour), metric]),
  );
  target.innerHTML = Array.from({ length: 24 }, (_, hour) => {
    const metric = hoursByNumber.get(hour) || {};
    const accuracy = Number(metric.accuracy);
    const hasAccuracy = Number.isFinite(accuracy);
    const hue = hasAccuracy ? Math.max(0, Math.min(142, accuracy * 1.42)) : 215;
    const color = hasAccuracy
      ? `hsl(${hue} 68% 43%)`
      : "var(--system-slate)";
    const title = hasAccuracy
      ? `${String(hour).padStart(2, "0")}:00 · %${fmtNumber(accuracy)} başarı · ${Number(metric.comparedHours || 0)} saat`
      : `${String(hour).padStart(2, "0")}:00 · Yeterli test verisi yok`;
    return `
      <div class="quality-hour" style="--quality-color:${color}" title="${esc(title)}">
        <span>${String(hour).padStart(2, "0")}</span>
        <strong>${hasAccuracy ? `%${fmtNumber(accuracy, 0)}` : "—"}</strong>
      </div>
    `;
  }).join("");
}

function renderValidationDirection(rows = []) {
  const target = $("validationDirectionTimeline");
  if (!target) return;
  const rowsByHour = new Map(
    rows.map((row) => [Number(row.hour), row]),
  );
  const normalizedRows = Array.from({ length: 24 }, (_, hour) => (
    rowsByHour.get(hour) || {
      hour,
      time: `${String(hour).padStart(2, "0")}:00`,
      forecastCategory: "missing",
      forecastLabel: "Veri yok",
      actualCategory: "missing",
      actualLabel: "Veri yok",
      actualPublished: false,
      match: null,
    }
  ));

  target.innerHTML = normalizedRows.map((row) => {
    const state = !row.actualPublished
      ? "waiting"
      : row.decisionAbstained
        ? "uncertain"
        : row.match
          ? "match"
          : "miss";
    const forecastCategory = row.forecastCategory || "missing";
    const actualCategory = row.actualCategory || "missing";
    const forecastShort = shortDirectionLabel(forecastCategory);
    const actualShort = shortDirectionLabel(actualCategory);
    const accessibleText = row.actualPublished
      ? `${row.time}: tahmin ${row.forecastLabel}, gerçek ${row.actualLabel}, ${validationResultLabel(row)}`
      : `${row.time}: tahmin ${row.forecastLabel}, gerçek değer bekleniyor`;

    const actualLane = state === "match"
      ? `
        <span class="validation-direction-pair" title="Tahmin ve gerçek: ${esc(row.actualLabel)}">
          <span class="validation-direction-marker actual compact ${esc(actualCategory)}"><i>G</i><b>${esc(actualShort)}</b></span>
          <span class="validation-direction-marker forecast compact ${esc(forecastCategory)}"><i>T</i><b>${esc(forecastShort)}</b></span>
        </span>
      `
      : state === "miss" || state === "uncertain"
        ? `<span class="validation-direction-marker actual ${esc(actualCategory)}" title="Gerçek: ${esc(row.actualLabel)}"><i>G</i><b>${esc(actualShort)}</b></span>`
        : '<span class="validation-direction-marker actual waiting" title="Gerçek değer bekleniyor"><i>G</i><b>Bekliyor</b></span>';
    const centerLane = "";
    const forecastLane = state !== "match"
      ? `<span class="validation-direction-marker forecast ${esc(forecastCategory)}" title="Tahmin: ${esc(row.forecastLabel)}"><i>T</i><b>${esc(forecastShort)}</b></span>`
      : "";

    return `
      <div class="validation-direction-hour ${state}" data-hour="${Number(row.hour)}" data-published="${Boolean(row.actualPublished)}" aria-label="${esc(accessibleText)}">
        <div class="validation-direction-lane actual-lane">${actualLane}</div>
        <div class="validation-direction-lane center-lane">${centerLane}</div>
        <div class="validation-direction-lane forecast-lane">${forecastLane}</div>
        <time>${esc(row.time)}</time>
      </div>
    `;
  }).join("");
  window.requestAnimationFrame(() => focusLatestValidationHour(false));
}

function renderValidation(data) {
  const summary = data.summary || {};
  const rows = data.rows || [];
  const recordedHours = Number(data.forecastLedger?.recordedHours || 0);
  const sourceLabel = data.forecastSource === "locked_ledger"
    ? `${recordedHours} saat kilitli yayın`
    : data.forecastSource === "hybrid_ledger"
      ? `${recordedHours} saat kilitli · ${Math.max(0, 24 - recordedHours)} saat kopyasız tamamlandı`
      : "Yeniden oluşturulan tahmin";
  setText(
    "validationMeta",
    `${fmtDate(data.date)} · ${summary.statusLabel || "Karşılaştırma"} · ${sourceLabel}`,
  );
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
    `${Number(summary.comparedHours || 0)} yön kararı · ${Number(summary.wrongHours || 0)} sapma · ${Number(summary.undecidedHours || 0)} kararsız`,
  );
  setText("validationMissing", `${Number(summary.missingHours || 0)} saat`);
  if (activeHistoricalDate) {
    setText(
      "historicalForecastHint",
      data.forecastSource === "locked_ledger"
        ? `${recordedHours} saatlik ilk yayın kaydı gerçek değerlerle karşılaştırılıyor.`
        : data.forecastSource === "hybrid_ledger"
          ? `${recordedHours} saatlik ilk yayın kaydı korundu; eksik saatler hedef günün gerçekleri kullanılmadan tamamlandı.`
          : "Tahmin yalnızca seçilen tarihten önceki verilerle yeniden kuruldu; gerçek değer sonradan karşılaştırıldı.",
    );
  }
  renderQualityMetrics(data.qualityMetrics || {});
  renderValidationDirection(rows);

  const body = $("validationTableBody");
  if (!body) return;
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="4">Karşılaştırılacak saat bulunamadı.</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => {
    const state = !row.actualPublished
      ? "waiting"
      : row.decisionAbstained
        ? "uncertain"
        : row.match
          ? "match"
          : "miss";
    return `
      <tr class="${state}">
        <td><b>${esc(row.time)}</b></td>
        <td><span class="direction-chip ${esc(row.forecastCategory)}">${esc(row.forecastLabel)}</span><small>%${fmtNumber(row.forecastConfidence)} güven</small></td>
        <td><span class="direction-chip ${esc(row.actualCategory)}">${esc(row.actualLabel)}</span></td>
        <td>
          <strong>${validationResultLabel(row)}</strong>
          ${row.reason ? `<small class="validation-reason">${esc(row.reason)}</small>` : ""}
        </td>
      </tr>
    `;
  }).join("");
}

function renderForecast(data) {
  const summary = data.summary || {};
  const schedule = data.schedule || {};
  const model = data.modelSummary || {};
  const calendar = data.calendarContext || {};
  const counts = summary.decisionCounts || summary.predictedCounts || {};
  const lowHours = summary.uncertainHours || summary.lowConfidenceHours || [];
  const targetLabel = [
    data.targetLabel || "Tahmin",
    new Date(`${data.targetDate}T00:00:00+03:00`).toLocaleDateString("tr-TR", { weekday: "long" }),
    calendar.label,
  ].filter(Boolean).join(" · ");

  setText("forecastHeroText", schedule.detail || "Geçmiş sistem yönü desenleri saat bazında incelenir.");
  setText("forecastTargetDate", fmtDate(data.targetDate));
  setText("forecastTargetLabel", targetLabel);
  setText("forecastModelName", model.name || "Çok modelli tahmin");
  setText("forecastModelDetail", model.detail || "Performansa göre ağırlıklı");
  const observedLegend = $("observedLegend");
  if (observedLegend) {
    observedLegend.hidden = Number(summary.observedHours || 0) === 0;
  }
  setText("timelineTitle", schedule.headline || `${data.targetLabel || "Seçili gün"} sistem yönü zaman şeridi`);
  const validationDate = activeHistoricalDate || schedule.validationDate || "";
  const validationTitleDate = validationDate || data.targetDate;
  setText(
    "validationTitle",
    `${fmtDate(validationTitleDate)} tarihindeki tahmin gerçekleşenle tuttu mu?`,
  );
  setText("dominantDirection", summary.dominantLabel || "—");
  setText(
    "dominantDetail",
    `${Number(counts.deficit || 0)} açık · ${Number(counts.surplus || 0)} fazla · ${Number(counts.balanced || 0)} dengede · ${Number(summary.uncertainCount || 0)} kararsız`,
  );
  setText("averageConfidence", `%${fmtNumber(summary.averageConfidence)}`);
  setText("sampleCount", `${Number(summary.sampleCount || 0)} gün`);
  setText("publishedHours", `${Number(summary.publishedHourCount || 0)} saatlik geçmiş veri okundu`);
  setText("lowConfidenceCount", `${lowHours.length} saat`);
  setText("lowConfidenceHours", lowHours.length ? lowHours.join(" · ") : "Kararsız saat yok");
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

function applyForecastPayload(payload) {
  renderForecast(payload);
  activeValidationDate = (
    activeHistoricalDate
    || payload.schedule?.validationDate
    || isoToday()
  );
  const liveButton = $("historicalForecastLive");
  if (liveButton) liveButton.hidden = !activeHistoricalDate;
  if (payload.validation) {
    renderValidation(payload.validation);
    return true;
  }
  return false;
}

async function loadForecast(force = false) {
  const button = $("forecastRefresh");
  const historyButton = $("historicalForecastRun");
  if (button) {
    button.disabled = true;
    button.textContent = "Tahmin hazırlanıyor…";
  }
  if (historyButton) historyButton.disabled = true;
  showError("");
  try {
    const params = new URLSearchParams();
    params.set("includeValidation", "1");
    if (activeHistoricalDate) {
      params.set("date", activeHistoricalDate);
      params.set("historical", "1");
    }
    const cacheKey = forecastClientCacheKey(params.toString());
    if (!force) {
      const cachedPayload = readForecastClientCache(
        cacheKey,
        Boolean(activeHistoricalDate),
      );
      if (cachedPayload) {
        applyForecastPayload(cachedPayload);
        return;
      }
    }
    if (force) params.set("refresh", "1");
    const query = params.toString();
    const response = await fetch(`/sistem-yonu-tahmini/api/forecast${query ? `?${query}` : ""}`, {
      headers: { Accept: "application/json" },
      credentials: "include",
    });
    if (response.status === 401) {
      sessionStorage.removeItem(cacheKey);
      window.location.replace("/login");
      return;
    }
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Tahmin alınamadı.");
    writeForecastClientCache(cacheKey, payload);
    if (!applyForecastPayload(payload)) {
      await loadValidation(force);
    }
  } catch (error) {
    showError(error.message || "Sistem yönü tahmini hazırlanamadı.");
    renderTimeline([]);
    renderProbabilities([]);
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = "↻ Tahmini yenile";
    }
    if (historyButton) historyButton.disabled = false;
  }
}

async function loadValidation(force = false) {
  const selectedDate = activeValidationDate || activeHistoricalDate || isoToday();
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
    renderQualityMetrics({});
    const body = $("validationTableBody");
    if (body) body.innerHTML = '<tr><td colspan="4">Karşılaştırma alınamadı.</td></tr>';
    const directionTimeline = $("validationDirectionTimeline");
    if (directionTimeline) {
      directionTimeline.innerHTML = '<div class="validation-chart-loading">Saatlik karşılaştırma alınamadı.</div>';
    }
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const validation = $("system-validation");
  const validationMount = $("validationMount");
  if (validation && validationMount) {
    validationMount.replaceWith(validation);
  }
  const historicalDate = $("historicalForecastDate");
  if (historicalDate) {
    historicalDate.max = isoToday();
    historicalDate.min = isoDaysAgo(365);
    historicalDate.value = isoToday();
  }
  const updateHistoricalDateNav = () => {
    const value = historicalDate?.value || "";
    const previous = $("historicalForecastPrev");
    const next = $("historicalForecastNext");
    if (previous) {
      previous.disabled = !value || value <= (historicalDate?.min || "");
    }
    if (next) {
      next.disabled = !value || value >= (historicalDate?.max || isoToday());
    }
  };
  const runHistoricalForecast = () => {
    const selectedDate = historicalDate?.value || "";
    if (!selectedDate) {
      showError("İncelemek için geçmiş bir tarih seçin.");
      return false;
    }
    if (selectedDate > isoToday()) {
      showError("Geçmiş tahmin testinde bugünden ileri tarih seçilemez.");
      return false;
    }
    activeHistoricalDate = selectedDate;
    setText(
      "historicalForecastHint",
      "Model seçilen günün gerçek yönünü görmeden, yalnızca önceki verilerle tahmin hazırlıyor…",
    );
    updateHistoricalDateNav();
    loadForecast(false);
    return true;
  };
  $("historicalForecastRun")?.addEventListener("click", runHistoricalForecast);
  $("historicalForecastPrev")?.addEventListener("click", () => {
    if (!historicalDate) return;
    const previousDate = shiftIsoDate(historicalDate.value, -1);
    if (!previousDate || previousDate < historicalDate.min) return;
    historicalDate.value = previousDate;
    updateHistoricalDateNav();
    runHistoricalForecast();
  });
  $("historicalForecastNext")?.addEventListener("click", () => {
    if (!historicalDate) return;
    const nextDate = shiftIsoDate(historicalDate.value, 1);
    if (!nextDate || nextDate > historicalDate.max) return;
    historicalDate.value = nextDate;
    updateHistoricalDateNav();
    runHistoricalForecast();
  });
  historicalDate?.addEventListener("change", updateHistoricalDateNav);
  updateHistoricalDateNav();
  $("historicalForecastLive")?.addEventListener("click", () => {
    activeHistoricalDate = "";
    setText(
      "historicalForecastHint",
      "Gerçek değer tahmin tamamlandıktan sonra karşılaştırılır.",
    );
    loadForecast(false);
  });
  $("forecastRefresh")?.addEventListener("click", () => loadForecast(true));
  $("validationHourPrev")?.addEventListener("click", () => moveValidationHours(-1));
  $("validationHourNext")?.addEventListener("click", () => moveValidationHours(1));
  $("validationHourLatest")?.addEventListener("click", () => focusLatestValidationHour(true));
  $("validationDirectionScroll")?.addEventListener(
    "scroll",
    updateValidationNavState,
    { passive: true },
  );
  window.addEventListener("resize", updateValidationNavState, { passive: true });
  loadForecast(false);
});
