// Custom suggestion dropdown for .tag-add-form inputs, positioned with plain
// CSS (absolute, anchored to the form) instead of the native <datalist>
// popup — Chromium miscalculates that popup's on-screen position inside
// this app's nested flexbox shell (sidebar + main), placing it far from the
// input it belongs to. Event delegation on document so this keeps working
// after htmx swaps in a fresh .tag-add-form.
(function () {
  function suggestionsFor(input) {
    return input.closest(".tag-add-form").querySelector(".tag-suggestions");
  }

  document.addEventListener("input", function (e) {
    var input = e.target.closest(".tag-add-form input[name='name']");
    if (!input) return;
    var box = suggestionsFor(input);
    var query = input.value.trim().toLowerCase();
    box.innerHTML = "";
    if (!query) {
      box.hidden = true;
      return;
    }
    var datalist = document.getElementById("all-tag-names");
    var matches = Array.prototype.slice
      .call(datalist.options)
      .map(function (o) { return o.value; })
      .filter(function (name) { return name.toLowerCase().includes(query); })
      .slice(0, 8);
    matches.forEach(function (name) {
      var row = document.createElement("div");
      row.textContent = name;
      box.appendChild(row);
    });
    box.hidden = matches.length === 0;
  });

  document.addEventListener("mousedown", function (e) {
    var row = e.target.closest(".tag-suggestions div");
    if (!row) return;
    e.preventDefault();
    var box = row.parentElement;
    box.parentElement.querySelector("input[name='name']").value = row.textContent;
    box.hidden = true;
  });

  document.addEventListener("focusout", function (e) {
    var input = e.target.closest(".tag-add-form input[name='name']");
    if (!input) return;
    setTimeout(function () { suggestionsFor(input).hidden = true; }, 0);
  });
})();
