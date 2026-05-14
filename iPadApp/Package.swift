// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DesktopAgent",
    platforms: [
        .iOS(.v17)
    ],
    targets: [
        .executableTarget(
            name: "DesktopAgent",
            path: "DesktopAgent",
            exclude: ["project.yml", "DesktopAgent.entitlements"],
            resources: [],
            linkerSettings: [
                .linkedFramework("ARKit"),
                .linkedFramework("CoreMotion"),
                .linkedFramework("Speech"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("PencilKit"),
            ]
        ),
        .testTarget(
            name: "DesktopAgentTests",
            dependencies: ["DesktopAgent"],
            path: "Tests"
        )
    ]
)
