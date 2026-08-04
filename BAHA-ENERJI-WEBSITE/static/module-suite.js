(() => {
  const body = document.body;
  const sidebar = document.querySelector(".suite-sidebar");
  const menuButton = document.querySelector(".suite-menu-button");
  const closeButton = document.querySelector(".suite-menu-close");
  const overlay = document.querySelector(".suite-sidebar-overlay");
  const lockButton = document.querySelector("#sidebar-lock, .suite-sidebar-lock-button, .sidebar-lock-button");
  const logoutButton = document.querySelector(".suite-logout-button");
  const links = [...document.querySelectorAll(".suite-sidebar nav a")];
  const liveStatus = document.querySelector(".suite-live-dot");
  const desktopSidebar = window.matchMedia("(min-width: 821px)");
  const desktopLock = window.matchMedia("(min-width: 1025px) and (hover: hover) and (pointer: fine)");
  const sidebarStorageKey = "baha-sidebar-collapsed";
  const sidebarPinnedStorageKey = "baha-sidebar-pinned";
  let navigationLockUntil = 0;
  let trackingFrame = 0;
  let desktopHoverTimer = 0;
  let desktopPointerInside = false;

  if ("serviceWorker" in navigator) {
    window.addEventListener(
      "load",
      () => navigator.serviceWorker.register("/sw.js").catch(() => {}),
      { once: true },
    );
  }

  if (!sidebar) return;

  function cleanUrlHash() {
    if (!window.location.hash || !window.history?.replaceState) return;
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  function targetSelector(link) {
    return link.dataset.suiteScroll || link.getAttribute("href") || "";
  }

  function setActiveLink(activeLink) {
    links.forEach((link) => {
      link.classList.toggle("active", link === activeLink);
    });
  }

  function isSidebarPinned() {
    return localStorage.getItem(sidebarPinnedStorageKey) === "true";
  }

  function canPinSidebar() {
    return desktopLock.matches;
  }

  function updateSidebarLockUi(pinned = isSidebarPinned()) {
    const locked = canPinSidebar() && pinned;
    body.classList.toggle("suite-sidebar-pinned", locked);
    lockButton?.classList.toggle("is-locked", locked);
    lockButton?.setAttribute("aria-pressed", String(locked));
    const label = locked ? "Yan menü kilidini aç" : "Yan menüyü sabitle";
    lockButton?.setAttribute("aria-label", label);
    lockButton?.setAttribute("title", label);
  }

  function setSidebar(open, persist = true) {
    if (desktopSidebar.matches) {
      if (open) body.classList.remove("suite-sidebar-dismissed");
      const collapsed = !open;
      body.classList.remove("suite-sidebar-open");
      if (!open) body.classList.remove("suite-sidebar-hovered");
      body.classList.toggle("suite-sidebar-collapsed", collapsed);
      menuButton?.setAttribute("aria-expanded", String(open));
      if (persist) {
        localStorage.setItem(sidebarStorageKey, String(collapsed));
      }
      updateSidebarLockUi(open && isSidebarPinned());
      return;
    }
    body.classList.remove("suite-sidebar-collapsed");
    body.classList.remove("suite-sidebar-pinned");
    body.classList.toggle("suite-sidebar-open", open);
    menuButton?.setAttribute("aria-expanded", String(open));
    updateSidebarLockUi(false);
  }

  function setSidebarPinned(pinned) {
    if (!canPinSidebar()) {
      updateSidebarLockUi(false);
      if (desktopSidebar.matches) setSidebar(false);
      return;
    }
    localStorage.setItem(sidebarPinnedStorageKey, String(pinned));
    updateSidebarLockUi(pinned);
    if (desktopSidebar.matches) {
      setSidebar(pinned);
    }
  }

  function setDesktopSidebarHover(open) {
    if (
      !desktopSidebar.matches
      || body.classList.contains("suite-sidebar-pinned")
      || (open && body.classList.contains("suite-sidebar-dismissed"))
      || !body.classList.contains("suite-sidebar-collapsed")
    ) return;
    window.clearTimeout(desktopHoverTimer);
    const apply = () => {
      body.classList.toggle("suite-sidebar-hovered", open);
      menuButton?.setAttribute("aria-expanded", String(open));
    };
    if (open) apply();
    else desktopHoverTimer = window.setTimeout(apply, 120);
  }

  sidebar.addEventListener("mouseenter", () => {
    desktopPointerInside = true;
    setDesktopSidebarHover(true);
  });
  sidebar.addEventListener("mouseleave", () => {
    desktopPointerInside = false;
    body.classList.remove("suite-sidebar-dismissed");
    setDesktopSidebarHover(false);
  });
  sidebar.addEventListener("focusin", () => setDesktopSidebarHover(true));
  sidebar.addEventListener("focusout", (event) => {
    if (!desktopPointerInside && !sidebar.contains(event.relatedTarget)) setDesktopSidebarHover(false);
  });

  menuButton?.addEventListener("click", () => {
    if (desktopSidebar.matches && canPinSidebar()) setSidebarPinned(true);
    else if (desktopSidebar.matches) setSidebar(body.classList.contains("suite-sidebar-collapsed"));
    else setSidebar(true);
  });
  closeButton?.addEventListener("click", () => {
    if (desktopSidebar.matches) {
      body.classList.add("suite-sidebar-dismissed");
      body.classList.remove("suite-sidebar-hovered");
      if (sidebar.contains(document.activeElement)) document.activeElement.blur();
    }
    if (desktopSidebar.matches && canPinSidebar() && isSidebarPinned()) setSidebarPinned(false);
    else setSidebar(false);
  });
  lockButton?.addEventListener("click", () => {
    if (!desktopSidebar.matches || !canPinSidebar()) return;
    setSidebarPinned(!isSidebarPinned());
  });
  overlay?.addEventListener("click", () => setSidebar(false));
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (desktopSidebar.matches) setDesktopSidebarHover(false);
    else setSidebar(false);
  });
  function syncSidebarMode() {
    if (desktopSidebar.matches) {
      setSidebar(canPinSidebar() && isSidebarPinned(), false);
    } else {
      setSidebar(false, false);
    }
  }
  desktopSidebar.addEventListener("change", syncSidebarMode);
  desktopLock.addEventListener("change", syncSidebarMode);
  window.addEventListener("storage", (event) => {
    if (event.key === sidebarPinnedStorageKey) syncSidebarMode();
  });
  syncSidebarMode();
  cleanUrlHash();

  links.forEach((link) => {
    link.addEventListener("click", (event) => {
      // Yumuşak kaydırma tamamlanana kadar tıklanan bağlantıyı koru.
      navigationLockUntil = Date.now() + 900;
      setActiveLink(link);
      if (!desktopSidebar.matches) setSidebar(false);
      const id = targetSelector(link);
      const target = id?.startsWith("#") ? document.querySelector(id) : null;
      if (target) {
        event.preventDefault();
        cleanUrlHash();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
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
      }
    });
  });

  async function loadAccount() {
    try {
      const response = await fetch("/api/session", {
        credentials: "same-origin",
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok || !payload.authenticated) return;
      const email = String(payload.username || "Baha Enerji Kullanıcısı");
      const emailNode = document.querySelector("[data-suite-user-email]");
      const initialNode = document.querySelector("[data-suite-user-initial]");
      if (emailNode) emailNode.textContent = email;
      if (initialNode) initialNode.textContent = email.trim().charAt(0).toLocaleUpperCase("tr-TR") || "B";
    } catch {
      // Hesap rozeti kritik değildir; veri ekranı çalışmaya devam eder.
    }
  }

  logoutButton?.addEventListener("click", async () => {
    logoutButton.disabled = true;
    logoutButton.textContent = "Oturum kapatılıyor…";
    try {
      await fetch("/api/logout", {
        method: "POST",
        credentials: "same-origin",
      });
    } finally {
      window.location.replace("/oturum-kapatildi");
    }
  });

  function mirrorStatus() {
    if (!liveStatus) return;
    const source = body.classList.contains("baha-suite-uretim")
      ? document.querySelector("#connectionStatus")
      : document.querySelector("#status");

    const update = () => {
      const explicitState = body.dataset.epiasState || "";
      const explicitDetail = body.dataset.epiasDetail || "";
      const text = explicitDetail || (source?.textContent || "").trim();
      const normalized = text.toLocaleLowerCase("tr-TR");
      const isError = explicitState === "error" || /hata|başarısız|erişilemedi/.test(normalized);
      const isWarning = explicitState === "warning" || /eksik|kısmi|henüz|yayımlanmadı|veri yok/.test(normalized);
      const isLoading = explicitState === "loading" || /yüklen|bağlan|hazırlan|alınıyor/.test(normalized);
      const suppressUretimWarning = body.classList.contains("baha-suite-uretim") && isWarning && !isError;
      liveStatus.classList.toggle("error", isError);
      liveStatus.classList.toggle("warning", !isError && isWarning && !suppressUretimWarning);
      liveStatus.classList.toggle("loading", !isError && !isWarning && isLoading);
      const label = liveStatus.querySelector("span");
      if (label) {
        label.textContent = isError
          ? "EPİAŞ · Bağlantı hatası"
          : isWarning && !suppressUretimWarning
          ? "EPİAŞ · Eksik veri"
          : isLoading
          ? "EPİAŞ · Veriler alınıyor"
          : "EPİAŞ · EPİAŞ canlı";
      }
      liveStatus.title = text;
    };

    update();
    if (source) {
      new MutationObserver(update).observe(source, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
      });
    }
    new MutationObserver(update).observe(body, {
      attributes: true,
      attributeFilter: ["data-epias-state", "data-epias-detail"],
    });
    window.addEventListener("baha:connectionstate", update);
  }

  function trackSections() {
    const sections = links
      .map((link) => {
        const id = targetSelector(link);
        const target = id?.startsWith("#") ? document.querySelector(id) : null;
        return target ? { link, target } : null;
      })
      .filter(Boolean);
    if (!sections.length) return;

    function syncActiveSection() {
      trackingFrame = 0;
      if (Date.now() < navigationLockUntil) return;

      const guideLine = Math.min(180, Math.max(96, window.innerHeight * 0.22));
      let current = sections[0];
      sections.forEach((section) => {
        if (section.target.getBoundingClientRect().top <= guideLine) {
          current = section;
        }
      });

      const pageBottom =
        window.scrollY + window.innerHeight >=
        document.documentElement.scrollHeight - 4;
      if (pageBottom) current = sections[sections.length - 1];
      setActiveLink(current.link);
    }

    function requestSync() {
      if (trackingFrame) return;
      trackingFrame = window.requestAnimationFrame(syncActiveSection);
    }

    window.addEventListener("scroll", requestSync, { passive: true });
    window.addEventListener("resize", requestSync);
    window.setTimeout(requestSync, 950);
    requestSync();
  }

  loadAccount();
  mirrorStatus();
  trackSections();
})();
