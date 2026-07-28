const form = document.querySelector("#loginForm");
const usernameInput = document.querySelector("#username");
const passwordInput = document.querySelector("#password");
const submitButton = document.querySelector("#loginSubmit");
const errorBox = document.querySelector("#loginError");
const passwordToggle = document.querySelector("#passwordToggle");
if (window.location.hash && window.history?.replaceState) {
  window.history.replaceState(null, "", window.location.pathname + window.location.search);
}

async function checkSession() {
  try {
    const response = await fetch("/api/session", {
      headers: { Accept: "application/json" },
    });
    const session = await response.json();
    if (session.authenticated) {
      window.location.replace("/piyasa/");
      return;
    }
  } catch {
    errorBox.textContent = "Sunucuya bağlanılamadı.";
  }
}

passwordToggle.addEventListener("click", () => {
  const visible = passwordInput.type === "text";
  passwordInput.type = visible ? "password" : "text";
  passwordToggle.setAttribute("aria-label", visible ? "Şifreyi göster" : "Şifreyi gizle");
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.textContent = "";
  if (!form.reportValidity()) return;

  submitButton.disabled = true;
  submitButton.querySelector("span").textContent = "Giriş yapılıyor…";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        username: usernameInput.value.trim(),
        password: passwordInput.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Giriş yapılamadı.");
    window.location.replace("/piyasa/");
  } catch (error) {
    errorBox.textContent = error.message || "Beklenmeyen bir hata oluştu.";
    passwordInput.select();
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span").textContent = "Panele giriş yap";
  }
});

checkSession();
