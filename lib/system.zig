// What hopper asks of this machine (Linux only): locks and who holds them, free space,
// systemd's units, the checkout's commit, and its log lines.

const std = @import("std");
const builtin = @import("builtin");
const linux = std.os.linux;
const hopper = @import("hopper.zig");
const Time = @import("time.zig").Time;

comptime {
    if (builtin.os.tag != .linux) @compileError("hopper runs on Linux");
}

/// The pid holding the lock on `path`, or null; without touching the lock (for looking often,
/// as the overview does: taking it for a moment could make a build starting just then fail).
/// From /proc/locks, which names the file by inode (its device there isn't the one stat gives
/// on btrfs), confirmed by the holder's open files where they can be read.
pub fn lockHolder(io: std.Io, arena: std.mem.Allocator, path: []const u8) !?i64 {
    const cwd = std.Io.Dir.cwd();
    const stat = cwd.statFile(io, path, .{}) catch return null;
    const inode = try std.fmt.allocPrint(arena, "{d}", .{stat.inode});
    const locks = try readFile(io, arena, "/proc/locks") orelse return null;
    var real_buffer: [std.fs.max_path_bytes]u8 = undefined;
    const real = real_buffer[0..try cwd.realPathFile(io, path, &real_buffer)];

    // "1: FLOCK  ADVISORY  WRITE 1690413 00:2b:4021 0 EOF"
    var lines = std.mem.splitScalar(u8, locks, '\n');
    while (lines.next()) |line| {
        var fields: [6][]const u8 = undefined;
        var count: usize = 0;
        var words = std.mem.tokenizeScalar(u8, line, ' ');
        while (count < fields.len) : (count += 1) {
            fields[count] = words.next() orelse break;
        }
        if (count < 6 or !std.mem.eql(u8, fields[1], "FLOCK")) continue;
        const file = fields[5];
        const file_inode = file[(std.mem.lastIndexOfScalar(u8, file, ':') orelse continue) + 1 ..];
        if (!std.mem.eql(u8, file_inode, inode)) continue;

        const pid = std.fmt.parseInt(i64, fields[4], 10) catch continue;
        switch (try holdsOpen(io, arena, pid, real)) {
            .yes, .cannot_tell => return pid,
            .no => continue,
        }
    }
    return null;
}

/// Whether process `pid` has `real_path` open. Someone else's process can't be looked at.
fn holdsOpen(io: std.Io, arena: std.mem.Allocator, pid: i64, real_path: []const u8) !enum { yes, no, cannot_tell } {
    const fd_path = try std.fmt.allocPrint(arena, "/proc/{d}/fd", .{pid});
    var fds = std.Io.Dir.cwd().openDir(io, fd_path, .{ .iterate = true }) catch |err| switch (err) {
        error.AccessDenied, error.PermissionDenied => return .cannot_tell,
        else => return .no,
    };
    defer fds.close(io);

    var target_buffer: [std.fs.max_path_bytes]u8 = undefined;
    var entries = fds.iterate();
    while (entries.next(io) catch return .no) |entry| {
        const length = fds.readLink(io, entry.name, &target_buffer) catch continue;
        if (std.mem.eql(u8, target_buffer[0..length], real_path)) return .yes;
    }
    return .no;
}

/// Whether someone holds the lock on `path` right now (for skipping, not for safety): takes it
/// for a moment, as common.is_locked does.
pub fn isLocked(io: std.Io, path: []const u8) !bool {
    const file = std.Io.Dir.cwd().openFile(io, path, .{}) catch |err| switch (err) {
        error.FileNotFound => return false,
        else => return err,
    };
    defer file.close(io);
    if (!try file.tryLock(io, .exclusive)) return true;
    file.unlock(io);
    return false;
}

