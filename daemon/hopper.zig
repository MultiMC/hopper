// hopper: moves releases through their stages: a commit becomes a release, goes into the local
// files tree, and is published. daemon/hopper.py in Zig: the same commands, the same files
// (HOPPER_STATE, its .status, .lock and .logs/), the same scripts run the same way, so either
// can take over from the other.
//
//     zig-out/bin/hopper [--config FILE] watch [--once]
//     zig-out/bin/hopper [--config FILE] release <stream> <commit>
//     zig-out/bin/hopper [--config FILE] release patrons
//     zig-out/bin/hopper [--config FILE] status
//
// The jobs (lib/jobs.zig): streams, each a branch of a repository and the script that builds a
// release from it (launcher, translations, website), and scheduled jobs with a cron-style
// schedule (patrons; lib/cron.zig), which run once for the most recent scheduled time they
// haven't handled. A release goes through files/deploy.py and files/publish.py, except the
// website's, which its own website/publish.py takes to the test site.
//
// watch    every WATCH_INTERVAL seconds, look at the head of each stream's branch and at each
//          schedule. What isn't handled yet goes through its build, deploy and publish, each
//          step only if the one before it succeeded. --once: look once, then exit.
// release  run the same steps for the given stream and commit, or scheduled job, now (e.g. to
//          retry a failed one)
// status   what has been handled, newest first
//
// hopper builds and publishes nothing itself: it runs the same scripts a person would. Each
// run is recorded in HOPPER_STATE with when each of its steps started and ended, its output in
// HOPPER_STATE.logs/. `watch` also keeps HOPPER_STATE.status (what it saw at its last look)
// and its own log, HOPPER_STATE.logs/hopper.log. A run that fails is not retried
// automatically. Stopping hopper (SIGTERM, Ctrl-C) stops the step that is running (SIGTERM,
// then SIGKILL after a minute) and records the run as interrupted. A stream with no runs yet
// adopts its current head as released, without building it. While a release build runs
// (e.g. one started by hand), the look skips what it would release. One hopper per state file.

const std = @import("std");
const hopper = @import("hopper");
const system = hopper.system;
const Time = hopper.time.Time;
const Job = hopper.jobs.Job;
const Entry = hopper.state.Entry;
const State = hopper.state.State;
const Status = hopper.state.Status;
const Settings = hopper.Settings;

const Allocator = std.mem.Allocator;
const posix = std.posix;

const usage =
    \\usage: zig-out/bin/hopper [--config FILE] watch [--once]
    \\       zig-out/bin/hopper [--config FILE] release <stream> <commit>
    \\       zig-out/bin/hopper [--config FILE] release <scheduled job>
    \\       zig-out/bin/hopper [--config FILE] status
    \\
;

// --- stopping -----------------------------------------------------------------------------

/// The signal that stopped hopper (SIGTERM, what a service manager sends; SIGINT, Ctrl-C);
/// 0 until one does. Checked where hopper can stop cleanly: between steps, and while it waits.
var stop_signal = std.atomic.Value(u32).init(0);
/// The step running now (0: none), which the signal is passed to (common.stop)
var step_pid = std.atomic.Value(i32).init(0);

/// How long a step has to stop before it's killed
const stop_grace_seconds = 60;

fn onStop(sig: posix.SIG) callconv(.c) void {
    stop_signal.store(@backingInt(sig), .seq_cst);
    const pid = step_pid.load(.seq_cst);
    if (pid != 0) posix.kill(pid, .TERM) catch {};
}

fn stopOnSignals(io: std.Io) !void {
    const action: posix.Sigaction = .{
        .handler = .{ .handler = onStop },
        .mask = posix.sigemptyset(),
        .flags = posix.SA.RESTART,
    };
    posix.sigaction(.TERM, &action, null);
    posix.sigaction(.INT, &action, null);
    const killer = try std.Thread.spawn(.{}, killAfterStop, .{io});
    killer.detach();
}

/// A step that hasn't stopped a minute after it was asked to is killed.
fn killAfterStop(io: std.Io) void {
    while (!stopped()) io.sleep(.fromMilliseconds(250), .awake) catch {};
    io.sleep(.fromSeconds(stop_grace_seconds), .awake) catch {};
    const pid = step_pid.load(.seq_cst);
    if (pid != 0) posix.kill(pid, .KILL) catch {};
}

