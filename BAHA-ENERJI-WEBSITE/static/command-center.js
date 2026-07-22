(() => {
  "use strict";

  const todayTR = () =>
    new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Istanbul" }).format(
      new Date()
    );

  function activeDate() {
    const candidates = [
      document.querySelector("#date-input"),
      document.querySelector("#barajDateSelect"),
      document.querySelector("#endDate"),
      document.querySelector("#consumptionDate"),
    ];
    for (const input of candidates) {
      const value = String(input?.value || "").trim();
      if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return value;
    }
    return todayTR();
  }

  document.querySelectorAll("[data-suite-report-link]").forEach((link) => {
    link.addEventListener("click", () => {
      link.href = `/rapor?date=${encodeURIComponent(activeDate())}`;
    });
  });
})();
