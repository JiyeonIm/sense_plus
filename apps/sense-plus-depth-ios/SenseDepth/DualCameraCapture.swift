import AVFoundation
import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ImageIO

final class DualCameraCapture: NSObject, DepthCapture {
    private struct Format {
        let video: AVCaptureDevice.Format
        let depth: AVCaptureDevice.Format
        let framesPerSecond: Int32

        var pixels: Int {
            let size = CMVideoFormatDescriptionGetDimensions(video.formatDescription)
            return Int(size.width) * Int(size.height)
        }
    }

    private enum SetupError: Error {
        case connectionUnavailable
    }

    private let queue = DispatchQueue(label: "org.senseplus.depth.dual-camera", qos: .userInitiated)
    private let videoOutput = AVCaptureVideoDataOutput()
    private let depthOutput = AVCaptureDepthDataOutput()
    private let onFrame: (DepthFrame) -> Void
    private let onFailure: (String) -> Void
    private var session: AVCaptureSession?
    private var synchronizer: AVCaptureDataOutputSynchronizer?
    private var observers: [NSObjectProtocol] = []
    private var watchdog: DispatchSourceTimer?
    private var lastPairTime = 0.0
    private var lastFrameTime = -Double.infinity

    static var isSupported: Bool {
        guard let device = AVCaptureDevice.default(.builtInDualWideCamera, for: .video, position: .back) else {
            return false
        }
        return captureFormat(for: device) != nil
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

    private static func captureFormat(for device: AVCaptureDevice) -> Format? {
        let candidates = device.formats.compactMap { video -> Format? in
            if #available(iOS 17.2, *), video.supportedVideoZoomRangesForDepthDataDelivery.isEmpty {
                return nil
            }
            guard let depth = video.supportedDepthDataFormats.first(where: {
                let pixelType = CMFormatDescriptionGetMediaSubType($0.formatDescription)
                return [kCVPixelFormatType_DepthFloat32, kCVPixelFormatType_DepthFloat16,
                        kCVPixelFormatType_DisparityFloat32, kCVPixelFormatType_DisparityFloat16].contains(pixelType)
            }), let rate = ([30, 25, 24, 20, 15] as [Int32]).first(where: { rate in
                video.videoSupportedFrameRateRanges.contains {
                    $0.minFrameRate <= Double(rate) && Double(rate) <= $0.maxFrameRate
                }
            }) else { return nil }
            return Format(video: video, depth: depth, framesPerSecond: rate)
        }
        return candidates.min { first, second in
            // A modest video size keeps the continuously running stereo depth pipeline responsive.
            let targetPixels = 1280 * 720
            let firstCost = abs(first.pixels - targetPixels)
            let secondCost = abs(second.pixels - targetPixels)
            if firstCost != secondCost { return firstCost < secondCost }
            return first.framesPerSecond > second.framesPerSecond
        }
    }

    private func startOnQueue() {
        guard session == nil else { return }
        guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
            onFailure("카메라 접근 권한이 필요해요.")
            return
        }
        guard let device = AVCaptureDevice.default(.builtInDualWideCamera, for: .video, position: .back),
              let format = Self.captureFormat(for: device) else {
            onFailure("이 아이폰에서 후면 듀얼 카메라의 깊이 영상을 받을 수 없어요.")
            return
        }

        let captureSession = AVCaptureSession()
        do {
            try configure(captureSession, device: device, format: format)
        } catch {
            onFailure("후면 듀얼 카메라의 깊이 영상을 시작할 수 없어요.")
            return
        }
        session = captureSession
        let sync = AVCaptureDataOutputSynchronizer(dataOutputs: [depthOutput, videoOutput])
        synchronizer = sync
        sync.setDelegate(self, queue: queue)
        observe(captureSession)
        captureSession.startRunning()
        guard captureSession.isRunning, !captureSession.isInterrupted else {
            fail("듀얼 카메라가 시작되지 않았어요. 다시 시도해 주세요.")
            return
        }