/// Hold an exclusive lock on `path` (a lock file) until the file is closed, or the program
/// ends however it ends; if someone else holds it, stop with the message `busy`. The same lock
/// (flock) as common.locked, so the Python and Zig programs keep each other out.
pub fn lock(io: std.Io, path: []const u8, busy: []const u8) !std.Io.File {
    const file = try std.Io.Dir.cwd().createFile(io, path, .{ .truncate = true });
    errdefer file.close(io);
    if (!try file.tryLock(io, .exclusive)) fail("error: {s} (locked: {s})", .{ busy, path });
    return file;
}

/// `path` opened for adding to its end (made if it isn't there, with its directory): a log.
pub fn openAppend(io: std.Io, path: []const u8) !std.Io.File {
    if (std.fs.path.dirname(path)) |dir| try std.Io.Dir.cwd().createDirPath(io, dir);
    const fd = try std.posix.openat(std.posix.AT.FDCWD, path, .{ .ACCMODE = .WRONLY, .CREAT = true, .APPEND = true, .CLOEXEC = true }, 0o644);
    return .{ .handle = fd, .flags = .{ .nonblocking = false } };
}

/// A log line, as common.logger writes them: "[12:14:09 releases] message", on stderr (and in
/// the file of logAlsoTo).
pub fn log(io: std.Io, comptime tool: []const u8, comptime format: []const u8, args: anytype) void {
    const now = Time.now(io).local();
    emit(io, .logger, "[{d:0>2}:{d:0>2}:{d:0>2} " ++ tool ++ "] " ++ format, .{ now.hour, now.minute, now.second } ++ args);
}

/// What stops a program (common.die's message), or what it did instead of what it would have
/// done: "error: message", or the message as it is
pub fn logLine(io: std.Io, comptime format: []const u8, args: anytype) void {
    emit(io, .other, format, args);
}

/// A file every log line also goes to, from now on (null: none): a long-running program's
/// own log, as common.log_also_to keeps it. Each line there starts with the date, and the time
/// too where it doesn't have it already.
pub fn logAlsoTo(file: ?std.Io.File) void {
    also_to = file;
}

var also_to: ?std.Io.File = null;

fn emit(io: std.Io, comptime kind: enum { logger, other }, comptime format: []const u8, args: anytype) void {
    var buffer: [8192]u8 = undefined;
    const line = std.fmt.bufPrint(&buffer, format ++ "\n", args) catch blk: {
        buffer[buffer.len - 4 ..].* = "...\n".*;
        break :blk buffer[0..];
    };
    std.debug.print("{s}", .{line});
    const file = also_to orelse return;
    const now = Time.now(io).local();
    var stamp_buffer: [32]u8 = undefined;
    const stamp = switch (kind) {
        .logger => std.fmt.bufPrint(&stamp_buffer, "{d}-{d:0>2}-{d:0>2} ", .{ now.year, now.month, now.day }),
        .other => std.fmt.bufPrint(&stamp_buffer, "{d}-{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}:{d:0>2} ", .{ now.year, now.month, now.day, now.hour, now.minute, now.second }),
    } catch unreachable;
    file.writeStreamingAll(io, stamp) catch {};
    file.writeStreamingAll(io, line) catch {};
}

pub const DiskUsage = struct {
    path: []const u8,
    free: u64,
    total: u64,
};

/// Free (for an unprivileged user) and total bytes of the filesystem `path` is on, like
/// Python's shutil.disk_usage. std has no statfs, so this calls the kernel itself.
pub fn diskUsage(arena: std.mem.Allocator, path: []const u8) !DiskUsage {
    // struct statfs on 64-bit Linux (man 2 statfs)
    const Statfs = extern struct {
        type: isize,
        bsize: isize,
        blocks: u64,
        bfree: u64,
        bavail: u64,
        files: u64,
        ffree: u64,
        fsid: [2]i32,
        namelen: isize,
        frsize: isize,
        flags: isize,
        spare: [4]isize,
    };
    comptime std.debug.assert(@sizeOf(usize) == 8);

    const path_z = try arena.dupeSentinel(u8, path, 0);
    var result: Statfs = undefined;
    const rc = linux.syscall2(.statfs, @intFromPtr(path_z.ptr), @intFromPtr(&result));
    if (linux.errno(rc) != .SUCCESS) return error.StatfsFailed;

    var block: u64 = @intCast(result.frsize);
    if (block == 0) block = @intCast(result.bsize);
    return .{ .path = path, .free = result.bavail * block, .total = result.blocks * block };
}

