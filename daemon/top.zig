// hopper-top: the overview (daemon/overview.zig) live in the terminal, like top; read-only.
//
//     ssh -t hopper@mikoshi hopper/zig-out/bin/top
//
// SSH is the access control and the transport: whoever may log in as hopper may look. It
// refreshes every two seconds. Keys:
//
//   overview   up/down, PgUp/PgDn, Home/End  select a run
//              Enter    that run's log
//              h        that run's setup's own log (hopper.log: its looks, and crashes)
//              r        refresh now
//              q        quit
//   log        up/down, PgUp/PgDn/space, Home/g, left/right   scroll
//              End/G, f follow the end as it grows (on by default for a running run;
//                       f turns it off again, so does scrolling up)
//              / n N    search (case-insensitive), next, previous
//              q, Esc   back
//
// Nothing here changes anything; actions will be requests the running hopper picks up. Drawn
// with zigtui: a Block per section, Tables for the jobs and the runs, a List for a log.

const std = @import("std");
const hopper = @import("hopper");
const overview = @import("overview");
const tui = @import("zigtui");

const Time = hopper.time.Time;
const RunStatus = hopper.state.RunStatus;
const Buffer = tui.Buffer;
const Rect = tui.Rect;
const Style = tui.Style;
const Allocator = std.mem.Allocator;

/// Puts the terminal back before a panic's trace
pub const panic = tui.panic;

const usage =
    \\usage: zig-out/bin/top
    \\
    \\The overview live in the terminal; keys are at the top of daemon/top.zig.
    \\
;

const refresh_ms = 2000;
const log_refresh_ms = 1000;
/// How long to wait for a key before looking at the clock again
const poll_ms = 250;

// --- how it looks -------------------------------------------------------------------------

const plain: Style = .{};
const bold: Style = .{ .modifier = .BOLD };
const dim: Style = .{ .modifier = .DIM };
const reversed: Style = .{ .modifier = .REVERSED };
const green: Style = .{ .fg = .green };
const red: Style = .{ .fg = .red };
const red_bold: Style = .{ .fg = .red, .modifier = .BOLD };
const yellow: Style = .{ .fg = .yellow };
const yellow_bold: Style = .{ .fg = .yellow, .modifier = .BOLD };
const cyan: Style = .{ .fg = .cyan };
const cyan_bold: Style = .{ .fg = .cyan, .modifier = .BOLD };

/// A row of a run: plain when it published, red when it didn't, yellow while it runs.
fn runStyle(status: RunStatus) Style {
    return switch (status) {
        .published => plain,
        .failed, .interrupted => red,
        .running => yellow_bold,
        .adopted => dim,
    };
}

/// A section: its name in a line across the screen.
fn section(title: []const u8) tui.Block {
    return .{
        .title = title,
        .borders = tui.Borders.TOP,
        .border_symbols = tui.BorderSymbols.line(),
        .border_style = dim,
        .title_style = bold,
    };
}

/// Text in one style, in a line of several.
const Span = struct {
    text: []const u8,
    style: Style = plain,
};

/// A line of spans, each formatted into the frame's memory.
const Line = struct {
    arena: Allocator,
    spans: std.ArrayList(Span) = .empty,

    fn add(line: *Line, style: Style, comptime format: []const u8, args: anytype) !void {
        try line.spans.append(line.arena, .{ .text = try std.fmt.allocPrint(line.arena, format, args), .style = style });
    }

    /// Row `y` of `area`, from its left, cut at its width; `fill` under all of it.
    fn draw(line: Line, buf: *Buffer, area: Rect, y: u16, fill: Style) void {
        if (y >= area.y + area.height) return;
        buf.fillArea(.{ .x = area.x, .y = y, .width = area.width, .height = 1 }, ' ', fill);
        var x = area.x;
        for (line.spans.items) |span| {
            const room = area.x + area.width -| x;
            if (room == 0) break;
            x += buf.putString(x, y, span.text, room, fill.merge(span.style));
        }
    }
};

fn duration(arena: Allocator, seconds: ?i64) ![]const u8 {
    var out: std.Io.Writer.Allocating = .init(arena);
    try overview.printDuration(&out.writer, seconds);
    return out.written();
}

/// "10-28 12:14", as the clock here showed it
fn shortTime(arena: Allocator, time: ?Time) ![]const u8 {
    const local = (time orelse return "").local();
    return std.fmt.allocPrint(arena, "{d:0>2}-{d:0>2} {d:0>2}:{d:0>2}", .{ local.month, local.day, local.hour, local.minute });
}

