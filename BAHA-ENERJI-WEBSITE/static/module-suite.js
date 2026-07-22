(() => {
  const body = document.body;
  const sidebar = document.querySelector(".suite-sidebar");
  const menuButton = document.querySelector(".suite-menu-button");
  const closeButton = document.querySelector(".suite-menu-close");
  const overlay = document.querySelector(".suite-sidebar-overlay");
  const logoutButton = document.querySelector(".suite-logout-button");
  const links = [...document.querySelectorAll(".suite-sidebar nav a")];
  const liveStatus = document.querySelector(".suite-live-dot");
  const desktopSidebar = window.matchMedia("(min-width: 821px)");
  const sidebarStorageKey = "baha-sidebar-collapsed";
  let navigationLockUntil = 0;
  let trackingFrame = 0;
  let desktopHoverTimer = 0;
  let desktopPointerInside = false;

  if (!sidebar) return;

  function setActiveLink(activeLink) {
    links.forEach((link) => {
      link.classList.toggle("active", link === activeLink);
    });
  }

  function setSidebar(open, persist = true) {
    if (desktopSidebar.matches) {
      const collapsed = !open;
      body.classList.remove("suite-sidebar-open");
      body.classList.remove("suite-sidebar-hovered");
      body.classList.toggle("suite-sidebar-collapsed", collapsed);
      menuButton?.setAttribute("aria-expanded", String(open));
      if (persist) {
        localStorage.setItem(sidebarStorageKey, String(collapsed));
      }
      return;
    }
    body.classList.remove("suite-sidebar-collapsed");
    body.classList.toggle("suite-sidebar-open", open);
    menuButton?.setAttribute("aria-expanded", String(open));
  }

  function setDesktopSidebarHover(open) {
    if (!desktopSidebar.matches || !body.classList.contains("suite-sidebar-collapsed")) return;
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
    setDesktopSidebarHover(false);
  });
  sidebar.addEventListener("focusin", () => setDesktopSidebarHover(true));
  sidebar.addEventListener("focusout", (event) => {
    if (!desktopPointerInside && !sidebar.contains(event.relatedTarget)) setDesktopSidebarHover(false);
  });

  menuButton?.addEventListener("click", () => setSidebar(true));
  closeButton?.addEventListener("click", () => setSidebar(false));
  overlay?.addEventListener("click", () => setSidebar(false));
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (desktopSidebar.matches) setDesktopSidebarHover(false);
    else setSidebar(false);
  });
  function syncSidebarMode() {
    if (desktopSidebar.matches) {
      setSidebar(false, false);
    } else {
      setSidebar(false, false);
    }
  }
  desktopSidebar.addEventListener("change", syncSidebarMode);
  syncSidebarMode();

  links.forEach((link) => {
    link.addEventListener("click", () => {
      // Yumuşak kaydırma tamamlanana kadar tıklanan bağlantıyı koru.
      navigationLockUntil = Date.now() + 900;
      setActiveLink(link);
      if (!desktopSidebar.matches) setSidebar(false);
      const id = link.getAttribute("href");
      const target = id?.startsWith("#") ? document.querySelector(id) : null;
      if (target) {
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
      window.location.replace("/login");
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
      liveStatus.classList.toggle("error", isError);
      liveStatus.classList.toggle("warning", !isError && isWarning);
      liveStatus.classList.toggle("loading", !isError && !isWarning && isLoading);
      const label = liveStatus.querySelector("span");
      if (label) {
        label.textContent = isError
          ? "EPİAŞ · Bağlantı hatası"
          : isWarning
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
        const id = link.getAttribute("href");
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
