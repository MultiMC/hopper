// hopper's jobs, as daemon/hopper.py has them: streams release every new commit of a branch,
// scheduled jobs release at the times of a cron-style schedule.

const std = @import("std");
const Settings = @import("settings.zig").Settings;

pub const Job = struct {
    name: []const u8,
    kind: Kind,

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

    pub fn enabled(job: Job, settings: Settings) bool {
        switch (job.kind) {
            .stream => |stream| return settings.isSet(stream.repo_setting),
            .scheduled => |scheduled| return settings.isSet(scheduled.enabled_setting) and
                settings.isSet(scheduled.schedule_setting),
        }
    }
};

pub const all = [_]Job{
    .{ .name = "launcher", .kind = .{ .stream = .{ .repo_setting = "LAUNCHER_REPO", .branch_setting = "LAUNCHER_BRANCH" } } },
    .{ .name = "translations", .kind = .{ .stream = .{ .repo_setting = "TRANSLATIONS_REPO", .branch_setting = "TRANSLATIONS_BRANCH" } } },
    .{ .name = "website", .kind = .{ .stream = .{ .repo_setting = "WEBSITE_REPO", .branch_setting = "WEBSITE_BRANCH" } } },
    .{ .name = "patrons", .kind = .{ .scheduled = .{ .schedule_setting = "PATRONS_SCHEDULE", .enabled_setting = "PATRONS_CONFIG" } } },
};

pub fn find(name: []const u8) ?Job {
    for (all) |job| {
        if (std.mem.eql(u8, job.name, name)) return job;
    }
    return null;
}
