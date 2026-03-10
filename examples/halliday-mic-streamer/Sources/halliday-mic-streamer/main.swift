@preconcurrency import AVFoundation
import Carbon.HIToolbox
import CoreGraphics
import Foundation

struct AudioConfig {
    let rate: Double
    let channels: AVAudioChannelCount
    let blockSize: AVAudioFrameCount
    let width: Int

    static let `default` = AudioConfig(rate: 16_000, channels: 1, blockSize: 1_024, width: 2)
}

struct CLIOptions {
    var host: String = "homeassistant.local"
    var port: Int = 10310
    var language: String = "en"
    var haToken: String? = ProcessInfo.processInfo.environment["HA_TOKEN"]

    static func parse(from args: [String]) throws -> CLIOptions {
        var options = CLIOptions()
        var index = 0

        while index < args.count {
            let arg = args[index]
            switch arg {
            case "--host":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--host") }
                options.host = args[index]
            case "--port":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--port") }
                guard let port = Int(args[index]) else { throw CLIError.invalidValue("--port") }
                options.port = port
            case "--language":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--language") }
                options.language = args[index]
            case "--ha-token":
                index += 1
                guard index < args.count else { throw CLIError.missingValue("--ha-token") }
                options.haToken = args[index]
            case "-h", "--help":
                printUsageAndExit()
            default:
                throw CLIError.unknownArgument(arg)
            }
            index += 1
        }

        return options
    }
}

enum CLIError: Error, CustomStringConvertible {
    case missingValue(String)
    case invalidValue(String)
    case unknownArgument(String)

    var description: String {
        switch self {
        case let .missingValue(flag):
            return "Missing value for \(flag)"
        case let .invalidValue(flag):
            return "Invalid value for \(flag)"
        case let .unknownArgument(arg):
            return "Unknown argument: \(arg)"
        }
    }
}

enum AppError: Error, CustomStringConvertible {
    case microphoneAccessDenied
    case failedToCreateAudioFormat
    case failedToCreateConverter
    case failedToStartAudioEngine(Error)
    case failedToCreateEventTap
    case streamConnectionFailed
    case malformedHeader
    case invalidJSON
    case emptyHost

    var description: String {
        switch self {
        case .microphoneAccessDenied:
            return "Microphone access denied"
        case .failedToCreateAudioFormat:
            return "Failed to create audio format"
        case .failedToCreateConverter:
            return "Failed to create audio converter"
        case let .failedToStartAudioEngine(error):
            return "Failed to start audio engine: \(error.localizedDescription)"
        case .failedToCreateEventTap:
            return "Failed to create keyboard event tap (enable Accessibility permissions for this app or terminal)"
        case .streamConnectionFailed:
            return "Failed to open socket streams"
        case .malformedHeader:
            return "Malformed Wyoming header"
        case .invalidJSON:
            return "Invalid JSON received from server"
        case .emptyHost:
            return "Host cannot be empty"
        }
    }
}

final class ConverterInputState: @unchecked Sendable {
    var supplied = false
}

final class PushToTalkRecorder {
    private let cfg: AudioConfig
    private let engine = AVAudioEngine()
    private let inputFormat: AVAudioFormat
    private let targetFormat: AVAudioFormat
    private let converter: AVAudioConverter
    private let stateQueue = DispatchQueue(label: "halliday.recorder.state")

    private var recording = false
    private var recordedBytes = 0
    private var chunkHandler: ((Data) -> Void)?

    init(cfg: AudioConfig) throws {
        self.cfg = cfg
        inputFormat = engine.inputNode.inputFormat(forBus: 0)

        guard let targetFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: cfg.rate,
            channels: cfg.channels,
            interleaved: true
        ) else {
            throw AppError.failedToCreateAudioFormat
        }
        self.targetFormat = targetFormat

        guard let converter = AVAudioConverter(from: inputFormat, to: targetFormat) else {
            throw AppError.failedToCreateConverter
        }
        self.converter = converter

        engine.inputNode.installTap(onBus: 0, bufferSize: cfg.blockSize, format: inputFormat) { [weak self] buffer, _ in
            self?.handleInput(buffer: buffer)
        }

