const std = @import("std");
const render = @import("../render/mod.zig");
const style = @import("../style/mod.zig");
const Rect = render.Rect;
const Buffer = render.Buffer;
const Style = style.Style;
const codepointWidth = render.codepointWidth;

pub const Paragraph = struct {
    text: []const u8,
    style: Style = .{},
    wrap: bool = true,

    pub fn render(self: Paragraph, area: Rect, buf: *Buffer) void {
        if (area.width == 0 or area.height == 0) return;

        var y_offset: u16 = 0;
        var x_offset: u16 = 0;
        var view = std.unicode.Utf8View.initUnchecked(self.text);
        var iter = view.iterator();

        while (iter.nextCodepoint()) |codepoint| {
            if (y_offset >= area.height) break;

            if (codepoint == '\n') {
                y_offset += 1;
                x_offset = 0;
                continue;
            }

            const w: u16 = @intCast(codepointWidth(codepoint));
            if (w == 0) continue;

            // Wide characters occupy two columns; wrap before one that would
            // straddle the right edge.
            if (x_offset + w > area.width) {
                if (self.wrap) {
                    y_offset += 1;
                    x_offset = 0;
                } else {
                    // Skip to next line
                    while (iter.nextCodepoint()) |c| {
                        if (c == '\n') break;
                    }
                    y_offset += 1;
                    x_offset = 0;
                    continue;
                }
            }

            if (y_offset < area.height) {
                buf.setChar(area.x + x_offset, area.y + y_offset, codepoint, self.style);
                x_offset += w;
            }
        }
    }
};

test "Paragraph renders wide characters at correct columns" {
    var buf = try render.Buffer.init(std.testing.allocator, 12, 1);
    defer buf.deinit();

    const para = Paragraph{ .text = "你好😊" };
    para.render(.{ .x = 0, .y = 0, .width = 12, .height = 1 }, &buf);

    // 你 (0-1), 好 (2-3), 😊 (4-5)
    try std.testing.expectEqual(@as(u21, 0x4F60), buf.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u2, 2), buf.get(0, 0).?.width);
    try std.testing.expectEqual(@as(u2, 0), buf.get(1, 0).?.width); // continuation
    try std.testing.expectEqual(@as(u21, 0x597D), buf.get(2, 0).?.char);
    try std.testing.expectEqual(@as(u2, 2), buf.get(2, 0).?.width);
    try std.testing.expectEqual(@as(u21, 0x1F60A), buf.get(4, 0).?.char);
    try std.testing.expectEqual(@as(u2, 2), buf.get(4, 0).?.width);
    try std.testing.expectEqual(@as(u2, 0), buf.get(5, 0).?.width); // continuation
}

test "Paragraph wraps before a wide character that would straddle the edge" {
    var buf = try render.Buffer.init(std.testing.allocator, 5, 2);
    defer buf.deinit();

    // "a你b" in a 5-column area: 'a' (0), 你 (1-2), 'b' (3).
    const para = Paragraph{ .text = "a你b" };
    para.render(.{ .x = 0, .y = 0, .width = 5, .height = 2 }, &buf);
    try std.testing.expectEqual(@as(u21, 'a'), buf.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u21, 0x4F60), buf.get(1, 0).?.char);
    try std.testing.expectEqual(@as(u21, 'b'), buf.get(3, 0).?.char);

    // A 4-column area: 'a' (0), 你 (1-2) fits, 'b' (3) fits. Edge case:
    // "ab你" in width 3 must wrap 你 to the next line instead of splitting it.
    var buf2 = try render.Buffer.init(std.testing.allocator, 3, 2);
    defer buf2.deinit();
    const para2 = Paragraph{ .text = "ab你" };
    para2.render(.{ .x = 0, .y = 0, .width = 3, .height = 2 }, &buf2);
    try std.testing.expectEqual(@as(u21, 'a'), buf2.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u21, 'b'), buf2.get(1, 0).?.char);
    try std.testing.expectEqual(@as(u21, 0x4F60), buf2.get(0, 1).?.char); // wrapped
}

test "Paragraph respects newlines and non-wrap mode" {
    var buf = try render.Buffer.init(std.testing.allocator, 6, 2);
    defer buf.deinit();

    const para = Paragraph{ .text = "ab\ncd", .wrap = false };
    para.render(.{ .x = 0, .y = 0, .width = 6, .height = 2 }, &buf);
    try std.testing.expectEqual(@as(u21, 'a'), buf.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u21, 'c'), buf.get(0, 1).?.char);
}
