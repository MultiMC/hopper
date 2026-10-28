// Releases in RELEASES_DIR, as lib/common.py has them: Launcher builds (<build number>/) and
// translations-*, patrons-*, template-* directories. A Launcher release's build/ (the compiler
// trees, ~1 GB) and src/ are only needed to look into how it was built: files/deploy.py uses
// its packages/ and deploy/.

const std = @import("std");
const Settings = @import("settings.zig").Settings;
const system = @import("system.zig");
const Time = @import("time.zig").Time;

const Allocator = std.mem.Allocator;

/// In a trimmed Launcher release: when build/ and src/ were removed
pub const trimmed_marker = "TRIMMED";

pub const Release = struct {
    name: []const u8,
    path: []const u8,
    /// launcher, translations, patrons, template, ... or null for a leftover .preparing-<pid>
    kind: ?[]const u8,
    /// When it was made: its manifest.json's last write, or the directory's
    time: Time,

    pub fn isTrimmed(release: Release, io: std.Io, arena: Allocator) !bool {
        return system.exists(io, try std.fs.path.join(arena, &.{ release.path, trimmed_marker }));
    }
};

/// The release directories, oldest first within each kind (build number, or the time in the
/// name).
pub fn list(io: std.Io, arena: Allocator, settings: Settings) ![]Release {
    const directory = settings.get("RELEASES_DIR");
    var dir = std.Io.Dir.cwd().openDir(io, directory, .{ .iterate = true }) catch |err| switch (err) {
        error.FileNotFound, error.NotDir => return &.{},
        else => return err,
    };
    defer dir.close(io);

    var found: std.ArrayList(Release) = .empty;
    var entries = dir.iterate();
    while (try entries.next(io)) |entry| {
        if (entry.kind != .directory) continue;
        const name = try arena.dupe(u8, entry.name);
        const path = try std.fs.path.join(arena, &.{ directory, name });
        try found.append(arena, .{ .name = name, .path = path, .kind = kindOf(name), .time = try madeAt(io, arena, path) });
    }
    std.mem.sort(Release, found.items, {}, olderFirst);
    return found.items;
}

fn kindOf(name: []const u8) ?[]const u8 {
    if (buildNumber(name) != null) return "launcher";
    if (std.mem.startsWith(u8, name, ".preparing-")) return null;
    return name[0 .. std.mem.indexOfScalar(u8, name, '-') orelse name.len];
}

/// A Launcher release's build number: its name, all digits
fn buildNumber(name: []const u8) ?u64 {
    for (name) |c| if (!std.ascii.isDigit(c)) return null;
    return std.fmt.parseInt(u64, name, 10) catch null;
}

fn madeAt(io: std.Io, arena: Allocator, path: []const u8) !Time {
    const cwd = std.Io.Dir.cwd();
    const manifest = try std.fs.path.join(arena, &.{ path, "manifest.json" });
    const stat = cwd.statFile(io, manifest, .{}) catch try cwd.statFile(io, path, .{});
    return .{ .seconds = stat.mtime.toSeconds() };
}

/// By build number (other kinds count as 0), then by name, as common.releases sorts them
fn olderFirst(_: void, a: Release, b: Release) bool {
    const a_number = buildNumber(a.name) orelse 0;
    const b_number = buildNumber(b.name) orelse 0;
    if (a_number != b_number) return a_number < b_number;
    return std.mem.lessThan(u8, a.name, b.name);
}

pub const Change = struct {
    action: enum { removed, trimmed },
    name: []const u8,
};

