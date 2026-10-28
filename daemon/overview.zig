// What hopper and meta are doing on this machine, gathered from their files and systemd.
//
//     zig-out/bin/overview [--json]
//
// For every setup (each settings file in config/ but defaults.env: production, files2), from
// its files: whether its hopper runs (the pid holding HOPPER_STATE's lock) and what it last saw
// (HOPPER_STATE.status), its service (hopper@<setup>), each job's last run, what's live (the
// published snapshot, the update channels, the website), and its runs, newest first; a file
// of it that isn't what hopper writes there is its "error" instead, and the other setups still
// show. For the machine: whether a release build runs (the pid holding its lock), the hopper checkout's
// commit, free space, and meta's service and timer.
//
// It only reads: run it any time, as the user hopper runs as. Without --json, a short text
// summary. The JSON is what other views read.
//
// The Zig version of daemon/overview.py, which it should match.

const std = @import("std");
const hopper = @import("hopper");

const system = hopper.system;
const Settings = hopper.Settings;
const Entry = hopper.state.Entry;
const Job = hopper.jobs.Job;
const Time = hopper.time.Time;

/// The newest runs listed per setup
const max_runs = 100;

const unit_properties = [_][]const u8{
    "ActiveState",          "SubState",               "Result",                 "UnitFileState",
    "ActiveEnterTimestamp", "InactiveEnterTimestamp", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
    "ExecMainStatus",       "NRestarts",              "MainPID",
};
const timer_properties = [_][]const u8{ "NextElapseUSecRealtime", "LastTriggerUSec" };

const usage =
    \\usage: overview [--json]
    \\
    \\What hopper and meta are doing on this machine. Only reads.
    \\
    \\  --json   everything, as JSON
    \\
;

// --- what it shows ------------------------------------------------------------------------

pub const Overview = struct {
    time: Time,
    host: []const u8,
    code: ?[]const u8,
    release_build: ?i64,
    disk: system.DiskUsage,
    setups: []const Setup,
    meta: Meta,
};

pub const Meta = struct {
    service: std.json.Value,
    timer: std.json.Value,
};

pub const Setup = struct {
    name: []const u8,
    config: []const u8,
    running: ?i64,
    service: std.json.Value,
    status: ?hopper.state.Status = null,
    log: []const u8,
    jobs: []const JobSummary = &.{},
    live: ?Live = null,
    runs: []const RunSummary = &.{},
    /// A file of the setup that isn't what hopper writes there, in place of what its files say
    @"error": ?[]const u8 = null,
};

pub const JobSummary = struct {
    name: []const u8,
    kind: []const u8,
    enabled: bool,
    /// A stream's repository and branch, a scheduled job's schedule
    source: []const u8,
    seen: ?hopper.state.Seen,
    last: ?RunSummary,
    last_published: ?RunSummary,
    /// For a stream: whether a run has the head it last saw
    head_handled: ?bool,
};

/// A state entry, with its job, a label, and how long it took (so far).
/// FIXME: does this really need the custom jsonStringify?
pub const RunSummary = struct {
    entry: Entry,
    job: []const u8,
    label: []const u8,
    seconds: ?i64,

    /// The entry's fields as they are in the state file (the ones it doesn't have, null, left
    /// out), then the summary's.
    pub fn jsonStringify(run: RunSummary, writer: *std.json.Stringify) !void {
        try writer.beginObject();
        inline for (@typeInfo(Entry).@"struct".field_names) |name| {
            const value = @field(run.entry, name);
            if (isPresent(value)) {
                try writer.objectField(name);
                try writer.write(value);
            }
        }
        try writer.objectField("job");
        try writer.write(run.job);
        try writer.objectField("label");
        try writer.write(run.label);
        try writer.objectField("seconds");
        try writer.write(run.seconds);
        try writer.endObject();
    }

    fn isPresent(value: anytype) bool {
        if (@typeInfo(@TypeOf(value)) == .optional) return value != null;
        return true;
    }
};

pub const Live = struct {
    /// The files tree's last published snapshot
    published: ?Published,
    /// The newest version in each update channel: {channel: {version id: [platforms]}}
    channels: std.json.Value,
    /// What's live of the website: {target: release}
    website: std.json.Value,
};

