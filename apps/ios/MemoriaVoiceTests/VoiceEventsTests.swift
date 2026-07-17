import Foundation
import Testing
@testable import MemoriaVoice

struct VoiceEventsTests {
    @Test func decodesAssistantState() throws {
        let data = Data(#"{"type":"assistant_state","session_id":"s-1","state":"speaking","turn_id":3,"generation_id":8,"at":"2026-07-15T08:00:00Z"}"#.utf8)

        let event = try VoiceEventDecoder.decode(data, topic: VoiceEventDecoder.topic)

        #expect(event == .assistantState(.init(
            type: "assistant_state",
            sessionID: "s-1",
            state: "speaking",
            turnID: 3,
            generationID: 8,
            at: "2026-07-15T08:00:00Z"
        )))
    }

    @Test func decodesHeardTranscript() throws {
        let data = Data(#"{"type":"transcript_delta","speaker":"assistant","text":"已经播出的文本","final":true,"heard":true,"turn_id":4,"generation_id":9}"#.utf8)

        let event = try VoiceEventDecoder.decode(data, topic: VoiceEventDecoder.topic)

        #expect(event == .transcript(.init(
            type: "transcript_delta",
            speaker: .assistant,
            text: "已经播出的文本",
            isFinal: true,
            heard: true,
            turnID: 4,
            generationID: 9
        )))
    }

    @Test func rejectsUnknownEvent() {
        let data = Data(#"{"type":"tool_result"}"#.utf8)
        #expect(throws: DecodingError.self) {
            try VoiceEventDecoder.decode(data, topic: VoiceEventDecoder.topic)
        }
    }

    @Test func rejectsWrongTopicAndNegativeSequence() {
        let valid = Data(#"{"type":"transcript_delta","speaker":"user","text":"你好","final":true,"turn_id":1,"generation_id":1}"#.utf8)
        #expect(throws: DecodingError.self) {
            try VoiceEventDecoder.decode(valid, topic: "other")
        }

        let negative = Data(#"{"type":"transcript_delta","speaker":"user","text":"你好","final":true,"turn_id":-1,"generation_id":1}"#.utf8)
        #expect(throws: DecodingError.self) {
            try VoiceEventDecoder.decode(negative, topic: VoiceEventDecoder.topic)
        }
    }

    @Test func rejectsLowerGeneration() {
        var fence = ClientGenerationFence()

        let acceptsFirst = fence.accepts(5)
        let acceptsSame = fence.accepts(5)
        let acceptsOlder = fence.accepts(4)
        #expect(acceptsFirst)
        #expect(acceptsSame)
        #expect(!acceptsOlder)
        #expect(fence.latest == 5)
        let acceptsNewer = fence.accepts(6)
        #expect(acceptsNewer)
        #expect(fence.latest == 6)
    }

    @Test func advancesFenceAfterRTCRecovery() {
        var fence = ClientGenerationFence()
        let acceptsInitial = fence.accepts(7)
        #expect(acceptsInitial)

        fence.advance()

        #expect(fence.latest == 8)
        let acceptsStale = fence.accepts(7)
        let acceptsCurrent = fence.accepts(8)
        #expect(!acceptsStale)
        #expect(acceptsCurrent)
    }

    @Test func transcriptReducerShowsOnlyHeardAssistantText() {
        let interim = TranscriptEvent(
            type: "transcript_delta",
            speaker: .assistant,
            text: "完整生成文本",
            isFinal: false,
            heard: false,
            turnID: 2,
            generationID: 3
        )
        let heard = TranscriptEvent(
            type: "transcript_delta",
            speaker: .assistant,
            text: "实际听到",
            isFinal: true,
            heard: true,
            turnID: 2,
            generationID: 3
        )

        let unheardLines = TranscriptReducer.applying(interim, to: [])
        let lines = TranscriptReducer.applying(heard, to: unheardLines)

        #expect(unheardLines.isEmpty)
        #expect(lines.count == 1)
        #expect(lines[0].text == "实际听到")
        #expect(lines[0].heard == true)
    }
}