/// A systemd unit's property (overview.zig has them as JSON: text, a number, or null)
fn unitText(unit: std.json.Value, name: []const u8) ?[]const u8 {
    const value = unit.object.get(name) orelse return null;
    return switch (value) {
        .string => |text| text,
        else => null,
    };
}

fn unitNumber(unit: std.json.Value, name: []const u8) ?i64 {
    const value = unit.object.get(name) orelse return null;
    return switch (value) {
        .integer => |number| number,
        else => null,
    };
}

/// A unit's time property (RFC 3339, as the overview writes them)
fn unitTime(unit: std.json.Value, name: []const u8) ?Time {
    return Time.parse(unitText(unit, name) orelse return null) catch null;
}

fn size(arena: Allocator, bytes: u64) ![]const u8 {
    const units = "BKMGT";
    var n: f64 = @floatFromInt(bytes);
    for (units, 0..) |unit, i| {
        if (n < 1024 or i == units.len - 1) {
            if (unit == 'G' or unit == 'T') return std.fmt.allocPrint(arena, "{d:.1}{c}", .{ n, unit });
            return std.fmt.allocPrint(arena, "{d:.0}{c}", .{ n, unit });
        }
        n /= 1024;
    }
    unreachable;
}

// --- what it shows ------------------------------------------------------------------------

/// One gathering of the overview, in its own memory (freed when the next one replaces it).
const Data = struct {
    arena: std.heap.ArenaAllocator,
    overview: overview.Overview,
    /// Every setup's runs, newest first
    runs: []const SetupRun,
    /// When it was gathered (the awake clock, for refreshing)
    loaded: i64,

    const SetupRun = struct {
        setup: *const overview.Setup,
        run: overview.RunSummary,
    };

    fn load(init: std.process.Init, paths: hopper.Paths) !*Data {
        const data = try init.gpa.create(Data);
        errdefer init.gpa.destroy(data);
        data.arena = .init(init.gpa);
        errdefer data.arena.deinit();
        const arena = data.arena.allocator();

        // Everything it gathers goes in this gathering's memory, the settings too
        var gathering = init;
        gathering.arena = &data.arena;
        data.overview = try overview.gather(gathering, .{
            .io = init.io,
            .arena = arena,
            .paths = paths,
            .now = .now(init.io),
        });

        var runs: std.ArrayList(SetupRun) = .empty;
        for (data.overview.setups) |*setup| {
            for (setup.runs) |run| try runs.append(arena, .{ .setup = setup, .run = run });
        }
        std.mem.sort(SetupRun, runs.items, {}, newerFirst);
        data.runs = runs.items;
        data.loaded = awakeMs(init.io);
        return data;
    }

    fn deinit(data: *Data, gpa: Allocator) void {
        data.arena.deinit();
        gpa.destroy(data);
    }

    fn newerFirst(_: void, a: SetupRun, b: SetupRun) bool {
        return a.run.entry.time.order(b.run.entry.time) == .gt;
    }
};

fn awakeMs(io: std.Io) i64 {
    return std.Io.Timestamp.now(io, .awake).toMilliseconds();
}

// --- the overview screen ------------------------------------------------------------------

