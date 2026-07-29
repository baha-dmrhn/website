(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const number = new Intl.NumberFormat("tr-TR", {
    maximumFractionDigits: 2,
  });

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      element.setAttribute(key, String(value));
    });
    return element;
  }

  function finite(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function mergeOptions(base, extra) {
    if (!extra || typeof extra !== "object") return base || {};
    const output = {...(base || {})};
    Object.entries(extra).forEach(([key, value]) => {
      const current = output[key];
      output[key] =
        current &&
        value &&
        typeof current === "object" &&
        typeof value === "object" &&
        !Array.isArray(current) &&
        !Array.isArray(value)
          ? mergeOptions(current, value)
          : value;
    });
    return output;
  }

  function niceMaximum(value) {
    if (!Number.isFinite(value) || value <= 0) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(value));
    const normalized = value / magnitude;
    const nice =
      normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return nice * magnitude;
  }

  class LocalEnergyChart {
    constructor(target, options) {
      this.target = target;
      this.options = options || {};
      this.resizeObserver = null;
      this.resizeTimer = null;
      this.renderedWidth = 0;
      this.renderedHeight = 0;
      window.__bahaEnergyCharts = window.__bahaEnergyCharts || new Set();
      window.__bahaEnergyCharts.add(this);
    }

    destroy() {
      if (this.resizeObserver) this.resizeObserver.disconnect();
      clearTimeout(this.resizeTimer);
      this.target.replaceChildren();
      this.target.classList.remove("energy-chart");
      window.__bahaEnergyCharts?.delete(this);
    }

    updateOptions(options) {
      this.options = mergeOptions(this.options, options || {});
      this.draw();
      return Promise.resolve();
    }

    render() {
      this.draw();
      if ("ResizeObserver" in window) {
        this.resizeObserver = new ResizeObserver((entries) => {
          const width = Math.round(entries[0]?.contentRect?.width || 0);
          const height = Math.round(entries[0]?.contentRect?.height || 0);
          const sameWidth = Math.abs(width - this.renderedWidth) < 3;
          const sameHeight = Math.abs(height - this.renderedHeight) < 3;
          if (!width || (sameWidth && sameHeight)) return;
          clearTimeout(this.resizeTimer);
          this.resizeTimer = setTimeout(() => this.draw(), 80);
        });
        this.resizeObserver.observe(this.target);
      }
      return Promise.resolve();
    }

    draw() {
      const series = Array.isArray(this.options.series)
        ? this.options.series
        : [];
      const categories = Array.isArray(this.options.xaxis?.categories)
        ? this.options.xaxis.categories
        : [];
      const values = series.flatMap((item) =>
        (item.data || []).map(finite).filter((value) => value !== null),
      );

      this.target.replaceChildren();
      this.target.classList.add("energy-chart");
      if (!values.length || !categories.length) {
        const empty = document.createElement("div");
        empty.className = "chart-loading";
        empty.textContent = this.options.noData?.text || "Veri bulunamadı";
        this.target.append(empty);
        return;
      }

      const type = this.options.chart?.type || "area";
      const dark = document.documentElement.dataset.theme === "dark";
      const gridColor = dark ? "#2b3951" : "#e7ecf3";
      const labelColor = dark ? "#9aa8c0" : "#8190a6";
      const width = Math.max(this.target.clientWidth || 760, 320);
      const configuredHeight = Number(this.options.chart?.height) || 320;
      const containerHeight = Number(this.target.clientHeight) || 0;
      const height = Math.max(configuredHeight, containerHeight, 230);
      const compact = width <= 700;
      const margin = compact
        ? { top: 20, right: 6, bottom: 42, left: 50 }
        : { top: 24, right: 22, bottom: 42, left: 72 };
      const plotWidth = width - margin.left - margin.right;
      const plotHeight = height - margin.top - margin.bottom;
      const colors = this.options.colors || [
        "#2d70ee",
        "#8165eb",
        "#2ab87b",
        "#f29a43",
      ];
      const rawMin = Math.min(...values, 0);
      const rawMax = Math.max(...values);
      const maximum = niceMaximum(rawMax * 1.08);
      const minimum = rawMin < 0 ? -niceMaximum(Math.abs(rawMin) * 1.08) : 0;
      const range = maximum - minimum || 1;
      const xaxisLabels = this.options.xaxis?.labels || {};
      const labelStep = Math.max(1, Number(xaxisLabels.step) || 3);
      const labelIndexes = categories
        .map((_, index) => index)
        .filter(
          (index) =>
            index % labelStep === 0 || index === categories.length - 1,
        );
      const visualRatioFor = (index) => {
        if (labelIndexes.length <= 1) return 0.5;
        const rightPosition = labelIndexes.findIndex(
          (labelIndex) => labelIndex >= index,
        );
        if (rightPosition <= 0) return 0;
        const leftIndex = labelIndexes[rightPosition - 1];
        const rightIndex = labelIndexes[rightPosition];
        const withinSegment =
          (index - leftIndex) / Math.max(rightIndex - leftIndex, 1);
        return (
          (rightPosition - 1 + withinSegment) /
          (labelIndexes.length - 1)
        );
      };
      const xFor = (index) =>
        margin.left +
        (categories.length <= 1
          ? plotWidth / 2
          : visualRatioFor(index) * plotWidth);
      const yFor = (value) =>
        margin.top + ((maximum - value) / range) * plotHeight;

      const svg = svgElement("svg", {
        viewBox: `0 0 ${width} ${height}`,
        role: "img",
        "aria-label": series
          .filter((item) => !item.hideInLegend)
          .map((item) => item.name)
          .join(" ve "),
      });
      const defs = svgElement("defs");
      series.forEach((item, index) => {
        const gradient = svgElement("linearGradient", {
          id: `energyGradient-${this.target.id}-${index}`,
          x1: "0",
          x2: "0",
          y1: "0",
          y2: "1",
        });
        gradient.append(
          svgElement("stop", {
            offset: "0%",
            "stop-color": colors[index % colors.length],
            "stop-opacity": index < 2 ? ".22" : ".08",
          }),
          svgElement("stop", {
            offset: "100%",
            "stop-color": colors[index % colors.length],
            "stop-opacity": "0",
          }),
        );
        defs.append(gradient);
      });
      svg.append(defs);

      const grid = svgElement("g", { "aria-hidden": "true" });
      for (let tick = 0; tick <= 4; tick += 1) {
        const value = maximum - (tick / 4) * range;
        const y = margin.top + (tick / 4) * plotHeight;
        grid.append(
          svgElement("line", {
            x1: margin.left,
            x2: width - margin.right,
            y1: y,
            y2: y,
            stroke: gridColor,
            "stroke-width": "1",
            "stroke-dasharray": "4 5",
          }),
        );
        const label = svgElement("text", {
          x: margin.left - 12,
          y: y + 4,
          fill: labelColor,
          "font-size": "10",
          "text-anchor": "end",
        });
        label.textContent = number.format(value);
        grid.append(label);
      }
      labelIndexes.forEach((index) => {
        const category = categories[index];
        const isLastLabel = index === categories.length - 1;
        const label = svgElement("text", {
          class: "energy-chart-x-label",
          x: xFor(index),
          y: height - 13,
          fill: labelColor,
          "font-size": compact
            ? xaxisLabels.compactFontSize || "9"
            : xaxisLabels.fontSize || "10",
          "text-anchor":
            index === 0
              ? "start"
              : isLastLabel
                ? "end"
                : "middle",
        });
        label.textContent = String(category);
        grid.append(label);
      });
      svg.append(grid);

      if (type === "bar") {
        this.drawBars(
          svg,
          series,
          categories,
          colors,
          xFor,
          yFor,
          margin,
          plotWidth,
          plotHeight,
        );
      } else {
        this.drawLines(
          svg,
          series,
          colors,
          xFor,
          yFor,
          margin,
          plotHeight,
          this.options.stroke || {},
        );
      }

      const guide = svgElement("line", {
        y1: margin.top,
        y2: margin.top + plotHeight,
        stroke: dark ? "#79a4ff" : "#2d70ee",
        "stroke-width": "1",
        "stroke-dasharray": "4 4",
        opacity: "0",
        "pointer-events": "none",
      });
      svg.append(guide);
      this.target.append(svg);

      const tooltip = document.createElement("div");
      tooltip.className = "energy-chart-tooltip";
      tooltip.hidden = true;
      this.target.append(tooltip);
      const pointFromEvent = (event) => {
        const bounds = svg.getBoundingClientRect();
        const svgX = ((event.clientX - bounds.left) / bounds.width) * width;
        let index = 0;
        let closestDistance = Number.POSITIVE_INFINITY;
        categories.forEach((_, candidateIndex) => {
          const distance = Math.abs(xFor(candidateIndex) - svgX);
          if (distance < closestDistance) {
            closestDistance = distance;
            index = candidateIndex;
          }
        });
        const x = xFor(index);
        return { bounds, index, x };
      };
      const onPointer = (event) => {
        const { bounds, index, x } = pointFromEvent(event);
        guide.setAttribute("x1", x);
        guide.setAttribute("x2", x);
        guide.setAttribute("opacity", "1");
        tooltip.innerHTML =
          `<strong>${categories[index] ?? "—"}</strong>` +
          series
            .map((item, seriesIndex) => {
              if (item.hideInTooltip) return "";
              const value = finite(item.data?.[index]);
              if (value === null && item.hideNullInTooltip) return "";
              const color = colors[seriesIndex % colors.length];
              const formattedValue =
                value === null
                  ? "—"
                  : typeof item.tooltipFormatter === "function"
                    ? item.tooltipFormatter(value, index)
                    : number.format(value);
              return `<span><b><i style="background:${color}"></i>${item.name}</b><em>${formattedValue}</em></span>`;
            })
            .join("");
        tooltip.hidden = false;

        const targetBounds = this.target.getBoundingClientRect();
        const edgeGap = 8;
        const anchorGap = 12;
        const anchorX =
          bounds.left -
          targetBounds.left +
          (x / width) * bounds.width;
        const anchorY = event.clientY - targetBounds.top;
        const tooltipWidth = tooltip.offsetWidth;
        const tooltipHeight = tooltip.offsetHeight;
        const maxLeft = Math.max(
          edgeGap,
          this.target.clientWidth - tooltipWidth - edgeGap,
        );
        const left = Math.max(
          edgeGap,
          Math.min(maxLeft, anchorX - tooltipWidth / 2),
        );
        const spaceAbove = anchorY - anchorGap;
        const spaceBelow =
          this.target.clientHeight - anchorY - anchorGap;
        const placeBelow =
          spaceAbove < tooltipHeight && spaceBelow >= tooltipHeight;
        const requestedTop = placeBelow
          ? anchorY + anchorGap
          : anchorY - anchorGap - tooltipHeight;
        const maxTop = Math.max(
          edgeGap,
          this.target.clientHeight - tooltipHeight - edgeGap,
        );
        const top = Math.max(edgeGap, Math.min(maxTop, requestedTop));

        tooltip.dataset.placement = placeBelow ? "below" : "above";
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
      };
      svg.addEventListener("pointermove", onPointer);
      svg.addEventListener("pointerleave", () => {
        tooltip.hidden = true;
        guide.setAttribute("opacity", "0");
      });
      svg.addEventListener("click", (event) => {
        const { index } = pointFromEvent(event);
        const selection = this.options.chart?.events?.dataPointSelection;
        const click = this.options.chart?.events?.click;
        if (typeof selection === "function") {
          selection(event, this, { dataPointIndex: index, seriesIndex: 0 });
        } else if (typeof click === "function") {
          click(event, this, { dataPointIndex: index, seriesIndex: 0 });
        }
      });

      if (type === "bar" || this.options.legend?.show) {
        const legend = document.createElement("div");
        legend.className = "energy-chart-legend";
        legend.innerHTML = series
          .map((item, index) => ({ item, index }))
          .filter(({ item }) => !item.hideInLegend)
          .map(
            ({ item, index }) =>
              `<span><i style="background:${colors[index % colors.length]}"></i>${item.name}</span>`,
          )
          .join("");
        this.target.append(legend);
      }
      this.renderedWidth = width;
      this.renderedHeight = height;
    }

    drawLines(svg, series, colors, xFor, yFor, margin, plotHeight, strokeOptions) {
      series.forEach((item, seriesIndex) => {
        const plottedPoints = (item.data || [])
          .map((value, index) => {
            const parsed = finite(value);
            return parsed === null ? null : [xFor(index), yFor(parsed)];
          });
        const segments = [];
        let currentSegment = [];
        plottedPoints.forEach((point) => {
          if (point) {
            currentSegment.push(point);
          } else if (currentSegment.length) {
            segments.push(currentSegment);
            currentSegment = [];
          }
        });
        if (currentSegment.length) segments.push(currentSegment);
        const points = segments.flat();
        if (!points.length) return;
        const widthOption = Array.isArray(strokeOptions.width)
          ? strokeOptions.width[seriesIndex]
          : strokeOptions.width;
        const dashOption = Array.isArray(strokeOptions.dashArray)
          ? strokeOptions.dashArray[seriesIndex]
          : strokeOptions.dashArray;
        const strokeWidth = Number.isFinite(Number(widthOption))
          ? Number(widthOption)
          : seriesIndex < 2 ? 2.6 : 1.8;
        const dashSize = Number.isFinite(Number(dashOption))
          ? Number(dashOption)
          : seriesIndex < 2 ? 0 : 7;
        const areaEnabled = item.fill !== false && seriesIndex < 2;
        if (!item.markerOnly) {
          segments.forEach((segment) => {
            const path = segment
              .map(([x, y], index) => `${index ? "L" : "M"} ${x} ${y}`)
              .join(" ");
            if (areaEnabled && segment.length > 1) {
              const areaPath =
                `${path} L ${segment.at(-1)[0]} ${margin.top + plotHeight} ` +
                `L ${segment[0][0]} ${margin.top + plotHeight} Z`;
              svg.append(
                svgElement("path", {
                  d: areaPath,
                  fill: `url(#energyGradient-${this.target.id}-${seriesIndex})`,
                }),
              );
            }
            svg.append(
              svgElement("path", {
                d: path,
                fill: "none",
                stroke: colors[seriesIndex % colors.length],
                "stroke-width": strokeWidth,
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
                "stroke-dasharray": dashSize > 0 ? `${dashSize} ${Math.max(3, dashSize * .85)}` : "0",
                opacity: areaEnabled ? "1" : ".82",
              }),
            );
          });
        }
        if (item.hideMarkers) return;
        points.forEach(([x, y]) => {
          svg.append(
            svgElement("circle", {
              cx: x,
              cy: y,
              r: item.markerRadius || "2.3",
              fill: item.markerFill || "#fff",
              stroke: colors[seriesIndex % colors.length],
              "stroke-width": "1.5",
              opacity: item.markerOnly ? "1" : points.length > 32 ? "0" : ".75",
            }),
          );
        });
      });
    }

    drawBars(
      svg,
      series,
      categories,
      colors,
      xFor,
      yFor,
      margin,
      plotWidth,
      plotHeight,
    ) {
      const groupWidth = Math.min(
        30,
        (plotWidth / Math.max(categories.length, 1)) * 0.68,
      );
      const barWidth = Math.max(2, groupWidth / Math.max(series.length, 1));
      const baseline = margin.top + plotHeight;
      categories.forEach((category, categoryIndex) => {
        const center = xFor(categoryIndex);
        series.forEach((item, seriesIndex) => {
          const value = finite(item.data?.[categoryIndex]);
          if (value === null) return;
          const y = yFor(Math.max(0, value));
          svg.append(
            svgElement("rect", {
              x:
                center -
                groupWidth / 2 +
                seriesIndex * barWidth +
                0.6,
              y,
              width: Math.max(1, barWidth - 1.2),
              height: Math.max(1, baseline - y),
              rx: Math.min(3, barWidth / 3),
              fill: colors[seriesIndex % colors.length],
              opacity: ".9",
            }),
          );
        });
      });
    }
  }

  window.ApexCharts = LocalEnergyChart;
})();
