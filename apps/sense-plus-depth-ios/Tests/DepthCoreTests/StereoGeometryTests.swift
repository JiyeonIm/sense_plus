import Foundation
import XCTest
@testable import DepthCore

final class StereoGeometryTests: XCTestCase {
    private let mainLens = StereoIntrinsics(fx: 970, fy: 920, cx: 652, cy: 361)
    private let wideLens = StereoIntrinsics(fx: 505, fy: 488, cx: 633, cy: 351)

    private var rotatedPair: StereoPose {
        let yaw = -0.115
        let roll = 0.037
        let cy = cos(yaw)
        let sy = sin(yaw)
        let cz = cos(roll)
        let sz = sin(roll)
        return StereoPose(
            rotationX: SIMD3(cz * cy, sz * cy, -sy),
            rotationY: SIMD3(-sz, cz, 0),
            rotationZ: SIMD3(cz * sy, sz * sy, cy),
            translation: SIMD3(-0.025, 0.012, 0.003)
        )
    }

    func testProjectionAndTriangulationAgreeWithKnownRotatedCameraCoordinates() throws {
        // Independently projected from three known 3D scene points at different depths.
        // The wide lens's focal lengths, principal point and camera pose all differ.
        let examples: [(main: SIMD2<Double>, wide: SIMD2<Double>, metres: Double)] = [
            (SIMD2(506.5, 556.5), SIMD2(477.0133044274294, 459.3622646391948), 0.8),
            (SIMD2(694.4375, 234.5), SIMD2(591.7153239693976, 286.33783329572884), 1.6),
            (SIMD2(786.3076923076924, 445.9230769230769), SIMD2(638.8376919494751, 397.75161673302784), 3.25),
        ]
        for example in examples {
            let projected = try XCTUnwrap(StereoGeometry.projectIntoWide(
                main: example.main, depth: example.metres, mainIntrinsics: mainLens,
                wideIntrinsics: wideLens, wideFromMain: rotatedPair
            ))
            XCTAssertEqual(projected.x, example.wide.x, accuracy: 0.0001)
            XCTAssertEqual(projected.y, example.wide.y, accuracy: 0.0001)
            let metres = try XCTUnwrap(StereoGeometry.triangulate(
                main: example.main, wide: example.wide, mainIntrinsics: mainLens,
                wideIntrinsics: wideLens, wideFromMain: rotatedPair
            ))
            XCTAssertEqual(metres, example.metres, accuracy: 0.0001)
        }
    }

    func testRejectsUnobservableAndNonPhysicalDepths() {
        let main = SIMD2(652.0, 361.0)
        let wide = SIMD2(633.0, 351.0)
        let baseline = parallelPair(translation: SIMD3(-0.02, 0, 0))
        let colocated = parallelPair(translation: .zero)

        XCTAssertNil(depth(main, wide, pose: colocated), "Rotation without a baseline cannot measure depth")
        XCTAssertNil(depth(main, wide, pose: baseline), "Parallel rays have no finite depth")
        XCTAssertNil(depth(main, SIMD2(643, 351), pose: baseline), "The correspondence intersects behind both lenses")
        XCTAssertNil(depth(main, SIMD2(624, 392), pose: baseline), "Large off-epipolar error is ambiguous")
        XCTAssertNil(depth(SIMD2(.nan, 361), wide, pose: baseline))
    }

    func testProjectionRejectsInvalidIntrinsicsAndDepth() {
        let main = SIMD2(652.0, 361.0)
        let baseline = parallelPair(translation: SIMD3(-0.02, 0, 0))
        let invalidLens = StereoIntrinsics(fx: 0, fy: 920, cx: 652, cy: 361)
        XCTAssertNil(StereoGeometry.projectIntoWide(
            main: main, depth: 1, mainIntrinsics: invalidLens, wideIntrinsics: wideLens, wideFromMain: baseline
        ))
        for invalidDepth in [-1.0, 0, .infinity, .nan] {
            XCTAssertNil(StereoGeometry.projectIntoWide(
                main: main, depth: invalidDepth, mainIntrinsics: mainLens, wideIntrinsics: wideLens,
                wideFromMain: baseline
            ))
        }
    }

    private func parallelPair(translation: SIMD3<Double>) -> StereoPose {
        StereoPose(
            rotationX: SIMD3(1, 0, 0), rotationY: SIMD3(0, 1, 0), rotationZ: SIMD3(0, 0, 1),
            translation: translation
        )
    }

    private func depth(_ main: SIMD2<Double>, _ wide: SIMD2<Double>, pose: StereoPose) -> Double? {
        StereoGeometry.triangulate(
            main: main, wide: wide, mainIntrinsics: mainLens, wideIntrinsics: wideLens, wideFromMain: pose
        )
    }
}