const Top = struct {
    init: std.process.Init,
    paths: hopper.Paths,
    data: *Data,
    /// Memory for drawing one frame
    frame: std.heap.ArenaAllocator,
    /// The selected row in the list of runs, 0 is the newest
    selected: usize = 0,
    /// The first row shown
    offset: usize = 0,
    /// How many runs fit on the screen
    page: usize = 1,

    fn refresh(top: *Top) !void {
        const data = try Data.load(top.init, top.paths);
        top.data.deinit(top.init.gpa);
        top.data = data;
        top.selected = @min(top.selected, data.runs.len -| 1);
    }

    fn run(top: *Top, terminal: *tui.Terminal) !void {
        while (true) {
            if (awakeMs(top.init.io) - top.data.loaded >= refresh_ms) try top.refresh();
            try terminal.draw(top, draw);

            const event = try terminal.backend_impl.pollEvent(poll_ms);
            switch (event) {
                .resize => |new| try terminal.resize(.{ .width = new.width, .height = new.height }),
                .key => |key| switch (key.code) {
                    .char => |c| switch (c) {
                        'q', 'Q' => return,
                        'c' => if (key.modifiers.ctrl) return,
                        'r' => try top.refresh(),
                        'k' => top.move(-1),
                        'j' => top.move(1),
                        'h' => {
                            const setup = if (top.data.runs.len > 0)
                                top.data.runs[top.selected].setup
                            else
                                &top.data.overview.setups[0];
                            const title = try std.fmt.allocPrint(top.frame.allocator(), "{s} hopper.log", .{setup.name});
                            try LogView.show(top.init, terminal, setup.log, title, true);
                            try top.refresh();
                        },
                        else => {},
                    },
                    .up => top.move(-1),
                    .down => top.move(1),
                    .page_up => top.move(-@as(isize, @intCast(top.page))),
                    .page_down => top.move(@intCast(top.page)),
                    .home => top.selected = 0,
                    .end => top.selected = top.data.runs.len -| 1,
                    .enter => if (top.data.runs.len > 0) {
                        const chosen = top.data.runs[top.selected];
                        const title = try std.fmt.allocPrint(top.frame.allocator(), "{s} {s} {s}", .{ chosen.setup.name, chosen.run.job, chosen.run.label });
                        try LogView.show(top.init, terminal, chosen.run.entry.log, title, chosen.run.entry.status == .running);
                        try top.refresh();
                    },
                    else => {},
                },
                else => {},
            }
        }
    }

    fn move(top: *Top, by: isize) void {
        const last: isize = @intCast(top.data.runs.len -| 1);
        const to = std.math.clamp(@as(isize, @intCast(top.selected)) + by, 0, last);
        top.selected = @intCast(to);
    }

    fn draw(top: *Top, buf: *Buffer) !void {
        _ = top.frame.reset(.retain_capacity);
        const arena = top.frame.allocator();
        const area = buf.getArea();
        const data = top.data.overview;
        const now = Time.now(top.init.io);

        var header: Line = .{ .arena = arena };
        const clock = data.time.local();
        try header.add(bold, " hopper-top  {s}  {d:0>2}:{d:0>2}:{d:0>2}", .{ data.host, clock.hour, clock.minute, clock.second });
        if (data.code) |code| try header.add(plain, "   code {s}", .{code});
        try header.add(plain, "   free {s} of {s}", .{ try size(arena, data.disk.free), try size(arena, data.disk.total) });
        if (data.release_build) |pid| try header.add(yellow_bold, "   release build running (pid {d})", .{pid});
        header.draw(buf, area, area.y, reversed);

        var y = area.y + 2;
        for (data.setups) |*setup| {
            y = try top.drawSetup(buf, area, y, setup, now) + 1;
        }
        y = try top.drawMeta(buf, area, y, data.meta, now) + 1;

        const footer = area.y + area.height -| 1;
        const runs_block = section(" runs  Enter: log, h: hopper.log ");
        const runs_area: Rect = .{ .x = area.x, .y = y, .width = area.width, .height = footer -| y };
        runs_block.render(runs_area, buf);
        const table_area = runs_block.inner(runs_area);
        top.page = @max(1, table_area.height -| 1);
        // Scroll just enough to keep the selected row on screen
        if (top.selected < top.offset) top.offset = top.selected;
        if (top.selected >= top.offset + top.page) top.offset = top.selected - top.page + 1;

        var rows: std.ArrayList(tui.widgets.Row) = .empty;
        for (top.data.runs) |item| {
            const cells = try runCells(arena, item.run);
            try rows.append(arena, .{
                .cells = try std.mem.concat(arena, []const u8, &.{ &.{item.setup.name}, cells }),
                .style = runStyle(item.run.entry.status),
            });
        }
        const runs_table: tui.Table = .{
            .columns = &.{
                .{ .header = "setup", .width = 11 },
                .{ .header = "job", .width = 13 },
                .{ .header = "what", .width = 22 },
                .{ .header = "status", .width = 22 },
                .{ .header = "started", .width = 11 },
                .{ .header = "took", .width = 7 },
            },
            .rows = rows.items,
            .header_style = dim,
            .selected_style = reversed,
            .selected = if (rows.items.len > 0) top.selected else null,
            .offset = top.offset,
        };
        // The table reverses the selected row's cells; the gaps between them too
        if (rows.items.len > 0 and table_area.height > 1) {
            const row_y = table_area.y + 1 + @as(u16, @intCast(top.selected - top.offset));
            buf.fillArea(.{ .x = table_area.x, .y = row_y, .width = table_area.width, .height = 1 }, ' ', reversed);
        }
        runs_table.render(table_area, buf);

        var keys: Line = .{ .arena = arena };
        try keys.add(dim, " up/down select  Enter log  h hopper.log  r refresh  q quit", .{});
        keys.draw(buf, area, footer, plain);
    }

    /// A setup's section: its hopper and service, its jobs, what's live. Returns the row after it.
    fn drawSetup(top: *Top, buf: *Buffer, area: Rect, top_y: u16, setup: *const overview.Setup, now: Time) !u16 {
        const arena = top.frame.allocator();
        var y = top_y;
        const title = try std.fmt.allocPrint(arena, " {s} ", .{setup.name});
        section(title).render(.{ .x = area.x, .y = y, .width = area.width, .height = 1 }, buf);
        y += 1;

        var line: Line = .{ .arena = arena };
        try line.add(plain, " ", .{});
        const service = setup.service;
        const active = unitText(service, "ActiveState");
        const file_state = unitText(service, "UnitFileState") orelse "None";
        const enabled = std.mem.eql(u8, file_state, "enabled") or
            std.mem.eql(u8, file_state, "linked-runtime") or
            std.mem.eql(u8, file_state, "enabled-runtime");
        if (setup.running) |pid| {
            try line.add(green, "hopper running", .{});
            if (active != null and std.mem.eql(u8, active.?, "active")) {
                try line.add(plain, " since {s}", .{try shortTime(arena, unitTime(service, "ActiveEnterTimestamp"))});
            }
            if (unitNumber(service, "NRestarts")) |restarts| {
                if (restarts > 0) try line.add(yellow_bold, "  {d} restarts (h: hopper.log)", .{restarts});
            }
            const releasing = for (setup.runs) |r| {
                if (r.entry.status == .running) break r;
            } else null;
            if (releasing) |r| {
                try line.add(yellow_bold, "  releasing {s} {s}: {s}, {s}", .{ r.job, r.label, r.entry.step orelse "None", try duration(arena, r.seconds) });
            } else if (setup.status) |status| {
                if (status.looked) |looked| if (status.pid == pid) {
                    const late = now.since(looked);
                    const stale = late > 3 * status.interval;
                    try line.add(if (stale) red_bold else dim, "  looked {d}s ago", .{late});
                };
            }
            if (setup.status) |status| {
                if (status.problem) |problem| try line.add(red_bold, "  last look failed: {s}", .{problem});
                if (top.data.overview.code) |code| if (!std.mem.eql(u8, status.code, code)) {
                    try line.add(yellow, "  runs {s}, checkout is {s}: restart it", .{ status.code, code });
                };
            }
        } else if (enabled) {
            try line.add(red_bold, "hopper NOT running: service {s}", .{active orelse "None"});
        } else {
            try line.add(dim, "off (service {s}, {s})", .{ active orelse "None", file_state });
        }
        line.draw(buf, area, y, plain);
        y += 1;

        if (setup.@"error") |problem| {
            var error_line: Line = .{ .arena = arena };
            try error_line.add(red_bold, "  can't read its files: {s}", .{problem});
            error_line.draw(buf, area, y, plain);
            return y + 1;
        }

        var rows: std.ArrayList(tui.widgets.Row) = .empty;
        for (setup.jobs) |job| try rows.append(arena, try jobRow(arena, job));
        const jobs_table: tui.Table = .{
            .columns = &.{
                .{ .header = "  job", .width = 15 },
                .{ .header = "source", .width = 12 },
                .{ .header = "what", .width = 22 },
                .{ .header = "status", .width = 22 },
                .{ .header = "started", .width = 11 },
                .{ .header = "took", .width = 7 },
                .{ .header = "" },
            },
            .rows = rows.items,
            .header_style = dim,
        };
        const table_height: u16 = @intCast(rows.items.len + 1);
        jobs_table.render(.{ .x = area.x, .y = y, .width = area.width, .height = @min(table_height, area.height -| y) }, buf);
        y += table_height;

        if (setup.live) |live| {
            (try liveLine(arena, live)).draw(buf, area, y, plain);
        }
        return y + 1;
    }

    fn drawMeta(top: *Top, buf: *Buffer, area: Rect, top_y: u16, meta: overview.Meta, now: Time) !u16 {
        const arena = top.frame.allocator();
        var y = top_y;
        section(" meta ").render(.{ .x = area.x, .y = y, .width = area.width, .height = 1 }, buf);
        y += 1;

        var line: Line = .{ .arena = arena };
        try line.add(plain, " ", .{});
        const service = meta.service;
        const active = unitText(service, "ActiveState") orelse "";
        const started = unitTime(service, "ExecMainStartTimestamp");
        if (std.mem.eql(u8, active, "active") or std.mem.eql(u8, active, "activating")) {
            if (started) |s| {
                try line.add(yellow_bold, "running for {s}", .{try duration(arena, now.since(s))});
            } else {
                try line.add(yellow_bold, "running", .{});
            }
        } else if (started) |s| {
            const result = unitText(service, "Result") orelse "None";
            var seconds: ?i64 = null;
            if (unitTime(service, "InactiveEnterTimestamp")) |ended| seconds = ended.since(s);
            try line.add(plain, "last run {s}  ", .{try shortTime(arena, s)});
            try line.add(if (std.mem.eql(u8, result, "success")) green else red_bold, "{s:<10}", .{result});
            try line.add(plain, "{s:>7}", .{try duration(arena, seconds)});
        } else {
            try line.add(dim, "not run since the machine started", .{});
        }
        if (unitTime(meta.timer, "NextElapseUSecRealtime")) |next| {
            const local = next.local();
            try line.add(dim, "  next {d:0>2}:{d:0>2}", .{ local.hour, local.minute });
        } else {
            try line.add(red_bold, "  timer off", .{});
        }
        line.draw(buf, area, y, plain);

        var journal: Line = .{ .arena = arena };
        try journal.add(dim, "  its output is only in the system journal (journalctl _SYSTEMD_USER_UNIT=meta.service, as an admin)", .{});
        journal.draw(buf, area, y + 1, plain);
        return y + 2;
    }
};