        do {
            try engine.start()
        } catch {
            throw AppError.failedToStartAudioEngine(error)
        }
    }

    deinit {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    func start(chunkHandler: @escaping (Data) -> Void) {
        stateQueue.sync {
            recordedBytes = 0
            self.chunkHandler = chunkHandler
            recording = true
        }
    }

    func stop() -> Int {
        stateQueue.sync {
            recording = false
            chunkHandler = nil
            let durationMs = Int((Double(recordedBytes) / (Double(cfg.width) * Double(cfg.channels) * cfg.rate)) * 1000.0)
            recordedBytes = 0
            return durationMs
        }
    }

    private func handleInput(buffer: AVAudioPCMBuffer) {
        let callback = stateQueue.sync { recording ? chunkHandler : nil }
        guard let callback else { return }

        let ratio = targetFormat.sampleRate / max(1.0, inputFormat.sampleRate)
        let outputCapacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 64)
        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: targetFormat, frameCapacity: outputCapacity) else {
            return
        }

        let inputState = ConverterInputState()
        var conversionError: NSError?
        let status = converter.convert(to: outputBuffer, error: &conversionError) { _, outStatus in
            if inputState.supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            inputState.supplied = true
            outStatus.pointee = .haveData
            return buffer
        }

        if status == .error || conversionError != nil {
            return
        }

        guard outputBuffer.frameLength > 0,
              let channelData = outputBuffer.int16ChannelData else {
            return
        }

        let byteCount = Int(outputBuffer.frameLength) * Int(targetFormat.streamDescription.pointee.mBytesPerFrame)
        let data = Data(bytes: channelData.pointee, count: byteCount)

        stateQueue.sync {
            guard recording else { return }
            recordedBytes += data.count
        }
        callback(data)
    }
}

final class KeyboardMonitor {
    var onSpaceDown: (() -> Void)?
    var onSpaceUp: (() -> Void)?
    var onEscDown: (() -> Void)?

    private var eventTap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?

    func start() throws {
        let mask = (1 << CGEventType.keyDown.rawValue) | (1 << CGEventType.keyUp.rawValue)

        let callback: CGEventTapCallBack = { _, type, event, userInfo in
            guard let userInfo else {
                return Unmanaged.passUnretained(event)
            }

            let monitor = Unmanaged<KeyboardMonitor>.fromOpaque(userInfo).takeUnretainedValue()

            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                if let tap = monitor.eventTap {
                    CGEvent.tapEnable(tap: tap, enable: true)
                }
                return Unmanaged.passUnretained(event)
            }

            let keyCode = Int(event.getIntegerValueField(.keyboardEventKeycode))

            if type == .keyDown {
                if keyCode == kVK_Space {
                    monitor.onSpaceDown?()
                } else if keyCode == kVK_Escape {
                    monitor.onEscDown?()
                }
            } else if type == .keyUp, keyCode == kVK_Space {
                monitor.onSpaceUp?()
            }

            return Unmanaged.passUnretained(event)
        }

        guard let tap = CGEvent.tapCreate(
            tap: .cgSessionEventTap,
            place: .headInsertEventTap,
            options: .defaultTap,
            eventsOfInterest: CGEventMask(mask),
            callback: callback,
            userInfo: UnsafeMutableRawPointer(Unmanaged.passUnretained(self).toOpaque())
        ) else {
            throw AppError.failedToCreateEventTap
        }

        eventTap = tap
        runLoopSource = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
        if let runLoopSource {
            CFRunLoopAddSource(CFRunLoopGetMain(), runLoopSource, .commonModes)
        }
        CGEvent.tapEnable(tap: tap, enable: true)
    }

    func stop() {
        if let source = runLoopSource {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), source, .commonModes)
        }
        if let tap = eventTap {
            CGEvent.tapEnable(tap: tap, enable: false)
        }
        runLoopSource = nil
        eventTap = nil
    }
}

func eventBytes(type: String, data: [String: Any]? = nil, payload: Data = Data()) throws -> Data {
    var header: [String: Any] = ["type": type]
    if let data, !data.isEmpty {
        header["data"] = data
    }
    if !payload.isEmpty {
        header["payload_length"] = payload.count
    }

    let json = try JSONSerialization.data(withJSONObject: header)
    guard var line = String(data: json, encoding: .utf8)?.data(using: .utf8) else {
        throw AppError.invalidJSON
    }
    line.append(0x0A)

    var output = Data()
    output.append(line)
    output.append(payload)
    return output
}

func readLine(stream: InputStream) throws -> Data {
    var line = Data()
    var byte: UInt8 = 0

    while true {
        let count = stream.read(&byte, maxLength: 1)
        if count < 0 {
            throw stream.streamError ?? AppError.streamConnectionFailed
        }
        if count == 0 {
            if line.isEmpty {
                throw AppError.malformedHeader
            }
            break
        }

        line.append(byte)
        if byte == 0x0A {
            break
        }
    }

    return line
}

func readExact(stream: InputStream, byteCount: Int) throws -> Data {
    var out = Data(count: byteCount)
    var offset = 0

    try out.withUnsafeMutableBytes { rawBuffer in
        guard let base = rawBuffer.bindMemory(to: UInt8.self).baseAddress else {
            throw AppError.streamConnectionFailed
        }

        while offset < byteCount {
            let readCount = stream.read(base.advanced(by: offset), maxLength: byteCount - offset)
            if readCount < 0 {
                throw stream.streamError ?? AppError.streamConnectionFailed
            }
            if readCount == 0 {
                throw AppError.streamConnectionFailed
            }
            offset += readCount
        }
    }

    return out
}