/// A systemd user unit's properties, as `systemctl --user show` gives them: numbers as
/// numbers, times as hopper writes them (RFC 3339), empty values (never) as null.
pub fn unit(io: std.Io, arena: std.mem.Allocator, name: []const u8, properties: []const []const u8) !std.json.Value {
    const joined = try std.mem.join(arena, ",", properties);
    const result = try std.process.run(arena, io, .{
        .argv = &.{ "systemctl", "--user", "show", name, "--timestamp=unix", "-p", joined },
    });

    var values: std.json.ObjectMap = .empty;
    var lines = std.mem.splitScalar(u8, result.stdout, '\n');
    while (lines.next()) |line| {
        const equals = std.mem.indexOfScalar(u8, line, '=') orelse continue;
        const key = line[0..equals];
        const text = line[equals + 1 ..];
        try values.put(arena, key, try unitValue(arena, text));
    }
    return .{ .object = values };
}

fn unitValue(arena: std.mem.Allocator, text: []const u8) !std.json.Value {
    if (text.len == 0) return .null;
    // A time: "@" and seconds since the epoch
    if (text[0] == '@') {
        if (std.fmt.parseInt(i64, text[1..], 10)) |seconds| {
            const time: Time = .{ .seconds = seconds };
            return .{ .string = try std.fmt.allocPrint(arena, "{f}", .{time}) };
        } else |_| {}
    }
    if (std.fmt.parseInt(i64, text, 10)) |number| {
        return .{ .integer = number };
    } else |_| {}
    return .{ .string = text };
}

/// `git describe --always --dirty` of the checkout at `path`, or null.
pub fn gitDescribe(io: std.Io, arena: std.mem.Allocator, path: []const u8) !?[]const u8 {
    const result = std.process.run(arena, io, .{
        .argv = &.{ "git", "-C", path, "describe", "--always", "--dirty" },
    }) catch return null;
    const described = std.mem.trim(u8, result.stdout, " \n");
    if (described.len == 0) return null;
    return described;
}

pub fn hostname(arena: std.mem.Allocator) ![]const u8 {
    var buffer: [std.posix.HOST_NAME_MAX]u8 = undefined;
    return arena.dupe(u8, try std.posix.gethostname(&buffer));
}

/// Stop the program with a message, like common.die.
pub fn fail(comptime format: []const u8, args: anytype) noreturn {
    std.debug.print(format ++ "\n", args);
    std.process.exit(1);
}

pub fn exists(io: std.Io, path: []const u8) bool {
    std.Io.Dir.cwd().access(io, path, .{}) catch return false;
    return true;
}

/// A whole file, or null if it doesn't exist. Read to its end rather than to the size it says
/// it has: files in /proc say 0 (std's readFileAlloc reads nothing of them).
pub fn readFile(io: std.Io, arena: std.mem.Allocator, path: []const u8) !?[]u8 {
    const file = std.Io.Dir.cwd().openFile(io, path, .{}) catch |err| switch (err) {
        error.FileNotFound => return null,
        else => return err,
    };
    defer file.close(io);

    var buffer: [4096]u8 = undefined;
    var reader = file.readerStreaming(io, &buffer);
    return reader.interface.allocRemaining(arena, .limited(64 * 1024 * 1024)) catch |err| switch (err) {
        error.ReadFailed => return reader.err.?,
        else => return err,
    };
}
