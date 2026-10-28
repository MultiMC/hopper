const std = @import("std");
const render = @import("../render/mod.zig");
const style = @import("../style/mod.zig");
const Rect = render.Rect;
const Buffer = render.Buffer;
const Style = style.Style;
const codepointWidth = render.codepointWidth;

pub const Tabs = struct {
    titles: []const []const u8,
    selected: usize = 0,
    style: Style = .{},
    /// Style applied to the active tab title.
    selected_style: Style = .{},
    /// Style applied to any inactive tab title.
    unselected_style: Style = .{},
    /// Character drawn between tab titles.
    divider: u21 = 0x2502, // │
    /// Padding spaces on each side of a title.
    padding: u16 = 1,

    pub fn render(self: Tabs, area: Rect, buf: *Buffer) void {
        if (area.width == 0 or area.height == 0 or self.titles.len == 0) return;

        var x: u16 = area.x;
        const y = area.y;

        // Fill background
        {
            var fx: u16 = area.x;
            while (fx < area.x + area.width) : (fx += 1) {
                buf.setChar(fx, y, ' ', self.style);
            }
        }

        for (self.titles, 0..) |title, i| {
            if (x >= area.x + area.width) break;

            const is_selected = i == self.selected;
            const tab_style = if (is_selected) self.style.merge(self.selected_style) else self.style.merge(self.unselected_style);

            // Left padding
            var p: u16 = 0;
            while (p < self.padding and x < area.x + area.width) : (p += 1) {
                buf.setChar(x, y, ' ', tab_style);
                x += 1;
            }

            // Title characters: advance by display width so a wide glyph is
            // not written into the previous character's trailing cell.
            var view = std.unicode.Utf8View.initUnchecked(title);
            var iter = view.iterator();
            while (iter.nextCodepoint()) |cp| {
                const w: u16 = @intCast(codepointWidth(cp));
                if (w == 0) continue;
                if (x + w > area.x + area.width) break;
                buf.setChar(x, y, cp, tab_style);
                x += w;
            }

            // Right padding
            p = 0;
            while (p < self.padding and x < area.x + area.width) : (p += 1) {
                buf.setChar(x, y, ' ', tab_style);
                x += 1;
            }

            // Divider (skip after last tab)
            if (i + 1 < self.titles.len and x < area.x + area.width) {
                buf.setChar(x, y, self.divider, self.style);
                x += 1;
            }
        }
    }

    /// Move selection to the next tab (wraps around).
    pub fn selectNext(self: *Tabs) void {
        if (self.titles.len == 0) return;
        self.selected = (self.selected + 1) % self.titles.len;
    }

    /// Move selection to the previous tab (wraps around).
    pub fn selectPrevious(self: *Tabs) void {
        if (self.titles.len == 0) return;
        if (self.selected == 0) {
            self.selected = self.titles.len - 1;
        } else {
            self.selected -= 1;
        }
    }
};

test "Tabs selectNext wraps" {
    var tabs = Tabs{ .titles = &.{ "A", "B", "C" }, .selected = 2 };
    tabs.selectNext();
    try std.testing.expectEqual(@as(usize, 0), tabs.selected);
}

test "Tabs selectPrevious wraps" {
    var tabs = Tabs{ .titles = &.{ "A", "B", "C" }, .selected = 0 };
    tabs.selectPrevious();
    try std.testing.expectEqual(@as(usize, 2), tabs.selected);
}

test "Tabs renders wide characters without clobbering them" {
    var buf = try render.Buffer.init(std.testing.allocator, 24, 1);
    defer buf.deinit();

    const tabs = Tabs{
        .titles = &.{ "中文", "B" },
        .selected = 0,
        .padding = 0,
    };
    tabs.render(.{ .x = 0, .y = 0, .width = 24, .height = 1 }, &buf);

    // "中文" occupies columns 0-3, then the divider, then "B".
    try std.testing.expectEqual(@as(u21, 0x4E2D), buf.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u2, 2), buf.get(0, 0).?.width);
    try std.testing.expectEqual(@as(u2, 0), buf.get(1, 0).?.width); // continuation
    try std.testing.expectEqual(@as(u21, 0x6587), buf.get(2, 0).?.char);
    try std.testing.expectEqual(@as(u2, 2), buf.get(2, 0).?.width);
    try std.testing.expectEqual(@as(u2, 0), buf.get(3, 0).?.width); // continuation
    try std.testing.expectEqual(@as(u21, 0x2502), buf.get(4, 0).?.char); // │
    try std.testing.expectEqual(@as(u21, 'B'), buf.get(5, 0).?.char);
}

test "Tabs stops before a wide character that would straddle the edge" {
    var buf = try render.Buffer.init(std.testing.allocator, 3, 1);
    defer buf.deinit();

    const tabs = Tabs{ .titles = &.{"中"}, .padding = 0 };
    tabs.render(.{ .x = 0, .y = 0, .width = 3, .height = 1 }, &buf);

    // 2-column glyph at column 0 fits; a second one would need columns 2-3
    // but the area is only 3 wide, so it must not be written.
    try std.testing.expectEqual(@as(u21, 0x4E2D), buf.get(0, 0).?.char);
    try std.testing.expectEqual(@as(u21, ' '), buf.get(2, 0).?.char);
}
