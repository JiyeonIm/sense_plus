import AVFoundation
import CoreImage
import SwiftUI
import UIKit

@MainActor
struct DepthView: View {
    @StateObject private var model = DepthViewModel()
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase

    private static let bone = Color(red: 0.93, green: 0.91, blue: 0.86)
    private static let navy = Color(red: 0.09, green: 0.16, blue: 0.22)
    private static let yellow = Color(red: 0.98, green: 0.80, blue: 0.29)
    private static let red = Color(red: 0.67, green: 0.16, blue: 0.13)
    private static let orange = Color(red: 0.72, green: 0.28, blue: 0.08)

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                title
                if let source = model.source {
                    modeControl(source)
                }
                reading
                cameraButton
                if model.isRunning {
                    Button { model.speakReading() } label: {
                        actionLabel("거리 듣기", symbol: "speaker.wave.2.fill")
                    }
                    .buttonStyle(DepthButtonStyle(fill: Self.navy, ink: .white, minimumHeight: 64))

                    preview
                }
            }
            .frame(maxWidth: 480, alignment: .leading)
            .padding(20)
            .frame(maxWidth: .infinity)
        }
        .background(Self.bone.ignoresSafeArea())
        .foregroundStyle(Self.navy)
        .preferredColorScheme(.light)
        .onChange(of: scenePhase, initial: true) { _, phase in
            model.setScenePhase(phase)
        }
        .onDisappear { model.stop() }
    }

    private var title: some View {
        VStack(alignment: .leading, spacing: 8) {
            (Text("SENSE") + Text("+").foregroundColor(Self.orange))
                .font(.title3.weight(.black))
                .accessibilityLabel("센스 플러스")
            Text("중앙 거리")
                .font(.largeTitle.weight(.bold))
                .accessibilityAddTraits(.isHeader)
        }
    }

    private func modeControl(_ source: DepthSource) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if model.sources.count > 1 && !model.isRunning && model.state != .requestingPermission {
                Menu {
                    ForEach(model.sources, id: \.rawValue) { option in
                        Button { model.select(option) } label: {
                            if option == source {
                                Label(option.title, systemImage: "checkmark")
                            } else {
                                Text(option.title)
                            }
                        }
                    }
                } label: {
                    Label(source.title, systemImage: "chevron.up.chevron.down")
                        .font(.headline)
                        .padding(.horizontal, 12)
                        .frame(minHeight: 48, alignment: .leading)
                        .background(.white.opacity(0.56), in: RoundedRectangle(cornerRadius: 8))
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("측정 방식")
                .accessibilityValue(source.title)
            } else {
                Text(source.title).font(.headline)
            }
            Text(source.detail).font(.subheadline)
        }
    }

    private var reading: some View {
        let reading = model.reading
        return VStack(alignment: .leading, spacing: 8) {
            Text(reading.title)
                .font(.title.weight(.bold))
                .monospacedDigit()
            if !reading.detail.isEmpty {
                Text(reading.detail).font(.body)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(18)
        .background(.white.opacity(0.56), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(reading.spoken)
    }

    private var cameraButton: some View {
        let title: String
        let symbol: String
        if model.isRunning {
            title = "측정 정지"
            symbol = "stop.fill"
        } else if model.state == .denied {
            title = "카메라 설정 열기"
            symbol = "gearshape"
        } else {
            title = "거리 측정 시작"
            symbol = "camera.fill"
        }
        return Button {
            if model.isRunning {
                model.stop()
            } else if model.state == .denied {
                if let settings = URL(string: UIApplication.openSettingsURLString) {
                    openURL(settings)
                }
            } else {
                model.start()
            }
        } label: {
            actionLabel(title, symbol: symbol)
        }
        .buttonStyle(DepthButtonStyle(
            fill: model.isRunning ? Self.red : Self.yellow,
            ink: model.isRunning ? .white : Self.navy,
            minimumHeight: 76
        ))
        .disabled(model.source == nil || model.state == .requestingPermission || model.isStopping)
    }

    private var preview: some View {
        ZStack {
            Self.navy
            if let image = model.preview?.image {
                GeometryReader { geometry in
                    let width = CGFloat(image.width)
                    let height = CGFloat(image.height)
                    let fit = min(geometry.size.width / width, geometry.size.height / height)
                    let diameter = min(width, height) * fit * 0.20
                    ZStack {
                        Image(decorative: image, scale: 1, orientation: .up)
                            .resizable()
                            .scaledToFit()
                        Circle()
                            .strokeBorder(.black, lineWidth: 5)
                            .overlay { Circle().strokeBorder(Self.yellow, lineWidth: 2) }
                            .frame(width: diameter, height: diameter)
                    }
                    .frame(width: geometry.size.width, height: geometry.size.height)
                }
            }
        }
        .aspectRatio(4.0 / 3.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .accessibilityHidden(true)
    }

    private func actionLabel(_ title: String, symbol: String) -> some View {
        HStack(spacing: 12) {
            Text(title).font(.title3.weight(.bold))
            Spacer(minLength: 0)
            Image(systemName: symbol)
                .font(.title3.weight(.semibold))
                .accessibilityHidden(true)
        }
    }
}

private struct DepthButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    let fill: Color
    let ink: Color
    let minimumHeight: CGFloat

    init(fill: Color, ink: Color, minimumHeight: CGFloat) {
        self.fill = fill
        self.ink = ink
        self.minimumHeight = minimumHeight
    }

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 18)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, minHeight: minimumHeight)
            .foregroundStyle(ink)
            .background(fill.opacity(configuration.isPressed ? 0.84 : 1), in: RoundedRectangle(cornerRadius: 12))
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.56)
    }
}

