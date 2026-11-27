import AVFoundation
import CoreMedia
import CoreVideo
import Foundation
import ImageIO
import simd

final class StereoCameraCapture: NSObject, DepthCapture {
    private struct Cameras {
        let main: AVCaptureDevice
        let wide: AVCaptureDevice
        let wideFromMain: simd_float4x3
    }

    private struct CaptureFormats {
        let main: AVCaptureDevice.Format
        let wide: AVCaptureDevice.Format
        let framesPerSecond: Int32
    }

    private struct CaptureGraph {
        let session: AVCaptureMultiCamSession
        let main: AVCaptureVideoDataOutput
        let wide: AVCaptureVideoDataOutput
    }

    private enum SetupError: Error {
        case captureConfiguration
        case calibrationUnavailable
        case capacityExceeded
    }

    private static let maximumPairSkew = 0.010
    private static let frameInterval = 0.18

    private let queue = DispatchQueue(label: "org.senseplus.depth.stereo", qos: .userInitiated)
    private let onFrame: (DepthFrame) -> Void
    private let onFailure: (String) -> Void
    private var session: AVCaptureMultiCamSession?
    private var synchronizer: AVCaptureDataOutputSynchronizer?
    private var mainOutput: AVCaptureVideoDataOutput?
    private var wideOutput: AVCaptureVideoDataOutput?
    private var wideFromMain: simd_float4x3?
    private var observers: [NSObjectProtocol] = []
    private var watchdog: DispatchSourceTimer?
    private var lastCalibratedPairAt = 0.0
    private var lastFrameAt = -Double.infinity

    static var isSupported: Bool {
        guard let cameras = discover() else { return false }
        return !formats(for: cameras).isEmpty
    }

    init(onFrame: @escaping (DepthFrame) -> Void, onFailure: @escaping (String) -> Void) {
        self.onFrame = onFrame
        self.onFailure = onFailure
        super.init()
    }

    func start() {
        queue.async { [weak self] in self?.startOnQueue() }
    }

    func stop(onStopped: @escaping () -> Void) {
        queue.async { [self] in
            stopOnQueue()
            onStopped()
        }
    }