func intValue(_ value: Any?) -> Int {
    if let int = value as? Int { return int }
    if let number = value as? NSNumber { return number.intValue }
    if let str = value as? String, let int = Int(str) { return int }
    return 0
}

func readEvent(stream: InputStream) throws -> ([String: Any], Data) {
    let lineData = try readLine(stream: stream)
    guard let lineText = String(data: lineData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines),
          let headerData = lineText.data(using: .utf8),
          var header = try JSONSerialization.jsonObject(with: headerData) as? [String: Any] else {
        throw AppError.invalidJSON
    }

    var data = header["data"] as? [String: Any] ?? [:]

    let dataLength = intValue(header["data_length"])
    if dataLength > 0 {
        let extra = try readExact(stream: stream, byteCount: dataLength)
        if let extraJSON = try JSONSerialization.jsonObject(with: extra) as? [String: Any] {
            for (key, value) in extraJSON {
                data[key] = value
            }
        }
    }

    let payloadLength = intValue(header["payload_length"])
    let payload = payloadLength > 0 ? try readExact(stream: stream, byteCount: payloadLength) : Data()

    header["data"] = data
    return (header, payload)
}

func writeAll(stream: OutputStream, data: Data) throws {
    try data.withUnsafeBytes { rawBuffer in
        guard let base = rawBuffer.bindMemory(to: UInt8.self).baseAddress else {
            throw AppError.streamConnectionFailed
        }

        var offset = 0
        while offset < data.count {
            let written = stream.write(base.advanced(by: offset), maxLength: data.count - offset)
            if written < 0 {
                throw stream.streamError ?? AppError.streamConnectionFailed
            }
            if written == 0 {
                throw AppError.streamConnectionFailed
            }
            offset += written
        }
    }
}

final class WyomingStreamingClient: @unchecked Sendable {
    var onPartialTranscript: ((String) -> Void)?
    var onFinalTranscript: ((String) -> Void)?
    var onError: ((Error) -> Void)?

    private let host: String
    private let port: Int
    private let language: String
    private let cfg: AudioConfig
    private let writeQueue = DispatchQueue(label: "halliday.wyoming.write")
    private let stateQueue = DispatchQueue(label: "halliday.wyoming.state")

    private var inputStream: InputStream?
    private var outputStream: OutputStream?
    private var closed = false

    init(host: String, port: Int, language: String, cfg: AudioConfig) {
        self.host = host
        self.port = port
        self.language = language
        self.cfg = cfg
    }

