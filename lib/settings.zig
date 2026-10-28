// The settings. config/defaults.env lists every setting with its default; a setup's file
// (config/production.env, ...) overrides some of them, and the environment overrides both.
// Both files are KEY=value lines; values may be quoted and may use $HOPPER (the checkout),
// $STATE, $HOME and settings defined before them, as $NAME or ${NAME}.

const std = @import("std");
const hopper = @import("hopper.zig");
const system = @import("system.zig");

const Map = std.array_hash_map.String([]const u8);

pub const Settings = struct {
    values: Map,

    /// A setting's value. Every setting is in defaults.env, so asking for one that isn't is a
    /// mistake in the program.
    pub fn get(self: Settings, name: []const u8) []const u8 {
        return self.values.get(name) orelse std.debug.panic("no setting {s} in defaults.env", .{name});
    }

    /// Whether a setting is set to anything (an empty value turns things off).
    pub fn isSet(self: Settings, name: []const u8) bool {
        return self.get(name).len > 0;
    }
};

/// The settings of a setup: defaults.env, then `config_file` (if there's one), then the
/// environment.
pub fn load(init: std.process.Init, paths: hopper.Paths, config_file: ?[]const u8) !Settings {
    const arena = init.arena.allocator();

    var base: Map = .empty;
    try base.put(arena, "HOPPER", paths.hopper);
    try base.put(arena, "STATE", paths.state);
    try base.put(arena, "HOME", init.environ_map.get("HOME") orelse "");

    const defaults_text = try system.readFile(init.io, arena, paths.defaults) orelse
        system.fail("{s} doesn't exist", .{paths.defaults});
    const defaults = try parse(arena, paths.defaults, defaults_text, base);
    var values = try defaults.clone(arena);

    if (config_file) |file| {
        // A setup's file can use the defaults too
        var known = try base.clone(arena);
        for (defaults.keys(), defaults.values()) |key, value| {
            try known.put(arena, key, value);
        }
        const config_text = try system.readFile(init.io, arena, file) orelse
            system.fail("config file not found: {s}", .{file});
        const overrides = try parse(arena, file, config_text, known);
        for (overrides.keys(), overrides.values()) |key, value| {
            if (!defaults.contains(key)) {
                system.fail("{s}: unknown setting {s} (all settings are in defaults.env)", .{ file, key });
            }
            try values.put(arena, key, value);
        }
    }
    for (defaults.keys()) |key| {
        if (init.environ_map.get(key)) |value| {
            try values.put(arena, key, value);
        }
    }
    return .{ .values = values };
}

/// Parse a KEY=value file; `known` gives the values $VARIABLES can refer to.
pub fn parse(arena: std.mem.Allocator, path: []const u8, text: []const u8, known: Map) !Map {
    var values: Map = .empty;
    var lines = std.mem.splitScalar(u8, text, '\n');
    var number: usize = 0;
    while (lines.next()) |raw_line| {
        number += 1;
        const line = std.mem.trim(u8, raw_line, " \t\r");
        if (line.len == 0 or line[0] == '#') continue;

        const equals = std.mem.indexOfScalar(u8, line, '=') orelse
            system.fail("{s}:{d}: expected KEY=value", .{ path, number });
        const key = line[0..equals];
        if (!isSettingName(key)) {
            system.fail("{s}:{d}: expected KEY=value", .{ path, number });
        }

        var value = line[equals + 1 ..];
        if (value.len >= 2 and value[0] == value[value.len - 1] and (value[0] == '"' or value[0] == '\'')) {
            value = value[1 .. value.len - 1];
        }
        const expanded = try expand(arena, value, known, values) orelse
            system.fail("{s}:{d}: unknown variable in {s}", .{ path, number, value });
        try values.put(arena, key, expanded);
    }
    return values;
}

/// `value` with its $NAME and ${NAME} replaced, from `values` first, then `known`; null if
/// one of them is neither.
fn expand(arena: std.mem.Allocator, value: []const u8, known: Map, values: Map) !?[]const u8 {
    var out: std.ArrayList(u8) = .empty;
    var i: usize = 0;
    while (i < value.len) {
        if (value[i] != '$') {
            try out.append(arena, value[i]);
            i += 1;
            continue;
        }

        var name_start = i + 1;
        var name_end = name_start;
        const braced = name_start < value.len and value[name_start] == '{';
        if (braced) name_start += 1;
        name_end = name_start;
        while (name_end < value.len and isWordChar(value[name_end])) name_end += 1;
        const name = value[name_start..name_end];

        // A $ that doesn't start a variable stays as it is
        if (name.len == 0 or (braced and (name_end >= value.len or value[name_end] != '}'))) {
            try out.append(arena, '$');
            i += 1;
            continue;
        }

        const replacement = values.get(name) orelse known.get(name) orelse return null;
        try out.appendSlice(arena, replacement);
        i = if (braced) name_end + 1 else name_end;
    }
    return out.items;
}

fn isSettingName(name: []const u8) bool {
    if (name.len == 0 or !std.ascii.isUpper(name[0])) return false;
    for (name) |c| {
        if (!std.ascii.isUpper(c) and !std.ascii.isDigit(c) and c != '_') return false;
    }
    return true;
}

fn isWordChar(c: u8) bool {
    return std.ascii.isAlphanumeric(c) or c == '_';
}

test "values, quotes and variables" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    var known: Map = .empty;
    try known.put(arena, "STATE", "/state");
    const text =
        \\# a comment
        \\
        \\ROOT=$STATE/files
        \\QUOTED="a b"
        \\BRACED=${ROOT}.snapshots
        \\PRICE=5$
    ;
    const values = try parse(arena, "test.env", text, known);
    try std.testing.expectEqualStrings("/state/files", values.get("ROOT").?);
    try std.testing.expectEqualStrings("a b", values.get("QUOTED").?);
    try std.testing.expectEqualStrings("/state/files.snapshots", values.get("BRACED").?);
    try std.testing.expectEqualStrings("5$", values.get("PRICE").?);
}
