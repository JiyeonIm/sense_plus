import CoreVideo
import simd

enum StereoFrameSolver {
    static func measure(
        main: CVPixelBuffer, wide: CVPixelBuffer,
        mainIntrinsics: simd_float3x3, wideIntrinsics: simd_float3x3, wideFromMain: simd_float4x3
    ) -> DepthResult {
        guard let mainPixels = luma(main), let widePixels = luma(wide) else {
            return .unavailable("동기화된 영상 데이터 대기 중")
        }
        let pose = StereoPose(
            rotationX: vector(wideFromMain.columns.0),
            rotationY: vector(wideFromMain.columns.1),
            rotationZ: vector(wideFromMain.columns.2),
            translation: vector(wideFromMain.columns.3)
        )
        return StereoMatcher.distance(
            main: mainPixels, wide: widePixels, mainIntrinsics: calibration(mainIntrinsics),
            wideIntrinsics: calibration(wideIntrinsics), wideFromMain: pose
        )
    }

    private static func calibration(_ matrix: simd_float3x3) -> StereoIntrinsics {
        StereoIntrinsics(fx: Double(matrix.columns.0.x), fy: Double(matrix.columns.1.y),
                         cx: Double(matrix.columns.2.x), cy: Double(matrix.columns.2.y))
    }

    private static func vector(_ vector: SIMD3<Float>) -> SIMD3<Double> {
        SIMD3(Double(vector.x), Double(vector.y), Double(vector.z))
    }

    private static func luma(_ buffer: CVPixelBuffer) -> StereoLumaImage? {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        guard format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
                || format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
              CVPixelBufferGetPlaneCount(buffer) == 2,
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let width = CVPixelBufferGetWidthOfPlane(buffer, 0)
        let height = CVPixelBufferGetHeightOfPlane(buffer, 0)
        let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        guard (32...4096).contains(width), (32...4096).contains(height), stride >= width,
              let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0) else { return nil }
        var pixels = [UInt8](repeating: 0, count: width * height)
        pixels.withUnsafeMutableBytes { bytes in
            guard let destination = bytes.baseAddress else { return }
            for row in 0..<height {
                destination.advanced(by: row * width).copyMemory(from: base.advanced(by: row * stride), byteCount: width)
            }
        }
        return StereoLumaImage(width: width, height: height, pixels: pixels)
    }
}
