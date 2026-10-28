// hopper's jobs, as daemon/hopper.py has them: streams release every new commit of a branch,
// scheduled jobs release at the times of a cron-style schedule.

const std = @import("std");
const Settings = @import("settings.zig").Settings;
const cron = @import("cron.zig");
const Time = @import("time.zig").Time;

pub const Job = struct {
    name: []const u8,
    kind: Kind,
    /// Makes a release (the last line of its output is the release's directory)
    build_script: []const u8,
    /// Takes the release instead of files/deploy.py and files/publish.py
    publish_script: ?[]const u8 = null,

    pub const Kind = union(enum) {
        stream: Stream,
        scheduled: Scheduled,
    };

    /// Watches a branch of a repository; off where the repository setting is empty.
    pub const Stream = struct {
        repo_setting: []const u8,
        branch_setting: []const u8,
    };

    /// Runs at the times of a schedule (this machine's time); off where either setting is empty.
    pub const Scheduled = struct {
        schedule_setting: []const u8,
        enabled_setting: []const u8,
    };

    /// A scheduled job's schedule, or why it isn't one
    pub fn schedule(job: Job, settings: Settings) error{BadSchedule}!cron.Schedule {
        return cron.Schedule.parse(settings.get(job.kind.scheduled.schedule_setting));
    }

    pub fn enabled(job: Job, settings: Settings) bool {
        switch (job.kind) {
            .stream => |stream| return settings.isSet(stream.repo_setting),
            .scheduled => |scheduled| return settings.isSet(scheduled.enabled_setting) and
                settings.isSet(scheduled.schedule_setting),
        }
    }
};

pub const all = [_]Job{
    .{
        .name = "launcher",
        .kind = .{ .stream = .{ .repo_setting = "LAUNCHER_REPO", .branch_setting = "LAUNCHER_BRANCH" } },
        .build_script = "launcher/release-build.py",
    },
    .{
        .name = "translations",
        .kind = .{ .stream = .{ .repo_setting = "TRANSLATIONS_REPO", .branch_setting = "TRANSLATIONS_BRANCH" } },
        .build_script = "translations/build.py",
    },
    .{
        .name = "website",
        .kind = .{ .stream = .{ .repo_setting = "WEBSITE_REPO", .branch_setting = "WEBSITE_BRANCH" } },
        .build_script = "website/build.py",
        .publish_script = "website/publish.py",
    },
    .{
        .name = "patrons",
        .kind = .{ .scheduled = .{ .schedule_setting = "PATRONS_SCHEDULE", .enabled_setting = "PATRONS_CONFIG" } },
        .build_script = "patrons/update.py",
    },
};

pub fn find(name: []const u8) ?Job {
    for (all) |job| {
        if (std.mem.eql(u8, job.name, name)) return job;
    }
    return null;
}
