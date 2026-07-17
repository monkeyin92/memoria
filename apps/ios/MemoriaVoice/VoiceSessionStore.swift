import Foundation
import LiveKit
import Observation

@MainActor
@Observable
final class VoiceSessionStore: NSObject, RoomDelegate, @unchecked Sendable {
    enum ConnectionPhase: Equatable {
        case idle
        case creatingSession
        case connecting
        case connected
        case reconnecting
        case ended
        case failed

        var label: String {
            switch self {
            case .idle: "尚未连接"
            case .creatingSession: "正在创建会话"
            case .connecting: "正在连接"
            case .connected: "已连接"
            case .reconnecting: "连接恢复中"
            case .ended: "会话已结束"
            case .failed: "连接失败"
            }
        }
    }

    enum AgentStatus: Equatable {
        case ready
        case listening
        case thinking
        case speaking
        case interrupted
        case toolWaiting
        case recovering
        case closed

        init(serverValue: String) {
            switch serverValue {
            case "listening", "user_speaking", "eot_pending": self = .listening
            case "thinking": self = .thinking
            case "speaking": self = .speaking
            case "interrupted", "interruption_pending": self = .interrupted
            case "tool_waiting": self = .toolWaiting
            case "recovering", "reconnecting": self = .recovering
            case "closed": self = .closed
            default: self = .ready
            }
        }

        var label: String {
            switch self {
            case .ready: "可以说话"
            case .listening: "正在听"
            case .thinking: "正在思考"
            case .speaking: "正在回答"
            case .interrupted: "已停止回答"
            case .toolWaiting: "正在处理任务"
            case .recovering: "正在恢复服务"
            case .closed: "已关闭"
            }
        }

        var symbol: String {
            switch self {
            case .ready: "waveform"
            case .listening: "ear"
            case .thinking: "ellipsis.bubble"
            case .speaking: "speaker.wave.2"
            case .interrupted: "stop.fill"
            case .toolWaiting: "gearshape.2"
            case .recovering: "arrow.clockwise"
            case .closed: "xmark.circle"
            }
        }
    }

    private(set) var connectionPhase: ConnectionPhase = .idle
    private(set) var agentStatus: AgentStatus = .ready
    private(set) var transcripts: [TranscriptLine] = []
    private(set) var micEnabled = true
    private(set) var errorMessage: String?
    private(set) var roomName: String?
    private(set) var sessionID: String?

    @ObservationIgnored private var generationFence = ClientGenerationFence()
    @ObservationIgnored private var liveKitSession: LiveKit.Session?
    @ObservationIgnored private var controlAPI: ControlAPIClient?
    @ObservationIgnored private var reconnectTask: Task<Void, Never>?
    @ObservationIgnored private var activeTurnID = 0
    @ObservationIgnored private var awaitingServerGenerationAfterReconnect = false
    @ObservationIgnored private var ending = false

    var isActive: Bool {
        switch connectionPhase {
        case .creatingSession, .connecting, .connected, .reconnecting:
            true
        case .idle, .ended, .failed:
            false
        }
    }

    var canSendText: Bool {
        connectionPhase == .connected
    }

    var canStopResponse: Bool {
        connectionPhase == .connected &&
            (agentStatus == .speaking || agentStatus == .thinking || agentStatus == .toolWaiting)
    }

    func start(baseURLText: String) async {
        guard !isActive else { return }
        resetForNewSession()
        connectionPhase = .creatingSession

        do {
            let api = try ControlAPIClient(baseURLText: baseURLText)
            controlAPI = api
            let created = try await api.createSession()
            sessionID = created.sessionID
            roomName = created.roomName

            let source = LiteralTokenSource(
                serverURL: created.liveKitURL,
                participantToken: created.participantToken,
                participantName: "Memoria iOS",
                roomName: created.roomName
            )
            let session = LiveKit.Session(
                tokenSource: source,
                options: SessionOptions(preConnectAudio: true, agentConnectTimeout: 20)
            )
            liveKitSession = session
            session.room.add(delegate: self)
            connectionPhase = .connecting
            await session.start()

            if let sessionError = session.error {
                throw sessionError
            }
        } catch is CancellationError {
            await end()
        } catch {
            connectionPhase = .failed
            errorMessage = error.localizedDescription
            await disconnectLiveKit()
        }
    }

    func toggleMicrophone() async {
        guard let session = liveKitSession else { return }
        let desired = !micEnabled
        do {
            try await session.room.localParticipant.setMicrophone(enabled: desired)
            micEnabled = desired
        } catch {
            errorMessage = "麦克风切换失败：\(error.localizedDescription)"
        }
    }

    func stopResponse() async {
        guard let sessionID, let controlAPI else { return }
        do {
            try await controlAPI.stopResponse(sessionID: sessionID)
        } catch {
            errorMessage = "停止回答失败：\(error.localizedDescription)"
        }
    }

