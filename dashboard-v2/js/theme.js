// Light/dark theme toggle. Defaults to the OS preference until the viewer
// makes an explicit choice, which then persists in localStorage. No
// dependency, no duplicate stylesheet — CSS variables in css/app.css do
// the actual theming, this module only sets/reads a data-theme attribute.

const THEME_KEY = "dashboard-v2-theme";

// localStorage can throw (private browsing, blocked storage) — never let
// a theme preference crash the app.
function safeGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function safeSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* best-effort only */ }
}

function savedTheme() {
  return safeGet(THEME_KEY); // "light" | "dark" | null (= follow system)
}

function isDarkNow() {
  const saved = savedTheme();
  if (saved) return saved === "dark";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
}

function apply(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}

function updateToggleUI(btn) {
  const dark = isDarkNow();
  btn.textContent = dark ? "☀ Light" : "🌙 Dark";
  btn.setAttribute("aria-pressed", String(dark));
}

export function initTheme() {
  apply(savedTheme());
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  updateToggleUI(btn);
  btn.addEventListener("click", () => {
    const next = isDarkNow() ? "light" : "dark";
    safeSet(THEME_KEY, next);
    apply(next);
    updateToggleUI(btn);
  });
}
