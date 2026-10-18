// Enables a bulk-action button once at least one checkbox inside a
// container is checked, disables it again once none are. Used by every
// "select some rows, then act on them" table (bundles, catalog items,
// Audible's owned-books table, ...) — each one just calls this once with
// its own container/checkbox-selector/button ids instead of writing its
// own copy of the same listener.
function wireBulkSelect(containerId, checkboxSelector, buttonId) {
  var container = document.getElementById(containerId);
  var btn = document.getElementById(buttonId);
  if (!container || !btn) return;
  container.addEventListener("change", function () {
    btn.disabled = container.querySelectorAll(checkboxSelector).length === 0;
  });
}