    func send(text: String) async -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let session = liveKitSession, canSendText else {
            return false
        }
        guard await session.send(text: trimmed) != nil else {
            errorMessage = session.error?.localizedDescription ?? "文字消息发送失败"
            return false
        }
        return true
    }

    func end() async {
        guard liveKitSession != nil || isActive else {
            connectionPhase = .ended
            agentStatus = .closed
            return
        }
        ending = true
        cancelReconnectTimeout()
        await disconnectLiveKit()
        connectionPhase = .ended
        agentStatus = .closed
        ending = false
    }

    func dismissError() {
        errorMessage = nil
    }

    func resumeAudioIfNeeded() async {
        guard connectionPhase == .connected, let session = liveKitSession else { return }
        do {
            try await session.room.localParticipant.setMicrophone(enabled: micEnabled)
            await setRemoteAudioSubscribed(true, in: session.room)
        } catch {
            errorMessage = "恢复音频失败：\(error.localizedDescription)"
        }
    }

    private func resetForNewSession() {
        cancelReconnectTimeout()
        transcripts = []
        generationFence.reset()
        activeTurnID = 0
        awaitingServerGenerationAfterReconnect = false
        micEnabled = true
        errorMessage = nil
        roomName = nil
        sessionID = nil
        agentStatus = .ready
        ending = false
    }

    private func disconnectLiveKit() async {
        guard let session = liveKitSession else { return }
        session.room.remove(delegate: self)
        await session.end()
        liveKitSession = nil
    }

    private func apply(data: Data, topic: String) {
        guard let event = try? VoiceEventDecoder.decode(data, topic: topic) else { return }
        switch event {
        case let .assistantState(state):
            guard state.sessionID == sessionID,
                  generationFence.accepts(state.generationID)
            else { return }
            activeTurnID = state.turnID
            awaitingServerGenerationAfterReconnect = false
            agentStatus = .init(serverValue: state.state)
        case let .transcript(transcript):
            guard generationFence.accepts(transcript.generationID) else { return }
            activeTurnID = transcript.turnID
            awaitingServerGenerationAfterReconnect = false
            apply(transcript: transcript)
        }
    }

    private func apply(transcript: TranscriptEvent) {
        transcripts = TranscriptReducer.applying(transcript, to: transcripts)
    }

    private func applySynchronizedAssistantTranscript(_ segments: [TranscriptionSegment]) {
        guard !awaitingServerGenerationAfterReconnect, !segments.isEmpty else { return }
        let text = segments.map(\.text).joined()
        guard !text.isEmpty else { return }
        apply(
            transcript: TranscriptEvent(
                type: "transcript_delta",
                speaker: .assistant,
                text: text,
                isFinal: segments.allSatisfy(\.isFinal),
                heard: true,
                turnID: activeTurnID,
                generationID: generationFence.latest
            )
        )
    }

    private func setRemoteAudioSubscribed(_ subscribed: Bool, in room: Room) async {
        let publications = room.remoteParticipants.values.flatMap(\.audioTracks)
            .compactMap { $0 as? RemoteTrackPublication }
        for publication in publications {
            try? await publication.set(subscribed: subscribed)
        }
    }

    private func scheduleReconnectTimeout() {
        cancelReconnectTimeout()
        reconnectTask = Task { [weak self] in
            do {
                try await Task.sleep(for: .seconds(10))
            } catch {
                return
            }
            guard let self, connectionPhase == .reconnecting else { return }
            connectionPhase = .failed
            errorMessage = "连接超时，请重新连接"
            await disconnectLiveKit()
        }
    }

    private func cancelReconnectTimeout() {
        reconnectTask?.cancel()
        reconnectTask = nil
    }

    nonisolated func room(
        _ room: Room,
        didUpdateConnectionState connectionState: ConnectionState,
        from oldConnectionState: ConnectionState
    ) {
        Task { @MainActor [weak self] in
            guard let self else { return }
            switch connectionState {
            case .connecting:
                connectionPhase = .connecting
            case .connected:
                if oldConnectionState == .reconnecting {
                    guard let controlAPI, let sessionID else {
                        connectionPhase = .failed
                        errorMessage = "重连同步失败，请重新连接"
                        await disconnectLiveKit()
                        return
                    }
                    do {
                        try await controlAPI.notifyRTCRecovered(sessionID: sessionID)
                        generationFence.advance()
                        awaitingServerGenerationAfterReconnect = true
                        await setRemoteAudioSubscribed(true, in: room)
                    } catch {
                        connectionPhase = .failed
                        errorMessage = "重连同步失败：\(error.localizedDescription)"
                        await disconnectLiveKit()
                        return
                    }
                }
                cancelReconnectTimeout()
                connectionPhase = .connected
                errorMessage = nil
            case .reconnecting:
                connectionPhase = .reconnecting
                agentStatus = .recovering
                scheduleReconnectTimeout()
                await setRemoteAudioSubscribed(false, in: room)
            case .disconnected:
                cancelReconnectTimeout()
                guard !ending, connectionPhase != .failed else { return }
                connectionPhase = .failed
                errorMessage = "语音房间连接已断开，请重新连接"
            case .disconnecting:
                break
            }
        }
    }

    nonisolated func room(
        _ room: Room,
        participant: RemoteParticipant?,
        didReceiveData data: Data,
        forTopic topic: String,
        encryptionType: EncryptionType
    ) {
        guard topic == VoiceEventDecoder.topic, participant?.kind == .agent else { return }
        Task { @MainActor [weak self] in
            self?.apply(data: data, topic: topic)
        }
    }

    nonisolated func room(
        _ room: Room,
        participant: Participant,
        trackPublication: TrackPublication,
        didReceiveTranscriptionSegments segments: [TranscriptionSegment]
    ) {
        guard participant.kind == .agent else { return }
        Task { @MainActor [weak self] in
            self?.applySynchronizedAssistantTranscript(segments)
        }
    }
}