pub const Published = struct {
    snapshot: []const u8,
    created: ?Time = null,
    kind: ?[]const u8 = null,
    about: ?[]const u8 = null,
};

// --- gathering it -------------------------------------------------------------------------

/// What every part needs: where things are, the clock, memory (freed when the program ends).
pub const Context = struct {
    io: std.Io,
    arena: std.mem.Allocator,
    paths: hopper.Paths,
    now: Time,
};

pub fn gather(init: std.process.Init, context: Context) !Overview {
    const io = context.io;
    const arena = context.arena;

    var setups: std.ArrayList(Setup) = .empty;
    for (try setupFiles(context)) |config_file| {
        const settings = try hopper.settings.load(init, context.paths, config_file);
        try setups.append(arena, try setup(context, config_file, settings));
    }

    const release_lock = try std.fs.path.join(arena, &.{ context.paths.state, "release-build.lock" });
    return .{
        .time = context.now,
        .host = try system.hostname(arena),
        .code = try system.gitDescribe(io, arena, context.paths.hopper),
        .release_build = try system.lockHolder(io, arena, release_lock),
        .disk = try system.diskUsage(arena, context.paths.state),
        .setups = setups.items,
        .meta = .{
            .service = try system.unit(io, arena, "meta.service", &unit_properties),
            .timer = try system.unit(io, arena, "meta.timer", &timer_properties),
        },
    };
}

/// The setups' settings files: config/*.env but defaults.env, production first.
fn setupFiles(context: Context) ![]const []const u8 {
    const arena = context.arena;
    var config = try std.Io.Dir.cwd().openDir(context.io, context.paths.config, .{ .iterate = true });
    defer config.close(context.io);

    var names: std.ArrayList([]const u8) = .empty;
    var entries = config.iterate();
    while (try entries.next(context.io)) |entry| {
        if (entry.kind != .file) continue;
        if (!std.mem.endsWith(u8, entry.name, ".env")) continue;
        if (std.mem.eql(u8, entry.name, "defaults.env")) continue;
        try names.append(arena, try arena.dupe(u8, entry.name));
    }
    std.mem.sort([]const u8, names.items, {}, productionFirst);

    var files: std.ArrayList([]const u8) = .empty;
    for (names.items) |name| {
        try files.append(arena, try std.fs.path.join(arena, &.{ context.paths.config, name }));
    }
    return files.items;
}

fn productionFirst(_: void, a: []const u8, b: []const u8) bool {
    const a_production = std.mem.eql(u8, a, "production.env");
    const b_production = std.mem.eql(u8, b, "production.env");
    if (a_production != b_production) return a_production;
    return std.mem.lessThan(u8, a, b);
}

/// A setup's hopper and service, and what its files say. A file of it that isn't what hopper
/// writes there is its error, in place of what its files say: the other setups still show.
fn setup(context: Context, config_file: []const u8, settings: Settings) !Setup {
    const io = context.io;
    const arena = context.arena;
    const name = std.fs.path.stem(config_file);
    const state_path = settings.get("HOPPER_STATE");

    const service_name = try std.fmt.allocPrint(arena, "hopper@{s}.service", .{name});
    var result: Setup = .{
        .name = name,
        .config = config_file,
        .running = try system.lockHolder(io, arena, try concat(arena, state_path, ".lock")),
        .service = try system.unit(io, arena, service_name, &unit_properties),
        .log = try concat(arena, state_path, ".logs/hopper.log"),
    };
    var unreadable: []const u8 = undefined;
    fromFiles(context, settings, &result, &unreadable) catch |err| switch (err) {
        error.NotWhatHopperWrites => {
            result.@"error" = try std.fmt.allocPrint(arena, "{s}: not what hopper writes there", .{unreadable});
        },
        else => return err,
    };
    return result;
}

