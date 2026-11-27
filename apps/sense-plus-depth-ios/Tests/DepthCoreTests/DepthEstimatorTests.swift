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
        assertUnavailable(DepthEstimator.metric(remaining + invalid, total: 32, source: .calibratedStereo))
        assertUnavailable(DepthEstimator.metric([.nan, .infinity, -1, 0], total: 4, source: .lidar))
    }

    func testMetricRejectsSeveralSurfacesInTheTarget() {
        let samples = [Float](repeating: 1, count: 12) + [Float](repeating: 3, count: 12)
        assertUnavailable(DepthEstimator.metric(samples, total: samples.count, source: .lidar))
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

    private func assertUnavailable(_ result: DepthResult, file: StaticString = #filePath, line: UInt = #line) {
        if case .unavailable = result { return }
        XCTFail("Expected unavailable result, got \(result)", file: file, line: line)
    }
}
