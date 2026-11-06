// Keeps a page's <main> current: fetches the page again and swaps it in. The pages work
// without this, by reloading them. When the page can't be fetched, says so, rather than
// leaving old numbers looking current.
(function () {
    "use strict";
    var INTERVAL = 2000;
    var main = document.querySelector("main[data-refresh]");
    var stale = document.getElementById("stale");
    if (!main) {
        return;
    }

    function selecting() {
        var selection = window.getSelection();
        return selection && !selection.isCollapsed;
    }

    function refresh() {
        // Replacing the page would drop a selection someone is about to copy
        if (document.hidden || selecting()) {
            return Promise.resolve();
        }
        return fetch(location.href, {cache: "no-store"})
            .then(function (response) {
                return response.text();
            })
            .then(function (text) {
                var next = new DOMParser().parseFromString(text, "text/html").querySelector("main");
                if (!next) {
                    throw new Error("no page");
                }
                main.replaceChildren.apply(main, Array.from(next.childNodes));
                stale.hidden = true;
            })
            .catch(function () {
                stale.hidden = false;
            });
    }

    function loop() {
        refresh().then(function () {
            setTimeout(loop, INTERVAL);
        });
    }
    setTimeout(loop, INTERVAL);
})();
