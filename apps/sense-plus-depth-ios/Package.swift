// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SenseDepthCore",
    targets: [
        .target(name: "DepthCore", path: "DepthCore"),
        .testTarget(name: "DepthCoreTests", dependencies: ["DepthCore"])
    ]
)
