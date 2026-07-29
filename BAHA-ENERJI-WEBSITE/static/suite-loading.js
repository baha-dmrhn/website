(() => {
  const splash = document.getElementById("bahaSuiteLoading");
  if (!splash) return;

  const minVisibleMs = 420;
  const maxVisibleMs = 12000;
  const startedAt = performance.now();
  let hideTimer = 0;
  let safetyTimer = 0;

  function clearTimers() {
    window.clearTimeout(hideTimer);
    window.clearTimeout(safetyTimer);
  }

  function armSafetyTimer() {
    window.clearTimeout(safetyTimer);
    safetyTimer = window.setTimeout(hideSplash, maxVisibleMs);
  }

  function hideSplash() {
    clearTimers();
    const wait = Math.max(0, minVisibleMs - (performance.now() - startedAt));
    hideTimer = window.setTimeout(() => {
      splash.classList.add("is-hidden");
      window.setTimeout(() => splash.remove(), 360);
    }, wait);
  }

  function showSplash() {
    clearTimers();
    if (!document.body.contains(splash)) {
      document.body.prepend(splash);
    }
    splash.classList.remove("is-hidden");
    splash.style.animation = "none";
    void splash.offsetWidth;
    splash.style.animation = "";
    armSafetyTimer();
  }

  function cleanLoadingBypassParameter() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("baha_ready")) return;
    url.searchParams.delete("baha_ready");
    window.history.replaceState(
      window.history.state,
      "",
      `${url.pathname}${url.search}${url.hash}`,
    );
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

  cleanLoadingBypassParameter();
  armSafetyTimer();
  document.addEventListener("DOMContentLoaded", hideSplash, { once: true });
  window.addEventListener("load", hideSplash, { once: true });
  window.addEventListener("pageshow", hideSplash);
  window.addEventListener("beforeunload", showSplash);

  document.addEventListener("click", (event) => {
    const anchor = event.target.closest?.("a[href]");
    if (!shouldShowForAnchor(anchor, event)) return;
    showSplash();
    window.setTimeout(() => {
      if (event.defaultPrevented) hideSplash();
    }, 0);
  });

  document.addEventListener("submit", (event) => {
    showSplash();
    window.setTimeout(() => {
      if (event.defaultPrevented) hideSplash();
    }, 0);
  });

  if (document.readyState !== "loading") {
    hideSplash();
  }
})();
