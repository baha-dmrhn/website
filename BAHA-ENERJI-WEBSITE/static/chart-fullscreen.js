(() => {
  "use strict";

  const body = document.body;
  if (!body?.classList.contains("baha-suite-page")) return;

  const pageKind = ["piyasa", "baraj", "uretim", "tuketim"].find((kind) =>
    body.classList.contains(`baha-suite-${kind}`),
  );
  if (!pageKind) return;

  const selectors = {
    piyasa: [
      { panel: "#next-day-ptf", heading: ".panel-heading", target: "#next-day-ptf-chart", title: "Ertesi Gün PTF grafiği", kind: "apex" },
      { panel: "#hourly-data", heading: ".panel-heading", target: "#price-chart", title: "PTF ve SMF fiyat grafiği", kind: "apex" },
      { panel: "#quantity-chart", closest: ".panel", heading: ".panel-heading", target: "#quantity-chart", title: "Saatlik YAL ve YAT grafiği", kind: "apex" },
    ],
    baraj: [
      { panel: "#baraj-map", heading: ".baraj-map-head", target: ".baraj-map-stage", title: "Türkiye havza haritası", kind: "map" },
      { panel: "#basinRegimeChart", heading: ".baraj-regime-chart-head", target: "#basinRegimeChart", title: "Havza doluluk rejimi grafiği", kind: "svg", frame: true },
    ],
    uretim: [
      { panel: ".trend-panel", heading: ".panel-head", target: "#trendChart", title: "UEVM ve UEÇM denge grafiği", kind: "svg" },
      { panel: ".groups-panel", heading: ".panel-head", target: "#groupBars", title: "Üretim ana grupları grafiği", kind: "bars" },
      { panel: ".mix-panel", heading: ".panel-head", target: ".donut-layout", title: "Üretim grup payları grafiği", kind: "donut" },
    ],
    tuketim: [
      { panel: "#consumption-chart", heading: "header", target: "#consumptionChart", title: "Saatlik tüketim grafiği", kind: "apex" },
      { panel: "#consumption-forecast", heading: "header", target: "#consumptionForecastChart", title: "Saatlik tüketim tahmini grafiği", kind: "apex" },
    ],
  };

  const expandIcon = `
    <svg class="suite-chart-expand-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5" />
    </svg>`;
  const closeIcon = `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M6 6l12 12M18 6 6 18" />
    </svg>`;

  const backdrop = document.createElement("button");
  backdrop.className = "suite-chart-backdrop";
  backdrop.type = "button";
  backdrop.tabIndex = -1;
  backdrop.setAttribute("aria-label", "Tam ekran grafiği kapat");

  const viewer = document.createElement("section");
  viewer.className = "suite-chart-viewer suite-chart-maximized";
  viewer.setAttribute("role", "dialog");
  viewer.setAttribute("aria-modal", "true");
  viewer.innerHTML = `
    <button class="suite-chart-viewer-close" type="button" aria-label="Tam ekran grafiği kapat" title="Tam ekranı kapat">${closeIcon}</button>
    <div class="suite-chart-viewer-canvas"></div>`;
  const viewerCanvas = viewer.querySelector(".suite-chart-viewer-canvas");
  const viewerClose = viewer.querySelector(".suite-chart-viewer-close");
  body.append(backdrop, viewer);

  let active = null;

  function announceResize() {
    requestAnimationFrame(() => {
      window.dispatchEvent(new Event("resize"));
      window.setTimeout(() => window.dispatchEvent(new Event("resize")), 180);
    });
  }

  function findApexChart(target) {
    const entries = window.Apex?._chartInstances;
    if (!Array.isArray(entries)) return null;
    const entry = entries.find(({ chart }) => {
      const element = chart?.el || chart?.w?.globals?.dom?.baseEl;
      return element === target || (element instanceof Element && target.contains(element));
    });
    return entry?.chart || null;
  }

  function setApexHeight(chart, height) {
    if (!chart?.updateOptions || height == null) return;
    try {
      Promise.resolve(
        chart.updateOptions({ chart: { height } }, false, false, false),
      ).catch(() => {});
    } catch (_error) {
      // Bazı ApexCharts sürümleri görünüm değişirken güncellemeyi reddedebilir.
    }
  }

  function resizeActiveChart() {
    if (!active || active.kind !== "apex") return;
    const chart = active.apexChart || findApexChart(active.target);
    if (!chart) return;
    active.apexChart = chart;
    if (active.apexOriginalHeight == null) {
      active.apexOriginalHeight = chart.w?.config?.chart?.height ?? "auto";
    }
    setApexHeight(chart, Math.max(320, viewerCanvas.clientHeight));
  }

  function closeFullscreen({ restoreFocus = true } = {}) {
    if (!active) return;
    const state = active;
    active = null;
    viewer.classList.remove("active");
    backdrop.classList.remove("active");
    body.classList.remove("suite-chart-fullscreen-open");
    state.target.classList.remove("suite-chart-viewer-target");
    state.placeholder.before(state.target);
    state.placeholder.remove();
    if (state.apexChart) setApexHeight(state.apexChart, state.apexOriginalHeight);
    state.button.setAttribute("aria-pressed", "false");
    state.button.setAttribute("aria-label", "Grafiği tam ekran aç");
    state.button.setAttribute("title", "Tam ekran aç");
    viewer.removeAttribute("data-chart-kind");
    viewer.removeAttribute("aria-label");
    announceResize();
    if (restoreFocus) state.button.focus();
  }

  function openFullscreen(resolved, button) {
    if (active?.target === resolved.target) {
      closeFullscreen();
      return;
    }
    if (active) closeFullscreen({ restoreFocus: false });
    const placeholder = document.createComment("suite-chart-placeholder");
    resolved.target.before(placeholder);
    active = {
      ...resolved,
      button,
      placeholder,
      apexChart: null,
      apexOriginalHeight: null,
    };
    resolved.target.classList.add("suite-chart-viewer-target");
    viewer.dataset.chartKind = resolved.kind;
    viewer.setAttribute("aria-label", resolved.title);
    viewerCanvas.replaceChildren(resolved.target);
    button.setAttribute("aria-pressed", "true");
    button.setAttribute("aria-label", "Tam ekran grafiği kapat");
    button.setAttribute("title", "Tam ekranı kapat");
    body.classList.add("suite-chart-fullscreen-open");
    backdrop.classList.add("active");
    viewer.classList.add("active");
    viewerClose.focus();
    requestAnimationFrame(() => {
      resizeActiveChart();
      announceResize();
    });
  }

  function createBarajChartFrame(chart, heading) {
    if (chart.parentElement?.classList.contains("suite-chart-frame")) {
      return chart.parentElement;
    }
    const frame = document.createElement("section");
    frame.className = "suite-chart-frame suite-chart-frame-contents";
    frame.setAttribute("aria-label", "Havza doluluk rejimi grafiği");
    heading.before(frame);
    frame.append(heading, chart);
    return frame;
  }

  function resolvePanel(spec) {
    const seed = document.querySelector(spec.panel);
    if (!seed) return null;
    let panel = spec.closest ? seed.closest(spec.closest) : seed;
    if (!panel) return null;
    let heading = panel.querySelector(spec.heading);
    if (spec.frame) {
      heading = document.querySelector(spec.heading);
      if (!heading) return null;
      panel = createBarajChartFrame(seed, heading);
    }
    const target = panel.querySelector(spec.target) ||
      (seed.matches(spec.target) ? seed : document.querySelector(spec.target));
    return heading && target ? { panel, heading, target, title: spec.title, kind: spec.kind } : null;
  }

  function addButton(spec) {
    const resolved = resolvePanel(spec);
    if (!resolved || resolved.panel.dataset.suiteChartFullscreen === "true") return;
    const { panel, heading, title } = resolved;
    panel.dataset.suiteChartFullscreen = "true";

    let actions = heading.querySelector(":scope > .suite-chart-header-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "suite-chart-header-actions";
      [...heading.children].slice(1).forEach((node) => actions.append(node));
      heading.append(actions);
    }

    const button = document.createElement("button");
    button.className = "suite-chart-fullscreen-button";
    button.type = "button";
    button.innerHTML = expandIcon;
    button.setAttribute("aria-label", "Grafiği tam ekran aç");
    button.setAttribute("aria-pressed", "false");
    button.setAttribute("title", "Tam ekran aç");
    button.addEventListener("click", () => openFullscreen(resolved, button));
    actions.append(button);
  }

  selectors[pageKind].forEach(addButton);
  viewerClose.addEventListener("click", () => closeFullscreen());
  backdrop.addEventListener("click", () => closeFullscreen());
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && active) closeFullscreen();
  });
  window.addEventListener("resize", () => {
    if (active?.kind === "apex") window.setTimeout(resizeActiveChart, 50);
  });
})();
