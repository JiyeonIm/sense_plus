import XCTest
@testable import DepthCore

final class StereoMatcherTests: XCTestCase {
    private let width = 256
    private let height = 192
    private let focalLength = 180.0
    private let baseline = 0.027

    func testTexturedPlaneWithKnownDisparityProducesMetricDepth() throws {
        let shift = 4
        let main = try lumaImage(pixels { x, y in texture(x, y) })
        // A point x in main appears at x - shift in wide for this camera pair.
        let wide = try lumaImage(pixels { x, y in texture(x + shift, y) })

        let result = match(main, wide)
        guard case let .metric(reading) = result else {
            return XCTFail("Expected a metric reading for a known textured plane, got \(result)")
        }
        XCTAssertEqual(reading.source, .calibratedStereo)
        XCTAssertEqual(Double(reading.metres), focalLength * baseline / Double(shift), accuracy: 0.15)
        XCTAssertGreaterThan(reading.sampleCount, 1)
    }

    func testUniformImagesCannotProduceADistance() throws {
        let image = try lumaImage(pixels { _, _ in 128 })
        let result = match(image, image)
        guard case .unavailable = result else {
            return XCTFail("Uniform pixels have no identifiable stereo correspondence, got \(result)")
        }
    }

    func testRepetitiveTextureCannotDisambiguateSeveralDepths() throws {
        let image = try lumaImage(pixels { x, y in texture(x % 4, y) })
        let result = match(image, image)
        guard case .unavailable = result else {
            return XCTFail("Several repeated disparities should be rejected, got \(result)")
        }
    }

    func testImageRequiresOneLumaSamplePerPixel() {
        XCTAssertNil(StereoLumaImage(width: width, height: height, pixels: [10, 20, 30]))
    }

    private func match(_ main: StereoLumaImage, _ wide: StereoLumaImage) -> DepthResult {
        let intrinsics = StereoIntrinsics(fx: focalLength, fy: focalLength, cx: Double(width) / 2, cy: Double(height) / 2)
        let pose = StereoPose(
            rotationX: SIMD3(1, 0, 0), rotationY: SIMD3(0, 1, 0), rotationZ: SIMD3(0, 0, 1),
            translation: SIMD3(-baseline, 0, 0)
        )
        return StereoMatcher.distance(
            main: main, wide: wide, mainIntrinsics: intrinsics, wideIntrinsics: intrinsics, wideFromMain: pose
        )
    }

    private func lumaImage(_ data: [UInt8]) throws -> StereoLumaImage {
        try XCTUnwrap(StereoLumaImage(width: width, height: height, pixels: data))
    }

    private func pixels(_ value: (Int, Int) -> UInt8) -> [UInt8] {
        (0..<height).flatMap { y in (0..<width).map { x in value(x, y) } }
    }

    private func texture(_ x: Int, _ y: Int) -> UInt8 {
        // Deterministic aperiodic texture avoids dependencies on a random seed or an image file.
        var sample = (UInt32(x) &* 0xA511_E9B3) ^ (UInt32(y) &* 0xC286_56ED)
        sample ^= sample >> 16
        sample = sample &* 0x7FEB_352D
        sample ^= sample >> 15
        return UInt8(truncatingIfNeeded: sample)
    }
}
