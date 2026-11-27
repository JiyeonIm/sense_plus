import XCTest
@testable import DepthCore

final class DistanceAnnouncementsTests: XCTestCase {
    func testRoundHalfMetresAndKeepNumericKoreanSpeech() throws {
        let examples: [(Float, String)] = [
            (0.48, "0.5"), (1.24, "1"), (1.25, "1.5"), (1.74, "1.5"), (1.75, "2"), (2.89, "3"),
        ]
        for (metres, expected) in examples {
            let rounded = try distance(metres)
            XCTAssertEqual(rounded.number, expected, "Input \(metres)")
            XCTAssertTrue(rounded.spoken.filter { !$0.isWhitespace }.contains(expected + "미터"), rounded.spoken)
        }
        XCTAssertNil(RoundedDistance(.unavailable("깊이를 확인할 수 없어요.")))
        XCTAssertNil(RoundedDistance(DepthMeasurement(source: .calibratedStereo, metres: .nan, sampleCount: 25)))
    }

    func testTrackedSurfaceSpeechIdentifiesTheSurface() throws {
        let rounded = try XCTUnwrap(RoundedDistance(DepthEstimator.surface(1.6)))
        XCTAssertEqual(rounded.number, "1.5")
        XCTAssertTrue(rounded.spoken.contains("중앙 표면"))
        XCTAssertTrue(rounded.spoken.contains("1.5미터"))
    }

    func testAutomaticSpeechRequiresThreeConsecutiveUsableMeasurements() throws {
        let reading = try distance(1.5)
        var announcements = DistanceAnnouncements()

        XCTAssertNil(announcements.update(reading, now: 10))
        XCTAssertNil(announcements.update(reading, now: 11))
        XCTAssertNil(announcements.update(nil, now: 12))
        XCTAssertNil(announcements.update(reading, now: 20))
        XCTAssertNil(announcements.update(reading, now: 21))
        XCTAssertEqual(announcements.update(reading, now: 23)?.number, "1.5")

        XCTAssertNil(announcements.update(reading, now: 27))
        XCTAssertNil(announcements.update(reading, now: 28))
        XCTAssertNil(announcements.update(reading, now: 29), "Do not repeat an unchanged distance")
    }

    func testChangedDistanceWaitsForCooldownAndPreviousSpeech() throws {
        let first = try distance(1.5)
        let second = try distance(2)
        var announcements = DistanceAnnouncements()

        XCTAssertNil(announcements.update(first, now: 10))
        XCTAssertNil(announcements.update(first, now: 11))
        XCTAssertEqual(announcements.update(first, now: 13)?.number, "1.5")

        XCTAssertNil(announcements.update(second, now: 13.25))
        XCTAssertNil(announcements.update(second, now: 14))
        XCTAssertNil(announcements.update(second, now: 15.5), "A changed reading must wait three seconds")
        XCTAssertNil(announcements.update(second, now: 17, isSpeaking: true))
        XCTAssertEqual(announcements.update(second, now: 18, isSpeaking: false)?.number, "2")
    }

    func testManualReadingDoesNotRepeatAutomatically() throws {
        let reading = try distance(1.5)
        var announcements = DistanceAnnouncements()
        announcements.record(reading, now: 10)

        XCTAssertNil(announcements.update(reading, now: 12))
        XCTAssertNil(announcements.update(reading, now: 13))
        XCTAssertNil(announcements.update(reading, now: 14))
    }

    private func distance(_ metres: Float) throws -> RoundedDistance {
        try XCTUnwrap(RoundedDistance(DepthMeasurement(source: .calibratedStereo, metres: metres, sampleCount: 25)))
    }
}
