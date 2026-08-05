(() => {
  "use strict";
  const printButton = document.querySelector("#reportPrint");
  printButton?.addEventListener("click", () => window.print());
  if (document.body.dataset.autoPrint === "true") {
    window.addEventListener("load", () => window.setTimeout(() => window.print(), 350));
  }
})();
