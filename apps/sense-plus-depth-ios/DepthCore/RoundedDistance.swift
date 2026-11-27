struct RoundedDistance: Equatable, Sendable {
    private let halfMetres: Int
    private let isTrackedSurface: Bool

    init?(_ measurement: DepthMeasurement) {
        guard DepthEstimator.metresRange.contains(measurement.metres) else { return nil }
        halfMetres = Int((measurement.metres * 2).rounded(.toNearestOrAwayFromZero))
        isTrackedSurface = measurement.source == .trackedSurface
    }

    init?(_ result: DepthResult) {
        guard case let .metric(measurement) = result else { return nil }
        self.init(measurement)
    }

    var number: String {
        let wholeMetres = halfMetres / 2
        return halfMetres.isMultiple(of: 2) ? String(wholeMetres) : "\(wholeMetres).5"
    }

    var spoken: String {
        let target = isTrackedSurface ? "중앙 표면은" : "중앙 대상은"
        return "\(target) 약 \(number)미터 앞에 있습니다."
    }
}

struct DistanceAnnouncements {
    private var candidate: RoundedDistance?
    private var successiveFrames = 0
    private var lastSpoken: RoundedDistance?
    private var lastSpokenAt: Double?

    mutating func update(
        _ distance: RoundedDistance?, now: Double, isSpeaking: Bool = false
    ) -> RoundedDistance? {
        guard let distance else {
            candidate = nil
            successiveFrames = 0
            return nil
        }
        if candidate != distance {
            candidate = distance
            successiveFrames = 1
        } else if successiveFrames < 3 {
            successiveFrames += 1
        }
        guard successiveFrames == 3, distance != lastSpoken, !isSpeaking,
              lastSpokenAt.map({ now - $0 >= 3 }) ?? true else { return nil }
        record(distance, now: now)
        return distance
    }

    mutating func record(_ distance: RoundedDistance, now: Double) {
        lastSpoken = distance
        lastSpokenAt = now
    }
}
