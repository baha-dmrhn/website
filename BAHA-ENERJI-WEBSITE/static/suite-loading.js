(() => {
  const splash = document.getElementById("bahaSuiteLoading");
  if (!splash) return;

  const minVisibleMs = 420;
  const startedAt = performance.now();
  let hideTimer = 0;

  function hideSplash() {
    clearTimeout(hideTimer);
    const wait = Math.max(0, minVisibleMs - (performance.now() - startedAt));
    hideTimer = window.setTimeout(() => {
      splash.classList.add("is-hidden");
      window.setTimeout(() => splash.remove(), 360);
    }, wait);
  }

  function showSplash() {
    clearTimeout(hideTimer);
    if (!document.body.contains(splash)) {
      document.body.prepend(splash);
    }
    splash.classList.remove("is-hidden");
  }

  function shouldShowForAnchor(anchor, event) {
    if (!anchor || event.defaultPrevented) return false;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return false;
    }
    if (anchor.target && anchor.target !== "_self") return false;
    if (anchor.hasAttribute("download")) return false;
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("#") || href.startsWith("javascript:")) return false;
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return false;
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) {
      return false;
    }
    return !/\.(?:xlsx|csv|pdf|png|jpe?g|webp|ico|json|geojson|webmanifest)$/i.test(url.pathname);
  }

  window.BahaSuiteLoading = { show: showSplash, hide: hideSplash };

  window.addEventListener("load", hideSplash, { once: true });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) hideSplash();
  });
  window.addEventListener("beforeunload", showSplash);

  document.addEventListener("click", (event) => {
    const anchor = event.target.closest?.("a[href]");
    if (shouldShowForAnchor(anchor, event)) showSplash();
  });

  document.addEventListener("submit", () => showSplash());

  if (document.readyState === "complete") {
    hideSplash();
  }
})();
