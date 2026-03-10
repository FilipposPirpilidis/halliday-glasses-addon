// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "halliday-mic-streamer",
    platforms: [
        .macOS(.v13)
    ],
    targets: [
        .executableTarget(
            name: "halliday-mic-streamer"
        )
    ]
)
