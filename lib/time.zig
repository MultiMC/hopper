// Points in time, and how hopper writes them: RFC 3339, in this machine's local time with its
// offset ("2026-10-28T12:14:09+01:00"), so a file says which moment it means and still reads as
// the clock on the wall. The zone is /etc/localtime (std.tz reads it); without one, UTC. The TZ
// environment variable isn't looked at.

const std = @import("std");
const system = @import("system.zig");

/// A point in time: seconds since 1970-01-01 00:00 UTC.
pub const Time = struct {
    seconds: i64,

    pub fn now(io: std.Io) Time {
        return .{ .seconds = std.Io.Timestamp.now(io, .real).toSeconds() };
    }

    /// Seconds from `earlier` to `time`
    pub fn since(time: Time, earlier: Time) i64 {
        return time.seconds - earlier.seconds;
    }

    pub fn order(a: Time, b: Time) std.math.Order {
        return std.math.order(a.seconds, b.seconds);
    }

    /// What the clock here shows at `time`
    pub fn local(time: Time) Local {
        const offset = zone.offsetAt(time.seconds);
        const epoch_seconds: std.time.epoch.EpochSeconds = .{ .secs = @intCast(time.seconds + offset) };
        const year_day = epoch_seconds.getEpochDay().calculateYearDay();
        const month_day = year_day.calculateMonthDay();
        const day_seconds = epoch_seconds.getDaySeconds();
        return .{
            .year = year_day.year,
            .month = month_day.month.numeric(),
            .day = month_day.day_index + 1,
            .hour = day_seconds.getHoursIntoDay(),
            .minute = day_seconds.getMinutesIntoHour(),
            .second = day_seconds.getSecondsIntoMinute(),
            .offset = offset,
        };
    }

    /// An RFC 3339 time, "2026-10-28T12:14:09+01:00" or "2026-10-28T11:14:09Z".
    pub fn parse(text: []const u8) error{BadTime}!Time {
        if (text.len < 20) return error.BadTime;
        if (text[4] != '-' or text[7] != '-' or text[10] != 'T' or text[13] != ':' or text[16] != ':') {
            return error.BadTime;
        }
        const year = try digits(text[0..4]);
        const month = try digits(text[5..7]);
        const day = try digits(text[8..10]);
        const hour = try digits(text[11..13]);
        const minute = try digits(text[14..16]);
        const second = try digits(text[17..19]);
        if (month < 1 or month > 12 or day < 1 or day > 31) return error.BadTime;
        if (hour > 23 or minute > 59 or second > 59) return error.BadTime;

        const as_if_utc = daysFromCivil(year, month, day) * std.time.s_per_day + hour * 3600 + minute * 60 + second;
        return .{ .seconds = as_if_utc - try parseOffset(text[19..]) };
    }

    /// RFC 3339 in local time, as hopper writes times: "2026-10-28T12:14:09+01:00" (with {f}).
    /// UTC is "+00:00", as Python's isoformat writes it, so both write a time the same way.
    pub fn format(time: Time, writer: *std.Io.Writer) std.Io.Writer.Error!void {
        const l = time.local();
        try writer.print("{d}-{d:0>2}-{d:0>2}T{d:0>2}:{d:0>2}:{d:0>2}", .{ l.year, l.month, l.day, l.hour, l.minute, l.second });

        var sign: u8 = '+';
        if (l.offset < 0) sign = '-';
        const offset: u32 = @abs(l.offset);
        try writer.print("{c}{d:0>2}:{d:0>2}", .{ sign, offset / 3600, offset % 3600 / 60 });
    }

    pub fn jsonParse(gpa: std.mem.Allocator, source: anytype, options: std.json.ParseOptions) !Time {
        const text = try std.json.innerParse([]const u8, gpa, source, options);
        return parse(text) catch error.InvalidCharacter;
    }

    pub fn jsonStringify(time: Time, writer: *std.json.Stringify) !void {
        try writer.print("\"{f}\"", .{time});
    }
};

/// A time as the clock here shows it, and how far that is ahead of UTC.
pub const Local = struct {
    year: u16,
    month: u8,
    day: u8,
    hour: u8,
    minute: u8,
    second: u8,
    /// Seconds
    offset: i32,
};

fn digits(text: []const u8) error{BadTime}!i64 {
    var number: i64 = 0;
    for (text) |c| {
        if (!std.ascii.isDigit(c)) return error.BadTime;
        number = number * 10 + (c - '0');
    }
    return number;
}

/// "Z" or "+01:00": seconds ahead of UTC
fn parseOffset(text: []const u8) error{BadTime}!i64 {
    if (std.mem.eql(u8, text, "Z")) return 0;
    if (text.len != 6 or text[3] != ':') return error.BadTime;
    var sign: i64 = 1;
    switch (text[0]) {
        '+' => {},
        '-' => sign = -1,
        else => return error.BadTime,
    }
    const hours = try digits(text[1..3]);
    const minutes = try digits(text[4..6]);
    if (hours > 23 or minutes > 59) return error.BadTime;
    return sign * (hours * 3600 + minutes * 60);
}

/// The zone times are shown and written in. One for the whole program, as in Python's time
/// module: std.json's write hooks can't be handed one. UTC until `useLocalZone`.
var zone: Zone = .utc;

/// Show and write times in this machine's zone, /etc/localtime. At the start of a program.
pub fn useLocalZone(io: std.Io, arena: std.mem.Allocator) !void {
    zone = try Zone.load(io, arena);
}