/// Trim and remove old releases: Launcher releases older than the newest
/// RELEASES_KEEP_BUILD_TREES lose build/ and src/ (TRIMMED records when); of each kind, only
/// the newest RELEASES_KEEP are kept at all. Nothing younger than RELEASES_MIN_AGE_HOURS is
/// touched, leftovers of interrupted builds (.preparing-*) included, nor a release named in a
/// RELEASES_DIR/*.live marker (what's live, e.g. the website). The caller holds the release
/// lock. Returns what it did (or, with `dry_run`, would do), as common.cleanup_releases.
pub fn cleanup(io: std.Io, arena: Allocator, settings: Settings, now: Time, dry_run: bool) ![]const Change {
    const keep = try std.fmt.parseInt(usize, settings.get("RELEASES_KEEP"), 10);
    const keep_trees = try std.fmt.parseInt(usize, settings.get("RELEASES_KEEP_BUILD_TREES"), 10);
    const min_age_hours = try std.fmt.parseFloat(f64, settings.get("RELEASES_MIN_AGE_HOURS"));
    const young = now.seconds - @as(i64, @intFromFloat(min_age_hours * 3600));

    // Each kind's releases, the kinds in the order they're first found ("" for leftovers)
    var by_kind: std.array_hash_map.String(std.ArrayList(Release)) = .empty;
    for (try list(io, arena, settings)) |release| {
        const entry = try by_kind.getOrPut(arena, release.kind orelse "");
        if (!entry.found_existing) entry.value_ptr.* = .empty;
        try entry.value_ptr.append(arena, release);
    }
    const live = try liveReleases(io, arena, settings.get("RELEASES_DIR"));

    var done: std.ArrayList(Change) = .empty;
    for (by_kind.keys(), by_kind.values()) |kind, found| {
        const releases = found.items;
        var old: std.StringHashMapUnmanaged(void) = .empty;
        for (releases) |release| {
            if (release.time.seconds <= young and !live.contains(release.name)) try old.put(arena, release.name, {});
        }

        // Leftovers go once they're old; of a kind, the old ones but the newest `keep`
        var removed: std.StringHashMapUnmanaged(void) = .empty;
        const removable = if (kind.len == 0 or keep == 0) releases else releases[0..releases.len -| keep];
        for (removable) |release| {
            if (!old.contains(release.name)) continue;
            try removed.put(arena, release.name, {});
            try done.append(arena, .{ .action = .removed, .name = release.name });
            if (!dry_run) try std.Io.Dir.cwd().deleteTree(io, release.path);
        }

        if (!std.mem.eql(u8, kind, "launcher")) continue;
        const trimmable = if (keep_trees == 0) releases else releases[0..releases.len -| keep_trees];
        for (trimmable) |release| {
            if (removed.contains(release.name) or !old.contains(release.name)) continue;
            if (try release.isTrimmed(io, arena)) continue;
            const build = try std.fs.path.join(arena, &.{ release.path, "build" });
            const src = try std.fs.path.join(arena, &.{ release.path, "src" });
            if (!system.exists(io, build) and !system.exists(io, src)) continue;
            try done.append(arena, .{ .action = .trimmed, .name = release.name });
            if (!dry_run) try trim(io, arena, release, build, src, now);
        }
    }
    return done.items;
}

fn trim(io: std.Io, arena: Allocator, release: Release, build: []const u8, src: []const u8, now: Time) !void {
    const cwd = std.Io.Dir.cwd();
    try cwd.deleteTree(io, build);
    try cwd.deleteTree(io, src);
    const l = now.local();
    const note = try std.fmt.allocPrint(arena, "build/ and src/ removed {d}-{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}:{d:0>2} by the release cleanup\n", .{
        l.year, l.month, l.day, l.hour, l.minute, l.second,
    });
    try cwd.writeFile(io, .{ .sub_path = try std.fs.path.join(arena, &.{ release.path, trimmed_marker }), .data = note });
}

/// The releases named in RELEASES_DIR/*.live markers
fn liveReleases(io: std.Io, arena: Allocator, directory: []const u8) !std.StringHashMapUnmanaged(void) {
    var live: std.StringHashMapUnmanaged(void) = .empty;
    var dir = std.Io.Dir.cwd().openDir(io, directory, .{ .iterate = true }) catch |err| switch (err) {
        error.FileNotFound, error.NotDir => return live,
        else => return err,
    };
    defer dir.close(io);
    var entries = dir.iterate();
    while (try entries.next(io)) |entry| {
        if (!std.mem.endsWith(u8, entry.name, ".live")) continue;
        const marker = try std.fs.path.join(arena, &.{ directory, entry.name });
        const text = try system.readFile(io, arena, marker) orelse continue;
        try live.put(arena, std.mem.trim(u8, text, " \t\r\n"), {});
    }
    return live;
}