/// A run's cells: what, status, when it started, how long it took.
fn runCells(arena: Allocator, run: overview.RunSummary) ![]const []const u8 {
    const entry = run.entry;
    var what: []const u8 = run.label;
    if (entry.build) |build| what = try std.fmt.allocPrint(arena, "#{d} {s}", .{ build, run.label });
    var status: []const u8 = @tagName(entry.status);
    if (entry.step) |step| if (entry.status != .published) {
        status = try std.fmt.allocPrint(arena, "{s} ({s})", .{ status, step });
    };
    const took = try std.fmt.allocPrint(arena, "{s:>7}", .{try duration(arena, run.seconds)});
    return arena.dupe([]const u8, &.{ run.job, what, status, try shortTime(arena, entry.time), took });
}

/// A job's row: its last run, and what's new about it.
fn jobRow(arena: Allocator, job: overview.JobSummary) !tui.widgets.Row {
    var source = job.source;
    if (std.mem.eql(u8, job.kind, "stream")) {
        // Just the branch
        if (std.mem.lastIndexOfScalar(u8, source, ' ')) |space| source = source[space + 1 ..];
    }
    const name = try std.fmt.allocPrint(arena, "  {s}", .{job.name});
    if (!job.enabled) {
        return .{ .cells = try arena.dupe([]const u8, &.{ name, source, "off" }), .style = dim };
    }
    const last = job.last orelse return .{ .cells = try arena.dupe([]const u8, &.{ name, source }) };

    var notes: std.ArrayList(u8) = .empty;
    var style = runStyle(last.entry.status);
    if (job.head_handled == false) {
        if (job.seen) |seen| try notes.print(arena, "new commit {s}  ", .{seen.head.?[0..@min(seen.head.?.len, 10)]});
        style = yellow_bold;
    }
    if (job.seen) |seen| if (seen.next) |next| {
        try notes.print(arena, "next {s}  ", .{try shortTime(arena, next)});
    };
    if (job.last_published) |published| if (published.entry.time.order(last.entry.time) != .eq) {
        try notes.print(arena, "last published {s}", .{try shortTime(arena, published.entry.time)});
    };
    const cells = try runCells(arena, last);
    return .{
        .cells = try std.mem.concat(arena, []const u8, &.{ &.{ name, source }, cells[1..], &.{notes.items} }),
        .style = style,
    };
}

