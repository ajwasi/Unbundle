(function () {
  function currentTheme() {
    var explicit = document.documentElement.dataset.theme;
    if (explicit === "light" || explicit === "dark") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function updateIcon(btn) {
    // Icon shows what clicking it *does*, not the current state — moon means
    // "go dark", sun means "go light", matching the convention most sites use.
    var dark = currentTheme() === "dark";
    var icon = dark ? "☀️" : "🌙";
    // The in-menu instance (data-with-label) also gets a text label, since it
    // sits among labelled rows there; the standalone sidebar-brand instance
    // stays icon-only, unchanged from before.
    btn.textContent = btn.dataset.withLabel ? icon + " " + (dark ? "Light mode" : "Dark mode") : icon;
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    updateIcon(btn);
    btn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("theme", next);
      updateIcon(btn);
    });
  });
})();
