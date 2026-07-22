(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const slides = [...document.querySelectorAll(".tv-slide")];
  const navButtons = [...document.querySelectorAll("#tvSlideNav button")];
  const ROTATE_MS = 15000;
  let currentSlide = 0;
  let paused = false;
  let rotateTimer = 0;

  const todayTR = () => new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Istanbul" }).format(new Date());
  const fmt = (value, digits = 2) => Number.isFinite(Number(value)) ? new Intl.NumberFormat("tr-TR", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value)) : "—";
  const pct = (value) => Number.isFinite(Number(value)) ? Math.max(0, Math.min(100, Number(value))) : 0;
  const numeric = (value) => value !== null && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
  const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (character) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[character]);
  const displayDate = (value) => value ? new Intl.DateTimeFormat("tr-TR", { day: "2-digit", month: "long", year: "numeric", timeZone: "Europe/Istanbul" }).format(new Date(`${value}T12:00:00+03:00`)) : "—";

  function setText(id, value) { const node = $(id); if (node) node.textContent = value; }
  function startProgress() {
    const progress = $("tvProgress");
    progress.classList.remove("running");
    void progress.offsetWidth;
    if (!paused) progress.classList.add("running");
  }
  function scheduleRotation() {
    clearTimeout(rotateTimer);
    startProgress();
    if (!paused) rotateTimer = window.setTimeout(() => showSlide((currentSlide + 1) % slides.length), ROTATE_MS);
  }
  function showSlide(index) {
    currentSlide = (index + slides.length) % slides.length;
    slides.forEach((slide, i) => slide.classList.toggle("active", i === currentSlide));
    navButtons.forEach((button, i) => button.classList.toggle("active", i === currentSlide));
    scheduleRotation();
  }

  function lineChart(targetId, series) {
    const target = $(targetId);
    if (!target) return;
    const valid = series.flatMap((item) => item.values.filter(Number.isFinite));
    if (!valid.length) { target.textContent = "Bu tarih için grafik verisi bulunamadı."; return; }
    const min = Math.min(...valid), max = Math.max(...valid), range = max - min || 1;
    const x = (index, count) => 55 + (index / Math.max(1, count - 1)) * 900;
    const y = (value) => 330 - ((value - min) / range) * 280;
    const paths = series.map((item) => {
      const points = item.values.map((value, index) => Number.isFinite(value) ? `${x(index, item.values.length)},${y(value)}` : null);
      const segments = []; let active = [];
      points.forEach((point) => { if (point) active.push(point); else if (active.length) { segments.push(active); active = []; } });
      if (active.length) segments.push(active);
      return segments.map((segment) => `<polyline points="${segment.join(" ")}" fill="none" stroke="${item.color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`).join("");
    }).join("");
    const grids = [50, 143, 236, 330].map((value) => `<line x1="55" y1="${value}" x2="955" y2="${value}" stroke="#253854" stroke-width="1" stroke-dasharray="8 8"/>`).join("");
    const labels = [[0,"00:00"],[6,"06:00"],[12,"12:00"],[18,"18:00"],[23,"23:00"]].map(([index,label]) => `<text x="${x(index,24)}" y="382" text-anchor="middle" fill="#7186a6" font-size="12">${label}</text>`).join("");
    target.innerHTML = `<svg viewBox="0 0 1000 400" preserveAspectRatio="xMidYMid meet" role="img" aria-label="Saatlik grafik">${grids}${paths}${labels}</svg>`;
  }

  function render(report) {
    const modules = report.modules || {};
    const market = modules.market || {}, marketSummary = market.summary || {};
    const next = modules.nextDayPtf || {}, nextSummary = next.summary || {};
    const dams = modules.dams || {}, damSummary = report.damSummary || {};
    const production = modules.production || {}, productionSummary = production.summary || {};
    const consumption = modules.consumption || {}, consumptionSummary = consumption.summary || {};
    const ptfAverage = marketSummary.ptfAverageByCurrency?.TRY ?? marketSummary.ptfAverage;

    setText("overviewMeta", `${displayDate(report.date)} · ${report.availableModules?.length || 0}/5 veri grubu hazır`);
    setText("tvPtf", fmt(ptfAverage));
    setText("tvPtfMeta", `${marketSummary.ptfPublishedHours ?? market.rows?.filter((row) => Number.isFinite(row.ptf)).length ?? 0} saat yayımlandı`);
    setText("tvDeviation", fmt(productionSummary.deviationPct));
    setText("tvProductionMeta", `${production.period?.comparableHours ?? 0} ortak saat`);
    setText("tvDamAverage", fmt(damSummary.average));
    setText("tvDamMeta", `${damSummary.count || 0} baraj · ${damSummary.source || "veri bekleniyor"}`);
    setText("tvConsumptionPeak", fmt(consumptionSummary.maximum));
    setText("tvConsumptionMeta", `${consumptionSummary.maximumHour || "—"} · ${consumptionSummary.availableHours || 0}/24 saat`);
    setText("tvNextStatus", next.publication?.label || "Yayın bekleniyor");
    setText("tvNextAverage", `${fmt(nextSummary.ptfAverageByCurrency?.TRY)} TL/MWh`);
    setText("tvNextHours", `${nextSummary.publishedHours || 0} / 24 saat`);

    setText("tvMarketDate", displayDate(market.date || report.date));
    setText("tvMarketPtf", fmt(ptfAverage)); setText("tvMarketSmf", fmt(marketSummary.smfAverage));
    setText("tvMarketYal", fmt(marketSummary.yalTotal)); setText("tvMarketYat", fmt(marketSummary.yatTotal));
    lineChart("tvMarketChart", [
      { color: "#3478f6", values: (market.rows || []).map((row) => numeric(row.ptf)) },
      { color: "#8767ed", values: (market.rows || []).map((row) => numeric(row.smf)) },
    ]);

    setText("tvDamSource", `${displayDate(damSummary.date || report.date)} · ${damSummary.source || "—"}`);
    setText("tvDamGaugeValue", `%${fmt(damSummary.average)}`);
    $("tvDamGauge")?.style.setProperty("--fill", `${pct(damSummary.average) * 3.6}deg`);
    setText("tvDamHighest", damSummary.highest ? `${damSummary.highest.name} · %${fmt(damSummary.highest.value)}` : "—");
    setText("tvDamLowest", damSummary.lowest ? `${damSummary.lowest.name} · %${fmt(damSummary.lowest.value)}` : "—");
    setText("tvDamCount", `${damSummary.count || 0} baraj`);
    const ranked = [...(dams.items || [])].filter((item) => Number.isFinite(Number(item.activeFullnessAmount))).sort((a,b) => Number(b.activeFullnessAmount) - Number(a.activeFullnessAmount)).slice(0,8);
    $("tvDamRanking").innerHTML = ranked.map((item,index) => `<div class="tv-rank-row"><span>${String(index+1).padStart(2,"0")}</span><b>${esc(item.dam)}</b><div class="tv-rank-track"><i style="width:${pct(item.activeFullnessAmount)}%"></i></div><span>%${fmt(item.activeFullnessAmount,1)}</span></div>`).join("") || "Baraj verisi bulunamadı.";

    setText("tvProductionPeriod", production.period ? `${displayDate(production.period.start)} · ${production.period.uevmHours || 0} UEVM saati` : "—");
    setText("tvUevm", fmt(productionSummary.uevmTotal)); setText("tvUecm", fmt(productionSummary.uecmTotal));
    $("tvGroupBars").innerHTML = (production.groups || []).map((group) => `<div class="tv-group-row"><div><span>${esc(group.label)}</span><small>${fmt(group.value)} MWh</small></div><div class="tv-group-track"><i style="width:${pct(group.share)}%"></i></div><b>%${fmt(group.share,1)}</b></div>`).join("") || "Üretim verisi bulunamadı.";

    setText("tvConsumptionDate", displayDate(consumption.date || report.date));
    setText("tvLatestConsumption", fmt(consumptionSummary.latest)); setText("tvAverageConsumption", fmt(consumptionSummary.average));
    setText("tvConsumptionHour", consumptionSummary.maximumHour || "—"); setText("tvConsumptionCoverage", `${consumptionSummary.availableHours || 0} / 24 saat yayımlandı`);
    lineChart("tvConsumptionChart", [{ color: "#44bfd0", values: (consumption.rows || []).map((row) => numeric(row.consumption)) }]);

    const errorCount = Object.keys(report.errors || {}).length;
    $("tvLive").classList.toggle("warning", errorCount > 0);
    $("tvLive").querySelector("span").textContent = errorCount ? `${errorCount} veri grubunda uyarı` : "EPİAŞ · Canlı veri bağlantısı";
    setText("tvUpdated", `Güncelleme ${new Date(report.generatedAt).toLocaleTimeString("tr-TR", { hour:"2-digit", minute:"2-digit" })}`);
    $("tvReport").href = `/rapor?date=${encodeURIComponent(report.date)}`;
  }

  async function load() {
    try {
      const response = await fetch(`/api/command-center?date=${encodeURIComponent(todayTR())}`, { credentials: "include" });
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) { window.location.replace("/login"); return; }
      if (!response.ok) throw new Error(data.error || "Veriler alınamadı.");
      render(data); $("tvLoading").classList.add("hidden");
    } catch (error) {
      $("tvLive").classList.add("warning");
      $("tvLive").querySelector("span").textContent = error.message || "Bağlantı hatası";
      $("tvLoading").querySelector("span").textContent = error.message || "Veriler alınamadı.";
    }
  }

  function updateClock() {
    const now = new Date();
    setText("tvClock", now.toLocaleTimeString("tr-TR", { timeZone:"Europe/Istanbul", hour:"2-digit", minute:"2-digit", second:"2-digit" }));
    setText("tvDate", now.toLocaleDateString("tr-TR", { timeZone:"Europe/Istanbul", weekday:"long", day:"2-digit", month:"long", year:"numeric" }));
  }
  navButtons.forEach((button) => button.addEventListener("click", () => showSlide(Number(button.dataset.target))));
  $("tvPause").addEventListener("click", () => { paused = !paused; $("tvPause").textContent = paused ? "Devam et" : "Duraklat"; $("tvPause").setAttribute("aria-pressed", String(paused)); scheduleRotation(); });
  $("tvFullscreen").addEventListener("click", async () => { if (!document.fullscreenElement) await document.documentElement.requestFullscreen?.(); else await document.exitFullscreen?.(); });
  document.addEventListener("fullscreenchange", () => { $("tvFullscreen").textContent = document.fullscreenElement ? "Tam ekrandan çık" : "Tam ekran"; });
  document.addEventListener("keydown", (event) => { if (event.key === "ArrowRight") showSlide(currentSlide + 1); if (event.key === "ArrowLeft") showSlide(currentSlide - 1); if (event.key === " ") { event.preventDefault(); $("tvPause").click(); } });
  updateClock(); window.setInterval(updateClock, 1000); showSlide(0); load(); window.setInterval(() => { if (!document.hidden) load(); }, 300000);
})();