/// What a setup's files say, into `result`: all of it, or (when a file isn't what hopper writes
/// there, named in `unreadable`) none.
fn fromFiles(context: Context, settings: Settings, result: *Setup, unreadable: *[]const u8) !void {
    const io = context.io;
    const arena = context.arena;
    const state_path = settings.get("HOPPER_STATE");

    const state = (hopper.state.State.read(io, arena, state_path) catch |err| return noted(err, state_path, unreadable)).value;
    const status_path = try hopper.state.Status.path(arena, state_path);
    var status: ?hopper.state.Status = null;
    if (hopper.state.Status.read(io, arena, status_path) catch |err| return noted(err, status_path, unreadable)) |parsed| {
        status = parsed.value;
    }

    var job_summaries: std.ArrayList(JobSummary) = .empty;
    for (hopper.jobs.all) |job| {
        const job_entries = state.runs(job.name);
        var seen: ?hopper.state.Seen = null;
        if (status) |s| seen = s.jobs.map.get(job.name);
        try job_summaries.append(arena, try jobSummary(context, settings, job, job_entries, seen));
    }

    // Every job's runs, newest first
    var runs: std.ArrayList(RunSummary) = .empty;
    for (state.jobs.map.keys()) |job_name| {
        if (hopper.jobs.find(job_name) == null) continue;
        for (state.runs(job_name)) |entry| {
            try runs.append(arena, try runSummary(context, job_name, entry));
        }
    }
    std.mem.sort(RunSummary, runs.items, {}, newerFirst);

    const setup_live = try live(context, settings, unreadable);
    result.status = status;
    result.jobs = job_summaries.items;
    result.live = setup_live;
    result.runs = runs.items[0..@min(runs.items.len, max_runs)];
}

/// `err`, reading `path`: if it's a file that isn't what hopper writes there, `path` goes in
/// `unreadable`.
fn noted(err: anyerror, path: []const u8, unreadable: *[]const u8) anyerror {
    if (err == error.NotWhatHopperWrites) unreadable.* = path;
    return err;
}

fn newerFirst(_: void, a: RunSummary, b: RunSummary) bool {
    return a.entry.time.order(b.entry.time) == .gt;
}

fn jobSummary(context: Context, settings: Settings, job: Job, entries: []const Entry, seen: ?hopper.state.Seen) !JobSummary {
    const arena = context.arena;
    var summary: JobSummary = .{
        .name = job.name,
        .kind = @tagName(job.kind),
        .enabled = job.enabled(settings),
        .source = undefined,
        .seen = seen,
        .last = null,
        .last_published = null,
        .head_handled = null,
    };

    switch (job.kind) {
        .stream => |stream| {
            summary.source = try std.fmt.allocPrint(arena, "{s} {s}", .{
                settings.get(stream.repo_setting),
                settings.get(stream.branch_setting),
            });
            if (seen) |s| {
                if (s.head) |head| summary.head_handled = hasCommit(entries, head);
            }
        },
        .scheduled => |scheduled| summary.source = settings.get(scheduled.schedule_setting),
    }

    if (entries.len > 0) {
        summary.last = try runSummary(context, job.name, entries[entries.len - 1]);
    }
    var i = entries.len;
    while (i > 0) {
        i -= 1;
        if (entries[i].status == .published) {
            summary.last_published = try runSummary(context, job.name, entries[i]);
            break;
        }
    }
    return summary;
}

fn hasCommit(entries: []const Entry, commit: []const u8) bool {
    for (entries) |entry| {
        if (entry.commit) |c| {
            if (std.mem.eql(u8, c, commit)) return true;
        }
    }
    return false;
}

/// An entry's summary. Runs still going have taken until now.
fn runSummary(context: Context, job_name: []const u8, entry: Entry) !RunSummary {
    var end: ?Time = null;
    if (entry.status == .running) {
        end = context.now;
    } else {
        end = entry.finished orelse entry.lastStepEnd();
    }

    var seconds: ?i64 = null;
    if (end) |e| seconds = e.since(entry.time);

    var shown = entry;
    if (entry.release) |release| shown.release = std.fs.path.basename(release);
    return .{
        .entry = shown,
        .job = job_name,
        .label = try entry.label(context.arena),
        .seconds = seconds,
    };
}

