// Moving a setup's tags, in steps: choose builds, Apply asks daemon/builds.py what that would
// do and shows it, Confirm queues the job. All the rules are in updater/tag.py; this only
// keeps the choices sensible, and the server refuses what they let through.
(function () {
    "use strict";
    var form = document.getElementById("tags");
    var dialog = document.getElementById("review");
    if (!form || !dialog) {
        return;
    }
    var setup = form.dataset.setup;
    var serial = form.dataset.serial;
    var current = {testing: Number(form.dataset.testing), stable: Number(form.dataset.stable)};
    var pending = document.getElementById("pending");
    var apply = document.getElementById("apply");
    var reset = document.getElementById("reset");
    var confirm = document.getElementById("review-confirm");
    var cancel = document.getElementById("review-cancel");
    var problem = document.getElementById("review-problem");
    var body = null;

    function radios(tag) {
        return Array.from(form.querySelectorAll('input[name="' + tag + '"]'));
    }

    function chosen(tag) {
        return Number(form.querySelector('input[name="' + tag + '"]:checked').value);
    }

    function choose(tag, build) {
        radios(tag).forEach(function (radio) {
            radio.checked = Number(radio.value) === build;
        });
    }

    function moves() {
        return ["testing", "stable"].filter(function (tag) {
            return chosen(tag) !== current[tag];
        });
    }

    function title(tag) {
        return tag.charAt(0).toUpperCase() + tag.slice(1);
    }

    function update() {
        var stable = chosen("stable");
        // Testing is never older than Stable
        radios("testing").forEach(function (radio) {
            radio.disabled = Number(radio.value) < Math.max(current.testing, stable);
        });
        var changed = moves();
        pending.textContent = changed.length ? changed.map(function (tag) {
            return title(tag) + " " + current[tag] + " → " + chosen(tag);
        }).join(", ") : "Nothing changed.";
        apply.disabled = reset.disabled = !changed.length;
        form.querySelectorAll("tr[data-build]").forEach(function (row) {
            var build = Number(row.dataset.build);
            row.classList.toggle("chosen", changed.some(function (tag) {
                return chosen(tag) === build;
            }));
        });
    }

    form.addEventListener("change", function (event) {
        // Stable beyond Testing takes Testing with it, visibly
        if (event.target.name === "stable" && chosen("stable") > chosen("testing")) {
            choose("testing", chosen("stable"));
        }
        update();
    });
    // A reload can bring back what the browser remembered of the form
    window.addEventListener("pageshow", function () {
        choose("testing", current.testing);
        choose("stable", current.stable);
        update();
    });

    reset.addEventListener("click", function () {
        choose("testing", current.testing);
        choose("stable", current.stable);
        update();
    });

    function post(action) {
        return fetch("/builds/" + encodeURIComponent(setup) + "/" + action, {
            method: "POST",
            headers: {"Content-Type": "application/x-www-form-urlencoded"},
            body: body
        }).then(function (response) {
            return response.json().then(function (data) {
                if (!response.ok) {
                    throw new Error(data.error || response.statusText);
                }
                return data;
            });
        });
    }

    function cell(row, text, className) {
        var td = document.createElement("td");
        td.textContent = text;
        if (className) {
            td.className = className;
        }
        row.append(td);
        return td;
    }

    function buildCell(row, number, builds) {
        var info = builds[String(number)] || {};
        var td = cell(row, "");
        var head = document.createElement("div");
        head.className = "mono build";
        head.textContent = number + " " + (info.commit || "").substring(0, 10);
        td.append(head);
        var subject = document.createElement("div");
        subject.className = "dim";
        subject.textContent = info.subject || "";
        td.append(subject);
    }

    function showProblem(text) {
        problem.textContent = text;
        problem.hidden = false;
    }

    function fill(report) {
        document.getElementById("review-title").textContent = "Change the tags of " + setup;
        var rows = document.getElementById("review-changes");
        rows.textContent = "";
        report.moved.forEach(function (tag) {
            var row = document.createElement("tr");
            cell(row, title(tag), "nowrap");
            buildCell(row, report.before[tag], report.builds);
            cell(row, "→");
            buildCell(row, report.after[tag], report.builds);
            rows.append(row);
        });
        var notes = document.getElementById("review-notes");
        notes.textContent = "";
        function note(text) {
            var item = document.createElement("li");
            item.textContent = text;
            notes.append(item);
        }
        report.moved.forEach(function (tag) {
            note("Launchers that follow " + title(tag) + " will be offered build " + report.after[tag] + ".");
        });
        if (report.moved.includes("testing") && !moves().includes("testing")) {
            note("Testing moves too: it can't be older than Stable.");
        }
        if (report.moved.includes("stable")) {
            note("This can't be undone: Stable only moves forward, and build " + report.after.stable +
                 " can't be removed until a newer one replaces it.");
        } else {
            note("Tags only move forward: a bad Testing build can be replaced by a newer one, or removed.");
        }
        note("The tag's downloads are written and everything is published to the " + setup + " files site.");
        document.getElementById("review-log").textContent = report.log;
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        var parameters = new URLSearchParams({serial: serial});
        moves().forEach(function (tag) {
            parameters.set(tag, chosen(tag));
        });
        body = parameters.toString();
        apply.disabled = true;
        pending.textContent = "Checking…";
        problem.hidden = true;
        confirm.disabled = false;
        cancel.disabled = false;
        post("check").then(function (report) {
            fill(report);
            update();
            dialog.showModal();
            cancel.focus();
        }).catch(function (error) {
            update();
            pending.textContent = "Refused: " + error.message + " (reload to see the current tags)";
        }).finally(function () {
            if (!dialog.open) {
                apply.disabled = !moves().length;
            }
        });
    });

    cancel.addEventListener("click", function () {
        dialog.close();
    });
    dialog.addEventListener("close", update);

    confirm.addEventListener("click", function () {
        confirm.disabled = true;
        cancel.disabled = true;
        problem.hidden = true;
        post("queue").then(function (answer) {
            var parts = answer.run.split(":");
            location.href = parts.length === 2 ? "/run/" + parts[0] + "/" + parts[1] : "/";
        }).catch(function (error) {
            // Whatever changed, what was reviewed may no longer hold: no second try from here
            cancel.disabled = false;
            showProblem("Nothing was done: " + error.message + ". Reload the page to see the current tags.");
        });
    });
    // Escape cancels, unless the job is being queued
    dialog.addEventListener("cancel", function (event) {
        if (cancel.disabled) {
            event.preventDefault();
        }
    });
})();
