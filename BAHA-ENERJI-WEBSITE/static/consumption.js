(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const number = new Intl.NumberFormat("tr-TR", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  const integer = new Intl.NumberFormat("tr-TR", {maximumFractionDigits: 0});
  const state = {
    data: null,
    comparison: null,
    chart: null,
    sequence: 0,
  };

  function todayTR() {
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: "Europe/Istanbul",
    }).format(new Date());
  }

  function displayDate(value) {
    return String(value || "").split("-").reverse().join(".");
  }

  function shiftedDate(value, days) {
    const date = new Date(`${value}T12:00:00`);
    date.setDate(date.getDate() + days);
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
  }

  function finiteValue(value) {
    return value !== null && value !== "" && Number.isFinite(Number(value));
  }

  function format(value) {
    return finiteValue(value) ? number.format(Number(value)) : "—";
  }

  function showAlert(message = "") {
    const alert = $("consumptionAlert");
    alert.textContent = message;
    alert.hidden = !message;
  }

  async function api(path) {
    let response;
    try {
      response = await fetch(path, {
        credentials: "same-origin",
        cache: "no-store",
      });
    } catch {
      throw new Error("İnternet bağlantısı yok veya sunucuya ulaşılamıyor.");
    }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.error || "Tüketim verileri alınamadı.");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function constrainDate(announce = false) {
    const input = $("consumptionDate");
    const today = todayTR();
    input.max = today;
    let corrected = false;
    if (!input.value || input.value > today) {
      input.value = today;
      corrected = true;
    }
    const atToday = input.value >= today;
    $("consumptionNext").disabled = atToday;
    $("consumptionNext").setAttribute("aria-disabled", String(atToday));
    if (corrected && announce) {
      showAlert("Bugünden ileri bir tarih seçilemez.");
    }
    return corrected;
  }

  function comparisonDate() {
    const mode = $("consumptionCompare").value;
    if (mode === "none") return null;
    return shiftedDate($("consumptionDate").value, mode === "week" ? -7 : -1);
  }

  function comparisonLabel() {
    return $("consumptionCompare").value === "week"
      ? "7 gün önce"
      : "Önceki gün";
  }

  function setDelta(node, current, previous, suffix = "") {
    node.className = "";
    if (!finiteValue(current) || !finiteValue(previous)) {
      node.textContent = "Karşılaştırma verisi yok";
      return;
    }
    const difference = Number(current) - Number(previous);
    const ratio = Number(previous) ? difference / Number(previous) * 100 : null;
    node.textContent = Number.isFinite(ratio)
      ? `${difference >= 0 ? "↑" : "↓"} %${number.format(Math.abs(ratio))} ${suffix || comparisonLabel()}`
      : `${difference >= 0 ? "+" : "−"}${number.format(Math.abs(difference))} MWh`;
    node.className = difference > 0 ? "positive" : difference < 0 ? "negative" : "";
  }

  function renderSummary() {
    const summary = state.data?.summary || {};
    const previous = state.comparison?.summary || {};
    $("latestConsumption").textContent = format(summary.latest);
    $("latestConsumptionMeta").textContent = summary.latestHour
      ? `${summary.latestHour} saatindeki son yayımlanan değer`
      : "Bu tarih için veri henüz yayımlanmadı";
    const hourlyChange = finiteValue(summary.latestChange) ? Number(summary.latestChange) : null;
    const hourlyPercent = finiteValue(summary.latestChangePercent) ? Number(summary.latestChangePercent) : null;
    const hourlyDelta = $("latestConsumptionDelta");
    hourlyDelta.className = hourlyChange !== null
      ? hourlyChange > 0 ? "positive" : hourlyChange < 0 ? "negative" : ""
      : "";
    hourlyDelta.textContent = hourlyChange !== null
      ? `${hourlyChange >= 0 ? "↑" : "↓"} ${number.format(Math.abs(hourlyChange))} MWh${hourlyPercent !== null ? ` · %${number.format(Math.abs(hourlyPercent))}` : ""} önceki saate göre`
      : "Önceki saat karşılaştırması yok";

    $("averageConsumption").textContent = format(summary.average);
    setDelta(
      $("averageConsumptionDelta"),
      summary.average,
      previous.average,
    );

    $("maximumConsumption").textContent = format(summary.maximum);
    $("maximumConsumptionMeta").textContent = summary.maximumHour
      ? `Zirve saati ${summary.maximumHour}`
      : "Zirve değeri bekleniyor";
    $("minimumConsumptionMeta").textContent = finiteValue(summary.minimum)
      ? `En düşük ${format(summary.minimum)} MWh · ${summary.minimumHour || "—"}`
      : "En düşük değer bekleniyor";

    $("availableHours").textContent = summary.availableHours ?? "—";
    $("missingHours").textContent = `${summary.missingHours ?? 24} saat veri bekleniyor`;
  }

  function renderInsight() {
    const summary = state.data?.summary || {};
    const previous = state.comparison?.summary || {};
    if (!summary.availableHours) {
      $("consumptionInsight").textContent = "Bu tarih için EPİAŞ gerçek zamanlı tüketim verisi henüz yayımlanmadı.";
      return;
    }
    let comparison = "";
    if (finiteValue(previous.average) && Number(previous.average)) {
      const change = (Number(summary.average) - Number(previous.average)) / Number(previous.average) * 100;
      comparison = ` Ortalama tüketim ${comparisonLabel().toLocaleLowerCase("tr-TR")} göre %${number.format(Math.abs(change))} ${change >= 0 ? "daha yüksek" : "daha düşük"}.`;
    }
    $("consumptionInsight").textContent =
      `${displayDate(state.data.date)} tarihinde ${summary.availableHours} saatlik veri yayımlandı. ` +
      `Son değer ${summary.latestHour || "—"} saatinde ${format(summary.latest)} MWh, ` +
      `günün zirvesi ${summary.maximumHour || "—"} saatinde ${format(summary.maximum)} MWh oldu.${comparison} ` +
      "Gerçek zamanlı tüketim verisi EPİAŞ tarafından yaklaşık iki saat geriden yayımlanır.";
  }

  function renderChart() {
    if (state.chart) state.chart.destroy();
    const rows = state.data?.rows || [];
    const comparisonRows = state.comparison?.rows || [];
    const series = [
      {
        name: "Seçili gün · MWh",
        data: rows.map((row) => row.consumption ?? undefined),
      },
    ];
    if (comparisonRows.length) {
      series.push({
        name: `${comparisonLabel()} · MWh`,
        data: comparisonRows.map((row) => row.consumption ?? undefined),
      });
    }
    $("comparisonLegend").hidden = !comparisonRows.length;
    $("comparisonLegend").textContent = comparisonLabel();
    state.chart = new window.ApexCharts($("consumptionChart"), {
      chart: {type: "area", height: 340, toolbar: {show: false}},
      series,
      colors: ["#2d70ee", "#8b6bdc"],
      xaxis: {categories: rows.map((row) => row.time)},
      legend: {show: comparisonRows.length > 0},
      noData: {text: "Bu tarih için tüketim verisi bulunamadı"},
    });
    state.chart.render();
  }

  function renderCoverage() {
    const rows = state.data?.rows || [];
    const available = rows.filter((row) => row.consumption !== null).length;
    const latestHour = state.data?.summary?.latestHour;
    $("coverageMeta").textContent = `${available} saat yayımlandı · ${24 - available} saat bekleniyor`;
    $("consumptionHours").innerHTML = rows.map((row) => {
      const hasData = row.consumption !== null;
      const latest = hasData && row.time === latestHour;
      return `<div class="consumption-hour${hasData ? " available" : ""}${latest ? " latest" : ""}" title="${row.time} · ${hasData ? `${format(row.consumption)} MWh` : "Veri bekleniyor"}"><strong>${row.time.slice(0, 2)}</strong><span>${hasData ? integer.format(row.consumption) : "Bekleniyor"}</span></div>`;
    }).join("");
  }

  function renderTable() {
    const rows = state.data?.rows || [];
    const comparisonByHour = new Map(
      (state.comparison?.rows || []).map((row) => [row.hour, row]),
    );
    const body = $("consumptionTableBody");
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="5">Bu tarih için veri bulunamadı.</td></tr>';
      return;
    }
    body.innerHTML = rows.map((row) => {
      const previous = comparisonByHour.get(row.hour)?.consumption;
      const current = row.consumption;
      const difference = finiteValue(current) && finiteValue(previous)
        ? Number(current) - Number(previous)
        : null;
      const deltaClass = difference === null ? "neutral" : difference > 0 ? "positive" : difference < 0 ? "negative" : "neutral";
      const status = current === null
        ? '<span class="consumption-state missing">Veri bekleniyor</span>'
        : '<span class="consumption-state">Yayımlandı</span>';
      return `<tr><td>${row.time}</td><td>${format(current)}</td><td>${format(previous)}</td><td><span class="consumption-delta ${deltaClass}">${difference === null ? "—" : `${difference >= 0 ? "+" : "−"}${format(Math.abs(difference))}`}</span></td><td>${status}</td></tr>`;
    }).join("");
  }

  function render() {
    renderSummary();
    renderInsight();
    renderChart();
    renderCoverage();
    renderTable();
    const summary = state.data?.summary || {};
    $("status").textContent = `EPİAŞ · ${summary.availableHours || 0}/24 saat yayımlandı`;
    $("consumptionXlsx").disabled = !summary.availableHours;
    const updated = new Date(state.data.updatedAt);
    const updateLabel = updated.toLocaleString("tr-TR", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
    const footer = $("consumptionFooterUpdated");
    if (footer) footer.textContent = updateLabel;
  }

  async function loadData(force = false) {
    constrainDate();
    const sequence = ++state.sequence;
    const date = $("consumptionDate").value;
    const compare = comparisonDate();
    const refresh = force ? "&refresh=1" : "";
    $("consumptionRefresh").disabled = true;
    $("consumptionXlsx").disabled = true;
    $("status").textContent = "Veriler yükleniyor…";
    showAlert();
    try {
      const data = await api(`/api/data?date=${encodeURIComponent(date)}${refresh}`);
      const comparison = compare
        ? await api(`/api/data?date=${encodeURIComponent(compare)}${refresh}`).catch(() => null)
        : null;
      if (sequence !== state.sequence) return;
      state.data = data;
      state.comparison = comparison;
      render();
    } catch (error) {
      if (sequence !== state.sequence) return;
      if (error.status === 401) {
        window.location.replace("/login");
        return;
      }
      state.data = null;
      state.comparison = null;
      $("status").textContent = "Veri alınamadı";
      showAlert(error.message || "Tüketim verileri alınamadı.");
    } finally {
      if (sequence === state.sequence) $("consumptionRefresh").disabled = false;
    }
  }

  function shiftSelection(days) {
    const current = $("consumptionDate").value || todayTR();
    const target = shiftedDate(current, days);
    const limited = target > todayTR() ? todayTR() : target;
    if (limited === current) {
      constrainDate();
      return;
    }
    $("consumptionDate").value = limited;
    constrainDate();
    loadData();
  }

  $("consumptionDate").value = todayTR();
  constrainDate();
  $("consumptionDate").addEventListener("change", () => {
    constrainDate(true);
    loadData();
  });
  $("consumptionPrev").addEventListener("click", () => shiftSelection(-1));
  $("consumptionNext").addEventListener("click", () => shiftSelection(1));
  $("consumptionToday").addEventListener("click", () => {
    $("consumptionDate").value = todayTR();
    constrainDate();
    loadData();
  });
  $("consumptionCompare").addEventListener("change", () => loadData());
  $("consumptionRefresh").addEventListener("click", () => loadData(true));
  $("consumptionXlsx").addEventListener("click", () => {
    if (!state.data?.summary?.availableHours) return;
    window.location.href = `/api/export.xlsx?date=${encodeURIComponent(state.data.date)}`;
  });
  window.addEventListener("baha:themechange", () => {
    if (state.data) renderChart();
  });
  window.setInterval(() => {
    if (!document.hidden && $("consumptionDate").value === todayTR()) {
      loadData(true);
    }
  }, 300000);
  loadData();
})();
