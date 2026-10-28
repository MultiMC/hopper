// The releases in RELEASES_DIR: list them, clean up old ones. daemon/releases.py in Zig.
//
//     zig-out/bin/releases [--config FILE] list
//     zig-out/bin/releases [--config FILE] cleanup [--dry-run]
//
// list     every release, oldest first within each kind (launcher, translations, patrons,
//          template), with its size and whether it was trimmed
// cleanup  Launcher releases older than the newest RELEASES_KEEP_BUILD_TREES lose build/ and
//          src/ (the compiler trees and the checkout; a TRIMMED file says when): what's left,
//          packages/, deploy/, logs, compat reports and manifest.json, is enough to deploy it
//          again. Of each kind, only the newest RELEASES_KEEP are kept at all. Nothing younger
//          than RELEASES_MIN_AGE_HOURS is touched; leftovers of interrupted builds
//          (.preparing-*) are removed once they are older. Skipped while a release build is
//          running (lib/releases.zig).

const std = @import("std");
const hopper = @import("hopper");
const releases = hopper.releases;
const system = hopper.system;

const usage =
    \\usage: zig-out/bin/releases [--config FILE] list
    \\       zig-out/bin/releases [--config FILE] cleanup [--dry-run]
    \\
;

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const arena = init.arena.allocator();

    var config_file: ?[]const u8 = null;
    var command: ?[]const u8 = null;
    var dry_run = false;
    const args = try init.minimal.args.toSlice(arena);
    var i: usize = 1;
    while (i < args.len) : (i += 1) {
        const arg = args[i];
        if (std.mem.eql(u8, arg, "--config")) {
            i += 1;
            if (i == args.len) system.fail("--config needs a file\n{s}", .{usage});
            config_file = args[i];
        } else if (std.mem.eql(u8, arg, "--dry-run")) {
            dry_run = true;
        } else if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            std.debug.print("{s}", .{usage});
            return;
        } else if (command == null and (std.mem.eql(u8, arg, "list") or std.mem.eql(u8, arg, "cleanup"))) {
            command = arg;
        } else {
            system.fail("unknown argument {s}\n{s}", .{ arg, usage });
        }
    }
    const what = command orelse system.fail("list or cleanup?\n{s}", .{usage});
    if (dry_run and !std.mem.eql(u8, what, "cleanup")) system.fail("--dry-run is for cleanup\n{s}", .{usage});

    try hopper.time.useLocalZone(io, arena);
    const paths = try hopper.Paths.find(init);
    const settings = try hopper.settings.load(init, paths, config_file);

    if (std.mem.eql(u8, what, "list")) return list(io, arena, settings);

    const release_lock = try paths.releaseLock(arena);
    if (try system.isLocked(io, release_lock)) {
        system.log(io, "releases", "a release build is running; not cleaning up now", .{});
        return;
    }
    try std.Io.Dir.cwd().createDirPath(io, paths.state);
    const held = try system.lock(io, release_lock, "another release build is running");
    defer held.close(io);

    const done = try releases.cleanup(io, arena, settings, .now(io), dry_run);
    for (done) |change| {
        if (dry_run) {
            system.log(io, "releases", "would be {t} {s}", .{ change.action, change.name });
        } else {
            system.log(io, "releases", "{t} {s}", .{ change.action, change.name });
        }
    }
    if (dry_run) {
        system.log(io, "releases", "{d} releases would change", .{done.len});
    } else {
        system.log(io, "releases", "{d} releases changed", .{done.len});
    }
}

fn list(io: std.Io, arena: std.mem.Allocator, settings: hopper.Settings) !void {
    var buffer: [4096]u8 = undefined;
    var stdout: std.Io.File.Writer = .init(.stdout(), io, &buffer);
    const out = &stdout.interface;
    for (try releases.list(io, arena, settings)) |release| {
        const made = release.time.local();
        const when = try std.fmt.allocPrint(arena, "{d}-{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}", .{ made.year, made.month, made.day, made.hour, made.minute });
        try out.print("{s:<48} {s:<13} {s:<17} {s:>6}{s}\n", .{
            release.name,
            release.kind orelse "leftover",
            when,
            try size(io, arena, release.path),
            if (try release.isTrimmed(io, arena)) "   trimmed" else "",
        });
    }
    try out.flush();
}

/// What `du -sh` says a directory takes
fn size(io: std.Io, arena: std.mem.Allocator, path: []const u8) ![]const u8 {
    const result = try std.process.run(arena, io, .{ .argv = &.{ "du", "-sh", path } });
    var fields = std.mem.tokenizeAny(u8, result.stdout, " \t\n");
    return fields.next() orelse "?";
}