/// Show and write times in `new` (tests).
pub fn useZone(new: Zone) void {
    zone = new;
}

pub const Zone = struct {
    /// Changes of the offset from UTC, oldest first: from `start` on, local time is UTC + `offset`
    changes: []const Change,

    pub const Change = struct { start: i64, offset: i32 };

    pub const utc: Zone = .{ .changes = &.{} };

    pub fn load(io: std.Io, arena: std.mem.Allocator) !Zone {
        const data = try system.readFile(io, arena, "/etc/localtime") orelse return utc;
        var reader: std.Io.Reader = .fixed(data);
        const tz = try std.tz.Tz.parse(arena, &reader);

        var changes: std.ArrayList(Change) = .empty;
        // Before the first transition: the first standard-time type
        for (tz.timetypes) |timetype| {
            if (!timetype.isDst()) {
                try changes.append(arena, .{ .start = std.math.minInt(i64), .offset = timetype.offset });
                break;
            }
        }
        for (tz.transitions) |transition| {
            try changes.append(arena, .{ .start = transition.ts, .offset = transition.timetype.offset });
        }
        return .{ .changes = changes.items };
    }

    /// Local time minus UTC, in seconds, at `seconds` since the epoch. (After the zone file's
    /// last transition, in 2037 for full zone files, the last offset stays.)
    pub fn offsetAt(z: Zone, seconds: i64) i32 {
        var offset: i32 = 0;
        for (z.changes) |change| {
            if (change.start > seconds) break;
            offset = change.offset;
        }
        return offset;
    }
};

/// Days since 1970-01-01 of a date in the proleptic Gregorian calendar (Howard Hinnant's
/// days_from_civil: years start in March, so the leap day is a year's last).
fn daysFromCivil(year: i64, month: i64, day: i64) i64 {
    var y = year;
    if (month <= 2) y -= 1;
    const era = @divFloor(y, 400);
    const year_of_era = y - era * 400;
    var month_from_march = month - 3;
    if (month <= 2) month_from_march = month + 9;
    const day_of_year = @divFloor(153 * month_from_march + 2, 5) + day - 1;
    const day_of_era = year_of_era * 365 + @divFloor(year_of_era, 4) - @divFloor(year_of_era, 100) + day_of_year;
    return era * 146097 + day_of_era - 719468;
}

fn expectFormatted(expected: []const u8, time: Time) !void {
    var buffer: [64]u8 = undefined;
    try std.testing.expectEqualStrings(expected, try std.fmt.bufPrint(&buffer, "{f}", .{time}));
}

test "UTC" {
    useZone(.utc);
    const time = try Time.parse("2026-10-28T12:14:09Z");
    try std.testing.expectEqual(@as(i64, 1793189649), time.seconds);
    try expectFormatted("2026-10-28T12:14:09+00:00", time);
    try std.testing.expectEqual(time, try Time.parse("2026-10-28T12:14:09+00:00"));
    try std.testing.expectEqual(@as(i64, 0), (try Time.parse("1970-01-01T00:00:00Z")).seconds);
}

test "offsets, and what isn't a time" {
    useZone(.utc);
    const utc = try Time.parse("2026-10-25T13:53:49Z");
    try std.testing.expectEqual(utc, try Time.parse("2026-10-25T14:53:49+01:00"));
    try std.testing.expectEqual(utc, try Time.parse("2026-10-25T08:53:49-05:00"));

    const not_times = [_][]const u8{
        "",
        "2026-10-25",
        "2026-10-25T13:53:49",
        "2026-10-25 13:53:49",
        "2026-10-25 13:53",
        "2026-10-25T13:53:49+0100",
        "2026-13-25T13:53:49Z",
        "2026-10-25T24:00:00Z",
        "2026-10-25T13:53:49+1:00",
        "2026-1O-25T13:53:49Z",
        "2026-10-25T13:53:49+01:00 ",
    };
    for (not_times) |text| {
        try std.testing.expectError(error.BadTime, Time.parse(text));
    }
}

test "across a change of offset" {
    defer useZone(.utc);
    // Central Europe in 2026: summer time (+2h) until 2026-10-25 01:00 UTC, then +1h
    const change = try Time.parse("2026-10-25T01:00:00Z");
    useZone(.{ .changes = &.{
        .{ .start = std.math.minInt(i64), .offset = 7200 },
        .{ .start = change.seconds, .offset = 3600 },
    } });
    try expectFormatted("2026-10-25T02:59:59+02:00", .{ .seconds = change.seconds - 1 });
    try expectFormatted("2026-10-25T02:00:00+01:00", change);
    // Six hours on the clock across the change are seven real ones
    const start = try Time.parse("2026-10-24T22:00:00+02:00");
    const end = try Time.parse("2026-10-25T04:00:00+01:00");
    try std.testing.expectEqual(@as(i64, 7 * 3600), end.since(start));
    try expectFormatted("2026-10-24T22:00:00+02:00", start);
}

test "in JSON" {
    useZone(.utc);
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const Run = struct { time: Time };
    const run = try std.json.parseFromSliceLeaky(Run, arena, "{\"time\": \"2026-10-28T12:14:09Z\"}", .{});
    const written = try std.json.Stringify.valueAlloc(arena, run, .{});
    try std.testing.expectEqualStrings("{\"time\":\"2026-10-28T12:14:09+00:00\"}", written);
    try std.testing.expectError(error.InvalidCharacter, std.json.parseFromSliceLeaky(Run, arena, "{\"time\": \"2026-10-28 12:14:09\"}", .{}));
}