/// What's live: each update channel's newest versions, the website, the files tree.
fn liveLine(arena: Allocator, live: overview.Live) !Line {
    var line: Line = .{ .arena = arena };
    try line.add(dim, "  {s:<13} ", .{"live"});

    const channels = live.channels.object;
    const names = try arena.dupe([]const u8, channels.keys());
    std.mem.sort([]const u8, names, {}, lessThan);
    for (names) |name| {
        const versions = channels.get(name).?.object;
        const ids = try arena.dupe([]const u8, versions.keys());
        std.mem.sort([]const u8, ids, {}, newerVersion);
        var text: std.ArrayList(u8) = .empty;
        try text.print(arena, "{s}", .{name});
        for (ids) |id| {
            const platforms = versions.get(id).?.array.items.len;
            try text.print(arena, " {s}", .{id});
            if (platforms > 1) try text.print(arena, "x{d}", .{platforms});
        }
        try line.add(cyan, "{s}  ", .{text.items});
    }

    const website = live.website.object;
    if (website.count() > 0) {
        const test_release = if (website.get("test")) |v| v.string else "None";
        const production = if (website.get("production")) |v| v.string else "None";
        if (std.mem.eql(u8, test_release, production)) {
            try line.add(cyan, "website test = production  ", .{});
        } else {
            try line.add(cyan, "website test {s}, production {s}  ", .{ test_release, production });
        }
    }
    if (live.published) |published| {
        const about = published.about orelse published.kind orelse "";
        try line.add(dim, "files: {s}, {s}", .{ about, published.snapshot[0..@min(published.snapshot.len, 13)] });
    }
    return line;
}

