// Builds hopper's Zig programs into zig-out/bin. They find the checkout from there
// (zig-out/bin/.. /..), so build them inside it:
//
//     zig build                     the programs
//     zig build test                the tests
//     zig build run -- --json       build and run the overview

const std = @import("std");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // What the programs share: settings, the state files, the system (lib/hopper.zig)
    const hopper = b.addModule("hopper", .{
        .root_source_file = b.path("lib/hopper.zig"),
        .target = target,
        .optimize = optimize,
    });

    const overview = b.addExecutable(.{
        .name = "overview",
        .root_module = b.createModule(.{
            .root_source_file = b.path("daemon/overview.zig"),
            .target = target,
            .optimize = optimize,
            .imports = &.{.{ .name = "hopper", .module = hopper }},
        }),
    });
    b.installArtifact(overview);

    const run = b.addRunArtifact(overview);
    run.step.dependOn(b.getInstallStep());
    run.addPassthruArgs();
    b.step("run", "Run the overview").dependOn(&run.step);

    const tests = b.addRunArtifact(b.addTest(.{ .root_module = hopper }));
    b.step("test", "Run the tests").dependOn(&tests.step);
}