        lastFrameTime = -.infinity
        lastPairTime = ProcessInfo.processInfo.systemUptime
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + .seconds(1), repeating: .seconds(1))
        timer.setEventHandler { [weak self] in
            guard let self, ProcessInfo.processInfo.systemUptime - self.lastPairTime >= 3 else { return }
            self.fail("카메라 영상과 깊이 데이터를 함께 받을 수 없어요.")
        }
        watchdog = timer
        timer.resume()
    }

    private func configure(_ captureSession: AVCaptureSession, device: AVCaptureDevice, format: Format) throws {
        captureSession.beginConfiguration()
        defer { captureSession.commitConfiguration() }
        let input = try AVCaptureDeviceInput(device: device)
        guard captureSession.canAddInput(input) else { throw SetupError.connectionUnavailable }
        captureSession.addInput(input)
        guard captureSession.canSetSessionPreset(.inputPriority) else { throw SetupError.connectionUnavailable }
        captureSession.sessionPreset = .inputPriority

        do {
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            device.activeFormat = format.video
            device.activeDepthDataFormat = format.depth
            let frameDuration = CMTime(value: 1, timescale: format.framesPerSecond)
            device.activeVideoMinFrameDuration = frameDuration
            device.activeVideoMaxFrameDuration = frameDuration
        }

        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [:]
        depthOutput.isFilteringEnabled = false
        guard captureSession.canAddOutput(depthOutput) else { throw SetupError.connectionUnavailable }
        captureSession.addOutput(depthOutput)
        guard captureSession.canAddOutput(videoOutput) else { throw SetupError.connectionUnavailable }
        captureSession.addOutput(videoOutput)

        if #available(iOS 17.2, *) {
            let depthZoomRanges = format.video.supportedVideoZoomRangesForDepthDataDelivery
            try device.lockForConfiguration()
            defer { device.unlockForConfiguration() }
            if !depthZoomRanges.contains(where: { $0.contains(device.videoZoomFactor) }) {
                let zoom = depthZoomRanges.compactMap { range -> CGFloat? in
                    let minimum = max(range.lowerBound, device.minAvailableVideoZoomFactor)
                    let maximum = min(range.upperBound, device.maxAvailableVideoZoomFactor)
                    return minimum <= maximum ? minimum : nil
                }.min()
                guard let zoom else { throw SetupError.connectionUnavailable }
                device.videoZoomFactor = zoom
            }
        }

        guard let videoConnection = videoOutput.connection(with: .video),
              depthOutput.connection(with: .depthData) != nil else { throw SetupError.connectionUnavailable }
        // Depth and video stay in the rear sensor's landscape coordinates. The preview rotates the RGB buffer.
        if #available(iOS 17.0, *) {
            if videoConnection.isVideoRotationAngleSupported(0) {
                videoConnection.videoRotationAngle = 0
            }
        } else if videoConnection.isVideoOrientationSupported {
            videoConnection.videoOrientation = .landscapeRight
        }
        if videoConnection.isVideoMirroringSupported {
            videoConnection.automaticallyAdjustsVideoMirroring = false
            videoConnection.isVideoMirrored = false
        }
    }

    private func observe(_ captureSession: AVCaptureSession) {
        let notifications: [(Notification.Name, String)] = [
            (AVCaptureSession.wasInterruptedNotification, "다른 앱이나 시스템이 카메라 사용을 중단했어요."),
            (AVCaptureSession.runtimeErrorNotification, "듀얼 카메라에서 오류가 발생했어요."),
            (AVCaptureSession.didStopRunningNotification, "카메라 영상 수신이 멈췄어요.")
        ]
        observers = notifications.map { name, message in
            NotificationCenter.default.addObserver(forName: name, object: captureSession, queue: nil) {
                [weak self, weak captureSession] _ in
                self?.queue.async { [weak self, weak captureSession] in
                    guard let self, let captureSession, self.session === captureSession else { return }
                    self.fail(message)
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
        for observer in observers {
            NotificationCenter.default.removeObserver(observer)
        }
        observers.removeAll()
        guard let session else { return }
        self.session = nil
        if session.isRunning { session.stopRunning() }
        // Release output ownership so a later start can use the same instance.
        session.removeOutput(videoOutput)
        session.removeOutput(depthOutput)
    }
}

extension DualCameraCapture: AVCaptureDataOutputSynchronizerDelegate {
    func dataOutputSynchronizer(
        _ synchronizer: AVCaptureDataOutputSynchronizer,
        didOutput collection: AVCaptureSynchronizedDataCollection
    ) {
        guard synchronizer === self.synchronizer, let session, session.isRunning, !session.isInterrupted,
              let video = collection.synchronizedData(for: videoOutput) as? AVCaptureSynchronizedSampleBufferData,
              let depth = collection.synchronizedData(for: depthOutput) as? AVCaptureSynchronizedDepthData,
              !video.sampleBufferWasDropped, !depth.depthDataWasDropped,
              let image = CMSampleBufferGetImageBuffer(video.sampleBuffer) else { return }

        let data = depth.depthData
        let now = ProcessInfo.processInfo.systemUptime
        let usesFloatDepth = data.depthDataType == kCVPixelFormatType_DepthFloat32
        guard usesFloatDepth || data.availableDepthDataTypes.contains(kCVPixelFormatType_DepthFloat32) else {
            queue.async { [weak self, weak synchronizer] in
                guard let self, let synchronizer, synchronizer === self.synchronizer else { return }
                self.fail("이 듀얼 카메라의 깊이 데이터 형식을 읽을 수 없어요.")
            }
            return
        }
        lastPairTime = now
        guard now - lastFrameTime >= 1.0 / 7 else { return }
        lastFrameTime = now

        let map = usesFloatDepth
            ? data.depthDataMap
            : data.converting(toDepthDataType: kCVPixelFormatType_DepthFloat32).depthDataMap
        let isMetric = data.depthDataAccuracy == .absolute && data.depthDataQuality == .high
        let result = DepthEstimator.map(map, source: .dualCamera, absolute: isMetric)
        onFrame(DepthFrame(image: image, orientation: .right, result: result))
    }
}
