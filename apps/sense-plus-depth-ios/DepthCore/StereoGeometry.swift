struct StereoIntrinsics {
    let fx: Double
    let fy: Double
    let cx: Double
    let cy: Double

    var isUsable: Bool {
        fx.isFinite && fy.isFinite && cx.isFinite && cy.isFinite && fx > 0 && fy > 0
    }

    func ray(at pixel: SIMD2<Double>) -> SIMD3<Double> {
        SIMD3((pixel.x - cx) / fx, (pixel.y - cy) / fy, 1)
    }

    func project(_ point: SIMD3<Double>) -> SIMD2<Double>? {
        guard point.z > 0 else { return nil }
        let pixel = SIMD2(fx * point.x / point.z + cx, fy * point.y / point.z + cy)
        return pixel.x.isFinite && pixel.y.isFinite ? pixel : nil
    }
}

struct StereoPose {
    // Columns of the rotation from the main to the ultrawide camera.
    let rotationX: SIMD3<Double>
    let rotationY: SIMD3<Double>
    let rotationZ: SIMD3<Double>
    let translation: SIMD3<Double>

    var isUsable: Bool {
        for column in [rotationX, rotationY, rotationZ] {
            guard abs(stereoDot(column, column) - 1) < 0.02 else { return false }
        }
        let cross = SIMD3(
            rotationX.y * rotationY.z - rotationX.z * rotationY.y,
            rotationX.z * rotationY.x - rotationX.x * rotationY.z,
            rotationX.x * rotationY.y - rotationX.y * rotationY.x
        )
        guard stereoDot(cross, rotationZ) > 0.98,
              abs(stereoDot(rotationX, rotationY)) < 0.02,
              abs(stereoDot(rotationX, rotationZ)) < 0.02,
              abs(stereoDot(rotationY, rotationZ)) < 0.02 else { return false }
        return (0.005...0.10).contains(stereoDot(translation, translation).squareRoot())
    }

    func rotate(_ point: SIMD3<Double>) -> SIMD3<Double> {
        rotationX * point.x + rotationY * point.y + rotationZ * point.z
    }

    func rotateBack(_ point: SIMD3<Double>) -> SIMD3<Double> {
        SIMD3(stereoDot(rotationX, point), stereoDot(rotationY, point), stereoDot(rotationZ, point))
    }

    var inverse: StereoPose {
        StereoPose(
            rotationX: SIMD3(rotationX.x, rotationY.x, rotationZ.x),
            rotationY: SIMD3(rotationX.y, rotationY.y, rotationZ.y),
            rotationZ: SIMD3(rotationX.z, rotationY.z, rotationZ.z),
            translation: -rotateBack(translation)
        )
    }
}

enum StereoGeometry {
    static func projectIntoWide(
        main: SIMD2<Double>, depth: Double,
        mainIntrinsics: StereoIntrinsics, wideIntrinsics: StereoIntrinsics, wideFromMain: StereoPose
    ) -> SIMD2<Double>? {
        guard depth.isFinite, depth > 0, mainIntrinsics.isUsable, wideIntrinsics.isUsable else { return nil }
        let point = wideFromMain.rotate(mainIntrinsics.ray(at: main) * depth) + wideFromMain.translation
        return wideIntrinsics.project(point)
    }

    static func triangulate(
        main: SIMD2<Double>, wide: SIMD2<Double>,
        mainIntrinsics: StereoIntrinsics, wideIntrinsics: StereoIntrinsics, wideFromMain: StereoPose
    ) -> Double? {
        guard mainIntrinsics.isUsable, wideIntrinsics.isUsable, wideFromMain.isUsable else { return nil }
        let mainRay = wideFromMain.rotate(mainIntrinsics.ray(at: main))
        let wideRay = wideIntrinsics.ray(at: wide)
        let a = stereoDot(mainRay, mainRay)
        let b = stereoDot(wideRay, wideRay)
        let cross = stereoDot(mainRay, wideRay)
        let determinant = a * b - cross * cross
        // Near-parallel rays amplify subpixel correspondence errors.
        guard determinant > a * b * 0.000004 else { return nil }
        let offset = wideFromMain.translation
        let mainProjection = stereoDot(mainRay, -offset)
        let wideProjection = stereoDot(wideRay, offset)
        let mainZ = (b * mainProjection + cross * wideProjection) / determinant
        let wideZ = (cross * mainProjection + a * wideProjection) / determinant
        guard mainZ.isFinite, wideZ.isFinite, mainZ > 0, wideZ > 0,
              let wideCheck = wideIntrinsics.project(mainRay * mainZ + offset),
              let mainCheck = mainIntrinsics.project(wideFromMain.rotateBack(wideRay * wideZ - offset)),
              stereoSquaredDistance(wide, wideCheck) <= 1.44,
              stereoSquaredDistance(main, mainCheck) <= 1.44 else { return nil }
        return mainZ
    }
}

private func stereoDot(_ lhs: SIMD3<Double>, _ rhs: SIMD3<Double>) -> Double {
    lhs.x * rhs.x + lhs.y * rhs.y + lhs.z * rhs.z
}

func stereoSquaredDistance(_ first: SIMD2<Double>, _ second: SIMD2<Double>) -> Double {
    let difference = first - second
    return difference.x * difference.x + difference.y * difference.y
}
