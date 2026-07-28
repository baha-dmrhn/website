(() => {
  let tries = 0;
  const maxDelay = 5000;

  async function retry() {
    tries += 1;
    try {
      const response = await fetch(`/health?loading=${Date.now()}`, {
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.ok) {
        window.location.reload();
        return;
      }
    } catch (_) {
      // Render cold-start veya geçici bağlantı hatasında hazırlık ekranında kal.
    }
    window.setTimeout(retry, Math.min(1200 + tries * 450, maxDelay));
  }

  window.setTimeout(retry, 900);
})();
