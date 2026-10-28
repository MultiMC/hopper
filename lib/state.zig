// The files hopper keeps, as types. Reading one that doesn't match them is an error: a field
// hopper doesn't know or a status it doesn't have means the file isn't what this program thinks
// it is. Each is read and written by its type: State.read, State.write and so on (lib/json.zig).
// Times in them are RFC 3339 (lib/time.zig).
//
//   HOPPER_STATE           State, {job: [Entry, ...]}, each oldest first
//   HOPPER_STATE.status    Status, what the running `hopper watch` last saw
//   <snapshot>.json        SnapshotInfo, next to each snapshot of a files tree
//   update/<platform>/<channel>/index.json    Channel, the versions the updater offers

const std = @import("std");
const hopper = @import("hopper.zig");
const json = @import("json.zig");
const time = @import("time.zig");
const Time = time.Time;

const Allocator = std.mem.Allocator;

/// One run of a job. Written when it starts and after every step, so a crash leaves it "running".
pub const Entry = struct {
    /// When it started
    time: Time,
    status: RunStatus,
    /// What it released: a commit (streams) or a scheduled time (scheduled jobs)
    commit: ?[]const u8 = null,
    scheduled: ?Time = null,
    /// Started by hand (`hopper release`), not by watching
    by_hand: ?bool = null,
    /// Its log, and the release it made
    log: ?[]const u8 = null,
    release: ?[]const u8 = null,
    /// The launcher's build number
    build: ?i64 = null,
    /// The step it stopped in, if it didn't finish
    step: ?[]const u8 = null,
    /// Its steps so far (none for an adopted commit), and when it ended
    steps: json.List(Step) = .{},
    finished: ?Time = null,

    /// What it's about: the commit (shortened) or the scheduled time, "2026-10-28T12:00"
    pub fn label(entry: Entry, arena: Allocator) ![]const u8 {
        if (entry.commit) |commit| {
            return commit[0..@min(commit.len, 10)];
        }
        const scheduled = (entry.scheduled orelse return "").local();
        return std.fmt.allocPrint(arena, "{d}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}", .{
            scheduled.year, scheduled.month, scheduled.day, scheduled.hour, scheduled.minute,
        });
    }

    /// When its last step ended, or null
    pub fn lastStepEnd(entry: Entry) ?Time {
        const steps = entry.steps.list.items;
        if (steps.len == 0) return null;
        return steps[steps.len - 1].end;
    }
};

pub const RunStatus = enum { running, published, failed, interrupted, adopted };

pub const Step = struct {
    step: []const u8,
    start: Time,
    end: ?Time = null,
    exit: ?i64 = null,
};

/// HOPPER_STATE: each job's runs. In the file, the jobs are the object's keys.
pub const State = struct {
    jobs: std.json.ArrayHashMap(json.List(Entry)) = .{},

    /// The state at `path`, or a new one if there's none.
    pub fn read(io: std.Io, gpa: Allocator, path: []const u8) !json.Parsed(State) {
        return try json.read(State, io, gpa, path) orelse json.fresh(State, gpa, .{});
    }

    pub fn write(state: State, io: std.Io, path: []const u8) !void {
        try json.write(io, path, state);
    }

    /// A job's runs, oldest first.
    pub fn runs(state: State, job: []const u8) []Entry {
        const entries = state.jobs.map.get(job) orelse return &.{};
        return entries.list.items;
    }

    pub fn jsonParse(gpa: Allocator, source: anytype, options: std.json.ParseOptions) !State {
        return .{ .jobs = try std.json.innerParse(std.json.ArrayHashMap(json.List(Entry)), gpa, source, options) };
    }

    pub fn jsonStringify(state: State, writer: *std.json.Stringify) !void {
        try writer.write(state.jobs);
    }
};

pub const Status = struct {
    pid: i64,
    started: Time,
    config: ?[]const u8 = null,
    /// The hopper commit it runs
    code: []const u8,
    interval: i64,
    /// The end of its last look: a heartbeat, except while a release runs
    looked: ?Time = null,
    /// Why the last look failed
    problem: ?[]const u8 = null,
    jobs: std.json.ArrayHashMap(Seen) = .{},

    /// Next to the state: HOPPER_STATE.status
    pub fn path(arena: Allocator, state_path: []const u8) ![]const u8 {
        return std.mem.concat(arena, u8, &.{ state_path, ".status" });
    }

    pub fn read(io: std.Io, gpa: Allocator, status_path: []const u8) !?json.Parsed(Status) {
        return json.read(Status, io, gpa, status_path);
    }

    pub fn write(status: Status, io: std.Io, status_path: []const u8) !void {
        try json.write(io, status_path, status);
    }
};

