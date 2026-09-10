(function () {
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.user-menu[open]").forEach(function (el) {
      if (!el.contains(e.target)) el.open = false;
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.user-menu[open]").forEach(function (el) {
      el.open = false;
    });
  });
})();
