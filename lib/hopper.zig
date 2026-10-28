// What hopper's Zig programs share: where things are, and stopping with a message. The rest is
// in the files next to this one.

const std = @import("std");

pub const json = @import("json.zig");
pub const settings = @import("settings.zig");
pub const state = @import("state.zig");
pub const jobs = @import("jobs.zig");
pub const time = @import("time.zig");
pub const system = @import("system.zig");

pub const Settings = settings.Settings;

/// Where things are: the checkout (the programs are in its zig-out/bin), its settings, and what
/// the scripts keep between runs, outside the checkout: $XDG_STATE_HOME/hopper, by default
/// ~/.local/state/hopper.
pub const Paths = struct {
    hopper: []const u8,
    config: []const u8,
    defaults: []const u8,
    state: []const u8,

    pub fn find(init: std.process.Init) !Paths {
        const arena = init.arena.allocator();
        const bin = try std.process.executableDirPathAlloc(init.io, arena);
        const hopper = std.fs.path.dirname(std.fs.path.dirname(bin) orelse bin) orelse bin;
        const config = try std.fs.path.join(arena, &.{ hopper, "config" });
        const defaults = try std.fs.path.join(arena, &.{ config, "defaults.env" });
        if (!system.exists(init.io, defaults)) {
            system.fail("{s} isn't in a hopper checkout's zig-out/bin (no {s})", .{ bin, defaults });
        }

        var state_home: []const u8 = undefined;
        if (init.environ_map.get("XDG_STATE_HOME")) |xdg| {
            state_home = xdg;
        } else {
            const home = init.environ_map.get("HOME") orelse system.fail("HOME isn't set", .{});
            state_home = try std.fs.path.join(arena, &.{ home, ".local", "state" });
        }
        const state_dir = try std.fs.path.join(arena, &.{ state_home, "hopper" });

        return .{ .hopper = hopper, .config = config, .defaults = defaults, .state = state_dir };
    }
};

test {
    std.testing.refAllDecls(@This());
}
