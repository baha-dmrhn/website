const state = {
  data: null,
  loading: false,
  latestAvailableDate: null,
  activeTrendSeries: new Set(["uevm", "uecm"]),
};

if ("scrollRestoration" in history) {
  history.scrollRestoration = "manual";
}
window.scrollTo(0, 0);

const groupLabels = {
  renewable: "Yenilenebilir",
  thermal: "Termik",
  natural_gas: "Doğal gaz",
  other: "Diğer / Uluslararası",
};

const groupColors = {
  renewable: "#2d70ee",
  thermal: "#f97350",
  natural_gas: "#41b8b2",
  other: "#8b6bdc",
};

const trendSeriesDefinitions = [
  { key: "uevm", label: "UEVM", color: "#2d70ee" },
  { key: "uecm", label: "UEÇM", color: "#ff6847" },
  { key: "sun", label: "Güneş", color: "#e0a323" },
  { key: "wind", label: "Rüzgâr", color: "#6c8dfa" },
  { key: "hydro", label: "Hidroelektrik", color: "#0e9690" },
  { key: "thermal", label: "Termik", color: "#596a83" },
  { key: "naturalGas", label: "Doğal gaz", color: "#41b8b2" },
];
const trendSeriesKeys = trendSeriesDefinitions.map((series) => series.key);

const sourceCardDetails = {
  hydro: "Barajlı · Akarsu",
  thermal: "Kömür türleri · Asfaltit · Fuel-oil · LNG · Nafta",
};

const iconPaths = {
  sun: '<circle cx="12" cy="12" r="3.5"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9 7 7M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"/>',
  wind: '<path d="M3 8h11.5c2.8 0 2.8-4 0-4-1.3 0-2.1.7-2.5 1.5M3 12h16c3.1 0 3.1 4.5 0 4.5-1.4 0-2.2-.8-2.6-1.6M3 16h7"/>',
  hydro: '<path d="M12 2s6 7 6 12a6 6 0 0 1-12 0c0-5 6-12 6-12Z"/><path d="M8.7 15.2c.6 1.5 1.7 2.2 3.3 2.2"/>',
  thermal: '<path d="M5 21V9l4 3V8l4 4V5l6 4v12H5Z"/><path d="M8 16h2M14 16h2"/>',
  natural_gas: '<path d="M13.2 2.5c.7 4-2.2 5-3.2 7.6-.5 1.4-.1 2.6.8 3.3-.1-2 1.2-3.1 2.4-4.2 2.2 2.1 4.2 4.3 4.2 7.2A5.4 5.4 0 0 1 12 22a5.8 5.8 0 0 1-5.7-6c0-4.6 3.9-7.3 6.9-13.5Z"/>',
};

const elements = {
  form: document.querySelector("#dateForm"),
  start: document.querySelector("#startDate"),
  end: document.querySelector("#endDate"),
  presets: [...document.querySelectorAll(".preset")],
  status: document.querySelector("#connectionStatus"),
  period: document.querySelector("#periodLabel"),
  kpis: document.querySelector("#kpiGrid"),
  trend: document.querySelector("#trendChart"),
  coverage: document.querySelector("#coverageLabel"),
  balance: document.querySelector("#balanceVisual"),
  balanceMarker: document.querySelector("#balanceMarker"),
  sourceCards: document.querySelector("#sourceCards"),
  groupBars: document.querySelector("#groupBars"),
  donut: document.querySelector("#mixDonut"),
  mixLegend: document.querySelector("#mixLegend"),
  table: document.querySelector("#sourceTable"),
  exportButton: document.querySelector("#exportButton"),
  updated: document.querySelector("#updatedAt"),
  tooltip: document.querySelector("#chartTooltip"),
  toast: document.querySelector("#toast"),
  dataAlert: document.querySelector("#dataAlert"),
  dataAlertText: document.querySelector("#dataAlertText"),
  dataAlertClose: document.querySelector("#dataAlertClose"),
  trendSeriesControls: document.querySelector("#trendSeriesControls"),
  dailyReportButton: document.querySelector("#dailyReportButton"),
  monthlyReportButton: document.querySelector("#monthlyReportButton"),
  dailyReportPeriod: document.querySelector("#dailyReportPeriod"),
  monthlyReportPeriod: document.querySelector("#monthlyReportPeriod"),
};

