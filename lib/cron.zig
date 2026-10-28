// Cron-style schedules, as daemon/cron.py has them: "minute hour day-of-month month day-of-week".
//
// Each field is *, a number, a range a-b, a list a,b,c, or any of those with a step (*/15,
// 1-30/2). Day of week: 0-7, 0 and 7 are Sunday. As in cron, when both day of month and day of
// week are restricted, a day matching either one is a match. Times are this machine's clock:
// a scheduled minute becomes a moment as Python makes one (Time.fromWall), so both write the
// same scheduled time, which is how hopper knows it handled it.
//
//     const schedule = try Schedule.parse("0 12 * * *");
//     schedule.latest(now)    the most recent scheduled minute at or before now
//     schedule.next(now)      the first one after now

const std = @import("std");
const time = @import("time.zig");
const Time = time.Time;

/// How far latest and next look: a schedule with no time in it (31 April) has none.
const days_looked = 400;

pub const Schedule = struct {
    minutes: std.StaticBitSet(60),
    hours: std.StaticBitSet(24),
    days: std.StaticBitSet(32),
    months: std.StaticBitSet(13),
    /// 0 is Sunday (7 in the schedule is Sunday too)
    weekdays: std.StaticBitSet(7),
    any_day: bool,
    any_weekday: bool,

    pub fn parse(expression: []const u8) error{BadSchedule}!Schedule {
        var fields: [5][]const u8 = undefined;
        var count: usize = 0;
        var parts = std.mem.tokenizeAny(u8, expression, " \t");
        while (parts.next()) |part| : (count += 1) {
            if (count == fields.len) return error.BadSchedule;
            fields[count] = part;
        }
        if (count != fields.len) return error.BadSchedule;

        var schedule: Schedule = .{
            .minutes = try parseField(60, fields[0], 0, 59),
            .hours = try parseField(24, fields[1], 0, 23),
            .days = try parseField(32, fields[2], 1, 31),
            .months = try parseField(13, fields[3], 1, 12),
            .weekdays = .empty,
            .any_day = std.mem.eql(u8, fields[2], "*"),
            .any_weekday = std.mem.eql(u8, fields[4], "*"),
        };
        const weekdays = try parseField(8, fields[4], 0, 7);
        var each = weekdays.iterator(.{});
        while (each.next()) |day| schedule.weekdays.set(day % 7);
        return schedule;
    }

    /// The most recent scheduled minute at or before `now`, or null if there was none in the
    /// last 400 days.
    pub fn latest(schedule: Schedule, now: Time) ?Time {
        const now_minute = @divFloor(now.wall(), 60) * 60;
        const today = @divFloor(now_minute, std.time.s_per_day);
        for (0..days_looked + 1) |back| {
            const day = today - @as(i64, @intCast(back));
            if (!schedule.dayMatches(day)) continue;
            var hour: usize = 24;
            while (hour > 0) {
                hour -= 1;
                if (!schedule.hours.isSet(hour)) continue;
                var minute: usize = 60;
                while (minute > 0) {
                    minute -= 1;
                    if (!schedule.minutes.isSet(minute)) continue;
                    const wall = wallSeconds(day, hour, minute);
                    if (wall <= now_minute) return .fromWall(wall);
                }
            }
        }
        return null;
    }

    /// The first scheduled minute after `now`, or null if there is none in the next 400 days.
    pub fn next(schedule: Schedule, now: Time) ?Time {
        const now_minute = @divFloor(now.wall(), 60) * 60;
        const today = @divFloor(now_minute, std.time.s_per_day);
        for (0..days_looked + 1) |ahead| {
            const day = today + @as(i64, @intCast(ahead));
            if (!schedule.dayMatches(day)) continue;
            for (0..24) |hour| {
                if (!schedule.hours.isSet(hour)) continue;
                for (0..60) |minute| {
                    if (!schedule.minutes.isSet(minute)) continue;
                    const wall = wallSeconds(day, hour, minute);
                    if (wall > now_minute) return .fromWall(wall);
                }
            }
        }
        return null;
    }

    /// Whether a day (since 1970-01-01, on the clock here) has scheduled times.
    fn dayMatches(schedule: Schedule, day: i64) bool {
        const epoch_day: std.time.epoch.EpochDay = .{ .day = @intCast(day) };
        const year_day = epoch_day.calculateYearDay();
        const month_day = year_day.calculateMonthDay();
        if (!schedule.months.isSet(month_day.month.numeric())) return false;
        const by_date = schedule.days.isSet(month_day.day_index + 1);
        // 1970-01-01 was a Thursday
        const by_weekday = schedule.weekdays.isSet(@intCast(@mod(day + 4, 7)));
        if (schedule.any_day or schedule.any_weekday) return by_date and by_weekday;
        return by_date or by_weekday;
    }
};

