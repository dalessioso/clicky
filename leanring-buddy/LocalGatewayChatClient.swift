//
//  LocalGatewayChatClient.swift
//  leanring-buddy
//
//  Localhost-only chat client. All assistant requests are sent to the Python
//  gateway running on 127.0.0.1:5000, which decides whether to use local models
//  or user-supplied cloud providers.
//

import Foundation

struct LocalGatewayPointTarget: Decodable {
    let x: Double
    let y: Double
    let label: String?
    let screenIndex: Int?

    enum CodingKeys: String, CodingKey {
        case x
        case y
        case label
        case screenIndex = "screen_index"
    }
}

struct LocalGatewayChatResponse: Decodable {
    let spokenSummary: String
    let detailedText: String
    let pointTarget: LocalGatewayPointTarget?

    enum CodingKeys: String, CodingKey {
        case spokenSummary = "spoken_summary"
        case detailedText = "detailed_text"
        case pointTarget = "point_target"
    }
}

final class LocalGatewayChatClient {
    private struct LocalGatewayChatRequest: Encodable {
        struct ConversationHistoryEntry: Encodable {
            let userPrompt: String
            let assistantResponse: String

            enum CodingKeys: String, CodingKey {
                case userPrompt = "user_prompt"
                case assistantResponse = "assistant_response"
            }
        }

        struct ScreenCapturePayload: Encodable {
            let label: String
            let mimeType: String
            let imageBase64: String
            let screenshotWidthInPixels: Int
            let screenshotHeightInPixels: Int
            let displayWidthInPoints: Int
            let displayHeightInPoints: Int
            let isCursorScreen: Bool

            enum CodingKeys: String, CodingKey {
                case label
                case mimeType = "mime_type"
                case imageBase64 = "image_base64"
                case screenshotWidthInPixels = "screenshot_width_in_pixels"
                case screenshotHeightInPixels = "screenshot_height_in_pixels"
                case displayWidthInPoints = "display_width_in_points"
                case displayHeightInPoints = "display_height_in_points"
                case isCursorScreen = "is_cursor_screen"
            }
        }

        let transcript: String
        let conversationHistory: [ConversationHistoryEntry]
        let screenCaptures: [ScreenCapturePayload]
        let supportsPointing: Bool
        let requestedResponseFormat: String

        enum CodingKeys: String, CodingKey {
            case transcript
            case conversationHistory = "conversation_history"
            case screenCaptures = "screen_captures"
            case supportsPointing = "supports_pointing"
            case requestedResponseFormat = "requested_response_format"
        }
    }

    struct RequestScreenCapture {
        let label: String
        let imageData: Data
        let isCursorScreen: Bool
        let screenshotWidthInPixels: Int
        let screenshotHeightInPixels: Int
        let displayWidthInPoints: Int
        let displayHeightInPoints: Int
    }

    struct ConversationHistoryEntry {
        let userPrompt: String
        let assistantResponse: String
    }

    private let chatURL = AppBundleConfiguration.localGatewayURL(path: "/chat")
    private let session: URLSession

    init() {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 120
        configuration.timeoutIntervalForResource = 300
        configuration.waitsForConnectivity = false
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        self.session = URLSession(configuration: configuration)
    }

    func sendChatRequest(
        transcript: String,
        conversationHistory: [ConversationHistoryEntry],
        screenCaptures: [RequestScreenCapture]
    ) async throws -> LocalGatewayChatResponse {
        var request = URLRequest(url: chatURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 120
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let chatRequest = LocalGatewayChatRequest(
            transcript: transcript,
            conversationHistory: conversationHistory.map {
                LocalGatewayChatRequest.ConversationHistoryEntry(
                    userPrompt: $0.userPrompt,
                    assistantResponse: $0.assistantResponse
                )
            },
            screenCaptures: screenCaptures.map {
                LocalGatewayChatRequest.ScreenCapturePayload(
                    label: $0.label,
                    mimeType: detectImageMediaType(for: $0.imageData),
                    imageBase64: $0.imageData.base64EncodedString(),
                    screenshotWidthInPixels: $0.screenshotWidthInPixels,
                    screenshotHeightInPixels: $0.screenshotHeightInPixels,
                    displayWidthInPoints: $0.displayWidthInPoints,
                    displayHeightInPoints: $0.displayHeightInPoints,
                    isCursorScreen: $0.isCursorScreen
                )
            },
            supportsPointing: true,
            requestedResponseFormat: "dual_channel"
        )

        request.httpBody = try JSONEncoder().encode(chatRequest)

        let (data, response) = try await session.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw NSError(
                domain: "LocalGatewayChatClient",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "The local chat gateway returned an invalid response."]
            )
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw NSError(
                domain: "LocalGatewayChatClient",
                code: httpResponse.statusCode,
                userInfo: [
                    NSLocalizedDescriptionKey: AppBundleConfiguration.localGatewayFailureMessage(
                        operationDescription: "Local chat",
                        httpResponse: httpResponse,
                        responseData: data
                    )
                ]
            )
        }

        return try JSONDecoder().decode(LocalGatewayChatResponse.self, from: data)
    }

    private func detectImageMediaType(for imageData: Data) -> String {
        if imageData.count >= 4 {
            let pngSignature: [UInt8] = [0x89, 0x50, 0x4E, 0x47]
            let firstFourBytes = [UInt8](imageData.prefix(4))
            if firstFourBytes == pngSignature {
                return "image/png"
            }
        }

        return "image/jpeg"
    }
}
