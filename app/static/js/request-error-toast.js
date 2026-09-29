(function () {
  // htmx swaps nothing and shows nothing on a non-2xx response by default —
  // without this, a tripped rate limit or an expired CSRF token (both plain
  // {"detail": "..."} JSON responses from FastAPI's own HTTPException, not
  // HTML) looked exactly like a button that simply did nothing. This parses
  // that shape (every such error in this app already uses it) and shows it
  // briefly instead, wherever in the app it happens.
  var toast = null;
  var hideTimer = null;

  function showToast(message) {
    if (toast) {
      clearTimeout(hideTimer);
      toast.remove();
    }
    toast = document.createElement("div");
    toast.className = "request-error-toast";
    toast.setAttribute("role", "alert");
    toast.textContent = message;
    toast.addEventListener("click", function () {
      toast.remove();
      toast = null;
    });
    document.body.appendChild(toast);
    hideTimer = setTimeout(function () {
      if (toast) {
        toast.remove();
        toast = null;
      }
    }, 6000);
  }

  function messageFrom(xhr) {
    try {
      var body = JSON.parse(xhr.responseText);
      if (body && typeof body.detail === "string") return body.detail;
    } catch (e) {
      // not JSON — fall through to a status-based default below
    }
    if (xhr.status === 429) return "Too many requests — please wait a moment before trying again.";
    if (xhr.status === 403) return "Your session token is out of date — please refresh the page and try again.";
    return "Something went wrong (" + xhr.status + ").";
  }

  document.body.addEventListener("htmx:responseError", function (evt) {
    showToast(messageFrom(evt.detail.xhr));
  });
  document.body.addEventListener("htmx:sendError", function () {
    showToast("Could not reach the server — check your connection and try again.");
  });
})();
