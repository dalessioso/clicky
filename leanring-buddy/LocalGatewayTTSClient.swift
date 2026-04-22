//
//  LocalGatewayTTSClient.swift
//  leanring-buddy
//
//  Localhost-only text-to-speech client. The Swift app sends speech text to
//  the Python gateway on 127.0.0.1:5000 and plays the returned audio bytes.
//

import AVFoundation
import Foundation

@MainActor
final class LocalGatewayTTSClient {
    private struct LocalGatewayTTSRequest: Encodable {
        let text: String
    }

    private let ttsURL = AppBundleConfiguration.localGatewayURL(path: "/tts")
    private let session: URLSession

    private var audioPlayer: AVAudioPlayer?

    init() {
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 30
        configuration.timeoutIntervalForResource = 60
        configuration.waitsForConnectivity = false
        configuration.urlCache = nil
        configuration.httpCookieStorage = nil
        self.session = URLSession(configuration: configuration)
    }

    func speakText(_ text: String) async throws {
        var request = URLRequest(url: ttsURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("audio/*", forHTTPHeaderField: "Accept")
        request.httpBody = try JSONEncoder().encode(LocalGatewayTTSRequest(text: text))

        let (data, response) = try await session.data(for: request)

        guard let httpResponse = response as? HTTPURLResponse else {
            throw NSError(
                domain: "LocalGatewayTTSClient",
                code: -1,
                userInfo: [NSLocalizedDescriptionKey: "The local TTS gateway returned an invalid response."]
            )
        }

        guard (200...299).contains(httpResponse.statusCode) else {
            throw NSError(
                domain: "LocalGatewayTTSClient",
                code: httpResponse.statusCode,
                userInfo: [
                    NSLocalizedDescriptionKey: AppBundleConfiguration.localGatewayFailureMessage(
                        operationDescription: "Local TTS",
                        httpResponse: httpResponse,
                        responseData: data
                    )
                ]
            )
        }

        try Task.checkCancellation()

        let player = try AVAudioPlayer(data: data)
        self.audioPlayer = player
        player.play()
    }

    var isPlaying: Bool {
        audioPlayer?.isPlaying ?? false
    }

    func stopPlayback() {
        audioPlayer?.stop()
        audioPlayer = nil
    }
}