private extension DepthSource {
    var title: String {
        switch self {
        case .lidar: return "LiDAR"
        case .dualCamera: return "듀얼 카메라"
        case .trackedSurface: return "평면 인식"
        }
    }

    var detail: String {
        switch self {
        case .lidar: return "화면 중앙 영역까지의 센서 거리예요."
        case .dualCamera: return "카메라 두 대로 화면 중앙 영역의 깊이를 추정해요."
        case .trackedSurface: return "화면 중앙에서 인식한 평면까지의 거리를 추정해요."
        }
    }
}

private struct Reading {
    let title: String
    let detail: String
    let spoken: String

    init(_ title: String, _ detail: String = "", spoken: String? = nil) {
        self.title = title
        self.detail = detail
        self.spoken = spoken ?? (detail.isEmpty ? title : "\(title). \(detail)")
    }

    init(result: DepthResult) {
        switch result {
        case let .metric(measurement):
            let amount = measurement.metres.formatted(.number.precision(.fractionLength(0 ... 1)))
            let context: String
            switch measurement.source {
            case .lidar: context = "중앙 영역의 LiDAR 거리"
            case .dualCamera: context = "중앙 영역의 추정 거리"
            case .trackedSurface: context = "중앙에서 인식된 평면까지의 추정 거리"
            }
            self.init("약 \(amount) m", context, spoken: "\(context)는 약 \(amount) 미터예요.")
        case let .relative(relation):
            let comparison: String
            switch relation {
            case .closer: comparison = "주변보다 가까워요"
            case .similar: comparison = "주변과 비슷해요"
            case .farther: comparison = "주변보다 멀어요"
            }
            self.init(comparison, "화면 중앙 영역의 상대 거리", spoken: "화면 중앙 영역이 \(comparison).")
        case let .unavailable(reason):
            self.init("거리 확인이 어려워요", reason)
        }
    }
}

private enum CaptureState: Equatable {
    case idle, requestingPermission, denied, starting, running, failure(String)
}

// Only this resized preview image and a derived measurement reach the main actor.
private struct RenderedDepthFrame: @unchecked Sendable {
    let image: CGImage?
    let result: DepthResult
    let receivedAt: TimeInterval
}

private final class DepthPreviewRenderer: @unchecked Sendable {
    private let context = CIContext(options: [.cacheIntermediates: false])
    private let lock = NSLock()
    private var nextFrameAt: TimeInterval = 0
    private var awaitingDelivery = false
    private var cancelled = false

    func render(_ frame: DepthFrame) -> RenderedDepthFrame? {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        guard !cancelled && !awaitingDelivery && now >= nextFrameAt else {
            lock.unlock()
            return nil
        }
        awaitingDelivery = true
        nextFrameAt = now + 0.12
        lock.unlock()

        let oriented = CIImage(cvPixelBuffer: frame.image).oriented(frame.orientation)
        let scale = min(1, 520 / max(oriented.extent.width, oriented.extent.height))
        let thumbnail = oriented.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        return RenderedDepthFrame(
            image: context.createCGImage(thumbnail, from: thumbnail.extent),
            result: frame.result,
            receivedAt: now
        )
    }

    func delivered() {
        lock.lock()
        awaitingDelivery = false
        lock.unlock()
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }
}

@MainActor
private final class DepthViewModel: ObservableObject {
    @Published private(set) var source: DepthSource?
    @Published private(set) var state = CaptureState.idle
    @Published private(set) var preview: RenderedDepthFrame?
    @Published private(set) var isStopping = false

    let sources: [DepthSource]
    private let speaker = AVSpeechSynthesizer()
    private let maximumFrameAge = 1.0
    private var capture: (any DepthCapture)?
    private var renderer: DepthPreviewRenderer?
    private var freshnessTask: Task<Void, Never>?
    private var generation = UUID()
    private var scenePhase = ScenePhase.inactive
    private var permissionPending = false
    private var startAfterPermission = false

    init() {
        var modes: [DepthSource] = []
        if ARDepthCapture.supportsLiDAR { modes.append(.lidar) }
        if DualCameraCapture.isSupported { modes.append(.dualCamera) }
        if ARDepthCapture.supportsSurface { modes.append(.trackedSurface) }
        sources = modes
        source = modes.first
    }

    var isRunning: Bool { state == .starting || state == .running }

