import XCTest
@testable import DepthCore

final class DepthEstimatorTests: XCTestCase {
    func testMetricUsesCentralMedianAndKeepsSource() {
        let samples: [Float] = [1.36, 1.48, 1.49, 1.49, 1.50, 1.50, 1.50, 1.50,
                                1.51, 1.51, 1.52, 1.68, .nan, .infinity]
        XCTAssertEqual(
            DepthEstimator.metric(samples, total: samples.count, source: .lidar),
            .metric(DepthMeasurement(source: .lidar, metres: 1.5, sampleCount: 12))
        )
    }

    func testMetricRejectsSparseAndOutOfRangeData() {
        let remaining = [Float](repeating: 2, count: 12)
        let invalid = [Float](repeating: 7, count: 20)
        assertUnavailable(DepthEstimator.metric(remaining + invalid, total: 32, source: .dualCamera))
        assertUnavailable(DepthEstimator.metric([.nan, .infinity, -1, 0], total: 4, source: .lidar))
    }

    func testMetricRejectsSeveralSurfacesInTheTarget() {
        let samples = [Float](repeating: 1, count: 12) + [Float](repeating: 3, count: 12)
        assertUnavailable(DepthEstimator.metric(samples, total: samples.count, source: .lidar))
    }

    func testRelativeOrderingUsesOnlyTheCurrentMap() {
        let near = [Float](repeating: 0.9, count: 18)
        let far = [Float](repeating: 2.1, count: 18)
        let nearVariation = [Float](repeating: 0.92, count: 18)
        XCTAssertEqual(compare(near, far), .relative(.closer))
        XCTAssertEqual(compare(far, near), .relative(.farther))
        XCTAssertEqual(compare(near, nearVariation), .relative(.similar))
        XCTAssertEqual(
            compare(near.map { $0 * 100 }, far.map { $0 * 100 }),
            .relative(.closer)
        )
    }

    func testRelativeRejectsMixedOrUnusableSamples() {
        let mixed = [Float](repeating: 0.8, count: 10) + [Float](repeating: 4, count: 10)
        let uniform = [Float](repeating: 2, count: 20)
        assertUnavailable(compare(mixed, uniform))
        assertUnavailable(compare([.infinity, .nan], uniform))
    }

    func testTrackedSurfaceIsDistinctAndBounded() {
        XCTAssertEqual(
            DepthEstimator.surface(0.5),
            .metric(DepthMeasurement(source: .trackedSurface, metres: 0.5, sampleCount: 1))
        )
        assertUnavailable(DepthEstimator.surface(.nan))
        assertUnavailable(DepthEstimator.surface(0.1))
        assertUnavailable(DepthEstimator.surface(5.01))
    }

    private func compare(_ first: [Float], _ second: [Float]) -> DepthResult {
        DepthEstimator.relative(center: first, centerTotal: first.count, surround: second, surroundTotal: second.count)
    }

    private func assertUnavailable(_ result: DepthResult, file: StaticString = #filePath, line: UInt = #line) {
        if case .unavailable = result { return }
        XCTFail("Expected unavailable result, got \(result)", file: file, line: line)
    }
}