/// What a setup has published: its files tree's last published snapshot, the newest version in
/// each update channel, the website.
fn live(context: Context, settings: Settings, unreadable: *[]const u8) !Live {
    const io = context.io;
    const arena = context.arena;
    const files_root = settings.get("FILES_ROOT");

    var published: ?Published = null;
    if (try system.readFile(io, arena, try concat(arena, files_root, ".published"))) |marker| {
        const snapshot = std.mem.trim(u8, marker, " \n");
        published = .{ .snapshot = snapshot };
        const info_path = try std.fmt.allocPrint(arena, "{s}.snapshots/{s}.json", .{ files_root, snapshot });
        const parsed = hopper.state.SnapshotInfo.read(io, arena, info_path) catch |err| return noted(err, info_path, unreadable);
        if (parsed) |p| {
            const info = p.value;
            published.?.created = info.created;
            published.?.kind = info.kind;
            published.?.about = info.about;
        }
    }

    return .{
        .published = published,
        .channels = try channels(context, files_root),
        .website = try website(context, settings.get("RELEASES_DIR")),
    };
}

/// {channel: {newest version id: [platforms]}}, from update/<platform>/<channel>/index.json.
/// Indexes that aren't what the updater reads are left out.
fn channels(context: Context, files_root: []const u8) !std.json.Value {
    const arena = context.arena;
    var result: std.json.ObjectMap = .empty;

    const update = try std.fs.path.join(arena, &.{ files_root, "update" });
    for (try sortedSubdirectories(context, update)) |platform| {
        const platform_path = try std.fs.path.join(arena, &.{ update, platform });
        for (try sortedSubdirectories(context, platform_path)) |channel| {
            const index_path = try std.fs.path.join(arena, &.{ platform_path, channel, "index.json" });
            const text = try system.readFile(context.io, arena, index_path) orelse continue;
            const index = std.json.parseFromSliceLeaky(hopper.state.Channel, arena, text, .{}) catch continue;
            if (index.Versions.len == 0) continue;

            var newest = index.Versions[0];
            for (index.Versions[1..]) |version| {
                if (version.Id.number() > newest.Id.number()) newest = version;
            }

            const by_id = try result.getOrPut(arena, channel);
            if (!by_id.found_existing) by_id.value_ptr.* = .{ .object = .empty };
            const platforms = try by_id.value_ptr.object.getOrPut(arena, newest.Id.text);
            if (!platforms.found_existing) platforms.value_ptr.* = .{ .array = .init(arena) };
            try platforms.value_ptr.array.append(.{ .string = platform });
        }
    }
    return .{ .object = result };
}

/// {target: release} from RELEASES_DIR/website-<target>.live
fn website(context: Context, releases_dir: []const u8) !std.json.Value {
    const arena = context.arena;
    var result: std.json.ObjectMap = .empty;
    for (try sortedFiles(context, releases_dir)) |name| {
        if (!std.mem.startsWith(u8, name, "website-") or !std.mem.endsWith(u8, name, ".live")) continue;
        const target = name["website-".len .. name.len - ".live".len];
        const path = try std.fs.path.join(arena, &.{ releases_dir, name });
        const release = try system.readFile(context.io, arena, path) orelse continue;
        try result.put(arena, target, .{ .string = std.mem.trim(u8, release, " \n") });
    }
    return .{ .object = result };
}

fn sortedSubdirectories(context: Context, path: []const u8) ![]const []const u8 {
    return sortedNames(context, path, .directory);
}

fn sortedFiles(context: Context, path: []const u8) ![]const []const u8 {
    return sortedNames(context, path, .file);
}

/// The names of the entries of a kind in a directory, sorted; none if it doesn't exist.
fn sortedNames(context: Context, path: []const u8, kind: std.Io.File.Kind) ![]const []const u8 {
    var dir = std.Io.Dir.cwd().openDir(context.io, path, .{ .iterate = true }) catch |err| switch (err) {
        error.FileNotFound => return &.{},
        else => return err,
    };
    defer dir.close(context.io);

    var names: std.ArrayList([]const u8) = .empty;
    var entries = dir.iterate();
    while (try entries.next(context.io)) |entry| {
        if (entry.kind != kind) continue;
        try names.append(context.arena, try context.arena.dupe(u8, entry.name));
    }
    std.mem.sort([]const u8, names.items, {}, lessThan);
    return names.items;
}

fn lessThan(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.lessThan(u8, a, b);
}