    private static func discover() -> Cameras? {
        guard AVCaptureMultiCamSession.isMultiCamSupported else { return nil }
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.builtInWideAngleCamera, .builtInUltraWideCamera],
            mediaType: .video,
            position: .back
        )
        for set in discovery.supportedMultiCamDeviceSets {
            guard let main = set.first(where: { $0.deviceType == .builtInWideAngleCamera }),
                  let wide = set.first(where: { $0.deviceType == .builtInUltraWideCamera }),
                  wide.isGeometricDistortionCorrectionSupported,
                  let calibration = factoryCalibration(main: main, wide: wide) else { continue }
            return Cameras(main: main, wide: wide, wideFromMain: calibration)
        }
        return nil
    }

    private static func factoryCalibration(main: AVCaptureDevice, wide: AVCaptureDevice) -> simd_float4x3? {
        guard let data = AVCaptureDevice.extrinsicMatrix(from: main, to: wide),
              data.count == MemoryLayout<simd_float4x3>.size else { return nil }
        let matrix = data.withUnsafeBytes { $0.loadUnaligned(as: simd_float4x3.self) }
        let rotation = simd_float3x3(columns: (matrix.columns.0, matrix.columns.1, matrix.columns.2))
        let translation = matrix.columns.3 / 1_000
        let baseline = simd_length(translation)
        guard (0.005...0.10).contains(baseline),
              abs(simd_determinant(rotation) - 1) <= 0.02,
              abs(simd_length_squared(matrix.columns.0) - 1) <= 0.02,
              abs(simd_length_squared(matrix.columns.1) - 1) <= 0.02,
              abs(simd_length_squared(matrix.columns.2) - 1) <= 0.02 else { return nil }
        return simd_float4x3(columns: (matrix.columns.0, matrix.columns.1, matrix.columns.2, translation))
    }

    private static func formats(for cameras: Cameras) -> [CaptureFormats] {
        [1280 * 720, 640 * 480].flatMap { targetPixels in
            ([30, 24, 20, 15] as [Int32]).compactMap { rate in
                guard let mainFormat = bestFormat(cameras.main, fps: rate, targetPixels: targetPixels),
                      let wideFormat = bestFormat(cameras.wide, fps: rate, targetPixels: targetPixels)
                else { return nil }
                return CaptureFormats(main: mainFormat, wide: wideFormat, framesPerSecond: rate)
            }
        }
    }

    private static func bestFormat(
        _ device: AVCaptureDevice, fps: Int32, targetPixels: Int
    ) -> AVCaptureDevice.Format? {
        let formats = device.formats.filter { format in
            let size = CMVideoFormatDescriptionGetDimensions(format.formatDescription)
            return format.isMultiCamSupported && (640...1920).contains(size.width)
                && (360...1440).contains(size.height) && Int(size.width) * Int(size.height) <= 1920 * 1080
                && format.videoSupportedFrameRateRanges.contains {
                    $0.minFrameRate <= Double(fps) && Double(fps) <= $0.maxFrameRate
                }
        }
        return formats.min { first, second in
            let a = CMVideoFormatDescriptionGetDimensions(first.formatDescription)
            let b = CMVideoFormatDescriptionGetDimensions(second.formatDescription)
            return abs(Int(a.width) * Int(a.height) - targetPixels)
                < abs(Int(b.width) * Int(b.height) - targetPixels)
        }
    }

    private func startOnQueue() {
        guard session == nil else { return }
        guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
            onFailure("카메라 접근 권한이 필요해요.")
            return
        }
        guard let cameras = Self.discover() else {
            onFailure("후면 두 카메라의 동시 촬영이나 공장 보정을 사용할 수 없어 거리 추정을 시작할 수 없어요.")
            return
        }
        let graph: CaptureGraph
        do {
            graph = try makeGraph(cameras)
        } catch SetupError.calibrationUnavailable {
            onFailure("두 카메라의 현재 영상 포맷에서 보정 정보를 받을 수 없어요.")
            return
        } catch SetupError.capacityExceeded {
            onFailure("현재 후면 카메라 두 대를 동시에 실행할 수 없어요.")
            return
        } catch {
            onFailure("후면 카메라 두 대를 시작할 수 없어요.")
            return
        }
        let captureSession = graph.session
        session = captureSession
        mainOutput = graph.main
        wideOutput = graph.wide
        wideFromMain = cameras.wideFromMain
        let sync = AVCaptureDataOutputSynchronizer(dataOutputs: [graph.main, graph.wide])
        synchronizer = sync
        sync.setDelegate(self, queue: queue)
        observe(captureSession)
        captureSession.startRunning()
        guard captureSession.isRunning, !captureSession.isInterrupted else {
            fail("후면 카메라 두 대가 시작되지 않았어요.")
            return
        }

        lastFrameAt = -.infinity
        lastCalibratedPairAt = ProcessInfo.processInfo.systemUptime
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .seconds(1), repeating: .seconds(1))
        timer.setEventHandler { [weak self] in
            guard let self, ProcessInfo.processInfo.systemUptime - self.lastCalibratedPairAt >= 3 else { return }
            self.fail("두 카메라에서 시간과 보정이 맞는 영상을 받을 수 없어요.")
        }
        watchdog = timer
        timer.resume()
    }

    private func makeGraph(_ cameras: Cameras) throws -> CaptureGraph {
        var failure: any Error = SetupError.captureConfiguration
        for formats in Self.formats(for: cameras) {
            let session = AVCaptureMultiCamSession()
            let main = AVCaptureVideoDataOutput()
            let wide = AVCaptureVideoDataOutput()
            do {
                try configure(session, cameras: cameras, formats: formats, main: main, wide: wide)
                guard session.hardwareCost <= 1, session.systemPressureCost <= 1 else {
                    throw SetupError.capacityExceeded
                }
                return CaptureGraph(session: session, main: main, wide: wide)
            } catch {
                failure = error
            }
        }
        throw failure
    }

    private func configure(
        _ session: AVCaptureMultiCamSession, cameras: Cameras, formats: CaptureFormats,
        main: AVCaptureVideoDataOutput, wide: AVCaptureVideoDataOutput
    ) throws {
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        let duration = CMTime(value: 1, timescale: formats.framesPerSecond)
        try addCamera(cameras.main, format: formats.main, duration: duration, output: main, to: session)
        try addCamera(cameras.wide, format: formats.wide, duration: duration, output: wide, to: session)
    }

    private func addCamera(
        _ device: AVCaptureDevice, format: AVCaptureDevice.Format, duration: CMTime,
        output: AVCaptureVideoDataOutput, to session: AVCaptureMultiCamSession
    ) throws {
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { throw SetupError.captureConfiguration }
        session.addInputWithNoConnections(input)
        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            device.activeFormat = format
            device.activeVideoMinFrameDuration = duration
            device.activeVideoMaxFrameDuration = duration
            if device.isGeometricDistortionCorrectionSupported {
                device.isGeometricDistortionCorrectionEnabled = true
            }
            if device.deviceType == .builtInUltraWideCamera, !device.isGeometricDistortionCorrectionEnabled {
                throw SetupError.calibrationUnavailable
            }
        }
        input.videoMinFrameDurationOverride = duration

        guard session.canAddOutput(output), let port = input.ports.first(where: { $0.mediaType == .video }) else {
            throw SetupError.captureConfiguration
        }
        output.alwaysDiscardsLateVideoFrames = true
        output.automaticallyConfiguresOutputBufferDimensions = false
        output.deliversPreviewSizedOutputBuffers = false
        session.addOutputWithNoConnections(output)
        let connection = AVCaptureConnection(inputPorts: [port], output: output)
        guard session.canAddConnection(connection) else { throw SetupError.captureConfiguration }
        session.addConnection(connection)
        if connection.isVideoStabilizationSupported {
            connection.preferredVideoStabilizationMode = .off
        }
        if connection.isVideoRotationAngleSupported(0) {
            connection.videoRotationAngle = 0
        }
        guard connection.videoRotationAngle == 0 else { throw SetupError.calibrationUnavailable }
        if connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = false
        }
        let lumaFormats = [kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
                           kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange]
        guard let pixelFormat = lumaFormats.first(where: { output.availableVideoPixelFormatTypes.contains($0) }) else {
            throw SetupError.captureConfiguration
        }
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: pixelFormat]
        guard connection.isCameraIntrinsicMatrixDeliverySupported else { throw SetupError.calibrationUnavailable }
        connection.isCameraIntrinsicMatrixDeliveryEnabled = true
    }

    private func observe(_ captureSession: AVCaptureMultiCamSession) {
        let names = [AVCaptureSession.wasInterruptedNotification, AVCaptureSession.runtimeErrorNotification,
                     AVCaptureSession.didStopRunningNotification]
        observers = names.map { name in
            NotificationCenter.default.addObserver(forName: name, object: captureSession, queue: nil) {
                [weak self, weak captureSession] _ in
                self?.queue.async { [weak self, weak captureSession] in
                    guard let self, let captureSession, self.session === captureSession else { return }
                    self.fail("두 카메라의 영상 수신이 중단됐어요. 다시 시도해 주세요.")
                }
            }
        }
    }

    private func fail(_ message: String) {
        stopOnQueue()
        onFailure(message)
    }

    private func stopOnQueue() {
        watchdog?.cancel()
        watchdog = nil
        synchronizer?.setDelegate(nil, queue: nil)
        synchronizer = nil
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        let stoppedSession = session
        session = nil
        mainOutput = nil
        wideOutput = nil
        wideFromMain = nil
        if let stoppedSession, stoppedSession.isRunning { stoppedSession.stopRunning() }
    }

    private static func intrinsics(_ sample: CMSampleBuffer, image: CVPixelBuffer) -> simd_float3x3? {
        guard let attachment = CMGetAttachment(
            sample, key: kCMSampleBufferAttachmentKey_CameraIntrinsicMatrix, attachmentModeOut: nil
        ) as? Data, attachment.count == MemoryLayout<simd_float3x3>.size,
              let description = CMSampleBufferGetFormatDescription(sample) else { return nil }
        let source = CMVideoFormatDescriptionGetDimensions(description)
        let width = Float(CVPixelBufferGetWidth(image))
        let height = Float(CVPixelBufferGetHeight(image))
        guard source.width > 0, source.height > 0, width > 0, height > 0 else { return nil }
        let scaleX = width / Float(source.width)
        let scaleY = height / Float(source.height)
        // Only a uniform resize can reuse the encoded-image calibration without a crop/warp model.
        guard abs(scaleX / scaleY - 1) < 0.01 else { return nil }
        let matrix = attachment.withUnsafeBytes { $0.loadUnaligned(as: simd_float3x3.self) }
        let (first, second, third) = matrix.columns
        let fx = first.x * scaleX
        let fy = second.y * scaleY
        let cx = third.x * scaleX
        let cy = third.y * scaleY
        guard (0.20...3).contains(fx / width), (0.20...4).contains(fy / height),
              (0.35...0.65).contains(cx / width), (0.35...0.65).contains(cy / height),
              abs(first.y) < 0.01, abs(first.z) < 0.01, abs(second.x) < 0.01,
              abs(second.z) < 0.01, abs(third.z - 1) < 0.01 else { return nil }
        return simd_float3x3(columns: (SIMD3(fx, 0, 0), SIMD3(0, fy, 0), SIMD3(cx, cy, 1)))
    }
}