function localISODate(value) {
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function setDefaultDates(days = 30) {
  const today = new Date();
  const end = new Date(today);
  const start = new Date(end);
  start.setDate(start.getDate() - (days - 1));
  elements.start.value = localISODate(start);
  elements.end.value = localISODate(end);
  elements.start.max = localISODate(today);
  elements.end.max = localISODate(today);
  elements.end.min = elements.start.value;
}

function quickReportRange(mode) {
  if (!state.latestAvailableDate) return null;
  const end = new Date(`${state.latestAvailableDate}T12:00:00`);
  const start = new Date(end);
  if (mode === "monthly") {
    start.setDate(1);
  }
  return {
    start: localISODate(start),
    end: localISODate(end),
  };
}

function updateQuickReportPeriods() {
  const daily = quickReportRange("daily");
  const monthly = quickReportRange("monthly");
  if (!daily || !monthly) return;

  elements.dailyReportPeriod.textContent = formatDate(daily.end);
  elements.monthlyReportPeriod.textContent =
    `${formatDate(monthly.start)} — ${formatDate(monthly.end)}`;
  elements.dailyReportButton.disabled = false;
  elements.monthlyReportButton.disabled = false;
}

function formatNumber(value, options = {}) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return new Intl.NumberFormat("tr-TR", {
    maximumFractionDigits: options.decimals ?? 0,
    notation: "standard",
    useGrouping: true,
    signDisplay: options.sign ? "always" : "auto",
  }).format(value);
}

function formatMWh(value) {
  return formatNumber(value);
}

function formatDate(value, includeTime = false) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("tr-TR", {
    day: "2-digit",
    month: "short",
    year: includeTime ? undefined : "numeric",
    hour: includeTime ? "2-digit" : undefined,
    minute: includeTime ? "2-digit" : undefined,
    timeZone: "Europe/Istanbul",
  }).format(new Date(value));
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => elements.toast.classList.remove("visible"), 3200);
}

function showDataAlert(message) {
  elements.dataAlertText.textContent = message;
  elements.dataAlert.classList.remove("hidden");
}

function hideDataAlert() {
  elements.dataAlert.classList.add("hidden");
}

function updateAvailabilityAlert() {
  if (
    state.latestAvailableDate &&
    elements.end.value &&
    elements.end.value > state.latestAvailableDate
  ) {
    showDataAlert(
      `Seçtiğiniz dönemin tamamı için EPİAŞ verisi henüz yok. EPİAŞ'ta veri bulunan son gün ${formatDate(state.latestAvailableDate)}.`,
    );
    return;
  }
  hideDataAlert();
}

function setLoading(loading) {
  state.loading = loading;
  const button = elements.form.querySelector("button");
  button.disabled = loading;
  button.querySelector("span").textContent = loading ? "Yükleniyor…" : "Veriyi getir";
  elements.start.disabled = loading;
  elements.end.disabled = loading;
  elements.presets.forEach((preset) => {
    preset.disabled = loading;
  });
}

async function loadDashboard() {
  if (state.loading) return;
  setLoading(true);
  elements.status.className = "connection";
  elements.status.querySelector("span").textContent = "Bağlanıyor";
  const params = new URLSearchParams({
    start: elements.start.value,
    end: elements.end.value,
  });
  const requestedPeriod = {
    start: elements.start.value,
    end: elements.end.value,
  };

  try {
    const response = await fetch(`/api/dashboard?${params.toString()}`, {
      headers: { Accept: "application/json" },
    });
    const payload = await response.json();
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) throw new Error(payload.error || "Veri alınamadı.");
    state.data = payload;
    render(payload, requestedPeriod);
  } catch (error) {
    elements.status.className = "connection error";
    elements.status.querySelector("span").textContent = "Bağlantı hatası";
    showToast(error.message || "Beklenmeyen bir hata oluştu.", true);
  } finally {
    setLoading(false);
  }
}

