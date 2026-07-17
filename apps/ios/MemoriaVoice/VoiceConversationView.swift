import SwiftUI

struct VoiceConversationView: View {
    let store: VoiceSessionStore

    @Environment(\.scenePhase) private var scenePhase
    @AppStorage("controlAPIBaseURL") private var controlAPIBaseURL = "http://127.0.0.1:8000"
    @State private var draft = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                connectionBanner
                transcriptContent
            }
            .safeAreaInset(edge: .bottom) {
                VStack(spacing: 12) {
                    if store.isActive {
                        sessionControls
                    } else {
                        connectionControls
                    }
                    if store.canSendText {
                        textInput
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .background(.ultraThinMaterial)
            }
            .navigationTitle("Memoria")
            .navigationBarTitleDisplayMode(.inline)
            .onChange(of: scenePhase) { _, phase in
                guard phase == .active else { return }
                Task { await store.resumeAudioIfNeeded() }
            }
        }
    }

    private var connectionBanner: some View {
        VStack(spacing: 8) {
            HStack(spacing: 10) {
                Image(systemName: store.agentStatus.symbol)
                    .symbolEffect(.pulse, isActive: store.agentStatus == .listening || store.agentStatus == .thinking)
                    .foregroundStyle(statusColor)
                VStack(alignment: .leading, spacing: 2) {
                    Text(store.agentStatus.label)
                        .font(.headline)
                    Text(store.connectionPhase.label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if store.connectionPhase == .creatingSession ||
                    store.connectionPhase == .connecting ||
                    store.connectionPhase == .reconnecting
                {
                    ProgressView()
                }
            }

            if let error = store.errorMessage {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle.fill")
                    Text(error)
                        .font(.footnote)
                    Spacer()
                    Button("关闭") { store.dismissError() }
                        .font(.footnote)
                }
                .foregroundStyle(.red)
                .accessibilityIdentifier("error-banner")
            }
        }
        .padding(16)
        .background(Color(uiColor: .secondarySystemBackground))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("connection-banner")
    }

    @ViewBuilder
    private var transcriptContent: some View {
        if store.transcripts.isEmpty {
            ContentUnavailableView {
                Label("开始一段对话", systemImage: "waveform.and.mic")
            } description: {
                Text("连接后直接说话，也可以发送文字。助手回答时麦克风仍会保持工作。")
            }
            .accessibilityIdentifier("empty-transcript")
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(store.transcripts) { line in
                            TranscriptBubble(line: line)
                                .id(line.id)
                        }
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: store.transcripts) { _, lines in
                    guard let last = lines.last else { return }
                    withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                }
                .accessibilityIdentifier("transcript-list")
            }
        }
    }

    private var connectionControls: some View {
        VStack(spacing: 10) {
            TextField("控制 API 地址", text: $controlAPIBaseURL)
                .textInputAutocapitalization(.never)
                .keyboardType(.URL)
                .autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)
                .accessibilityIdentifier("control-api-url")

            Button {
                inputFocused = false
                Task { await store.start(baseURLText: controlAPIBaseURL) }
            } label: {
                Label("开始语音会话", systemImage: "mic.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .accessibilityIdentifier("start-session")
        }
    }

    private var sessionControls: some View {
        HStack(spacing: 12) {
            Button {
                Task { await store.toggleMicrophone() }
            } label: {
                Label(
                    store.micEnabled ? "静音" : "开启麦克风",
                    systemImage: store.micEnabled ? "mic.fill" : "mic.slash.fill"
                )
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .accessibilityIdentifier("toggle-microphone")

            Button(role: .destructive) {
                Task { await store.stopResponse() }
            } label: {
                Label("停止回答", systemImage: "stop.fill")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!store.canStopResponse)
            .accessibilityIdentifier("stop-response")

            Button(role: .destructive) {
                Task { await store.end() }
            } label: {
                Image(systemName: "phone.down.fill")
                    .frame(minWidth: 34)
            }
            .buttonStyle(.bordered)
            .accessibilityLabel("结束会话")
            .accessibilityIdentifier("end-session")
        }
    }

    private var textInput: some View {
        HStack(spacing: 10) {
            TextField("输入文字消息", text: $draft, axis: .vertical)
                .lineLimit(1 ... 4)
                .textFieldStyle(.roundedBorder)
                .focused($inputFocused)
                .submitLabel(.send)
                .onSubmit { sendDraft() }
                .accessibilityIdentifier("message-input")

            Button(action: sendDraft) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
            }
            .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel("发送")
            .accessibilityIdentifier("send-message")
        }
    }

    private var statusColor: Color {
        switch store.agentStatus {
        case .speaking: .blue
        case .listening: .green
        case .thinking, .toolWaiting: .orange
        case .interrupted, .closed: .red
        case .recovering: .yellow
        case .ready: .primary
        }
    }

    private func sendDraft() {
        let text = draft
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        Task {
            if await store.send(text: text) {
                draft = ""
            }
        }
    }
}

private struct TranscriptBubble: View {
    let line: TranscriptLine

    var body: some View {
        HStack {
            if line.speaker == .assistant {
                bubble
                Spacer(minLength: 44)
            } else {
                Spacer(minLength: 44)
                bubble
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(speakerLabel)：\(line.text)")
    }

    private var bubble: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(speakerLabel)
                .font(.caption.bold())
                .foregroundStyle(.secondary)
            Text(line.text)
                .font(.body)
            if !line.isFinal {
                Text(line.heard == true ? "播放中" : "转写中")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .background(
            line.speaker == .assistant
                ? Color(uiColor: .secondarySystemBackground)
                : Color.accentColor.opacity(0.16),
            in: RoundedRectangle(cornerRadius: 16, style: .continuous)
        )
    }

    private var speakerLabel: String {
        line.speaker == .assistant ? "助手" : "我"
    }
}

#Preview {
    VoiceConversationView(store: VoiceSessionStore())
}
