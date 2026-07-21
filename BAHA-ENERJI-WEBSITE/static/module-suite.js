(() => {
  const body = document.body;
  const sidebar = document.querySelector(".suite-sidebar");
  const menuButton = document.querySelector(".suite-menu-button");
  const closeButton = document.querySelector(".suite-menu-close");
  const overlay = document.querySelector(".suite-sidebar-overlay");
  const logoutButton = document.querySelector(".suite-logout-button");
  const links = [...document.querySelectorAll(".suite-sidebar nav a")];
  const liveStatus = document.querySelector(".suite-live-dot");
  let navigationLockUntil = 0;
  let trackingFrame = 0;

  if (!sidebar) return;

  function setActiveLink(activeLink) {
    links.forEach((link) => {
      link.classList.toggle("active", link === activeLink);
    });
  }

  function setSidebar(open) {
    body.classList.toggle("suite-sidebar-open", open);
    menuButton?.setAttribute("aria-expanded", String(open));
  }

  menuButton?.addEventListener("click", () => setSidebar(true));
  closeButton?.addEventListener("click", () => setSidebar(false));
  overlay?.addEventListener("click", () => setSidebar(false));
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") setSidebar(false);
  });

  links.forEach((link) => {
    link.addEventListener("click", () => {
      // Yumuşak kaydırma tamamlanana kadar tıklanan bağlantıyı koru.
      navigationLockUntil = Date.now() + 900;
      setActiveLink(link);
      setSidebar(false);
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
    if (!source) return;

    const update = () => {
      const text = (source.textContent || "").trim();
      const normalized = text.toLocaleLowerCase("tr-TR");
      const isError = /hata|yok|başarısız|erişilemedi/.test(normalized);
      const isLoading = /yüklen|bağlan|hazırlan/.test(normalized);
      liveStatus.classList.toggle("error", isError);
      liveStatus.classList.toggle("loading", !isError && isLoading);
      const label = liveStatus.querySelector("span");
      if (label) label.textContent = "EPİAŞ · EPİAŞ canlı";
      liveStatus.title = text;
    };

    update();
    new MutationObserver(update).observe(source, {
      childList: true,
      subtree: true,
      characterData: true,
      attributes: true,
    });
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