function render(data, requestedPeriod = null) {
  elements.status.className = "connection live";
  elements.status.querySelector("span").textContent = "EPİAŞ canlı";

  elements.start.value = data.period.start;
  elements.end.value = data.period.end;
  const discoveredLatestDate = data.meta.latestAvailableDate || data.period.end;
  if (
    !state.latestAvailableDate ||
    discoveredLatestDate > state.latestAvailableDate
  ) {
    state.latestAvailableDate = discoveredLatestDate;
  }
  updateQuickReportPeriods();
  const today = localISODate(new Date());
  elements.start.max = today;
  elements.end.max = today;
  elements.end.min = elements.start.value;
  elements.start.title = "En geç bugünün tarihi seçilebilir.";
  elements.end.title = elements.start.title;
  elements.period.textContent = `${formatDate(data.period.start)} — ${formatDate(data.period.end)}`;
  elements.updated.textContent = formatDate(data.meta.generatedAt, true);
  elements.coverage.textContent = `${data.period.comparableHours} / ${data.period.hours} SAAT EŞLEŞTİ`;

  if (data.meta.warning) {
    const requestedText = requestedPeriod
      ? `${formatDate(requestedPeriod.start)} — ${formatDate(requestedPeriod.end)}`
      : "Seçtiğiniz dönem";
    const availableStart = data.meta.availableStartDate || data.period.start;
    const availableEnd = data.meta.availableEndDate || data.period.end;
    showDataAlert(
      `${requestedText} döneminin tamamı için EPİAŞ verisi henüz yayımlanmadı. Kullanılabilir ${formatDate(availableStart)} — ${formatDate(availableEnd)} verileri gösteriliyor.`,
    );
  } else {
    hideDataAlert();
  }

  renderKpis(data.summary, data.period);
  renderTrend(data.series);
  renderBalance(data.summary);
  renderSourceCards(data.sourceCards);
  renderGroups(data.groups);
  renderDonut(data.groups);
  renderTable(data.sources);
}

function renderKpis(summary, period) {
  const deviation = summary.deviationPct;
  const kpis = [
    {
      index: "01",
      label: "Toplam UEVM",
      value: formatMWh(summary.uevmTotal),
      unit: "MWh · SİSTEME VERİŞ",
    },
    {
      index: "02",
      label: "Toplam UEÇM",
      value: formatMWh(summary.uecmTotal),
      unit: "MWh · SİSTEMDEN ÇEKİŞ",
    },
    {
      index: "03",
      label: "Net fark",
      value: formatNumber(summary.difference, { sign: true }),
      unit: `UEVM − UEÇM · ${period.comparableHours} ORTAK SAAT · MWh`,
      accent: true,
    },
    {
      index: "04",
      label: "Yüzdesel sapma",
      value: deviation === null ? "—" : `${formatNumber(deviation, { decimals: 2, sign: true })}%`,
      unit: `${period.comparableHours} ORTAK SAAT · SİSTEM SEVİYESİ`,
    },
    {
      index: "05",
      label: "Saatlik ortalama",
      value: formatMWh(summary.hourlyAverage),
      unit: `${period.uevmHours} UEVM SAATİ · MWh`,
    },
  ];

  elements.kpis.innerHTML = kpis
    .map(
      (kpi) => `
        <article class="kpi-card">
          <div class="kpi-top"><span>${kpi.label}</span><span>${kpi.index}</span></div>
          <strong class="kpi-number">${kpi.value}</strong>
          <span class="kpi-unit">${kpi.unit}</span>
          ${kpi.accent ? '<i class="kpi-accent" aria-hidden="true"></i>' : ""}
        </article>`,
    )
    .join("");
}