    func start() throws {
        guard !host.isEmpty else {
            throw AppError.emptyHost
        }

        var readStream: Unmanaged<CFReadStream>?
        var writeStream: Unmanaged<CFWriteStream>?
        CFStreamCreatePairWithSocketToHost(nil, host as CFString, UInt32(port), &readStream, &writeStream)

        guard let read = readStream?.takeRetainedValue(),
              let write = writeStream?.takeRetainedValue() else {
            throw AppError.streamConnectionFailed
        }

        let input = read as InputStream
        let output = write as OutputStream
        input.open()
        output.open()

        inputStream = input
        outputStream = output

        try send(eventType: "transcribe", data: ["language": language])
        try send(
            eventType: "audio-start",
            data: ["rate": Int(cfg.rate), "width": cfg.width, "channels": Int(cfg.channels)]
        )

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.readLoop()
        }
    }

    func sendAudioChunk(_ chunk: Data) {
        writeQueue.async { [weak self] in
            guard let self else { return }
            do {
                try self.send(
                    eventType: "audio-chunk",
                    data: ["rate": Int(self.cfg.rate), "width": self.cfg.width, "channels": Int(self.cfg.channels)],
                    payload: chunk
                )
            } catch {
                self.fail(error)
            }
        }
    }

    func finish() {
        writeQueue.async { [weak self] in
            guard let self else { return }
            do {
                try self.send(eventType: "audio-stop", data: [:])
            } catch {
                self.fail(error)
            }
        }
    }

    func cancel() {
        close()
    }

    private func send(eventType: String, data: [String: Any], payload: Data = Data()) throws {
        let isClosed = stateQueue.sync { closed }
        guard !isClosed, let outputStream else { return }
        try writeAll(stream: outputStream, data: try eventBytes(type: eventType, data: data, payload: payload))
    }

    private func readLoop() {
        do {
            guard let inputStream else {
                throw AppError.streamConnectionFailed
            }

            while true {
                let isClosed = stateQueue.sync { closed }
                if isClosed {
                    return
                }

                let (event, _) = try readEvent(stream: inputStream)
                let type = event["type"] as? String ?? ""
                let data = event["data"] as? [String: Any] ?? [:]
                let text = (data["text"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

                if type == "transcript-chunk", !text.isEmpty {
                    onPartialTranscript?(text)
                } else if type == "transcript" {
                    onFinalTranscript?(text)
                    close()
                    return
                } else if type == "error" {
                    let message = (data["message"] as? String ?? "Unknown Wyoming error").trimmingCharacters(in: .whitespacesAndNewlines)
                    throw NSError(domain: "HallidayMicStreamer", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
                }
            }
        } catch {
            fail(error)
        }
    }

    private func fail(_ error: Error) {
        let wasClosed = stateQueue.sync { () -> Bool in
            let value = closed
            closed = true
            return value
        }
        if !wasClosed {
            closeStreams()
            onError?(error)
        }
    }

    private func close() {
        let shouldClose = stateQueue.sync { () -> Bool in
            if closed {
                return false
            }
            closed = true
            return true
        }
        if shouldClose {
            closeStreams()
        }
    }

    private func closeStreams() {
        inputStream?.close()
        outputStream?.close()
        inputStream = nil
        outputStream = nil
    }
}

func requestMicrophonePermission() -> Bool {
    if AVCaptureDevice.authorizationStatus(for: .audio) == .authorized {
        return true
    }

    let semaphore = DispatchSemaphore(value: 0)
    final class PermissionState: @unchecked Sendable {
        var granted = false
    }
    let permissionState = PermissionState()
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        permissionState.granted = ok
        semaphore.signal()
    }
    semaphore.wait()
    return permissionState.granted
}

func printUsageAndExit() -> Never {
    print("Usage: halliday-mic-streamer [--host HOST] [--port PORT] [--language LANG] [--ha-token TOKEN]")
    print("  Default target: homeassistant.local:10310")
    print("  HA_TOKEN is optional and not used for direct Wyoming TCP streaming.")
    exit(0)
}

@main
struct HallidayMicStreamer {
    static func main() {
        do {
            let options = try CLIOptions.parse(from: Array(CommandLine.arguments.dropFirst()))

            guard requestMicrophonePermission() else {
                throw AppError.microphoneAccessDenied
            }

            let cfg = AudioConfig.default
            let recorder = try PushToTalkRecorder(cfg: cfg)
            let keyboard = KeyboardMonitor()
            let stateQueue = DispatchQueue(label: "halliday.app.state")

            var spaceDown = false
            var client: WyomingStreamingClient?

            print("Push-to-talk ready.")
            print("Hold SPACE to stream. Release SPACE to finish. Press ESC to quit.")
            print("Target: \(options.host):\(options.port)")
            if let token = options.haToken, !token.isEmpty {
                print("Home Assistant token detected but not used for direct Wyoming transport.")
            }

            keyboard.onSpaceDown = {
                stateQueue.sync {
                    guard !spaceDown, client == nil else { return }
                    spaceDown = true

                    let streamingClient = WyomingStreamingClient(
                        host: options.host,
                        port: options.port,
                        language: options.language,
                        cfg: cfg
                    )

                    streamingClient.onPartialTranscript = { text in
                        print("[partial] \(text)")
                    }

                    streamingClient.onFinalTranscript = { text in
                        print("[stt] \(text.isEmpty ? "(no text)" : text)")
                        stateQueue.sync {
                            client = nil
                        }
                    }

                    streamingClient.onError = { error in
                        print("[err] \(error.localizedDescription)")
                        stateQueue.sync {
                            client = nil
                            spaceDown = false
                        }
                    }

                    do {
                        try streamingClient.start()
                        client = streamingClient
                        print("\n[rec] START")
                        recorder.start { chunk in
                            streamingClient.sendAudioChunk(chunk)
                        }
                    } catch {
                        print("[err] \(error)")
                        client = nil
                        spaceDown = false
                    }
                }
            }

            keyboard.onSpaceUp = {
                stateQueue.sync {
                    guard spaceDown else { return }
                    spaceDown = false
                    let durationMs = recorder.stop()
                    print("[rec] STOP  (\(durationMs) ms)")
                    client?.finish()
                }
            }

            keyboard.onEscDown = {
                stateQueue.sync {
                    _ = recorder.stop()
                    client?.cancel()
                    client = nil
                    spaceDown = false
                }
                print("\nBye.")
                keyboard.stop()
                CFRunLoopStop(CFRunLoopGetMain())
            }

            try keyboard.start()
            CFRunLoopRun()
        } catch {
            fputs("[fatal] \(error)\n", stderr)
            exit(1)
        }
    }
}
