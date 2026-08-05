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
      { panel: "#consumption-chart", heading: "header", target: "#consumptionChart", title: "Saatlik tüketim ve tahmin grafiği", kind: "apex" },
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

  function apexNodes(target) {
    const canvas = target.matches?.(".apexcharts-canvas")
      ? target
      : target.querySelector(".apexcharts-canvas");
    return [
      target,
      canvas,
      target.querySelector(".apexcharts-svg"),
      target.querySelector(":scope > svg"),
      target.querySelector(".apexcharts-inner"),
      target.querySelector(".apexcharts-graphical"),
    ].filter(Boolean);
  }

  function snapshotApexStyles(target) {
    return apexNodes(target).map((node) => ({
      node,
      height: node.style.height,
      minHeight: node.style.minHeight,
      maxHeight: node.style.maxHeight,
      width: node.style.width,
      attrHeight: node.getAttribute?.("height"),
      attrWidth: node.getAttribute?.("width"),
    }));
  }

  function restoreApexStyles(snapshot) {
    snapshot?.forEach((state) => {
      state.node.style.height = state.height;
      state.node.style.minHeight = state.minHeight;
      state.node.style.maxHeight = state.maxHeight;
      state.node.style.width = state.width;
      if (!state.node.setAttribute) return;
      if (state.attrHeight == null) state.node.removeAttribute("height");
      else state.node.setAttribute("height", state.attrHeight);
      if (state.attrWidth == null) state.node.removeAttribute("width");
      else state.node.setAttribute("width", state.attrWidth);
    });
  }

  function chartOwnsTarget(chart, target) {
    const nodes = [
      chart?.el,
      chart?.w?.globals?.dom?.baseEl,
      chart?.w?.globals?.dom?.elWrap,
      chart?.w?.globals?.dom?.Paper?.node?.closest?.(".apexcharts-canvas"),
    ].filter((node) => node instanceof Element);
    return nodes.some((node) =>
      node === target || target.contains(node) || node.contains(target),
    );
  }

  function findApexChart(target) {
    const localChart = [...(window.__bahaEnergyCharts || [])].find((chart) => {
      const chartTarget = chart?.target;
      return (
        chartTarget instanceof Element &&
        (chartTarget === target ||
          target.contains(chartTarget) ||
          chartTarget.contains(target))
      );
    });
    if (localChart) return localChart;
    const entries = window.Apex?._chartInstances;
    if (!Array.isArray(entries)) return null;
    for (const item of entries) {
      const chart = item?.chart || item;
      if (chartOwnsTarget(chart, target)) return chart;
    }
    return null;
  }

  function forceApexDomHeight(target, height, width) {
    const safeHeight = Math.max(320, Math.floor(height || 0));
    const safeWidth = Math.max(320, Math.floor(width || 0));
    const heightPx = `${safeHeight}px`;
    const widthPx = `${safeWidth}px`;
    apexNodes(target).forEach((node) => {
      node.style.height = heightPx;
      node.style.minHeight = heightPx;
      node.style.maxHeight = "none";
      node.style.width = widthPx;
      if (node.setAttribute) {
        node.setAttribute("height", String(safeHeight));
        node.setAttribute("width", String(safeWidth));
      }
    });
  }

  function setApexHeight(chart, height) {
    if (!chart?.updateOptions || height == null) return;
    try {
      Promise.resolve(
        chart.updateOptions(
          {
            chart: {
              height,
              redrawOnParentResize: true,
              redrawOnWindowResize: true,
            },
          },
          false,
          false,
          false,
        ),
      ).catch(() => {});
    } catch (_error) {
      // Bazı ApexCharts sürümleri görünüm değişirken güncellemeyi reddedebilir.
    }
  }

  function resizeActiveChart() {
    if (!active || active.kind !== "apex") return;
    const height = Math.max(320, viewerCanvas.clientHeight);
    const width = Math.max(320, viewerCanvas.clientWidth);
    forceApexDomHeight(active.target, height, width);
    const chart = active.apexChart || findApexChart(active.target);
    if (!chart) {
      announceResize();
      return;
    }
    active.apexChart = chart;
    if (active.apexOriginalHeight == null) {
      active.apexOriginalHeight = chart.w?.config?.chart?.height ?? "auto";
    }
    setApexHeight(chart, height);
  }

  function scheduleApexResize() {
    [0, 80, 220, 460].forEach((delay) => {
      window.setTimeout(resizeActiveChart, delay);
    });
  }

  function closeFullscreen({ restoreFocus = true } = {}) {
    if (!active) return;
    const state = active;
    active = null;
    viewer.classList.remove("active");
    backdrop.classList.remove("active");
    body.classList.remove("suite-chart-fullscreen-open");
    state.target.classList.remove("suite-chart-viewer-target");
    restoreApexStyles(state.apexStyleSnapshot);
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
      apexStyleSnapshot: resolved.kind === "apex"
        ? snapshotApexStyles(resolved.target)
        : null,
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
      if (resolved.kind === "apex") scheduleApexResize();
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
