const std = @import("std");

const Example = struct {
    /// Executable name, and the source file under `examples/`.
    name: []const u8,
    step: []const u8,
    description: []const u8,
};

const examples = [_]Example{
    .{ .name = "dashboard", .step = "run-dashboard", .description = "Run system monitor dashboard" },
    .{ .name = "kitty_graphics", .step = "run-kitty", .description = "Run Kitty graphics demo" },
    .{ .name = "themes_demo", .step = "run-themes", .description = "Run themes demo" },
    .{ .name = "mouse_demo", .step = "run-mouse", .description = "Run mouse demo" },
    .{ .name = "widgets_demo", .step = "run-widgets-demo", .description = "Run new widgets showcase" },
};

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // Module export for other projects
    const zigtui_module = b.addModule("zigtui", .{
        .root_source_file = b.path("src/lib.zig"),
    });

    // Tests
    const lib_tests = b.addTest(.{
        .root_module = b.createModule(.{
            .root_source_file = b.path("src/lib.zig"),
            .target = target,
            .optimize = optimize,
        }),
    });
    const run_lib_tests = b.addRunArtifact(lib_tests);
    const test_step = b.step("test", "Run library tests");
    test_step.dependOn(&run_lib_tests.step);

    // Examples: `zig build examples` builds them all, `zig build run-*` runs one
    const examples_step = b.step("examples", "Build example applications");

    for (examples) |example| {
        const exe = b.addExecutable(.{
            .name = example.name,
            .root_module = b.createModule(.{
                .root_source_file = b.path(b.fmt("examples/{s}.zig", .{example.name})),
                .target = target,
                .optimize = optimize,
                .imports = &.{
                    .{ .name = "zigtui", .module = zigtui_module },
                },
            }),
        });
        const install = b.addInstallArtifact(exe, .{});
        examples_step.dependOn(&install.step);

        const run = b.addRunArtifact(exe);
        run.step.dependOn(&install.step);
        b.step(example.step, example.description).dependOn(&run.step);
    }
}