    var reading: Reading {
        switch state {
        case .idle:
            if source == nil { return Reading("측정할 수 없어요", "이 기기에서 지원하는 측정 방식이 없어요.") }
            return Reading("측정 준비", "확인할 방향을 향해 후면 카메라를 들어 주세요.")
        case .requestingPermission:
            return Reading("카메라 접근 확인 중", "카메라 사용을 허용하면 측정을 시작해요.")
        case .denied:
            return Reading("카메라 접근이 필요해요", "설정에서 SENSE+의 카메라 접근을 허용해 주세요.")
        case .starting:
            return Reading("카메라 켜는 중")
        case .running:
            if let preview, ProcessInfo.processInfo.systemUptime - preview.receivedAt <= maximumFrameAge {
                return Reading(result: preview.result)
            }
            return Reading("새 거리 정보가 없어요", "카메라를 잠시 고정해 주세요.")
        case let .failure(reason):
            return Reading("거리 측정이 어려워요", reason)
        }
    }

    func select(_ source: DepthSource) {
        guard !isRunning, state != .requestingPermission, sources.contains(source) else { return }
        self.source = source
        if case .failure = state { state = .idle }
    }

    func setScenePhase(_ phase: ScenePhase) {
        scenePhase = phase
        if phase == .active {
            if state == .denied && AVCaptureDevice.authorizationStatus(for: .video) == .authorized {
                state = .idle
            }
            // Dismissing the iOS permission sheet can deliver the reply before activation.
            finishPermissionRequest()
        } else if isRunning || phase == .background {
            stop()
        }
    }

    func start() {
        guard scenePhase == .active, source != nil, !isRunning, !isStopping else { return }
        if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
            startAfterPermission = true
            state = .requestingPermission
            guard !permissionPending else { return }
            permissionPending = true
            AVCaptureDevice.requestAccess(for: .video) { [weak self] _ in
                Task { @MainActor in
                    self?.permissionPending = false
                    self?.finishPermissionRequest()
                }
            }
        } else {
            startIfAuthorized()
        }
    }

    private func finishPermissionRequest() {
        guard startAfterPermission, !permissionPending, scenePhase == .active else { return }
        startAfterPermission = false
        startIfAuthorized()
    }

    private func startIfAuthorized() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            if let source { openCapture(source) }
        case .denied:
            state = .denied
        case .restricted:
            state = .failure("이 iPhone에서 카메라 접근이 제한되어 있어요.")
        case .notDetermined:
            state = .idle
        @unknown default:
            state = .failure("카메라 권한을 확인할 수 없어요.")
        }
    }

    private func openCapture(_ source: DepthSource) {
        let id = UUID()
        generation = id
        let openedAt = ProcessInfo.processInfo.systemUptime
        let renderer = DepthPreviewRenderer()
        self.renderer = renderer
        let onFrame: @Sendable (DepthFrame) -> Void = { [weak self] frame in
            guard let rendered = renderer.render(frame) else { return }
            Task { @MainActor in
                defer { renderer.delivered() }
                guard let self, self.generation == id, self.isRunning,
                      ProcessInfo.processInfo.systemUptime - rendered.receivedAt <= self.maximumFrameAge else { return }
                self.preview = rendered
                if self.state == .starting { self.state = .running }
            }
        }
        let onFailure: @Sendable (String) -> Void = { [weak self] message in
            Task { @MainActor in
                guard let self, self.generation == id, self.isRunning else { return }
                self.stop()
                self.state = .failure(message)
            }
        }
        switch source {
        case .lidar, .trackedSurface:
            capture = ARDepthCapture(source: source, onFrame: onFrame, onFailure: onFailure)
        case .dualCamera:
            capture = DualCameraCapture(onFrame: onFrame, onFailure: onFailure)
        }
        state = .starting
        freshnessTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .milliseconds(250))
                guard !Task.isCancelled, self?.checkCameraFreshness(generation: id, openedAt: openedAt) == true else { return }
            }
        }
        capture?.start()
    }

    private func checkCameraFreshness(generation id: UUID, openedAt: TimeInterval) -> Bool {
        guard generation == id else { return false }
        let now = ProcessInfo.processInfo.systemUptime
        if state == .starting && now - openedAt > 5 {
            stop()
            state = .failure("카메라 영상이 오지 않았어요. 다시 시작해 주세요.")
            return false
        }
        if let preview, now - preview.receivedAt > maximumFrameAge {
            self.preview = nil
        }
        return true
    }

    func stop() {
        generation = UUID()
        startAfterPermission = false
        freshnessTask?.cancel()
        freshnessTask = nil
        renderer?.cancel()
        renderer = nil
        if let capture {
            isStopping = true
            capture.stop { [weak self] in
                Task { @MainActor in self?.isStopping = false }
            }
        }
        capture = nil
        speaker.stopSpeaking(at: .immediate)
        preview = nil
        state = .idle
    }

    func speakReading() {
        guard isRunning else { return }
        let utterance = AVSpeechUtterance(string: reading.spoken)
        utterance.voice = AVSpeechSynthesisVoice(language: "ko-KR")
        speaker.stopSpeaking(at: .immediate)
        speaker.speak(utterance)
    }
}
