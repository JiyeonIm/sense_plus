import CoreVideo
import Foundation
import ImageIO

protocol DepthCapture: AnyObject {
    func start()
    func stop(onStopped: @escaping () -> Void)
}

struct DepthFrame {
    let image: CVPixelBuffer
    let orientation: CGImagePropertyOrientation
    let result: DepthResult
    let receivedAt: TimeInterval

    init(
        image: CVPixelBuffer, orientation: CGImagePropertyOrientation, result: DepthResult,
        receivedAt: TimeInterval = ProcessInfo.processInfo.systemUptime
    ) {
        self.image = image
        self.orientation = orientation
        self.result = result
        self.receivedAt = receivedAt
    }
}

extension DepthEstimator {
    private struct RegionSamples {
        var center: [Float] = []
        var centerTotal = 0
    }

    static func map(
        _ map: CVPixelBuffer, confidence: CVPixelBuffer? = nil, source: DepthSource
    ) -> DepthResult {
        if source == .lidar, confidence == nil {
            return .unavailable("LiDAR 깊이의 신뢰도를 확인할 수 없어요.")
        }
        guard let samples = sampleCenter(map, confidence: confidence) else {
            return .unavailable("깊이 데이터 형식을 확인할 수 없어요.")
        }
        return metric(samples.center, total: samples.centerTotal, source: source)
    }

    private static func sampleCenter(
        _ map: CVPixelBuffer, confidence: CVPixelBuffer?
    ) -> RegionSamples? {
        let width = CVPixelBufferGetWidth(map)
        let height = CVPixelBufferGetHeight(map)
        let format = CVPixelBufferGetPixelFormatType(map)
        guard min(width, height) >= 12,
              !CVPixelBufferIsPlanar(map),
              format == kCVPixelFormatType_DepthFloat32 || format == kCVPixelFormatType_OneComponent32Float,
              CVPixelBufferLockBaseAddress(map, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(map, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(map) else { return nil }

        let confidenceBase: UnsafeMutableRawPointer?
        let confidenceStride: Int
        if let confidence {
            guard CVPixelBufferGetPixelFormatType(confidence) == kCVPixelFormatType_OneComponent8,
                  !CVPixelBufferIsPlanar(confidence),
                  CVPixelBufferGetWidth(confidence) == width, CVPixelBufferGetHeight(confidence) == height,
                  CVPixelBufferLockBaseAddress(confidence, .readOnly) == kCVReturnSuccess else { return nil }
            guard let address = CVPixelBufferGetBaseAddress(confidence) else {
                CVPixelBufferUnlockBaseAddress(confidence, .readOnly)
                return nil
            }
            confidenceBase = address
            confidenceStride = CVPixelBufferGetBytesPerRow(confidence)
        } else {
            confidenceBase = nil
            confidenceStride = 0
        }
        defer {
            if let confidence {
                CVPixelBufferUnlockBaseAddress(confidence, .readOnly)
            }
        }

        let rowBytes = CVPixelBufferGetBytesPerRow(map)
        let middleX = width / 2
        let middleY = height / 2
        let shortSide = min(width, height)
        let radius = max(2, shortSide / 10)
        let step = max(1, shortSide / 120)
        var collected = RegionSamples()

        for y in stride(from: max(0, middleY - radius), through: min(height - 1, middleY + radius), by: step) {
            let data = base.advanced(by: y * rowBytes).assumingMemoryBound(to: Float32.self)
            let grades = confidenceBase?.advanced(by: y * confidenceStride).assumingMemoryBound(to: UInt8.self)
            let dy = y - middleY
            for x in stride(from: max(0, middleX - radius), through: min(width - 1, middleX + radius), by: step) {
                let dx = x - middleX
                guard dx * dx + dy * dy <= radius * radius else { continue }
                collected.centerTotal += 1
                // ARKit confidence map uses 0/1/2 for low/medium/high confidence.
                if let grades, grades[x] != 1 && grades[x] != 2 { continue }
                collected.center.append(data[x])
            }
        }
        return collected
    }
}