extension StereoCameraCapture: AVCaptureDataOutputSynchronizerDelegate {
    func dataOutputSynchronizer(
        _ synchronizer: AVCaptureDataOutputSynchronizer,
        didOutput collection: AVCaptureSynchronizedDataCollection
    ) {
        guard synchronizer === self.synchronizer, let session, session.isRunning, !session.isInterrupted,
              let mainOutput, let wideOutput, let wideFromMain,
              let main = collection.synchronizedData(for: mainOutput) as? AVCaptureSynchronizedSampleBufferData,
              !main.sampleBufferWasDropped,
              let mainImage = CMSampleBufferGetImageBuffer(main.sampleBuffer) else { return }

        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastFrameAt >= Self.frameInterval else { return }
        lastFrameAt = now

        let result: DepthResult
        if let wide = collection.synchronizedData(for: wideOutput) as? AVCaptureSynchronizedSampleBufferData,
           !wide.sampleBufferWasDropped,
           let wideImage = CMSampleBufferGetImageBuffer(wide.sampleBuffer),
           mainOutput.connection(with: .video)?.activeVideoStabilizationMode == .off,
           wideOutput.connection(with: .video)?.activeVideoStabilizationMode == .off,
           abs(CMTimeSubtract(
               CMSampleBufferGetPresentationTimeStamp(main.sampleBuffer),
               CMSampleBufferGetPresentationTimeStamp(wide.sampleBuffer)
           ).seconds) <= Self.maximumPairSkew,
           let mainK = Self.intrinsics(main.sampleBuffer, image: mainImage),
           let wideK = Self.intrinsics(wide.sampleBuffer, image: wideImage) {
            lastCalibratedPairAt = now
            result = StereoFrameSolver.measure(
                main: mainImage, wide: wideImage, mainIntrinsics: mainK, wideIntrinsics: wideK, wideFromMain: wideFromMain
            )
        } else {
            result = .unavailable("두 카메라의 시간과 보정을 확인하고 있어요.")
        }
        onFrame(DepthFrame(image: mainImage, orientation: .right, result: result, receivedAt: now))
    }
}
