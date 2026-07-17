import Foundation

enum VoiceUIEvent: Equatable, Sendable {
    case assistantState(AssistantStateEvent)
    case transcript(TranscriptEvent)
}

struct AssistantStateEvent: Decodable, Equatable, Sendable {
    let type: String
    let sessionID: String
    let state: String
    let turnID: Int
    let generationID: Int
    let at: String

    enum CodingKeys: String, CodingKey {
        case type
        case sessionID = "session_id"
        case state
        case turnID = "turn_id"
        case generationID = "generation_id"
        case at
    }
}

struct TranscriptEvent: Decodable, Equatable, Sendable {
    enum Speaker: String, Decodable, Sendable {
        case user
        case assistant
    }

    let type: String
    let speaker: Speaker
    let text: String
    let isFinal: Bool
    let heard: Bool?
    let turnID: Int
    let generationID: Int

    enum CodingKeys: String, CodingKey {
        case type
        case speaker
        case text
        case isFinal = "final"
        case heard
        case turnID = "turn_id"
        case generationID = "generation_id"
    }
}

enum VoiceEventDecoder {
    static let topic = "voice-agent.ui"

    private struct Discriminator: Decodable {
        let type: String
    }

    static func decode(_ data: Data, topic: String) throws -> VoiceUIEvent {
        guard topic == self.topic else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: [], debugDescription: "Unsupported LiveKit data topic")
            )
        }
        let decoder = JSONDecoder()
        let event: VoiceUIEvent
        switch try decoder.decode(Discriminator.self, from: data).type {
        case "assistant_state":
            event = .assistantState(try decoder.decode(AssistantStateEvent.self, from: data))
        case "transcript_delta":
            event = .transcript(try decoder.decode(TranscriptEvent.self, from: data))
        default:
            throw DecodingError.dataCorrupted(
                .init(codingPath: [], debugDescription: "Unsupported voice UI event")
            )
        }
        guard event.turnID >= 0, event.generationID >= 0 else {
            throw DecodingError.dataCorrupted(
                .init(codingPath: [], debugDescription: "Negative event sequence")
            )
        }
        return event
    }
}

private extension VoiceUIEvent {
    var turnID: Int {
        switch self {
        case let .assistantState(event): event.turnID
        case let .transcript(event): event.turnID
        }
    }

    var generationID: Int {
        switch self {
        case let .assistantState(event): event.generationID
        case let .transcript(event): event.generationID
        }
    }
}

struct ClientGenerationFence: Equatable, Sendable {
    private(set) var latest = 0

    mutating func accepts(_ generationID: Int) -> Bool {
        guard generationID >= latest else { return false }
        latest = generationID
        return true
    }

    mutating func reset() {
        latest = 0
    }

    mutating func advance() {
        latest += 1
    }
}

struct TranscriptLine: Identifiable, Equatable, Sendable {
    let speaker: TranscriptEvent.Speaker
    var text: String
    var isFinal: Bool
    var heard: Bool?
    let turnID: Int
    let generationID: Int

    var id: String {
        "\(speaker.rawValue)-\(turnID)-\(generationID)"
    }
}

enum TranscriptReducer {
    static func applying(_ event: TranscriptEvent, to lines: [TranscriptLine]) -> [TranscriptLine] {
        guard event.speaker == .user || event.heard == true else { return lines }
        let line = TranscriptLine(
            speaker: event.speaker,
            text: event.text,
            isFinal: event.isFinal,
            heard: event.heard,
            turnID: event.turnID,
            generationID: event.generationID
        )
        var result = lines
        if let index = result.lastIndex(where: { $0.id == line.id }),
           !result[index].isFinal || (line.speaker == .assistant && line.heard == true)
        {
            result[index] = line
        } else {
            result.append(line)
        }
        return result
    }
}