fn concat(arena: std.mem.Allocator, a: []const u8, b: []const u8) ![]const u8 {
    return std.mem.concat(arena, u8, &.{ a, b });
}

// --- showing it ---------------------------------------------------------------------------

fn printText(arena: std.mem.Allocator, out: *std.Io.Writer, overview: Overview) !void {
    try out.print("{s}  {f}  code {s}", .{ overview.host, overview.time, overview.code orelse "?" });
    if (overview.release_build != null) try out.writeAll("  (a release build is running)");
    try out.writeAll("\n");

    for (overview.setups) |s| {
        try out.print("\n{s}: hopper {s}, service {s}/{s}, {s} restarts\n", .{
            s.name,
            if (s.running != null) "running" else "not running",
            try unitText(arena, s.service, "ActiveState"),
            try unitText(arena, s.service, "UnitFileState"),
            try unitText(arena, s.service, "NRestarts"),
        });
        if (s.@"error") |problem| try out.print("  can't read its files: {s}\n", .{problem});
        for (s.jobs) |job| {
            var source = job.source;
            if (std.mem.eql(u8, job.kind, "stream")) {
                // Just the branch
                const space = std.mem.lastIndexOfScalar(u8, source, ' ') orelse 0;
                source = std.mem.trim(u8, source[space..], " ");
            }
            try out.print("  {s:<13} {s:<4} {s:<26} ", .{ job.name, if (job.enabled) "" else "off", source });
            if (job.last) |last| {
                const started = last.entry.time.local();
                try out.print("{d:0>2}-{d:0>2} {d:0>2}:{d:0>2} {s} {s} ", .{
                    started.month, started.day, started.hour, started.minute, last.label, @tagName(last.entry.status),
                });
                try printDuration(out, last.seconds);
                try out.writeAll("\n");
            } else {
                try out.writeAll("-\n");
            }
        }
    }

    try out.print("\nmeta: last run {s}, {s} (exit {s}), next {s}\n", .{
        try unitText(arena, overview.meta.service, "ExecMainStartTimestamp"),
        try unitText(arena, overview.meta.service, "Result"),
        try unitText(arena, overview.meta.service, "ExecMainStatus"),
        try unitText(arena, overview.meta.timer, "NextElapseUSecRealtime"),
    });
}

/// A unit property as text: "None" when it's empty, as the Python version shows it.
fn unitText(arena: std.mem.Allocator, unit: std.json.Value, name: []const u8) ![]const u8 {
    const value = unit.object.get(name) orelse return "None";
    switch (value) {
        .string => |text| return text,
        .integer => |number| return std.fmt.allocPrint(arena, "{d}", .{number}),
        else => return "None",
    }
}

pub fn printDuration(out: *std.Io.Writer, seconds: ?i64) !void {
    // Unsigned: a signed number padded with zeros gets a sign ("+14")
    const s: u64 = @intCast(@max(seconds orelse return, 0));
    if (s < 3600) {
        try out.print("{d}m{d:0>2}s", .{ @divFloor(s, 60), @mod(s, 60) });
    } else {
        try out.print("{d}h{d:0>2}m", .{ @divFloor(s, 3600), @divFloor(@mod(s, 3600), 60) });
    }
}

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const arena = init.arena.allocator();

    var json = false;
    const args = try init.minimal.args.toSlice(arena);
    for (args[1..]) |arg| {
        if (std.mem.eql(u8, arg, "--json")) {
            json = true;
        } else if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            std.debug.print("{s}", .{usage});
            return;
        } else {
            system.fail("unknown argument {s}\n{s}", .{ arg, usage });
        }
    }

    try hopper.time.useLocalZone(io, arena);
    const context: Context = .{
        .io = io,
        .arena = arena,
        .paths = try hopper.Paths.find(init),
        .now = .now(io),
    };
    const overview = try gather(init, context);

    var buffer: [4096]u8 = undefined;
    var stdout: std.Io.File.Writer = .init(.stdout(), io, &buffer);
    const out = &stdout.interface;
    if (json) {
        try std.json.Stringify.value(overview, .{ .whitespace = .indent_2 }, out);
        try out.writeAll("\n");
    } else {
        try printText(arena, out, overview);
    }
    try out.flush();
}