function downsample(series, maxPoints = 220) {
  if (series.length <= maxPoints) return series;
  const bucket = Math.ceil(series.length / maxPoints);
  const sampled = [];
  for (let i = 0; i < series.length; i += bucket) {
    const values = series.slice(i, i + bucket);
    const average = (key) => {
      const valid = values
        .map((row) => row[key])
        .filter((value) => value !== null && value !== undefined);
      return valid.length ? valid.reduce((sum, v) => sum + v, 0) / valid.length : null;
    };
    const row = {
      timestamp: values[Math.floor(values.length / 2)].timestamp,
    };
    trendSeriesKeys.forEach((key) => {
      row[key] = average(key);
    });
    sampled.push(row);
  }
  return sampled;
}

function renderTrend(rawSeries) {
  const series = downsample(rawSeries);
  const activeSeries = trendSeriesDefinitions.filter((definition) =>
    state.activeTrendSeries.has(definition.key),
  );
  if (!activeSeries.length) {
    elements.trend.innerHTML =
      '<div class="chart-loading">Grafikte göstermek için en az bir seri seçin.</div>';
    return;
  }

  const width = 900;
  const height = 350;
  const margin = { top: 18, right: 18, bottom: 38, left: 64 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const values = series
    .flatMap((row) => activeSeries.map((definition) => row[definition.key]))
    .filter((value) => value !== null && value !== undefined);
  if (!values.length) {
    elements.trend.innerHTML = '<div class="chart-loading">Bu aralıkta veri bulunamadı.</div>';
    return;
  }

  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const padding = Math.max((rawMax - rawMin) * 0.12, rawMax * 0.03, 1);
  const min = Math.max(0, rawMin - padding);
  const max = rawMax + padding;
  const x = (index) => margin.left + (index / Math.max(series.length - 1, 1)) * innerWidth;
  const y = (value) => margin.top + (1 - (value - min) / Math.max(max - min, 1)) * innerHeight;
  const path = (key) => {
    let started = false;
    return series
      .map((row, index) => {
        if (row[key] === null || row[key] === undefined) {
          started = false;
          return "";
        }
        const command = started ? "L" : "M";
        started = true;
        return `${command}${x(index).toFixed(2)},${y(row[key]).toFixed(2)}`;
      })
      .join(" ");
  };

  let areaMarkup = "";
  if (state.activeTrendSeries.has("uevm")) {
    const uevmPath = path("uevm");
    let lastIndex = series.length - 1;
    while (
      lastIndex > 0 &&
      (series[lastIndex].uevm === null || series[lastIndex].uevm === undefined)
    ) {
      lastIndex -= 1;
    }
    if (uevmPath) {
      const areaPath = `${uevmPath} L${x(Math.max(lastIndex, 0))},${margin.top + innerHeight} L${margin.left},${margin.top + innerHeight} Z`;
      areaMarkup = `<path class="uevm-area" d="${areaPath}"/>`;
    }
  }

  const yTicks = Array.from({ length: 5 }, (_, index) => min + ((max - min) * index) / 4).reverse();
  const xTickIndexes = [...new Set([0, Math.floor((series.length - 1) / 2), series.length - 1])];
  const lineMarkup = activeSeries
    .map(
      (definition) =>
        `<path class="trend-series-line" data-series="${definition.key}" style="stroke:${definition.color}" d="${path(definition.key)}"/>`,
    )
    .join("");
  const pointMarkup = activeSeries
    .map(
      (definition) =>
        `<circle class="chart-point" data-series="${definition.key}" r="5" fill="${definition.color}"/>`,
    )
    .join("");

  elements.trend.innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${activeSeries.map((definition) => definition.label).join(", ")} saatlik çizgi grafiği" preserveAspectRatio="none">
      <defs>
        <linearGradient id="uevmGradient" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#2d70ee" stop-opacity=".18"/>
          <stop offset="100%" stop-color="#2d70ee" stop-opacity="0"/>
        </linearGradient>
      </defs>
      ${yTicks
        .map(
          (value) => `
            <line class="chart-gridline" x1="${margin.left}" y1="${y(value)}" x2="${width - margin.right}" y2="${y(value)}"/>
            <text class="chart-axis-label" x="${margin.left - 10}" y="${y(value) + 3}" text-anchor="end">${formatNumber(value)}</text>`,
        )
        .join("")}
      ${xTickIndexes
        .map(
          (index) => `
            <text class="chart-axis-label" x="${x(index)}" y="${height - 10}" text-anchor="${index === 0 ? "start" : index === series.length - 1 ? "end" : "middle"}">${formatDate(series[index].timestamp)}</text>`,
        )
        .join("")}
      ${areaMarkup}
      ${lineMarkup}
      <line class="chart-crosshair" id="crosshair" x1="0" y1="${margin.top}" x2="0" y2="${margin.top + innerHeight}"/>
      ${pointMarkup}
      <rect class="chart-hitbox" x="${margin.left}" y="${margin.top}" width="${innerWidth}" height="${innerHeight}"/>
    </svg>`;

  const svg = elements.trend.querySelector("svg");
  const hitbox = svg.querySelector(".chart-hitbox");
  const crosshair = svg.querySelector("#crosshair");
  const points = new Map(
    activeSeries.map((definition) => [
      definition.key,
      svg.querySelector(`.chart-point[data-series="${definition.key}"]`),
    ]),
  );

  hitbox.addEventListener("pointermove", (event) => {
    const rect = svg.getBoundingClientRect();
    const svgX = ((event.clientX - rect.left) / rect.width) * width;
    const index = Math.max(0, Math.min(series.length - 1, Math.round(((svgX - margin.left) / innerWidth) * (series.length - 1))));
    const row = series[index];
    const pointX = x(index);
    crosshair.setAttribute("x1", pointX);
    crosshair.setAttribute("x2", pointX);
    crosshair.style.opacity = "1";
    activeSeries.forEach((definition) => {
      const value = row[definition.key];
      setPoint(
        points.get(definition.key),
        pointX,
        value === null || value === undefined ? null : y(value),
      );
    });
    elements.tooltip.innerHTML = `
      <strong>${formatDate(row.timestamp, true)}</strong>
      ${activeSeries
        .map(
          (definition) =>
            `<span><i class="tooltip-series-dot" style="background:${definition.color}"></i>${definition.label} · ${formatMWh(row[definition.key])} MWh</span>`,
        )
        .join("")}`;
    elements.tooltip.style.left = `${Math.min(event.clientX, window.innerWidth - 205)}px`;
    elements.tooltip.style.top = `${Math.max(event.clientY, 75)}px`;
    elements.tooltip.classList.add("visible");
  });

  hitbox.addEventListener("pointerleave", () => {
    crosshair.style.opacity = "0";
    points.forEach((point) => {
      point.style.opacity = "0";
    });
    elements.tooltip.classList.remove("visible");
  });
}

function setPoint(element, x, y) {
  if (y === null) {
    element.style.opacity = "0";
    return;
  }
  element.setAttribute("cx", x);
  element.setAttribute("cy", y);
  element.style.opacity = "1";
}

function renderBalance(summary) {
  elements.balance.innerHTML = `
    <span class="balance-label">UEVM − UEÇM</span>
    <strong>${formatNumber(summary.difference, { sign: true })}</strong>
    <small>MWh · ${summary.deviationPct === null ? "—" : `${formatNumber(summary.deviationPct, { decimals: 2, sign: true })}%`}</small>`;
  const bounded = Math.max(-10, Math.min(10, summary.deviationPct || 0));
  elements.balanceMarker.style.left = `${50 + bounded * 4}%`;
}

function renderSourceCards(cards) {
  elements.sourceCards.innerHTML = cards
    .map(
      (card, index) => `
        <article class="source-card">
          <div class="source-card-head">
            <span>0${index + 1} · ${card.label}</span>
            <i class="source-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24">${iconPaths[card.id]}</svg>
            </i>
          </div>
          ${
            sourceCardDetails[card.id]
              ? `<small class="source-card-components">${sourceCardDetails[card.id]}</small>`
              : ""
          }
          <div class="source-card-value">
            <strong>${formatMWh(card.value)}</strong>
            <span>MWh · TOPLAM UEVM</span>
          </div>
          <span class="source-share">%${formatNumber(card.share, { decimals: 1 })}</span>
        </article>`,
    )
    .join("");
}

function renderGroups(groups) {
  elements.groupBars.innerHTML = groups
    .map(
      (group) => `
        <div class="group-row">
          <div class="group-row-head">
            <strong>${group.label}</strong>
            <span>${formatMWh(group.value)} MWh · %${formatNumber(group.share, { decimals: 1 })}</span>
          </div>
          <div class="group-track"><i style="width:${Math.max(0, Math.min(group.share, 100))}%"></i></div>
          <small class="group-sources">${group.sources.join(" · ")}</small>
        </div>`,
    )
    .join("");
}

function renderDonut(groups) {
  const sortedGroups = [...groups].sort(
    (a, b) => Number(b.share || 0) - Number(a.share || 0),
  );
  const total = groups.reduce((sum, group) => sum + group.value, 0) || 1;
  let cursor = 0;
  const stops = sortedGroups.map((group) => {
    const start = cursor;
    cursor += (group.value / total) * 100;
    return `${groupColors[group.id]} ${start}% ${cursor}%`;
  });
  elements.donut.style.background = `conic-gradient(${stops.join(",")})`;
  elements.donut.querySelector("strong").textContent = formatMWh(total);
  elements.mixLegend.innerHTML = sortedGroups
    .map(
      (group) => `
        <div class="donut-legend-row">
          <i style="background:${groupColors[group.id]}"></i>
          <span>${group.label}</span>
          <strong>%${formatNumber((group.value / total) * 100, { decimals: 1 })}</strong>
        </div>`,
    )
    .join("");
}

function renderTable(sources) {
  elements.table.innerHTML = sources
    .map(
      (source) => `
        <tr>
          <td><span class="source-name"><i style="background:${groupColors[source.group]}"></i>${source.label}</span></td>
          <td>${groupLabels[source.group]}</td>
          <td class="numeric">${formatMWh(source.value)} MWh</td>
          <td class="numeric">%${formatNumber(source.share, { decimals: 2 })}</td>
        </tr>`,
    )
    .join("");
}

async function downloadXlsx(range, button, idleLabel, successMessage) {
  if (!range) return;
  const buttonLabel = button.querySelector("span");
  const params = new URLSearchParams({
    start: range.start,
    end: range.end,
  });

  button.disabled = true;
  buttonLabel.textContent = "Hazırlanıyor…";
  try {
    const response = await fetch(`/api/export.xlsx?${params.toString()}`);
    if (response.status === 401) {
      window.location.replace("/login");
      return;
    }
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || "Excel dosyası hazırlanamadı.");
    }

    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const filename =
      filenameMatch?.[1] ||
      `baha-uretim-epias-${range.start}-${range.end}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    showToast(successMessage);
  } catch (error) {
    showToast(error.message || "Excel dosyası hazırlanamadı.", true);
  } finally {
    button.disabled = false;
    buttonLabel.textContent = idleLabel;
  }
}

