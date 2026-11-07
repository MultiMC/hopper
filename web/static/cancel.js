// Cancelling a queued run asks first. The forms are in <main>, which refresh.js replaces, so
// this listens on the document.
(function () {
    "use strict";
    document.addEventListener("submit", function (event) {
        var form = event.target;
        if (!form.classList.contains("cancel")) {
            return;
        }
        if (!window.confirm("Cancel " + form.dataset.run + "? It won't run: it ends as aborted when its turn comes.")) {
            event.preventDefault();
        }
    });
})();
