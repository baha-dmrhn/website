(() => {
  const STORAGE_KEY = "baha-theme";
  const root = document.documentElement;
  const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

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

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", connectControls, {
      once: true,
    });
  } else {
    connectControls();
  }

  window.addEventListener("storage", (event) => {
    if (event.key === STORAGE_KEY) apply(selectedTheme());
  });

  systemDark.addEventListener("change", () => {
    if (!storedTheme()) apply(selectedTheme());
  });
})();
