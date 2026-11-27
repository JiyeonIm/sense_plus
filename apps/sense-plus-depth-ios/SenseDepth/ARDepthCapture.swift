import ARKit
import CoreGraphics
import CoreVideo
import Foundation
import ImageIO
import simd

final class ARDepthCapture: NSObject, DepthCapture, ARSessionDelegate {
    static var supportsLiDAR: Bool {
        ARWorldTrackingConfiguration.isSupported
            && ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }

    static var supportsSurface: Bool {
        ARWorldTrackingConfiguration.isSupported
    }

    private static let frameInterval: TimeInterval = 0.15
    private static let maximumSurfaceGap: TimeInterval = 0.45
    private static let surfaceSamples = 4

    private let source: DepthSource
    private let onFrame: (DepthFrame) -> Void
    private let onFailure: (String) -> Void
    private let session = ARSession()
    private let queue = DispatchQueue(label: "sense.depth.ar", qos: .userInitiated)

    // The session delegate and all mutations are on the same serial queue.
    private var configuration: ARWorldTrackingConfiguration?
    private var running = false
    private var interrupted = false
    private var lastFrameTime: TimeInterval?
    private var surfaceAnchorID: UUID?
    private var surfaceTime: TimeInterval?
    private var surfaceDistances: [Float] = []

    init(source: DepthSource, onFrame: @escaping (DepthFrame) -> Void, onFailure: @escaping (String) -> Void) {
        self.source = source
        self.onFrame = onFrame
        self.onFailure = onFailure
        super.init()
        session.delegateQueue = queue
        session.delegate = self
    }

    func start() {
        queue.async { [weak self] in
            self?.startSession()
        }
    }

    func stop(onStopped: @escaping () -> Void) {
        queue.async { [self] in
            stopSession()
            onStopped()
        }
    }

    private func startSession() {
        guard !running else { return }
        guard Self.supportsSurface else {
            onFailure("이 기기에서는 AR 카메라 추적을 사용할 수 없습니다.")
            return
        }

        let config = ARWorldTrackingConfiguration()
        switch source {
        case .lidar:
            guard Self.supportsLiDAR else {
                onFailure("이 기기에서는 LiDAR 깊이 영상을 사용할 수 없습니다.")
                return
            }
            config.frameSemantics.insert(.sceneDepth)
        case .trackedSurface:
            config.planeDetection = [.horizontal, .vertical]
        case .dualCamera:
            onFailure("AR 캡처에서는 듀얼 카메라 깊이를 사용할 수 없습니다.")
            return
        }

        resetFrameHistory()
        configuration = config
        running = true
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
    }

    private func stopSession() {
        guard running else { return }
        running = false
        interrupted = false
        configuration = nil
        resetFrameHistory()
        session.pause()
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard running, !interrupted else { return }

        let trackingNormal: Bool
        if case .normal = frame.camera.trackingState {
            trackingNormal = true
        } else {
            trackingNormal = false
            resetSurfaceHistory()
        }

        if let lastFrameTime {
            let elapsed = frame.timestamp - lastFrameTime
            if elapsed >= 0, elapsed < Self.frameInterval { return }
        }
        lastFrameTime = frame.timestamp

        let result: DepthResult
        if trackingNormal {
            switch source {
            case .lidar:
                result = lidarResult(frame)
            case .trackedSurface:
                result = surfaceResult(frame, session: session)
            case .dualCamera:
                result = .unavailable("듀얼 카메라 깊이 영상을 받지 못했습니다.")
            }
        } else {
            result = .unavailable("카메라 추적이 안정되면 깊이를 표시합니다.")
        }

        // ARKit delivers the rear sensor image in its unrotated orientation.
        onFrame(DepthFrame(image: frame.capturedImage, orientation: .right, result: result))
    }

    private func lidarResult(_ frame: ARFrame) -> DepthResult {
        guard let depth = frame.sceneDepth, let confidence = depth.confidenceMap else {
            return .unavailable("신뢰도 정보가 포함된 LiDAR 깊이 영상을 기다리고 있습니다.")
        }
        return DepthEstimator.map(depth.depthMap, confidence: confidence, source: .lidar, absolute: true)
    }

    private func surfaceResult(_ frame: ARFrame, session: ARSession) -> DepthResult {
        let query = frame.raycastQuery(
            from: CGPoint(x: 0.5, y: 0.5), allowing: .existingPlaneGeometry, alignment: .any
        )
        guard let hit = session.raycast(query).first,
              hit.target == .existingPlaneGeometry,
              let anchor = hit.anchor as? ARPlaneAnchor else {
            resetSurfaceHistory()
            return .unavailable("화면 중앙에서 추적된 표면을 찾지 못했습니다.")
        }

        // ARKit's camera looks down negative Z. Use forward depth, as in the LiDAR map.
        let cameraPoint = simd_inverse(frame.camera.transform) * hit.worldTransform.columns.3
        let depth = -cameraPoint.z
        guard depth.isFinite, depth > 0 else {
            resetSurfaceHistory()
            return .unavailable("카메라 앞쪽 표면의 깊이를 확인하지 못했습니다.")
        }

        if surfaceAnchorID != anchor.identifier || !isConsecutiveSurfaceSample(frame.timestamp) {
            resetSurfaceHistory()
        }
        surfaceAnchorID = anchor.identifier
        surfaceTime = frame.timestamp
        if surfaceDistances.count == Self.surfaceSamples {
            surfaceDistances.removeFirst()
        }
        surfaceDistances.append(depth)
        guard surfaceDistances.count == Self.surfaceSamples else {
            return .unavailable("화면 중앙 표면의 깊이가 안정되기를 기다리고 있습니다.")
        }

        let ordered = surfaceDistances.sorted()
        let middle = ordered.count / 2
        let median = (ordered[middle - 1] + ordered[middle]) * 0.5
        guard ordered[ordered.count - 1] - ordered[0] <= max(0.08, 0.08 * median) else {
            return .unavailable("화면 중앙 표면의 깊이 변화가 큽니다.")
        }
        return DepthEstimator.surface(median)
    }

    private func isConsecutiveSurfaceSample(_ timestamp: TimeInterval) -> Bool {
        guard let surfaceTime else { return false }
        let elapsed = timestamp - surfaceTime
        return elapsed > 0 && elapsed <= Self.maximumSurfaceGap
    }

    func sessionWasInterrupted(_ session: ARSession) {
        guard running else { return }
        interrupted = true
        resetFrameHistory()
        onFailure("AR 카메라 연결이 중단되어 깊이 확인을 멈췄습니다.")
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        guard running, interrupted, let configuration else { return }
        resetFrameHistory()
        interrupted = false
        session.run(configuration, options: [.resetTracking, .removeExistingAnchors])
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        guard running else { return }
        stopSession()
        onFailure("AR 카메라를 사용할 수 없습니다. 카메라 권한과 기기 상태를 확인해 주세요.")
    }

    private func resetFrameHistory() {
        lastFrameTime = nil
        resetSurfaceHistory()
    }

    private func resetSurfaceHistory() {
        surfaceAnchorID = nil
        surfaceTime = nil
        surfaceDistances.removeAll(keepingCapacity: true)
    }
}