fn stopped() bool {
    return stop_signal.load(.seq_cst) != 0;
}

/// Sleep, unless hopper is stopped meanwhile.
fn sleep(io: std.Io, seconds: u64) error{Stopped}!void {
    var left_ms = seconds * 1000;
    while (left_ms > 0) {
        if (stopped()) return error.Stopped;
        const slice = @min(left_ms, 250);
        io.sleep(.fromMilliseconds(@intCast(slice)), .awake) catch {};
        left_ms -= slice;
    }
    if (stopped()) return error.Stopped;
}

/// What a program run returned: its exit status, or minus the signal that ended it (as
/// Python's returncode), and its standard output if it was asked for.
const Ran = struct {
    exit: i64,
    output: []const u8 = "",
};

/// Run a program and wait for it; if hopper is stopped meanwhile, so is the program (the caller
/// sees it with `stopped()` once the program has ended).
fn run(io: std.Io, arena: Allocator, argv: []const []const u8, stdout: std.process.SpawnOptions.StdIo, stderr: std.process.SpawnOptions.StdIo) !Ran {
    var child = try std.process.spawn(io, .{ .argv = argv, .stdout = stdout, .stderr = stderr });
    step_pid.store(child.id.?, .seq_cst);
    defer step_pid.store(0, .seq_cst);
    // A stop that came before the program could be told
    if (stopped()) posix.kill(child.id.?, .TERM) catch {};

    var ran: Ran = .{ .exit = 0 };
    if (stdout == .pipe) {
        var buffer: [4096]u8 = undefined;
        var reader = child.stdout.?.readerStreaming(io, &buffer);
        ran.output = reader.interface.allocRemaining(arena, .unlimited) catch |err| switch (err) {
            error.ReadFailed => return reader.err.?,
            else => return err,
        };
    }
    ran.exit = switch (try child.wait(io)) {
        .exited => |code| code,
        .signal => |sig| -@as(i64, @backingInt(sig)),
        else => -1,
    };
    return ran;
}

// --- hopper -------------------------------------------------------------------------------