function exportXlsx() {
  if (!state.data) return;
  downloadXlsx(
    {
      start: state.data.period.start,
      end: state.data.period.end,
    },
    elements.exportButton,
    "XLSX indir",
    "Seçili dönem Excel raporu indirildi.",
  );
}

function exportQuickReport(mode) {
  const range = quickReportRange(mode);
  if (!range) {
    showToast("Rapor için yayımlanmış veri henüz belirlenemedi.", true);
    return;
  }
  const isMonthly = mode === "monthly";
  downloadXlsx(
    range,
    isMonthly ? elements.monthlyReportButton : elements.dailyReportButton,
    isMonthly ? "Aylık XLSX" : "Günlük XLSX",
    isMonthly ? "Aylık Excel raporu indirildi." : "Günlük Excel raporu indirildi.",
  );
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!elements.form.reportValidity()) {
    showToast("En geç bugünün tarihi seçilebilir.", true);
    return;
  }
  if (elements.start.value > elements.end.value) {
    showToast("Başlangıç tarihi bitiş tarihinden sonra olamaz.", true);
    return;
  }
  elements.presets.forEach((button) => button.classList.remove("active"));
  loadDashboard();
});

elements.start.addEventListener("change", () => {
  if (
    elements.start.value &&
    (!elements.end.value || elements.start.value > elements.end.value)
  ) {
    elements.end.value = elements.start.value;
  }
  elements.end.min = elements.start.value;
  elements.presets.forEach((button) => button.classList.remove("active"));
  updateAvailabilityAlert();
});

