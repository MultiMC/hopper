// Shows a run's log as it grows. It reads /run/<job>/<number>/log, which stays open until a
// running run ends.
(function () {
    "use strict";
    var box = document.getElementById("log");
    var info = document.getElementById("log-info");
    var followBox = document.getElementById("follow");
    if (!box) {
        return;
    }
    var lines = 0;
    var partial = "";
    var ansi = /\x1b\[[0-9;]*[A-Za-z]/g;

    function follow() {
        return followBox.checked;
    }

    // A line rewritten with \r (progress) shows what it said last
    function clean(line) {
        var text = line.replace(/\r$/, "");
        return text.substring(text.lastIndexOf("\r") + 1).replace(ansi, "");
    }

    function kind(line) {
        if (line.startsWith("===")) {
            return "log-step";
        }
        var lower = line.toLowerCase();
        if (lower.includes("error") || lower.includes("traceback") || line.includes(" crashed:")) {
            return "log-error";
        }
        return null;
    }

    function add(text) {
        var parts = (partial + text).split("\n");
        partial = parts.pop();
        var fragment = document.createDocumentFragment();
        var plain = "";
        parts.forEach(function (raw) {
            var line = clean(raw);
            var name = kind(line);
            if (name) {
                if (plain) {
                    fragment.append(plain);
                    plain = "";
                }
                var span = document.createElement("span");
                span.className = name;
                span.textContent = line + "\n";
                fragment.append(span);
            } else {
                plain += line + "\n";
            }
        });
        if (plain) {
            fragment.append(plain);
        }
        box.append(fragment);
        lines += parts.length;
        info.textContent = lines + " lines";
        if (follow()) {
            window.scrollTo(0, document.documentElement.scrollHeight);
        }
    }

    // Scrolling away stops following. Only what the user does counts: the page also moves by
    // itself as it changes.
    function stopFollowing() {
        followBox.checked = false;
    }
    window.addEventListener("wheel", function (event) {
        if (event.deltaY < 0) {
            stopFollowing();
        }
    }, {passive: true});
    window.addEventListener("touchmove", stopFollowing, {passive: true});
    window.addEventListener("keydown", function (event) {
        if (["ArrowUp", "PageUp", "Home"].includes(event.key)) {
            stopFollowing();
        }
    });
    // The scrollbar
    window.addEventListener("mousedown", function (event) {
        if (event.clientX >= document.documentElement.clientWidth) {
            stopFollowing();
        }
    });

    followBox.addEventListener("change", function () {
        if (follow()) {
            window.scrollTo(0, document.documentElement.scrollHeight);
        }
    });

    function read() {
        return fetch(box.dataset.url, {cache: "no-store"}).then(function (response) {
            if (!response.ok) {
                return response.text().then(function (text) {
                    throw new Error(text.trim() || "no such log");
                });
            }
            var reader = response.body.getReader();
            var decoder = new TextDecoder();
            function next() {
                return reader.read().then(function (part) {
                    if (part.done) {
                        add(decoder.decode());
                        if (partial) {
                            add("\n");
                        }
                        return;
                    }
                    add(decoder.decode(part.value, {stream: true}));
                    return next();
                });
            }
            return next();
        });
    }

    read().then(function () {
        info.textContent = lines + " lines, the end";
        followBox.disabled = true;
    }).catch(function (error) {
        info.textContent = lines + " lines";
        var problem = document.getElementById("log-problem");
        problem.textContent = "Can't read the log: " + error.message;
        problem.hidden = false;
    });
})();