/// What one hopper works with: its settings, its state (and the memory it's in), and its
/// files next to the state.
const Hopper = struct {
    io: std.Io,
    paths: hopper.Paths,
    settings: Settings,
    /// The settings file, absolute, as the scripts get it (--config), or null
    config: ?[]const u8,
    state_path: []const u8,
    logs: []const u8,
    state: State,
    /// What the state's runs are in: added runs and what they record go here
    arena: Allocator,
    /// Why the last look failed, as common.last_error
    problem: ?[]const u8 = null,

    fn save(h: *Hopper) !void {
        try h.state.write(h.io, h.state_path);
    }

    /// Stop this look (common.die): log why, keep it for the status.
    fn fail(h: *Hopper, comptime format: []const u8, args: anytype) error{ LookFailed, OutOfMemory } {
        const message = try std.fmt.allocPrint(h.arena, format, args);
        h.problem = message;
        system.logLine(h.io, "error: {s}", .{message});
        return error.LookFailed;
    }

    fn log(h: *Hopper, comptime format: []const u8, args: anytype) void {
        system.log(h.io, "hopper", format, args);
    }

    /// Runs still "running" when no hopper holds the lock are left over from a hopper that
    /// was killed or crashed.
    fn interruptStale(h: *Hopper, scratch: Allocator) !void {
        for (h.state.jobs.map.keys(), h.state.jobs.map.values()) |name, *runs| {
            for (runs.list.items) |*entry| {
                if (entry.status != .running) continue;
                entry.status = .interrupted;
                h.log("{s} {s} was left running by a previous hopper; recorded as interrupted", .{ name, try entry.label(scratch) });
            }
        }
        try h.save();
    }

    fn handledCommit(h: *Hopper, job: Job, commit: []const u8) bool {
        for (h.state.runs(job.name)) |entry| {
            if (entry.commit) |c| if (std.mem.eql(u8, c, commit)) return true;
        }
        return false;
    }

    fn handledTime(h: *Hopper, job: Job, scheduled: Time) bool {
        for (h.state.runs(job.name)) |entry| {
            if (entry.scheduled) |s| if (s.seconds == scheduled.seconds) return true;
        }
        return false;
    }

    /// The commit at the head of a stream's branch
    fn head(h: *Hopper, scratch: Allocator, job: Job) ![]const u8 {
        const stream = job.kind.stream;
        const repo = h.settings.get(stream.repo_setting);
        const branch = h.settings.get(stream.branch_setting);
        const ref = try std.fmt.allocPrint(scratch, "refs/heads/{s}", .{branch});
        const argv = [_][]const u8{ "git", "ls-remote", repo, ref };
        const ran = try run(h.io, scratch, &argv, .pipe, .inherit);
        if (stopped()) return error.Stopped;
        if (ran.exit != 0) return h.fail("command failed (exit {d}): {s}", .{ ran.exit, try std.mem.join(scratch, " ", &argv) });
        const line = std.mem.trim(u8, ran.output, " \t\r\n");
        if (line.len == 0) return h.fail("{s} has no branch {s}", .{ repo, branch });
        return line[0 .. std.mem.indexOfAny(u8, line, " \t") orelse line.len];
    }

    fn watch(h: *Hopper) !void {
        const io = h.io;
        const interval = try std.fmt.parseInt(u64, h.settings.get("WATCH_INTERVAL"), 10);
        for (hopper.jobs.all) |job| switch (job.kind) {
            .stream => |stream| if (job.enabled(h.settings)) {
                h.log("watching {s}: {s} {s}", .{ job.name, h.settings.get(stream.repo_setting), h.settings.get(stream.branch_setting) });
            } else {
                h.log("watching {s}: off ({s} is empty)", .{ job.name, stream.repo_setting });
            },
            .scheduled => {},
        };
        for (hopper.jobs.all) |job| switch (job.kind) {
            .stream => {},
            .scheduled => |scheduled| if (job.enabled(h.settings)) {
                h.log("scheduled {s}: {s}", .{ job.name, h.settings.get(scheduled.schedule_setting) });
            } else {
                h.log("scheduled {s}: off ({s} is empty)", .{ job.name, scheduled.enabled_setting });
            },
        };
        h.log("every {d} s", .{interval});

        var status: StatusFile = try .start(h, interval);
        try status.save(io);
        var scratch_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer scratch_state.deinit();
        while (true) {
            _ = scratch_state.reset(.retain_capacity);
            if (h.look(scratch_state.allocator(), &status)) {
                status.status.problem = null;
            } else |err| switch (err) {
                error.LookFailed => {
                    h.log("this look failed (see above); trying again in {d} s", .{interval});
                    status.status.problem = h.problem;
                },
                else => return err,
            }
            status.status.looked = .now(io);
            try status.save(io);
            try sleep(io, interval);
        }
    }

    /// One look at every stream and scheduled job; handles at most one release each.
    /// `scratch` is this look's memory.
    fn look(h: *Hopper, scratch: Allocator, status: ?*StatusFile) !void {
        const release_lock = try h.paths.releaseLock(scratch);
        for (hopper.jobs.all) |job| {
            if (job.kind != .stream or !job.enabled(h.settings)) continue;
            const commit = try h.head(scratch, job);
            if (status) |s| try s.saw(job, .{ .head = commit, .looked = .now(h.io) });
            if (h.handledCommit(job, commit)) continue;
            if (h.state.runs(job.name).len == 0) {
                _ = try h.state.add(h.arena, job.name, .{ .commit = try h.arena.dupe(u8, commit), .time = .now(h.io), .status = .adopted });
                try h.save();
                h.log("{s}: first look, {s} adopted as already released (not built)", .{ job.name, commit[0..@min(commit.len, 10)] });
                continue;
            }
            if (try system.isLocked(h.io, release_lock)) {
                h.log("{s}: new commit {s}, but a release build is running; looking again later", .{ job.name, commit[0..@min(commit.len, 10)] });
                continue;
            }
            try h.release(job, .{ .commit = commit });
        }

        for (hopper.jobs.all) |job| {
            if (job.kind != .scheduled or !job.enabled(h.settings)) continue;
            const schedule = try job.schedule(h.settings);
            const now: Time = .now(h.io);
            const latest = schedule.latest(now);
            if (status) |s| try s.saw(job, .{ .latest = latest, .next = schedule.next(now), .looked = now });
            const scheduled = latest orelse continue;
            if (h.handledTime(job, scheduled)) continue;
            if (try system.isLocked(h.io, release_lock)) {
                h.log("{s}: due ({f}), but a release build is running; looking again later", .{ job.name, scheduled });
                continue;
            }
            try h.release(job, .{ .scheduled = .{ .time = scheduled } });
        }
    }

    /// The job's build, then its deploy and publish, each only if the one before succeeded,
    /// for what the entry says (a commit or a scheduled time). Their output goes to one log
    /// file per run.
    fn release(h: *Hopper, job: Job, what: What) !void {
        var r: Run = try .start(h, job, what);
        defer r.log_file.close(h.io);
        r.steps() catch |err| {
            if (r.entry.status != .published) {
                // Stopped (or failed) in a step; in the cleanup after publishing it's still published
                r.finish(.interrupted, "stopped during {s}; not retried automatically", .{r.entry.step orelse "?"}) catch {};
            } else {
                r.entry.step = null;
                h.save() catch {};
            }
            return err;
        };
    }

    fn printStatus(h: *Hopper, arena: Allocator) !void {
        const Item = struct { job: []const u8, entry: Entry };
        var items: std.ArrayList(Item) = .empty;
        for (h.state.jobs.map.keys(), h.state.jobs.map.values()) |name, runs| {
            for (runs.list.items) |entry| try items.append(arena, .{ .job = name, .entry = entry });
        }
        std.mem.sort(Item, items.items, {}, struct {
            fn newerFirst(_: void, a: Item, b: Item) bool {
                return a.entry.time.order(b.entry.time) == .gt;
            }
        }.newerFirst);

        var buffer: [4096]u8 = undefined;
        var stdout: std.Io.File.Writer = .init(.stdout(), h.io, &buffer);
        const out = &stdout.interface;
        for (items.items) |item| {
            const entry = item.entry;
            var build: []const u8 = "";
            if (entry.build) |number| build = try std.fmt.allocPrint(arena, "{d}", .{number});
            var status: []const u8 = @tagName(entry.status);
            if (entry.step) |step| status = try std.fmt.allocPrint(arena, "{s} ({s})", .{ status, step });
            try out.print("{f}  {s:<12} {s}  {s:<6} {s:<22} {s}\n", .{ entry.time, item.job, try entry.label(arena), build, status, entry.release orelse "" });
        }
        try out.flush();
    }
};