elements.end.addEventListener("change", () => {
  elements.presets.forEach((button) => button.classList.remove("active"));
  updateAvailabilityAlert();
});

elements.presets.forEach((button) => {
  button.addEventListener("click", () => {
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - (Number(button.dataset.range) - 1));
    const startValue = localISODate(start);
    const endValue = localISODate(end);
    elements.presets.forEach((item) => item.classList.toggle("active", item === button));
    elements.start.value = startValue;
    elements.end.value = endValue;
    elements.end.min = elements.start.value;

    if (
      state.data?.period.start === startValue &&
      state.data?.period.end === endValue
    ) {
      showToast(
        `${formatDate(startValue)} — ${formatDate(endValue)} zaten ekranda gösteriliyor.`,
      );
      return;
    }
    loadDashboard();
  });
});

elements.trendSeriesControls.addEventListener("click", (event) => {
  const button = event.target.closest(".series-toggle");
  if (!button) return;
  const key = button.dataset.series;
  const isActive = state.activeTrendSeries.has(key);
  if (isActive && state.activeTrendSeries.size === 1) {
    showToast("Grafikte en az bir çizgi açık kalmalı.", true);
    return;
  }

  if (isActive) {
    state.activeTrendSeries.delete(key);
  } else {
    state.activeTrendSeries.add(key);
  }
  const willBeActive = !isActive;
  button.classList.toggle("active", willBeActive);
  button.setAttribute("aria-pressed", String(willBeActive));
  elements.tooltip.classList.remove("visible");
  if (state.data) renderTrend(state.data.series);
});

document.querySelector("#methodButton").addEventListener("click", () => {
  showToast(state.data?.meta.methodology || "Metodoloji verisi yükleniyor.");
});

elements.exportButton.addEventListener("click", exportXlsx);
elements.dataAlertClose.addEventListener("click", hideDataAlert);
elements.dailyReportButton.addEventListener("click", () => exportQuickReport("daily"));
elements.monthlyReportButton.addEventListener("click", () => exportQuickReport("monthly"));

document.querySelector("#logoutButton").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", {
      method: "POST",
      headers: { Accept: "application/json" },
    });
  } finally {
    window.location.replace("/login");
  }
});

window.addEventListener("resize", () => {
  if (state.data) renderTrend(state.data.series);
});

window.addEventListener("pageshow", () => window.scrollTo(0, 0));

setDefaultDates();
loadDashboard();
