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
    forecast: null,
    chart: null,
    forecastChart: null,
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

  function setConnectionState(state = "live", detail = "") {
    document.body.dataset.epiasState = state;
    document.body.dataset.epiasDetail = detail;
    window.dispatchEvent(
      new CustomEvent("baha:connectionstate", {
        detail: {state, message: detail},
      }),
    );
  }

  function clearConsumptionDashboard(message = "Tüketim verileri alınamadı.") {
    state.data = null;
    state.comparison = null;
    state.forecast = null;
    renderSummary();
    if (state.chart) {
      state.chart.destroy();
      state.chart = null;
    }
    if (state.forecastChart) {
      state.forecastChart.destroy();
      state.forecastChart = null;
    }
    const chartEmpty = document.createElement("div");
    chartEmpty.className = "chart-loading";
    chartEmpty.textContent = message;
    $("consumptionChart").replaceChildren(chartEmpty);
    const forecastEmpty = document.createElement("div");
    forecastEmpty.className = "chart-loading";
    forecastEmpty.textContent = message;
    $("consumptionForecastChart").replaceChildren(forecastEmpty);
    $("forecastDate").textContent = "—";
    $("forecastAverage").textContent = "—";
    $("forecastMaximum").textContent = "—";
    $("forecastMaximumHour").textContent = "Saat bekleniyor";
    $("forecastError").textContent = "—";
    $("forecastCoverage").textContent = "Karşılaştırma yok";
    $("forecastConfidence").textContent = "Model hazırlanamadı";
    $("forecastTrainingMeta").textContent = "Geçmiş veri doğrulanamadı";
    $("forecastMethodNote").textContent = message;
    $("consumptionInsight").textContent = message;
    $("coverageMeta").textContent = "Veri durumu doğrulanamadı";
    $("consumptionHours").replaceChildren();
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = message;
    row.append(cell);
    $("consumptionTableBody").replaceChildren(row);
    $("consumptionXlsx").disabled = true;
    const footer = $("consumptionFooterUpdated");
    if (footer) footer.textContent = "—";
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
      xaxis: {
        categories: rows.map((row) => row.time),
        labels: {step: 3, fontSize: "9", compactFontSize: "7.2"},
      },
      legend: {show: comparisonRows.length > 0},
      noData: {text: "Bu tarih için tüketim verisi bulunamadı"},
    });
    state.chart.render();
  }

  function renderForecast() {
    if (state.forecastChart) {
      state.forecastChart.destroy();
      state.forecastChart = null;
    }
    const forecast = state.forecast;
    const summary = forecast?.summary || {};
    const rows = forecast?.rows || [];
    $("forecastDate").textContent = forecast?.date ? displayDate(forecast.date) : "—";
    $("forecastAverage").textContent = format(summary.average);
    $("forecastMaximum").textContent = format(summary.maximum);
    $("forecastMaximumHour").textContent = summary.maximumHour
      ? `${summary.maximumHour} · MWh`
      : "Saat bekleniyor";
    $("forecastConfidence").textContent = summary.trainingDays
      ? `${String(summary.confidence || "düşük").toLocaleUpperCase("tr-TR")} GÜVEN · ${summary.trainingDays} gün`
      : "Yeterli geçmiş veri yok";
    $("forecastError").textContent = finiteValue(summary.meanAbsolutePercentageError)
      ? `%${number.format(summary.meanAbsolutePercentageError)}`
      : "Bekleniyor";
    $("forecastCoverage").textContent = summary.comparedHours
      ? `${summary.comparedHours} gerçekleşen saat · ort. mutlak hata ${format(summary.meanAbsoluteError)} MWh`
      : "Gerçek tüketim yayımlandıkça hesaplanır";
    $("forecastTrainingMeta").textContent = summary.forecastHours
      ? `${summary.forecastHours} saat tahmin · ${forecast.method || "Ağırlıklı saat profili"}`
      : "Tahmin için yeterli geçmiş tüketim verisi bulunamadı";
    $("forecastMethodNote").textContent = forecast?.methodNote || "Tahmin verisi hazırlanamadı.";

    const hasActual = rows.some((row) => finiteValue(row.actual));
    $("forecastActualLegend").hidden = !hasActual;
    if (!rows.length || !rows.some((row) => finiteValue(row.forecast))) {
      const empty = document.createElement("div");
      empty.className = "chart-loading";
      empty.textContent = "Saatlik tüketim tahmini için yeterli veri bulunamadı.";
      $("consumptionForecastChart").replaceChildren(empty);
      return;
    }
    const series = [{
      name: "Tahmin · MWh",
      data: rows.map((row) => finiteValue(row.forecast) ? Number(row.forecast) : undefined),
    }];
    if (hasActual) {
      series.push({
        name: "Gerçekleşen · MWh",
        data: rows.map((row) => finiteValue(row.actual) ? Number(row.actual) : undefined),
      });
    }
    state.forecastChart = new window.ApexCharts($("consumptionForecastChart"), {
      chart: {type: "area", height: 330, toolbar: {show: false}},
      series,
      colors: ["#f09a3e", "#2db77c"],
      stroke: {width: [3, 3], dashArray: [7, 0]},
      xaxis: {
        categories: rows.map((row) => row.time),
        labels: {step: 3, fontSize: "9", compactFontSize: "7.2"},
      },
      legend: {show: hasActual},
      noData: {text: "Tüketim tahmini bulunamadı"},
    });
    state.forecastChart.render();
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
    renderForecast();
    renderCoverage();
    renderTable();
    const summary = state.data?.summary || {};
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
    showAlert();
    setConnectionState("loading", "Tüketim verileri alınıyor");
    try {
      const data = await api(`/api/data?date=${encodeURIComponent(date)}${refresh}`);
      let comparisonFailed = false;
      let forecastFailed = false;
      const [comparison, forecast] = await Promise.all([
        compare
          ? api(`/api/data?date=${encodeURIComponent(compare)}${refresh}`).catch(() => {
              comparisonFailed = true;
              return null;
            })
          : Promise.resolve(null),
        api(`/api/forecast?baseDate=${encodeURIComponent(date)}${refresh}`).catch(() => {
          forecastFailed = true;
          return null;
        }),
      ]);
      if (sequence !== state.sequence) return;
      state.data = data;
      state.comparison = comparison;
      state.forecast = forecast;
      render();
      window.BahaTracking?.publish({
        module: "tuketim",
        date,
        latest: data.summary?.latest,
        average: data.summary?.average,
        availableHours: data.summary?.availableHours,
      });
      const hasData = Number(data.summary?.availableHours || 0) > 0;
      if (!hasData || comparisonFailed || forecastFailed) {
        setConnectionState(
          "warning",
          !hasData
            ? "Seçilen tarih için tüketim verisi henüz yayımlanmadı"
            : comparisonFailed
              ? "Karşılaştırma verisi alınamadı"
              : "Tüketim tahmini hazırlanamadı",
        );
      } else {
        setConnectionState("live", "Tüketim verisi doğrulandı");
      }
    } catch (error) {
      if (sequence !== state.sequence) return;
      if (error.status === 401) {
        window.location.replace("/login");
        return;
      }
      const message = error.message || "Tüketim verileri alınamadı.";
      clearConsumptionDashboard(message);
      setConnectionState("error", message);
      showAlert(message);
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
    if (state.data) {
      renderChart();
      renderForecast();
    }
  });
  window.setInterval(() => {
    if (!document.hidden && $("consumptionDate").value === todayTR()) {
      loadData(true);
    }
  }, 300000);
  loadData();
})();