/// HOPPER_STATE.status, what the running `watch` saw, for the overview (lib/state.zig, Status).
/// Only written by `watch`; whether a hopper is running at all is the state's lock.
const StatusFile = struct {
    path: []const u8,
    status: Status,
    /// What the status's own values are in (a head is only kept again when it changes)
    arena: Allocator,

    fn start(h: *Hopper, interval: u64) !StatusFile {
        return .{
            .path = try Status.path(h.arena, h.state_path),
            .arena = h.arena,
            .status = .{
                .pid = std.os.linux.getpid(),
                .started = .now(h.io),
                .config = h.config,
                .code = try system.gitDescribe(h.io, h.arena, h.paths.hopper) orelse "",
                .interval = @intCast(interval),
            },
        };
    }

    fn saw(file: *StatusFile, job: Job, seen: hopper.state.Seen) !void {
        var kept = seen;
        const entry = try file.status.jobs.map.getOrPut(file.arena, job.name);
        if (seen.head) |head| {
            const same = entry.found_existing and entry.value_ptr.head != null and std.mem.eql(u8, entry.value_ptr.head.?, head);
            kept.head = if (same) entry.value_ptr.head else try file.arena.dupe(u8, head);
        }
        entry.value_ptr.* = kept;
    }

    fn save(file: *StatusFile, io: std.Io) !void {
        try file.status.write(io, file.path);
    }
};

/// What a run releases: a stream's commit, or a scheduled job's time (by hand: `release`).
const What = union(enum) {
    commit: []const u8,
    scheduled: struct { time: Time, by_hand: bool = false },
};

