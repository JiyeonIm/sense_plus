import Foundation

enum DepthSource: String, CaseIterable, Equatable, Sendable {
    case lidar
    case calibratedStereo
    case dualCamera
    case trackedSurface
}

struct DepthMeasurement: Equatable, Sendable {
    let source: DepthSource
    let metres: Float
    let sampleCount: Int
}

enum DepthResult: Equatable, Sendable {
    case metric(DepthMeasurement)
    case unavailable(String)
}

enum DepthEstimator {
    static let metresRange: ClosedRange<Float> = 0.25...5

    static func metric(
        _ samples: [Float], total: Int, source: DepthSource, minimumSamples: Int = 12
    ) -> DepthResult {
        // Keep out-of-range or invalid points in the coverage denominator.
        let values = samples.filter { metresRange.contains($0) }
        guard let summary = summary(values, total: total, minimumSamples: minimumSamples) else {
            return .unavailable("중앙에 신뢰할 만한 거리 데이터가 부족해요.")
        }
        let allowableSpread = max(0.12, summary.median * 0.20)
        guard summary.upper - summary.lower <= allowableSpread else {
            return .unavailable("중앙에 여러 깊이가 섞여 있어요. 한 표면을 비춰 주세요.")
        }
        return .metric(DepthMeasurement(source: source, metres: summary.median, sampleCount: values.count))
    }

    static func surface(_ metres: Float) -> DepthResult {
        guard metresRange.contains(metres) else {
            return .unavailable("추적된 표면이 시험 거리 범위를 벗어났어요.")
        }
        return .metric(DepthMeasurement(source: .trackedSurface, metres: metres, sampleCount: 1))
    }

    private struct Summary {
        let lower: Float
        let median: Float
        let upper: Float
    }

    private static func summary(_ values: [Float], total: Int, minimumSamples: Int) -> Summary? {
        guard total > 0, values.count >= max(minimumSamples, (total + 1) / 2) else { return nil }
        let sorted = values.sorted()
        let last = sorted.count - 1
        let middle = sorted.count / 2
        let median = sorted.count.isMultiple(of: 2)
            ? sorted[middle - 1] / 2 + sorted[middle] / 2
            : sorted[middle]
        return Summary(lower: sorted[last / 5], median: median, upper: sorted[last * 4 / 5])
    }
}
