(() => {
  "use strict";

  const storageKey = "open-flame-theme";
  const allowedThemes = new Set(["system", "light", "dark"]);
  const root = document.documentElement;

  function storedTheme() {
    try {
      const value = localStorage.getItem(storageKey);
      return allowedThemes.has(value) ? value : "system";
    } catch {
      return "system";
    }
  }

  function applyTheme(value, persist) {
    const theme = allowedThemes.has(value) ? value : "system";
    root.dataset.theme = theme;
    for (const control of document.querySelectorAll("[data-of-theme]")) {
      control.value = theme;
    }
    if (persist) {
      try {
        localStorage.setItem(storageKey, theme);
      } catch {
        // A blocked preference store must never block the local application.
      }
    }
  }

  function bindThemeControls() {
    root.classList.remove("no-js");
    applyTheme(storedTheme(), false);
    for (const control of document.querySelectorAll("[data-of-theme]")) {
      control.addEventListener("change", () => applyTheme(control.value, true));
    }
  }

  function bindPointerFeedback() {
    if (typeof document.addEventListener !== "function") return;
    const activeControls = new Set();
    const clear = () => {
      for (const control of activeControls) {
        delete control.dataset.pointerActive;
      }
      activeControls.clear();
    };
    const activate = event => {
      if (typeof event.button === "number" && event.button !== 0) return;
      const control = event.target.closest?.(
        "button:not(:disabled), .button-link:not([aria-disabled='true'])"
      );
      if (!control) return;
      control.dataset.pointerActive = "true";
      activeControls.add(control);
    };
    document.addEventListener("pointerdown", activate, true);
    document.addEventListener("mousedown", activate, true);
    document.addEventListener("touchstart", activate, {capture: true, passive: true});
    document.addEventListener("pointerup", clear, true);
    document.addEventListener("pointercancel", clear, true);
    document.addEventListener("mouseup", clear, true);
    document.addEventListener("touchend", clear, true);
    document.addEventListener("touchcancel", clear, true);
    window.addEventListener("blur", clear);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bindThemeControls, {once: true});
  } else {
    bindThemeControls();
  }
  bindPointerFeedback();

  window.addEventListener("storage", event => {
    if (event.key === storageKey) applyTheme(event.newValue, false);
  });
})();