/// One release going through the steps, recorded in the state as it goes. The entry records
/// the full commit from the release's manifest (a commit given by hand may be abbreviated).
const Run = struct {
    h: *Hopper,
    job: Job,
    entry: *Entry,
    log_path: []const u8,
    log_file: std.Io.File,

    fn start(h: *Hopper, job: Job, what: What) !Run {
        const io = h.io;
        const now: Time = .now(io);
        var entry: Entry = .{ .time = now, .status = .running, .step = "build" };
        switch (what) {
            .commit => |commit| entry.commit = try h.arena.dupe(u8, commit),
            .scheduled => |scheduled| {
                entry.scheduled = scheduled.time;
                if (scheduled.by_hand) entry.by_hand = true;
            },
        }
        const label = try entry.label(h.arena);

        const l = now.local();
        const log_path = try std.fmt.allocPrint(h.arena, "{s}/{d}{d:0>2}{d:0>2}-{d:0>2}{d:0>2}{d:0>2}-{s}-{s}.log", .{
            h.logs, l.year, l.month, l.day, l.hour, l.minute, l.second, job.name, label,
        });
        try std.Io.Dir.cwd().createDirPath(io, h.logs);
        const log_file = try std.Io.Dir.cwd().createFile(io, log_path, .{});
        entry.log = log_path;

        const added = try h.state.add(h.arena, job.name, entry);
        try h.save();
        h.log("{s}: releasing {s} (log: {s})", .{ job.name, label, log_path });
        return .{ .h = h, .job = job, .entry = added, .log_path = log_path, .log_file = log_file };
    }

    fn steps(r: *Run) !void {
        const h = r.h;
        var scratch_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
        defer scratch_state.deinit();
        const scratch = scratch_state.allocator();

        var build_args: []const []const u8 = &.{};
        if (r.entry.commit) |commit| build_args = try scratch.dupe([]const u8, &.{commit});
        const built = try r.step(scratch, "build", r.job.build_script, build_args, .capture);
        try r.log_file.writeStreamingAll(h.io, built.output);
        // The release: the last line of what the build printed
        const output = std.mem.trim(u8, built.output, " \t\r\n");
        var release: ?[]const u8 = null;
        if (output.len > 0) {
            const line_start = if (std.mem.lastIndexOfScalar(u8, output, '\n')) |newline| newline + 1 else 0;
            release = std.mem.trimEnd(u8, output[line_start..], "\r");
            try r.recordRelease(scratch, release.?);
        }
        if (built.exit != 0) return r.finish(.failed, "build failed, nothing deployed", .{});
        return r.deployAndPublish(scratch, release orelse return r.finish(.failed, "the build named no release, nothing deployed", .{}));
    }

    fn deployAndPublish(r: *Run, scratch: Allocator, release: []const u8) !void {
        const h = r.h;
        if (r.job.publish_script) |script| {
            if ((try r.step(scratch, "publish", script, &.{release}, .log)).exit != 0) {
                return r.finish(.failed, "publish failed", .{});
            }
        } else if ((try r.step(scratch, "deploy", "files/deploy.py", &.{release}, .log)).exit != 0) {
            return r.finish(.failed, "deploy failed, nothing published", .{});
        } else if ((try r.step(scratch, "publish", "files/publish.py", &.{}, .log)).exit != 0) {
            return r.finish(.failed, "publish failed", .{});
        }
        if (r.entry.build) |build| {
            try r.finish(.published, "build {d} published", .{build});
        } else {
            try r.finish(.published, "{s} published", .{std.fs.path.basename(release)});
        }

        // Housekeeping, not part of the release: a failure is only logged
        if ((try r.step(scratch, "cleanup", "daemon/releases.py", &.{"cleanup"}, .log)).exit != 0) {
            h.log("{s} {s}: the release cleanup failed (see {s})", .{ r.job.name, try r.entry.label(scratch), r.log_path });
        }
        r.entry.step = null;
        try h.save();
    }

    /// Run one script, with the settings file; its stderr (and stdout, unless it's captured)
    /// goes to the run's log. If hopper is stopped meanwhile, the script is stopped too.
    fn step(r: *Run, scratch: Allocator, name: []const u8, script: []const u8, args: []const []const u8, stdout: enum { capture, log }) !Ran {
        const h = r.h;
        const io = h.io;
        const started: Time = .now(io);
        try r.entry.steps.list.append(h.arena, .{ .step = name, .start = started });
        const record = &r.entry.steps.list.items[r.entry.steps.list.items.len - 1];
        r.entry.step = name;
        try h.save();

        const header = try std.fmt.allocPrint(scratch, "\n=== {f} {s}: {s} {s}\n", .{ started, name, script, try std.mem.join(scratch, " ", args) });
        try r.log_file.writeStreamingAll(io, header);

        var argv: std.ArrayList([]const u8) = .empty;
        try argv.append(scratch, try std.fs.path.join(scratch, &.{ h.paths.hopper, script }));
        if (h.config) |config| try argv.appendSlice(scratch, &.{ "--config", config });
        try argv.appendSlice(scratch, args);

        // When it ended (and how) is saved with whatever comes next
        const ran = run(io, scratch, argv.items, if (stdout == .capture) .pipe else .{ .file = r.log_file }, .{ .file = r.log_file }) catch |err| {
            record.end = .now(io);
            return err;
        };
        record.end = .now(io);
        record.exit = ran.exit;
        if (stopped()) return error.Stopped;
        return ran;
    }

    fn recordRelease(r: *Run, scratch: Allocator, release: []const u8) !void {
        const h = r.h;
        r.entry.release = try h.arena.dupe(u8, release);
        const manifest_path = try std.fs.path.join(scratch, &.{ release, "manifest.json" });
        if (try system.readFile(h.io, scratch, manifest_path)) |text| {
            const manifest = try std.json.parseFromSliceLeaky(std.json.Value, scratch, text, .{});
            if (manifest == .object) {
                if (manifest.object.get("commit")) |commit| if (commit == .string) {
                    r.entry.commit = try h.arena.dupe(u8, commit.string);
                };
                if (manifest.object.get("build_number")) |build| if (build == .integer) {
                    r.entry.build = build.integer;
                };
            }
        }
        try h.save();
    }

    fn finish(r: *Run, status: hopper.state.RunStatus, comptime format: []const u8, args: anytype) !void {
        const h = r.h;
        r.entry.status = status;
        r.entry.finished = .now(h.io);
        if (status == .published) r.entry.step = null;
        try h.save();
        var buffer: [64]u8 = undefined;
        var fixed: std.heap.FixedBufferAllocator = .init(&buffer);
        const label = r.entry.label(fixed.allocator()) catch "?";
        h.log("{s} {s}: " ++ format ++ " (log: {s})", .{ r.job.name, label } ++ args ++ .{r.log_path});
    }
};

