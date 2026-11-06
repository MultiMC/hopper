// The actions on a setup's builds, in steps, as they aren't to be easy to get wrong: choose,
// ask daemon/builds.py what that would do and show it in a popup, and only Confirm queues the
// job. All the rules are in the scripts; this only keeps the choices sensible, and the
// server refuses what they let through. Withdrawing also takes typing the build's number.
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
    var pageProblem = document.getElementById("page-problem");
    var table = document.getElementById("review-table");
    var typed = document.getElementById("review-typed");
    var typedInput = document.getElementById("review-typed-input");
    var body = null;
    var mustType = null;
    var busy = false;

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

    function megabytes(bytes) {
        return Math.round(bytes / 1048576) + " MB";
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

    function resetChoices() {
        choose("testing", current.testing);
        choose("stable", current.stable);
        update();
    }
    // A reload can bring back what the browser remembered of the form
    window.addEventListener("pageshow", resetChoices);
    reset.addEventListener("click", resetChoices);

    function post(step) {
        return fetch("/builds/" + encodeURIComponent(setup) + "/" + step, {
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

    function describe(number, builds) {
        var info = builds[String(number)] || {};
        return number + " " + (info.commit || "").substring(0, 10);
    }

    function buildCell(row, number, builds) {
        var td = cell(row, "");
        var head = document.createElement("div");
        head.className = "mono build";
        head.textContent = describe(number, builds);
        td.append(head);
        var subject = document.createElement("div");
        subject.className = "dim";
        subject.textContent = (builds[String(number)] || {}).subject || "";
        td.append(subject);
    }

    function note(text) {
        var item = document.createElement("li");
        item.textContent = text;
        document.getElementById("review-notes").append(item);
    }

    function fillMoved(report) {
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
        table.hidden = !report.moved.length;
    }

    function fillTag(report) {
        fillMoved(report);
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
    }

    function fillRemove(report) {
        fillMoved(report);
        var info = report.builds[String(report.build)] || {};
        note("Build " + describe(report.build, report.builds) + (info.subject ? " (" + info.subject + ")" : "") +
             " is removed for good. Its number is never used again.");
        note("Its manifest and the " + report.files + " content files only it lists (" + megabytes(report.bytes) +
             ") leave the " + setup + " files site.");
        if (report.archive_removed) {
            note("It has been Stable: its archive, otherwise kept for good, is removed too, and with it the record of that.");
        }
        report.moved.forEach(function (tag) {
            note(title(tag) + " moves back to build " + report.after[tag] + ".");
        });
        note("Launchers already on it stay on it until the next build; they can tell the user it was withdrawn.");
    }

    function fillPrune(report) {
        table.hidden = true;
        note("Counted toward the cap: " + megabytes(report.counted) + " of " + megabytes(report.cap) + ". " +
             report.builds_total + " builds, " + report.protected + " of them never purged.");
        if (!report.purged.length) {
            note("Nothing to purge.");
            return;
        }
        report.purged.forEach(function (build) {
            var info = report.builds[String(build)] || {};
            note("Purge " + describe(build, report.builds) + (info.subject ? " (" + info.subject + ")" : ""));
        });
        note("That frees " + megabytes(report.bytes) + ". They aren't withdrawn and no tag moves; a Launcher on one still updates.");
        note("This can't be undone, and everything is published to the " + setup + " files site.");
    }

    // Asks what the action would do, and shows that. typeThis: what must be typed to go on.
    function review(heading, parameters, fillReport, danger, typeThis) {
        parameters.set("serial", serial);
        body = parameters.toString();
        pageProblem.hidden = true;
        problem.hidden = true;
        return post("check").then(function (report) {
            document.getElementById("review-title").textContent = heading;
            document.getElementById("review-notes").textContent = "";
            fillReport(report);
            document.getElementById("review-log").textContent = report.log;
            var nothing = report.action === "prune" && !report.purged.length;
            mustType = typeThis;
            typed.hidden = !typeThis;
            typedInput.value = "";
            if (typeThis) {
                typed.querySelector("label").textContent = "Type " + typeThis + " to confirm:";
            }
            confirm.hidden = nothing;
            confirm.disabled = Boolean(typeThis);
            confirm.classList.toggle("danger", danger);
            cancel.textContent = nothing ? "Close" : "Cancel";
            cancel.disabled = false;
            dialog.showModal();
            cancel.focus();
        }).catch(function (error) {
            pageProblem.textContent = "Refused: " + error.message + ". Reload the page to see the current state.";
            pageProblem.hidden = false;
        });
    }

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        var parameters = new URLSearchParams({action: "tag"});
        moves().forEach(function (tag) {
            parameters.set(tag, chosen(tag));
        });
        apply.disabled = true;
        pending.textContent = "Checking…";
        review("Change the tags of " + setup, parameters, fillTag, false, null).finally(update);
    });

    form.addEventListener("click", function (event) {
        var build = event.target.dataset && event.target.dataset.withdraw;
        if (!build) {
            return;
        }
        event.target.disabled = true;
        review("Withdraw build " + build + " from " + setup, new URLSearchParams({action: "remove", build: build}),
               fillRemove, true, build).finally(function () {
            event.target.disabled = false;
        });
    });

    document.getElementById("prune").addEventListener("submit", function (event) {
        event.preventDefault();
        var cap = document.getElementById("prune-cap").value;
        var parameters = new URLSearchParams({action: "prune"});
        if (cap !== "") {
            parameters.set("cap_mb", cap);
        }
        review("Prune " + setup, parameters, fillPrune, true, null);
    });

    typedInput.addEventListener("input", function () {
        confirm.disabled = busy || typedInput.value.trim() !== mustType;
    });
    // Enter in the box isn't a way to confirm
    typedInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            event.preventDefault();
        }
    });

    cancel.addEventListener("click", function () {
        dialog.close();
    });

    confirm.addEventListener("click", function () {
        busy = true;
        confirm.disabled = true;
        cancel.disabled = true;
        typedInput.disabled = true;
        problem.hidden = true;
        post("queue").then(function (answer) {
            var parts = answer.run.split(":");
            location.href = parts.length === 2 ? "/run/" + parts[0] + "/" + parts[1] : "/";
        }).catch(function (error) {
            // Whatever changed, what was reviewed may no longer hold: no second try from here
            cancel.disabled = false;
            problem.textContent = "Nothing was done: " + error.message + ". Reload the page to see the current state.";
            problem.hidden = false;
        });
    });
    // Escape cancels, unless the job is being queued
    dialog.addEventListener("cancel", function (event) {
        if (cancel.disabled) {
            event.preventDefault();
        }
    });
})();