fn wallSeconds(day: i64, hour: usize, minute: usize) i64 {
    return day * std.time.s_per_day + @as(i64, @intCast(hour * 3600 + minute * 60));
}

/// A field's values: *, n, a-b, lists of them, each with a /step.
fn parseField(comptime size: usize, text: []const u8, low: usize, high: usize) error{BadSchedule}!std.StaticBitSet(size) {
    var values: std.StaticBitSet(size) = .empty;
    var items = std.mem.splitScalar(u8, text, ',');
    while (items.next()) |item| {
        var base = item;
        var step: usize = 1;
        if (std.mem.indexOfScalar(u8, item, '/')) |slash| {
            base = item[0..slash];
            step = try number(item[slash + 1 ..]);
            if (step == 0) return error.BadSchedule;
        }
        var start = low;
        var end = high;
        if (!std.mem.eql(u8, base, "*")) {
            if (std.mem.indexOfScalar(u8, base, '-')) |dash| {
                start = try number(base[0..dash]);
                end = try number(base[dash + 1 ..]);
            } else {
                start = try number(base);
                end = start;
            }
        }
        if (start < low or start > end or end > high) return error.BadSchedule;
        var value = start;
        while (value <= end) : (value += step) values.set(value);
    }
    return values;
}

fn number(text: []const u8) error{BadSchedule}!usize {
    return std.fmt.parseInt(usize, text, 10) catch error.BadSchedule;
}

fn expectTime(expected: []const u8, actual: ?Time) !void {
    var buffer: [64]u8 = undefined;
    try std.testing.expectEqualStrings(expected, try std.fmt.bufPrint(&buffer, "{f}", .{actual.?}));
}

test "latest and next" {
    time.useZone(.utc);
    const noon = try Schedule.parse("0 12 * * *");
    const now = try Time.parse("2026-10-28T12:14:09Z");
    try expectTime("2026-10-28T12:00:00+00:00", noon.latest(now));
    try expectTime("2026-10-29T12:00:00+00:00", noon.next(now));
    // The minute itself is the latest, not the next
    const at_noon = try Time.parse("2026-10-28T12:00:30Z");
    try expectTime("2026-10-28T12:00:00+00:00", noon.latest(at_noon));
    try expectTime("2026-10-29T12:00:00+00:00", noon.next(at_noon));

    const quarters = try Schedule.parse("*/15 9-17 * * 1-5");
    // 2026-10-31 is a Saturday: back to Friday's last quarter, on to Monday's first
    const saturday = try Time.parse("2026-10-31T10:00:00Z");
    try expectTime("2026-10-30T17:45:00+00:00", quarters.latest(saturday));
    try expectTime("2026-11-02T09:00:00+00:00", quarters.next(saturday));

    // Day of month or day of week, when both are given: the 1st, or a Sunday (7)
    const either = try Schedule.parse("0 0 1 * 7");
    try expectTime("2026-11-01T00:00:00+00:00", either.next(saturday));
    try expectTime("2026-11-08T00:00:00+00:00", either.next(try Time.parse("2026-11-01T00:00:00Z")));

    const never = try Schedule.parse("0 0 31 4 *");
    try std.testing.expectEqual(@as(?Time, null), never.next(now));
}

test "what isn't a schedule" {
    const not_schedules = [_][]const u8{ "", "0 12 * *", "0 12 * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * * 13 *", "* * * * 8", "5-1 * * * *", "*/0 * * * *", "a * * * *", "1- * * * *" };
    for (not_schedules) |text| {
        try std.testing.expectError(error.BadSchedule, Schedule.parse(text));
    }
}