fn lessThan(_: void, a: []const u8, b: []const u8) bool {
    return std.mem.lessThan(u8, a, b);
}

/// Version ids, highest first (ones that aren't numbers count as 0, as the overview's do)
fn newerVersion(_: void, a: []const u8, b: []const u8) bool {
    const a_number = std.fmt.parseInt(i64, a, 10) catch 0;
    const b_number = std.fmt.parseInt(i64, b, 10) catch 0;
    return a_number > b_number;
}

// --- a log --------------------------------------------------------------------------------

/// A log file, read as it grows.
const LogView = struct {
    io: std.Io,
    gpa: Allocator,
    /// The lines read so far, and what they're in
    arena: std.heap.ArenaAllocator,
    lines: std.ArrayList([]const u8) = .empty,
    path: ?[]const u8,
    title: []const u8,
    follow: bool,
    /// How far it's read, and the line it's in the middle of
    read_to: u64 = 0,
    partial: []const u8 = "",
    loaded: i64 = 0,
    /// The first line and column shown
    top: usize = 0,
    left: usize = 0,
    search: ?[]const u8 = null,
    message: []const u8 = "",
    /// Typing a search
    prompt: ?tui.widgets.TextInput(256) = null,
    page: usize = 1,

    fn show(init: std.process.Init, terminal: *tui.Terminal, path: ?[]const u8, title: []const u8, follow: bool) !void {
        var view: LogView = .{
            .io = init.io,
            .gpa = init.gpa,
            .arena = .init(init.gpa),
            .path = path,
            .title = title,
            .follow = follow,
        };
        defer view.arena.deinit();
        try view.run(terminal);
    }

    fn run(view: *LogView, terminal: *tui.Terminal) !void {
        while (true) {
            if (awakeMs(view.io) - view.loaded >= log_refresh_ms) {
                try view.load();
                view.loaded = awakeMs(view.io);
            }
            const screen = try terminal.getSize();
            view.page = @max(1, screen.height -| 2);
            const last_top = view.lines.items.len -| view.page;
            if (view.follow) view.top = last_top;
            view.top = @min(view.top, last_top);
            try terminal.draw(view, draw);

            const event = try terminal.backend_impl.pollEvent(poll_ms);
            switch (event) {
                .resize => |new| try terminal.resize(.{ .width = new.width, .height = new.height }),
                .key => |pressed| {
                    if (view.prompt != null) {
                        try view.typeSearch(pressed);
                    } else if (!try view.key(pressed, last_top)) {
                        return;
                    }
                },
                else => {},
            }
        }
    }

    /// Act on a key; false: back to the overview.
    fn key(view: *LogView, pressed: tui.KeyEvent, last_top: usize) !bool {
        const page: isize = @intCast(view.page);
        var by: isize = 0;
        switch (pressed.code) {
            .esc => return false,
            .up => by = -1,
            .down => by = 1,
            .page_up => by = -page,
            .page_down => by = page,
            .home => {
                view.top = 0;
                view.follow = false;
            },
            .end => {
                view.top = last_top;
                view.follow = true;
            },
            .left => view.left -|= 8,
            .right => view.left += 8,
            .char => |c| switch (c) {
                'q' => return false,
                'c' => if (pressed.modifiers.ctrl) return false,
                'k' => by = -1,
                'j' => by = 1,
                ' ' => by = page,
                'g' => {
                    view.top = 0;
                    view.follow = false;
                },
                'G' => {
                    view.top = last_top;
                    view.follow = true;
                },
                'f' => view.follow = !view.follow,
                '/' => {
                    view.prompt = .{ .cursor_style = reversed };
                    view.follow = false;
                },
                'n' => {
                    view.find(.forward, view.top + 1);
                    view.follow = false;
                },
                'N' => {
                    if (view.top > 0) view.find(.backward, view.top - 1);
                    view.follow = false;
                },
                else => {},
            },
            else => {},
        }
        if (by != 0) {
            const moved = @as(isize, @intCast(view.top)) + by;
            view.top = @intCast(@max(0, moved));
            if (by < 0) view.follow = false;
        }
        return true;
    }

    /// A key while typing a search: Enter searches, Esc doesn't.
    fn typeSearch(view: *LogView, pressed: tui.KeyEvent) !void {
        var prompt = &view.prompt.?;
        switch (pressed.code) {
            .esc => view.prompt = null,
            .enter => {
                const text = std.mem.trim(u8, prompt.value(), " ");
                if (text.len > 0) view.search = try view.arena.allocator().dupe(u8, text);
                view.prompt = null;
                view.find(.forward, view.top);
            },
            .backspace => prompt.deleteBackward(),
            .delete => prompt.deleteForward(),
            .left => prompt.moveCursorLeft(),
            .right => prompt.moveCursorRight(),
            .home => prompt.moveCursorHome(),
            .end => prompt.moveCursorEnd(),
            .char => |c| prompt.insertCodepoint(c),
            else => {},
        }
    }

    fn find(view: *LogView, direction: enum { forward, backward }, start: usize) void {
        const needle = view.search orelse return;
        const lines = view.lines.items;
        switch (direction) {
            .forward => for (start..lines.len) |i| {
                if (std.ascii.findIgnoreCase(lines[i], needle) != null) return view.found(i);
            },
            .backward => {
                var i = @min(start + 1, lines.len);
                while (i > 0) {
                    i -= 1;
                    if (std.ascii.findIgnoreCase(lines[i], needle) != null) return view.found(i);
                }
            },
        }
        view.message = std.fmt.allocPrint(view.arena.allocator(), "not found: {s}", .{needle}) catch "not found";
    }

    fn found(view: *LogView, line: usize) void {
        view.top = line;
        view.message = "";
    }

    /// Read what the log has gained since the last look.
    fn load(view: *LogView) !void {
        const arena = view.arena.allocator();
        const path = view.path orelse {
            view.message = "this run has no log: adopted runs were never built, so nothing ran";
            return;
        };
        const file = std.Io.Dir.cwd().openFile(view.io, path, .{}) catch {
            view.message = try std.fmt.allocPrint(arena, "can't read {s}", .{path});
            return;
        };
        defer file.close(view.io);
        const length = try file.length(view.io);
        if (length < view.read_to) {
            // Replaced or truncated: start over
            view.lines = .empty;
            view.read_to = 0;
            view.partial = "";
        }
        if (length == view.read_to) return;

        const data = try view.gpa.alloc(u8, length - view.read_to);
        defer view.gpa.free(data);
        const read = try file.readPositionalAll(view.io, data, view.read_to);
        view.read_to += read;

        const text = try std.mem.concat(arena, u8, &.{ view.partial, data[0..read] });
        var pieces = std.mem.splitScalar(u8, text, '\n');
        var piece = pieces.next().?;
        while (pieces.next()) |next| : (piece = next) {
            try view.lines.append(arena, try shown(arena, piece));
        }
        view.partial = piece;
    }

    fn draw(view: *LogView, buf: *Buffer) !void {
        const arena = view.arena.allocator();
        const area = buf.getArea();

        var header: Line = .{ .arena = arena };
        try header.add(bold, " {s}  ", .{view.title});
        try header.add(plain, "{s}  {d} lines", .{ view.path orelse "", view.lines.items.len });
        if (view.follow) try header.add(bold, "  following", .{});
        header.draw(buf, area, area.y, reversed);

        const end = @min(view.lines.items.len, view.top + view.page);
        var items: std.ArrayList(tui.ListItem) = .empty;
        for (view.lines.items[view.top..end]) |text| {
            var style = plain;
            if (std.mem.startsWith(u8, text, "===")) {
                style = cyan_bold;
            } else if (std.ascii.findIgnoreCase(text, "error") != null or
                std.ascii.findIgnoreCase(text, "traceback") != null or
                std.mem.indexOf(u8, text, " crashed:") != null)
            {
                style = red;
            } else if (view.search) |needle| if (std.ascii.findIgnoreCase(text, needle) != null) {
                style = yellow_bold;
            };
            try items.append(arena, .{ .content = skipColumns(text, view.left), .style = style });
        }
        const list: tui.List = .{ .items = items.items, .highlight_symbol = "" };
        list.render(.{ .x = area.x, .y = area.y + 1, .width = area.width, .height = area.height -| 2 }, buf);

        const bottom: Rect = .{ .x = area.x, .y = area.y + area.height -| 1, .width = area.width, .height = 1 };
        if (view.prompt) |*prompt| {
            buf.setString(bottom.x, bottom.y, "/", plain);
            prompt.render(.{ .x = bottom.x + 1, .y = bottom.y, .width = bottom.width -| 1, .height = 1 }, buf);
            return;
        }
        var footer: Line = .{ .arena = arena };
        if (view.message.len > 0) {
            try footer.add(dim, "{s}", .{view.message});
        } else {
            const first = if (view.lines.items.len > 0) view.top + 1 else 0;
            try footer.add(dim, " {d}-{d} of {d}   up/down PgUp/PgDn g/G left/right  f follow  / n N search  q back", .{ first, end, view.lines.items.len });
        }
        footer.draw(buf, area, bottom.y, plain);
    }
};

