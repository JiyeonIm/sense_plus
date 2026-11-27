struct StereoLumaImage {
    let width: Int
    let height: Int
    let pixels: [UInt8]

    init?(width: Int, height: Int, pixels: [UInt8]) {
        guard width >= 32, height >= 32, width <= 4096, height <= 4096,
              pixels.count == width * height else { return nil }
        self.width = width
        self.height = height
        self.pixels = pixels
    }

    func luma(at point: SIMD2<Double>) -> Double? {
        guard point.x >= 0, point.x < Double(width - 1),
              point.y >= 0, point.y < Double(height - 1) else { return nil }
        let x = Int(point.x)
        let y = Int(point.y)
        let dx = point.x - Double(x)
        let dy = point.y - Double(y)
        let above = y * width + x
        let below = above + width
        let top = Double(pixels[above]) * (1 - dx) + Double(pixels[above + 1]) * dx
        let bottom = Double(pixels[below]) * (1 - dx) + Double(pixels[below + 1]) * dx
        return top * (1 - dy) + bottom * dy
    }

    func containsCentralRegion(_ point: SIMD2<Double>) -> Bool {
        abs(point.x - Double(width) / 2) < Double(width) * 0.25
            && abs(point.y - Double(height) / 2) < Double(height) * 0.25
    }
}

enum StereoMatcher {
    private static let inverseDepthSteps = 96
    private static let inverseFar = 1 / Double(DepthEstimator.metresRange.upperBound)
    private static let inverseStep = (1 / Double(DepthEstimator.metresRange.lowerBound) - inverseFar)
        / Double(inverseDepthSteps)

    static func distance(
        main: StereoLumaImage, wide: StereoLumaImage,
        mainIntrinsics: StereoIntrinsics, wideIntrinsics: StereoIntrinsics, wideFromMain: StereoPose
    ) -> DepthResult {
        guard mainIntrinsics.isUsable, wideIntrinsics.isUsable, wideFromMain.isUsable else {
            return .unavailable("카메라 보정 정보를 기다리는 중")
        }
        let mainToWide = Search(source: main, destination: wide, sourceK: mainIntrinsics,
                                destinationK: wideIntrinsics, pose: wideFromMain)
        let wideToMain = Search(source: wide, destination: main, sourceK: wideIntrinsics,
                                destinationK: mainIntrinsics, pose: wideFromMain.inverse)
        let center = SIMD2(Double(main.width - 1) / 2, Double(main.height - 1) / 2)
        let spacing = max(5, Double(min(main.width, main.height)) * 0.032)
        var depths: [Float] = []
        var total = 0
        for row in -2...2 {
            for column in -2...2 where row * row + column * column <= 5 {
                total += 1
                let pixel = center + SIMD2(Double(column), Double(row)) * spacing
                guard let matched = mainToWide.match(from: pixel),
                      let roundTrip = wideToMain.match(from: matched),
                      stereoSquaredDistance(roundTrip, pixel) <= 2.25,
                      let meters = StereoGeometry.triangulate(
                        main: pixel, wide: matched, mainIntrinsics: mainIntrinsics,
                        wideIntrinsics: wideIntrinsics, wideFromMain: wideFromMain
                      ) else { continue }
                depths.append(Float(meters))
            }
        }
        return DepthEstimator.metric(depths, total: total, source: .calibratedStereo, minimumSamples: 8)
    }

    private struct PatchSample {
        let ray: SIMD3<Double>
        let value: Double
    }

    private struct Search {
        let source: StereoLumaImage
        let destination: StereoLumaImage
        let sourceK: StereoIntrinsics
        let destinationK: StereoIntrinsics
        let pose: StereoPose

        func match(from center: SIMD2<Double>) -> SIMD2<Double>? {
            guard source.containsCentralRegion(center) else { return nil }
            var samples: [PatchSample] = []
            var sum = 0.0
            var squaredSum = 0.0
            for y in stride(from: -8, through: 8, by: 2) {
                for x in stride(from: -8, through: 8, by: 2) {
                    let pixel = center + SIMD2(Double(x), Double(y))
                    guard let value = source.luma(at: pixel) else { return nil }
                    samples.append(PatchSample(ray: pose.rotate(sourceK.ray(at: pixel)), value: value))
                    sum += value
                    squaredSum += value * value
                }
            }
            let count = Double(samples.count)
            let sourceVariation = squaredSum - sum * sum / count
            guard sourceVariation >= count * 81 else { return nil }
            let ray = pose.rotate(sourceK.ray(at: center))
            var scores = [Double](repeating: -1, count: StereoMatcher.inverseDepthSteps + 1)
            var bestIndex = 0
            for index in scores.indices {
                let depth = depth(at: Double(index))
                guard let pixel = destinationK.project(ray * depth + pose.translation),
                      destination.containsCentralRegion(pixel) else { continue }
                scores[index] = correlation(samples, sourceSum: sum, variation: sourceVariation, depth: depth)
                if scores[index] > scores[bestIndex] { bestIndex = index }
            }
            guard bestIndex > 0, bestIndex < scores.count - 1, scores[bestIndex] >= 0.78,
                  let best = destinationK.project(ray * depth(at: Double(bestIndex)) + pose.translation),
                  let directionOnly = destinationK.project(ray),
                  stereoSquaredDistance(best, directionOnly) >= 1.96 else { return nil }

            // Demand a distinct peak across the calibrated epipolar search.
            for index in scores.indices where index != bestIndex && scores[index] > scores[bestIndex] - 0.055 {
                guard let competitor = destinationK.project(ray * depth(at: Double(index)) + pose.translation)
                else { continue }
                if stereoSquaredDistance(best, competitor) >= 2.25 { return nil }
            }
            let left = scores[bestIndex - 1]
            let right = scores[bestIndex + 1]
            let curvature = left + right - 2 * scores[bestIndex]
            guard left > -1, right > -1, curvature < -0.00002 else { return nil }
            let fraction = max(-0.5, min(0.5, (left - right) / (2 * curvature)))
            return destinationK.project(ray * depth(at: Double(bestIndex) + fraction) + pose.translation)
        }

        private func depth(at index: Double) -> Double {
            1 / (StereoMatcher.inverseFar + StereoMatcher.inverseStep * index)
        }

        private func correlation(
            _ samples: [PatchSample], sourceSum: Double, variation: Double, depth: Double
        ) -> Double {
            var sum = 0.0
            var squaredSum = 0.0
            var cross = 0.0
            for sample in samples {
                // Reproject each patch pixel so the two lenses compare the same region.
                guard let pixel = destinationK.project(sample.ray * depth + pose.translation),
                      let value = destination.luma(at: pixel) else { return -1 }
                sum += value
                squaredSum += value * value
                cross += sample.value * value
            }
            let count = Double(samples.count)
            let destinationVariation = squaredSum - sum * sum / count
            guard destinationVariation >= count * 64 else { return -1 }
            return (cross - sourceSum * sum / count) / (variation * destinationVariation).squareRoot()
        }
    }
}