/// What `hopper watch` last saw of a job: a stream's branch head, a scheduled job's times.
pub const Seen = struct {
    head: ?[]const u8 = null,
    latest: ?Time = null,
    next: ?Time = null,
    looked: Time,
};

pub const SnapshotInfo = struct {
    created: Time,
    kind: []const u8,
    about: []const u8,

    /// <snapshot>.json, next to the snapshot
    pub fn read(io: std.Io, gpa: Allocator, info_path: []const u8) !?json.Parsed(SnapshotInfo) {
        return json.read(SnapshotInfo, io, gpa, info_path);
    }
};

pub const Channel = struct {
    Versions: []const Version,
    Channels: ?std.json.Value = null,
    ApiVersion: ?std.json.Value = null,
};

pub const Version = struct {
    Id: VersionId,
    Name: []const u8,
};

/// A version's ID: newer indexes have strings, older ones numbers.
pub const VersionId = struct {
    text: []const u8,

    pub fn jsonParse(arena: Allocator, source: anytype, options: std.json.ParseOptions) !VersionId {
        const value = try std.json.innerParse(std.json.Value, arena, source, options);
        switch (value) {
            .string => |text| return .{ .text = text },
            .integer => |integer| return .{ .text = try std.fmt.allocPrint(arena, "{d}", .{integer}) },
            else => return error.UnexpectedToken,
        }
    }

    pub fn jsonStringify(id: VersionId, writer: *std.json.Stringify) !void {
        try writer.write(id.text);
    }

    /// For finding the newest: its number, or 0 if it isn't one.
    pub fn number(id: VersionId) u64 {
        return std.fmt.parseInt(u64, id.text, 10) catch 0;
    }
};

test "the state, read and written" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();
    // Central European winter time
    time.useZone(.{ .changes = &.{.{ .start = std.math.minInt(i64), .offset = 3600 }} });
    defer time.useZone(.utc);

    const text =
        \\{
        \\  "patrons": [
        \\    {
        \\      "time": "2026-10-28T12:14:10+01:00",
        \\      "status": "published",
        \\      "scheduled": "2026-10-28T12:00:00+01:00",
        \\      "steps": [
        \\        {
        \\          "step": "build",
        \\          "start": "2026-10-28T12:14:10+01:00",
        \\          "end": "2026-10-28T12:14:24+01:00",
        \\          "exit": 0
        \\        }
        \\      ],
        \\      "finished": "2026-10-28T12:14:24+01:00"
        \\    }
        \\  ]
        \\}
        \\
    ;
    var state = try std.json.parseFromSliceLeaky(State, arena, text, .{});
    const entry = state.runs("patrons")[0];
    try std.testing.expectEqual(RunStatus.published, entry.status);
    try std.testing.expectEqualStrings("2026-10-28T12:00", try entry.label(arena));
    try std.testing.expectEqual(try Time.parse("2026-10-28T11:14:24Z"), entry.lastStepEnd().?);
    try std.testing.expectEqual(0, state.runs("launcher").len);

    // Written back as it was read
    var out: std.Io.Writer.Allocating = .init(arena);
    try json.writeTo(&out.writer, state);
    try std.testing.expectEqualStrings(text, out.written());

    // A run added
    const patrons = state.jobs.map.getPtr("patrons").?;
    try patrons.list.append(arena, .{ .time = try Time.parse("2026-10-28T13:00:01+01:00"), .status = .running });
    try std.testing.expectEqual(.running, state.runs("patrons")[1].status);

    const unknown_status =
        \\{"patrons": [{"time": "2026-10-28T12:14:10+01:00", "status": "exploded"}]}
    ;
    try std.testing.expectError(error.InvalidEnumTag, std.json.parseFromSliceLeaky(State, arena, unknown_status, .{}));
}

test "version IDs, strings or numbers" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const channel = try std.json.parseFromSliceLeaky(Channel, arena,
        \\{"Versions": [{"Id": 8, "Name": "1.0.8"}, {"Id": "4284", "Name": "0.7.0-4284"}]}
    , .{});
    try std.testing.expectEqualStrings("8", channel.Versions[0].Id.text);
    try std.testing.expectEqual(@as(u64, 4284), channel.Versions[1].Id.number());
}
