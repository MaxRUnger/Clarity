
(function () {
  var params = new URLSearchParams(window.location.search);
  if (params.get("print") !== "1") {
    return;
  }
  window.addEventListener("load", function () {
    window.setTimeout(function () {
      // Lets the Tailwind CDN finish applying styles.
      window.print();
      params.delete("print");
      var query = params.toString();
      var next = window.location.pathname + (query ? "?" + query : "") + window.location.hash;
      history.replaceState(null, "", next);
    }, 250);
  });
})();

document.getElementById('printClassSheetBtn').addEventListener('click', function () {
  window.print();
});
