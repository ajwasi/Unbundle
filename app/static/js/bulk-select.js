// Enables a bulk-action button once at least one checkbox inside a
// container is checked, disables it again once none are. Used by every
// "select some rows, then act on them" table (bundles, catalog items,
// Audible's owned-books table, ...) — each one just calls this once with
// its own container/checkbox-selector/button ids instead of writing its
// own copy of the same listener.
//
// Also wires Shift+click range-select: holding Shift while clicking a
// checkbox sets every checkbox between it and the last one clicked to the
// same state, same convention Gmail/a file manager's own list view already
// use. checkboxSelector may be several comma-separated pieces (e.g. bundle
// detail's own ".item-check:checked, input[name=formats]:checked" for two
// unrelated groups of checkboxes in one table) — a range only ever spans
// checkboxes matching the SAME piece the click landed on, so shift-clicking
// an item checkbox can never also sweep in an unrelated format checkbox.
// Each piece's own ":checked" suffix is stripped to get "every checkbox in
// this group" (not just the currently-checked ones) to range over.
function wireBulkSelect(containerId, checkboxSelector, buttonId) {
  var container = document.getElementById(containerId);
  var btn = document.getElementById(buttonId);
  if (!container || !btn) return;
  container.addEventListener("change", function () {
    btn.disabled = container.querySelectorAll(checkboxSelector).length === 0;
  });

  var groupSelectors = checkboxSelector.split(",").map(function (s) {
    return s.replace(":checked", "").trim();
  });
  var lastClicked = {};

  container.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLInputElement) || target.type !== "checkbox") return;
    var group = groupSelectors.find(function (sel) { return target.matches(sel); });
    if (!group) return;

    if (event.shiftKey && lastClicked[group]) {
      var boxes = Array.prototype.slice.call(container.querySelectorAll(group));
      var from = boxes.indexOf(lastClicked[group]);
      var to = boxes.indexOf(target);
      if (from !== -1 && to !== -1) {
        var lo = Math.min(from, to), hi = Math.max(from, to);
        for (var i = lo; i <= hi; i++) {
          boxes[i].checked = target.checked;
        }
        // Programmatic .checked assignments above don't fire their own
        // change events — one on the container re-runs the enable/disable
        // logic above, same as the "select all" checkboxes already trigger
        // manually after their own bulk toggle.
        container.dispatchEvent(new Event("change"));
      }
    }
    lastClicked[group] = target;
  });
}