/// A log line as it's shown: one rewritten with \r (progress) shows what it said last, tabs
/// are spaces to the next multiple of 8.
fn shown(arena: Allocator, raw: []const u8) ![]const u8 {
    var line = std.mem.trimEnd(u8, raw, "\r");
    if (std.mem.lastIndexOfScalar(u8, line, '\r')) |cr| line = line[cr + 1 ..];
    if (std.mem.indexOfScalar(u8, line, '\t') == null) return line;

    var out: std.ArrayList(u8) = .empty;
    var column: usize = 0;
    for (line) |c| {
        if (c == '\t') {
            const spaces = 8 - column % 8;
            try out.appendNTimes(arena, ' ', spaces);
            column += spaces;
        } else {
            try out.append(arena, c);
            // Continuation bytes of UTF-8 don't start a column
            if (c & 0xC0 != 0x80) column += 1;
        }
    }
    return out.items;
}

/// `text` without its first `columns` columns.
fn skipColumns(text: []const u8, columns: usize) []const u8 {
    var i: usize = 0;
    var skipped: usize = 0;
    while (i < text.len and skipped < columns) {
        const length = std.unicode.utf8ByteSequenceLength(text[i]) catch 1;
        const end = @min(i + length, text.len);
        const width = if (std.unicode.utf8Decode(text[i..end])) |cp| tui.codepointWidth(cp) else |_| 1;
        skipped += width;
        i = end;
    }
    return text[i..];
}

pub fn main(init: std.process.Init) !void {
    const io = init.io;
    const arena = init.arena.allocator();

    const args = try init.minimal.args.toSlice(arena);
    for (args[1..]) |arg| {
        if (std.mem.eql(u8, arg, "-h") or std.mem.eql(u8, arg, "--help")) {
            std.debug.print("{s}", .{usage});
            return;
        }
        hopper.system.fail("unknown argument {s}\n{s}", .{ arg, usage });
    }

    try hopper.time.useLocalZone(io, arena);
    const paths = try hopper.Paths.find(init);
    // Gathered once before the terminal is taken, so what stops it (settings) is readable
    const data = try Data.load(init, paths);

    var backend = try tui.backend.init(init.gpa, io);
    defer backend.deinit();
    var terminal = try tui.Terminal.init(init.gpa, backend.interface());
    defer terminal.deinit();
    try terminal.hideCursor();

    var top: Top = .{ .init = init, .paths = paths, .data = data, .frame = .init(init.gpa) };
    defer top.frame.deinit();
    defer top.data.deinit(init.gpa);
    try top.run(&terminal);
}
