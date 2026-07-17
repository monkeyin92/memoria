import Foundation

struct CreateVoiceSessionResponse: Decodable, Sendable {
    let sessionID: String
    let liveKitURL: URL
    let roomName: String
    let participantToken: String
    let expiresIn: Int
    let agentName: String

    enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case liveKitURL = "livekit_url"
        case roomName = "room_name"
        case participantToken = "participant_token"
        case expiresIn = "expires_in"
        case agentName = "agent_name"
    }
}

struct ControlAPIClient: Sendable {
    enum ClientError: LocalizedError {
        case invalidBaseURL
        case invalidResponse
        case server(status: Int, message: String)

        var errorDescription: String? {
            switch self {
            case .invalidBaseURL:
                "控制 API 地址无效"
            case .invalidResponse:
                "控制 API 返回了无法识别的数据"
            case let .server(status, message):
                "控制 API 请求失败（\(status)）：\(message)"
            }
        }
    }

    private struct CreateRequest: Encodable {
        struct Client: Encodable {
            let platform: String
            let timezone: String
        }

        let userID: String
        let locale: String
        let client: Client

        enum CodingKeys: String, CodingKey {
            case userID = "user_id"
            case locale
            case client
        }
    }

    private struct StopRequest: Encodable {
        let reason: String
    }

    private struct EmptyRequest: Encodable {}

    let baseURL: URL
    var urlSession: URLSession = .shared

    init(baseURLText: String, urlSession: URLSession = .shared) throws {
        let trimmed = baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: trimmed),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              url.host != nil
        else {
            throw ClientError.invalidBaseURL
        }
        baseURL = url
        self.urlSession = urlSession
    }

    func createSession() async throws -> CreateVoiceSessionResponse {
        let payload = CreateRequest(
            userID: "ios-\(UUID().uuidString.lowercased())",
            locale: "zh-CN",
            client: .init(
                platform: "ios",
                timezone: TimeZone.current.identifier
            )
        )
        return try await send(
            path: ["v1", "sessions"],
            body: payload,
            response: CreateVoiceSessionResponse.self
        )
    }

    func stopResponse(sessionID: String) async throws {
        let _: EmptyResponse = try await send(
            path: ["v1", "sessions", sessionID, "stop-response"],
            body: StopRequest(reason: "user_button"),
            response: EmptyResponse.self
        )
    }

    func notifyRTCRecovered(sessionID: String) async throws {
        let _: EmptyResponse = try await send(
            path: ["v1", "sessions", sessionID, "rtc-recovered"],
            body: EmptyRequest(),
            response: EmptyResponse.self
        )
    }

    private func send<RequestBody: Encodable, ResponseBody: Decodable>(
        path: [String],
        body: RequestBody,
        response: ResponseBody.Type
    ) async throws -> ResponseBody {
        var url = baseURL
        for component in path {
            url.append(path: component)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 10
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)

        let (data, rawResponse) = try await urlSession.data(for: request)
        guard let httpResponse = rawResponse as? HTTPURLResponse else {
            throw ClientError.invalidResponse
        }
        guard (200 ..< 300).contains(httpResponse.statusCode) else {
            let detail = Self.errorDetail(from: data)
            throw ClientError.server(status: httpResponse.statusCode, message: detail)
        }
        do {
            return try JSONDecoder().decode(response, from: data)
        } catch {
            throw ClientError.invalidResponse
        }
    }

    private static func errorDetail(from data: Data) -> String {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let detail = object["detail"] as? String
        else {
            return "未知错误"
        }
        return detail
    }
}

private struct EmptyResponse: Decodable {
    init(from decoder: Decoder) throws {
        _ = try decoder.singleValueContainer()
    }
}