// --- the commands -------------------------------------------------------------------------

const Command = union(enum) {
    watch: struct { once: bool },
    release: struct { job: Job, commit: ?[]const u8 },
    status,
};

fn parseArgs(args: []const []const u8) struct { config: ?[]const u8, command: Command } {
    var config: ?[]const u8 = null;
    var words: [3][]const u8 = undefined;
    var count: usize = 0;
    var once = false;
    var i: usize = 1;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "--config")) {
            i += 1;
            if (i == args.len) system.fail("--config needs a file\n{s}", .{usage});
            config = args[i];
        } else if (std.mem.eql(u8, arg, "--once")) {
            once = true;
        } else if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            std.debug.print("{s}", .{usage});
            std.process.exit(0);
        } else if (count < words.len) {
            words[count] = arg;
            count += 1;
        } else {
            system.fail("unknown argument {s}\n{s}", .{ arg, usage });
        }
    }
    if (count == 0) system.fail("watch, release or status?\n{s}", .{usage});
    const name = words[0];
    if (once and !std.mem.eql(u8, name, "watch")) system.fail("--once is for watch\n{s}", .{usage});
    if (std.mem.eql(u8, name, "watch") and count == 1) return .{ .config = config, .command = .{ .watch = .{ .once = once } } };
    if (std.mem.eql(u8, name, "status") and count == 1) return .{ .config = config, .command = .status };
    if (std.mem.eql(u8, name, "release") and count >= 2) {
        const job = hopper.jobs.find(words[1]) orelse system.fail("no job {s}\n{s}", .{ words[1], usage });
        const commit: ?[]const u8 = if (count == 3) words[2] else null;
        if ((job.kind == .stream) != (commit != null)) system.fail("a stream needs a commit, a scheduled job none\n{s}", .{usage});
        return .{ .config = config, .command = .{ .release = .{ .job = job, .commit = commit } } };
    }
    system.fail("unknown command\n{s}", .{usage});
}

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const arena = init.arena.allocator();
    const parsed = parseArgs(try init.minimal.args.toSlice(arena));

    try stopOnSignals(io);
    try hopper.time.useLocalZone(io, arena);
    const paths = try hopper.Paths.find(init);
    var config: ?[]const u8 = null;
    if (parsed.config) |file| config = try std.Io.Dir.cwd().realPathFileAlloc(io, file, arena);
    const settings = try hopper.settings.load(init, paths, config);
    for (hopper.jobs.all) |job| {
        if (job.kind == .scheduled and job.enabled(settings)) {
            _ = job.schedule(settings) catch system.fail("{s}: not a schedule: {s}", .{ job.kind.scheduled.schedule_setting, settings.get(job.kind.scheduled.schedule_setting) });
        }
    }

    const state_path = settings.get("HOPPER_STATE");
    const state = (hopper.state.State.read(io, arena, state_path) catch |err| switch (err) {
        error.NotWhatHopperWrites => system.fail("{s}: not what hopper writes there", .{state_path}),
        else => return err,
    });
    var h: Hopper = .{
        .io = io,
        .paths = paths,
        .settings = settings,
        .config = config,
        .state_path = state_path,
        .logs = try std.mem.concat(arena, u8, &.{ state_path, ".logs" }),
        .state = state.value,
        .arena = state.arena.allocator(),
    };
    if (parsed.command == .status) return h.printStatus(arena);

    const busy = try std.fmt.allocPrint(arena, "another hopper uses {s}", .{state_path});
    if (std.fs.path.dirname(state_path)) |dir| try std.Io.Dir.cwd().createDirPath(io, dir);
    const held = try system.lock(io, try std.mem.concat(arena, u8, &.{ state_path, ".lock" }), busy);
    defer held.close(io);

    var log_file: ?std.Io.File = null;
    if (parsed.command == .watch and !parsed.command.watch.once) {
        log_file = try system.openAppend(io, try std.fs.path.join(arena, &.{ h.logs, "hopper.log" }));
        system.logAlsoTo(log_file);
    }
    commands(&h, arena, parsed.command) catch |err| switch (err) {
        error.Stopped => {
            // As Python's: 128 + SIGTERM, which the service counts as a clean stop
            const signal = stop_signal.load(.seq_cst);
            if (log_file != null) {
                if (signal == @backingInt(posix.SIG.INT)) {
                    system.logLine(io, "stopped (Ctrl-C)", .{});
                } else {
                    system.logLine(io, "stopped (exit {d})", .{128 + signal});
                }
            }
            std.process.exit(@intCast(128 + signal));
        },
        error.LookFailed => std.process.exit(1),
        else => {
            if (log_file) |file| {
                const now = Time.now(io).local();
                var buffer: [256]u8 = undefined;
                const line = std.fmt.bufPrint(&buffer, "{d}-{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}:{d:0>2} crashed: {s}\n", .{ now.year, now.month, now.day, now.hour, now.minute, now.second, @errorName(err) }) catch "crashed\n";
                file.writeStreamingAll(io, line) catch {};
            }
            return err;
        },
    };
}

fn commands(h: *Hopper, arena: Allocator, command: Command) !void {
    try h.interruptStale(arena);
    switch (command) {
        .release => |r| {
            const job = r.job;
            if (!job.enabled(h.settings)) switch (job.kind) {
                .stream => |stream| system.fail("error: {s} isn't enabled with these settings ({s} is empty)", .{ job.name, stream.repo_setting }),
                .scheduled => |scheduled| system.fail("error: {s} isn't enabled with these settings ({s})", .{ job.name, scheduled.enabled_setting }),
            };
            if (r.commit) |commit| {
                try h.release(job, .{ .commit = commit });
            } else {
                // This minute, as the clock here shows it
                const this_minute: Time = .fromWall(@divFloor(Time.now(h.io).wall(), 60) * 60);
                try h.release(job, .{ .scheduled = .{ .time = this_minute, .by_hand = true } });
            }
        },
        .watch => |watch| if (watch.once) {
            var scratch_state = std.heap.ArenaAllocator.init(std.heap.page_allocator);
            defer scratch_state.deinit();
            try h.look(scratch_state.allocator(), null);
        } else {
            try h.watch();
        },
        .status => unreachable,
    }
}
