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

  const commandToggle = document.querySelector(".suite-command-toggle");
  const commandMenu = document.querySelector(".suite-command-menu");

  function setCommandMenu(open) {
    if (!commandToggle || !commandMenu) return;
    commandMenu.classList.toggle("open", open);
    commandToggle.classList.toggle("active", open);
    commandToggle.setAttribute("aria-expanded", String(open));
  }

  commandToggle?.addEventListener("click", (event) => {
    event.stopPropagation();
    setCommandMenu(!commandMenu?.classList.contains("open"));
  });
  commandMenu?.addEventListener("click", (event) => event.stopPropagation());
  commandMenu?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setCommandMenu(false));
  });
  document.addEventListener("click", () => setCommandMenu(false));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setCommandMenu(false);
  });
})();
