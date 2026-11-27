import CoreVideo
import ImageIO

protocol DepthCapture: AnyObject {
    func start()
    func stop(onStopped: @escaping () -> Void)
}

struct DepthFrame {
    let image: CVPixelBuffer
    let orientation: CGImagePropertyOrientation
    let result: DepthResult
}

extension DepthEstimator {
    private struct RegionSamples {
        var center: [Float] = []
        var centerTotal = 0
        var surround: [Float] = []
        var surroundTotal = 0
    }

    static func map(
        _ map: CVPixelBuffer, confidence: CVPixelBuffer? = nil, source: DepthSource, absolute: Bool
    ) -> DepthResult {
        if source == .lidar, confidence == nil {
            return .unavailable("LiDAR 깊이의 신뢰도를 확인할 수 없어요.")
        }
        guard let samples = sampleRegions(map, confidence: confidence, includeSurround: !absolute) else {
            return .unavailable("깊이 데이터 형식을 확인할 수 없어요.")
        }
        if absolute {
            return metric(samples.center, total: samples.centerTotal, source: source)
        }
        return relative(
            center: samples.center, centerTotal: samples.centerTotal,
            surround: samples.surround, surroundTotal: samples.surroundTotal
        )
    }

    private static func sampleRegions(
        _ map: CVPixelBuffer, confidence: CVPixelBuffer?, includeSurround: Bool
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
        let centerRadius = max(2, shortSide / 10)
        let innerRing = max(centerRadius + 1, shortSide * 16 / 100)
        let outerRing = max(innerRing + 1, shortSide * 28 / 100)
        let radius = includeSurround ? outerRing : centerRadius
        let step = max(1, shortSide / 120)
        var collected = RegionSamples()

        for y in stride(from: max(0, middleY - radius), through: min(height - 1, middleY + radius), by: step) {
            let data = base.advanced(by: y * rowBytes).assumingMemoryBound(to: Float32.self)
            let grades = confidenceBase?.advanced(by: y * confidenceStride).assumingMemoryBound(to: UInt8.self)
            let dy = y - middleY
            for x in stride(from: max(0, middleX - radius), through: min(width - 1, middleX + radius), by: step) {
                let dx = x - middleX
                let distanceSquared = dx * dx + dy * dy
                let inCenter = distanceSquared <= centerRadius * centerRadius
                let inRing = includeSurround && distanceSquared >= innerRing * innerRing && distanceSquared <= outerRing * outerRing
                guard inCenter || inRing else { continue }
                if inCenter { collected.centerTotal += 1 } else { collected.surroundTotal += 1 }
                // ARKit confidence map uses 0/1/2 for low/medium/high confidence.
                if let grades, grades[x] != 1 && grades[x] != 2 { continue }
                if inCenter { collected.center.append(data[x]) } else { collected.surround.append(data[x]) }
            }
        }
        return collected
    }
}
