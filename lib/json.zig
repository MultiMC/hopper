// Reading and writing hopper's JSON files, after OpenTitus's json.zig: a file read is a
// std.json.Parsed (its value and the arena it's in, freed together), files are written whole
// and replaced at once (like the Python's .new and os.replace), and List is a list that can be
// added to, for the lists in them.

const std = @import("std");
const hopper = @import("hopper.zig");
const system = hopper.system;

const Allocator = std.mem.Allocator;
const ParseOptions = std.json.ParseOptions;

/// A file's value and the arena it's in.
pub const Parsed = std.json.Parsed;

/// A file as a `T`, in its own arena; null if it doesn't exist. A file that doesn't parse as a
/// `T` (a field it doesn't have, a value it can't be) stops the program, naming the file.
pub fn read(comptime T: type, io: std.Io, gpa: Allocator, path: []const u8) !?Parsed(T) {
    const text = try system.readFile(io, gpa, path) orelse return null;
    defer gpa.free(text);
    return std.json.parseFromSlice(T, gpa, text, .{ .allocate = .alloc_always }) catch |err| switch (err) {
        error.OutOfMemory => return error.OutOfMemory,
        else => system.fail("{s}: not what hopper writes there ({s})", .{ path, @errorName(err) }),
    };
}

/// A new value of a file not written yet, in its own arena (as `read` gives them).
pub fn fresh(comptime T: type, gpa: Allocator, value: T) !Parsed(T) {
    const arena = try gpa.create(std.heap.ArenaAllocator);
    arena.* = .init(gpa);
    return .{ .arena = arena, .value = value };
}

/// Write `value` to `path`: to a new file that then replaces it, so whoever reads it sees the
/// old file or the new one, never a part. Its directory is made if it isn't there.
pub fn write(io: std.Io, path: []const u8, value: anytype) !void {
    var file = try std.Io.Dir.cwd().createFileAtomic(io, path, .{ .make_path = true, .replace = true });
    defer file.deinit(io);

    var buffer: [4096]u8 = undefined;
    var writer = file.file.writer(io, &buffer);
    writeTo(&writer.interface, value) catch |err| switch (err) {
        error.WriteFailed => return writer.err.?,
    };
    try writer.interface.flush();
    try file.replace(io);
}

/// How hopper writes JSON: indented by two, as the Python does, and without the fields it
/// doesn't have (null).
pub fn writeTo(writer: *std.Io.Writer, value: anytype) std.Io.Writer.Error!void {
    var stream: std.json.Stringify = .{
        .writer = writer,
        .options = .{ .whitespace = .indent_2, .emit_null_optional_fields = false },
    };
    try stream.write(value);
    try writer.writeByte('\n');
}

/// A JSON array as a list that can be added to (std.json parses arrays into slices only).
pub fn List(comptime T: type) type {
    return struct {
        list: std.ArrayList(T) = .empty,

        pub fn jsonParse(gpa: Allocator, source: anytype, options: ParseOptions) !@This() {
            var list: std.ArrayList(T) = .empty;
            errdefer list.deinit(gpa);

            if (try source.next() != .array_begin) return error.UnexpectedToken;
            while (try source.peekNextTokenType() != .array_end) {
                try list.append(gpa, try std.json.innerParse(T, gpa, source, options));
            }
            _ = try source.next();
            return .{ .list = list };
        }

        pub fn jsonParseFromValue(gpa: Allocator, source: std.json.Value, options: ParseOptions) !@This() {
            if (source != .array) return error.UnexpectedToken;

            var list: std.ArrayList(T) = .empty;
            errdefer list.deinit(gpa);
            for (source.array.items) |item| {
                try list.append(gpa, try std.json.innerParseFromValue(T, gpa, item, options));
            }
            return .{ .list = list };
        }

        pub fn jsonStringify(self: @This(), writer: *std.json.Stringify) !void {
            try writer.write(self.list.items);
        }
    };
}

test "lists, read and written" {
    var arena_state = std.heap.ArenaAllocator.init(std.testing.allocator);
    defer arena_state.deinit();
    const arena = arena_state.allocator();

    const Numbers = struct { numbers: List(i64) };
    var numbers = try std.json.parseFromSliceLeaky(Numbers, arena, "{\"numbers\": [1, 2]}", .{});
    try numbers.numbers.list.append(arena, 3);

    var out: std.Io.Writer.Allocating = .init(arena);
    try writeTo(&out.writer, numbers);
    try std.testing.expectEqualStrings(
        \\{
        \\  "numbers": [
        \\    1,
        \\    2,
        \\    3
        \\  ]
        \\}
        \\
    , out.written());

    const value = try std.json.parseFromSliceLeaky(std.json.Value, arena, "{\"numbers\": [4]}", .{});
    const from_value = try std.json.parseFromValueLeaky(Numbers, arena, value, .{});
    try std.testing.expectEqualSlices(i64, &.{4}, from_value.numbers.list.items);
}
