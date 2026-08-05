(() => {
  const STORAGE_KEY = "baha-theme";
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  root.classList.add("suite-motion-enabled");

  function storedTheme() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch {
      return null;
    }
  }

  function selectedTheme() {
    return storedTheme() || (systemDark.matches ? "dark" : "light");
  }

  function updateControls(theme) {
    const dark = theme === "dark";
    document
      .querySelectorAll("[data-suite-theme-toggle], #theme-toggle")
      .forEach((button) => {
        button.textContent = dark ? "☀" : "☾";
        button.setAttribute(
          "aria-label",
          dark ? "Açık temaya geç" : "Koyu temaya geç",
        );
        button.setAttribute("aria-pressed", String(dark));
        button.title = dark ? "Açık temaya geç" : "Koyu temaya geç";
      });
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.content = dark ? "#0b1426" : "#101c35";
  }

  function apply(theme, announce = true) {
    const normalized = theme === "dark" ? "dark" : "light";
    const changed = root.dataset.theme !== normalized;
    root.dataset.theme = normalized;
    updateControls(normalized);
    if (announce && changed) {
      window.dispatchEvent(
        new CustomEvent("baha:themechange", {
          detail: { theme: normalized },
        }),
      );
    }
    return normalized;
  }

  function set(theme) {
    const normalized = theme === "dark" ? "dark" : "light";
    try {
      localStorage.setItem(STORAGE_KEY, normalized);
    } catch {
      // Depolama kapalı olsa bile mevcut sekmede tema değişmeye devam eder.
    }
    return apply(normalized);
  }

  function toggle() {
    return set(root.dataset.theme === "dark" ? "light" : "dark");
  }

  window.BahaTheme = {
    key: STORAGE_KEY,
    apply,
    get: selectedTheme,
    set,
    toggle,
  };

  apply(selectedTheme(), false);

  function connectControls() {
    updateControls(root.dataset.theme);
    document
      .querySelectorAll("[data-suite-theme-toggle]")
      .forEach((button) => {
        if (button.dataset.themeConnected === "true") return;
        button.dataset.themeConnected = "true";
        button.addEventListener("click", toggle);
      });
  }

  function connectPageMotion() {
    const body = document.body;
    if (!body?.classList.contains("baha-suite-page")) return;

    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => body.classList.add("suite-page-ready"));
    });

    document.querySelectorAll(".baha-suite-nav a").forEach((link) => {
      if (link.dataset.motionConnected === "true") return;
      link.dataset.motionConnected = "true";
      link.addEventListener("click", (event) => {
        if (
          event.defaultPrevented ||
          event.button !== 0 ||
          event.metaKey ||
          event.ctrlKey ||
          event.shiftKey ||
          event.altKey ||
          link.target === "_blank" ||
          link.hasAttribute("download")
        ) return;

        const target = new URL(link.href, window.location.href);
        if (target.origin !== window.location.origin) return;
        if (target.pathname === window.location.pathname && target.search === window.location.search) return;

        if (!reducedMotion.matches) {
          body.classList.remove("suite-page-ready");
          body.classList.add("suite-page-leaving");
        }
      });
    });

    document
      .querySelectorAll('.baha-suite-piyasa .sidebar nav a[href^="#"]')
      .forEach((link) => {
        link.addEventListener("click", () => {
          const target = document.querySelector(link.getAttribute("href"));
          if (!target || reducedMotion.matches) return;
          window.setTimeout(() => {
            target.classList.remove("suite-section-arriving");
            void target.offsetWidth;
            target.classList.add("suite-section-arriving");
            target.addEventListener(
              "animationend",
              () => target.classList.remove("suite-section-arriving"),
              {once: true},
            );
          }, 180);
        });
      });
  }

  function connectRoutePrefetching() {
    const eligiblePaths = new Set([
      "/piyasa/",
      "/baraj/",
      "/uretim/",
      "/tuketim/",
      "/sistem-yonu-tahmini/",
      "/tv/",
    ]);
    const warmed = new Set();

    function warmRoute(link) {
      if (!link?.href) return;
      const target = new URL(link.href, window.location.href);
      if (
        target.origin !== window.location.origin ||
        !eligiblePaths.has(target.pathname) ||
        target.pathname === window.location.pathname ||
        warmed.has(target.pathname)
      ) return;
      warmed.add(target.pathname);
      const prefetch = document.createElement("link");
      prefetch.rel = "prefetch";
      prefetch.href = target.pathname;
      document.head.append(prefetch);
    }

    document
      .querySelectorAll('.baha-suite-nav a[href], .suite-command-menu a[href]')
      .forEach((link) => {
        link.addEventListener("pointerenter", () => warmRoute(link), { passive: true });
        link.addEventListener("focus", () => warmRoute(link), { passive: true });
        link.addEventListener("touchstart", () => warmRoute(link), { passive: true });
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      connectControls();
      connectPageMotion();
      connectRoutePrefetching();
    }, {once: true});
  } else {
    connectControls();
    connectPageMotion();
    connectRoutePrefetching();
  }

  window.addEventListener("pageshow", () => {
    document.body?.classList.remove("suite-page-leaving");
    document.body?.classList.add("suite-page-ready");
  });

  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY) apply(selectedTheme());
  });

  systemDark.addEventListener("change", () => {
    if (!storedTheme()) apply(selectedTheme());
  });
})();
