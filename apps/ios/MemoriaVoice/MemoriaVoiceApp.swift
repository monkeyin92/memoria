import SwiftUI

@main
struct MemoriaVoiceApp: App {
    @State private var voiceSession = VoiceSessionStore()

    var body: some Scene {
        WindowGroup {
            VoiceConversationView(store: voiceSession)
        }
    }
}
